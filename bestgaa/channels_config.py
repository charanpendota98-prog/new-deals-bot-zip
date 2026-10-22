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

    **Watch a SOURCE (2026-10-22, "channel link petti source add chesi post cheyali")**::

        {"watch": [{"channel": "DealSourceX",          # or https://t.me/+invite
                    "targets": ["*"],                  # "*" = the main product feed
                    "enabled": true,
                    "note": "hinted by the operator",
                    "added_at": 1792610000.0}]}

    A watched source is a channel the bot READS. The dashboard stores the intent
    (which channel, where its posts should go); the running bot resolves it with
    its own session, refuses anything it has not joined, and only then adds it to
    live routing - so a typo, a private channel it is not in, or a link that is
    really one of our own destinations can never silently swallow posts.

Stdlib only: importing this must never require telethon, aiohttp, or bot env.
"""
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

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
    return {"version": SCHEMA_VERSION, "channels": [], "whatsapp": [], "watch": []}


def normalize_handle(name: str) -> str:
    value = str(name or "").strip()
    if value.lower().startswith(("t.me/", "telegram.me/", "www.t.me/")):
        value = "https://" + value
    if "://" in value:
        try:
            url = urlsplit(value)
            if (url.scheme.lower() not in {"http", "https"}
                    or (url.hostname or "").lower() not in {"t.me", "www.t.me", "telegram.me"}
                    or url.username or url.password or url.port or url.query or url.fragment):
                return value
            handle = url.path.strip("/")
            # Only the public channel root is accepted, not invites or post links.
            return handle if HANDLE_RE.fullmatch(handle) else value
        except ValueError:
            return value
    return value.lstrip("@").strip()


MAX_SOURCES = 100


def normalize_source(value, known_sources=()):
    """Canonical public handle or private invite; never accept a message URL."""
    raw = str(value or "").strip()
    if raw == "*":
        return raw
    invite = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.org)/(?:\+|joinchat/)([A-Za-z0-9_-]+)/*", raw)
    candidate = "https://t.me/+" + invite[1] if invite else normalize_handle(raw)
    # Keep legacy configured keys (including their casing/invite spelling).
    for known in known_sources:
        existing = str(known)
        equivalent = ("https://t.me/+" + existing.split("+", 1)[1]) if "/+" in existing else normalize_handle(existing)
        if ((candidate.startswith("https://") and candidate == equivalent)
                or (not candidate.startswith("https://") and candidate.lower() == equivalent.lower())):
            return existing
    if HANDLE_RE.fullmatch(candidate) or re.fullmatch(r"https://t\.me/\+[A-Za-z0-9_-]+", candidate):
        return candidate
    raise ValueError("source must be a public channel link, @username, or private invite (not a message link)")


def source_keys(raw, known_sources=()):
    if not isinstance(raw, list) or any(not isinstance(s, str) for s in raw):
        raise ValueError("sources must be a list of strings")
    if len(raw) > MAX_SOURCES:
        raise ValueError(f"select at most {MAX_SOURCES} sources; nothing was truncated")
    sources, seen = [], set()
    for value in raw:
        if not value.strip():
            continue
        source = normalize_source(value, known_sources)
        identity = source if "/+" in source else source.lower()
        if identity not in seen:
            seen.add(identity)
            sources.append(source)
    if not sources:
        raise ValueError("pick at least one source (or '*' for every main source)")
    if "*" in sources and sources != ["*"]:
        raise ValueError("choose either every main source or selected sources, not both")
    return sources


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


WA_CORE_KEYS = ("under99", "under499", "best_of")
WA_VALUE_RE = re.compile(r"^\d{1,20}(-\d{1,20})?@(g\.us|newsletter|s\.whatsapp\.net)$")


def validate_whatsapp(entry: dict, known_categories: set[str],
                      seen_values: set[str]) -> tuple[dict, list[str]]:
    """One dashboard-managed WhatsApp channel: which shelf/core slot it carries.

    The bridge builds WhatsApp policy from `WA_CHANNEL_<KEY>` env at startup, so a
    row saved here is the operator's DECLARED intent plus the exact env line to
    paste; the dashboard never restarts the bridge and never pretends otherwise.
    A value shared by two keys is refused outright - the bridge logs that as a
    collision and silently stops filtering, so catching it at save time is
    cheaper than a week of unfiltered posts.
    """
    errors: list[str] = []
    if not isinstance(entry, dict):
        return {}, ["whatsapp entry must be an object"]
    key = str(entry.get("key", "") or "").strip().lower()
    if not key:
        errors.append("whatsapp entry needs a key (a category or under99/under499/best_of)")
    elif key not in known_categories and key not in WA_CORE_KEYS:
        errors.append(f"whatsapp key {key!r} is not a category key or one of "
                      f"{', '.join(WA_CORE_KEYS)}")
    value = str(entry.get("value", "") or "").strip()
    if not value:
        errors.append("whatsapp entry needs a channel id (…@newsletter or …@g.us)")
    elif len(value) > 64 or re.search(r"[\x00-\x1f\x7f]", value):
        errors.append("whatsapp channel id looks malformed")
    elif not WA_VALUE_RE.match(value):
        errors.append("whatsapp channel id must look like 120363…@newsletter or …@g.us "
                      "(invite links cannot be WhatsApp channels)")
    elif value.lower() in seen_values:
        errors.append(f"whatsapp channel {value} is already assigned to another key - "
                      f"the bridge would stop filtering one of them")
    cleaned = {
        "key": key,
        "value": value,
        "enabled": bool(entry.get("enabled", True)),
        "note": str(entry.get("note", ""))[:200],
    }
    return cleaned, errors


def validate_whatsapp_list(raw, known_categories: set[str]) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["whatsapp must be a list"]
    seen: set[str] = set()
    seen_keys: set[str] = set()
    cleaned: list[dict] = []
    for index, entry in enumerate(raw, start=1):
        row, row_errors = validate_whatsapp(entry, known_categories, seen)
        for err in row_errors:
            errors.append(f"whatsapp {index}: {err}")
        if row.get("key"):
            if row["key"] in seen_keys:
                errors.append(f"whatsapp {index}: key {row['key']} appears twice")
                continue
            seen_keys.add(row["key"])
        if row.get("value") and not any(e.startswith(f"whatsapp {index}") and "already" in e
                                        for e in row_errors):
            seen.add(row["value"].lower())
        if not row_errors:
            cleaned.append(row)
    return cleaned, errors


def wa_env_lines(rows: list[dict]) -> list[str]:
    """The exact env lines the bridge needs, generated from the saved intent."""
    out: list[str] = []
    for row in rows or []:
        if not row.get("enabled", True) or not row.get("value"):
            continue
        out.append(f"WA_CHANNEL_{str(row.get('key', '')).upper()}={row['value']}")
    return out


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
                normalized_sources = source_keys(entry.get("sources", []))
            except ValueError:
                continue
            if not isinstance(entry.get("sources", []), list):
                continue
            channels.append({
                "name": normalize_handle(entry.get("name", "")),
                "sources": normalized_sources,
                "categories": [str(c).strip().lower() for c in raw_cats
                               if str(c).strip()][:20],
                "amazon_tag": bool(entry.get("amazon_tag", False)),
                "enabled": bool(entry.get("enabled", True)),
                "note": str(entry.get("note", ""))[:200],
            })
    whatsapp: list[dict] = []
    for entry in data.get("whatsapp") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "") or "").strip().lower()
        value = str(entry.get("value", "") or "").strip()
        if key and value:
            whatsapp.append({
                "key": key, "value": value,
                "enabled": bool(entry.get("enabled", True)),
                "note": str(entry.get("note", ""))[:200],
            })
    watch: list[dict] = []
    for entry in data.get("watch") or []:
        if not isinstance(entry, dict):
            continue
        channel = str(entry.get("channel", "") or "").strip()
        if not channel:
            continue
        raw_targets = entry.get("targets", ["*"])
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        targets = [str(t).strip() for t in (raw_targets if isinstance(raw_targets, list) else [])
                   if str(t).strip()]
        watch.append({
            "channel": channel,
            "targets": targets or ["*"],
            "enabled": bool(entry.get("enabled", True)),
            "note": str(entry.get("note", ""))[:200],
            "added_at": float(entry.get("added_at") or 0.0),
        })
    return {"version": SCHEMA_VERSION, "channels": channels, "whatsapp": whatsapp,
            "watch": watch}


def save_channels(data: dict, path: Path | None = None) -> Path:
    """Atomically write channels.json (tmp file + rename, fsync)."""
    target = Path(path) if path else channels_json_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": SCHEMA_VERSION,
                          "channels": (data or {}).get("channels", []),
                          "whatsapp": (data or {}).get("whatsapp", []),
                          "watch": (data or {}).get("watch", [])},
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
    bot has never seen - it may be a brand-new source that still needs joined-account
    access checks on the running bot or at approved startup). A mistyped
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
        errors.append("use a public Telegram channel link (https://t.me/ChannelName) or @username; private invites and message links are not supported")
    known_lower = {str(s).lower() for s in (known_sources or set())}
    if name.lower() in {str(t).lower() for t in (existing_targets or set())}:
        errors.append(f"@{name} is already a channel - dashboard entries must be new names")
    try:
        sources = source_keys(entry.get("sources", []), known_sources)
    except ValueError as exc:
        errors.append(str(exc))
        sources = []
    for source in sources:
        if source == "*":
            continue
        if source.lower() == name.lower() or source.lower() in {normalize_handle(t).lower() for t in existing_targets}:
            errors.append(f"source {source} is a destination; refusing a repost loop")
        if source.lower() not in known_lower:
            warnings.append(f"source {source} needs access verification; join with the bot session account first. No automatic joining or history replay.")
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
        "sources": sources,
        "categories": categories[:20],
        "amazon_tag": bool((entry or {}).get("amazon_tag", False)),
        "enabled": bool((entry or {}).get("enabled", True)),
        "note": str((entry or {}).get("note", ""))[:200],
    }
    if cleaned["amazon_tag"]:
        warnings.append("our Amazon tag rides ONLY on channels declared in Associates "
                        "Central - list this channel there first, or Amazon may flag it")
    return cleaned, errors, warnings


WATCH_MAX = 100


def validate_watch(entry: dict, existing_targets: set[str],
                   known_targets: set[str] | None = None) -> tuple[dict, list[str], list[str]]:
    """One dashboard-managed SOURCE channel. Returns (cleaned, errors, warnings).

    `channel` is what the operator pasted - a public link, @username or invite.
    `targets` is where its posts should go: ["*"] means the main product feed
    (the bot resolves that to its own NO_TRICKS_TARGETS list, so a code change
    there is respected instead of frozen into the config).
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(entry, dict):
        return {}, ["watch entry must be a JSON object"], []
    try:
        channel = normalize_source(entry.get("channel", ""))
    except ValueError as exc:
        return {}, [f"channel: {exc}"], []
    if not channel:
        return {}, ["channel is required (paste the channel link)"], []
    known = {str(t).lower() for t in (existing_targets or set())}
    if channel.lower() in known:
        errors.append(f"{channel} is one of OUR posting channels; a destination cannot "
                      f"be its own source (that would repost our own posts)")
    raw_targets = entry.get("targets", ["*"])
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    if not isinstance(raw_targets, list) or len(raw_targets) > 40:
        return {}, ['targets must be a list of up to 40 channel names (or ["*"])'], []
    targets: list[str] = []
    for value in raw_targets:
        name = str(value or "").strip()
        if not name:
            continue
        if name == "*":
            targets.append("*")
            continue
        cleaned_name = normalize_handle(name)
        if not cleaned_name:
            errors.append(f"target {name[:40]!r} is not a channel name")
            continue
        if known and cleaned_name.lower() not in known:
            errors.append(f"target {cleaned_name} is not one of our channels")
            continue
        if not known:
            warnings.append(f"target {cleaned_name} cannot be confirmed until the bot "
                            f"reports its channels")
        if cleaned_name.lower() not in {t.lower() for t in targets}:
            targets.append(cleaned_name)
    if not targets:
        errors.append("pick at least one destination channel (or \"*\" for the main feed)")
    if "*" in targets and targets != ["*"]:
        errors.append("choose either the main feed or named channels, not both")
    if channel.lower() in {t.lower() for t in targets}:
        errors.append("a channel cannot be its own destination")
    if not errors:
        if channel.startswith("https://t.me/+"):
            warnings.append("private source: the bot session account must already be a "
                            "member; nothing is joined automatically")
        elif not (known_targets or known):
            warnings.append("the bot must report its channels before this can activate")
        else:
            warnings.append("the bot checks access with its own session and activates "
                            "this source live; nothing is joined automatically and no "
                            "old posts are replayed")
    cleaned = {
        "channel": channel,
        "targets": targets,
        "enabled": bool(entry.get("enabled", True)),
        "note": str(entry.get("note", ""))[:200],
        "added_at": float(entry.get("added_at") or time.time()),
    }
    return cleaned, errors, warnings


