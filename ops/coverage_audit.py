#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BestGAA source-coverage auditor (and self-healer).

Answers one question with numbers instead of feelings: "source lo anni posts
unnayi, mana channels lo post ayaya?"  It reads the bot's SQLite DB and compares
what arrived from each source with what actually reached at least one target,
then classifies every gap:

  posted      at least one target received it
  in-flight   pending / processing right now (the workers are on it)
  dedup       refused on purpose: the same campaign/product was already posted
  policy      refused on purpose: night window, stale loot, no deal content
  LOST        closed without ever reaching a target for a reason that the current
              code can handle (conversion/API/provenance/health/skip bugs)

LOST rows are exactly the "posts that are in the source but never appeared in our
channels".  `--heal` re-queues them (plus every 'failed' row in the window) so the
running bot posts them on its next cycle.  Dedup truth is NOT cleared, so a
healed row that a second source already delivered is refused again as a
duplicate instead of posting twice.

Read-only by default; `--heal` takes a timestamped DB backup first.

  python3 ops/coverage_audit.py                      # last 12 hours, all sources
  python3 ops/coverage_audit.py --hours 48 --show 40
  python3 ops/coverage_audit.py --heal --db /opt/bestgaa-bot/bestgaa.sqlite3
  python3 ops/coverage_audit.py --json | jq '.lost | length'

Exit status: 0 = nothing lost, 1 = recoverable gaps exist, 2 = healed some rows
(the bot will post them), 3 = DB/schema problem.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# A closed job is a *policy* refusal (correct behaviour, never re-queue it) when
# its recorded reason says the deal was a duplicate, expired, out of window, or
# carried no deal content at all.  Anything else that closed without a successful
# send is a bug-shaped gap and gets reported as LOST.
POLICY_MARKERS = (
    "duplicate", "already posted", "already delivered", "all products",
    "night", "stale", "expired", "no urls", "no affiliate", "no monetizable",
    "no deal content", "not a deal", "promo", "skipped: no", "quiet",
    "silence", "no eligible targets", "oversized",
)
# Reasons the CURRENT build can recover: the deal had real content, the old code
# just gave up on it.
RECOVERABLE_MARKERS = (
    "conversion retry required", "returned no link", "provenance", "no verified",
    "no monetizable", "broken merchant destination", "foreign url survived",
    "no links survived", "unresolved", "blocked before send", "stuck",
    "target unresolved", "no affiliate url",
)


