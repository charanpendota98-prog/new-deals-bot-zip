"""Category-wise reporting: per-shelf truth from the live DB, log, config and rules.

Covers ops/category_report.py (the engine both the CLI and the dashboard's
/api/categories use) end to end: config -> shelf index, the real delivery ledger,
the pending backlog, the bot's own CATEGORY log lines, the findings matrix, the
CLI contract, and the HTTP endpoint. Also the three questions a total cannot
answer - whether the links a shelf published pay us, its IST-day trend, and how
the same shelf is configured on Telegram and on WhatsApp - plus the drill-down
rows, ops/category_watch.py (an alarm that speaks once), and the served
dashboard markup for all of it.

Why it matters: a shelf can sit empty for a week - target unresolved, rules
version stale, Amazon tag never declared - while Main keeps posting and every
aggregate number looks healthy. These checks are the difference between "the
electronics channel got nothing" and "the electronics channel got nothing, here
is the logged reason and the finding that names it".

Offline and read-only: a private temp database built with the bot's own Store
(so the schema is the real one), a synthetic log, and no Telegram, WhatsApp or
affiliate access. The suite asserts the database bytes are unchanged afterwards,
that the money vocabulary matches ops/conversion_report.py link shape by link
shape, and that a watchdog run writes nothing but its own state file.
"""
import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = Path(tempfile.mkdtemp(prefix="bestgaa-category-report-"))

# Isolate before importing the bot: importing it creates logs/ and a default DB
# next to the source. The gate already exports these; setdefault keeps its values.
for key, value in (("BOT_DB_PATH", str(WORK / "import.sqlite3")),
                   ("BOT_LOG_DIR", str(WORK / "logs")),
                   ("BOT_MEDIA_DIR", str(WORK / "media")),
                   ("TELEGRAM_SESSION", str(WORK / "session")),
                   ("TELEGRAM_API_ID", "1"), ("TELEGRAM_API_HASH", "x"),
                   ("EARNKARO_API_KEY", "k")):
    os.environ.setdefault(key, value)

sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import channels_config  # noqa: E402
import main_bot_new as bot  # noqa: E402
import category_report as cr  # noqa: E402
from dashboard import app as dash  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:400]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp(when: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


RULES = cr.load_rules()
REAL_KEYS = [str(c["key"]) for c in RULES.get("categories", [])]

# A one-category taxonomy: it lets a scenario be genuinely clean (every category
# reachable, nothing silent) so --strict can be proven to exit 0, not just to fail.
MINI_RULES = {
    "version": 42, "min_score": 3, "min_margin": 2,
    "categories": [{"key": "electronics", "label": "Electronics",
                   "aliases": [["earbuds", 5], ["wireless earbuds", 6]]}],
}


def make_db(path: Path, deliveries, queue_rows=(), pending=()) -> Path:
    """A real bot-schema database: bot.Store creates the tables, we fill them."""
    store = bot.Store(path)
    now = time.time()
    for index, (target, status, error, age) in enumerate(deliveries, start=1):
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (index, -100 - index, index, "deals", "done", 1, "", now - age,
             str(-100 - index), "Demo deal", json.dumps([target])))
        store.conn.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?)",
                           (index, target, status, 1, 1, error))
    for index, (targets, status, age) in enumerate(pending, start=len(deliveries) + 1):
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (index, -900 - index, index, "deals", status, 0, "", now - age,
             str(-900 - index), None, json.dumps(targets)))
    for index, (target, age) in enumerate(queue_rows, start=len(deliveries) + len(pending) + 1):
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (index, -800 - index, index, "deals", "done", 1, "", now - age,
             str(-800 - index), "Demo", json.dumps([target])))
    store.conn.commit()
    store.conn.close()
    return path


def write_log(path: Path, lines) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


SHELF_CONFIG = {"version": 1, "channels": [
    {"name": "ElectroShelf", "sources": ["deals"], "categories": ["electronics"],
     "enabled": True, "amazon_tag": False},
    {"name": "StyleShelf", "sources": ["deals"], "categories": ["fashion", "accessories"],
     "enabled": True, "amazon_tag": True},
    {"name": "OffShelf", "sources": ["deals"], "categories": ["grocery"],
     "enabled": False, "amazon_tag": False},
    {"name": "MyMainDeals", "sources": ["*"], "enabled": True, "amazon_tag": True},
]}


# ------------------------------------------------------- 1. config -> shelves --

def test_config_math():
    print("\n== config: channels.json + env -> which target keeps which category ==")
    view = cr.channels_view(SHELF_CONFIG, RULES)
    check("a shelf channel is listed under its own category (name normalized like the bot's)",
          [c["name"] for c in view["shelves"]["electronics"]] == ["electroshelf"], str(view["shelves"]))
    check("a two-category shelf is listed under both",
          [c["name"] for c in view["shelves"]["fashion"]] == ["styleshelf"]
          and [c["name"] for c in view["shelves"]["accessories"]] == ["styleshelf"])
    check("an unfiltered channel is Main, never a shelf",
          [c["name"] for c in view["main"]] == ["mymaindeals"], str(view["main"]))

    index = cr.target_categories(SHELF_CONFIG, RULES, {})
    check("the shelf target keeps exactly its category",
          index.get("electroshelf") == frozenset({"electronics"}), str(index))
    check("a two-category target keeps both",
          index.get("styleshelf") == frozenset({"fashion", "accessories"}), str(index))
    check("an unfiltered target maps to None (takes every category)",
          index.get("mymaindeals") is None, str(index))
    check("a disabled shelf still indexes its target (its ledger rows must count)",
          index.get("offshelf") == frozenset({"grocery"}), str(index))

    env_index = cr.target_categories(SHELF_CONFIG, RULES,
                                     {"TG_CATEGORY_GROCERY_TARGET": "@GroceryDeals"})
    check("an env-declared shelf target joins the index",
          env_index.get("grocerydeals") == frozenset({"grocery"}), str(env_index))
    check("env targets are normalized (@ stripped, lowercased)",
          cr.normalize_target("@GroceryDeals") == "grocerydeals")
    check("env_targets maps category -> declared target",
          cr.env_targets({"TG_CATEGORY_GROCERY_TARGET": "@GroceryDeals",
                          "AMAZON_TAG_CATEGORY_TARGETS": "grocerydeals"}) == {"grocery": "grocerydeals"})

    env = cr.load_env([str(write_log(WORK / "sample.env", [
        "# a comment", "TELEGRAM_API_ID=1", "EARNKARO_API_KEY=super-secret-token",
        "TG_CATEGORY_BEAUTY_TARGET=@BeautyDeals", "AMAZON_TAG_CATEGORY_TARGETS=beautydeals"]))])
    check("load_env keeps the file's category keys",
          {"TG_CATEGORY_BEAUTY_TARGET", "AMAZON_TAG_CATEGORY_TARGETS"} <= set(env), str(env))
    check("load_env keeps nothing else from the file",
          set(env) - {"TG_CATEGORY_BEAUTY_TARGET", "AMAZON_TAG_CATEGORY_TARGETS"} <= set(os.environ),
          str(sorted(env)))

    # The CLI and the dashboard must see the same shelves: one reads .env files,
    # the other also had the process environment, so the engine reads both.
    env_file = write_log(WORK / "merge.env", ["TG_CATEGORY_FOOTWEAR_TARGET=@FromFile"])
    os.environ["TG_CATEGORY_FOOTWEAR_TARGET"] = "@FromProcessEnv"
    os.environ["WA_CHANNEL_FOOTWEAR"] = "120363777@newsletter"
    try:
        merged = cr.load_env([str(env_file)])
        check("a .env file wins over the process environment (the deployed truth is the file)",
              merged.get("TG_CATEGORY_FOOTWEAR_TARGET") == "@FromFile", str(merged))
        process_only = cr.load_env([])
        check("a shelf declared only in the process environment is still seen by the CLI",
              process_only.get("WA_CHANNEL_FOOTWEAR") == "120363777@newsletter",
              str(sorted(process_only)))
        check("the merged view still keeps no credential",
              not any("secret" in str(v).lower() for v in merged.values()), str(merged))
    finally:
        os.environ.pop("TG_CATEGORY_FOOTWEAR_TARGET", None)
        os.environ.pop("WA_CHANNEL_FOOTWEAR", None)
    check("load_env never keeps a credential from the same file",
          "super-secret-token" not in json.dumps(env), str(env))

    broken = cr.load_rules(WORK / "does-not-exist.json")
    check("unreadable rules are a reported state, not a crash",
          broken["categories"] == [] and "cannot read" in broken.get("load_error", ""), str(broken))


# --------------------------------------------------- 2. the delivery ledger ----

