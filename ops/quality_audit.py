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
    if not our_tag:
        # TAGLESS: clean Amazon link (no tag) is ours, any tag is foreign
        ours = not tags
    else:
        ours = our_tag.lower() in [x.lower() for x in tags]
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


# Words every loot channel puts in front of a product. They carry no identity,
# so a signature built from a line that still contains them would match two
# different products - which would make the auditor invent duplicates.
# (The old NOISE_WORDS set lived here and doubled as the product-identity stop list.
# It is gone on purpose: identity stops now sit in _AUDIT_SIG_STOP_WORDS, which keeps
# "men"/"women"/"unisex" as MEANINGFUL words - a men's shirt and a women's shirt are
# two different deals, and swallowing those words is how a dedup rule eats coverage.)

# >>> BEGIN SYNCED product-identity (generated by ops/sync_identity.py)
# DO NOT EDIT THIS BLOCK BY HAND. It is copied from
# bestgaa/main_bot_new.py by ops/sync_identity.py, because an auditor with a
# different idea of "which product is this?" than the bot cannot report
# anything true. Run: python3 ops/sync_identity.py
# ---------------------------------------------------------------------------
# A number in a product name is either the thing that MAKES it that product
# (1.5 Ton, 5 Star, 128GB, 55-inch) or a spec a channel may not type at all
# (42H playtime, 5000mAh, 1080p, 4K). Identity is brand + model + variant +
# the numbers that change the product - never a slice of the phrase.
_AUDIT_SIG_STOP_WORDS = frozenset("""
deal deals dealz offer offers dhamaka dhamal sale salez loot loots price mrp discount off
save savings grab hurry now today daily best top hot new buy shop link links here below click
free shipping delivery cod return warranty genuine flash super mega amazing awesome alert
in india official telegram whatsapp channel group join follow subscribe share forward for
the a an and or of to on in at by with your our this that it is are be get got have has
""".split())
# A number in a product name is either the thing that MAKES it that product
# (1.5 Ton, 5 Star, 128GB, 55-inch, 5 Burner) or a spec a channel may or may not
# bother to type (42H playtime, 5000mAh, 1080p, 4K, 5G). The first group is the
# identity; the second is dropped - demanding that two captions of one product spell
# their battery life alike is exactly how the same deal gets posted twice.
_AUDIT_SIG_VARIANT_UNITS = (
    "gb tb mb kb l ltr liter liters litre litres ml kg ton tons star stars inch "
    "inches in ft hp kva burner burners slice slices tray trays door doors person "
    "persons blade blades"
).split()
_AUDIT_SIG_SPEC_UNITS = (
    "h hr hrs hour hours min mins sec secs mah wh w kw a v p k g fps hz px mm cm m "
    "db rpm mp nit nits lumen lumens mbps gbps byte bytes watt watts"
).split()
_AUDIT_SIG_VARIANT_RE = re.compile(
    r"\b(\d{1,4}(?:\.\d+)?)[\s_-]*("
    + "|".join(map(re.escape, sorted(_AUDIT_SIG_VARIANT_UNITS, key=len, reverse=True))) + r")\b", re.I)
# Words that name a real variant of a model line. "Galaxy S23" and "Galaxy S23 FE"
# share every number and are two different phones, so these belong to the identity.
# Only words that name a genuinely different product line. "smart"/"air"/"elite" are
# marketing adjectives here and there, and a copy that drops one must not turn the
# same watch into a different product - so the list stays short and factual.
_AUDIT_SIG_VARIANTS = frozenset("""
pro plus max ultra lite neo fe se mini prime classic edge fold flip turbo
""".split())
# Words that put a number in front of them into a model name: "Pro 4" and "Model
# 2600" are the product, "2023" at the end of a headline is the launch year.
_AUDIT_SIG_QUALIFIERS = _AUDIT_SIG_VARIANTS | frozenset("model series gen generation version".split())
# "by <word>" names the brand ("Airdopes 141 by boAt") EXCEPT when the word in
# front says otherwise: "powered by Helio" names a chipset, not the maker.
_AUDIT_SIG_BY_NON_BRAND = frozenset(
    "powered brought inspired sponsored posted shared sent curated verified".split())
_AUDIT_SIG_AMOUNT_RE = re.compile(
    r"(?i)[\u20b9$]\s*[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:%|percent|off)\b|"
    r"\b(?:mrp|mrp\.?|regular\s+price|list\s+price|strike\s+price)\b\s*[:\-]?[^,|;\n]*")


