"""The operator's own channels: see them all, act on many, approve joins, repost.

The request (2026-10-25), in their words:

  "manam admin ga vunna channel anni chupinchali ... multiple buttons kuda
   pettelaga undali ... oka channel nunchi inkoka channel ki easy ga button
   pettelaga undali ... konni channel button nokkagane kakunda bot vallani
   accept cheyali oka 2-3 seconds lo, appudu vallaki official ga anipisthadi"

Four things, one screen:

1. **Inventory** - every channel where the account is creator/admin, with the
   rights Telegram reported (`can_post`, `can_invite`), so "is this mine?" and
   "may I post here?" are answered by data.
2. **Bulk actions** - tick many channels, one button, one atomic save
   (enable/disable/remove/add-as-destination/category), with per-channel
   refusals reported instead of silently dropped.
3. **Join requests** - opt-in per channel, approved after ~2.5 s, capped per
   minute, logged; OFF unless the operator turns it on; the bot never joins
   anything on its own account.
4. **Repost** - our post -> our other channels, by a button. Queued by the
   dashboard (append-only), forwarded once by the bot (which owns the session),
   refused for a stranger's channel, for itself, and for a private link.

Everything here is offline: the pure modules are called directly, the HTTP API
is driven through aiohttp's test client against a temporary bot directory, and
the bot file is read as text to pin the wiring (handler, tasks, no auto-join).
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as Entity

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))

import channel_directory as directory          # noqa: E402
import channels_config as cfg                  # noqa: E402
import join_gate as jg                         # noqa: E402
import repost as rp                            # noqa: E402

FAILS = []


def check(name, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def channel(cid, title, username=None, *, broadcast=True, creator=False, rights=None,
            megagroup=False, left=False, members=0, date=0):
    return Entity(id=cid, title=title, username=username, broadcast=broadcast,
                  megagroup=megagroup, gigagroup=False, left=left, deleted=False,
                  participants_count=members, date=date, creator=creator,
                  admin_rights=rights)


# --------------------------------------------------------------------------- #
# 1. the inventory: where are we admin, and what may we do there?
# --------------------------------------------------------------------------- #
def test_inventory():
    print("\n== every channel we run, with the rights Telegram reported ==")
    rows = [
        channel(1, "Loot Zone India", "LootZoneIndia11", creator=True, members=52000),
        channel(2, "Under 99 Shelf", "under99deals11",
                rights={"post_messages": True, "invite_users": True}, members=900),
        channel(3, "Read Only Deals", "readonlydeals", members=100),
        channel(4, "Family Group", None, broadcast=False, megagroup=True,
                rights={"invite_users": True}),
    ]
    payload = directory.build(rows, now=1_000.0)
    by_name = {row["username"]: row for row in payload["channels"] if row["username"]}
    check("a creator can post and approve",
          by_name["LootZoneIndia11"]["role"] == "creator"
          and by_name["LootZoneIndia11"]["can_post"]
          and by_name["LootZoneIndia11"]["can_invite"], by_name["LootZoneIndia11"])
    check("an admin with the rights can post and approve",
          by_name["under99deals11"]["role"] == "admin"
          and by_name["under99deals11"]["can_post"]
          and by_name["under99deals11"]["can_invite"], by_name["under99deals11"])
    check("a member is listed but cannot post",
          by_name["readonlydeals"]["role"] == "member"
          and not by_name["readonlydeals"]["can_post"], by_name["readonlydeals"])
    check("the payload counts what the panel shows",
          payload["admin_count"] == 3 and payload["creator_count"] == 1
          and payload["invite_count"] == 3, {k: payload[k] for k in
                                             ("admin_count", "creator_count", "invite_count")})
    check("a group with rights is admin too, and cannot be a public source",
          any(row["kind"] == "group" and row["admin"] and not row["addable"]
              for row in payload["channels"]), payload["channels"])
    check("admin channels sort first",
          payload["channels"][0]["admin"] is True
          and payload["channels"][-1]["admin"] is False,
          [row["role"] for row in payload["channels"]])
    check("a left channel is still hidden",
          all(row["title"] != "Left" for row in
              directory.build([channel(9, "Left", "lefty", left=True)])["channels"]), "")


# --------------------------------------------------------------------------- #
# 2. one save must not eat another feature's section
# --------------------------------------------------------------------------- #
def test_config_roundtrip():
    print("\n== channels.json keeps a section this module does not own ==")
    td = Path(tempfile.mkdtemp(prefix="cc-config-"))
    path = td / "channels.json"
    data = {"version": 1,
            "channels": [{"name": "OurShelf", "sources": ["*"], "categories": [],
                          "enabled": True}],
            "whatsapp": [], "watch": [],
            "join_requests": {"enabled": True, "delay": 2.5,
                              "channels": [{"name": "OurShelf", "enabled": True}]}}
    cfg.save_channels(data, path)
    stored = json.loads(path.read_text())
    check("the new section is written", (stored.get("join_requests") or {}).get("enabled") is True,
          stored.keys())
    check("and read back", (cfg.load_channels(path).get("join_requests") or {}).get("delay") == 2.5, "")
    reloaded = cfg.load_channels(path)
    reloaded["channels"][0]["enabled"] = False
    cfg.save_channels(reloaded, path)
    after = json.loads(path.read_text())
    check("a later save of the CHANNELS does not delete it",
          after["join_requests"]["enabled"] is True and after["channels"][0]["enabled"] is False,
          after)


# --------------------------------------------------------------------------- #
# 3. join requests: off by default, 2.5s, capped, logged, never joins us
# --------------------------------------------------------------------------- #
def test_join_gate():
    print("\n== join requests are approved in ~2.5s, but only where asked ==")
    off = jg.section({})
    check("auto-approval is OFF by default", off["enabled"] is False, off)
    verdict = jg.plan(off, "OurShelf")
    check("and nothing is approved while it is off",
          verdict["action"] == "skip", verdict)

    data = {"join_requests": {"enabled": True, "delay": 2.5,
                              "channels": [{"name": "OurShelf"},
                                           {"name": "@Paused", "enabled": False},
                                           {"name": "Busy", "per_minute": 2}]}}
    on = jg.section(data)
    check("an enabled channel gets the default 2.5s",
          jg.plan(on, "ourshelf") == {"action": "approve",
                                      "reason": "auto-approved after 2.5s", "delay": 2.5},
          jg.plan(on, "ourshelf"))
    check("a channel that is not on the list is left for a human",
          jg.plan(on, "stranger")["action"] == "skip", jg.plan(on, "stranger"))
    check("a paused channel is left alone",
          jg.plan(on, "paused")["action"] == "skip", jg.plan(on, "paused"))
    check("the per-minute cap holds the burst",
          jg.plan(on, "busy", now=100.0, recent=[99.0, 99.5])["action"] == "hold",
          jg.plan(on, "busy", now=100.0, recent=[99.0, 99.5]))
    check("old approvals fall out of the window",
          jg.plan(on, "busy", now=1000.0, recent=[1.0, 2.0])["action"] == "approve", "")
    check("a silly delay is clamped, not obeyed",
          jg.plan({"enabled": True, "channels": {"x": {"enabled": True, "delay": 900}}},
                  "x")["delay"] == jg.MAX_DELAY, "")
    check("and a zero delay becomes the 1s floor, never instant",
          jg.section({"join_requests": {"enabled": True, "delay": 0,
                                        "channels": [{"name": "x"}]}})["delay"] == jg.MIN_DELAY, "")

    saved = os.environ.get("JOIN_AUTO_APPROVE")
    try:
        os.environ["JOIN_AUTO_APPROVE"] = "0"
        check("JOIN_AUTO_APPROVE=0 stops it without touching the config",
              jg.section(data)["enabled"] is False, jg.section(data))
    finally:
        if saved is None:
            os.environ.pop("JOIN_AUTO_APPROVE", None)
        else:
            os.environ["JOIN_AUTO_APPROVE"] = saved

    # the async path: delay, then exactly one approval, and a log row
    td = Path(tempfile.mkdtemp(prefix="cc-join-"))
    gate = jg.JoinGate(log_path=td / "join.log.json", data_provider=lambda: data)
    calls = []

    async def scenario():
        async def approve():
            calls.append("approved")

        async def sleep(seconds):
            calls.append(("sleep", seconds))
        first = await gate.handle("OurShelf", 501, approve, sleep=sleep, name="OurShelf")
        second = await gate.handle("Stranger", 502, approve, sleep=sleep, name="Stranger")
        return first, second

    first, second = asyncio.run(scenario())
    check("an allowed request sleeps the delay and is approved once",
          calls == [("sleep", 2.5), "approved"] and first["action"] == "approved", calls)
    check("a request from an unlisted channel is never approved",
          second["action"] == "skip" and len(calls) == 2, second)
    log = json.loads((td / "join.log.json").read_text())
    check("every decision is written down, with the user id",
          len(log) == 2 and {row["action"] for row in log} == {"approved", "skip"}
          and log[0]["user_id"] == 501, log)

    # a failure to approve must be recorded, not swallowed
    gate2 = jg.JoinGate(log_path=td / "join2.json", data_provider=lambda: data)

    async def failing():
        async def approve():
            raise RuntimeError("FLOOD_WAIT_42")

        async def sleep(_seconds):
            return None
        return await gate2.handle("OurShelf", 7, approve, sleep=sleep, name="OurShelf")

    failed = asyncio.run(failing())
    check("an approval Telegram refuses is logged as failed",
          failed["action"] == "failed" and "FLOOD_WAIT" in failed["reason"], failed)


def test_join_gate_wiring():
    print("\n== the bot wires it, and still never joins anything itself ==")
    bot = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    check("the bot handles the pending-join-request update",
          "UpdatePendingJoinRequests" in bot and "on_pending_join_requests" in bot, "")
    check("the handler is optional, so an older Telethon cannot break boot",
          "_UpdatePendingJoinRequests is not None" in bot, "")
    check("a poll covers the updates that were missed",
          "join_request_poll_loop" in bot and "GetChatInviteImportersRequest" in bot, "")
    check("approval goes through the gate (delay, cap, log)",
          "join_gate()" in bot and "gate.handle(" in bot, "")
    check("the bot NEVER joins a channel on its own account",
          "JoinChannelRequest" not in bot and "join_channel" not in bot.replace(
              "join_channel_name", ""), "")
    check("the poll task is started and drained",
          "join_poll_task = asyncio.create_task" in bot and "await join_poll_task" in bot, "")
    check("the dashboard reads the same log the bot writes",
          "join_requests_log.json" in bot
          and "join_requests_log.json" in (ROOT / "dashboard" / "app.py").read_text(), "")


# --------------------------------------------------------------------------- #
# 4. repost: our post -> our other channels, exactly once
# --------------------------------------------------------------------------- #
def test_repost_module():
    print("\n== one press, one forward, only between OUR channels ==")
    td = Path(tempfile.mkdtemp(prefix="cc-repost-"))
    queue = td / "repost_requests.jsonl"
    ours = ["LootZoneIndia11", "under99deals11", "Main"]
    clean, errors = rp.submit({"link": "https://t.me/LootZoneIndia11/1234",
                               "targets": ["under99deals11", "Main"]}, ours, path=queue)
    check("a post of ours can go to two of ours", not errors and clean["targets"] ==
          ["under99deals11", "main"], errors)
    check("it is queued on disk", len(rp.queued(queue)) == 1, rp.queued(queue))

    _, errors = rp.submit({"link": "https://t.me/stranger/9", "targets": ["Main"]},
                          ours, path=queue)
    check("a stranger's channel is refused", bool(errors), errors)
    _, errors = rp.submit({"link": "https://t.me/Main/9", "targets": ["Main"]}, ours, path=queue)
    check("a channel cannot repost into itself", bool(errors), errors)
    _, errors = rp.submit({"link": "https://t.me/c/123/9", "targets": ["Main"]}, ours, path=queue)
    check("a private (t.me/c/…) link is refused", bool(errors), errors)
    _, errors = rp.submit({"link": "https://t.me/Main/9", "targets": ["StrangerTV"]},
                          ours, path=queue)
    check("and a destination that is not ours is refused", bool(errors), errors)
    _, errors = rp.submit({"link": "https://t.me/Main/9",
                           "targets": [f"ours{i}" for i in range(20)]}, ours, path=queue)
    check("too many destinations in one press is refused", bool(errors), errors)

    rp.record({"id": clean["id"], "action": "sent", "sent": ["main"]}, td / "log.json")
    check("a forwarded request is never forwarded again",
          rp.queued(queue, rp.done_ids(td / "log.json")) == [], rp.queued(queue))
    check("the log is what the page shows",
          rp.read_log(td / "log.json")[-1]["action"] == "sent", rp.read_log(td / "log.json"))


def test_repost_bot_wiring():
    bot = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    print("\n== the BOT forwards (it owns the session), the page only asks ==")
    check("the bot drains the queue", "repost_loop" in bot and "forward_messages" in bot, "")
    check("re-validated on the bot side, because the file is on disk",
          "_repost.validate(" in bot, "")
    check("each request is recorded once", "_repost.record(" in bot
          and "_repost.done_ids()" in bot, "")
    check("the task is started and drained",
          "repost_task = asyncio.create_task" in bot and "await repost_task" in bot, "")
    check("a repost failure never takes down posting",
          "REPOST | pass failed" in bot, "")


# --------------------------------------------------------------------------- #
# 5. the HTTP API the page actually calls
# --------------------------------------------------------------------------- #
def build_bot_dir():
    base = Path(tempfile.mkdtemp(prefix="cc-api-"))
    bot = base / "bestgaa-bot"
    bot.mkdir()
    (bot / ".env").write_text(
        "TELEGRAM_API_ID=1234567\n" + "TELEGRAM_API_HASH=" + "a" * 32
        + "\nEARNKARO_API_KEY=x.y.z\nAMAZON_TAG=mama086-21\n", encoding="utf-8")
    conn = sqlite3.connect(bot / "bestgaa.sqlite3")
    conn.execute("CREATE TABLE deliveries (target TEXT, status TEXT)")
    conn.commit()
    conn.close()
    (bot / "channels.json").write_text(json.dumps({
        "version": 1,
        "channels": [{"name": "LootZoneIndia11", "sources": ["*"], "categories": [],
                      "amazon_tag": False, "enabled": True}],
        "whatsapp": [], "watch": []}), encoding="utf-8")
    (bot / "telegram_channels.json").write_text(json.dumps({
        "schema": 1, "generated_at": 1_700_000_000, "count": 3, "addable": 3,
        "channels": [
            {"id": 1, "title": "Loot Zone", "username": "LootZoneIndia11", "kind": "channel",
             "addable": True, "private": False, "members": 52000, "admin": True,
             "creator": True, "can_post": True, "can_invite": True, "role": "creator"},
            {"id": 2, "title": "Shelf", "username": "under99deals11", "kind": "channel",
             "addable": True, "private": False, "members": 900, "admin": True,
             "creator": False, "can_post": True, "can_invite": True, "role": "admin"},
            {"id": 3, "title": "Read Only", "username": "readonlydeals", "kind": "channel",
             "addable": True, "private": False, "members": 10, "admin": False,
             "creator": False, "can_post": False, "can_invite": False, "role": "member"},
        ]}), encoding="utf-8")
    return bot


def test_dashboard_api():
    print("\n== the page's own endpoints (temporary bot directory) ==")
    bot = build_bot_dir()
    os.environ["BESTGAA_DIR"] = str(bot)
    os.environ["DASHBOARD_TOKEN"] = "test-token"
    sys.path.insert(0, str(ROOT))
    from dashboard import app as dash  # noqa: PLC0415 - after BESTGAA_DIR is set

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer
        client = TestClient(TestServer(dash.create_app()))
        await client.start_server()
        headers = {"X-Dashboard-Token": "test-token"}
        out = {}

        r = await client.get("/api/directory", headers=headers)
        body = await r.json()
        out["admin"] = body.get("admin")
        out["join"] = body.get("join_requests")
        out["roles"] = {row["username"]: row["role"] for row in body.get("channels", [])
                        if row.get("username")}

        r = await client.post("/api/channels/bulk", headers=headers,
                              json={"action": "disable", "channels": ["LootZoneIndia11"]})
        out["disable"] = await r.json()
        r = await client.post("/api/channels/bulk", headers=headers,
                              json={"action": "target", "channels": ["under99deals11"],
                                    "sources": ["*"]})
        out["target"] = await r.json()
        r = await client.post("/api/channels/bulk", headers=headers,
                              json={"action": "bogus", "channels": ["under99deals11"]})
        out["bogus"] = (r.status, await r.json())
        r = await client.post("/api/channels/bulk", headers=headers,
                              json={"action": "enable", "channels": ["not-ours"]})
        out["not_ours"] = (r.status, await r.json())
        r = await client.post("/api/channels/bulk", headers=headers,
                              json={"action": "category",
                                    "channels": ["under99deals11"],
                                    "categories": ["nonsense"]})
        out["bad_category"] = (r.status, await r.json())

        r = await client.post("/api/join-requests", headers=headers,
                              json={"enabled": True, "delay": 2.5,
                                    "channels": [{"name": "LootZoneIndia11"}]})
        out["join_save"] = await r.json()
        r = await client.post("/api/join-requests", headers=headers,
                              json={"enabled": True, "channels": [{"name": "somebody-else"}]})
        out["join_stranger"] = (r.status, await r.json())
        r = await client.post("/api/join-requests", headers=headers,
                              json={"enabled": True, "delay": 900, "channels": []})
        out["join_delay"] = (r.status, await r.json())
        r = await client.get("/api/join-requests", headers=headers)
        out["join_get"] = await r.json()

        r = await client.post("/api/repost", headers=headers,
                              json={"link": "https://t.me/LootZoneIndia11/77",
                                    "targets": ["under99deals11"]})
        out["repost"] = await r.json()
        r = await client.post("/api/repost", headers=headers,
                              json={"link": "https://t.me/stranger/7",
                                    "targets": ["under99deals11"]})
        out["repost_stranger"] = (r.status, await r.json())
        r = await client.get("/api/repost", headers=headers)
        out["repost_get"] = await r.json()
        await client.close()
        return out

    data = asyncio.run(scenario())
    check("the page knows every admin channel and its role",
          (data["admin"] or {}).get("count") == 2
          and data["roles"].get("LootZoneIndia11") == "creator"
          and data["roles"].get("readonlydeals") == "member", data["roles"])
    check("join requests start OFF with the 2.5s default",
          data["join"]["enabled"] is False and data["join"]["delay"] == 2.5, data["join"])

    check("bulk disable works", data["disable"].get("done") == ["lootzoneindia11"], data["disable"])
    check("bulk add-as-destination works", data["target"].get("done") == ["under99deals11"],
          data["target"])
    check("an unknown action is refused with the list of real ones",
          data["bogus"][0] == 400 and "enable/disable" in " ".join(data["bogus"][1]["errors"]),
          data["bogus"])
    check("a channel that is not configured is reported, not crashed on",
          data["not_ours"][0] == 400
          and data["not_ours"][1]["skipped"][0]["errors"] == ["not a dashboard channel"],
          data["not_ours"])
    check("a bad category is refused per channel",
          data["bad_category"][0] == 400
          and "unknown category" in str(data["bad_category"][1]), data["bad_category"])

    check("saving auto-approval stores the channel",
          [row["name"] for row in data["join_save"]["channels"]] == ["LootZoneIndia11"],
          data["join_save"])
    check("a channel we do not admin is refused",
          data["join_stranger"][0] == 400
          and "not one of our channels" in " ".join(data["join_stranger"][1]["errors"]),
          data["join_stranger"])
    check("a 900-second delay is refused, not clamped silently",
          data["join_delay"][0] == 400, data["join_delay"])
    check("the saved setting is what the next read returns",
          data["join_get"]["enabled"] is True
          and data["join_get"]["channels"][0]["name"] == "LootZoneIndia11", data["join_get"])

    check("a repost of our post is queued",
          (data["repost"].get("queued") or {}).get("targets") == ["under99deals11"],
          data["repost"])
    check("a stranger's link is refused at the API too",
          data["repost_stranger"][0] == 400, data["repost_stranger"])
    check("the queue is readable for the page",
          len(data["repost_get"].get("queued") or []) == 1, data["repost_get"])

    # the file the bot will drain is the bot's own directory, not the checkout
    check("the queue lands beside the bot's channels.json",
          (bot / "repost_requests.jsonl").exists(), sorted(p.name for p in bot.iterdir()))
    os.environ.pop("BESTGAA_DIR", None)


# --------------------------------------------------------------------------- #
# 6. the page tells the operator what it can and cannot do
# --------------------------------------------------------------------------- #
def test_page_markup():
    print("\n== the page has the buttons, and says what they do ==")
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    for ids, label in (
            (["channelcontrol", "ccsummary", "cclist"], "the channel-control card"),
            (["jrenabled", "jrdelay", "jrlist", "jrrecent"], "the join-request controls"),
            (["repostlink", "reposttargets", "repostlog"], "the repost button")):
        check(label + " exists", all(f'id="{one}"' in html for one in ids),
              [one for one in ids if f'id="{one}"' not in html])
    for action in ("enable", "disable", "target", "category", "remove"):
        check(f"a bulk button for {action} exists", f"ccBulk('{action}')" in html, "")
    check("the card says the bot never joins a channel itself",
          "Nothing here joins a channel" in html, "")
    check("the repost card explains it is a button, not a rule",
          "This is a button, not a rule" in html, "")
    check("the join card says it is off by default",
          "Off by default" in html, "")


def main():
    test_inventory()
    test_config_roundtrip()
    test_join_gate()
    test_join_gate_wiring()
    test_repost_module()
    test_repost_bot_wiring()
    test_dashboard_api()
    test_page_markup()
    print("\n" + ("CHANNEL CONTROL TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_channel_control: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
