#!/usr/bin/env python3
"""Category-wise truth: which shelf got what, and why the rest never arrived.

"Categories wise ga em jarigindi?" was not answerable from data until now. The
bot classifies every deal, routes it to a shelf, and logs the decision — but
nothing ever totalled it per category, so a shelf could sit empty for a week
(its target unresolved, its rules version stale, its Amazon tag never declared)
while Main kept posting and every aggregate number looked healthy.

This reads the live database, the bot log, `channels.json`, the installed rules
and the bot's own snapshot, and answers per shelf:

    python3 ops/category_report.py                       # last 24h, human output
    python3 ops/category_report.py --hours 72 --strict    # exit 1 on any finding
    python3 ops/category_report.py --strict-errors        # exit 1 on errors (deploy gate)
    python3 ops/category_report.py --days 14              # a longer IST-day trend
    python3 ops/category_report.py --expand electronics   # the rows behind one shelf
    python3 ops/category_report.py --json                 # for the dashboard/CI
    python3 ops/category_report.py --db /path/bot_state.sqlite3 --log /path/bot.log

Per category it reports: the channels that serve it (enabled or not, Amazon-tag
declared or not), sends/skips/holds from the delivery ledger, the pending
backlog routed at it, how many deals the classifier actually sent its way
(`CATEGORY ROUTE`), how many were withheld from it and why (`CATEGORY WITHHELD`
reasons: mixed evidence, insufficient product evidence), and the last time
anything landed there.

Three questions a total can never answer, answered per shelf:

  money    did the links this shelf published actually pay us? Every delivered
           post's URLs are matched against `link_cache` and `owned_link_proofs`
           and classified `owned` / `earns_nothing` / `foreign_tag` (an Amazon
           tag that is not ours pays a stranger) / `unattributed`. Our tag is
           READ from the bot's and the bridge's own `OUR_AMAZON_TAGS`, never
           copied here, and the two sets are compared: a drift means one surface
           credits another account. The route vocabulary is the same decision
           tree as `ops/conversion_report.py`, and the test suite proves the two
           agree link shape by link shape.
  trend    sends and skips per IST day (`--days`, default 7), so "a bad Tuesday"
           is visible instead of averaged away. The dashboard draws it as a
           sparkline; here it prints as block characters that survive SSH.
  surfaces the same shelf on Telegram (`TG_CATEGORY_<KEY>_TARGET`, channels.json)
           and on WhatsApp (`WA_CHANNEL_<KEY>`), including the bridge's rule that
           a category channel shared with another category or with a core channel
           gets NO `category:<key>` policy - configured is not the same as
           filtered. WhatsApp values are shown only when they are a plain JID;
           anything else is masked, so an invite link in the same .env cannot be
           echoed by a report.

Two more questions, answered from numbers the bot already keeps: the shelf's
deal quality (its own `premium_score` average/min/max over the posts it received,
plus the rupee spread from `posted_deals.price`) and which sources actually fed
it, against the sources its channels declare - a renamed source shows up as
undeclared traffic, a silent one as a dead declaration.

`--expand <key>` adds the newest delivery rows for one shelf (queue id, IST time,
target, status, bucketed reason, money verdict, text snippet) so a number can be
opened instead of argued with. `--html PATH` writes the whole report as one
self-contained HTML file: no JavaScript, no external asset, every value escaped.

Findings are the part that saves a week of guessing:

  rules_parity         the bot's and the bridge's category_rules.json differ, so
                       Telegram and WhatsApp disagree about what a product is
  rules_version_drift  the running bot classified with a different rules version
                       than the one installed here (deploy or restart pending)
  routing_disabled     the log says category routing is OFF (rules unreadable)
  target_collision     one Telegram target was given to two categories, so those
                       routes are disabled by the bot
  target_unresolved    a shelf's target is not reachable; deals fell back to Main
  no_channel           nothing enabled can receive this category at all
  silent_shelf         Main posted in the window, this shelf got zero sends
  skipping_shelf       this shelf skips more than it sends
  withheld_majority    most of what was classified for this shelf never reached it
  tagless_shelf        (info) Amazon links are withheld here until the tag is
                       declared - the documented policy, stated as a fact
  foreign_tag_posted   a published post carries an Amazon tag that is not ours
  unmonetized_shelf    posts went out with links that pay nobody
  money_blind          (info) nothing on this shelf could be matched to the link
                       tables, so commission is unproven rather than absent
  amazon_tag_parity    the bot and the bridge publish with different tags
  wa_category_collision  two WhatsApp categories share a channel (or reuse a core
                       one), so the bridge filters neither
  wa_surface_missing / tg_surface_missing / wa_shelf_unfiltered
  amazon_tag_surface_mismatch  the tag is declared on one surface only
  unknown_surface_channel    a WA_CHANNEL_* name that is not a category key
  ledger_capped / ledger_unreadable  the money and trend read is partial, stated

`ops/category_watch.py` turns these findings into an alarm that speaks once: it
prints only what is NEW, exits 1 for a cron or systemd `OnFailure` to act on, and
announces RESOLVED when a condition disappears. Its only write is its own small
state file.'new>

Read-only: it opens the database in `mode=ro`, never writes, never restarts
anything, and needs no Telegram, WhatsApp or affiliate credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
BOT_RULES = ROOT / "bestgaa" / "category_rules.json"
BRIDGE_RULES = ROOT / "tg-wa-bridge" / "category_rules.json"

IST_OFFSET = 5 * 3600 + 30 * 60  # Asia/Kolkata, no tzdata needed

# ------------------------------------------------------------------- inputs --


def load_rules(path: Path | None = None) -> dict:
    """The installed taxonomy. A missing/corrupt file is a finding, not a crash."""
    target = Path(path or BOT_RULES)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 0, "categories": [], "min_score": 3, "min_margin": 2,
                "load_error": f"cannot read {target}"}
    if not isinstance(data, dict):
        return {"version": 0, "categories": [], "load_error": f"{target} is not an object"}
    data.setdefault("categories", [])
    return data


def fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def load_env(files: list[str] | None = None) -> dict[str, str]:
    """KEY=VALUE from .env files, for the env-declared category targets.

    Only the keys this report needs are kept, so a token in the same file cannot
    leak into output or JSON. What is kept is all channel plumbing, not
    credentials: Telegram usernames, WhatsApp channel ids (masked unless they are
    a plain JID - see `mask_wa`) and the public Amazon Associates tag that
    already appears in every published link.
    """
    wanted = ("TG_CATEGORY_", "AMAZON_TAG_CATEGORY_TARGETS", "AMAZON_TAG",
              "WA_CHANNEL_", "WA_AMAZON_TAG_CATEGORY_TARGETS")
    candidates = [Path(f) for f in (files or [])]
    if not candidates:
        default = ROOT / "bestgaa" / ".env"
        candidates = [default] if default.exists() else []
    env: dict[str, str] = {}
    # The process environment first, then the files over it: a deployed bot's
    # truth is its .env / unit file, but an operator (and the dashboard) also
    # passes shelf targets in the environment. Reading both is what keeps
    # `python3 ops/category_report.py` and /api/categories from disagreeing about
    # which shelves exist.
    for key, value in os.environ.items():
        if key.startswith(wanted) and str(value).strip():
            env[key] = str(value).strip()
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key.startswith(wanted):
                continue
            env[key] = value.strip().strip("'\"")
    return env


def normalize_target(name: str) -> str:
    return str(name or "").strip().lstrip("@").lower()


def source_id(value: str) -> str:
    """Compare sources the way channels_config does.

    A handle is case-insensitive; an invite link's code is not (`t.me/+AbC` and
    `t.me/+abc` are different chats). Without this a declared source and the same
    source in the ledger can look like two different things, and "undeclared
    traffic" would be pure noise.
    """
    text = str(value or "").strip()
    if not text:
        return "(no source)"
    return text if "/+" in text else text.lower()


def channels_view(data: dict, rules: dict) -> dict:
    """channels.json split into category shelves and unfiltered (Main) targets."""
    known = {str(c.get("key", "")).lower() for c in rules.get("categories", []) if isinstance(c, dict)}
    shelves: dict[str, list[dict]] = {key: [] for key in known}
    main: list[dict] = []
    for entry in (data or {}).get("channels", []) or []:
        if not isinstance(entry, dict):
            continue
        name = normalize_target(entry.get("name"))
        if not name:
            continue
        cats = [str(c).strip().lower() for c in (entry.get("categories") or [])
                if str(c).strip().lower() in known]
        row = {
            "name": name,
            "enabled": bool(entry.get("enabled", True)),
            "amazon_tag": bool(entry.get("amazon_tag", False)),
            "sources": len(entry.get("sources") or []),
            "source_names": [source_id(v) for v in (entry.get("sources") or [])
                             if str(v).strip()],
            "all_sources": "*" in (entry.get("sources") or []),
            "categories": cats,
        }
        if cats:
            for key in cats:
                shelves[key].append(row)
        else:
            main.append(row)
    return {"shelves": shelves, "main": main}


def target_categories(data: dict, rules: dict, env: dict | None = None) -> dict[str, frozenset | None]:
    """target (lowercased, @ stripped) -> the categories it keeps, or None for all.

    None means "unfiltered" (Main): the bot's own rule is that a target with no
    category filter takes every category, so its deliveries must never be
    counted as a shelf send.
    """
    env = env or {}
    view = channels_view(data, rules)
    index: dict[str, frozenset | None] = {}
    for row in view["main"]:
        index[row["name"]] = None
    for key, rows in view["shelves"].items():
        for row in rows:
            existing = index.get(row["name"])
            merged = {key} if existing is None else set(existing) | {key}
            index[row["name"]] = frozenset(merged)
    # Env-declared shelves (TG_CATEGORY_<KEY>_TARGET) exist independently of the
    # dashboard config, and the bot routes to them before any dashboard filter.
    for name, value in env.items():
        if not name.startswith("TG_CATEGORY_") or not value.strip():
            continue
        key = name[len("TG_CATEGORY_"):].split("_TARGET")[0].lower()
        target = normalize_target(value)
        if not target:
            continue
        existing = index.get(target)
        index[target] = frozenset({key}) if existing is None else frozenset(existing | {key})
    return index


def env_targets(env: dict | None = None) -> dict[str, str]:
    """category key -> the Telegram target declared for it in the environment."""
    out: dict[str, str] = {}
    for name, value in (env or {}).items():
        if name.startswith("TG_CATEGORY_") and value.strip():
            key = name[len("TG_CATEGORY_"):].split("_TARGET")[0].lower()
            out[key] = normalize_target(value)
    return out


# ----------------------------------------------------------------- database --


def open_readonly(path: Path) -> sqlite3.Connection | None:
    try:
        if not Path(path).exists():
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


REASON_BUCKETS = (
    (re.compile(r"flood", re.I), "flood_wait"),
    (re.compile(r"category target unresolved|target unresolved", re.I), "target_unresolved"),
    (re.compile(r"not authorized|admin|permission|forbidden", re.I), "permission"),
    (re.compile(r"entity|could not find|unresolved", re.I), "entity_unresolved"),
    (re.compile(r"provenance|own link|strict|unmonetiz", re.I), "link_provenance"),
    (re.compile(r"categor", re.I), "category_policy"),
    (re.compile(r"duplicat|already", re.I), "duplicate"),
    (re.compile(r"media|photo|video|file", re.I), "media"),
)


def bucket_reason(error: str) -> str:
    text = str(error or "").strip()
    if not text:
        return "unspecified"
    for pattern, bucket in REASON_BUCKETS:
        if pattern.search(text):
            return bucket
    return "other: " + text[:48]


def pending_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    cols = table_columns(conn, "queue")
    if "targets_json" not in cols or "status" not in cols:
        return []
    try:
        return [(str(r[0] or ""), str(r[1] or "")) for r in conn.execute(
            "SELECT targets_json, status FROM queue WHERE status IN ('pending','scheduled','queued')")]
    except sqlite3.OperationalError:
        return []


def count_deliveries(rows, index: dict[str, frozenset | None]) -> dict:
    """Per-category and Main totals from the delivery ledger.

    A target that keeps two categories counts once for each: the question "did
    the electronics shelf get posts?" is per shelf, not per delivery.
    """
    empty = {"sent": 0, "skipped": 0, "held": 0, "last_sent_at": 0.0, "reasons": Counter()}
    buckets: dict[str, dict] = {"main": dict(empty, reasons=Counter())}
    for key in index.values():
        for cat in (key or ()):  # None -> unfiltered, already counted as main
            buckets.setdefault(cat, dict(empty, reasons=Counter()))
    for target, status, error, created_at in rows:
        cats = index.get(normalize_target(target), "unknown")
        if cats == "unknown":
            # A target nobody configured: still counted, under its own name (with
            # the operator's spelling kept), so the report cannot silently drop
            # deliveries it does not understand.
            cats = frozenset({"target:" + str(target or "?").strip().lstrip("@")})
        keys = ["main"] if cats is None else sorted(cats)
        for key in keys:
            bucket = buckets.setdefault(key, dict(empty, reasons=Counter()))
            if status == "sent" and not error:
                bucket["sent"] += 1
                bucket["last_sent_at"] = max(bucket["last_sent_at"], created_at)
            elif status == "held":
                bucket["held"] += 1
            else:
                bucket["skipped"] += 1
                if status == "sent" and error:
                    bucket["reasons"][bucket_reason(error) + " (sent-with-error)"] += 1
                elif status == "skipped":
                    bucket["reasons"][bucket_reason(error)] += 1
                else:
                    bucket["reasons"][f"status:{status or 'empty'}"] += 1
    return buckets


def count_pending(rows, index: dict[str, frozenset | None]) -> Counter:
    pending: Counter = Counter()
    for targets_json, _status in rows:
        try:
            targets = json.loads(targets_json) if targets_json else []
        except ValueError:
            targets = []
        if not isinstance(targets, list):
            continue
        seen: set[str] = set()
        for target in targets:
            cats = index.get(normalize_target(target), None)
            for key in (["main"] if cats is None else sorted(cats or [])):
                if key not in seen:
                    seen.add(key)
                    pending[key] += 1
    return pending


# ------------------------------------------- money: does a click pay anybody? --

# The same monetization vocabulary ops/conversion_report.py uses. It is restated
# here rather than imported so a deployed bundle that ships only this file still
# answers correctly - and test_category_report.py compares the two decision trees
# link shape by link shape, so they cannot drift apart silently.
HYPD_HOSTS = ("hypd.store",)
EARNKARO_HOSTS = ("ekaro.in", "ekaro.app", "earnkaro.com", "affiliaters.in",
                  "earnkaro.in", "linkredirect.in", "clnk.in", "clnk.app")
SHORTENER_HOSTS = ("bit.ly", "j.mp", "bitly.com", "is.gd", "t.co", "buff.ly")
AMAZON_HOSTS = ("amazon.in", "amazon.com", "amzn.to", "amzn.in")
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+")
PAID_ROUTES = frozenset({"amazon", "earnkaro", "hypd", "affiliate"})
MONEY_BUCKETS = ("owned", "earns_nothing", "foreign_tag", "unattributed")
# A post is judged by its best link, but a foreign tag outranks everything: one
# link that pays a stranger is the finding, even next to nine of ours.
MONEY_RANK = {"unattributed": 0, "earns_nothing": 1, "owned": 2, "foreign_tag": 3}

PY_TAG_RE = re.compile(r"OUR_AMAZON_TAGS\s*=\s*\{([^}]*)\}")
JS_TAG_RE = re.compile(r"OUR_AMAZON_TAGS\s*=\s*\[([^\]]*)\]")
QUOTED_RE = re.compile(r"[\"']([A-Za-z0-9_-]{3,})[\"']")


def host_of(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def in_hosts(host: str, hosts) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def clean_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def route_of(affiliate: str, resolved: str) -> str:
    """Which monetization route produced a link (mirrors conversion_report)."""
    a, r = affiliate or "", resolved or ""
    if a and r and clean_url(a) == clean_url(r):
        return "passthrough"                      # clean merchant link: earns nothing
    joined = (a + " " + r).lower()
    if "hypd.store" in joined:
        return "hypd"
    host = host_of(a)
    if in_hosts(host, EARNKARO_HOSTS) or "ekaro" in a.lower() or "affiliaters" in a.lower():
        return "earnkaro"
    if in_hosts(host, AMAZON_HOSTS) or "amazon.in" in joined:
        return "amazon"
    if in_hosts(host, SHORTENER_HOSTS):
        if "amazon." in r.lower():
            return "amazon"
        if "hypd.store" in r.lower():
            return "hypd"
        if "ekaro" in r.lower():
            return "earnkaro"
        return "shortened"
    low = a.lower()
    if "affextparam2=" in low or "affid=" in low or "tag=" in low:
        return "affiliate"
    return "other"


def amazon_tag_of(url: str) -> str:
    """The Associates tag a URL carries, or "" when it carries none."""
    try:
        query = urlparse(url or "").query or ""
    except ValueError:
        return ""
    for pair in query.split("&"):
        name, _, value = pair.partition("=")
        if name.strip().lower() == "tag":
            return value.strip().lower()
    return ""


def our_amazon_tags(root: Path | None = None) -> dict:
    """Our Associates tag as each service actually defines it - read, not copied.

    Both the bot and the bridge hardcode the set and refuse any other configured
    tag, so a third copy here would be a third thing to drift. Reading the two
    sources also makes the drift itself a finding: if Telegram publishes one tag
    and WhatsApp another, one surface is paying somebody else.
    """
    base = Path(root or ROOT)
    out = {"bestgaa": set(), "bridge": set(), "read_error": ""}
    try:
        text = (base / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8", errors="replace")
        match = PY_TAG_RE.search(text)
        if match:
            out["bestgaa"] = {t.lower() for t in QUOTED_RE.findall(match.group(1))}
    except OSError as exc:
        out["read_error"] = f"bestgaa/main_bot_new.py: {exc}"
    try:
        text = (base / "tg-wa-bridge" / "bridge.js").read_text(encoding="utf-8", errors="replace")
        match = JS_TAG_RE.search(text)
        if match:
            out["bridge"] = {t.lower() for t in QUOTED_RE.findall(match.group(1))}
    except OSError as exc:
        out["read_error"] = (out["read_error"] + f"; tg-wa-bridge/bridge.js: {exc}").strip("; ")
    return out


def link_index(conn) -> dict:
    """What the bot published, plus the proof behind its own shortened hops.

    `link_cache.affiliate_url` is the published link and `resolved_url` the real
    destination; `owned_link_proofs` keeps the destination behind a bit.ly/is.gd
    hop, so a short link is still attributable to us instead of looking blind.
    """
    out: dict = {"by_affiliate": {}, "by_source": {}, "proofs": {}, "tables": []}
    if conn is None:
        return out
    if table_columns(conn, "link_cache"):
        out["tables"].append("link_cache")
        try:
            for row in conn.execute(
                    "SELECT source_url, affiliate_url, resolved_url FROM link_cache"):
                affiliate, resolved = str(row[1] or ""), str(row[2] or "")
                route = route_of(affiliate, resolved)
                if affiliate:
                    out["by_affiliate"][clean_url(affiliate)] = (route, resolved)
                if row[0]:
                    out["by_source"][clean_url(str(row[0]))] = (route, resolved)
        except sqlite3.OperationalError:
            pass
    if table_columns(conn, "owned_link_proofs"):
        out["tables"].append("owned_link_proofs")
        try:
            for row in conn.execute(
                    "SELECT affiliate_url, destination_url FROM owned_link_proofs"):
                affiliate, destination = clean_url(str(row[0] or "")), str(row[1] or "")
                if not affiliate:
                    continue
                out["proofs"][affiliate] = destination
                known = out["by_affiliate"].get(affiliate)
                if known is None or known[0] in ("shortened", "other", "passthrough"):
                    out["by_affiliate"][affiliate] = (route_of(destination, ""), destination)
        except sqlite3.OperationalError:
            pass
    return out


def url_money(url: str, links: dict, our_tags: frozenset) -> tuple[str, str]:
    """One URL -> (bucket, route). An Amazon page is ours only with our tag."""
    trimmed = clean_url(url.rstrip(".,;:)"))
    known = links["by_affiliate"].get(trimmed) or links["by_source"].get(trimmed)
    if known is not None:
        route, resolved = known
    else:
        proof = links["proofs"].get(trimmed)
        resolved = proof or ""
        route = route_of(proof, "") if proof else route_of(url, "")
    if route == "amazon" or in_hosts(host_of(url), AMAZON_HOSTS):
        tag = amazon_tag_of(url) or amazon_tag_of(resolved)
        if tag and our_tags and tag not in our_tags:
            return ("foreign_tag", "amazon")       # pays a stranger, not nobody
        if tag:
            return ("owned", "amazon" if our_tags else "amazon_tag_unverified")
        return ("earns_nothing", "amazon_untagged")
    if route == "shortened":
        # Our shortener, but no proof row and no cache row: we cannot show which
        # destination it hides, so it is not counted as earning.
        return ("unattributed", "shortened_no_proof")
    if route in PAID_ROUTES:
        return ("owned", route)
    if route == "passthrough":
        return ("earns_nothing", "passthrough")
    return ("earns_nothing", route or "other")


def post_money(text: str, links: dict, our_tags: frozenset) -> tuple[str, str]:
    """One published post -> (bucket, route). Its worst problem wins the label,
    except that a foreign tag outranks everything (see MONEY_RANK)."""
    urls = URL_RE.findall(text or "")
    if not urls:
        return ("unattributed", "no_link_in_text")
    best, best_route = "unattributed", ""
    for url in urls:
        bucket, route = url_money(url, links, our_tags)
        if MONEY_RANK[bucket] > MONEY_RANK[best] or not best_route:
            best, best_route = bucket, route
    return (best, best_route)


def unknown_key(target: str) -> str:
    """A target no config claims keeps the operator's spelling (see findings)."""
    return "target:" + str(target or "?").strip().lstrip("@")


