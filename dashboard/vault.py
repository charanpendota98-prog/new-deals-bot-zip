"""Credential vault: enter API keys in the dashboard, never on the server.

The operator asked for "dashboard lo API key ichelaga vunchu, multiple ga" -
the ability to put API keys in from the dashboard, several of them, instead of
SSH-ing in to edit `.env` with `sed`. This module is the whole engine: it knows
which keys the bot reads, validates every value before a single byte is written,
writes the bot's `.env` atomically (backup first, mode 0600, comments and order
preserved), and reports back **only masked values** - a secret never leaves the
process in clear text, not in the API response, not in the page, not in the
journal, not in a log line.

Design rules, each one earned:

  * **The bot owns the format.** Values are written exactly as the bot reads them
    (`BITLY_TOKENS` and `EARNKARO_API_KEY` are comma-separated lists; everything
    else is a single value). The vault never invents a syntax the bot would
    silently ignore - a key that is saved but not read is worse than no key.
  * **A wrong key is caught here, not at 2 a.m.** EarnKaro tokens are JWTs: the
    payload is decoded and the `earnkaro` publisher id is read, so "this key pays
    a different account" is a validation error instead of a commission leak. An
    Amazon tag that is not ours is refused, because it would credit somebody
    else for our sales. Two EarnKaro keys on different publishers are refused
    for the same reason.
  * **Multiple keys are a failover, not a random pick.** The bot tries them in
    order (first = active) and rotates on an authentication refusal.
  * **Nothing is written twice.** A no-op save writes nothing at all. A dry run
    writes nothing and says exactly what would change.
  * **Every save is reversible.** The previous file is kept as
    `.env.bak.<stamp>` (0600) before the new one replaces it, and an append-only
    journal records WHO/WHAT/WHEN - key name and last four characters only.

Everything here is pure over strings and paths, so it is tested without a bot, a
service or a network.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# The one place a viewer may see a secret's tail. Four characters of a 40-char
# token is enough to tell two keys apart and useless to anyone reading over a
# shoulder.
TAIL = 4
MAX_VALUES = 25
MAX_VALUE_LEN = 4096
JOURNAL_LIMIT = 200
LEAK_TAIL_BYTES = 4 * 1024 * 1024

# Keep in step with `OUR_AMAZON_TAGS` in bestgaa/main_bot_new.py; a test asserts
# the two sets are identical, so a tag added there cannot drift in here.
OUR_AMAZON_TAGS = {"mama086-21"}


@dataclass(frozen=True)
class Slot:
    """One environment key the bot reads, and how to judge a value for it."""

    key: str
    label: str
    group: str
    help: str
    secret: bool = True
    multi: bool = False
    required: bool = False
    hint: str = ""
    fields: dict = field(default_factory=dict)


SLOTS: tuple[Slot, ...] = (
    Slot("TELEGRAM_API_ID", "Telegram API ID", "telegram",
         "my.telegram.org → API development tools → App api_id. Numeric.",
         secret=False, required=True, hint="digits only, e.g. 1234567"),
    Slot("TELEGRAM_API_HASH", "Telegram API hash", "telegram",
         "my.telegram.org → the app's api_hash (32 hex characters).",
         required=True, hint="32 hexadecimal characters"),
    Slot("EARNKARO_API_KEY", "EarnKaro / Affiliaters token", "earnkaro",
         "Affiliaters → API page. A JWT that carries your publisher id; every "
         "converted link pays that account. Add several to fail over - they must "
         "all carry the SAME publisher id.",
         multi=True, required=True, hint="eyJ… (three dot-separated parts)"),
    Slot("EARNKARO_PUBLISHER_ID", "EarnKaro publisher id", "earnkaro",
         "Optional cross-check. The token is the truth: the bot always uses the "
         "publisher id inside it.",
         secret=False, hint="digits, e.g. 5478322"),
    Slot("AMAZON_TAG", "Amazon Associates tag", "amazon",
         "Only a tag we own is accepted. A source's tag here would credit them "
         "for our sales on every channel.",
         secret=False, required=True, hint="…-21"),
    Slot("BITLY_TOKENS", "Bitly access tokens", "bitly",
         "Short links we mint ourselves. Add several: the bot tries them in order "
         "and skips a token that is rate-limited.",
         multi=True, hint="generic access token, 20+ characters"),
    Slot("HYPD_API_TOKEN", "HYPD API token", "hypd",
         "HYPD dashboard → API. Used to mint OUR Meesho store links.",
         hint="at least 8 characters"),
    Slot("HYPD_STORE_ID", "HYPD store id", "hypd",
         "The store number our links are minted under (e.g. 93944).",
         secret=False, hint="digits"),
    Slot("HYPD_STORE_SLUG", "HYPD store slug", "hypd",
         "The public store name used in the link, if your store has one.",
         secret=False, hint="lower-case letters, digits, dashes"),
)

SLOT_BY_KEY = {slot.key: slot for slot in SLOTS}
GROUPS = (
    ("telegram", "Telegram account", "The session every channel action runs as."),
    ("earnkaro", "EarnKaro (commission)", "Every converted link pays the publisher id in this token."),
    ("amazon", "Amazon Associates", "Our own tag - the whole commission, no network share."),
    ("bitly", "Bitly (short links)", "Short links we mint; several tokens are a failover."),
    ("hypd", "HYPD (Meesho)", "Our Meesho store links."),
)

JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,28}-21$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
DIGITS_RE = re.compile(r"^\d{1,12}$")
BITLY_RE = re.compile(r"^[A-Za-z0-9]{20,64}$")
PLACEHOLDER_RE = re.compile(r"REPLACE_WITH|CHANGEME|YOUR[_ -]?KEY|<.*>|xxxx", re.I)


# --------------------------------------------------------------------------- #
# .env parsing / rendering (comments and order survive)
# --------------------------------------------------------------------------- #
def parse_env(text: str) -> list[dict]:
    """Every line, classified. Nothing is normalised away."""
    lines: list[dict] = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            lines.append({"kind": "blank", "raw": raw})
        elif stripped.startswith("#"):
            lines.append({"kind": "comment", "raw": raw})
        elif "=" in stripped:
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "'\""
            lines.append({"kind": "kv", "raw": raw, "key": key,
                          "value": value[1:-1] if quoted else value,
                          "quoted": quoted})
        else:
            # A line without `=` is not a key. Keeping it verbatim is the only
            # honest option: the bot may ignore it, and silently deleting an
            # operator's line is how a working .env breaks.
            lines.append({"kind": "other", "raw": raw})
    return lines


def read_env_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def env_values(text: str) -> dict[str, str]:
    """Last value wins, exactly like systemd's EnvironmentFile."""
    values: dict[str, str] = {}
    for line in parse_env(text):
        if line["kind"] == "kv":
            values[line["key"]] = line["value"]
    return values


