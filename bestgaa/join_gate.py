"""Approve join requests to OUR channels in ~2.5 seconds - opt-in, capped, logged.

The operator's request (2026-10-25): "konni channel button nokkagane kakunda
bot vallani accept cheyali oka 2-3 seconds lo - appudu vallaki official ga
anipisthadi". People who ask to join a channel we run should not wait hours for
a human; the bot already has a session in that channel, and approving a request
is a one-line API call for it.

What this is NOT:
  * it is not the bot joining anything - nothing here touches our own
    membership, and the standing rule "nothing auto-joins a channel" is
    untouched;
  * it is not automatic by default - a channel must be listed, and the global
    switch (or `JOIN_AUTO_APPROVE=0` in the environment) turns the whole thing
    off;
  * it is not unlimited - a per-channel per-minute cap keeps a raid from making
    the account look like a bot farm, and every decision is written to a log the
    dashboard reads, so "who approved this?" has an answer.

The decision function is pure (`plan`) so every rule below is testable without
Telegram: disabled channel, cap reached, delay clamp, our own channels only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# "2-3 seconds": long enough to look like somebody noticed, short enough that the
# person is still in the app. Clamped, so a typo in the config cannot make every
# approval instant (obvious automation) or take an hour.
DEFAULT_DELAY = 2.5
MIN_DELAY, MAX_DELAY = 1.0, 60.0
DEFAULT_PER_MINUTE = 20
MAX_PER_MINUTE = 200
SECTION = "join_requests"
LOG_ROWS = 500


def _bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _float(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def name_of(value) -> str:
    """A channel as a stable key: no link, no @, lower case."""
    text = str(value or "").strip()
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "t.me/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.lstrip("@").split("?")[0].strip("/")
    return text.lower()


def section(data: dict | None) -> dict:
    """The whole setting, normalized. Anything odd becomes "off"."""
    raw = (data or {}).get(SECTION)
    if not isinstance(raw, dict):
        return {"enabled": False, "delay": DEFAULT_DELAY, "channels": {}}
    channels: dict[str, dict] = {}
    for row in raw.get("channels") or []:
        if not isinstance(row, dict):
            continue
        key = name_of(row.get("name") or row.get("channel") or "")
        if not key:
            continue
        channels[key] = {
            "name": str(row.get("name") or row.get("channel") or key)[:80],
            "enabled": _bool(row.get("enabled"), True),
            "delay": _float(row.get("delay") or raw.get("delay"), DEFAULT_DELAY,
                            MIN_DELAY, MAX_DELAY),
            "per_minute": _int(row.get("per_minute") or raw.get("per_minute"),
                               DEFAULT_PER_MINUTE, 1, MAX_PER_MINUTE),
            "added_at": row.get("added_at") or 0,
        }
    return {
        "enabled": _bool(raw.get("enabled"), False) and not _env_off(),
        "delay": _float(raw.get("delay"), DEFAULT_DELAY, MIN_DELAY, MAX_DELAY),
        "channels": channels,
    }


def _env_off() -> bool:
    """`JOIN_AUTO_APPROVE=0` stops the whole feature without editing a config."""
    value = os.getenv("JOIN_AUTO_APPROVE")
    if value is None or not value.strip():
        return False
    return not _bool(value, True)


def validate(name, entry: dict | None, known: dict | None = None) -> tuple[dict, list, list]:
    """One channel's settings, judged. Errors block, warnings explain."""
    errors: list[str] = []
    warnings: list[str] = []
    key = name_of(name)
    if not key:
        return {}, ["a channel name or link is required"], []
    entry = entry if isinstance(entry, dict) else {}
    clean = {
        "name": str(entry.get("name") or key).strip()[:80],
        "enabled": _bool(entry.get("enabled"), True),
        "delay": _float(entry.get("delay"), DEFAULT_DELAY, MIN_DELAY, MAX_DELAY),
        "per_minute": _int(entry.get("per_minute"), DEFAULT_PER_MINUTE, 1, MAX_PER_MINUTE),
        "added_at": float(entry.get("added_at") or time.time()),
    }
    if known is not None and known and key not in known:
        errors.append(f"{key} is not one of our channels - the bot can only approve "
                      f"requests where this account is an admin")
    if clean["delay"] > 10:
        warnings.append(f"{clean['delay']:.1f}s is slow: the person may leave before approval")
    return clean, errors, warnings