def test_ledger():
    print("\n== ledger: per-shelf sends, skips, holds, backlog, unknown targets ==")
    db = make_db(WORK / "ledger.sqlite3", deliveries=[
        ("ElectroShelf", "sent", "", 60),
        ("ElectroShelf", "sent", "", 120),
        ("ElectroShelf", "skipped", "category target unresolved", 90),
        ("ElectroShelf", "sent", "FloodWaitError: 12s", 30),
        ("StyleShelf", "sent", "", 45),
        ("MyMainDeals", "sent", "", 10),
        ("MyMainDeals", "held", "manual review", 20),
        ("GhostChannel", "sent", "", 15),
    ], pending=[(["ElectroShelf", "MyMainDeals"], "pending", 5),
                (["StyleShelf"], "pending", 6)])

    report = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                             rules=RULES, env={}, snapshot={})
    by_key = {c["key"]: c for c in report["categories"]}
    electronics = by_key["electronics"]
    check("shelf sends are counted for that shelf only",
          electronics["deliveries"]["sent"] == 2, str(electronics["deliveries"]))
    check("a skip is a skip, and its reason is bucketed",
          electronics["deliveries"]["skipped"] == 2
          and electronics["skip_reasons"].get("target_unresolved") == 1
          and electronics["skip_reasons"].get("flood_wait (sent-with-error)") == 1,
          str(electronics["skip_reasons"]))
    check("'sent with an error' is never counted as a real send",
          electronics["deliveries"]["sent"] == 2, str(electronics))
    check("the last send time is the newest real send",
          electronics["last_sent_at"] > 0 and electronics["last_sent"].endswith("IST"),
          str(electronics["last_sent"]))
    check("Main counts only unfiltered targets",
          report["main"]["deliveries"]["sent"] == 1
          and report["main"]["deliveries"]["held"] == 1, str(report["main"]["deliveries"]))
    check("shelf sends never inflate Main",
          report["main"]["deliveries"]["sent"] == 1, str(report["main"]))

    style = by_key["fashion"]
    check("a two-category shelf counts the same delivery under both categories",
          style["deliveries"]["sent"] == 1
          and by_key["accessories"]["deliveries"]["sent"] == 1,
          f"{style['deliveries']} / {by_key['accessories']['deliveries']}")

    check("a target no config claims is reported, not silently dropped",
          report["unrouted_targets"] == ["target:GhostChannel"], str(report["unrouted_targets"]))
    check("an unknown target keeps the operator's spelling in the report",
          "GhostChannel" in report["unrouted_targets"][0], str(report["unrouted_targets"]))
    check("an unknown target produces a finding naming it",
          any(f["kind"] == "unknown_target" and "GhostChannel" in f["detail"]
              for f in report["findings"]), str(report["findings"]))

    check("the pending backlog is attributed per shelf",
          electronics["pending"] == 1 and by_key["fashion"]["pending"] == 1
          and report["main"]["pending"] == 1,
          f"{electronics['pending']} / {by_key['fashion']['pending']} / {report['main']['pending']}")

    grocery = by_key["grocery"]
    check("a configured-but-disabled shelf is reported as disabled, not unconfigured",
          grocery["state"] == "disabled", str(grocery["state"]))
    check("a disabled shelf still lists the target it would use",
          grocery["targets"] == ["offshelf"] and grocery["active_targets"] == [],
          str(grocery["targets"]))
    check("a shelf with no configuration at all is 'no_channel', a different problem",
          by_key["beauty"]["state"] == "no_channel" and by_key["beauty"]["targets"] == [],
          str(by_key["beauty"]["state"]))
    check("the disabled shelf's channels are listed with enabled=false",
          grocery["channels"] and grocery["channels"][0]["enabled"] is False)

    check("tagless targets are named (Amazon links are withheld there by policy)",
          electronics["tagless_targets"] == ["electroshelf"]
          and by_key["fashion"]["tagless_targets"] == [], str(electronics["tagless_targets"]))
    check("a tagless shelf yields an informational finding, not an actionable one",
          any(f["kind"] == "tagless_shelf" and f["severity"] == "info"
              and f["category"] == "electronics" for f in report["findings"]))

    before = sha(db)
    cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                    rules=RULES, env={}, snapshot={})
    check("the report leaves the database bytes untouched (read-only)", sha(db) == before)

    empty = cr.build_report(db=WORK / "no-such-db.sqlite3", log=None, hours=24,
                            channels_data=SHELF_CONFIG, rules=RULES, env={}, snapshot={})
    check("a missing database is a finding, not a crash",
          empty["db_readable"] is False
          and any(f["kind"] == "db_unreadable" for f in empty["findings"]), str(empty["findings"]))
    check("a missing database still reports every shelf from config",
          len(empty["categories"]) == len(REAL_KEYS), str(len(empty["categories"])))


# --------------------------------------------------------- 3. the bot's log ----

def test_log_signals():
    print("\n== log: CATEGORY ROUTE / WITHHELD / COLLISION / UNRESOLVED / DISABLED ==")
    now = time.time()
    log = write_log(WORK / "bot.log", [
        f"{stamp(now - 60)} [INFO] CATEGORY ROUTE | Electronics -> @ElectroShelf | queue=11",
        f"{stamp(now - 59)} [INFO] CATEGORY ROUTE | Electronics -> @ElectroShelf | queue=12",
        f"{stamp(now - 58)} [INFO] CATEGORY ROUTE | Clothing -> @StyleShelf | queue=13",
        f"{stamp(now - 57)} [INFO] CATEGORY WITHHELD | queue=14 category=Electronics "
        "reason=mixed_or_conflicting targets=ElectroShelf",
        f"{stamp(now - 56)} [INFO] CATEGORY WITHHELD | queue=15 category=electronics "
        "reason=insufficient_product_evidence targets=ElectroShelf",
        f"{stamp(now - 55)} [INFO] CATEGORY WITHHELD | queue=16 category=unknown "
        "reason=mixed_or_conflicting targets=ElectroShelf",
        f"{stamp(now - 54)} [WARNING] CATEGORY TARGET UNRESOLVED | @ElectroShelf not reachable; "
        "the deal still goes to the Main feed and other channels",
        f"{stamp(now - 53)} [WARNING] CATEGORY TARGET COLLISION | @StyleShelf is assigned to "
        "fashion, accessories; those category routes are disabled",
        # outside the 24h window: must be ignored
        f"{stamp(now - 48 * 3600)} [INFO] CATEGORY ROUTE | Electronics -> @ElectroShelf | queue=1",
        f"{stamp(now - 48 * 3600)} [WARNING] CATEGORY ROUTING DISABLED | rules unreadable",
        f"{stamp(now - 52)} [INFO] something else entirely | queue=99",
    ])
    report = cr.build_report(db=None, log=log, hours=24, channels_data=SHELF_CONFIG,
                             rules=RULES, env={}, snapshot={})
    by_key = {c["key"]: c for c in report["categories"]}
    electronics = by_key["electronics"]

    check("routes are counted per category from the log (by label or key)",
          electronics["routed"] == 2, str(electronics["routed"]))
    check("a route logged under the label maps to the right shelf",
          by_key["fashion"]["routed"] == 1, str(by_key["fashion"]["routed"]))
    check("withheld posts are counted with their reasons (label and key fold together)",
          electronics["withheld"]["total"] == 2
          and electronics["withheld"]["reasons"]["mixed_or_conflicting"] == 1
          and electronics["withheld"]["reasons"]["insufficient_product_evidence"] == 1,
          str(electronics["withheld"]))
    check("posts withheld with no category are reported separately, not lost",
          report["unclassified_withheld"]["total"] == 1
          and report["unclassified_withheld"]["reasons"] == {"mixed_or_conflicting": 1},
          str(report["unclassified_withheld"]))
    check("unclassified withholding below the routed volume is informational, not alarming",
          any(f["kind"] == "unclassified_withheld" and f["severity"] == "info"
              for f in report["findings"]), str(report["findings"]))
    check("the human report prints the unclassified line",
          "UNCLASSIFIED" in cr.render(report), cr.render(report)[-400:])
    check("an unresolved shelf target is attributed to the shelf that uses it",
          electronics["unresolved_targets"] == ["electroshelf"], str(electronics["unresolved_targets"]))
    check("an unresolved target is an actionable finding",
          any(f["kind"] == "target_unresolved" and f["category"] == "electronics"
              and f["severity"] == "warn" for f in report["findings"]), str(report["findings"]))
    check("a target collision is an error finding",
          any(f["kind"] == "target_collision" and f["severity"] == "error"
              and "fashion, accessories" in f["category"] for f in report["findings"]),
          str(report["findings"]))
    check("withheld >= routed is reported as a withheld majority",
          any(f["kind"] == "withheld_majority" and f["category"] == "electronics"
              for f in report["findings"]), str(report["findings"]))
    check("lines outside the window are ignored",
          report["log_signals"]["routing_disabled"] == []
          and electronics["routed"] == 2, str(report["log_signals"]))
    check("the log line count is reported so a silent parser is visible",
          report["log_lines_read"] == 11, str(report["log_lines_read"]))

    disabled_log = write_log(WORK / "disabled.log", [
        f"{stamp(now - 30)} [WARNING] CATEGORY ROUTING DISABLED | /x/category_rules.json "
        "could not be loaded: nope"])
    disabled = cr.build_report(db=None, log=disabled_log, hours=24, channels_data=SHELF_CONFIG,
                               rules=RULES, env={}, snapshot={})
    check("category routing OFF is an error finding with the bot's own message",
          any(f["kind"] == "routing_disabled" and f["severity"] == "error"
              and "could not be loaded" in f["detail"] for f in disabled["findings"]),
          str(disabled["findings"]))

    missing = cr.build_report(db=None, log=WORK / "no-such.log", hours=24,
                              channels_data=SHELF_CONFIG, rules=RULES, env={}, snapshot={})
    check("a missing log is not a crash and reports zero log evidence",
          missing["log_lines_read"] == 0 and missing["totals"]["routed"] == 0)


# ------------------------------------------------------- 4. findings matrix ----

def test_findings():
    print("\n== findings: parity, version drift, silence, no channel ==")
    now = time.time()
    db = make_db(WORK / "findings.sqlite3", deliveries=[("MyMainDeals", "sent", "", 60)])

    drifted = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                              rules=RULES, env={}, snapshot={"category_rules_version": 999})
    check("the window is a rolling one computed at call time, not an IST day boundary",
          abs((now - 24 * 3600) - drifted["since"]) < 300, str(drifted["since"]))
    check("a bot running different rules than this checkout is an error",
          any(f["kind"] == "rules_version_drift" and f["severity"] == "error"
              and "v999" in f["detail"] for f in drifted["findings"]), str(drifted["findings"]))
    check("a category nobody can receive is a warning, not silence",
          any(f["kind"] == "no_channel" and f["category"] == "beauty"
              for f in drifted["findings"]), str(drifted["findings"]))
    check("a shelf that got nothing while Main posted is named as silent",
          any(f["kind"] == "silent_shelf" and f["category"] == "electronics"
              for f in drifted["findings"]), str(drifted["findings"]))

    matched = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                              rules=RULES, env={},
                              snapshot={"category_rules_version": RULES.get("version")})
    check("a matching bot rules version produces no drift finding",
          not any(f["kind"] == "rules_version_drift" for f in matched["findings"]),
          str(matched["findings"]))

    # Rules parity: the two services must classify identically. Break it on a copy.
    original = cr.BRIDGE_RULES
    broken_copy = WORK / "bridge_rules.json"
    broken_copy.write_text(json.dumps({**RULES, "version": 777}), encoding="utf-8")
    try:
        cr.BRIDGE_RULES = broken_copy
        parity = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                                 rules=RULES, env={}, snapshot={})
        check("bot/bridge rules divergence is an error finding",
              any(f["kind"] == "rules_parity" and f["severity"] == "error"
                  for f in parity["findings"]), str(parity["findings"]))
    finally:
        cr.BRIDGE_RULES = original
    intact = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                             rules=RULES, env={}, snapshot={})
    check("the real bot and bridge rules files are in parity right now",
          not any(f["kind"] == "rules_parity" for f in intact["findings"]), str(intact["findings"]))
    check("the shared taxonomy fingerprint is published in the report",
          intact["bot_rules_fingerprint"] == intact["bridge_rules_fingerprint"]
          and len(intact["bot_rules_fingerprint"]) == 64)

    skipping = make_db(WORK / "skipping.sqlite3", deliveries=[
        ("ElectroShelf", "skipped", "target unresolved", 60),
        ("ElectroShelf", "skipped", "FloodWait", 50)])
    skipped = cr.build_report(db=skipping, log=None, hours=24, channels_data=SHELF_CONFIG,
                              rules=RULES, env={}, snapshot={})
    by_key = {c["key"]: c for c in skipped["categories"]}
    check("a shelf that skips more than it sends is flagged",
          by_key["electronics"]["state"] == "skipping"
          and any(f["kind"] == "skipping_shelf" and f["category"] == "electronics"
                  for f in skipped["findings"]), str(by_key["electronics"]))

    healthy = make_db(WORK / "healthy.sqlite3", deliveries=[
        ("ElectroShelf", "sent", "", 60), ("ElectroShelf", "sent", "", 40)])
    ok_report = cr.build_report(db=healthy, log=None, hours=24,
                                channels_data={"version": 1, "channels": [
                                    {"name": "ElectroShelf", "sources": ["deals"],
                                     "categories": ["electronics"], "enabled": True,
                                     "amazon_tag": True}]},
                                rules=MINI_RULES, env={},
                                snapshot={"category_rules_version": MINI_RULES["version"]})
    actionable = [f for f in ok_report["findings"] if f["severity"] in ("error", "warn")]
    check("a healthy single-shelf setup has no actionable finding",
          not actionable, str(ok_report["findings"]))
    check("the healthy shelf reports state=ok with its sends",
          ok_report["categories"][0]["state"] == "ok"
          and ok_report["categories"][0]["deliveries"]["sent"] == 2, str(ok_report["categories"]))
    check("a declared tag target is not reported as tagless",
          ok_report["categories"][0]["tagless_targets"] == [], str(ok_report["categories"][0]))


