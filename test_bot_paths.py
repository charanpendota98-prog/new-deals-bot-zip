"""One resolver for "where is the bot?" - and every consumer asks it.

The bug this suite exists for (2026-10-23, three symptoms, one cause):

* the deploy report's section 6 said `state=no_channel` for every shelf;
* the dashboard saved watch entries into `<checkout>/bestgaa/channels.json`
  while the running bot read `.../bestgaa-bot/bestgaa-bot/channels.json`;
* `insight health`, `quality_audit` and `coverage_audit` read a database that
  does not exist in the checkout, so they reported a bot that was not there.

Each component had its own guess and most guesses were relative to the CODE
(`Path(__file__).parent`), not to the BOT. On a server the code is a git
checkout and the bot is an app directory, so every one of those guesses was
silently wrong, and none of them said so.

`bestgaa/bot_paths.py` now answers the question once, in a fixed order, and can
explain itself. This suite builds a fake server (an app directory with `.env`,
a database and `channels.json`; a separate checkout that has neither) and
asserts that every consumer lands on the app directory, that the running bot's
own behaviour is unchanged, and that a wrong answer is loud instead of quiet.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILS = []


def check(name, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def make_server_layout():
    """A bot app dir and a separate checkout, exactly like the server."""
    base = Path(tempfile.mkdtemp(prefix="botpaths-"))
    bot = base / "bestgaa-bot" / "bestgaa-bot"
    bot.mkdir(parents=True)
    (bot / ".env").write_text(
        "TELEGRAM_API_ID=1234567\n"
        f"TELEGRAM_API_HASH={'a' * 32}\n"
        "EARNKARO_API_KEY=test\n"
        "AMAZON_TAG=mamba-21\n", encoding="utf-8")
    conn = sqlite3.connect(bot / "bestgaa.sqlite3")
    conn.execute("CREATE TABLE deliveries (target TEXT, status TEXT)")
    conn.commit()
    conn.close()
    (bot / "channels.json").write_text(
        json.dumps({"version": 1, "channels": [{"name": "MyShelf", "sources": ["*"]}],
                    "whatsapp": [], "watch": [{"channel": "DealSourceX",
                                               "targets": ["*"], "enabled": True}]}),
        encoding="utf-8")
    (bot / "logs").mkdir()
    (bot / "logs" / "bot.log").write_text("CATEGORY | electronics | routed\n", encoding="utf-8")
    (bot / "routing_snapshot.json").write_text(json.dumps({"category_rules_version": 3}),
                                               encoding="utf-8")
    checkout = base / "checkout"
    (checkout / "bestgaa").mkdir(parents=True)
    return bot, checkout


def run(code, checkout, env_extra=None):
    """Run the REAL code, with the fake checkout as cwd and only systemd's env.

    The module under test is imported from this repository (that is the code
    being shipped); `checkout` stands in for "the dashboard's WorkingDirectory",
    and the environment is exactly what the unit gives it - no BOT_DB_PATH, no
    PYTHONPATH, nothing that would make the resolution trivially right.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/root"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(env_extra or {})
    script = (
        "import os, sys, json\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'bestgaa')!r})\n"
        "import bot_paths\n"
        + code
    )
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, cwd=str(checkout))