def plan(setting: dict, channel, *, now: float | None = None,
         recent: list[float] | None = None) -> dict:
    """Approve, wait, or refuse - and say why. Pure: no clock, no network."""
    now = time.time() if now is None else now
    key = name_of(channel)
    if not setting.get("enabled"):
        return {"action": "skip", "reason": "auto-approval is off for this bot", "delay": 0}
    entry = (setting.get("channels") or {}).get(key)
    if not entry:
        return {"action": "skip", "reason": f"{key or 'this channel'} is not on the list", "delay": 0}
    if not entry.get("enabled", True):
        return {"action": "skip", "reason": f"{key} auto-approval is paused", "delay": 0}
    window = [stamp for stamp in (recent or []) if now - float(stamp) < 60.0]
    if len(window) >= int(entry.get("per_minute") or DEFAULT_PER_MINUTE):
        return {"action": "hold",
                "reason": (f"{len(window)} approvals in the last minute for {key} - "
                           f"cap is {entry.get('per_minute')}; this one is left for a human"),
                "delay": 0}
    delay = _float(entry.get("delay"), DEFAULT_DELAY, MIN_DELAY, MAX_DELAY)
    return {"action": "approve", "reason": f"auto-approved after {delay:.1f}s",
            "delay": delay}


class JoinGate:
    """The running state: recent approvals per channel + a durable log.

    `approve` is injected (the bot passes Telethon's `event.approve`), so the
    whole class is exercised in tests with a plain async function.
    """

    def __init__(self, log_path: Path | None = None, *, data_provider=None,
                 clock=time.time):
        self.log_path = Path(log_path) if log_path else None
        self.data_provider = data_provider or (lambda: {})
        self.clock = clock
        self.recent: dict[str, list[float]] = {}
        self.approved = 0
        self.refused = 0

    def setting(self) -> dict:
        try:
            return section(self.data_provider() or {})
        except Exception:  # noqa: BLE001 - a bad config must not kill the hot path
            return {"enabled": False, "delay": DEFAULT_DELAY, "channels": {}}

    def _record(self, row: dict) -> None:
        if not self.log_path:
            return
        try:
            try:
                rows = json.loads(self.log_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rows = []
            if not isinstance(rows, list):
                rows = []
            rows.append(row)
            rows = rows[-LOG_ROWS:]
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.log_path.with_suffix(self.log_path.suffix + ".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.log_path)
        except OSError:
            pass

    async def handle(self, channel, user_id, approve, *, sleep=None, name: str = "") -> dict:
        """One join request: decide, wait the chosen delay, then approve."""
        setting = self.setting()
        key = name_of(channel)
        verdict = plan(setting, key, now=self.clock(),
                       recent=self.recent.get(key, []))
        row = {
            "at": self.clock(), "channel": key, "display": name or key,
            "user_id": int(user_id or 0), "action": verdict["action"],
            "reason": verdict["reason"],
        }
        if verdict["action"] != "approve":
            self.refused += 1
            self._record(row)
            return row
        if sleep is not None:
            await sleep(verdict["delay"])
        try:
            await approve()
        except Exception as exc:  # noqa: BLE001 - log, never crash the event loop
            row["action"] = "failed"
            row["reason"] = f"approve failed: {exc}"
            self.refused += 1
            self._record(row)
            return row
        self.approved += 1
        self.recent.setdefault(key, []).append(self.clock())
        self.recent[key] = [stamp for stamp in self.recent[key]
                            if self.clock() - float(stamp) < 300.0]
        row["action"] = "approved"
        self._record(row)
        return row

    def summary(self) -> dict:
        return {"approved": self.approved, "refused": self.refused,
                "channels": sorted(self.recent)}
