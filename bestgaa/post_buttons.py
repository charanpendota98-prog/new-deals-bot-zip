"""Post buttons: the "Join … ❤️" row under our Telegram posts — now per channel.

Big channels end every post with a button row ("Join Shpsy Loots ❤️ →") because a
reader who taps the deal and never joins is a reader lost. Telegram draws those
buttons BELOW the message (they are not part of the photo or the caption), and it
allows inline URL buttons on channel posts; WhatsApp channels have no inline
buttons at all, so this is a Telegram-only surface and says so.

The operator's request (2026-10-25): *"mana existing and new ga add chesе channels
ki easy ga cheselaga undali dashboard — ah channel post inkoka channel buttons add
cheyelaga"*. One row for every post was not enough: a post in **LootZone** should
offer **the other shelves**, not LootZone itself. So the config is per channel:

    {"version": 2,
     "buttons": [{"label": "Join Shpsy Loots ❤️", "url": "https://t.me/shpsyloots"}],
     "per_channel": {
        "LootZoneIndia11":  {"mode": "auto"},                    # the other channels
        "Under99Deals11":   {"mode": "custom",
                             "buttons": [{"label": "Join LootZone ❤️",
                                          "url": "https://t.me/LootZoneIndia11"}]},
        "ReadOnlyShelf":    {"mode": "none"}},                    # explicitly no row
     "auto_label": "Join {title} ❤️",
     "auto_limit": 2}

`mode` is one of:

    "default"  the global `buttons` row (what every channel had before)
    "auto"     built from OUR OTHER channels (never the channel itself), labelled
               from `auto_label` ({title} = the channel's title) - this is the
               one-press answer to "every channel should point at the others"
    "custom"   an explicit row for this channel
    "none"     no buttons here at all

Rules, enforced here and re-enforced by the dashboard endpoint:

* at most two buttons (Telegram's own row limit is much higher, but two join
  buttons is what a reader actually reads; more is spam);
* label 1..40 chars, no control characters;
* URL must be https and must point at a Telegram host (t.me / telegram.me /
  telegram.dog / telegram.org) - a join button that leaves Telegram is a
  phishing button, and our own folder link (t.me/addlist/…) is inside that
  allowlist already;
* two buttons may not carry the same URL, and a button may never link the
  channel it is posted in (`exclude_self`) - a self-join button is noise and an
  auto row that included it would be a copy of every post's own channel.

Empty or unreadable config means NO buttons, which is exactly the old behaviour:
the send path asks `resolve()` and passes nothing when the list is empty, so this
feature cannot change a single byte of what goes out until an operator configures
it.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path

SCHEMA_VERSION = 2
MAX_BUTTONS = 2
MAX_LABEL = 40
ALLOWED_HOSTS = ("t.me", "telegram.me", "telegram.dog", "telegram.org")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{5,64}$")
HANDLE_SLOT = "{title}"
DEFAULT_AUTO_LABEL = "Join {title} \u2764\ufe0f"
MODES = ("default", "auto", "custom", "none")


def config_path() -> Path:
    """post_buttons.json next to channels.json (same dir, same ownership)."""
    try:
        import channels_config
    except ImportError:  # pragma: no cover - package-style import
        from bestgaa import channels_config
    return channels_config.channels_json_path().parent / "post_buttons.json"


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def handle_of(value) -> str:
    """`@Name`, `https://t.me/Name`, `Name` -> `name` (or ""). Never raises."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text or text.lower().startswith(("t.me/", "telegram.me/")):
        try:
            from urllib.parse import urlsplit
            parts = urlsplit(text if "://" in text else "https://" + text)
            text = parts.path
        except ValueError:
            return ""
        text = text.strip("/").split("/")[0]
    return text.lstrip("@").strip().lower()


def is_public_handle(name: str) -> bool:
    return bool(HANDLE_RE.match(str(name or "")))


def _url_handle(url: str) -> str:
    return handle_of(url)


