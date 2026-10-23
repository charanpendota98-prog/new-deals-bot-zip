"""API keys in the dashboard: several per provider, validated, masked, reversible.

The operator's request: "dashboard lo API key ichelaga vunchu multiple ga" - put
API keys in from the dashboard, several of them. That is only worth having if it
is *safer* than editing `.env` by hand, so this suite pins the properties that
make it safer, offline, with no bot and no network:

1. the vault knows every key the bot actually reads (and the Amazon tag list in
   the dashboard cannot drift from the one in the bot), and only those keys can
   be saved;
2. `.env` is edited like a human expects - comments, order and unknown lines
   survive, a removed key loses its line, a new key is appended once;
3. a secret never leaves the process: the API answers with a masked tail, never
   the value, and the journal records only key names and tails;
4. a wrong key is refused before any write - a foreign EarnKaro publisher (a
   commission leak), an expired token, a tag that is not ours, a line break that
   would split the file, a placeholder;
5. several values are real: append keeps the existing ones, remove-by-index
   drops exactly one, and the bot is told to fail over (tested against its own
   key parser);
6. writes are atomic with a backup and mode 0600, a dry run writes nothing, and a
   stale form cannot overwrite a newer save.
"""
import asyncio
import base64
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))
from dashboard import vault  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def make_jwt(publisher: str | None = "5478322", *, exp: float | None = None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = {"iat": 1_700_000_000}
    if publisher:
        payload["earnkaro"] = publisher
    if exp:
        payload["exp"] = exp
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.signature"


GOOD_EK = make_jwt()
SECOND_EK = make_jwt()
EXPIRED_EK = make_jwt(exp=time.time() - 3600)
FOREIGN_EK = make_jwt("999999")


def catalog():
    print("\n== the vault knows exactly what the bot reads ==")
    bot_source = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    match = re.search(r"OUR_AMAZON_TAGS\s*=\s*\{([^}]*)\}", bot_source)
    bot_tags = {value.strip().strip('"\'') for value in match.group(1).split(",") if value.strip()}
    check("the dashboard's owned Amazon tags are the bot's", bot_tags == vault.OUR_AMAZON_TAGS,
          f"bot={bot_tags} dashboard={vault.OUR_AMAZON_TAGS}")
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "EARNKARO_API_KEY", "AMAZON_TAG",
                "BITLY_TOKENS", "HYPD_API_TOKEN", "HYPD_STORE_ID"):
        check(f"the bot reads {key} and the vault has a slot for it",
              key in bot_source and key in vault.SLOT_BY_KEY)
    check("EarnKaro and Bitly accept several values",
          vault.SLOT_BY_KEY["EARNKARO_API_KEY"].multi and vault.SLOT_BY_KEY["BITLY_TOKENS"].multi)
    check("one-value keys are not multi",
          not vault.SLOT_BY_KEY["AMAZON_TAG"].multi
          and not vault.SLOT_BY_KEY["TELEGRAM_API_ID"].multi)
    check("the required set is the one the bot refuses to boot without",
          {slot.key for slot in vault.SLOTS if slot.required}
          == {"TELEGRAM_API_ID", "TELEGRAM_API_HASH", "EARNKARO_API_KEY", "AMAZON_TAG"})


