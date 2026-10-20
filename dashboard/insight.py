"""Ops insight engine: health, live pulse, one-post trace, search, scorecard.

Everything here is read-only over the bot database and the files beside it. It
exists because "the dashboard shows numbers" was not enough: an operator needs
to know WHY a number looks wrong, WHAT happened in the last ten minutes, and
what happened to ONE post — without SSH-ing into the server at 2 a.m.

  * `health()`   — a self-diagnosis: database, schema, backlog, silence, safety
                   policy parity between bot and bridge, category rules parity,
                   channels config, routing snapshot, log errors, disk, services.
                   Every non-green check carries a concrete fix line.
  * `pulse()`    — one merged timeline of everything that happened (intake,
                   send attempt, refusal, link conversion, owned-link proof),
                   plus how long each kind has been silent.
  * `trace()`    — one queue row, the whole chain: what the planner predicted,
                   what the safety gate said, what the price/category readers
                   read, which targets actually received it, which were skipped
                   and with what error, and the delivered link.
  * `search()`   — find a post by text, source, target, queue id or a price.
  * `scorecard()`— per channel and per source: sends, skips (with the top
                   reason), last send, average quality, best hour, silence.

Honesty rules
-------------
* Missing table or column is reported as `unknown`/absent, never invented.
* `health()` never mutates anything; `services` is optional and fails soft.
* Timestamps are stored as epoch seconds; every human string here is IST.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

IST_OFFSET = 5 * 3600 + 30 * 60
DAY = 86400.0

REQUIRED_TABLES = ("queue", "deliveries", "link_cache", "blocked_content")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def ist(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(ts) + IST_OFFSET)) + " IST"


def ist_day_start(now: float | None = None) -> float:
    now = time.time() if now is None else now
    return (now + IST_OFFSET) // DAY * DAY - IST_OFFSET


def age(seconds: float | None) -> str:
    """A duration a human reads at a glance: 9m, 3h, 2d."""
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 2 * DAY:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / DAY:.1f}d"


def _tables(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _one(conn: sqlite3.Connection, sql: str, params=(), default=0):
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def _sha(path: Path) -> str:
    try:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _check(key, title, state, detail, fix="") -> dict:
    return {"key": key, "title": title, "state": state, "detail": detail, "fix": fix}


def _worst(states) -> str:
    rank = {"ok": 0, "info": 0, "unknown": 1, "warn": 2, "fail": 3}
    worst = "ok"
    for state in states:
        if rank.get(state, 1) > rank.get(worst, 0):
            worst = state
    return worst


# --------------------------------------------------------------------------- #
# 1. health
# --------------------------------------------------------------------------- #
def health(*, db: Path, root: Path, channels_json: Path, snapshot: dict,
           log_path: Path | None, now: float | None = None,
           services: bool = False, service_runner=None) -> dict:
    """Self-diagnosis. Returns checks + facts; `overall` is the worst state."""
    now = time.time() if now is None else now
    db = Path(db)
    root = Path(root)
    checks: list[dict] = []
    facts: dict = {"db": str(db), "root": str(root), "now": now}

    # ---- database --------------------------------------------------------
    if not db.exists():
        checks.append(_check("db_present", "Bot database", "fail",
                             f"{db} does not exist",
                             "Start the bot once (it creates the file), or point BOT_DB_PATH at the real DB."))
    else:
        size = db.stat().st_size
        facts["db_size"] = size
        wal = db.with_name(db.name + "-wal")
        facts["db_wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        checks.append(_check("db_present", "Bot database", "ok",
                             f"{size / 1024:.0f} KB · WAL {facts['db_wal_bytes'] / 1024:.0f} KB"))
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                tables = _tables(conn)
                missing = [t for t in REQUIRED_TABLES if t not in tables]
                if integrity.lower() == "ok":
                    checks.append(_check("db_integrity", "Database integrity", "ok",
                                         "quick_check ok"))
                else:
                    checks.append(_check("db_integrity", "Database integrity", "fail",
                                         integrity[:200],
                                         "Stop the bot and run: sqlite3 <db> 'PRAGMA integrity_check;' then .recover"))
                if missing:
                    checks.append(_check("db_schema", "Database schema", "fail",
                                         "missing tables: " + ", ".join(missing),
                                         "The bot creates these on start; if it persists, the bot is running old code."))
                else:
                    checks.append(_check("db_schema", "Database schema", "ok",
                                         "all tables present"))
                checks.extend(_health_queue(conn, now, facts))
                checks.extend(_health_sends(conn, now, facts))
                checks.extend(_health_safety(conn, now, root, facts))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            checks.append(_check("db_integrity", "Database integrity", "fail",
                                 f"cannot read: {exc}",
                                 "Check file permissions and that no other process holds it exclusively."))

    # ---- shared policy / rules parity ------------------------------------
    bot_rules = root / "bestgaa" / "category_rules.json"
    bridge_rules = root / "tg-wa-bridge" / "category_rules.json"
    if bot_rules.exists() and bridge_rules.exists():
        same = _sha(bot_rules) == _sha(bridge_rules)
        version = _json(bot_rules).get("version", "?")
        facts["category_rules_version"] = version
        checks.append(_check("rules_parity", "Category rules (bot vs bridge)", "ok" if same else "fail",
                             f"v{version} · {'identical' if same else 'DIFFERENT'}",
                             "" if same else "Copy bestgaa/category_rules.json over tg-wa-bridge/category_rules.json and restart both."))
    else:
        checks.append(_check("rules_parity", "Category rules (bot vs bridge)", "unknown",
                             "category_rules.json not found in the checkout"))

    bot_policy = root / "bestgaa" / "safety_rules.json"
    bridge_dir, deployed = bridge_directory(root)
    bridge_policy = bridge_dir / "safety_rules.json"
    facts["bridge_dir"] = str(bridge_dir)
    facts["bridge_deployed"] = deployed
    if bot_policy.exists():
        policy = _json(bot_policy)
        facts["safety_policy_version"] = policy.get("version", "?")
        if bridge_policy.exists():
            same = _sha(bot_policy) == _sha(bridge_policy)
            checks.append(_check("policy_parity", "Safety policy (bot vs bridge)",
                                 "ok" if same else "fail",
                                 f"v{policy.get('version', '?')} · "
                                 f"{'identical' if same else 'DIFFERENT'}",
                                 "" if same else
                                 "The bridge would refuse a different set than the bot. Redeploy both from one bundle."))
        elif not deployed:
            checks.append(_check("policy_parity", "Safety policy (bot vs bridge)", "unknown",
                                 f"no deployed bridge on this host ({bridge_dir})",
                                 "Set BRIDGE_DIR=~/tg-wa-bridge (or any path) to compare the running bridge's copy."))
        else:
            checks.append(_check("policy_parity", "Safety policy (bot vs bridge)", "warn",
                                 f"the bridge at {bridge_dir} has no safety_rules.json",
                                 "Ship bestgaa/safety_rules.json into ~/tg-wa-bridge and restart the bridge."))
    else:
        checks.append(_check("policy_parity", "Safety policy (bot vs bridge)", "unknown",
                             "safety_rules.json not found"))

    # ---- channels config + routing snapshot ------------------------------
    channels_json = Path(channels_json)
    if channels_json.exists():
        data = _json(channels_json)
        count = len(data.get("channels") or [])
        facts["dashboard_channels"] = count
        checks.append(_check("channels_config", "Channels config", "ok",
                             f"{count} dashboard channel(s) · {ist(channels_json.stat().st_mtime)}"))
    else:
        checks.append(_check("channels_config", "Channels config", "warn",
                             "channels.json not written yet",
                             "Save a channel in the dashboard, or ignore if you only use built-in channels."))
    started = float((snapshot or {}).get("started_at") or 0)
    facts["bot_started_at"] = started
    facts["bot_uptime_s"] = max(0.0, now - started) if started else 0
    keys = [k for k in ("source_targets", "price_targets", "category_filters", "targets")
            if (snapshot or {}).get(k)]
    if not snapshot:
        checks.append(_check("routing_snapshot", "Routing snapshot", "warn",
                             "the running bot has not published one",
                             "Restart the bot on the merged code; it writes routing_snapshot.json at start."))
    elif len(keys) < 3:
        checks.append(_check("routing_snapshot", "Routing snapshot", "warn",
                             "published but missing: " + ", ".join(
                                 k for k in ("source_targets", "price_targets", "category_filters")
                                 if k not in keys),
                             "Restart the bot so the snapshot carries the routing map the planner reads."))
    else:
        checks.append(_check("routing_snapshot", "Routing snapshot", "ok",
                             f"{len((snapshot or {}).get('targets') or [])} targets · "
                             f"uptime {age(facts['bot_uptime_s'])}"))

    # ---- log -------------------------------------------------------------
    if log_path:
        log_path = Path(log_path)
        if log_path.exists():
            size = log_path.stat().st_size
            mtime = log_path.stat().st_mtime
            facts["log_path"] = str(log_path)
            facts["log_age_s"] = max(0.0, now - mtime)
            errors, floods = _log_scan(log_path)
            facts["log_errors"] = errors
            facts["log_floodwaits"] = floods
            state = "ok"
            detail = f"{size / 1024 / 1024:.1f} MB · last line {age(now - mtime)} ago"
            if errors:
                state, detail = "warn", detail + f" · {errors} error line(s) in the tail"
            if floods:
                state = "warn"
                detail += f" · {floods} FloodWait"
            checks.append(_check("bot_log", "Bot log", state, detail,
                                 "Read the tail: journalctl -u bestgaa -n 200 --no-pager"
                                 if state != "ok" else ""))
        else:
            checks.append(_check("bot_log", "Bot log", "unknown",
                                 f"{log_path} not found",
                                 "Set BOT_LOG to the real path, or read journalctl -u bestgaa."))

    # ---- disk ------------------------------------------------------------
    try:
        usage = shutil.disk_usage(str(db.parent if db.exists() else root))
        free_pct = usage.free / usage.total * 100
        facts["disk_free_pct"] = round(free_pct, 1)
        checks.append(_check("disk", "Disk space", "warn" if free_pct < 10 else "ok",
                             f"{free_pct:.1f}% free on the data volume",
                             "Free space: logs and -wal files grow during a retry storm." if free_pct < 10 else ""))
    except OSError:
        pass

    # ---- services (optional; never assumed) ------------------------------
    if services:
        runner = service_runner or _systemctl_active
        for service in ("bestgaa", "tg-wa-bridge", "bestgaa-dashboard"):
            state, detail = runner(service)
            checks.append(_check(f"service:{service}", f"Service {service}", state, detail,
                                 "" if state == "ok" else f"sudo systemctl status {service}"))

    overall = _worst([c["state"] for c in checks])
    counts = {name: sum(1 for c in checks if c["state"] == name)
              for name in ("ok", "warn", "fail", "unknown")}
    bad = [c for c in checks if c["state"] in ("fail", "warn")]
    headline = ("All systems answer." if not bad else
                "; ".join(c["title"] + " — " + c["detail"] for c in bad[:3]))
    return {"generated_at": now, "overall": overall, "headline": headline,
            "counts": counts, "checks": checks, "facts": facts}


def _health_queue(conn, now, facts) -> list[dict]:
    out = []
    statuses = {}
    try:
        for status, count in conn.execute("SELECT status, COUNT(*) FROM queue GROUP BY status"):
            statuses[str(status or "?")] = int(count)
    except sqlite3.Error:
        pass
    facts["queue"] = statuses
    pending = statuses.get("pending", 0)
    oldest = _one(conn, "SELECT MIN(created_at) FROM queue WHERE status='pending'", (), 0.0)
    pending_age = (now - float(oldest)) if oldest else 0.0
    facts["oldest_pending_age_s"] = pending_age
    if pending and pending_age > 3 * 3600:
        out.append(_check("queue_backlog", "Queue backlog", "warn",
                          f"{pending} pending · oldest waiting {age(pending_age)}",
                          "Check the bot is running and the sources are reachable: "
                          "sudo systemctl status bestgaa"))
    else:
        out.append(_check("queue_backlog", "Queue backlog", "ok",
                          f"{pending} pending" + (f" · oldest {age(pending_age)}" if pending else "")))
    failed = statuses.get("failed", 0)
    facts["failed"] = failed
    if failed:
        top = _one(conn, "SELECT last_error FROM queue WHERE status='failed' "
                         "AND COALESCE(last_error,'')<>'' GROUP BY last_error "
                         "ORDER BY COUNT(*) DESC LIMIT 1", (), "")
        out.append(_check("queue_failures", "Failed posts", "warn", f"{failed} failed · {str(top)[:120]}",
                          "Open the trace for one of them: the dashboard shows the exact error per target."))
    else:
        out.append(_check("queue_failures", "Failed posts", "ok", "none"))
    return out


def _health_sends(conn, now, facts) -> list[dict]:
    out = []
    cols = _cols(conn, "deliveries")
    cols_queue = _cols(conn, "queue")
    if not cols:
        return out
    error_expr = "COALESCE(d.last_error,'')" if "last_error" in cols else "''"
    last = _one(conn, "SELECT MAX(q.created_at) FROM queue q JOIN deliveries d ON d.queue_id=q.id "
                      f"WHERE d.status='sent' AND {error_expr}=''", (), 0.0)
    facts["last_send_at"] = last
    silent = (now - float(last)) if last else None
    facts["silence_s"] = silent
    day_sends = _one(conn, "SELECT COUNT(*) FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                           f"WHERE q.created_at>=? AND d.status='sent' AND {error_expr}=''",
                     (ist_day_start(now),), 0)
    facts["today_sends"] = int(day_sends)
    hour_ist = int(((now + IST_OFFSET) % DAY) // 3600)
    if last and silent is not None and silent > 6 * 3600 and 9 <= hour_ist <= 23:
        state, detail = "warn", f"nothing delivered for {age(silent)}"
    elif not last:
        state, detail = "warn", "no delivered post in the database yet"
    else:
        state, detail = "ok", f"last delivered {age(silent)} ago · {int(day_sends)} today"
    out.append(_check("send_flow", "Sending", state, detail,
                      "Check sources, provenance and per-target skips: /api/channel-pipe"
                      if state != "ok" else ""))
    # quality of the posts going out
    if "premium_score" in cols_queue:
        avg = _one(conn, "SELECT AVG(premium_score) FROM queue WHERE premium_score IS NOT NULL "
                         "AND created_at>=?", (now - 7 * DAY,), None)
        facts["avg_quality"] = round(float(avg), 1) if avg is not None else None
    return out


def _health_safety(conn, now, root, facts) -> list[dict]:
    out = []
    if "blocked_content" in _tables(conn):
        today = _one(conn, "SELECT COUNT(*) FROM blocked_content WHERE created_at>=?",
                     (ist_day_start(now),), 0)
        week = _one(conn, "SELECT COUNT(*) FROM blocked_content WHERE created_at>=?",
                    (now - 7 * DAY,), 0)
        facts["blocks_today"] = int(today)
        facts["blocks_week"] = int(week)
        state, detail = "ok", f"{int(today)} refused today · {int(week)} in 7 days"
        if week >= 25 and today >= 10:
            state, detail = "warn", detail + " · that is a spike"
        out.append(_check("safety_blocks", "Content safety", state, detail,
                          "Look at the refusals: a spike usually means one source turned spammy."
                          if state != "ok" else ""))
    return out


def _log_scan(path: Path, tail_bytes: int = 200_000) -> tuple[int, int]:
    """Error and FloodWait lines in the tail of the log (bounded read)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            blob = handle.read().decode("utf-8", "replace")
    except OSError:
        return 0, 0
    lines = blob.splitlines()[-600:]
    errors = sum(1 for line in lines
                 if re.search(r"\b(ERROR|CRITICAL|Traceback)\b", line))
    floods = sum(1 for line in lines if "FloodWait" in line or "flood wait" in line.lower())
    return errors, floods