# ------------------------------------------------------------ 5. CLI contract --

def test_cli():
    print("\n== CLI: --json, --strict, --rules, --hours ==")
    td = WORK / "cli"
    td.mkdir(exist_ok=True)
    db = make_db(td / "bot.sqlite3", deliveries=[("ElectroShelf", "sent", "", 60)])
    (td / "channels.json").write_text(json.dumps({"version": 1, "channels": [
        {"name": "ElectroShelf", "sources": ["deals"], "categories": ["electronics"],
         "enabled": True, "amazon_tag": True}]}), encoding="utf-8")
    (td / "routing_snapshot.json").write_text(json.dumps(
        {"category_rules_version": MINI_RULES["version"], "started_at": time.time()}),
        encoding="utf-8")
    (td / "mini_rules.json").write_text(json.dumps(MINI_RULES), encoding="utf-8")
    env = {**os.environ, "CHANNELS_JSON_PATH": str(td / "channels.json"),
           "BOT_DB_PATH": str(db), "BOT_LOG_DIR": str(td / "logs")}

    def run(*args):
        return subprocess.run([sys.executable, str(ROOT / "ops" / "category_report.py"), *args],
                              capture_output=True, text=True, env=env, cwd=str(ROOT), check=False)

    result = run("--rules", str(td / "mini_rules.json"), "--db", str(db), "--json")
    check("--json exits 0 and prints one JSON document", result.returncode == 0, result.stderr)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = {}
    check("--json carries the per-shelf numbers",
          payload.get("categories", [{}])[0].get("key") == "electronics"
          and payload["categories"][0]["deliveries"]["sent"] == 1, result.stdout[:400])
    check("--json has no Counter objects left in it (serialisable end to end)",
          "Counter(" not in result.stdout)
    check("--strict exits 0 when the shelf is healthy",
          run("--rules", str(td / "mini_rules.json"), "--db", str(db), "--strict").returncode == 0,
          result.stderr)

    human = run("--rules", str(td / "mini_rules.json"), "--db", str(db))
    check("human output names the shelf, its state and its counts",
          "Electronics" in human.stdout and "state=ok" in human.stdout
          and "sent=1" in human.stdout, human.stdout[:400])
    check("human output reports the window and the rules version",
          "window=24h" in human.stdout and f"rules=v{MINI_RULES['version']}" in human.stdout,
          human.stdout[:300])

    strict_dirty = run("--rules", str(td / "mini_rules.json"),
                       "--db", str(td / "absent.sqlite3"))
    check("a missing database is still exit 0 without --strict", strict_dirty.returncode == 0,
          strict_dirty.stderr)
    check("--strict exits 1 when a finding is actionable",
          run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent.sqlite3"),
              "--strict").returncode == 1)

    narrow = run("--rules", str(td / "mini_rules.json"), "--db", str(db), "--hours", "0.0001",
                 "--json")
    try:
        narrow_payload = json.loads(narrow.stdout)
    except ValueError:
        narrow_payload = {}
    check("--hours narrows the window (an old send falls out of a tiny one)",
          narrow_payload.get("categories", [{}])[0].get("deliveries", {}).get("sent") == 0,
          narrow.stdout[:300])

    # --strict-errors is what a deploy gate uses: warn-level findings (a shelf the
    # operator never configured, an unreadable DB) must not fail a rollout, while
    # real breakage must.
    warn_only = run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent.sqlite3"),
                    "--strict-errors")
    check("--strict exits 1 on a warn-only finding",
          run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent.sqlite3"),
              "--strict").returncode == 1)
    check("--strict-errors tolerates warn-only findings (a deploy is not held by an unused shelf)",
          warn_only.returncode == 0, warn_only.stderr[:300])

    snapshot_path = td / "routing_snapshot.json"
    original_snapshot = snapshot_path.read_text(encoding="utf-8")
    try:
        snapshot_path.write_text(json.dumps({"category_rules_version": 999}), encoding="utf-8")
        errored = run("--rules", str(td / "mini_rules.json"), "--db", str(db), "--strict-errors")
        check("--strict-errors fails on an error finding (bot classifying with other rules)",
              errored.returncode == 1, errored.stdout[-300:] + errored.stderr[-300:])
        check("the failing run names the finding on stderr",
              "rules_version_drift" in errored.stderr, errored.stderr[:300])
    finally:
        snapshot_path.write_text(original_snapshot, encoding="utf-8")
    check("restoring the snapshot makes the same command pass again",
          run("--rules", str(td / "mini_rules.json"), "--db", str(db),
              "--strict-errors").returncode == 0)

    bad = run("--nonsense")
    check("an unknown flag exits 2 instead of guessing", bad.returncode == 2, bad.stderr[:200])


# ------------------------------------------------------ 6. dashboard endpoint --

