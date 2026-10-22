"""Append-only live activation for dashboard destinations.

No service restarts, history replay, automatic joins or merchant requests.
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
                 resolve_target, on_applied=lambda: None, path=None, snapshot_file=None,
                 resolve_source=None, on_source_added=lambda *args: None,
                 builtin_sources=None, source_details=None, main_feed=None):
        self.routing, self.tags, self.filters = routing, tag_targets, category_filters
        self.source_map, self.target_map = source_map, target_map
        self.builtin_sources = set(builtin_sources) if builtin_sources is not None else set(routing)
        self.resolve_source = resolve_source
        self.on_source_added = on_source_added
        self.source_details = source_details if source_details is not None else {}
        # Where a watched source's posts go when the dashboard says "*": the bot's
        # own main product feed, resolved at runtime (never frozen in config).
        self.main_feed = [str(t) for t in (main_feed or [])]
        self.source_checks = {}
        self.watch_states: dict[str, dict] = {}
        self.applied = {}
        for channel in initial_channels:
            if channel.get("enabled", True):
                channel = dict(channel, sources=config.source_keys(channel["sources"], routing))
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
        self.last_data: dict = {"channels": []}

    def publish(self, states, observed, error=""):
        try:
            base = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
            if not isinstance(base, dict):
                base = {}
        except (OSError, ValueError):
            base = {}
        base.update(live_reload=True, live_sources=self.resolve_source is not None, heartbeat_at=time.time(),
                    started_at=base.get("started_at", self.started_at),
                    live_channels=states, live_error=error,
                    observed_revision=observed, applied_revision=self.applied_revision,
                    sources=sorted(self.routing),
                    main_sources=sorted(s for s in self.builtin_sources if s not in self.tricks),
                    source_details=self.source_details,
                    dashboard_added=sorted(c["name"] for c in self.applied.values()),
                    watch_states=self.watch_states,
                    watch_configured=sorted(row.get("channel", "")
                                            for row in config.watch_entries(self.last_data)),
                    main_feed=self.main_feed,
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
        self.last_data = data
        if observed != self.last_observed:
            self.source_checks.clear()
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
            repair_existing = False
            if key in self.applied:
                same = policy(cleaned) == policy(self.applied[key])
                if not same:
                    states[name] = {"state": "restart_required", "detail": "Existing route kept unchanged. Approve a restart to apply this edit."}
                    continue
                current = self.target_map.get(name)
                wanted_now = self.builtin_sources - self.tricks if cleaned["sources"] == ["*"] else set(cleaned["sources"])
                mapped_now = {entry[0] for entry in self.source_map.values()}
                source_ready = bool(wanted_now & mapped_now) if cleaned["sources"] == ["*"] else wanted_now <= mapped_now
                access_ready = current and (getattr(current, "creator", False) or getattr(getattr(current, "admin_rights", None), "post_messages", False))
                if source_ready and access_ready:
                    states[name] = {"state": "active", "detail": "Routing loaded; normal send safeguards apply"}
                    continue
                repair_existing = True  # same saved policy; retry access, never change its rules
            if not cleaned["enabled"]:
                states[name] = {"state": "disabled", "detail": "Not added to live routing"}
                continue
            wanted = ([s for s in self.builtin_sources if s not in self.tricks]
                      if cleaned["sources"] == ["*"] else cleaned["sources"])
            wanted = sorted(wanted)
            mapped = {v[0] for v in self.source_map.values()}
            missing = [s for s in wanted if s not in mapped]
            if not wanted or (self.resolve_source is None and (any(s not in self.routing for s in wanted) or not any(s in mapped for s in wanted))):
                states[name] = {"state": "waiting_sources", "detail": "Source access is not confirmed. Join using the bot session account; existing routes stay unchanged."}
                continue
            # A wildcard covers only the original main feeds, not channel-local
            # custom sources. Existing unavailable feeds retain their recovery flow.
            if cleaned["sources"] == ["*"] and any(s in mapped for s in wanted):
                missing = []
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
                if not entity_id or any(getattr(e, "id", None) == entity_id for target_name, e in self.target_map.items()
                                            if not (repair_existing and target_name == name)):
                    raise ValueError("This destination is already configured under another name")
                if any(abs(int(source_id)) in {entity_id, 1000000000000 + entity_id} for source_id in self.source_map):
                    raise ValueError("Destination is an intake source; refusing a repost loop")
                staged_sources = {}
                deferred = False
                for source in missing:
                    cached = self.source_checks.get(source)
                    if cached and time.time() - cached[1] < 1800:
                        source_entity = cached[0]
                    else:
                        if attempts >= 3:
                            states[name] = {"state": "checking_sources", "detail": f"Checking selected sources in batches; next: {source}. No routes changed yet."}
                            deferred = True
                            break
                        attempts += 1
                        source_entity = await asyncio.wait_for(self.resolve_source(source), timeout=8)
                        if (not source_entity or not getattr(source_entity, "broadcast", False)
                                or getattr(source_entity, "left", False)):
                            raise ValueError(f"Source {source}: join this broadcast channel with the bot session account first")
                        self.source_checks[source] = (source_entity, time.time())
                    sid = getattr(source_entity, "id", None)
                    if not sid or sid == entity_id or any(getattr(e, "id", None) == sid for e in self.target_map.values()):
                        raise ValueError(f"Source {source} is a posting destination; refusing a loop")
                    aliases = {entry[0] for key, entry in self.source_map.items()
                               if abs(int(key)) in {sid, 1000000000000 + sid}}
                    if aliases and source not in aliases:
                        raise ValueError(f"Source {source} is already configured as {sorted(aliases)[0]}; select that source instead")
                    if any(getattr(e, "id", None) == sid for e in staged_sources.values()):
                        raise ValueError("Two selected source links identify the same channel; keep just one")
                    staged_sources[source] = source_entity
                if deferred:
                    continue
                # Recheck after the network await: do not activate deleted/edited config.
                latest = self.path.read_bytes() if self.path.exists() else b""
                if revision(latest) != observed:
                    self.publish({}, revision(latest), "Config changed during access checks; retrying next pass")
                    return
                # Stage all routing without mutation. Commit below has no awaits.
                staged = {source: list(targets) for source, targets in self.routing.items()}
                staged_tags = set(self.tags)
                summary = ({"added": [name]} if repair_existing else
                           config.apply_to_routing(staged, staged_tags, {"channels": [cleaned]},
                                                   self.builtins, self.tricks,
                                                   main_sources=self.builtin_sources))
                if summary["added"] != [name]:
                    raise ValueError("Destination conflicts with existing routing")
                activated = time.time()
                for source, source_entity in staged_sources.items():
                    sid = int(source_entity.id)
                    for source_id in (sid, -(1000000000000 + sid)):
                        self.source_map[source_id] = (source, list(staged.get(source, [])))
                    self.source_details[source] = {"title": str(getattr(source_entity, "title", "") or source),
                                                   "state": "ready", "checked_at": self.source_checks[source][1],
                                                   "activated_at": activated}
                    self.on_source_added(source, source_entity, activated)
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
        await self.sync_watch(data, observed)
        for key, old in self.applied.items():
            if key not in processed:
                states[old["name"]] = {"state": "restart_required", "detail": "Removed in saved config; existing route remains until an approved restart."}
        if all(s["state"] in {"active", "disabled"} for s in states.values()):
            self.applied_revision = observed
        self.publish(states, observed)

    async def sync_watch(self, data, observed):
        """Activate watched SOURCE channels - the "paste a link, watch it" flow.

        Same rules as a destination: nothing is joined, nothing is replayed, and
        a source that is not a broadcast channel the session is already in is
        refused with the reason. Once access is proven the route is attached to
        the live `source_map`, which is exactly what the message handler reads -
        so the next post from that channel lands in our channels, converted.
        """
        rows = config.watch_entries(data)
        seen: set[str] = set()
        for row in rows:
            channel = str(row.get("channel", "")).strip()
            if not channel:
                continue
            key = channel.lower()
            seen.add(key)
            if not row.get("enabled", True):
                self.watch_states[channel] = {"state": "disabled",
                                              "detail": "Saved but not watched"}
                continue
            if self.resolve_source is None or self.on_source_added is None:
                self.watch_states[channel] = {
                    "state": "restart_required",
                    "detail": "This bot build cannot watch new sources live; "
                              "an approved restart applies it"}
                continue
            mapped = {str(entry[0]).lower() for entry in self.source_map.values()}
            if key in mapped:
                wanted = self._watch_targets(row)
                current = list(self.routing.get(channel, []))
                if all(any(str(t).lower() == w.lower() for t in current) for w in wanted):
                    self.watch_states[channel] = {
                        "state": "watching",
                        "detail": f"Live: posts route to {', '.join(current) or 'nowhere'}"}
                    continue
            retry = self.retry_after.get(f"watch:{key}", 0)
            if retry > time.time():
                self.watch_states[channel] = self.cached_errors.get(
                    f"watch:{key}", {"state": "needs_attention", "detail": "Retrying shortly"})
                continue
            self.watch_states[channel] = {"state": "checking_access",
                                          "detail": "Checking access with the bot session; "
                                                    "no message is sent"}
            try:
                # A name that is already one of OUR destinations is a repost loop,
                # whatever the network says: catch it before spending a request.
                if channel.lower() in {str(name).lower() for name in self.target_map} | {
                        str(name).lower() for name in self.builtins}:
                    raise ValueError("this is one of OUR posting channels - "
                                     "a destination cannot be its own source")
                entity = await asyncio.wait_for(self.resolve_source(channel), timeout=10)
                if not entity or not getattr(entity, "broadcast", False):
                    raise ValueError("not a Telegram broadcast channel (or not reachable)")
                if getattr(entity, "left", False):
                    raise ValueError("the bot session is not a member - join it first")
                sid = abs(int(getattr(entity, "id", 0) or 0))
                if not sid:
                    raise ValueError("Telegram returned no channel id")
                if any(getattr(e, "id", None) == sid for e in self.target_map.values()):
                    raise ValueError("this is one of OUR posting channels - "
                                     "a destination cannot be its own source")
                # Two pasted links that lead to the same channel would fight over
                # one route and double-post; keep the first and say so.
                for other_id, (other_name, _others) in self.source_map.items():
                    if abs(int(other_id)) in {sid, 1000000000000 + sid} \
                            and str(other_name).lower() != channel.lower():
                        raise ValueError(f"this channel is already watched as {other_name}")
                wanted = self._watch_targets(row)
                if not wanted:
                    raise ValueError("no destination channels are configured for this source")
                # Every destination must be a real entity in the live target map,
                # otherwise the post is queued and then dropped at send time. A
                # named destination that the bot has not resolved yet is checked
                # here (before the commit) with the same session.
                for target in wanted:
                    if target in self.target_map:
                        continue
                    if self.resolve_target is None:
                        raise ValueError(f"destination @{target} is not resolved by this bot build")
                    target_entity = await asyncio.wait_for(self.resolve_target(target), timeout=8)
                    if not target_entity or not getattr(target_entity, "broadcast", False):
                        raise ValueError(f"destination @{target} is not a reachable broadcast channel")
                    if not (getattr(target_entity, "creator", False) or
                            getattr(getattr(target_entity, "admin_rights", None), "post_messages", False)):
                        raise ValueError(f"destination @{target} is missing Post Messages permission")
                    self.target_map[target] = target_entity
                # Commit with no awaits: route + source_map in one step.
                targets = self.routing.setdefault(channel, [])
                for target in wanted:
                    if all(str(t).lower() != target.lower() for t in targets):
                        targets.append(target)
                for source_id in (sid, -(1000000000000 + sid)):
                    self.source_map[source_id] = (channel, list(targets))
                self.source_details[channel] = {
                    "title": str(getattr(entity, "title", "") or channel),
                    "state": "ready", "checked_at": time.time(),
                    "activated_at": time.time(), "watched": True}
                self.on_source_added(channel, entity, time.time())
                self.watch_states[channel] = {
                    "state": "watching",
                    "detail": f"Live: new posts from @{getattr(entity, 'username', channel)} "
                              f"route to {', '.join(targets)}"}
                log.info("DASHBOARD SOURCE WATCH | %s -> %s", channel, ", ".join(targets))
            except Exception as exc:  # noqa: BLE001 - one bad source is one row
                detail = str(exc)[:240] or type(exc).__name__
                self.watch_states[channel] = {"state": "needs_attention", "detail": detail}
                self.cached_errors[f"watch:{key}"] = self.watch_states[channel]
                self.retry_after[f"watch:{key}"] = time.time() + max(
                    30, int(getattr(exc, "seconds", 0) or 0))
                log.warning("DASHBOARD SOURCE WATCH failed | %s: %s", channel, detail)
        for channel in list(self.watch_states):
            if channel.lower() not in seen:
                self.watch_states.pop(channel, None)

    def _watch_targets(self, row) -> list[str]:
        """The destinations for one watched source ("*" = the main product feed)."""
        configured = [str(t) for t in (row.get("targets") or ["*"])]
        if configured == ["*"]:
            return [t for t in self.main_feed if t]
        return [t for t in configured if t]

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
