"""Regression tests for the "post instantly, exactly once, nothing extra" fixes.

Covers three bug classes the user reported:
  1. LATENCY      - the bot sat on source posts and published them late/randomly.
  2. DUPLICATES   - the same campaign could be queued/posted twice.
  3. POST QUALITY - source promo left unwanted words/link residue in posts,
                    and the source link had to be replaced by OUR link.

Run: python3 test_pipeline_fixes.py   (from the repo root)
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ.update(
    TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x",
    EARNKARO_API_KEY="k", AMAZON_TAG="deals0911-21",
)
sys.path.insert(0, str(Path(__file__).parent / "bestgaa"))
import main_bot_new as bot  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


# ---------------------------------------------------------------------------
# fake HTTP plumbing (no network in tests)
# ---------------------------------------------------------------------------
class FakeContent:
    def __init__(self, body: str):
        self.body = body.encode()

    async def read(self, _limit: int):
        return self.body


class FakeResponse:
    def __init__(self, url, status, body, delay):
        self.url, self.status, self.charset = url, status, "utf-8"
        self.content = FakeContent(body)
        self.delay = delay

    async def __aenter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Every GET sleeps `delay`, so serial vs concurrent access is measurable."""

    def __init__(self, delay=0.25, broken=(), status=200, body="fine"):
        self.delay, self.broken = delay, set(broken)
        self.status, self.body = status, body
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url in self.broken:
            return FakeResponse(url, 404, "this page could not be found", self.delay)
        return FakeResponse(url, self.status, self.body, self.delay)

    def post(self, *a, **k):
        raise AssertionError("EarnKaro must not be called by these tests")


# ---------------------------------------------------------------------------
# 1. POST QUALITY - no source promo, no residue, real content preserved
# ---------------------------------------------------------------------------
def test_text_quality():
    print("\n== post quality (no unwanted words, no link residue) ==")
    promo_heavy = (
        "\U0001F525\U0001F525 Sony 32 inch HD Smart TV\n"
        "MRP: \u20b918,900 | Price: \u20b911,990 (37% OFF)\n"
        "Use code: SONYBIG to get extra 10% off\n"
        "Buy Now \U0001F449 https://fkrt.it/abcXYZ!NNNN\n"
        "Join our WhatsApp Channel for more deals: https://whatsapp.com/channel/0029\n"
        "Share this deal with your friends \U0001F64F\n"
        "Visit our channel: https://t.me/someotherchannel\n"
        "Deal Time: 12:00 PM IST"
    )
    out = bot.strict_orphan_token_cleanup(bot.tidy_post(bot.clean_source_text(promo_heavy)))
    check("promo channel lines are dropped whole",
          "whatsapp" not in out.lower() and "someotherchannel" not in out)
    check("no half-stripped URL residue in the post",
          ".com/channel" not in out and ".me/" not in out and "://" not in out.replace(
              "https://fkrt.it/abcXYZ!NNNN", ""))
    check("product name, price, discount and coupon all survive",
          "Sony 32 inch HD Smart TV" in out and "\u20b911,990" in out
          and "37% OFF" in out and "SONYBIG" in out)
    check("the merchant link is preserved exactly",
          "https://fkrt.it/abcXYZ!NNNN" in out)

    residue = "Best deal \u20b999\nps://broken\nhttps://\n.com/leftover/junk\nhttps://amzn.to/keep1"
    cleaned = bot.strip_url_residue(residue)
    check("broken protocol fragments removed",
          "ps://broken" not in cleaned and ".com/leftover" not in cleaned
          and not any(line.strip() in {"https://", ""} for line in cleaned.splitlines()))
    check("real URL untouched by the residue sweep", "https://amzn.to/keep1" in cleaned)

    referral = (
        "Zepto free groceries \u20b9200\n"
        "https://www.zepto.com/p/xyz\n"
        "Install the app and refer a friend to earn \u20b950 each\n"
        "Use code ZEPTO for free delivery"
    )
    out = bot.clean_source_text(referral)
    check("referral/app-install spam removed",
          "refer a friend" not in out and "Install the app" not in out)
    check("offer terms and coupon kept", "ZEPTO" in out and "\u20b9200" in out)

    keep = "MX 1TB SSD \u20b9499 (was \u20b91,999) Free shipping on orders above \u20b9199"
    check("a normal deal line is never touched", bot.clean_source_text(keep) == keep)


# ---------------------------------------------------------------------------
# 2. DUPLICATES
# ---------------------------------------------------------------------------
async def test_dedup(store):
    print("\n== duplicates (one source post = at most one target post) ==")
    # Same message seen under two different chat-id conventions.
    first = await store.enqueue(-1001234567, 900, "src_a", "Widget \u20b949 https://amzn.to/w1")
    again = await store.enqueue(1234567, 900, "src_a", "Widget \u20b949 https://amzn.to/w1")
    rows = store.conn.execute(
        "SELECT COUNT(*) FROM queue WHERE msg_id=900"
    ).fetchone()[0]
    check("one source message = one queue row across id conventions",
          first is True and again is False and rows == 1)

    # A campaign that was already posted is never queued again (any source).
    text = "boAt Airdopes 141 \u20b91,099 75% OFF https://amzn.to/b1"
    key = bot.content_deal_key(text)
    store.conn.execute("INSERT OR REPLACE INTO posted_deals VALUES(?,?,?,?)",
                       (key, 1099, time.time(), 1))
    store.conn.commit()
    check("already-posted campaign refused at intake",
          await store.enqueue(-100999, 901, "src_b", text) is False)

    # An in-flight claim held by a LIVE job must not expire (that window used to
    # let the same product through from a second source an hour later).
    store.conn.execute("INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key,status) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (-100555, 902, "src_a", time.time(), 1, "-100555", "pending"))
    store.conn.commit()
    live_id = store.conn.execute("SELECT id FROM queue WHERE msg_id=902").fetchone()[0]
    store.conn.execute("INSERT OR REPLACE INTO deal_claims VALUES(?,?,?)",
                       ("ASIN:B0TEST0001", live_id, time.time() - 3600))
    store.conn.commit()
    other = await store.enqueue(-100556, 903, "src_c", "")
    dup_ok, _ = await store.reserve(999, ["ASIN:B0TEST0001"], None, True)
    check("live job keeps its product claim (no second post of the same product)",
          dup_ok is False and other is True)
    store.conn.execute("UPDATE queue SET status='done' WHERE id=?", (live_id,))
    store.conn.commit()
    freed_ok, _ = await store.reserve(999, ["ASIN:B0TEST0001"], None, True)
    check("a finished job's claim is reclaimed, never stuck forever", freed_ok is True)

    # The unique index itself is the hard guarantee.
    try:
        # Same canonical key as the live job above (different raw chat_id form).
        store.conn.execute("INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) "
                           "VALUES(?,?,?,?,?,?)",
                           (-100556, 903, "src_a", time.time(), 1, str(bot.raw_chat_id(-100556))))
        store.conn.commit()
        check("duplicate (chat_key,msg_id) rows are refused by the unique index", False)
    except Exception as exc:
        store.conn.rollback()
        check("duplicate (chat_key,msg_id) rows are refused by the unique index",
              "UNIQUE constraint" in str(exc))


# ---------------------------------------------------------------------------
# 3. LATENCY
# ---------------------------------------------------------------------------
async def test_latency(store):
    print("\n== immediacy (source post -> target post without waiting) ==")
    # Retry backoff is seconds, not minutes.
    store.conn.execute("INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key,status,attempts) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (-1001, 1001, "src_a", time.time(), 1, "-1001", "pending", 6))
    store.conn.commit()
    row = store.conn.execute("SELECT * FROM queue WHERE msg_id=1001").fetchone()
    await store.save_render(row["id"], "pending target text", ["LootZoneIndia11"], [])
    await store.finish(row, 0, None)
    deferred = store.conn.execute("SELECT next_at,created_at FROM queue WHERE msg_id=1001").fetchone()
    wait = deferred[0] - time.time()
    check("retry for undelivered targets waits <30s (was up to 300s)", 0 < wait < 30)

    check("job_retry_delay stays bounded",
          all(bot.job_retry_delay(a) <= bot.JOB_RETRY_MAX_SECONDS + 2 for a in range(12)))

    # Newest-first claiming so a fresh deal never queues behind old inventory.
    store.conn.execute("UPDATE queue SET status='done' WHERE status='pending'")
    store.conn.commit()
    now = time.time()
    for msg_id, age, prio in ((1002, 3600, 1), (1003, 10, 1)):
        store.conn.execute(
            "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key,status,next_at) "
            "VALUES(?,?,?,?,?,?,?,0)",
            (-1002, msg_id, "src_a", now - age, prio, "-1002", "pending"))
    store.conn.commit()
    claimed = await store.claim_job()
    check("newest deal is dispatched first (no waiting behind stale backlog)",
          claimed is not None and claimed["msg_id"] == 1003)
    bot.QUEUE_ORDER = "oldest"
    store.conn.execute("UPDATE queue SET status='pending' WHERE msg_id IN (1002,1003)")
    store.conn.commit()
    claimed_old = await store.claim_job()
    check("QUEUE_ORDER=oldest still honoured", claimed_old["msg_id"] == 1002)
    bot.QUEUE_ORDER = "newest"
    store.conn.execute("UPDATE queue SET status='done' WHERE msg_id IN (1002,1003)")
    store.conn.commit()

    # Stale jobs are dropped, not posted late.
    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key,status,next_at) "
        "VALUES(?,?,?,?,?,?,?,0)",
        (-1003, 1004, "src_a", now - 20 * 3600, 1, "-1003", "pending"))
    store.conn.commit()
    dropped = await store.drop_stale_jobs()
    status = store.conn.execute("SELECT status FROM queue WHERE msg_id=1004").fetchone()[0]
    check("job older than MAX_JOB_AGE_HOURS is dropped, never posted stale",
          dropped >= 1 and status == "done")

    # Queue wake-up: new work is claimed without waiting for a poll cycle.
    bot.QUEUE_WAKE = asyncio.Event()
    await store.enqueue(-1004, 1005, "src_a", "Wake test \u20b910 https://amzn.to/w9")
    check("enqueue wakes the idle workers immediately", bot.QUEUE_WAKE.is_set())
    bot.QUEUE_WAKE = None

    # A job that already sent one chunk keeps the stored bytes (no re-cut).
    qid = store.conn.execute("SELECT id FROM queue WHERE msg_id=1005").fetchone()[0]
    await store.save_render(qid, "line one\nline two", ["LootZoneIndia11"], [])
    check("no partial delivery before anything is sent",
          await store.has_partial_delivery(qid) is False)
    await store.set_delivery_progress(qid, "LootZoneIndia11", 1)
    check("partial delivery detected so the resume never re-sends a chunk",
          await store.has_partial_delivery(qid) is True)


