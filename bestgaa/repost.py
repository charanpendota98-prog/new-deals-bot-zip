"""Forward a post from ONE of our channels to the others - by a button, not a rule.

The operator's request (2026-10-25): "mana oka channel nunchi inkoka channel ki
easy ga button pettelaga undali dashboard lo". One click: take the post at
`https://t.me/OurShelf/1234` and forward it to Main / another shelf.

Why this is a QUEUE and not a route. Automatic routing of our own channel into
another one is how these systems grow loops (A→B, B→A) and duplicate posts, and
this repo already refuses "a destination cannot be its own source" for exactly
that reason. A human pressing a button is unambiguous: the intent is recorded
with a timestamp, the bot forwards it once, and the result is logged. So:

    dashboard --append 1 line--> repost_requests.jsonl
    bot       --drain-->          forward_messages --append--> repost_log.json

The queue is append-only JSON Lines (a small write on an O_APPEND handle is
atomic), so the dashboard never rewrites a file the bot is reading and the bot
never rewrites the operator's requests. Statuses live in the log, keyed by the
request id.

Safety rules, all enforced on the BOT side as well as in the dashboard because
the file is on disk and anything can write it:

  * the source must be one of OUR channels (the bot re-checks against its own
    target map - a stranger's channel is never forwarded from);
  * every destination must be one of OUR channels, and never the source itself;
  * at most `MAX_TARGETS` destinations per request, `MAX_PENDING` queued;
  * a request id is processed once (the log is the record of what was done).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

MAX_TARGETS = 10
MAX_PENDING = 50
LOG_ROWS = 200

LINK_RE = re.compile(r"^(?:https?://)?(?:t(?:elegram)?\.me)/([A-Za-z0-9_]{4,64})/(\d{1,12})/?$")


def request_path(base: Path | None = None) -> Path:
    """Where the dashboard appends and the bot drains (beside channels.json)."""
    return Path(base) if base else Path(_channels_config().pending_path().parent
                                        / "repost_requests.jsonl")


def log_path(base: Path | None = None) -> Path:
    return Path(base) if base else Path(_channels_config().pending_path().parent
                                        / "repost_log.json")


def _channels_config():
    try:
        from . import channels_config
    except ImportError:  # pragma: no cover - flat imports on the server
        import channels_config
    return channels_config


def name_of(value) -> str:
    text = str(value or "").strip()
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "t.me/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    return text.lstrip("@").split("?")[0].split("/")[0].strip().lower()


def parse_link(link: str) -> tuple[str, int]:
    """`https://t.me/OurShelf/1234` -> ("ourshelf", 1234). Raises ValueError.

    A private-channel link (`t.me/c/…`) is refused on purpose: it needs the
    numeric entity and says nothing about which of OUR channels it is.
    """
    match = LINK_RE.match(str(link or "").strip())
    if not match:
        raise ValueError("paste a post link like https://t.me/YourChannel/1234")
    channel, message_id = match.group(1), int(match.group(2))
    if not message_id:
        raise ValueError("the link has no message number")
    return channel.lower(), message_id


def validate(row: dict, ours) -> tuple[dict, list]:
    """One request, judged against the channels this account owns."""
    errors: list[str] = []
    if not isinstance(row, dict):
        return {}, ["request must be a JSON object"]
    try:
        channel, message_id = parse_link(row.get("link") or row.get("from") or "")
    except ValueError as exc:
        return {}, [str(exc)]
    own = {name_of(name) for name in (ours or [])}
    if own and channel not in own:
        errors.append(f"{channel} is not one of our channels - a repost may only start "
                      f"from a channel this account owns")
    targets: list[str] = []
    raw = row.get("targets")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return {}, ["pick at least one destination channel"]
    if len(raw) > MAX_TARGETS:
        errors.append(f"at most {MAX_TARGETS} destinations per repost")
        raw = raw[:MAX_TARGETS]
    for value in raw:
        key = name_of(value)
        if not key:
            continue
        if own and key not in own:
            errors.append(f"{key} is not one of our channels")
            continue
        if key == channel:
            errors.append("a channel cannot repost into itself")
            continue
        if key not in targets:
            targets.append(key)
    if not targets and not errors:
        errors.append("pick at least one destination channel")
    clean = {"link": str(row.get("link") or "").strip()[:200], "from": channel,
             "message_id": message_id, "targets": targets,
             "note": str(row.get("note") or "")[:200], "created_at": time.time(),
             "id": uuid.uuid4().hex[:12]}
    return clean, errors


def submit(row: dict, ours, *, path: Path | None = None) -> tuple[dict, list]:
    """Validate and append one request. Returns (request, errors)."""
    clean, errors = validate(row, ours)
    if errors:
        return clean, errors
    target = request_path(path)
    pending = len(queued(target, [row.get("id") for row in read_log(target) if
                                  row.get("action") == "sent"]))
    if pending >= MAX_PENDING:
        return clean, [f"{MAX_PENDING} reposts are already queued - wait for the bot to drain them"]
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(clean, ensure_ascii=False) + "\n"
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return clean, []


def queued(path: Path, done_ids=None) -> list[dict]:
    """Requests still to do, oldest first. A malformed line is skipped, not fatal."""
    done = {str(value) for value in (done_ids or [])}
    rows: list[dict] = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
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
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if str(row["id"]) in done:
            continue
        rows.append(row)
    return rows


def read_log(path: Path | None = None) -> list[dict]:
    try:
        rows = json.loads(log_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def record(row: dict, path: Path | None = None) -> dict:
    """Append one outcome to the durable log (bounded, atomic)."""
    target = log_path(path)
    rows = read_log(path) + [row]
    rows = rows[-LOG_ROWS:]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass
    return row


def done_ids(path: Path | None = None) -> set[str]:
    """Requests that already reached a final state - never forwarded twice."""
    return {str(row.get("id")) for row in read_log(path)
            if row.get("action") in ("sent", "failed")}
