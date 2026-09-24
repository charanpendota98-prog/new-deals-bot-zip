"""HYPD creator-store links: OUR link, Bitly-shortened, product remembered.

Run: python3 test_hypd_links.py   (from the repo root)

USER RULE (2026-09-24): "hypd idi meesho products ni mana link tho convert cheyu
... paina links ni perefctga manam links ga chesi cheyu ... bitly tho shorten ga
chesi cheyu convert chesi shorten chesi"

What is pinned here, and why:

1. RECOGNITION. Our store's share link (hypd.store/93944/afflink/<token>) is OUR
   monetized link; another creator's store id is somebody else's money and must
   never be treated as ours.
2. NO UNWRAPPING. resolve() used to follow the redirect, and the tracked
   parameters (affid=infhypd, affExtParam1, affExtParam2) were then stripped as
   "tracking noise" - the user's own commission link became a clean untagged
   page worth nothing. The HYPD link is now final: it is never resolved by the
   pipeline and never sent to EarnKaro.
3. BITLY. Every HYPD link is shortened ("convert chesi shorten chesi"), and the
   short link - not the raw one - is what the post carries and what the
   provenance cache knows.
4. THE PRODUCT BEHIND IT. The merchant page is recorded, so the same product
   arriving later as a bare Meesho/Shopsy link (no EarnKaro campaign) earns on
   the same HYPD link instead of posting untagged.
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

OUR_LINK = "https://hypd.store/93944/afflink/daoli7dtm6mc5h7k1ffg"
OUR_LINK_2 = "https://hypd.store/93944/afflink/daol5bac45l0tc0oo5rg"
OUR_LINK_3 = "https://hypd.store/93944/afflink/daol52dtm6mc5h7k1ejg"
FOREIGN_LINK = "https://hypd.store/88888/afflink/someoneelsestoken1"
DESTINATION = ("https://www.meesho.com/cotton-saree/p/abc12345"
               "?affid=infhypd&affExtParam1=6ab13e1eb6ce5677d060574c"
               "&affExtParam2=daoli7dtm6mc5h7k1ffg")


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
    for link in (OUR_LINK, OUR_LINK_2, OUR_LINK_3):
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
            result = asyncio.run(aff.convert(OUR_LINK_2, False))
            check("a shortener outage never loses the user's own link",
                  bool(result) and result.affiliate == OUR_LINK_2, repr(result))
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
            text = f"Smart deal\n{OUR_LINK_3}"
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
            result = asyncio.run(aff.convert(OUR_LINK_3, False))
            check("the merchant page is read out of the redirect page",
                  bool(result) and "meesho.com/cotton-saree/p/xyz98765" in result.resolved,
                  repr(getattr(result, "resolved", None)))
        finally:
            bot.store = original


def test_config_defaults():
    check("the store id is configurable and defaults to ours",
          bot.HYPD_STORE_ID == "93944", bot.HYPD_STORE_ID)
    check("our store slug is known", "smartdeals" in bot.OUR_HYPD_STORES,
          str(sorted(bot.OUR_HYPD_STORES)))
    check("Bitly shortening of hypd links is on by default", bot.HYPD_ALWAYS_BITLY is True, "")
    check("Meesho and Shopsy are the merchants this covers",
          {"meesho.com", "shopsy.in"} <= bot.HYPD_MERCHANT_DOMAINS, str(bot.HYPD_MERCHANT_DOMAINS))
    source = (Path(bot.__file__).read_text(encoding="utf-8"))
    check("our hypd link is returned before any health/convert path",
          source.index("if is_our_hypd_link(source_url) or is_our_hypd_link(resolved):")
          < source.index('raise RuntimeError("EarnKaro circuit open")'), "ordering changed")


def test_deploy_wiring():
    """The server scripts must SHIP and SHOW this config, not just the code."""
    repo = Path(__file__).parent
    hotfix = (repo / "ops" / "apply_dual_hotfix.sh").read_text(encoding="utf-8")
    for needle, what in (
        ("'HYPD_STORE_ID':'93944'", "bot .env gets our store id"),
        ("'HYPD_ALWAYS_BITLY':'true'", "bot .env gets always-Bitly"),
        ("'HYPD_MERCHANT_DOMAINS':'meesho.com,shopsy.in'", "bot .env gets the merchant list"),
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

    runbook = (repo / "ops" / "HYPD_OUR_LINKS_2026-09-24.txt").read_text(encoding="utf-8")
    for link in (OUR_LINK, OUR_LINK_2, OUR_LINK_3):
        check(f"the runbook lists {link.rsplit('/', 1)[-1]}", link in runbook, "")
    check("the runbook names the Meesho source channel",
          "t.me/+6LA1ljXGlbNmMjA1" in runbook, "")


def main():
    test_recognition()
    test_resolve_never_unwraps_our_link()
    test_convert_publishes_the_hypd_link_bitly_shortened()
    test_bitly_outage_keeps_the_hypd_link()
    test_a_bare_meesho_link_earns_on_the_learned_hypd_link()
    test_foreign_store_link_is_not_republished()
    test_shorten_pass_covers_hypd_links()
    test_destination_from_page_markup()
    test_config_defaults()
    test_deploy_wiring()
    print(f"\nHYPD LINK TESTS PASS ({PASS} checks)")


main()