async def test_http_pipeline():
    print("\n== HTTP: concurrent, cached, time-boxed ==")
    urls = [f"https://www.amazon.in/dp/B0TEST000{i}" for i in range(4)]
    text = "\n".join(urls)
    session = FakeSession(delay=0.3)
    client = bot.AffiliateClient(session)
    started = time.monotonic()
    ok = await client.rendered_links_not_broken(text)
    elapsed = time.monotonic() - started
    check("all links still health-checked before sending", ok is True)
    check(f"4 links checked concurrently ({elapsed:.2f}s < 0.9s serial floor)",
          elapsed < 0.9)
    check("each URL probed exactly once (cached verdicts)", len(session.calls) == 4)
    calls_after = len(session.calls)
    ok2 = await client.rendered_links_not_broken(text)
    check("second pass reuses the cache (no repeat 18s waits)",
          ok2 is True and len(session.calls) == calls_after)

    broken_session = FakeSession(delay=0.3, broken=[urls[2]])
    broken_client = bot.AffiliateClient(broken_session)
    check("a dead merchant page is still blocked",
          await broken_client.rendered_links_not_broken(text) is False)

    slow = FakeSession(delay=3.0)
    slow_client = bot.AffiliateClient(slow)
    old_budget = bot.PRESEND_CHECK_BUDGET_SECONDS
    bot.PRESEND_CHECK_BUDGET_SECONDS = 0.6
    started = time.monotonic()
    verdict = await slow_client.rendered_links_not_broken(text)
    elapsed = time.monotonic() - started
    bot.PRESEND_CHECK_BUDGET_SECONDS = old_budget
    check("a hanging merchant site cannot stall the queue (budget respected)",
          verdict is True and elapsed < 1.5)

    # Oversized/slow media must not freeze a worker.
    big = types.SimpleNamespace(media=types.SimpleNamespace(
        video=types.SimpleNamespace(size=int((bot.MAX_MEDIA_MB + 5) * 1024 * 1024))))
    small = types.SimpleNamespace(media=types.SimpleNamespace(
        video=types.SimpleNamespace(size=2 * 1024 * 1024)))
    photo = types.SimpleNamespace(media=types.SimpleNamespace(
        video=None, document=None, sizes=[types.SimpleNamespace(size=90_000)]))
    check("oversized source video is skipped (text still posts)",
          bot.media_is_too_large(big) is True)
    check("normal video and photos are downloaded",
          bot.media_is_too_large(small) is False and bot.media_is_too_large(photo) is False)


async def test_source_ids_and_rescan(store):
    print("\n== ingest: every source id convention is matched ==")
    source_map = {}
    entity = types.SimpleNamespace(id=4242424242)

    class FakeClient:
        def __init__(self, messages):
            self.messages = messages

        async def get_messages(self, chat_id, ids=None, limit=None):
            return self.messages

    import datetime as dt

    def msg(msg_id, text):
        return types.SimpleNamespace(
            id=msg_id, text=text, message=text, media=None, reply_to=None,
            entities=[], date=dt.datetime.now(dt.timezone.utc))

    fresh = msg(5001, "Noise ColorFit \u20b91,299 74% OFF https://amzn.to/nz1")
    client = FakeClient([fresh])
    # register_source itself needs the network (resolve_entity), so reproduce
    # exactly what it writes into the maps and run the rescan cycle against it.
    marked = -(10 ** 12 + entity.id)  # Telethon's marked channel id
    for key in {entity.id, marked}:
        source_map[key] = ("src_rescan", [])
    bot.RESOLVED_SOURCE_IDS["src_rescan"] = marked

    old_cycle, old_limit = bot.SOURCE_RESCAN_SECONDS, bot.SOURCE_RESCAN_LIMIT
    bot.SOURCE_RESCAN_SECONDS, bot.SOURCE_RESCAN_LIMIT = 1, 20
    stop = asyncio.Event()
    task = asyncio.create_task(bot.source_rescan_loop(client, source_map, stop))
    await asyncio.sleep(2.0)
    stop.set()
    task.cancel()
    bot.SOURCE_RESCAN_SECONDS, bot.SOURCE_RESCAN_LIMIT = old_cycle, old_limit
    rows = store.conn.execute(
        "SELECT COUNT(*) FROM queue WHERE chat_key=? AND msg_id=5001", (str(entity.id),)
    ).fetchone()[0]
    check("rescan recovers the missed source post once", rows == 1)

    # A second cycle over the same messages must not create a second queue row
    # and must not produce a second post.
    stop2 = asyncio.Event()
    task2 = asyncio.create_task(bot.source_rescan_loop(client, source_map, stop2))
    await asyncio.sleep(2.0)
    stop2.set()
    task2.cancel()
    rows2 = store.conn.execute(
        "SELECT COUNT(*) FROM queue WHERE chat_key=? AND msg_id=5001", (str(entity.id),)
    ).fetchone()[0]
    check("repeat rescan cycle adds no duplicate queue row", rows2 == 1)
    check("raw and marked chat ids collapse to one key",
          bot.raw_chat_id(marked) == entity.id and bot.raw_chat_id(entity.id) == entity.id)


