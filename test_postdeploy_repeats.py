"""Post-deploy repeats (2026-10-11): one queue posted up to three times to the
same targets, and policy skips were recorded as sends.

PR #14 (5a3e2b6) is deployed with production stopped. Observed in production:

  * queues for captions 212, 373 and 259 were each delivered THREE times to the
    same targets (POST_RETRIES = 3 is the tell);
  * Adidas queues 351498 / 351502 carry last_error='duplicate product' on
    deliveries rows whose status is 'sent'.

Root causes, each pinned below so it cannot come back:

  T1  deliver() kept the delivery CHECKPOINT inside the Telegram send-retry
      loop. A ledger write that threw (locked/busy database during a VACUUM,
      a checkpoint, or an overlapping writer right after a deploy) retried
      the TELEGRAM send instead of the write: one queue, three posts.
  T2  ledger writes on the send path had no retry of their own; transient
      writer contention must be absorbed by the write, never escalated into
      a re-send.
  T3  policy skips (duplicate product, review filters, undeclared-tag
      targets, ...) were stored as status='sent'. They are status='skipped'
      now: terminal like 'sent', never retried, never counted as a send.
  T4  both audits counted status='sent' as delivered, so skip rows read as
      real sends. They now count only empty-reason 'sent' rows as sends and
      report skips distinctly (including legacy skip-marked rows).
  T5  dedup keys on VERIFIED product identity only (a merchant id read from
      the verified merchant/affiliate URL). A strict-all post without
      trustworthy product identity is skipped as a unit - nothing invented,
      no merchant page fetched, no URL-hash identity, no orphaned name/price.
  T6  the identity gate is strict-all only; legacy rendering is untouched.
  T7  an edited job blocked from revive by skip rows alone can revive; only
      a real send blocks it.
  T8  the settle writes AFTER deliver() returns (delivery ok=True,
      mark_product_posted, premium completion) can still fail: the job must
      never requeue-into-resend. The post is already out, so the target is
      remembered and the next pass retries only the ledger write.

Run: python3 test_postdeploy_repeats.py   (from the repo root)
"""
import asyncio
import importlib.util
import os
import sqlite3
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

bot.STRICT_OUR_LINKS = False

R = "\u20b9"
FAILS: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def load_ops_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "ops" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Ent:
    def __init__(self, title: str):
        self.title = title
        self.id = abs(hash(title)) % 10**9


class SendCounter:
    """A Telegram client that counts exactly what a subscriber would see."""

    def __init__(self):
        self.sends: list[str] = []

    async def send_message(self, entity, content, **kwargs):
        self.sends.append(content)

        class _Res:
            id = len(self.sends)
        return _Res()

    async def send_file(self, entity, file, **kwargs):
        return await self.send_message(entity, kwargs.get("caption", ""))


# ---------------------------------------------------------------------------
# T1: a failed checkpoint must never re-send an already-sent chunk
# ---------------------------------------------------------------------------
def test_checkpoint_failure_never_resends():
    print("\n== T1: checkpoint failure never re-sends (the triple-post) ==")

    async def run():
        client = SendCounter()
        checkpoints: list[int] = []

        async def good_checkpoint(count: int):
            checkpoints.append(count)

        ok, error = await bot.deliver(client, Ent("t"), f"Deal {R}99", None,
                                      progress_callback=good_checkpoint)
        check("a normal send posts once and checkpoints once",
              ok and error == "" and len(client.sends) == 1 and checkpoints == [1],
              f"sends={len(client.sends)} checkpoints={checkpoints}")

        async def broken_checkpoint(count: int):
            raise sqlite3.OperationalError("database is locked")

        before = len(client.sends)
        ok, error = await bot.deliver(client, Ent("t"), f"Deal {R}99", None,
                                      progress_callback=broken_checkpoint)
        check("a throwing checkpoint still posts exactly once (never 3x)",
              ok and len(client.sends) - before == 1,
              f"sends={len(client.sends) - before} ok={ok} error={error!r}")

        # A two-chunk post with a dead ledger: both chunks go out, each once.
        long_text = ("word " * 1200).strip()
        assert len(bot.chunks(long_text, 4096)) == 2
        before = len(client.sends)
        ok, _ = await bot.deliver(client, Ent("t"), long_text, None,
                                  progress_callback=broken_checkpoint)
        check("a multi-chunk post with a dead ledger sends each chunk once",
              ok and len(client.sends) - before == 2,
              f"sends={len(client.sends) - before}")
    asyncio.run(run())


