#!/usr/bin/env python3
"""Category watchdog: say it once, when it happens, and say when it is fixed.

`ops/category_report.py` answers "what did each shelf receive?". This is the part
that answers it *without anybody asking*: run it from cron or a systemd timer and
it prints only what is NEW.

    python3 ops/category_watch.py                 # one pass: new alerts, exit 1 if any
    python3 ops/category_watch.py --quiet          # exit code only (cron/systemd)
    python3 ops/category_watch.py --min-severity error   # page on breakage only
    python3 ops/category_watch.py --hours 6 --days 3     # a tighter window
    python3 ops/category_watch.py --json           # machine-readable
    python3 ops/category_watch.py --resolve        # forget the state after a fix

Why the exit code is 1 only for NEW alerts: a shelf that has been silent for a
week would otherwise fail every single run, and an alarm that always rings is an
alarm nobody reads. Repeat conditions stay in the state file with a `seen` count
and are printed again only with --repeat-after.

    0  nothing new (or a condition resolved)
    1  at least one NEW alert at or above --min-severity
    2  the report itself could not be built (that is also an alert)

What it watches: every error and warn finding from the category report - rules
parity and version drift, routing disabled, a target collision, an unreachable
target, a shelf that is silent while Main posts, a shelf skipping more than it
sends, a majority of its deals withheld, posts whose links pay nobody, posts
carrying a stranger's Amazon tag, a WhatsApp shelf that collides or is missing,
and an unreadable ledger. `unclassified_withheld` is deliberately quiet: the
classifier abstaining on a mixed post is the documented behaviour, not an outage.

State: one small JSON file (default `<db directory>/.category_watch.json`, or
CATEGORY_WATCH_STATE, or --state). That is the only thing this script writes. It
opens the bot database read-only through the report engine, never touches
channels.json or the rules, never restarts a service and never posts anything.
The state write is atomic (temp file + rename), and an unreadable state file is
treated as empty rather than fatal: a watchdog that crashes silently is worse
than one that repeats itself.

Install (the operator's call - nothing here deploys itself):

    # cron: check every 15 minutes, mail only when something new is wrong
    */15 * * * * cd /home/ubuntu/bestgaa-bot/bestgaa-bot && \\
        python3 ops/category_watch.py --quiet || true

    # systemd: a timer whose unit fails on a NEW alert, so OnFailure can notify
    #   ops/category_watch.service  -> ExecStart=... ops/category_watch.py --quiet
    #                                  OnFailure=notify-admins@%H.service
    #   ops/category_watch.timer    -> OnCalendar=*:0/15, Persistent=true
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# A finding that is documentation, not an outage: the classifier abstaining on a
# mixed or unclear post is the designed behaviour, and paging on it would train
# everybody to ignore the watchdog.
QUIET_KINDS = frozenset({"unclassified_withheld"})

SEVERITIES = ("error", "warn", "info")


def load_engine():
    """The report engine, loaded by path so this runs from any bundle layout."""
    spec = importlib.util.spec_from_file_location("watch_category_report",
                                                  HERE / "category_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_state(db: Path) -> Path:
    override = os.environ.get("CATEGORY_WATCH_STATE", "").strip()
    if override:
        return Path(override)
    return Path(db).resolve().parent / ".category_watch.json"


def read_state(path: Path) -> dict:
    """The remembered alerts. Unreadable or corrupt means "start fresh"."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"alerts": {}, "note": ""}
    if not isinstance(data, dict) or not isinstance(data.get("alerts"), dict):
        return {"alerts": {}, "note": f"{path} did not hold an alert list; starting fresh"}
    return data


