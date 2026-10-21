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
from dashboard import insight, trends  # noqa: E402
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


def build_busy_db(path: Path, now: float) -> None:
    """A database whose WINDOW is bigger than the event list this time.

    The capped list is exactly what /api/pulse used to count: 4,999 intakes and
    6,813 sends in 24 h were reported as "500", the list limit. These numbers
    are small enough to assert exactly and still far above any list limit.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE queue (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
      msg_id INTEGER NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
      rendered_text TEXT, targets_json TEXT, deal_keys_json TEXT, premium_score INTEGER,
      last_error TEXT, created_at REAL NOT NULL);
    CREATE TABLE deliveries (queue_id INTEGER NOT NULL, target TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
      chunks_sent INTEGER NOT NULL DEFAULT 0, last_error TEXT, PRIMARY KEY(queue_id,target));
    CREATE TABLE send_attempts (queue_id INTEGER NOT NULL, target TEXT NOT NULL,
      chunk_index INTEGER NOT NULL, payload_hash TEXT NOT NULL, state TEXT NOT NULL,
      updated_at REAL NOT NULL, PRIMARY KEY(queue_id,target,chunk_index));
    CREATE TABLE blocked_content (id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL,
      source TEXT NOT NULL DEFAULT '', msg_id INTEGER, queue_id INTEGER,
      target TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
      label TEXT NOT NULL DEFAULT '', matched TEXT NOT NULL DEFAULT '',
      excerpt TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL DEFAULT '',
      reason TEXT NOT NULL DEFAULT '', policy_version TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL);
    CREATE TABLE link_cache (source_url TEXT PRIMARY KEY, affiliate_url TEXT NOT NULL,
      resolved_url TEXT, deal_key TEXT, created_at REAL NOT NULL);
    """)
    qid = 0
    queue_rows, delivery_rows, attempt_rows = [], [], []
    # current window: 900 intakes, 800 delivered, 200 skipped, 30 refused, 500 links
    for i in range(900):
        qid += 1
        at = now - 23 * 3600 + i * 60          # inside the last 24 h
        status = "pending" if i >= 895 else ("failed" if i >= 888 else "done")
        source = "DealSauce" if i % 2 else "TrickXpert"
        queue_rows.append((qid, -1, qid, source, status, 1, 0, "deal " + str(i),
                           None, None, 50, "FloodWait" if status == "failed" else "", at))
        if i < 800:
            target = "LootZoneIndia11" if i % 2 else "PowerLoots1"
            delivery_rows.append((qid, target, "sent", 1, 1, ""))
            attempt_rows.append((qid, target, 0, f"h{qid}", "sent", at + 5))
        elif i < 1000:
            target = "LootZoneIndia11" if i % 2 else "PowerLoots1"
            delivery_rows.append((qid, target, "skipped", 1, 0,
                                  "duplicate deal_key" if i % 3 else "budget: 30/day"))
    # previous window (24-48 h): 400 intakes, 300 delivered, 100 skipped, 200 refused
    for i in range(400):
        qid += 1
        at = now - 47 * 3600 + i * 60
        queue_rows.append((qid, -1, qid, "DealSauce", "done", 1, 0, "old deal " + str(i),
                           None, None, 50, "", at))
        if i < 340:
            delivery_rows.append((qid, "LootZoneIndia11", "sent", 1, 1, ""))
            attempt_rows.append((qid, "LootZoneIndia11", 0, f"o{qid}", "sent", at + 5))
        else:
            delivery_rows.append((qid, "LootZoneIndia11", "skipped", 1, 0,
                                  "duplicate deal_key"))
    conn.executemany("INSERT INTO queue VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", queue_rows)
    conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", delivery_rows)
    conn.executemany("INSERT INTO send_attempts VALUES (?,?,?,?,?,?)", attempt_rows)
    conn.executemany(
        "INSERT INTO blocked_content(stage,source,msg_id,queue_id,target,category,label,"
        "matched,excerpt,severity,reason,policy_version,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("intake", "TrickXpert", 1000 + i, None, "", "gambling_betting", "Gambling",
          "1xbet", "***", "block", "not a deal", "5", now - 20 * 3600 + i * 60)
         for i in range(30)]
        + [("intake", "TrickXpert", 2000 + i, None, "", "gambling_betting", "Gambling",
            "1xbet", "***", "block", "not a deal", "5", now - 40 * 3600 + i * 60)
           for i in range(200)])
    conn.executemany("INSERT INTO link_cache VALUES (?,?,?,?,?)",
                     [(f"https://s/{i}", f"https://bitli.in/{i}", "https://www.amazon.in/dp/X",
                       f"k{i}", now - 22 * 3600 + i * 60) for i in range(500)]
                     # Three tokenless hops over an untagged Amazon page - the exact
                     # shape of the two is.gd links reported on 2026-10-21.
                     + [(f"https://s/hop{i}", f"https://is.gd/hop{i}",
                         "https://www.amazon.in/dp/Y", f"h{i}", now - 3 * 3600 + i * 60)
                        for i in range(3)])
    conn.commit()
    conn.close()