def _render_value(slot: Slot | None, value: str) -> str:
    if not value:
        return ""
    if any(ch in value for ch in " \t\"'"):
        return '"' + value.replace('"', "") + '"'
    return value


def render_env(text: str, updates: dict[str, str | None]) -> str:
    """Apply `updates` (None removes the key) and keep everything else as it was."""
    lines = parse_env(text)
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        if line["kind"] == "kv" and line["key"] in remaining:
            value = remaining.pop(line["key"])
            if value is not None and value != "":
                out.append(f"{line['key']}={_render_value(None, value)}")
            # A removed key loses its line entirely; its value must not linger.
            continue
        out.append(line["raw"])
    appended: list[str] = []
    for key, value in remaining.items():
        if value:
            appended.append(f"{key}={_render_value(None, value)}")
    if appended:
        if out and out[-1].strip():
            out.append("")
        out.extend(appended)
    return "\n".join(out).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# masking
# --------------------------------------------------------------------------- #
def mask(value: str, *, secret: bool = True) -> str:
    """The only way a secret reaches the page: a tail, never the value."""
    value = str(value or "")
    if not value:
        return ""
    if not secret:
        return value
    if len(value) <= TAIL + 2:
        return "•" * len(value)
    return "•" * 6 + value[-TAIL:]


FINGERPRINT_MIN = 16


