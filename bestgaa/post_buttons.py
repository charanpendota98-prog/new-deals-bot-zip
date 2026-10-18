"""Post buttons: up to two Telegram URL buttons under every deal post.

Big channels end every post with a button row - "Join Shpsy Loots ❤️" - because a
reader who taps the deal and never joins is a reader lost. Telegram draws those
buttons BELOW the message (they are not part of the photo or the caption), and
it allows inline URL buttons on channel posts; WhatsApp channels have no inline
buttons at all, so this is a Telegram-only surface and says so.

The config lives next to channels.json (`post_buttons.json`) because the
dashboard already owns that directory atomically:

    {"version": 1, "buttons": [{"label": "Join Shpsy Loots ❤️",
                                "url": "https://t.me/shpsyloots"}]}

Rules, enforced here and re-enforced by the dashboard endpoint:

* at most two buttons (Telegram's own row limit is much higher, but two join
  buttons is what a reader actually reads; more is spam);
* label 1..40 chars, no control characters;
* URL must be https and must point at a Telegram host (t.me / telegram.me /
  telegram.dog / telegram.org) - a join button that leaves Telegram is a
  phishing button, and our own folder link (t.me/addlist/…) is inside that
  allowlist already;
* two buttons may not carry the same URL.

Empty or unreadable config means NO buttons, which is exactly today's behaviour:
the send path asks `load_buttons()` and passes nothing when the list is empty,
so this feature cannot change a single byte of what goes out until an operator
configures it - and posting itself stays off until
`ops/approve_posting.sh --i-approve-posting`.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
MAX_BUTTONS = 2
MAX_LABEL = 40
ALLOWED_HOSTS = ("t.me", "telegram.me", "telegram.dog", "telegram.org")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


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


def load_buttons(path: Path | None = None) -> list[dict]:
    """The configured buttons, tolerantly: missing/corrupt/oversized means fewer."""
    target = Path(path) if path else config_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    cleaned, _errors = validate_buttons(data.get("buttons"))
    return cleaned


def save_buttons(buttons: list[dict], path: Path | None = None) -> tuple[Path, list[str]]:
    """Atomically write the config. Returns (path, errors); errors means NOT written."""
    cleaned, errors = validate_buttons(buttons)
    if errors:
        return Path(path) if path else config_path(), errors
    target = Path(path) if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": SCHEMA_VERSION, "buttons": cleaned},
                         ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".post-buttons-", suffix=".tmp",
                                    dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target, []