# ---------------------------------------------------------------------------
# T2: transient ledger contention is absorbed by the write itself
# ---------------------------------------------------------------------------
def test_ledger_writes_ride_out_contention():
    print("\n== T2: busy-locked commits retry instead of failing the send ==")

    class FlakyConn:
        def __init__(self, real, failures: int, exc: Exception):
            self._real = real
            self._failures = failures
            self._exc = exc
            self.calls = 0

        def commit(self):
            self.calls += 1
            if self.calls <= self._failures:
                raise self._exc
            return self._real.commit()

        def __getattr__(self, name):
            return getattr(self._real, name)

    async def run():
        store = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t2.sqlite3")))
        try:
            store.conn.execute(
                "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) "
                "VALUES(?,?,?,?,?,?)", (7, 7, "s", time.time(), 1, "7"))
            qid = store.conn.execute("SELECT id FROM queue").fetchone()[0]
            store.conn.execute("INSERT INTO deliveries(queue_id,target) VALUES(?,?)",
                               (qid, "LootZoneIndia11"))
            store.conn.commit()
            real = store.conn
            store.conn = FlakyConn(real, 2, sqlite3.OperationalError("database is locked"))
            try:
                await store.delivery(qid, "LootZoneIndia11", True, "")
                await store.set_delivery_progress(qid, "LootZoneIndia11", 1)
            finally:
                store.conn = real
            row = real.execute("SELECT status,attempts,chunks_sent FROM deliveries").fetchone()
            check("two locked commits still record the send",
                  tuple(row) == ("sent", 1, 1), str(tuple(row)))

            store.conn = FlakyConn(real, 99, sqlite3.OperationalError("boom: no such thing"))
            try:
                raised = False
                try:
                    await store.delivery(qid, "LootZoneIndia11", True, "")
                except sqlite3.OperationalError:
                    raised = True
            finally:
                store.conn = real
            check("a non-busy ledger error still raises (never silently swallowed)", raised)
        finally:
            store.conn.close()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# T3: a duplicate skip is status='skipped' - terminal, unretried, unsent
# ---------------------------------------------------------------------------
class PassthroughAffiliate:
    """Nothing monetizable: the merchant page passes through clean (legacy)."""

    def __init__(self, store):
        self._store = store

    async def resolve(self, url):
        return url

    async def convert(self, source, multi_link, resolved=None):
        return None

    async def cache_link(self, *args, **kwargs):
        return None

    async def shorten_long_urls_in_text(self, text):
        return text

    async def link_not_broken(self, url):
        return True

    async def rendered_links_not_broken(self, text):
        return True


class JobClient(SendCounter):
    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    async def get_messages(self, chat_id, ids=None):
        return self.messages.get((chat_id, ids))


class JobMsg:
    def __init__(self, text, msg_id):
        self.text = self.message = text
        self.id = msg_id
        self.media = None
        self.entities = []
        self.reply_to = None
        self.reply_markup = None


def test_duplicate_skip_is_not_a_send():
    print("\n== T3: duplicate skips are 'skipped', never 'sent', never retried ==")
    first = ("\U0001F525 PRICE DROP \U0001F525\nLG 1.5 Ton 5 Star Inverter Split AC\n"
             f"Now {R}36,990 (MRP {R}74,990, 50% off)\n"
             "Use code: LGAC1500 | No cost EMI\nBuy \U0001F449 https://www.croma.com/ac-detail-a1?size=M")
    repeat = (f"LG 1.5 Ton 5 Star Inverter Split AC\n{R}36,990 (50% off)\n"
              "Free installation this week\nhttps://www.croma.com/ac-detail-a2")

    async def run():
        store = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t3.sqlite3")))
        old_store = bot.store
        bot.store = store
        try:
            await store.enqueue(-1009201, 201, "dealsvelocity", first, False)
            await store.enqueue(-1009202, 202, "dealsvelocity", repeat, False)
            client = JobClient({(-1009201, 201): JobMsg(first, 201),
                                (-1009202, 202): JobMsg(repeat, 202)})
            tmap = {name: Ent(name) for name in
                    set(bot.SOURCE_TO_TARGETS["dealsvelocity"]) | {"LootZoneIndia11"}}
            affiliate = PassthroughAffiliate(store)
            row1 = await store.claim_job()
            await bot.process_job(client, affiliate, tmap, row1)
            sends_after_first = len(client.sends)
            check("the first-claimed copy posts (control)", sends_after_first >= 1,
                  f"sends={sends_after_first}")
            first_targets = [r[0] for r in store.conn.execute(
                "SELECT target FROM deliveries WHERE queue_id=? ORDER BY target",
                (row1["id"],))]
            check("first-copy deliveries are real sends",
                  all(r[0] == "sent" and (r[1] or "") == "" for r in store.conn.execute(
                      "SELECT status,last_error FROM deliveries WHERE queue_id=?", (row1["id"],))),
                  str(first_targets))
            row2 = await store.claim_job()
            await bot.process_job(client, affiliate, tmap, row2)
            check("the same-product copy sends nothing more",
                  len(client.sends) == sends_after_first,
                  f"sends grew {sends_after_first} -> {len(client.sends)}")
            skipped = [tuple(r) for r in store.conn.execute(
                "SELECT target,status,last_error FROM deliveries WHERE queue_id=? ORDER BY target",
                (row2["id"],))]
            check("every same-product delivery is status='skipped' with the reason kept",
                  bool(skipped) and all(status == "skipped" and err == "duplicate product"
                                        for _, status, err in skipped),
                  str(skipped))
            check("a skipped target is never pending again",
                  await store.pending_targets(row2["id"]) == [],
                  str(await store.pending_targets(row2["id"])))
            real = store.conn.execute(
                "SELECT count(*) FROM deliveries WHERE queue_id=? AND status='sent' "
                "AND COALESCE(last_error,'')=''", (row2["id"],)).fetchone()[0]
            check("the real-send predicate counts zero sends for the repeat", real == 0, str(real))
            status2 = store.conn.execute(
                "SELECT status FROM queue WHERE id=?", (row2["id"],)).fetchone()[0]
            check("the same-product job closes done (no requeue, no stuck pending)",
                  status2 == "done", status2)
        finally:
            bot.store = old_store
            store.conn.close()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# T4: the audits tell duplicate skips apart from real sends