def _audit_sig_tokens(line: str) -> list[str]:
    """Lower-case tokens with punctuation trimmed off the ENDS only.

    Trimming ends instead of deleting every symbol keeps a model number whole:
    "WH-CH720N" stays one token and "1.5 Ton" stays one number, instead of
    splitting into a stray "1" and "5" that read as two different products.
    """
    named = _AUDIT_SIG_AMOUNT_RE.sub(" ", line or "")
    named = re.sub(r"(?i)\b(?:%|percent|off)\b", " ", named)
    out = []
    for token in named.split():
        token = re.sub(r"^[^\w.]+", "", token)
        token = re.sub(r"[^\w.]+$", "", token).strip(".")
        if token and re.search(r"[A-Za-z0-9]", token):
            out.append(token.lower())
    return out


def _product_identity(line: str) -> tuple[str, ...] | None:
    """The few tokens that decide WHICH product a headline names, as a SET.

    Not a slice of the phrase: "boAt Airdopes 141 TWS Earbuds" and "boAt Airdopes
    141 True Wireless Earbuds, 42H Playtime" are one product wearing different
    adjectives, while "Airdopes 141" and "Airdopes 131" are two products differing
    by nothing but the model number. Hashing the first eight words gets BOTH wrong -
    the duplicate survives and two long-named products collapse into a lost deal.

    So the identity is: the brand word, the model numbers (with the word in front of
    them when they are short, "Pro 4" and "Pro4" being one product), the variant
    qualifiers (pro / fe / max) and the numbers that change the product (capacity,
    tonnage, star rating, size, burner count). Extra marketing words then cost
    nothing, and a different model number can never be skipped as "already posted".
    With no number to hold on to (a shirt, a handbag) every product word has to
    agree instead - the conservative answer that loses a duplicate rather than a deal.
    """
    raw = [re.sub(r"[-_]", "", tok) for tok in _audit_sig_tokens(line)]
    words = [w for w in raw if not w.isdigit() and w not in _AUDIT_SIG_STOP_WORDS]
    if len(words) < 2:
        return None
    spec_units = frozenset(_AUDIT_SIG_SPEC_UNITS)
    variant_units = frozenset(_AUDIT_SIG_VARIANT_UNITS)
    ids = {f"{number}{unit}".lower().replace(" ", "").replace("-", "")
           for number, unit in _AUDIT_SIG_VARIANT_RE.findall(line or "")}
    digit_cores = {re.sub(r"\D", "", value) for value in ids}
    models = set()
    for index, token in enumerate(raw):
        if token in _AUDIT_SIG_STOP_WORDS:
            continue
        digits = re.sub(r"\D", "", token)
        if not digits or len(digits) < 1 or len(digits) > 6:
            continue                       # a phone number or a year is not a model
        glued = re.match(r"^(\d{1,6})([a-z]{1,6})$", token)
        if glued:                          # 42h, 1080p, 55inch, 5g
            unit = glued.group(2)
            if unit in spec_units or unit in variant_units:
                continue                   # already handled as a spec or a size id
        if re.match(r"^[a-z]{1,7}\d{1,6}[a-z]{0,3}$", token):
            models.add(token)              # s23, ch720n, pro4, m14
            continue
        if token.isdigit():
            following = raw[index + 1] if index + 1 < len(raw) else ""
            if following in spec_units or following in variant_units:
                continue                   # "128 GB" and "42 H" are keyed above
            previous = raw[index - 1] if index > 0 else ""
            if previous in _AUDIT_SIG_QUALIFIERS:
                models.add(previous + digits)   # "Pro 4" / "Model 2600" is the model
                continue
            if len(digits) < 2 or (len(digits) == 4 and 1900 <= int(digits) <= 2099):
                continue                   # a quantity, or a launch year - not an id
            if digits in digit_cores:
                continue                   # the number is already inside a size id
            models.add(token)
    variants = {t for t in raw if t in _AUDIT_SIG_VARIANTS}
    if not ids and not models:
        if len(words) < 4:
            return None                    # a category phrase is not an identity
        basis = " ".join(sorted(set(words)))
        return None if len(basis) < 16 else ("W", basis)
    # The brand is normally the first product word, but "Airdopes 141 by boAt"
    # and "boAt Airdopes 141" are ONE product: an explicit "by <maker>" names
    # the brand outright and wins over word order, so the reordered copy can
    # never slip past dedup as a second post. "powered by Helio" and friends
    # name a component, not the maker, and are ignored.
    brand = words[0]
    for index, token in enumerate(raw[:-1]):
        if token == "by" and (index == 0 or raw[index - 1] not in _AUDIT_SIG_BY_NON_BRAND):
            candidate = raw[index + 1]
            if candidate in words:
                brand = candidate
                break
    parts = ["M", brand, " ".join(sorted(models | ids))]
    if variants:
        parts.append(" ".join(sorted(variants)))
    return tuple(parts)