def validate_buttons(raw) -> tuple[list[dict], list[str]]:
    """Clean a submitted button list; every problem is an error, never a guess."""
    errors: list[str] = []
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["buttons must be a list"]
    if len(raw) > MAX_BUTTONS:
        errors.append(f"at most {MAX_BUTTONS} buttons (a reader reads two, not six)")
        raw = raw[:MAX_BUTTONS]
    seen: set[str] = set()
    cleaned: list[dict] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            errors.append(f"button {index} must be an object with label and url")
            continue
        label = str(entry.get("label", "") or "").strip()
        url = str(entry.get("url", "") or "").strip()
        if not label:
            errors.append(f"button {index} needs a label")
        elif len(label) > MAX_LABEL:
            errors.append(f"button {index} label is {len(label)} chars; Telegram readers "
                          f"see at most {MAX_LABEL}")
        elif CONTROL_RE.search(label):
            errors.append(f"button {index} label contains control characters")
        if not url:
            errors.append(f"button {index} needs a url")
            continue
        if not url.lower().startswith("https://"):
            errors.append(f"button {index} url must be https (a join button over plain "
                          f"http is a phishing button)")
            continue
        host = _host_of(url)
        if host not in ALLOWED_HOSTS:
            errors.append(f"button {index} url host {host or '?'} is not a Telegram host; "
                          f"join buttons must stay on Telegram "
                          f"({', '.join(ALLOWED_HOSTS)})")
            continue
        if url.lower() in seen:
            errors.append(f"button {index} repeats an earlier url")
            continue
        seen.add(url.lower())
        cleaned.append({"label": label, "url": url})
    return cleaned, errors


FOLDER_RE = re.compile(r"OUR_FOLDER_LINK\s*=\s*\"([^\"]+)\"")


def preset_buttons() -> list[dict]:
    """One-click candidates for the button row: our own channels + the folder.

    Read, not typed: the public handles come from channels.json (enabled
    channels only) and the folder link from the bot's own constant, so a preset
    can never point at a channel we do not own. Invite-link channels are skipped
    - a join button wants a public t.me/<handle> URL.
    """
    try:
        import channels_config
    except ImportError:  # pragma: no cover - package-style import
        from bestgaa import channels_config
    out: list[dict] = []
    seen: set[str] = set()
    for entry in (channels_config.load_channels().get("channels") or []):
        name = str(entry.get("name") or "").strip()
        if not entry.get("enabled", True) or not channels_config.HANDLE_RE.match(name):
            continue
        url = f"https://t.me/{name}"
        if url.lower() in seen:
            continue
        seen.add(url.lower())
        label = f"Join {name} \u2764\ufe0f"
        out.append({"label": label[:MAX_LABEL], "url": url})
    folder = ""
    with contextlib.suppress(OSError):
        match = FOLDER_RE.search(
            (Path(__file__).resolve().parent / "main_bot_new.py").read_text(encoding="utf-8"))
        if match:
            folder = match.group(1)
    if folder and folder.lower() not in seen:
        out.append({"label": "All Loot Channels \U0001f4c2", "url": folder})
    return out


# --------------------------------------------------------------------------- #
#  the config: one global row + a mode per channel
# --------------------------------------------------------------------------- #
def empty_config() -> dict:
    return {"version": SCHEMA_VERSION, "buttons": [], "per_channel": {},
            "auto_label": DEFAULT_AUTO_LABEL, "auto_limit": MAX_BUTTONS}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _channel_entry(name, entry) -> dict | None:
    """One per-channel row, read tolerantly (a bad row is dropped, not fatal)."""
    handle = handle_of(name)
    if not is_public_handle(handle):
        return None
    if not isinstance(entry, dict):
        entry = {}
    mode = str(entry.get("mode") or "default").strip().lower()
    if mode not in MODES:
        mode = "default"
    buttons, _errors = validate_buttons(entry.get("buttons"))
    return {"name": handle, "mode": mode,
            "buttons": _without_self(buttons, handle) if mode == "custom" else []}