# ---------------------------------------------------------------------------
def test_audits_distinguish_skips_from_sends():
    print("\n== T4: quality + coverage audits separate skips from sends ==")
    qa = load_ops_module("quality_audit")
    ca = load_ops_module("coverage_audit")
    db_path = Path(tempfile.mktemp(suffix="-repeats-t4.sqlite3"))
    store = bot.Store(db_path)
    now = time.time()
    publisher = "5478322"
    owned_short = "https://bit.ly/repeats-owned-1"
    try:
        post = f"Adidas Ultraboost Shoes\n{R}3,499 (65% off)\n{owned_short}"
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (1, -101, 1, "deals", "done", 1, "", now - 60, "-101", post, '["LootZoneIndia11"]'))
        store.conn.execute("INSERT INTO deliveries VALUES(1,'LootZoneIndia11','sent',1,1,'')")
        # Legacy Adidas shape: an older build's skip recorded as a send.
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (2, -102, 2, "deals", "done", 1, "", now - 120, "-102", post, '["LootZoneIndia11"]'))
        store.conn.execute("INSERT INTO deliveries VALUES(2,'LootZoneIndia11','sent',1,0,'duplicate product')")
        # New shape: a textless job concluded wholly by a policy skip.
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (3, -103, 3, "deals", "done", 1, "", now - 180, "-103", None, '["LootZoneIndia11"]'))
        store.conn.execute("INSERT INTO deliveries VALUES(3,'LootZoneIndia11','skipped',1,0,'duplicate product')")
        # Control: genuinely lost (failed, nothing sent, no policy reason).
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (4, -104, 4, "deals", "failed", 3, "boom exploded", now - 240, "-104", None, '[]'))
        # Control: a closed job with a target still open is still a gap.
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (5, -105, 5, "deals", "done", 1, "", now - 300, "-105", None, '["LootZoneIndia11"]'))
        store.conn.execute("INSERT INTO deliveries VALUES(5,'LootZoneIndia11','pending',1,0,'')")
        store.conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)",
                           ("https://www.flipkart.com/x/p/itmR1", owned_short,
                            "https://www.flipkart.com/x/p/itmR1", "PID:R1", now))
        store.conn.execute(
            "INSERT INTO owned_link_proofs(affiliate_url,destination_url,proof_type,publisher_id,created_at)"
            " VALUES(?,?,?,?,?)",
            (owned_short, "https://ekaro.in/enkr-repeats-1", "earnkaro_api", publisher, now))
        store.conn.commit()

        findings, _posts, _links, delivery = qa.audit(store.conn, 50, "mama086-21", publisher)
        kinds = {}
        for item in findings:
            kinds.setdefault(item["kind"], []).append(item)
        check("real_sends counts only the Telegram send (1, not 2)",
              delivery["real_sends"] == 1, str(delivery))
        check("both skip shapes are reported as skips (legacy + new)",
              delivery["skipped"] == 2, str(delivery))
        check("the skip reason names the duplicate",
              any("duplicate product" in reason for reason in delivery["skip_reasons"]),
              str(delivery["skip_reasons"]))
        check("a skip-only queue is never called a duplicate SEND",
              not [i for i in kinds.get("DUPLICATE", []) + kinds.get("SAME-PRODUCT", [])
                   if i.get("queue_id") in (2, 3)],
              str([i for k in ("DUPLICATE", "SAME-PRODUCT") for i in kinds.get(k, [])]))
        coverage_ids = {i.get("queue_id") for i in kinds.get("COVERAGE", [])}
        check("a skip-concluded job is not 'lost'", 3 not in coverage_ids, str(coverage_ids))
        check("a genuinely lost job still is", 4 in coverage_ids, str(coverage_ids))
        check("an open target on a closed job is still flagged", 5 in coverage_ids,
              str(coverage_ids))
    finally:
        store.conn.close()

    report = ca.audit(db_path, 12.0)
    states = {}
    for row in report["lost"]:
        states[row["id"]] = "lost"
    # Re-derive per-queue states the way the report buckets them.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sent = {r["queue_id"] for r in conn.execute(
        "SELECT queue_id FROM deliveries WHERE status='sent' AND COALESCE(last_error,'')=''")}
    conn.close()
    check("coverage: the real send reads posted", 1 in sent and 1 not in states, str(states))
    per_source = report["per_source"].get("deals", {})
    check("coverage: legacy + new skip shapes read as dedup, not posted/lost",
          per_source.get("dedup", 0) == 2 and per_source.get("lost", 0) == 1, str(per_source))
    check("coverage: the lost control is the only LOST row",
          sorted(states) == [4], str(sorted(states)))


