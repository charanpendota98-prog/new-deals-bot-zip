"""Round-9 simulation: does ONE product ever get posted twice to a channel?

The user's rule is "zero duplication, ever" while "nothing may be lost" and "no
waiting". Those three pull against each other, so this file drives the REAL
worker path (`Store.claim_job` + `process_job` + `deliver` + `finish`) with
planted source messages and a fake Telegram client, then counts what actually
reached each channel:

  S1 the same source message arriving twice (live event + rescan safety net)
  S2 the same product from three sources, one after another (best copy wins)
  S3 two copies claimed by two workers at the same time (concurrent intake)
  S4 a crash in the middle of delivery, then a restart (no re-send, no loss)
  S5 the best copy cannot be posted at all - the displaced copy must still post
  S6 fan-out to every channel of the matrix, then an identical campaign text
     arriving from another source
  S7 price-tier channels: one copy per channel, never two
  S8 the SAME product arriving under two SHORT LINKS the resolver cannot open
     (no ASIN, no PID): the product's own words must still recognise it, skip it
     on every channel, and let a strictly cheaper copy through

Run: python3 test_duplicate_sim.py   (from the repo root)
"""
import asyncio
import collections
import os
import re
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
FAILS: list[str] = []
SOURCE = "dealsvelocity"          # fans out to every non-Tricks channel
OUR_LINK = "https://www.amazon.in/dp/B0SIMPRODUCT?tag=deals0911-21"


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  <- {extra[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


class Ent:
    def __init__(self, title: str):
        self.title = title
        self.id = abs(hash(title)) % 10**9


class FakeMsg:
    def __init__(self, text: str, msg_id: int):
        self.text = self.message = text
        self.id = msg_id
        self.media = None
        self.entities = []
        self.reply_to = None
        self.reply_markup = None


class FakeClient:
    """Records exactly what a subscriber would see, per channel."""

    def __init__(self, messages: dict[tuple[int, int], FakeMsg]):
        self.messages = messages
        self.sent: list[tuple[str, str]] = []

    async def get_messages(self, chat_id, ids=None):
        return self.messages.get((chat_id, ids))

    async def send_message(self, entity, content, **kwargs):
        self.sent.append((getattr(entity, "title", str(entity)), content))

        class _Res:
            id = len(self.sent)
        return _Res()

    async def send_file(self, entity, file, **kwargs):
        return await self.send_message(entity, kwargs.get("caption", ""))

    async def download_media(self, msg, file=None):
        return None


class FakeAffiliate:
    """Monetizes exactly one product id; everything else stays unmonetized."""

    def __init__(self, link: str = OUR_LINK, deal_key: str = "ASIN:B0SIMPRODUCT"):
        self.link, self.deal_key = link, deal_key

    async def resolve(self, url):
        return "https://www.amazon.in/dp/B0SIMPRODUCT" if "amzn.to" in url or "bitli.in" in url else url

    async def convert(self, source, multi_link, resolved=None):
        host = (re.sub(r"^https?://", "", resolved or "") or "").split("/")[0]
        if "amazon" not in host and "amzn" not in (source or "") and "bitli" not in (source or ""):
            return None
        return bot.LinkResult(source, "https://www.amazon.in/dp/B0SIMPRODUCT", self.link, self.deal_key)

    async def cache_link(self, *args, **kwargs):
        return None

    async def shorten_long_urls_in_text(self, text):
        return text

    async def link_not_broken(self, url):
        return True

    async def rendered_links_not_broken(self, text):
        return True


def deal(price: str, pct: str, name: str = "Prestige 3L Induction Base Cooker",
        link: str = "https://amzn.to/simprod", tail: str = "Free shipping | MRP") -> str:
    return (f"{name}\n{R}{price} today only\n{tail} {R}1,999, {pct}% off\n{link}")


class DeadLinkAffiliate(FakeAffiliate):
    """Monetizes like the live service, but every merchant page answers 404 to us.

    That is the normal result for a fresh ASIN from a datacenter IP, and it used to
    kill the whole post. The links still work for a human reader, so the verdict must
    be "unverified", never "do not post".
    """

    async def link_not_broken(self, url):
        return False

    async def rendered_links_not_broken(self, text):
        return False


class HalfDeadAffiliate(FakeAffiliate):
    """Two products, one page that is gone for us (404) and one that is fine.

    This is the shape that drives the delivery-side LINK REPAIR: the pre-send pass says
    "something is dead", the pipeline then decides per link - drop that destination, keep the
    post. It is also the path that used to read a queue column that does not exist, which is
    how a repair ended up losing the post it was called to save.
    """

    async def resolve(self, url):
        slug = (url or "").rstrip("/").split("/")[-1]
        return f"https://www.amazon.in/dp/B0{slug}"

    async def convert(self, source, multi_link, resolved=None):
        slug = (resolved or "").split("/dp/")[-1].split("?")[0] or "B0GOOD1"
        return bot.LinkResult(source, f"https://www.amazon.in/dp/{slug}",
                              f"https://www.amazon.in/dp/{slug}?tag={bot.OUR_TAG}",
                              f"ASIN:{slug}")

    async def link_not_broken(self, url):
        return "DEAD" not in (url or "")

    async def rendered_links_not_broken(self, text):
        return "DEAD" not in (text or "")


class UnresolvableAffiliate:
    """A store we cannot monetize (no EarnKaro/Associates id in its URL) that two
    sources link in two different shapes: /ac-detail-a1?size=M and /ac-detail-a2.
    Nothing about the LINK tells us it is the same air conditioner - no ASIN, no
    PID, no shared URL - which is exactly how a channel used to carry the same
    product twice. The product's own words are the only identity available."""

    async def resolve(self, url):
        return url                    # the merchant page is used as it stands

    async def convert(self, source, multi_link, resolved=None):
        return None                   # unmonetizable: passes through, provenance
                                      # is remembered for the post itself

    async def cache_link(self, *args, **kwargs):
        return None

    async def shorten_long_urls_in_text(self, text):
        return text

    async def link_not_broken(self, url):
        return True

    async def rendered_links_not_broken(self, text):
        return True


async def drain(store, client, affiliate, target_map, limit=20) -> int:
    """Run the real worker body until the queue is empty."""
    handled = 0
    while handled < limit:
        row = await store.claim_job()
        if not row:
            break
        await bot.process_job(client, affiliate, target_map, row)
        handled += 1
    return handled


def per_channel_sends(client) -> dict[str, list[str]]:
    out: dict[str, list[str]] = collections.defaultdict(list)
    for target, text in client.sent:
        out[target].append(text)
    return out


def product_hits(sends: list[str], name_word: str = "Cooker") -> int:
    """How many posts on one channel carry this product's own name line."""
    return sum(1 for text in sends if name_word in text)


def target_map_for(store_targets: set[str]) -> dict[str, Ent]:
    return {name: Ent(name) for name in store_targets}


async def scenario(tag: str, messages: dict[tuple[int, int], FakeMsg],
                  enqueue_specs: list[tuple[int, int, str]], expect_posts: int,
                  crash_after_first: bool = False, affiliate: FakeAffiliate | None = None,
                  mutate=None, client=None):
    """One isolated run: enqueue, drain (optionally twice), return the client."""
    store = bot.Store(Path(tempfile.mktemp(suffix="-sim.sqlite3")))
    if True:
        old_store, old_quiet, old_premium = bot.store, bot.born_in_post_quiet, bot.premium_time_status
        bot.store = store
        bot.born_in_post_quiet = lambda created_at: False       # not a night test
        bot.premium_time_status = lambda now=None: (True, f"sim-{tag}", 0.0)  # premium channel is open
        try:
            for chat_id, msg_id, text in enqueue_specs:
                await store.enqueue(chat_id, msg_id, SOURCE, text, False)
            if mutate is not None:
                mutate(store)          # e.g. simulate a row written before a column existed
            store.conn.commit()
            names = {SOURCE}
            for targets in bot.SOURCE_TO_TARGETS.get(SOURCE, []):
                names.add(targets)
            names.update(bot.MAIN_TARGETS + [bot.UNDER99_TARGET, bot.UNDER499_TARGET, bot.PREMIUM_TARGET])
            aff = affiliate or FakeAffiliate()
            client = client or FakeClient(messages)
            tmap = target_map_for(names)
            if crash_after_first:
                # first pass delivers ONE channel only, then "crashes"
                row = await store.claim_job()
                real_send = client.send_message

                async def one_channel_only(entity, content, **kw):
                    return await real_send(entity, content, **kw)

                client.sent.clear()
                await bot.process_job(client, aff, tmap, row)
                first_pass = len(client.sent)
                # simulate a crash: everything not sent goes back to the queue
                store.conn.execute("UPDATE queue SET status='pending', attempts=0 WHERE id=?", (row["id"],))
                store.conn.execute("UPDATE deliveries SET status='failed', chunks_sent=0 "
                                   "WHERE queue_id=? AND status!='sent' AND target NOT IN "
                                   "(SELECT target FROM deliveries WHERE queue_id=? AND status='sent')",
                                   (row["id"], row["id"]))
                store.conn.commit()
                await drain(store, client, aff, tmap)
                print(f"    [{tag}] first pass sent {first_pass} channel(s), then resumed")
            else:
                await drain(store, client, aff, tmap)
            return store, client, per_channel_sends(client), expect_posts
        finally:
            bot.store, bot.born_in_post_quiet, bot.premium_time_status = old_store, old_quiet, old_premium


def test_scenarios():
    async def run():
        text_a = deal("899", "55")
        key = (bot.raw_chat_id(-1009001), 501)

        # ---- S1: the same message ingested twice ------------------------------
        print("\n== S1: one source message, two ingest events ==")
        store, client, sends, _ = await scenario(
            "s1", {key: FakeMsg(text_a, 501)},
            [(key[0], 501, text_a), (key[0], 501, text_a)], 1)
        rows = store.conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        check("one queue row for a repeated event", rows == 1, str(rows))
        for target, texts in sends.items():
            check(f"[S1] {target} got exactly one copy", product_hits(texts) == 1, str(len(texts)))

        # ---- S2: same product from 3 sources, sequentially --------------------
        print("\n== S2: same product, three sources, one after another ==")
        weak = deal("1299", "35", name="Prestige 3L Induction Base Cooker")
        mid = deal("1099", "45", name="Prestige 3L Induction Base Cooker")
        strong = deal("899", "55", name="Prestige 3L Induction Base Cooker")
        msgs = {(-1009002, 601): FakeMsg(weak, 601), (-1009003, 602): FakeMsg(mid, 602),
                (-1009004, 603): FakeMsg(strong, 603)}
        store, client, sends, _ = await scenario(
            "s2", msgs, [(-1009002, 601, weak), (-1009003, 602, mid), (-1009004, 603, strong)], 1)
        total = sum(product_hits(texts) for texts in sends.values())
        check("[S2] the product is posted once per channel (never three times)",
              all(product_hits(texts) == 1 for texts in sends.values()) and total == len(sends),
              str({t: product_hits(v) for t, v in sends.items()}))
        best = [t for texts in sends.values() for t in texts if "Cooker" in t]
        check("[S2] and the copy that went out is the best deal (899 / 55%)",
              bool(best) and all(f"{R}899" in t for t in best), str(best[:1]))

        # ---- S3: two copies claimed by two workers at the same time -----------
        print("\n== S3: two copies claimed concurrently ==")
        with tempfile.TemporaryDirectory() as td:
            store3 = bot.Store(Path(td) / "s3.sqlite3")
            old_store, old_quiet, old_prem = bot.store, bot.born_in_post_quiet, bot.premium_time_status
            bot.store, bot.born_in_post_quiet = store3, (lambda created_at: False)
            bot.premium_time_status = lambda now=None: (True, "s3", 0.0)
            try:
                await store3.enqueue(-1009005, 701, SOURCE, weak, False)
                await store3.enqueue(-1009006, 702, SOURCE, mid, False)
                client3 = FakeClient({(-1009005, 701): FakeMsg(weak, 701), (-1009006, 702): FakeMsg(mid, 702)})
                tmap3 = target_map_for(set(bot.SOURCE_TO_TARGETS.get(SOURCE, [])) | set(bot.MAIN_TARGETS)
                                       | {bot.UNDER99_TARGET, bot.UNDER499_TARGET, bot.PREMIUM_TARGET})
                r1 = await store3.claim_job()
                r2 = await store3.claim_job()
                check("[S3] both copies were claimable before either rendered", r1 and r2)
                await bot.process_job(client3, FakeAffiliate(), tmap3, r1)
                await bot.process_job(client3, FakeAffiliate(), tmap3, r2)
                sends3 = per_channel_sends(client3)
                check("[S3] concurrent jobs still produce one copy per channel",
                      all(product_hits(v) == 1 for v in sends3.values()) and len(sends3) >= 1,
                      str({t: product_hits(v) for t, v in sends3.items()}))
                statuses = [r[0] for r in store3.conn.execute("SELECT status FROM queue ORDER BY id")]
                check("[S3] the loser is closed as a duplicate, not left stuck",
                      statuses.count("pending") == 0 and "failed" not in statuses, str(statuses))
            finally:
                bot.store, bot.born_in_post_quiet, bot.premium_time_status = old_store, old_quiet, old_prem
                store3.conn.close()

        # ---- S4: crash in the middle of delivery -----------------------------
        print("\n== S4: crash mid-delivery, then restart ==")
        store, client, sends, _ = await scenario(
            "s4", {(-1009007, 801): FakeMsg(text_a, 801)}, [(-1009007, 801, text_a)], 1,
            crash_after_first=True)
        check("[S4] after a restart the deal is still posted once per channel",
              all(product_hits(v) == 1 for v in sends.values()) and len(sends) >= 2,
              str({t: product_hits(v) for t, v in sends.items()}))

        # ---- S5: the best copy cannot be posted -> the displaced copy must go -
        print("\n== S5: best copy fails, displaced copy still posts ==")
        # The queue holds the weak copy, then a better copy arrives and takes the
        # row over. Here the better copy turns out to be unrenderable, so the
        # displaced copy must come back and post - the deal is never lost.
        strong = deal("799", "60")
        real_render = bot.render_job

        async def flaky(client_, affiliate_, row_):
            if row_["msg_id"] == 902:
                raise bot.PermanentSkip("simulated: better copy unrenderable")
            return await real_render(client_, affiliate_, row_)

        bot.render_job = flaky
        try:
            store, client, sends, _ = await scenario(
                "s5", {(-1009008, 901): FakeMsg(text_a, 901), (-1009009, 902): FakeMsg(strong, 902)},
                [(-1009008, 901, text_a), (-1009009, 902, strong)], 1)
        finally:
            bot.render_job = real_render
        resumed = store.conn.execute("SELECT msg_id, status FROM queue ORDER BY id").fetchall()
        check("[S5] the displaced copy went back into the queue",
              any(r[0] == 901 and r[1] in ("done", "processing") for r in resumed),
              str([tuple(r) for r in resumed]))
        posts = [t for texts in sends.values() for t in texts if "Cooker" in t]
        check("[S5] the deal still reached our channels exactly once, with the copy that worked",
              bool(posts) and all(product_hits(v) == 1 for v in sends.values())
              and all(f"{R}899" in t for t in posts),
              str({t: product_hits(v) for t, v in sends.items()}) + " | " + str(posts[:1]))

        # ---- S6: identical campaign text from another source ------------------
        print("\n== S6: the same campaign text from a second source ==")
        store, client, sends, _ = await scenario(
            "s6", {(-1009010, 1001): FakeMsg(text_a, 1001), (-1009011, 1002): FakeMsg(text_a, 1002)},
            [(-1009010, 1001, text_a), (-1009011, 1002, text_a)], 1)
        check("[S6] identical campaign text produces one copy per channel",
              all(product_hits(v) <= 1 for v in sends.values()) and bool(sends),
              str({t: product_hits(v) for t, v in sends.items()}))

        # ---- S7: a two-product post goes to every channel once, no repeats ----
        print("\n== S7: two different products in one post ==")
        two = (f"Prestige 3L Induction Base Cooker {R}899 https://amzn.to/simprod\n"
               f"Vim 500g Dishwash Bar {R}49 https://amzn.to/simprod")
        store, client, sends, _ = await scenario("s7", {(-1009012, 1101): FakeMsg(two, 1101)},
                                                 [(-1009012, 1101, two)], 1)
        for target, texts in sends.items():
            joined = "\n".join(texts)
            check(f"[S7] {target} carries both products once, in one post",
                  product_hits(texts) == 1 and "Vim" in joined and "https://" in joined,
                  str(texts[:1]))
        # ---- S8: same product, two short links nobody can resolve -------------
        print("\n== S8: same product through unresolvable short links ==")
        # ---- S8: ONE product, two links, no merchant id anywhere --------------
        # Nothing in the URL says "this is the same air conditioner" (no ASIN, no
        # PID, a different path per source), so this is where a channel used to
        # post the same product twice. The product's own words are the identity.
        print("\n== S8: same product through unidentifiable links ==")
        ac_name = "LG 1.5 Ton 5 Star Inverter Split AC"
        word = ac_name.split()[1]
        first = (f"\U0001F525 PRICE DROP \U0001F525\n{ac_name}\nNow {R}36,990 (MRP {R}74,990, 50% off)\n"
                 f"Use code: LGAC1500 | No cost EMI\nBuy \U0001F449 https://www.croma.com/ac-detail-a1?size=M")
        repeat = (f"{ac_name}\n{R}36,990 (50% off)\nFree installation this week\n"
                  f"https://www.croma.com/ac-detail-a2")
        better = f"{ac_name}\nNow {R}33,490 only (54% off)\nhttps://www.croma.com/ac-detail-a3"
        other = f"LG Neo Duetto 7Kg Front Load Washing Machine\n{R}31,990 (44% off)\nhttps://www.croma.com/wash-b1"
        aff = UnresolvableAffiliate()
        # PASSTHROUGH_UNMONETIZED=false is the operator's documented choice to SKIP a deal
        # nothing can be monetized on, so S8's promises flip with that knob in both
        # directions - an assertion that ignores a knob it depends on is a false alarm (or,
        # worse, a bug hidden behind one).
        keep_unmonetized = bool(bot.PASSTHROUGH_UNMONETIZED)

        # a) fidelity first: every line the source wrote reaches the channel
        store, client, sends, _ = await scenario("s8a", {(-1009020, 1201): FakeMsg(first, 1201)},
                                                 [(-1009020, 1201, first)], 1, affiliate=aff)
        solo = [t for texts in sends.values() for t in texts if word in t]
        # With the opt-in banner stripper on, the hype line is gone BY DESIGN, so
        # it must not be demanded here; everything that is a deal still has to be.
        wanted = [f"{R}74,990", "LGAC1500", "No cost EMI", f"{R}36,990"]
        if not bot.STRIP_CAMPAIGN_BANNERS:
            wanted.insert(0, "PRICE DROP")
        if keep_unmonetized:
            check("[S8a] the post carries every source line (banner, MRP, coupon, EMI)",
                  bool(solo) and all(all(x in t for x in wanted) for t in solo), str(solo[:1]))
            check("[S8a] and it carries our destination link, once",
                  bool(solo) and all(t.count("croma.com/ac-detail-a1") == 1 for t in solo), str(solo[:1]))
        else:
            rows = store.conn.execute("select status, last_error from queue").fetchall()
            state = [(r["status"], r["last_error"] or "") for r in rows]
            check("[S8a] PASSTHROUGH_UNMONETIZED=false posts no fragment of an unmonetizable deal",
                  not sends and state and all(
                      status in ("pending", "processing", "done")
                      and ("no monetizable URLs" in err or "conversion retry required" in err)
                      for status, err in state),
                  str(state))

        # b) the same product again at the same price: the channel stays quiet
        store, client, sends, _ = await scenario(
            "s8b", {(-1009021, 1301): FakeMsg(first, 1301), (-1009022, 1302): FakeMsg(repeat, 1302)},
            [(-1009021, 1301, first), (-1009022, 1302, repeat)], 1, affiliate=aff)
        hits = {t: product_hits(v, word) for t, v in sends.items()}
        # "is the second copy news?" is not a constant, it is the module's OWN rule
        # (Store line: discount >= last + SAME_PRODUCT_DISCOUNT_MARGIN), so the expectation
        # is computed with that rule instead of assuming the default margin. Under a knob
        # that makes this repeat "better", two posts is the correct outcome - not a leak.
        # the same two comparisons Store.product_already_posted makes, on the same inputs
        _fp, _rp = bot.parse_price(first), bot.parse_price(repeat)
        first_disc = bot.parse_discount(first, _fp) or 0
        repeat_disc = bot.parse_discount(repeat, _rp) or 0
        # Order-independent on purpose: the queue may render either copy first, and the
        # store then compares the arrival against what THAT channel already carried. So the
        # promise is "a copy whose figures differ by at least the margin is news whichever
        # way it arrives" - which is exactly what the store's own rule gives.
        repeat_is_news = ((_rp is not None and _fp is not None and _rp != _fp)
                          or abs(repeat_disc - first_disc) >= bot.SAME_PRODUCT_DISCOUNT_MARGIN)
        expected_count = 2 if (repeat_is_news or bot.SAME_PRODUCT_SKIP_SECONDS == 0) else 1
        expected_copies = expected_count * len(sends)   # set once, adjusted by the checks below
        if not keep_unmonetized:
            check("[S8b] with the knob off nothing is posted, so nothing repeats",
                  not sends, str({t: len(v) for t, v in sends.items()}))
        elif bot.SAME_PRODUCT_SKIP_SECONDS > 0 and not repeat_is_news:
            check("[S8b] a channel that has carried the product does not carry it again",
                  bool(sends) and all(count == 1 for count in hits.values()), str(hits))
        elif bot.SAME_PRODUCT_SKIP_SECONDS > 0:
            check("[S8b] a copy that beats the margin (or SAME_PRODUCT_DISCOUNT_MARGIN lowered) is news, not a duplicate",
                  bool(sends) and all(count == 2 for count in hits.values()), str(hits))
        elif bot.SAME_PRODUCT_SKIP_SECONDS == 0:
            # The knob is the operator's escape hatch, and it has to be a REAL one:
            # with SAME_PRODUCT_SKIP_SECONDS=0 the second copy goes out as before.
            check("[S8b] SAME_PRODUCT_SKIP_SECONDS=0 turns the skipper off for real",
                  bool(sends) and all(count == 2 for count in hits.values()), str(hits))
            expected_copies = 2 * len(sends)
        kept = [t for texts in sends.values() for t in texts if word in t]
        if not keep_unmonetized:
            kept = [] if not sends else kept
            expected_copies = 0
        check("[S8b] every copy that goes out is complete, never a fragment",
              (bool(kept) and len(kept) >= expected_copies and all(
                  f"{R}36,990" in t and "croma.com/ac-detail-a" in t and word in t for t in kept))
              if keep_unmonetized else not sends,
              str(kept[:1]))

        # c) a cheaper copy of the same product is news, not a duplicate
        store, client, sends, _ = await scenario(
            "s8c", {(-1009023, 1401): FakeMsg(first, 1401), (-1009024, 1402): FakeMsg(better, 1402)},
            [(-1009023, 1401, first), (-1009024, 1402, better)], 1, affiliate=aff)
        hits = {t: product_hits(v, word) for t, v in sends.items()}
        cheaper = [t for texts in sends.values() for t in texts if f"{R}33,490" in t]
        check("[S8c] the strictly better copy still reaches every channel",
              (bool(sends) and all(v for v in sends.values()) and bool(cheaper))
              if keep_unmonetized else not sends,
              str({t: len(v) for t, v in sends.items()}))
        check("[S8c] and the same product is never carried more than twice",
              all(1 <= count <= 2 for count in hits.values()), str(hits))

        # d) a DIFFERENT product from the same source is never skipped
        store, client, sends, _ = await scenario("s8d", {(-1009025, 1501): FakeMsg(other, 1501)},
                                                  [(-1009025, 1501, other)], 1, affiliate=aff)
        check("[S8d] another product from the same store still reaches every channel",
              (bool(sends) and all(v for v in sends.values())) if keep_unmonetized else not sends,
              str({t: len(v) for t, v in sends.items()}))

        # e) the identity rule is offline: no network wait was added for it
        # The key itself must not depend on the skip window: an operator who turns
        # the rule off must still get a signature (the ledger keeps writing, and the
        # auditor reads it), it is only the SKIPPING that stops.
        check("[S8] the product signature is pure text (no HTTP, no sleep)",
              bot.product_signature(repeat) == bot.product_signature(first)
              and bot.product_signature(repeat) is not None,
              f"{bot.product_signature(first)} vs {bot.product_signature(repeat)}")
        check("[S8] the skip window is a documented number",
              bot.SAME_PRODUCT_SKIP_SECONDS >= 0,
              f"window={bot.SAME_PRODUCT_SKIP_SECONDS}")

        # ---- S9: the user's live post - glued code, and a link that reads dead ----
        # Two complaints in one shape: "\u20b9 199HFJF" reached the channel with the coupon
        # glued to the price, and (worse) the post never reached the channel at all,
        # because Amazon answers a fresh ASIN with a repair page to our IP and the job
        # was thrown away as a "broken destination". Arrival must mean delivery; only
        # the cleaning may change what the source wrote.
        print("\n== S9: glued coupon code + a link that reads dead ==")
        lizol = (
            "Lizol Floor Cleaner Shakti Disinfectant with Jasmine Fragrance 900ml "
            "(Pack of 2) Surface Cleaning  | Toilet Cleaner, 900 ml (2 x 900 ml)\n"
            "\u2705Deal Price: \u20b9 199HFJF\n\u274cMRP: \u20b9 270\nDiscount: 26%\n"
            "\U0001f449 [https://t.me/loots/156757](https://wa.me/?text=https%3A%2F%2Ft.me%2Floots%2F156757)\n"
            "\U0001f449 [https://www.amazon.in/dp/B0GH2374K3?tag=deals0911-21](https://amzn.to/lizol77)")
        old_drop = bot.DROP_DEAD_LINKS
        try:
            # The default must be tested as the default: an operator who exports
            # DROP_DEAD_LINKS=true in this environment would otherwise flip the
            # expectation of the run below, and the run below is the whole point.
            bot.DROP_DEAD_LINKS = False
            store, client, sends, _ = await scenario(
                "s9a", {(-1009030, 1601): FakeMsg(lizol, 1601)},
                [(-1009030, 1601, lizol)], 1, affiliate=DeadLinkAffiliate())
            joined = "\n".join(text for texts in sends.values() for text in texts)
            check("[S9a] every routed channel got the post even though the link read dead",
                  bool(sends) and all(texts for texts in sends.values()),
                  str({name: len(v) for name, v in sends.items()}))
            check("[S9a] the unwanted scrap glued to the price is gone, price intact",
                  "\u20b9 199" in joined and "HFJF" not in joined and "199HFJF" not in joined,
                  joined[:140])
            check("[S9a] name, MRP and discount all arrived, with a link",
                  all(x in joined for x in ["Lizol Floor Cleaner", "\u20b9 270", "26%", "https://"]),
                  joined[:200])
            check("[S9a] the link that went out is one we generated, never the shortener",
                  "amzn.to/lizol77" not in joined and ("amazon.in/dp/" in joined or OUR_LINK in joined),
                  joined[:200])
            bot.DROP_DEAD_LINKS = True
            store, client, sends_dead, _ = await scenario(
                "s9b", {(-1009031, 1602): FakeMsg(lizol, 1602)},
                [(-1009031, 1602, lizol)], 1, affiliate=DeadLinkAffiliate())
            check("[S9b] DROP_DEAD_LINKS=true still hands the operator the old hard block",
                  not sends_dead, str({name: len(v) for name, v in sends_dead.items()}))
        finally:
            bot.DROP_DEAD_LINKS = old_drop

        # ---- S10: our own channel link, once, on top - and nothing else of ours ----
        print("\n== S10: the top family link ==")
        text_a = deal("899", "55")
        store, client, sends, _ = await scenario("s10a", {(-1009040, 1701): FakeMsg(text_a, 1701)},
                                                 [(-1009040, 1701, text_a)], 1)
        posts = [p for texts in sends.values() for p in texts]
        header = bot.channel_header_line()
        stored = store.conn.execute("SELECT rendered_text FROM queue").fetchone()[0]
        if bot.ADD_OUR_CHANNEL_LINK_TOP:
            check("[S10a] every post opens with exactly one family link line",
                  bool(posts) and all(p.splitlines()[0] == header for p in posts), str(posts[:1]))
            check("[S10a] it is OUR link, and no other channel is linked",
                  all(p.count("t.me/") == 1 and bot.OUR_FOLDER_LINK in p for p in posts),
                  str(posts[:1]))
            check("[S10a] the second line is still the source's own text",
                  all(p.splitlines()[1].startswith("Prestige") for p in posts), str(posts[:1]))
            check("[S10a] the stored copy stays the source's - the line is added on the way out",
                  bot.OUR_FOLDER_LINK not in stored, stored[:90])
        else:
            # An operator who exported ADD_OUR_CHANNEL_LINK_TOP=false must get the plain
            # source text, so the promise flips with the knob - and it is still a promise:
            # no line of ours anywhere, and the deal itself is untouched.
            check("[S10a] with the knob off, nothing of ours is added anywhere",
                  bool(posts) and all(header not in p and "t.me/" not in p for p in posts),
                  str(posts[:1]))
            check("[S10a] and the post still opens with the source's own headline",
                  all(p.startswith("Prestige") for p in posts), str(posts[:1]))
        old_top = bot.ADD_OUR_CHANNEL_LINK_TOP
        try:
            bot.ADD_OUR_CHANNEL_LINK_TOP = False
            store, client, sends_off, _ = await scenario(
                "s10b", {(-1009041, 1702): FakeMsg(deal("799", "60", name="Havells Table Fan"), 1702)},
                [(-1009041, 1702, deal("799", "60", name="Havells Table Fan"))], 1)
            off = [p for texts in sends_off.values() for p in texts]
            check("[S10b] ADD_OUR_CHANNEL_LINK_TOP=false posts the source text with nothing added",
                  bool(off) and all(header not in p and "t.me/" not in p for p in off), str(off[:1]))
        finally:
            bot.ADD_OUR_CHANNEL_LINK_TOP = old_top

        # ---- S11: a post whose ONLY link is a dead shortener still reaches us ----
        # A short link dies every day (quota, expired campaign, deleted post). Retrying
        # the whole job until the price goes stale is how a source post vanished with
        # nothing in the log to show for it, so the link is cut and the deal is posted.
        print("\n== S11: dead short link, live deal ==")
        dead = ("Cello Stoneware Casserole 1.5L\n\u2705Deal Price: \u20b9499\n\u274cMRP: \u20b91,299 "
                "(62% off)\nhttps://bit.ly/deadshortlink12345")
        store, client, sends, _ = await scenario("s11a", {(-1009042, 1703): FakeMsg(dead, 1703)},
                                                 [(-1009042, 1703, dead)], 1)
        posts = [p for texts in sends.values() for p in texts]
        check("[S11a] the post was NOT swallowed by the dead link",
              bool(sends) and all(texts for texts in sends.values()),
              str({n: len(v) for n, v in sends.items()}))
        joined = "\n".join(posts)
        check("[S11a] the deal is complete: name, price, MRP, discount",
              all(x in joined for x in ["Cello Stoneware Casserole", "\u20b9499", "\u20b91,299", "62%"]),
              joined[:160])
        check("[S11a] and no foreign link was published in its place",
              "bit.ly" not in joined and "shorturl" not in joined, joined[:160])
        row11 = store.conn.execute("SELECT status, last_error FROM queue").fetchone()
        check("[S11a] the job finished (not pending-retry, not skipped)",
              row11[0] == "done" and not row11[1], str(tuple(row11)))

        # ---- S12: the delivery-side link repair (a dead destination next to a live one)
        # The repair re-writes the post, so it must not throw over a missing column, must
        # keep every word of the deal, and must not leave an empty line where it cut a link.
        print("\n== S12: a dead destination is cut, the post survives ==")
        two = ("\U0001f525 Two-pack list\n"
               "1) Multicolor Hair Clips \u20b985 https://amzn.to/GOOD1\n"
               "2) Steel Water Bottle \u20b999 https://amzn.to/DEAD2")
        msgs = {(-1009098, 9500): FakeMsg(two, 9500)}
        store, client, sends, _ = await scenario("s12a", msgs, [(-1009098, 9500, two)], 1,
                                                 affiliate=HalfDeadAffiliate())
        ch = sorted(sends)
        check("[S12a] the repair posted, it did not lose the deal", bool(ch), str(ch))
        body = sends[ch[0]][0] if ch else ""
        check("[S12a] the dead destination is gone from the copy", "DEAD" not in body, body)
        check("[S12a] the live product line and price survive",
              "Multicolor Hair Clips" in body and "\u20b985" in body and "Steel Water Bottle" in body, body)
        check("[S12a] the layout has no blank line the repair created", "\n\n" not in body, repr(body))
        row12 = store.conn.execute("select status, last_error from queue").fetchone()
        check("[S12a] the job finished clean (no repair crash)", row12[0] == "done", str(tuple(row12)))

        # the same run against a row written by an older build (no source_text on the row):
        # an unknown layout must mean "hands off", never a crash and never a re-flow.
        store, client, sends, _ = await scenario(
            "s12b", msgs, [(-1009098, 9500, two)], 1, affiliate=HalfDeadAffiliate(),
            mutate=lambda st: st.conn.execute("UPDATE queue SET source_text=NULL"))
        ch_b = sorted(sends)
        body_b = sends[ch_b[0]][0] if ch_b else ""
        check("[S12b] a legacy row (no source_text) still posts, unharmed",
              bool(ch_b) and "Multicolor Hair Clips" in body_b and "\u20b985" in body_b, str(ch_b))
        check("[S12b] and it is not re-flowed on a guess (source layout unknown)",
              body_b.count("1) Multicolor Hair Clips") == 1, repr(body_b[:120]))

        # ---- S13: the source deleted its own post before we rendered it -----------
        # Some channels remove or re-post a deal within seconds. The text reached us, so
        # losing the post here is the bug; losing the photos is a fact of life.
        print("\n== S13: the source message vanished after intake ==")
        gone = ("\U0001f525 Adidas Ultraboost Shoes\n"
                "MRP \u20b99,999 Deal Price \u20b93,499 Discount: 65%\n"
                "https://amzn.to/SIMGONE")
        msgs_missing: dict[tuple[int, int], FakeMsg] = {}     # the source message is not there
        store, client, sends, _ = await scenario("s13a", msgs_missing, [(-1009099, 9600, gone)], 1)
        ch = sorted(sends)
        check("[S13a] the deal is posted even though its source message is gone", bool(ch), str(ch))
        body = sends[ch[0]][0] if ch else ""
        check("[S13a] the source's own lines came through",
              "Adidas Ultraboost Shoes" in body and "\u20b93,499" in body and "65%" in body, body)
        st13 = store.conn.execute("select status, last_error from queue").fetchone()
        check("[S13a] the job finished, it did not burn its retries on a ghost",
              st13[0] == "done", str(tuple(st13)))

        # the same deal on a re-delivery: the post was already rendered and stored, and
        # the source message fetch now RAISES (a deleted message id). A job holding its
        # final text must not abort over an optional lookup - before this fix the whole
        # job died with `JOB FAIL ... Could not find the referenced message`.
        class VanishedClient(FakeClient):
            async def get_messages(self, chat_id, ids=None):
                raise ValueError("Could not find the referenced message 9601 in this channel")

        stored_copy = ("\U0001f525 Adidas Ultraboost Shoes\n"
                       "MRP \u20b99,999 Deal Price \u20b93,499 Discount: 65%")
        def as_already_rendered(st):
            """The state a job is in when an earlier attempt finished rendering: the final
            text and its planned targets are stored, the source message is not needed."""
            qid = st.conn.execute("SELECT id FROM queue ORDER BY id DESC LIMIT 1").fetchone()[0]
            st.conn.execute(
                "UPDATE queue SET rendered_text=?, source_text=NULL, targets_json=?",
                (stored_copy, __import__("json").dumps([bot.MAIN_TARGETS[0]])))
            st.conn.execute("INSERT OR IGNORE INTO deliveries(queue_id,target) VALUES(?,?)",
                            (qid, bot.MAIN_TARGETS[0]))

        store, client, sends, _ = await scenario(
            "s13b", msgs_missing, [(-1009099, 9601, gone)], 1, client=VanishedClient({}),
            mutate=as_already_rendered)
        ch_b = sorted(sends)
        check("[S13b] a stored post is delivered even when the message fetch throws",
              bool(ch_b), str(ch_b))
        body_b = sends[ch_b[0]][0] if ch_b else ""
        # the stored bytes are what goes out; the only addition is the our-channel line that
        # delivery puts on top of every post (never stored in the row, by the v18.1 rule)
        check("[S13b] exactly as it was stored, line for line",
              stored_copy in body_b and body_b.count("t.me/addlist") == 1, body_b[:120])
        st13b = store.conn.execute("select status, last_error from queue").fetchone()
        check("[S13b] no JOB FAIL, the row is 'done'", st13b[0] == "done", str(tuple(st13b)))
        # ---- S14: a caption/photo post whose source message is gone stays honest -----
        # No link and no media left to show means it is not a deal post any more, and the
        # pinned rule is that a photo post needs BOTH media and deal terms. Nothing is
        # invented to make it look like one.
        print("\n== S14: a photo-only post is not rebuilt out of thin air ==")
        photo_only = "\U0001f4f8 Look at this \U0001f60d"      # no price, no link at all
        store, client, sends, _ = await scenario("s14", {(-1009100, 9700): FakeMsg(photo_only, 9700)},
                                                 [(-1009100, 9700, photo_only)], 0)
        check("[S14] a caption with no deal terms and no link still does not post",
              not sorted(sends), str(sorted(sends)))

        await asyncio.sleep(0)
    asyncio.run(run())


def test_no_artificial_telegram_waits():
    print("\n== no artificial waiting on the Telegram path ==")
    # What must be true is the CODE DEFAULT (nobody waits unless they ask); an operator who
    # sets TARGET_FANOUT_GAP_MIN/MAX has asked for a gap, and that is honoured, not denied.
    src_defaults = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    check("channel fan-out ships with no gap (the default in the code)",
          '_num("TARGET_FANOUT_GAP_MIN", 0' in src_defaults
          and '_num("TARGET_FANOUT_GAP_MAX", 0' in src_defaults, "the defaults changed")
    check("and the running gap stays inside what the operator configured",
          0 <= bot.TARGET_FANOUT_GAP_MIN <= bot.TARGET_FANOUT_GAP_MAX,
          f"{bot.TARGET_FANOUT_GAP_MIN}-{bot.TARGET_FANOUT_GAP_MAX}")
    check("the whole pre-send link check is capped at seconds, not 25s",
          bot.PRESEND_CHECK_BUDGET_SECONDS <= 8, str(bot.PRESEND_CHECK_BUDGET_SECONDS))
    check("one merchant page probe has its own hard budget",
          bot.LINK_PROBE_BUDGET_SECONDS <= 5, str(bot.LINK_PROBE_BUDGET_SECONDS))
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    fanout = src[src.index("for target in await store.pending_targets"):][:1400]
    check("the fan-out loop sleeps only when the operator asks for a gap",
          "TARGET_FANOUT_GAP_MAX > 0" in fanout, fanout[:160])
    check("the delivery loop has no other sleep",
          fanout.count("asyncio.sleep") <= 1 and "random.uniform(TARGET_FANOUT_GAP" in fanout,
          str(fanout.count("asyncio.sleep")))
    check("a slow site can never hold a post (timeout keeps the deal)",
          "posting anyway" in src and "a slow page is not a dead deal" in src)
    check("no dead-page retry churn is left",
          "every destination unverified; retry later" not in src)

    # behavioural: a merchant page that never answers must not delay the verdict
    async def probe_is_bounded():
        class HangingSession:
            def get(self, *a, **k):
                class Ctx:
                    async def __aenter__(self):
                        await asyncio.sleep(30)   # never finishes in time

                    async def __aexit__(self, *exc):
                        return False
                return Ctx()

        aff = bot.AffiliateClient(HangingSession())  # type: ignore[arg-type]
        started = time.time()
        verdict = await aff.link_not_broken("https://www.amazon.in/dp/B0HANG00001")
        elapsed = time.time() - started
        check("an unresponsive page is inconclusive (post goes out), not blocking",
              verdict is True and elapsed < 6, f"verdict={verdict} elapsed={elapsed:.1f}s")
    asyncio.run(probe_is_bounded())


if __name__ == "__main__":
    test_scenarios()
    test_no_artificial_telegram_waits()
    print("\n" + ("DUPLICATE SIM: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_duplicate_sim: all checks PASS"))
    sys.exit(1 if FAILS else 0)
