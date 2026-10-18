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
    python3 ops/category_report.py --json                 # for the dashboard/CI
    python3 ops/category_report.py --db /path/bot_state.sqlite3 --log /path/bot.log

Per category it reports: the channels that serve it (enabled or not, Amazon-tag
declared or not), sends/skips/holds from the delivery ledger, the pending
backlog routed at it, how many deals the classifier actually sent its way
(`CATEGORY ROUTE`), how many were withheld from it and why (`CATEGORY WITHHELD`
reasons: mixed evidence, insufficient product evidence), and the last time
anything landed there.

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

Read-only: it opens the database in `mode=ro`, never writes, never restarts
anything, and needs no Telegram, WhatsApp or affiliate credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

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

    Values are never printed. Only the two category keys this report needs are
    kept, so a token in the same file cannot leak into output or JSON.
    """
    wanted = ("TG_CATEGORY_", "AMAZON_TAG_CATEGORY_TARGETS")
    candidates = [Path(f) for f in (files or [])]
    if not candidates:
        default = ROOT / "bestgaa" / ".env"
        candidates = [default] if default.exists() else []
    env: dict[str, str] = {}
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


def delivery_rows(conn: sqlite3.Connection, since: float) -> list[tuple[str, str, str, float]]:
    cols = table_columns(conn, "deliveries")
    if not cols:
        return []
    error_expr = "COALESCE(d.last_error,'')" if "last_error" in cols else "''"
    query = (f"SELECT d.target AS target, d.status AS status, {error_expr} AS error, "
             "q.created_at AS created_at FROM deliveries d "
             "JOIN queue q ON q.id = d.queue_id WHERE q.created_at >= ?")
    try:
        return [(str(r["target"] or ""), str(r["status"] or ""), str(r["error"] or ""),
                 float(r["created_at"] or 0.0)) for r in conn.execute(query, (since,))]
    except sqlite3.OperationalError:
        return []


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


def build_report(db: Path | None = None, log: Path | None = None, hours: float = 24.0,
                 channels_data: dict | None = None, rules: dict | None = None,
                 env: dict | None = None, snapshot: dict | None = None) -> dict:
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

    conn = open_readonly(db) if db else None
    ledger: dict = {}
    pending: Counter = Counter()
    db_ok = False
    if conn is not None:
        db_ok = True
        try:
            ledger = count_deliveries(delivery_rows(conn, since), index)
            pending = count_pending(pending_rows(conn), index)
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
        rows = view["shelves"].get(key, [])
        enabled = [r for r in rows if r["enabled"]]
        env_target = {declared[key]} if declared.get(key) else set()
        # "configured" is every target that claims this shelf, including disabled
        # ones: a shelf turned OFF must still be visible, and must not be
        # misreported as a category nobody ever configured.
        targets = sorted({r["name"] for r in rows} | env_target)
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
                          "sources": r["sources"], "all_sources": r["all_sources"]} for r in rows],
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
        },
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

    main_sent = report["main"]["deliveries"]["sent"]
    for shelf in report["categories"]:
        key = shelf["key"]
        for target in shelf["unresolved_targets"]:
            add("warn", "target_unresolved", key,
                f"@{target} was not reachable; those deals fell back to Main instead of this shelf")
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
    main = report["main"]
    lines.append("  MAIN (unfiltered) targets={count} sent={sent} skipped={skipped} held={held} "
                 "pending={pending} | last send {last}".format(
                     count=len(main["targets"]), pending=main["pending"],
                     last=main["last_sent"], **main["deliveries"]))
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
    parser.add_argument("--json", action="store_true", help="machine-readable report")
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
                          env=load_env(args.env_file))
    if args.json:
        print(json.dumps(jsonable(report), indent=2, ensure_ascii=False))
    else:
        print(render(report))
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
