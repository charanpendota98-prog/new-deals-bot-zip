"""Watch a SOURCE channel: paste the link, its posts become our posts.

Covers the whole "channel link petti source add chesthe perfectgaa post cheyali"
path, offline and with no Telegram:

  * bestgaa/channels_config.py  - validate_watch / watch_entries /
    apply_watch_to_routing and the `watch` block surviving save+load
  * bestgaa/live_channels.py    - sync_watch: access proven with the session,
    route + source_map committed atomically, no join, no replay, refusals
    (our own channel, duplicate alias, missing destination) surfaced per row
  * dashboard/app.py            - GET/POST/PATCH/DELETE /api/watch over real HTTP

The end state asserted everywhere is the one that matters: SOURCE_TO_TARGETS
(the map process_job reads) contains the new source and the source_map entries
the NewMessage handler reads point at it.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))
from bestgaa import channels_config as cfg  # noqa: E402
from bestgaa.live_channels import LiveChannels  # noqa: E402
from dashboard import app as dash  # noqa: E402

FAILS: list[str] = []
OURS = {"SecretLootIndia1", "LootZoneIndia11", "PowerLoots1", "LootzoneTricks"}


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def entity(eid, title="Source", username="sourcechan", **kw):
    base = dict(id=eid, title=title, username=username, broadcast=True, left=False,
                creator=False, admin_rights=SimpleNamespace(post_messages=True))
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------- config rules --
def test_watch_config():
    print("\n== watch config: the link the operator pasted ==")
    ok = {"channel": "https://t.me/DealSourceX", "targets": ["*"]}
    cleaned, errors, warnings = cfg.validate_watch(ok, OURS)
    check("a public channel link validates to a bare name",
          not errors and cleaned["channel"] == "DealSourceX", str(errors))
    check("the default destination is the main feed", cleaned["targets"] == ["*"], cleaned)
    check("activation is described as verify-only (no join, no replay)",
          any("nothing is joined" in w and "no " in w for w in warnings), str(warnings))
    check("a fresh entry is enabled and timestamped",
          cleaned["enabled"] is True and cleaned["added_at"] > 0, cleaned)

    for form in ("@DealSourceX", "t.me/DealSourceX", "https://telegram.me/DealSourceX/"):
        c, e, _ = cfg.validate_watch({"channel": form}, OURS)
        check(f"{form!r} normalizes like the public link", not e and c["channel"] == "DealSourceX",
              str(e))

    _, errors, _ = cfg.validate_watch({"channel": "LootZoneIndia11", "targets": ["*"]}, OURS)
    check("our own channel is refused as a source (repost loop)", bool(errors), str(errors))
    _, errors, _ = cfg.validate_watch({"channel": "selfdeals", "targets": ["SelfDeals"]}, OURS)
    check("a channel cannot be its own destination", bool(errors), str(errors))
    _, errors, _ = cfg.validate_watch({"channel": "DealSourceX", "targets": ["*", "PowerLoots1"]}, OURS)
    check("main feed and named channels cannot be mixed", bool(errors), str(errors))
    _, errors, _ = cfg.validate_watch({"channel": "DealSourceX", "targets": ["NotOurChannel"]}, OURS)
    check("an unknown destination is refused when the bot reported its channels",
          bool(errors), str(errors))
    c, errors, warnings = cfg.validate_watch({"channel": "DealSourceX", "targets": ["PowerLoots1"]},
                                             set())
    check("with no bot report yet the target warns, never silently passes",
          not errors and any("cannot be confirmed" in w for w in warnings), str(warnings))
    _, errors, _ = cfg.validate_watch({"channel": "", "targets": ["*"]}, OURS)
    check("an empty channel is refused", bool(errors), str(errors))
    _, errors, _ = cfg.validate_watch({"channel": "https://evil.test/x", "targets": ["*"]}, OURS)
    check("a non-Telegram link is refused", bool(errors), str(errors))
    _, errors, _ = cfg.validate_watch({"channel": "DealSourceX", "targets": ["*"] * 41}, OURS)
    check("an absurd target list is refused", bool(errors), str(errors))
    _, errors, warnings = cfg.validate_watch({"channel": "https://t.me/+PrivateInvite"}, OURS)
    check("a private invite is accepted but warns that nothing is joined",
          not errors and any("nothing is joined" in w for w in warnings), (errors, warnings))
    c, _, _ = cfg.validate_watch({"channel": "DealSourceX", "note": "x" * 500}, OURS)
    check("a long note is truncated, never rejected", len(c["note"]) == 200, len(c["note"]))

    print("\n== watch config: routing merge ==")
    routes = {"deals": ["LootZoneIndia11"], "TrickSource": ["LootzoneTricks"]}
    summary = cfg.apply_watch_to_routing(
        routes, {"watch": [
            {"channel": "DealSourceX", "targets": ["*"], "enabled": True},
            {"channel": "NamedSource", "targets": ["PowerLoots1"], "enabled": True},
            {"channel": "OffSource", "targets": ["*"], "enabled": False},
            {"channel": "LootZoneIndia11", "targets": ["*"], "enabled": True},
            {"channel": "TrickOnly", "enabled": True,
             "targets": ["LootzoneTricks"]},
        ]}, ["LootZoneIndia11", "PowerLoots1"],
        tricks_sources={"TrickSource"})
    check("'*' expands to the bot's own main feed",
          routes.get("DealSourceX") == ["LootZoneIndia11", "PowerLoots1"], routes)
    check("named destinations are kept verbatim",
          routes.get("NamedSource") == ["PowerLoots1"], routes)
    check("a disabled source is not routed",
          "OffSource" not in routes, routes)
    check("a destination can never become a source",
          "LootZoneIndia11" not in routes, routes)
    check("a tricks destination is honoured only when named explicitly",
          routes.get("TrickOnly") == ["LootzoneTricks"], routes)
    check("the result lists what is watched",
          [row["channel"] for row in summary["watching"]]
          == ["DealSourceX", "NamedSource", "TrickOnly"],
          summary["watching"])
    check("the merge never raises on junk",
          cfg.apply_watch_to_routing({}, {"watch": ["nope", None, {}, "x"]},
                                     ["A"])["watching"] == [])

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "channels.json"
        cfg.save_channels({"channels": [], "watch": [
            {"channel": "DealSourceX", "targets": ["*"], "enabled": True,
             "note": "from the group", "added_at": 123.0}]}, path)
        loaded = cfg.load_channels(path)
        row = (loaded.get("watch") or [{}])[0]
        # v19 keeps the source's category policy on the row; an entry written
        # without it loads with an empty list, which means "every category".
        check("save/load round-trips the watch block field by field",
              loaded.get("watch") == [{"channel": "DealSourceX", "targets": ["*"],
                                       "categories": [], "enabled": True,
                                       "note": "from the group",
                                       "added_at": 123.0}], loaded.get("watch"))
        check("a watch entry keeps the operator's note", row.get("note") == "from the group", row)
        cfg.save_channels({"channels": []}, path)
        check("saving without a watch block writes an empty one, never a stale list",
              cfg.load_channels(path).get("watch") == [], cfg.load_channels(path))


# ------------------------------------------------------------- live activation --
class FakeBot:
    """One fake Telegram: a resolvable source, our destination entities."""

    def __init__(self, temp):
        self.path = Path(temp) / "channels.json"
        self.snap = Path(temp) / "routing_snapshot.json"
        self.routes = {"deals": ["LootZoneIndia11"], "TrickSource": ["LootzoneTricks"]}
        self.tags: set = set()
        self.sources = {123: ("deals", ["LootZoneIndia11"]),
                        -1000000000123: ("deals", ["LootZoneIndia11"])}
        self.targets = {"LootZoneIndia11": entity(999, "Loot", "lootzoneindia11"),
                        "PowerLoots1": entity(998, "Power", "powerloots1")}
        self.added: list = []
        self.calls: list = []
        self.rights: dict = {}
        self.manager = LiveChannels(
            self.routes, self.tags, {}, self.sources, self.targets, [],
            {"LootZoneIndia11", "PowerLoots1"}, {"electronics"}, {"TrickSource"},
            self.resolve_target, None, path=self.path, snapshot_file=self.snap,
            resolve_source=self.resolve_source,
            on_source_added=lambda name, ent, when: self.added.append((name, when)),
            builtin_sources={"deals", "TrickSource"}, source_details={},
            main_feed=["LootZoneIndia11", "PowerLoots1"])

    # Distinct real channels get distinct ids, like Telegram: the alias check
    # (two pasted links that are one channel) depends on it.
    IDS = {"DealSourceX": 555, "NamedSource": 556, "NoAccess": 557, "OffSource": 558}

    async def resolve_source(self, name):
        self.calls.append(("source", name))
        await asyncio.sleep(0)
        told = self.rights.get(name)
        if isinstance(told, Exception):
            raise told
        return told if told is not None else entity(self.IDS.get(name, 555),
                                                    "New deals", "newdealsX")

    async def resolve_target(self, name):
        self.calls.append(("target", name))
        told = self.rights.get(name)
        if isinstance(told, Exception):
            raise told
        return told if told is not None else entity(777, name, name.lower())

    def save(self, watch):
        cfg.save_channels({"channels": [], "watch": watch}, self.path)


def test_live_watch():
    print("\n== live watch: paste → prove access → route ==")

    async def run():
        with tempfile.TemporaryDirectory() as td:
            bot = FakeBot(td)
            bot.save([{"channel": "DealSourceX", "targets": ["*"], "enabled": True}])
            await bot.manager.sync_once()
            check("the new source now has a live route (what process_job reads)",
                  bot.routes.get("DealSourceX") == ["LootZoneIndia11", "PowerLoots1"], bot.routes)
            check("both id conventions the update handler can see are mapped",
                  bot.sources.get(555, ("",))[0] == "DealSourceX"
                  and bot.sources.get(-1000000000555, ("",))[0] == "DealSourceX",
                  {k: v[0] for k, v in bot.sources.items()})
            check("the source_map route matches the routing map",
                  bot.sources[555][1] == bot.routes["DealSourceX"], bot.sources[555])
            check("the bot session is the only thing contacted (no send, no join)",
                  bot.calls == [("source", "DealSourceX")], bot.calls)
            check("activation is stamped so old posts are never replayed",
                  len(bot.added) == 1 and bot.added[0][1] > 0, bot.added)
            state = json.loads(bot.snap.read_text())
            check("the dashboard sees state=watching for the new source",
                  state["watch_states"]["DealSourceX"]["state"] == "watching",
                  state.get("watch_states"))
            check("the destination list is published for the operator",
                  "LootZoneIndia11" in state["watch_states"]["DealSourceX"]["detail"], state)
            check("the snapshot carries watch_configured, main_feed and the source",
                  state["watch_configured"] == ["DealSourceX"]
                  and state["main_feed"] == ["LootZoneIndia11", "PowerLoots1"]
                  and "DealSourceX" in state["sources"], sorted(state)[:6])
            before = dict(bot.routes)
            await bot.manager.sync_once()
            check("a second pass is idempotent (no duplicate route, no second activation)",
                  bot.routes == before and len(bot.added) == 1, (bot.routes, bot.added))

            # A destination there is no permission for: refused, nothing routed.
            bot.save([{"channel": "NoAccess", "targets": ["*"], "enabled": True}])
            bot.rights["NoAccess"] = ValueError("the bot session is not a member - join it first")
            await bot.manager.sync_once()
            state = json.loads(bot.snap.read_text())
            check("a source the session is not in is refused with the reason",
                  state["watch_states"]["NoAccess"]["state"] == "needs_attention"
                  and "not a member" in state["watch_states"]["NoAccess"]["detail"],
                  state["watch_states"].get("NoAccess"))
            check("the refused source never reaches routing",
                  "NoAccess" not in bot.routes, bot.routes)

            # Our own channel pasted as a source: caught without a network call.
            bot.save([{"channel": "powerloots1", "targets": ["*"], "enabled": True}])
            bot.calls.clear()
            await bot.manager.sync_once()
            state = json.loads(bot.snap.read_text())
            check("one of our channels is refused as a source before any request",
                  state["watch_states"]["powerloots1"]["state"] == "needs_attention"
                  and ("OUR" in state["watch_states"]["powerloots1"]["detail"]
                       or "own" in state["watch_states"]["powerloots1"]["detail"]),
                  state["watch_states"].get("powerloots1"))
            check("the refusal costs no Telegram request",
                  not [c for c in bot.calls if c[0] == "source"], bot.calls)

            # A named destination the bot has not resolved yet.
            bot.rights.pop("Under99Loots", None)
            bot.save([{"channel": "NamedSource", "targets": ["Under99Loots"], "enabled": True}])
            await bot.manager.sync_once()
            check("a named destination is resolved before the route is committed",
                  ("target", "Under99Loots") in bot.calls and "Under99Loots" in bot.targets,
                  bot.calls)
            check("the route points only at the named destination",
                  bot.routes.get("NamedSource") == ["Under99Loots"], bot.routes)
            bot.save([{"channel": "BadDest", "targets": ["GoneChannel"], "enabled": True}])
            bot.rights["GoneChannel"] = ValueError("destination is not reachable")
            await bot.manager.sync_once()
            check("an unreachable destination blocks the route with a reason",
                  "BadDest" not in bot.routes and
                  json.loads(bot.snap.read_text())["watch_states"]["BadDest"]["state"]
                  == "needs_attention", bot.routes)

            # Disabled rows are saved intent only.
            bot.save([{"channel": "OffSource", "targets": ["*"], "enabled": False}])
            await bot.manager.sync_once()
            states = json.loads(bot.snap.read_text())["watch_states"]
            check("a disabled source is reported as disabled, not watched",
                  states["OffSource"]["state"] == "disabled", states)
            check("a disabled source gets no route", "OffSource" not in bot.routes, bot.routes)

            # Removing the row clears the state (the route is left to the next boot).
            bot.save([])
            await bot.manager.sync_once()
            check("a removed watch row disappears from the published states",
                  json.loads(bot.snap.read_text())["watch_states"] == {},
                  json.loads(bot.snap.read_text())["watch_states"])

            # A build without a resolver must say so instead of pretending.
            bot.save([{"channel": "DealSourceX", "targets": ["*"]}])
            old = bot.manager.resolve_source
            bot.manager.resolve_source = None
            await bot.manager.sync_once()
            check("an old bot build reports restart_required rather than silence",
                  json.loads(bot.snap.read_text())["watch_states"]["DealSourceX"]["state"]
                  == "restart_required", json.loads(bot.snap.read_text())["watch_states"])
            bot.manager.resolve_source = old

    asyncio.run(run())


# --------------------------------------------------------------- the dashboard --
async def _started_app():
    runner = web.AppRunner(dash.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server else []
    port = sockets[0].getsockname()[1] if sockets else 8080
    return runner, f"http://127.0.0.1:{port}"


def test_watch_api():
    print("\n== dashboard API: paste a link in the browser ==")

    async def run():
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            saved = {k: os.environ.get(k) for k in
                     ("BOT_DB_PATH", "CHANNELS_JSON_PATH", "DASHBOARD_TOKEN",
                      "DASHBOARD_INSECURE")}
            os.environ["BOT_DB_PATH"] = str(tdp / "dash.sqlite3")
            os.environ["CHANNELS_JSON_PATH"] = str(tdp / "channels.json")
            os.environ["DASHBOARD_TOKEN"] = "test-token-123"
            os.environ.pop("DASHBOARD_INSECURE", None)
            (tdp / "routing_snapshot.json").write_text(json.dumps({
                "live_reload": True, "heartbeat_at": 9e12,
                "targets": sorted(OURS), "main_feed": ["LootZoneIndia11", "PowerLoots1"],
                "watch_states": {"DealSourceX": {"state": "watching",
                                                 "detail": "Live: new posts route to LootZoneIndia11"}},
                "source_targets": {"deals": ["LootZoneIndia11"]},
            }), encoding="utf-8")
            cfg.save_channels({"channels": [], "watch": [
                {"channel": "DealSourceX", "targets": ["*"], "enabled": True}]},
                tdp / "channels.json")
            runner, base = await _started_app()
            heads = {"X-Dashboard-Token": "test-token-123"}
            try:
                async with aiohttp.ClientSession() as session:
                    async def call(method, path, body=None, headers=None):
                        async with session.request(method, base + path, json=body,
                                                   headers=headers if headers is not None else heads) as r:
                            return r.status, await r.json(content_type=None)

                    status, _ = await call("GET", "/api/watch", headers={})
                    check("the watch list needs the token", status == 401, status)

                    status, data = await call("GET", "/api/watch")
                    check("the saved source is listed with its live state",
                          status == 200 and data["watch"][0]["channel"] == "DealSourceX"
                          and data["watch"][0]["state"] == "watching", data.get("watch"))
                    check("the picker is built from the bot's own reported channels",
                          set(data["catalog"]["targets"]) >= OURS, data.get("catalog"))
                    check("the main feed option comes from the bot",
                          data["catalog"]["main_feed"] == ["LootZoneIndia11", "PowerLoots1"],
                          data.get("catalog"))

                    status, data = await call("POST", "/api/watch",
                                              {"channel": "https://t.me/FreshSource9"})
                    check("pasting a link with no target defaults to the main feed",
                          status == 200 and data["source"]["targets"] == ["*"], data)
                    check("the response says what will happen and where it posts",
                          "Nothing is joined" in data["next"]
                          and "LootZoneIndia11" in data["next"], data.get("next"))
                    saved_watch = cfg.load_channels(tdp / "channels.json")["watch"]
                    check("the new source is persisted, not just returned",
                          [r["channel"] for r in saved_watch] == ["DealSourceX", "FreshSource9"],
                          saved_watch)

                    status, data = await call("POST", "/api/watch",
                                              {"channel": "https://t.me/FreshSource9"})
                    check("the same source cannot be added twice", status == 400, (status, data))
                    status, data = await call("POST", "/api/watch",
                                              {"channel": "https://t.me/LootZoneIndia11"})
                    check("one of our channels is refused as a source", status == 400, (status, data))
                    status, data = await call("POST", "/api/watch",
                                              {"channel": "https://t.me/FreshSource9",
                                               "targets": ["NopeDeals"]})
                    check("an unknown destination is refused", status == 400, (status, data))
                    status, data = await call("POST", "/api/watch",
                                              {"channel": "https://t.me/FreshSource9"},
                                              headers={**heads, "If-Match": "stale"})
                    check("a stale form is refused instead of overwriting", status == 409,
                          (status, data))

                    status, data = await call("PATCH", "/api/watch/FreshSource9",
                                              {"enabled": False})
                    check("a source can be paused without deleting it",
                          status == 200
                          and cfg.load_channels(tdp / "channels.json")["watch"][1]["enabled"] is False,
                          data)
                    status, data = await call("PATCH", "/api/watch/DealSourceX",
                                              {"targets": ["PowerLoots1"]})
                    check("the destination can be changed to a named channel",
                          status == 200
                          and cfg.load_channels(tdp / "channels.json")["watch"][0]["targets"]
                          == ["PowerLoots1"], data)
                    status, data = await call("PATCH", "/api/watch/DealSourceX",
                                              {"targets": ["*", "PowerLoots1"]})
                    check("mixing the main feed with named channels is refused",
                          status == 400, (status, data))
                    status, data = await call("PATCH", "/api/watch/Ghost", {"enabled": True})
                    check("editing a source that is not there is a 404", status == 404, status)

                    status, data = await call("DELETE", "/api/watch/FreshSource9")
                    check("a source can be removed",
                          status == 200 and [r["channel"] for r in
                                             cfg.load_channels(tdp / "channels.json")["watch"]]
                          == ["DealSourceX"], data)
                    check("removal is recorded so the operator sees the change is pending",
                          dash.pending_restart() or
                          cfg.load_channels(tdp / "channels.json")["watch"] ==
                          [{"channel": "DealSourceX", "targets": ["PowerLoots1"],
                            "enabled": True, "note": "", "added_at":
                            cfg.load_channels(tdp / "channels.json")["watch"][0]["added_at"]}],
                          "config state")
                    status, data = await call("DELETE", "/api/watch/Ghost")
                    check("removing a source that is not there is a 404", status == 404, status)
            finally:
                await runner.cleanup()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    asyncio.run(run())


def test_ui_surface():
    print("\n== dashboard page: the paste-a-link card exists ==")
    html = dash.UI_HTML
    check("the page has a watch-source card", 'id="watchsource"' in html)
    check("it offers a link field, a target picker and a main-feed default",
          all(token in html for token in
              ('id="watchlink"', 'id="watchtargets"', 'id="watchmain"', 'id="watchnote"')))
    check("it calls the API without inventing a token",
          "/api/watch" in html and "loadWatch()" in html)
    check("the card explains that nothing is joined and no history is replayed",
          "Nothing is joined automatically" in html and "old post is replayed" in html)
    check("every watched row shows the bot's own state and reason",
          "data-watch" in html and "row.detail" in html)


if __name__ == "__main__":
    test_watch_config()
    test_live_watch()
    test_watch_api()
    test_ui_surface()
    print("\n" + ("WATCH SOURCE TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_watch_source: all checks PASS"))
    sys.exit(1 if FAILS else 0)