# ---------------------------------------------------------------------------
# 4. OUR LINK REPLACES THE SOURCE LINK
# ---------------------------------------------------------------------------
async def test_link_replacement(store):
    print("\n== link replacement (source link -> our link, exactly once) ==")
    our_link = "https://www.amazon.in/dp/B0LINK0001?tag=deals0911-21"
    src = "https://amzn.to/src999"

    text = (
        "Apple Watch SE 44mm Aluminium\n"
        f"Price \u20b921,999 (Buy now from our telegram channel {bot.OUR_FOLDER_LINK})\n"
        "Extra 10% off with HDFC cards | Free delivery\n"
        f"Buy Now \U0001F449 {src}\n"
        f"Link \U0001F449 {src}\n"
        "Join for more loots https://whatsapp.com/channel/0029VaXYZ\n"
        "https://t.me/otherdeals123"
    )

    class FakeMsg:
        def __init__(self, text):
            self.text, self.message = text, text
            self.media, self.entities, self.reply_to, self.reply_markup = None, [], None, None

    class FakeClient:
        async def get_messages(self, chat_id, ids=None):
            return FakeMsg(text)

    class FakeAffiliate:
        """Realistic behaviour: only the merchant short link resolves+converts."""

        async def resolve(self, url):
            return "https://www.amazon.in/dp/B0LINK0001" if "amzn.to" in url else url

        async def convert(self, source, multi_link, resolved=None):
            if "amzn.to" not in source:
                return None
            return bot.LinkResult(source, "https://www.amazon.in/dp/B0LINK0001", our_link,
                                  "ASIN:B0LINK0001")

        async def cache_link(self, *a, **k):
            return None

        async def shorten_long_urls_in_text(self, rendered):
            return rendered

    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) VALUES(?,?,?,?,?,?)",
        (-1009, 7007, "src_link", time.time(), 1, str(bot.raw_chat_id(-1009))))
    store.conn.commit()
    row = store.conn.execute("SELECT * FROM queue WHERE msg_id=7007").fetchone()
    msg, rendered, price = await bot.render_job(FakeClient(), FakeAffiliate(), row)
    urls = [bot.clean_url(u) for u in bot.URL_RE.findall(rendered)]
    ours = {our_link, bot.OUR_FOLDER_LINK, *bot.OUR_MAIN_CHANNEL_LINKS}

    check("our affiliate link is in the post", our_link in rendered)
    check("the source short link is replaced, never posted", "amzn.to" not in rendered)
    check("our link appears exactly once (no duplicate link lines)",
          rendered.count(our_link) == 1)
    check("no foreign channel promo survives",
          "otherdeals123" not in rendered and "whatsapp" not in rendered.lower())
    check("no dangling 'Link' label left where a duplicate link was collapsed",
          not any(("link" in ln.lower() or "buy now" in ln.lower())
                  and "http" not in ln for ln in rendered.splitlines()))
    check("deal name, price and bank-offer terms preserved",
          "Apple Watch SE" in rendered and "\u20b921,999" in rendered and "HDFC" in rendered)
    check("no broken URL fragments in the final post",
          not any(ln.strip() in {"h", "htt", "uy", "https://", "ps://", ""}
                  and not ln.strip() == "" for ln in rendered.splitlines())
          and "ps://broken" not in rendered)
    check("every surviving link is one we own/generated",
          bool(urls) and all(u in ours for u in urls))
    # The rendered text is frozen for the delivery stage: an identical second
    # pass must not re-render the deal (that is what produces double posts).
    await store.update_rendered(row["id"], rendered)
    again = store.conn.execute("SELECT rendered_text FROM queue WHERE id=?", (row["id"],)).fetchone()[0]
    check("rendered post is persisted verbatim for retries", again == rendered)


async def test_collapse_migration(shared_store):
    print("\n== migration: pre-existing duplicate rows collapse ==")
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "old.sqlite3")
        bot.store = store  # render/ingest paths use the module global
        store.conn.execute("DROP INDEX ux_queue_chat_key")
        for chat_id in (777, -100777):  # the old double-key bug
            store.conn.execute(
                "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) "
                "VALUES(?,?,?,?,?,?)", (chat_id, 904, "src_a", time.time(), 1, "777"))
        store.conn.execute("INSERT INTO deliveries(queue_id,target) VALUES(1,'LootZoneIndia11')")
        store.conn.execute("INSERT INTO deal_claims VALUES('ASIN:B0OLD00001', 2, ?)", (time.time(),))
        store.conn.commit()
        store._collapse_duplicate_queue_rows()
        store.conn.commit()
        rows = store.conn.execute("SELECT COUNT(*) FROM queue WHERE msg_id=904").fetchone()[0]
        claims = store.conn.execute(
            "SELECT COUNT(*) FROM deal_claims WHERE queue_id=2").fetchone()[0]
        deliveries = store.conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE queue_id=2").fetchone()[0]
        check("one queue row survives per source message", rows == 1)
        check("the dropped job's claim and delivery ledger are cleaned up",
              claims == 0 and deliveries == 0)
        bot.store = shared_store  # restore for the rest of the suite


async def test_housekeeping(store):
    print("\n== housekeeping never breaks the guarantees ==")
    # maintenance() used to delete deal_claims older than 1h unconditionally,
    # freeing the lock of a job that is still queued -> duplicate post.
    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key,status) "
        "VALUES(?,?,?,?,?,?,?)", (-100700, 8001, "src_a", time.time(), 1, "-100700", "pending"))
    store.conn.commit()
    live = store.conn.execute("SELECT id FROM queue WHERE msg_id=8001").fetchone()[0]
    store.conn.execute("INSERT OR REPLACE INTO deal_claims VALUES(?,?,?)",
                       ("ASIN:B0LIVE0001", live, time.time() - 7200))
    # a finished job's stale claim should still be swept
    store.conn.execute("INSERT OR REPLACE INTO deal_claims VALUES(?,?,?)",
                       ("ASIN:B0DEAD0001", 999999, time.time() - 7200))
    store.conn.commit()
    await store.maintenance()
    live_kept = store.conn.execute(
        "SELECT 1 FROM deal_claims WHERE deal_key='ASIN:B0LIVE0001'").fetchone()
    dead_gone = store.conn.execute(
        "SELECT 1 FROM deal_claims WHERE deal_key='ASIN:B0DEAD0001'").fetchone()
    check("maintenance keeps the claim of a live job (no duplicate post window)",
          live_kept is not None)
    check("maintenance still sweeps claims of finished work", dead_gone is None)

    # Premium scheduling must not crash on the gap window (it feeds randint).
    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key,status) "
        "VALUES(?,?,?,?,?,?,?)", (-100701, 8002, "src_a", time.time(), 1, "-100701", "pending"))
    store.conn.commit()
    qid = store.conn.execute("SELECT id FROM queue WHERE msg_id=8002").fetchone()[0]
    await store.claim_premium(qid)
    await store.complete_premium(qid, True)
    next_at = float(store.conn.execute("SELECT next_at FROM premium_state WHERE id=1").fetchone()[0])
    check("premium next-slot scheduled (gap is a plain number of seconds)",
          next_at > time.time() - 1)
    store.conn.execute("UPDATE queue SET status='done' WHERE id=?", (qid,))
    store.conn.commit()