# ---------------------------------------------------------------------------
# T5 + T6: strict-all requires verified product identity; legacy does not
# ---------------------------------------------------------------------------
class MapAffiliate:
    def __init__(self, resolve_map, convert_map):
        self.resolve_map = resolve_map
        self.convert_map = convert_map

    async def resolve(self, url):
        return self.resolve_map.get(url, url)

    async def convert(self, source, multi_link, resolved=None):
        return self.convert_map.get(source)

    async def shorten_long_urls_in_text(self, rendered):
        return rendered

    async def link_not_broken(self, url):
        return True

    async def rendered_links_not_broken(self, text):
        return True

    async def cache_link(self, *args, **kwargs):
        return None


async def _queue_row(store, text, msg_id):
    store.conn.execute(
        "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key) VALUES(?,?,?,?,?,?)",
        (111, msg_id, "some_source", time.time(), bot.classify_priority(text, False),
         str(bot.raw_chat_id(111))))
    store.conn.commit()
    return store.conn.execute("SELECT * FROM queue ORDER BY id DESC LIMIT 1").fetchone()


def test_strict_requires_verified_identity():
    print("\n== T5+T6: no trustworthy identity -> strict skips, legacy posts ==")
    opaque_source = "https://bitli.in/repeats-opaque"
    opaque_resolved = "https://www.flipkart.com/mega-sale/offer"
    real_source = "https://bitli.in/repeats-real"
    real_resolved = "https://www.flipkart.com/boat-airdopes-141/p/itmABCDEF123?pid=XYZ987654"
    assert bot.verified_product_id(opaque_resolved) is None
    assert bot.verified_product_id(real_resolved) == "PID:XYZ987654"

    async def run():
        store = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t5.sqlite3")))
        saved = (bot.store, bot.STRICT_OUR_LINKS, bot.OUR_TAG, bot.OUR_EK_ID)
        bot.store = store
        try:
            # --- strict: opaque identity is skipped as a unit ---
            bot.STRICT_OUR_LINKS, bot.OUR_TAG, bot.OUR_EK_ID = True, "mama086-21", "5478322"
            row = await _queue_row(store, f"Sale picks {R}499\n{opaque_source}", 501)
            client = JobClient({(111, 501): JobMsg(f"Sale picks {R}499\n{opaque_source}", 501)})
            affiliate = MapAffiliate(
                {opaque_source: opaque_resolved},
                {opaque_source: bot.LinkResult(opaque_source, opaque_resolved,
                                               "https://ekaro.in/enkr-opaque55", "URL:opaque55")})
            try:
                await bot.render_job(client, affiliate, row)
                check("strict skips a post with no trustworthy product identity", False,
                      "rendered without identity")
            except bot.PermanentSkip as exc:
                check("strict skips a post with no trustworthy product identity",
                      "no trustworthy product identity" in str(exc), str(exc))
            except Exception as exc:  # noqa: BLE001 - must be the identity skip, nothing else
                check("strict skips a post with no trustworthy product identity", False,
                      f"{type(exc).__name__}: {exc}")

            # --- strict: verified identity renders with its own link ---
            bot.STRICT_OUR_LINKS, bot.OUR_TAG, bot.OUR_EK_ID = True, "mama086-21", "5478322"
            good_text = f"boAt Airdopes 141 TWS Earbuds {R}999 60% OFF\n{real_source}"
            row = await _queue_row(store, good_text, 502)
            client = JobClient({(111, 502): JobMsg(good_text, 502)})
            affiliate = MapAffiliate(
                {real_source: real_resolved},
                {real_source: bot.LinkResult(real_source, real_resolved,
                                             "https://ekaro.in/enkr-real77", "PID:XYZ987654")})
            _msg, rendered, _price = await bot.render_job(client, affiliate, row)
            check("strict renders the verified product with its own link",
                  "https://ekaro.in/enkr-real77" in rendered and real_source not in rendered,
                  rendered[:160])

            # --- strict: a stale URL-hash cache key upgrades to the verified id ---
            legacy_source = "https://www.flipkart.com/boat-bassheads/p/itmZZZ111?pid=PID999AAA"
            await store.cache_link(legacy_source, "https://ekaro.in/enkr-cache-up",
                                   legacy_source, "URL:legacy-stale")
            await store.remember_owned_link_proof("https://ekaro.in/enkr-cache-up",
                                                  "https://ekaro.in/enkr-cache-up", "earnkaro_api")
            real_affiliate = bot.AffiliateClient(None)
            upgraded = await real_affiliate.convert(legacy_source, False)
            check("a strict cache hit re-keys on the verified merchant id",
                  upgraded is not None and upgraded.deal_key == "PID:PID999AAA",
                  str(upgraded.deal_key if upgraded else None))

            # --- legacy: the same opaque post still renders (gate is strict-only) ---
            bot.STRICT_OUR_LINKS = False
            row = await _queue_row(store, f"Sale picks {R}499\n{opaque_source}", 503)
            client = JobClient({(111, 503): JobMsg(f"Sale picks {R}499\n{opaque_source}", 503)})
            affiliate = MapAffiliate({opaque_source: opaque_resolved}, {})
            _msg, rendered, _price = await bot.render_job(client, affiliate, row)
            check("legacy mode still renders the opaque merchant page",
                  "flipkart.com/mega-sale/offer" in rendered, rendered[:160])
        finally:
            bot.store, bot.STRICT_OUR_LINKS, bot.OUR_TAG, bot.OUR_EK_ID = saved
            store.conn.close()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# T7: only a real send blocks an edit-revive