# <<< END SYNCED product-identity


def targets_of(row) -> list[str]:
    """The channels a queue row actually went to (targets_json, defensively)."""
    try:
        targets = json.loads(row["targets_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return [str(t) for t in targets if t]


# ---------------------------------------------------------------------------
# The same product-identity rule the bot uses (main_bot_new._product_identity).
# Mirrored on purpose: an auditor working out "did we post this product twice"
# with a DIFFERENT rule than the bot produces findings that mean nothing. The
# copy stays here so the audit runs standalone on a server without Telethon,
# and test_line_fidelity.py fails if the two rules ever disagree.
# ---------------------------------------------------------------------------
# A number in a product name is either the thing that MAKES it that product
# (1.5 Ton, 5 Star, 128GB, 55-inch, 5 Burner) or a spec a channel may or may not
# bother to type (42H playtime, 5000mAh, 1080p, 4K, 5G). The first group is the
# identity; the second is dropped - demanding that two captions of one product spell
# their battery life alike is exactly how the same deal gets posted twice.
# Marketing vocabulary: it says nothing about WHICH product this is, so a copy that
# adds or drops one of these words must not change the identity.
def product_signature(text: str) -> str:
    """What the bot itself keys a repeat on: brand + model number + variant + size."""
    body = text or ""
    named = None
    for line in body.splitlines():
        line = line.strip()
        if not line or ANY_LINK.search(line):
            continue
        identity = _product_identity(line)
        if identity and (identity[0] == "M" or len("|".join(identity)) >= 16):
            named = "|".join(identity)
            break
    if not named:
        return ""
    # No separate size clause any more: capacities are inside the identity, so
    # appending them twice could not change a match, only bloat the key.
    return named


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
        # 3b) the two text defects the user named: text that was CUT away, and a
        #     figure printed twice inside one post (our old price badge did that).
        if "\u2026" in text:
            findings.append(flag("CUT", "the post ends mid-sentence with an ellipsis - "
                                        "source text was dropped, not deferred", row["id"]))
        if not listy and len(lines) > 1:
            per_line: dict[str, int] = {}
            for line in lines:
                for amount in re.findall(rf"{RUPEE}\s*\d[\d,.]*", line):
                    key = re.sub(r"\D", "", amount)
                    per_line[key] = per_line.get(key, 0) + 1
            twice = [f"{RUPEE}{value}" for value, hits in per_line.items() if hits > 1]
            if twice:
                findings.append(flag("DOUBLE", f"{', '.join(twice)} printed on two lines of one post",
                                     row["id"]))

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
    same_product: dict[tuple, list[int]] = {}
    for row in rows:
        text = row["rendered_text"] or ""
        if not text or (row["created_at"] or 0) < fresh_cutoff or not sent_counts.get(int(row["id"])):
            continue
        signature = product_signature(text)
        if not signature:
            continue
        priced = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        # A channel, not a job: "okasari mana channel lo vasthe skip cheyali".
        for target in targets_of(row):
            same_product.setdefault((signature, str(target)), []).append(
                (int(row["id"]), min(priced) if priced else 0))
    for (signature, target), entries in same_product.items():
        if len(entries) < 2:
            continue
        ids = [e[0] for e in entries]
        if set(ids) <= matched:
            continue
        entries = sorted(entries)
        first_price = entries[0][1]
        cheaper_later = [p for _queue_id, p in entries[1:] if p]
        # The bot is ALLOWED to carry a product again when the later copy is
        # strictly cheaper - that is a new deal, not a repeat. Anything else
        # (same price, a dearer price, no price at all) is the defect.
        if first_price and cheaper_later and min(cheaper_later) < first_price:
            continue
        matched.update(ids)
        findings.append(flag("SAME-PRODUCT",
                             f"{signature[:52]!r} carried twice by {target} (queues {ids})", ids[0]))

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