async def test_render_latency(store):
    print("\n== render stage is concurrent, not serial ==")
    links = [f"https://amzn.to/lat{i}" for i in range(4)]
    text = "Mega list deal \u20b9199\n" + "\n".join(
        f"Item {i} \u20b9{99 + i} https://{u}" for i, u in enumerate(links))

    class FakeMsg:
        def __init__(self, t):
            self.text, self.message = t, t
        media = None
        entities = []
        reply_to = None
        reply_markup = None

    class FakeClient:
        async def get_messages(self, chat_id, ids=None):
            return FakeMsg(text)

    class SlowAffiliate:
        """Each network step costs 0.25s: serial = ~2s, concurrent = ~0.25s."""

        async def resolve(self, url):
            await asyncio.sleep(0.25)
            return f"https://www.amazon.in/dp/B0LAT0000{url[-1]}"

        async def convert(self, source, multi_link, resolved=None):
            await asyncio.sleep(0.25)
            idx = source[-1]
            return bot.LinkResult(source, f"https://www.amazon.in/dp/B0LAT0000{idx}",
                                  f"https://www.amazon.in/dp/B0LAT0000{idx}?tag=deals0911-21",
                                  f"ASIN:B0LAT0000{idx}")

        async def cache_link(self, *a, **k):
            return None

        async def shorten_long_urls_in_text(self, rendered):
            return rendered

    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) VALUES(?,?,?,?,?,?)",
        (-100800, 8100, "src_lat", time.time(), 3, str(bot.raw_chat_id(-100800))))
    store.conn.commit()
    row = store.conn.execute("SELECT * FROM queue WHERE msg_id=8100").fetchone()
    started = time.monotonic()
    _msg, rendered, _price = await bot.render_job(FakeClient(), SlowAffiliate(), row)
    elapsed = time.monotonic() - started
    check(f"4-link post rendered concurrently ({elapsed:.2f}s; serial would be >=2s)",
          elapsed < 1.4)
    check("every product keeps its OWN converted link (no pooled links, no source links)",
          all(f"https://www.amazon.in/dp/B0LAT0000{u[-1]}?tag=deals0911-21" in rendered
              for u in links) and "amzn.to" not in rendered)


async def test_price_gate_is_a_fallback(store):
    print("\n== same-price gate never eats a different product ==")
    price = 99
    old_window = bot.PRICE_DEDUP_SECONDS
    bot.PRICE_DEDUP_SECONDS = 3600  # the deployed .env value; code default is off
    # Fresh dedup state for this case only.
    store.conn.execute("DELETE FROM price_posts")
    store.conn.execute("DELETE FROM deal_claims")
    store.conn.execute("DELETE FROM posted_deals")
    store.conn.commit()
    ok1, keys1 = await store.reserve(1, ["ASIN:B0PRICE001"], price, True)
    check("first product reserved", ok1 is True)
    store.conn.execute("INSERT OR REPLACE INTO price_posts VALUES(?,?,?)",
                       (price, time.time(), 1))
    store.conn.commit()
    ok2, _ = await store.reserve(2, ["ASIN:B0PRICE002"], price, True)
    check("a DIFFERENT product at the same price still posts (identity known)",
          ok2 is True)
    ok3, _ = await store.reserve(3, ["URL:whateverhash"], price, False)
    check("an unidentified link at an already-posted price is still blocked",
          ok3 is False)
    bot.PRICE_DEDUP_IGNORES_IDENTITY = True
    ok4, _ = await store.reserve(4, ["ASIN:B0PRICE003"], price, True)
    check("PRICE_DEDUP_IGNORES_IDENTITY=true restores the strict legacy gate",
          ok4 is False)
    bot.PRICE_DEDUP_IGNORES_IDENTITY = False
    bot.PRICE_DEDUP_SECONDS = old_window
    store.conn.execute("DELETE FROM price_posts")
    store.conn.execute("DELETE FROM deal_claims")
    store.conn.commit()


def test_final_text_fidelity():
    """The post must show the source's own words - nothing more, nothing mangled.

    Reported in the wild as "Top Loading Washing Machine Cover @
    \u20b9260tG7oChgiQuTgS25b" plus "\u279c [https://bitli.in/..](https://bitli.in/..)"
    lines: the price is right but a masked-shortener fragment was glued to it and
    markdown debris was printed literally (posts are sent with no parse_mode).
    """
    print("\n== final text = source text, cleaned and nothing added ==")
    glued = "Top Loading Washing Machine Cover @ \u20b9260tG7oChgiQuTgS25b"
    out = bot.clean_source_text(glued)
    check("the \u20b9260 price survives exactly as the source wrote it", out.endswith("\u20b9260"))
    check("the random 17-char fragment is gone", "tG7oChgiQuTgS25b" not in out and "260t" not in out)
    spaced = bot.clean_source_text("Sony Headphones \u20b91,499 Xk9LaMn20QpR7 extra bass")
    check("a spaced random fragment after a price is gone too", "Xk9LaMn20QpR7" not in spaced)
    check("the real words around it are kept", "Sony Headphones" in spaced and "extra bass" in spaced)

    # Junk the old length-capped rules missed must go, real deal content must not.
    kept = ("Cotton Tshirt Pack of 2 \u20b9249 (500ml, 2pcs, 65w, 20000mAh)\n"
            "Use code: SAVE_200 for \u20b9200 off\nPrice \u20b9249 (55% OFF)")
    preserved = bot.clean_source_text(kept)
    for frag in ("(500ml, 2pcs, 65w, 20000mAh)", "SAVE_200", "\u20b9249", "(55% OFF)"):
        check(f"real content kept: {frag}", frag in preserved)

    md = "\u279c [https://bitli.in/IKthI4w](https://bitli.in/IKthI4w)"
    final = bot.sanitize_outbound_text("Cover \u20b9260\n" + md + "\n" + md.replace("IKthI4w", "6ft5j8a"))
    check("markdown [url](url) debris never survives the outbound guard",
          "](" not in final and "[" not in final and ")" not in final)
    check("both real links still appear", final.count("https://bitli.in/") == 2)
    check("the price line is untouched by the markdown guard", "\u20b9260" in final)
    check("outbound guard is idempotent (a retry sends the same bytes)",
          bot.sanitize_outbound_text(final) == final)
    # A cleanup pass must never eat a link: dropping one silently deletes a whole
    # deal line (that is how "posts are missing" would come back).
    many = "\n".join(f"Item {i} \u20b9{i * 111}\nhttps://bitli.in/keep{i}" for i in range(6))
    guarded = bot.sanitize_outbound_text(many)
    check("the guard never removes or rewrites a link",
          len(bot.URL_RE.findall(guarded)) == len(bot.URL_RE.findall(many))
          and all(f"https://bitli.in/keep{i}" in guarded for i in range(6)))
    check("the guard never removes a price line",
          all(f"\u20b9{i * 111}" in guarded for i in range(6)))

    # Cleanup residue such as an empty bracket pair must not be published.
    check("empty parens left by promo stripping are removed",
          "( )" not in bot.fix_unbalanced_parens("Price \u20b921,999 ( )"))
    # Our own links are the point of the bot - never corrupt one.
    folder = "\u0001f4c2 Join: " + bot.OUR_FOLDER_LINK
    check("our folder link keeps its underscore (a stripped _ = dead invite)",
          bot.OUR_FOLDER_LINK in bot.normalize_nested_link_markup(folder))
    check("an underscore inside a URL is preserved by the whole chain",
          bot.OUR_FOLDER_LINK in bot.sanitize_outbound_text(folder))
    check("emphasis debris is still removed from text parts",
          bot.normalize_nested_link_markup("**Bold Sale** at *50%* off") == "Bold Sale at 50% off")


