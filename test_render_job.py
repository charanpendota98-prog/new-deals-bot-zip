"""Integration test for render_job: proves the Telegram pipeline renders and
routes posts correctly — service offers, mixed posts, store posts, social-only.
Run: python3 test_render_job.py   (from the repo root)
"""
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.update(
    TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x",
    EARNKARO_API_KEY="k", AMAZON_TAG="deals0911-21",
)
sys.path.insert(0, str(Path(__file__).parent / "bestgaa"))
import main_bot_new as bot  # noqa: E402


class FakeMsg:
    def __init__(self, text):
        self.text = text
        self.message = text
        self.media = None
        self.entities = []
        self.reply_to = None


class FakeClient:
    def __init__(self, msg):
        self.msg = msg

    async def get_messages(self, chat_id, ids=None):
        return self.msg


class FakeAffiliate:
    def __init__(self, resolve_map, convert_map):
        self.resolve_map = resolve_map
        self.convert_map = convert_map

    async def resolve(self, url):
        return self.resolve_map.get(url, url)

    async def convert(self, source, multi_link, resolved=None):
        return self.convert_map.get(source)

    async def shorten_long_urls_in_text(self, rendered):
        return rendered  # shortening is a real-Bitly path; tested separately below


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


async def queue_row(store, text, source="some_source", msg_id=1):
    await store.enqueue(111, msg_id, source, text, False)
    row = store.conn.execute("SELECT * FROM queue ORDER BY id DESC LIMIT 1").fetchone()
    return row


async def case(store, name, text, resolve_map, convert_map, expect_exception=None,
               expect_render_contains=(), expect_targets_superset=(), msg_id=1,
               source="some_source"):
    print(f"\n== {name} ==")
    row = await queue_row(store, text, source=source, msg_id=msg_id)
    client = FakeClient(FakeMsg(text))
    affiliate = FakeAffiliate(resolve_map, convert_map)
    try:
        msg, rendered, price = await bot.render_job(client, affiliate, row)
    except Exception as exc:
        if expect_exception and isinstance(exc, expect_exception):
            check(f"raised {type(exc).__name__} ({exc})", True)
            return
        check(f"unexpected exception {type(exc).__name__}: {exc}", False)
        return
    if expect_exception:
        check(f"expected {expect_exception.__name__} but render succeeded", False)
        return
    targets = await store.pending_targets(row["id"])
    check("render returned text with URLs", bool(rendered and bot.URL_RE.search(rendered)))
    for fragment in expect_render_contains:
        check(f"rendered contains {fragment[:60]!r}", fragment in rendered)
    for target in expect_targets_superset:
        check(f"routed to {target}", target in targets)


AMAZON_AFF = "https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21"


def amazon_result():
    return bot.LinkResult(
        source="https://amzn.to/abc",
        resolved="https://www.amazon.in/dp/B0GLY3Q2XR",
        affiliate=AMAZON_AFF,
        deal_key="amazon:B0GLY3Q2XR",
    )


