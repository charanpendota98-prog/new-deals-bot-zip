"""Pipeline integrity: did every post we took in actually come out somewhere?

The user's standard is "edho okati miss avvakoodadu" - nothing dropped. Every
other panel answers a question about *one* stage (what the bot logged, what the
ledger says, what a channel received). This module answers the only question
that cannot be answered by looking at one stage:

    intake = posted + withheld + waiting + failed + dropped(reason)
                                                    ^^^^^^^^^^^^^^^^^ must be ZERO
    any post outside that equation is "unaccounted" and is reported as an error

It reads the queue table and the delivery ledger for the same window, classifies
every intake into exactly one terminal or live state, and reports anything that
falls outside the equation - a post the bot took in and never recorded a reason
for. That is the only honest definition of a "dropped" post, and it is reported
as an error, never averaged away.

Everything here is read-only, defensive about old/partial schemas, and pure over
a connection so it can be unit-tested against a synthetic database.
"""
from __future__ import annotations

import sqlite3
import time

try:  # package import (dashboard/app.py) and script import (tests) both work
    from . import insight
except ImportError:  # pragma: no cover - script-style import
    import insight  # type: ignore

DAY = insight.DAY
IST = insight.ist
AGE = insight.age

# Queue `status` values written by the bot.
LIVE = {"pending", "processing"}
TERMINAL = {"done", "failed"}
# Delivery `status` values (older builds wrote 'sent' + an error for a withheld
# row, so a non-empty last_error on a 'sent' row is still a WITHHOLD, not a send).
SENT = "sent"
SKIPPED = "skipped"
HELD = "held"

REASON_MAX = 90


