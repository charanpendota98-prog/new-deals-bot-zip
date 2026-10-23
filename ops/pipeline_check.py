#!/usr/bin/env python3
"""Pipeline check: did every post we took in come out somewhere?

The dashboard answers this on screen. This answers it from cron or a systemd
timer, so a post that left the queue without a delivery row and without a reason
pages somebody instead of waiting to be noticed.

    python3 ops/pipeline_check.py                      # last 24h, human output
    python3 ops/pipeline_check.py --hours 6            # tighter window
    python3 ops/pipeline_check.py --strict             # any warn fails too
    python3 ops/pipeline_check.py --json               # machine-readable
    python3 ops/pipeline_check.py --db /path/bestgaa.sqlite3

Exit codes (same convention as the other ops tools):

    0  every intake is accounted for (and, with --strict, no warnings either)
    1  at least one post is UNACCOUNTED (or a warning with --strict)
    2  the check itself could not run (missing/unreadable database)

What "accounted for" means, exactly: each queue row in the window is POSTED (at
least one real send), WITHHELD (a delivery row recorded a refusal), DROPPED (the
queue closed it with a reason - the night window, a stale job, a duplicate at
intake), WAITING (still live) or FAILED (with a reason). A terminal row with no
send, no delivery row and no stored reason falls outside all five: that is the
only state reported as a lost post, and it is listed by queue id.

Read-only: opens the database with mode=ro, writes nothing, touches no service,
never posts. Uses the same engine as GET /api/pipeline, so the CLI and the screen
can never disagree.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT / "dashboard"))

try:
    from dashboard import pipeline  # noqa: E402  (package import)
except ImportError:  # pragma: no cover - script-style import
    import pipeline  # type: ignore # noqa: E402


# Where the bot is actually installed on the server. The checkout and the app
# directory are two different places, which is exactly how this tool came to say
# "cannot read /home/ubuntu/bestgaa-latest/bestgaa/bestgaa.sqlite3" while the bot
# was writing to /home/ubuntu/bestgaa-bot/bestgaa-bot/bestgaa.sqlite3.
APP_DIR_DEFAULT = Path("/home/ubuntu/bestgaa-bot/bestgaa-bot")


def bot_app_dir() -> Path:
    """The running bot's directory: BESTGAA_DIR, else the deployed install."""
    explicit = os.getenv("BESTGAA_DIR", "").strip()
    if explicit:
        return Path(explicit)
    if (APP_DIR_DEFAULT / "bestgaa.sqlite3").exists() or (APP_DIR_DEFAULT / ".env").exists():
        return APP_DIR_DEFAULT
    return ROOT


def load_env_file(path: Path) -> None:
    """Read KEY=value lines (comments ignored) without overriding the real env."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def db_candidates() -> list[Path]:
    app = bot_app_dir()
    found: list[Path] = []
    env_value = os.getenv("BOT_DB_PATH", "").strip()
    if env_value:
        found.append(Path(env_value))
    load_env_file(app / ".env")
    env_value = os.getenv("BOT_DB_PATH", "").strip()
    if env_value and Path(env_value) not in found:
        found.append(Path(env_value))
    for candidate in (app / "bestgaa.sqlite3", ROOT / "bestgaa" / "bestgaa.sqlite3"):
        if candidate not in found:
            found.append(candidate)
    return found


def default_db() -> Path:
    """The bot's real database - the same one the dashboard and the bot use."""
    for candidate in db_candidates():
        if candidate.exists():
            return candidate
    return db_candidates()[0]


def declared() -> tuple[set[str], list[str], list[str]]:
    """(sources, targets, watched) exactly as the dashboard defines them.

    Read from the bot's own snapshot + channels.json, using the same function the
    HTTP endpoint calls, so the cron output and the screen cannot disagree. A
    missing snapshot (fresh install, bot never started) is simply "nothing
    declared yet", never an error.
    """
    try:
        from dashboard import app
        import channels_config
    except Exception:  # noqa: BLE001 - the accounting itself must still run
        return set(), [], []
    try:
        snap = app.snapshot()
        data = channels_config.load_channels()
    except Exception:  # noqa: BLE001
        return set(), [], []
    try:
        sources = app.declared_sources(snap, data)
    except Exception:  # noqa: BLE001
        sources = set()
    targets = [str(t) for t in (snap.get("targets") or [])]
    watched = [str(row.get("channel", "")) for row in channels_config.watch_entries(data)]
    return sources, targets, watched


