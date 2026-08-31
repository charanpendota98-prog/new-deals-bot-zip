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


async def main():
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "t.sqlite3")
        bot.store = store
        test_text_quality()
        await test_collapse_migration(store)
        await test_dedup(store)
        await test_latency(store)
        await test_http_pipeline()
        await test_source_ids_and_rescan(store)
        await test_link_replacement(store)
        await test_housekeeping(store)
        await test_render_latency(store)
        await test_price_gate_is_a_fallback(store)
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


asyncio.run(main())
