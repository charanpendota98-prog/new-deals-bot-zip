"""Post buttons, per channel: every channel's post offers the OTHER channels.

The request (2026-10-25), with a screenshot of a real post ending in
"Join Shopsy Loots ❤️ →":

  "mana existing and new ga add chesе channels ki easy ga cheselaga undali
   dashboard ... ah channel post inkoka channel buttons add cheyelaga advanced cheyu"

One global row was not enough: a post in LootZone must offer the other shelves,
not LootZone itself. So `post_buttons.json` grew from one row to one row per
channel, with four modes (`default` / `auto` / `custom` / `none`), and "auto" is
the one-press answer - build each channel's row from our OTHER channels.

What is pinned here:

* the old global row still works, byte for byte, for every channel that has no
  entry - and a row that links its own channel loses that button, not the row;
* `auto` excludes the channel itself, labels from the title, dedupes, honours the
  two-button limit, and never invents a handle;
* `custom` is validated like any button (Telegram host, https, 40 chars, no
  controls), and a button linking the channel it is posted in is refused;
* saving is all-or-nothing and atomic, and saving the default row no longer
  wipes the per-channel modes (that is the bug this rewrite closes);
* the bot's send path resolves the row FOR THE TARGET CHANNEL (pinned through the
  real `post_inline_buttons`), and `deliver` is called with that target;
* the dashboard endpoint previews the resolved row per channel through the same
  function, so the preview cannot disagree with what goes out.

Run: python3 test_channel_buttons.py   (from the repo root)
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))

import post_buttons                                            # noqa: E402

FAILS = []


def check(name, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


GOOD = [{"label": "Join Shopsy Loots ❤️", "url": "https://t.me/shpsyloots"}]
OURS = ["LootZoneIndia11", "under99deals11", "DailyStealsHub"]
TITLES = {"lootzoneindia11": "Loot Zone India", "under99deals11": "Under 99 Shelf",
          "dailystealshub": "Daily Steals Hub"}


def temp_config(payload=None):
    base = Path(tempfile.mkdtemp(prefix="buttons-"))
    path = base / "post_buttons.json"
    if payload is not None:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return base, path


# --------------------------------------------------------------------------- #
# 1. handles and the old global row
# --------------------------------------------------------------------------- #
def test_handles_and_default():
    print("\n== a handle is a handle, however it is written ==")
    check("a link, an @name and a bare name are one handle",
          post_buttons.handle_of("https://t.me/LootZoneIndia11")
          == post_buttons.handle_of("@LootZoneIndia11")
          == post_buttons.handle_of("LootZoneIndia11") == "lootzoneindia11", "")
    check("a message link still names the channel",
          post_buttons.handle_of("https://t.me/LootZoneIndia11/1234") == "lootzoneindia11", "")
    check("nonsense is not a public handle",
          not post_buttons.is_public_handle(post_buttons.handle_of("a b"))
          and not post_buttons.is_public_handle(post_buttons.handle_of("invite+code")), "")

    print("\n== the old row still works, and never links the channel it is in ==")
    base, path = temp_config({"version": 1, "buttons": GOOD})
    check("a channel with no entry gets the default row",
          post_buttons.resolve("under99deals11", path=path) == GOOD, "")
    rows = post_buttons.resolve("shpsyloots", path=path)
    check("the channel the button links loses that button (and only that)",
          rows == [], rows)
    check("no config at all still means no buttons",
          post_buttons.resolve("anything", path=base / "missing.json") == [], "")


# --------------------------------------------------------------------------- #
# 2. auto: every channel points at the others
# --------------------------------------------------------------------------- #
def test_auto():
    print("\n== auto builds the row from our OTHER channels ==")
    base, path = temp_config({"version": 2,
                              "per_channel": {"LootZoneIndia11": {"mode": "auto"}},
                              "auto_label": "Join {title} ❤️"})
    rows = post_buttons.resolve("LootZoneIndia11", ours=OURS, titles=TITLES, path=path)
    labels = [row["label"] for row in rows]
    urls = [row["url"] for row in rows]
    check("the channel itself is never in its own row",
          "https://t.me/LootZoneIndia11" not in urls, urls)
    check("the other channels are", "https://t.me/under99deals11" in urls, urls)
    check("the label comes from the channel title",
          labels and labels[0] == "Join Under 99 Shelf ❤️", labels)
    check("at most two (a reader reads two)", len(rows) == 2, rows)
    check("an invite/private value is skipped, not turned into a button",
          all("+" not in row["url"] for row in
              post_buttons.resolve("LootZoneIndia11", ours=[*OURS, "https://t.me/+secret"],
                                   titles=TITLES, path=path)), "")
    base2, path2 = temp_config({"per_channel": {"LootZoneIndia11": {"mode": "auto"}},
                                "auto_limit": 1})
    check("auto_limit is obeyed",
          len(post_buttons.resolve("LootZoneIndia11", ours=OURS, titles=TITLES,
                                   path=path2)) == 1, "")
    base3, path3 = temp_config({"per_channel": {"LootZoneIndia11": {"mode": "auto"}},
                                "auto_label": "Our shelf"})
    rows3 = post_buttons.resolve("LootZoneIndia11", ours=OURS, titles=TITLES, path=path3)
    check("a label without {title} is used as written",
          rows3 and rows3[0]["label"] == "Our shelf", rows3)


# --------------------------------------------------------------------------- #
# 3. custom, none, and the refusals
# --------------------------------------------------------------------------- #
def test_custom_none_and_validation():
    print("\n== custom rows are validated like any button ==")
    base, path = temp_config({"per_channel": {
        "LootZoneIndia11": {"mode": "custom", "buttons": [
            {"label": "Join Under 99 ❤️", "url": "https://t.me/under99deals11"}]},
        "under99deals11": {"mode": "none"}}})
    rows = post_buttons.resolve("LootZoneIndia11", ours=OURS, titles=TITLES, path=path)
    check("a custom row is used as saved",
          rows == [{"label": "Join Under 99 ❤️", "url": "https://t.me/under99deals11"}], rows)
    check("'none' really means none",
          post_buttons.resolve("under99deals11", ours=OURS, titles=TITLES, path=path) == [], "")

    print("\n== nothing is written when anything is wrong ==")
    bad_cases = {
        "a button that leaves Telegram": {"per_channel": {"LootZoneIndia11": {
            "mode": "custom", "buttons": [{"label": "x", "url": "https://evil.example"}]}}},
        "a button linking its own channel": {"per_channel": {"LootZoneIndia11": {
            "mode": "custom", "buttons": [{"label": "x", "url": "https://t.me/LootZoneIndia11"}]}}},
        "an empty custom row": {"per_channel": {"LootZoneIndia11": {"mode": "custom",
                                                                   "buttons": []}}},
        "an unknown mode": {"per_channel": {"LootZoneIndia11": {"mode": "sometimes"}}},
        "a handle that is not public": {"per_channel": {"a b": {"mode": "auto"}}},
        "a third button on the default row": {"buttons": GOOD + [
            {"label": "b", "url": "https://t.me/other1"},
            {"label": "c", "url": "https://t.me/other2"}]},
    }
    for label, payload in bad_cases.items():
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        _target, errors, _clean = post_buttons.save_config(payload, path)
        after = path.read_text(encoding="utf-8") if path.exists() else ""
        check(f"{label} is refused, and nothing is written",
              bool(errors) and before == after, errors)

    print("\n== a good config round-trips, atomically ==")
    base2, path2 = temp_config()
    payload = {"buttons": GOOD,
               "per_channel": {"LootZoneIndia11": {"mode": "auto"},
                               "under99deals11": {"mode": "none"}},
               "auto_label": "Join {title} ❤️", "auto_limit": 2}
    target, errors, cleaned = post_buttons.save_config(payload, path2)
    check("it saves", not errors and target == path2, errors)
    saved = json.loads(path2.read_text(encoding="utf-8"))
    check("the file carries the whole shape",
          saved["version"] == post_buttons.SCHEMA_VERSION
          and "per_channel" in saved and saved["auto_label"] == "Join {title} ❤️", list(saved))
    check("and reads back as saved",
          post_buttons.load_config(path2)["per_channel"]["under99deals11"]["mode"] == "none", "")
    check("no tmp litter is left",
          not list(Path(path2).parent.glob(".post-buttons-*")), list(Path(path2).parent.iterdir()))
    # The bug this rewrite closes: the old save path wrote a fixed two-key payload.
    post_buttons.save_buttons([{"label": "Only default", "url": "https://t.me/onlydefault"}],
                              path2)
    after = post_buttons.load_config(path2)
    check("saving the DEFAULT row no longer wipes the per-channel modes",
          after["per_channel"].get("lootzoneindia11", {}).get("mode") == "auto", after)
    check("and the default row is the new one",
          after["buttons"] == [{"label": "Only default",
                                "url": "https://t.me/onlydefault"}], after["buttons"])


# --------------------------------------------------------------------------- #
# 4. our channels, and what the page can offer
# --------------------------------------------------------------------------- #
def test_our_channels():
    print("\n== which channels a button may point at ==")
    base, _path = temp_config()
    saved_env = os.environ.get("CHANNELS_JSON_PATH")
    os.environ["CHANNELS_JSON_PATH"] = str(base / "channels.json")
    (base / "channels.json").write_text(json.dumps({"version": 1, "channels": [
        {"name": "electroshelf", "enabled": True, "sources": ["*"], "categories": []},
        {"name": "offshelf", "enabled": False, "sources": ["*"], "categories": []},
        {"name": "https://t.me/+PrivateInvite", "enabled": True, "sources": ["*"]},
    ]}), encoding="utf-8")
    (base / "telegram_channels.json").write_text(json.dumps({
        "schema": 1, "generated_at": 1, "channels": [
            {"id": 1, "title": "Loot Zone India", "username": "LootZoneIndia11",
             "admin": True, "creator": True, "kind": "channel"},
            {"id": 2, "title": "Someone Else", "username": "strangerchannel",
             "admin": False, "creator": False, "kind": "channel"},
        ]}), encoding="utf-8")
    names = [row["name"] for row in post_buttons.our_channels()]
    check("an enabled dashboard channel is offered", "electroshelf" in names, names)
    check("a disabled one is not", "offshelf" not in names, names)
    check("a private invite is not (a button needs a public link)",
          not any("+" in name for name in names), names)
    check("a channel we admin is offered with its title",
          "lootzoneindia11" in names, names)
    check("a stranger's channel is not", "strangerchannel" not in names, names)
    titles = post_buttons.title_map()
    if saved_env is None:
        os.environ.pop("CHANNELS_JSON_PATH", None)
    else:
        os.environ["CHANNELS_JSON_PATH"] = saved_env
    check("the title is what a button will read",
          titles.get("lootzoneindia11") == "Loot Zone India", titles)

    print("\n== the preview says where a row comes from ==")
    base2, path2 = temp_config({"buttons": GOOD,
                                "per_channel": {"lootzoneindia11": {"mode": "auto"},
                                                "under99deals11": {"mode": "none"}}})
    auto = post_buttons.effective_row("LootZoneIndia11", ours=OURS, titles=TITLES, path=path2)
    check("auto says it is built from the other channels",
          auto["mode"] == "auto" and "other channels" in auto["where"], auto)
    off = post_buttons.effective_row("under99deals11", ours=OURS, titles=TITLES, path=path2)
    check("none says the channel has no buttons",
          off["mode"] == "none" and not off["buttons"], off)
    defaulted = post_buttons.effective_row("dailystealshub", ours=OURS, titles=TITLES,
                                          path=path2)
    check("an unconfigured channel says it uses the default row",
          defaulted["mode"] == "default" and "default row" in defaulted["where"], defaulted)


# --------------------------------------------------------------------------- #
# 5. the bot: the row belongs to the channel it is posted in
# --------------------------------------------------------------------------- #
def test_bot_send_path():
    print("\n== the send path resolves the row for THAT channel ==")
    base, _path = temp_config()
    (base / ".env").write_text("TELEGRAM_API_ID=1\n" + "TELEGRAM_API_HASH=" + "x" * 32
                               + "\nEARNKARO_API_KEY=k\n", encoding="utf-8")
    (base / "channels.json").write_text(json.dumps({"version": 1, "channels": [
        {"name": "LootZoneIndia11", "enabled": True, "sources": ["*"], "categories": []},
        {"name": "under99deals11", "enabled": True, "sources": ["*"], "categories": []},
        {"name": "DailyStealsHub", "enabled": True, "sources": ["*"], "categories": []},
    ], "watch": [], "whatsapp": []}), encoding="utf-8")
    (base / "telegram_channels.json").write_text(json.dumps({
        "schema": 1, "generated_at": 1, "channels": [
            {"id": 1, "title": "Loot Zone India", "username": "LootZoneIndia11",
             "admin": True, "creator": True, "kind": "channel"},
            {"id": 2, "title": "Under 99 Shelf", "username": "under99deals11",
             "admin": True, "creator": False, "kind": "channel"},
        ]}), encoding="utf-8")
    (base / "post_buttons.json").write_text(json.dumps({
        "version": 2,
        "per_channel": {"LootZoneIndia11": {"mode": "auto"},
                        "under99deals11": {"mode": "auto"},
                        "DailyStealsHub": {"mode": "none"}},
        "auto_label": "Join {title} ❤️"}), encoding="utf-8")
    keys = ("BESTGAA_DIR", "BOT_DB_PATH", "CHANNELS_JSON_PATH", "BESTGAA_BOT_ENV",
            "BOT_LOG_DIR", "BOT_MEDIA_DIR", "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
            "EARNKARO_API_KEY", "AMAZON_TAG")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update({
        "BESTGAA_DIR": str(base), "BOT_DB_PATH": str(base / "bestgaa.sqlite3"),
        "CHANNELS_JSON_PATH": str(base / "channels.json"),
        "BESTGAA_BOT_ENV": str(base / ".env"), "BOT_LOG_DIR": str(base / "logs"),
        "BOT_MEDIA_DIR": str(base / "media"), "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "x" * 32, "EARNKARO_API_KEY": "k", "AMAZON_TAG": "",
    })
    sys.path.insert(0, str(ROOT / "bestgaa"))
    import main_bot_new as bot                                  # noqa: PLC0415

    loot = bot.post_inline_buttons("LootZoneIndia11")
    under = bot.post_inline_buttons("under99deals11")
    none = bot.post_inline_buttons("DailyStealsHub")
    def flatten(rows):
        """Telethon hands back [[button], [button]] - one button per row."""
        return [row[0] if isinstance(row, list) else row for row in rows]

    def urls(rows):
        out = []
        for button in flatten(rows):
            inner = getattr(button, "button", None) or getattr(button, "type", None)
            out.append(getattr(button, "url", None) or getattr(inner, "url", ""))
        return out
    loot_urls, under_urls = urls(loot), urls(under)
    check("a post in LootZone does not link LootZone",
          "https://t.me/LootZoneIndia11" not in loot_urls, loot_urls)
    check("and only ever points at our public channels",
          loot_urls and len(loot_urls) <= 2
          and all(url.startswith("https://t.me/") for url in loot_urls), loot_urls)
    check("the other shelf's post never links the other shelf itself",
          "https://t.me/under99deals11" not in under_urls, under_urls)
    check("'none' posts without buttons", none == [], none)
    labels = [getattr(button, "text", "") for button in flatten(under)]
    check("the label is the other channel's TITLE, not its handle",
          "Join Loot Zone India ❤️" in labels, labels)
    check("every label is a join label", all(label.startswith("Join ") for label in labels),
          labels)
    source = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    check("deliver() takes the target", "target: str | None = None) -> tuple[bool, str]" in source, "")
    check("and the row is resolved for it", "post_inline_buttons(target) or None" in source, "")
    check("the call site passes the channel it is sending to",
          "attempt_callback=send_attempt, target=target," in source, "")
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# --------------------------------------------------------------------------- #
# 6. the endpoint the page calls
# --------------------------------------------------------------------------- #
def test_api():
    print("\n== the page's button endpoints (temporary bot directory) ==")
    base, _path = temp_config()
    bot = base / "bestgaa-bot"
    bot.mkdir()
    (bot / ".env").write_text("TELEGRAM_API_ID=1234567\n" + "TELEGRAM_API_HASH=" + "a" * 32
                              + "\nEARNKARO_API_KEY=x.y.z\n", encoding="utf-8")
    (bot / "channels.json").write_text(json.dumps({"version": 1, "channels": [
        {"name": "LootZoneIndia11", "enabled": True, "sources": ["*"], "categories": []},
        {"name": "under99deals11", "enabled": True, "sources": ["*"], "categories": []},
        {"name": "DailyStealsHub", "enabled": True, "sources": ["*"], "categories": []},
    ], "watch": [], "whatsapp": []}), encoding="utf-8")
    (bot / "telegram_channels.json").write_text(json.dumps({
        "schema": 1, "generated_at": 1, "channels": [
            {"id": 1, "title": "Loot Zone India", "username": "LootZoneIndia11",
             "admin": True, "creator": True, "kind": "channel"},
            {"id": 2, "title": "Under 99 Shelf", "username": "under99deals11",
             "admin": True, "creator": False, "kind": "channel"},
        ]}), encoding="utf-8")
    keys = ("BESTGAA_DIR", "BOT_DB_PATH", "CHANNELS_JSON_PATH", "BESTGAA_BOT_ENV",
            "DASHBOARD_TOKEN")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update({"BESTGAA_DIR": str(bot), "BOT_DB_PATH": str(bot / "bestgaa.sqlite3"),
                       "CHANNELS_JSON_PATH": str(bot / "channels.json"),
                       "BESTGAA_BOT_ENV": str(bot / ".env"), "DASHBOARD_TOKEN": "test-token"})
    sys.path.insert(0, str(ROOT))
    from dashboard import app as dash                            # noqa: PLC0415

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer
        client = TestClient(TestServer(dash.create_app()))
        await client.start_server()
        headers = {"X-Dashboard-Token": "test-token"}
        out = {}
        r = await client.get("/api/post-buttons", headers=headers)
        out["get"] = (r.status, await r.json())
        r = await client.put("/api/post-buttons", headers=headers, json={
            "per_channel": [{"name": "LootZoneIndia11", "mode": "auto"},
                            {"name": "under99deals11", "mode": "auto"}]})
        out["auto"] = (r.status, await r.json())
        r = await client.put("/api/post-buttons", headers=headers, json={
            "per_channel": [{"name": "LootZoneIndia11", "mode": "custom", "buttons": [
                {"label": "Join myself", "url": "https://t.me/LootZoneIndia11"}]}]})
        out["self"] = (r.status, await r.json())
        r = await client.put("/api/post-buttons", headers=headers, json={})
        out["empty"] = (r.status, await r.json())
        r = await client.put("/api/post-buttons", headers=headers, json={
            "buttons": [{"label": "Join Shopsy Loots ❤️", "url": "https://t.me/shpsyloots"}]})
        out["default"] = (r.status, await r.json())
        r = await client.put("/api/post-buttons", headers=headers, json={
            "per_channel": [{"name": "under99deals11", "mode": "none"}]})
        out["none"] = (r.status, await r.json())
        await client.close()
        return out

    data = asyncio.run(scenario())
    view = data["get"][1]
    check("the endpoint lists our channels with a mode",
          view["channels"] and all("mode" in row for row in view["channels"]),
          view.get("channels"))
    check("and previews the row each one will carry",
          all("buttons" in row and "where" in row for row in view["channels"]), view.get("channels"))
    check("auto for a channel is saved", data["auto"][0] == 200, data["auto"][1].get("errors"))
    preview = {row["name"].lower(): row for row in data["auto"][1]["channels"]}
    check("its preview never contains the channel itself",
          "https://t.me/LootZoneIndia11" not in [row["url"] for row in
                                                 preview["lootzoneindia11"]["buttons"]],
          preview["lootzoneindia11"])
    check("a button linking its own channel is refused",
          data["self"][0] == 400
          and "cannot link the channel it is posted in" in " ".join(data["self"][1]["errors"]),
          data["self"][1].get("errors"))
    check("an empty body is refused (it would wipe everything)", data["empty"][0] == 400,
          data["empty"][1].get("errors"))
    check("saving only the default row keeps the per-channel modes",
          data["default"][0] == 200
          and {row["name"].lower() for row in data["default"][1]["per_channel"]} >= {
              "lootzoneindia11", "under99deals11"}, data["default"][1].get("errors"))
    check("and 'none' still removes a channel's row",
          data["none"][0] == 200
          and [row for row in data["none"][1]["channels"]
               if row["name"].lower() == "under99deals11"][0]["buttons"] == [],
          data["none"][1].get("channels"))
    restore_saved = saved
    for key, value in restore_saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_page_markup():
    print("\n== the page has the per-channel editor ==")
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    for needle, label in (
            ('id="pbchannels"', "the per-channel list"),
            ("pbSetMode(", "a mode selector"),
            ("pbAutoAll()", "the one-press auto-for-everyone button"),
            ("pbSaveCustom(", "the custom-row save"),
            ("pbRender()", "the renderer"),
            ("/api/post-buttons", "the endpoint")):
        check(label + " exists", needle in html, needle)
    check("the page explains auto never includes the channel itself",
          "never itself" in html, "")
    check("and says the preview is what a reader taps",
          "what a reader taps" in html, "")


def main():
    test_handles_and_default()
    test_auto()
    test_custom_none_and_validation()
    test_our_channels()
    test_bot_send_path()
    test_api()
    test_page_markup()
    print("\n" + ("CHANNEL BUTTON TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_channel_buttons: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