def read_only(path: Path) -> sqlite3.Connection:
    """mode=ro: this tool can never change the ledger it is judging."""
    if not path.exists():
        raise FileNotFoundError(path)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def render(payload: dict, *, strict: bool) -> tuple[str, int]:
    """(text, exit code) - a human sentence first, the details after it."""
    lines = [f"PIPELINE | {payload['verdict']}",
             f"  window: last {payload['window_hours']:g}h from {payload['since_ist']}"]
    latency = payload.get("first_send_seconds") or {}
    if latency.get("p50") is not None:
        lines.append(f"  first send: median {latency['p50']:.0f}s, "
                     f"p95 {(latency.get('p95') or 0):.0f}s, "
                     f"slowest {(latency.get('max') or 0):.0f}s")
    for finding in payload.get("findings", []):
        lines.append(f"  [{finding['state'].upper()}] {finding['title']}: {finding['detail']}")
        if finding.get("fix"):
            lines.append(f"      fix: {finding['fix']}")
    ids = payload.get("unaccounted_ids") or []
    if ids:
        lines.append("  unaccounted queue ids: " + ", ".join(str(i) for i in ids[:40]))
    bad = [finding for finding in payload.get("findings", []) if finding["state"] == "fail"]
    warns = [finding for finding in payload.get("findings", []) if finding["state"] == "warn"]
    if bad or (strict and warns):
        code = 1
    else:
        code = 0
    if code == 0:
        tail = "VERDICT: clean - every intake is accounted for"
        if warns:
            tail += f" ({len(warns)} warning(s); add --strict to fail on those)"
        lines.append(tail)
    else:
        lines.append("VERDICT: a post is unaccounted for" if bad
                     else "VERDICT: warnings under --strict")
    return "\n".join(lines), code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Did every post we took in come out somewhere? (read-only)")
    parser.add_argument("--db", default="", help="bot SQLite path (default: the dashboard's BOT_DB_PATH)")
    parser.add_argument("--hours", type=float, default=24.0, help="window in hours (default 24)")
    parser.add_argument("--strict", action="store_true", help="also exit 1 on warnings")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--no-config", action="store_true",
                        help="judge the database only (do not read channels.json/the snapshot)")
    args = parser.parse_args(argv)

    path = Path(args.db) if args.db else default_db()
    try:
        conn = read_only(path)
    except (FileNotFoundError, sqlite3.Error) as exc:
        if args.json:
            print(json.dumps({"error": f"database not readable: {exc}", "db": str(path),
                              "verdict": "the check could not run"}, indent=1))
        else:
            tried = "\n  ".join(str(candidate) for candidate in db_candidates())
            print(f"ERROR: cannot read {path} ({exc}).\n"
                  f"Searched, in order:\n  {tried}\n"
                  "Set BOT_DB_PATH in the bot's .env (or pass --db) and re-run; "
                  "this tool never writes.", file=sys.stderr)
        return 2
    sources, targets, watched = (set(), [], []) if args.no_config else declared()
    try:
        payload = pipeline.funnel(conn, hours=args.hours, declared_sources=sources,
                                  declared_targets=targets, watched=watched)
        payload["unaccounted_ids"] = pipeline.unaccounted_ids(conn, hours=args.hours)
    except sqlite3.DatabaseError as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "db": str(path)}, indent=1))
        else:
            print(f"ERROR: {path} is not a readable bot database ({exc})", file=sys.stderr)
        return 2
    finally:
        conn.close()

    payload["db"] = str(path)
    text, code = render(payload, strict=args.strict)
    if args.json:
        payload["exit_code"] = code
        print(json.dumps(payload, indent=1))
    else:
        print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