def test_the_resolver_picks_the_bot_not_the_checkout():
    print("\n== a checkout asks, the app directory answers ==")
    bot, checkout = make_server_layout()
    # The app directory has to be one the resolver knows. Install it where the
    # resolver looks, or point BESTGAA_DIR at it - both are real server setups.
    out = run("print(json.dumps(bot_paths.describe()))", checkout,
              {"BESTGAA_DIR": str(bot)})
    rows = {row["name"]: row for row in json.loads(out.stdout)}
    check("BESTGAA_DIR is honoured first", rows["bot directory"]["path"] == str(bot),
          out.stdout + out.stderr)
    check("the database is the bot's, not the checkout's",
          rows["database"]["path"] == str(bot / "bestgaa.sqlite3"), rows["database"])
    check("channels.json is the bot's",
          rows["channels.json"]["path"] == str(bot / "channels.json"), rows["channels.json"])
    check(".env is the bot's", rows[".env"]["path"] == str(bot / ".env"), rows[".env"])
    check("the log is the bot's",
          rows["bot log"]["path"] == str(bot / "logs" / "bot.log"), rows["bot log"])
    check("the resolver explains which rule fired",
          "BESTGAA_DIR" in out.stdout, out.stdout)

    # BOT_DB_PATH alone must be enough: that is what an operator pastes from a
    # service file, and it must drag channels.json along with it.
    out = run("print(json.dumps(bot_paths.describe()))", checkout,
              {"BOT_DB_PATH": str(bot / "bestgaa.sqlite3")})
    rows = {row["name"]: row for row in json.loads(out.stdout)}
    check("BOT_DB_PATH alone puts channels.json beside the database",
          rows["channels.json"]["path"] == str(bot / "channels.json"), rows["channels.json"])
    check("and the bot directory is the database's parent",
          rows["bot directory"]["path"] == str(bot), rows["bot directory"])

    # With no environment at all, the known install layout is the answer - and
    # the checkout that ran the query never becomes it.
    out = run(
        f"bot_paths.INSTALL_HINTS = ({str(bot)!r},)\n"
        "print(json.dumps(bot_paths.describe()))", checkout)
    rows = {row["name"]: row for row in json.loads(out.stdout)}
    check("with no configuration the installed layout wins over the checkout",
          rows["bot directory"]["path"] == str(bot), out.stdout + out.stderr)
    check("and the rule that fired is named",
          "installed layout" in rows["bot directory"]["rule"], rows["bot directory"])

    # The bot's OWN .env is the last word on where its data lives.
    moved = bot.parent / "data"
    moved.mkdir()
    (bot / ".env").write_text((bot / ".env").read_text()
                              + f"BOT_DB_PATH={moved / 'live.sqlite3'}\n", encoding="utf-8")
    out = run("print(str(bot_paths.db_path()))", checkout, {"BESTGAA_DIR": str(bot)})
    check("the bot's .env can move the database (and the dashboard follows it)",
          out.stdout.strip() == str(moved / "live.sqlite3"), out.stdout + out.stderr)


def test_the_bot_itself_is_unchanged():
    print("\n== the running bot keeps resolving to its own directory ==")
    bot, checkout = make_server_layout()
    # From INSIDE the app directory, with no env at all: the module's directory
    # is a bot directory, so nothing changes for the bot.
    for name in ("main_bot_new.py", "channels_config.py", "bot_paths.py"):
        shutil.copy2(ROOT / "bestgaa" / name, bot / name)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/root"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import bot_paths, channels_config\n"
         "print(bot_paths.bot_dir())\n"
         "print(channels_config.channels_json_path())\n"
         "print(channels_config.snapshot_path())\n"],
        capture_output=True, text=True, env=env, cwd=str(bot))
    lines = proc.stdout.strip().splitlines()
    check("the bot resolves to its own directory with no configuration",
          lines and lines[0] == str(bot), proc.stdout + proc.stderr)
    check("and channels_config follows it (the dashboard's file, not another)",
          len(lines) > 1 and lines[1] == str(bot / "channels.json"), lines)
    check("the snapshot stays beside channels.json",
          len(lines) > 2 and lines[2] == str(bot / "routing_snapshot.json"), lines)


def test_every_consumer_agrees():
    print("\n== the dashboard, the reports and the audit read the SAME files ==")
    bot, checkout = make_server_layout()
    # The checkout gets a copy of the code, exactly as `git clone` would.
    for name in ("bot_paths.py", "channels_config.py"):
        shutil.copy2(ROOT / "bestgaa" / name, checkout / "bestgaa" / name)
    env = {"BESTGAA_DIR": str(bot)}
    code = (
        "import json\n"
        "from dashboard import app as dash, insight\n"
        "sys.path.insert(0, %r)\n"
        "import category_report, quality_audit, coverage_audit\n"
        "print(json.dumps({\n"
        "  'app.db': str(dash.db_path()),\n"
        "  'app.dir': str(dash.bot_app_dir()),\n"
        "  'app.env': str(dash.bot_env_path()),\n"
        "  'insight.db': str(insight._default_paths()['db']),\n"
        "  'insight.channels': str(insight._default_paths()['channels_json']),\n"
        "  'report.db': str(category_report.default_db()),\n"
        "  'report.log': str(category_report.default_log()),\n"
        "  'report.channels': str(category_report.default_channels_json()),\n"
        "  'report.env': str(category_report.default_env_file()),\n"
        "  'audit.env': [str(p) for p in quality_audit.bot_env_candidates(None)],\n"
        "  'coverage.db': str(coverage_audit.default_db()),\n"
        "}))\n" % str(ROOT / "ops"))
    proc = run(code, checkout, env)
    try:
        got = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        got = {}
    if not got:
        check("the consumers import and answer", False, proc.stdout + proc.stderr)
        return
    check("dashboard and ops reports open the same database",
          {got["app.db"], got["insight.db"], got["report.db"], got["coverage.db"]}
          == {str(bot / "bestgaa.sqlite3")}, got)
    check("and the same channels.json",
          got["insight.channels"] == got["report.channels"] == str(bot / "channels.json"), got)
    check("and the same .env",
          got["app.env"] == got["report.env"] == str(bot / ".env")
          and got["audit.env"][0] == str(bot / ".env"), got)
    check("and the same log",
          got["report.log"] == str(bot / "logs" / "bot.log"), got)
    check("the dashboard's app dir is the bot's",
          got["app.dir"] == str(bot), got)