def watch_entries(data: dict | None) -> list[dict]:
    """Enabled-intent view of the saved watch list (never raises)."""
    rows = (data or {}).get("watch")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict) and str(row.get("channel", "")).strip():
            out.append(row)
    return out


def apply_watch_to_routing(source_to_targets: dict, data: dict,
                           main_targets, tricks_sources=None,
                           existing_targets: set[str] | None = None) -> dict:
    """Merge watched SOURCES into the live routing map (mutates it).

    A watched source with no route posts nowhere, so this is what makes "I
    added the channel" actually deliver. Returns
    {"watching": [...], "skipped": [...], "warnings": [...]}.
    """
    summary: dict = {"watching": [], "skipped": [], "warnings": []}
    main = [str(t) for t in (main_targets or [])]
    # `tricks_sources` are SOURCE names (as in apply_to_routing); they are kept in
    # the signature so a caller cannot confuse the two, and they are NOT used to
    # filter destination names — `main_targets` already is the no-tricks feed.
    del tricks_sources
    in_use = {str(t).lower() for targets in source_to_targets.values() for t in targets}
    in_use |= {str(t).lower() for t in (existing_targets or set())}
    in_use |= {t.lower() for t in main}
    rows = watch_entries(data)
    if len(rows) > WATCH_MAX:
        summary["warnings"].append(f"only the first {WATCH_MAX} watched sources are used")
        rows = rows[:WATCH_MAX]
    for entry in rows:
        try:
            cleaned, errors, _ = validate_watch(entry, in_use)
            if errors:
                summary["skipped"].append(str(entry.get("channel", "?"))[:40])
                summary["warnings"].append(
                    f"skipped source {str(entry.get('channel', '?'))[:40]}: {errors[0]}")
                continue
            if not cleaned["enabled"]:
                summary["skipped"].append(cleaned["channel"])
                continue
            wanted = main if cleaned["targets"] == ["*"] else cleaned["targets"]
            wanted = [t for t in wanted if t]
            if not wanted:
                summary["skipped"].append(cleaned["channel"])
                summary["warnings"].append(
                    f"source {cleaned['channel']}: no destination left after the tricks filter")
                continue
            targets = source_to_targets.setdefault(cleaned["channel"], [])
            for target in wanted:
                if all(str(t).lower() != target.lower() for t in targets):
                    targets.append(target)
            summary["watching"].append({"channel": cleaned["channel"],
                                        "targets": list(targets)})
        except Exception as exc:  # noqa: BLE001 - one bad row never breaks boot
            summary["skipped"].append(str((entry or {}).get("channel", "?"))[:40])
            summary["warnings"].append(f"skipped a source: {exc}"[:160])
    return summary


def apply_to_routing(source_to_targets: dict, amazon_tag_targets: set, data: dict,
                     existing_targets: set[str] | None = None,
                     tricks_sources: set[str] | frozenset | None = None,
                     main_sources=None) -> dict:
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
    wildcard_sources = list(main_sources) if main_sources is not None else list(source_to_targets)
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
            sources = source_keys(entry.get("sources", []), source_to_targets)
            if sources == ["*"]:
                sources = [s for s in wildcard_sources if s not in tricks]
            if any(s.lower() == name.lower() or s.lower() in in_use for s in sources):
                raise ValueError("source is a posting destination; refusing a loop")
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
