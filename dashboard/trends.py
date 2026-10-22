"""Trends and triage: is this window better or worse, and what needs a human now?

Two questions the rest of the dashboard did not answer:

* **"Is it getting better or worse?"** `compare()` puts one window against the
  window immediately before it - sends, skips, refusals, conversions, intakes,
  failures, and the backlog sitting in the queue - each with an exact delta, a
  per-channel split and a bar per equal slice of the window. Equal slices, not
  "the last 24 hours": a 7-day view is twenty-four 7-hour bars, so the numbers
  in the chart still add up to the numbers in the headline.

* **"What do I actually have to do?"** `attention()` merges the health checks,
  the rules drift, a refusal spike and the trend deltas into one ranked list,
  each row carrying the fix in the same sentence as the fault. It is a pure
  function of what the caller already loaded, so it can never disagree with the
  panels those facts came from.

Everything here is read-only over the bot database. Timestamps are epoch
seconds; every human string is IST.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from dashboard.insight import age, ist, _cols, _tables

# key -> (label, up-is-good?, one-line explanation)
METRICS = (
    ("sent", "Sent", True,
     "deliveries the ledger records as delivered with no error"),
    ("skipped", "Skipped", False,
     "deliveries refused by a rule (duplicate, category, quiet hours, budget)"),
    ("refused", "Refused", False,
     "posts stopped by the public-channel content policy before sending"),
    ("converted", "Links converted", True,
     "source links turned into OUR affiliate link"),
    ("intake", "Intake", True, "posts read from the source channels"),
    ("failed", "Failed", False, "queue rows the bot gave up on"),
)


def _num(row, index=0) -> int:
    try:
        return int(row[index] or 0)
    except (TypeError, IndexError, ValueError):
        return 0


def _query(conn: sqlite3.Connection, sql: str, args=()) -> list:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def _scalar(conn: sqlite3.Connection, sql: str, args=()) -> int:
    rows = _query(conn, sql, args)
    return _num(rows[0]) if rows else 0


def _sums(conn: sqlite3.Connection, *, start: float, end: float, buckets: int,
          target: str = "") -> dict:
    """Every metric over [start, end), plus one count per equal slice."""
    tables = _tables(conn)
    width = max(1.0, (end - start) / max(1, buckets))
    out: dict[str, dict] = {}
    target_filter = " AND d.target=?" if target else ""
    args_target = [target] if target else []

    def series(sql: str, args) -> list[int]:
        rows = _query(conn, sql, args)
        buckets_out = [0] * buckets
        for index, count in rows:
            if index is None:
                continue
            index = max(0, min(buckets - 1, int(index)))
            buckets_out[index] += _num((count,))
        return buckets_out

    bucket_of = f"MIN({buckets - 1}, CAST(({{col}}-{start!r})/{width!r} AS INTEGER))"

    if "queue" in tables:
        rows = _query(conn, "SELECT COUNT(*), SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), "
                            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) "
                            "FROM queue WHERE created_at>=? AND created_at<?", (start, end))
        total, failed, pending = (rows[0] if rows else (0, 0, 0))
        out["intake"] = {"value": _num((total,)), "series": series(
            "SELECT " + bucket_of.format(col="created_at") + " b, COUNT(*) FROM queue "
            "WHERE created_at>=? AND created_at<? GROUP BY b", (start, end))}
        out["failed"] = {"value": _num((failed,)), "series": series(
            "SELECT " + bucket_of.format(col="created_at") + " b, COUNT(*) FROM queue "
            "WHERE created_at>=? AND created_at<? AND status='failed' GROUP BY b", (start, end))}
        del pending
    else:
        for key in ("intake", "failed"):
            out[key] = {"value": 0, "series": []}

    if "deliveries" in tables:
        cols = _cols(conn, "deliveries")
        error_expr = "COALESCE(d.last_error,'')" if "last_error" in cols else "''"
        sent_where = "d.status='sent' AND " + error_expr + "=''"
        skip_where = "(d.status='skipped' OR (d.status='sent' AND " + error_expr + "!=''))"

        def delivery_series(where: str) -> list[int]:
            return series(
                "SELECT " + bucket_of.format(col="q.created_at") + " b, COUNT(*) "
                "FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                "WHERE q.created_at>=? AND q.created_at<?" + target_filter + " AND " + where
                + " GROUP BY b", (start, end, *args_target))

        def delivery_total(where: str) -> int:
            rows = _query(conn, "SELECT COUNT(*) FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                                "WHERE q.created_at>=? AND q.created_at<?" + target_filter
                                + " AND " + where, (start, end, *args_target))
            return _num(rows[0]) if rows else 0

        out["sent"] = {"value": delivery_total(sent_where), "series": delivery_series(sent_where)}
        out["skipped"] = {"value": delivery_total(skip_where), "series": delivery_series(skip_where)}
        out["_skips_by_reason"] = _query(
            conn, "SELECT COALESCE(NULLIF(" + error_expr + ",''),'(no reason recorded)') r, "
                  "COUNT(*) FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                  "WHERE q.created_at>=? AND q.created_at<?" + target_filter + " AND " + skip_where
                  + " GROUP BY r ORDER BY 2 DESC LIMIT 8", (start, end, *args_target))
        out["_sent_by_target"] = _query(
            conn, "SELECT d.target, COUNT(*) FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                  "WHERE q.created_at>=? AND q.created_at<?" + target_filter + " AND " + sent_where
                  + " GROUP BY d.target", (start, end, *args_target))
        out["_skips_by_target"] = _query(
            conn, "SELECT d.target, COUNT(*) FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                  "WHERE q.created_at>=? AND q.created_at<?" + target_filter + " AND " + skip_where
                  + " GROUP BY d.target", (start, end, *args_target))
    else:
        for key in ("sent", "skipped"):
            out[key] = {"value": 0, "series": []}
        out["_skips_by_reason"] = []
        out["_sent_by_target"] = []
        out["_skips_by_target"] = []

    if "blocked_content" in tables:
        out["refused"] = {
            "value": _scalar(conn, "SELECT COUNT(*) FROM blocked_content "
                                   "WHERE created_at>=? AND created_at<?", (start, end)),
            "series": series("SELECT " + bucket_of.format(col="created_at") + " b, COUNT(*) "
                             "FROM blocked_content WHERE created_at>=? AND created_at<? GROUP BY b",
                             (start, end))}
    else:
        out["refused"] = {"value": 0, "series": []}

    if "link_cache" in tables:
        out["converted"] = {
            "value": _scalar(conn, "SELECT COUNT(*) FROM link_cache "
                                   "WHERE created_at>=? AND created_at<?", (start, end)),
            "series": series("SELECT " + bucket_of.format(col="created_at") + " b, COUNT(*) "
                             "FROM link_cache WHERE created_at>=? AND created_at<? GROUP BY b",
                             (start, end))}
    else:
        out["converted"] = {"value": 0, "series": []}
    return out


def backlog(conn: sqlite3.Connection) -> int:
    """What is sitting in the queue right now (not a windowed number)."""
    if "queue" not in _tables(conn):
        return 0
    return _scalar(conn, "SELECT COUNT(*) FROM queue WHERE status='pending'")


def _pct(current: int, previous: int) -> float | None:
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100.0, 1)


def compare(conn: sqlite3.Connection, *, hours: float = 24.0, now: float | None = None,
            buckets: int = 24, target: str = "") -> dict:
    """This window against the one immediately before it."""
    now = time.time() if now is None else now
    hours = max(0.25, min(168.0, float(hours)))
    buckets = max(4, min(48, int(buckets)))
    since = now - hours * 3600
    before = since - hours * 3600
    readable = "queue" in _tables(conn)
    current = _sums(conn, start=since, end=now, buckets=buckets, target=target)
    previous = _sums(conn, start=before, end=since, buckets=buckets, target=target)

    metrics = []
    for key, label, up_good, why in METRICS:
        cur = int(current.get(key, {}).get("value", 0))
        prev = int(previous.get(key, {}).get("value", 0))
        delta = cur - prev
        # A percentage against a nearly empty window is noise, not a trend:
        # "+578%" because the bot only has one day of history reads as a huge
        # win. Say "no baseline" instead of a number that cannot be trusted.
        thin = prev < max(5, int(cur * 0.1))
        pct = None if thin else _pct(cur, prev)
        direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
        sentiment = "flat" if delta == 0 else (
            "good" if (delta > 0) == up_good else "bad")
        metrics.append({
            "key": key, "label": label, "why": why, "up_is_good": up_good,
            "current": cur, "previous": prev, "delta": delta, "pct": pct,
            "baseline_thin": thin,
            "direction": direction, "sentiment": sentiment,
            "series": current.get(key, {}).get("series") or [],
            "previous_series": previous.get(key, {}).get("series") or [],
        })
    # The backlog is a snapshot, not a window: there is no honest "before" to
    # compare it with, so it carries no delta rather than a made-up one.
    metrics.append({
        "key": "backlog", "label": "Queued now", "why": "rows waiting for a worker",
        "up_is_good": False, "current": backlog(conn), "previous": None,
        "delta": None, "pct": None, "direction": "flat", "sentiment": "flat",
        "baseline_thin": False, "series": [], "previous_series": [],
    })

    # Per channel: who is receiving, and is that better or worse than before.
    sent_now = {str(r[0]): _num(r, 1) for r in current.get("_sent_by_target", [])}
    sent_before = {str(r[0]): _num(r, 1) for r in previous.get("_sent_by_target", [])}
    skip_now = {str(r[0]): _num(r, 1) for r in current.get("_skips_by_target", [])}
    skip_before = {str(r[0]): _num(r, 1) for r in previous.get("_skips_by_target", [])}
    channels = []
    for name in sorted(set(sent_now) | set(sent_before) | set(skip_now) | set(skip_before)):
        cur, prev = sent_now.get(name, 0), sent_before.get(name, 0)
        channels.append({
            "target": name, "sent": cur, "previous_sent": prev, "delta": cur - prev,
            "pct": _pct(cur, prev), "skipped": skip_now.get(name, 0),
            "previous_skipped": skip_before.get(name, 0),
            "sentiment": "flat" if cur == prev else ("good" if cur > prev else "bad"),
        })
    channels = [row for row in channels
                if row["sent"] or row["previous_sent"] or row["skipped"] or row["previous_skipped"]]
    channels.sort(key=lambda row: row["sent"], reverse=True)

    # Why things were skipped, now vs before, so a new reason cannot hide.
    reasons_now = {str(r[0]): _num(r, 1) for r in current.get("_skips_by_reason", [])}
    reasons_before = {str(r[0]): _num(r, 1) for r in previous.get("_skips_by_reason", [])}
    skip_reasons = []
    for reason in sorted(set(reasons_now) | set(reasons_before),
                         key=lambda r: reasons_now.get(r, 0), reverse=True)[:6]:
        cur, prev = reasons_now.get(reason, 0), reasons_before.get(reason, 0)
        skip_reasons.append({"reason": reason, "current": cur, "previous": prev,
                             "delta": cur - prev, "pct": _pct(cur, prev)})

    by_key = {m["key"]: m for m in metrics}
    window = hours if hours < 48 else round(hours / 24, 1)
    unit = "h" if hours < 48 else "d"
    # A window of zeros on a database that cannot be read is not a quiet
    # period, it is an unreadable file: say so instead of drawing a flat line.
    verdict = (_verdict(by_key, f"{window:g}{unit}") if readable else
               "this database has no queue table yet - there is nothing to compare")
    return {
        "generated_at": now,
        "window_hours": hours,
        "window_label": f"{window:g}{unit}",
        "since_ist": ist(since),
        "previous_since_ist": ist(before),
        "buckets": buckets,
        "bucket_seconds": round((now - since) / buckets),
        "bucket_starts": [since + i * (now - since) / buckets for i in range(buckets)],
        "metrics": metrics,
        "by_metric": by_key,
        "channels": channels,
        "skip_reasons": skip_reasons,
        "backlog": backlog(conn),
        "verdict": verdict,
        "db_readable": readable,
    }


def _verdict(by_key: dict, label: str) -> str:
    """One plain sentence about the biggest movement, with both numbers."""
    sent = by_key.get("sent", {})
    refused = by_key.get("refused", {})
    skipped = by_key.get("skipped", {})
    bits = []
    if sent:
        change = sent.get("pct")
        if change is None:
            bits.append(f"{sent['current']:,} sent in the last {label}")
        elif change == 0:
            bits.append(f"{sent['current']:,} sent, exactly the same as the {label} before")
        else:
            bits.append(f"{sent['current']:,} sent, {change:+.1f}% vs the {label} before")
    if skipped.get("current"):
        bits.append(f"{skipped['current']:,} skipped")
    if refused.get("current"):
        bits.append(f"{refused['current']:,} refused by the content policy")
    if sent.get("current") == 0 and sent.get("previous"):
        bits.insert(0, "NOTHING was delivered")
    return (" · ".join(bits) or "no activity in this window") + "."


# --------------------------------------------------------------------------- #
#  link hygiene - "is every published link provably ours?"
# --------------------------------------------------------------------------- #
# The classification itself lives in ops/category_report.py (url_money), the
# same verdicts the money report and the conversion card use, so the brief can
# never disagree with them. This module only remembers the answer: /api/brief
# runs every five seconds and the scan reads 5,000 cache rows (~170 ms).
_HYGIENE_MEMO: dict = {}
_HYGIENE_TTL = 60.0


def link_hygiene(conn: sqlite3.Connection, reader, our_tags, *,
                 ttl: float = _HYGIENE_TTL, now: float | None = None,
                 limit: int = 3) -> dict:
    """Memoised `reader(conn, our_tags, limit=...)`.

    Cached per database state (path, mtime, size), so a changed database never
    serves a stale answer and an unchanged one costs nothing. `reader` is the
    category-report function, injected because importing it here would drag the
    whole report into every dashboard request.
    """
    now = time.time() if now is None else now
    if conn is None or reader is None:
        return {"available": False, "memo": False, "checked_age_s": 0.0,
                "checked_at": now}
    try:
        db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        key = (str(db_path), db_path.stat().st_mtime, db_path.stat().st_size)
    except (sqlite3.Error, OSError, TypeError, IndexError):
        key = ("", 0, 0)
    # The tag set is part of the key: a newly added tag changes every verdict.
    key = (key, tuple(sorted(str(t).lower() for t in (our_tags or ()))))
    cached = _HYGIENE_MEMO.get("entry")
    if cached and cached["key"] == key and now - cached["at"] < ttl:
        return {**cached["value"], "memo": True,
                "checked_age_s": round(now - cached["at"], 1)}
    try:
        value = dict(reader(conn, our_tags, limit=limit) or {})
    except Exception as exc:  # noqa: BLE001 - never let the brief die for a stat
        return {"available": False, "error": str(exc)[:200], "memo": False,
                "checked_at": now, "checked_age_s": 0.0}
    value.update({"memo": False, "checked_at": now, "checked_age_s": 0.0})
    _HYGIENE_MEMO["entry"] = {"key": key, "at": now, "value": value}
    return value


# --------------------------------------------------------------------------- #
#  attention - what a human has to do, ranked
# --------------------------------------------------------------------------- #
SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}
README_FIX = {
    "db_present": "Start the bot once (it creates the file), or point BOT_DB_PATH at the real DB.",
    "db_integrity": "Stop the bot and run: sqlite3 <db> 'PRAGMA integrity_check;' then .recover",
    "db_schema": "Restart the bot on the merged code; it creates missing tables at start.",
    "rules_parity": "Copy bestgaa/category_rules.json over tg-wa-bridge/category_rules.json.",
    "policy_parity": "Ship the same safety_rules.json to the bot and the bridge.",
    "channels_config": "Save a channel in the dashboard, or ignore if you only use built-ins.",
    "routing_snapshot": "Restart the bot; it publishes routing_snapshot.json at start.",
    "bot_log": "journalctl -u bestgaa -n 200 --no-pager",
    "disk": "Free space: logs and -wal files grow during a retry storm.",
}


def attention(*, health: dict | None = None, drift: dict | None = None,
              safety: dict | None = None, trends: dict | None = None,
              categories: list | None = None, conversion: dict | None = None,
              links: dict | None = None, pipeline: dict | None = None,
              limit: int = 12,
              now: float | None = None) -> dict:
    """Ranked "what needs you now" list, built only from facts already loaded.

    Nothing here queries anything: pass in the health payload, the rules drift,
    a refusal-spike reading, the trend comparison, category findings and the
    conversion summary, and get back one deduplicated, severity-ordered list.
    That keeps the list honest - it cannot invent a fault the panels do not
    show - and cheap enough to refresh every few seconds.
    """
    now = time.time() if now is None else now
    items: list[dict] = []

    def add(severity: str, key: str, title: str, detail: str, fix: str = "",
            anchor: str = "") -> None:
        if severity not in SEVERITY_RANK:
            severity = "info"
        items.append({"id": f"{key}:{len(items)}", "kind": key, "severity": severity,
                      "title": title, "detail": detail, "fix": fix or "",
                      "anchor": anchor, "at": now})

    for check in (health or {}).get("checks") or []:
        state = check.get("state")
        if state not in ("fail", "warn"):
            continue
        severity = "error" if state == "fail" else "warn"
        key = str(check.get("key") or "check")
        fix = check.get("fix") or README_FIX.get(key.split(":")[-1], "")
        add(severity, key, str(check.get("title") or key), str(check.get("detail") or ""),
            fix, "#console")

    if drift:
        installed = drift.get("installed")
        running = drift.get("running")
        if installed and running and str(installed) != str(running):
            add("error", "rules_drift",
                f"Bot is classifying with v{running}, this checkout has v{installed}",
                "Category decisions made since the last deploy may not match the rules the "
                "dashboard shows. A restart applies the installed rules.",
                "sudo systemctl restart bestgaa", "#categories")
        if drift.get("pending_restart"):
            add("warn", "pending_restart", "Saved channel changes are not applied yet",
                "New channels can activate live; edits and removals need an approved restart.",
                "Restart the bot from the console, or sudo systemctl restart bestgaa", "#console")

    if safety:
        last_hour = int(safety.get("last_hour") or 0)
        baseline = float(safety.get("avg_hour") or 0)
        if last_hour >= 3 and baseline and last_hour > max(3.0, baseline * 3):
            add("warn", "refusal_spike",
                f"{last_hour} refusals in the last hour",
                f"About {baseline:.1f}/hour is normal over seven days; something is flooding "
                "intake with content the policy refuses.",
                "Open Safety to see which source and category, then mute that source.", "#safety")

    if trends:
        by_key = trends.get("by_metric") or {}
        sent = by_key.get("sent") or {}
        skipped = by_key.get("skipped") or {}
        refused = by_key.get("refused") or {}
        label = trends.get("window_label") or "window"
        if sent.get("current") == 0 and int(sent.get("previous") or 0) > 0:
            add("error", "no_sends",
                f"Nothing delivered in the last {label}",
                f"The {label} before delivered {int(sent['previous']):,}. Either the bot is down, "
                "every target is skipping, or the queue is empty.",
                "Open Console health, then Trace the newest post.", "#console")
        elif sent.get("pct") is not None and float(sent["pct"]) <= -40 and int(sent.get("current") or 0) > 0:
            add("warn", "sends_down",
                f"Deliveries down {abs(float(sent['pct'])):.0f}% vs the {label} before",
                f"{int(sent.get('current') or 0):,} delivered now against "
                f"{int(sent.get('previous') or 0):,} before.",
                "Check the top skip reason on the scorecard before changing any rule.", "#scorecard")
        if skipped.get("pct") is not None and float(skipped["pct"]) >= 60 and int(skipped.get("current") or 0) >= 20:
            add("warn", "skips_up",
                f"Skips up {float(skipped['pct']):.0f}% vs the {label} before",
                f"{int(skipped.get('current') or 0):,} skipped now against "
                f"{int(skipped.get('previous') or 0):,} before.",
                "The scorecard names the top reason per channel.", "#scorecard")
        if refused.get("pct") is not None and float(refused["pct"]) >= 100 and int(refused.get("current") or 0) >= 10:
            add("warn", "refused_up",
                f"Content refusals doubled: {int(refused.get('current') or 0):,}",
                f"Up from {int(refused.get('previous') or 0):,} in the {label} before.",
                "Open Safety: the source and category of every refusal are listed.", "#safety")

    if pipeline:
        lost = int(pipeline.get("unaccounted") or 0)
        if lost:
            ids = pipeline.get("unaccounted_ids") or []
            add("error", "pipeline_unaccounted",
                f"{lost} post(s) cannot be accounted for",
                "Taken in, never posted, and no reason recorded"
                + (" - queue " + ", ".join(str(i) for i in ids[:8]) if ids else "")
                + ". This is the one finding that can mean a lost deal or a lost commission.",
                "Open Integrity, then Trace for those queue ids.", "#pipeline")
        elif str(pipeline.get("overall")) == "warn":
            top = next((row for row in pipeline.get("findings", [])
                        if row.get("state") == "warn"), None)
            if top:
                add("warn", "pipeline_" + str(top.get("key", "finding")),
                    str(top.get("title") or "Pipeline warning"),
                    str(top.get("detail") or ""),
                    str(top.get("fix") or ""), "#pipeline")

    for finding in categories or []:
        severity = "error" if finding.get("severity") == "error" else (
            "warn" if finding.get("severity") == "warn" else "info")
        kind = str(finding.get("kind") or "category")
        where = f" ({finding['category']})" if finding.get("category") else ""
        add(severity, f"category:{kind}", f"Category: {kind.replace('_', ' ')}{where}",
            str(finding.get("detail") or ""),
            "Open Categories for the shelf, its channel and its last delivery.", "#categories")

    if links and links.get("available", True):
        hops = int(links.get("unproven_hops") or 0)
        if hops:
            example = (links.get("hop_examples") or [{}])[0].get("url", "")
            add("error", "unproven_hops",
                f"{hops:,} published link(s) are short hops we cannot prove are ours",
                "A hop opens fine but says nothing about who minted it; the two is.gd links "
                "reported on 2026-10-21 resolved to untagged Amazon pages and earned nothing."
                + (f" Newest: {example}" if example else ""),
                "python3 ops/link_doctor.py --recent 50 --strict", "#conversion")
        nothing = int(links.get("earns_nothing") or 0)
        cached = int(links.get("cached") or 0)
        if not hops and cached and nothing >= max(10, cached * 0.5):
            add("warn", "earns_nothing_links",
                f"{nothing:,} of {cached:,} published links earn nothing",
                "They carry no tag of ours and no stored proof, so every click pays nobody.",
                "python3 ops/link_doctor.py --recent 50", "#conversion")

    if conversion:
        foreign = int(conversion.get("foreign_tag") or 0)
        nothing = int(conversion.get("earns_nothing") or 0)
        posts = int(conversion.get("posts") or 0)
        if foreign:
            add("error", "foreign_tag",
                f"{foreign:,} published link(s) carry someone else's affiliate tag",
                "Those clicks earn for another account. Every published link must be ours.",
                "python3 ops/conversion_report.py   # then re-check the offenders", "#conversion")
        elif posts and nothing == posts:
            add("warn", "no_earners",
                f"All {posts:,} delivered posts in this window earn nothing",
                "No owned short link or publisher link was found behind them.",
                "python3 ops/earnkaro_check.py --plan --deal-url '<a real source link>'",
                "#conversion")

    # Deduplicate on (severity, title): health and the trend checks can describe
    # the same silence, and the operator should read it once.
    seen: set[tuple[str, str]] = set()
    unique = []
    for item in items:
        fingerprint = (item["severity"], item["title"])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(item)
    unique.sort(key=lambda item: (SEVERITY_RANK[item["severity"]], item["title"]))
    unique = unique[:max(1, int(limit))]

    counts = {name: sum(1 for item in unique if item["severity"] == name)
              for name in SEVERITY_RANK}
    if not unique:
        headline = "Nothing needs you - every check is green."
    else:
        head = unique[:2]
        headline = (f"{len(unique)} thing{'s' if len(unique) > 1 else ''} need"
                    f"{'' if len(unique) > 1 else 's'} you: "
                    + "; ".join(item["title"] for item in head))
        if len(unique) > 2:
            headline += f"; +{len(unique) - 2} more"
        headline += "."
    return {
        "generated_at": now,
        "healthy": not unique,
        "counts": counts,
        "headline": headline,
        "items": unique,
        "sources": {"health": bool(health), "categories": categories is not None,
                    "trends": bool(trends), "conversion": bool(conversion),
                    "links": bool(links), "safety": bool(safety),
                    "drift": bool(drift)},
    }


def drift_facts(*, installed, running, pending_restart: bool = False,
                bridge_rules=None) -> dict:
    """The shape attention() expects for rules drift, from values already read."""
    return {"installed": installed, "running": running,
            "pending_restart": bool(pending_restart), "bridge": bridge_rules}


def safety_spike(conn: sqlite3.Connection, *, now: float | None = None) -> dict:
    """Refusals in the last hour against the seven-day hourly average."""
    now = time.time() if now is None else now
    if "blocked_content" not in _tables(conn):
        return {"last_hour": 0, "avg_hour": 0.0, "week": 0}
    last_hour = _scalar(conn, "SELECT COUNT(*) FROM blocked_content WHERE created_at>=?",
                        (now - 3600,))
    week = _scalar(conn, "SELECT COUNT(*) FROM blocked_content WHERE created_at>=?",
                   (now - 7 * 86400,))
    return {"last_hour": last_hour, "avg_hour": round(week / (7 * 24), 2), "week": week}


def summary(conn: sqlite3.Connection, *, hours: float = 24.0, now: float | None = None) -> dict:
    """The small, cheap part of `compare()`: six totals and the backlog."""
    now = time.time() if now is None else now
    hours = max(0.25, min(168.0, float(hours)))
    since = now - hours * 3600
    facts = _sums(conn, start=since, end=now, buckets=4)
    return {key: {"current": int(facts.get(key, {}).get("value", 0))}
            for key, _label, _good, _why in METRICS} | {
        "backlog": backlog(conn),
        "window_hours": hours,
        "since_ist": ist(since),
        "age": age(now - since),
    }