def test_a_no_channel_verdict_names_the_file_it_read():
    print("\n== 'no_channel' can never be a silent wrong-path answer ==")
    bot, checkout = make_server_layout()
    shutil.copy2(ROOT / "ops" / "category_report.py", checkout / "ops" / "category_report.py") \
        if (checkout / "ops").exists() else None
    (checkout / "ops").mkdir(exist_ok=True)
    for name in ("category_report.py", "quality_audit.py"):
        shutil.copy2(ROOT / "ops" / name, checkout / "ops" / name)
    for name in ("bot_paths.py", "channels_config.py"):
        shutil.copy2(ROOT / "bestgaa" / name, checkout / "bestgaa" / name)
    shutil.copy2(ROOT / "bestgaa" / "category_rules.json", checkout / "bestgaa" / "category_rules.json")

    # (a) the bot directory is found: the report says which channels.json it read
    proc = subprocess.run(
        [sys.executable, str(checkout / "ops" / "category_report.py"), "--hours", "1"],
        capture_output=True, text=True, cwd=str(checkout),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HOME": os.environ.get("HOME", "/root"),
             "BESTGAA_DIR": str(bot), "PYTHONDONTWRITEBYTECODE": "1"})
    check("the report prints where it read from",
          "CATEGORY REPORT | db=" in proc.stderr, proc.stderr[-300:])
    check("and names the bot's channels.json, not the checkout's",
          str(bot / "channels.json") in proc.stderr, proc.stderr[-300:])
    check("so the declared shelf is not reported as missing",
          "no channels.json there" not in proc.stderr, proc.stderr[-300:])

    # (b) when the file really is absent, it says so instead of failing quietly
    proc = subprocess.run(
        [sys.executable, str(checkout / "ops" / "category_report.py"), "--hours", "1",
         "--channels-json", str(checkout / "nowhere.json")],
        capture_output=True, text=True, cwd=str(checkout),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HOME": os.environ.get("HOME", "/root"),
             "BESTGAA_DIR": str(bot), "PYTHONDONTWRITEBYTECODE": "1"})
    check("a missing channels.json is announced with the fix",
          "no channels.json there" in proc.stderr and "BESTGAA_DIR" in proc.stderr,
          proc.stderr[-400:])

    # (c) --json stays machine readable (the announcement goes to stderr)
    proc = subprocess.run(
        [sys.executable, str(checkout / "ops" / "category_report.py"), "--hours", "1", "--json"],
        capture_output=True, text=True, cwd=str(checkout),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HOME": os.environ.get("HOME", "/root"),
             "BESTGAA_DIR": str(bot), "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        json.loads(proc.stdout)
        ok_json = True
    except ValueError:
        ok_json = False
    check("--json output stays pure JSON", ok_json, proc.stdout[:200])


def test_the_installer_and_the_deploy_pin_the_directory():
    print("\n== the install paths name the directory once ==")
    installer = (ROOT / "ops" / "install_dashboard_secure.sh").read_text(encoding="utf-8")
    deploy = (ROOT / "ops" / "deploy_and_verify.sh").read_text(encoding="utf-8")
    check("the dashboard installer pins BESTGAA_DIR in dashboard.env",
          "BESTGAA_DIR=$BOT_DIR" in installer and "dashboard.env" in installer, "")
    check("and proves the database it will read exists", "database present" in installer, "")
    check("the deploy exports BESTGAA_DIR for every python helper",
          "export BESTGAA_DIR" in deploy, "")
    check("and prints what the resolver chose",
          "bot_paths.py" in deploy and "resolver:" in deploy, "")
    check("section 7 asks the PAGE which bot directory it edits",
          "app_dir" in deploy and "the dashboard edits" in deploy, "")
    service = (ROOT / "dashboard" / "bestgaa-dashboard.service").read_text(encoding="utf-8")
    check("the unit explains where BESTGAA_DIR comes from", "BESTGAA_DIR" in service, "")
    for name in ("bestgaa/deploy_bestgaa.sh", "ops/repack_bundles.sh", "ops/build_deploy_zip.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        check(f"{name} ships bot_paths.py (channels_config imports it)", "bot_paths.py" in text, "")


def test_optional_keys_do_not_mask_real_problems():
    print("\n== the keys panel warns about problems, not about optional keys ==")
    sys.path.insert(0, str(ROOT))
    from dashboard import vault

    import base64

    def jwt(claims):
        def part(payload):
            raw = json.dumps(payload).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return f"{part({'alg': 'HS256'})}.{part(claims)}.sig"

    ready_keys = {"TELEGRAM_API_ID": "1234567", "TELEGRAM_API_HASH": "a" * 32,
                  "EARNKARO_API_KEY": jwt({"earnkaro": "5478322"}),
                  "AMAZON_TAG": "mama086-21"}
    verdict = vault.readiness(ready_keys, bot_running=True, bot_has_it=True, db_ok=True,
                              leaks={"hits": []})
    check("every required key present and valid is READY, not 'needs attention'",
          verdict["state"] == "ready", verdict["headline"])
    check("optional keys that are not set are listed, not warned about",
          verdict["warn"] == 0 and verdict["optional_off"] > 0, verdict["checks"])
    check("the headline still names them",
          "Optional, not set" in verdict["headline"], verdict["headline"])

    verdict = vault.readiness({**ready_keys, "HYPD_API_TOKEN": "abcdefgh"},
                              bot_running=True, bot_has_it=True, db_ok=True,
                              leaks={"hits": []})
    check("a real half-configuration IS a warning (token without store id)",
          verdict["state"] == "attention" and "HYPD minting" in verdict["headline"],
          verdict["headline"])

    verdict = vault.readiness({**ready_keys, "HYPD_STORE_ID": "93944"},
                              bot_running=True, bot_has_it=True, db_ok=True,
                              leaks={"hits": []})
    check("the other half is a warning too (store id without a token)",
          verdict["state"] == "attention" and "HYPD minting" in verdict["headline"],
          verdict["headline"])

    verdict = vault.readiness({**ready_keys, "HYPD_API_TOKEN": "abcdefgh",
                               "HYPD_STORE_ID": "93944"},
                              bot_running=True, bot_has_it=True, db_ok=True,
                              leaks={"hits": []})
    check("a complete HYPD set is quiet", verdict["state"] == "ready", verdict["headline"])

    # A missing REQUIRED key must still block: the fix must not silence anything real.
    broken = dict(ready_keys)
    broken.pop("AMAZON_TAG")
    verdict = vault.readiness(broken, bot_running=True, bot_has_it=True, db_ok=True,
                              leaks={"hits": []})
    check("a missing required key still blocks", verdict["state"] == "blocked",
          verdict["headline"])

    # And EVERY slot is judged, not a hand-picked four.
    slot_names = {slot.label for slot in vault.SLOTS}
    judged = {c["name"] for c in verdict["checks"]}
    check("every key in the vault is judged by the readiness verdict",
          slot_names <= judged, sorted(slot_names - judged))


def test_the_index_renders_the_optional_state():
    print("\n== the page shows 'optional', not a warning colour ==")
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    check("the readiness pill knows the optional state",
          "c.state==='off'" in html and "'optional'" in html, "")


def main():
    test_the_resolver_picks_the_bot_not_the_checkout()
    test_the_bot_itself_is_unchanged()
    test_every_consumer_agrees()
    test_a_no_channel_verdict_names_the_file_it_read()
    test_the_installer_and_the_deploy_pin_the_directory()
    test_optional_keys_do_not_mask_real_problems()
    test_the_index_renders_the_optional_state()
    print("\n" + ("BOT PATHS TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_bot_paths: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
