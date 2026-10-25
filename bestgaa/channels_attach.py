"""Attach the channels that ALREADY run to the dashboard — one list, one press.

The operator's request (2026-10-25): *"mana existing running channel ki kuda ottu
add cheyagalgali neatga dashboard lo"* — attaching a channel that is already
running must be easy, and it must be visible in ONE list.

Before this module the dashboard had three half-answers:

  * **code targets** (`SOURCE_TO_TARGETS`) — running, but they only exist in
    Python: no way to pause one, note it, or see it next to the rest;
  * **dashboard channels** (`channels.json` -> `channels`) — visible, but only
    the ones created *from* the dashboard;
  * **the directory** (`telegram_channels.json`) — every channel the session can
    read, with Telegram's own rights, but read-only.

So an existing running channel could not be *managed*: it could not be paused,
annotated, or offered as ours. `attach` is that missing verb. It records the
intent in `channels.json` (section `attach`) and does exactly ONE of two things:

    mode "existing"  the channel is already a routing target (code or dashboard)
                     -> NO routing change. Attaching only registers it so the
                     dashboard can pause it, note it and suggest sources for it.
    mode "new"       the channel is one where we are admin but the bot does not
                     post -> this module also appends a normal dashboard channel
                     entry (sources from the request, default every main source),
                     validated by `channels_config.validate_channel`. That is what
                     makes the bot actually post there - never a hand-written file.

`detach` undoes exactly that: the registry row goes, and a mode "new" row takes
its dashboard channel with it (a detach can never leave a channel half-managed).
A CODE target keeps posting - detaching cannot edit Python - and the answer says
so instead of pretending.

**Pause** is the other half of "manage it here": `posting: false` on an attached
row (or `enabled: false` on a dashboard channel). The bot's fan-out calls
`paused_targets()` for every post, which re-reads the file when its mtime moves -
so the button works without a restart. A paused channel receives nothing NEW and
no delivery row is written for it; nothing is replayed when it is resumed.

Stdlib only: importing this must never require telethon, aiohttp, or bot env.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SECTION = "attach"
SCHEMA_VERSION = 1
MAX_ATTACHED = 200
NOTE_MAX = 200
MODES = ("existing", "new")


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


def handle(value) -> str:
    """`@Name`, `https://t.me/Name`, `t.me/Name/12` -> `Name` (or "")."""
    return _posts().clean_handle(value)


def key(value) -> str:
    return handle(value).lower()


def link(value) -> str | None:
    name = handle(value)
    return f"https://t.me/{name}" if name else None


def empty() -> dict:
    return {"schema": SCHEMA_VERSION, "rows": []}


def entries(data: dict | None = None) -> list[dict]:
    """Every attached channel, in file order, malformed rows skipped."""
    rows = (data if data is not None else _config().load_channels()).get(SECTION)
    if isinstance(rows, dict):          # tolerate {"rows": [...]} if hand-written
        rows = rows.get("rows")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = handle(row.get("name"))
        if not name:
            continue
        out.append({
            "name": name,
            "mode": str(row.get("mode") or "existing").lower()
            if str(row.get("mode") or "existing").lower() in MODES else "existing",
            "posting": bool(row.get("posting", True)),
            "note": str(row.get("note") or "")[:NOTE_MAX],
            "role": str(row.get("role") or "")[:20],
            "attached_at": float(row.get("attached_at") or 0.0),
        })
    return out


def find(data: dict | None, name) -> tuple[int | None, dict | None]:
    wanted = key(name)
    for index, row in enumerate(entries(data)):
        if row["name"].lower() == wanted:
            return index, row
    return None, None


def store(data: dict, rows: list[dict]) -> None:
    """Write the registry back into the config dict (caller saves it)."""
    data[SECTION] = {"schema": SCHEMA_VERSION, "rows": rows[:MAX_ATTACHED]}


def paused(data: dict | None = None) -> set[str]:
    """Channels that must receive nothing NEW: attached + paused, or disabled.

    A dashboard channel's `enabled: false` means the same thing as a pause, so
    both spellings are answered here - one set for the bot and for the page.
    """
    config = _config()
    if data is None:
        try:
            data = config.load_channels()
        except Exception:  # noqa: BLE001 - a broken file must not stop posting
            return set()
    names = {row["name"].lower() for row in entries(data) if not row["posting"]}
    for row in data.get("channels") or []:
        if isinstance(row, dict) and row.get("enabled") is False:
            channel_key = key(row.get("name"))
            if channel_key:
                names.add(channel_key)
    return names


_PAUSE_CACHE: dict = {"mtime": None, "path": None, "names": frozenset()}


def paused_targets(path: Path | None = None) -> set[str]:
    """`paused()` with an mtime cache, for the bot's per-post fan-out.

    Reading a 5 KB file per post is cheap, but doing it on every post when the
    file has not changed is cheaper - and the mtime check is what makes a
    dashboard pause take effect on the NEXT post without a restart.
    """
    config = _config()
    target = Path(path) if path else config.channels_json_path()
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return set()
    if _PAUSE_CACHE["mtime"] == mtime and _PAUSE_CACHE["path"] == str(target):
        return set(_PAUSE_CACHE["names"])
    try:
        data = config.load_channels(target)
    except Exception:  # noqa: BLE001
        return set()
    names = frozenset(paused(data))
    _PAUSE_CACHE.update({"mtime": mtime, "path": str(target), "names": names})
    return set(names)


def _directory_index(rows) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        for value in (row.get("username"), row.get("title")):
            channel_key = key(value)
            if channel_key:
                index.setdefault(channel_key, row)
    return index


def validate(row: dict, *, data: dict, directory_rows=(), targets=(),
             watch_channels=()) -> tuple[dict, list[str], list[str]]:
    """One attach request, judged against what Telegram reported and what runs.

    Returns (clean, errors, warnings). The mode is DECIDED here, never chosen by
    the caller: a channel the bot already posts to is "existing" (no routing
    change), anything else we administer is "new" (a real dashboard channel).
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(row, dict):
        return {}, ["attach request must be a JSON object"], []
    name = handle(row.get("name") or row.get("channel") or row.get("link"))
    if not name:
        return {}, ["paste the channel link (https://t.me/YourChannel) or @username"], []
    target_keys = {str(t).strip().lstrip("@").lower() for t in (targets or ()) if str(t).strip()}
    dashboard_names = {key(item.get("name")) for item in data.get("channels") or []
                       if isinstance(item, dict)}
    directory = _directory_index(directory_rows)
    seen = directory.get(name.lower())
    is_target = name.lower() in target_keys
    if not seen and not is_target:
        errors.append(
            f"@{name} is not in the bot's channel list - refresh the directory in the "
            "source studio (the bot publishes it every 15 minutes), or add it as a "
            "source first so its access is checked. Nothing was attached.")
    if seen is not None:
        kind = str(seen.get("kind") or "channel").lower()
        if kind != "channel":
            errors.append(f"@{name} is a {kind}, not a broadcast channel - the bot posts "
                          "only to channels")
        if not bool(seen.get("admin") or seen.get("creator")):
            errors.append(f"this account is only a member of @{name} - attaching needs "
                          "creator or admin rights")
        if not bool(seen.get("can_post")) and not is_target:
            errors.append(f"Telegram says this account cannot post in @{name} "
                          "(no post_messages right) - nothing was attached")
        if not bool(seen.get("can_post")) and is_target:
            warnings.append(f"Telegram reports no post_messages right in @{name}; it is a "
                            "target already, so it is attached - check that in Telegram")
    elif is_target:
        warnings.append(f"the bot has not published rights for @{name} yet; it is a target "
                        "already, so it is attached as it is")
    index, existing = find(data, name)
    if existing is not None:
        errors.append(f"@{existing['name']} is already attached")
    # A channel cannot be a SOURCE and a destination at the same time: its own
    # posts would be routed back into it (or worse, into each other). This repo
    # refuses that everywhere else, so attaching refuses it too.
    if name.lower() in {key(item) for item in (watch_channels or ()) if handle(item)}:
        errors.append(f"@{name} is one of your SOURCES - a channel cannot be both a source "
                      "and a destination (that is how a loop starts). Remove it from the "
                      "watch list first.")
    mode = "existing" if (is_target or name.lower() in dashboard_names) else "new"
    posting = row.get("posting")
    cleaned = {
        "name": name,
        "mode": mode,
        "posting": True if posting is None else bool(posting),
        "note": str(row.get("note") or "")[:NOTE_MAX],
        "role": str((seen or {}).get("role") or "")[:20],
        "attached_at": time.time(),
    }
    if mode == "new":
        warnings.append(
            f"@{name} is not a target yet: attaching adds it as a dashboard channel, so the "
            "bot starts posting there once it picks the config up (live, or after a restart).")
    elif name.lower() in dashboard_names:
        warnings.append(f"@{name} is already a dashboard channel - attaching only adds it to "
                        "this list (pause/note/suggest), it changes no routing")
    else:
        warnings.append(f"@{name} is a code target - attaching changes no routing; detaching "
                        "will not stop it posting")
    return cleaned, errors, warnings