def _rows(conn: sqlite3.Connection, sql: str, args=()) -> list[tuple]:
    try:
        return list(conn.execute(sql, args))
    except sqlite3.OperationalError:
        return []


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _percentile(values: list[float], share: float) -> float | None:
    """Nearest-rank percentile; no numpy, no surprise interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))
    return float(ordered[index])


def _pct(part: float, whole: float) -> float:
    return round(100.0 * float(part) / max(1.0, float(whole)), 1)


def _reason(text: str) -> str:
    """Group free-text errors so a table shows ten rows, not a thousand."""
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    return value[:REASON_MAX]


def _classify(queue_status: str, sends: int, withheld: int, held: int,
              waiting: int, reason: str = "") -> str:
    """One state per intake, in the order the operator cares about.

    A row with at least one real send is POSTED even if another target refused
    it: the post did reach a channel. A row with no send but a delivery-level
    refusal is WITHHELD (a decision recorded per target). A row the queue itself
    closed with a reason (night window, stale, duplicate at intake) is DROPPED -
    explained, and therefore not a hole. A row that is still live is WAITING.
    Only a terminal row with no send, no delivery row and no reason at all is
    UNACCOUNTED, and that is the one state this module treats as an error.
    """
    if sends > 0:
        return "posted"
    if withheld > 0 or held > 0:
        return "withheld"
    if queue_status in LIVE or waiting > 0:
        return "waiting"
    if queue_status == "failed":
        return "failed"
    return "dropped" if str(reason or "").strip() else "unaccounted"


def funnel(conn: sqlite3.Connection, *, hours: float = 24.0,
           now: float | None = None, declared_sources=None,
           declared_targets=None, watched=None) -> dict:
    """The whole intake → delivery accounting for one window.

    `declared_sources` / `declared_targets` / `watched` come from the bot's own
    config + snapshot, so this module can say "you asked for this and nothing
    arrived" instead of only describing what happened to arrive.
    """
    now = time.time() if now is None else float(now)
    hours = max(0.25, min(24 * 14, float(hours)))
    since = now - hours * 3600.0
    tables = insight._tables(conn)
    declared_sources = {str(s).lower() for s in (declared_sources or ())}
    declared_targets = [str(t) for t in (declared_targets or ())]
    watched = {str(w).lower() for w in (watched or ())}

    queues: dict[int, dict] = {}
    if "queue" in tables:
        cols = insight._cols(conn, "queue")
        error_expr = "COALESCE(last_error,'')" if "last_error" in cols else "''"
        for row in _rows(conn,
                         "SELECT id, source, status, " + error_expr + ", created_at "
                         "FROM queue WHERE created_at>=? AND created_at<=? "
                         "ORDER BY created_at", (since, now)):
            queues[int(row[0])] = {
                "id": int(row[0]), "source": str(row[1] or ""), "status": str(row[2] or ""),
                "error": str(row[3] or ""), "created_at": float(row[4] or 0),
                "sends": 0, "withheld": 0, "held": 0, "waiting": 0,
                "targets_sent": set(), "reasons": {}, "first_sent": None,
            }

    if queues and "deliveries" in tables:
        dcols = insight._cols(conn, "deliveries")
        derror = "COALESCE(d.last_error,'')" if "last_error" in dcols else "''"
        for queue_id, target, status, error in _rows(
                conn,
                "SELECT d.queue_id, d.target, d.status, " + derror + " "
                "FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                "WHERE q.created_at>=? AND q.created_at<=?",
                (since, now)):
            job = queues.get(int(queue_id))
            if job is None:
                continue
            status = str(status or "")
            error = _reason(error)
            if status == SENT and not error:
                job["sends"] += 1
                job["targets_sent"].add(str(target or ""))
            elif status in {SKIPPED, HELD} or (status == SENT and error):
                job["withheld"] += 1
                if error:
                    job["reasons"][error] = job["reasons"].get(error, 0) + 1
                if status == HELD:
                    job["held"] += 1
            elif status == "pending":
                job["waiting"] += 1

    # When did the first chunk of each post actually leave (queue ingest → send)?
    if queues and "send_attempts" in tables:
        for queue_id, stamp in _rows(
                conn,
                "SELECT s.queue_id, MIN(s.updated_at) FROM send_attempts s "
                "JOIN queue q ON q.id=s.queue_id "
                "WHERE s.state='sent' AND q.created_at>=? AND q.created_at<=? "
                "GROUP BY s.queue_id", (since, now)):
            job = queues.get(int(queue_id))
            if job is not None and stamp:
                job["first_sent"] = float(stamp)

    # One pass over the receipts: classify the post AND fold it into its source.
    # (The obvious "for each source, filter every job" shape is O(sources x jobs)
    # and cost ~0.6 s on a 50k-post window; this is one pass.)
    by_source: dict[str, dict] = {}
    for job in queues.values():
        job["state"] = _classify(job["status"], job["sends"], job["withheld"],
                                 job["held"], job["waiting"], job["error"])
        if job["state"] == "posted" and job["first_sent"]:
            job["latency"] = max(0.0, job["first_sent"] - job["created_at"])
        else:
            job["latency"] = None
        if job["state"] == "waiting":
            job["wait_age"] = max(0.0, now - job["created_at"])
        else:
            job["wait_age"] = 0.0
        bucket = by_source.setdefault(job["source"], {
            "state": {}, "intake": 0, "reasons": {}, "latencies": []})
        bucket["intake"] += 1
        bucket["state"][job["state"]] = bucket["state"].get(job["state"], 0) + 1
        if job["latency"] is not None:
            bucket["latencies"].append(job["latency"])
        for reason, count in job["reasons"].items():
            bucket["reasons"][reason] = bucket["reasons"].get(reason, 0) + count

    states = ("posted", "withheld", "dropped", "waiting", "failed", "unaccounted")
    totals = {state: sum(1 for job in queues.values() if job["state"] == state)
              for state in states}
    intake = len(queues)
    unexplained = totals["unaccounted"]

    # ---- per source -------------------------------------------------------
    cadence: dict[str, list[float]] = {}
    if "queue" in tables:
        for source, stamp in _rows(
                conn,
                "SELECT source, created_at FROM queue WHERE created_at>=? AND created_at<=? "
                "ORDER BY created_at", (now - 7 * DAY, now)):
            cadence.setdefault(str(source or ""), []).append(float(stamp or 0))
    all_sources = set(cadence) | {job["source"] for job in queues.values()}
    source_rows: list[dict] = []
    for source in all_sources:
        bucket = by_source.get(source) or {"state": {}, "intake": 0, "reasons": {},
                                           "latencies": []}
        mine = bucket["intake"]
        counts = {state: bucket["state"].get(state, 0) for state in states}
        stamps = cadence.get(source, [])
        gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
        median_gap = _median(gaps)
        last = stamps[-1] if stamps else 0.0
        silent_for = (now - last) if last else None
        reason_counts = bucket["reasons"]
        top_reason = max(reason_counts.items(), key=lambda item: item[1])[0] if reason_counts else ""
        latencies = bucket["latencies"]
        is_declared = source.lower() in declared_sources
        ratio = (round(float(silent_for) / median_gap, 1)
                 if median_gap and silent_for is not None else None)
        overdue = bool(is_declared and median_gap and silent_for is not None
                       and silent_for > max(3 * median_gap, 1800))
        if counts["unaccounted"]:
            verdict, why = "unaccounted", "taken in, never posted, and no reason recorded"
        elif counts["posted"] and counts["withheld"] > counts["posted"]:
            verdict, why = "withholding", "more posts were held back than sent"
        elif counts["posted"]:
            verdict, why = "delivering", "posts are reaching channels"
        elif counts["waiting"]:
            verdict, why = "waiting", "taken in, not sent yet"
        elif counts["failed"]:
            verdict, why = "failing", "every intake in this window failed"
        elif counts["withheld"]:
            verdict, why = "withholding", "every intake in this window was held back"
        elif overdue:
            verdict, why = "overdue", (f"quiet for {ratio:g}x its usual gap"
                                       if ratio else "silent far longer than its own rhythm")
        elif mine:
            verdict, why = "quiet", "nothing to send from this source yet"
        elif is_declared:
            verdict, why = "silent", "configured, but nothing arrived in this window"
        else:
            verdict, why = "observed", "seen in the database, not in the saved config"
        source_rows.append({
            "source": source,
            "declared": is_declared,
            "watched": source.lower() in watched,
            "intake": mine,
            "posted": counts["posted"], "withheld": counts["withheld"],
            "waiting": counts["waiting"], "failed": counts["failed"],
            "dropped": counts["dropped"],
            "unaccounted": counts["unaccounted"],
            "post_share": _pct(counts["posted"], mine),
            "top_withhold_reason": top_reason,
            "last_intake_at": last or None,
            "last_intake": IST(last) if last else "—",
            "silent_for": silent_for,
            "silent_for_human": AGE(silent_for) if silent_for is not None else "—",
            "typical_gap": median_gap,
            "typical_gap_human": AGE(median_gap) if median_gap else "—",
            "silence_ratio": ratio,
            "latency_p50": _percentile(latencies, 0.5),
            "latency_p95": _percentile(latencies, 0.95),
            "verdict": verdict, "why": why,
        })
    order = {"unaccounted": 0, "withholding": 1, "failing": 2, "silent": 3,
             "overdue": 4, "waiting": 5, "quiet": 6, "delivering": 7, "observed": 8}
    source_rows.sort(key=lambda row: (order.get(row["verdict"], 9),
                                      -row["unaccounted"], -row["intake"]))

    # ---- per destination --------------------------------------------------
    received: dict[str, int] = {}
    withheld_by_target: dict[str, int] = {}
    if queues and "deliveries" in tables:
        dcols = insight._cols(conn, "deliveries")
        derror = "COALESCE(d.last_error,'')" if "last_error" in dcols else "''"
        for target, status, error, count in _rows(
                conn,
                "SELECT d.target, d.status, " + derror + ", COUNT(*) "
                "FROM deliveries d JOIN queue q ON q.id=d.queue_id "
                "WHERE q.created_at>=? AND q.created_at<=? GROUP BY d.target, d.status, "
                + derror, (since, now)):
            name = str(target or "")
            if str(status) == SENT and not str(error or ""):
                received[name] = received.get(name, 0) + int(count)
            else:
                withheld_by_target[name] = withheld_by_target.get(name, 0) + int(count)
    target_rows = []
    for target in sorted(set(declared_targets) | set(received) | set(withheld_by_target)):
        sends = received.get(target, 0)
        holds = withheld_by_target.get(target, 0)
        declared = target in declared_targets
        if sends:
            state = "receiving"
        elif holds:
            state = "refusing"
        elif declared:
            state = "silent"
        else:
            state = "unknown"
        target_rows.append({"target": target, "declared": declared, "sends": sends,
                            "withheld": holds, "state": state})

    # ---- integrity findings ----------------------------------------------
    findings: list[dict] = []

    def add(key, state, title, detail, fix=""):
        findings.append({"key": key, "state": state, "title": title,
                         "detail": detail, "fix": fix})

    if unexplained:
        ids = sorted(job["id"] for job in queues.values()
                     if job["state"] == "unaccounted")[:25]
        add("unaccounted", "fail",
            f"{unexplained} intake(s) cannot be accounted for",
            "No send, no recorded reason, and the job is not waiting: queue "
            + ", ".join(str(i) for i in ids) + ("…" if unexplained > len(ids) else ""),
            "Open Trace for those queue ids; a row like this means the post left the "
            "queue without a delivery row or an error. Never let it sit - it is a "
            "possible lost post or a possible lost commission.")
    if totals["failed"]:
        reasons: dict[str, int] = {}
        for job in queues.values():
            if job["state"] == "failed":
                reasons[_reason(job["error"]) or "no reason recorded"] = 1 + reasons.get(
                    _reason(job["error"]) or "no reason recorded", 0)
        top = sorted(reasons.items(), key=lambda item: -item[1])[:3]
        add("failed", "warn", f"{totals['failed']} intake(s) failed",
            "; ".join(f"{count}× {reason[:70]}" for reason, count in top),
            "Trace a failed queue id to see which stage refused it. A failure must "
            "always carry a reason; an empty one is itself a bug.")
    stuck = [job for job in queues.values()
             if job["status"] == "processing" and job["wait_age"] > 900]
    if stuck:
        add("stuck", "warn", f"{len(stuck)} post(s) stuck in processing",
            "Oldest: queue " + str(sorted(job["id"] for job in stuck)[0])
            + f", waiting {AGE(max(job['wait_age'] for job in stuck))}",
            "The bot reclaims orphaned 'processing' rows after 30 minutes; if this "
            "keeps happening the worker is dying mid-job - check the log for "
            "WORKER crashed lines.")
    oldest_wait = max((job["wait_age"] for job in queues.values()
                       if job["state"] == "waiting"), default=0.0)
    if oldest_wait > 3600:
        add("backlog", "warn", f"oldest waiting post is {AGE(oldest_wait)} old",
            f"{totals['waiting']} post(s) waiting to be sent",
            "Check the queue panel and the quiet window (02:00-06:00 IST pauses "
            "posting on purpose); a backlog older than that is a stalled worker.")
    silent_targets = [row for row in target_rows if row["declared"] and row["state"] == "silent"]
    if silent_targets:
        add("silent_targets", "warn",
            f"{len(silent_targets)} configured channel(s) received nothing",
            ", ".join(row["target"] for row in silent_targets[:6]),
            "Either the category policy withheld everything for that shelf, or the "
            "route does not include it. Check Categories for its withhold reasons.")
    silent_sources = [row for row in source_rows if row["verdict"] in {"silent", "overdue"}]
    if silent_sources:
        add("silent_sources", "info",
            f"{len(silent_sources)} source(s) are quieter than their own rhythm",
            ", ".join(f"{row['source']} ({row['silent_for_human']})"
                      for row in silent_sources[:6]),
            "A watched source that stops posting usually means the bot lost channel "
            "access or the source moved. Open Watch source to see its live state.")
    undeclared = [row for row in source_rows if not row["declared"] and row["intake"]]
    if undeclared and declared_sources:
        add("undeclared", "info",
            f"{len(undeclared)} source(s) are posting to us but are not in channels.json",
            ", ".join(row["source"] for row in undeclared[:6]),
            "That is normal for the built-in feeds (they live in code). If it is not a "
            "built-in, add it to the dashboard so its route has an owner.")

    states_present = [finding["state"] for finding in findings]
    overall = "fail" if "fail" in states_present else (
        "warn" if "warn" in states_present else "ok")

    equation = (f"{intake} in = {totals['posted']} posted + {totals['withheld']} withheld "
                f"+ {totals['dropped']} dropped (reason recorded) + "
                f"{totals['waiting']} waiting + {totals['failed']} failed")
    if unexplained:
        verdict = (f"ALERT: {unexplained} post(s) unaccounted for - "
                   f"{equation} + {unexplained} unexplained")
    elif intake:
        verdict = f"Nothing dropped: {equation}"
    else:
        verdict = "No intake in this window"

    latency_all = [job["latency"] for job in queues.values() if job["latency"] is not None]
    return {
        "generated_at": now,
        "window_hours": hours,
        "since": since,
        "since_ist": IST(since),
        "overall": overall,
        "verdict": verdict,
        "equation": equation,
        "intake": intake,
        "tiles": [
            {"key": "intake", "label": "taken in", "value": intake, "state": "ok"},
            {"key": "posted", "label": "posted", "value": totals["posted"], "state": "ok"},
            {"key": "withheld", "label": "withheld (per channel)", "value": totals["withheld"],
             "state": "info"},
            {"key": "dropped", "label": "dropped (reason recorded)", "value": totals["dropped"],
             "state": "info"},
            {"key": "waiting", "label": "waiting", "value": totals["waiting"],
             "state": "info"},
            {"key": "failed", "label": "failed", "value": totals["failed"],
             "state": "warn" if totals["failed"] else "ok"},
            {"key": "unaccounted", "label": "UNACCOUNTED", "value": unexplained,
             "state": "fail" if unexplained else "ok"},
        ],
        "first_send_seconds": {"p50": _percentile(latency_all, 0.5),
                               "p95": _percentile(latency_all, 0.95),
                               "max": max(latency_all) if latency_all else None},
        "blocked_keys": ["unaccounted", "failed", "stuck", "backlog", "silent_targets"],
        "findings": findings,
        "sources": source_rows,
        "targets": target_rows,
        "declared_and_real": {
            "declared": len(declared_sources),
            "watched": len(watched),
            "seen": len({job["source"] for job in queues.values()}),
            "declared_but_silent": [row["source"] for row in source_rows
                                    if row["verdict"] in {"silent", "overdue"}],
            "seen_but_not_declared": [row["source"] for row in source_rows
                                      if not row["declared"] and row["intake"]],
        },
        "database": "ok" if "queue" in tables else "old",
    }


def unaccounted_ids(conn: sqlite3.Connection, *, hours: float = 24.0,
                    now: float | None = None, limit: int = 200) -> list[int]:
    """Just the queue ids that break the equation - for a drill-down or a CLI.

    Same definition as `funnel`: terminal, no delivery row at all, and not even a
    stored reason. Anything with a reason is a decision, not a hole.
    """
    now = time.time() if now is None else float(now)
    since = now - max(0.25, min(24 * 14, float(hours))) * 3600.0
    if "queue" not in insight._tables(conn):
        return []
    cols = insight._cols(conn, "queue")
    if "last_error" not in cols:
        return []
    return [int(row[0]) for row in _rows(
        conn,
        "SELECT q.id FROM queue q WHERE q.created_at>=? AND q.created_at<=? "
        "AND q.status IN ('done','failed') AND COALESCE(q.last_error,'')='' "
        "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.queue_id=q.id) "
        "ORDER BY q.id LIMIT ?", (since, now, int(limit)))]
