"""Append-only live activation for dashboard destinations.

No service restarts, history replay, source subscriptions or merchant requests.
Existing routing is deliberately immutable here: edits/removals require an
approved restart. New public channels are staged and permission-checked first.
"""
import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

try:
    from . import channels_config as config
except ImportError:
    import channels_config as config

log = logging.getLogger("bestgaa")


def revision(raw):
    return hashlib.sha256(raw).hexdigest()


def policy(entry):
    """Notes/order do not change delivery policy."""
    return (tuple(sorted(entry["sources"])), tuple(sorted(entry["categories"])),
            bool(entry["amazon_tag"]), bool(entry["enabled"]))


def atomic_snapshot(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".routing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LiveChannels:
    def __init__(self, routing, tag_targets, category_filters, source_map, target_map,
                 initial_channels, builtin_targets, known_categories, tricks_sources,
                 resolve_target, on_applied=lambda: None, path=None, snapshot_file=None):
        self.routing, self.tags, self.filters = routing, tag_targets, category_filters
        self.source_map, self.target_map = source_map, target_map
        self.applied = {}
        for channel in initial_channels:
            if channel.get("enabled", True):
                self.applied.setdefault(channel["name"].lower(), channel)
        self.builtins = set(builtin_targets)
        self.categories, self.tricks = set(known_categories), set(tricks_sources)
        self.resolve_target, self.on_applied = resolve_target, on_applied
        self.path = Path(path) if path else config.channels_json_path()
        self.snapshot_file = Path(snapshot_file) if snapshot_file else config.snapshot_path()
        self.retry_after = {}
        self.access_backoff_until = 0
        self.cached_errors = {}
        self.applied_revision = None
        self.started_at = time.time()
        self.last_observed = None

    def publish(self, states, observed, error=""):
        try:
            base = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
            if not isinstance(base, dict):
                base = {}
        except (OSError, ValueError):
            base = {}
        base.update(live_reload=True, heartbeat_at=time.time(),
                    started_at=base.get("started_at", self.started_at),
                    live_channels=states, live_error=error,
                    observed_revision=observed, applied_revision=self.applied_revision,
                    sources=sorted(self.routing),
                    main_sources=sorted(s for s in self.routing if s not in self.tricks),
                    dashboard_added=sorted(c["name"] for c in self.applied.values()),
                    targets=sorted(self.builtins | {t for values in self.routing.values() for t in values}))
        atomic_snapshot(self.snapshot_file, base)

    async def sync_once(self):
        states = {}
        try:
            raw = self.path.read_bytes() if self.path.exists() else b""
            observed = revision(raw)
            data = json.loads(raw) if raw else {"channels": []}
            if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
                raise ValueError("channels must be a list")
        except (OSError, ValueError) as exc:
            self.publish({}, None, f"Invalid/unreadable config; existing routes unchanged: {type(exc).__name__}")
            return
        if observed != self.last_observed:
            self.retry_after.clear()
            self.cached_errors.clear()
            self.last_observed = observed
        names = [config.normalize_handle(c.get("name", "")).lower()
                 for c in data["channels"] if isinstance(c, dict)]
        processed = set()
        attempts = 0
        for raw_entry in data["channels"]:
            if not isinstance(raw_entry, dict):
                states[f"invalid-{len(states)}"] = {"state": "invalid", "detail": "Channel must be an object"}
                continue
            name = config.normalize_handle(raw_entry.get("name", ""))
            key = name.lower()
            processed.add(key)
            cleaned, errors, _ = config.validate_channel(raw_entry, set(self.routing),
                                                         self.builtins, self.categories)
            if names.count(key) != 1:
                errors.append("Duplicate channel names in config")
            if any(field in raw_entry and not isinstance(raw_entry[field], bool)
                   for field in ("enabled", "amazon_tag")):
                errors.append("enabled and amazon_tag must be booleans")
            if errors:
                states[name] = {"state": "invalid", "detail": "; ".join(errors)}
                continue
            if key in self.applied:
                same = policy(cleaned) == policy(self.applied[key])
                states[name] = {"state": "active" if same else "restart_required",
                                "detail": "Routing loaded; normal send safeguards apply" if same else
                                "Existing route kept unchanged. Approve a restart to apply this edit."}
                continue
            if not cleaned["enabled"]:
                states[name] = {"state": "disabled", "detail": "Not added to live routing"}
                continue
            wanted = ([s for s in self.routing if s not in self.tricks]
                      if cleaned["sources"] == ["*"] else cleaned["sources"])
            mapped = {v[0] for v in self.source_map.values()}
            if not wanted or any(s not in self.routing for s in wanted) or not any(s in mapped for s in wanted):
                states[name] = {"state": "waiting_sources", "detail": "No usable existing source, or new intake requested. Existing intake is not changed live."}
                continue
            if self.retry_after.get(key, 0) > time.time():
                states[name] = self.cached_errors[key]
                continue
            if time.time() < self.access_backoff_until:
                states[name] = {"state": "needs_attention", "detail": "Telegram rate limit: new access checks paused until its retry window ends"}
                continue
            if attempts >= 3:
                states[name] = {"state": "pending", "detail": "Waiting for the next access-check pass"}
                continue
            attempts += 1
            states[name] = {"state": "checking_access", "detail": "Checking channel access; no test message sent"}
            self.publish(states, observed)
            try:
                entity = await asyncio.wait_for(self.resolve_target(name), timeout=8)
                if not entity or not getattr(entity, "broadcast", False):
                    raise ValueError("Public broadcast channel unavailable; check the link and account access")
                if not (getattr(entity, "creator", False) or
                        getattr(getattr(entity, "admin_rights", None), "post_messages", False)):
                    raise ValueError("Posting permission missing: make the bot/session account an admin with Post Messages")
                entity_id = getattr(entity, "id", None)
                if not entity_id or any(getattr(e, "id", None) == entity_id for e in self.target_map.values()):
                    raise ValueError("This destination is already configured under another name")
                if any(abs(int(source_id)) in {entity_id, 1000000000000 + entity_id} for source_id in self.source_map):
                    raise ValueError("Destination is an intake source; refusing a repost loop")
                # Recheck after the network await: do not activate deleted/edited config.
                latest = self.path.read_bytes() if self.path.exists() else b""
                if revision(latest) != observed:
                    self.publish({}, revision(latest), "Config changed during access checks; retrying next pass")
                    return
                # Stage all routing without mutation. Commit below has no awaits.
                staged = {source: list(targets) for source, targets in self.routing.items()}
                staged_tags = set(self.tags)
                summary = config.apply_to_routing(staged, staged_tags, {"channels": [cleaned]},
                                                  self.builtins, self.tricks)
                if summary["added"] != [name]:
                    raise ValueError("Destination conflicts with existing routing")
                self.routing.clear()
                self.routing.update(staged)
                self.tags.clear()
                self.tags.update(staged_tags)
                cats = cleaned["categories"]
                if cats:
                    self.filters[key] = frozenset(cats)
                self.target_map[name] = entity
                self.applied[key] = cleaned
                for sid, (source, _) in list(self.source_map.items()):
                    self.source_map[sid] = (source, list(self.routing.get(source, [])))
                self.on_applied()
                states[name] = {"state": "active", "detail": "Access checked; eligible newly rendered jobs can route here. No history replay."}
                log.info("DASHBOARD LIVE ADD | @%s categories=%s", name, cats or "all")
            except Exception as exc:
                states[name] = {"state": "needs_attention", "detail": str(exc)[:240] or type(exc).__name__}
                self.cached_errors[key] = states[name]
                retry_seconds = max(30, int(getattr(exc, "seconds", 0) or 0))
                self.retry_after[key] = time.time() + retry_seconds
                if getattr(exc, "seconds", 0):
                    self.access_backoff_until = max(self.access_backoff_until, time.time() + retry_seconds)
        for key, old in self.applied.items():
            if key not in processed:
                states[old["name"]] = {"state": "restart_required", "detail": "Removed in saved config; existing route remains until an approved restart."}
        if all(s["state"] in {"active", "disabled"} for s in states.values()):
            self.applied_revision = observed
        self.publish(states, observed)

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.sync_once()
            except Exception:
                log.exception("Dashboard live reload failed; existing routes kept")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
