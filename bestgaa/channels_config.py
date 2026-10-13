"""Dashboard-managed Telegram channels (operator-added targets).

The bot's built-in routing (SOURCE_TO_TARGETS and friends in main_bot_new.py)
is code. This module is the dashboard's side of it: extra posting channels the
operator adds from the web UI, stored as plain JSON next to the bot database
(so a code redeploy never wipes them), merged over the built-in maps at bot
startup.

Additive ONLY: dashboard channels can add new targets and route existing (or
new) sources to them. They can never remove or rename a built-in target, and a
missing or corrupt file simply means "no dashboard channels" - startup never
breaks because of this file.

Schema (channels.json)::

    {"version": 1,
     "channels": [{"name": "MyNewDeals",
                   "sources": ["dealsvelocity", "deals"] | ["*"],
                   "categories": ["electronics"],
                   "amazon_tag": false,
                   "enabled": true,
                   "note": "added from dashboard"}]}
    "*" routes every known source except the Tricks-only ones.
    "categories" is optional: empty/missing means every product category, while
    a list (e.g. ["electronics"]) keeps ONLY posts classified into those
    categories - a category shelf like an electronics-only channel.

Stdlib only: importing this must never require telethon, aiohttp, or bot env.
"""
import json
import os
import re
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{5,64}$")
# Invite links (t.me/+...) are valid sources even though they are not handles.
INVITE_RE = re.compile(r"^https?://(t(elegram)?\.me|telegram\.org)/\+\S+$", re.I)


def channels_json_path() -> Path:
    """Where channels.json lives: CHANNELS_JSON_PATH, else next to the bot DB."""
    env = os.getenv("CHANNELS_JSON_PATH", "").strip()
    if env:
        return Path(env)
    db = os.getenv("BOT_DB_PATH", "").strip()
    if db:
        return Path(db).parent / "channels.json"
    return Path(__file__).resolve().parent / "channels.json"


def snapshot_path() -> Path:
    """Where the bot drops its routing snapshot for the dashboard to read."""
    return channels_json_path().parent / "routing_snapshot.json"


def pending_path() -> Path:
    """Sidecar the dashboard writes on every change; cleared once applied."""
    return channels_json_path().parent / ".channels_pending"


def empty_config() -> dict:
    return {"version": SCHEMA_VERSION, "channels": []}


def normalize_handle(name: str) -> str:
    return str(name or "").strip().lstrip("@").strip()


def category_keys(raw) -> list[str]:
    """Only an explicit empty list (or absent field) means unrestricted.

    A malformed shelf must NEVER silently become an all-category channel.
    Accept a single nonempty string for legacy hand-written configurations.
    """
    if isinstance(raw, str):
        raw = [raw]
    if (not isinstance(raw, list) or len(raw) > 20
            or any(not isinstance(key, str) or not CATEGORY_KEY_RE.fullmatch(key.strip().lower())
                   for key in raw)):
        raise ValueError("categories must be a list of up to 20 nonempty category keys; [] means all")
    return list(dict.fromkeys(key.strip().lower() for key in raw))


def load_channels(path: Path | None = None) -> dict:
    """Read channels.json, tolerantly: missing/corrupt/legacy means empty."""
    target = Path(path) if path else channels_json_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return empty_config()
    try:
        data = json.loads(raw)
    except ValueError:
        return empty_config()
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        return empty_config()
    channels = []
    for entry in data["channels"]:
        if isinstance(entry, dict) and normalize_handle(entry.get("name", "")):
            try:
                raw_cats = category_keys(entry.get("categories", []))
            except ValueError:
                continue
            if not isinstance(entry.get("sources", []), list):
                continue
            channels.append({
                "name": normalize_handle(entry.get("name", "")),
                "sources": [str(s).strip() for s in (entry.get("sources") or [])
                            if str(s).strip()][:60],
                "categories": [str(c).strip().lower() for c in raw_cats
                               if str(c).strip()][:20],
                "amazon_tag": bool(entry.get("amazon_tag", False)),
                "enabled": bool(entry.get("enabled", True)),
                "note": str(entry.get("note", ""))[:200],
            })
    return {"version": SCHEMA_VERSION, "channels": channels}


def save_channels(data: dict, path: Path | None = None) -> Path:
    """Atomically write channels.json (tmp file + rename, fsync)."""
    target = Path(path) if path else channels_json_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": SCHEMA_VERSION,
                          "channels": (data or {}).get("channels", [])},
                         ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".channels-", suffix=".tmp", dir=str(target.parent))
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
    return target


CATEGORY_KEY_RE = re.compile(r"^[a-z0-9_]{2,40}$")


