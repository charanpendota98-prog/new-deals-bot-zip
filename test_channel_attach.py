"""Attach the channels that already run — and get told which channel to add next.

The request (2026-10-25), in the operator's words:

  "mana existing running channel ki kuda ottu add cheyagalgali neatga dashboard
   lo ... oka channel nunchi inko channels suggest cheyali ... more and more
   advanced ga vundali"

Two verbs, one list:

1. **Attach** - an EXISTING channel (a code target, a dashboard channel, or a
   channel we admin but do not post to) becomes managed here: it can be paused,
   annotated, and used as the starting point for suggestions. Attaching a channel
   the bot does not post to yet creates a real dashboard channel; attaching a
   code target changes no routing (and detach says so).
2. **Suggest** - from ONE of our channels, the BOT asks Telegram for its similar
   channels and reads that channel's own recent posts for `t.me/…`/`@…` mentions,
   then lists candidates that are not already ours.

Everything is offline: the pure modules are driven directly, the HTTP API through
aiohttp's test client against a temporary bot directory, the bot file read as text
to pin the wiring (fan-out pause, the task, the request it sends).
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as Entity

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))

import channel_suggest as suggest          # noqa: E402
import channels_attach as attach           # noqa: E402
import channels_config as cfg              # noqa: E402

FAILS = []


def check(name, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


DIRECTORY = [
    {"id": 1, "title": "Loot Zone", "username": "LootZoneIndia11", "kind": "channel",
     "admin": True, "creator": True, "can_post": True, "can_invite": True, "role": "creator",
     "members": 52000, "last_message_at": 1_700_000_000},
    {"id": 2, "title": "Under 99", "username": "under99deals11", "kind": "channel",
     "admin": True, "creator": False, "can_post": True, "can_invite": True, "role": "admin",
     "members": 900, "last_message_at": 1_700_000_000},
    {"id": 3, "title": "New Shelf", "username": "newshelfdeals", "kind": "channel",
     "admin": True, "creator": True, "can_post": True, "can_invite": True, "role": "creator",
     "members": 12, "last_message_at": 0},
    {"id": 4, "title": "Read Only", "username": "readonlydeals", "kind": "channel",
     "admin": False, "creator": False, "can_post": False, "can_invite": False, "role": "member",
     "members": 10, "last_message_at": 0},
    {"id": 5, "title": "Family", "username": "", "kind": "group", "admin": True,
     "creator": False, "can_post": True, "can_invite": True, "role": "admin", "members": 4,
     "last_message_at": 0},
    {"id": 6, "title": "Source Deals", "username": "Sourcedealsx", "kind": "channel",
     "admin": True, "creator": True, "can_post": True, "can_invite": True, "role": "creator",
     "members": 30, "last_message_at": 0},
]
TARGETS = ["LootZoneIndia11", "under99deals11"]


def bot_dir(payload=None):
    """A throwaway bot directory with just enough config for these modules."""
    base = Path(tempfile.mkdtemp(prefix="attach-test-"))
    bot = base / "bestgaa-bot"
    bot.mkdir()
    (bot / ".env").write_text(
        "TELEGRAM_API_ID=1234567\n" + "TELEGRAM_API_HASH=" + "a" * 32
        + "\nEARNKARO_API_KEY=x.y.z\nAMAZON_TAG=mama086-21\n", encoding="utf-8")
    conn = sqlite3.connect(bot / "bestgaa.sqlite3")
    conn.execute("CREATE TABLE deliveries (target TEXT, status TEXT)")
    conn.commit()
    conn.close()
    data = payload if payload is not None else {
        "version": 1,
        "channels": [{"name": "LootZoneIndia11", "sources": ["*"], "categories": [],
                      "amazon_tag": False, "enabled": True}],
        "whatsapp": [],
        "watch": [{"channel": "Sourcedealsx", "targets": ["*"], "enabled": True}],
    }
    (bot / "channels.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    (bot / "routing_snapshot.json").write_text(json.dumps({
        "started_at": time.time(), "live_reload": True, "heartbeat_at": time.time(),
        "targets": ["LootZoneIndia11", "under99deals11"],
        "dashboard_added": ["LootZoneIndia11"],
        "main_feed": ["LootZoneIndia11"], "sources": [], "source_details": {}}),
        encoding="utf-8")
    (bot / "telegram_channels.json").write_text(json.dumps({
        "schema": 1, "generated_at": time.time(), "count": len(DIRECTORY),
        "addable": 3, "channels": DIRECTORY}), encoding="utf-8")
    return bot


def point_at(bot):
    """Pin every resolver at the fake bot (the offline gate exports BOT_DB_PATH)."""
    keys = ("BESTGAA_DIR", "BOT_DB_PATH", "CHANNELS_JSON_PATH", "BESTGAA_BOT_ENV",
            "DASHBOARD_TOKEN")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update({
        "BESTGAA_DIR": str(bot),
        "BOT_DB_PATH": str(bot / "bestgaa.sqlite3"),
        "CHANNELS_JSON_PATH": str(bot / "channels.json"),
        "BESTGAA_BOT_ENV": str(bot / ".env"),
        "DASHBOARD_TOKEN": "test-token",
    })
    return saved


def restore(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# --------------------------------------------------------------------------- #
# 1. attach: existing running channels become manageable
# --------------------------------------------------------------------------- #
def test_attach_existing():
    print("\n== attach an existing running channel (no routing change) ==")
    bot = bot_dir()
    saved = point_at(bot)
    path = bot / "channels.json"
    result = attach.attach({"name": "@under99deals11"}, directory_rows=DIRECTORY,
                           targets=TARGETS, path=path)
    check("a channel the bot already posts to is attached as 'existing'",
          (result.get("attached") or {}).get("mode") == "existing", result)
    check("and no dashboard channel was invented for it",
          result.get("channel") is None, result.get("channel"))
    saved_data = json.loads(path.read_text(encoding="utf-8"))
    names = [row["name"] for row in saved_data.get("channels") or []]
    check("the routing config is untouched", names == ["LootZoneIndia11"], names)
    check("the attachment is recorded in channels.json",
          [row["name"] for row in attach.entries(saved_data)] == ["under99deals11"],
          attach.entries(saved_data))
    check("the watch list and other sections survive the save",
          saved_data.get("watch") and len(saved_data["watch"]) == 1, saved_data.get("watch"))
    check("attaching the same channel twice is refused",
          attach.attach({"name": "under99deals11"}, directory_rows=DIRECTORY,
                        targets=TARGETS, path=path)["errors"] != [], "")
    restore(saved)


def test_attach_new():
    print("\n== attach a channel we admin but do not post to (a real destination) ==")
    bot = bot_dir()
    saved = point_at(bot)
    path = bot / "channels.json"
    result = attach.attach({"name": "https://t.me/newshelfdeals", "sources": ["*"]},
                           directory_rows=DIRECTORY, targets=TARGETS, path=path,
                           known_sources={"LootZoneIndia11", "Sourcedealsx"})
    check("a channel that is not a target is attached as 'new'",
          (result.get("attached") or {}).get("mode") == "new", result)
    created = result.get("channel") or {}
    check("and a dashboard channel was created for it",
          created.get("name") == "newshelfdeals" and created.get("sources") == ["*"], created)
    saved_data = json.loads(path.read_text(encoding="utf-8"))
    check("both land in channels.json (registry + channel)",
          attach.entries(saved_data) and len(saved_data.get("channels") or []) == 2,
          saved_data.get("channels"))
    check("the created channel is enabled, so the bot will post there",
          (saved_data.get("channels") or [{}])[-1].get("enabled") is True,
          saved_data.get("channels"))
    check("attaching says it needs the bot to pick the config up",
          result.get("pending_restart") is True, result)
    restore(saved)


def test_attach_refusals():
    print("\n== attach refuses what would not work, with the reason ==")
    bot = bot_dir()
    saved = point_at(bot)
    path = bot / "channels.json"
    cases = {
        "a channel the bot has never seen": ("StrangerChannelX", "not in the bot's channel list"),
        "a channel we are only a member of": ("readonlydeals", "creator or admin"),
        "a group": ("Family", "not a broadcast channel"),
        "a channel we currently watch as a source": ("Sourcedealsx", "cannot be both a source"),
        "nothing": ("", "paste the channel link"),
    }
    for label, (name, needle) in cases.items():
        result = attach.attach({"name": name}, directory_rows=DIRECTORY, targets=TARGETS,
                               path=path)
        errors = " ".join(result.get("errors") or [])
        check(f"{label} is refused", needle in errors, errors)
    check("nothing was written by any refusal",
          attach.entries(json.loads(path.read_text(encoding="utf-8"))) == [], "")
    restore(saved)


def test_detach_and_pause():
    print("\n== detach undoes exactly what attach did; pause is per channel ==")
    bot = bot_dir()
    saved = point_at(bot)
    path = bot / "channels.json"
    attach.attach({"name": "newshelfdeals"}, directory_rows=DIRECTORY, targets=TARGETS,
                  path=path, known_sources={"LootZoneIndia11"})
    attach.attach({"name": "LootZoneIndia11"}, directory_rows=DIRECTORY, targets=TARGETS,
                  path=path)
    ok = attach.detach("newshelfdeals", path=path)
    check("detaching a 'new' attachment removes its dashboard channel too",
          ok.get("channel_removed") is True, ok)
    data = json.loads(path.read_text(encoding="utf-8"))
    check("so the bot stops posting there",
          [row["name"] for row in data.get("channels") or []] == ["LootZoneIndia11"],
          data.get("channels"))
    kept = attach.detach("LootZoneIndia11", path=path)
    check("detaching a code target keeps it posting and says so",
          kept.get("channel_removed") is False
          and any("still a routing target" in warn for warn in kept.get("warnings") or []), kept)
    check("detaching something that is not attached is refused, not crashed",
          attach.detach("under99deals11", path=path).get("errors") != [], "")

    attach.attach({"name": "under99deals11"}, directory_rows=DIRECTORY, targets=TARGETS,
                  path=path)
    paused = attach.set_posting("under99deals11", False, path=path)
    check("a pause is recorded", paused.get("row", {}).get("posting") is False, paused)
    check("and shows up in the pause list the bot reads",
          "under99deals11" in attach.paused(cfg.load_channels(path)),
          attach.paused(cfg.load_channels(path)))
    resumed = attach.set_posting("under99deals11", True, path=path)
    check("resuming clears it",
          "under99deals11" not in attach.paused(cfg.load_channels(path)), resumed)
    restore(saved)


def test_paused_targets_cache():
    print("\n== the bot's pause list follows the file, not a restart ==")
    bot = bot_dir()
    saved = point_at(bot)
    path = bot / "channels.json"
    attach.attach({"name": "under99deals11"}, directory_rows=DIRECTORY, targets=TARGETS,
                  path=path)
    check("nothing paused yet", attach.paused_targets(path) == set(), attach.paused_targets(path))
    attach.set_posting("under99deals11", False, path=path)
    # Force a distinct mtime: a same-second write must still be seen.
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))
    check("a pause is visible immediately", attach.paused_targets(path) == {"under99deals11"},
          attach.paused_targets(path))
    data = cfg.load_channels(path)
    data["channels"][0]["enabled"] = False
    cfg.save_channels(data, path)
    stamp += 5
    os.utime(path, (stamp, stamp))
    paused = attach.paused_targets(path)
    check("a disabled dashboard channel counts as paused too",
          "lootzoneindia11" in paused, paused)
    data["channels"][0]["enabled"] = True
    cfg.save_channels(data, path)
    stamp += 5
    os.utime(path, (stamp, stamp))
    check("and enabling it unpauses it",
          "lootzoneindia11" not in attach.paused_targets(path), attach.paused_targets(path))
    restore(saved)


def test_inventory():
    print("\n== one list: what runs from code, from here, and what is only a candidate ==")
    bot = bot_dir()
    saved = point_at(bot)
    path = bot / "channels.json"
    attach.attach({"name": "newshelfdeals"}, directory_rows=DIRECTORY, targets=TARGETS,
                  path=path, known_sources={"LootZoneIndia11"})
    data = cfg.load_channels(path)
    view = attach.inventory(data=data, targets=[*TARGETS, "newshelfdeals"],
                            dashboard_names=["LootZoneIndia11", "newshelfdeals"],
                            directory_rows=DIRECTORY, watch_channels=["Sourcedealsx"])
    rows = {row["name"].lower(): row for row in view["rows"]}
    check("a code-only target is marked as such",
          "code" in rows["under99deals11"]["origins"]
          and "dashboard" not in rows["under99deals11"]["origins"], rows["under99deals11"])
    check("a dashboard channel is not called a code target",
          "dashboard" in rows["lootzoneindia11"]["origins"]
          and "code" not in rows["lootzoneindia11"]["origins"], rows["lootzoneindia11"])
    check("an attached channel says so",
          rows["newshelfdeals"]["attached"] is True and rows["newshelfdeals"]["ours"], rows["newshelfdeals"])
    check("a channel where we are only a member is not in OUR list at all",
          "readonlydeals" not in rows, sorted(rows))
    check("a channel that is one of our SOURCES is listed but not attachable",
          rows["sourcedealsx"]["is_source"] is True
          and rows["sourcedealsx"]["attachable"] is False
          and "cannot be both" in rows["sourcedealsx"]["why_not"], rows.get("sourcedealsx"))
    check("the counts add up",
          view["counts"]["ours"] == 3 and view["counts"]["attached"] == 1, view["counts"])
    check("every row that has a username carries a t.me link",
          all(row["link"] == "https://t.me/" + row["name"] for row in view["rows"]
              if row["link"]), [row["link"] for row in view["rows"]])
    restore(saved)


# --------------------------------------------------------------------------- #
# 2. suggest: the next channel, from one we already run
# --------------------------------------------------------------------------- #
def test_mentions():
    print("\n== what a post actually links to ==")
    text = ("🔥 Deal: https://t.me/OtherDealsHub/1234 and t.me/+InviteOnlyCode\n"
            "also @AnotherDeals and @AnotherDeals again · deals@gmail.com is not a channel")
    found = suggest.parse_mentions(text)
    check("a t.me post link becomes a channel",
          "OtherDealsHub" in found, found)
    check("@mentions are found once each", found.count("AnotherDeals") == 1, found)
    check("an email address is not a channel",
          all("gmail" not in name.lower() for name in found), found)


def test_from_entities():
    print("\n== Telegram's answers, filtered to what a link can address ==")
    rows = suggest.from_entities([
        Entity(username="SimilarDeals", title="Similar Deals", broadcast=True,
               participants_count=1200, left=False, deleted=False),
        Entity(username="", title="No Username", broadcast=True, participants_count=5,
               left=False, deleted=False),
        Entity(username="AGroupHere", title="Group", broadcast=False,
               megagroup=True, participants_count=9, left=False, deleted=False),
        Entity(username="LeftChannel", title="Left", broadcast=True,
               participants_count=9, left=True, deleted=False),
    ])
    names = [row["username"] for row in rows]
    check("a public channel is kept", names == ["SimilarDeals"], names)
    check("an invite-only channel is dropped (no link to add)",
          "No Username" not in names and "" not in names, names)
    check("a group is dropped", "AGroupHere" not in names, names)
    check("a channel we left is dropped", "LeftChannel" not in names, names)


def test_build_and_exclude():
    print("\n== candidates are ours-aware, ranked, and honest about the source ==")
    similar = [{"username": "SimilarDeals", "title": "Similar Deals", "members": 1200},
               {"username": "under99deals11", "title": "already ours", "members": 900}]
    mentions = ["MentionedDeal", "MentionedDeal", "OtherMention", "Sourcedealsx"]
    rows = suggest.build("LootZoneIndia11", similar=similar, mentions=mentions,
                         exclude={"under99deals11", "Sourcedealsx"}, limit=10)
    by_name = {row["username"].lower(): row for row in rows}
    check("a channel that is already ours is never suggested",
          "under99deals11" not in by_name and "sourcedealsx" not in by_name, list(by_name))
    check("the channel we started from is never suggested",
          "lootzoneindia11" not in by_name, list(by_name))
    check("Telegram's picks come first",
          rows and rows[0]["username"] == "SimilarDeals"
          and rows[0]["source"] == "similar", rows[:2])
    check("its own mentions come after, most-mentioned first",
          [row["username"] for row in rows[1:]] == ["MentionedDeal", "OtherMention"],
          [row["username"] for row in rows])
    check("every row says where the idea came from",
          all(row["why"] and row["source"] in ("similar", "mentioned") for row in rows), rows)
    check("the limit is honoured", len(suggest.build("LootZoneIndia11", similar=similar,
                                                     mentions=mentions, limit=1)) == 1, "")


def test_request_roundtrip():
    print("\n== the dashboard asks once; the bot answers once ==")
    base = Path(tempfile.mkdtemp(prefix="suggest-"))
    requests = base / "suggest_requests.jsonl"
    answers = base / "channel_suggestions.json"
    clean, errors = suggest.validate_request({"channel": "StrangerChannelX"},
                                             ours=["LootZoneIndia11"])
    check("suggestions start from a channel we run", errors != [], errors)
    clean, errors = suggest.submit({"channel": "@LootZoneIndia11", "limit": 500},
                                   ours=["LootZoneIndia11"], path=requests)
    check("a valid ask is queued", not errors and clean["channel"] == "LootZoneIndia11", errors)
    check("an absurd scan limit is clamped, not obeyed", clean["limit"] == suggest.MAX_SCAN,
          clean["limit"])
    queued = suggest.pending(requests)
    check("the bot sees exactly one request", len(queued) == 1, queued)
    answer = suggest.answer(queued[0], similar=[{"username": "SimilarDeals", "title": "S",
                                                 "members": 10}],
                            mentions=["MentionedDeal"], exclude={"LootZoneIndia11"})
    suggest.record(answer, answers)
    check("the answer is stored under the request id",
          suggest.done_ids(answers) == {clean["id"]}, suggest.done_ids(answers))
    check("so the bot never asks twice", suggest.pending(requests, suggest.done_ids(answers)) == [],
          suggest.pending(requests, suggest.done_ids(answers)))
    check("the answer counts both sources",
          answer["similar_count"] == 1 and answer["mentioned_count"] == 1, answer)
    answered = suggest.read_results(answers)["requests"][clean["id"]]
    check("the stored row carries the links the page renders",
          [row["link"] for row in answered["suggestions"]]
          == ["https://t.me/SimilarDeals", "https://t.me/MentionedDeal"],
          answered["suggestions"])
    # An answered request must not count against the pending cap: the first
    # version passed the REQUEST path to done_ids(), so after enough asks every
    # new one was refused for good.
    many = base / "many_requests.jsonl"
    many_answers = base / "many_answers.json"
    for index in range(suggest.MAX_PENDING + 3):
        ask, ask_errors = suggest.submit({"channel": "LootZoneIndia11"},
                                          ours=["LootZoneIndia11"], path=many,
                                          answers=many_answers)
        assert not ask_errors, ask_errors
        suggest.record(suggest.answer(ask), many_answers)
    check("answered requests never fill the pending cap",
          len(suggest.pending(many, suggest.done_ids(many_answers))) == 0,
          len(suggest.pending(many, suggest.done_ids(many_answers))))
    check("an unusable channel is an error row, not an empty list",
          suggest.answer({"id": "x", "channel": "X"}, error="cannot resolve")["count"] == 0
          and "cannot resolve" in suggest.answer({"id": "x", "channel": "X"},
                                                 error="cannot resolve")["error"], "")
    requests.write_text(requests.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    check("a corrupt request line is skipped, never fatal",
          len(suggest.pending(requests)) == 1, suggest.pending(requests))


# --------------------------------------------------------------------------- #
# 3. the HTTP endpoints the page calls
# --------------------------------------------------------------------------- #
def test_api():
    print("\n== the page's own endpoints (temporary bot directory) ==")
    bot = bot_dir()
    saved = point_at(bot)
    sys.path.insert(0, str(ROOT))
    from dashboard import app as dash  # noqa: PLC0415 - after BESTGAA_DIR is set

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer
        client = TestClient(TestServer(dash.create_app()))
        await client.start_server()
        headers = {"X-Dashboard-Token": "test-token"}
        out = {}
        r = await client.get("/api/own", headers=headers)
        out["own"] = (r.status, await r.json())
        r = await client.post("/api/own/attach", headers=headers,
                              json={"name": "newshelfdeals"})
        out["attach"] = (r.status, await r.json())
        r = await client.post("/api/own/attach", headers=headers, json={"name": "readonlydeals"})
        out["attach_bad"] = (r.status, await r.json())
        r = await client.post("/api/own/posting", headers=headers,
                              json={"name": "newshelfdeals", "posting": False})
        out["pause"] = (r.status, await r.json())
        r = await client.post("/api/own/posting", headers=headers,
                              json={"name": "newshelfdeals", "posting": "no"})
        out["pause_bad"] = (r.status, await r.json())
        r = await client.post("/api/own/suggest", headers=headers,
                              json={"channel": "LootZoneIndia11"})
        out["ask"] = (r.status, await r.json())
        r = await client.post("/api/own/suggest", headers=headers,
                              json={"channel": "readonlydeals"})
        out["ask_bad"] = (r.status, await r.json())
        r = await client.get("/api/own/suggest", headers=headers)
        out["asked"] = (r.status, await r.json())
        r = await client.post("/api/own/detach", headers=headers, json={"name": "newshelfdeals"})
        out["detach"] = (r.status, await r.json())
        await client.close()
        return out

    data = asyncio.run(scenario())
    own = data["own"][1]
    names = {row["name"].lower(): row for row in own.get("rows") or []}
    check("the list contains our code targets",
          "under99deals11" in names and names["under99deals11"]["origins"] == ["code"],
          names.get("under99deals11"))
    check("and the ones added from the dashboard are marked as ours, not as code",
          "lootzoneindia11" in names and names["lootzoneindia11"]["ours"] is True
          and "code" not in names["lootzoneindia11"]["origins"], names.get("lootzoneindia11"))
    check("and the admin channels we have not attached as candidates",
          names.get("newshelfdeals", {}).get("attachable") is True, names.get("newshelfdeals"))
    status, attached = data["attach"]
    check("attaching a new admin channel succeeds",
          status == 200 and (attached.get("attached") or {}).get("mode") == "new", attached)
    check("its next step is phrased honestly (the bot picks the config up)",
          "picks the config up" in str(attached.get("next")), attached.get("next"))
    check("a channel we only read is refused",
          data["attach_bad"][0] == 400
          and "creator or admin" in " ".join(data["attach_bad"][1].get("errors") or []),
          data["attach_bad"])
    check("a pause is accepted", data["pause"][0] == 200, data["pause"][1].get("errors"))
    check("no delivery is replayed when it resumes",
          "replayed" in str(data["pause"][1].get("next")), data["pause"][1].get("next"))
    check("a non-boolean pause is refused", data["pause_bad"][0] == 400, data["pause_bad"])
    check("asking for suggestions queues one request",
          data["ask"][0] == 200 and (data["ask"][1].get("queued") or {}).get("channel")
          == "LootZoneIndia11", data["ask"])
    check("from a channel that is not ours is refused", data["ask_bad"][0] == 400,
          data["ask_bad"])
    check("the request lands beside channels.json for the bot",
          (bot / "suggest_requests.jsonl").exists(), sorted(item.name for item in bot.iterdir()))
    check("nothing is answered until the bot answers",
          data["asked"][1].get("suggestions") in ([], None), data["asked"][1].get("suggestions"))
    check("detaching removes what attaching created", data["detach"][0] == 200,
          data["detach"])
    config = json.loads((bot / "channels.json").read_text(encoding="utf-8"))
    check("so the config no longer has that channel",
          [row["name"] for row in config.get("channels") or []] == ["LootZoneIndia11"],
          config.get("channels"))
    restore(saved)


def test_page_markup():
    print("\n== the page has the list, the buttons and the honest wording ==")
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    for ids, label in (
            (["ownchannels", "ownlist", "ownsummary", "owncount"], "our-channels card"),
            (["owninput", "ownsearch"], "the attach box and the filter"),
            (["sglist", "sgmsg"], "the suggestion panel")):
        check(label + " exists", all(f'id="{one}"' in html for one in ids),
              [one for one in ids if f'id="{one}"' not in html])
    for needle, label in (
            ("ownAttach(", "an attach button"),
            ("ownDetach(", "a detach button"),
            ("ownPosting(", "a pause/resume button"),
            ("ownSuggest(", "a suggest button"),
            ("ownAddSource(", "add-as-source on a suggestion"),
            ("/api/own/attach", "the attach endpoint"),
            ("/api/own/suggest", "the suggest endpoint"),
            ("loadOwn()", "the card loads with the page")):
        check(label + " exists", needle in html, needle)
    check("the card says attaching never joins or posts",
          "Attaching never joins a channel and never posts anything" in html, "")
    check("the pause line says nothing is replayed",
          "nothing is replayed" in html, "")
    check("the suggestion panel says it is read-only",
          "Read-only: nothing is joined, nothing is forwarded" in html, "")


def test_bot_wiring():
    print("\n== the bot honours a pause and answers the suggestion request ==")
    bot = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    check("the fan-out reads the pause list", "paused_for_posting()" in bot, "")
    check("and a paused target gets no delivery row", "TARGET PAUSED" in bot, "")
    check("a post whose every target is paused is not 'no eligible targets'",
          "every target for this post is paused in the dashboard" in bot, "")
    check("a manual send refuses a paused channel with a reason",
          "every channel you picked is paused" in bot, "")
    check("the suggestion loop exists", "async def channel_suggest_loop" in bot, "")
    check("it asks Telegram for similar channels",
          "GetChannelRecommendationsRequest" in bot, "")
    check("it scans recent posts for mentions", "parse_mentions" in bot, "")
    check("it is started as its own task",
          "suggest_task = asyncio.create_task(channel_suggest_loop" in bot, "")
    check("and awaited on shutdown", "await suggest_task" in bot, "")
    check("its poll interval is configurable", 'SUGGEST_POLL_SECONDS' in bot, "")
    check("it never joins anything", "JoinChannelRequest" not in bot, "")
    check("the suggestion module is imported defensively",
          "the Suggest button is" in bot, "")


def main():
    test_attach_existing()
    test_attach_new()
    test_attach_refusals()
    test_detach_and_pause()
    test_paused_targets_cache()
    test_inventory()
    test_mentions()
    test_from_entities()
    test_build_and_exclude()
    test_request_roundtrip()
    test_api()
    test_page_markup()
    test_bot_wiring()
    print("\n" + ("CHANNEL ATTACH TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_channel_attach: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