def attach(row: dict, *, directory_rows=(), targets=(), path: Path | None = None,
           known_sources=None, known_categories=None, watch_channels=None) -> dict:
    """Validate + save one attachment (and its dashboard channel when it is new)."""
    config = _config()
    data = config.load_channels(path)
    watched = list(watch_channels or ()) or [
        str(item.get("channel")) for item in data.get("watch") or []
        if isinstance(item, dict) and item.get("channel")]
    cleaned, errors, warnings = validate(row, data=data, directory_rows=directory_rows,
                                         targets=targets, watch_channels=watched)
    if errors:
        return {"errors": errors, "warnings": warnings, "attached": None, "channel": None}
    if len(entries(data)) >= MAX_ATTACHED:
        return {"errors": [f"{MAX_ATTACHED} channels are already attached - detach one first"],
                "warnings": warnings, "attached": None, "channel": None}
    created = None
    if cleaned["mode"] == "new":
        raw_sources = row.get("sources")
        if isinstance(raw_sources, str):
            raw_sources = [raw_sources]
        if not raw_sources:
            raw_sources = ["*"]
        known = {str(s) for s in (known_sources or set())} | {
            str(s) for s in (targets or ())}
        channel, channel_errors, channel_warnings = config.validate_channel(
            {"name": cleaned["name"], "sources": raw_sources,
             "categories": row.get("categories") or [],
             "amazon_tag": bool(row.get("amazon_tag", False)),
             "enabled": bool(cleaned["posting"]),
             "note": cleaned["note"] or "attached from the dashboard"},
            known, {str(t) for t in (targets or ())}, known_categories)
        warnings.extend(channel_warnings)
        if channel_errors:
            return {"errors": [f"could not create the dashboard channel: {err}"
                               for err in channel_errors],
                    "warnings": warnings, "attached": None, "channel": None}
        data.setdefault("channels", []).append(channel)
        created = channel
    rows = entries(data) + [cleaned]
    store(data, rows)
    config.save_channels(data, path)
    return {"attached": cleaned, "channel": created, "errors": [], "warnings": warnings,
            "pending_restart": cleaned["mode"] == "new"}


