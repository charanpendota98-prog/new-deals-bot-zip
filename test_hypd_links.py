"""HYPD creator-store links: verified Meesho only, our link, Bitly-shortened.

Run: python3 test_hypd_links.py   (from the repo root)

USER RULE (2026-09-24): "hypd idi meesho products ni mana link tho convert cheyu
... paina links ni perefctga manam links ga chesi cheyu ... bitly tho shorten ga
chesi cheyu convert chesi shorten chesi"

What is pinned here, and why:

1. RECOGNITION. Our store's share link (hypd.store/93944/afflink/<token>) is
   owned by us; another creator's store id is somebody else's money. Ownership
   alone is not enough: only verified Meesho destinations qualify for HYPD.
2. NO UNWRAPPING. resolve() used to follow the redirect, and the tracked
   parameters (affid=infhypd, affExtParam1, affExtParam2) were then stripped as
   "tracking noise" - the user's own commission link became a clean untagged
   page worth nothing. A verified Meesho HYPD link is final: it is never
   unwrapped or sent to EarnKaro. Shopsy and unknown destinations use no HYPD.
3. BITLY. Each verified Meesho HYPD link is shortened ("convert chesi shorten
   chesi"), and the post carries the short link when available.
4. THE PRODUCT BEHIND IT. The verified Meesho page is recorded, so a later bare
   Meesho link can use the same HYPD link. Shopsy is never routed through HYPD.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hypd-test-")
os.environ.update(
    TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x",
    EARNKARO_API_KEY="k", AMAZON_TAG="",
    BOT_DB_PATH=str(Path(_TMP) / "test.sqlite3"),
    # A token is configured so the fake session's Bitly endpoint is used (with
    # no token at all the client correctly skips Bitly and tries is.gd).
    BITLY_TOKENS="test-token",
)
sys.path.insert(0, str(Path(__file__).parent / "bestgaa"))
import main_bot_new as bot  # noqa: E402

PASS = 0

KNOWN_SHOPSY_LINK = "https://hypd.store/93944/afflink/daoli7dtm6mc5h7k1ffg"
OUR_LINK = "https://hypd.store/93944/afflink/daoll7ltm6mc5h7k1fq0"
FOREIGN_LINK = "https://hypd.store/88888/afflink/someoneelsestoken1"
DESTINATION = ("https://www.meesho.com/cotton-saree/p/abc12345"
               "?affid=infhypd&affExtParam1=6ab13e1eb6ce5677d060574c"
               "&affExtParam2=daoll7ltm6mc5h7k1fq0")


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print(f"ok  {name}")
    else:
        print(f"FAIL {name} <- {detail}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# A fake aiohttp session: it answers GETs (HYPD redirect / health probes) and
# POSTs (EarnKaro, Bitly) and records every call.
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, url, body="", status=200, location=None):
        self.url = url
        self.status = status
        self._body = body.encode() if isinstance(body, str) else body
        self.charset = "utf-8"
        self.content = self

    async def read(self, _limit=None):
        return self._body

    async def text(self):
        return self._body.decode("utf-8", "ignore")

    async def json(self, content_type=None):
        return json.loads(self._body.decode("utf-8"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """GET hypd.store -> the merchant page; GET anything else -> itself (alive)."""

    def __init__(self, hypd_destination=DESTINATION, bitly="https://bit.ly/hypd1",
                 hypd_body=""):
        self.hypd_destination = hypd_destination
        self.bitly = bitly
        self.hypd_body = hypd_body
        self.gets = []
        self.posts = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        if bot.is_hypd_link(url):
            # A redirect when a destination is configured; otherwise the SAME
            # url with whatever body the test wants (the JS-redirect case).
            return FakeResponse(self.hypd_destination or url, self.hypd_body or "<html>ok</html>")
        return FakeResponse(url, "<html>ok</html>")

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        if "bitly" in url:
            if not self.bitly:
                return FakeResponse(url, "{}", status=500)
            return FakeResponse(url, json.dumps({"link": self.bitly}))
        # EarnKaro (must never be called for our HYPD links)
        self.ek_calls = getattr(self, "ek_calls", 0) + 1
        return FakeResponse(url, json.dumps({"success": 0, "message": "no campaign"}))


class Aff(bot.AffiliateClient):
    def __init__(self, session):
        self._health, self._short_cache, self._short_to_long = {}, {}, {}
        self.session = session


def test_recognition():
    for link in (KNOWN_SHOPSY_LINK, OUR_LINK):
        check(f"{link[:46]}... is OUR hypd link", bot.is_our_hypd_link(link), link)
        check("...and is recognised as a hypd link at all", bot.is_hypd_link(link), link)
        check("...with the store read out of the path",
              bot.hypd_store_of(link) == bot.HYPD_STORE_ID, bot.hypd_store_of(link))
        check("...and a stable token", bot.hypd_afflink_token(link).startswith("93944:"),
              bot.hypd_afflink_token(link))
    check("another creator's store is NOT ours", not bot.is_our_hypd_link(FOREIGN_LINK), FOREIGN_LINK)
    check("but is still a hypd link", bot.is_hypd_link(FOREIGN_LINK), FOREIGN_LINK)
    check("a store link with no afflink path is not ours",
          not bot.is_our_hypd_link("https://hypd.store/93944"), "bare store url")
    check("an unrelated host is not ours", not bot.is_hypd_link("https://www.meesho.com/x/p/1"),
          "meesho")
    # The store USERNAME works too (some shares use it instead of the id).
    check("the store username is accepted as well",
          bot.is_our_hypd_link(f"https://hypd.store/{bot.HYPD_STORE_SLUG}/afflink/tok1234567"),
          bot.HYPD_STORE_SLUG)


def test_resolve_never_unwraps_our_link():
    session = FakeSession()
    aff = Aff(session)
    resolved = asyncio.run(aff.resolve(OUR_LINK))
    check("resolve() leaves OUR hypd link exactly as it is", resolved == OUR_LINK, resolved)
    check("and makes no network call for it", not session.gets, str(session.gets))


def test_convert_publishes_the_hypd_link_bitly_shortened():
    # A private DB per run: the link cache is durable.
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd.sqlite3")
        try:
            session = FakeSession(bitly="https://bit.ly/ourhypd")
            aff = Aff(session)
            result = asyncio.run(aff.convert(OUR_LINK, False))
            check("our hypd link converts to a LinkResult", bool(result), repr(result))
            check("the post carries the BITLY link, not the raw one",
                  result.affiliate == "https://bit.ly/ourhypd", repr(result))
            check("Bitly was actually called once",
                  sum(1 for p in session.posts if "bitly" in p["url"]) == 1,
                  str([p["url"] for p in session.posts]))
            check("EarnKaro was never called for our own link",
                  getattr(session, "ek_calls", 0) == 0, str(session.posts))
            check("the merchant page behind the link is recorded as the destination",
                  result.resolved == DESTINATION, repr(result.resolved))
            check("the product identity comes from that page",
                  result.deal_key.startswith("PID:") or result.deal_key.startswith("URL:"),
                  result.deal_key)
            # The provenance gate the delivery path runs must accept the SHORT link.
            ok = asyncio.run(bot.store.verify_generated_text(f"Deal\n{result.affiliate}"))
            check("the short link passes the provenance gate", ok, "not in link_cache")
            # ...and the same product is now known to earn via OUR link.
            learned = asyncio.run(bot.store.hypd_link_for(DESTINATION))
            check("the product -> OUR hypd link map was written", learned == OUR_LINK, str(learned))
        finally:
            bot.store = original


def test_bitly_outage_keeps_the_hypd_link():
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd2.sqlite3")
        try:
            session = FakeSession(bitly=None)          # Bitly + is.gd both unusable
            aff = Aff(session)
            result = asyncio.run(aff.convert(OUR_LINK, False))
            check("a shortener outage never loses the user's own link",
                  bool(result) and result.affiliate == OUR_LINK, repr(result))
        finally:
            bot.store = original


def test_a_bare_meesho_link_earns_on_the_learned_hypd_link():
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd3.sqlite3")
        try:
            # 1. The HYPD link is seen (and learned) first.
            aff = Aff(FakeSession(bitly="https://bit.ly/learned"))
            asyncio.run(aff.convert(OUR_LINK, False))
            # 2. A bare Meesho link for the SAME product arrives.
            session = FakeSession(bitly="https://bit.ly/meesho")
            aff2 = Aff(session)
            meesho = "https://www.meesho.com/cotton-saree/p/abc12345"
            result = asyncio.run(aff2.convert(meesho, False))
            check("the bare Meesho link now earns on OUR hypd link",
                  bool(result) and result.affiliate in ("https://bit.ly/meesho", OUR_LINK),
                  repr(result))
            check("and it is a hypd/Bitly link, never an EarnKaro one",
                  getattr(session, "ek_calls", 0) == 0, str(session.posts))
        finally:
            bot.store = original


def test_foreign_store_link_is_not_republished():
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd4.sqlite3")
        try:
            session = FakeSession(bitly="https://bit.ly/ours")
            aff = Aff(session)
            result = asyncio.run(aff.convert(FOREIGN_LINK, False))
            check("another creator's hypd link is never published as ours",
                  result is None or "hypd.store" not in str(result.affiliate), repr(result))
        finally:
            bot.store = original


def test_shorten_pass_covers_hypd_links():
    """The final shortening pass must Bitly our hypd links even when short."""
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd5.sqlite3")
        try:
            session = FakeSession(bitly="https://bit.ly/final")
            aff = Aff(session)
            text = f"Smart deal\n{OUR_LINK}"
            out = asyncio.run(aff.shorten_long_urls_in_text(text))
            check("the raw hypd link never leaves the bot when Bitly works",
                  "hypd.store" not in out and "bit.ly/final" in out, out)
            # A source's own short link is still never touched.
            out2 = asyncio.run(aff.shorten_long_urls_in_text("Deal\nhttps://bit.ly/source-own"))
            check("a source's own bit.ly is left exactly as it came",
                  out2.endswith("https://bit.ly/source-own"), out2)
        finally:
            bot.store = original


def test_destination_from_page_markup():
    """HYPD links that redirect with JS: the page itself names the merchant."""
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd6.sqlite3")
        try:
            body = ('<script>window.location.href = "https://www.meesho.com/cotton-saree/p/xyz98765'
                    '?affid=infhypd&affExtParam2=tok";</script>')
            # The session returns the SAME url (no server-side redirect) but a
            # body that names the merchant page.
            session = FakeSession(hypd_destination=None, hypd_body=body, bitly="https://bit.ly/js")
            aff = Aff(session)
            result = asyncio.run(aff.convert(OUR_LINK, False))
            check("the merchant page is read out of the redirect page",
                  bool(result) and "meesho.com/cotton-saree/p/xyz98765" in result.resolved,
                  repr(getattr(result, "resolved", None)))
        finally:
            bot.store = original


def test_shopsy_is_not_routed_through_our_hypd_store():
    """Only Meesho may use HYPD; stale Shopsy mappings/cache must be ignored."""
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd-shopsy.sqlite3")
        try:
            shopsy = "https://www.shopsy.in/duffle-bag/p/SHOPSY123"
            key = bot.product_key(shopsy)
            # Simulate a row learned under the old Meesho+Shopsy policy.
            bot.store.conn.execute(
                "INSERT INTO hypd_links(token,store,afflink_url,resolved_url,product_key,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (bot.hypd_afflink_token(KNOWN_SHOPSY_LINK), "93944", KNOWN_SHOPSY_LINK, shopsy, key, 1.0))
            bot.store.conn.commit()
            check("legacy Shopsy mapping is hidden from Meesho listings",
                  not bot.store.recent_hypd_links(), str(bot.store.recent_hypd_links()))
            awaitable = bot.store.cache_link(shopsy, "https://bit.ly/old-shopsy-hypd", shopsy, key)
            asyncio.run(awaitable)

            session = FakeSession(bitly="https://bit.ly/new-link")
            result = asyncio.run(Aff(session).convert(shopsy, False))
            check("a stale Shopsy HYPD short link is not served from cache", result is None,
                  repr(result))
            stale_count = bot.store.conn.execute("SELECT COUNT(*) FROM hypd_links").fetchone()[0]
            check("the stale Shopsy HYPD mapping is removed from storage", stale_count == 0,
                  f"{stale_count} row(s) remain")
            check("Shopsy follows the ordinary EarnKaro path, not Bitly/HYPD",
                  getattr(session, "ek_calls", 0) == 1
                  and not any("bitly" in p["url"] for p in session.posts), str(session.posts))

            # If a source itself supplies an OUR HYPD share link for Shopsy, strip
            # the out-of-scope HYPD hop, try the ordinary network, and keep a clean
            # merchant fallback rather than publishing our HYPD link or dropping it.
            direct_session = FakeSession(
                hypd_destination=shopsy, bitly="https://bit.ly/must-not-use-hypd")
            direct = asyncio.run(Aff(direct_session).convert(OUR_LINK, False))
            check("a direct HYPD link to Shopsy is replaced with the clean merchant URL",
                  bool(direct) and direct.affiliate.startswith("https://www.shopsy.in/")
                  and "hypd.store" not in direct.affiliate and "bit.ly" not in direct.affiliate,
                  repr(direct))
            check("the direct Shopsy HYPD link never enters our product map",
                  not bot.store.recent_hypd_links(), str(bot.store.recent_hypd_links()))
            check("store learning rejects non-Meesho destinations",
                  not asyncio.run(bot.store.remember_hypd_link(OUR_LINK, shopsy, key)), "")
        finally:
            bot.store = original


def test_out_of_scope_hypd_destination_still_earns():
    """USER REPORT 2026-10-04: "product open avuthundi kaani adi mana links kaadu".

    OUR HYPD share link pointing at a NON-Meesho page must not publish that page
    bare when the page itself accepts our publisher id (Flipkart family): the
    fallback is stamped, so the click pays US instead of nobody.
    """
    with tempfile.TemporaryDirectory() as td:
        original, original_id = bot.store, bot.OUR_EK_ID
        bot.store = bot.Store(Path(td) / "hypd-attr.sqlite3")
        bot.OUR_EK_ID = "5478322"
        try:
            flipkart = "https://www.flipkart.com/duffle-bag/p/itmFF123?pid=1"
            session = FakeSession(hypd_destination=flipkart, bitly="https://bit.ly/must-not-use")
            result = asyncio.run(Aff(session).convert(OUR_LINK, False))
            check("an out-of-scope HYPD destination is still published (deal never lost)",
                  bool(result) and "flipkart.com" in result.affiliate, repr(result))
            check("and it carries OUR publisher id instead of opening for free",
                  bool(result) and "affExtParam2=5478322" in result.affiliate, repr(result))
            check("the HYPD hop itself is gone",
                  bool(result) and "hypd.store" not in result.affiliate, repr(result))
        finally:
            bot.store, bot.OUR_EK_ID = original, original_id


def test_unverified_hypd_destination_is_rejected():
    """An unknown merchant page is never published or shortened through HYPD."""
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd-unverified.sqlite3")
        try:
            body = '<script>window.location="https://merchant.example/product/123"</script>'
            session = FakeSession(hypd_destination=None, hypd_body=body,
                                  bitly="https://bit.ly/must-not-use")
            result = asyncio.run(Aff(session).convert(OUR_LINK, False))
            check("an unknown HYPD destination is rejected", result is None, repr(result))
            check("unknown destinations are neither shortened nor sent to EarnKaro",
                  not session.posts, str(session.posts))
            check("an unknown destination cannot be learned",
                  not asyncio.run(bot.store.remember_hypd_link(
                      OUR_LINK, "https://merchant.example/product/123", "PID:123")), "")
        finally:
            bot.store = original


def test_config_defaults():
    check("the store id is configurable and defaults to ours",
          bot.HYPD_STORE_ID == "93944", bot.HYPD_STORE_ID)
    check("our store slug is known", "smartdeals" in bot.OUR_HYPD_STORES,
          str(sorted(bot.OUR_HYPD_STORES)))
    check("Bitly shortening of hypd links is on by default", bot.HYPD_ALWAYS_BITLY is True, "")
    check("HYPD is scoped to Meesho only",
          bot.HYPD_MERCHANT_DOMAINS == {"meesho.com"}, str(bot.HYPD_MERCHANT_DOMAINS))
    check("Shopsy is deliberately excluded from HYPD",
          "shopsy.in" not in bot.HYPD_MERCHANT_DOMAINS, str(bot.HYPD_MERCHANT_DOMAINS))
    source = (Path(bot.__file__).read_text(encoding="utf-8"))
    check("our hypd link is returned before any health/convert path",
          source.index("if is_our_hypd_link(source_url) or is_our_hypd_link(resolved):")
          < source.index('raise RuntimeError("EarnKaro circuit open")'), "ordering changed")


def test_the_curation_queue_records_what_cannot_earn_yet():
    """A Meesho product with no curated HYPD link must NOT fail silently.

    Without a curated HYPD link, a Meesho post earns nothing. The bot records
    it as a to-do
    (ops/hypd_links.py --wanted) and says so once, with the exact fix.
    """
    with tempfile.TemporaryDirectory() as td:
        original = bot.store
        bot.store = bot.Store(Path(td) / "hypd6.sqlite3")
        try:
            meesho = "https://www.meesho.com/cotton-saree/p/none99999"
            aff = Aff(FakeSession(bitly="https://bit.ly/x"))
            result = asyncio.run(aff.convert(meesho, False))
            wanted = bot.store.recent_hypd_wanted(limit=10)
            check("a bare Meesho link with NO curated HYPD link earns nothing (yet)",
                  result is None or "hypd.store" not in str(result.affiliate), repr(result))
            check("and the product lands on the curation to-do list",
                  any(w["product_url"] == meesho for w in wanted), str(wanted))
            check("the to-do entry carries the product identity",
                  any(w["product_key"] for w in wanted), str(wanted))

            # Learning the link clears the to-do and monetizes from now on.
            asyncio.run(bot.store.remember_hypd_link(
                OUR_LINK, meesho,
                "PID:www.meesho.com:none99999"))
            check("learning the HYPD link clears the to-do entry",
                  not bot.store.recent_hypd_wanted(limit=10), str(bot.store.recent_hypd_wanted()))
            session = FakeSession(bitly="https://bit.ly/nowours")
            result = asyncio.run(Aff(session).convert(meesho, False))
            # The post carries the Bitly link; Bitly was asked to shorten OUR
            # hypd link - that is the proof the commission link is ours.
            shortened_urls = [p.get("json", {}).get("long_url") for p in session.posts
                              if "bitly" in str(p.get("url"))]
            check("and the same product now earns on OUR link",
                  bool(result) and result.affiliate == "https://bit.ly/nowours"
                  and OUR_LINK in shortened_urls, repr(result) + str(shortened_urls))
        finally:
            bot.store = original


def test_the_ops_tool_shows_the_to_do_list():
    source = (Path(__file__).parent / "ops" / "hypd_links.py").read_text(encoding="utf-8")
    check("ops/hypd_links.py has a --wanted listing",
          '"--wanted"' in source and "recent_hypd_wanted" in source, "")
    check("and it tells the operator exactly what to do about it",
          "curate the product" in source and "copy its share link" in source, "")
    # The deploy gate must run this suite.
    deploy = (Path(__file__).parent / "ops" / "deploy_and_verify.sh").read_text(encoding="utf-8")
    check("the deploy gate runs the hypd suite", "test_hypd_links" in deploy, "")


def test_deploy_wiring():
    """The server scripts must SHIP and SHOW this config, not just the code."""
    repo = Path(__file__).parent
    hotfix = (repo / "ops" / "apply_dual_hotfix.sh").read_text(encoding="utf-8")
    for needle, what in (
        ("'HYPD_STORE_ID':'93944'", "bot .env gets our store id"),
        ("'HYPD_STORE_SLUG':'smartdeals'", "bot .env gets our store slug"),
        ("'HYPD_ALWAYS_BITLY':'true'", "bot .env gets always-Bitly"),
        ("'HYPD_MERCHANT_DOMAINS':'meesho.com'", "bot .env pins HYPD to Meesho only"),
        ("'HYPD_STORES':'93944,smartdeals'", "bridge .env gets our store ids"),
    ):
        check(what, needle in hotfix, needle)

    diagnose = (repo / "ops" / "diagnose.sh").read_text(encoding="utf-8")
    check("diagnose.sh shows the live HYPD config",
          "HYPD_STORE_ID:$BESTGAA_DIR/.env" in diagnose
          and "HYPD_STORES:$BRIDGE_DIR/.env" in diagnose, "")

    deploy = (repo / "ops" / "deploy_and_verify.sh").read_text(encoding="utf-8")
    check("the deploy gate runs this suite before shipping",
          "test_hypd_links" in deploy, "")
    check("and the EarnKaro suite too",
          "test_earnkaro_conversion" in deploy, "")

    tool = repo / "ops" / "hypd_links.py"
    check("the ops tool exists", tool.exists(), str(tool))
    source = tool.read_text(encoding="utf-8")
    check("the ops tool learns links through the bot's own client",
          "bot.AffiliateClient" in source and "remember_hypd_link" in source, "")
    check("and never stores a foreign creator's store as ours",
          "NEVER used as ours" in source, "")
    check("the HYPD tool requires a verified Meesho destination",
          "HYPD_MERCHANT_DOMAINS" in source and "NOT STORED" in source, "")
    check("HYPD dry-run resolves without mutating bot state",
          "client._hypd_destination(url)" in source and "Do not call convert()" in source, "")

    fresh = repo / "ops" / "deploy_fresh.sh"
    check("ops/deploy_fresh.sh exists (the one-command fresh deploy)", fresh.exists(), str(fresh))
    fresh_src = fresh.read_text(encoding="utf-8") if fresh.exists() else ""
    for needle, what in (
        ("./ops/deploy_and_verify.sh", "it ships through deploy_and_verify.sh"),
        ("ops/earnkaro_check.py", "it proves the EarnKaro key live"),
        ("ops/hypd_links.py", "it teaches OUR hypd links"),
        ("ops/conversion_report.py", "it prints the per-route status"),
        ("--dry-run", "it can show the plan without changing anything"),
        ("--verify-only", "it can verify without deploying"),
    ):
        check(what, needle in fresh_src, needle)
    check("the fresh deploy verifies our new HYPD candidate before learning it",
          "daoll7ltm6mc5h7k1fq0" in fresh_src, "")
    check("the fresh deploy never learns the known Shopsy HYPD link",
          "daoli7dtm6mc5h7k1ffg" not in fresh_src, "")
    check("and it never unwraps our links (no resolver is called on them)",
          "resolve_our" not in fresh_src, "")

    runbook = (repo / "ops" / "HYPD_OUR_LINKS_2026-09-24.txt").read_text(encoding="utf-8")
    for link in (OUR_LINK,):
        check(f"the Meesho-only runbook records candidate {link.rsplit('/', 1)[-1]}", link in runbook, "")
    check("the runbook labels the known Shopsy link as excluded",
          KNOWN_SHOPSY_LINK in runbook and "stays excluded from Meesho routes" in runbook, "")
    check("the runbook pins the merchant configuration to Meesho",
          "HYPD_MERCHANT_DOMAINS=meesho.com" in runbook, "")


def main():
    test_recognition()
    test_resolve_never_unwraps_our_link()
    test_convert_publishes_the_hypd_link_bitly_shortened()
    test_bitly_outage_keeps_the_hypd_link()
    test_a_bare_meesho_link_earns_on_the_learned_hypd_link()
    test_foreign_store_link_is_not_republished()
    test_shorten_pass_covers_hypd_links()
    test_destination_from_page_markup()
    test_shopsy_is_not_routed_through_our_hypd_store()
    test_out_of_scope_hypd_destination_still_earns()

    test_unverified_hypd_destination_is_rejected()
    test_config_defaults()
    test_deploy_wiring()
    test_the_curation_queue_records_what_cannot_earn_yet()
    test_the_ops_tool_shows_the_to_do_list()
    print(f"\nHYPD LINK TESTS PASS ({PASS} checks)")


main()