def default_db() -> Path:
    env = os.getenv("BOT_DB_PATH", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    for candidate in (
        Path.cwd() / "bestgaa.sqlite3",
        here / "bestgaa.sqlite3",
        here.parent / "bestgaa" / "bestgaa.sqlite3",
        Path.home() / "bestgaa-bot" / "bestgaa-bot" / "bestgaa.sqlite3",
        Path.home() / "bestgaa" / "bestgaa.sqlite3",
    ):
        if candidate.exists():
            return candidate
    return here.parent / "bestgaa" / "bestgaa.sqlite3"


def classify(last_error: str, status: str, sent: int) -> str:
    if sent:
        return "posted"
    reason = (last_error or "").lower()
    if status in ("pending", "processing"):
        return "in-flight"
    if any(marker in reason for marker in RECOVERABLE_MARKERS):
        return "lost"
    if any(marker in reason for marker in ("duplicate", "already posted", "all products",
                                           "duplicate pending content")):
        return "dedup"
    if any(marker in reason for marker in POLICY_MARKERS):
        return "policy"
    if not reason:
        return "policy"          # closed silently by an older build: usually a skip
    return "lost"


def audit(db: Path, hours: float) -> dict:
    if not db.exists():
        raise SystemExit(f"ERROR: no database at {db} (run this on the bot server)")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = time.time() - hours * 3600
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "queue" not in tables:
        raise SystemExit("ERROR: this DB has no 'queue' table - wrong database?")
    has_deliveries = "deliveries" in tables
    rows = conn.execute(
        "SELECT id, chat_id, msg_id, source, status, attempts, last_error, created_at "
        "FROM queue WHERE created_at>=?", (since,),
    ).fetchall()
    sent_counts: dict[int, int] = {}
    if has_deliveries:
        for r in conn.execute("SELECT queue_id, COUNT(*) c FROM deliveries "
                              "WHERE status='sent' GROUP BY queue_id"):
            sent_counts[r["queue_id"]] = r["c"]
    per_source: dict[str, dict[str, int]] = {}
    lost: list[dict] = []
    for row in rows:
        state = classify(row["last_error"] or "", row["status"], sent_counts.get(row["id"], 0))
        bucket = per_source.setdefault(row["source"], {"seen": 0, "posted": 0, "in-flight": 0,
                                                        "dedup": 0, "policy": 0, "lost": 0})
        bucket["seen"] += 1
        bucket[state] += 1
        if state == "lost":
            lost.append({"id": row["id"], "source": row["source"], "chat_id": row["chat_id"],
                         "msg_id": row["msg_id"], "attempts": row["attempts"],
                         "age_h": round((time.time() - row["created_at"]) / 3600, 1),
                         "reason": (row["last_error"] or "")[:160]})
    conn.close()
    return {"window_hours": hours, "rows": len(rows), "per_source": per_source, "lost": lost}


def heal(db: Path, ids: list[int], backup: bool = True) -> int:
    if not ids:
        return 0
    if backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(db, db.with_suffix(f".sqlite3.pre-audit-heal.{stamp}"))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        now = time.time()
        mark = f"audit heal at {time.strftime('%Y-%m-%d %H:%M')}"
        done = 0
        for job_id in ids:
            row = conn.execute("SELECT id, status FROM queue WHERE id=?", (job_id,)).fetchone()
            if not row:
                continue
            conn.execute(
                "UPDATE queue SET status='pending', next_at=?, attempts=0, rendered_text=NULL, "
                "last_error=? WHERE id=?",
                (now, mark, job_id),
            )
            # Forget only the *live claim* on this job so re-render is allowed.
            # posted_deals (the record of what subscribers already saw) is left
            # untouched: if the deal really went out from another source, the
            # normal dedup refuses it again instead of posting twice.
            conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (job_id,))
            if "deliveries" in {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}:
                conn.execute("UPDATE deliveries SET chunks_sent=0 WHERE queue_id=? AND status<>'sent'",
                             (job_id,))
            done += 1
        conn.commit()
        return done
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="BestGAA source coverage audit")
    ap.add_argument("--db", default=str(default_db()), help="path to bestgaa.sqlite3")
    ap.add_argument("--hours", type=float, default=12.0, help="look-back window (default 12h)")
    ap.add_argument("--show", type=int, default=15, help="how many LOST rows to print")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--heal", action="store_true", help="re-queue LOST rows (takes a DB backup)")
    ap.add_argument("--no-backup", action="store_true", help="skip the .pre-audit-heal copy")
    ap.add_argument("--max-age-hours", type=float, default=6.0,
                    help="never heal rows older than this (stale loot must not post late)")
    args = ap.parse_args()

    db = Path(args.db).expanduser()
    report = audit(db, args.hours)
    healable = [row for row in report["lost"] if row["age_h"] <= args.max_age_hours]
    too_old = len(report["lost"]) - len(healable)

    if args.json:
        report["healable"] = len(healable)
        report["too_old_to_heal"] = too_old
        print(json.dumps(report, indent=2))
    else:
        print(f"BestGAA coverage audit | db={db} | window={args.hours:g}h | rows={report['rows']}")
        print()
        header = f"{'source':34s} {'seen':>5s} {'posted':>7s} {'live':>5s} {'dedup':>6s} {'policy':>7s} {'LOST':>5s}"
        print(header)
        print("-" * len(header))
        for source, bucket in sorted(report["per_source"].items(),
                                     key=lambda kv: (-kv[1]["lost"], -kv[1]["seen"])):
            print(f"{source[:34]:34s} {bucket['seen']:5d} {bucket['posted']:7d} "
                  f"{bucket['in-flight']:5d} {bucket['dedup']:6d} {bucket['policy']:7d} "
                  f"{bucket['lost']:5d}")
        print()
        if not report["lost"]:
            print("OK - every source post in the window either reached a target or was")
            print("refused by design (dedup / night window / stale / no deal content).")
        else:
            print(f"LOST (never reached any target, recoverable): {len(report['lost'])}")
            for row in report["lost"][:max(0, args.show)]:
                print(f"  queue={row['id']} {row['source'][:22]:22s} msg={row['msg_id']} "
                      f"age={row['age_h']}h attempts={row['attempts']} :: {row['reason']}")
            if len(report["lost"]) > args.show:
                print(f"  ... {len(report['lost']) - args.show} more (--show N to print more)")
            print(f"\nhealable within {args.max_age_hours:g}h: {len(healable)} "
                  f"(too old to post: {too_old})")
            if healable and not args.heal:
                print("run again with --heal to re-queue them; the bot posts them next cycle.")
    if args.heal:
        n = heal(db, [row["id"] for row in healable], backup=not args.no_backup)
        print(f"HEALED {n} job(s) -> status='pending'. The running bot picks them up "
              f"immediately (workers wake on insert); already-posted deals are still "
              f"blocked by dedup, so nothing repeats.")
        return 2 if n else (1 if report["lost"] else 0)
    return 1 if report["lost"] else 0


if __name__ == "__main__":
    sys.exit(main())