def test_counts_and_trends():
    print("\n== exact counts + trends: this window against the one before ==")
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "busy.sqlite3"
        build_busy_db(db, now)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # ---- the count bug: a capped list is not a count --------------
            flow = insight.pulse(conn, hours=24, limit=50, now=now)
            truth = {
                "intake": conn.execute("SELECT COUNT(*) FROM queue WHERE created_at>=?",
                                       (now - 86400,)).fetchone()[0],
                "send": conn.execute("SELECT COUNT(*) FROM send_attempts WHERE updated_at>=?",
                                     (now - 86400,)).fetchone()[0],
                "block": conn.execute("SELECT COUNT(*) FROM blocked_content WHERE created_at>=?",
                                      (now - 86400,)).fetchone()[0],
                "link": conn.execute("SELECT COUNT(*) FROM link_cache WHERE created_at>=?",
                                     (now - 86400,)).fetchone()[0],
            }
            check("pulse counts the whole window, not the 50 rows it shows",
                  len(flow["events"]) == 50
                  and all(flow["counts"][key] == value for key, value in truth.items()),
                  f"{flow['counts']} vs {truth}")
            check("an exact count is bigger than the visible list",
                  flow["counts"]["intake"] > len(flow["events"]))
            check("the chart adds up to the totals it is drawn from",
                  sum(b["intake"] for b in flow["hourly"]) == truth["intake"]
                  and sum(b["send"] for b in flow["hourly"]) == truth["send"],
                  f'{sum(b["intake"] for b in flow["hourly"])} vs {truth["intake"]}')
            check("the payload says the counts are exact and only the list is capped",
                  flow.get("exact_counts") is True and flow["capped"] is True)
            check("a filtered view counts only what it shows",
                  insight.pulse(conn, hours=24, kind="block", now=now)["counts"]["send"] == 0)
            seven = insight.pulse(conn, hours=168, now=now)
            check("a 7-day window keeps 24 bars that split the whole window",
                  len(seven["hourly"]) == 24
                  and sum(b["intake"] for b in seven["hourly"]) == seven["counts"]["intake"],
                  str(seven["chart"]))
            check("a 7-day bar is seven hours wide, not one",
                  seven["chart"]["bucket_seconds"] == 7 * 3600, str(seven["chart"]))

            # ---- trends ---------------------------------------------------
            window = trends.compare(conn, hours=24, now=now)
            by_key = window["by_metric"]
            check("trends: delivered count", by_key["sent"]["current"] == 800,
                  str(by_key["sent"]))
            check("trends: the window before", by_key["sent"]["previous"] == 340)
            check("trends: exact delta and percentage",
                  by_key["sent"]["delta"] == 460 and by_key["sent"]["pct"] == 135.3,
                  f"{by_key['sent']['delta']} {by_key['sent']['pct']}")
            check("trends: more sends is a good movement",
                  by_key["sent"]["sentiment"] == "good" and by_key["sent"]["direction"] == "up")
            check("trends: more skips is a bad movement",
                  by_key["skipped"]["current"] == 100 and by_key["skipped"]["previous"] == 60
                  and by_key["skipped"]["sentiment"] == "bad", str(by_key["skipped"]))
            check("trends: refusals fell",
                  by_key["refused"]["current"] == 30 and by_key["refused"]["previous"] == 200
                  and by_key["refused"]["sentiment"] == "good", str(by_key["refused"]))
            check("trends: every bar list adds up to its own total",
                  all(sum(m["series"]) == m["current"] for m in window["metrics"]
                      if m["series"]), str([(m["key"], sum(m["series"]), m["current"])
                                            for m in window["metrics"] if m["series"]]))
            check("trends: a metric with no history gets no fake percentage",
                  by_key["converted"]["previous"] == 0
                  and by_key["converted"]["pct"] is None
                  and by_key["converted"]["baseline_thin"] is True,
                  str(by_key["converted"]))
            check("trends: the backlog is a snapshot without a delta",
                  by_key["backlog"]["current"] == 5 and by_key["backlog"]["pct"] is None,
                  str(by_key["backlog"]))
            check("trends: the failed queue rows are counted",
                  by_key["failed"]["current"] == 7, str(by_key["failed"]))
            check("trends: per channel, both directions",
                  {row["target"] for row in window["channels"]}
                  == {"LootZoneIndia11", "PowerLoots1"}
                  and any(row["sentiment"] == "good" for row in window["channels"]),
                  str(window["channels"]))
            check("trends: skip reasons ranked by how often they happen now",
                  window["skip_reasons"][0]["current"] >= window["skip_reasons"][-1]["current"])
            check("trends: one plain sentence with both numbers",
                  "800 sent" in window["verdict"] and "before" in window["verdict"],
                  window["verdict"])
            week = trends.compare(conn, hours=168, now=now)
            check("trends: the window label follows the window",
                  week["window_label"] == "7d" and "7d" in week["verdict"], week["verdict"])
            check("trends: summary is the cheap subset used by the brief",
                  trends.summary(conn, hours=24, now=now)["sent"]["current"] == 800)
            check("trends: safety spike reads the last hour and the week",
                  trends.safety_spike(conn, now=now)["week"] == 230,
                  str(trends.safety_spike(conn, now=now)))
        finally:
            conn.close()


