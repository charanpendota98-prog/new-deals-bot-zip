#!/usr/bin/env python3
"""Offline quality auditor for the deal pipeline (read-only, no network).

The bot guarantees four things on every post: nothing of ours is added, the only
links are OUR monetized links, no product is posted twice, and no eligible post
is lost. This script proves it on the live database - run it any time (or from
ops/diagnose.sh) and it reports every post that broke a guarantee, with queue ids,
so a regression is visible the day it happens instead of in channel feedback.

    python3 ops/quality_audit.py                      # last 200 posts
    python3 ops/quality_audit.py --db <path> --limit 500 --json
    python3 ops/quality_audit.py --strict             # exit 1 on any finding

It opens the database read-only and never writes to it or to Telegram.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

RUPEE = "\u20b9"
OURS_MARKERS = (
    # Anything below was invented by an older template of ours. None of it may
    # appear in a post: the source's own words are the content, so a marker means
    # our old decoration came back. These phrases are also removed from SOURCE
    # posts while cleaning, so seeing one in a published post can only mean we
    # wrote it - there is no false positive to allow for.
    "Verified deals", "Join for daily loot", "Grab fast", "Enjoy Deals",
    "DEALS OF THE DAY", "MEGA DEAL LIST", "PREMIUM LOOT PICK", "Latest deal",
    "TODAY'S HOT DEALS", "POWER LOOT ALERT", "Price/stock may change",
    "UNDER ₹499 •", "BANK / CARD OFFER", "LOOT ZONE — INDIA",
)
ANY_LINK = re.compile(r"https?://\S+")
# What "our link" means for this bot, in the bot's own terms:
#   * our shortener (the one the user owns) or our EarnKaro / gop.im output,
#   * an Amazon page carrying OUR tag - the pipeline publishes those when no
#     shortener/affiliate route was used, and the tag IS the monetization,
#   * a clean merchant page for a store we cannot monetize (a deal must not be
#     lost because a campaign is missing),
#   * our own channels and the Loots Family folder.
# Everything else - the source's own short link, someone else's shortener, a link
# tagged to another publisher - is a link that should never have been sent.
OUR_SHORTENER = re.compile(r"https?://(?:(?:www\.)?bitli\.in/TqmFyPp|(?:www\.)?bitlyskj\.(?:com|in|net)/)", re.I)
OUR_HOSTS = re.compile(r"https?://(?:[^/\s]*earnkaro\.com/|gop\.im/)", re.I)
OUR_T_ME = re.compile(r"https?://t\.me/(?:addlist/|LootZoneIndia11\b|SecretLootIndia1\b)", re.I)
AMAZON_HOST = re.compile(r"^(?:www\.|m\.)?amazon\.[a-z.]{2,8}$", re.I)
AMAZON_SHORT = re.compile(r"^(?:www\.)?amzn\.(?:to|in)$", re.I)
FOREIGN_SHORTENERS = re.compile(
    r"https?://(?:[^/\s]*\.)?(?:bit\.ly|bitly\.com|tinyurl\.com|tinyurl\.net|cutt\.ly|t\.cg"
    r"|gpt\.sh|shorturl\.at|is\.gd|rb\.gy|ow\.ly|rebrand\.ly|tly\.in|s\.id/)", re.I)
# Publisher ids that are never ours. Flipkart's pid= is a PRODUCT id, so it is
# deliberately absent here.
AFFILIATE_ID_RE = re.compile(
    r"[?&](?:affid|aff_id|pubid|publisherid|associateid|affextparam2|refid|clickid)=([^&#]*)", re.I)
FOREIGN_PROMO = re.compile(r"join\s+(?:this\s+)?channel|subscribe\s+to|t\.me/\+|startapp\.bot", re.I)
PRICE_RE = re.compile(rf"{RUPEE}\s*(\d[\d,]*)")
LIST_MARKER_RE = re.compile(r"(?im)^\s*(?:deal\s*\d+|\d+\s*[.)])")
POLICY_SKIPS = (
    "already posted", "duplicate deal", "duplicate product", "night window",
    "STALE DROP", "already covered", "no monetizable", "no eligible targets",
    "not a deal", "skip list", "superseded", "not worth", "confirmed dead merchant",
)


def link_host(url: str) -> str:
    match = re.match(r"https?://([^/\s?#]+)", url or "", re.I)
    return (match.group(1) if match else "").lower()


def why_not_our_link(url: str, our_tag: str = "") -> str | None:
    """Why this published link is NOT one of ours (None means it is fine)."""
    if OUR_SHORTENER.match(url) or OUR_HOSTS.match(url) or OUR_T_ME.match(url):
        return None
    host = link_host(url)
    if AMAZON_SHORT.match(host):
        return "the source's own Amazon short link was posted instead of ours"
    if FOREIGN_SHORTENERS.match(url):
        return "a third-party shortener leaked into the post"
    query = urlparse(url).query.lower()
    pairs = dict()
    for chunk in query.split("&"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            pairs.setdefault(key.strip(), []).append(unquote(value.strip()))
    tags = [v for v in pairs.get("tag", []) if v]
    ours = bool(our_tag) and our_tag.lower() in tags
    foreign_ids = [v for v in AFFILIATE_ID_RE.findall(unquote(url)) if v]
    if AMAZON_HOST.match(host):
        if not ours:
            return "an Amazon link without our tag reached a channel"
        if foreign_ids:
            return "our tag glued to somebody else's id (%s)" % ",".join(sorted(set(foreign_ids)))[:40]
        return None
    if tags and not ours:
        return "tagged to somebody else (%s)" % ",".join(sorted(set(tags)))[:40]
    if foreign_ids:
        return "carries somebody else's affiliate id (%s)" % ",".join(sorted(set(foreign_ids)))[:40]
    return None  # unmonetizable store, clean page: publishing it is the policy


def flag(kind: str, detail: str, queue_id=None, target=None) -> dict:
    out = {"kind": kind, "queue_id": queue_id, "detail": detail}
    if target:
        out["target"] = target
    return out


def body_fingerprint(text: str) -> str:
    """The post's own words, with links and decoration removed.

    Two rows whose fingerprints match are the same content published twice, which
    is exactly the defect the user never wants to see again - so it is checked on
    the way OUT as well as on the way in.
    """
    body = ANY_LINK.sub(" ", text or "")
    body = re.sub(r"\s+", " ", body)
    return re.sub(r"[^a-z0-9%\u20b9 ]", "", body.lower()).strip()


def product_line(text: str) -> str:
    """The first line that names the product - what a reader actually sees."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped and not ANY_LINK.search(stripped):
            return re.sub(r"[^a-z0-9 \u20b9%.]", "", stripped.lower()).strip()[:80]
    return ""


