"""Offline lost-response, restart, simultaneous-worker and identity regressions."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent
IMPORT_TMP = tempfile.TemporaryDirectory(prefix="delivery-fence-import-")
os.environ.update(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="offline", EARNKARO_API_KEY="offline",
                  BOT_DB_PATH=f"{IMPORT_TMP.name}/import.db", BOT_LOG_DIR=f"{IMPORT_TMP.name}/logs",
                  BOT_MEDIA_DIR=f"{IMPORT_TMP.name}/media", TELEGRAM_SESSION=f"{IMPORT_TMP.name}/session")
sys.path.insert(0, str(ROOT / "bestgaa"))
import main_bot_new as bot  # noqa: E402
from test_postdeploy_repeats import Ent, JobClient, PassthroughAffiliate, SendCounter  # noqa: E402


class Fences(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="delivery-fences-")
        self.path = Path(self.tmp.name) / "test.db"
        self.store = bot.Store(self.path)
        self.old_store = bot.store
        bot.store = self.store
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        bot.store = self.old_store
        bot.SETTLED_BUT_UNRECORDED.clear()
        self.store.conn.close()
        self.tmp.cleanup()

    async def row(self, n=1, text=None, targets=("FenceShelfA",)):
        text = text or "Acme Model X34 wireless speaker ₹999\nhttps://www.croma.com/product/p/FENCE123"
        self.store.conn.execute(
            "INSERT INTO queue(chat_id,msg_id,source,created_at,chat_key,source_text) VALUES(?,?,?,?,?,?)",
            (111, n, "fence_test_source", time.time(), "111", text))
        self.store.conn.commit()
        qid = self.store.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for url in bot.URL_RE.findall(text):
            await self.store.cache_link(url, url, url, bot.product_key(url))
        await self.store.save_render(qid, text, list(targets), [f"fixture:{n}"])
        return self.store.conn.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone()

    def attempt(self, qid=1, target="FenceShelfA", store=None):
        async def callback(index, digest, state):
            return await (store or self.store).send_attempt(qid, target, index, digest, state)
        return callback

    async def test_lost_response_is_not_three_sends_and_survives_restart(self):
        class AcceptedButLost(SendCounter):
            async def send_message(self, *args, **kwargs):
                await super().send_message(*args, **kwargs)
                raise TimeoutError("remote accepted; reply lost")
        row = await self.row()
        client = AcceptedButLost()
        with self.assertRaises(bot.AmbiguousDelivery):
            await bot.deliver(client, Ent("A"), "deal", None, attempt_callback=self.attempt(row['id']))
        self.assertEqual(len(client.sends), 1)
        self.store.conn.close()
        self.store = bot.Store(self.path)
        bot.store = self.store
        with self.assertRaises(bot.AmbiguousDelivery):
            await bot.deliver(client, Ent("A"), "deal", None, attempt_callback=self.attempt(row['id']))
        self.assertEqual(len(client.sends), 1)

    async def test_crash_after_intent_before_receipt_holds_without_network(self):
        await self.row()
        await self.store.send_attempt(1, "FenceShelfA", 0, "digest", "started")
        other = bot.Store(self.path)
        try:
            with self.assertRaises(bot.AmbiguousDelivery):
                await other.send_attempt(1, "FenceShelfA", 0, "digest", "started")
        finally:
            other.conn.close()

    async def test_failed_intent_write_prevents_network(self):
        client = SendCounter()
        async def unavailable(*args):
            raise OSError("disk full")
        with self.assertRaises(OSError):
            await bot.deliver(client, Ent("A"), "deal", None, attempt_callback=unavailable)
        self.assertEqual(client.sends, [])

    async def test_confirmed_receipt_resumes_without_resending(self):
        await self.row()
        client = SendCounter()
        self.assertTrue((await bot.deliver(client, Ent("A"), "deal", None,
                                          attempt_callback=self.attempt()))[0])
        other = bot.Store(self.path)
        try:
            self.assertEqual(await other.delivery_progress(1, "FenceShelfA"), 1)
            self.assertTrue((await bot.deliver(client, Ent("A"), "deal", None,
                                              attempt_callback=self.attempt(store=other)))[0])
        finally:
            other.conn.close()
        self.assertEqual(len(client.sends), 1)
        with self.assertRaises(bot.AmbiguousDelivery):
            await bot.deliver(client, Ent("A"), "changed deal", None, attempt_callback=self.attempt())

    async def test_rejection_is_retryable(self):
        await self.row()
        client = SendCounter()
        with patch.object(client, "send_message", AsyncMock(side_effect=bot.ChatAdminRequiredError(None))):
            self.assertFalse((await bot.deliver(client, Ent("A"), "deal", None,
                                               attempt_callback=self.attempt()))[0])
        self.assertTrue((await bot.deliver(client, Ent("A"), "deal", None,
                                          attempt_callback=self.attempt()))[0])
        self.assertEqual(len(client.sends), 1)

    async def test_aliases_and_entire_roundup_are_checked(self):
        # Old host-shaped keys must match after upgrading, without DB migration.
        self.store.conn.execute("INSERT INTO posted_products VALUES(?,?,?,?,?)",
                                ("PID:www.meesho.com:abc123", "A", 999, 30, time.time()))
        self.store.conn.commit()
        self.assertTrue((await self.store.product_already_posted("A", ["PID:meesho.com:abc123"], 1, 99))[0])
        self.assertFalse((await self.store.product_already_posted("B", ["PID:m.meesho.com:abc123"], 1, 99))[0])
        self.assertFalse((await self.store.product_already_posted("A", ["PID:other.com:abc123"], 1, 99))[0])
        self.assertFalse((await self.store.product_already_posted("A", ["PID:meesho.com:def456"], 1, 99))[0])
        urls = [f"https://www.amazon.in/dp/B0TEST{i:04d}" for i in range(18)]
        keys = await self.store.text_identity_keys("\n".join(urls))
        self.assertEqual(len(keys), 18)
        await self.store.mark_product_posted("A", keys[0], 999, 30, keys[1:])
        self.assertTrue((await self.store.product_already_posted("A", [keys[-1]], 1, 99))[0])

    async def test_cached_short_links_keep_last_product_identity(self):
        for i in range(18):
            self.store.conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)",
                (f"https://source.invalid/{i}", f"https://bit.ly/fence{i}",
                 f"https://www.amazon.in/dp/B0TEST{i:04d}", f"ASIN:B0TEST{i:04d}", time.time()))
        self.store.conn.commit()
        keys = await self.store.text_identity_keys("\n".join(f"https://bit.ly/fence{i}" for i in range(18)))
        self.assertIn("ASIN:B0TEST0017", keys)

    async def test_target_claim_is_atomic_across_connections_and_rechecks_hour(self):
        second = bot.Store(self.path)
        try:
            self.assertTrue(await self.store.claim_target_products(1, "A", ["PID:www.meesho.com:abc123"]))
            self.assertFalse(await second.claim_target_products(2, "A", ["PID:m.meesho.com:abc123"]))
            self.assertTrue(await second.claim_target_products(2, "B", ["PID:m.meesho.com:abc123"]))
            await self.store.mark_product_posted("A", "PID:www.meesho.com:abc123", 999, 30, queue_id=1)
            self.assertFalse(await second.claim_target_products(2, "A", ["PID:meesho.com:abc123"]))
            self.store.conn.execute("UPDATE posted_products SET posted_at=?", (time.time()-3601,))
            self.store.conn.commit()
            self.assertFalse((await self.store.product_already_posted("A", ["PID:meesho.com:abc123"], 499, 70))[0])
            self.assertTrue(await second.claim_target_products(2, "A", ["PID:meesho.com:abc123"]))
        finally:
            second.conn.close()

    async def test_two_actual_workers_one_product_one_send(self):
        first = await self.row(1)
        second = await self.row(2, "Another source calls it portable audio ₹899\nhttps://www.croma.com/product/p/FENCE123")
        entered, release = asyncio.Event(), asyncio.Event()
        class BlockingClient(JobClient):
            async def send_message(self, *args, **kwargs):
                result = await super().send_message(*args, **kwargs)
                entered.set()
                await release.wait()
                return result
        client = BlockingClient({})
        targets = {"FenceShelfA": Ent("A")}
        affiliate = PassthroughAffiliate(self.store)
        task = asyncio.create_task(bot.process_job(client, affiliate, targets, first))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(bot.process_job(client, affiliate, targets, second), 3)
        finally:
            release.set()
            await task
        self.assertEqual(len(client.sends), 1)
        self.assertEqual(self.store.conn.execute("SELECT status FROM deliveries WHERE queue_id=?", (second['id'],)).fetchone()[0], "skipped")

    async def test_pipeline_hold_is_not_a_send_or_skip_and_other_target_runs(self):
        row = await self.row(targets=("FenceShelfA", "FenceShelfB"))
        class LostReply(JobClient):
            async def send_message(self, entity, *args, **kwargs):
                result = await super().send_message(entity, *args, **kwargs)
                if entity.title == "A":
                    raise TimeoutError("lost response")
                return result
        client = LostReply({})
        targets = {"FenceShelfA": Ent("A"), "FenceShelfB": Ent("B")}
        await bot.process_job(client, PassthroughAffiliate(self.store), targets, row)
        statuses = dict(self.store.conn.execute("SELECT target,status FROM deliveries"))
        self.assertEqual(statuses, {"FenceShelfA": "held", "FenceShelfB": "sent"})
        self.assertEqual(await self.store.pending_targets(row['id']), [])
        self.assertFalse(await self.store.claim_target_products(10, "FenceShelfA", ["PID:www.croma.com:fence123"]))
        self.store.conn.close()
        self.store = bot.Store(self.path)
        bot.store = self.store
        await bot.process_job(client, PassthroughAffiliate(self.store), targets, row)
        self.assertEqual(len(client.sends), 2)
        from ops.quality_audit import audit
        findings, _, _, summary = audit(self.store.conn, 20, "", "")
        self.assertEqual(summary['held'], 1)
        self.assertEqual(summary['real_sends'], 1)
        self.assertEqual(summary['skipped'], 0)
        self.assertTrue(any(item['kind'] == 'DELIVERY_HELD' for item in findings))

    async def test_settlement_failure_restart_retries_only_bookkeeping(self):
        row = await self.row()
        client = JobClient({})
        target = {"FenceShelfA": Ent("A")}
        with patch.object(self.store, "mark_product_posted", AsyncMock(side_effect=OSError("disk full"))):
            await bot.process_job(client, PassthroughAffiliate(self.store), target, row)
        self.assertEqual(len(client.sends), 1)
        bot.SETTLED_BUT_UNRECORDED.clear()  # real restart loses process memory
        self.store.conn.close()
        self.store = bot.Store(self.path)
        bot.store = self.store
        fresh = self.store.conn.execute("SELECT * FROM queue WHERE id=?", (row['id'],)).fetchone()
        with self.assertLogs(bot.log, level="INFO") as recovery_log:
            await bot.process_job(client, PassthroughAffiliate(self.store), target, fresh)
        self.assertTrue(any("sent=0 | settlement_retries=1" in line for line in recovery_log.output))
        self.assertEqual(len(client.sends), 1)
        self.assertEqual(self.store.conn.execute("SELECT status FROM deliveries").fetchone()[0], "sent")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM target_product_claims").fetchone()[0], 0)

    async def test_changed_media_layout_on_partial_resume_is_held(self):
        await self.row()
        client = SendCounter()
        # Pretend Telegram accepted the photo/caption and explicitly rejected
        # the next text part. A later fetch cannot recover the media.
        client.send_message = AsyncMock(side_effect=bot.ChatAdminRequiredError(None))
        client.send_file = AsyncMock(return_value=Ent("receipt"))
        text = "word " * 1500
        ok, _ = await bot.deliver(client, Ent("A"), text, ["photo.jpg"], attempt_callback=self.attempt())
        self.assertFalse(ok)
        self.assertEqual(await self.store.delivery_progress(1, "FenceShelfA"), 1)
        calls_before = client.send_message.await_count
        with self.assertRaises(bot.AmbiguousDelivery):
            await bot.deliver(client, Ent("A"), text, [], start_chunk=1, attempt_callback=self.attempt())
        self.assertEqual(client.send_message.await_count, calls_before)

    async def test_album_publication_timeout_never_falls_back(self):
        class Album(SendCounter):
            async def __call__(self, request):
                self.sends.append(type(request).__name__)
                raise TimeoutError("album accepted but reply lost")
        client = Album()
        with self.assertRaises(bot.AmbiguousDelivery):
            await bot.deliver(client, Ent("A"), "deal", ["a.jpg", "b.jpg"], media_refs=["p1", "p2"])
        self.assertEqual(client.sends, ["SendMultiMediaRequest"])


if __name__ == '__main__':
    unittest.main(verbosity=2)