def load_config(path: Path | None = None) -> dict:
    """The whole config, tolerantly: missing/corrupt/oversized means fewer rows."""
    raw = _read_json(Path(path) if path else config_path())
    buttons, _errors = validate_buttons(raw.get("buttons"))
    per_channel: dict[str, dict] = {}
    for name, entry in (raw.get("per_channel") or {}).items() if isinstance(
            raw.get("per_channel"), dict) else []:
        cleaned = _channel_entry(name, entry)
        if cleaned:
            per_channel[cleaned["name"]] = cleaned
    auto_label = str(raw.get("auto_label") or DEFAULT_AUTO_LABEL).strip()[:MAX_LABEL]
    try:
        auto_limit = int(raw.get("auto_limit") or MAX_BUTTONS)
    except (TypeError, ValueError):
        auto_limit = MAX_BUTTONS
    return {"version": SCHEMA_VERSION, "buttons": buttons, "per_channel": per_channel,
            "auto_label": auto_label or DEFAULT_AUTO_LABEL,
            "auto_limit": max(1, min(MAX_BUTTONS, auto_limit))}


def validate_config(data: dict) -> tuple[dict, list[str]]:
    """Validate a whole submitted config. Any error means NOTHING is written."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return empty_config(), ["config must be a JSON object"]
    buttons, button_errors = validate_buttons(data.get("buttons"))
    errors.extend(button_errors)
    auto_label = str(data.get("auto_label") or DEFAULT_AUTO_LABEL).strip()
    if not auto_label:
        errors.append("the auto label cannot be empty")
    elif len(auto_label) > MAX_LABEL:
        errors.append(f"the auto label is {len(auto_label)} chars; Telegram readers see at "
                      f"most {MAX_LABEL}")
    elif CONTROL_RE.search(auto_label):
        errors.append("the auto label contains control characters")
    try:
        auto_limit = int(data.get("auto_limit") or MAX_BUTTONS)
    except (TypeError, ValueError):
        auto_limit = MAX_BUTTONS
        errors.append("auto_limit must be a number")
    auto_limit = max(1, min(MAX_BUTTONS, auto_limit))
    raw_per = data.get("per_channel") or {}
    if not isinstance(raw_per, dict):
        return empty_config(), errors + ["per_channel must be an object of channel -> entry"]
    per_channel: dict[str, dict] = {}
    for name, entry in raw_per.items():
        handle = handle_of(name)
        if not is_public_handle(handle):
            errors.append(f"{str(name)[:40]!r} is not a public channel handle")
            continue
        if not isinstance(entry, dict):
            errors.append(f"@{handle}: the entry must be an object with a mode")
            continue
        mode = str(entry.get("mode") or "default").strip().lower()
        if mode not in MODES:
            errors.append(f"@{handle}: mode must be one of {', '.join(MODES)}")
            continue
        rows, row_errors = validate_buttons(entry.get("buttons") if mode == "custom" else [])
        errors.extend(f"@{handle}: {err}" for err in row_errors)
        if mode == "custom" and not rows and not row_errors:
            errors.append(f"@{handle}: a custom row needs at least one button "
                          "(use mode 'none' for no buttons)")
        self_links = [row["url"] for row in rows if _url_handle(row["url"]) == handle]
        if self_links:
            errors.append(f"@{handle}: a button cannot link the channel it is posted in "
                          f"({self_links[0]})")
        per_channel[handle] = {"name": handle, "mode": mode,
                               "buttons": rows if mode == "custom" else []}
    return ({"version": SCHEMA_VERSION, "buttons": buttons, "per_channel": per_channel,
             "auto_label": auto_label or DEFAULT_AUTO_LABEL, "auto_limit": auto_limit},
            errors)


def save_config(data: dict, path: Path | None = None) -> tuple[Path, list[str], dict]:
    """Atomically write the whole config. Returns (path, errors, cleaned)."""
    cleaned, errors = validate_config(data)
    target = Path(path) if path else config_path()
    if errors:
        return target, errors, cleaned
    payload = json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".post-buttons-", suffix=".tmp",
                                    dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle_out:
            handle_out.write(payload)
            handle_out.flush()
            os.fsync(handle_out.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target, [], cleaned


# --------------------------------------------------------------------------- #
#  what actually rides on a post
# --------------------------------------------------------------------------- #
def _without_self(rows, target: str) -> list[dict]:
    key = handle_of(target)
    if not key:
        return list(rows or [])
    return [row for row in (rows or []) if _url_handle(row.get("url", "")) != key]


def _auto_buttons(target: str, config: dict, ours, titles) -> list[dict]:
    """One button per OTHER channel we run, labelled from its title."""
    key = handle_of(target)
    wanted = titles or {}
    label_template = config.get("auto_label") or DEFAULT_AUTO_LABEL
    limit = max(1, min(MAX_BUTTONS, int(config.get("auto_limit") or MAX_BUTTONS)))
    rows: list[dict] = []
    seen: set[str] = set()
    for value in ours or ():
        name = handle_of(value)
        if not is_public_handle(name) or name == key or name in seen:
            continue
        seen.add(name)
        title = " ".join(str(wanted.get(name) or name).split())[:MAX_LABEL]
        label = label_template.replace(HANDLE_SLOT, title).strip()[:MAX_LABEL]
        if not label or CONTROL_RE.search(label):
            continue
        rows.append({"label": label, "url": f"https://t.me/{name}"})
        if len(rows) >= limit:
            break
    cleaned, _errors = validate_buttons(rows)
    return _without_self(cleaned, target)


def resolve(target, *, ours=(), titles=None, path: Path | None = None) -> list[dict]:
    """The button row for ONE channel's post. Never raises, never links itself."""
    config = load_config(path)
    key = handle_of(target)
    entry = config["per_channel"].get(key) if key else None
    mode = entry["mode"] if entry else "default"
    if mode == "none":
        return []
    if mode == "custom":
        return _without_self(entry.get("buttons") or [], key)
    if mode == "auto":
        return _auto_buttons(key, config, ours, titles)
    return _without_self(config["buttons"], key)