def test_attention():
    print("\n== attention: one ranked list, every row with its fix ==")
    health = {
        "checks": [
            {"key": "db_present", "title": "Bot database", "state": "ok", "detail": "ok", "fix": ""},
            {"key": "queue_failures", "title": "Failed posts", "state": "fail",
             "detail": "9 failed", "fix": "Open the trace."},
            {"key": "disk", "title": "Disk space", "state": "warn",
             "detail": "4% free", "fix": "Free space."},
            {"key": "policy_parity", "title": "Safety policy", "state": "unknown",
             "detail": "no bridge", "fix": "Set BRIDGE_DIR."},
        ],
        "overall": "fail",
    }
    brief = trends.attention(health=health)
    check("only fail/warn become actions, not ok or unknown",
          [item["title"] for item in brief["items"]] == ["Failed posts", "Disk space"],
          str([i["title"] for i in brief["items"]]))
    check("a fault sorts before a warning", brief["items"][0]["severity"] == "error")
    check("each action carries a fix and the panel that explains it",
          all(item["fix"] and item["anchor"] for item in brief["items"]))
    check("the counts add up to the list",
          sum(brief["counts"].values()) == len(brief["items"]))
    check("the headline says how many need you", brief["headline"].startswith("2 things need you"),
          brief["headline"])
    check("an unknown check is not silently promoted or dropped", 
          all("Safety policy" != item["title"] for item in brief["items"]))

    clean = trends.attention(health={"checks": [{"key": "db_present", "title": "Bot database",
                                                "state": "ok", "detail": "fine", "fix": ""}]})
    check("nothing wrong says nothing needs you",
          clean["healthy"] and clean["items"] == []
          and clean["headline"].startswith("Nothing needs you"), clean["headline"])

    drift = trends.attention(drift={"installed": 5, "running": 4, "pending_restart": True})
    titles = [item["title"] for item in drift["items"]]
    check("a rules drift is an error and names both versions",
          any("v4" in t and "v5" in t for t in titles), str(titles))
    check("a pending restart is a warning with the restart command",
          any("restart" in (item["fix"] or "") for item in drift["items"]), str(drift["items"]))

    spike = trends.attention(safety={"last_hour": 40, "avg_hour": 2.0})
    check("a refusal spike is called out with both numbers",
          "40 refusals" in spike["items"][0]["title"]
          and "2.0" in spike["items"][0]["detail"], str(spike["items"]))
    quiet = trends.attention(safety={"last_hour": 3, "avg_hour": 2.0})
    check("a normal refusal rate is not a crisis", quiet["items"] == [])

    stalled = trends.attention(trends={"window_label": "24h", "by_metric": {
        "sent": {"current": 0, "previous": 400, "pct": None},
        "skipped": {"current": 0, "previous": 0, "pct": None},
        "refused": {"current": 0, "previous": 0, "pct": None}}})
    check("nothing delivered is an error that names the window",
          any(item["severity"] == "error" and "Nothing delivered" in item["title"]
              for item in stalled["items"]), str(stalled["items"]))

    foreign = trends.attention(conversion={"foreign_tag": 3, "earns_nothing": 10, "posts": 10})
    check("a foreign affiliate tag is an error about whose money it is",
          any("someone else's affiliate tag" in item["title"] for item in foreign["items"]),
          str(foreign["items"]))
    check("...and it tells the operator what to run",
          any("conversion_report" in (item["fix"] or "") for item in foreign["items"]))

    cats = trends.attention(categories=[
        {"severity": "warn", "kind": "no_channel", "category": "beauty",
         "detail": "no channel can receive it"}])
    check("a category finding becomes an action pointing at Categories",
          cats["items"][0]["anchor"] == "#categories"
          and "beauty" in cats["items"][0]["title"], str(cats["items"]))

    both = trends.attention(health=health, drift={"installed": 5, "running": 4},
                            trends={"window_label": "1h", "by_metric": {
                                "sent": {"current": 0, "previous": 0, "pct": None}}},
                            limit=2)
    check("the list is capped without losing the worst",
          len(both["items"]) == 2 and both["items"][0]["severity"] == "error",
          str([(i["severity"], i["title"]) for i in both["items"]]))
    dupes = trends.attention(health={"checks": [
        {"key": "a", "title": "Same thing", "state": "warn", "detail": "1", "fix": "f"},
        {"key": "b", "title": "Same thing", "state": "warn", "detail": "2", "fix": "f"}]})
    check("the same fault reported twice is read once", len(dupes["items"]) == 1,
          str(dupes["items"]))