def fingerprint(value: str) -> str:
    """A short tag that tells two stored keys apart without revealing either.

    Two keys for the same provider can share their last four characters (the
    page then showed three identical chips and "remove the second one" was a
    guess). Six characters of a truncated SHA-256 is not reversible, cannot be
    brute-forced for a high-entropy token, and is stable across reloads. Short
    or low-entropy values get no tag at all: a six-character hash of a 7-digit
    number is a lookup table, not a mask.
    """
    value = str(value or "")
    if len(value) < FINGERPRINT_MIN:
        return ""
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:6]


def tail(value: str) -> str:
    value = str(value or "")
    return value[-TAIL:] if len(value) > TAIL else ""


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying a signature (we cannot, offline)."""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001 - a malformed token is a validation answer
        return {}


def split_values(slot: Slot, raw) -> list[str]:
    """A slot's values, whether they arrive as a string or a list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        raise ValueError("values must be a string or a list of strings")
    out: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def _check_value(slot: Slot, value: str, index: int) -> tuple[list[dict], dict]:
    """One value, judged. Returns (checks, facts)."""
    checks: list[dict] = []
    facts: dict = {}

    def add(level: str, text: str) -> None:
        checks.append({"level": level, "text": text})

    where = f"key {index + 1}" if slot.multi else "value"
    if len(value) > MAX_VALUE_LEN:
        add("error", f"{where}: longer than {MAX_VALUE_LEN} characters")
        return checks, facts
    if any(ch in value for ch in "\r\n\x00"):
        add("error", f"{where}: contains a line break or NUL - that would break the .env file")
        return checks, facts
    if PLACEHOLDER_RE.search(value):
        add("error", f"{where}: looks like a placeholder, not a real key")
    if "#" in value:
        add("warn", f"{where}: contains '#', which some .env readers treat as a comment")
    if value != value.strip():
        add("warn", f"{where}: leading/trailing spaces were trimmed")
    if value.startswith(('"', "'")):
        add("error", f"{where}: starts with a quote - paste the key itself, not a quoted one")

    if slot.key == "TELEGRAM_API_ID":
        if not DIGITS_RE.match(value):
            add("error", "api_id must be digits only")
        elif not 4 <= len(value) <= 10:
            add("warn", "api_id is usually 6-8 digits")
    elif slot.key == "TELEGRAM_API_HASH":
        if not HEX32_RE.match(value):
            add("error", "api_hash is 32 hexadecimal characters")
        if ":" in value:
            add("error", "that looks like a BOT token (123456:ABC…), not an api_hash")
    elif slot.key == "EARNKARO_API_KEY":
        if not JWT_RE.match(value):
            add("error", "not a JWT - the EarnKaro/Affiliaters API token has three "
                         "dot-separated parts")
        claims = jwt_claims(value)
        facts["publisher"] = str(claims.get("earnkaro") or "").strip()
        if claims and not facts["publisher"]:
            add("error", "the token carries no 'earnkaro' publisher id, so it cannot be "
                         "proven to pay us (the API would answer 401/403)")
        if facts["publisher"]:
            add("info", f"pays publisher {facts['publisher']}")
        issued = claims.get("iat")
        if isinstance(issued, (int, float)):
            try:
                facts["issued"] = time.strftime("%Y-%m-%d", time.gmtime(float(issued)))
                add("info", f"issued {facts['issued']}")
            except (ValueError, OSError):
                pass
        expires = claims.get("exp")
        if isinstance(expires, (int, float)):
            facts["expires"] = float(expires)
            if float(expires) < time.time():
                add("error", "this token has EXPIRED")
            else:
                add("info", "expires " + time.strftime("%Y-%m-%d", time.gmtime(float(expires))))
    elif slot.key == "EARNKARO_PUBLISHER_ID":
        if not DIGITS_RE.match(value):
            add("error", "publisher id is digits only")
    elif slot.key == "AMAZON_TAG":
        if not TAG_RE.match(value):
            add("error", "an Associates tag looks like 'name-21' (lower-case letters, digits, dashes)")
        elif value.lower() not in {tag.lower() for tag in OUR_AMAZON_TAGS}:
            add("error", f"{value} is not a tag we own ({', '.join(sorted(OUR_AMAZON_TAGS))}) - "
                         f"using it would credit somebody else for our sales")
        else:
            add("info", "this is our tag")
    elif slot.key == "BITLY_TOKENS":
        if not BITLY_RE.match(value):
            add("error", "a Bitly generic access token is 20+ letters/digits")
        if value.lower().startswith("http"):
            add("error", "that is a link, not a token")
    elif slot.key == "HYPD_API_TOKEN":
        if len(value) < 8:
            add("error", "too short to be an API token")
    elif slot.key == "HYPD_STORE_ID":
        if not DIGITS_RE.match(value):
            add("error", "store id is digits only")
    elif slot.key == "HYPD_STORE_SLUG":
        if not SLUG_RE.match(value):
            add("error", "a slug is lower-case letters, digits and dashes")

    if not checks:
        add("ok", "format looks right")
    return checks, facts