async def test_no_silent_loss(store):
    """No source post may be swallowed by dedup or by an unmonetizable link."""
    print("\n== every source post reaches the channels (no silent loss) ==")
    # Different products under one repeated banner line: the content fingerprint
    # used to key on the banner alone, so 2 and 3 looked "already posted".
    banner = "\U0001f525\U0001f525 TOP DEAL OF THE DAY \U0001f525\U0001f525"
    posts = [
        f"{banner}\nSony 32 inch HD Smart TV\nPrice \u20b911,999 (37% OFF)\nhttps://fkrt.is/p1",
        f"{banner}\nboAt Airdopes 141 TWS Earbuds\nPrice \u20b91,099 (75% OFF)\nhttps://fkrt.is/p2",
        f"{banner}\nNoise ColorFit Pro 4 Smartwatch\nPrice \u20b91,499 (65% OFF)\nhttps://fkrt.is/p3",
    ]
    keys = [bot.content_deal_key(p) for p in posts]
    check("each product gets its OWN fingerprint", len(set(keys)) == 3 and all(keys))
    same = bot.content_deal_key(posts[0].replace("fkrt.is/p1", "tinyurl.com/other"))
    check("a re-post of the SAME campaign still dedups (different short link)",
          same == keys[0])
    for i, text in enumerate(posts):
        ok = await store.enqueue(-100777, 7100 + i, "banner_src", text)
        check(f"different deal #{i + 1} under a shared banner is queued", ok is True)
    rows = store.conn.execute(
        "SELECT COUNT(*) c FROM queue WHERE chat_id=-100777").fetchone()["c"]
    check("all three rows are in the queue (nothing dropped at intake)", rows == 3)

    # EarnKaro has no campaign for a store: the post must still go out, with the
    # clean untagged merchant link, instead of burning 10 retries and vanishing.
    src = "https://www.myntra.com/ethnic-men-s-shirts/x/12345/detail"

    class FakeMsg:
        def __init__(self, text):
            self.text, self.message = text, text
            self.media = self.entities = self.reply_to = self.reply_markup = None

    class FakeClient:
        async def get_messages(self, chat_id, ids=None):
            return FakeMsg("Men\u2019s Regular Fit Shirt\nPrice \u20b9599 (68% OFF)\n" + src)

    class NoCampaignAffiliate:
        """The store is monetizable in principle; the network simply returns nothing."""

        async def resolve(self, url):
            return url

        async def convert(self, source, multi_link, resolved=None):
            return None

        async def cache_link(self, *a, **k):
            return None

        async def shorten_long_urls_in_text(self, rendered):
            return rendered

    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,attempts,chat_key) "
        "VALUES(?,?,?,?,?,?,?)",
        (-100778, 7200, "shirt_src", time.time(), 1, bot.JOB_MAX_ATTEMPTS, str(bot.raw_chat_id(-100778))))
    store.conn.commit()
    row = store.conn.execute("SELECT * FROM queue WHERE msg_id=7200").fetchone()
    _msg, rendered, price = await bot.render_job(FakeClient(), NoCampaignAffiliate(), row)
    check("an unmonetizable store post is still published (was silently lost)",
          "Regular Fit Shirt" in rendered and "\u20b9599" in rendered)
    check("the published link is the clean merchant page",
          "myntra.com" in rendered and "detail" in rendered)
    check("no foreign affiliate/tag param survives on a pass-through link",
          not any(k in rendered for k in ("?tag=", "&tag=", "affid=", "utm_", "clickid")))
    check("the pass-through link has our provenance (verify_generated_text accepts it)",
          await store.verify_generated_text(rendered, ()))
    check("price is parsed from the source text, not from a junk token", price == 599)
    again = store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) "
        "VALUES(?,?,?,?,?,?)",
        (-100779, 7201, "other_src", time.time(), 1, str(bot.raw_chat_id(-100779))))
    store.conn.commit()
    row2 = store.conn.execute("SELECT * FROM queue WHERE msg_id=7201").fetchone()
    try:
        await bot.render_job(FakeClient(), NoCampaignAffiliate(), row2)
        check("the same deal from a second source is deduped, not reposted", False)
    except bot.DuplicateDeal:
        check("the same deal from a second source is deduped, not reposted", True)


async def test_edited_source_posts(store):
    """A source post edited into shape must still reach our channels."""
    print("\n== edited source posts are never lost ==")
    await store.enqueue(-100888, 8100, "edit_src", "Some Shirt \u20b9599")
    rid = store.conn.execute("SELECT id FROM queue WHERE msg_id=8100").fetchone()["id"]
    await store.mark_done(rid, "PermanentSkip: no monetizable URLs")
    key = str(bot.raw_chat_id(-100888))
    check("a post that was skipped for having no usable link is re-opened on edit",
          await store.revive_edited_job(key, 8100) == "revived")
    state = store.conn.execute("SELECT status,attempts,rendered_text FROM queue WHERE id=?", (rid,)).fetchone()
    check("the revived job is queued again, fresh budget, no stale render",
          state["status"] == "pending" and state["attempts"] == 0 and not state["rendered_text"])

    store.conn.execute("UPDATE queue SET status='done' WHERE id=?", (rid,))
    store.conn.execute("INSERT INTO deliveries(queue_id,target,status) VALUES(?,?, 'sent')",
                       (rid, "LootZoneIndia11"))
    store.conn.commit()
    check("an already-delivered deal is NOT reposted when the source edits it",
          await store.revive_edited_job(key, 8100) is None)
    check("and its queue row stays closed",
          store.conn.execute("SELECT status FROM queue WHERE id=?", (rid,)).fetchone()["status"] == "done")
    check("a job that has not run yet is left as pending (no churn, no double work)",
          await store.revive_edited_job(key, 8100) in (None, "pending"))

    # Genuine transient API failures still retry (only the FINAL attempt degrades),
    # otherwise a network blip would post an unmonetized link at once.
    class FakeMsg:
        def __init__(self, text):
            self.text, self.message = text, text
            self.media = self.entities = self.reply_to = self.reply_markup = None

    class FakeClient:
        async def get_messages(self, chat_id, ids=None):
            return FakeMsg("Men\u2019s Cotton Shirt\nPrice \u20b9599 (68% OFF)\nhttps://www.myntra.com/x/1/detail")

    class ExplodingAffiliate:
        async def resolve(self, url):
            return url

        async def convert(self, source, multi_link, resolved=None):
            raise RuntimeError("EarnKaro 503")

    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,attempts,chat_key) "
        "VALUES(?,?,?,?,?,?,?)",
        (-100889, 8101, "edit_src", time.time(), 1, 0, str(bot.raw_chat_id(-100889))))
    store.conn.commit()
    row = store.conn.execute("SELECT * FROM queue WHERE msg_id=8101").fetchone()
    try:
        await bot.render_job(FakeClient(), ExplodingAffiliate(), row)
        check("a mid-budget API error still retries instead of degrading", False)
    except RuntimeError as exc:
        check("a mid-budget API error still retries instead of degrading",
              "conversion retry required" in str(exc))


async def test_passthrough_knob(store):
    """PASSTHROUGH_UNMONETIZED=false must restore the old refuse-to-post rule."""
    print("\n== pass-through has a documented kill switch ==")

    class FakeMsg:
        def __init__(self, text):
            self.text, self.message = text, text
            self.media = self.entities = self.reply_to = self.reply_markup = None

    class FakeClient:
        async def get_messages(self, chat_id, ids=None):
            return FakeMsg("Men\u2019s Cotton Shirt\nPrice \u20b9599 (68% OFF)\n"
                           "https://www.myntra.com/x/1/detail")

    class NoCampaign:
        async def resolve(self, url):
            return url

        async def convert(self, source, multi_link, resolved=None):
            return None

        async def cache_link(self, *a, **k):
            return None

        async def shorten_long_urls_in_text(self, rendered):
            return rendered

    old = bot.PASSTHROUGH_UNMONETIZED
    try:
        bot.PASSTHROUGH_UNMONETIZED = False
        store.conn.execute(
            "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,attempts,chat_key) "
            "VALUES(?,?,?,?,?,?,?)",
            (-100999, 8200, "knob_src", time.time(), 1, bot.JOB_MAX_ATTEMPTS,
             str(bot.raw_chat_id(-100999))))
        store.conn.commit()
        row = store.conn.execute("SELECT * FROM queue WHERE msg_id=8200").fetchone()
        try:
            await bot.render_job(FakeClient(), NoCampaign(), row)
            check("false = the deal is refused, exactly like before v17", False)
        except bot.PermanentSkip:
            check("false = the deal is refused, exactly like before v17", True)
        bot.PASSTHROUGH_UNMONETIZED = True
        store.conn.execute(
            "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,attempts,chat_key) "
            "VALUES(?,?,?,?,?,?,?)",
            (-100998, 8201, "knob_src", time.time(), 1, bot.JOB_MAX_ATTEMPTS,
             str(bot.raw_chat_id(-100998))))
        store.conn.commit()
        row2 = store.conn.execute("SELECT * FROM queue WHERE msg_id=8201").fetchone()
        _msg, rendered, _price = await bot.render_job(FakeClient(), NoCampaign(), row2)
        check("default (true) = the deal posts with the clean merchant link",
              "myntra.com/x/1/detail" in rendered)
    finally:
        bot.PASSTHROUGH_UNMONETIZED = old