def test_link_hygiene():
    """The brief must be able to say "this hop proves nothing" cheaply.

    Classification is ops/category_report.link_hygiene() - the same url_money()
    the money report uses - so this test pins the wrapper's two jobs: it never
    disagrees with the report, and it does not rescan 5,000 rows every five
    seconds.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "category_report", Path(__file__).with_name("ops") / "category_report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)

    tmp = Path(tempfile.mkdtemp(prefix="hygiene-"))
    path = tmp / "busy.sqlite3"
    build_busy_db(path, time.time())
    conn = sqlite3.connect(path)
    tags = frozenset({"mama086-21"})

    direct = report.link_hygiene(conn, tags, limit=5)
    check("hygiene counts the tokenless hops the report cannot name",
          direct["unproven_hops"] == 3,
          str(direct.get("unproven_hops")))
    check("hygiene agrees with the money report on every shared bucket",
          all(direct[k] == report.link_conversion(conn, tags, limit=1)["totals"][k]
              for k in ("cached", "owned", "foreign_tag", "earns_nothing", "unattributed")),
          str({k: direct[k] for k in ("cached", "owned", "earns_nothing")}))
    check("hygiene names the example links, with their destination host",
          direct["hop_examples"] and direct["hop_examples"][0]["host"] == "is.gd"
          and direct["hop_examples"][0]["resolved_host"] == "www.amazon.in",
          str(direct["hop_examples"])[:200])
    check("a bitli.in hop is not called unprovable - it is our shortener",
          all(row["host"] != "bitli.in" for row in direct["hop_examples"]))

    calls = {"n": 0}

    def reader(c, our_tags, limit=3):
        calls["n"] += 1
        return report.link_hygiene(c, our_tags, limit=limit)

    first = trends.link_hygiene(conn, reader, tags)
    second = trends.link_hygiene(conn, reader, tags)
    check("the brief's reader is called once for an unchanged database",
          calls["n"] == 1, str(calls["n"]))
    check("the memoised answer is marked as a memo, with its age",
          second["memo"] is True and second["checked_age_s"] >= 0,
          str({k: second.get(k) for k in ("memo", "checked_age_s")}))
    check("the memoised answer is the same answer",
          {k: second[k] for k in direct} == {k: first[k] for k in direct})
    conn.execute("INSERT INTO link_cache VALUES ('https://s/new', 'https://is.gd/new', "
                 "'https://www.amazon.in/dp/Z', 'kz', ?)", (time.time(),))
    conn.commit()
    third = trends.link_hygiene(conn, reader, tags)
    check("a changed database is never served from the memo",
          calls["n"] == 2 and third["memo"] is False and third["unproven_hops"] == 4,
          f"calls={calls['n']} hops={third.get('unproven_hops')}")
    trends._HYGIENE_MEMO.clear()          # a fresh database, not a memo hit
    check("a reader that raises leaves the brief standing, not broken",
          trends.link_hygiene(conn, lambda *a, **k: 1 / 0, tags)["available"] is False)
    conn.close()


def test_degenerate_states():
    """A dashboard that lies is worse than one that is down.

    Five ways a real server gets into a state the happy-path tests never see:
    no database at all, a zero-byte file, an old schema, a corrupt file, and a
    channels.json that is not JSON. Every endpoint must answer with a sentence,
    never an HTML 500 (that is what `/api/recent`, `/api/audit`, `/api/categories`
    and `/api/safety` used to do on a corrupt file) and never a confident empty
    answer that hides the fault.
    """
    print("\n== degenerate states: no db / empty / old schema / corrupt / bad config ==")
    # The bot module builds a Store at import time, so it must be imported
    # before any probe points BOT_DB_PATH somewhere else.
    import importlib
    importlib.import_module("main_bot_new")

    async def probe(db: Path, channels: Path):
        saved = {key: os.environ.get(key) for key in
                 ("BOT_DB_PATH", "CHANNELS_JSON_PATH", "DASHBOARD_TOKEN", "BOT_LOG")}
        os.environ.update({"BOT_DB_PATH": str(db),
                           "CHANNELS_JSON_PATH": str(channels),
                           "DASHBOARD_TOKEN": "test-token-123",
                           "BOT_LOG": str(db.parent / "missing.log")})
        runner, base = await dash_started()
        out = {}
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20)) as sess:
                for path in ("/api/status", "/api/channels", "/api/recent?limit=5",
                             "/api/audit?limit=20",
                             "/api/categories?hours=24", "/api/safety?days=7",
                             "/api/health", "/api/pulse?hours=6", "/api/trace?queue_id=1",
                             "/api/search?q=ab", "/api/scorecard?days=7",
                             "/api/export?what=health", "/api/export?what=pulse",
                             "/api/link-conversion?limit=5", "/api/channel-pipe?target=A&source=B",
                             "/api/trends?hours=24", "/api/brief", "/api/brief?services=1"):
                    async with sess.get(base + path,
                                        headers={"X-Dashboard-Token": "test-token-123"}) as resp:
                        text = await resp.text()
                        out[path] = (resp.status, text)
        finally:
            await runner.cleanup()
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return out

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        absent = tdp / "absent"
        cases = {}

        cases["no database"] = asyncio.run(
            probe(absent / "x.sqlite3", absent / "channels.json"))

        empty = tdp / "empty.sqlite3"
        empty.write_bytes(b"")
        cases["zero-byte file"] = asyncio.run(probe(empty, tdp / "c1.json"))

        old = tdp / "old.sqlite3"
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE queue(id INTEGER PRIMARY KEY, chat_id INT, msg_id INT, "
                     "source TEXT, status TEXT, attempts INT, created_at REAL, "
                     "rendered_text TEXT, targets_json TEXT)")
        conn.execute("INSERT INTO queue VALUES(1,-1,1,'deals','done',1,?,?, '[]')",
                     (time.time(), "hi https://bit.ly/x"))
        conn.commit()
        conn.close()
        cases["old schema"] = asyncio.run(probe(old, tdp / "c2.json"))

        corrupt = tdp / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a database" * 400)
        cases["corrupt file"] = asyncio.run(probe(corrupt, tdp / "c3.json"))

        real = tdp / "real.sqlite3"
        store = bot.Store(real)
        store.conn.commit()
        store.conn.close()
        junk = tdp / "junk.json"
        junk.write_text("{not json")
        cases["garbage channels.json"] = asyncio.run(probe(real, junk))

        for name, responses in cases.items():
            codes = {path: status for path, (status, _) in responses.items()}
            check(f"{name}: no endpoint 500s",
                  all(status < 500 or status == 503 for status in codes.values()),
                  str({p: c for p, c in codes.items() if c >= 500 and c != 503}))
            check(f"{name}: every failure is JSON with a sentence",
                  all(not text.startswith("<!") and (status == 200 or '"error"' in text)
                      for status, text in responses.values()),
                  str([p for p, (c, t) in responses.items()
                       if c != 200 and ('"error"' not in t or t.startswith("<!"))]))

        # the fault is named, not swallowed
        status, text = cases["corrupt file"]["/api/safety?days=7"]
        check("a corrupt database is named in the answer",
              status == 503 and "not a SQLite database" in text, text[:160])
        status, text = cases["corrupt file"]["/api/categories?hours=24"]
        check("a corrupt database cannot blank the category panel silently",
              status == 503, f"{status} {text[:120]}")
        status, text = cases["no database"]["/api/health"]
        check("health calls a missing database a fault, not a clean bill",
              status == 200 and '"overall": "fail"' in text, text[:160])
        status, text = cases["no database"]["/api/audit?limit=20"]
        check("a missing database is 'come back later' (503), not 404", status == 503,
              f"{status} {text[:120]}")
        status, text = cases["zero-byte file"]["/api/trace?queue_id=1"]
        check("an empty database says it has no queue table yet",
              status == 503 and "no queue table yet" in text, f"{status} {text[:160]}")
        status, text = cases["corrupt file"]["/api/trace?queue_id=1"]
        check("trace on a corrupt file explains the file, not the id",
              status == 503 and "not a SQLite database" in text, f"{status} {text[:160]}")
        status, text = cases["old schema"]["/api/trace?queue_id=1"]
        check("trace still works on an old schema missing last_error",
              status == 200 and '"queue_id": 1' in text, f"{status} {text[:160]}")
        status, text = cases["no database"]["/api/trends?hours=24"]
        check("trends without a database says there is nothing to compare",
              status == 200 and "no database yet" in text, f"{status} {text[:140]}")
        status, text = cases["corrupt file"]["/api/trends?hours=24"]
        check("trends on a corrupt file is a sentence, never a crash",
              status == 200 and '"verdict"' in text or status == 503, f"{status} {text[:140]}")
        status, text = cases["corrupt file"]["/api/brief"]
        check("the brief still ranks what it can on a corrupt file",
              status in (200, 503) and not text.startswith("<!"), f"{status} {text[:140]}")
        status, text = cases["garbage channels.json"]["/api/channels"]
        check("a garbage channels.json is tolerated, not fatal",
              status == 200 and '"whatsapp"' in text, f"{status} {text[:120]}")


def test_http():
    print("\n== HTTP: health / pulse / trace / search / scorecard ==")

    async def run():
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            db = tdp / "ops.sqlite3"
            now = time.time()
            build_db(db, now)
            # One published tokenless hop over an untagged page - the shape of the
            # two is.gd links reported on 2026-10-21.
            with sqlite3.connect(db) as hop_conn:
                hop_conn.execute(
                    "INSERT INTO link_cache VALUES(?,?,?,?,?)",
                    ("https://s/isgd", "https://is.gd/c3Gl9d",
                     "https://www.amazon.in/dp/B0HNPB6QBS", "ASIN:B0HNPB6QBS", now - 50))
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

                    # ---- trends + brief: the two new panels -----------------
                    status, body = await call("/api/trends?hours=24", token=False)
                    check("trends requires the token", status == 401, str(status))
                    status, body = await call("/api/trends?hours=24")
                    check("trends answers 200", status == 200, str(status))
                    check("trends compares this window with the one before",
                          all({"current", "previous", "delta", "sentiment"} <= set(m)
                              for m in body.get("metrics", [])), str(body.get("metrics"))[:200])
                    check("trends names the window in words",
                          body.get("window_label") == "24h", str(body.get("window_label")))
                    check("trends splits per channel and per skip reason",
                          isinstance(body.get("channels"), list)
                          and isinstance(body.get("skip_reasons"), list))
                    check("trends carries a one-sentence verdict",
                          str(body.get("verdict", "")).endswith("."), body.get("verdict"))
                    status, body = await call("/api/trends?hours=nonsense&buckets=nonsense")
                    check("a nonsense window falls back to 24h instead of 500",
                          status == 200 and body.get("window_label") == "24h", str(status))
                    status, body = await call("/api/trends?hours=99999")
                    check("a huge window is clamped, not attempted",
                          status == 200 and body.get("window_hours") == 168.0,
                          str(body.get("window_hours")))

                    status, body = await call("/api/brief", token=False)
                    check("the brief requires the token", status == 401, str(status))
                    status, body = await call("/api/brief")
                    check("the brief answers 200", status == 200, str(status))
                    check("the brief carries the health it used, once",
                          body.get("health", {}).get("overall") in ("ok", "warn", "fail", "unknown"),
                          str(body.get("health", {}).get("overall")))
                    check("the brief says whether anything needs a human",
                          isinstance(body.get("healthy"), bool) and "headline" in body,
                          str(body.get("headline")))
                    check("every item in the brief names its panel and its fix",
                          all(item.get("anchor") for item in body.get("items", [])),
                          str(body.get("items"))[:200])
                    check("the brief reports the installed and running rules version",
                          "installed" in body.get("rules", {}) and "running" in body.get("rules", {}),
                          str(body.get("rules")))
                    check("the brief counts published hops nobody can prove are ours",
                          body.get("links", {}).get("unproven_hops") == 1,
                          str({k: body.get("links", {}).get(k)
                               for k in ("unproven_hops", "cached", "available")}))
                    check("...and ranks them as work for a human",
                          any("cannot prove" in item["title"] for item in body.get("items", [])),
                          str([i["title"] for i in body.get("items", [])])[:300])
                    status, conv = await call("/api/link-conversion?limit=3")
                    check("the conversion card carries the same hygiene figures",
                          status == 200
                          and conv.get("hygiene", {}).get("unproven_hops") == 1,
                          str(conv.get("hygiene", {}).get("unproven_hops")))
                    check("...so the brief and the card can never disagree",
                          conv["hygiene"]["cached"] == body["links"]["cached"]
                          and conv["hygiene"]["owned"] == body["links"]["owned"],
                          f"{conv['hygiene']['cached']} vs {body['links']['cached']}")
                    status, body = await call("/api/brief?services=1")
                    check("the brief can ask systemd too", status == 200, str(status))

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

                    # ---- the server is not a one-request-at-a-time machine ---
                    # Every read-only panel queries SQLite synchronously. Before
                    # `in_thread`, eight concurrent requests took as long as eight
                    # sequential ones and the whole dashboard froze during a
                    # refresh. Two concurrent slow scorecards must overlap.
                    import time as _time
                    real_scorecard = dash.insight.scorecard
                    dash.insight.scorecard = lambda *a, **k: (_time.sleep(0.6), real_scorecard(*a, **k))[1]
                    try:
                        async def slow_scorecard():
                            t0 = _time.time()
                            await call("/api/scorecard?days=7")
                            return _time.time() - t0
                        started = _time.time()
                        await asyncio.gather(slow_scorecard(), slow_scorecard())
                        concurrent = _time.time() - started
                    finally:
                        dash.insight.scorecard = real_scorecard
                    check("two slow panels run at the same time, not one after the other",
                          concurrent < 1.05, f"{concurrent:.2f}s (serial would be ~1.2s)")

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
    test_counts_and_trends()
    test_attention()
    test_link_hygiene()
    test_degenerate_states()
    test_http()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: " + "; ".join(FAILS[:6]))
        return 1
    print("\nops insight PASS: health, timeline with exact counts, trace, search, "
          "scorecard, window-over-window trends, the ranked attention list, "
          "engine and HTTP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