async def _started_app():
    runner = web.AppRunner(dash.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server else []
    return runner, f"http://127.0.0.1:{sockets[0].getsockname()[1] if sockets else 8080}"


def test_http_endpoint():
    print("\n== HTTP: /api/categories is the same engine, token-gated, read-only ==")

    async def run():
        td = WORK / "http"
        td.mkdir(exist_ok=True)
        db = make_db(td / "dash.sqlite3", deliveries=[
            ("ElectroShelf", "sent", "", 60), ("ElectroShelf", "skipped", "target unresolved", 30),
            ("MyMainDeals", "sent", "", 20)])
        (td / "channels.json").write_text(json.dumps(SHELF_CONFIG), encoding="utf-8")
        (td / "routing_snapshot.json").write_text(json.dumps(
            {"category_rules_version": RULES.get("version"), "started_at": time.time()}),
            encoding="utf-8")
        log = write_log(td / "bot.log", [
            f"{stamp(time.time() - 40)} [INFO] CATEGORY ROUTE | Electronics -> @ElectroShelf | queue=7",
            f"{stamp(time.time() - 39)} [INFO] CATEGORY WITHHELD | queue=8 category=Electronics "
            "reason=mixed_or_conflicting targets=ElectroShelf",
            f"{stamp(time.time() - 38)} [WARNING] CATEGORY TARGET UNRESOLVED | @ElectroShelf "
            "not reachable; the deal still goes to the Main feed and other channels"])

        saved = {key: os.environ.get(key) for key in
                 ("BOT_DB_PATH", "BOT_LOG", "BOT_LOG_DIR", "CHANNELS_JSON_PATH",
                  "DASHBOARD_TOKEN", "DASHBOARD_INSECURE")}
        os.environ["BOT_DB_PATH"] = str(db)
        os.environ["BOT_LOG"] = str(log)
        os.environ.pop("BOT_LOG_DIR", None)
        os.environ["CHANNELS_JSON_PATH"] = str(td / "channels.json")
        os.environ["DASHBOARD_TOKEN"] = "category-token"
        os.environ.pop("DASHBOARD_INSECURE", None)
        before = sha(db)
        runner = None
        try:
            runner, base = await _started_app()
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async def call(path, token=True):
                    headers = {"X-Dashboard-Token": "category-token"} if token else {}
                    async with session.get(base + path, headers=headers) as resp:
                        try:
                            body = await resp.json()
                        except (ValueError, aiohttp.ContentTypeError):
                            body = {}
                        return resp.status, body

                status, _ = await call("/api/categories", token=False)
                check("the category endpoint requires the token", status == 401, str(status))

                status, body = await call("/api/categories?hours=24")
                check("GET /api/categories answers 200 with the shelf list",
                      status == 200 and len(body.get("categories", [])) == len(REAL_KEYS),
                      f"{status} {str(body)[:200]}")
                by_key = {c["key"]: c for c in body.get("categories", [])}
                check("the endpoint reports the shelf's real sends and skips",
                      by_key["electronics"]["deliveries"] == {"sent": 1, "skipped": 1, "held": 0},
                      str(by_key.get("electronics", {}).get("deliveries")))
                check("the endpoint reports classifier routing and withholding from the log",
                      by_key["electronics"]["routed"] == 1
                      and by_key["electronics"]["withheld"]["total"] == 1
                      and by_key["electronics"]["withheld"]["reasons"] == {"mixed_or_conflicting": 1},
                      str(by_key.get("electronics", {})))
                check("the endpoint reports Main separately from the shelves",
                      body["main"]["deliveries"]["sent"] == 1, str(body.get("main")))
                check("the endpoint publishes the rules versions for parity",
                      body["rules_version"] == RULES.get("version")
                      and body["bot_rules_version"] == RULES.get("version"), str(body)[:200])
                check("the endpoint's findings include the unresolved shelf target",
                      any(f["kind"] == "target_unresolved" for f in body.get("findings", [])),
                      str(body.get("findings")))

                direct = cr.build_report(db=db, log=log, hours=24,
                                         channels_data=channels_config.load_channels(),
                                         rules=cr.load_rules(), env={},
                                         snapshot=cr.load_snapshot())
                cli_by_key = {c["key"]: c for c in direct["categories"]}
                api_by_key = {c["key"]: c for c in body["categories"]}
                disagreements = [
                    key for key in cli_by_key
                    if (api_by_key[key]["deliveries"] != cli_by_key[key]["deliveries"]
                        or api_by_key[key]["routed"] != cli_by_key[key]["routed"]
                        or api_by_key[key]["withheld"] != cli_by_key[key]["withheld"]
                        or api_by_key[key]["pending"] != cli_by_key[key]["pending"])
                ]
                check("the dashboard and the CLI cannot disagree (same engine, same numbers)",
                      not disagreements and bool(cli_by_key),
                      f"{disagreements} {direct['totals']}")
                check("the endpoint and the CLI agree on the findings too",
                      [f["kind"] for f in body["findings"]]
                      == [f["kind"] for f in direct["findings"]],
                      str([f["kind"] for f in body["findings"]]))

                status, _ = await call("/api/categories?hours=not-a-number")
                check("a bad window falls back to the default instead of 500-ing", status == 200,
                      str(status))
                status, body = await call("/api/categories?hours=99999")
                check("an absurd window is clamped, not honoured",
                      status == 200 and body.get("window_hours") == 168.0, str(body.get("window_hours")))

                missing_db = WORK / "http" / "gone.sqlite3"
                os.environ["BOT_DB_PATH"] = str(missing_db)
                status, body = await call("/api/categories")
                check("with no database the endpoint still answers, flagging it unreadable",
                      status == 200 and body.get("db_readable") is False
                      and body.get("db_exists") is False, str(body)[:200])
        finally:
            if runner is not None:
                await runner.cleanup()
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        check("the endpoint left the database bytes untouched", sha(db) == before)

    asyncio.run(run())


# --------------------------------------------------------- 7. UI is wired up ---

def test_ui_wiring():
    print("\n== UI: the panel exists, is escaped, and cannot break refresh() ==")
    html = dash.UI_HTML
    check("the dashboard serves a categories panel",
          'id="categories"' in html and 'id="catrows"' in html and 'id="catstats"' in html)
    check("the panel is reachable from the nav", 'href="#categories"' in html)
    check("refresh() drives the panel", "await loadCategories();" in html)
    check("a dead category endpoint cannot blank the rest of the dashboard",
          "never throws" in html and "}catch(e){" in html.replace("} catch(e) {", "}catch(e){"))
    body = html.split("<script>")[1].split("</script>")[0]
    for function in ("function renderCategories", "async function loadCategories",
                     "function catStateBadge"):
        check(f"the served JavaScript defines {function.split()[-1]}", function in body)
    # Every interpolated field must pass through escapeHTML: category labels,
    # target names and skip reasons all originate in channel config or log text.
    render_body = body.split("function renderCategories")[1].split("async function loadCategories")[0]
    for field in ("c.label", "c.key", "t)", "f.detail", "f.kind", "c.last_sent"):
        check(f"renderCategories escapes {field}", f"escapeHTML({field}" in render_body
              or f"escapeHTML({field.rstrip(')')}" in render_body, render_body[:200])
    # The invariant that matters: nothing from the response reaches innerHTML
    # directly. Every dynamic value is escaped into a named local first, so a
    # malicious category label, channel name or logged skip reason cannot inject
    # markup into the operator's dashboard.
    for raw in ("${f.", "${c.", "${w.", "${d.", "${t}"):
        check(f"the panel never interpolates a response field directly ({raw[2:] or 'target'})",
              raw not in render_body, render_body[:300])
    for escaped in ("escapeHTML(f.severity", "escapeHTML(f.kind", "escapeHTML(f.detail",
                    "escapeHTML(c.label", "escapeHTML(c.key", "escapeHTML(c.last_sent",
                    "escapeHTML(d.sent", "escapeHTML(w.total", "escapeHTML(t)"):
        check(f"the panel escapes {escaped[len('escapeHTML('):]}", escaped in render_body,
              render_body[:300])
    check("skip and withhold reasons (log text) are escaped before they are joined",
          "escapeHTML(r)+'×'+escapeHTML(n)" in render_body, render_body[:300])


def make_money_db(path: Path, posts, links=(), proofs=()) -> Path:
    """A real bot-schema DB whose queue rows carry the published text.

    Money is only answerable from what actually went out, so these fixtures write
    rendered_text per post plus the two tables that prove a link is ours
    (link_cache for the published pair, owned_link_proofs behind a shortener hop).
    """
    store = bot.Store(path)
    now = time.time()
    for index, (target, status, error, age, text) in enumerate(posts, start=1):
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (index, -100 - index, index, "deals", "done", 1, "", now - age,
             str(-100 - index), text, json.dumps([target])))
        store.conn.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?)",
                           (index, target, status, 1, 1, error))
    for source_url, affiliate_url, resolved_url in links:
        store.conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)",
                           (source_url, affiliate_url, resolved_url, "k:" + affiliate_url, now))
    for affiliate_url, destination_url in proofs:
        store.conn.execute("INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)",
                           (affiliate_url, destination_url, "bitly", "5478322", now))
    store.conn.commit()
    store.conn.close()
    return path


EVIL_LABEL = '<img src=x onerror="window.__pwned=1">'
OUR_TAG = sorted(cr.our_amazon_tags()["bestgaa"] or {"mama086-21"})[0]
STRANGER_TAG = "somebody-else-21"
EMPTY_LINKS = cr.link_index(None)      # the real empty shape, keys included

# The money fixture needs a third shelf, so home_kitchen gets one instead of the
# HYPD post being counted under an unknown target name.
MONEY_CONFIG = {"version": 1, "channels": SHELF_CONFIG["channels"] + [
    {"name": "HomeShelf", "sources": ["deals"], "categories": ["home_kitchen"],
     "enabled": True, "amazon_tag": True},
]}