# ---------------------------------------------------------------------------
def test_revive_ignores_skip_rows():
    print("\n== T7: skip rows do not block an edit revive; real sends do ==")

    async def run():
        store = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t7.sqlite3")))
        try:
            for qid, msg_id, status, error in ((11, 111, "skipped", "duplicate product"),
                                               (12, 112, "sent", "")):
                store.conn.execute(
                    "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,"
                    "created_at,chat_key) VALUES(?,?,?,?,?,?,?,?,?)",
                    (qid, -200, msg_id, "s", "done", 1, "", time.time(), "-200"))
                store.conn.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?)",
                                   (qid, "LootZoneIndia11", status, 1, 0, error))
            store.conn.commit()
            revived = await store.revive_edited_job("-200", 111)
            blocked = await store.revive_edited_job("-200", 112)
            check("a skip-only job revives on edit", revived == "revived", str(revived))
            check("a really-sent job does not", blocked is None, str(blocked))
        finally:
            store.conn.close()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# T8: a settle write that fails AFTER the send never causes a re-send
# ---------------------------------------------------------------------------
def test_failed_settle_never_resends():
    print("\n== T8: ledger failure after the send retries the write, not the post ==")
    text = (f"boAt Stone 350 Speaker {R}1,299 (35% off)\nhttps://www.croma.com/sp-detail-t8")

    async def run():
        store = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t8.sqlite3")))
        old_store, old_delay = bot.store, bot.UNRECORDED_SETTLE_RETRY_DELAY
        bot.store = store
        bot.SETTLED_BUT_UNRECORDED.clear()
        bot.UNRECORDED_SETTLE_RETRY_DELAY = 0
        real_delivery = store.delivery
        real_mark = store.mark_product_posted

        async def broken_delivery(queue_id, target, ok, error=""):
            if ok:
                raise sqlite3.OperationalError("disk I/O error")
            return await real_delivery(queue_id, target, ok, error)

        async def broken_mark(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        try:
            await store.enqueue(-1009301, 301, "dealsvelocity", text, False)
            client = JobClient({(-1009301, 301): JobMsg(text, 301)})
            tmap = {name: Ent(name) for name in
                    set(bot.SOURCE_TO_TARGETS["dealsvelocity"]) | {"LootZoneIndia11"}}
            affiliate = PassthroughAffiliate(store)
            row = await store.claim_job()
            qid = row["id"]

            store.delivery = broken_delivery
            store.mark_product_posted = broken_mark
            await bot.process_job(client, affiliate, tmap, row)  # must not raise
            routed = store.conn.execute(
                "SELECT count(*) FROM deliveries WHERE queue_id=?", (qid,)).fetchone()[0]
            check("the post goes out once per target even with a dead settle ledger",
                  routed >= 1 and len(client.sends) == routed,
                  f"sends={len(client.sends)} routed={routed}")
            check("every unsettled target is remembered for write-only retry",
                  len(bot.SETTLED_BUT_UNRECORDED) == routed,
                  str(bot.SETTLED_BUT_UNRECORDED))
            status = store.conn.execute(
                "SELECT status FROM queue WHERE id=?", (qid,)).fetchone()[0]
            check("the job requeues (the ledger still owes the record)", status == "pending",
                  status)

            row = store.conn.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone()
            await bot.process_job(client, affiliate, tmap, row)
            check("the requeue retries the write, never the send",
                  len(client.sends) == routed, f"sends={len(client.sends)}")

            store.delivery = real_delivery
            store.mark_product_posted = real_mark
            row = store.conn.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone()
            await bot.process_job(client, affiliate, tmap, row)
            check("a healed ledger records the send without re-posting",
                  len(client.sends) == routed, f"sends={len(client.sends)}")
            sent_rows = store.conn.execute(
                "SELECT count(*) FROM deliveries WHERE queue_id=? AND status='sent'",
                (qid,)).fetchone()[0]
            check("all targets recorded sent after healing", sent_rows == routed, str(sent_rows))
            status = store.conn.execute(
                "SELECT status FROM queue WHERE id=?", (qid,)).fetchone()[0]
            check("the job closes done after healing", status == "done", status)
            check("the unrecorded set drains once the write lands",
                  not bot.SETTLED_BUT_UNRECORDED, str(bot.SETTLED_BUT_UNRECORDED))
        finally:
            bot.store = old_store
            bot.UNRECORDED_SETTLE_RETRY_DELAY = old_delay
            bot.SETTLED_BUT_UNRECORDED.clear()
            store.conn.close()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# T9: "better" must be meaningful - token drops and math-noise never repost
# ---------------------------------------------------------------------------
def test_better_copy_bar_is_meaningful():
    print("\n== T9: only a real drop reposts an already-posted product ==")

    async def run():
        store = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t9.sqlite3")))
        try:
            sig = "SIG:test-better-bar-9"
            await store.mark_product_posted("LootZoneIndia11", sig, 36990, 50)

            async def skipped(price, discount):
                skip, _ = await store.product_already_posted(
                    "LootZoneIndia11", sig, price, discount)
                return skip

            # Inside the 1h cool-down EVERYTHING waits - even a real drop.
            check("cool-down: same figures wait", await skipped(36990, 50))
            check("cool-down: even a real 9.5% drop waits", await skipped(33490, 54))
            # Past the cool-down the meaningful-better bar applies.
            store.conn.execute("UPDATE posted_products SET posted_at=posted_at-7200")
            store.conn.commit()
            check("same product, same figures: skipped", await skipped(36990, 50))
            check("a one-rupee drop is not news: skipped", await skipped(36989, 50))
            check("a 1.3% caption wobble is not news: skipped", await skipped(36500, 50))
            check("a real 9.5% drop still posts", not await skipped(33490, 54))
            check("same price, bigger claimed percent (MRP math): skipped",
                  await skipped(36990, 65))
            check("paying MORE for a bigger percent is never news: skipped",
                  await skipped(37990, 70))
            check("a real drop with a deeper discount still posts",
                  not await skipped(33490, 65))
            other, _ = await store.product_already_posted("PowerLoots1", sig, 36990, 50)
            check("a channel that never carried it still posts", not other)
            await store.mark_product_posted("LootZoneIndia11", "SIG:noprice-9", None, None)
            store.conn.execute("UPDATE posted_products SET posted_at=posted_at-7200 "
                               "WHERE signature='SIG:noprice-9'")
            store.conn.commit()
            priced, _ = await store.product_already_posted(
                "LootZoneIndia11", "SIG:noprice-9", 999, 10)
            check("a price where none was known is still news", not priced)
        finally:
            store.conn.close()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# T10: exact link keys catch the same product in different words
# ---------------------------------------------------------------------------
def test_link_keys_catch_reworded_repeats():
    print("\n== T10: a repeat whose headline missed still shares a link key ==")

    async def run():
        store = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t10.sqlite3")))
        try:
            pid_url = "https://www.flipkart.com/boat-airdopes-141/p/itmABCDEF123?pid=XYZ987654"
            pid_key = "PID:XYZ987654"
            assert bot.product_key(pid_url) == pid_key
            keys = await store.text_identity_keys(
                f"boAt Airdopes 141 {R}999\n{pid_url}\n{bot.OUR_FOLDER_LINK}\n"
                "https://wa.me/?text=share-this-deal")
            check("a merchant PID behind the URL becomes an identity key",
                  pid_key in keys, str(keys))
            check("share/invite hosts are never identity keys",
                  not any("t.me" in key or "wa.me" in key for key in keys)
                  and keys == [pid_key], str(keys))

            # ANY key match skips: the headline missed, the link key caught it.
            await store.mark_product_posted("LootZoneIndia11", "SIG:headline-A-10",
                                            999, 60, [pid_key])
            skip, why = await store.product_already_posted(
                "LootZoneIndia11", ["SIG:headline-B-10", pid_key], 999, 60)
            check("a different headline with the same link key is skipped",
                  skip and "cool-down" in why, why)
            post, _ = await store.product_already_posted(
                "LootZoneIndia11", ["SIG:headline-B-10"], 999, 60)
            check("without the shared key it would post (the key did it)", not post)

            # End to end: a stale-name repost of the same product link waits.
            caption_a = f"boAt Airdopes 141 TWS Earbuds {R}999 60% OFF\n{pid_url}"
            caption_b = (f"boAt Rockerz 255 Pro+ Neckband {R}999 60% OFF\n{pid_url}\n"
                         "Price drop alert, grab fast!")
            sig_a, sig_b = bot.product_signature(caption_a), bot.product_signature(caption_b)
            check("the two headlines read as different products (test is meaningful)",
                  sig_a and sig_b and sig_a != sig_b, f"{sig_a} vs {sig_b}")
            old_store = bot.store
            live = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t10b.sqlite3")))
            bot.store = live
            try:
                # Raw inserts: enqueue() would merge the two same-product rows
                # via swap_in_better_copy before the send-time guard is reached.
                # Two rows also arrive in real life (second lands after the
                # first is claimed), and that is the layer pinned here.
                await _queue_row(live, caption_a, 401)
                row_a = live.conn.execute("SELECT * FROM queue ORDER BY id DESC LIMIT 1").fetchone()
                live.conn.execute("UPDATE queue SET chat_id=?, msg_id=? WHERE id=?",
                                  (-1009401, 401, row_a["id"]))
                await _queue_row(live, caption_b, 402)
                row_b = live.conn.execute("SELECT * FROM queue ORDER BY id DESC LIMIT 1").fetchone()
                live.conn.execute("UPDATE queue SET chat_id=?, msg_id=? WHERE id=?",
                                  (-1009402, 402, row_b["id"]))
                live.conn.commit()
                client = JobClient({(-1009401, 401): JobMsg(caption_a, 401),
                                    (-1009402, 402): JobMsg(caption_b, 402)})
                tmap = {name: Ent(name) for name in
                        set(bot.SOURCE_TO_TARGETS["dealsvelocity"]) | {"LootZoneIndia11"}}
                affiliate = PassthroughAffiliate(live)
                await bot.process_job(client, affiliate, tmap, await live.claim_job())
                first_sends = len(client.sends)
                await bot.process_job(client, affiliate, tmap, await live.claim_job())
                check("the same-link repost sends nothing more",
                      first_sends >= 1 and len(client.sends) == first_sends,
                      f"sends={len(client.sends)}")
            finally:
                bot.store = old_store
                live.conn.close()

            # Past the 24h window a genuine re-offer posts again.
            store.conn.execute("UPDATE posted_products SET posted_at=posted_at-90000")
            store.conn.commit()
            post, _ = await store.product_already_posted(
                "LootZoneIndia11", ["SIG:headline-B-10", pid_key], 999, 60)
            check("past the window the re-offer posts again", not post)
        finally:
            store.conn.close()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# T11: strict pre-send link guard - their links never post
# ---------------------------------------------------------------------------
def test_presend_link_guard():
    print("\n== T11: final text re-verified link by link before a strict send ==")

    async def run():
        store = bot.Store(Path(tempfile.mktemp(suffix="-repeats-t11.sqlite3")))
        saved = (bot.store, bot.STRICT_OUR_LINKS, bot.OUR_TAG, bot.OUR_EK_ID)
        bot.store, bot.STRICT_OUR_LINKS, bot.OUR_TAG, bot.OUR_EK_ID = (
            store, True, "mama086-21", "5478322")
        try:
            owned_short = "https://ekaro.in/enkr-presend-11"
            resolved = "https://www.flipkart.com/boat-airdopes-141/p/itmABCDEF123?pid=XYZ987654"
            now = time.time()
            store.conn.execute(
                "INSERT INTO link_cache VALUES(?,?,?,?,?)",
                ("https://bitli.in/presend-src-11", owned_short, resolved,
                 "PID:XYZ987654", now))
            store.conn.execute(
                "INSERT INTO owned_link_proofs(affiliate_url,destination_url,proof_type,"
                "publisher_id,created_at) VALUES(?,?,?,?,?)",
                (owned_short, owned_short, "earnkaro_api", "5478322", now))
            store.conn.commit()

            ok, _ = await store.presend_links_verified(f"Deal {R}999\n{owned_short}")
            check("our proven short link passes", ok)
            ok, _ = await store.presend_links_verified(
                f"Deal {R}999\nhttps://www.amazon.in/dp/B0GUARD0001?tag=mama086-21")
            check("our tagged Amazon link passes", ok)
            ok, _ = await store.presend_links_verified(
                f"Deal {R}999\n{owned_short}\n{bot.OUR_FOLDER_LINK}")
            check("our own family link alongside passes", ok)
            ok, bad = await store.presend_links_verified(
                f"Deal {R}999\n{owned_short}\nhttps://bitli.in/their-link-11")
            check("a source short link stops the post",
                  not ok and bad == "https://bitli.in/their-link-11", bad)
            ok, bad = await store.presend_links_verified(
                "Deal\nhttps://www.amazon.in/dp/B0GUARD0001?tag=someoneelse-21")
            check("a foreign Amazon tag stops the post", not ok, bad)
            ok, bad = await store.presend_links_verified(f"Deal {R}999\n{resolved}")
            check("a clean merchant page with no proof stops the post", not ok, bad)
            ok, _ = await store.presend_links_verified("Just words, no links at all")
            check("linkless text has nothing to leak: passes", ok)

            # Wiring: a guard failure skips the target instead of posting.
            text = f"boAt Airdopes 141 TWS Earbuds {R}999 60% OFF\nhttps://bitli.in/presend-src-11"
            row = await _queue_row(store, text, 511)
            client = JobClient({(111, 511): JobMsg(text, 511)})
            tmap = {"LootZoneIndia11": Ent("LootZoneIndia11"),
                    "PowerLoots1": Ent("PowerLoots1")}
            affiliate = MapAffiliate(
                {"https://bitli.in/presend-src-11": resolved},
                {"https://bitli.in/presend-src-11": bot.LinkResult(
                    "https://bitli.in/presend-src-11", resolved, owned_short, "PID:XYZ987654")})
            real_guard = store.presend_links_verified

            async def failing_guard(_text):
                return False, "https://evil.invalid/x"

            store.presend_links_verified = failing_guard
            await bot.process_job(client, affiliate, tmap, row)
            check("a guard failure posts nothing",
                  len(client.sends) == 0, f"sends={len(client.sends)}")
            states = [tuple(r) for r in store.conn.execute(
                "SELECT status,last_error FROM deliveries WHERE queue_id=?", (row["id"],))]
            check("the guarded target is recorded skipped with the reason",
                  bool(states) and all(status == "skipped" and "pre-send" in (err or "")
                                        for status, err in states), str(states))
            store.presend_links_verified = real_guard
        finally:
            bot.store, bot.STRICT_OUR_LINKS, bot.OUR_TAG, bot.OUR_EK_ID = saved
            store.conn.close()
    asyncio.run(run())


if __name__ == "__main__":
    test_checkpoint_failure_never_resends()
    test_ledger_writes_ride_out_contention()
    test_duplicate_skip_is_not_a_send()
    test_audits_distinguish_skips_from_sends()
    test_strict_requires_verified_identity()
    test_revive_ignores_skip_rows()
    test_failed_settle_never_resends()
    test_better_copy_bar_is_meaningful()
    test_link_keys_catch_reworded_repeats()
    test_presend_link_guard()
    print("\n" + ("POSTDEPLOY REPEAT TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_postdeploy_repeats: all checks PASS"))
    sys.exit(1 if FAILS else 0)
