#!/usr/bin/env python3
"""Ops insight engine + the five new dashboard endpoints.

`dashboard/insight.py` answers the questions an operator otherwise SSHes in for:
is anything broken (health), what just happened (pulse), what happened to THIS
post (trace), where is that thing I remember (search), and who is actually
receiving (scorecard).

These tests build a real bot database with the bot's own Store, then check the
engine and the HTTP API against it. No Telegram, no network, no posting.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
os.environ.setdefault("EARNKARO_PUBLISHER_ID", "5478322")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))
import main_bot_new as bot  # noqa: E402
from dashboard import app as dash  # noqa: E402
from dashboard import insight  # noqa: E402
from dashboard import source_target  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra[:400]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def build_db(path: Path, now: float) -> None:
    store = bot.Store(path)
    rows = [
        # id, source, text, status, attempts, error
        (1, "DealSauce", "Samsung neo qled 55\u201d @ \u20b960k\nhttps://link.amazon/B0gWibFuA",
         "done", 1, ""),
        (2, "DealSauce", "boAt Airdopes 141 @ \u20b9899\nhttps://amzn.to/abc141", "done", 1, ""),
        (3, "TrickXpert", "Prestige 5L cooker @ \u20b91,299\nhttps://fkrt.co/x9", "pending", 0, ""),
        (4, "DealSauce", "Sony Bravia 55 inch 4K TV @ \u20b954,990", "failed", 3, "FloodWait 30s"),
    ]
    for qid, source, text, status, attempts, error in rows:
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json,premium_score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, -qid, qid, source, status, attempts, error, now - qid * 600, str(-qid), text,
             '["LootZoneIndia11"]', 0 if qid != 2 else 900))
        if status in ("done", "failed"):
            store.conn.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?)",
                               (qid, "LootZoneIndia11", "sent" if status == "done" else "skipped",
                                1 if status == "done" else 0, 1 if status == "done" else 0, ""))
            store.conn.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?)",
                               (qid, "ElectroShelf", "skipped", 1, 0,
                                "content policy: refusal"))
        store.conn.execute("INSERT INTO send_attempts VALUES(?,?,?,?,?,?)",
                           (qid, "LootZoneIndia11", 0, f"h{qid}",
                            "sent" if status == "done" else "started", now - qid * 300))

    # The refusal ledger, written exactly the shape the bot writes it (the
    # excerpt masks the matched term; here the test states the real columns).
    store.conn.executemany(
        "INSERT INTO blocked_content(stage,source,msg_id,queue_id,target,category,label,"
        "matched,reason,policy_version,excerpt,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [("intake", "TrickXpert", 771, None, "", "sexual_explicit",
          "Explicit sexual content", "sex",
          "Our channels are public and read by everyone, including women and families. "
          "Explicit sexual content is never posted.", "2026-10-19.4",
          "*** video leaked, join fast", now - 90),
         ("presend", "Offerzone_deals", 772, 3, "LootZoneIndia11", "gambling_betting",
          "Gambling / betting", "1xbet.com",
          "Betting spam is not a deal and is often a scam.", "2026-10-19.4",
          "******** bonus join now", now - 120)])
    now_ = now
    for src, aff, res, key in [
            ("https://link.amazon/B0gWibFuA", "https://bitli.in/neoQLED55",
             "https://www.amazon.in/dp/B0CZXQ9K7T", "ASIN:B0CZXQ9K7T"),
            ("https://amzn.to/abc141", "https://bitli.in/boat141",
             "https://www.amazon.in/dp/B08XYZ", "ASIN:B08XYZ")]:
        store.conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)",
                           (src, aff, res, key, now_ - 200))
    store.conn.execute(
        "INSERT INTO owned_link_proofs(affiliate_url,destination_url,proof_type,publisher_id,"
        "created_at) VALUES(?,?,?,?,?)",
        ("https://bitli.in/neoQLED55", "https://www.amazon.in/dp/B0CZXQ9K7T", "amazon_tag",
         "mama086-21", now_ - 100))
    store.conn.commit()
    store.conn.close()


def seed_snapshot(path: Path, now: float) -> dict:
    snap = {
        "started_at": now - 3600,
        "sources": ["DealSauce", "TrickXpert"],
        "main_sources": ["DealSauce"],
        "targets": ["LootZoneIndia11", "PowerLoots1", "Under99Deals11", "ElectroShelf"],
        "source_targets": {"DealSauce": ["LootZoneIndia11", "PowerLoots1"]},
        "price_targets": {"under99": "Under99Deals11", "under499": "under499loots",
                          "premium": "Premiumlootsdeals", "power": "PowerLoots1"},
        "category_filters": {"ElectroShelf": ["electronics"]},
        "categories": {"electronics": "Electronics", "fashion": "Clothing"},
    }
    path.write_text(json.dumps(snap), encoding="utf-8")
    return snap


def test_engine():
    print("\n== engine: health / pulse / trace / search / scorecard ==")
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        db = tdp / "ops.sqlite3"
        build_db(db, now)
        snap = seed_snapshot(tdp / "routing_snapshot.json", now)
        channels = {"version": 1, "channels": [
            {"name": "ElectroShelf", "sources": ["DealSauce"],
             "categories": ["electronics"], "enabled": True}]}
        (tdp / "channels.json").write_text(json.dumps(channels), encoding="utf-8")
        log = tdp / "bot.log"
        log.write_text("2026-10-20 10:00:00 INFO started\n"
                       "2026-10-20 10:05:00 ERROR could not deliver\n", encoding="utf-8")

        # ---- health ------------------------------------------------------
        report = insight.health(db=db, root=ROOT, channels_json=tdp / "channels.json",
                                snapshot=snap, log_path=log, now=now)
        keys = {item["key"] for item in report["checks"]}
        for expected in ("db_present", "db_integrity", "db_schema", "queue_backlog",
                         "queue_failures", "send_flow", "safety_blocks", "rules_parity",
                         "policy_parity", "channels_config", "routing_snapshot", "bot_log",
                         "disk"):
            check(f"health reports {expected}", expected in keys, str(sorted(keys)))
        check("health is never 'ok' while a post is failed",
              report["overall"] in ("warn", "fail"), report["overall"])
        failed = next(c for c in report["checks"] if c["key"] == "queue_failures")
        check("a failed post names its error", "FloodWait" in failed["detail"], failed["detail"])
        check("a non-ok check carries a fix line",
              all(c["fix"] for c in report["checks"] if c["state"] in ("warn", "fail")))
        check("health counts add up to the checks",
              sum(report["counts"].values()) == len(report["checks"]))
        check("health reads the real backlog",
              report["facts"]["queue"].get("pending") == 1, str(report["facts"]["queue"]))
        check("health knows uptime from the snapshot",
              report["facts"]["bot_uptime_s"] > 3000, str(report["facts"]["bot_uptime_s"]))
        check("health names the missing bridge instead of failing",
              next(c for c in report["checks"] if c["key"] == "policy_parity")["state"]
              in ("unknown", "ok", "warn"))

        # a missing database is a fault, not a crash
        gone = insight.health(db=tdp / "nope.sqlite3", root=ROOT,
                              channels_json=tdp / "channels.json", snapshot={},
                              log_path=None, now=now)
        check("a missing database is reported as a fault", gone["overall"] == "fail",
              gone["overall"])
        check("...and still returns checks", bool(gone["checks"]))

        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # ---- pulse ---------------------------------------------------
            flow = insight.pulse(conn, hours=24, now=now)
            check("pulse counts every kind",
                  set(flow["counts"]) == set(insight.EVENT_KINDS), str(flow["counts"]))
            check("pulse sees the intakes", flow["counts"]["intake"] == 4,
                  str(flow["counts"]))
            check("pulse sees the refusals", flow["counts"]["block"] == 2,
                  str(flow["counts"]))
            check("pulse sees link conversions", flow["counts"]["link"] == 2)
            check("pulse sees the owned-link proof", flow["counts"]["proof"] == 1)
            check("pulse is newest-first",
                  all(flow["events"][i]["at"] >= flow["events"][i + 1]["at"]
                      for i in range(len(flow["events"]) - 1)))
            check("pulse carries an IST stamp and an age per event",
                  all(ev["at_ist"].endswith("IST") and ev["age"] for ev in flow["events"]))
            only_blocks = insight.pulse(conn, hours=24, kind="block", now=now)
            check("pulse filters by kind",
                  {e["kind"] for e in only_blocks["events"]} == {"block"})
            by_source = insight.pulse(conn, hours=24, source="TrickXpert", now=now)
            check("pulse filters by source",
                  all(e["source"] == "TrickXpert" for e in by_source["events"]),
                  str([e["source"] for e in by_source["events"]]))
            empty = insight.pulse(conn, hours=0.25, now=now + 30 * 86400)
            check("an empty window is empty, not an error",
                  empty["events"] == [] and empty["counts"]["send"] == 0)
            check("last-of-kind is reported for each kind",
                  set(empty["last"]) == set(insight.EVENT_KINDS))
            check("pulse returns one bucket per hour, capped at 24",
                  len(flow["hourly"]) == 24, str(len(flow["hourly"])))
            check("the hourly buckets carry every kind",
                  all(set(insight.EVENT_KINDS) <= set(bucket) for bucket in flow["hourly"]))
            check("the bucket total matches the events",
                  sum(b["intake"] for b in flow["hourly"]) == flow["counts"]["intake"],
                  f'{sum(b["intake"] for b in flow["hourly"])} vs {flow["counts"]["intake"]}')
            short = insight.pulse(conn, hours=3, now=now)
            check("a 3h window gives 3 buckets", len(short["hourly"]) == 3)

            csv_text = insight.to_csv(flow["events"], ["at_ist", "kind", "label"])
            check("csv has a header and one line per row",
                  csv_text.splitlines()[0] == "at_ist,kind,label"
                  and len(csv_text.splitlines()) == len(flow["events"]) + 1)
            quoted = insight.to_csv([{"a": 'x,y', "b": 1, "c": None}], ["a", "b", "c"])
            check("csv quotes a comma-bearing field instead of splitting it",
                  quoted.splitlines() == ["a,b,c", '"x,y",1,'], repr(quoted))

            # ---- trace ---------------------------------------------------
            traced = insight.trace(conn, 1, now=now, analyze=source_target.analyze,
                                   snapshot=snap)
            check("trace shows what was received",
                  traced["received_by"] == ["LootZoneIndia11"], str(traced["received_by"]))
            check("trace shows what was withheld", traced["withheld_by"] == ["ElectroShelf"])
            check("trace says WHY a target withheld",
                  any("content policy" in row["detail"]
                      for row in traced["planned_vs_actual"]))
            check("trace prices the reported post from the post itself",
                  (traced["predicted"] or {}).get("facts", {}).get("price") == 60000,
                  str((traced["predicted"] or {}).get("facts")))
            check("trace finds the converted link",
                  any("bitli.in/neoQLED55" in l["affiliate"] for l in traced["links"]),
                  str(traced["links"]))
            check("trace carries the source text",
                  "neo qled" in traced["post"]["text"].lower())
            pending = insight.trace(conn, 3, now=now, analyze=source_target.analyze,
                                    snapshot=snap)
            check("a pending post traces without deliveries",
                  pending["deliveries"] == [] and pending["post"]["status"] == "pending")
            refused = insight.trace(conn, 3, now=now, analyze=source_target.analyze,
                                    snapshot=snap)
            check("a withheld post shows the refusal row", len(refused["blocks"]) == 1,
                  str(refused["blocks"]))
            try:
                insight.trace(conn, 9999, now=now)
                check("tracing a missing post raises", False)
            except LookupError:
                check("tracing a missing post raises LookupError", True)

            # ---- search --------------------------------------------------
            hit = insight.search(conn, "qled", now=now)
            check("search finds a post by its text", hit["count"] == 1, str(hit["kinds"]))
            check("search marks what it found", hit["results"][0]["kind"] == "post")
            by_id = insight.search(conn, "2", now=now)
            check("search by queue id returns that post",
                  any(r["queue_id"] == 2 and r["kind"] == "post" for r in by_id["results"]),
                  str([(r["kind"], r["queue_id"]) for r in by_id["results"]]))
            at_channel = insight.search(conn, "@ElectroShelf", now=now)
            check("search accepts @channel", at_channel["count"] >= 1, str(at_channel))
            refusal = insight.search(conn, "1xbet", now=now)
            check("search finds a refusal", refusal["kinds"].get("refusal") == 1)
            check("search escapes LIKE wildcards",
                  insight.search(conn, "100%", now=now)["count"] == 0)
            try:
                insight.search(conn, "x", now=now)
                check("one character is refused", False)
            except ValueError:
                check("one character is refused", True)

            # ---- scorecard -----------------------------------------------
            card = insight.scorecard(conn, days=7, now=now)
            by_target = {row["target"]: row for row in card["channels"]}
            check("scorecard counts real sends", by_target["LootZoneIndia11"]["sends"] == 2,
                  str(by_target.get("LootZoneIndia11")))
            check("scorecard counts refusals separately",
                  by_target["ElectroShelf"]["skips"] == 3,
                  str(by_target.get("ElectroShelf")))
            check("scorecard names the top skip reason",
                  "content policy" in by_target["ElectroShelf"]["top_skip_reason"])
            check("scorecard shows the last send",
                  by_target["LootZoneIndia11"]["last_send_ist"].endswith("IST"))
            check("scorecard has a source view",
                  {row["source"] for row in card["sources"]} == {"DealSauce", "TrickXpert"})
            check("scorecard totals reconcile",
                  card["totals"]["sends"] == sum(r["sends"] for r in card["channels"]))
        finally:
            conn.close()


def test_http():
    print("\n== HTTP: health / pulse / trace / search / scorecard ==")

    async def run():
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            db = tdp / "ops.sqlite3"
            now = time.time()
            build_db(db, now)
            seed_snapshot(tdp / "routing_snapshot.json", now)
            (tdp / "channels.json").write_text(json.dumps({"version": 1, "channels": []}))
            saved = {key: os.environ.get(key) for key in
                     ("BOT_DB_PATH", "CHANNELS_JSON_PATH", "DASHBOARD_TOKEN",
                      "DASHBOARD_INSECURE", "DASHBOARD_RESTART_CMD", "BOT_LOG")}
            os.environ["BOT_DB_PATH"] = str(db)
            os.environ["CHANNELS_JSON_PATH"] = str(tdp / "channels.json")
            os.environ["DASHBOARD_TOKEN"] = "test-token-123"
            os.environ.pop("DASHBOARD_INSECURE", None)
            os.environ["DASHBOARD_RESTART_CMD"] = "true"
            os.environ["BOT_LOG"] = str(tdp / "missing.log")
            runner, base = await dash_started()
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async def call(path, token=True):
                        headers = {"X-Dashboard-Token": "test-token-123"} if token else {}
                        async with sess.get(base + path, headers=headers) as resp:
                            try:
                                body = await resp.json()
                            except (ValueError, aiohttp.ContentTypeError):
                                body = {}
                            return resp.status, body

                    status, body = await call("/api/health", token=False)
                    check("health requires the token", status == 401, str(status))
                    status, body = await call("/api/health")
                    check("health answers 200", status == 200, str(status))
                    check("health names its overall state",
                          body.get("overall") in ("ok", "warn", "fail", "unknown"),
                          str(body.get("overall")))
                    check("health returns checks with fixes",
                          all(set(c) >= {"key", "title", "state", "detail"} for c in body["checks"]))
                    check("health reports the routing snapshot is present",
                          any(c["key"] == "routing_snapshot" and c["state"] == "ok"
                              for c in body["checks"]))
                    status, body = await call("/api/health?services=1")
                    check("health can ask systemd without failing", status == 200, str(status))

                    status, body = await call("/api/pulse?hours=24")
                    check("pulse answers 200", status == 200)
                    check("pulse lists events", len(body.get("events", [])) >= 5,
                          str(len(body.get("events", []))))
                    status, body = await call("/api/pulse?kind=block")
                    check("pulse honours the kind filter",
                          {e["kind"] for e in body["events"]} == {"block"})
                    status, body = await call("/api/pulse?hours=nonsense")
                    check("a bad window falls back instead of 500", status == 200, str(status))

                    status, body = await call("/api/trace?queue_id=1")
                    check("trace answers 200", status == 200, str(status))
                    check("trace reports who received the post",
                          body["received_by"] == ["LootZoneIndia11"], str(body.get("received_by")))
                    check("trace includes the live safety policy", "policy" in body)
                    status, _ = await call("/api/trace?queue_id=abc")
                    check("a non-numeric queue id is a 400", status == 400, str(status))
                    status, body = await call("/api/trace?queue_id=424242")
                    check("an unknown queue id is a 404", status == 404, str(status))
                    check("...with a readable error", "no queue row" in body.get("error", ""))

                    status, body = await call("/api/search?q=qled")
                    check("search answers 200", status == 200)
                    check("search finds the posted deal", body.get("count") == 1, str(body))
                    status, _ = await call("/api/search?q=a")
                    check("a one-letter search is a 400", status == 400, str(status))
                    status, body = await call("/api/search?q=")
                    check("an empty search is a 400", status == 400, str(status))

                    # ---- CSV exports: same rows, with a header and a filename --
                    async def export(path, token=True):
                        headers = {"X-Dashboard-Token": "test-token-123"} if token else {}
                        async with sess.get(base + path, headers=headers) as resp:
                            return resp.status, await resp.text(), dict(resp.headers)

                    status, text, headers = await export("/api/export?what=scorecard")
                    check("scorecard csv exports", status == 200, str(status))
                    check("the export is served as a file",
                          headers.get("Content-Disposition", "").startswith("attachment;"),
                          str(headers.get("Content-Disposition")))
                    check("the scorecard csv has a header row",
                          text.splitlines()[0].startswith("kind,target,source"), text[:80])
                    check("the scorecard csv has one line per row",
                          len(text.strip().splitlines()) >= 3, text[:120])
                    status, text, _ = await export("/api/export?what=pulse&hours=24")
                    check("the timeline csv exports", status == 200 and
                          text.splitlines()[0].startswith("at_ist,kind"), text[:80])
                    check("timeline rows are IST-stamped",
                          all("IST" in line for line in text.strip().splitlines()[1:6]))
                    status, text, _ = await export("/api/export?what=search&q=qled")
                    check("the search csv exports", status == 200 and "qled" in text, text[:80])
                    status, text, _ = await export("/api/export?what=health")
                    check("the health csv exports", status == 200 and
                          text.splitlines()[0] == "key,title,state,detail,fix", text[:80])
                    check("an export without a token is refused",
                          (await export("/api/export?what=health", token=False))[0] == 401)
                    check("an unknown export is a 400",
                          (await export("/api/export?what=nope"))[0] == 400)
                    check("an unknown format is a 400",
                          (await export("/api/export?what=health&format=xml"))[0] == 400)
                    status, text, _ = await export("/api/export?what=health&format=json")
                    check("json is offered too", status == 200 and '"checks"' in text, text[:80])
                    status, text, _ = await export("/api/export?what=search&q=a")
                    check("a too-short search export is a 400", status == 400, str(status))

                    status, body = await call("/api/scorecard?days=7")
                    check("scorecard answers 200", status == 200)
                    check("scorecard lists channels and sources",
                          body.get("channels") and body.get("sources"))
                    check("scorecard carries the window",
                          body.get("days") == 7, str(body.get("days")))
            finally:
                await runner.cleanup()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    asyncio.run(run())


async def dash_started():
    runner = web.AppRunner(dash.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server else []
    port = sockets[0].getsockname()[1] if sockets else 8080
    return runner, f"http://127.0.0.1:{port}"


def main() -> int:
    test_engine()
    test_http()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: " + "; ".join(FAILS[:6]))
        return 1
    print("\nops insight PASS: health, timeline, trace, search and scorecard, "
          "engine and HTTP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
