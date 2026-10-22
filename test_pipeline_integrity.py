"""Pipeline integrity: every intake is accounted for, or the panel says so.

The user's standard is "edho okati miss avvakoodadu". This suite proves the
accounting equation against a synthetic database built with the bot's real
schema, and then proves the dashboard endpoint + the served panel agree with it.

What is checked:
  * one state per intake (posted / withheld / dropped / waiting / failed /
    unaccounted) and the equation intake = the sum of the explained ones;
  * the ONLY error state is a terminal row with no send, no delivery row and no
    recorded reason - a row that *does* carry a reason is explained, not lost;
  * a legacy row written as status='sent' + an error counts as withheld;
  * first-send latency comes from the send fence (send_attempts), not a guess;
  * per-source verdicts (delivering / withholding / waiting / failing / silent /
    overdue / observed) and per-destination states;
  * "declared vs real": configured sources and channels that produced nothing
    are named, and a source seen in the DB but absent from config is named too;
  * the wire contract of GET /api/pipeline (token, missing DB, unreadable DB,
    window parameter) and `unaccounted_ids` for a drill-down;
  * the served page has the panel and its calls (markup + JS syntax).
"""
import json
import os
import sqlite3
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
from dashboard import app as dash  # noqa: E402
from dashboard import pipeline  # noqa: E402

FAILS: list[str] = []
NOW = 1_800_000_000.0


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


SCHEMA = """
CREATE TABLE queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
  source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
  rendered_text TEXT, targets_json TEXT, deal_keys_json TEXT, premium_score INTEGER,
  last_error TEXT, created_at REAL NOT NULL);
CREATE TABLE deliveries (
  queue_id INTEGER NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, chunks_sent INTEGER NOT NULL DEFAULT 0,
  last_error TEXT);
CREATE TABLE send_attempts (
  queue_id INTEGER NOT NULL, target TEXT NOT NULL, chunk_index INTEGER NOT NULL,
  payload_hash TEXT NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE blocked_content (
  id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
  msg_id INTEGER, queue_id INTEGER, target TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
  matched TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
  policy_version TEXT NOT NULL DEFAULT '', excerpt TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL);
"""


def add_job(conn, job_id, source, status, created, error=""):
    conn.execute("INSERT INTO queue (id,chat_id,msg_id,source,status,last_error,created_at,"
                 "attempts,next_at) VALUES(?,?,?,?,?,?,?,0,0)",
                 (job_id, -1000 - job_id, job_id, source, status, error, created))


def add_delivery(conn, job_id, target, status, error="", chunks=1):
    conn.execute("INSERT INTO deliveries (queue_id,target,status,attempts,chunks_sent,last_error)"
                 " VALUES(?,?,?,1,?,?)", (job_id, target, status, chunks, error))


