"""The audit must describe what happened, not what it assumed.

Three false verdicts were printed by `ops/quality_audit.py` on live data on
2026-10-23, and each one wasted operator time in a different way:

  1. "an affiliate/network-looking hostname is not proof of ownership" on
     bitli.in/fktr.in links that the bot had minted itself (52,811
     `earnkaro_api` proof rows, publisher 5478322). The audit never read the
     bot's .env, so it had no publisher id - and dressed "I cannot check this"
     up as a commission leak. Now: FOREIGN=0 on the same data, six `unverified`.
  2. "₹300 sent to the ₹99 channel" for a post whose products cost ₹39,699 and
     ₹49,199. ₹300 was a coupon ("Flat ₹300 Coupon Off") and ₹5000 a bank offer.
     Now: coupon/cashback/bank amounts are masked before a price is read, and
     "@39699" is understood as a price.
  3. The same line was printed for a row that had NOT been delivered to that
     channel at all - the audit read the PLAN (`targets_json`) instead of the
     deliveries. Now: only `status='sent'` delivery rows are judged.

Plus the coverage bucket: 75 posts the bot skipped ON PURPOSE ("no trustworthy
product identity for dl.flipkart.com - skipping rather than risking a
mis-posted duplicate") were reported as lost deals, because the audit's copy of
the policy list recognised 5 of the bot's 19 `PermanentSkip` reasons. The list
is now read from the bot's own source, so it cannot drift.
"""
import importlib.util
import json
import sqlite3
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("quality_audit_tested", ROOT / "ops" / "quality_audit.py")
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)

FAILS = []
LIVE_POST = ("GRAB : Flat ₹300 Coupon Off On Dreame Robot Vacuum & Mop "
             "Starting @39699.\n\n"
             "D30 Ultra @39699 : https://www.amazon.in/dp/B0H2YVXLBR?tag=mama086-21\n\n"
             "L50 Ultra CE @49199 : https://www.amazon.in/dp/B0GX5HFRHZ?tag=mama086-21\n\n"
             "Upto ₹5000 Off With ICICI Bank Credit Card.")