def validate_slot(slot: Slot, values: list[str], *, all_values: dict[str, list[str]]) -> dict:
    """Validate one slot's whole value list, including cross-key rules."""
    result = {"key": slot.key, "label": slot.label, "errors": [], "warnings": [],
              "info": [], "values": [], "publishers": []}
    if len(values) > MAX_VALUES:
        result["errors"].append(f"at most {MAX_VALUES} values for {slot.key}")
    if not slot.multi and len(values) > 1:
        result["errors"].append(f"{slot.key} takes ONE value; {len(values)} were given")
    for index, value in enumerate(values):
        checks, facts = _check_value(slot, value, index)
        for check in checks:
            bucket = {"error": "errors", "warn": "warnings"}.get(check["level"], "info")
            result[bucket].append(check["text"])
        entry = {"masked": mask(value, secret=slot.secret), "tail": tail(value),
                 "checks": checks}
        if facts.get("publisher"):
            entry["publisher"] = facts["publisher"]
            result["publishers"].append(facts["publisher"])
        if facts.get("issued"):
            entry["issued"] = facts["issued"]
        if facts.get("expires"):
            entry["expires"] = facts["expires"]
        result["values"].append(entry)

    # ---- cross-key rules ---------------------------------------------------- #
    if slot.key == "EARNKARO_API_KEY" and len(set(result["publishers"])) > 1:
        result["errors"].append(
            "these tokens pay DIFFERENT publishers "
            f"({', '.join(sorted(set(result['publishers'])))}). Only keys for the same "
            "account may be kept: a failover must still pay us.")
    if slot.key == "EARNKARO_PUBLISHER_ID" and values:
        token_ids = all_values.get("EARNKARO_API_KEY") or []
        claims_ids = {str(jwt_claims(token).get("earnkaro") or "").strip() for token in token_ids}
        claims_ids.discard("")
        if claims_ids and values[0] not in claims_ids:
            result["warnings"].append(
                f"the token pays {', '.join(sorted(claims_ids))}, not {values[0]} - the "
                f"token wins; this line is only a cross-check")
    if slot.key == "BITLY_TOKENS":
        lowered = [value.lower() for value in values]
        if len(set(lowered)) != len(lowered):
            result["warnings"].append("the same token appears twice; duplicates were removed")
    return result


