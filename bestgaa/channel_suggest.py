"""Suggest the next channel — from one we already run, not from memory.

The operator's request (2026-10-25): *"oka channel nunchi inko channels suggest
cheyali"* — stand on one channel and get told which channels to add next, and
*"more and more advanced"*.

Two honest sources, both read-only:

1. **Telegram's own recommendations** — `channels.GetChannelRecommendations`
   (MTProto) answers "what is similar to this channel" for the session account.
   This is Telegram's opinion, not ours; the row says so.
2. **Mentions in the channel's own recent posts** — a deals channel constantly
   links its siblings (`t.me/OtherDeals`, `@OtherDeals`). Those are channels the
   operator already trusts enough to publish, and they are free to collect.

Nothing here joins, messages or forwards anything. A suggestion is a ROW with a
link; the operator decides, one press, using the same endpoints the rest of the
dashboard already uses (add as source / attach as ours).

The dashboard cannot ask Telegram (only the bot owns the session), so the shape
is the one this repo already uses for repost and manual send:

    dashboard --append--> suggest_requests.jsonl     (one line per press)
    bot       --drain---> suggest_log:  channel_suggestions.json

The bot re-validates every request (is this channel one of OURS? is the limit
sane?) because the request file is on disk and anything can write it.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

MAX_PENDING = 20
MAX_SUGGESTIONS = 25
MAX_SCAN = 60
MAX_AGE_SECONDS = 6 * 3600

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{5,64}$")
MENTION_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,64})(?:/\d+)?", re.I)
AT_RE = re.compile(r"(?<![A-Za-z0-9_/])@([A-Za-z0-9_]{5,64})\b")


def _config():
    try:
        from . import channels_config
    except ImportError:  # pragma: no cover - flat imports on the server
        import channels_config
    return channels_config


def _posts():
    try:
        from . import posts
    except ImportError:  # pragma: no cover - flat imports on the server
        import posts
    return posts


def request_path(base: Path | None = None) -> Path:
    """Where the dashboard appends and the bot drains (beside channels.json)."""
    return Path(base) if base else Path(_config().pending_path().parent
                                        / "suggest_requests.jsonl")


def result_path(base: Path | None = None) -> Path:
    return Path(base) if base else Path(_config().pending_path().parent
                                        / "channel_suggestions.json")


def handle(value) -> str:
    return _posts().clean_handle(value)


def link(value) -> str | None:
    name = handle(value)
    return f"https://t.me/{name}" if name else None


# --------------------------------------------------------------------------- #
#  the dashboard side: one press -> one line
# --------------------------------------------------------------------------- #
def validate_request(row: dict, ours=()) -> tuple[dict, list[str]]:
    """One press, judged against the channels this account owns."""
    if not isinstance(row, dict):
        return {}, ["request must be a JSON object"]
    name = handle(row.get("channel") or row.get("name") or row.get("link"))
    if not name:
        return {}, ["pick a channel first (https://t.me/YourChannel or @username)"]
    own = {handle(item).lower() for item in (ours or ()) if handle(item)}
    if own and name.lower() not in own:
        return {}, [f"@{name} is not one of our channels - suggestions start from a channel "
                    "this account runs"]
    try:
        limit = int(row.get("limit") or 40)
    except (TypeError, ValueError):
        limit = 40
    limit = max(5, min(MAX_SCAN, limit))
    clean = {
        "id": uuid.uuid4().hex[:12],
        "channel": name,
        "limit": limit,
        "created_at": time.time(),
        "note": str(row.get("note") or "")[:200],
    }
    return clean, []


def submit(row: dict, ours=(), *, path: Path | None = None,
           answers: Path | None = None) -> tuple[dict, list[str]]:
    """Validate and append one request. Returns (request, errors).

    `answers` is the RESULT file, not the request file: an answered request is
    finished work, and it must not count against the pending cap forever (the
    first version passed the request path here, so after MAX_PENDING asks in a
    channel's lifetime every new ask was refused).
    """
    clean, errors = validate_request(row, ours)
    if errors:
        return clean, errors
    target = request_path(path)
    queued = pending(target, done=done_ids(answers))
    if len(queued) >= MAX_PENDING:
        return clean, [f"{MAX_PENDING} suggestion requests are already queued - wait for the "
                       "bot to drain them"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle_out:
        handle_out.write(json.dumps(clean, ensure_ascii=False) + "\n")
        handle_out.flush()
        os.fsync(handle_out.fileno())
    return clean, []


def pending(path: Path | None = None, done=()) -> list[dict]:
    """Requests still to do, oldest first. A malformed line is skipped, not fatal."""
    finished = {str(value) for value in (done or ())}
    rows: list[dict] = []
    try:
        text = Path(path or request_path()).read_text(encoding="utf-8", errors="replace")
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
        if not isinstance(row, dict) or not row.get("id") or not row.get("channel"):
            continue
        if str(row["id"]) in finished:
            continue
        rows.append(row)
    return rows


def read_results(path: Path | None = None) -> dict:
    try:
        payload = json.loads(Path(path or result_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": 1, "updated_at": 0, "requests": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("requests"), dict):
        return {"schema": 1, "updated_at": 0, "requests": {}}
    return payload


def done_ids(path: Path | None = None) -> set[str]:
    """Request ids the bot has already answered - never asked twice."""
    return {str(key) for key in (read_results(path).get("requests") or {})}


def record(result: dict, path: Path | None = None) -> dict:
    """Store one answer (bounded, atomic). Keyed by request id."""
    target = Path(path or result_path())
    payload = read_results(path)
    requests = dict(payload.get("requests") or {})
    request_id = str((result or {}).get("id") or "")
    if request_id:
        requests[request_id] = result
    # Old answers are dropped rather than allowed to grow without bound.
    cutoff = time.time() - MAX_AGE_SECONDS
    kept = {key: row for key, row in requests.items()
            if not isinstance(row, dict) or float(row.get("at") or 0) >= cutoff}
    payload = {"schema": 1, "updated_at": time.time(), "requests": kept}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass
    return result


# --------------------------------------------------------------------------- #
#  the bot side: pure parsing and ranking
# --------------------------------------------------------------------------- #
def parse_mentions(text) -> list[str]:
    """Every channel handle a post links to or @mentions (order kept, deduped)."""
    found: list[str] = []
    for match in MENTION_RE.finditer(str(text or "")):
        name = match.group(1)
        if name.lower() not in {item.lower() for item in found}:
            found.append(name)
    for match in AT_RE.finditer(str(text or "")):
        name = match.group(1)
        if name.lower() not in {item.lower() for item in found}:
            found.append(name)
    return found


def _entity_field(entity, *names, default=None):
    for name in names:
        if isinstance(entity, dict):
            if entity.get(name) is not None:
                return entity[name]
        else:
            value = getattr(entity, name, None)
            if value is not None:
                return value
    return default


def from_entities(entities) -> list[dict]:
    """Telegram's recommendation entities -> plain rows (public channels only)."""
    out: list[dict] = []
    for entity in entities or ():
        username = str(_entity_field(entity, "username", default="") or "").strip()
        if not HANDLE_RE.match(username):
            continue        # no public link -> cannot be added by link, so not offered
        if _entity_field(entity, "left", default=False) or _entity_field(entity, "deleted", default=False):
            continue
        if not bool(_entity_field(entity, "broadcast", default=False)):
            continue        # a group is not a source
        try:
            members = int(_entity_field(entity, "participants_count", default=0) or 0)
        except (TypeError, ValueError):
            members = 0
        title = " ".join(str(_entity_field(entity, "title", "name", default="") or "").split())[:120]
        out.append({"username": username, "title": title or username, "members": members})
    return out


def build(channel: str, *, similar=(), mentions=(), exclude=(), limit: int = MAX_SUGGESTIONS,
          now: float | None = None) -> list[dict]:
    """Rank candidates from both sources into one honest list.

    `exclude` is everything we already have (our targets, watched sources,
    attached channels, paused ones) - a suggestion that is already ours is not a
    suggestion. Telegram's picks come first (they know the neighbourhood), then
    the ones the channel itself mentions, most-mentioned first.
    """
    now = time.time() if now is None else now
    skip = {handle(item).lower() for item in (exclude or ()) if handle(item)}
    skip.add(handle(channel).lower())
    rows: dict[str, dict] = {}

    def add(username, *, source, why, title="", members=0):
        channel_key = username.lower()
        if channel_key in skip or channel_key in rows:
            return
        if not HANDLE_RE.match(username):
            return
        rows[channel_key] = {
            "username": username,
            "title": title or username,
            "link": link(username),
            "members": int(members or 0),
            "source": source,
            "why": why,
            "found_at": now,
        }

    for row in similar or ():
        add(str(row.get("username") or ""), source="similar",
            why=f"Telegram recommends it next to @{handle(channel)}",
            title=str(row.get("title") or ""), members=row.get("members") or 0)
    counted: dict[str, int] = {}
    titles: dict[str, str] = {}
    spellings: dict[str, str] = {}
    for value in mentions or ():
        if isinstance(value, dict):
            name, title = str(value.get("username") or ""), str(value.get("title") or "")
        else:
            name, title = str(value or ""), ""
        channel_key = handle(name).lower()
        if not channel_key:
            continue
        counted[channel_key] = counted.get(channel_key, 0) + 1
        # Telegram handles are case-insensitive, but the page should show the
        # channel the way the source wrote it - "OtherDealsHub", not "otherdealshub".
        spellings.setdefault(channel_key, handle(name))
        if title and channel_key not in titles:
            titles[channel_key] = title
    for channel_key in sorted(counted, key=lambda item: (-counted[item], item)):
        times = counted[channel_key]
        add(spellings.get(channel_key) or channel_key, source="mentioned",
            why=f"linked {times}× in the recent posts of @{handle(channel)}",
            title=titles.get(channel_key, ""))
    ordered = sorted(rows.values(), key=lambda row: (row["source"] != "similar",
                                                     -row["members"], row["username"].lower()))
    return ordered[:max(1, int(limit))]


def answer(request: dict, *, similar=(), mentions=(), exclude=(), error="",
           similar_error="", scan_error="", limit: int = MAX_SUGGESTIONS) -> dict:
    """The row the bot stores for one request (what the page renders)."""
    channel = handle(request.get("channel"))
    rows = [] if error else build(channel, similar=similar, mentions=mentions,
                                  exclude=exclude, limit=limit)
    notes = [note for note in (similar_error, scan_error) if note]
    return {
        "id": str(request.get("id") or ""),
        "channel": channel,
        "link": link(channel),
        "at": time.time(),
        "suggestions": rows,
        "count": len(rows),
        "similar_count": sum(1 for row in rows if row["source"] == "similar"),
        "mentioned_count": sum(1 for row in rows if row["source"] == "mentioned"),
        "error": error,
        "notes": notes,
        "next": ("Nothing was joined and nothing was posted. Add one as a source, or attach it "
                 "as ours, with the buttons beside each row."),
    }