def build(path: Path, now: float | None = None) -> sqlite3.Connection:
    """A database shaped like the bot's own; `now` is when "the window" ends."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    hour = 3600.0
    NOW = time.time() if now is None else float(now)

    # 1) posted to two channels, the second one refused: still POSTED.
    add_job(conn, 1, "deals", "done", NOW - 5 * hour)
    add_delivery(conn, 1, "LootZoneIndia11", "sent")
    add_delivery(conn, 1, "PowerLoots1", "skipped", "duplicate product")
    conn.execute("INSERT INTO send_attempts VALUES(1,'LootZoneIndia11',0,'h','sent',?)",
                 (NOW - 5 * hour + 40,))
    # 2) withheld everywhere: nothing sent, one recorded refusal.
    add_job(conn, 2, "deals", "done", NOW - 4 * hour)
    add_delivery(conn, 2, "LootZoneIndia11", "skipped", "product already posted")
    # 3) legacy withheld shape: status='sent' with an error is NOT a send.
    add_job(conn, 3, "TrickXpert", "done", NOW - 3 * hour)
    add_delivery(conn, 3, "LootzoneTricks", "sent", "duplicate product")
    # 4) waiting, still pending.
    add_job(conn, 4, "QuietSource", "pending", NOW - 600)
    # 5) failed with a reason.
    add_job(conn, 5, "BrokenSource", "failed", NOW - 2 * hour, "no verified affiliate link")
    # 6) dropped by the queue with a reason (night window): explained, not lost.
    add_job(conn, 6, "deals", "done", NOW - 90 * 60,
            "night-born deal expired by 06:00 (lists only)")
    # 7) UNACCOUNTED: terminal, no delivery row, no reason at all.
    add_job(conn, 7, "SilentSource", "done", NOW - 70 * 60)
    # 8) outside the window: must not be counted.
    add_job(conn, 8, "deals", "done", NOW - 30 * hour)
    add_delivery(conn, 8, "LootZoneIndia11", "sent")
    conn.commit()
    return conn


def test_engine():
    print("\n== pipeline engine: the equation, from the bot's own tables ==")
    with tempfile.TemporaryDirectory() as td:
        conn = build(Path(td) / "p.sqlite3", now=NOW)
        payload = pipeline.funnel(
            conn, hours=24, now=NOW,
            declared_sources={"deals", "QuietSource", "SilentSource", "BrokenSource"},
            declared_targets=["LootZoneIndia11", "PowerLoots1", "LootzoneTricks",
                              "Under99Loots"],
            watched={"SilentSource"})
        conn.close()

        check("the window is honoured (the 30h-old row is not counted)",
              payload["intake"] == 7, payload["intake"])
        tiles = {tile["key"]: tile for tile in payload["tiles"]}
        check("posted counts a post that reached at least one channel",
              tiles["posted"]["value"] == 1, tiles)
        check("withheld counts per-channel refusals, including the legacy shape",
              tiles["withheld"]["value"] == 2, tiles)
        check("a queue-level drop with a reason is explained, not unaccounted",
              tiles["dropped"]["value"] == 1, tiles)
        check("waiting is live, neither posted nor dropped",
              tiles["waiting"]["value"] == 1, tiles)
        check("a failed row is its own state", tiles["failed"]["value"] == 1, tiles)
        check("exactly the one silent row is UNACCOUNTED",
              tiles["unaccounted"]["value"] == 1, tiles)
        check("the tiles sum to the intake (nothing can hide in a gap)",
              sum(tile["value"] for tile in payload["tiles"] if tile["key"] != "intake")
              == payload["intake"], [(t["key"], t["value"]) for t in payload["tiles"]])
        check("one unaccounted row makes the whole page FAIL, not warn",
              payload["overall"] == "fail", payload["overall"])
        check("the verdict states the equation and the hole",
              "ALERT" in payload["verdict"] and "1" in payload["verdict"], payload["verdict"])
        check("the equation string lists every explained state",
              all(word in payload["equation"] for word in
                  ("posted", "withheld", "dropped", "waiting", "failed")),
              payload["equation"])

        findings = {finding["key"]: finding for finding in payload["findings"]}
        check("the unaccounted row is named by queue id, with a fix",
              "7" in findings["unaccounted"]["detail"] and findings["unaccounted"]["fix"],
              findings.get("unaccounted"))
        check("the failure is reported with its reason",
              "affiliate" in findings["failed"]["detail"], findings.get("failed"))
        check("the configured channel that received nothing is named",
              "Under99Loots" in findings["silent_targets"]["detail"],
              findings.get("silent_targets"))
        check("a source seen in the DB but not in config is named",
              "TrickXpert" in findings["undeclared"]["detail"], findings.get("undeclared"))

        sources = {row["source"]: row for row in payload["sources"]}
        check("a delivering source says so",
              sources["deals"]["verdict"] == "delivering", sources["deals"])
        check("its withheld and explained drops are visible per source, not averaged",
              (sources["deals"]["posted"], sources["deals"]["withheld"],
               sources["deals"]["dropped"]) == (1, 1, 1), sources["deals"])
        check("a source that only ever fails reads as failing",
              sources["BrokenSource"]["verdict"] == "failing", sources["BrokenSource"])
        check("a source with only intake waiting reads as waiting",
              sources["QuietSource"]["verdict"] == "waiting", sources["QuietSource"])
        check("a source with an unaccounted post reads as unaccounted, not quiet",
              sources["SilentSource"]["verdict"] == "unaccounted", sources["SilentSource"])
        check("the watched flag survives into the row",
              sources["SilentSource"]["watched"] is True, sources["SilentSource"])
        check("a source's silence is measured against its own rhythm",
              isinstance(sources["deals"]["silence_ratio"], float),
              sources["deals"].get("silence_ratio"))
        check("first-send latency is measured, not guessed",
              40.0 <= (sources["deals"]["latency_p50"] or 0) <= 41.0, sources["deals"])
        check("problem sources sort above healthy ones",
              payload["sources"][0]["verdict"] in {"withholding", "failing", "silent",
                                                   "overdue", "unaccounted"},
              [row["verdict"] for row in payload["sources"]])
        targets = {row["target"]: row for row in payload["targets"]}
        check("a channel that received a post is 'receiving'",
              targets["LootZoneIndia11"]["state"] == "receiving", targets["LootZoneIndia11"])
        check("a declared channel with zero sends and zero refusals is 'silent'",
              targets["Under99Loots"]["state"] == "silent", targets["Under99Loots"])
        check("the declared-vs-real summary counts what the operator asked for",
              payload["declared_and_real"]["declared"] == 4
              and payload["declared_and_real"]["watched"] == 1,
              payload["declared_and_real"])

        # ---- the same database with the hole removed must read clean --------
        with tempfile.TemporaryDirectory() as td2:
            path = Path(td2) / "clean.sqlite3"
            conn = build(path, now=NOW)
            conn.execute("UPDATE queue SET last_error='manual cancel: test cleanup' WHERE id=7")
            conn.commit()
            ids = pipeline.unaccounted_ids(conn, hours=24, now=NOW)
            clean = pipeline.funnel(conn, hours=24, now=NOW)
            conn.close()
            check("unaccounted_ids finds nothing once a reason exists", ids == [], ids)
            check("with the hole explained the page is no longer FAIL",
                  clean["overall"] != "fail", clean["overall"])
            check("the verdict then says nothing was dropped",
                  clean["verdict"].startswith("Nothing dropped"), clean["verdict"])

        # ---- an unreadable / old database must not explode ------------------
        with tempfile.TemporaryDirectory() as td3:
            empty = sqlite3.connect(Path(td3) / "empty.sqlite3")
            payload = pipeline.funnel(empty, hours=24, now=NOW)
            empty.close()
            check("a database with no bot tables answers 'old', not an exception",
                  payload["database"] == "old" and payload["intake"] == 0,
                  payload["database"])
            check("an empty database has no unaccounted hole",
                  payload["overall"] == "ok", payload["overall"])


async def _started_app():
    runner = web.AppRunner(dash.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server else []
    port = sockets[0].getsockname()[1] if sockets else 8080
    return runner, f"http://127.0.0.1:{port}"


def test_endpoint():
    print("\n== GET /api/pipeline: what the panel is allowed to promise ==")

    async def run():
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            db = tdp / "bot.sqlite3"
            build(db).close()
            saved = {key: os.environ.get(key) for key in
                     ("BOT_DB_PATH", "CHANNELS_JSON_PATH", "DASHBOARD_TOKEN",
                      "DASHBOARD_INSECURE")}
            os.environ["BOT_DB_PATH"] = str(db)
            os.environ["CHANNELS_JSON_PATH"] = str(tdp / "channels.json")
            os.environ["DASHBOARD_TOKEN"] = "test-token-123"
            os.environ.pop("DASHBOARD_INSECURE", None)
            (tdp / "routing_snapshot.json").write_text(json.dumps({
                "live_reload": True, "heartbeat_at": time.time(),
                "targets": ["LootZoneIndia11", "PowerLoots1", "Under99Loots"],
                "sources": ["deals", "QuietSource", "SilentSource", "BrokenSource"],
                "main_sources": ["deals", "QuietSource"],
            }), encoding="utf-8")
            channels_config.save_channels({
                "channels": [{"name": "Under99Loots", "sources": ["*"], "enabled": True}],
                "watch": [{"channel": "SilentSource", "targets": ["*"], "enabled": True}],
            }, tdp / "channels.json")
            runner, base = await _started_app()
            try:
                async with aiohttp.ClientSession() as session:
                    async def call(path, headers=None):
                        async with session.get(base + path,
                                               headers=headers if headers is not None
                                               else {"X-Dashboard-Token": "test-token-123"}) as r:
                            return r.status, await r.json(content_type=None)

                    status, _ = await call("/api/pipeline", headers={})
                    check("the panel needs the token", status == 401, status)
                    status, data = await call("/api/pipeline?hours=24")
                    check("the endpoint answers the equation",
                          status == 200 and data["intake"] == 7
                          and data["equation"].startswith("7 in ="), (status, data.get("equation")))
                    check("the endpoint exposes the unaccounted queue ids for a drill-down",
                          data["unaccounted_ids"] == [7], data.get("unaccounted_ids"))
                    check("declared sources come from the bot's snapshot + watch rows",
                          {"deals", "QuietSource", "SilentSource"}.issubset(
                              set(data["declared_and_real"]["declared_but_silent"])
                              | {"deals", "QuietSource", "SilentSource"}),
                          data["declared_and_real"])
                    check("a configured channel that received nothing is named",
                          any(row["target"] == "Under99Loots" and row["state"] == "silent"
                              for row in data["targets"]), data["targets"])
                    check("the window parameter is clamped, never a 500",
                          (await call("/api/pipeline?hours=99999"))[0] == 200
                          and (await call("/api/pipeline?hours=nonsense"))[0] == 200, "")

                    # A lost post must outrank every cosmetic finding on the console.
                    async with session.get(base + "/api/brief?hours=24&token=test-token-123") as r:
                        brief = await r.json()
                        top = (brief.get("items") or [{}])[0]
                        check("the console's ranked list opens with the unaccounted post",
                              top.get("kind") == "pipeline_unaccounted"
                              and top.get("severity") == "error"
                              and top.get("anchor") == "#pipeline", top)
                        check("the brief carries the same pipeline facts the panel shows",
                              brief.get("pipeline", {}).get("unaccounted") == 1
                              and brief["pipeline"].get("unaccounted_ids") == [7],
                              brief.get("pipeline"))

                    # The CSV must be the same rows the panel shows.
                    async with session.get(base + "/api/export?what=pipeline&hours=24&token=test-token-123") as r:
                        body = await r.text()
                        check("the integrity table exports as CSV with a filename",
                              r.status == 200 and "attachment" in r.headers.get("Content-Disposition", "")
                              and body.splitlines()[0].startswith("kind,source,target,declared"),
                              (r.status, body[:120]))
                        check("the CSV carries the unaccounted row and its verdict",
                              "SilentSource" in body and "unaccounted" in body
                              and "sends" in body.splitlines()[0], body[:200])
                    async with session.get(base + "/api/export?what=nope&token=test-token-123") as r:
                        check("an unknown export names the valid kinds",
                              r.status == 400 and "pipeline" in json.dumps(await r.json()),
                              r.status)

                    async def missing_database():
                        os.environ["BOT_DB_PATH"] = str(tdp / "nope.sqlite3")
                        status, data = await call("/api/pipeline")
                        check("no database is 'unknown', never a fake zero",
                              status == 200 and data["database"] == "missing"
                              and data["overall"] == "unknown", (status, data.get("database")))

                        bad = tdp / "corrupt.sqlite3"
                        bad.write_bytes(b"this is not a database" * 40)
                        os.environ["BOT_DB_PATH"] = str(bad)
                        status, data = await call("/api/pipeline")
                        check("an unreadable database is a readable 503, not a stack trace",
                              status == 503 and "sqlite" in json.dumps(data).lower()
                              and "traceback" not in json.dumps(data).lower(),
                              (status, data))

                    await missing_database()
            finally:
                await runner.cleanup()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    asyncio_run(run())


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def test_panel_markup():
    print("\n== the served page: the integrity panel ==")
    html = dash.UI_HTML
    check("the page has a pipeline integrity card", 'id="pipeline"' in html)
    check("it shows the equation and the tiles",
          'id="plineverdict"' in html and 'id="plinetiles"' in html)
    check("it lists findings with fixes and per-source rows",
          'id="plinefindings"' in html and 'id="plinesources"' in html)
    check("it calls the endpoint through the shared token-aware helper",
          "/api/pipeline" in html and "loadPipeline(" in html)
    check("the copy states the promise in words an operator can check",
          "Nothing dropped" in html or "unaccounted" in html)
    script = html.split("<script>")[1].split("</script>")[0]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ui.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        check("the served JavaScript still parses", proc.returncode == 0, proc.stderr[:300])


def test_cli():
    print("\n== ops/pipeline_check.py: the same equation, from cron ==")
    script = ROOT / "ops" / "pipeline_check.py"
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "bot.sqlite3"
        build(db).close()
        env = dict(os.environ, BOT_DB_PATH=str(db),
                   CHANNELS_JSON_PATH=str(Path(td) / "no-channels.json"))

        def run(*args):
            return subprocess.run([sys.executable, str(script), *args],
                                  capture_output=True, text=True, env=env)

        proc = run("--no-config")
        check("a lost post exits 1 and prints the queue id",
              proc.returncode == 1 and "queue 7" in proc.stdout, (proc.returncode, proc.stdout))
        check("the human output starts with the equation",
              proc.stdout.startswith("PIPELINE | ALERT:") and "7 in = 1 posted" in proc.stdout,
              proc.stdout[:200])
        proc = run("--no-config", "--json")
        payload = json.loads(proc.stdout)
        check("--json is machine-readable and carries the exit code",
              payload["exit_code"] == 1 and payload["unaccounted_ids"] == [7],
              payload.get("exit_code"))
        check("--json never prints a Python traceback",
              "Traceback" not in proc.stdout + proc.stderr, proc.stderr[:200])

        conn = sqlite3.connect(db)
        conn.execute("UPDATE queue SET last_error='manual cancel: test' WHERE id=7")
        conn.commit()
        conn.close()
        proc = run("--no-config")
        check("with the hole explained the check is clean (exit 0)",
              proc.returncode == 0 and "VERDICT: clean" in proc.stdout,
              (proc.returncode, proc.stdout[-200:]))
        proc = run("--no-config", "--strict")
        check("--strict turns the remaining warning into a failure",
              proc.returncode == 1 and "warnings under --strict" in proc.stdout,
              (proc.returncode, proc.stdout[-160:]))

        proc = run("--db", str(Path(td) / "missing.sqlite3"))
        check("a missing database exits 2 with a readable sentence",
              proc.returncode == 2 and "cannot read" in proc.stderr, proc.stderr[:200])
        proc = run("--json", "--db", str(Path(td) / "missing.sqlite3"))
        check("--json reports the same failure as data",
              proc.returncode == 2 and "could not run" in proc.stdout, proc.stdout[:200])


if __name__ == "__main__":
    test_engine()
    test_endpoint()
    test_panel_markup()
    test_cli()
    print("\n" + ("PIPELINE INTEGRITY TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_pipeline_integrity: all checks PASS"))
    sys.exit(1 if FAILS else 0)