def test_money():
    print("\n== money: does a click on this shelf pay us, nobody, or a stranger? ==")
    tag = OUR_TAG
    db = make_money_db(WORK / "money.sqlite3", posts=[
        ("ElectroShelf", "sent", "", 60, f"Earbuds 65 pct off https://www.amazon.in/dp/B0OWN?tag={tag}"),
        ("ElectroShelf", "sent", "", 120, f"Phone https://www.amazon.in/dp/B0THEIF?tag={STRANGER_TAG}"),
        ("ElectroShelf", "sent", "", 180, "Bare page https://www.amazon.in/dp/B0NOTAG"),
        ("ElectroShelf", "skipped", "category target unresolved", 200,
         f"Never delivered https://www.amazon.in/dp/B0SKIP?tag={tag}"),
        ("StyleShelf", "sent", "", 90, "Kurta https://www.flipkart.com/kurta/p/itm123"),
        ("HomeShelf", "sent", "", 30, "Mixer https://hypd.store/product/abc123"),
        ("MyMainDeals", "sent", "", 45, "No link at all, just text"),
    ], links=[
        ("https://www.flipkart.com/kurta/p/itm123",
         "https://www.flipkart.com/kurta/p/itm123",
         "https://www.flipkart.com/kurta/p/itm123"),          # passthrough: earns nothing
    ], proofs=[
        ("https://bit.ly/3ours", f"https://www.amazon.in/dp/B0SHORT?tag={tag}"),
    ])

    report = cr.build_report(db=db, log=None, hours=24, channels_data=MONEY_CONFIG,
                             rules=RULES, env={}, snapshot={})
    by_key = {c["key"]: c for c in report["categories"]}
    electro = by_key["electronics"]["money"]
    check("a post carrying our own Associates tag is counted as earning",
          electro["owned"] == 1, str(electro))
    check("a post carrying a stranger's tag is its own bucket, not 'earns nothing'",
          electro["foreign_tag"] == 1, str(electro))
    check("an untagged Amazon page is counted as earning nobody",
          electro["earns_nothing"] >= 1, str(electro))
    check("a skipped post is never counted in money: it reached nobody",
          electro["posts"] == 3, str(electro))
    check("the routes behind the verdict are named",
          "amazon" in electro["routes"], str(electro["routes"]))
    check("the examples name the queue id so an operator can find the post",
          any(example["queue_id"] for example in electro["examples"]), str(electro["examples"]))

    style = by_key["fashion"]["money"]
    check("a clean merchant passthrough (link_cache affiliate == resolved) earns nothing",
          style["earns_nothing"] == 1 and style["routes"].get("passthrough") == 1, str(style))
    home = by_key["home_kitchen"]["money"]
    check("a verified HYPD link is ours", home["owned"] == 1 and home["routes"].get("hypd") == 1,
          str(home))
    check("a post with no link at all is unattributed, not silently 'owned'",
          report["main"]["money"]["unattributed"] == 1, str(report["main"]["money"]))

    check("the headline counts a post once even when a shelf serves two categories",
          report["money_totals"]["posts"] == 6
          and by_key["fashion"]["money"]["posts"] + by_key["accessories"]["money"]["posts"] == 2,
          str(report["money_totals"]))
    check("the paid-posts total is the number of posts that actually earn",
          report["totals"]["paid_posts"] == 2, str(report["totals"]))

    kinds = {f["kind"] for f in report["findings"]}
    check("a stranger's tag on a published post is a finding",
          "foreign_tag_posted" in kinds, str(kinds))
    check("the finding names the tag that is ours, so the fix is obvious",
          any(tag in f["detail"] for f in report["findings"]
              if f["kind"] == "foreign_tag_posted"),
          str([f["detail"] for f in report["findings"] if f["kind"] == "foreign_tag_posted"]))
    check("posts that earn nobody are a finding", "unmonetized_shelf" in kinds, str(kinds))
    check("a shelf whose posts cannot be matched to the link tables says so",
          "money_blind" in kinds, str(kinds))

    # A shortened hop is ours only when the proof row exists.
    links = {"by_affiliate": {}, "by_source": {}, "proofs": {"https://bit.ly/3ours":
                                                             f"https://www.amazon.in/dp/B0?tag={tag}"}}
    check("a bit.ly hop with an owned_link_proofs row is attributed to us",
          cr.url_money("https://bit.ly/3ours", links, frozenset({tag}))[0] == "owned",
          str(cr.url_money("https://bit.ly/3ours", links, frozenset({tag}))))
    check("a bit.ly hop with no proof is unattributed, never assumed to earn",
          cr.url_money("https://bit.ly/3unknown", EMPTY_LINKS, frozenset({tag}))[0]
          == "unattributed",
          str(cr.url_money("https://bit.ly/3unknown", EMPTY_LINKS, frozenset({tag}))))
    check("a post is judged by its worst link, but a foreign tag outranks everything",
          cr.post_money(f"https://hypd.store/x https://www.amazon.in/dp/y?tag={STRANGER_TAG}",
                        EMPTY_LINKS, frozenset({tag}))[0] == "foreign_tag")
    check("an EarnKaro link is ours",
          cr.url_money("https://ekaro.in/abc", EMPTY_LINKS, frozenset()) == ("owned", "earnkaro"),
          str(cr.url_money("https://ekaro.in/abc", EMPTY_LINKS, frozenset())))

    tags = cr.our_amazon_tags()
    check("our Associates tag is read from the bot's source, not copied into this report",
          bool(tags["bestgaa"]) and not tags["read_error"], str(tags))
    check("the bridge's tag set is read too", bool(tags["bridge"]), str(tags))
    check("both services publish with the same tag today",
          tags["bestgaa"] == tags["bridge"], str(tags))

    # Prove the parity check itself works: two different tag sets must be an error.
    fake = WORK / "tagdrift"
    (fake / "bestgaa").mkdir(parents=True, exist_ok=True)
    (fake / "tg-wa-bridge").mkdir(parents=True, exist_ok=True)
    (fake / "bestgaa" / "main_bot_new.py").write_text('OUR_AMAZON_TAGS = {"ours-21"}\n',
                                                      encoding="utf-8")
    (fake / "tg-wa-bridge" / "bridge.js").write_text("const OUR_AMAZON_TAGS = ['theirs-21']\n",
                                                     encoding="utf-8")
    drifted = cr.our_amazon_tags(root=fake)
    check("a tag drift between the two services is detected",
          drifted["bestgaa"] != drifted["bridge"], str(drifted))
    drifted_report = dict(report, our_amazon_tags={"bestgaa": sorted(drifted["bestgaa"]),
                                                   "bridge": sorted(drifted["bridge"]),
                                                   "read_error": ""})
    check("a tag drift is an ERROR finding: one surface credits another account",
          any(f["kind"] == "amazon_tag_parity" and f["severity"] == "error"
              for f in cr.findings(drifted_report)),
          str([f["kind"] for f in cr.findings(drifted_report)]))
    unreadable = cr.our_amazon_tags(root=WORK / "does-not-exist")
    check("missing source files are a reported state, not a crash",
          unreadable["read_error"] and not unreadable["bestgaa"], str(unreadable))

    # The money vocabulary is restated here, so it must match the conversion
    # report's decision tree link shape by link shape.
    import conversion_report as conv  # noqa: PLC0415 - parity is the point
    shapes = [
        ("https://www.amazon.in/dp/B0X?tag=ours-21", ""),
        ("https://amzn.to/3x", "https://www.amazon.in/dp/B0X?tag=ours-21"),
        ("https://bit.ly/3x", ""),
        ("https://bit.ly/3x", "https://hypd.store/p/1"),
        ("https://ekaro.in/abc", "https://www.amazon.in/dp/B0X"),
        ("https://hypd.store/p/1", ""),
        ("https://www.flipkart.com/x", "https://www.flipkart.com/x"),
        ("https://meesho.com/x?affid=9", ""),
        ("https://example.com/x", ""),
    ]
    mismatches = [(a, r, cr.route_of(a, r), conv.route_of(a, r)) for a, r in shapes
                  if cr.route_of(a, r) != conv.route_of(a, r)]
    check("route_of agrees with ops/conversion_report.py on every link shape",
          not mismatches, str(mismatches))
    for name in ("AMAZON_HOSTS", "EARNKARO_HOSTS", "SHORTENER_HOSTS", "HYPD_HOSTS"):
        check(f"the {name} table matches the conversion report's",
              tuple(getattr(cr, name)) == tuple(getattr(conv, name)),
              f"{getattr(cr, name)} vs {getattr(conv, name)}")

    capped = cr.build_report(db=db, log=None, hours=24, channels_data=MONEY_CONFIG,
                             rules=RULES, env={}, snapshot={}, max_posts=2)
    check("the ledger cap is reported, never applied silently",
          capped["ledger_capped"] is True, str(capped["ledger_capped"]))
    check("hitting the cap is a finding that tells the operator what to do",
          any(f["kind"] == "ledger_capped" for f in capped["findings"]),
          str([f["kind"] for f in capped["findings"]]))
    check("the capped report still renders", "MONEY |" in cr.render(capped))


def test_trend():
    print("\n== trend: per-shelf IST days, the shape a total hides ==")
    now = time.time()
    db = make_money_db(WORK / "trend.sqlite3", posts=[
        ("ElectroShelf", "sent", "", 60, "today https://hypd.store/a"),
        ("ElectroShelf", "sent", "", 25 * 3600, "yesterday https://hypd.store/b"),
        ("ElectroShelf", "sent", "", 49 * 3600, "two days ago https://hypd.store/c"),
        ("ElectroShelf", "skipped", "FloodWaitError: 30s", 25 * 3600, "skipped yesterday"),
        ("ElectroShelf", "held", "manual review", 60, "held today"),
        ("MyMainDeals", "sent", "", 60, "main today https://hypd.store/m"),
    ])
    report = cr.build_report(db=db, log=None, hours=72, channels_data=SHELF_CONFIG,
                             rules=RULES, env={}, snapshot={}, days=3)
    by_key = {c["key"]: c for c in report["categories"]}
    series = by_key["electronics"]["history"]
    check("the trend has exactly the requested number of IST days",
          len(series) == 3 and len(report["history_days"]) == 3, str(series))
    check("days are oldest-first with today last",
          series[-1]["day"] == cr.ist_day(now) and series[0]["day"] < series[-1]["day"],
          str(series))
    check("each day is an ISO date an operator can read",
          all(len(d["day"]) == 10 and d["day"][4] == "-" for d in series), str(series))
    check("sends land on the day they happened",
          series[-1]["sent"] == 1 and series[-2]["sent"] == 1 and series[-3]["sent"] == 1,
          str(series))
    check("skips are tracked separately from sends",
          series[-2]["skipped"] == 1 and series[-1]["skipped"] == 0, str(series))
    check("a hold is neither a send nor a skip in the trend",
          sum(d["sent"] for d in series) == 3 and sum(d["skipped"] for d in series) == 1,
          str(series))
    check("Main gets its own trend", len(report["main"]["history"]) == 3
          and report["main"]["history"][-1]["sent"] == 1, str(report["main"]["history"]))
    check("a shelf nobody posted to still gets a full zero series (a flat line is data)",
          len(by_key["grocery"]["history"]) == 3
          and all(d["sent"] == 0 for d in by_key["grocery"]["history"]),
          str(by_key["grocery"]["history"]))

    check("the IST day is UTC+5:30, not the server's local day",
          cr.ist_day(time.mktime(time.gmtime(0))) != ""
          and cr.ist_day(0) == "1970-01-01", str(cr.ist_day(0)))
    # 18:30 UTC is 00:00 IST the next day: the boundary that makes a "quiet
    # evening" look like a dead day if the offset is wrong.
    boundary = 1760000000.0
    check("an evening UTC timestamp rolls to the next IST day",
          cr.ist_day(boundary + 18.5 * 3600) > cr.ist_day(boundary),
          f"{cr.ist_day(boundary)} -> {cr.ist_day(boundary + 18.5 * 3600)}")

    # A 2-day series over a 30-day window: the two recent sends belong in it, and
    # the 49h-old one must NOT be folded into the newest day to make the total look
    # bigger than the window really is.
    old = cr.build_report(db=db, log=None, hours=720, channels_data=SHELF_CONFIG,
                          rules=RULES, env={}, snapshot={}, days=2)
    old_series = {c["key"]: c for c in old["categories"]}["electronics"]["history"]
    check("a row older than the series is not folded into the newest day",
          sum(d["sent"] for d in old_series) == 2 and old_series[-1]["sent"] == 1,
          str(old_series))
    check("a long window still reports every send in its totals",
          {c["key"]: c for c in old["categories"]}["electronics"]["deliveries"]["sent"] == 3,
          str({c["key"]: c for c in old["categories"]}["electronics"]["deliveries"]))

    check("a sparkline is one character per day",
          len(cr.sparkline([0, 1, 5])) == 3, cr.sparkline([0, 1, 5]))
    check("a rising trend rises", cr.sparkline([1, 4, 9])[-1] > cr.sparkline([1, 4, 9])[0])
    check("an all-zero trend is a flat floor, not an empty string",
          cr.sparkline([0, 0, 0]) == cr.SPARK[0] * 3, cr.sparkline([0, 0, 0]))
    check("a sparkline never crashes on an empty series", cr.sparkline([]) == "")
    check("the rendered report shows the trend and the money line",
          "trend   :" in cr.render(report) and "MONEY |" in cr.render(report))


