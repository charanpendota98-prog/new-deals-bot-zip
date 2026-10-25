"""Post buttons: two join buttons under every Telegram post, configured safely.

A big channel ends every post with a "Join … ❤️" button; Telegram draws inline
URL buttons below the message and WhatsApp channels have none at all. These
checks pin the rules that make the feature safe to ship:

* validation never guesses - a non-Telegram host, plain http, a third button, a
  repeated URL, an empty or 41-char label are all errors, and an error means
  NOTHING is written;
* missing/corrupt config means zero buttons, and the bot's send path then passes
  buttons=None, i.e. today's bytes;
* the config writes atomically (tmp file + rename), like channels.json;
* with a saved row the bot builds real Telethon Button.url rows, in order.

Run: python3 test_post_buttons.py   (from the repo root)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# Importing the bot opens (and on first write creates) its database. Point it at a
# throwaway path: a stray `bestgaa/bestgaa.sqlite3` left in the checkout makes
# bot_paths treat the CHECKOUT as the bot directory (see test_bot_paths.py).
_SCRATCH = Path(tempfile.mkdtemp(prefix="buttons-bot-"))
os.environ.update(
    TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x",
    EARNKARO_API_KEY="k", AMAZON_TAG="",
    BOT_DB_PATH=str(_SCRATCH / "bot.sqlite3"),
    BOT_LOG_DIR=str(_SCRATCH / "logs"),
    BOT_MEDIA_DIR=str(_SCRATCH / "media"),
    CHANNELS_JSON_PATH=str(_SCRATCH / "channels.json"),
)
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "bestgaa"))

import post_buttons                       # noqa: E402

FAILURES: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASSED
    if ok:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name}  <- {detail}")


GOOD = [{"label": "Join Shpsy Loots ❤️", "url": "https://t.me/shpsyloots"},
        {"label": "Under 99rs Loot Deals 🤟", "url": "https://t.me/under99rs"}]


def main() -> int:
    print("validation refuses to guess")
    cleaned, errors = post_buttons.validate_buttons(GOOD)
    check("two Telegram buttons are valid", cleaned == GOOD and errors == [], (cleaned, errors))
    cleaned, errors = post_buttons.validate_buttons(None)
    check("no config means no buttons and no error", cleaned == [] and errors == [], errors)
    cleaned, errors = post_buttons.validate_buttons([
        {"label": "Join", "url": "https://evil.example/join"}])
    check("a join button that leaves Telegram is refused",
          cleaned == [] and any("not a Telegram host" in e for e in errors), errors)
    cleaned, errors = post_buttons.validate_buttons([
        {"label": "Join", "url": "http://t.me/insecure"}])
    check("plain http is refused", cleaned == [] and any("https" in e for e in errors), errors)
    cleaned, errors = post_buttons.validate_buttons([
        {"label": "a", "url": "https://t.me/x"}, {"label": "b", "url": "https://t.me/y"},
        {"label": "c", "url": "https://t.me/z"}])
    check("a third button is refused", any("at most 2" in e for e in errors), errors)
    cleaned, errors = post_buttons.validate_buttons([
        {"label": "a", "url": "https://t.me/x"}, {"label": "b", "url": "https://t.me/X"}])
    check("the same URL twice is refused", any("repeats" in e for e in errors), errors)
    cleaned, errors = post_buttons.validate_buttons([{"label": "", "url": "https://t.me/x"}])
    check("an empty label is refused", any("needs a label" in e for e in errors), errors)
    cleaned, errors = post_buttons.validate_buttons(
        [{"label": "x" * 41, "url": "https://t.me/x"}])
    check("a 41-char label is refused", any("40" in e for e in errors), errors)
    cleaned, errors = post_buttons.validate_buttons(
        [{"label": "join\x07now", "url": "https://t.me/x"}])
    check("control characters are refused", any("control" in e for e in errors), errors)
    cleaned, errors = post_buttons.validate_buttons("not a list")
    check("a non-list is refused", errors == ["buttons must be a list"], errors)
    cleaned, errors = post_buttons.validate_buttons([
        {"label": "  Join us  ", "url": "  https://t.me/addlist/5V7_ViAGDxAwNTI1  "}])
    check("whitespace is trimmed and the folder link is inside the allowlist",
          cleaned == [{"label": "Join us",
                       "url": "https://t.me/addlist/5V7_ViAGDxAwNTI1"}], cleaned)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "post_buttons.json"
        print("the config file is tolerant and atomic")
        check("a missing file means zero buttons", post_buttons.load_buttons(path) == [])
        path.write_text("{not json", encoding="utf-8")
        check("a corrupt file means zero buttons, never a crash",
              post_buttons.load_buttons(path) == [])
        path.write_text(json.dumps({"buttons": GOOD + GOOD}), encoding="utf-8")
        check("an oversized file loads only the first two",
              post_buttons.load_buttons(path) == GOOD)
        target, errors = post_buttons.save_buttons(GOOD, path)
        check("saving writes the file", target == path and errors == [] and path.is_file())
        check("and what is saved is what loads", post_buttons.load_buttons(path) == GOOD)
        before = path.read_text(encoding="utf-8")
        target, errors = post_buttons.save_buttons(
            [{"label": "x", "url": "https://evil.example"}], path)
        check("a rejected save changes nothing", errors and path.read_text(encoding="utf-8") == before)
        check("and leaves no tmp litter",
              not list(Path(tmp).glob(".post-buttons-*")), list(Path(tmp).iterdir()))
        check("the saved shape is versioned JSON",
              json.loads(path.read_text(encoding="utf-8"))["version"]
              == post_buttons.SCHEMA_VERSION)

        print("presets come from our own channels, never from a typed URL")
        os.environ["CHANNELS_JSON_PATH"] = str(Path(tmp) / "channels.json")
        (Path(tmp) / "channels.json").write_text(json.dumps({"version": 1, "channels": [
            {"name": "electroshelf", "enabled": True, "categories": ["electronics"],
             "sources": ["deals"]},
            {"name": "offshelf", "enabled": False, "categories": [], "sources": ["deals"]},
            {"name": "https://t.me/+PrivateInvite", "enabled": True, "categories": [],
             "sources": ["deals"]},
        ]}), encoding="utf-8")
        presets = post_buttons.preset_buttons()
        urls = [entry["url"] for entry in presets]
        check("enabled public channels become one-click presets",
              "https://t.me/electroshelf" in urls, urls)
        check("a disabled channel is not offered", "https://t.me/offshelf" not in urls, urls)
        check("a private invite is not offered as a join button",
              not any("+PrivateInvite" in u for u in urls), urls)
        check("the folder link rides along from the bot's own constant",
              any("addlist" in u for u in urls), urls)
        check("preset labels fit Telegram's button",
              all(len(entry["label"]) <= post_buttons.MAX_LABEL for entry in presets), presets)

        print("the send path is unchanged until an operator saves a row")
        import main_bot_new as bot
        check("with the demo row the bot builds two Telethon button rows",
              len(bot.post_inline_buttons()) == 2)
        row = bot.post_inline_buttons()[0][0]
        # Telethon renamed the URL-button type in 1.45; read both shapes, exactly
        # like main_bot_new.button_url() reads buttons on INBOUND posts.
        inner = getattr(row, "button", None) or getattr(row, "type", None)
        row_url = getattr(row, "url", None) or getattr(inner, "url", "")
        check("each row is a URL button carrying label and url",
              getattr(row, "text", "") == GOOD[0]["label"] and row_url == GOOD[0]["url"],
              repr(row))
        post_buttons.save_buttons([], path)
        check("clearing the config returns [] (buttons=None downstream)",
              bot.post_inline_buttons() == [])
        path.unlink()
        check("a deleted config also returns []", bot.post_inline_buttons() == [])

    print(f"\ntest_post_buttons: {PASSED} checks passed, {len(FAILURES)} failed")
    if FAILURES:
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