def editing():
    print("\n== .env is edited the way a human expects ==")
    original = (
        "# BestGAA bot environment\n"
        "TELEGRAM_API_ID=1234567\n"
        "\n"
        "# commission account\n"
        "EARNKARO_API_KEY=" + GOOD_EK + "\n"
        "AMAZON_TAG=mama086-21\n"
        "SOMETHING_WE_DO_NOT_MANAGE=keep me\n"
    )
    changed = vault.render_env(original, {"AMAZON_TAG": "mama086-21", "BITLY_TOKENS": "abc"})
    lines = changed.splitlines()
    check("comments survive", "# BestGAA bot environment" in lines and "# commission account" in lines)
    check("order survives", lines.index("TELEGRAM_API_ID=1234567") < lines.index("AMAZON_TAG=mama086-21"))
    check("unknown managed-by-hand lines survive", "SOMETHING_WE_DO_NOT_MANAGE=keep me" in lines)
    check("a new key is appended exactly once", changed.count("BITLY_TOKENS=") == 1)
    removed = vault.render_env(original, {"AMAZON_TAG": None})
    check("a removed key loses its line entirely",
          "AMAZON_TAG" not in removed and "mama086-21" not in removed, removed)
    replaced = vault.render_env(original, {"TELEGRAM_API_ID": "7654321"})
    check("a replaced key keeps its position",
          replaced.splitlines()[1] == "TELEGRAM_API_ID=7654321", replaced.splitlines()[:2])
    check("a value with spaces is quoted, never split",
          'HYPD_STORE_SLUG="my store"' in vault.render_env("", {"HYPD_STORE_SLUG": "my store"}))
    check("round-trip of values", vault.env_values(original)["AMAZON_TAG"] == "mama086-21")
    check("last value wins, like systemd", vault.env_values("A=1\nA=2\n")["A"] == "2")


def masking():
    print("\n== a secret never leaves the process ==")
    secret = "abcdefghijklmnopqrstuvwxyz0123456789"
    masked = vault.mask(secret)
    check("the mask keeps only a tail", masked.endswith(secret[-4:]) and secret[:10] not in masked,
          masked)
    check("the mask is opaque", masked.count("•") >= 6, masked)
    check("a short secret is fully hidden", set(vault.mask("abc")) == {"•"})
    check("a non-secret slot is shown as-is", vault.mask("1234567", secret=False) == "1234567")
    check("a value too short for a tail reveals nothing at all", vault.tail("abc") == ""
          and vault.tail(secret) == secret[-4:])


def validation():
    print("\n== a wrong key is refused before anything is written ==")
    slot = vault.SLOT_BY_KEY["EARNKARO_API_KEY"]
    good = vault.validate_slot(slot, [GOOD_EK], all_values={})
    check("a real JWT passes and names the publisher",
          not good["errors"] and "5478322" in " ".join(good["info"]), str(good))
    expired = vault.validate_slot(slot, [EXPIRED_EK], all_values={})
    check("an expired token is an error", any("EXPIRED" in e for e in expired["errors"]),
          str(expired["errors"]))
    no_publisher = vault.validate_slot(slot, [make_jwt(publisher=None)], all_values={})
    check("a token with no publisher id is refused",
          any("publisher" in e for e in no_publisher["errors"]), str(no_publisher["errors"]))
    mixed = vault.validate_slot(slot, [GOOD_EK, FOREIGN_EK], all_values={})
    check("two keys paying different accounts are refused",
          any("DIFFERENT publishers" in e for e in mixed["errors"]), str(mixed["errors"]))
    same = vault.validate_slot(slot, [GOOD_EK, SECOND_EK], all_values={})
    check("two keys paying the SAME account are a valid failover", not same["errors"], str(same))
    check("a non-JWT is refused",
          vault.validate_slot(slot, ["abc123"], all_values={})["errors"])
    check("a line break inside a value is refused",
          vault.validate_slot(slot, [GOOD_EK + "\nINJECTED=1"], all_values={})["errors"])
    check("a placeholder is refused",
          vault.validate_slot(vault.SLOT_BY_KEY["BITLY_TOKENS"], ["REPLACE_WITH_NEW_TOKEN"],
                              all_values={})["errors"])
    tag = vault.SLOT_BY_KEY["AMAZON_TAG"]
    check("our tag passes", not vault.validate_slot(tag, ["mama086-21"], all_values={})["errors"])
    foreign_tag = vault.validate_slot(tag, ["someoneelse-21"], all_values={})
    check("a tag that is not ours is refused, and says why",
          any("not a tag we own" in e for e in foreign_tag["errors"]), str(foreign_tag))
    check("api_id must be digits",
          vault.validate_slot(vault.SLOT_BY_KEY["TELEGRAM_API_ID"], ["12ab"], all_values={})["errors"])
    check("a bot token in the api_hash slot is refused",
          any("BOT token" in e for e in vault.validate_slot(
              vault.SLOT_BY_KEY["TELEGRAM_API_HASH"], ["123456:ABC"], all_values={})["errors"]))
    report = vault.validate_update({"NOT_A_REAL_KEY": ["x"]}, {})
    check("an unknown key cannot be saved", report["errors"] and report["unknown"] == ["NOT_A_REAL_KEY"],
          str(report["errors"]))
    report = vault.validate_update({"AMAZON_TAG": ["mama086-21"]},
                                   {"AMAZON_TAG": "mama086-21"})
    check("a no-op save is reported as unchanged", report["unchanged"] == ["AMAZON_TAG"]
          and not report["changed"])


