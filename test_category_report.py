"""Category-wise reporting: per-shelf truth from the live DB, log, config and rules.

Covers ops/category_report.py (the engine both the CLI and the dashboard's
/api/categories use) end to end: config -> shelf index, the real delivery ledger,
the pending backlog, the bot's own CATEGORY log lines, the findings matrix, the
CLI contract, and the HTTP endpoint.

Why it matters: a shelf can sit empty for a week - target unresolved, rules
version stale, Amazon tag never declared - while Main keeps posting and every
aggregate number looks healthy. These checks are the difference between "the
electronics channel got nothing" and "the electronics channel got nothing, here
is the logged reason and the finding that names it".

Offline and read-only: a private temp database built with the bot's own Store
(so the schema is the real one), a synthetic log, and no Telegram, WhatsApp or
affiliate access. The suite asserts the database bytes are unchanged afterwards.
"""
import asyncio
import hashlib
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
    check("load_env keeps only the category keys",
          set(env) == {"TG_CATEGORY_BEAUTY_TARGET", "AMAZON_TAG_CATEGORY_TARGETS"}, str(env))
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


if __name__ == "__main__":
    try:
        test_config_math()
        test_ledger()
        test_log_signals()
        test_findings()
        test_cli()
        test_http_endpoint()
        test_ui_wiring()
    finally:
        import shutil
        shutil.rmtree(WORK, ignore_errors=True)
    print("\n" + (f"CATEGORY REPORT TESTS: FAILURES: {', '.join(FAILS)}" if FAILS
                  else f"test_category_report: all {CHECKS} checks PASS"))
    sys.exit(1 if FAILS else 0)
