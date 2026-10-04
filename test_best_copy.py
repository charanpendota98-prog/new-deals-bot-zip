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
os.environ.setdefault("AMAZON_TAG", "")
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
        f"https://www.amazon.in/dp/{ASIN}\n"
        f"MRP {R}599, 45% off, limited period")
STRONG = (f"Cooking Oil 5L jar ({R}199)\n"
          f"https://www.amazon.in/dp/{ASIN}\n"
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
                f"https://bitli.in/TqmFyPp/AbC123")
    clean, notes = bot.enforce_numeric_fidelity(source, rendered)
    check("a real price is never deleted because a token was glued to it",
          f"{R}22,990" in clean, clean)
    check("the source's own MRP and discount stay",
          f"{R}36,990" in clean and "38%" in clean.replace(" ", ""), clean)
    check("a price the source never printed is dropped", "9,999" not in clean, clean)
    check("digits inside our link are untouched (masked, not judged)",
          "AbC123" in clean and "tag=" not in clean, clean)
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
    tag = ""
    ours = [
        "https://bitli.in/TqmFyPp/AbC1",
        "https://www.bitlyskj.com/zz",
        "https://www.amazon.in/dp/B0TAGGED001",
        "https://www.amazon.in/dp/B0TAGGED002",
        "https://www.myntra.com/x/1/detail",              # unmonetizable store, clean page
        "https://www.flipkart.com/pride/p/itcx?pids=flipkart_karos_offers_sphome.7264141.p1",
        "https://t.me/addlist/abcDEF",                      # our Loots Family folder
        "https://d7zd1k.earnkaro.com/s/click-link/xyz",
        "https://www.amazon.in/dp/B0NOTAGGED01",            # TAGLESS: clean is ours
    ]
    theirs = [
        "https://amzn.to/sourceShort",                      # the source's own link
        "https://www.amazon.in/dp/B0X?tag=competitor",
        "https://www.amazon.in/dp/B0X?tag=deals0911-21",   # TAGLESS: ex-our tag is foreign
        "https://bitly.com/sponsorOnly",
        "https://www.flipkart.com/buy/p?affid=otherpub",
        "https://www.amazon.in/dp/B0X?tag=deals0911-21&affid=zz",
        "https://www.amazon.in/dp/B0TAGGED001?tag=deals0911-21", # TAGLESS: any tag is foreign
    ]
    for url in ours:
        check(f"accepted as ours: {url[:44]}", qa.why_not_our_link(url, tag) is None,
              str(qa.why_not_our_link(url, tag)))
    for url in theirs:
        check(f"refused as not ours: {url[:44]}", qa.why_not_our_link(url, tag) is not None)


