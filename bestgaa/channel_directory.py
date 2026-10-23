"""The channels this Telegram account can actually read, as a file the dashboard reads.

The operator's request: "ah telegram lo vunna channel anni chupisthe, bestgaa
select cheskoni, multiple gaa add cheskogalaru" - show every channel the bot
session is in, so a source can be picked instead of typed.

This module is the pure half of that: it turns Telethon dialog entities into one
small, stable JSON payload. The loop in `main_bot_new.py` does the network call
(`client.iter_dialogs`), this decides what is safe and useful to publish.

Rules:
  * read-only - listing a dialog is not joining it, and nothing here sends;
  * a channel with no public @username cannot be added by link (the bot can still
    read it, and it may already be a source), so it is published with
    `addable: false` and the dashboard disables the checkbox;
  * broadcast channels are the only thing that may become a SOURCE (the same rule
    the live activation enforces), so groups/supergroups are published with
    `kind: "group"` and `addable: false` - visible, never silently selectable;
  * bounded: at most `limit` rows, each field trimmed, so a 500-channel account
    cannot produce a multi-megabyte snapshot every 15 minutes. The count that is
    dropped is published as `truncated` - a cap must never look like "that is all
    there is" (a fixed 1000 did exactly that on a 1122-dialog account).
"""
from __future__ import annotations

import time

SCHEMA = 1
DEFAULT_LIMIT = 5000
TITLE_MAX = 120
USERNAME_MAX = 64


def _field(obj, *names, default=None):
    """Read the first present attribute/key, so dicts and entities both work."""
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _clean(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def entry(entity, *, now: float | None = None) -> dict | None:
    """One channel as the dashboard needs it, or None when it must be hidden."""
    now = time.time() if now is None else now
    channel_id = _field(entity, "id")
    if not channel_id:
        return None
    if _field(entity, "left", default=False) or _field(entity, "deleted", default=False):
        return None
    broadcast = bool(_field(entity, "broadcast", default=False))
    megagroup = bool(_field(entity, "megagroup", default=False))
    title = _clean(_field(entity, "title", "name", default=""), TITLE_MAX)
    username = _clean(_field(entity, "username", default=""), USERNAME_MAX)
    kind = "channel" if broadcast else ("group" if megagroup else "chat")
    try:
        members = int(_field(entity, "participants_count", default=0) or 0)
    except (TypeError, ValueError):
        members = 0
    stamp = _field(entity, "date", "last_message_at", default=None)
    try:
        last_at = float(stamp.timestamp()) if hasattr(stamp, "timestamp") else float(stamp or 0)
    except (TypeError, ValueError):
        last_at = 0.0
    if not title and not username:
        return None  # nothing an operator could recognise
    return {
        "id": int(channel_id),
        "title": title or username,
        "username": username,
        "kind": kind,
        # A source must be a public broadcast channel: that is what a link can
        # address and what the activation check requires.
        "addable": bool(broadcast and username),
        "private": not bool(username),
        "members": members,
        "last_message_at": last_at,
        "seen_at": now,
    }


def build(entities, *, now: float | None = None, limit: int = DEFAULT_LIMIT) -> dict:
    """The whole payload: deduplicated, sorted, bounded, and honest about time."""
    now = time.time() if now is None else now
    rows: dict[int, dict] = {}
    skipped = 0
    truncated = 0
    cap = max(1, int(limit))
    for entity in entities or ():
        row = entry(entity, now=now)
        if row is None:
            skipped += 1
            continue
        if row["id"] in rows:
            continue
        if len(rows) >= cap:
            # Counted, not silently ignored: the picker says "N more not shown".
            truncated += 1
            continue
        rows[row["id"]] = row
    channels = sorted(rows.values(), key=lambda row: (not row["addable"], row["title"].lower()))
    return {
        "schema": SCHEMA,
        "generated_at": now,
        "count": len(channels),
        "addable": sum(1 for row in channels if row["addable"]),
        "skipped": skipped,
        "truncated": truncated,
        "limit": cap,
        "note": ("Channels this bot session can read. Nothing here was joined or "
                 "messaged; only public broadcast channels can become a source."),
        "channels": channels,
    }