def test_surfaces():
    print("\n== surfaces: the same shelf on Telegram and on WhatsApp ==")
    wa_env = {
        "WA_CHANNEL_ELECTRONICS": "120363001@newsletter",
        "WA_CHANNEL_FASHION": "120363002@newsletter",
        "WA_CHANNEL_UNDER99": "120363009@newsletter",
        "WA_AMAZON_TAG_CATEGORY_TARGETS": "electronics",
        "TG_CATEGORY_ELECTRONICS_TARGET": "@ElectroShelf",
    }
    report = cr.build_report(db=None, log=None, hours=24, channels_data=SHELF_CONFIG,
                             rules=RULES, env=wa_env, snapshot={})
    by_key = {c["key"]: c for c in report["categories"]}
    electronics = by_key["electronics"]["surfaces"]
    check("a WhatsApp channel is read from WA_CHANNEL_<KEY> for the matching category",
          electronics["whatsapp"]["channel"] == "120363001@newsletter", str(electronics))
    check("a unique channel really gets the bridge's category policy",
          electronics["whatsapp"]["filtered"] is True
          and electronics["whatsapp"]["policy"] == "category:electronics", str(electronics))
    check("the core channels are kept out of the category list",
          "under99" not in report["whatsapp"]["channels"]
          and report["whatsapp"]["core"].get("under99") == "120363009@newsletter",
          str(report["whatsapp"]))
    check("the Telegram side of the same shelf is reported next to it",
          electronics["telegram"]["targets"] and electronics["telegram"]["amazon_tag"] is False,
          str(electronics["telegram"]))

    kinds = {f["kind"] for f in report["findings"]}
    check("a shelf configured on Telegram but missing on WhatsApp is a finding",
          "wa_surface_missing" in kinds, str(kinds))
    check("the Amazon tag declared on one surface only is a finding",
          "amazon_tag_surface_mismatch" in kinds
          or not electronics["whatsapp"]["amazon_tag"], str(kinds))

    shared = dict(wa_env, WA_CHANNEL_HOME_KITCHEN="120363001@newsletter")
    shared_report = cr.build_report(db=None, log=None, hours=24, channels_data=SHELF_CONFIG,
                                    rules=RULES, env=shared, snapshot={})
    check("two categories sharing one WhatsApp channel is a collision",
          any(f["kind"] == "wa_category_collision" and f["severity"] == "error"
              for f in shared_report["findings"]),
          str([f["kind"] for f in shared_report["findings"]]))
    shared_keys = {c["key"]: c for c in shared_report["categories"]}
    check("a colliding channel is reported as NOT filtered (the bridge keeps the old policy)",
          shared_keys["electronics"]["surfaces"]["whatsapp"]["filtered"] is False
          and shared_keys["home_kitchen"]["surfaces"]["whatsapp"]["filtered"] is False,
          str(shared_keys["electronics"]["surfaces"]))
    check("the collision finding says why, so the fix is one env change",
          any("share one WhatsApp channel" in f["detail"] for f in shared_report["findings"]),
          str([f["detail"] for f in shared_report["findings"]
               if f["kind"] == "wa_category_collision"]))

    core_clash = dict(wa_env, WA_CHANNEL_FASHION="120363009@newsletter")
    core_report = cr.build_report(db=None, log=None, hours=24, channels_data=SHELF_CONFIG,
                                  rules=RULES, env=core_clash, snapshot={})
    check("a category channel that is also a core channel is a collision too",
          any(f["kind"] == "wa_category_collision" and "core" in f["detail"]
              for f in core_report["findings"]),
          str([f["detail"] for f in core_report["findings"]
               if f["kind"] == "wa_category_collision"]))

    ghost = dict(wa_env, WA_CHANNEL_GHOSTS="120363099@newsletter")
    ghost_report = cr.build_report(db=None, log=None, hours=24, channels_data=SHELF_CONFIG,
                                   rules=RULES, env=ghost, snapshot={})
    check("a WA_CHANNEL_* name that is not a category key is reported as ignored",
          any(f["kind"] == "unknown_surface_channel" and "WA_CHANNEL_GHOSTS" in f["detail"]
              for f in ghost_report["findings"]),
          str([f["detail"] for f in ghost_report["findings"]
               if f["kind"] == "unknown_surface_channel"]))

    plain = cr.build_report(db=None, log=None, hours=24, channels_data=SHELF_CONFIG,
                            rules=RULES, env={}, snapshot={})
    check("an operator who does not run WhatsApp is not nagged about WhatsApp",
          not any(f["kind"].startswith(("wa_", "amazon_tag_surface")) for f in plain["findings"]),
          str([f["kind"] for f in plain["findings"]]))
    check("an unconfigured surface says so instead of looking broken",
          plain["whatsapp"]["configured"] is False
          and "not judged" in cr.render(plain), str(plain["whatsapp"]))

    invite = dict(wa_env, WA_CHANNEL_ELECTRONICS="https://chat.whatsapp.com/AbC123Secret")
    masked = cr.build_report(db=None, log=None, hours=24, channels_data=SHELF_CONFIG,
                             rules=RULES, env=invite, snapshot={})
    text = cr.render(masked)
    check("an invite link is never printed: only a JID is shown verbatim",
          "AbC123Secret" not in text and "AbC123Secret" not in json.dumps(cr.jsonable(masked)),
          text[:300])
    check("a masked value is reported as set, so the shelf is still visible",
          masked["whatsapp"]["channels"]["electronics"] == "set (masked)"
          and masked["whatsapp"]["masked"] == 1, str(masked["whatsapp"]))
    check("with masked values a collision is not guessed at",
          masked["whatsapp"]["comparable"] is False
          and any(f["kind"] == "wa_values_masked" for f in masked["findings"]),
          str([f["kind"] for f in masked["findings"]]))

    env = cr.load_env([str(write_log(WORK / "wa.env", [
        "WA_CHANNEL_ELECTRONICS=120363001@newsletter",
        "WA_AMAZON_TAG_CATEGORY_TARGETS=electronics",
        "EARNKARO_API_KEY=super-secret-token",
        "TELEGRAM_API_HASH=another-secret"]))])
    check("load_env keeps the WhatsApp surface keys",
          env.get("WA_CHANNEL_ELECTRONICS") == "120363001@newsletter"
          and env.get("WA_AMAZON_TAG_CATEGORY_TARGETS") == "electronics", str(sorted(env)))
    check("load_env still keeps no credential from the same file",
          not any("secret" in value for value in env.values())
          and "EARNKARO_API_KEY" not in env and "TELEGRAM_API_HASH" not in env, str(sorted(env)))
    check("mask_wa shows a JID and masks everything else",
          cr.mask_wa("120363001@newsletter") == "120363001@newsletter"
          and cr.mask_wa("https://chat.whatsapp.com/X") == "set (masked)"
          and cr.mask_wa("") == "")


def test_drilldown():
    print("\n== drill-down: the newest rows behind one shelf's numbers ==")
    db = make_money_db(WORK / "drill.sqlite3", posts=[
        ("ElectroShelf", "sent", "", 30, f"newest https://www.amazon.in/dp/B0A?tag={OUR_TAG}"),
        ("ElectroShelf", "skipped", "category target unresolved", 60, "skipped one"),
        ("ElectroShelf", "sent", "", 90, "older https://hypd.store/p/x"),
        ("MyMainDeals", "sent", "", 45, "main https://hypd.store/p/m"),
        ("GhostChannel", "sent", "", 50, "unknown target https://hypd.store/p/g"),
    ])
    report = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                             rules=RULES, env={}, snapshot={}, expand="electronics")
    rows = report["recent"]["electronics"]
    check("--expand lists the shelf's own rows", len(rows) == 3, str(len(rows)))
    check("newest first, so the operator sees the current state",
          rows[0]["queue_id"] == 1 and rows[-1]["queue_id"] == 3, str([r["queue_id"] for r in rows]))
    check("each row carries its money verdict",
          rows[0]["money"] == "owned" and rows[1]["money"] == "", str(rows[:2]))
    check("a skipped row shows its bucketed reason",
          rows[1]["reason"] == "target_unresolved" and rows[0]["reason"] == "", str(rows[1]))
    check("a row keeps a short text snippet for recognition",
          rows[0]["snippet"] and len(rows[0]["snippet"]) <= 140, str(rows[0]["snippet"]))
    check("without --expand the payload carries no rows at all",
          cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                          rules=RULES, env={}, snapshot={})["recent"] == {})
    limited = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                              rules=RULES, env={}, snapshot={}, expand="electronics",
                              expand_limit=1)
    check("--expand-limit caps the list", len(limited["recent"]["electronics"]) == 1)
    main_rows = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                                rules=RULES, env={}, snapshot={}, expand="main")
    check("'main' can be expanded like a shelf", len(main_rows["recent"]["main"]) == 1,
          str(main_rows["recent"]))
    unknown = cr.build_report(db=db, log=None, hours=24, channels_data=SHELF_CONFIG,
                              rules=RULES, env={}, snapshot={}, expand="target:GhostChannel")
    check("an unknown target can be expanded under the name the report gave it",
          len(unknown["recent"]["target:GhostChannel"]) == 1, str(unknown["recent"]))
    check("the drill-down is rendered, and survives JSON",
          "RECENT [electronics]" in cr.render(report)
          and json.dumps(cr.jsonable(report))[:10])


QUALITY_CONFIG = {"version": 1, "channels": [
    {"name": "ElectroShelf", "sources": ["deals", "lootzone"], "categories": ["electronics"],
     "enabled": True, "amazon_tag": True},
    {"name": "StyleShelf", "sources": ["*"], "categories": ["fashion", "accessories"],
     "enabled": True, "amazon_tag": True},
    {"name": "HomeShelf", "sources": ["deals"], "categories": ["home_kitchen"],
     "enabled": True, "amazon_tag": True},
    {"name": "MyMainDeals", "sources": ["*"], "enabled": True, "amazon_tag": True},
]}


def make_quality_db(path: Path, posts, prices=()) -> Path:
    """A ledger that also carries the bot's own deal ranking and recorded prices.

    `queue.premium_score` and `posted_deals.price` are the bot's numbers; the
    report must compare them, never recompute or re-judge them.
    """
    store = bot.Store(path)
    now = time.time()
    for index, (target, status, error, age, source, score) in enumerate(posts, start=1):
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json,premium_score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (index, -100 - index, index, source, "done", 1, "", now - age,
             str(-100 - index), f"deal {index} https://hypd.store/p/{index}",
             json.dumps([target]), score))
        store.conn.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?)",
                           (index, target, status, 1, 1, error))
    for deal_key, price, queue_id in prices:
        store.conn.execute("INSERT INTO posted_deals VALUES(?,?,?,?)",
                           (deal_key, price, now - 60, queue_id))
    store.conn.commit()
    store.conn.close()
    return path