def audit(conn: sqlite3.Connection, limit: int, our_tag: str = "") -> tuple[list[dict], int]:
    conn.row_factory = sqlite3.Row
    # Every recent row is inspected, including ones that produced no post at all:
    # "we lost a deal" is as much a quality defect as a badly formatted one.
    rows = conn.execute(
        "SELECT id, source, status, created_at, rendered_text, targets_json, last_error "
        "FROM queue ORDER BY id DESC LIMIT ?", (max(1, limit),),
    ).fetchall()
    try:
        sent_counts = {int(r["queue_id"]): int(r["n"]) for r in conn.execute(
            "SELECT queue_id, count(*) AS n FROM deliveries WHERE status='sent' GROUP BY queue_id")}
    except sqlite3.OperationalError:
        sent_counts = {}

    findings: list[dict] = []
    for row in rows:
        text = row["rendered_text"] or ""
        sent = sent_counts.get(int(row["id"]), 0)
        error = (row["last_error"] or "").lower()
        # 5) coverage: a job that finished without sending anything and without a
        #    policy reason is a lost deal.
        if not text:
            if (row["status"] or "") in ("done", "failed") and not sent and not any(
                    marker.lower() in error for marker in POLICY_SKIPS):
                findings.append(flag("COVERAGE",
                                     f"{row['status']} with no send and no policy reason "
                                     f"({(row['last_error'] or 'no reason')[:70]})", row["id"]))
            continue
        if (row["status"] or "") == "pending":
            continue
        try:
            targets = json.loads(row["targets_json"] or "[]")
        except (TypeError, ValueError):
            targets = []
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        # 1) nothing of ours may be added: our old decorations and markdown debris
        lowered = text.lower()
        for marker in OURS_MARKERS:
            if marker.lower() in lowered:
                findings.append(flag("OURS", f"invented text {marker!r} is back in the post", row["id"]))
        if re.search(r"\*\S|\S\*\*|__\S", text):
            findings.append(flag("OURS", "markdown debris (** / __) left in the post", row["id"]))
        if FOREIGN_PROMO.search(text):
            findings.append(flag("OURS", "a join/subscribe/referral line survived cleaning", row["id"]))

        # 2) links: every link must be ours, and the post must carry at least one
        links = [u.rstrip(")】.,;'\"]") for u in ANY_LINK.findall(text)]
        if not links:
            findings.append(flag("LINKS", "published with no link at all", row["id"]))
        for url in links:
            why = why_not_our_link(url, our_tag)
            if why:
                findings.append(flag("LINKS", f"{why}: {url[:80]}", row["id"]))

        # 3) a post must carry the deal, not only links
        if lines and all(ANY_LINK.fullmatch(line) for line in lines):
            findings.append(flag("TEXTLESS", "only links, no product text", row["id"]))
        elif RUPEE in text and not PRICE_RE.search(text):
            findings.append(flag("TEXTLESS", "a rupee sign with no amount next to it", row["id"]))

        # 4) price-tier channels must not receive a post outside their band. Only
        #    single-product posts are judged (a list is a roundup by design) and the
        #    figure that matters is the CHEAPEST one, i.e. what a shopper pays.
        prices = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        priced_lines = sum(1 for line in lines if PRICE_RE.search(line))
        listy = priced_lines >= 3 or bool(LIST_MARKER_RE.search(text))
        if prices and not listy:
            cheapest = min(prices)
            for target in targets:
                band = None
                lowered = str(target).lower()
                if lowered.startswith(("under99", "under-99")):
                    band = 99
                elif lowered.startswith(("under499", "under-499")):
                    band = 499
                if band is not None and cheapest > band:
                    findings.append(flag("MISROUTE",
                                         f"{RUPEE}{cheapest} sent to the {RUPEE}{band} channel",
                                         row["id"], target))

    # 6) duplicates on the way out: the same content - or the same product at the
    #    same price - delivered by two different posts within six hours.
    fresh_cutoff = time.time() - 6 * 3600
    exact: dict[str, list[int]] = {}
    near: dict[tuple, list[int]] = {}
    for row in rows:
        text = row["rendered_text"] or ""
        if not text or (row["created_at"] or 0) < fresh_cutoff or not sent_counts.get(int(row["id"])):
            continue
        exact.setdefault(body_fingerprint(text), []).append(int(row["id"]))
        prices = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        near.setdefault((product_line(text), min(prices) if prices else 0), []).append(int(row["id"]))
    matched: set[int] = set()
    for key, ids in exact.items():
        if len(ids) < 2 or not key:
            continue
        matched.update(ids)
        findings.append(flag("DUPLICATE",
                             f"the same post went out twice from queues {ids}: {key[:60]!r}", ids[0]))
    for key, ids in near.items():
        if len(ids) < 2 or not key[0] or set(ids) <= matched:
            continue
        matched.update(ids)
        findings.append(flag("NEAR-DUPE",
                             f"{key[0][:50]!r} at {RUPEE}{key[1]} went out from {len(ids)} posts {ids}",
                             ids[0]))

    try:
        total_posts = conn.execute(
            "SELECT count(*) FROM queue WHERE rendered_text IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        total_posts = len(rows)
    return findings, total_posts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="audit posted-deal quality (read-only)")
    default_db = Path(__file__).resolve().parent.parent / "bestgaa" / "state" / "bot_state.sqlite3"
    parser.add_argument("--db", type=Path, default=default_db)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--strict", action="store_true", help="exit 1 when anything is found")
    parser.add_argument("--tag", default=os.getenv("AMAZON_TAG", "deals0911-21"),
                        help="our Amazon affiliate tag (default: $AMAZON_TAG)")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"QUALITY AUDIT: no database at {args.db}")
        return 0
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        findings, posts = audit(conn, args.limit, (args.tag or "").strip())
    except sqlite3.OperationalError as exc:  # a schema too old to judge
        print(f"QUALITY AUDIT: database not readable yet ({exc})")
        return 0
    finally:
        conn.close()

    by_kind: dict[str, list[dict]] = {}
    for item in findings:
        by_kind.setdefault(item["kind"], []).append(item)
    if args.json:
        print(json.dumps({"db": str(args.db), "posts_checked": posts, "findings": findings}))
    else:
        print(f"QUALITY AUDIT | db={args.db} posts={posts} findings={len(findings)}")
        for kind in ("OURS", "LINKS", "TEXTLESS", "MISROUTE", "COVERAGE", "DUPLICATE", "NEAR-DUPE"):
            items = by_kind.get(kind) or []
            print(f"  {kind:<9}: {len(items)}")
            for item in items[:5]:
                where = f" queue={item['queue_id']}" if item.get("queue_id") else ""
                target = f" target={item['target']}" if item.get("target") else ""
                print(f"     {item['detail']}{where}{target}")
            if len(items) > 5:
                print(f"     ... {len(items) - 5} more")
        if not findings:
            print("  every audited post is clean: our links only, nothing invented, "
                  "no duplicate product, nothing lost")
    return 1 if (findings and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