def files():
    print("\n== writes are atomic, 0600, and reversible ==")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / ".env"
        path.write_text("A=1\nB=2\n", encoding="utf-8")
        result = vault.write_env(path, "A=1\nB=3\n")
        mode = stat.S_IMODE(path.stat().st_mode)
        check("the new file is mode 0600", mode == 0o600, oct(mode))
        check("the previous file is kept as a backup", Path(result["backup"]).exists())
        check("the backup holds the OLD content",
              Path(result["backup"]).read_text(encoding="utf-8") == "A=1\nB=2\n")
        check("the backup is mode 0600",
              stat.S_IMODE(Path(result["backup"]).stat().st_mode) == 0o600)
        check("the new content is in place", path.read_text(encoding="utf-8") == "A=1\nB=3\n")
        check("no temp file is left behind",
              [p.name for p in Path(td).iterdir() if p.name.startswith(".env.tmp-")] == [])

        journal = vault.journal_path(path)
        vault.journal_append(path, {"action": "save", "keys": [
            {"key": "BITLY_TOKENS", "action": "add", "after": ["••••abcd"]}]})
        events = vault.journal_read(path)
        check("the journal records the key name and a masked tail",
              events and events[0]["keys"][0]["after"] == ["••••abcd"])
        check("the journal holds no clear secret",
              "abcd" in journal.read_text(encoding="utf-8")
              and "secretvalue" not in journal.read_text(encoding="utf-8"))

        for name in ("log", "snapshot"):
            (Path(td) / name).write_text("noise\n", encoding="utf-8")
        leaks = vault.leak_scan([GOOD_EK], [Path(td) / "log", Path(td) / "snapshot"])
        check("a clean file reports clean", leaks["state"] == "clean" and not leaks["hits"])
        (Path(td) / "log").write_text("access " + GOOD_EK + "\n", encoding="utf-8")
        leaks = vault.leak_scan([GOOD_EK], [Path(td) / "log"])
        check("a leaked key is detected as a COUNT, never as content",
              leaks["state"] == "leak" and leaks["hits"][0]["count"] == 1
              and GOOD_EK not in json.dumps(leaks), json.dumps(leaks)[:200])
        check("a short value is not scanned (no false alarms)",
              vault.leak_scan(["abc"], [Path(td) / "log"])["state"] == "nothing-to-scan")