def write_state(path: Path, state: dict) -> str:
    """Atomic: a crash mid-write must not leave a state file nobody can read."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        return f"cannot write the watch state {path}: {exc}"
    return ""


def alert_id(finding: dict) -> str:
    return f"{finding.get('kind', '?')}|{finding.get('category', '')}"


def ist_text(module, stamp: float) -> str:
    return module.ist_text(stamp) if stamp else "never"


def evaluate(report: dict, min_severity: str, repeat_after: float, now: float,
             state: dict) -> dict:
    """Diff the current findings against what was already reported.

    Two lists, on purpose: `tracked` is every finding this watchdog remembers
    (whatever the severity floor) and `wanted` is what this run may page about. A
    cron that watches errors only must not erase the memory of the warnings
    another cron is watching, and a condition can only be RESOLVED when it is
    gone from the report, not when it falls below this run's floor.
    """
    floor = SEVERITIES.index(min_severity) if min_severity in SEVERITIES else 1
    in_scope = SEVERITIES[:floor + 1]
    tracked = [finding for finding in report.get("findings", [])
               if finding.get("kind") not in QUIET_KINDS]
    wanted = [finding for finding in tracked if finding.get("severity") in in_scope]

    alerts = state.get("alerts", {})
    fresh, repeats, resolved = [], [], []
    for finding in wanted:
        key = alert_id(finding)
        known = alerts.get(key)
        entry = {
            "kind": finding.get("kind", "?"),
            "severity": finding.get("severity", "?"),
            "category": finding.get("category", ""),
            "detail": finding.get("detail", ""),
            "first_seen": (known or {}).get("first_seen", now),
            "last_seen": now,
            "seen": int((known or {}).get("seen", 0)) + 1,
            "notified_at": (known or {}).get("notified_at", 0.0),
        }
        if known is None:
            entry["notified_at"] = now
            fresh.append(dict(entry, id=key))
        elif repeat_after and now - float(known.get("notified_at") or 0.0) >= repeat_after:
            entry["notified_at"] = now
            repeats.append(dict(entry, id=key))
        else:
            repeats.append(dict(entry, id=key, suppressed=True))
        alerts[key] = entry
    for finding in tracked:                     # remember, even below the floor
        key = alert_id(finding)
        known = alerts.get(key) or {}
        alerts.setdefault(key, {
            "kind": finding.get("kind", "?"), "severity": finding.get("severity", "?"),
            "category": finding.get("category", ""), "detail": finding.get("detail", ""),
            "first_seen": known.get("first_seen", now), "last_seen": now,
            "seen": int(known.get("seen", 0)) + (0 if key in
                                                 {alert_id(f) for f in wanted} else 1),
            "notified_at": known.get("notified_at", 0.0),
        })
    live = {alert_id(f) for f in tracked}
    for key, entry in list(alerts.items()):
        if key not in live:
            resolved.append(dict(entry, id=key))
            del alerts[key]
    state["alerts"] = alerts
    state["last_run"] = now
    state["min_severity"] = min_severity
    return {"fresh": fresh, "repeats": repeats, "resolved": resolved,
            "state": state, "watched": len(wanted), "tracked": len(tracked)}


def render(result: dict, module, quiet: bool) -> str:
    if quiet:
        return ""
    lines = []
    for item in result["fresh"]:
        where = f" [{item['category']}]" if item["category"] else ""
        lines.append("ALERT {severity:5} {kind}{where}: {detail}".format(
            severity=item["severity"].upper(), kind=item["kind"], where=where,
            detail=item["detail"]))
    for item in result["repeats"]:
        if item.get("suppressed"):
            continue
        where = f" [{item['category']}]" if item["category"] else ""
        lines.append("STILL {severity:5} {kind}{where} (seen {seen}x since {first}): "
                     "{detail}".format(
                         severity=item["severity"].upper(), kind=item["kind"], where=where,
                         seen=item["seen"], first=ist_text(module, item["first_seen"]),
                         detail=item["detail"]))
    for item in result["resolved"]:
        where = f" [{item['category']}]" if item["category"] else ""
        lines.append("RESOLVED {kind}{where} (was seen {seen}x, first {first})".format(
            kind=item["kind"], where=where, seen=item["seen"],
            first=ist_text(module, item["first_seen"])))
    suppressed = sum(1 for item in result["repeats"] if item.get("suppressed"))
    if not lines:
        lines.append("WATCH | nothing new: {watched} condition(s) in scope, {tracked} tracked, "
                     "{suppressed} already reported".format(
                         watched=result["watched"], tracked=result.get("tracked", 0),
                         suppressed=suppressed))
    elif suppressed:
        lines.append("WATCH | {suppressed} known condition(s) not repeated".format(
            suppressed=suppressed))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="exit 0 = nothing new, 1 = a NEW alert, 2 = the report could not be built")
    parser.add_argument("--db", type=Path, default=None, help="bot database (read-only)")
    parser.add_argument("--log", type=Path, default=None, help="bot log with CATEGORY lines")
    parser.add_argument("--hours", type=float, default=6.0,
                        help="report window (default 6: a watchdog looks at now, not last week)")
    parser.add_argument("--days", type=int, default=3, help="trend days (default 3)")
    parser.add_argument("--env-file", action="append", default=[],
                        help="a .env to read shelf targets from (repeatable)")
    parser.add_argument("--rules", type=Path, default=None, help="a category_rules.json to use")
    parser.add_argument("--state", type=Path, default=None,
                        help="where the dedupe state lives (default: next to the database)")
    parser.add_argument("--min-severity", choices=("error", "warn", "info"), default="warn",
                        help="page on this severity and worse (default warn)")
    parser.add_argument("--repeat-after", type=float, default=0.0,
                        help="re-report a still-open alert after this many hours (default: never)")
    parser.add_argument("--quiet", action="store_true", help="print nothing; exit code only")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    parser.add_argument("--resolve", action="store_true",
                        help="forget the remembered alerts (after a fix, or to re-arm)")
    args = parser.parse_args(argv)

    try:
        module = load_engine()
    except Exception as exc:                      # noqa: BLE001 - a watchdog must not crash
        print(f"WATCH | cannot load ops/category_report.py: {exc}", file=sys.stderr)
        return 2

    db = Path(args.db) if args.db else module.default_db()
    state_path = Path(args.state) if args.state else default_state(db)
    if args.resolve:
        error = write_state(state_path, {"alerts": {}, "note": "cleared by --resolve",
                                         "last_run": time.time()})
        print(error or f"WATCH | forgot every remembered alert ({state_path})")
        return 1 if error else 0

    state = read_state(state_path)
    try:
        report = module.build_report(db=db,
                                     log=Path(args.log) if args.log else module.default_log(),
                                     hours=args.hours, days=args.days,
                                     rules=module.load_rules(args.rules) if args.rules else None,
                                     env=module.load_env(args.env_file))
    except Exception as exc:                      # noqa: BLE001 - a watchdog must not crash
        print(f"WATCH | the category report could not be built: {exc}", file=sys.stderr)
        return 2

    now = time.time()
    result = evaluate(report, args.min_severity, args.repeat_after * 3600.0, now, state)
    if state.get("note"):
        result["state_note"] = state["note"]
    write_error = write_state(state_path, result["state"])
    if write_error and not args.quiet:
        print(write_error, file=sys.stderr)

    if args.json:
        payload = {
            "generated_at": now, "state": str(state_path), "window_hours": args.hours,
            "min_severity": args.min_severity, "watched": result["watched"],
            "tracked": result.get("tracked", 0),
            "new": [{k: v for k, v in item.items() if k != "id"} for item in result["fresh"]],
            "still_open": [{k: v for k, v in item.items() if k != "id"}
                           for item in result["repeats"] if not item.get("suppressed")],
            "suppressed": sum(1 for item in result["repeats"] if item.get("suppressed")),
            "resolved": [{k: v for k, v in item.items() if k != "id"}
                         for item in result["resolved"]],
            "write_error": write_error,
            "state_note": result.get("state_note", ""),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        text = render(result, module, args.quiet)
        if text:
            print(text)
        if write_error:
            print(write_error, file=sys.stderr)
    return 1 if result["fresh"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