def effective_row(target, *, ours=(), titles=None, path: Path | None = None) -> dict:
    """What the page shows for one channel: mode, where it comes from, the row."""
    config = load_config(path)
    key = handle_of(target)
    entry = config["per_channel"].get(key) if key else None
    mode = entry["mode"] if entry else "default"
    rows = resolve(target, ours=ours, titles=titles, path=path)
    where = {"none": "no buttons in this channel", "custom": "this channel's own row",
             "auto": "built from your other channels",
             "default": "the default row" if config["buttons"] else "no default row"}[mode]
    return {"name": handle_of(target), "mode": mode, "buttons": rows, "where": where,
            "configured": bool(entry), "would_be_empty": not rows}


def our_channels(path: Path | None = None) -> list[dict]:
    """Our channels with readable titles: what an AUTO row can point at.

    channels.json (enabled dashboard channels) first, then the bot's published
    directory filtered to the channels this account administers - the same two
    sources `channels_attach.inventory` merges, reduced to what a join button
    needs (a public handle and a title).
    """
    try:
        import channels_config
    except ImportError:  # pragma: no cover - package-style import
        from bestgaa import channels_config
    rows: dict[str, dict] = {}
    for entry in (channels_config.load_channels().get("channels") or []):
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        handle = handle_of(entry.get("name"))
        if is_public_handle(handle):
            rows.setdefault(handle, {"name": handle, "title": handle,
                                     "url": f"https://t.me/{handle}", "from": "dashboard"})
    with contextlib.suppress(OSError, ValueError):
        payload = json.loads(channels_config.directory_path().read_text(encoding="utf-8"))
        for entry in (payload.get("channels") or []) if isinstance(payload, dict) else []:
            if not isinstance(entry, dict) or not (entry.get("admin") or entry.get("creator")):
                continue
            handle = handle_of(entry.get("username"))
            if not is_public_handle(handle):
                continue
            title = " ".join(str(entry.get("title") or handle).split())[:MAX_LABEL]
            rows.setdefault(handle, {"name": handle, "title": title or handle,
                                     "url": f"https://t.me/{handle}", "from": "directory"})
    return sorted(rows.values(), key=lambda row: row["name"])


def title_map(rows=None) -> dict[str, str]:
    return {row["name"]: row["title"] for row in (our_channels() if rows is None else rows)}


# --------------------------------------------------------------------------- #
#  back-compat: the old global-row API (still used by the CLI and older pages)
# --------------------------------------------------------------------------- #
def load_buttons(path: Path | None = None) -> list[dict]:
    return load_config(path)["buttons"]


def save_buttons(buttons: list[dict], path: Path | None = None) -> tuple[Path, list[str]]:
    """Replace ONLY the default row; per-channel modes are preserved."""
    config = load_config(path)
    config["buttons"] = buttons or []
    target, errors, _cleaned = save_config(config, path)
    return target, errors