async def main():
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "t.sqlite3")
        bot.store = store  # render_job uses the module-level store

        # 1. Service-only (Zomato short link): pass-through, fanned out.
        await case(
            store, "service-only Zomato",
            "Zomato 50% OFF today. Use code ZOM50.\nhttps://zom.to/abc123",
            {"https://zom.to/abc123": "https://www.zomato.in/restaurants/zom50-offer"},
            {},
            expect_render_contains=["https://www.zomato.in/restaurants/zom50-offer"],
            expect_targets_superset=bot.ALL_OWNED_TARGETS,
            msg_id=1,
        )

        # 2. Same service offer again: dedup catches it.
        await case(
            store, "service repeat (dedup)",
            "Zomato 50% OFF today. Use code ZOM50.\nhttps://zom.to/abc123",
            {"https://zom.to/abc123": "https://www.zomato.in/restaurants/zom50-offer"},
            {},
            expect_exception=bot.DuplicateDeal,
            msg_id=2,
        )

        # 3. Mixed: Amazon + Zomato in one post — both survive.
        await case(
            store, "mixed Amazon + Zomato",
            "T-shirt 70% off\nhttps://amzn.to/abc\nZomato code ZOM50 50% off\nhttps://zom.to/xyz789",
            {
                "https://amzn.to/abc": "https://www.amazon.in/dp/B0GLY3Q2XR",
                "https://zom.to/xyz789": "https://www.zomato.in/restaurants/zom50b",
            },
            {"https://amzn.to/abc": amazon_result()},
            expect_render_contains=[AMAZON_AFF, "https://www.zomato.in/restaurants/zom50b"],
            expect_targets_superset=bot.ALL_OWNED_TARGETS,
            msg_id=3,
        )

        # 4. Store-only (Amazon): the normal monetized path still works.
        await case(
            store, "store-only Amazon",
            "Cotton Kurta 75% off. Deal Price: \u20b9499\nhttps://amzn.to/kurta1",
            {"https://amzn.to/kurta1": "https://www.amazon.in/dp/B0KURTA001"},
            {
                "https://amzn.to/kurta1": bot.LinkResult(
                    source="https://amzn.to/kurta1",
                    resolved="https://www.amazon.in/dp/B0KURTA001",
                    affiliate="https://www.amazon.in/dp/B0KURTA001?tag=deals0911-21",
                    deal_key="amazon:B0KURTA001",
                )
            },
            expect_render_contains=["https://www.amazon.in/dp/B0KURTA001?tag=deals0911-21"],
            msg_id=4,
        )

        # 5. Social-only (YouTube): rewritten into OUR folder promo (by design:
        # foreign join links never survive), routed to the Tricks channel.
        await case(
            store, "social-only YouTube -> own promo",
            "Check this video\nhttps://youtube.com/watch?v=abc123",
            {},
            {},
            expect_render_contains=[bot.OUR_FOLDER_LINK],
            msg_id=5,
        )

        # 6. Swiggy via swiggy.com long link.
        await case(
            store, "service Swiggy",
            "Swiggy One free for 3 months on this page\nhttps://www.swiggy.com/swiggy-one-offer-xyz",
            {},
            {},
            expect_render_contains=["https://www.swiggy.com/swiggy-one-offer-xyz"],
            expect_targets_superset=bot.ALL_OWNED_TARGETS,
            msg_id=6,
        )

        # 7. Rescan-loop sanity: enqueue returns True once, False on repeat.
        added1 = await store.enqueue(222, 9, "some_source", "x https://a/1")
        added2 = await store.enqueue(222, 9, "some_source", "x https://a/1")
        check("enqueue reports new insert then duplicate", added1 is True and added2 is False)

        # 7b. TELEGRAM GUARANTEE: best-deal curation is WhatsApp-only. A weak
        # deal (low discount, no price band, no media) from an UNMAPPED source
        # still reaches the main Telegram feed; themed channels may skip it.
        await case(
            store, "telegram always posts a weak deal to main feed",
            "Generic Product Item here small text\nhttps://amzn.to/weak1",
            {"https://amzn.to/weak1": "https://www.amazon.in/dp/B0WEAKDEAL1"},
            {"https://amzn.to/weak1": bot.LinkResult(
                source="https://amzn.to/weak1",
                resolved="https://www.amazon.in/dp/B0WEAKDEAL1",
                affiliate="https://www.amazon.in/dp/B0WEAKDEAL1?tag=deals0911-21",
                deal_key="amazon:B0WEAKDEAL1",
            )},
            expect_render_contains=["https://www.amazon.in/dp/B0WEAKDEAL1?tag=deals0911-21"],
            expect_targets_superset=["LootZoneIndia11"],
            msg_id=70,
        )

        # 7c. UNDER99 SOURCE posting an item ABOVE ₹99: it must not be dropped
        # (old behaviour raised PermanentSkip). It still reaches the main feed;
        # only the Under99 price channel is withheld.
        await case(
            store, "under99 source over-99 item still posts to main feed",
            "Premium Smart Watch Big Display\nDeal Price: \u20b92499\nhttps://amzn.to/over99",
            {"https://amzn.to/over99": "https://www.amazon.in/dp/B0OVER99ITM"},
            {"https://amzn.to/over99": bot.LinkResult(
                source="https://amzn.to/over99",
                resolved="https://www.amazon.in/dp/B0OVER99ITM",
                affiliate="https://www.amazon.in/dp/B0OVER99ITM?tag=deals0911-21",
                deal_key="amazon:B0OVER99ITM",
            )},
            expect_render_contains=["https://www.amazon.in/dp/B0OVER99ITM?tag=deals0911-21"],
            expect_targets_superset=["LootZoneIndia11"],
            msg_id=71,
            source=next(iter(bot.UNDER99_SOURCES)),
        )
        row99 = store.conn.execute("SELECT id FROM queue WHERE msg_id=71").fetchone()
        targets99 = await store.pending_targets(row99["id"])
        check("over-99 item withheld from Under99 channel only", "Under99Deals11" not in targets99)

        # 8. Provenance leak fix: an Amazon link carrying OUR Associates tag is
        # self-proving — it passes the final provenance gate even though the
        # link_cache (empty store here) never saw a conversion-API row for it.
        # A foreign-tag Amazon link must still be rejected.
        our_tag = "https://www.amazon.in/dp/B0TAGPROVENANCE?tag=deals0911-21"
        foreign_tag = "https://www.amazon.in/dp/B0TAGPROVENANCE?tag=thief-21"
        check("is_our_amazon_tag_link recognises our tag", bot.Store.is_our_amazon_tag_link(our_tag) is True)
        check("is_our_amazon_tag_link rejects foreign tag", bot.Store.is_our_amazon_tag_link(foreign_tag) is False)
        check("is_our_amazon_tag_link rejects non-amazon", bot.Store.is_our_amazon_tag_link("https://fktr.in/x") is False)
        check("our-tag amazon link passes provenance with EMPTY cache",
              await store.verify_generated_text(f"Deal {our_tag}") is True)
        check("foreign-tag amazon link still fails provenance with empty cache",
              await store.verify_generated_text(f"Deal {foreign_tag}") is False)

        # 9. Promo/footer noise (join/follow/share/notify, t.me self-promo,
        # handles, emoji-only) is stripped; real deal content survives.
        promo = "\n".join([
            "Men Cotton Casual Shirt", "Deal Price: ₹699", "MRP: ₹1999 65% OFF",
            "🔔 JOIN OUR TELEGRAM CHANNEL FOR MORE LOOTS", "📢 t.me/somechannel",
            "Share with your friends", "Turn on notifications",
            "Don't miss this deal!!", "@somechannel", "#deals",
            "Premium cotton fabric slim fit",
            "https://www.amazon.in/dp/B0PROMO123?tag=deals0911-21",
        ])
        cleaned = bot.clean_source_text(promo)
        for gone in ["JOIN OUR TELEGRAM", "t.me/somechannel", "Share with",
                     "notifications", "Don't miss", "@somechannel", "#deals"]:
            check(f"promo noise removed: {gone!r}", gone not in cleaned)
        for kept in ["Men Cotton Casual Shirt", "₹699", "65% OFF",
                     "Premium cotton fabric slim fit", "B0PROMO123"]:
            check(f"deal content kept: {kept!r}", kept in cleaned)
        check("pure promo line detected", bot.is_promo_noise_line("Join now") is True)
        check("real content not promo", bot.is_promo_noise_line("Pack of 2 cotton shirts for men blue") is False)
        # Inline CTA glued to a price/deal line is stripped; the deal survives.
        glued = "Wireless Earbuds Black\nDeal Price: ₹799 grab fast buy now\nMRP: ₹2999"
        cleaned_glued = bot.clean_source_text(glued)
        check("inline CTA removed from price line",
              "grab fast" not in cleaned_glued and "buy now" not in cleaned_glued)
        check("price survives inline CTA strip", "₹799" in cleaned_glued)
        check("product survives inline CTA strip", "Wireless Earbuds" in cleaned_glued)
        check("MRP survives inline CTA strip", "₹2999" in cleaned_glued)
        check("standalone CTA line stripped",
              "don't miss" not in bot.clean_source_text("Shoes ₹499\nDon't miss this deal!!"))
        # Premium channel = ONLY the best: 80%+ discounts, sub-99, best lists,
        # card offers. A 75% mid-price deal does NOT make premium; an 80% one does.
        check("80% deal is premium-eligible",
              bot.eligible_for_premium("Wireless Earbuds 80% off", 1299, 80) is True)
        check("75% mid-price deal is NOT premium",
              bot.eligible_for_premium("Bluetooth Speaker 75% off", 1499, 75) is False)
        check("sub-99 loot is premium-eligible",
              bot.eligible_for_premium("Cotton socks under 99", 89, 60) is True)
        check("40% expensive deal is NOT premium",
              bot.eligible_for_premium("Smart watch 40% off", 2499, 40) is False)

        # FINAL shortening pass: any long affiliate link left in the post (e.g.
        # a long Amazon category/search URL with our tag) is shortened; already
        # short links are never re-shortened; an unavailable shortener keeps the
        # original link.
        long1 = ("https://www.amazon.in/s?i=watches&k=sonata&linkId=abc123&"
                 "rh=n%3A1350387031%2Cn%3A2563504031&rnid=1350387031&"
                 "s=price-asc-rank&tag=deals0911-21")
        long2 = long1.replace("abc123", "def456")
        class _ShortenAff(bot.AffiliateClient):
            def __init__(self): self.cached = {}
            async def shorten(self, long_url):
                return self.shorts.get(long_url)
            async def cache_link(self, source_url, affiliate, resolved, key):
                self.cached[affiliate] = (source_url, key)
        aff_short = _ShortenAff()
        aff_short.shorts = {long1: "https://bit.ly/menwatches", long2: "https://bit.ly/womenwatches"}
        sample = f"Starts At ₹407🔥\nMen : {long1}\nWomen : {long2}\n"
        out = await aff_short.shorten_long_urls_in_text(sample)
        check("long amazon category links shortened",
              "https://bit.ly/menwatches" in out and "https://bit.ly/womenwatches" in out
              and "rh=n%3A" not in out)
        check("shortened links registered in link_cache for provenance",
              "https://bit.ly/menwatches" in aff_short.cached
              and "https://bit.ly/womenwatches" in aff_short.cached)

        class FailAff(bot.AffiliateClient):
            def __init__(self): pass
            async def shorten(self, long_url):
                return None  # Bitly/is.gd unavailable
        kept = await FailAff().shorten_long_urls_in_text(sample)
        check("shortener failure keeps the original long link", long1 in kept and long2 in kept)

        class NoDoubleAff(bot.AffiliateClient):
            def __init__(self): pass
            async def shorten(self, long_url):
                raise AssertionError("must not re-shorten an already-short link")
        short_post = "Watch ₹407\nhttps://bit.ly/abc\nhttps://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21"
        same = await NoDoubleAff().shorten_long_urls_in_text(short_post)
        check("short links not passed to the shortener", same == short_post)

        # Clumsy multi-product list: /dp/ links with smid/psc/th noise collapse to
        # the native short form (free, tag kept); the big hidden-keywords SEARCH
        # link is Bitly-shortened. Post becomes neat.
        check("compact dp link drops smid/psc/th, keeps tag",
              bot.compact_amazon_product_link(
                  "https://www.amazon.in/dp/B0DQPT85TB?psc=1&smid=A1WYWER0W24N8S&tag=deals0911-21")
              == "https://www.amazon.in/dp/B0DQPT85TB?tag=deals0911-21")
        check("compact leaves search links alone",
              "/s?" in bot.compact_amazon_product_link(
                  "https://www.amazon.in/s?hidden-keywords=B0X+%7C+B0Y&tag=deals0911-21"))
        hidden = ("https://www.amazon.in/s?hidden-keywords=B0H3LPRGX3+%7C+B0H36MXL3V+%7C+B0F4NDZ2VC"
                  "&psc=1&th=1&tag=deals0911-21")
        class ListAff(bot.AffiliateClient):
            def __init__(self): self.shortened = {}
            async def shorten(self, u):
                return "https://bit.ly/hiddendeals" if "hidden-keywords" in u else None
            async def cache_link(self, *a, **k): pass
        clumsy = ("More | Apply coupon\n"
                  f"{hidden}\n"
                  "https://www.amazon.in/dp/B0F4NFCHX8?psc=1&smid=A1WYWER0W24N8S&tag=deals0911-21\n"
                  "https://www.amazon.in/dp/B0G1SVRWG3?psc=1&smid=AJ6SIZC8YQDZX&tag=deals0911-21\n")
        neat = await ListAff().shorten_long_urls_in_text(clumsy)
        check("hidden-keywords search link bitly-shortened", "https://bit.ly/hiddendeals" in neat)
        check("dp links compacted to short native form",
              "https://www.amazon.in/dp/B0F4NFCHX8?tag=deals0911-21" in neat
              and "https://www.amazon.in/dp/B0G1SVRWG3?tag=deals0911-21" in neat)
        check("smid/psc noise removed from the post",
              "smid=" not in neat and "psc=1" not in neat)

        # Forwarded/nested message: [[URL](URL)](URL) wrappers with double
        # &amp;amp; escaping -> one clean URL per product, fully decoded.
        nested = (
            "More | Apply coupon\n"
            "[[https://www.amazon.in/s?hidden-keywords=B0H3+%7C+B0H36&amp;amp;psc=1&amp;amp;tag=deals0911-21]"
            "(https://www.amazon.in/s?hidden-keywords=B0H3+%7C+B0H36&amp;psc=1&amp;tag=deals0911-21)]"
            "(https://www.amazon.in/s?hidden-keywords=B0H3+%7C+B0H36&psc=1&tag=deals0911-21)\n"
            "[[https://www.amazon.in/dp/B0DQPT85TB?psc=1&amp;amp;smid=A1WYWER0W24N8S&amp;amp;tag=deals0911-21]"
            "(https://www.amazon.in/dp/B0DQPT85TB?psc=1&amp;smid=A1WYWER0W24N8S&amp;tag=deals0911-21)]"
            "(https://www.amazon.in/dp/B0DQPT85TB?psc=1&smid=A1WYWER0W24N8S&tag=deals0911-21)\n")
        norm = bot.normalize_nested_link_markup(nested)
        check("nested [[url](url)](url) collapses to one url",
              norm.count("B0DQPT85TB") == 1 and "[[" not in norm)
        check("double &amp;amp; fully decoded", "&amp" not in norm)

        class NestedAff(bot.AffiliateClient):
            def __init__(self): pass
            async def shorten(self, u):
                return "https://bit.ly/hiddendeals" if "hidden-keywords" in u else None
            async def cache_link(self, *a, **k): pass
        neat_nested = await NestedAff().shorten_long_urls_in_text(bot.clean_source_text(nested))
        check("nested post tidies to bit.ly + compact dp (no smid/amp/brackets)",
              "https://bit.ly/hiddendeals" in neat_nested
              and "https://www.amazon.in/dp/B0DQPT85TB?tag=deals0911-21" in neat_nested
              and "smid=" not in neat_nested and "&amp" not in neat_nested and "[[" not in neat_nested)

        # USER'S EXACT POST: markdown-wrapped Amazon links, the SAME URL
        # stacked 2-3x in "[url](url](url))" with &amp; escaping and smid/psc
        # noise. Must render as ONE neat short line per product.
        def _md_block(url):
            amp = url.replace("&", "&amp;")
            return f"[{amp}]({amp}]({url}))"
        hidden_full = ("https://www.amazon.in/s?hidden-keywords=B0H3LPRGX3+%7C+B0H36MXL3V"
                       "+%7C+B0F4NDZ2VC+%7C+B0G1SVRWG3+%7C+B0FMF6X8Z5+%7C+B0FMNXX9QS"
                       "+%7C+B0H3LNDF6S+%7C+B0G1MT24K6&psc=1&th=1&tag=deals0911-21")
        dp_asins = ["B0DQPT85TB", "B0F4NFCHX8", "B0FMF6X8Z5",
                    "B0FMNXX9QS", "B0G1SVRWG3", "B0FJ7D2KBQ"]
        user_post = ("More | Apply coupon\n" + _md_block(hidden_full) + "\n" + "\n".join(
            _md_block(f"https://www.amazon.in/dp/{a}?psc=1&smid=A1WYWER0W24N8S&tag=deals0911-21")
            for a in dp_asins))
        cleaned_user = bot.clean_source_text(user_post)
        user_lines = cleaned_user.splitlines()
        # NOTE: some ASINs also appear inside the hidden-keywords SEARCH list,
        # so assert per-LINE URL counts, not raw ASIN counts.
        check("user md post: one URL per line, no brackets/amp, header kept",
              len(user_lines) == 8 and user_lines[0] == "More | Apply coupon"
              and all(len(bot.URL_RE.findall(line)) == 1 for line in user_lines[1:])
              and all(f"/dp/{a}?" in cleaned_user for a in dp_asins)
              and not re.search(r"[\[\]]", cleaned_user) and "&amp" not in cleaned_user)
        class UserPostAff(bot.AffiliateClient):
            def __init__(self): self.calls = 0
            async def shorten(self, u):
                self.calls += 1
                return "https://bit.ly/hiddendeals" if "hidden-keywords" in u else None
            async def cache_link(self, *a, **k): pass
        up_aff = UserPostAff()
        user_final = await up_aff.shorten_long_urls_in_text(cleaned_user)
        check("user md post final: header + 1 bitly search link + 6 compact dp",
              user_final.splitlines()[0] == "More | Apply coupon"
              and "https://bit.ly/hiddendeals" in user_final
              and all(f"https://www.amazon.in/dp/{a}?tag=deals0911-21" in user_final
                      for a in dp_asins)
              and len(user_final.splitlines()) == 8)
        check("user md post: exactly ONE bitly call (no re-shorten per copy)",
              up_aff.calls == 1)
        check("user md post final: zero junk (smid/psc/amp/brackets)",
              "smid=" not in user_final and "psc=1" not in user_final
              and "&amp" not in user_final and not re.search(r"[\[\]()]", user_final))
        # New source channels are registered and fan out to the non-Tricks main
        # targets (Secret + LootZoneIndia11 + PowerLoots1); premium/price/card
        # routes are added dynamically on top.
        for new_source in ("Only_discount_Deals", "amazinglootsdealsoffers", "FlashDealsUnlimited"):
            check(f"{new_source} routed to non-tricks targets",
                  new_source in bot.SOURCE_TO_TARGETS
                  and bot.SOURCE_TO_TARGETS[new_source] == list(bot.NO_TRICKS_TARGETS))
        check("FlashDealsUnlimited registered as a source",
              "FlashDealsUnlimited" in bot.SOURCE_TO_TARGETS)
        check("strip_inline_cta keeps a normal line",
              bot.strip_inline_cta("Premium cotton slim fit shirt") == "Premium cotton slim fit shirt")
        # STRICT global sweep: CTA anywhere in the line is removed; real deal
        # words (free shipping, use code, limited stock, quantity) survive.
        mid_cta = bot.strip_inline_cta("Cotton Kurta Set 70% OFF buy now MRP ₹1999")
        check("mid-line buy now stripped", "buy now" not in mid_cta and "Cotton Kurta Set" in mid_cta)
        noti = bot.strip_inline_cta("Free shipping above ₹499 turn on notifications for more loots")
        check("notification promo stripped, shipping kept",
              "notifications" not in noti and "Free shipping" in noti)
        safe = bot.strip_inline_cta("Use code MYNTRA at checkout · Limited stock · Pack of 2")
        check("coupon/stock/quantity words survive",
              "Use code MYNTRA" in safe and "Limited stock" in safe and "Pack of 2" in safe)
        tme = bot.strip_inline_cta("Follow us on t.me/somechannel for deals ₹299")
        check("t.me self-promo stripped, deal kept", "t.me/" not in tme and "₹299" in tme)
        cleaned_mid = bot.clean_source_text("Shoes ₹499\nGreat deal grab now before gone\nPremium leather")
        check("standalone+inline CTA removed, content kept",
              "grab now" not in cleaned_mid and "Premium leather" in cleaned_mid)
        # 10. Long links qualify for shortening; a clean short amazon /dp does not.
        long_link = ("https://www.amazon.in/s?k=puma+shoes+men&rh=n%3A1571283031"
                     "%2Cn%3A1983396031&rnid=1983396031&s=price-asc-rank&tag=deals0911-21")
        short_dp = "https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21"
        check("long link flagged for shortening", len(long_link) > bot.SHORTEN_MIN_LEN)
        check("short amazon dp not shortened", not (bot.should_use_bitly(short_dp, False) or len(short_dp) > bot.SHORTEN_MIN_LEN))

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


asyncio.run(main())
