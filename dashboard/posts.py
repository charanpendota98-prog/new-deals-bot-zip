"""Every post, with the buttons that act on it - and the queue they write to.

The operator's request (2026-10-25): *"every post ki button ravatam leda … oka
channel ki button … channel link kuda"*. In the dashboard a post is a row you can
DO something with, not a line you read:

  * **where it went** (each channel, with its status and its public link),
  * **where it came from** (the source channel + a link to the exact message),
  * **Send to…** - a human decision to put this same post into another of our
    channels, one press.

Why the button writes a FILE instead of the database: the dashboard is
read-only on the bot's database by contract (one writer: the bot), so a press
appends one JSON line that the bot drains, exactly like the repost queue. The bot
re-checks everything (the row still exists, the target is really ours, that
channel does not already carry this post) and then records the outcome in
`manual_send_log.json`.

Nothing here invents a link: a channel link is built only from a real @username
(`https://t.me/<username>`), and a message link only from the source handle plus
the message number. A channel without a public username gets `None`, and the page
shows the title instead of a link that would 404.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

MAX_TARGETS = 12
MAX_PENDING = 60
LOG_ROWS = 200
SNIPPET = 220

HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")
URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")


def clean_handle(value) -> str:
    """`@Name`, `https://t.me/Name`, `t.me/Name/12` -> `Name` (or "")."""
    text = str(value or "").strip()
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/",
                   "http://telegram.me/", "t.me/", "@"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("?")[0].split("#")[0].strip("/")
    text = text.split("/")[0]
    return text if HANDLE_RE.match(text) else ""


def channel_link(name, username=None) -> str | None:
    """The public link of a channel, or None when there is no real username.

    A private channel has no link to give (a `t.me/c/…` link is meaningless
    outside the account), and a link that would 404 is worse than no link.
    """
    handle = clean_handle(username) or clean_handle(name)
    return f"https://t.me/{handle}" if handle else None


def message_link(channel, message_id, username=None) -> str | None:
    """`https://t.me/<handle>/<id>` for a real message number, else None."""
    handle = clean_handle(username) or clean_handle(channel)
    try:
        number = int(message_id)
    except (TypeError, ValueError):
        return None
    if not handle or number <= 0:
        return None
    return f"https://t.me/{handle}/{number}"


def request_path(base: Path | None = None) -> Path:
    return Path(base) if base else _base() / "manual_send_requests.jsonl"


def log_path(base: Path | None = None) -> Path:
    return Path(base) if base else _base() / "manual_send_log.json"


def _base() -> Path:
    """Beside channels.json - resolved by the bot's own resolver, never guessed."""
    try:
        import channels_config  # noqa: PLC0415 - same directory on the server
        return channels_config.pending_path().parent
    except ImportError:  # pragma: no cover
        return Path(__file__).resolve().parent.parent / "bestgaa"


# --------------------------------------------------------------------------- #
#  the feed
# --------------------------------------------------------------------------- #
def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def _title(text: str) -> str:
    """The first meaningful line: what an operator recognises the post by."""
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if len(line) >= 8 and not line.lower().startswith(("http", "join ", "follow ")):
            return line[:120]
    return " ".join((text or "").split())[:120]