def test_link_ownership_is_data_backed():
    """OUR link is decided from the bot's OWN data, not from a domain list.

    The auditor used to say "bit.ly is a third-party shortener" - which, since
    the 2026-09-06 policy (every list post carries OUR Bitly links), means it
    flagged our own healthy posts and `--strict` cried wolf. A false alarm is how
    a real one gets ignored, so ownership is now read from the table the
    pipeline's provenance gate reads (link_cache) plus the token's publisher id.
    Pinned here in both directions: ours accepted, foreign still refused.
    """
    print("\n== link ownership: our runtime links are ours, strangers' are not ==")
    import importlib.util
    spec = importlib.util.spec_from_file_location("quality_audit_links", ROOT / "ops" / "quality_audit.py")
    qa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qa)

    ours_bitly = "https://bit.ly/3xYzAbC"
    ours_isgd = "https://is.gd/Mint7"
    foreign_bitly = "https://bit.ly/sponsorOnly"
    minted = {ours_bitly, ours_isgd}

    check("our own Bitly link is ours when the cache minted it",
          qa.why_not_our_link(ours_bitly, "", "", minted) is None,
          str(qa.why_not_our_link(ours_bitly, "", "", minted)))
    check("our is.gd fallback link is ours the same way",
          qa.why_not_our_link(ours_isgd, "", "", minted) is None,
          str(qa.why_not_our_link(ours_isgd, "", "", minted)))
    check("somebody else's bit.ly link is still refused",
          qa.why_not_our_link(foreign_bitly, "", "", minted) is not None,
          str(qa.why_not_our_link(foreign_bitly, "", "", minted)))
    check("a short link with no cache row explains WHY it is suspect",
          "no link_cache row" in (qa.why_not_our_link(foreign_bitly, "", "", minted) or ""),
          str(qa.why_not_our_link(foreign_bitly, "", "", minted)))
    check("a host we never mint on keeps the plain third-party verdict",
          qa.why_not_our_link("https://tinyurl.com/x", "", "", minted) ==
          "a third-party shortener leaked into the post",
          str(qa.why_not_our_link("https://tinyurl.com/x", "", "", minted)))

    publisher = "5478322"
    direct_ours = (f"https://www.flipkart.com/x/p/itm1?pid=1&affExtParam2={publisher}")
    direct_theirs = "https://www.flipkart.com/x/p/itm1?pid=1&affExtParam2=999999"
    check("a direct Flipkart link carrying OUR publisher id is ours",
          qa.why_not_our_link(direct_ours, "", publisher) is None,
          str(qa.why_not_our_link(direct_ours, "", publisher)))
    check("the same link is suspect when no publisher id is configured",
          qa.why_not_our_link(direct_ours, "", "") is not None, "")
    check("somebody else's publisher id is still refused",
          qa.why_not_our_link(direct_theirs, "", publisher) is not None, "")
    check("a foreign id glued to our tag is still refused",
          qa.why_not_our_link("https://www.amazon.in/dp/B0X?tag=mama086-21&affid=zz",
                              "mama086-21", publisher) is not None, "")
    # USER REPORT 2026-10-04: the converted Shopsy link carried BOTH our
    # affExtParam2 and another channel's campaign id `cmpid=AFF_deals101`. With
    # both present it is OURS (we are paid), but a `cmpid=AFF_*` with no id of
    # ours names somebody else - the audit must see that on its own, not only
    # when an `affid` happens to sit next to it.
    shopsy_shape = ("https://www.shopsy.in/x/p/itm8ad37c08bc9ac?pid=1&mcn=LEHLAH"
                    "&affid=deals101&cmpid=AFF_deals101"
                    f"&affExtParam1=ENKR20261004A2196972183&affExtParam2={publisher}")
    check("a link with OUR id and another channel's cmpid is still ours",
          qa.why_not_our_link(shopsy_shape, "", publisher) is None,
          str(qa.why_not_our_link(shopsy_shape, "", publisher)))
    check("a link naming only a stranger's cmpid campaign is foreign",
          qa.classify_link("https://www.flipkart.com/x/p/itm1?pid=1&cmpid=AFF_deals101",
                           "", publisher) == "foreign",
          qa.classify_link("https://www.flipkart.com/x/p/itm1?pid=1&cmpid=AFF_deals101",
                           "", publisher))

    check("classify_link buckets our runtime short link as our-short",
          qa.classify_link(ours_bitly, "", "", minted) == "our-short",
          qa.classify_link(ours_bitly, "", "", minted))
    check("classify_link buckets our publisher id as our-publisher",
          qa.classify_link(direct_ours, "", publisher) == "our-publisher",
          qa.classify_link(direct_ours, "", publisher))
    check("classify_link buckets a network/HYPD short link as our-short, not clean-merchant",
          qa.classify_link("https://ekaro.in/enkr123") == "our-short"
          and qa.classify_link("https://hypd.store/93944/afflink/daoll7ltm6mc5h7k1fq0") == "our-short",
          f"{qa.classify_link('https://ekaro.in/enkr123')} / "
          f"{qa.classify_link('https://hypd.store/93944/afflink/daoll7ltm6mc5h7k1fq0')}")
    check("classify_link buckets a clean unmonetizable store page as clean-merchant",
          qa.classify_link("https://www.myntra.com/x/1/detail") == "clean-merchant",
          qa.classify_link("https://www.myntra.com/x/1/detail"))
    check("classify_link buckets a stranger's tag as foreign",
          qa.classify_link("https://www.ajio.com/p/9?tag=rivalpub", "mama086-21") == "foreign",
          qa.classify_link("https://www.ajio.com/p/9?tag=rivalpub", "mama086-21"))
    # USER REPORT 2026-10-04 ("product opens but it is not our link ... commission
    # never comes"): a Flipkart/Myntra page posted WITHOUT our publisher id can
    # pay us (the network's affExtParam2 works on the page itself), so it is not
    # a "clean merchant page kept on purpose" - it is a finding, counted apart.
    check("a Flipkart-family page with no publisher id is reported as unattributed",
          qa.why_not_our_link("https://www.myntra.com/x/1/detail", "", publisher) is not None
          and "NOBODY is paid" in (qa.why_not_our_link("https://www.myntra.com/x/1/detail",
                                                       "", publisher) or ""),
          str(qa.why_not_our_link("https://www.myntra.com/x/1/detail", "", publisher)))
    check("classify_link gives it its own bucket, not 'clean-merchant'",
          qa.classify_link("https://www.myntra.com/x/1/detail", "", publisher) == "unattributed",
          qa.classify_link("https://www.myntra.com/x/1/detail", "", publisher))
    check("the same page WITH our publisher id is ours, and stays that way",
          qa.classify_link(f"https://www.myntra.com/x/1/detail?affExtParam2={publisher}",
                           "", publisher) == "our-publisher")
    check("a store with no attribution contract is still a clean merchant page",
          qa.classify_link("https://www.ajio.com/p/9", "", publisher) == "clean-merchant",
          qa.classify_link("https://www.ajio.com/p/9", "", publisher))

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "links.sqlite3"
        conn = __import__("sqlite3").connect(db)
        conn.executescript(
            "CREATE TABLE queue(id INTEGER PRIMARY KEY, chat_id INTEGER, msg_id INTEGER,"
            " source TEXT, status TEXT, attempts INTEGER, last_error TEXT, created_at REAL,"
            " chat_key TEXT, rendered_text TEXT, targets_json TEXT);"
            "CREATE TABLE deliveries(queue_id INTEGER, target TEXT, status TEXT);"
            "CREATE TABLE link_cache(source_url TEXT PRIMARY KEY, affiliate_url TEXT NOT NULL,"
            " resolved_url TEXT, deal_key TEXT, created_at REAL NOT NULL);")
        now = time.time()
        good_post = (f"boAt Airdopes 141 TWS Earbuds\n{R}1,099 (78% off)\n{ours_bitly}\n"
                     f"https://www.myntra.com/x/1/detail")
        conn.execute("INSERT INTO queue VALUES(1,-1001,11,'lootnow','done',0,'',?,'k',?,?)",
                     (now, good_post, json.dumps(["LootZoneIndia11"])))
        conn.execute("INSERT INTO deliveries VALUES(1,'LootZoneIndia11','sent')")
        conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)",
                     ("https://www.flipkart.com/x/p/itm1", ours_bitly,
                      "https://www.flipkart.com/x/p/itm1", "k", now))
        conn.commit()
        conn.close()

        out = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                              "--db", str(db), "--json"], capture_output=True, text=True)
        report = json.loads(out.stdout)
        summary = report["link_summary"]
        check("the audit reports a link-perfection summary from data",
              summary["links"] == 2 and summary["our-short"] == 1
              and summary["clean-merchant"] == 1 and summary["foreign"] == 0, str(summary))
        check("our own Bitly link produces NO finding any more",
              not [f for f in report["findings"] if f["kind"] == "LINKS"],
              str([f for f in report["findings"] if f["kind"] == "LINKS"]))
        strict = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                                 "--db", str(db), "--strict"], capture_output=True, text=True)
        check("--strict no longer fails a healthy post that carries OUR short link",
              strict.returncode == 0, strict.stdout[-300:])
        human = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                                "--db", str(db)], capture_output=True, text=True)
        check("the human output answers the question in one line",
              "LINK PERFECTION | links=2 our-short=1" in human.stdout, human.stdout[-400:])

        # The 2026-10-04 commission bug, proven from data: a bare Flipkart-family
        # page opens for the reader and pays nobody. With the publisher id known,
        # the audit must say so (and --strict must fail) instead of filing it as a
        # harmless "clean merchant" page.
        conn = __import__("sqlite3").connect(db)
        conn.execute("INSERT INTO queue VALUES(2,-1002,12,'lootnow','done',0,'',?,'k',?,?)",
                     (now, "Kurta Set\n" + R + "799 (60% off)\n"
                      "https://www.myntra.com/x/2/detail", json.dumps(["LootZoneIndia11"])))
        conn.execute("INSERT INTO deliveries VALUES(2,'LootZoneIndia11','sent')")
        conn.commit()
        conn.close()
        strict = subprocess.run([sys.executable, str(ROOT / "ops" / "quality_audit.py"),
                                 "--db", str(db), "--publisher", publisher, "--strict"],
                                capture_output=True, text=True)
        check("with a publisher id known, a link that pays nobody fails --strict",
              strict.returncode == 1 and "NOBODY is paid" in strict.stdout, strict.stdout[-500:])
        check("and the summary counts them in their own bucket (both bare Myntra links)",
              "unattributed=2" in strict.stdout, strict.stdout[:400])


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
                      f"https://www.amazon.in/dp/{ASIN}")
        put(1, "lootnow", 101, clean_post, ["LootZoneIndia11"], extra_ids=[f"amazon:{ASIN}"])
        put(2, "lootnow", 102, clean_post, ["LootZoneIndia11"], extra_ids=[f"amazon:{ASIN}"])
        put(3, "under499loots", 103,
            f"{R}640\n**Deal**\nhttps://www.amazon.in/dp/B0FOREIGN01\n✅ Verified deals • Enjoy Grab fast",
            ["Under499Deals11"])
        put(4, "lootnow", 104, f"Cool thing {R}1,999\nhttps://bitly.com/sponsorOnly\n"
                              f"https://www.amazon.in/dp/B0NOTAGGED1?tag=deals0911-21\n"
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
    test_link_ownership_is_data_backed()
    test_best_copy_swap()
    test_numeric_fidelity_gate()
    test_quality_auditor()
    print("\n" + ("BEST-COPY/FIDELITY/AUDIT TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_best_copy: all checks PASS"))
    sys.exit(1 if FAILS else 0)