def detach(name, *, path: Path | None = None) -> dict:
    """Remove one attachment (and the dashboard channel it created)."""
    config = _config()
    data = config.load_channels(path)
    index, row = find(data, name)
    if row is None:
        return {"errors": [f"@{handle(name) or name} is not attached"], "removed": None,
                "channel_removed": False, "warnings": []}
    rows = entries(data)
    rows.pop(index)
    store(data, rows)
    warnings: list[str] = []
    removed_channel = False
    if row["mode"] == "new":
        kept = []
        for item in data.get("channels") or []:
            if isinstance(item, dict) and key(item.get("name")) == row["name"].lower():
                removed_channel = True
                continue
            kept.append(item)
        data["channels"] = kept
        if not removed_channel:
            warnings.append("its dashboard channel was already gone - nothing else changed")
    else:
        warnings.append(f"@{row['name']} is still a routing target, so the bot keeps posting "
                        "there; only the dashboard attachment was removed")
    config.save_channels(data, path)
    return {"errors": [], "removed": row, "channel_removed": removed_channel,
            "warnings": warnings, "pending_restart": removed_channel}


def set_posting(name, posting: bool, *, path: Path | None = None) -> dict:
    """Pause or resume one attached channel (or a dashboard channel)."""
    config = _config()
    data = config.load_channels(path)
    index, row = find(data, name)
    warnings: list[str] = []
    if row is None:
        channel_key = key(name)
        touched = False
        for item in data.get("channels") or []:
            if isinstance(item, dict) and key(item.get("name")) == channel_key:
                item["enabled"] = bool(posting)
                touched = True
        if not touched:
            return {"errors": [f"@{handle(name) or name} is not attached - attach it first"],
                    "row": None, "warnings": []}
        config.save_channels(data, path)
        return {"errors": [], "row": {"name": handle(name), "posting": bool(posting),
                                      "mode": "dashboard"}, "warnings": []}
    rows = entries(data)
    rows[index]["posting"] = bool(posting)
    store(data, rows)
    # A dashboard channel mirrors the pause so the bot's routing and the page
    # cannot disagree about whether this channel is receiving.
    for item in data.get("channels") or []:
        if isinstance(item, dict) and key(item.get("name")) == row["name"].lower():
            item["enabled"] = bool(posting)
    if row["mode"] == "existing":
        warnings.append("this channel also runs from code, so the pause is enforced where the "
                        "post is fanned out (no delivery row is written for it)")
    config.save_channels(data, path)
    return {"errors": [], "row": {**rows[index], "posting": bool(posting)},
            "warnings": warnings}