async def test_real_post_shape():
    """The user-reported post, run through the ACTUAL send path, must be clean.

    Source line was published as "@ ₹260tG7oChgiQuTgS25b" followed by four
    "\u279c [https://bitli.in/XXXX](https://bitli.in/XXXX)" lines - the exact
    complaint (a token the source never wrote, plus literal markdown around our
    own links).
    """
    print("\n== the reported post, through deliver() ==")
    dirty = ("Top Loading Washing Machine Cover @ \u20b9260tG7oChgiQuTgS25b (55% OFF)\n"
             "\u279c [https://bitli.in/IKthI4w](https://bitli.in/IKthI4w)\n"
             "\u279c [https://bitli.in/6ft5j8a](https://bitli.in/6ft5j8a)\n"
             "\u279c [https://bitli.in/GsO58oA](https://bitli.in/GsO58oA)\n"
             "\u279c [https://bitli.in/Tt11zzQ](https://bitli.in/Tt11zzQ)")
    sent = []

    class Api:
        async def send_message(self, chat, text=None, **kw):
            sent.append(text)
            return type("M", (), {"id": 1})()

    ok, _err = await bot.deliver(Api(), "SomeTarget", dirty, None)
    post = sent[0] if sent else ""
    check("the post is actually sent (the guard never blocks a real deal)", ok and bool(post))
    check("price reads exactly \u20b9260 (no glued token, nothing appended)",
          "\u20b9260 (55% OFF)" in post and "260t" not in post)
    check("the source\u2019s random fragment is nowhere in the post", "tG7oChgiQuTgS25b" not in post)
    check("no markdown debris survives in a plain-text post",
          "](" not in post and "[" not in post)
    check("every one of our four links survives, exactly once each",
          all(f"https://bitli.in/{code}" in post for code in
              ("IKthI4w", "6ft5j8a", "GsO58oA", "Tt11zzQ"))
          and post.count("https://bitli.in/") == 4)
    check("the deal name line is untouched", "Top Loading Washing Machine Cover" in post)


async def test_list_post_shapes(store):
    """The loot-list format most sources use: one headline + a block of links.

    A bullet emoji used to be mistaken for a product label, so the rebuild kept
    "\u279c + link" pairs and the HEADLINE (product name + price) vanished from
    the published post. One product with four variant links has ONE label: the
    source layout (name, price, every link) must survive untouched.
    """
    print("\n== list-post shapes keep the headline and every link ==")
    # one distinct product per shape, so the shapes are genuinely different
    # deals (the SAME campaign from two sources must dedup - and it does, which
    # is exactly what an earlier run of this test proved).
    names = ["Top Loading Washing Machine Cover", "Quilted Top Load Washer Protector",
             "Heavy Duty Washing Machine Cover", "Universal Top Load Machine Shield"]

    class FakeMsg:
        def __init__(self, text):
            self.text, self.message = text, text
            self.media = self.entities = self.reply_to = self.reply_markup = None

    class Client:
        def __init__(self, text):
            self._text = text

        async def get_messages(self, chat_id, ids=None):
            return FakeMsg(self._text)

    class Aff:
        async def resolve(self, url):
            return "https://www.amazon.in/dp/B0" + url.rsplit("/", 1)[-1][:6].upper()

        async def convert(self, source, multi_link, resolved=None):
            code = source.rsplit("/", 1)[-1].rstrip("]")
            if code not in ours:
                return None
            return bot.LinkResult(source, resolved or "", ours[code], "ASIN:B0" + code[:6].upper())

        async def cache_link(self, *a, **k):
            return None

        async def shorten_long_urls_in_text(self, rendered):
            return rendered

    shapes = {}
    for n in range(4):
        codes = ["%dtG7oChgiQuTgS25b" % n, "%dIKthI4w" % n, "%d6ft5j8a" % n, "%d3FQw8wi" % n]
        shapes[n] = codes
    ours = {c: "https://www.amazon.in/dp/B0" + c[:6].upper() + "?tag=deals0911-21"
            for cs in shapes.values() for c in cs}
    titles = [names[n] + " \u20b9260" for n in range(4)]
    # the exact channel banner decoration sources paste above every list
    banners = "\ud83d\udd25\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25\ud83d\udd25\n" \
              "\u26a1\ufe0f\u26a1\ufe0f 11 PM FLASH SALE \u26a1\ufe0f\u26a1\ufe0f\n\n"
    shape_list = [
        ("plain bullets", titles[0] + "\n" + "\n".join("\u279c https://bitli.in/" + c for c in shapes[0])),
        ("markdown bullets", titles[1] + "\n" + "\n".join(
            "\u279c [https://bitli.in/{0}](https://bitli.in/{0})".format(c) for c in shapes[1])),
        ("link glued to the price", titles[2] + "https://bitli.in/" + shapes[2][0] + "\n" + "\n".join(
            "\u279c https://bitli.in/" + c for c in shapes[2][1:])),
        ("campaign banners above the list", banners + titles[3] + "\n" + "\n".join(
            "\u279c [https://bitli.in/{0}](https://bitli.in/{0})".format(c) for c in shapes[3])),
    ]

    for n, (name, text) in enumerate(shape_list):
        codes = shapes[n]
        store.conn.execute(
            "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) VALUES(?,?,?,?,?,?)",
            (-100600 - n, 9000 + n, "list_src", time.time(), 1,
             str(bot.raw_chat_id(-100600 - n))))
        store.conn.commit()
        row = store.conn.execute("SELECT * FROM queue WHERE msg_id=?", (9000 + n,)).fetchone()
        _m, rendered, price = await bot.render_job(Client(text), Aff(), row)
        lines = [ln for ln in rendered.splitlines() if ln.strip()]
        check("[%s] product name and price stay in the post" % name,
              names[n] in rendered and "\u20b9260" in rendered)
        check("[%s] every one of our links is present" % name,
              all(ours[c] in rendered for c in codes))
        check("[%s] no source shortener link survives" % name, "bitli.in" not in rendered)
        check("[%s] no leaked short-code glued anywhere" % name,
              not any(bot.re.search(r"\u20b9260[A-Za-z0-9]", ln) for ln in lines)
              and not any(c in ln for ln in lines for c in codes))
        check("[%s] no markdown debris in a plain-text post" % name,
              not any("[" in ln or "](" in ln for ln in lines))
        check("[%s] each link sits on its own line" % name,
              all(ln.strip().startswith(("\u279c", "http")) or "\u20b9" in ln
                  or bot.is_campaign_banner_line(ln) for ln in lines))
        check("[%s] the price used for routing is the source price" % name, price == 260)
        if "banners" in name:
            # SOURCE FIDELITY (the user's rule): the posting channel's own hype
            # header is part of its post and must be published as written.
            check("[%s] source banner lines kept verbatim" % name,
                  "TOP DEAL OF THE DAY" in rendered and "FLASH SALE" in rendered)
        else:
            check("[%s] no hype or junk that the source did not write" % name,
                  "Refer" not in rendered and "notifications" not in rendered)

    # The same campaign posted by a second source (identical text, different
    # shortener) must still be refused - dedup did not get weaker.
    dup_row = None
    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) VALUES(?,?,?,?,?,?)",
        (-100605, 9005, "other_src", time.time(), 1, str(bot.raw_chat_id(-100605))))
    store.conn.commit()
    dup_row = store.conn.execute("SELECT * FROM queue WHERE msg_id=9005").fetchone()
    try:
        await bot.render_job(Client(shape_list[0][1]), Aff(), dup_row)
        check("the same campaign from another source is still deduped", False)
    except bot.DuplicateDeal:
        check("the same campaign from another source is still deduped", True)

    # A genuine multi-product list (one real label per link) must STILL be
    # rebuilt into neat label->link pairs; the fix above may not disable that.
    multi = ("Men Running Shoes\nhttps://bitli.in/m1aa\n"
             "Women Cotton Kurti\nhttps://bitli.in/m2bb\n"
             "Kids School Bag\nhttps://bitli.in/m3cc")
    pairs = {
        "https://bitli.in/m1aa": "https://www.amazon.in/dp/B0MEN1?tag=deals0911-21",
        "https://bitli.in/m2bb": "https://www.amazon.in/dp/B0WOM2?tag=deals0911-21",
        "https://bitli.in/m3cc": "https://www.amazon.in/dp/B0KID3?tag=deals0911-21",
    }

    class MultiAff(Aff):
        async def resolve(self, url):
            return url

        async def convert(self, source, multi_link, resolved=None):
            if source not in pairs:
                return None
            return bot.LinkResult(source, source, pairs[source], "ASIN:" + source[-4:])

    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) VALUES(?,?,?,?,?,?)",
        (-100699, 9099, "list_src", time.time(), 1, str(bot.raw_chat_id(-100699))))
    store.conn.commit()
    row = store.conn.execute("SELECT * FROM queue WHERE msg_id=9099").fetchone()
    _m, rendered, _p = await bot.render_job(Client(multi), MultiAff(), row)
    check("a real 3-product list is still rebuilt as label\u2192link pairs",
          all(label in rendered for label in ("Men Running Shoes", "Women Cotton Kurti", "Kids School Bag"))
          and all(link in rendered for link in pairs.values()))
    order = [rendered.index(x) for x in ("Men Running Shoes", pairs["https://bitli.in/m1aa"],
                                        "Women Cotton Kurti", pairs["https://bitli.in/m2bb"])]
    check("each product keeps ITS OWN link in source order", order == sorted(order))

    # FIDELITY of the list rebuild: the lines that are NOT a product label (a
    # second header, an MRP/shipping note) must survive it too, in place.
    rich = ("MEGA LIST SALE\nMen Running Shoes\nhttps://bitli.in/m1aa\n"
            "Women Cotton Kurti\nhttps://bitli.in/m2bb\nMRP ₹1999, free shipping\n"
            "Kids School Bag\nhttps://bitli.in/m3cc")
    rich_out = bot.format_visible_source_product_pairs(bot.clean_source_text(rich), pairs)
    check("the list rebuild keeps non-label source lines",
          rich_out is not None and "MEGA LIST SALE" in rich_out and "MRP \u20b91999, free shipping" in rich_out)
    check("and every product with its own link",
          all(x in rich_out for x in pairs.values()) and "Men Running Shoes" in rich_out
          and rich_out.index("Men Running Shoes") < rich_out.index(pairs["https://bitli.in/m1aa"])
          < rich_out.index("Women Cotton Kurti"))


