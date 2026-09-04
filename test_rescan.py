"""Functional test for source_rescan_loop (the event-stream dead-man's switch).
Run: python3 test_rescan.py   (from the repo root)
"""
import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path

os.environ.update(
    TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x",
    EARNKARO_API_KEY="k", AMAZON_TAG="",
)
sys.path.insert(0, str(Path(__file__).parent / "bestgaa"))
import main_bot_new as bot  # noqa: E402


class ScanMsg:
    def __init__(self, msg_id, text, age_seconds=60):
        self.id = msg_id
        self.text = text
        self.message = text
        self.media = None
        self.reply_to = None
        self.date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_seconds)


class ScanClient:
    """Returns a fixed message list for the -100 form; the rescan must call it."""

    def __init__(self, messages):
        self.messages = messages
        self.calls = 0

    async def get_messages(self, chat_id, limit=None):
        self.calls += 1
        return self.messages


async def main():
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "t.sqlite3")
        bot.store = store
        bot.SOURCE_RESCAN_SECONDS = 1  # fast cycles for the test
        bot.SOURCE_RESCAN_LIMIT = 10

        # Fresh deal (1 min old) + an old one (outside the 1s test window).
        fresh = ScanMsg(501, "Fresh deal \u20b949\nhttps://amzn.to/xx1", age_seconds=30)
        stale = ScanMsg(502, "Old deal\nhttps://amzn.to/xx2", age_seconds=999_999)
        client = ScanClient([stale, fresh])
        source_map = {12345: ("src1", []), -10012345: ("src1", [])}

        stop = asyncio.Event()
        task = asyncio.create_task(bot.source_rescan_loop(client, source_map, stop))
        await asyncio.sleep(2.5)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()

        rows = store.conn.execute(
            "SELECT msg_id, source FROM queue WHERE chat_id=-10012345 ORDER BY msg_id"
        ).fetchall()
        ids = sorted(r[0] for r in rows)
        assert ids == [501], f"expected only the fresh msg queued, got {ids}"
        assert client.calls >= 1, "rescan never called get_messages"
        assert not any(r[0] == 502 for r in rows), "stale message must not be re-queued"
        print("ok  fresh message recovered by rescan; stale message ignored")

        # Second cycle: same message must NOT be re-queued (idempotent).
        client2 = ScanClient([ScanMsg(501, "Fresh deal \u20b949\nhttps://amzn.to/xx1", age_seconds=10)])
        stop2 = asyncio.Event()
        task2 = asyncio.create_task(bot.source_rescan_loop(client2, source_map, stop2))
        await asyncio.sleep(2.5)
        stop2.set()
        try:
            await asyncio.wait_for(task2, timeout=5)
        except asyncio.TimeoutError:
            task2.cancel()
        count = store.conn.execute(
            "SELECT COUNT(*) FROM queue WHERE chat_id=-10012345 AND msg_id=501"
        ).fetchone()[0]
        assert count == 1, f"duplicate queue rows for msg 501: {count}"
        print("ok  repeat scan is idempotent (no duplicate queue rows)")

    print("RESCAN TESTS PASS")


asyncio.run(main())