def test_quality_and_sources():
    print("\n== quality + sources: which shelf gets weak deals, and from where ==")
    db = make_quality_db(WORK / "quality.sqlite3", posts=[
        ("ElectroShelf", "sent", "", 3600, "deals", 1300),
        ("ElectroShelf", "sent", "", 7200, "deals", 1300),
        ("ElectroShelf", "sent", "", 9000, "Deals", 1300),        # same source, other case
        ("ElectroShelf", "sent", "", 9500, "deals", 1300),
        ("ElectroShelf", "sent", "", 10000, "unknownsrc", 100),   # not declared anywhere
        ("StyleShelf", "sent", "", 3600, "anything", 100),        # '*' channel: unrestricted
        ("StyleShelf", "sent", "", 4000, "anything", 100),
        ("StyleShelf", "sent", "", 4400, "anything", 100),
        ("HomeShelf", "sent", "", 5000, "deals", None),           # no score at all
        ("HomeShelf", "sent", "", 5400, "deals", None),
        ("HomeShelf", "skipped", "FloodWaitError: 9s", 5600, "deals", 900),
        ("MyMainDeals", "sent", "", 6000, "deals", 900),
        ("MyMainDeals", "sent", "", 6400, "deals", 900),
    ], prices=[("d1", 99, 1), ("d2", 749, 2), ("d3", 2499, 3), ("d4", 49, 4)])

    report = cr.build_report(db=db, log=None, hours=24, channels_data=QUALITY_CONFIG,
                             rules=RULES, env={}, snapshot={})
    by_key = {c["key"]: c for c in report["categories"]}
    electro = by_key["electronics"]["quality"]
    check("the bot's own premium_score is averaged per shelf, not recomputed",
          electro["avg_score"] == 1060.0 and electro["min_score"] == 100
          and electro["max_score"] == 1300 and electro["scored"] == 5, str(electro))
    check("a shelf's recorded prices are read from posted_deals",
          electro["price_rows"] == 4 and electro["price_min"] == 49
          and electro["price_median"] == 424 and electro["price_max"] == 2499, str(electro))
    check("the cheap-deal bands are counted (the bot's own premium thresholds)",
          electro["under_99"] == 2 and electro["under_499"] == 0, str(electro))
    home = by_key["home_kitchen"]["quality"]
    check("posts with no premium_score are counted as unscored, not as zero",
          home["scored"] == 0 and home["unscored"] == 2 and home["posts"] == 2, str(home))
    check("a shelf whose deals cannot be scored says so instead of looking bad",
          any(f["kind"] == "quality_blind" and f["category"] == "home_kitchen"
              for f in report["findings"]), str([f["kind"] for f in report["findings"]]))
    check("a shelf with no recorded prices says the spread is unknown",
          any(f["kind"] == "price_blind" and f["category"] == "home_kitchen"
              for f in report["findings"]), str([f["kind"] for f in report["findings"]]))

    kinds = {(f["kind"], f["category"]) for f in report["findings"]}
    check("a shelf averaging under half the fleet is called weak, with both numbers",
          ("weak_shelf_quality", "fashion") in kinds, str(sorted(kinds)))
    weak = next(f["detail"] for f in report["findings"]
                if f["kind"] == "weak_shelf_quality" and f["category"] == "fashion")
    check("the weak-shelf finding names the shelf average and the fleet average",
          "100.0" in weak and str(report["quality_fleet"]["avg_score"]) in weak, weak)
    check("the fleet average counts every delivered post exactly once",
          report["quality_fleet"]["posts"] == 12
          and report["quality_fleet"]["scored"] == 10, str(report["quality_fleet"]))
    check("a two-post sample is not judged against the fleet",
          ("weak_shelf_quality", "electronics") not in kinds, str(sorted(kinds)))

    check("sources are counted per shelf",
          by_key["electronics"]["quality"]["sources"].get("deals") == 4,
          str(by_key["electronics"]["quality"]["sources"]))
    check("a handle source is case-insensitive, like channels_config compares it",
          cr.source_id("Deals") == cr.source_id("deals") == "deals")
    check("an invite link's code keeps its case (different chats, not a typo)",
          cr.source_id("https://t.me/+AbC") != cr.source_id("https://t.me/+abc"))
    check("an empty source is reported, not dropped",
          cr.source_id("") == "(no source)")

    check("traffic from a source the shelf does not declare is a finding",
          ("undeclared_source", "electronics") in kinds, str(sorted(kinds)))
    undeclared = next(f["detail"] for f in report["findings"]
                      if f["kind"] == "undeclared_source" and f["category"] == "electronics")
    check("the finding names the undeclared source", "unknownsrc" in undeclared, undeclared)
    check("a '*' channel is unrestricted, so any source is declared by definition",
          ("undeclared_source", "fashion") not in kinds
          and report["source_matrix"]["unrestricted"]["fashion"] is True, str(sorted(kinds)))
    check("a declared source that produced nothing is listed as DEAD, not silently absent",
          report["source_matrix"]["declared_only"] == ["lootzone"]
          and any(f["kind"] == "dead_source" and "lootzone" in f["detail"]
                  for f in report["findings"]),
          str(report["source_matrix"]["declared_only"]))

    matrix = report["source_matrix"]
    check("the matrix has a column per shelf plus Main",
          matrix["columns"][-1] == "main" and "electronics" in matrix["columns"],
          str(matrix["columns"]))
    check("the matrix cells add up to the shelf's own source counts",
          matrix["cells"]["deals"]["electronics"] == 4
          and matrix["cells"]["anything"]["fashion"] == 3, str(matrix["cells"]))
    check("the matrix totals rank the sources",
          list(matrix["totals"])[0] == "deals", str(matrix["totals"]))
    text = cr.render(report)
    check("the rendered report carries the quality and source-matrix blocks",
          "QUALITY |" in text and "SOURCE MATRIX |" in text, text[:200])
    check("the rendered matrix marks a dead source and names its owner",
          "lootzone" in text and "DEAD (declared by electronics)" in text, text[-800:])
    check("each shelf prints its observed and declared sources side by side",
          "sources : deals x4" in text and "declared: deals, lootzone" in text, text[:1200])
    payload = json.loads(json.dumps(cr.jsonable(report)))
    payload_electronics = next(c for c in payload["categories"] if c["key"] == "electronics")
    check("quality and the matrix survive JSON for the dashboard",
          payload_electronics["quality"]["avg_score"] == 1060.0
          and payload["source_matrix"]["declared_only"] == ["lootzone"]
          and payload["quality_fleet"]["posts"] == 12,
          str(payload["source_matrix"])[:200])

    # A malicious source name is data: it must round-trip intact for the UI to
    # escape, never be interpreted here.
    evil = make_quality_db(WORK / "evil-source.sqlite3", posts=[
        ("ElectroShelf", "sent", "", 60, EVIL_LABEL, 900)])
    evil_report = cr.build_report(db=evil, log=None, hours=24, channels_data=QUALITY_CONFIG,
                                  rules=RULES, env={}, snapshot={})
    check("a hostile source name is reported verbatim as data",
          EVIL_LABEL in evil_report["source_matrix"]["sources"]
          and EVIL_LABEL in cr.render(evil_report), str(evil_report["source_matrix"]["sources"]))


def watch_fixture(td: Path) -> tuple[Path, dict]:
    """A genuinely healthy shelf: mini rules, one send, tag declared, versions equal.

    The watchdog must be provable in both directions, so it needs a world with no
    findings at all - otherwise "exit 0" would only mean "nothing new since the
    last crash".
    """
    td.mkdir(parents=True, exist_ok=True)
    db = make_money_db(td / "bot.sqlite3",
                       posts=[("ElectroShelf", "sent", "", 60, "Earbuds https://hypd.store/p/1")])
    (td / "channels.json").write_text(json.dumps({"version": 1, "channels": [
        {"name": "ElectroShelf", "sources": ["deals"], "categories": ["electronics"],
         "enabled": True, "amazon_tag": True}]}), encoding="utf-8")
    (td / "routing_snapshot.json").write_text(json.dumps(
        {"category_rules_version": MINI_RULES["version"], "started_at": time.time()}),
        encoding="utf-8")
    (td / "mini_rules.json").write_text(json.dumps(MINI_RULES), encoding="utf-8")
    env = {**os.environ, "CHANNELS_JSON_PATH": str(td / "channels.json"),
           "BOT_DB_PATH": str(db), "BOT_LOG_DIR": str(td / "logs")}
    return db, env


