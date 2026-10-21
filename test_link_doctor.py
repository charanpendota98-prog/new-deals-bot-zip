#!/usr/bin/env python3
"""Link doctor + the "no unprovable hop" policy.

The report that produced this file (2026-10-21): two `https://is.gd/xxxx` links
in a channel, both resolving to UNTAGGED `amazon.in/dp/...` pages. They open
fine, so nothing looked broken - and every click earned nobody. The bot's own
auditor calls them FOREIGN: an is.gd hop carries no publisher and no record of
who minted it.

What is pinned here:
  * the doctor's verdicts (ours / unproven hop / dead / unreachable / loop),
    including real redirect-following against a local server,
  * the repaired link it prints (the same page with OUR tag),
  * the sweep over a real database, with a planted bad hop,
  * the bot: no tokenless hop is minted by default, no untagged Amazon link is
    ever shortened, a hop is never wrapped in another hop, and a tokenless hop
    without proof is refused at pre-send even with strict mode off.

No Telegram, no internet: the only network is a loopback aiohttp server.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
os.environ.setdefault("EARNKARO_PUBLISHER_ID", "5478322")
os.environ.setdefault("AMAZON_TAG", "mama086-21")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))

import main_bot_new as bot  # noqa: E402
from ops import link_doctor as doctor  # noqa: E402

FAILS: list[str] = []
TAG = "mama086-21"
PUB = "5478322"


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


# --------------------------------------------------------------------------- #
# a loopback server that behaves like a shortener
# --------------------------------------------------------------------------- #
def make_app():
    async def health(_request):
        return web.json_response({"ok": True})

    async def hop(request):
        """302 to /product (a live page) or /missing (a 404)."""
        target = request.query.get("to", "product")
        raise web.HTTPFound(f"/{target}" + ("?tag=" + TAG if request.query.get("tag") == "1" else ""))

    async def product(_request):
        return web.Response(text="<html><title>Blanket</title>Price ₹100</html>",
                            content_type="text/html")

    async def missing(_request):
        return web.Response(status=404, text="<html>Page Not Found</html>",
                            content_type="text/html")

    async def soft404(_request):
        # A 200 whose body is a "page not found" page: the shape that looks fine
        # to a status-code check and dead to a person.
        return web.Response(text="<html>Sorry! We couldn't find that page</html>",
                            content_type="text/html")

    async def loop(request):
        raise web.HTTPFound(f"/loop?n={int(request.query.get('n', '0')) + 1}")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/hop", hop)
    app.router.add_get("/product", product)
    app.router.add_get("/missing", missing)
    app.router.add_get("/soft404", soft404)
    app.router.add_get("/loop", loop)
    return app


async def start_server():
    runner = web.AppRunner(make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------- #
# 1. the doctor's verdicts
# --------------------------------------------------------------------------- #
def test_verdicts():
    print("\n== link doctor: does it work, where does it land, is it ours ==")
    audit = doctor.load_audit()
    minted = {"https://bitli.in/our-proofed", "https://bit.ly/our-proofed"}
    off = doctor.classify("https://is.gd/c3Gl9d", audit=audit, minted=minted, tag=TAG,
                          publisher=PUB, offline=True)
    check("the reported is.gd link is an unproven hop", off["verdict"] == "UNPROVEN-HOP",
          off["verdict"])
    check("...and the reason names the tokenless problem",
          "tokenless" in off["why"], off["why"])
    check("...and it is a hop", off["hop"] is True)

    ours = doctor.classify(f"https://www.amazon.in/dp/B0HNPB6QBS?tag={TAG}", audit=audit,
                           minted=minted, tag=TAG, publisher=PUB, offline=True)
    check("a tagged Amazon link is ours", ours["verdict"] == "OURS", ours["verdict"])
    foreign = doctor.classify("https://www.amazon.in/dp/B0HNPB6QBS?tag=someoneelse-21",
                              audit=audit, minted=minted, tag=TAG, publisher=PUB,
                              offline=True)
    check("a foreign tag is not ours", foreign["verdict"] == "NOT-OURS", foreign["verdict"])
    bare = doctor.classify("https://www.amazon.in/dp/B0HNPB6QBS", audit=audit,
                           minted=minted, tag=TAG, publisher=PUB, offline=True)
    check("an untagged product page is not ours", bare["verdict"] == "NOT-OURS",
          bare["verdict"])
    proven = doctor.classify("https://bitli.in/our-proofed", audit=audit, minted=minted,
                             tag=TAG, publisher=PUB, offline=True)
    check("a hop WITH a stored proof is ours", proven["verdict"] == "OURS",
          proven["verdict"] + " " + proven["why"])
    short_proof = doctor.classify("https://is.gd/proofed", audit=audit,
                                  minted={"https://is.gd/proofed"}, tag=TAG,
                                  publisher=PUB, offline=True)
    check("even a tokenless hop is ours when the cache proves we minted it",
          short_proof["verdict"] == "OURS", short_proof["verdict"])

    check("the repaired link is the same page with our tag",
          doctor.repair("https://www.amazon.in/dp/B0HNPB6QBS?ref_=x&tag=old-21", TAG, PUB)
          == f"https://www.amazon.in/dp/B0HNPB6QBS?ref_=x&tag={TAG}",
          doctor.repair("https://www.amazon.in/dp/B0HNPB6QBS?ref_=x&tag=old-21", TAG, PUB))
    check("the repaired link carries no duplicate tag",
          doctor.repair("https://www.amazon.in/dp/B0X?tag=a&tag=b", TAG, PUB).count("tag=") == 1,
          doctor.repair("https://www.amazon.in/dp/B0X?tag=a&tag=b", TAG, PUB))
    check("a Flipkart page is repaired with our publisher id",
          "affExtParam2=" + PUB in doctor.repair(
              "https://www.flipkart.com/x/p/itm1?pid=P1", TAG, PUB),
          doctor.repair("https://www.flipkart.com/x/p/itm1?pid=P1", TAG, PUB))
    check("nothing is invented for a host we cannot attribute",
          doctor.repair("https://example.com/deal", TAG, PUB) == "")
    check("a service link is not treated as a hop",
          doctor.looks_like_hop("https://t.me/LootZoneIndia11") is False)
    check("an unknown single-slug host IS treated as a hop",
          doctor.looks_like_hop("https://loot.deals/xYz9") is True)


def test_network():
    print("\n== link doctor: real redirects (loopback) ==")
    audit = doctor.load_audit()
    minted: set[str] = set()

    async def run():
        runner, base = await start_server()
        try:
            good = await doctor.follow(aiohttp.ClientSession(), f"{base}/hop")
            missing = await doctor.follow(aiohttp.ClientSession(), f"{base}/missing")
            soft = await doctor.follow(aiohttp.ClientSession(), f"{base}/soft404")
            loop = await doctor.follow(aiohttp.ClientSession(), f"{base}/loop",
                                       max_hops=3)
            dead_host = await doctor.follow(
                aiohttp.ClientSession(), "http://127.0.0.1:1/nothing", timeout=3)
            return base, good, missing, soft, loop, dead_host
        finally:
            await runner.cleanup()

    base, good, missing, soft, loop, dead_host = asyncio.run(run())

    item = doctor.classify(f"{base}/hop", audit=audit, minted=minted, tag=TAG,
                           publisher=PUB, probe=good)
    check("a hop is followed to the page it lands on",
          item["final"].endswith("/product") and item["status"] == 200, str(item))
    check("...and the path is reported hop by hop", len(good["chain"]) >= 2,
          str(good["chain"]))
    check("...and it is still not ours", item["verdict"] == "UNPROVEN-HOP", item["verdict"])
    check("...and no repaired link is invented for a host we cannot attribute",
          item["repair"] == "", item["repair"])
    # The real case: a hop that lands on an Amazon product page. The repair is the
    # product page itself, carrying our tag - exactly what should have been posted.
    landed = doctor.classify(
        "https://is.gd/c3Gl9d", audit=audit, minted=minted, tag=TAG, publisher=PUB,
        probe={"chain": [{"url": "https://is.gd/c3Gl9d", "status": 302,
                          "location": "https://www.amazon.in/dp/B0HNPB6QBS"},
                         {"url": "https://www.amazon.in/dp/B0HNPB6QBS", "status": 200,
                          "location": ""}],
               "final": "https://www.amazon.in/dp/B0HNPB6QBS", "status": 200,
               "body": "<html>Blanket ₹100</html>", "error": ""})
    check("an is.gd hop to an untagged product page is UNPROVEN-HOP",
          landed["verdict"] == "UNPROVEN-HOP", landed["verdict"])
    check("...the reason says the destination earns nothing",
          "earns nothing" in landed["why"], landed["why"])
    check("...and the repaired link is the tagged product page",
          landed["repair"] == f"https://www.amazon.in/dp/B0HNPB6QBS?tag={TAG}",
          landed["repair"])

    dead = doctor.classify(f"{base}/missing", audit=audit, minted=minted, tag=TAG,
                           publisher=PUB, probe=missing)
    check("a 404 is DEAD", dead["verdict"] == "DEAD", dead["verdict"])
    check("...and the status is shown", dead["status"] == 404, str(dead["status"]))

    soft_dead = doctor.classify(f"{base}/soft404", audit=audit, minted=minted, tag=TAG,
                                publisher=PUB, probe=soft)
    check("a 200 whose page says 'not found' is DEAD too",
          soft_dead["verdict"] == "DEAD", soft_dead["verdict"])

    looping = doctor.classify(f"{base}/loop", audit=audit, minted=minted, tag=TAG,
                              publisher=PUB, probe=loop)
    check("a redirect loop is LOOP", looping["verdict"] == "LOOP", looping["verdict"])

    unreachable = doctor.classify("http://127.0.0.1:1/x", audit=audit, minted=minted,
                                  tag=TAG, publisher=PUB, probe=dead_host)
    check("a host with nothing behind it is UNREACHABLE", 
          unreachable["verdict"] == "UNREACHABLE", unreachable["verdict"])


# --------------------------------------------------------------------------- #
# 2. the sweep over a real database
# --------------------------------------------------------------------------- #
def build_db(path: Path, now: float) -> None:
    store = bot.Store(path)
    store.conn.executemany(
        "INSERT INTO link_cache(source_url,affiliate_url,resolved_url,deal_key,created_at) "
        "VALUES(?,?,?,?,?)",
        [("https://www.amazon.in/dp/B0GOOD00001", "https://bitli.in/good1",
          "https://www.amazon.in/dp/B0GOOD00001", "ASIN:B0GOOD00001", now - 60),
         ("https://www.amazon.in/dp/B0HNPB6QBS", "https://is.gd/c3Gl9d",
          "https://www.amazon.in/dp/B0HNPB6QBS", "ASIN:B0HNPB6QBS", now - 120),
         ("https://www.amazon.in/dp/B097GNDJ37", "https://is.gd/bO79HA",
          "https://www.amazon.in/dp/B097GNDJ37", "ASIN:B097GNDJ37", now - 180)])
    store.conn.execute(
        "INSERT INTO owned_link_proofs(affiliate_url,destination_url,proof_type,publisher_id,"
        "created_at) VALUES(?,?,?,?,?)",
        ("https://bitli.in/good1", f"https://www.amazon.in/dp/B0GOOD00001?tag={TAG}",
         "amazon_native", TAG, now - 60))
    store.conn.execute(
        "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
        "chat_key,rendered_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (1, -1, 1, "DealSauce", "done", 1, "", now - 300, "-1",
         "VOK57 blanket ₹100\nhttps://is.gd/c3Gl9d", "[]"))
    store.conn.commit()
    store.conn.close()


def test_sweep():
    print("\n== link doctor: the sweep over what was actually published ==")
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "bot.sqlite3"
        build_db(db, now)
        code = doctor.main(["--recent", "10", "--db", str(db), "--offline"])
        check("the sweep answers without the network", code == 0, str(code))
        strict = doctor.main(["--recent", "10", "--db", str(db), "--offline", "--strict"])
        check("--strict exits 1 when a published hop is unproven", strict == 1, str(strict))
        clean_db = Path(td) / "clean.sqlite3"
        store = bot.Store(clean_db)
        store.conn.execute(
            "INSERT INTO link_cache VALUES(?,?,?,?,?)",
            ("https://www.amazon.in/dp/B0GOOD00002",
             f"https://www.amazon.in/dp/B0GOOD00002?tag={TAG}",
             f"https://www.amazon.in/dp/B0GOOD00002?tag={TAG}", "ASIN:B0GOOD00002", now))
        store.conn.commit()
        store.conn.close()
        check("--strict exits 0 when every link is ours",
              doctor.main(["--recent", "5", "--db", str(clean_db), "--offline",
                           "--strict"]) == 0)

        # JSON is the machine-readable form of the same answer
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            doctor.main(["--recent", "10", "--db", str(db), "--offline", "--json"])
        payload = json.loads(buffer.getvalue())
        verdicts = {item["url"]: item["verdict"] for item in payload["results"]}
        check("json reports both planted hops as unproven",
              verdicts.get("https://is.gd/c3Gl9d") == "UNPROVEN-HOP"
              and verdicts.get("https://is.gd/bO79HA") == "UNPROVEN-HOP", str(verdicts))
        check("json still reports our proven link as ours",
              verdicts.get("https://bitli.in/good1") == "OURS", str(verdicts))
        check("the queue text is swept too, not just the cache",
              "https://is.gd/c3Gl9d" in verdicts, str(list(verdicts)))

        # a missing database is a sentence, not a crash
        check("a missing database is a sentence with exit 2, not a crash",
              doctor.main(["--recent", "5", "--db", str(Path(td) / "nope.sqlite3"),
                           "--offline"]) == 2)


# --------------------------------------------------------------------------- #
# 3. the bot no longer creates the class
# --------------------------------------------------------------------------- #
def test_policy():
    print("\n== bot policy: no unprovable hop is minted or published ==")
    check("the tokenless fallback is off unless a host opts in",
          bot.TOKENLESS_SHORTENER is False, str(bot.TOKENLESS_SHORTENER))
    may = bot.AffiliateClient.may_mint_short_link
    tag_up = bot.AffiliateClient.with_our_attribution
    check("an untagged Amazon page is tagged BEFORE a hop is minted",
          tag_up("https://www.amazon.in/dp/B0HNPB6QBS")
          == f"https://www.amazon.in/dp/B0HNPB6QBS?tag={TAG}",
          tag_up("https://www.amazon.in/dp/B0HNPB6QBS"))
    check("a stranger's tag on the destination is replaced, never carried along",
          tag_up("https://www.amazon.in/dp/B0HNPB6QBS?tag=theirs-21")
          == f"https://www.amazon.in/dp/B0HNPB6QBS?tag={TAG}")
    check("a non-Amazon URL is left exactly as it is",
          tag_up("https://www.flipkart.com/x/p/itm1?pid=P1") ==
          "https://www.flipkart.com/x/p/itm1?pid=P1")
    check("our tagged Amazon page can be shortened",
          may(f"https://www.amazon.in/dp/B0HNPB6QBS?tag={TAG}") is True)
    check("a hop is never wrapped in another hop",
          may("https://is.gd/c3Gl9d") is False and may("https://bitli.in/x") is False)
    check("a long converter URL can still be shortened",
          may("https://ekaro.in/enkr20261021longlink") is False
          and may("https://www.flipkart.com/long-product-page/p/itmabc?pid=P1&lid=xyz") is True,
          "our own short domains are hops; a plain merchant page is not")

    async def run():
        aff = bot.AffiliateClient.__new__(bot.AffiliateClient)
        aff._short_cache, aff._short_to_long = {}, {}
        aff.session = _FakeSession(bitly_ok=False, isgd_ok=True)
        aff.bitly = _returns(None)
        return aff

    class _FakeSession:
        def __init__(self, bitly_ok, isgd_ok):
            self.bitly_ok, self.isgd_ok = bitly_ok, isgd_ok
            self.gets = []

        def get(self, url, **kwargs):
            self.gets.append((url, kwargs.get("params", {})))
            return _FakeResponse("https://is.gd/minted123")

    class _FakeResponse:
        def __init__(self, text):
            self.status, self._text = 200, text

        async def text(self):
            return self._text

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _returns(value):
        async def inner(*args, **kwargs):
            return value
        return inner

    async def shorten_cases():
        aff = await run()
        out = await aff.shorten("https://www.amazon.in/s?k=blanket&tag=" + TAG)
        isgd_called = aff.session.gets
        untagged = await aff.shorten("https://www.amazon.in/dp/B0HNPB6QBS")
        hop = await aff.shorten("https://is.gd/c3Gl9d")
        return out, isgd_called, untagged, hop

    async def shorten_tagged_target():
        aff = await run()                       # Bitly down, is.gd would answer
        bot.TOKENLESS_SHORTENER = True          # opt in for this one call
        try:
            result = await aff.shorten("https://www.amazon.in/dp/B0HNPB6QBS")
        finally:
            bot.TOKENLESS_SHORTENER = False
        return result, list(aff.session.gets)

    tagged_result, asked = asyncio.run(shorten_tagged_target())
    asked_targets = [params.get("url", "") for _url, params in asked]
    check("the shortener is asked for the TAGGED destination, not the bare page",
          any(f"tag={TAG}" in target for target in asked_targets), str(asked_targets))
    check("...and the hop it returns is the one published", tagged_result == "https://is.gd/minted123",
          str(tagged_result))

    out, isgd_called, untagged, hop = asyncio.run(shorten_cases())
    check("with Bitly down the shortener returns nothing, so the native link is kept",
          out is None, str(out))
    check("...and is.gd is never even called", isgd_called == [], str(isgd_called))
    check("an untagged Amazon link is refused before any network call", untagged is None)
    check("a hop is refused before any network call", hop is None)

    # pre-send: a tokenless hop without proof is refused even with strict mode OFF
    async def gate():
        with tempfile.TemporaryDirectory() as td:
            store = bot.Store(Path(td) / "s.sqlite3")
            saved = (bot.store, bot.STRICT_OUR_LINKS)
            bot.store, bot.STRICT_OUR_LINKS = store, False
            try:
                ok, bad = await store.presend_links_verified("Deal\nhttps://is.gd/c3Gl9d")
                tagged = f"https://www.amazon.in/dp/B0HNPB6QBS?tag={TAG}"
                ok_tagged, _ = await store.presend_links_verified(f"Deal\n{tagged}")
                verified = await store.verify_generated_text("Deal\nhttps://is.gd/c3Gl9d")
                return ok, bad, ok_tagged, verified
            finally:
                bot.store, bot.STRICT_OUR_LINKS = saved
                store.conn.close()

    ok, bad, ok_tagged, verified = asyncio.run(gate())
    check("an unproven tokenless hop is refused at pre-send, strict or not",
          ok is False and bad == "https://is.gd/c3Gl9d", f"{ok} {bad}")
    check("...while our tagged native link passes", ok_tagged is True)
    check("...and the legacy verify path refuses it as well", verified is False)


def main() -> int:
    test_verdicts()
    test_network()
    test_sweep()
    test_policy()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: " + "; ".join(FAILS[:6]))
        return 1
    print("\nlink doctor PASS: real redirects, dead and looping links, a repaired "
          "link for every one that is not ours, the published-links sweep, and a bot "
          "that neither mints nor posts an unprovable hop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