def validate_channel(entry: dict, known_sources: set[str],
                     existing_targets: set[str],
                     known_categories: set[str] | None = None,
                     ) -> tuple[dict, list[str], list[str]]:
    """Validate one dashboard channel. Returns (cleaned, errors, warnings).

    errors blocks the save; warnings are shown but allowed (e.g. a source the
    bot has never seen - it may be a brand-new source, the bot resolves names
    at startup and logs SOURCE FAIL for anything unreachable). A mistyped
    category would silently starve the channel, so an unknown category is an
    error whenever the bot has told us its real category keys (and a warning
    when it has not reported yet).
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(entry, dict):
        return {}, ["channel must be a JSON object"], []
    name = normalize_handle((entry or {}).get("name", ""))
    if not HANDLE_RE.match(name):
        errors.append("name must be a Telegram username: 5-64 letters/digits/_ (no @, no invite link)")
    known_lower = {str(s).lower() for s in (known_sources or set())}
    if name.lower() in {str(t).lower() for t in (existing_targets or set())}:
        errors.append(f"@{name} is already a channel - dashboard entries must be new names")
    raw_sources = entry.get("sources", [])
    if not isinstance(raw_sources, list) or any(not isinstance(s, str) for s in raw_sources):
        errors.append("sources must be a list of strings")
        raw_sources = []
    sources = [s.strip() for s in raw_sources if s.strip()]
    if not sources:
        errors.append("pick at least one source (or '*' for every main source)")
    elif sources != ["*"]:
        for source in sources[:60]:
            if len(source) > 120:
                errors.append(f"source {source[:40]!r} is too long")
            elif (source.lower() not in known_lower
                    and not INVITE_RE.match(source) and not HANDLE_RE.match(source)):
                errors.append(f"source {source[:40]!r} is not a username or invite link")
            elif source.lower() not in known_lower:
                warnings.append(f"source {source} is new to the bot - it will be resolved at startup")
    try:
        raw_cats = category_keys(entry.get("categories", []))
    except ValueError as exc:
        errors.append(str(exc))
        raw_cats = []
    categories: list[str] = []
    known_cats = {str(c).lower() for c in (known_categories or set())}
    for cat in raw_cats:
        key = str(cat).strip().lower()
        if not key or key in categories:
            continue
        if not CATEGORY_KEY_RE.match(key):
            errors.append(f"category {str(cat)[:40]!r} is not a valid category key")
        elif known_cats and key not in known_cats:
            errors.append(f"unknown category {key!r} - pick from {sorted(known_cats)}")
        elif not known_cats:
            warnings.append(f"category {key} is not confirmed yet (bot has not reported "
                            "its categories) - it applies after the next restart")
        categories.append(key)
    cleaned = {
        "name": name,
        "sources": ["*"] if sources == ["*"] else sources[:60],
        "categories": categories[:20],
        "amazon_tag": bool((entry or {}).get("amazon_tag", False)),
        "enabled": bool((entry or {}).get("enabled", True)),
        "note": str((entry or {}).get("note", ""))[:200],
    }
    if cleaned["amazon_tag"]:
        warnings.append("our Amazon tag rides ONLY on channels declared in Associates "
                        "Central - list this channel there first, or Amazon may flag it")
    return cleaned, errors, warnings


def apply_to_routing(source_to_targets: dict, amazon_tag_targets: set, data: dict,
                     existing_targets: set[str] | None = None,
                     tricks_sources: set[str] | frozenset | None = None) -> dict:
    """Merge dashboard channels into live routing maps (mutates them).

    source_to_targets: SOURCE_TO_TARGETS (targets appended per channel).
    amazon_tag_targets: AMAZON_TAG_TARGETS (added only when the entry opts in).
    Returns {"added": [...], "tag_added": [...], "category_filters": {name: [...]},
             "skipped": [...], "warnings": [...]}.
    Never raises on bad data: a broken entry is skipped with a warning.
    """
    summary: dict = {"added": [], "tag_added": [], "category_filters": {},
                     "skipped": [], "warnings": []}
    try:
        channels = (data or {}).get("channels") or []
    except Exception:
        return summary
    if not isinstance(channels, list):
        return summary
    tricks = {str(s) for s in (tricks_sources or set())}
    in_use = {str(t).lower() for targets in source_to_targets.values() for t in targets}
    in_use |= {str(t).lower() for t in (existing_targets or set())}
    for entry in channels:
        try:
            if not isinstance(entry, dict):
                continue
            name = normalize_handle(entry.get("name", ""))
            if not name or not HANDLE_RE.match(name):
                summary["skipped"].append(str(entry.get("name", "?"))[:40])
                summary["warnings"].append(f"skipped channel {name or '?'}: bad name")
                continue
            if not entry.get("enabled", True):
                summary["skipped"].append(name)
                continue
            if name.lower() in in_use:
                summary["skipped"].append(name)
                summary["warnings"].append(
                    f"skipped @{name}: a channel by that name already exists")
                continue
            # Validate BEFORE mutating any routing or tag maps.
            cats = category_keys(entry.get("categories", []))
            sources = entry.get("sources") or []
            if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
                raise ValueError("sources must be a list of strings")
            if sources == ["*"]:
                sources = [s for s in source_to_targets if s not in tricks]
            routed = 0
            for source in sources:
                source = str(source)
                if not source:
                    continue
                targets = source_to_targets.setdefault(source, [])
                if all(str(t).lower() != name.lower() for t in targets):
                    targets.append(name)
                    routed += 1
            if entry.get("amazon_tag"):
                amazon_tag_targets.add(name)
                summary["tag_added"].append(name)
            if cats:
                summary["category_filters"][name] = cats
            summary["added"].append(name)
            in_use.add(name.lower())
            if not routed:
                summary["warnings"].append(f"@{name}: no sources routed (empty list?)")
        except Exception as exc:
            summary["skipped"].append(str((entry or {}).get("name", "?"))[:40])
            summary["warnings"].append(f"skipped a channel: {exc}"[:160])
    return summary