def keys_for(target: str, index: dict) -> list[str]:
    cats = index.get(normalize_target(target), "unknown")
    if cats is None:
        return ["main"]                    # unfiltered channel: everything lands here
    if cats == "unknown":
        return [unknown_key(target)]
    return sorted(cats)


def ledger_rows(conn, since: float, max_posts: int = 20000) -> dict:
    """The delivery ledger joined to what was actually published.

    One read feeds the totals, the money view, the daily history and the
    drill-down, so those four can never disagree about the same delivery.
    """
    result = {"rows": [], "capped": False, "error": ""}
    if conn is None or not table_columns(conn, "deliveries"):
        return result
    delivery_cols = table_columns(conn, "deliveries")
    queue_cols = table_columns(conn, "queue")
    error_expr = "COALESCE(d.last_error,'')" if "last_error" in delivery_cols else "''"
    text_expr = "q.rendered_text" if "rendered_text" in queue_cols else "NULL"
    source_expr = "q.source" if "source" in queue_cols else "NULL"
    score_expr = "q.premium_score" if "premium_score" in queue_cols else "NULL"
    query = (f"SELECT d.queue_id AS queue_id, d.target AS target, d.status AS status, "
             f"{error_expr} AS error, q.created_at AS created_at, "
             f"COALESCE({text_expr},'') AS text, {source_expr} AS source, "
             f"{score_expr} AS score "
             "FROM deliveries d JOIN queue q ON q.id = d.queue_id "
             "WHERE q.created_at >= ? ORDER BY q.created_at DESC LIMIT ?")
    try:
        fetched = conn.execute(query, (since, int(max_posts) + 1)).fetchall()
    except sqlite3.OperationalError as exc:
        result["error"] = str(exc)
        return result
    result["capped"] = len(fetched) > int(max_posts)
    for row in fetched[:int(max_posts)]:
        result["rows"].append({
            "queue_id": int(row["queue_id"] or 0),
            "target": str(row["target"] or ""),
            "status": str(row["status"] or ""),
            "error": str(row["error"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "text": str(row["text"] or ""),
            "source": str(row["source"] or "") or "(no source)",
            "score": (None if row["score"] is None else int(row["score"])),
        })
    return result


def price_index(conn) -> dict[int, list[int]]:
    """queue_id -> the rupee prices the bot recorded for what it posted.

    `posted_deals.price` is the price behind the published deal, so a shelf's
    price spread is read from the bot's own record instead of parsed back out of
    the caption text.
    """
    out: dict[int, list[int]] = {}
    if conn is None or not table_columns(conn, "posted_deals"):
        return out
    try:
        for row in conn.execute(
                "SELECT queue_id, price FROM posted_deals WHERE price IS NOT NULL"):
            price = int(row[1] or 0)
            if price > 0:
                out.setdefault(int(row[0] or 0), []).append(price)
    except (sqlite3.OperationalError, ValueError, TypeError):
        return out
    return out


def link_conversion(conn, our_tags: frozenset, limit: int = 25) -> dict:
    """Source link -> our link: what the converter actually did, from the ledger.

    `link_cache` is the converter's own book: the source URL it received, the
    affiliate URL it published and the resolved destination. Classifying the
    published URL with the same url_money() the money report uses answers the
    operator's real question - "mana links maruthunnaya?" - with evidence per
    link instead of a vibe: owned (our tag / our shortener / a proof row),
    foreign_tag (pays a stranger), earns_nothing, unattributed.
    """
    totals = {"cached": 0, "owned": 0, "foreign_tag": 0, "earns_nothing": 0,
              "unattributed": 0, "proofs": 0, "capped": False}
    rows_out: list[dict] = []
    links = link_index(conn) if conn is not None else {"by_affiliate": {}, "by_source": {},
                                                       "proofs": {}}
    totals["proofs"] = len(links.get("proofs") or {})
    if conn is None:
        return {"rows": rows_out, "totals": totals, "converted_pct": None}
    try:
        rows = conn.execute(
            "SELECT source_url, affiliate_url, resolved_url, created_at FROM link_cache "
            "ORDER BY created_at DESC LIMIT 5000").fetchall()
    except sqlite3.OperationalError:
        return {"rows": rows_out, "totals": totals, "converted_pct": None}
    if len(rows) >= 5000:
        totals["capped"] = True
    for source_url, affiliate_url, resolved_url, created_at in rows:
        # positional on purpose: this view also runs under test connections that
        # carry no row_factory.
        affiliate = str(affiliate_url or "")
        bucket, route = url_money(affiliate, links, our_tags)
        totals["cached"] += 1
        if bucket in totals:
            totals[bucket] += 1
        if len(rows_out) < limit:
            rows_out.append({
                "source": str(source_url or ""),
                "affiliate": affiliate,
                "resolved_host": host_of(str(resolved_url or "")),
                "route": route,
                "bucket": bucket,
                "proof": clean_url(affiliate) in (links.get("proofs") or {}),
                "at": ist_text(created_at) if created_at else "",
            })
    pct = None
    if totals["cached"]:
        pct = round(totals["owned"] * 100 / totals["cached"], 1)
    return {"rows": rows_out, "totals": totals, "converted_pct": pct}


def deal_quality(rows, index: dict, prices: dict[int, list[int]]) -> dict:
    """Per destination: how good were the deals it actually received?

    `premium_score` is the bot's own ranking (discount x10, a sub-Rs99 / Rs499
    bonus, a lowest-price-claim bonus) and `posted_deals.price` is the rupee price
    it published. Both are read, never recomputed or re-judged here, so "this
    shelf gets weak deals" is the bot's own opinion of them, not a new one.
    """
    def empty() -> dict:
        return {"posts": 0, "scores": [], "prices": [], "unscored": 0, "sources": Counter()}

    # "_fleet" is every delivered post once, so a shelf can be compared against
    # the whole operation without double-counting multi-category channels.
    out: dict = {"main": empty(), "_fleet": empty()}
    for row in rows:
        if row["status"] != "sent" or row["error"]:
            continue
        fleet = out["_fleet"]
        fleet["posts"] += 1
        fleet["sources"][source_id(row["source"])] += 1
        if row["score"] is None:
            fleet["unscored"] += 1
        else:
            fleet["scores"].append(int(row["score"]))
        fleet["prices"].extend(prices.get(int(row["queue_id"]), []))
        for key in keys_for(row["target"], index):
            shelf = out.setdefault(key, empty())
            shelf["posts"] += 1
            shelf["sources"][source_id(row["source"])] += 1
            if row["score"] is None:
                shelf["unscored"] += 1
            else:
                shelf["scores"].append(int(row["score"]))
            shelf["prices"].extend(prices.get(int(row["queue_id"]), []))
    return out


def quality_view(bucket: dict) -> dict:
    scores = sorted(bucket.get("scores") or [])
    prices = sorted(bucket.get("prices") or [])
    return {
        "posts": int(bucket.get("posts", 0)),
        "scored": len(scores),
        "unscored": int(bucket.get("unscored", 0)),
        "avg_score": round(statistics.fmean(scores), 1) if scores else None,
        "min_score": scores[0] if scores else None,
        "max_score": scores[-1] if scores else None,
        "zero_score": sum(1 for value in scores if value <= 0),
        "price_rows": len(prices),
        "price_min": prices[0] if prices else None,
        "price_median": int(statistics.median(prices)) if prices else None,
        "price_max": prices[-1] if prices else None,
        "under_99": sum(1 for value in prices if value <= 99),
        "under_499": sum(1 for value in prices if 100 <= value <= 499),
        "sources": dict((bucket.get("sources") or Counter()).most_common()),
    }


def source_matrix(quality: dict, view: dict, rules: dict) -> dict:
    """Which source actually feeds which shelf - and which declared source is dead.

    `channels.json` declares the sources a channel listens to; the ledger says
    what really arrived. The difference is the drift: a renamed source shows up
    as undeclared traffic, a dead one as a declaration nobody ever fulfils.
    """
    keys = [str(c.get("key", "")).strip().lower()
            for c in rules.get("categories", []) if isinstance(c, dict) and c.get("key")]
    columns = keys + ["main"]
    cells: dict[str, dict[str, int]] = {}
    totals: Counter = Counter()
    for column in columns:
        for source, count in (quality.get(column) or {}).get("sources", Counter()).items():
            cells.setdefault(source, {})[column] = int(count)
            totals[source] += int(count)
    declared: dict[str, list[str]] = {}
    unrestricted: dict[str, bool] = {}
    for key in keys:
        rows = view["shelves"].get(key, [])
        enabled = [r for r in rows if r["enabled"]]
        sources: set[str] = set()
        for row in enabled:
            if row["all_sources"]:
                sources.add("*")
            sources.update(row.get("source_names") or [])
        declared[key] = sorted(sources)
        unrestricted[key] = any(row["all_sources"] for row in enabled) or not enabled
    declared_only = sorted(
        {source for values in declared.values() for source in values if source != "*"}
        - set(cells))
    return {
        "columns": columns,
        "sources": [name for name, _count in totals.most_common()],
        "cells": {name: cells.get(name, {}) for name in totals},
        "totals": dict(totals.most_common()),
        "declared": declared,
        "unrestricted": unrestricted,
        "declared_only": declared_only,
    }


def count_money(rows, index: dict, links: dict, our_tags: frozenset) -> dict:
    """Per shelf: how many published posts carry a link that pays us.

    Only rows the ledger marks as delivered are counted - a skipped post never
    reached anybody, so it can neither earn nor fail to earn.
    """
    def empty() -> dict:
        return dict({bucket: 0 for bucket in MONEY_BUCKETS},
                    posts=0, routes=Counter(), examples=[])

    out: dict = {"main": empty()}
    for row in rows:
        if row["status"] != "sent" or row["error"]:
            continue
        bucket, route = post_money(row["text"], links, our_tags)
        for key in keys_for(row["target"], index):
            shelf = out.setdefault(key, empty())
            shelf["posts"] += 1
            shelf[bucket] += 1
            if route:
                shelf["routes"][route] += 1
            if bucket in ("earns_nothing", "foreign_tag", "unattributed") \
                    and len(shelf["examples"]) < 3:
                shelf["examples"].append({
                    "queue_id": row["queue_id"], "target": row["target"],
                    "bucket": bucket, "route": route, "at": ist_text(row["created_at"]),
                    "host": host_of((URL_RE.findall(row["text"] or [""]) or [""])[0]),
                })
    return out


# ------------------------------------------------------- history: the trend --


def ist_day(stamp: float) -> str:
    """The IST calendar day a timestamp falls in (UTC+5:30, no tzdata)."""
    return time.strftime("%Y-%m-%d", time.gmtime(max(0.0, float(stamp)) + IST_OFFSET))


def day_series(days: int, now: float | None = None) -> list[str]:
    """The last `days` IST days, oldest first, today last."""
    stamp = time.time() if now is None else now
    return [ist_day(stamp - offset * 86400.0) for offset in range(int(days) - 1, -1, -1)]


def history(rows, index: dict, days: int, now: float | None = None) -> dict:
    """Per-shelf sends and skips per IST day, for a sparkline and a bad-day hunt.

    A hold is neither: the ledger already counts it separately, and counting it
    here would make a paused shelf look like a failing one.
    """
    series = day_series(max(1, int(days)), now)
    known = set(series)

    def empty() -> dict:
        return {day: {"sent": 0, "skipped": 0} for day in series}

    out: dict = {"days": series, "main": empty()}
    for row in rows:
        day = ist_day(row["created_at"])
        if day not in known:
            continue                       # older than the series (a long window)
        for key in keys_for(row["target"], index):
            shelf = out.setdefault(key, empty())
            if row["status"] == "sent" and not row["error"]:
                shelf[day]["sent"] += 1
            elif row["status"] != "held":
                shelf[day]["skipped"] += 1
    return out


def recent_for(rows, index: dict, links: dict, our_tags: frozenset,
               key: str, limit: int = 12) -> list[dict]:
    """The newest deliveries for one shelf (or `main`, or an unknown target)."""
    out: list[dict] = []
    for row in rows:                       # ledger_rows is newest-first
        if key not in keys_for(row["target"], index):
            continue
        delivered = row["status"] == "sent" and not row["error"]
        bucket, route = post_money(row["text"], links, our_tags) if delivered else ("", "")
        out.append({
            "queue_id": row["queue_id"], "target": row["target"], "status": row["status"],
            "reason": "" if delivered else (bucket_reason(row["error"])
                                            or f"status:{row['status'] or 'empty'}"),
            "error": (row["error"] or "")[:120], "money": bucket, "route": route,
            "at": ist_text(row["created_at"]),
            "snippet": " ".join((row["text"] or "").split())[:140],
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


# --------------------------------------- surfaces: Telegram vs WhatsApp shelf --

WA_JID_RE = re.compile(r"^[A-Za-z0-9._-]+@(newsletter|g\.us|s\.whatsapp\.net|broadcast)$")
WA_CORE_ENV = (("WA_CHANNEL_UNDER99", "under99"), ("WA_CHANNEL_UNDER499", "under499"),
               ("WA_CHANNEL_BEST_OF", "best_of"))


def mask_wa(value: str) -> str:
    """A WhatsApp channel id is public in every message; an invite link is not.

    JIDs are shown verbatim (they are also what makes a collision detectable),
    anything else is reported as merely set, so a surface check can never leak a
    credential that happens to live in the same .env.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return text if WA_JID_RE.match(text) else "set (masked)"


def wa_surfaces(env: dict | None, rules: dict) -> dict:
    """What WhatsApp is configured to do with each category, from env alone.

    The bridge builds `category:<key>` policies only from `WA_CHANNEL_<KEY>`, and
    only when that JID is unique among the category channels and is not also a
    core channel - otherwise it logs a warning and the shelf is NOT filtered.
    Both rules are re-checked here, offline, before they cost a week of posts.
    """
    env = env or {}
    keys = {str(c.get("key", "")).strip().lower()
            for c in rules.get("categories", []) if isinstance(c, dict)}
    channels: dict[str, str] = {}
    unknown: list[str] = []
    for name, value in sorted(env.items()):
        if not name.startswith("WA_CHANNEL_"):
            continue
        slug = name[len("WA_CHANNEL_"):].strip().lower()
        if slug in {core for _, core in WA_CORE_ENV}:
            continue
        masked = mask_wa(value)
        if not masked:
            continue
        if slug in keys:
            channels[slug] = masked
        else:
            unknown.append(name)
    core = {slug: mask_wa(env.get(name, "")) for name, slug in WA_CORE_ENV}
    core = {slug: value for slug, value in core.items() if value}
    wa_tags = {v.strip().lower() for v in
               (env.get("WA_AMAZON_TAG_CATEGORY_TARGETS", "") or "").split(",") if v.strip()}
    wa_tags &= keys
    masked_values = [v for v in list(channels.values()) + list(core.values())
                     if v == "set (masked)"]
    # A collision can only be proved from comparable values; two different invite
    # links both mask to the same string, so say "cannot tell" instead of guessing.
    comparable = not masked_values
    collisions: list[dict] = []
    if comparable:
        seen: dict[str, list[str]] = {}
        for slug, jid in channels.items():
            seen.setdefault(jid, []).append(slug)
        for jid, slugs in sorted(seen.items()):
            if len(slugs) > 1:
                collisions.append({"jid": jid, "categories": sorted(slugs),
                                   "why": "two categories share one WhatsApp channel"})
        for slug, jid in sorted(channels.items()):
            if jid in core.values():
                owner = sorted(name for name, value in core.items() if value == jid)
                collisions.append({"jid": jid, "categories": [slug],
                                   "why": f"the same channel is the {owner[0]} core channel"})
    return {"channels": channels, "core": core, "tag_categories": sorted(wa_tags),
            "unknown_env": unknown, "configured": bool(channels or core or wa_tags),
            "collisions": collisions, "comparable": comparable,
            "masked": len(masked_values)}


# ---------------------------------------------------------------------- log --

STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
ROUTE_RE = re.compile(r"CATEGORY ROUTE \| (.+?) -> @(\S+) \| queue=(\d+)")
WITHHELD_RE = re.compile(
    r"CATEGORY WITHHELD \| queue=(\d+) category=(\S+) reason=(\S+) targets=(.*)$")
COLLISION_RE = re.compile(r"CATEGORY TARGET COLLISION \| @(\S+) is assigned to (.+?);")
UNRESOLVED_RE = re.compile(r"CATEGORY TARGET UNRESOLVED \| @(\S+)")
DISABLED_RE = re.compile(r"CATEGORY ROUTING DISABLED \| (.*)$")


def log_stamp(line: str) -> float:
    match = STAMP_RE.match(line)
    if not match:
        return 0.0
    try:
        return time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0.0


def parse_log(path: Path | None, since: float, rules: dict) -> dict:
    """Category decisions the bot actually logged, inside the window."""
    out = {
        "log": str(path) if path else "",
        "lines_read": 0,
        "routes": Counter(),          # category label -> count
        "route_targets": Counter(),   # category label -> target
        "withheld": {},               # category key -> Counter(reason)
        "withheld_last_at": {},       # category key -> ts
        "collisions": [],
        "unresolved": Counter(),
        "routing_disabled": [],
        "last_route_at": 0.0,
    }
    if not path or not Path(path).exists():
        return out
    label_to_key = {}
    for definition in rules.get("categories", []):
        if isinstance(definition, dict):
            label_to_key[str(definition.get("label", "")).strip().lower()] = str(definition.get("key", ""))
            label_to_key[str(definition.get("key", "")).strip().lower()] = str(definition.get("key", ""))
    try:
        handle = Path(path).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return out
    with handle:
        for line in handle:
            out["lines_read"] += 1
            if "CATEGORY" not in line:
                continue
            stamp = log_stamp(line)
            if stamp and stamp < since:
                continue
            route = ROUTE_RE.search(line)
            if route:
                label = route.group(1).strip()
                out["routes"][label] += 1
                out["route_targets"][f"{label} -> @{route.group(2)}"] += 1
                out["last_route_at"] = max(out["last_route_at"], stamp)
                continue
            withheld = WITHHELD_RE.search(line)
            if withheld:
                raw_category = withheld.group(2).strip().lower()
                key = label_to_key.get(raw_category, raw_category)
                reasons = out["withheld"].setdefault(key, Counter())
                reasons[withheld.group(3).strip() or "unknown"] += 1
                if stamp:
                    out["withheld_last_at"][key] = max(out["withheld_last_at"].get(key, 0.0), stamp)
                continue
            collision = COLLISION_RE.search(line)
            if collision:
                entry = {"target": collision.group(1), "categories": collision.group(2).strip(),
                         "at": stamp}
                if entry not in out["collisions"]:
                    out["collisions"].append(entry)
                continue
            unresolved = UNRESOLVED_RE.search(line)
            if unresolved:
                out["unresolved"][normalize_target(unresolved.group(1))] += 1
                continue
            disabled = DISABLED_RE.search(line)
            if disabled:
                out["routing_disabled"].append(disabled.group(1).strip()[:200])
    return out


# ------------------------------------------------------------------- report --


def ist_text(stamp: float) -> str:
    if not stamp:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M IST", time.gmtime(stamp + IST_OFFSET))


def default_db() -> Path:
    env = os.getenv("BOT_DB_PATH", "").strip()
    if env:
        return Path(env)
    for candidate in (ROOT / "bestgaa" / "state" / "bot_state.sqlite3",
                      ROOT / "bestgaa" / "bestgaa.sqlite3"):
        if candidate.exists():
            return candidate
    return ROOT / "bestgaa" / "state" / "bot_state.sqlite3"


def default_log() -> Path:
    env = os.getenv("BOT_LOG", "").strip()
    if env:
        return Path(env)
    log_dir = os.getenv("BOT_LOG_DIR", "").strip()
    if log_dir:
        return Path(log_dir) / "bot.log"
    for candidate in (ROOT / "bestgaa" / "logs" / "bot.log",
                      Path(os.path.expanduser("~/bestgaa-bot/bestgaa-bot/logs/bot.log"))):
        if candidate.exists():
            return candidate
    return ROOT / "bestgaa" / "logs" / "bot.log"


def load_snapshot() -> dict:
    """The routing snapshot the RUNNING bot wrote next to channels.json.

    It carries `category_rules_version`, which is the only way to know whether
    the live bot classifies with the taxonomy installed here or with an older one
    (a deploy or an approved restart is pending). Guessing the filename would
    silently report "bot never reported" forever, so it is resolved exactly the
    way bestgaa/channels_config.py resolves it.
    """
    channels_env = os.getenv("CHANNELS_JSON_PATH", "").strip()
    if channels_env:
        base = Path(channels_env).parent
    else:
        db_env = os.getenv("BOT_DB_PATH", "").strip()
        base = Path(db_env).parent if db_env else ROOT / "bestgaa"
    candidates = [base / "routing_snapshot.json",
                  default_db().parent / "routing_snapshot.json",
                  Path(os.path.expanduser("~/bestgaa-bot/bestgaa-bot/routing_snapshot.json"))]
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def money_view(money: dict, key: str) -> dict:
    shelf = money.get(key) or {}
    routes = shelf.get("routes") or Counter()
    return dict({"posts": int(shelf.get("posts", 0))},
                **{bucket: int(shelf.get(bucket, 0)) for bucket in MONEY_BUCKETS},
                routes=dict(routes.most_common(6)),
                examples=list(shelf.get("examples") or []))


def history_view(hist: dict, key: str) -> list[dict]:
    shelf = hist.get(key) or {}
    return [{"day": day, "sent": int((shelf.get(day) or {}).get("sent", 0)),
             "skipped": int((shelf.get(day) or {}).get("skipped", 0))}
            for day in hist.get("days", [])]


def money_totals(rows, links: dict, our_tags: frozenset) -> dict:
    """Every delivered post once, whatever shelf it landed on.

    A shelf total can count one post twice (a channel serving two categories), so
    the headline number is computed from the rows themselves.
    """
    out = dict({bucket: 0 for bucket in MONEY_BUCKETS}, posts=0)
    routes: Counter = Counter()
    for row in rows:
        if row["status"] != "sent" or row["error"]:
            continue
        bucket, route = post_money(row["text"], links, our_tags)
        out["posts"] += 1
        out[bucket] += 1
        if route:
            routes[route] += 1
    out["routes"] = dict(routes.most_common(8))
    return out


def wa_filtered_keys(wa: dict) -> set[str]:
    """Which WhatsApp category channels really get a `category:<key>` policy.

    The bridge drops the policy when the JID is shared with another category or
    with a core channel, so "configured" and "filtered" are not the same thing.
    """
    if not wa.get("comparable"):
        return set(wa.get("channels", {}))     # masked values: only the bridge can tell
    blocked = set()
    for collision in wa.get("collisions", []):
        blocked.update(collision["categories"])
    return set(wa.get("channels", {})) - blocked


def build_report(db: Path | None = None, log: Path | None = None, hours: float = 24.0,
                 channels_data: dict | None = None, rules: dict | None = None,
                 env: dict | None = None, snapshot: dict | None = None,
                 days: int = 7, expand: str | None = None, expand_limit: int = 12,
                 max_posts: int = 20000) -> dict:
    """Everything category-wise, from data only. Pure: no printing, no writes."""
    rules = load_rules() if rules is None else rules
    env = load_env() if env is None else env
    snapshot = load_snapshot() if snapshot is None else snapshot
    if channels_data is None:
        try:
            sys.path.insert(0, str(ROOT / "bestgaa"))
            import channels_config  # noqa: PLC0415 - imported here so the report
            channels_data = channels_config.load_channels()   # works without the bot
        except Exception:
            channels_data = {"version": 1, "channels": []}
    since = time.time() - max(0.0, hours) * 3600.0

    view = channels_view(channels_data, rules)
    index = target_categories(channels_data, rules, env)
    declared = env_targets(env)
    tag_declared = {normalize_target(v) for v in
                    (env.get("AMAZON_TAG_CATEGORY_TARGETS", "") or "").split(",") if v.strip()}

    tags = our_amazon_tags()
    our_tags = frozenset(tags["bestgaa"] | tags["bridge"])
    wa = wa_surfaces(env, rules)
    filtered_wa = wa_filtered_keys(wa)

    conn = open_readonly(db) if db else None
    ledger: dict = {}
    pending: Counter = Counter()
    money: dict = {}
    quality: dict = {}
    prices: dict = {}
    links = link_index(None)
    rows: list[dict] = []
    capped = False
    ledger_error = ""
    hist = {"days": day_series(max(1, int(days))), "main": {}}
    db_ok = False
    if conn is not None:
        db_ok = True
        try:
            # One read of the ledger feeds the totals, the money view, the daily
            # history and the drill-down, so those four cannot disagree.
            links = link_index(conn)
            fetched = ledger_rows(conn, since, max_posts)
            rows, capped, ledger_error = fetched["rows"], fetched["capped"], fetched["error"]
            ledger = count_deliveries(
                [(r["target"], r["status"], r["error"], r["created_at"]) for r in rows], index)
            pending = count_pending(pending_rows(conn), index)
            money = count_money(rows, index, links, our_tags)
            hist = history(rows, index, max(1, int(days)))
            prices = price_index(conn)
            quality = deal_quality(rows, index, prices)
        finally:
            conn.close()

    logged = parse_log(log, since, rules)

    categories = []
    for definition in rules.get("categories", []):
        if not isinstance(definition, dict):
            continue
        key = str(definition.get("key", "")).strip().lower()
        if not key:
            continue
        label = str(definition.get("label", key))
        # `shelf_rows`, never `rows`: the ledger rows read above feed the money
        # view and the trend after this loop, and a shadowed name here silently
        # turned "what did this shelf publish" into "which channels claim it".
        shelf_rows = view["shelves"].get(key, [])
        enabled = [r for r in shelf_rows if r["enabled"]]
        env_target = {declared[key]} if declared.get(key) else set()
        # "configured" is every target that claims this shelf, including disabled
        # ones: a shelf turned OFF must still be visible, and must not be
        # misreported as a category nobody ever configured.
        targets = sorted({r["name"] for r in shelf_rows} | env_target)
        active = sorted({r["name"] for r in enabled} | env_target)
        bucket = ledger.get(key, {"sent": 0, "skipped": 0, "held": 0,
                                  "last_sent_at": 0.0, "reasons": Counter()})
        withheld = logged["withheld"].get(key, Counter())
        routes = sum(count for route_label, count in logged["routes"].items()
                     if route_label.strip().lower() in (label.lower(), key))
        unresolved_here = sorted({t for t in logged["unresolved"] if t in targets})
        tagged = sorted({t for t in active
                         if t in tag_declared or any(r["amazon_tag"] for r in enabled if r["name"] == t)})
        if not targets:
            state = "no_channel"
        elif not active:
            state = "disabled"
        elif bucket["sent"] == 0 and bucket["skipped"] == 0 and routes == 0:
            state = "silent"
        elif bucket["skipped"] > bucket["sent"]:
            state = "skipping"
        else:
            state = "ok"
        categories.append({
            "key": key,
            "label": label,
            "aliases": len(definition.get("aliases") or []),
            "state": state,
            "targets": targets,
            "active_targets": active,
            "tag_targets": tagged,
            "tagless_targets": sorted(set(active) - set(tagged)),
            "channels": [{"name": r["name"], "enabled": r["enabled"], "amazon_tag": r["amazon_tag"],
                          "sources": r["sources"], "source_names": r.get("source_names") or [],
                          "all_sources": r["all_sources"]}
                         for r in shelf_rows],
            "env_target": declared.get(key, ""),
            "deliveries": {"sent": bucket["sent"], "skipped": bucket["skipped"],
                           "held": bucket["held"]},
            "last_sent_at": bucket["last_sent_at"],
            "last_sent": ist_text(bucket["last_sent_at"]),
            "pending": int(pending.get(key, 0)),
            "routed": routes,
            "withheld": {"total": sum(withheld.values()),
                         "reasons": dict(withheld.most_common()),
                         "last_at": logged["withheld_last_at"].get(key, 0.0)},
            "skip_reasons": dict(bucket["reasons"].most_common(6)),
            "unresolved_targets": unresolved_here,
            "money": money_view(money, key),
            "quality": quality_view(quality.get(key) or {}),
            "history": history_view(hist, key),
            "surfaces": {
                "telegram": {"targets": active, "amazon_tag": bool(tagged)},
                "whatsapp": {"channel": wa["channels"].get(key, ""),
                             "filtered": key in filtered_wa,
                             "policy": f"category:{key}" if key in filtered_wa else "",
                             "amazon_tag": key in wa["tag_categories"]},
            },
        })

    known_keys = {c["key"] for c in categories}
    unclassified = logged["withheld"].get("unknown", Counter())
    foreign = {key: value for key, value in logged["withheld"].items()
               if key not in known_keys and key != "unknown"}

    main_bucket = ledger.get("main", {"sent": 0, "skipped": 0, "held": 0,
                                      "last_sent_at": 0.0, "reasons": Counter()})
    report = {
        "generated_at": time.time(),
        "window_hours": hours,
        "since": since,
        "db": str(db) if db else "",
        "db_readable": db_ok,
        "log": logged["log"],
        "log_lines_read": logged["lines_read"],
        "rules_version": rules.get("version", 0),
        "rules_load_error": rules.get("load_error", ""),
        "bot_rules_version": snapshot.get("category_rules_version"),
        "bot_started_at": snapshot.get("started_at", 0),
        "bot_rules_fingerprint": fingerprint(BOT_RULES),
        "bridge_rules_fingerprint": fingerprint(BRIDGE_RULES),
        "categories": categories,
        "main": {
            "targets": sorted({r["name"] for r in view["main"]}),
            "channels": view["main"],
            "deliveries": {"sent": main_bucket["sent"], "skipped": main_bucket["skipped"],
                           "held": main_bucket["held"]},
            "last_sent_at": main_bucket["last_sent_at"],
            "last_sent": ist_text(main_bucket["last_sent_at"]),
            "pending": int(pending.get("main", 0)),
            "skip_reasons": dict(main_bucket["reasons"].most_common(6)),
            "money": money_view(money, "main"),
            "quality": quality_view(quality.get("main") or {}),
            "history": history_view(hist, "main"),
        },
        "history_days": hist["days"],
        "money_totals": money_totals(rows, links, our_tags),
        "quality_fleet": quality_view(quality.get("_fleet") or {}),
        "source_matrix": source_matrix(quality, view, rules),
        "price_rows": sum(len(v) for v in prices.values()),
        "our_amazon_tags": {"bestgaa": sorted(tags["bestgaa"]), "bridge": sorted(tags["bridge"]),
                            "read_error": tags["read_error"]},
        "link_tables": links["tables"],
        "ledger_capped": capped,
        "ledger_error": ledger_error,
        "max_posts": int(max_posts),
        "whatsapp": wa,
        "recent": ({expand: recent_for(rows, index, links, our_tags, expand, expand_limit)}
                   if expand else {}),
        "unrouted_targets": sorted(k for k in ledger if k.startswith("target:")),
        "unclassified_withheld": {
            "total": sum(unclassified.values()),
            "reasons": dict(unclassified.most_common()),
            "last_at": logged["withheld_last_at"].get("unknown", 0.0),
            "note": ("posts the classifier did not place in any category; a filtered "
                     "shelf never receives them, by design"),
        },
        "foreign_category_withheld": {
            key: {"total": sum(value.values()), "reasons": dict(value.most_common())}
            for key, value in sorted(foreign.items())
        },
        "log_signals": {
            "collisions": logged["collisions"],
            "unresolved": dict(logged["unresolved"]),
            "routing_disabled": logged["routing_disabled"],
            "last_route": ist_text(logged["last_route_at"]),
        },
        "totals": {
            "shelves": len(categories),
            "shelves_with_channel": sum(1 for c in categories if c["targets"]),
            "sent": sum(c["deliveries"]["sent"] for c in categories),
            "skipped": sum(c["deliveries"]["skipped"] for c in categories),
            "routed": sum(c["routed"] for c in categories),
            "withheld": sum(c["withheld"]["total"] for c in categories),
            "main_sent": main_bucket["sent"],
            "paid_posts": sum(1 for r in rows if r["status"] == "sent" and not r["error"]
                              and post_money(r["text"], links, our_tags)[0] == "owned"),
        },
    }
    report["findings"] = findings(report)
    return report


def findings(report: dict) -> list[dict]:
    """What an operator must act on, per shelf. Severity: error > warn > info."""
    found: list[dict] = []

    def add(severity: str, kind: str, category: str, detail: str) -> None:
        found.append({"severity": severity, "kind": kind, "category": category, "detail": detail})

    if report["rules_load_error"]:
        add("error", "rules_unreadable", "", report["rules_load_error"])
    if (report["bot_rules_fingerprint"] and report["bridge_rules_fingerprint"]
            and report["bot_rules_fingerprint"] != report["bridge_rules_fingerprint"]):
        add("error", "rules_parity", "",
            "bestgaa/category_rules.json and tg-wa-bridge/category_rules.json differ: "
            "Telegram and WhatsApp would classify the same product differently")
    bot_version = report.get("bot_rules_version")
    if bot_version is not None and int(bot_version or 0) != int(report["rules_version"] or 0):
        add("error", "rules_version_drift", "",
            f"the running bot classified with rules v{bot_version}; this checkout has "
            f"v{report['rules_version']} - a deploy or approved restart is pending")
    for message in report["log_signals"]["routing_disabled"]:
        add("error", "routing_disabled", "", f"the bot logged: CATEGORY ROUTING DISABLED | {message}")
    for collision in report["log_signals"]["collisions"]:
        add("error", "target_collision", collision["categories"],
            f"@{collision['target']} is assigned to {collision['categories']}; "
            "the bot disabled those category routes")
    if not report["db_readable"] and report["db"]:
        add("warn", "db_unreadable", "", f"cannot open {report['db']} read-only; counts are zero")
    if report.get("ledger_error"):
        add("warn", "ledger_unreadable", "",
            f"the delivery ledger could not be read ({report['ledger_error']}); "
            "sends, money and trend are zero")
    if report["ledger_capped"]:
        add("warn", "ledger_capped", "",
            f"only the newest {report['max_posts']} deliveries were read, so money and trend "
            "cover part of this window - narrow --hours or raise --max-posts")

    tags = report["our_amazon_tags"]
    if tags["read_error"]:
        add("warn", "amazon_tag_unreadable", "",
            f"OUR_AMAZON_TAGS could not be read from source ({tags['read_error']}); "
            "no Amazon link can be proved ours")
    elif tags["bestgaa"] and tags["bridge"] and set(tags["bestgaa"]) != set(tags["bridge"]):
        add("error", "amazon_tag_parity", "",
            f"the bot publishes with {tags['bestgaa']} and the bridge with {tags['bridge']}: "
            "one surface credits a different Associates account for the same deal")
    elif not (tags["bestgaa"] or tags["bridge"]):
        add("warn", "amazon_tag_unknown", "",
            "neither service exposes OUR_AMAZON_TAGS, so Amazon links are counted as ours "
            "without proof (route amazon_tag_unverified)")

    def money_findings(label: str, key: str, earned: dict) -> None:
        """What the published posts on one destination say about the money.

        Main is judged by the same rule as a shelf: it is where most posts go, so
        a link that pays nobody there is the most expensive kind.
        """
        if earned["foreign_tag"]:
            add("warn", "foreign_tag_posted", key,
                f"{earned['foreign_tag']} post(s) on '{label}' carry an Amazon tag that is not "
                f"ours (ours: {tags['bestgaa'] or tags['bridge'] or 'unknown'}) - those clicks "
                "pay a stranger. Warn, not error: blocking a deploy cannot un-publish them")
        if earned["earns_nothing"]:
            routes = ", ".join(f"{r} x{n}" for r, n in list(earned["routes"].items())[:3])
            add("warn", "unmonetized_shelf", key,
                f"{earned['earns_nothing']} of {earned['posts']} post(s) published on '{label}' "
                f"carry a link that pays nobody ({routes or 'no route'})")
        if earned["posts"] and earned["unattributed"] == earned["posts"]:
            add("info", "money_blind", key,
                f"none of the {earned['posts']} post(s) on '{label}' could be matched to "
                f"link_cache/owned_link_proofs ({', '.join(report['link_tables']) or 'no tables'}), "
                "so commission here is unproven rather than absent")

    fleet = report["quality_fleet"]
    fleet_avg = fleet.get("avg_score")

    def quality_findings(label: str, key: str, earned: dict, declared=None,
                         unrestricted: bool = False) -> None:
        """Was this destination given good deals, and by the sources it declares?

        `premium_score` and `posted_deals.price` are the bot's own numbers; this
        only compares a shelf against the fleet, and says "unproven" when the
        columns are empty rather than inventing a verdict.
        """
        if earned["posts"] and not earned["scored"]:
            add("info", "quality_blind", key,
                f"none of the {earned['posts']} post(s) on '{label}' carry a premium_score, so "
                "deal quality here is unproven (rows rendered before that column existed?)")
        elif (earned["scored"] >= 3 and fleet_avg and earned["avg_score"] is not None
                and earned["avg_score"] < 0.5 * fleet_avg):
            add("warn", "weak_shelf_quality", key,
                f"'{label}' averages a premium_score of {earned['avg_score']} over "
                f"{earned['scored']} scored post(s) against {fleet_avg} fleet-wide: the weakest "
                "deals are landing on this shelf")
        if earned["posts"] and not earned["price_rows"]:
            add("info", "price_blind", key,
                f"no posted_deals.price rows sit behind '{label}' posts, so its rupee spread is "
                "unknown rather than cheap")
        if earned.get("zero_score"):
            add("info", "zero_score_posts", key,
                f"{earned['zero_score']} post(s) on '{label}' scored 0: no discount, no price "
                "bonus and no lowest-price claim")
        if declared is not None and not unrestricted:
            undeclared = sorted(set(earned["sources"]) - set(declared) - {"(no source)"})
            if undeclared:
                add("warn", "undeclared_source", key,
                    f"'{label}' received posts from {', '.join(undeclared)}, which its channels do "
                    "not declare now - either the config changed after those posts or a renamed "
                    "source is still feeding this shelf")

    wa = report["whatsapp"]
    if wa["configured"]:
        for collision in wa["collisions"]:
            add("error", "wa_category_collision", ",".join(collision["categories"]),
                f"{collision['jid']}: {collision['why']}, so the bridge keeps the existing "
                "target policy and these categories are NOT filtered on WhatsApp "
                "(give each category its own channel)")
        for name in wa["unknown_env"]:
            add("warn", "unknown_surface_channel", "",
                f"{name} is set but no category has that key - the bridge builds its category "
                "channels from category_rules.json, so this value is ignored")
        if wa["masked"]:
            add("info", "wa_values_masked", "",
                f"{wa['masked']} WhatsApp channel value(s) are not plain JIDs and are shown "
                "masked; a shared-channel collision cannot be proved offline")

    main_sent = report["main"]["deliveries"]["sent"]
    for shelf in report["categories"]:
        key = shelf["key"]
        for target in shelf["unresolved_targets"]:
            add("warn", "target_unresolved", key,
                f"@{target} was not reachable; those deals fell back to Main instead of this shelf")

        # Cross-surface coverage: a shelf that exists on one service only is a
        # silent half-deploy, and it is checked before the no_channel `continue`
        # so a WhatsApp-only shelf is still reported.
        surface = shelf["surfaces"]
        tg_on, wa_on = bool(surface["telegram"]["targets"]), bool(surface["whatsapp"]["channel"])
        if wa["configured"] and tg_on and not wa_on:
            add("warn", "wa_surface_missing", key,
                f"Telegram routes '{shelf['label']}' to "
                f"{', '.join('@' + t for t in surface['telegram']['targets'])} but "
                f"WA_CHANNEL_{key.upper()} is unset: on WhatsApp those deals go to the core "
                "channels unfiltered")
        if wa["configured"] and wa_on and not tg_on:
            add("warn", "tg_surface_missing", key,
                f"WhatsApp has a channel for '{shelf['label']}' "
                f"({surface['whatsapp']['channel']}) but Telegram has no target for it: "
                "the same deal reaches one audience only")
        if wa_on and not surface["whatsapp"]["filtered"]:
            add("warn", "wa_shelf_unfiltered", key,
                f"{surface['whatsapp']['channel']} is set for '{shelf['label']}' but the bridge "
                "will not give it a category policy (see the collision finding), so it behaves "
                "like a core channel")
        if wa["configured"] and tg_on and wa_on \
                and surface["telegram"]["amazon_tag"] != surface["whatsapp"]["amazon_tag"]:
            declared = "Telegram" if surface["telegram"]["amazon_tag"] else "WhatsApp"
            add("warn", "amazon_tag_surface_mismatch", key,
                f"the Amazon tag is declared for '{shelf['label']}' on {declared} only: the "
                "other surface strips the monetized link, so the same deal earns on one and "
                "not the other")

        money_findings(shelf["label"], key, shelf["money"])
        quality_findings(shelf["label"], key, shelf["quality"],
                         declared=report["source_matrix"]["declared"].get(key),
                         unrestricted=bool(report["source_matrix"]["unrestricted"].get(key)))

        if not shelf["targets"]:
            add("warn", "no_channel", key,
                f"no channel or TG_CATEGORY_{key.upper()}_TARGET is configured for "
                f"'{shelf['label']}' - nothing can ever receive it")
            continue
        if shelf["state"] == "disabled":
            add("warn", "disabled_shelf", key,
                f"{', '.join('@' + c['name'] for c in shelf['channels'])} is disabled: "
                f"'{shelf['label']}' is configured but receives nothing")
        if shelf["state"] == "silent" and main_sent:
            add("warn", "silent_shelf", key,
                f"Main sent {main_sent} post(s) in this window but '{shelf['label']}' sent 0 "
                f"and was routed 0 - check its target, its sources and the rules version")
        if shelf["state"] == "skipping":
            top = ", ".join(f"{reason} x{count}" for reason, count in
                            list(shelf["skip_reasons"].items())[:3]) or "no reason recorded"
            add("warn", "skipping_shelf", key,
                f"'{shelf['label']}' skipped {shelf['deliveries']['skipped']} vs "
                f"{shelf['deliveries']['sent']} sent ({top})")
        routed, withheld = shelf["routed"], shelf["withheld"]["total"]
        if withheld and routed and withheld >= routed:
            reasons = ", ".join(f"{r} x{n}" for r, n in list(shelf["withheld"]["reasons"].items())[:3])
            add("warn", "withheld_majority", key,
                f"{withheld} post(s) classified for '{shelf['label']}' were withheld from it "
                f"vs {routed} routed ({reasons})")
        if shelf["tagless_targets"]:
            add("info", "tagless_shelf", key,
                f"Amazon links stay withheld on {', '.join('@' + t for t in shelf['tagless_targets'])} "
                "until the tag is declared (AMAZON_TAG_CATEGORY_TARGETS / the channel's amazon_tag) - "
                "this is the documented policy, not a fault")
    money_findings("Main", "", report["main"]["money"])
    quality_findings("Main", "", report["main"]["quality"], declared=None, unrestricted=True)
    dead = report["source_matrix"]["declared_only"]
    if dead:
        add("info", "dead_source", "",
            f"{len(dead)} declared source(s) produced nothing in this window: "
            f"{', '.join(dead[:6])}{' …' if len(dead) > 6 else ''} - widen --hours before "
            "concluding they are dead")
    unclassified_total = report["unclassified_withheld"]["total"]
    if unclassified_total:
        routed_total = report["totals"]["routed"]
        severity = "warn" if unclassified_total >= max(1, routed_total) else "info"
        reasons = ", ".join(f"{reason} x{count}" for reason, count in
                            list(report["unclassified_withheld"]["reasons"].items())[:3])
        add(severity, "unclassified_withheld", "",
            f"{unclassified_total} post(s) were withheld from every shelf because no category "
            f"matched ({reasons or 'no reason logged'}); at {severity} level this means "
            + ("abstention is out-running routing - the taxonomy may need an alias"
               if severity == "warn" else "normal for mixed or unclear posts"))
    for key, value in report["foreign_category_withheld"].items():
        add("warn", "unknown_category_withheld", key,
            f"the bot withheld {value['total']} post(s) for category '{key}', which is not in the "
            "installed taxonomy - the running bot and this checkout classify differently")
    for target in report["unrouted_targets"]:
        add("warn", "unknown_target", target.replace("target:", ""),
            f"the ledger has deliveries for @{target.replace('target:', '')}, which no config claims; "
            "its sends are counted under that name only")
    order = {"error": 0, "warn": 1, "info": 2}
    found.sort(key=lambda item: (order.get(item["severity"], 9), item["kind"], item["category"]))
    return found


# ------------------------------------------------------------------ rendering --

SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def sparkline(values) -> str:
    """A trend in eight block characters: readable over SSH, no chart library."""
    series = [max(0, int(v or 0)) for v in (values or [])]
    if not series:
        return ""
    top = max(series)
    if not top:
        return SPARK[0] * len(series)
    return "".join(SPARK[min(7, int(round(v * 7 / top)))] for v in series)


def render(report: dict) -> str:
    lines = [
        "CATEGORY REPORT | window={hours:g}h db={db}{dbstate} rules=v{version}{botversion} "
        "shelves={shelves} ({with_channel} reachable)".format(
            hours=report["window_hours"],
            db=report["db"] or "-",
            dbstate="" if report["db_readable"] else " (NOT READABLE)",
            version=report["rules_version"],
            botversion=(f" / bot v{report['bot_rules_version']}"
                        if report.get("bot_rules_version") is not None else " / bot never reported"),
            shelves=report["totals"]["shelves"],
            with_channel=report["totals"]["shelves_with_channel"]),
    ]
    totals = report["totals"]
    lines.append(
        "  TOTALS | shelf-sent={sent} shelf-skipped={skipped} routed={routed} withheld={withheld} "
        "| Main sent={main}".format(**totals, main=totals["main_sent"]))
    for shelf in report["categories"]:
        targets = ", ".join(
            "@{name}{state}{tag}".format(
                name=t,
                state="" if t in shelf["active_targets"] else "(disabled)",
                tag="" if t in shelf["tag_targets"] else "(tagless)")
            for t in shelf["targets"]) or "none"
        lines.append("  {label} [{key}] state={state} aliases={aliases}".format(
            label=shelf["label"], key=shelf["key"], state=shelf["state"], aliases=shelf["aliases"]))
        lines.append("    targets : {targets}".format(targets=targets))
        lines.append("    delivery: sent={sent} skipped={skipped} held={held} pending={pending} "
                     "| last send {last}".format(
                         pending=shelf["pending"], last=shelf["last_sent"],
                         **shelf["deliveries"]))
        lines.append("    classify: routed={routed} withheld={withheld}{reasons}".format(
            routed=shelf["routed"], withheld=shelf["withheld"]["total"],
            reasons=(" (" + ", ".join(f"{r} x{n}" for r, n in
                                      list(shelf["withheld"]["reasons"].items())[:3]) + ")")
            if shelf["withheld"]["reasons"] else ""))
        if shelf["skip_reasons"]:
            lines.append("    skips   : " + ", ".join(
                f"{reason} x{count}" for reason, count in list(shelf["skip_reasons"].items())[:4]))
        earned = dict(shelf["money"])
        if earned["posts"]:
            route_text = ", ".join(f"{route} x{count}" for route, count in
                                   list(earned["routes"].items())[:3])
            earned["routes"] = f" ({route_text})" if route_text else ""
            lines.append("    money   : owned={owned} earns-nothing={earns_nothing} "
                         "foreign-tag={foreign_tag} unattributed={unattributed} "
                         "of {posts} post(s){routes}".format(**earned))
        else:
            lines.append("    money   : nothing delivered in this window")
        sent_series = [day["sent"] for day in shelf["history"]]
        lines.append("    trend   : {days}d sent {spark} ({sent}) | skipped ({skipped})".format(
            days=len(sent_series), spark=sparkline(sent_series),
            sent="/".join(str(v) for v in sent_series) or "-",
            skipped="/".join(str(day["skipped"]) for day in shelf["history"]) or "-"))
        earned_q = shelf["quality"]
        if earned_q["posts"]:
            score_text = (f"avg {earned_q['avg_score']} (min {earned_q['min_score']}, "
                          f"max {earned_q['max_score']}) over {earned_q['scored']} scored"
                          if earned_q["scored"] else "no premium_score on any post")
            price_text = (f" | price Rs{earned_q['price_min']}-Rs{earned_q['price_max']}, "
                          f"median Rs{earned_q['price_median']} "
                          f"({earned_q['under_99']} under Rs99)"
                          if earned_q["price_rows"] else " | no recorded prices")
            lines.append(f"    quality : {score_text}{price_text}")
        else:
            lines.append("    quality : nothing delivered in this window")
        observed = ", ".join(f"{name} x{count}" for name, count in
                             list(earned_q["sources"].items())[:5]) or "none"
        declared_here = report["source_matrix"]["declared"].get(shelf["key"]) or []
        lines.append("    sources : {observed}{declared}".format(
            observed=observed,
            declared=("" if report["source_matrix"]["unrestricted"].get(shelf["key"])
                      else f" | declared: {', '.join(declared_here) or 'none'}")))
        surface = shelf["surfaces"]
        whatsapp = surface["whatsapp"]
        lines.append("    surface : telegram {tg}{tgtag} | whatsapp {wa}{policy}{watag}".format(
            tg=", ".join("@" + t for t in surface["telegram"]["targets"]) or "none",
            tgtag="(tag)" if surface["telegram"]["amazon_tag"] else "(tagless)",
            wa=whatsapp["channel"] or "not configured",
            policy=(f" {whatsapp['policy']}" if whatsapp["policy"]
                    else (" (NOT filtered)" if whatsapp["channel"] else "")),
            watag="(tag)" if whatsapp["amazon_tag"] else ""))
    main = report["main"]
    lines.append("  MAIN (unfiltered) targets={count} sent={sent} skipped={skipped} held={held} "
                 "pending={pending} | last send {last}".format(
                     count=len(main["targets"]), pending=main["pending"],
                     last=main["last_sent"], **main["deliveries"]))
    money = dict(report["money_totals"])
    if money["posts"]:
        route_text = ", ".join(f"{route} x{count}" for route, count in
                               list(money["routes"].items())[:4])
        money["routes"] = f" ({route_text})" if route_text else ""
        lines.append("  MONEY | {posts} delivered post(s): owned={owned} "
                     "earns-nothing={earns_nothing} foreign-tag={foreign_tag} "
                     "unattributed={unattributed}{routes}".format(**money))
    else:
        lines.append("  MONEY | nothing delivered in this window, so nothing to attribute")
    fleet = report["quality_fleet"]
    if fleet["posts"]:
        lines.append("  QUALITY | fleet avg premium_score {avg} over {scored} scored post(s)"
                     "{price} | sources: {sources}".format(
                         avg=fleet["avg_score"] if fleet["avg_score"] is not None else "-",
                         scored=fleet["scored"],
                         price=(f" | price median Rs{fleet['price_median']}"
                                f" (Rs{fleet['price_min']}-Rs{fleet['price_max']},"
                                f" {fleet['under_99']} under Rs99)")
                         if fleet["price_rows"] else "",
                         sources=", ".join(f"{name} x{count}" for name, count in
                                           list(fleet["sources"].items())[:6]) or "none"))
    matrix = report["source_matrix"]
    if matrix["sources"]:
        lines.append("  SOURCE MATRIX | sends per source per shelf"
                     + (" (DEAD = declared, nothing arrived)" if matrix["declared_only"] else ""))
        for source in matrix["sources"]:
            cells = ", ".join(f"{column} {count}" for column, count in
                              sorted(matrix["cells"].get(source, {}).items()))
            lines.append(f"    {source:<24}: {cells}")
        for source in matrix["declared_only"]:
            owners = sorted(key for key, names in matrix["declared"].items() if source in names)
            lines.append(f"    {source:<24}: DEAD (declared by {', '.join(owners) or 'nobody'})")
    whatsapp = report["whatsapp"]
    lines.append("  WHATSAPP | {channels} category channel(s), {core} core, tags: {tags}"
                 "{state}".format(
                     channels=len(whatsapp["channels"]), core=len(whatsapp["core"]),
                     tags=", ".join(whatsapp["tag_categories"]) or "none",
                     state="" if whatsapp["configured"] else " (no WA_CHANNEL_* set: not judged)"))
    for key, items in report["recent"].items():
        lines.append(f"  RECENT [{key}] newest {len(items)} delivery row(s):")
        for item in items:
            reason = f" reason={item['reason']}" if item["reason"] else ""
            paid = f" money={item['money']}/{item['route']}" if item["money"] else ""
            lines.append("    #{qid} {at} -> {target} {status}{reason}{paid} | {snippet}".format(
                qid=item["queue_id"], at=item["at"], target=item["target"] or "?",
                status=item["status"], reason=reason, paid=paid,
                snippet=item["snippet"][:80] or "(no text)"))
    unclassified = report["unclassified_withheld"]
    if unclassified["total"]:
        lines.append("  UNCLASSIFIED | {total} post(s) withheld from every shelf (no category "
                     "matched){reasons}".format(
                         total=unclassified["total"],
                         reasons=(" - " + ", ".join(f"{r} x{n}" for r, n in
                                                    list(unclassified["reasons"].items())[:3]))
                         if unclassified["reasons"] else ""))
    for key, value in report["foreign_category_withheld"].items():
        lines.append(f"  UNKNOWN CATEGORY | the bot withheld {value['total']} post(s) for "
                     f"'{key}', which this taxonomy does not define")
    signals = report["log_signals"]
    lines.append("  LOG SIGNALS | last category route {last} | collisions={collisions} "
                 "unresolved={unresolved} routing-disabled={disabled}".format(
                     last=signals["last_route"], collisions=len(signals["collisions"]),
                     unresolved=sum(signals["unresolved"].values()),
                     disabled=len(signals["routing_disabled"])))
    hard = [f for f in report["findings"] if f["severity"] in ("error", "warn")]
    info = [f for f in report["findings"] if f["severity"] == "info"]
    lines.append("  FINDINGS: {hard} actionable, {info} informational".format(
        hard=len(hard), info=len(info)))
    for item in report["findings"]:
        where = f" [{item['category']}]" if item["category"] else ""
        lines.append("    {sev:5} {kind}{where}: {detail}".format(
            sev=item["severity"].upper(), kind=item["kind"], where=where, detail=item["detail"]))
    if not report["findings"]:
        lines.append("    every reachable shelf received posts, no route was disabled, "
                     "and both services classify with the same rules")
    return "\n".join(lines)


HTML_CSS = """
:root{color-scheme:dark}
body{margin:0;padding:24px;background:#0b131f;color:#e2e8f0;
     font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 8px;color:#c7d4e5}
.sub{color:#94a3b8;font-size:12px;margin:0 0 18px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 4px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1d2b3f;vertical-align:top}
th{color:#9dacc1;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
tr.error td{background:#2a1215}tr.warn td{background:#241a12}tr.info td{background:#101a24}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;background:#25364a}
.pill.ok{background:#14532d}.pill.warn{background:#7c2d12}.pill.error{background:#7f1d1d}
.muted{color:#94a3b8;font-size:12px}.bad{color:#fca5a5}.good{color:#86efac}
.spark{font-size:15px;letter-spacing:1px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0 4px}
.stat{background:#101a27;border:1px solid #1d2b3f;border-radius:10px;padding:8px 12px;min-width:110px}
.stat b{display:block;font-size:19px}.stat span{color:#94a3b8;font-size:11px}
footer{margin-top:26px;color:#94a3b8;font-size:12px;border-top:1px solid #1d2b3f;padding-top:10px}
code{background:#101a27;padding:1px 5px;border-radius:5px;font-size:12px}
"""


def render_html(report: dict) -> str:
    """A shareable weekly/shift report: one self-contained HTML file.

    No JavaScript, no external asset, no font or CDN reference - it opens from a
    phone, an email attachment or `file://` and cannot fetch or run anything.
    Every value passes through `html.escape`, because shelf labels, channel names,
    source names and logged reasons are all operator-controlled text.
    """
    def e(value) -> str:
        """html.escape wants str; report values are ints, floats and None too."""
        return html.escape(str(value if value is not None else ""))

    totals = report["totals"]
    fleet = report["quality_fleet"]
    matrix = report["source_matrix"]
    findings = report["findings"]
    parts: list[str] = []
    parts.append("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    parts.append(f"<title>Category report {e(ist_text(report['generated_at']))}</title>")
    parts.append(f"<style>{HTML_CSS}</style></head><body>")
    parts.append("<h1>What each shelf actually received</h1>")
    parts.append(
        '<p class="sub">window {hours:g}h &middot; generated {stamp} &middot; installed rules '
        'v{version} &middot; bot reported v{bot} &middot; database <code>{db}</code>{dbstate}'
        '</p>'.format(
            hours=report["window_hours"], stamp=e(ist_text(report["generated_at"])),
            version=e(report["rules_version"]),
            bot=e(report.get("bot_rules_version") if report.get("bot_rules_version") is not None
                  else "never"),
            db=e(report["db"] or "-"),
            dbstate="" if report["db_readable"] else ' <span class="bad">(NOT READABLE)</span>'))
    money = report["money_totals"]
    stats = [
        (totals["shelves_with_channel"], "shelves reachable"),
        (totals["sent"], "shelf sends"),
        (totals["skipped"], "shelf skips"),
        (totals["routed"], "routed by classifier"),
        (totals["withheld"], "withheld from shelves"),
        (totals["main_sent"], "Main sends"),
        (money["owned"], "posts that pay us"),
        (money["earns_nothing"] + money["foreign_tag"], "posts paying nobody/stranger"),
        (fleet["avg_score"] if fleet["avg_score"] is not None else "-", "fleet avg score"),
    ]
    parts.append('<div class="stats">' + "".join(
        f'<div class="stat"><b>{e(value)}</b><span>{e(label)}</span></div>'
        for value, label in stats) + "</div>")

    parts.append(f"<h2>Findings ({len(findings)})</h2>")
    if findings:
        parts.append("<table><thead><tr><th>Severity</th><th>Finding</th><th>Shelf</th>"
                     "<th>What it means</th></tr></thead><tbody>")
        for item in findings:
            parts.append('<tr class="{sev}"><td><span class="pill {sev}">{sev}</span></td>'
                         "<td>{kind}</td><td>{where}</td><td>{detail}</td></tr>".format(
                             sev=e(item["severity"]), kind=e(item["kind"]),
                             where=e(item["category"] or "-"), detail=e(item["detail"])))
        parts.append("</tbody></table>")
    else:
        parts.append('<p class="good">No findings: every reachable shelf received posts, no '
                     'category route was disabled, and both services classify with the same '
                     'rules.</p>')

    parts.append("<h2>Per shelf</h2>")
    parts.append("<table><thead><tr><th>Shelf</th><th>State</th><th>Targets</th><th>Sent</th>"
                 "<th>Skip</th><th>Hold</th><th>Pend</th><th>Routed</th><th>Withheld</th>"
                 "<th>Money</th><th>Quality</th><th>Trend</th><th>Surfaces</th>"
                 "<th>Last send</th></tr></thead><tbody>")
    for shelf in report["categories"]:
        earned, quality = shelf["money"], shelf["quality"]
        surface = shelf["surfaces"]
        targets = ", ".join(
            "@{name}{state}{tag}".format(
                name=e(t),
                state="" if t in shelf["active_targets"] else " (disabled)",
                tag="" if t in shelf["tag_targets"] else " (tagless)")
            for t in shelf["targets"]) or '<span class="muted">none</span>'
        money_cell = ("{owned} paid &middot; {nothing} earn nothing{foreign}{unproven}"
                      '<br><span class="muted">{posts} post(s)</span>'.format(
                          owned=e(earned["owned"]), nothing=e(earned["earns_nothing"]),
                          foreign=(f' &middot; <span class="bad">{e(earned["foreign_tag"])}'
                                   ' foreign tag</span>') if earned["foreign_tag"] else "",
                          unproven=(f' &middot; {e(earned["unattributed"])} unproven')
                          if earned["unattributed"] else "",
                          posts=e(earned["posts"]))
                      ) if earned["posts"] else '<span class="muted">nothing delivered</span>'
        quality_cell = ("avg {avg} (min {lo}, max {hi})<br><span class=\"muted\">{price}</span>"
                        .format(avg=e(quality["avg_score"]), lo=e(quality["min_score"]),
                                hi=e(quality["max_score"]),
                                price=(f"Rs{e(quality['price_min'])}-Rs{e(quality['price_max'])}, "
                                       f"median Rs{e(quality['price_median'])}")
                        if quality["price_rows"] else "no recorded prices")) \
            if quality["scored"] else '<span class="muted">unscored</span>'
        series = shelf["history"]
        trend_cell = ('<span class="spark" title="{title}">{spark}</span>'
                      '<br><span class="muted">{days}d</span>'.format(
                          title=e("sends per IST day: "
                                  + "/".join(str(d["sent"]) for d in series) or "none"),
                          spark=e(sparkline(series)), days=e(len(series)))
                      ) if series else '<span class="muted">no trend</span>'
        surface_cell = ("TG {tg}{tgtag}<br>WA {wa}".format(
            tg=", ".join("@" + e(t) for t in surface["telegram"]["targets"])
            or '<span class="muted">none</span>',
            tgtag="" if surface["telegram"]["amazon_tag"] else ' <span class="muted">(tagless)</span>',
            wa=(e(surface["whatsapp"]["channel"])
                + (f' <span class="pill ok">{e(surface["whatsapp"]["policy"])}</span>'
                   if surface["whatsapp"]["filtered"]
                   else ' <span class="pill warn">not filtered</span>'))
            if surface["whatsapp"]["channel"] else '<span class="muted">not configured</span>'))
        skips = ", ".join(f"{e(r)} x{e(n)}" for r, n in list(shelf["skip_reasons"].items())[:3])
        withheld = ", ".join(f"{e(r)} x{e(n)}" for r, n in
                             list(shelf["withheld"]["reasons"].items())[:3])
        parts.append(
            "<tr><td><b>{label}</b><br><span class=\"muted\">{key} &middot; {aliases} aliases"
            "</span></td><td><span class=\"pill {statecls}\">{state}</span></td>"
            "<td>{targets}</td><td>{sent}</td><td>{skipped}{skips}</td><td>{held}</td>"
            "<td>{pending}</td><td>{routed}</td><td>{withheld_total}{withheld_reasons}</td>"
            "<td>{money}</td><td>{quality}</td><td>{trend}</td><td>{surface}</td>"
            "<td class=\"muted\">{last}</td></tr>".format(
                label=e(shelf["label"]), key=e(shelf["key"]), aliases=e(shelf["aliases"]),
                state=e(shelf["state"]),
                statecls="ok" if shelf["state"] == "ok" else "warn",
                targets=targets, sent=e(shelf["deliveries"]["sent"]),
                skipped=e(shelf["deliveries"]["skipped"]),
                skips=f'<br><span class="muted">{skips}</span>' if skips else "",
                held=e(shelf["deliveries"]["held"]), pending=e(shelf["pending"]),
                routed=e(shelf["routed"]), withheld_total=e(shelf["withheld"]["total"]),
                withheld_reasons=(f'<br><span class="muted">{withheld}</span>' if withheld else ""),
                money=money_cell, quality=quality_cell, trend=trend_cell, surface=surface_cell,
                last=e(shelf["last_sent"] or "never")))
    parts.append("</tbody></table>")

    if matrix["sources"] or matrix["declared_only"]:
        parts.append("<h2>Which source feeds which shelf</h2>")
        parts.append("<table><thead><tr><th>Source</th>"
                     + "".join(f"<th>{e(column)}</th>" for column in matrix["columns"])
                     + "<th>Total</th></tr></thead><tbody>")
        for source in matrix["sources"] + matrix["declared_only"]:
            dead = source in matrix["declared_only"]
            cells = matrix["cells"].get(source, {})
            parts.append("<tr{cls}><td>{name}{mark}</td>{cells}<td>{total}</td></tr>".format(
                cls=' class="info"' if dead else "",
                name=e(source),
                mark=(' <span class="pill warn">DEAD</span>' if dead else ""),
                cells="".join(f"<td>{e(cells.get(column, 0) or '')}</td>"
                              for column in matrix["columns"]),
                total=e(matrix["totals"].get(source, 0))))
        parts.append("</tbody></table>")

    main = report["main"]
    parts.append("<h2>Main (unfiltered) and everything else</h2>")
    parts.append("<table><tbody>")
    parts.append("<tr><td>Main</td><td>sent {sent}, skipped {skipped}, held {held}, pending "
                 "{pending}</td><td class=\"muted\">last send {last}</td></tr>".format(
                     sent=e(main["deliveries"]["sent"]), skipped=e(main["deliveries"]["skipped"]),
                     held=e(main["deliveries"]["held"]), pending=e(main["pending"]),
                     last=e(main["last_sent"] or "never")))
    unclassified = report["unclassified_withheld"]
    if unclassified["total"]:
        parts.append("<tr><td>Unclassified</td><td>{total} post(s) withheld from every shelf"
                     "</td><td class=\"muted\">{reasons}</td></tr>".format(
                         total=e(unclassified["total"]),
                         reasons=e(", ".join(f"{r} x{n}" for r, n in
                                             list(unclassified["reasons"].items())[:4]))))
    for key, value in report["foreign_category_withheld"].items():
        parts.append("<tr><td>Unknown category</td><td>the bot withheld {total} post(s) for "
                     "{key}</td><td class=\"muted\">not in the installed taxonomy</td></tr>"
                     .format(total=e(value["total"]), key=e(key)))
    for target in report["unrouted_targets"]:
        parts.append("<tr><td>Unclaimed target</td><td>{target}</td><td class=\"muted\">no "
                     "config claims it</td></tr>".format(target=e(target)))
    parts.append("</tbody></table>")

    whatsapp = report["whatsapp"]
    parts.append("<footer>WhatsApp surface: {channels} category channel(s), {core} core, tags "
                 "{tags}{state}. Read-only report: the database was opened <code>mode=ro</code>, "
                 "nothing was queued, sent or restarted. Reproduce with "
                 "<code>python3 ops/category_report.py --hours {hours:g}</code>.</footer>".format(
                     channels=e(len(whatsapp["channels"])), core=e(len(whatsapp["core"])),
                     tags=e(", ".join(whatsapp["tag_categories"]) or "none"),
                     state="" if whatsapp["configured"] else " (not configured: not judged)",
                     hours=report["window_hours"]))
    parts.append("</body></html>")
    return "\n".join(parts)


def jsonable(report: dict) -> dict:
    """Counter objects are not JSON-serialisable; nothing else changes shape."""
    def clean(value):
        if isinstance(value, Counter):
            return dict(value)
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        return value
    return clean(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=default_db(), help="bot database (read-only)")
    parser.add_argument("--log", type=Path, default=default_log(), help="bot log with CATEGORY lines")
    parser.add_argument("--hours", type=float, default=24.0, help="window (default 24)")
    parser.add_argument("--env-file", action="append", default=[],
                        help="a .env to read TG_CATEGORY_*_TARGET from (repeatable)")
    parser.add_argument("--rules", type=Path, default=None,
                        help="a category_rules.json to report against (default: the installed one). "
                             "Lets you measure a taxonomy before installing it")
    parser.add_argument("--days", type=int, default=7,
                        help="how many IST days of per-shelf trend to include (default 7)")
    parser.add_argument("--expand", default=None,
                        help="also list the newest delivery rows for one shelf key "
                             "(or 'main', or an unknown target name)")
    parser.add_argument("--expand-limit", type=int, default=12,
                        help="how many rows --expand lists (default 12)")
    parser.add_argument("--max-posts", type=int, default=20000,
                        help="safety cap on ledger rows read for money/trend/drill-down; "
                             "hitting it is reported as a finding, never silently")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--html", default=None, metavar="PATH",
                        help="also write a self-contained HTML report (no JavaScript, no "
                             "external asset) to PATH, or to stdout with '-'")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when any error/warn finding exists")
    parser.add_argument("--strict-errors", action="store_true",
                        help="exit 1 only on error findings (rules parity/version, routing "
                             "disabled, target collision). This is what a deploy gate uses: an "
                             "operator who runs three shelves must not fail a rollout because "
                             "the other four have no channel")
    args = parser.parse_args(argv)

    report = build_report(db=args.db, log=args.log, hours=args.hours,
                          rules=load_rules(args.rules) if args.rules else None,
                          env=load_env(args.env_file), days=args.days,
                          expand=args.expand, expand_limit=args.expand_limit,
                          max_posts=args.max_posts)
    if args.html == "-":
        # stdout carries the document only, so it can be redirected or piped
        # straight into a mail; every human summary line goes to stderr.
        print(render_html(report))
    else:
        if args.json:
            print(json.dumps(jsonable(report), indent=2, ensure_ascii=False))
        else:
            print(render(report))
        if args.html:
            document = render_html(report)
            target = Path(args.html)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(document, encoding="utf-8")
            except OSError as exc:
                print(f"CATEGORY REPORT | cannot write {target}: {exc}", file=sys.stderr)
                return 2
            print(f"CATEGORY REPORT | HTML written to {target} "
                  f"({len(document)} bytes, no JavaScript, no external asset)", file=sys.stderr)
    if args.strict or args.strict_errors:
        wanted = ("error", "warn") if args.strict else ("error",)
        actionable = [f for f in report["findings"] if f["severity"] in wanted]
        if actionable:
            flag = "--strict" if args.strict else "--strict-errors"
            print(f"\nCATEGORY REPORT | {flag}: {len(actionable)} finding(s) at "
                  f"{'error/warn' if args.strict else 'error'} severity", file=sys.stderr)
            for item in actionable[:10]:
                where = f" [{item['category']}]" if item["category"] else ""
                print(f"  {item['severity'].upper()} {item['kind']}{where}: {item['detail']}",
                      file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