def inventory(*, data: dict | None = None, targets=(), dashboard_names=(),
              directory_rows=(), watch_channels=()) -> dict:
    """ONE list: every channel we run, plus the admin ones we could attach.

    `targets` is what the bot reported it posts to (code + dashboard, merged),
    `dashboard_names` what the dashboard created, `directory_rows` the bot's own
    inventory with Telegram's rights. Nothing here is invented: a channel with no
    source of truth is not in the list.
    """
    config = _config()
    data = config.load_channels() if data is None else data
    attached = {row["name"].lower(): row for row in entries(data)}
    dashboard = {key(name): str(name) for name in (dashboard_names or ())}
    dashboard.update({key(item.get("name")): str(item.get("name"))
                      for item in data.get("channels") or []
                      if isinstance(item, dict) and handle(item.get("name"))})
    directory = _directory_index(directory_rows)
    paused_names = paused(data)
    sources = {key(item) for item in (watch_channels or ()) if handle(item)}
    rows: dict[str, dict] = {}

    def row_for(name, *, from_target=False, from_dashboard=False, from_attach=False):
        channel_key = key(name)
        if not channel_key:
            return None
        current = rows.get(channel_key)
        if current is None:
            seen = directory.get(channel_key) or {}
            current = {
                "name": handle(name),
                "link": link(name),
                "origins": [],
                "attached": False,
                "mode": "",
                "note": "",
                "posting": True,
                "paused": channel_key in paused_names,
                "role": str(seen.get("role") or ""),
                "admin": bool(seen.get("admin") or seen.get("creator")),
                "creator": bool(seen.get("creator")),
                "can_post": bool(seen.get("can_post")),
                "kind": str(seen.get("kind") or ""),
                "members": int(seen.get("members") or 0),
                "last_message_at": float(seen.get("last_message_at") or 0),
                "seen_by_bot": bool(seen),
            }
            rows[channel_key] = current
        if from_target and "code" not in current["origins"]:
            current["origins"].append("code")
        if from_dashboard and "dashboard" not in current["origins"]:
            current["origins"].append("dashboard")
        if from_attach and "attached" not in current["origins"]:
            current["origins"].append("attached")
        return current

    for name in targets or ():
        row_for(name, from_target=True)
    for channel_key, name in dashboard.items():
        # The bot reports one merged target list, so a dashboard channel arrives
        # here looking like a code target. It is not one: code targets cannot be
        # removed from the dashboard, dashboard channels can.
        current = row_for(name, from_dashboard=True)
        if current is not None:
            current["origins"] = [item for item in current["origins"] if item != "code"] \
                or ["dashboard"]
    for channel_key, attached_row in attached.items():
        current = row_for(attached_row["name"], from_attach=True)
        if current is not None:
            current["attached"] = True
            current["mode"] = attached_row["mode"]
            current["note"] = attached_row["note"]
            current["posting"] = bool(attached_row["posting"])
            current["paused"] = not attached_row["posting"] or current["paused"]
    # Candidates: channels we administer, may post in, that are not ours yet.
    for channel_key, seen in directory.items():
        if channel_key in rows:
            continue
        if not (seen.get("admin") or seen.get("creator")):
            continue
        row_for(seen.get("username") or seen.get("title"), from_attach=False)

    out: list[dict] = []
    for current in rows.values():
        ours = bool(current["origins"])
        can_post = current["can_post"]
        is_source = current["name"].lower() in sources
        attachable = bool(current["name"]) and not current["attached"] and not is_source and (
            ours or (current["admin"] and can_post and current["kind"] == "channel"))
        why_not = ""
        if current["attached"]:
            why_not = "already attached"
        elif is_source:
            why_not = "it is one of your sources (a channel cannot be both)"
        elif not ours and not current["admin"]:
            why_not = "you are only a member there"
        elif not ours and not can_post:
            why_not = "Telegram reports no post_messages right"
        elif not ours and current["kind"] and current["kind"] != "channel":
            why_not = f"it is a {current['kind']}, not a broadcast channel"
        elif not current["seen_by_bot"] and not ours:
            why_not = "the bot has not published it yet"
        current.update({
            "ours": ours,
            "attachable": attachable,
            "detachable": current["attached"],
            "why_not": why_not,
            "is_source": is_source,
            "label": (" / ".join(current["origins"]) if current["origins"]
                      else "candidate"),
        })
        out.append(current)
    out.sort(key=lambda row: (not row["ours"], row["paused"], row["name"].lower()))
    return {
        "rows": out,
        "counts": {
            "ours": sum(1 for row in out if row["ours"]),
            "attached": sum(1 for row in out if row["attached"]),
            "paused": sum(1 for row in out if row["paused"]),
            "candidates": sum(1 for row in out if row["attachable"]),
            "code": sum(1 for row in out if "code" in row["origins"]),
            "dashboard": sum(1 for row in out if "dashboard" in row["origins"]),
        },
        "note": ("Attaching registers an EXISTING channel so the dashboard can pause it, note "
                 "it and suggest sources for it. A channel we admin but do not post to is "
                 "added as a real dashboard channel; a code target is left exactly as it is."),
    }


def config_json(data: dict | None = None) -> str:
    """The registry as the page shows it (used by tests and the ops report)."""
    return json.dumps(entries(data), ensure_ascii=False, indent=1)