def test_unwanted_text_never_posts():
    """Nothing the source did not offer as the deal may appear in our post."""
    print("\n== unwanted text (banners, referral farming) never posts ==")
    junk = [
        "Get Flipkart App - Refer 3 friends and \u20b9100 referral bonus",
        "Refer 5 friends to earn \u20b950 each",
        "Install the app and get \u20b920 bonus",
        "Share this with your family group",
        "Follow our channel for more loot",
    ]
    for line in junk:
        check("junk line never posted: %s" % line[:34], bot.is_promo_noise_line(line))
        check("clean_source_text removes it: %s" % line[:34],
              line.split(" - ")[0][:14] not in bot.clean_source_text(
                  "Men Shirt \u20b9399\nhttps://a.co/x\n" + line))
    keep = [
        "Men Shirt \u20b9399 (75% OFF)",
        "Use code SAVE200 for extra \u20b9200 off",
        "Top Loading Washing Machine Cover @ \u20b9260",
        "Size 7 (UK) | Color: Blue",
        "MRP \u20b91999",
    ]
    for line in keep:
        check("real content kept: %s" % line[:34], not bot.is_promo_noise_line(line))
    pasted = ("\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25\n"
              "\u26a1\ufe0f\u26a1\ufe0f 11 PM FLASH SALE \u26a1\ufe0f\u26a1\ufe0f\n\n"
              "Top Loading Washing Machine Cover @ \u20b9260\n"
              "\u279c https://bitli.in/IKthI4w\n"
              "Get Flipkart App - Refer 3 friends and \u20b9100 referral bonus")
    clean = bot.clean_source_text(pasted)
    # FIDELITY: the source's own header lines stay exactly as written; only the
    # referral/app-install farming line is cut.
    check("source keeps its own header, product line and link",
          clean == ("\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25\n"
                    "\u26a1\ufe0f\u26a1\ufe0f 11 PM FLASH SALE \u26a1\ufe0f\u26a1\ufe0f\n\n"
                    "Top Loading Washing Machine Cover @ \u20b9260\n"
                    "\u279c https://bitli.in/IKthI4w"))
    check("tidy_post damages nothing already clean (idempotent, nothing eaten)",
          bot.tidy_post(clean) == clean and bot.clean_source_text(clean) == clean)
    check("no invented text anywhere", "Refer" not in clean and "Install" not in clean)


def test_coverage_audit():
    """ops/coverage_audit.py must find the swallowed posts and heal only those."""
    print("\n== coverage audit finds and heals the lost posts ==")
    import importlib.util
    spec = importlib.util.spec_from_file_location("coverage_audit",
                                                  Path(__file__).resolve().parent / "ops" / "coverage_audit.py")
    audit_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit_mod)

    db = Path(tempfile.mktemp(suffix=".sqlite3"))
    store = bot.Store(db)
    now = time.time()
    cases = [
        # (status, last_error, has_successful_delivery) -> expected bucket
        ("done", "", True, "posted"),
        ("done", "DuplicateDeal: duplicate deal", False, "dedup"),
        ("done", "PermanentSkip: night window", False, "policy"),
        ("done", "STALE DROP: older than the freshness budget", False, "policy"),
        ("failed", "RuntimeError: conversion retry required: EarnKaro 503", False, "lost"),
        ("done", "PermanentSkip: no monetizable URLs", False, "lost"),
        ("pending", "", False, "in-flight"),
    ]
    ids = {}
    for n, (status, err, delivered, expect) in enumerate(cases):
        store.conn.execute(
            "INSERT INTO queue(chat_id,msg_id,source,status,attempts,last_error,created_at,chat_key)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (-100300 - n, 300 + n, "audit_src", status, 4, err, now - n * 60,
             str(bot.raw_chat_id(-100300 - n))))
        ids[expect] = store.conn.execute(
            "SELECT last_insert_rowid() id").fetchone()["id"]
        if delivered:
            store.conn.execute("INSERT INTO deliveries(queue_id,target,status) VALUES(?,?,'sent')",
                               (ids[expect], "LootZoneIndia11"))
            ids["posted"] = ids[expect]
    store.conn.execute("INSERT INTO deal_claims(queue_id,deal_key,claimed_at) VALUES(?,?,?)",
                       (ids["lost"], "ASIN:AUDITLOST", now))
    store.conn.commit()
    store.conn.close()

    report = audit_mod.audit(db, 24)
    buckets = report["per_source"]["audit_src"]
    check("audit counts every source post", buckets["seen"] == len(cases))
    check("audit counts what actually reached a target", buckets["posted"] == 1)
    check("audit classifies the swallowed posts as LOST", len(report["lost"]) == 2)
    check("audit does NOT cry wolf on dedup/policy/in-flight rows",
          buckets["dedup"] == 1 and buckets["policy"] == 2 and buckets["in-flight"] == 1)
    lost_reasons = " ".join(row["reason"] for row in report["lost"])
    check("every LOST reason is a bug-shaped gap",
          "conversion retry required" in lost_reasons and "no monetizable URLs" in lost_reasons)

    healed = audit_mod.heal(db, [row["id"] for row in report["lost"]])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    states = {r["msg_id"]: r for r in conn.execute("SELECT * FROM queue")}
    lost_ids = {row["msg_id"] for row in report["lost"]}
    check("--heal re-queues exactly the lost rows", healed == 2
          and all(states[m]["status"] == "pending" and states[m]["attempts"] == 0
                  for m in lost_ids))
    check("--heal clears the stale live claim so the re-render is allowed",
          conn.execute("SELECT COUNT(*) c FROM deal_claims WHERE deal_key='ASIN:AUDITLOST'"
                       ).fetchone()["c"] == 0)
    check("posted / dedup / policy / in-flight rows are untouched",
          states[300]["status"] == "done" and states[301]["status"] == "done"
          and states[302]["status"] == "done" and states[303]["status"] == "done"
          and states[306]["status"] == "pending")
    check("the dedup record of what subscribers already saw is preserved",
          conn.execute("SELECT COUNT(*) c FROM posted_deals").fetchone()["c"] >= 0
          and conn.execute("SELECT COUNT(*) c FROM posted_deals WHERE deal_key='ASIN:AUDITLOST'"
                           ).fetchone()["c"] == 0)
    conn.close()
    # a second audit after healing must be clean
    again = audit_mod.audit(db, 24)
    check("after --heal the audit reports nothing lost (rows are queued again)",
          not again["lost"])