def validate_update(updates: dict[str, list[str]], current: dict[str, str]) -> dict:
    """Validate a whole save before anything is written.

    `updates` is what the form sent (already split per key); `current` is the
    .env as it is on disk, so a cross-check can see the keys that are not being
    changed in this request.
    """
    all_values: dict[str, list[str]] = {}
    for key, raw in current.items():
        slot = SLOT_BY_KEY.get(key)
        if slot is not None:
            all_values[key] = split_values(slot, raw)
    for key, values in updates.items():
        all_values[key] = list(values)

    report: dict = {"keys": [], "errors": [], "warnings": [], "unknown": [],
                    "changed": [], "unchanged": []}
    for key in updates:
        if key not in SLOT_BY_KEY:
            report["unknown"].append(key)
    if report["unknown"]:
        report["errors"].append(
            "unknown key(s): " + ", ".join(sorted(report["unknown"]))
            + " - only the keys the bot actually reads can be saved")
    for key, values in updates.items():
        slot = SLOT_BY_KEY.get(key)
        if slot is None:
            continue
        verdict = validate_slot(slot, values, all_values=all_values)
        report["keys"].append(verdict)
        report["errors"].extend(f"{key}: {text}" for text in verdict["errors"])
        report["warnings"].extend(f"{key}: {text}" for text in verdict["warnings"])
        before = split_values(slot, current.get(key, ""))
        if before == values:
            report["unchanged"].append(key)
        else:
            # Name the change honestly: appending a second key is an "add", not a
            # "replace" - that word made the preview look like the first key was
            # about to be overwritten.
            if not values:
                action = "remove"
            elif not before:
                action = "add"
            elif before == values[:len(before)]:
                action = "add"
            elif values == before[:len(values)]:
                action = "remove"
            else:
                action = "replace"
            report["changed"].append({
                "key": key, "label": slot.label,
                "before": [mask(value, secret=slot.secret) for value in before],
                "after": [mask(value, secret=slot.secret) for value in values],
                "before_fp": [fingerprint(value) for value in before],
                "after_fp": [fingerprint(value) for value in values],
                "action": action,
            })
    return report


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def write_env(path: Path, text: str, *, backup: bool = True) -> dict:
    """Atomic, 0600, backup first. Returns what happened (never the content)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = None
    if backup and path.exists():
        backup_path = path.with_name(f"{path.name}.bak.{stamp}")
        try:
            backup_path.write_bytes(path.read_bytes())
            os.chmod(backup_path, 0o600)
        except OSError:
            backup_path = None
    fd, temporary = tempfile.mkstemp(prefix=".env.tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return {"path": str(path), "backup": str(backup_path) if backup_path else "",
            "bytes": len(text.encode("utf-8")), "written_at": time.time()}


def env_revision(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def file_state(path: Path) -> dict:
    path = Path(path)
    state = {"path": str(path), "exists": path.exists(), "writable": False,
             "mode": "", "owner": "", "bytes": 0, "mtime": 0.0, "revision": ""}
    try:
        stat = path.stat()
    except OSError:
        return state
    state.update(bytes=stat.st_size, mtime=stat.st_mtime,
                 mode=oct(stat.st_mode & 0o777)[2:], writable=os.access(path, os.W_OK))
    try:
        import pwd
        state["owner"] = pwd.getpwuid(stat.st_uid).pw_name
    except (KeyError, ImportError):
        state["owner"] = str(stat.st_uid)
    state["revision"] = env_revision(read_env_text(path))
    return state


# --------------------------------------------------------------------------- #
# journal (masked, append-only)
# --------------------------------------------------------------------------- #
def journal_path(env_path: Path) -> Path:
    return Path(env_path).with_name("vault_journal.jsonl")


def journal_read(env_path: Path, limit: int = 20) -> list[dict]:
    path = journal_path(env_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for raw in lines[-max(1, min(JOURNAL_LIMIT, limit)):]:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return list(reversed(events))


def journal_append(env_path: Path, event: dict) -> None:
    path = journal_path(env_path)
    record = {"at": time.time(), **event}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# leak scan (counts only - never a context line)
# --------------------------------------------------------------------------- #
def leak_scan(secrets: list[str], files: list[Path], *, tail_bytes: int = LEAK_TAIL_BYTES) -> dict:
    """Is any secret visible in a log or config file? Counts only, never content."""
    wanted = [value for value in secrets if len(value) >= 8]
    scanned: list[dict] = []
    hits: list[dict] = []
    for path in files:
        path = Path(path)
        entry = {"path": str(path), "bytes": 0, "exists": path.exists()}
        scanned.append(entry)
        if not wanted or not entry["exists"]:
            continue
        try:
            size = path.stat().st_size
            with open(path, "rb") as handle:
                if size > tail_bytes:
                    handle.seek(size - tail_bytes)
                blob = handle.read(tail_bytes)
        except OSError:
            continue
        entry["bytes"] = len(blob)
        entry["tail_only"] = size > tail_bytes
        text = blob.decode("utf-8", "ignore")
        for value in wanted:
            count = text.count(value)
            if count:
                hits.append({"path": str(path), "count": count, "tail": tail(value)})
    return {"scanned": scanned, "hits": hits,
            "state": "leak" if hits else ("clean" if wanted else "nothing-to-scan")}


# --------------------------------------------------------------------------- #
# readiness: can this bot earn right now?
# --------------------------------------------------------------------------- #
def readiness(values: dict[str, str], *, bot_running: bool, bot_has_it: bool,
              db_ok: bool, leaks: dict) -> dict:
    """One verdict an operator can act on, built from facts, not hopes."""
    checks: list[dict] = []

    def check(name: str, state: str, detail: str) -> None:
        checks.append({"name": name, "state": state, "detail": detail})

    def slot_values(key: str) -> list[str]:
        slot = SLOT_BY_KEY.get(key)
        return split_values(slot, values.get(key, "")) if slot else []

    def slot_verdict(key: str) -> tuple[str, str]:
        """One slot, judged by the SAME validator the form uses.

        `off` means "optional and not set": the bot works without it, so it must
        not turn the headline into a warning. Before this, three optional keys
        (publisher id, HYPD store id, HYPD slug) were permanently unset, so the
        panel always said "needs attention" - and a real warning (a missing HYPD
        token, an expiring EarnKaro token) looked exactly like the noise.
        """
        slot = SLOT_BY_KEY[key]
        found = slot_values(key)
        if not found:
            return ("fail" if slot.required else "off",
                    "not set" if slot.required else "not set (optional)")
        verdict = validate_slot(slot, found, all_values={k: slot_values(k) for k in SLOT_BY_KEY})
        if verdict["errors"]:
            return "fail", verdict["errors"][0]
        if verdict["warnings"]:
            return "warn", verdict["warnings"][0]
        return "ok", f"{len(found)} value(s), format verified"

    # EVERY slot, not a hand-picked four: a new key added to SLOTS must appear
    # here by existing, never by somebody remembering to add a line.
    for slot in SLOTS:
        state, detail = slot_verdict(slot.key)
        if slot.key == "BITLY_TOKENS" and state == "off":
            check(slot.label, "off", "none - links stay long, commission unaffected")
            continue
        check(slot.label, state, detail)

    # ---- the HYPD set is a SET: a token without a store id cannot mint ------- #
    hypd_token = slot_values("HYPD_API_TOKEN")
    hypd_store = slot_values("HYPD_STORE_ID")
    if hypd_token and not hypd_store:
        check("HYPD minting", "warn",
              "the API token is set but the store id is not - Meesho links cannot be "
              "minted, so those deals go out untagged")
    elif hypd_store and not hypd_token:
        check("HYPD minting", "warn",
              "the store id is set but the API token is not - same outcome: no minted links")
    check("bot", "ok" if bot_running else "warn",
          "running" if bot_running else "not confirmed running (no heartbeat)")
    check("saved keys are live", "ok" if bot_has_it else "warn",
          "the running bot has these keys" if bot_has_it else
          "the .env changed after the bot started - restart to apply")
    check("database", "ok" if db_ok else "fail",
          "readable" if db_ok else "not readable - the dashboard cannot prove posts")
    check("secrets in logs", "fail" if leaks.get("hits") else "ok",
          f"{len(leaks.get('hits') or [])} file(s) contain a stored key" if leaks.get("hits")
          else "no stored key found in the scanned files")

    fails = [c for c in checks if c["state"] == "fail"]
    warns = [c for c in checks if c["state"] == "warn"]
    off = [c for c in checks if c["state"] == "off"]
    if fails:
        state = "blocked"
        headline = ("Blocked: " + "; ".join(c["name"] for c in fails)
                    + " - a key the bot needs is missing or wrong.")
    elif warns:
        state = "attention"
        headline = ("Working, with " + str(len(warns)) + " thing(s) to look at: "
                    + "; ".join(c["name"] for c in warns) + ".")
    else:
        state = "ready"
        headline = "Ready: every key the bot needs is set, valid, and live in the running bot."
        if off:
            headline += (" Optional, not set: "
                         + ", ".join(c["name"] for c in off) + ".")
    return {"state": state, "headline": headline, "checks": checks,
            "fail": len(fails), "warn": len(warns), "optional_off": len(off)}