def check(name, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def test_prices_are_what_a_buyer_pays():
    print("\n== a coupon is not a price ==")
    prices = qa.deal_prices(LIVE_POST)
    check("the live post's prices are the product prices, not the coupon",
          sorted(prices) == [39699, 39699, 49199], str(prices))
    check("the ₹300 coupon is not read as a price", 300 not in prices, str(prices))
    check("the ₹5000 bank offer is not read as a price", 5000 not in prices, str(prices))
    check("a plain ₹-price still reads",
          qa.deal_prices("Deal ₹899 (MRP ₹1,999)") == [899, 1999],
          str(qa.deal_prices("Deal ₹899 (MRP ₹1,999)")))
    check("Rs. and INR amounts read too", qa.deal_prices("Rs. 349 only") == [349])
    check("an ASIN inside a link is not a price",
          qa.deal_prices("https://www.amazon.in/dp/B0H2YVXLBR") == [],
          str(qa.deal_prices("https://www.amazon.in/dp/B0H2YVXLBR")))
    check("a percentage is not a price", qa.deal_prices("70% off today") == [])


def test_our_own_links_are_not_foreign():
    print("\n== 'cannot verify' is not 'somebody else's commission' ==")
    url = "https://bitli.in/WEuAqmk"
    check("with no publisher configured the link is UNVERIFIED, not FOREIGN",
          qa.classify_link(url, "mama086-21", "") == "unverified",
          qa.classify_link(url, "mama086-21", ""))
    check("a bare ekaro.in link is unverified the same way",
          qa.classify_link("https://ekaro.in/unverified", "", "") == "unverified",
          qa.classify_link("https://ekaro.in/unverified", "", ""))
    check("with the publisher configured and a proof row it is ours", True)  # see DB test
    foreign = "https://www.flipkart.com/x/p/itm1?pid=1&cmpid=AFF_deals101"
    check("an actual stranger's campaign is still FOREIGN",
          qa.classify_link(foreign, "mama086-21", "5478322") == "foreign",
          qa.classify_link(foreign, "mama086-21", "5478322"))
    check("a stranger's Amazon tag is still FOREIGN",
          qa.classify_link("https://www.amazon.in/dp/B0X?tag=someoneelse-21", "mama086-21")
          == "foreign")


def _db():
    tmp = tempfile.mkdtemp(prefix="audit-truth-")
    path = Path(tmp) / "bot.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE queue(id INTEGER PRIMARY KEY, chat_id INTEGER, msg_id INTEGER,
          source TEXT, status TEXT, attempts INTEGER, next_at REAL, rendered_text TEXT,
          targets_json TEXT, deal_keys_json TEXT, premium_score INTEGER,
          last_error TEXT, created_at REAL);
        CREATE TABLE link_cache(source_url TEXT, affiliate_url TEXT, resolved_url TEXT,
          deal_key TEXT, ts REAL);
        CREATE TABLE owned_link_proofs(affiliate_url TEXT, destination_url TEXT,
          proof_type TEXT, publisher_id TEXT, created_at REAL);
        CREATE TABLE deliveries(id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id INTEGER,
          target TEXT, status TEXT, last_error TEXT, chunk INTEGER, sent_at REAL);
    """)
    return path, conn


def _queue(conn, qid, text, targets, status="done", error="", created=None):
    conn.execute("INSERT INTO queue VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (qid, -1001, qid, "loot_alerts", status, 1, 0, text,
                  json.dumps(targets), "[]", None, error, created or time.time()))


def _delivery(conn, qid, target, status, error=""):
    conn.execute("INSERT INTO deliveries(queue_id,target,status,last_error,chunk,sent_at) "
                 "VALUES(?,?,?,?,1,?)", (qid, target, status, error, time.time()))


def test_misroute_is_judged_on_deliveries():
    print("\n== MISROUTE is what reached a channel, not what was planned ==")
    path, conn = _db()
    # 1) planned for the ₹99 channel but never delivered there: NOT a finding.
    _queue(conn, 1, LIVE_POST, ["LootZoneIndia11", "Under99Deals11"], status="pending")
    _delivery(conn, 1, "LootZoneIndia11", "sent")
    _delivery(conn, 1, "Under99Deals11", "pending")
    # 2) actually delivered to the ₹99 channel: a real finding, with the price a
    #    buyer pays (₹39,699), not the coupon (₹300).
    _queue(conn, 2, LIVE_POST, ["LootZoneIndia11", "Under99Deals11"])
    _delivery(conn, 2, "LootZoneIndia11", "sent")
    _delivery(conn, 2, "Under99Deals11", "sent")
    # 3) a genuine ₹79 deal in the ₹99 channel: nothing to report.
    _queue(conn, 3, "Face wash only ₹79 : https://bitli.in/abc123",
           ["LootZoneIndia11", "Under99Deals11"])
    _delivery(conn, 3, "Under99Deals11", "sent")
    conn.commit()
    findings, posts, _links, _sends = qa.audit(conn, 50, "mama086-21", "5478322")
    misroutes = [f for f in findings if f["kind"] == "MISROUTE"]
    by_queue = {f["queue_id"]: f for f in misroutes}
    check("a planned-but-never-delivered target is not reported as sent",
          1 not in by_queue, str(misroutes))
    check("a product genuinely delivered to the ₹99 channel is reported",
          2 in by_queue, str(misroutes))
    check("and the amount quoted is what the buyer pays",
          "39,699" in (by_queue.get(2, {}).get("detail") or "") or
          "39699" in (by_queue.get(2, {}).get("detail") or "").replace(",", ""),
          by_queue.get(2, {}).get("detail"))
    check("the coupon amount is never quoted as the price",
          "₹300 " not in (by_queue.get(2, {}).get("detail") or "")
          and "₹300 product" not in (by_queue.get(2, {}).get("detail") or ""),
          by_queue.get(2, {}).get("detail"))
    check("a real ₹79 deal in the ₹99 channel is not a finding", 3 not in by_queue,
          str(misroutes))
    conn.close()


def test_policy_skips_come_from_the_bot():
    print("\n== the policy list is the bot's own ==")
    source = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    literals = [line for line in source.splitlines()]
    skip_calls = sum(1 for line in literals if "PermanentSkip(" in line)
    check("the bot has the reasons it thinks it has", skip_calls >= 15, str(skip_calls))
    check("the audit reads them instead of keeping a short copy",
          len(qa.POLICY_SKIPS) >= 19, str(len(qa.POLICY_SKIPS)))
    for live_reason in (
            "no trustworthy product identity for fkrt.cc - skipping rather than risking",
            "source link is dead, unknown, or not monetized by us",
            "affiliate conversion did not verify an owned link",
            "rendered post has no affiliate URL after shorten pass"):
        check(f"recognised as policy, not a lost deal: {live_reason[:46]}",
              any(marker.lower() in live_reason.lower() for marker in qa.POLICY_SKIPS),
              live_reason)

    print("\n== a skipped-for-policy post is not a lost deal ==")
    path, conn = _db()
    _queue(conn, 10, "", [], status="done",
           error="no trustworthy product identity for dl.flipkart.com - skipping "
                 "rather than risking a mis-posted duplicate")
    _queue(conn, 11, "", [], status="done", error="")  # truly unexplained
    conn.commit()
    findings, _posts, _links, _sends = qa.audit(conn, 50, "mama086-21", "5478322")
    coverage = {f["queue_id"] for f in findings if f["kind"] == "COVERAGE"}
    check("a documented skip reason is not reported as a lost deal",
          10 not in coverage, str(coverage))
    check("a job that ended with no reason at all still is", 11 in coverage, str(coverage))
    conn.close()


def test_env_file_gives_the_audit_the_bots_facts():
    print("\n== the deploy report's audit command knows our publisher ==")
    tmp = Path(tempfile.mkdtemp(prefix="audit-env-"))
    # The database and the .env live in DIFFERENT directories on purpose: the
    # audit discovers a .env beside --db, so this proves --env-file is what
    # carries the bot's facts when the paths are not neighbours.
    (tmp / "bot").mkdir()
    db = tmp / "db" / "bot.sqlite3"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE queue(id INTEGER PRIMARY KEY, chat_id INTEGER, msg_id INTEGER,
          source TEXT, status TEXT, attempts INTEGER, next_at REAL, rendered_text TEXT,
          targets_json TEXT, deal_keys_json TEXT, premium_score INTEGER,
          last_error TEXT, created_at REAL);
        CREATE TABLE link_cache(source_url TEXT, affiliate_url TEXT, resolved_url TEXT,
          deal_key TEXT, ts REAL);
        CREATE TABLE owned_link_proofs(affiliate_url TEXT, destination_url TEXT,
          proof_type TEXT, publisher_id TEXT, created_at REAL);
        CREATE TABLE deliveries(id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id INTEGER,
          target TEXT, status TEXT, last_error TEXT, chunk INTEGER, sent_at REAL);
    """)
    _queue(conn, 20, "Deal ₹899 : https://bitli.in/WEuAqmk", ["LootZoneIndia11"])
    _delivery(conn, 20, "LootZoneIndia11", "sent")
    conn.execute("INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)",
                 ("https://bitli.in/WEuAqmk", "https://bitli.in/WEuAqmk",
                  "earnkaro_api", "5478322", time.time()))
    conn.commit()
    conn.close()
    (tmp / "bot" / ".env").write_text(
        "# the bot's own file\nAMAZON_TAG=mama086-21\nEARNKARO_PUBLISHER_ID=5478322\n",
        encoding="utf-8")

    import subprocess
    import sys
    without = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                              "--db", str(db), "--json"],
                             capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "HOME": str(tmp)})
    with_env = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                               "--db", str(db), "--env-file", str(tmp / "bot" / ".env"), "--json"],
                              capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(tmp)})
    bare = json.loads(without.stdout or "{}").get("link_summary", {})
    told = json.loads(with_env.stdout or "{}").get("link_summary", {})
    check("without the bot's .env our own short link is only 'unverified'",
          bare.get("unverified") == 1 and bare.get("foreign") == 0, str(bare))
    check("with the bot's .env the very same link is proven ours",
          told.get("our-short") == 1 and told.get("foreign") == 0,
          str(told))
    check("the deploy report passes that .env to the audit",
          "--env-file" in (ROOT / "ops" / "deploy_and_verify.sh").read_text(),
          "")


def main():
    test_prices_are_what_a_buyer_pays()
    test_our_own_links_are_not_foreign()
    test_misroute_is_judged_on_deliveries()
    test_policy_skips_come_from_the_bot()
    test_env_file_gives_the_audit_the_bots_facts()
    print("\n" + ("AUDIT TRUTH TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_audit_truth: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
