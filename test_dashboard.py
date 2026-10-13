"""Operator dashboard: channels.json config + HTTP API.

Covers bestgaa/channels_config.py (validate/save/merge) and dashboard/app.py
(auth, status, channels CRUD, pending-restart, recent, audit, restart) over
real HTTP against an isolated temp database. No Telegram, no network.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))
import channels_config  # noqa: E402
import main_bot_new as bot  # noqa: E402
from dashboard import app as dash  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def test_config_manager():
    print("\n== config: validate / save / merge ==")
    known = {"dealsvelocity", "deals"}
    existing = {"LootZoneIndia11", "PowerLoots1"}

    cleaned, errors, warnings = channels_config.validate_channel(
        {"name": "@MyNewDeals", "sources": ["dealsvelocity"], "amazon_tag": False}, known, existing)
    check("a good channel validates (@ stripped)", not errors and cleaned["name"] == "MyNewDeals",
          str(errors))

    _, errors, _ = channels_config.validate_channel({"name": "ab", "sources": ["deals"]}, known, existing)
    check("a short name is rejected", bool(errors), str(errors))
    _, errors, _ = channels_config.validate_channel(
        {"name": "https://t.me/+abc", "sources": ["deals"]}, known, existing)
    check("an invite link is not a channel name", bool(errors), str(errors))
    _, errors, _ = channels_config.validate_channel({"name": "FreshDeals9", "sources": []}, known, existing)
    check("an empty source list is rejected", bool(errors), str(errors))
    _, errors, _ = channels_config.validate_channel(
        {"name": "LootZoneIndia11", "sources": ["deals"]}, known, existing)
    check("an existing channel name is rejected", bool(errors), str(errors))
    cleaned, errors, warnings = channels_config.validate_channel(
        {"name": "FreshDeals9", "sources": ["brandnewsource"], "amazon_tag": True}, known, existing)
    check("an unknown source warns but passes", not errors and bool(warnings), str(errors))
    check("the Amazon-tag opt-in carries its Associates warning",
          any("Associates" in w for w in warnings), str(warnings))

    cats = {"electronics", "fashion"}
    cleaned_cat, errors, _ = channels_config.validate_channel(
        {"name": "ElectroShelf", "sources": ["*"], "categories": ["Electronics"]},
        known, existing, cats)
    check("a category shelf validates (key lowercased)",
          not errors and cleaned_cat["categories"] == ["electronics"], str(errors))
    _, errors, _ = channels_config.validate_channel(
        {"name": "BadShelf", "sources": ["*"], "categories": ["nope"]}, known, existing, cats)
    check("an unknown category is rejected when keys are known", bool(errors), str(errors))
    _, errors, warnings = channels_config.validate_channel(
        {"name": "EarlyShelf", "sources": ["*"], "categories": ["electronics"]}, known, existing)
    check("without reported keys the category warns but passes",
          not errors and any("not confirmed" in w for w in warnings), str(errors))

    for bad in (None, False, 42, {}, "", [""], [None], ["electronics", ""]):
        entry = {"name": "UnsafeShelf", "sources": ["*"], "categories": bad}
        _, errors, _ = channels_config.validate_channel(entry, known, existing, cats)
        check("malformed shelf filter is rejected", bool(errors), repr(bad))
        routes = {"deals": ["LootZoneIndia11"]}
        summary = channels_config.apply_to_routing(routes, set(), {"channels": [entry]})
        check("malformed shelf never partially joins routing",
              routes == {"deals": ["LootZoneIndia11"]} and not summary["added"], repr(bad))
        with tempfile.TemporaryDirectory() as bad_dir:
            config = Path(bad_dir) / "channels.json"
            config.write_text(json.dumps({"channels": [entry]}))
            check("malformed stored shelf never becomes unrestricted",
                  not channels_config.load_channels(config)["channels"], repr(bad))

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "channels.json"
        data = {"version": 1, "channels": [cleaned]}
        channels_config.save_channels(data, path)
        check("save/load round-trips", channels_config.load_channels(path) == data)
        (Path(td) / "bad.json").write_text("{nope", encoding="utf-8")
        check("a corrupt file loads as empty",
              channels_config.load_channels(Path(td) / "bad.json") == {"version": 1, "channels": []})

    routing = {"dealsvelocity": ["LootZoneIndia11"], "deals": ["LootZoneIndia11"],
               "TrickXpert": ["LootzoneTricks"]}
    tags: set = set()
    summary = channels_config.apply_to_routing(
        routing, tags,
        {"version": 1, "channels": [
            {"name": "StarDeals", "sources": ["*"], "amazon_tag": True, "enabled": True},
            {"name": "OffDeals", "sources": ["deals"], "amazon_tag": False, "enabled": False},
            {"name": "LootZoneIndia11", "sources": ["deals"], "enabled": True},
            {"name": "??", "sources": ["deals"], "enabled": True},
        ]},
        existing_targets={"LootZoneIndia11"}, tricks_sources={"TrickXpert"})
    check("'*' routes every non-Tricks source",
          "StarDeals" in routing["dealsvelocity"] and "StarDeals" in routing["deals"]
          and "StarDeals" not in routing["TrickXpert"], str(routing))
    check("the tag opt-in joins the tag set", "StarDeals" in tags, str(tags))
    check("disabled + colliding + malformed entries are skipped, never fatal",
          sorted(summary["skipped"]) == ["??", "LootZoneIndia11", "OffDeals"]
          and summary["added"] == ["StarDeals"], str(summary))

    routing2 = {"deals": ["LootZoneIndia11"]}
    summary2 = channels_config.apply_to_routing(routing2, set(), {"version": 1, "channels": [
        {"name": "ElectroShelf", "sources": ["deals"], "categories": ["electronics"],
         "enabled": True}]})
    check("apply_to_routing reports the shelf filter",
          summary2["category_filters"] == {"ElectroShelf": ["electronics"]}
          and "ElectroShelf" in routing2["deals"], str(summary2))


async def _started_app():
    runner = web.AppRunner(dash.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server else []
    port = sockets[0].getsockname()[1] if sockets else 8080
    return runner, f"http://127.0.0.1:{port}"


def test_http_api():
    print("\n== HTTP API: auth, status, channels, recent, audit, restart ==")

    async def run():
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            db = tdp / "dash.sqlite3"
            store = bot.Store(db)
            now = time.time()
            post = "Demo deal \u20b9999\nhttps://bit.ly/dash-owned-1"
            store.conn.execute(
                "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
                "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (1, -1, 1, "deals", "done", 1, "", now - 60, "-1", post, '["LootZoneIndia11"]'))
            store.conn.execute("INSERT INTO deliveries VALUES(1,'LootZoneIndia11','sent',1,1,'')")
            store.conn.execute(
                "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
                "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (2, -2, 2, "deals", "pending", 0, "", now - 30, "-2", None, '[]'))
            store.conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)",
                               ("https://bitli.in/dash-src-1", "https://bit.ly/dash-owned-1",
                                "https://www.flipkart.com/x/p/itmD1", "PID:D1", now))
            store.conn.execute(
                "INSERT INTO owned_link_proofs(affiliate_url,destination_url,proof_type,"
                "publisher_id,created_at) VALUES(?,?,?,?,?)",
                ("https://bit.ly/dash-owned-1", "https://ekaro.in/enkr-dash-1",
                 "earnkaro_api", "5478322", now))
            store.conn.commit()
            store.conn.close()

            saved = {key: os.environ.get(key) for key in
                     ("BOT_DB_PATH", "CHANNELS_JSON_PATH", "DASHBOARD_TOKEN",
                      "DASHBOARD_INSECURE", "DASHBOARD_RESTART_CMD", "EARNKARO_PUBLISHER_ID")}
            os.environ["BOT_DB_PATH"] = str(db)
            os.environ["CHANNELS_JSON_PATH"] = str(tdp / "channels.json")
            os.environ["DASHBOARD_TOKEN"] = "test-token-123"
            os.environ.pop("DASHBOARD_INSECURE", None)
            os.environ["DASHBOARD_RESTART_CMD"] = "true"
            os.environ["EARNKARO_PUBLISHER_ID"] = "5478322"
            (tdp / "routing_snapshot.json").write_text(json.dumps({
                "started_at": now - 100, "sources": ["deals", "dealsvelocity"],
                "targets": ["LootZoneIndia11", "PowerLoots1"], "dashboard_added": [],
                "categories": {"electronics": "Electronics", "fashion": "Fashion"},
                "channels_mtime": 0}), encoding="utf-8")
            runner = None
            try:
                runner, base = await _started_app()
                timeout = aiohttp.ClientTimeout(total=20)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async def call(method, path, token=True, **kw):
                        headers = {"X-Dashboard-Token": "test-token-123"} if token else {}
                        async with sess.request(method, base + path, headers=headers, **kw) as resp:
                            try:
                                body = await resp.json()
                            except (ValueError, aiohttp.ContentTypeError):
                                body = {}
                            return resp.status, body

                    status, _ = await call("GET", "/api/status", token=False)
                    check("no token -> 401", status == 401, str(status))
                    status, body = await call("POST", "/api/category-preview", token=False,
                                              json={"text": "Cotton kurti"})
                    check("category preview requires auth", status == 401)
                    status, body = await call("POST", "/api/category-preview",
                                              json={"text": "Cotton kurti"})
                    check("preview explains clothing evidence without sending",
                          status == 200 and body.get("category") == "fashion"
                          and body.get("reason") == "matched" and bool(body.get("evidence")))
                    status, body = await call("POST", "/api/category-preview",
                                              json={"text": "Shoes and shoe rack"})
                    check("preview withholds mixed posts", status == 200
                          and body.get("category") is None
                          and body.get("reason") == "mixed_or_conflicting")
                    for bad_body in ([], {}, {"text": "x" * 12001},
                                     {"text": "earbuds", "links": "not-a-list"}):
                        status, body = await call("POST", "/api/category-preview", json=bad_body)
                        check("invalid preview payload returns 400", status == 400)

                    status, body = await call("GET", "/api/status")
                    check("status reports queue + today's sends",
                          status == 200 and body.get("queue", {}).get("done") == 1
                          and body.get("today_sends") == 1, str(body)[:200])
                    check("no restart pending on a fresh checkout",
                          body.get("pending_restart") is False, str(body.get("pending_restart")))

                    status, body = await call("GET", "/api/channels")
                    check("channels lists builtin + sources + categories from the snapshot",
                          status == 200 and "LootZoneIndia11" in body.get("builtin", [])
                          and "deals" in body.get("sources", [])
                          and body.get("categories", {}).get("electronics") == "Electronics",
                          str(body)[:200])

                    check("installed taxonomy is available despite an older bot snapshot",
                          body.get("category_rules_version") == 2
                          and body.get("categories", {}).get("accessories") == "Bags & Accessories"
                          and body.get("categories", {}).get("fashion") == "Clothing")

                    status, body = await call("POST", "/api/channels",
                                              json={"name": "ab", "sources": ["deals"]})
                    check("a bad channel is rejected with errors",
                          status == 400 and bool(body.get("errors")), str(body)[:200])
                    status, body = await call("POST", "/api/channels",
                                              json={"name": "MyNewDeals", "sources": ["*"],
                                                    "note": "dashboard test"})
                    check("a good channel is added and flagged pending-restart",
                          status == 200 and body["channel"]["name"] == "MyNewDeals"
                          and body.get("pending_restart") is True, str(body)[:200])
                    status, body = await call("GET", "/api/status")
                    check("the pending flag survives across calls", body.get("pending_restart") is True)

                    # The bot restarts and reports the new mtime: pending clears itself.
                    mtime = os.path.getmtime(tdp / "channels.json")
                    snap = json.loads((tdp / "routing_snapshot.json").read_text(encoding="utf-8"))
                    snap["channels_mtime"] = mtime
                    (tdp / "routing_snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
                    status, body = await call("GET", "/api/status")
                    check("pending clears once the bot reports the change applied",
                          body.get("pending_restart") is False, str(body.get("pending_restart")))

                    status, body = await call("PATCH", "/api/channels/MyNewDeals",
                                              json={"enabled": False})
                    check("disable flips the flag and re-pends",
                          status == 200 and body["channel"]["enabled"] is False
                          and body.get("pending_restart") is True, str(body)[:200])
                    status, body = await call("DELETE", "/api/channels/MyNewDeals")
                    check("delete removes it", status == 200 and body.get("deleted") == "mynewdeals",
                          str(body)[:200])
                    status, body = await call("DELETE", "/api/channels/MyNewDeals")
                    check("deleting twice 404s", status == 404, str(status))

                    status, body = await call("POST", "/api/channels",
                                              json={"name": "ElectroShelf", "sources": ["*"],
                                                    "categories": ["electronics"]})
                    check("a category shelf is added with its filter",
                          status == 200 and body["channel"]["categories"] == ["electronics"],
                          str(body)[:200])
                    status, body = await call("POST", "/api/channels",
                                              json={"name": "BadShelf", "sources": ["*"],
                                                    "categories": ["nope"]})
                    check("an unknown category is rejected by the API",
                          status == 400 and bool(body.get("errors")), str(body)[:200])
                    for bad in (False, None, 42, [""]):
                        status, body = await call("PATCH", "/api/channels/ElectroShelf",
                                                  json={"categories": bad})
                        check("API rejects malformed filters without clearing the shelf", status == 400)
                    status, body = await call("GET", "/api/channels")
                    shelf = next(c for c in body["dashboard"] if c["name"] == "ElectroShelf")
                    check("rejected edits preserve the old electronics filter",
                          shelf["categories"] == ["electronics"])
                    status, body = await call("PATCH", "/api/channels/ElectroShelf",
                                              json={"categories": ["fashion"]})
                    check("the shelf filter is editable",
                          status == 200 and body["channel"]["categories"] == ["fashion"],
                          str(body)[:200])

                    status, body = await call("GET", "/api/recent?limit=5")
                    jobs = body.get("jobs", [])
                    check("recent shows jobs with send counts",
                          status == 200 and jobs and jobs[0]["id"] == 2, str(jobs)[:200])

                    status, body = await call("GET", "/api/audit?limit=50")
                    check("audit mirrors the quality auditor over the live DB",
                          status == 200 and body.get("posts_checked") == 1
                          and body.get("delivery_summary", {}).get("real_sends") == 1,
                          str(body)[:200])

                    status, body = await call("POST", "/api/restart")
                    check("restart runs the configured command and clears pending",
                          status == 200 and body.get("ok") is True, str(body)[:200])
                    status, body = await call("GET", "/api/status")
                    check("pending is clear after the restart",
                          body.get("pending_restart") is False, str(body.get("pending_restart")))
            finally:
                if runner is not None:
                    await runner.cleanup()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
    asyncio.run(run())


def test_bot_merge():
    print("\n== bot merge: dashboard channels join live routing ==")
    routing = {"deals": ["LootZoneIndia11"]}
    tags = {"smartbuyhub11"}
    summary = channels_config.apply_to_routing(
        routing, tags,
        {"version": 1, "channels": [
            {"name": "EveningLoots", "sources": ["deals"], "amazon_tag": True, "enabled": True}]},
        existing_targets={"LootZoneIndia11", "smartbuyhub11"}, tricks_sources=set())
    check("a dashboard channel lands in routing + tag set",
          routing["deals"] == ["LootZoneIndia11", "EveningLoots"] and "EveningLoots" in tags
          and summary["added"] == ["EveningLoots"], f"{routing} {tags}")
    summary = channels_config.apply_to_routing(routing, tags, {"version": 1, "channels": "junk"})
    check("a junk payload never raises and changes nothing",
          summary["added"] == [] and routing["deals"] == ["LootZoneIndia11", "EveningLoots"],
          str(summary))


def test_package_import_merge():
    print("\n== bot merge: package-style import also applies channels.json ==")
    tmp = Path(tempfile.mkdtemp(prefix="dash-pkg-"))
    (tmp / "channels.json").write_text(json.dumps({"version": 1, "channels": [
        {"name": "PkgProbe", "sources": ["dealsvelocity"], "enabled": True}]}), encoding="utf-8")
    env = dict(os.environ, BOT_DB_PATH=str(tmp / "pkg.sqlite3"),
               BOT_LOG_DIR=str(tmp / "logs"), BOT_MEDIA_DIR=str(tmp / "media"),
               TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x", EARNKARO_API_KEY="k")
    code = ("import json, bestgaa.main_bot_new as b; "
            "print('ROUTED' if 'PkgProbe' in b.SOURCE_TO_TARGETS.get('dealsvelocity', set()) "
            "else 'NOT-ROUTED'); "
            "s=json.load(open(r'" + str(tmp / "routing_snapshot.json") + "')); "
            "print('SNAP-OK' if 'PkgProbe' in s.get('dashboard_added', []) else 'SNAP-MISS')")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(ROOT), env=env, timeout=300)
    out = (proc.stdout or "") + (proc.stderr or "")
    check("package-style import routes the dashboard channel", "ROUTED" in proc.stdout, out[-300:])
    check("package-style import snapshots dashboard_added", "SNAP-OK" in proc.stdout, out[-300:])


def test_category_shelf():
    print("\n== category shelf: dashboard targets keep only their categories ==")
    old = bot.DASHBOARD_CATEGORY_FILTERS
    bot.DASHBOARD_CATEGORY_FILTERS = {"electroshelf": frozenset({"electronics"})}
    try:
        check("the shelf keeps its own category",
              bot.dashboard_category_ok("ElectroShelf", "electronics"))
        check("the shelf drops other categories",
              not bot.dashboard_category_ok("ElectroShelf", "fashion"))
        check("the shelf drops unclassified posts and promos",
              not bot.dashboard_category_ok("ElectroShelf", ""))
        check("unfiltered channels and built-ins pass everything",
              bot.dashboard_category_ok("MyNewDeals", "fashion")
              and bot.dashboard_category_ok("LootZoneIndia11", ""))
    finally:
        bot.DASHBOARD_CATEGORY_FILTERS = old


if __name__ == "__main__":
    test_config_manager()
    test_http_api()
    test_bot_merge()
    test_package_import_merge()
    test_category_shelf()
    print("\n" + ("DASHBOARD TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_dashboard: all checks PASS"))
    sys.exit(1 if FAILS else 0)