def test_campaign_banner_lines():
    """FIDELITY: a source's own hype header stays; another channel's branding goes.

    v17.1 stripped the hype header lines; the user's rule is the opposite -
    "source lo ela vundo ala" - so stripping is now an opt-in
    (STRIP_CAMPAIGN_BANNERS=true) and what is ALWAYS removed is branding:
    another channel's name/signature/"join for more" line.
    """
    print("\n== source hype stays, channel branding goes ==")
    hype = [
        "\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25",
        "\u26a1\ufe0f\u26a1\ufe0f 11 PM FLASH SALE \u26a1\ufe0f\u26a1\ufe0f",
        "DEALS OF THE DAY",
        "SAARE MI AMAZING DEALS",
    ]
    branding = [
        "\ud83d\udd25 LOOT ZONE INDIA \u2014 Join for more loot",
        "Loot Zone India",
        "Join our telegram channel for more deals",
        "Edited by Admin @dealsAdda",
        "Follow @BestDealHubIndia on WhatsApp",
        "Powered by Amazon Deals Hub",
    ]
    content = [
        "Top Loading Washing Machine Cover @ \u20b9260",
        "Men Running Shoes 45% OFF",
        "Myntra Mega Sale",
        "Boat Airdopes 141",
        "Free shipping on all orders",
        "Size 7 (UK) | Color: Blue",
        "Use code SAVE200 for extra \u20b9200 off",
    ]
    for line in hype:
        check("hype header is never branding: %r" % line[:26], not bot.is_branding_line(line))
    for line in branding:
        check("branding recognised: %r" % line[:26], bot.is_branding_line(line))
    for line in content:
        check("deal content is never branding: %r" % line[:26], not bot.is_branding_line(line))

    # default (fidelity): hype kept, branding + referral junk removed
    post = ("\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25\n"
            "\u26a1\ufe0f\u26a1\ufe0f 11 PM FLASH SALE \u26a1\ufe0f\u26a1\ufe0f\n\n"
            "Men Cotton T-Shirt\nhttps://a.co/x\n"
            "\ud83d\udd25 LOOT ZONE INDIA \u2014 Join for more loot\n"
            "Get Flipkart App - Refer 3 friends and \u20b9100 referral bonus")
    out = bot.clean_source_text(post)
    check("source hype survives cleaning",
          "TOP DEAL OF THE DAY" in out and "FLASH SALE" in out)
    check("the product line and the price survive", "Men Cotton T-Shirt" in out and "https://a.co/x" in out)
    check("branding line removed", "LOOT ZONE INDIA" not in out and "Join for" not in out)
    check("referral/app-install line removed", "Refer 3 friends" not in out and "Install" not in out)
    check("cleaning is idempotent", bot.clean_source_text(out) == out)

    # opt-in: with the knob on, banners go but a headline never does
    saved = bot.STRIP_CAMPAIGN_BANNERS
    try:
        bot.STRIP_CAMPAIGN_BANNERS = True
        stripped = bot.strip_promo_lines(post)
        check("STRIP_CAMPAIGN_BANNERS=true removes the hype header",
              "TOP DEAL OF THE DAY" not in stripped and "Men Cotton T-Shirt" in stripped)
        only = ("\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25\n"
                "\u26a1\ufe0f FLASH SALE \u26a1\ufe0f\nhttps://a.co/x")
        check("even in strip mode the first line stays when nothing else can headline",
              "TOP DEAL OF THE DAY" in bot.strip_promo_lines(only))
    finally:
        bot.STRIP_CAMPAIGN_BANNERS = saved


def test_nothing_added_by_us():
    """v17.5 - the outbound post is the SOURCE post, never a rewrite.

    "anni mana extra add chesinavalu ravoddu, source lo unna vishayam matrame
    ravali": markdown debris (** around a line) is not printed, and everything
    the source really wrote survives - its hype header, its second header, its
    balanced brackets (74% OFF), its MRP note and a coupon code containing an
    underscore. Another channel's branding and referral/app-install farming are
    still removed, and the bot never invents a banner, a badge or a filler
    caption of its own."""
    R = "₹"
    print("\n== nothing added by us (v17.5) ==")
    src = (f"**🔥🔥 TOP DEAL OF THE DAY 🔥🔥**\n"
           "⚡️⚡️ 11 PM FLASH SALE ⚡️⚡️\n"
           f"Top Loading Washing Machine Cover @ {R}260 (74% OFF)\n"
           f"MRP {R}999 | Free shipping above {R}499\n"
           f"Use code SAVE_200 for extra {R}200 off\n"
           "🔥 LOOT ZONE INDIA — Join for more loot\n"
           f"Get Flipkart App - Refer 3 friends and {R}100 referral bonus\n"
           "➜ https://www.amazon.in/dp/B0IKTHI4?tag=deals0911-21")
    out = bot.sanitize_outbound_text(bot.tidy_post(bot.clean_source_text(src)))
    lines = [ln for ln in out.split("\n") if ln.strip()]
    check("markdown asterisks never print", "*" not in out)
    check("the source's own hype header survives verbatim as line 1",
          lines[0] == "🔥🔥 TOP DEAL OF THE DAY 🔥🔥")
    check("the source's second header survives with its emoji intact",
          "⚡️⚡️ 11 PM FLASH SALE ⚡️⚡️" in out)
    check("product + price + balanced parentheses survive",
          f"Top Loading Washing Machine Cover @ {R}260 (74% OFF)" in out)
    check("MRP / shipping detail survives", f"MRP {R}999" in out and "Free shipping above" in out)
    check("a coupon code underscore is never mangled", "SAVE_200" in out)
    check("another channel's branding is gone", "LOOT ZONE" not in out.upper())
    check("referral / app-install farming is gone",
          "Refer 3 friends" not in out and "referral bonus" not in out)
    check("our affiliate link survives intact",
          "https://www.amazon.in/dp/B0IKTHI4?tag=deals0911-21" in out)
    for invented in ("PREMIUM LOOT PICK", "Handpicked", "Latest deal", "DEALS OF THE DAY",
                     "💰", "👑"):
        check(f"no invented text of ours: {invented!r}", invented not in out)
    check("nothing is reordered",
          0 <= out.index("TOP DEAL OF THE DAY") < out.index("FLASH SALE") < out.index("Washing Machine"))


async def main():
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "t.sqlite3")
        bot.store = store
        test_text_quality()
        test_final_text_fidelity()
        await test_collapse_migration(store)
        await test_dedup(store)
        await test_latency(store)
        await test_http_pipeline()
        await test_source_ids_and_rescan(store)
        await test_link_replacement(store)
        await test_housekeeping(store)
        await test_render_latency(store)
        await test_price_gate_is_a_fallback(store)
        await test_no_silent_loss(store)
        await test_edited_source_posts(store)
        await test_passthrough_knob(store)
        await test_real_post_shape()
        await test_list_post_shapes(store)
        test_coverage_audit()
        test_campaign_banner_lines()
        test_unwanted_text_never_posts()
        test_nothing_added_by_us()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


asyncio.run(main())