def bridge_directory(root: Path) -> tuple[Path, bool]:
    """Where the running WhatsApp bridge lives, and whether it is a real deploy."""
    env = os.getenv("BRIDGE_DIR", "").strip()
    if env:
        path = Path(env).expanduser()
        return path, path.exists()
    deployed = Path(os.path.expanduser("~/tg-wa-bridge"))
    if deployed.exists():
        return deployed, True
    return Path(root) / "tg-wa-bridge", False


def _systemctl_active(service: str) -> tuple[str, str]:
    try:
        result = subprocess.run(["systemctl", "is-active", service],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown", "systemctl not available here"
    state = (result.stdout or "").strip() or "unknown"
    if state == "active":
        return "ok", "active"
    if state in ("unknown", ""):
        return "unknown", "systemd did not answer here"
    return "warn", f"systemd says: {state}"


# --------------------------------------------------------------------------- #
# 2. pulse — one merged timeline
# --------------------------------------------------------------------------- #
EVENT_KINDS = ("intake", "send", "block", "link", "proof")


def pulse(conn: sqlite3.Connection, *, hours: float = 6.0, kind: str = "",
          source: str = "", target: str = "", limit: int = 200,
          now: float | None = None) -> dict:
    """Everything that happened in the window, newest first, from every table."""
    now = time.time() if now is None else now
    since = now - max(0.25, min(168.0, float(hours))) * 3600
    limit = max(1, min(500, int(limit)))
    tables = _tables(conn)
    events: list[dict] = []

    def add(**event):
        if kind and event["kind"] != kind:
            return
        if source and event.get("source") != source:
            return
        if target and event.get("target") != target:
            return
        events.append(event)

    if "queue" in tables:
        for row in conn.execute(
                "SELECT id, source, status, created_at, COALESCE(last_error,''), "
                "COALESCE(rendered_text,'') FROM queue WHERE created_at>=? "
                "ORDER BY created_at DESC LIMIT 500", (since,)):
            text = str(row[5] or "")
            add(kind="intake", at=float(row[3] or 0), queue_id=int(row[0]),
                source=str(row[1] or ""), target="",
                label=f"intake · {row[2]}", status=str(row[2] or ""),
                detail=(text.splitlines()[0][:120] if text else "") or str(row[4] or "")[:120],
                severity="bad" if row[2] == "failed" else "info")

    if "send_attempts" in tables:
        rows = conn.execute(
            "SELECT a.queue_id, a.target, a.state, a.updated_at, q.source "
            "FROM send_attempts a LEFT JOIN queue q ON q.id=a.queue_id "
            "WHERE a.updated_at>=? AND a.state IN ('sent','uncertain','started') "
            "ORDER BY a.updated_at DESC LIMIT 500", (since,))
        for queue_id, tgt, state, at, src in rows:
            add(kind="send", at=float(at or 0), queue_id=int(queue_id or 0),
                source=str(src or ""), target=str(tgt or ""),
                label=f"send · {state}", status=str(state or ""),
                detail=("delivered" if state == "sent" else
                        "needs manual review" if state == "uncertain" else "in flight"),
                severity={"sent": "ok", "uncertain": "bad", "started": "info"}.get(state, "info"))

    if "blocked_content" in tables:
        for row in conn.execute(
                "SELECT queue_id, source, target, stage, category, label, matched, "
                "excerpt, created_at FROM blocked_content WHERE created_at>=? "
                "ORDER BY created_at DESC LIMIT 500", (since,)):
            add(kind="block", at=float(row[8] or 0),
                queue_id=int(row[0] or 0) if row[0] is not None else None,
                source=str(row[1] or ""), target=str(row[2] or ""),
                label=f"refused · {row[3]} · {row[4]}",
                status=str(row[6] or ""),
                detail=f"{row[5]} — matched “{row[6]}”" if row[6] else str(row[5] or ""),
                severity="warn")

    if "link_cache" in tables:
        for row in conn.execute(
                "SELECT source_url, affiliate_url, deal_key, created_at FROM link_cache "
                "WHERE created_at>=? ORDER BY created_at DESC LIMIT 300", (since,)):
            add(kind="link", at=float(row[3] or 0), queue_id=None, source="", target="",
                label="link converted", status="owned",
                detail=f"{_short(str(row[0]))} → {_short(str(row[1]))}",
                severity="ok")

    if "owned_link_proofs" in tables:
        for row in conn.execute(
                "SELECT affiliate_url, destination_url, proof_type, publisher_id, created_at "
                "FROM owned_link_proofs WHERE created_at>=? ORDER BY created_at DESC LIMIT 200",
                (since,)):
            add(kind="proof", at=float(row[4] or 0), queue_id=None, source="", target="",
                label=f"proof · {row[2]}", status=str(row[3] or ""),
                detail=f"{_short(str(row[0]))} → {_short(str(row[1]))}", severity="ok")

    # Hourly throughput for the chart: one bucket per IST hour, every kind, so
    # a gap is a visible empty bar instead of something to guess about.
    buckets = max(1, min(24, int(round(float(hours)))))
    hourly = []
    for step in range(buckets - 1, -1, -1):
        start = now - (step + 1) * 3600
        row = {"at": start, "hour_ist": time.strftime("%H:00", time.gmtime(start + IST_OFFSET))}
        for name in EVENT_KINDS:
            row[name] = 0
        hourly.append(row)
    for event in events:
        offset = int((now - event["at"]) // 3600)
        if 0 <= offset < buckets:
            hourly[buckets - 1 - offset][event["kind"]] += 1

    events.sort(key=lambda e: (e["at"], e["kind"]), reverse=True)
    capped = len(events) > limit
    counts = {k: sum(1 for e in events if e["kind"] == k) for k in EVENT_KINDS}
    last: dict = {}
    for name in EVENT_KINDS:
        found = next((e for e in events if e["kind"] == name), None)
        last[name] = ({"at": found["at"], "at_ist": ist(found["at"]),
                       "label": found["label"], "age_s": max(0.0, now - found["at"]),
                       "age": age(now - found["at"])} if found else None)
    silences = []
    for name in EVENT_KINDS:
        found = last.get(name)
        if found is None:
            continue
        if name in ("send", "intake") and found["age_s"] > 6 * 3600:
            silences.append({"kind": name, "age": found["age"],
                             "why": f"nothing of this kind in {found['age']}"})
    return {
        "generated_at": now,
        "window_hours": float(hours),
        "since_ist": ist(since),
        "counts": counts,
        "last": last,
        "silences": silences,
        "hourly": hourly,
        "capped": capped,
        "events": [{"at": e["at"], "at_ist": ist(e["at"]), "age": age(now - e["at"]),
                    **{k: v for k, v in e.items() if k != "at"}} for e in events[:limit]],
    }


def _short(url: str, limit: int = 46) -> str:
    url = str(url or "")
    if len(url) <= limit:
        return url
    return url[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# 3. trace — one post, the whole chain
# --------------------------------------------------------------------------- #
def trace(conn: sqlite3.Connection, queue_id: int, *, now: float | None = None,
          analyze=None, snapshot: dict | None = None, channels: list | None = None) -> dict:
    """Everything known about one queued post: predicted vs actual, per target."""
    now = time.time() if now is None else now
    tables = _tables(conn)
    if "queue" not in tables:
        raise KeyError("no queue table in this database")
    quality_col = "premium_score" if "premium_score" in _cols(conn, "queue") else "NULL"
    row = conn.execute(
        "SELECT id, chat_id, msg_id, source, status, attempts, COALESCE(rendered_text,''), "
        "COALESCE(targets_json,''), COALESCE(last_error,''), created_at, "
        + quality_col + " FROM queue WHERE id=?", (int(queue_id),)).fetchone()
    if row is None:
        raise LookupError(f"no queue row {queue_id}")
    text = str(row[6] or "")
    source = str(row[3] or "")
    post = {
        "queue_id": int(row[0]), "chat_id": row[1], "msg_id": row[2],
        "source": source, "status": str(row[4] or ""), "attempts": int(row[5] or 0),
        "created_at": float(row[9] or 0), "created_ist": ist(row[9]),
        "age": age(now - float(row[9] or now)),
        "error": str(row[8] or ""), "premium_score": row[10],
        "text": text[:4000],
    }

    # what the planner predicted (same engine as Source → Target)
    predicted = None
    if analyze is not None and text.strip():
        try:
            predicted = analyze(text, source, snapshot=snapshot, channels=channels)
        except Exception as exc:  # noqa: BLE001 - the trace must still render
            predicted = {"warnings": [f"planner failed: {exc}"], "plan": {"targets": [],
                                                                         "excluded": []}}

    # what actually happened, per target
    deliveries: list[dict] = []
    if "deliveries" in tables:
        cols = _cols(conn, "deliveries")
        error_expr = "COALESCE(last_error,'')" if "last_error" in cols else "''"
        chunks = "chunks_sent" if "chunks_sent" in cols else "0"
        for tgt, status, attempts, sent, err in conn.execute(
                f"SELECT target, status, attempts, {chunks}, {error_expr} FROM deliveries "
                "WHERE queue_id=? ORDER BY target", (int(queue_id),)):
            deliveries.append({"target": str(tgt or ""), "status": str(status or ""),
                               "attempts": int(attempts or 0), "chunks": int(sent or 0),
                               "error": str(err or ""),
                               "received": status == "sent" and not err})
    attempts: list[dict] = []
    if "send_attempts" in tables:
        for tgt, index, state, updated in conn.execute(
                "SELECT target, chunk_index, state, updated_at FROM send_attempts "
                "WHERE queue_id=? ORDER BY updated_at", (int(queue_id),)):
            attempts.append({"target": str(tgt or ""), "chunk": int(index or 0),
                             "state": str(state or ""), "at": float(updated or 0),
                             "at_ist": ist(updated)})
    blocks: list[dict] = []
    if "blocked_content" in tables:
        rows = conn.execute(
            "SELECT stage, target, category, label, matched, reason, policy_version, excerpt, "
            "created_at FROM blocked_content WHERE queue_id=? OR (source=? AND msg_id=?) "
            "ORDER BY created_at", (int(queue_id), source, row[2])).fetchall()
        for stage, tgt, category, label, matched, reason, version, excerpt, at in rows:
            blocks.append({"stage": stage, "target": tgt, "category": category, "label": label,
                           "matched": matched, "reason": reason, "policy_version": version,
                           "excerpt": excerpt, "at": float(at or 0), "at_ist": ist(at)})

    links: list[dict] = []
    if "link_cache" in tables and text:
        for src_url, aff, resolved, key, at in conn.execute(
                "SELECT source_url, affiliate_url, COALESCE(resolved_url,''), "
                "COALESCE(deal_key,''), created_at FROM link_cache ORDER BY created_at DESC "
                "LIMIT 400"):
            if src_url and src_url in text:
                links.append({"source": src_url, "affiliate": aff, "resolved": resolved,
                              "deal_key": key, "at": float(at or 0), "at_ist": ist(at)})

    # prediction vs reality
    planned: list[dict] = []
    if predicted:
        for target in (predicted.get("plan") or {}).get("targets") or []:
            name = str(target.get("name") or "")
            actual = next((d for d in deliveries if d["target"] == name), None)
            if actual is None:
                verdict, why = "not_attempted", "planned, but the bot never wrote a delivery row"
            elif actual["received"]:
                verdict, why = "delivered", "posted"
            elif actual["status"] == "held":
                verdict, why = "held", actual["error"] or "held for manual review"
            else:
                verdict, why = "skipped", actual["error"] or actual["status"]
            planned.append({"target": name, "phase": target.get("phase", ""),
                            "why": target.get("why", ""), "verdict": verdict,
                            "detail": why, "chunks": (actual or {}).get("chunks", 0)})
        planned_names = {p["target"] for p in planned}
        for delivery in deliveries:
            if delivery["target"] not in planned_names:
                planned.append({"target": delivery["target"], "phase": "observed",
                                "why": "delivered by a route the current snapshot does not predict",
                                "verdict": "delivered" if delivery["received"] else "skipped",
                                "detail": delivery["error"] or delivery["status"],
                                "chunks": delivery["chunks"]})

    return {"generated_at": now, "post": post, "predicted": predicted,
            "planned_vs_actual": planned, "deliveries": deliveries,
            "attempts": attempts, "blocks": blocks, "links": links,
            "received_by": [d["target"] for d in deliveries if d["received"]],
            "withheld_by": [d["target"] for d in deliveries if not d["received"]]}


# --------------------------------------------------------------------------- #
# 4. search
# --------------------------------------------------------------------------- #
def search(conn: sqlite3.Connection, query: str, *, limit: int = 40,
           now: float | None = None) -> dict:
    """Find posts/channels/refusals by text, source, target, price or queue id."""
    now = time.time() if now is None else now
    query = str(query or "").strip()
    limit = max(1, min(100, int(limit)))
    if len(query) < 2 and not query.isdigit():
        raise ValueError("search needs at least 2 characters (a queue id can be 1)")
    # "@ElectroShelf" is how an operator copies a channel name; the ledger stores
    # it without the @, so match both spellings.
    bare = query[1:] if query.startswith("@") and len(query) > 1 else query
    if bare != query and len(bare) < 2:
        raise ValueError("search needs at least 2 characters")
    tables = _tables(conn)
    like = "%" + bare.replace("%", "").replace("_", "") + "%"
    results: list[dict] = []
    numeric = query.isdigit() and len(query) <= 12

    if "queue" in tables:
        sql = ("SELECT id, source, status, created_at, COALESCE(rendered_text,''), "
               "COALESCE(last_error,'') FROM queue WHERE ")
        if numeric:
            sql += "id=? OR "
        sql += "rendered_text LIKE ? OR source LIKE ? ORDER BY created_at DESC LIMIT ?"
        params = ((int(query),) if numeric else ()) + (like, like, limit)
        try:
            for qid, source, status, created, text, error in conn.execute(sql, params):
                results.append({"kind": "post", "queue_id": int(qid), "at": float(created or 0),
                                "at_ist": ist(created), "source": str(source or ""),
                                "target": "", "status": str(status or ""),
                                "snippet": _snippet(text, query) or str(error or "")})
        except sqlite3.Error:
            pass

    if "deliveries" in tables:
        cols = _cols(conn, "deliveries")
        error_expr = "COALESCE(d.last_error,'')" if "last_error" in cols else "''"
        try:
            for qid, tgt, status, err, created in conn.execute(
                    "SELECT d.queue_id, d.target, d.status, " + error_expr + ", q.created_at "
                    "FROM deliveries d LEFT JOIN queue q ON q.id=d.queue_id "
                    "WHERE d.target LIKE ? OR " + error_expr + " LIKE ? "
                    "ORDER BY q.created_at DESC LIMIT ?", (like, like, limit)):
                results.append({"kind": "target", "queue_id": int(qid or 0),
                                "at": float(created or 0), "at_ist": ist(created),
                                "source": "", "target": str(tgt or ""),
                                "status": str(status or ""),
                                "snippet": str(err or "") or f"delivery {status}"})
        except sqlite3.Error:
            pass

    if "blocked_content" in tables:
        try:
            for qid, stage, source, tgt, category, matched, reason, created in conn.execute(
                    "SELECT queue_id, stage, source, target, category, matched, reason, created_at "
                    "FROM blocked_content WHERE source LIKE ? OR category LIKE ? OR matched LIKE ? "
                    "OR excerpt LIKE ? OR reason LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (like, like, like, like, like, limit)):
                results.append({"kind": "refusal", "queue_id": int(qid or 0),
                                "at": float(created or 0), "at_ist": ist(created),
                                "source": str(source or ""), "target": str(tgt or ""),
                                "status": str(stage or ""),
                                "snippet": f"{category}: {matched} — {str(reason or '')[:140]}"})
        except sqlite3.Error:
            pass

    results.sort(key=lambda r: r["at"], reverse=True)
    capped = len(results) > limit
    kinds: dict = {}
    for item in results:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    return {"generated_at": now, "query": query, "count": len(results), "kinds": kinds,
            "capped": capped, "results": results[:limit]}


def _snippet(text: str, query: str, width: int = 130) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return ""
    index = text.lower().find(query.lower())
    if index < 0:
        return text[:width]
    start = max(0, index - width // 3)
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


# --------------------------------------------------------------------------- #
# 5. scorecard — per channel and per source
# --------------------------------------------------------------------------- #
def scorecard(conn: sqlite3.Connection, *, days: int = 7, now: float | None = None) -> dict:
    """Who is actually receiving, who is silent, and which source earns its place."""
    now = time.time() if now is None else now
    days = max(1, min(30, int(days)))
    since = now - days * DAY
    day_since = ist_day_start(now)
    tables = _tables(conn)
    channels: list[dict] = []
    if "deliveries" in tables:
        cols = _cols(conn, "deliveries")
        error_expr = "COALESCE(d.last_error,'')" if "last_error" in cols else "''"
        rows = conn.execute(
            "SELECT d.target, "
            "SUM(CASE WHEN d.status='sent' AND " + error_expr + "='' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN d.status<>'sent' OR " + error_expr + "<>'' THEN 1 ELSE 0 END), "
            "MAX(CASE WHEN d.status='sent' AND " + error_expr + "='' THEN q.created_at END), "
            "MAX(q.created_at), COUNT(*) "
            "FROM deliveries d JOIN queue q ON q.id=d.queue_id WHERE q.created_at>=? "
            "GROUP BY d.target ORDER BY 2 DESC", (since,)).fetchall()
        for target, sends, skips, last_send, last_any, total in rows:
            top_reason = _one(conn, "SELECT d.last_error FROM deliveries d JOIN queue q "
                                    "ON q.id=d.queue_id WHERE d.target=? AND q.created_at>=? "
                                    "AND COALESCE(d.last_error,'')<>'' GROUP BY d.last_error "
                                    "ORDER BY COUNT(*) DESC LIMIT 1", (target, since), "")
            quality = None
            if "premium_score" in _cols(conn, "queue"):
                quality = _one(conn, "SELECT AVG(q.premium_score) FROM deliveries d "
                                     "JOIN queue q ON q.id=d.queue_id WHERE d.target=? "
                                     "AND q.created_at>=? AND q.premium_score IS NOT NULL",
                               (target, since), None)
            hour = _one(conn, "SELECT CAST(((q.created_at + ?) % 86400)/3600 AS INTEGER) "
                              "FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                              "WHERE d.target=? AND q.created_at>=? AND d.status='sent' "
                              "GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
                        (IST_OFFSET, target, since), None)
            today = _one(conn, "SELECT COUNT(*) FROM deliveries d JOIN queue q "
                               "ON q.id=d.queue_id WHERE d.target=? AND q.created_at>=? "
                               f"AND d.status='sent' AND {error_expr}=''", (target, day_since), 0)
            channels.append({
                "target": str(target or ""),
                "sends": int(sends or 0), "skips": int(skips or 0),
                "today": int(today or 0), "total": int(total or 0),
                "last_send_at": float(last_send or 0), "last_send_ist": ist(last_send),
                "last_send_age_s": (now - float(last_send)) if last_send else None,
                "last_any_at": float(last_any or 0), "last_any_ist": ist(last_any),
                "top_skip_reason": str(top_reason or "")[:160],
                "avg_quality": round(float(quality), 1) if quality is not None else None,
                "best_hour_ist": int(hour) if hour is not None else None,
                "deliver_rate": round(100.0 * int(sends or 0) / max(1, int(total or 0)), 1),
            })

    sources: list[dict] = []
    if "queue" in tables:
        for source, total, done, failed, last in conn.execute(
                "SELECT source, COUNT(*), "
                "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), MAX(created_at) "
                "FROM queue WHERE created_at>=? GROUP BY source ORDER BY 2 DESC", (since,)):
            sources.append({"source": str(source or ""), "intakes": int(total or 0),
                            "done": int(done or 0), "failed": int(failed or 0),
                            "last_intake_at": float(last or 0), "last_intake_ist": ist(last),
                            "last_intake_age_s": (now - float(last)) if last else None,
                            "post_rate": round(100.0 * int(done or 0) / max(1, int(total or 0)), 1)})

    return {"generated_at": now, "days": days, "since_ist": ist(since),
            "channels": channels, "sources": sources,
            "totals": {"channels": len(channels),
                       "sends": sum(c["sends"] for c in channels),
                       "skips": sum(c["skips"] for c in channels),
                       "sources": len(sources)}}


# --------------------------------------------------------------------------- #
# CSV export (same rows the panels show)
# --------------------------------------------------------------------------- #
def to_csv(rows, columns) -> str:
    """RFC 4180 CSV from the same dicts the panels render. Text stays text."""
    import csv
    import io
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    return buffer.getvalue()


def _csv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


# --------------------------------------------------------------------------- #
# CLI (same engine, no server): python3 -m dashboard.insight health
# --------------------------------------------------------------------------- #
def _default_paths() -> dict:
    root = Path(__file__).resolve().parent.parent
    db = Path(os.getenv("BOT_DB_PATH") or (root / "bestgaa" / "bestgaa.sqlite3"))
    channels = Path(os.getenv("CHANNELS_JSON_PATH") or (root / "bestgaa" / "channels.json"))
    snapshot = _json(channels.parent / "routing_snapshot.json")
    log = Path(os.getenv("BOT_LOG") or (root / "bestgaa" / "logs" / "bot.log"))
    return {"root": root, "db": db, "channels_json": channels, "snapshot": snapshot,
            "log_path": log}


def main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="BestGAA ops insight (read-only)")
    parser.add_argument("what", choices=["health", "pulse", "trace", "search", "scorecard"])
    parser.add_argument("value", nargs="?", default="")
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    paths = _default_paths()
    conn = None
    try:
        if paths["db"].exists():
            conn = sqlite3.connect(f"file:{paths['db']}?mode=ro", uri=True, timeout=3)
        if args.what == "health":
            data = health(db=paths["db"], root=paths["root"],
                          channels_json=paths["channels_json"], snapshot=paths["snapshot"],
                          log_path=paths["log_path"], services=True)
        elif conn is None:
            print("no database at", paths["db"])
            return 1
        elif args.what == "pulse":
            data = pulse(conn, hours=args.hours, limit=args.limit)
        elif args.what == "trace":
            from dashboard import source_target
            data = trace(conn, int(args.value), analyze=source_target.analyze,
                         snapshot=paths["snapshot"])
        elif args.what == "search":
            data = search(conn, args.value, limit=args.limit)
        else:
            data = scorecard(conn, days=args.days)
    finally:
        if conn is not None:
            conn.close()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.what == "health":
        print(f"OVERALL {data['overall'].upper()} — {data['headline']}")
        for item in data["checks"]:
            print(f"  [{item['state']:>7}] {item['title']}: {item['detail']}")
            if item["fix"] and item["state"] != "ok":
                print(f"            fix: {item['fix']}")
    elif args.what == "pulse":
        print(f"pulse {data['window_hours']}h · " +
              " · ".join(f"{k}={v}" for k, v in data["counts"].items()))
        for event in data["events"]:
            print(f"  {event['at_ist']}  {event['kind']:<6} {event['label']:<34} {event['detail'][:90]}")
    elif args.what == "trace":
        post = data["post"]
        print(f"#{post['queue_id']} {post['source']} {post['status']} · {post['created_ist']}")
        print("  received:", ", ".join(data["received_by"]) or "nobody")
        for row in data["planned_vs_actual"]:
            print(f"  {row['verdict']:<13} @{row['target']}: {row['detail']}")
        for block in data["blocks"]:
            print(f"  refused at {block['stage']}: {block['label']} ({block['matched']})")
    elif args.what == "search":
        print(f"{data['count']} hit(s) for {data['query']!r}")
        for row in data["results"]:
            print(f"  {row['at_ist']}  {row['kind']:<8} #{row['queue_id']:<6} {row['snippet'][:90]}")
    else:
        print(f"scorecard, last {data['days']} days")
        for row in data["channels"]:
            print(f"  @{row['target']:<22} {row['sends']:>4} sent · {row['skips']:>3} skipped · "
                  f"last {row['last_send_ist']}")
        for row in data["sources"]:
            print(f"  {row['source']:<20} {row['intakes']:>4} intakes · {row['post_rate']}% posted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