def test_watchdog():
    print("\n== watchdog: says it once, says when it is fixed, writes only its state ==")
    td = WORK / "watch"
    db, env = watch_fixture(td)
    watch = str(ROOT / "ops" / "category_watch.py")
    state = td / "state.json"

    def run(*args, use_env=None):
        return subprocess.run([sys.executable, watch, *args], capture_output=True, text=True,
                              env=use_env if use_env is not None else env, cwd=str(ROOT),
                              check=False)

    base = ("--rules", str(td / "mini_rules.json"), "--db", str(db), "--state", str(state))
    healthy = run(*base)
    check("a healthy shelf produces no alert and exit 0",
          healthy.returncode == 0 and "ALERT" not in healthy.stdout, healthy.stdout[:300])
    check("a quiet pass says what it is tracking, so silence is not mystery",
          "nothing new" in healthy.stdout, healthy.stdout[:300])
    check("the watchdog writes its state file", state.exists())

    # An abstaining classifier is the documented behaviour, not an outage.
    (td / "logs").mkdir(parents=True, exist_ok=True)
    log = write_log(td / "logs" / "bot.log", [
        "CATEGORY WITHHELD | queue=9 category=unknown reason=mixed_or_conflicting targets="])
    quiet_kind = run(*base, "--log", str(log))
    check("unclassified_withheld never pages anybody (QUIET_KINDS)",
          quiet_kind.returncode == 0 and "unclassified_withheld" not in quiet_kind.stdout,
          quiet_kind.stdout[:300])

    broken = run(*base[:2], "--rules", str(td / "mini_rules.json"),
                 "--db", str(td / "absent.sqlite3"), "--state", str(state))
    check("a new problem alerts and exits 1",
          broken.returncode == 1 and "ALERT" in broken.stdout, broken.stdout[:300])
    check("the alert names the finding and the fix",
          "db_unreadable" in broken.stdout, broken.stdout[:300])
    again = run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent.sqlite3"),
                "--state", str(state))
    check("the same problem is not re-alerted on the next run (an alarm that always rings "
          "is an alarm nobody reads)", again.returncode == 0 and "ALERT" not in again.stdout,
          again.stdout[:300])
    check("a still-open condition is counted, not forgotten",
          json.loads(state.read_text(encoding="utf-8"))["alerts"], state.read_text()[:200])

    fixed = run(*base)
    check("a condition that disappears is announced as RESOLVED",
          "RESOLVED" in fixed.stdout and "db_unreadable" in fixed.stdout, fixed.stdout[:300])
    check("resolving is not itself an alert", fixed.returncode == 0, fixed.stdout[:200])

    state.write_text("{ this is not json", encoding="utf-8")
    corrupt = run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent.sqlite3"),
                  "--state", str(state))
    check("a corrupt state file starts fresh instead of crashing",
          corrupt.returncode == 1 and "ALERT" in corrupt.stdout
          and "Traceback" not in corrupt.stderr, corrupt.stderr[:300])
    check("the corrupt state was replaced by a readable one",
          json.loads(state.read_text(encoding="utf-8"))["alerts"] != {})

    # Same finding, same id: it is only "new" again once the memory is cleared,
    # which is exactly what --resolve is for (and what a fix does in real life).
    state.write_text(json.dumps({"alerts": {}}), encoding="utf-8")
    quiet_run = run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent2.sqlite3"),
                    "--state", str(state), "--quiet")
    check("--quiet prints nothing but still fails on a new alert",
          quiet_run.returncode == 1 and quiet_run.stdout == "", quiet_run.stdout[:200])

    state.write_text(json.dumps({"alerts": {}}), encoding="utf-8")
    machine = run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent3.sqlite3"),
                  "--state", str(state), "--json")
    try:
        payload = json.loads(machine.stdout)
    except ValueError:
        payload = {}
    check("--json reports the new alert machine-readably",
          machine.returncode == 1 and payload.get("new")
          and payload["new"][0]["kind"] == "db_unreadable", machine.stdout[:300])
    check("--json separates new, still-open, suppressed and resolved",
          set(payload) >= {"new", "still_open", "suppressed", "resolved", "watched", "tracked"},
          str(sorted(payload)))

    error_floor = run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent4.sqlite3"),
                      "--state", str(state), "--min-severity", "error")
    check("--min-severity error does not page on a warn-only world",
          error_floor.returncode == 0 and "ALERT" not in error_floor.stdout,
          error_floor.stdout[:300])
    kept = run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent4.sqlite3"),
               "--state", str(state), "--json")
    check("an error-only run does not erase the warnings another watcher remembers",
          json.loads(kept.stdout)["tracked"] >= 1
          and not json.loads(kept.stdout)["new"], kept.stdout[:300])

    # Read-only, like the report: the only file this script may write is its state.
    before_db, before_cfg = sha(db), sha(td / "channels.json")
    run(*base)
    check("a watchdog run leaves the database bytes untouched", sha(db) == before_db)
    check("a watchdog run leaves channels.json untouched", sha(td / "channels.json") == before_cfg)
    check("no temp file is left behind by the atomic state write",
          not list(td.glob("*.tmp")), str(list(td.glob("*.tmp"))))

    cleared = run(*base, "--resolve")
    check("--resolve forgets every remembered alert",
          cleared.returncode == 0
          and json.loads(state.read_text(encoding="utf-8"))["alerts"] == {},
          cleared.stdout[:200] + state.read_text()[:200])
    rearm = run("--rules", str(td / "mini_rules.json"), "--db", str(td / "absent5.sqlite3"),
                "--state", str(state))
    check("after --resolve the same condition alerts again (re-armed, not muted)",
          rearm.returncode == 1 and "ALERT" in rearm.stdout, rearm.stdout[:200])

    # The unit behind the CLI: repeat-after, severity scope and the seen counter.
    spec = importlib.util.spec_from_file_location("watch_under_test", watch)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = {"findings": [{"severity": "warn", "kind": "silent_shelf", "category": "electronics",
                            "detail": "Main posted, this shelf did not"}]}
    now = time.time()
    first = module.evaluate(report, "warn", 0.0, now, {"alerts": {}})
    check("evaluate marks a first sighting as new", len(first["fresh"]) == 1, str(first))
    second = module.evaluate(report, "warn", 0.0, now + 60, first["state"])
    check("evaluate suppresses the repeat", second["fresh"] == []
          and second["repeats"][0]["suppressed"] is True, str(second))
    check("evaluate counts how long a condition has been open",
          second["state"]["alerts"]["silent_shelf|electronics"]["seen"] == 2,
          str(second["state"]["alerts"]))
    third = module.evaluate(report, "warn", 3600.0, now + 7200, second["state"])
    check("--repeat-after re-reports a condition that is still open",
          third["repeats"] and third["repeats"][0].get("suppressed") is not True, str(third))
    gone = module.evaluate({"findings": []}, "warn", 0.0, now + 9000, third["state"])
    check("a finding that is gone is resolved exactly once",
          len(gone["resolved"]) == 1 and gone["state"]["alerts"] == {}, str(gone))
    info_only = module.evaluate({"findings": [{"severity": "info", "kind": "tagless_shelf",
                                               "category": "x", "detail": "policy"}]},
                                "warn", 0.0, now, {"alerts": {}})
    check("an informational finding is tracked but never pages",
          info_only["fresh"] == [] and info_only["tracked"] == 1, str(info_only))
    rendered = module.render(first, cr, quiet=False)
    check("the rendered alert line carries the severity, kind, shelf and fix",
          rendered.startswith("ALERT WARN  silent_shelf [electronics]"), rendered[:200])
    check("--quiet renders nothing at all", module.render(first, cr, quiet=True) == "")


def test_ui_wiring_advanced():
    print("\n== UI: money, trend, surfaces and the drill-down are escaped too ==")
    html = dash.UI_HTML
    body = html.split("<script>")[1].split("</script>")[0]
    for function in ("function sparkline", "function moneyCell", "function surfaceCell",
                     "async function toggleShelf", "function renderShelfDetail",
                     "function findShelfRow"):
        check(f"the served JavaScript defines {function.split()[-1]}", function in body)
    check("the table grew the money, quality, trend and surface columns",
          "<th>Money</th><th>Deal quality</th><th>Trend</th><th>Surfaces</th>" in html, html[:200])
    check("every empty state spans the new column count",
          'colspan="14"' in html and 'colspan="10"' not in html.split("id=\"categories\"")[1]
          .split("</table>")[0], "a stale colspan would break the empty states")
    check("the trend window has its own picker",
          'id="catdays"' in html and "days=" in body)
    check("rows are clickable and carry their key",
          'data-key="${key}"' in body and "closest('tr[data-key]')" in body)
    check("an open drill-down freezes the table instead of snapping shut",
          "table paused while a shelf row is open" in body
          and "tr.open" in body, body[:200])
    drill = body.split("function renderShelfDetail")[1].split("function renderCategories")[0]
    check("the drill-down builds text nodes, never markup",
          "textContent" in drill and "innerHTML" not in drill, drill[:300])
    opened = body.split("async function toggleShelf")[1].split("function renderShelfDetail")[0]
    check("opening a shelf never assigns innerHTML either", "innerHTML" not in opened,
          opened[:300])
    for name in ("moneyCell", "surfaceCell"):
        fn = body.split(f"function {name}")[1].split("\nfunction ")[0]
        check(f"{name} escapes what it interpolates", "escapeHTML(" in fn, fn[:200])
        for raw in ("${m.", "${wa.", "${tg.", "${sf."):
            check(f"{name} never interpolates a response field directly ({raw[2:]})",
                  raw not in fn, fn[:200])
    check("the sparkline is pure arithmetic on numbers from the report",
          "Math.max(0,Number(" in body and "blocks[Math.min(7," in body)
    check("the money headline is a stat, not a footnote",
          "'posts that pay us'" in body, body[:200])
    for function in ("function qualityCell", "function renderSourceMatrix"):
        check(f"the served JavaScript defines {function.split()[-1]}", function in body)
    check("the panel grew a source-matrix table under the shelf table",
          'id="catsources"' in html and 'id="catsourcehead"' in html
          and "Which source feeds which shelf" in html, html[:200])
    check("a declared-but-silent source is marked DEAD in the matrix",
          '<span class="badge warn">DEAD</span>' in body)
    qcell = body.split("function qualityCell")[1].split("\nfunction ")[0]
    check("qualityCell escapes what it interpolates", "escapeHTML(" in qcell, qcell[:200])
    check("qualityCell never interpolates a response field directly",
          "${q." not in qcell, qcell[:200])
    matrix_fn = body.split("function renderSourceMatrix")[1].split("\nfunction ")[0]
    check("the matrix escapes source names and column keys",
          "escapeHTML(name)" in matrix_fn and "escapeHTML(c)" in matrix_fn, matrix_fn[:300])
    check("the matrix never interpolates a response field directly",
          "${matrix." not in matrix_fn and "${cells[" not in matrix_fn
          and "${name}" not in matrix_fn, matrix_fn[:300])
    check("the empty states span the 14-column table",
          'colspan="14"' in html and 'colspan="13"' not in html, html[:200])
    check("the navigation reaches the card studio",
          'href="#cardstudio"' in html and 'id="cardstudio"' in html, html[:200])
    check("the studio says it is preview-only",
          "Preview only" in html and "approve_posting.sh" in html, html[:200])
    studio_js = body.split("async function loadStudio")[1].split("\nfunction ")[0]
    check("the studio puts captions in as text, never markup",
          "getElementById('cardcaption').textContent=" in studio_js
          and "cardcaption').innerHTML" not in studio_js, studio_js[:300])
    check("the studio escapes every plan field it interpolates",
          all(token in studio_js for token in
              ("escapeHTML(row.surface)", "escapeHTML(row.destination)",
               "escapeHTML(row.why)", "escapeHTML(row.format)")), studio_js[:400])
    check("the studio never interpolates a response field directly",
          "${row." not in studio_js and "${f." not in studio_js
          and "${data." not in studio_js, studio_js[:400])
    check("the card image is fetched through the tokened API, not a bare URL",
          "fetch('/api/card.png?'" in studio_js and "headers()" in studio_js
          and "URL.createObjectURL" in studio_js, studio_js[:400])
    check("the WhatsApp surface summary reaches the note",
          "WhatsApp: " in body and "COLLISION" in body)
    check("a capped ledger is announced in the panel",
          "ledger_capped" in body and "money and trend cover part of this window" in body)


if __name__ == "__main__":
    try:
        test_config_math()
        test_ledger()
        test_money()
        test_trend()
        test_surfaces()
        test_drilldown()
        test_quality_and_sources()
        test_log_signals()
        test_findings()
        test_cli()
        test_watchdog()
        test_http_endpoint()
        test_ui_wiring()
        test_ui_wiring_advanced()
    finally:
        import shutil
        shutil.rmtree(WORK, ignore_errors=True)
    print("\n" + (f"CATEGORY REPORT TESTS: FAILURES: {', '.join(FAILS)}" if FAILS
                  else f"test_category_report: all {CHECKS} checks PASS"))
    sys.exit(1 if FAILS else 0)
