"""v17.6/v17.7 tests: best-copy selection, numeric fidelity gate, quality auditor.

Three guarantees this file pins down:
  1. BEST COPY - when several sources post the SAME product (exact merchant id),
     the queue publishes the better deal, still as ONE row (no duplicate), and
     the displaced copy is restored if the better one cannot be posted.
  2. NUMERIC FIDELITY - a price or a discount the source never printed is a bug;
     it is removed from the post, and the post is never dropped because of it.
  3. QUALITY AUDITOR - ops/quality_audit.py catches invented text, foreign links,
     misrouted price tiers and lost posts on the live database (read-only).

Run: python3 test_best_copy.py   (from the repo root)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
os.environ.setdefault("AMAZON_TAG", "deals0911-21")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
import main_bot_new as bot  # noqa: E402

R = "\u20b9"
FAILS = []


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  <- {extra[:200]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


ASIN = "B0TESTASIN1"
WEAK = (f"Cooking Oil 5L jar ({R}214)\n"
        f"https://www.amazon.in/dp/{ASIN}?tag=deals0911-21\n"
        f"MRP {R}599, 45% off, limited period")
STRONG = (f"Cooking Oil 5L jar ({R}199)\n"
          f"https://www.amazon.in/dp/{ASIN}?tag=deals0911-21\n"
          f"MRP {R}999, 80% off, today alone")
LISTY = (f"1. Oil jar {R}214 https://www.amazon.in/dp/{ASIN}\n"
         f"2. Soap 500ml {R}49 https://www.amazon.in/dp/OTHERPID12")


def test_product_identity_matching():
    print("\n== product identity: exact ids only ==")
    ids = bot.product_identities(WEAK)
    check("an /dp/ link yields the merchant product id", ids == [ASIN], str(ids))
    check("a shortener-only post yields nothing (never re-pointed)",
          bot.product_identities("Cool deal " + R + "99 https://bitli.in/TqmFyPp/abc12") == [])
    check("no name-fuzzy matching happens",
          bot.product_identities("Best Oil Jar Ever, 45% off, https://example.com/oil-jar") == [])
    check("render deal keys and intake ids normalise together",
          bot.comparable_product_ids([f"amazon:{ASIN}"]) == bot.comparable_product_ids([ASIN]),
          str(bot.comparable_product_ids([f"amazon:{ASIN}"])))
    check("the stronger copy out-scores the weaker one",
          bot.deal_quality_score(STRONG) > bot.deal_quality_score(WEAK),
          f"{bot.deal_quality_score(STRONG)} vs {bot.deal_quality_score(WEAK)}")


def test_best_copy_swap():
    print("\n== best copy takes over the pending row, still one row ==")

    async def run():
        with tempfile.TemporaryDirectory() as td:
            store = bot.Store(Path(td) / "t.sqlite3")
            # source A queued the weak copy from chat -100900 msg 11
            check("weak copy is queued", await store.enqueue(-100900, 11, "lootnow", WEAK, False))
            row = store.conn.execute("SELECT * FROM queue").fetchone()
            check("the row carries its score and product id",
                  (row["quality"] or 0) > 0 and ASIN in (row["product_keys"] or ""),
                  f"quality={row['quality']} keys={row['product_keys']}")

            # source B posts the SAME product with a better price a minute later
            check("better copy returns False (no second row)",
                  await store.enqueue(-100901, 21, "smartpicks", STRONG, False) is False)
            rows = store.conn.execute("SELECT * FROM queue ORDER BY id").fetchall()
            check("still exactly ONE queue row - no duplicate", len(rows) == 1, str(len(rows)))
            live = rows[0]
            check("that row now points at the better copy",
                  live["msg_id"] == 21 and live["source"] == "smartpicks", f"{live['msg_id']}/{live['source']}")
            check("the better score is recorded", live["quality"] >= bot.deal_quality_score(STRONG),
                  str(live["quality"]))
            check("the displaced copy is remembered with its real chat id",
                  live["superseded_msg_id"] == 11 and str(live["superseded_chat_id"]) == "-100900"
                  and int(live["superseded_chat_id"]) == -100900,
                  f"{live['superseded_chat_id']}/{live['superseded_msg_id']}")

            # an even weaker copy arriving later must not downgrade anything
            WORSE = WEAK.replace("45% off", "5% off").replace(f"{R}214", f"{R}614")
            check("a weaker copy is suppressed, row untouched",
                  await store.swap_in_better_copy(-100902, 31, "thirdsrc", WORSE, 2) is True)
            again = store.conn.execute("SELECT * FROM queue").fetchone()
            check("the row still holds the best copy", again["msg_id"] == 21, str(again["msg_id"]))
            check("one row throughout", len(store.conn.execute("SELECT id FROM queue").fetchall()) == 1)

            # a MULTI-product post is never re-pointed, even with the same id
            check("a list post is queued normally", await store.enqueue(-100903, 41, "lootnow", LISTY, False))
            better_for_list = await store.swap_in_better_copy(-100905, 61, "lootnow", STRONG, 2)
            still_list = store.conn.execute("SELECT * FROM queue WHERE msg_id=41").fetchone()
            check("a pending LIST row is never hijacked by a single-product copy",
                  still_list is not None and still_list["msg_id"] == 41
                  and len(json.loads(still_list["product_keys"] or "[]")) > 1,
                  f"{still_list['msg_id']}/{better_for_list}")
            # a Tricks post is never re-pointed (untouched path, its own row)
            tricks_text = f"GST refund scheme open now, {R}0 fee\nhttps://t.me/gstrefunds/123"
            check("a tricks post is queued normally",
                  await store.enqueue(-100904, 51, "gstrefunds", tricks_text, False))
            tricks_row = store.conn.execute(
                "SELECT * FROM queue WHERE chat_key=?", (str(bot.raw_chat_id(-100904)),)).fetchone()
            check("tricks source keeps its own row and its own msg id",
                  tricks_row is not None and tricks_row["msg_id"] == 51)

            # coverage guard: if the better copy cannot be posted, the original returns
            live = store.conn.execute("SELECT * FROM queue WHERE msg_id=21").fetchone()
            check("restore puts the displaced copy back in the queue",
                  await store.restore_superseded(live) is True)
            back = store.conn.execute(
                "SELECT * FROM queue WHERE chat_key=? AND msg_id=11",
                (str(bot.raw_chat_id(-100900)),)).fetchone()
            check("the original copy is pending again with the chat id the fetcher needs",
                  back is not None and back["status"] == "pending" and back["chat_id"] == -100900,
                  str(dict(back) if back else None))
            live = store.conn.execute("SELECT * FROM queue WHERE msg_id=21").fetchone()
            check("the pointer is cleared so it restores only once",
                  live["superseded_msg_id"] is None)
            check("a second restore does nothing", await store.restore_superseded(live) is False)
            check("still no duplicate row for that product",
                  len(store.conn.execute("SELECT id FROM queue WHERE msg_id IN (11,21)").fetchall()) == 2)
            await store.aclose() if hasattr(store, "aclose") else store.conn.close()

    asyncio.run(run())


def test_numeric_fidelity_gate():
    print("\n== numeric fidelity gate ==")
    source = (f"IFB 6kg Fully Automatic Washing Machine ({R}22,990)\n"
              f"Amazon Link: https://bitli.in/TqmFyPp/AbC123\n"
              f"MRP {R}36,990 | 38% off")
    rendered = (f"IFB 6kg Fully Automatic Washing Machine ({R}22,990tG7o1L5)\n"
                f"MRP {R}36,990 | 38% off | extra {R}9,999 (99% off) code LOOT500\n"
                f"https://bitli.in/TqmFyPp/AbC123?tag=deals0911-21")
    clean, notes = bot.enforce_numeric_fidelity(source, rendered)
    check("a real price is never deleted because a token was glued to it",
          f"{R}22,990" in clean, clean)
    check("the source's own MRP and discount stay",
          f"{R}36,990" in clean and "38%" in clean.replace(" ", ""), clean)
    check("a price the source never printed is dropped", "9,999" not in clean, clean)
    check("digits inside our link are untouched (masked, not judged)",
          "tag=deals0911-21" in clean and "AbC123" in clean, clean)
    check("the coupon code itself is not damaged by the price removal", "LOOT500" in clean, clean)
    check("removals are logged", len(notes) >= 1, str(notes))
    same, notes_same = bot.enforce_numeric_fidelity(source, source)
    check("a faithful post is returned unchanged", same == source, same)
    check("nothing is logged for a faithful post", notes_same == [], str(notes_same))
    # commas must not create a false mismatch
    src2 = f"Item {R}1,999 https://amzn.to/xy1"
    out2, notes2 = bot.enforce_numeric_fidelity(src2, f"Item {R}1999 https://amzn.to/xy1")
    check("1,999 and 1999 are the same number to the gate", notes2 == [] and f"{R}1999" in out2, str(notes2))


def test_link_policy():
    """What counts as OUR link, and what must never leave a channel."""
    print("\n== link ownership policy ==")
    import importlib.util
    spec = importlib.util.spec_from_file_location("quality_audit", ROOT / "ops" / "quality_audit.py")
    qa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qa)
    tag = "deals0911-21"
    ours = [
        "https://bitli.in/TqmFyPp/AbC1",
        "https://www.bitlyskj.com/zz",
        "https://www.amazon.in/dp/B0TAGGED001?tag=deals0911-21",
        "https://www.amazon.in/dp/B0TAGGED002?tag=deals0911-21&linkCode=x",
        "https://www.myntra.com/x/1/detail",              # unmonetizable store, clean page
        "https://www.flipkart.com/pride/p/itcx?pids=flipkart_karos_offers_sphome.7264141.p1",
        "https://t.me/addlist/abcDEF",                      # our Loots Family folder
        "https://d7zd1k.earnkaro.com/s/click-link/xyz",
    ]
    theirs = [
        "https://amzn.to/sourceShort",                      # the source's own link
        "https://www.amazon.in/dp/B0NOTAGGED01",            # ours never goes untagged
        "https://www.amazon.in/dp/B0X?tag=competitor",
        "https://bitly.com/sponsorOnly",
        "https://www.flipkart.com/buy/p?affid=otherpub",
        "https://www.amazon.in/dp/B0X?tag=deals0911-21&affid=zz",
    ]
    for url in ours:
        check(f"accepted as ours: {url[:44]}", qa.why_not_our_link(url, tag) is None,
              str(qa.why_not_our_link(url, tag)))
    for url in theirs:
        check(f"refused as not ours: {url[:44]}", qa.why_not_our_link(url, tag) is not None)


def test_quality_auditor():
    print("\n== ops/quality_audit.py catches every class of defect ==")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "audit.sqlite3"
        store = bot.Store(db)
        now = time.time()

        def put(queue_id: int, source: str, msg_id: int, text: str, targets, status="done",
                error="", sent=1, extra_ids=None):
            store.conn.execute(
                "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
                "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (queue_id, -100900 - queue_id, msg_id, source, status, 0, error, now - queue_id * 60,
                 str(bot.raw_chat_id(-100900 - queue_id)), text, json.dumps(list(targets))))
            for target in (["LootZoneIndia11"] if sent == 0 else targets):
                store.conn.execute("INSERT INTO deliveries(queue_id,target,status) VALUES(?,?,?)",
                                   (queue_id, target, "sent" if sent else "failed"))
            for key in (extra_ids or []):
                store.conn.execute("INSERT OR REPLACE INTO deal_claims(deal_key,queue_id,claimed_at) VALUES(?,?,?)",
                                   (key, queue_id, now))

        clean_post = (f"Prestige 2L Pressure Cooker ({R}899)\n"
                      f"MRP {R}1,999 | 55% off\nhttps://bitli.in/TqmFyPp/Cook99\n"
                      f"https://www.amazon.in/dp/{ASIN}?tag=deals0911-21")
        put(1, "lootnow", 101, clean_post, ["LootZoneIndia11"], extra_ids=[f"amazon:{ASIN}"])
        put(2, "lootnow", 102, clean_post, ["LootZoneIndia11"], extra_ids=[f"amazon:{ASIN}"])
        put(3, "under499loots", 103,
            f"{R}640\n**Deal**\nhttps://www.amazon.in/dp/B0FOREIGN01\n✅ Verified deals • Enjoy Grab fast",
            ["Under499Deals11"])
        put(4, "lootnow", 104, f"Cool thing {R}1,999\nhttps://bitly.com/sponsorOnly\n"
                              f"https://www.amazon.in/dp/B0NOTAGGED1\n"
                              f"https://www.ajio.com/p/900123?tag=rivalpub", ["PowerLoots1"])
        put(5, "lootnow", 105, f"{R}1,999 cooker\nhttps://bitli.in/TqmFyPp/x1", ["Under99Deals11"])
        put(6, "lootnow", 106, "", ["LootZoneIndia11"], status="failed",
            error="RuntimeError: EarnKaro 503 kept retrying", sent=0)
        # the round-10 classes: one figure printed twice inside a single post, a
        # post that was cut instead of deferred, and the same product re-posted to
        # the same channel at a WORSE price
        put(7, "lootnow", 107,
            f"Sony WH-CH720N Wireless Headphones\nNow {R}5,990 (MRP {R}10,990)\n"
            f"Pay {R}5,990 with code SONY10\nhttps://bitli.in/TqmFyPp/Sony7", ["LootZoneIndia11"])
        put(8, "lootnow", 108,
            "Sony WH-CH720N Wireless Headphones with mic and case, battery…",
            ["LootZoneIndia11"])
        tv = f"Samsung 43 Inch Crystal 4K Smart Television\nNow {{price}} (MRP {R}74,900)\n"
        put(10, "lootnow", 110, tv.format(price=f"{R}8,990") + "https://bitli.in/TqmFyPp/Tv10",
            ["LootZoneIndia11"])
        put(11, "lootnow", 111, tv.format(price=f"{R}7,490") + "https://bitli.in/TqmFyPp/Tv11",
            ["LootZoneIndia11"])
        put(9, "lootnow", 109,
            f"Sony WH-CH720N Wireless Headphones\nNow {R}6,490 (MRP {R}10,990)\n"
            f"https://bitli.in/TqmFyPp/Sony9", ["LootZoneIndia11"])
        store.conn.commit()
        store.conn.close()

        out = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                              "--db", str(db), "--limit", "50", "--json"],
                             capture_output=True, text=True)
        check("auditor runs clean on the database", out.returncode == 0, out.stderr[-300:])
        report = json.loads(out.stdout)
        kinds = {}
        for finding in report["findings"]:
            kinds.setdefault(finding["kind"], []).append(finding)
        check("duplicate product across posts is caught", "DUPLICATE" in kinds, str(kinds))
        check("invented text / markdown debris is caught",
              any("OURS" == k for k in kinds) and
              len(kinds.get("OURS", [])) >= 2, str(kinds.get("OURS")))
        link_details = " | ".join(item["detail"] for item in kinds.get("LINKS", []))
        check("a third-party shortener is caught", "shortener" in link_details, link_details)
        check("an Amazon link without our tag is caught", "without our tag" in link_details, link_details)
        check("a link tagged to another publisher is caught",
              "somebody else" in link_details, link_details)
        check("a price-tier misroute is caught", "MISROUTE" in kinds, str(kinds.get("MISROUTE")))
        check("a post cut with an ellipsis is caught", "CUT" in kinds, str(list(kinds)))
        check("a figure repeated inside one single-product post is caught",
              "DOUBLE" in kinds and any("5990" in item["detail"] for item in kinds["DOUBLE"]),
              str(kinds.get("DOUBLE")))
        sony_dupes = [item for item in kinds.get("SAME-PRODUCT", []) if "sony" in item["detail"].lower()]
        check("the same product carried twice by one channel is caught",
              len(sony_dupes) == 1 and "7" in sony_dupes[0]["detail"], str(kinds.get("SAME-PRODUCT")))
        check("a cheaper re-post of the same product is NOT called a duplicate",
              not any("samsung" in item["detail"].lower()
                      for item in kinds.get("SAME-PRODUCT", [])),
              str(kinds.get("SAME-PRODUCT")))
        check("a lost post (failed, no send, no policy reason) is caught",
              "COVERAGE" in kinds, str(kinds.get("COVERAGE")))
        check("the clean post itself is not flagged",
              all(item.get("queue_id") != 1 for item in report["findings"]),
              str([item for item in report["findings"] if item.get("queue_id") == 1]))

        strict = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                                 "--db", str(db), "--strict"], capture_output=True, text=True)
        check("--strict exits non-zero so diagnose can alert", strict.returncode == 1, strict.stdout[-200:])
        human = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"), "--db", str(db)],
                               capture_output=True, text=True)
        check("human output summarises each category",
              all(line in human.stdout for line in ("QUALITY AUDIT", "OURS", "LINKS", "DUPLICATE")),
              human.stdout[-400:])

        empty = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                                "--db", str(Path(td) / "missing.sqlite3")],
                               capture_output=True, text=True)
        check("a missing database is reported, not crashed on",
              empty.returncode == 0 and "no database" in empty.stdout, empty.stdout)


if __name__ == "__main__":
    test_product_identity_matching()
    test_link_policy()
    test_best_copy_swap()
    test_numeric_fidelity_gate()
    test_quality_auditor()
    print("\n" + ("BEST-COPY/FIDELITY/AUDIT TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_best_copy: all checks PASS"))
    sys.exit(1 if FAILS else 0)