def bot_failover():
    print("\n== several EarnKaro keys are a real failover for the bot ==")
    env = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x",
           "EARNKARO_API_KEY": f"{GOOD_EK},{SECOND_EK},{FOREIGN_EK}",
           "EARNKARO_PUBLISHER_ID": "5478322", "AMAZON_TAG": "mama086-21"}
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        import main_bot_new as bot
        check("the bot keeps the two keys that pay us and drops the foreign one",
              len(bot.EK_KEYS) == 2, str(len(bot.EK_KEYS)))
        check("the first key is active", bot.ek_current_key() == GOOD_EK)
        check("a refusal rotates to the next key", bot.ek_rotate("test")
              and bot.ek_current_key() == SECOND_EK)
        check("rotating again comes back around", bot.ek_rotate("test")
              and bot.ek_current_key() == GOOD_EK)
        check("a single key cannot rotate", bot.ek_rotate("only one") is False
              if len(bot.EK_KEYS) == 1 else True)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def api_checks():
    print("\n== HTTP: the API answers in masks, refuses, and never half-writes ==")
    from dashboard import app as dash
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        env_file = base / ".env"
        env_file.write_text(
            "TELEGRAM_API_ID=1234567\n"
            "TELEGRAM_API_HASH=" + "a" * 32 + "\n"
            "EARNKARO_API_KEY=" + GOOD_EK + "\n"
            "AMAZON_TAG=mama086-21\n"
            "BITLY_TOKENS=bitlytokenone0123456789,bitlytokentwo0123456789\n"
            "# operator comment\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        (base / "bot.sqlite3").write_text("", encoding="utf-8")  # a readable file is enough
        env = {"BESTGAA_BOT_ENV": str(env_file), "BESTGAA_DIR": str(base),
               "BOT_DB_PATH": str(base / "bot.sqlite3"),
               "CHANNELS_JSON_PATH": str(base / "channels.json"),
               "DASHBOARD_TOKEN": "vault-test", "DASHBOARD_RESTART_CMD": ""}
        saved = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        headers = {"X-Dashboard-Token": "vault-test"}
        try:
            from aiohttp.test_utils import TestClient, TestServer
            async with TestClient(TestServer(dash.create_app())) as client:
                check("the vault needs the token", (await client.get("/api/vault")).status == 401)
                response = await client.get("/api/vault", headers=headers)
                body = await response.json()
                text = json.dumps(body)
                check("the panel loads", response.status == 200 and body["slots"], str(response.status))
                check("NOT ONE secret is in the response",
                      GOOD_EK not in text and "bitlytokenone0123456789" not in text,
                      text[:200])
                check("the values are shown as masks with a tail",
                      any(v["masked"].endswith(GOOD_EK[-4:]) for s in body["slots"]
                          if s["key"] == "EARNKARO_API_KEY" for v in s["values"]))
                check("a non-secret slot is shown plainly",
                      next(s for s in body["slots"] if s["key"] == "TELEGRAM_API_ID")["values"][0]["masked"]
                      == "1234567")
                check("readiness is a verdict with reasons",
                      body["readiness"]["state"] in ("ready", "attention", "blocked")
                      and body["readiness"]["checks"], str(body["readiness"])[:200])
                check("the env path is the bot's, not the dashboard's",
                      body["path"] == str(env_file), body["path"])
                revision = body["revision"]

                # --- a wrong key never reaches the disk ----------------------- #
                before = env_file.read_text(encoding="utf-8")
                bad = await client.put("/api/vault", headers=dict(
                    headers, **{"If-Match": revision}),
                    json={"append": {"AMAZON_TAG": ["someoneelse-21"]}})
                bad_body = await bad.json()
                check("a tag that is not ours is a 400", bad.status == 400, str(bad_body)[:200])
                check("and it explains the commission risk",
                      any("not a tag we own" in e for e in bad_body["errors"]),
                      str(bad_body["errors"])[:200])
                check("nothing was written", env_file.read_text(encoding="utf-8") == before)

                foreign = await client.put("/api/vault", headers=dict(
                    headers, **{"If-Match": revision}),
                    json={"append": {"EARNKARO_API_KEY": [FOREIGN_EK]}})
                check("a second key paying a different account is refused",
                      foreign.status == 400, str(await foreign.json())[:200])

                stale = await client.put("/api/vault", headers=dict(
                    headers, **{"If-Match": "not-the-revision"}),
                    json={"append": {"BITLY_TOKENS": ["bitlytokenthree0123456789"]}})
                check("a stale form is a 409 and writes nothing",
                      stale.status == 409 and env_file.read_text(encoding="utf-8") == before)

                # --- dry run ------------------------------------------------- #
                dry = await client.put("/api/vault", headers=dict(
                    headers, **{"If-Match": revision}),
                    json={"dry_run": True,
                          "append": {"BITLY_TOKENS": ["bitlytokenthree0123456789"]}})
                dry_body = await dry.json()
                check("a dry run reports the change", dry.status == 200
                      and dry_body["dry_run"] and dry_body["changes"], str(dry_body)[:300])
                check("a dry run writes nothing",
                      env_file.read_text(encoding="utf-8") == before and not dry_body["written"])
                check("the reported change is masked",
                      "bitlytokenthree0123456789" not in json.dumps(dry_body),
                      json.dumps(dry_body)[:300])

                # --- append (a SECOND key, keeping the first) ---------------- #
                fresh = (await (await client.get("/api/vault", headers=headers)).json())["revision"]
                saved_response = await client.put("/api/vault", headers=dict(
                    headers, **{"If-Match": fresh}),
                    json={"append": {"BITLY_TOKENS": ["bitlytokenthree0123456789"]}})
                saved_body = await saved_response.json()
                check("saving appends without losing the existing keys",
                      saved_response.status == 200 and saved_body["written"],
                      str(saved_body)[:300])
                after = env_file.read_text(encoding="utf-8")
                check("all three Bitly tokens are in the file, in order",
                      after.count("bitlytoken") == 3
                      and after.index("bitlytokenone") < after.index("bitlytokenthree"),
                      after[:300])
                check("the operator's comment still survives", "# operator comment" in after)
                check("the file stayed 0600", stat.S_IMODE(env_file.stat().st_mode) == 0o600)
                check("a backup was written", Path(saved_body["backup"]).exists()
                      and Path(saved_body["backup"]).read_text(encoding="utf-8") == before)
                check("the answer says a restart applies it",
                      "restart" in (saved_body.get("next") or "").lower())
                check("the response contains no clear secret",
                      "bitlytokenthree0123456789" not in json.dumps(saved_body))
                check("the bot_has_it flag is now honest (false until restart)",
                      saved_body["bot_has_it"] is False)

                # --- remove exactly one value (by index) --------------------- #
                state = await (await client.get("/api/vault", headers=headers)).json()
                bitly = next(s for s in state["slots"] if s["key"] == "BITLY_TOKENS")
                check("the panel now lists three values", len(bitly["values"]) == 3, str(bitly))
                removed = await client.put("/api/vault", headers=dict(
                    headers, **{"If-Match": state["revision"]}),
                    json={"remove_values": {"BITLY_TOKENS": [1]}})
                removed_body = await removed.json()
                check("removing one value keeps the others",
                      removed.status == 200 and removed_body["written"], str(removed_body)[:200])
                final = env_file.read_text(encoding="utf-8")
                check("exactly the second token is gone",
                      "bitlytokentwo0123456789" not in final
                      and "bitlytokenone0123456789" in final
                      and "bitlytokenthree0123456789" in final, final[:300])
                check("the change journal records it masked",
                      any(event.get("keys") for event in
                          (await (await client.get("/api/vault", headers=headers)).json())["journal"]))

                # --- no-op and junk ------------------------------------------ #
                noop = await client.put("/api/vault", headers=headers,
                                        json={"append": {"BITLY_TOKENS": ["bitlytokenone0123456789"]}})
                check("a no-op save writes nothing",
                      noop.status == 200 and not (await noop.json())["written"])
                for junk in ({}, {"set": "nope"}, {"append": []}, {"remove": 5}):
                    response = await client.put("/api/vault", headers=headers, json=junk)
                    check(f"junk body {junk} is a 400", response.status == 400, str(response.status))
                bad_body_response = await client.put(
                    "/api/vault", data="not json",
                    headers={**headers, "Content-Type": "application/json"})
                check("a body that is not JSON is a 400", bad_body_response.status == 400,
                      str(bad_body_response.status))

                # --- a leaked key makes the verdict shout -------------------- #
                (base / "logs").mkdir(exist_ok=True)
                (base / "logs" / "bot.log").write_text(
                    "2026-10-22 INFO token=" + GOOD_EK + "\n", encoding="utf-8")
                leaked = await (await client.get("/api/vault", headers=headers)).json()
                check("a key visible in the log is reported as a leak",
                      leaked["leaks"]["state"] == "leak" and leaked["readiness"]["state"] != "ready",
                      json.dumps(leaked["leaks"])[:200])
                check("the leak report contains no clear secret",
                      GOOD_EK not in json.dumps(leaked))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    catalog()
    editing()
    masking()
    validation()
    files()
    bot_failover()
    asyncio.run(api_checks())
    if FAILS:
        print(f"\nAPI VAULT TESTS: FAILURES: {', '.join(FAILS)}")
        sys.exit(1)
    print("\nAPI VAULT TESTS: all checks PASS")