def feed(conn: sqlite3.Connection, *, limit: int = 40, ours=None, now=None) -> list[dict]:
    """The newest posts with everything a row's buttons need.

    Read-only. Every claim in a row comes from the database or from a link that
    was built from real fields - a post with no public source has no link, and
    says so, instead of showing a broken one.
    """
    limit = max(1, min(200, int(limit)))
    now = time.time() if now is None else now
    queue_cols = _columns(conn, "queue")
    if not queue_cols:
        return []
    delivery_cols = _columns(conn, "deliveries")
    ours_by_key = {str(name).lower(): str(name) for name in (ours or [])}

    select = ["id", "source", "status", "created_at", "rendered_text", "chat_id", "msg_id"]
    if "last_error" in queue_cols:
        select.append("last_error")
    rows = conn.execute(
        f"SELECT {', '.join(select)} FROM queue ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()

    wanted = [row["id"] for row in rows]
    deliveries: dict[int, list[dict]] = {}
    if wanted and delivery_cols:
        placeholders = ",".join("?" for _ in wanted)
        error_col = "COALESCE(last_error,'')" if "last_error" in delivery_cols else "''"
        for delivery in conn.execute(
                f"SELECT queue_id, target, status, {error_col} AS last_error "
                f"FROM deliveries WHERE queue_id IN ({placeholders}) ORDER BY target",
                wanted):
            deliveries.setdefault(int(delivery["queue_id"]), []).append({
                "target": str(delivery["target"]),
                "status": str(delivery["status"]),
                "error": str(delivery["last_error"] or "")[:120],
                "link": channel_link(delivery["target"]),
                "ours": str(delivery["target"]).lower() in ours_by_key,
            })

    out = []
    for row in rows:
        text = str(row["rendered_text"] or "")
        sent = [d for d in deliveries.get(int(row["id"]), []) if d["status"] == "sent"]
        skipped = [d for d in deliveries.get(int(row["id"]), [])
                   if d["status"] in ("skipped", "held")]
        out.append({
            "id": int(row["id"]),
            "source": str(row["source"] or ""),
            "source_link": message_link(row["source"], row["msg_id"]),
            "title": _title(text),
            "snippet": " ".join(text.split())[:SNIPPET],
            "status": str(row["status"] or ""),
            "created_at": row["created_at"],
            "age_s": round(now - float(row["created_at"] or now), 1),
            "urls": len(set(URL_RE.findall(text))),
            "link": (URL_RE.findall(text) or [""])[0],
            "sent": [d["target"] for d in sent],
            "sent_detail": sent,
            "not_sent": [d["target"] for d in skipped],
            "targets": deliveries.get(int(row["id"]), []),
            "error": str(row["last_error"] or "")[:160] if "last_error" in queue_cols else "",
            # The button needs to know it can act at all: a post with no stored
            # text cannot be re-sent without re-rendering, and that is a job.
            "resendable": bool(text.strip()),
        })
    return out


# --------------------------------------------------------------------------- #
#  "Send to…": validate, queue, log
# --------------------------------------------------------------------------- #
def validate_request(row: dict, *, job_ids, ours) -> tuple[dict, list]:
    """One request, judged WITHOUT writing anything."""
    errors: list[str] = []
    if not isinstance(row, dict):
        return {}, ["request must be a JSON object"]
    try:
        queue_id = int(row.get("queue_id"))
    except (TypeError, ValueError):
        return {}, ["queue_id must be a number"]
    known_jobs = {int(value) for value in (job_ids or [])}
    if known_jobs and queue_id not in known_jobs:
        errors.append(f"post {queue_id} is not in this bot's queue")
    ours_keys = {clean_handle(name).lower() for name in (ours or []) if clean_handle(name)}
    raw = row.get("targets")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return {}, ["pick at least one channel"]
    targets: list[str] = []
    for value in raw:
        key = clean_handle(value).lower()
        if not key:
            errors.append(f"{str(value)[:40]!r} is not a channel name")
            continue
        if ours_keys and key not in ours_keys:
            errors.append(f"{key} is not one of our channels")
            continue
        if key not in targets:
            targets.append(key)
    if len(targets) > MAX_TARGETS:
        errors.append(f"at most {MAX_TARGETS} channels per press")
        targets = targets[:MAX_TARGETS]
    clean = {"id": uuid.uuid4().hex[:12], "queue_id": queue_id, "targets": targets,
             "note": str(row.get("note") or "")[:200], "created_at": time.time()}
    return clean, errors


def submit(row: dict, *, job_ids, ours, path: Path | None = None) -> tuple[dict, list]:
    clean, errors = validate_request(row, job_ids=job_ids, ours=ours)
    if errors:
        return clean, errors
    target = request_path(path)
    if len(pending(target)) >= MAX_PENDING:
        return clean, [f"{MAX_PENDING} sends are already queued - wait for the bot to catch up"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(clean, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return clean, []


def pending(path: Path | None = None, done=None) -> list[dict]:
    """Requests the bot has not finished yet (oldest first)."""
    finished = {str(value) for value in (done or [])}
    rows: list[dict] = []
    try:
        text = request_path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id") and str(row["id"]) not in finished:
            rows.append(row)
    return rows


def read_log(path: Path | None = None) -> list[dict]:
    try:
        rows = json.loads(log_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def record(row: dict, path: Path | None = None) -> dict:
    target = log_path(path)
    rows = (read_log(path) + [row])[-LOG_ROWS:]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass
    return row


def finished_ids(path: Path | None = None) -> set[str]:
    return {str(row.get("id")) for row in read_log(path)
            if row.get("action") in ("sent", "skipped", "failed")}
