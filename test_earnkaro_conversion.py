"""EarnKaro conversion contract + the 2026-09-24 first-preference sources.

Run: python3 test_earnkaro_conversion.py   (from the repo root)

What this file pins, and why each check exists:

1.  THE REQUEST. The public converter is called with `convert_option:
    "convert_only"` and a Bearer token. Omitting convert_option let the
    account-level default decide the RESPONSE, and a response shape the bot
    could not read meant a post that went out unmonetized ("perfectgaa convert
    avvatleu").
2.  EVERY RESPONSE SHAPE. `data` has been seen as the link, as the whole deal
    text with the link inside it, as a list, and as an object of link fields;
    failures carry a sentence ("Url not found in post!") with success 0.
3.  THE KEY. The token is a JWT naming the EarnKaro publisher that gets paid;
    the bot reads that publisher from the token, pins it (foreign-publisher
    guard) and says out loud which account is earning.
4.  AMAZON. AMAZON_VIA_EARNKARO=true sends Amazon to EarnKaro like every other
    store (Associates is still rejecting the account, so a native tag earns
    nothing); the native tagged link is the fallback, and `=false` restores the
    2026-09-06 pure-native behaviour.
5.  THE THREE NEW SOURCES. They fan out to the non-Tricks main targets, are
    recognised as first preference however they are spelled (invite hashes are
    case-carrying), and are claimed before ordinary sources.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="earnkaro-test-")

# The WORKING SHAPE of the converter token (a JWT whose payload names the
# publisher that gets paid) with a deliberately fake signature: the real token is
# a secret and never belongs in the repository. The claims are the ones the live
# token carries. The suite SUPPLIES it as EARNKARO_API_KEY on purpose: a test
# must not depend on whichever .env happens to sit next to it (a fresh clone has
# no .env at all, and a developer machine has a real one).
TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJfaWQiOiI2YTZmOTA0OTZkZmY5NjY1Njk2ZmM2MjQiLCJlYXJua2FybyI6IjU0NzgzMjIiLCJpYXQiOjE3OTAyNzM3MTB9."
    "not-a-real-signature-only-the-claims-matter"
)

os.environ.update(
    TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x",
    EARNKARO_API_KEY=TOKEN, AMAZON_TAG="",
    # A private DB: the link cache is durable, and a cached row from another
    # suite would be returned instead of the link this test's fake API minted.
    BOT_DB_PATH=str(Path(_TMP) / "test.sqlite3"),
)
sys.path.insert(0, str(Path(__file__).parent / "bestgaa"))
import main_bot_new as bot  # noqa: E402

PASS = 0


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print(f"ok  {name}")
    else:
        print(f"FAIL {name} <- {detail}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# 1. Response parsing: every shape the API has actually answered with
# ---------------------------------------------------------------------------
def test_response_shapes():
    real = "https://ekaro.in/enkr20260924123456789"
    cases = [
        ("a plain link",
         json.dumps({"success": 1, "data": real}), real),
        ("a link inside the returned deal text",
         json.dumps({"success": 1,
                     "data": "boAt Rockerz 255 at \u20b9899\n" + real + "\nBuy fast"}),
         real),
        ("a list of links",
         json.dumps({"success": 1, "data": ["not a link", real]}), real),
        ("an object of link fields",
         json.dumps({"success": 1, "data": {"converted_url": real, "merchant": "Amazon"}}),
         real),
        ("a nested object",
         json.dumps({"success": 1, "data": {"links": [{"url": real}]}}), real),
        ("success as a string",
         json.dumps({"success": "1", "data": real}), real),
        ("status instead of success",
         json.dumps({"status": "success", "data": real}), real),
        ("a trailing bracket after the link",
         json.dumps({"success": 1, "data": real + ")"}), real),
        ("a url-encoded query survives",
         json.dumps({"success": 1, "data": "https://www.flipkart.com/x/p/itm1?pid=1&lid=2"}),
         "https://www.flipkart.com/x/p/itm1?pid=1&lid=2"),
    ]
    for name, body, expected in cases:
        got, reason = bot.parse_earnkaro_response(body)
        check(f"parsed {name}", got == expected, f"got={got!r} reason={reason!r}")

    failures = [
        ("success=0 with the API's own sentence",
         json.dumps({"success": 0, "message": "Url not found in post!"})),
        ("'could not locate' data",
         json.dumps({"success": 0, "data": "could not locate any product link"})),
        ("an empty body", ""),
        ("an HTML error page", "<html><body>502 Bad Gateway</body></html>"),
        ("a payload with no url at all", json.dumps({"success": 1, "data": {"merchant": "Amazon"}})),
    ]
    for name, body in failures:
        got, reason = bot.parse_earnkaro_response(body)
        check(f"refused {name}", got is None and bool(reason), f"got={got!r} reason={reason!r}")

    _, reason = bot.parse_earnkaro_response(json.dumps({"success": 0, "message": "Url not found in post!"}))
    check("the API's own words reach the log", "Url not found in post!" in reason, reason)


# ---------------------------------------------------------------------------
# 2. The request itself (convert_option + Bearer token) and what we publish
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Records every POST and answers with the next canned body."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        body = self.bodies.pop(0) if self.bodies else json.dumps({"success": 0, "message": "exhausted"})
        return FakeResponse(body)


class Aff(bot.AffiliateClient):
    """The real client with the network and the DB taken out."""

    def __init__(self, session):
        self._health, self._short_cache, self._short_to_long = {}, {}, {}
        self.session = session
        self.cached = []

    async def cache_link(self, *args):
        self.cached.append(args)

    async def resolve(self, url):
        return url

    async def link_not_broken(self, url):
        return True

    async def shorten(self, url):
        return None


def test_request_contract():
    session = FakeSession([json.dumps({"success": 1, "data": "https://ekaro.in/enkr1"})])
    aff = Aff(session)
    result = asyncio.run(aff.convert("https://www.flipkart.com/x/p/itm1?pid=1", False))
    check("a Flipkart link converts", bool(result) and result.affiliate.startswith("https://ekaro.in/"),
          repr(result))
    post = session.posts[0]
    check("the API is called once", len(session.posts) == 1, str(len(session.posts)))
    check("convert_option=convert_only is sent",
          post["json"].get("convert_option") == "convert_only", str(post["json"]))
    check("the deal is the CLEAN merchant url (tracking params gone)",
          post["json"]["deal"].startswith("https://www.flipkart.com/x/p/itm1"), str(post["json"]["deal"]))
    check("the token rides as a Bearer header",
          post["headers"]["Authorization"] == f"Bearer {bot.EK_KEY}", str(post["headers"]))
    check("the documented endpoint is used",
          post["url"] == "https://ekaro-api.affiliaters.in/api/converter/public", post["url"])
    cached_row = asyncio.run(bot.store.cached_link("https://www.flipkart.com/x/p/itm1?pid=1"))
    check("the converted link is cached for the next post",
          bool(cached_row) and cached_row["affiliate_url"] == "https://ekaro.in/enkr1",
          str(dict(cached_row) if cached_row else None))


def test_generated_links_keep_their_attribution():
    """USER REPORT (2026-09-25): "earnkaro tho short ga cheyatledu". The
    compaction pass rebuilt a generated Flipkart link keeping `pid` only and
    dropped affExtParam2=<our publisher id>: the post looked tidy and paid
    nobody. The attribution is now part of the compact form - the tidy link
    and the commission survive together."""
    long_fk = ("https://www.flipkart.com/boat-bottle-black/p/itm0613?lid=LST123"
               "&marketplace=FLIPKART&pid=ITM123&otracker=clip"
               "&affExtParam1=acct&affExtParam2=5478322")
    session = FakeSession([json.dumps({"success": 1, "data": long_fk})])
    aff = Aff(session)  # shorten() -> None: the raw form must already be safe
    result = asyncio.run(aff.convert(
        "https://www.flipkart.com/boat-bottle-black/p/itm0613?pid=ITM123", False))
    check("a generated Flipkart link is published (not dropped)",
          bool(result), repr(result))
    check("and it STILL pays US after compaction",
          bool(result) and "affExtParam2=5478322" in result.affiliate
          and "affExtParam1=acct" in result.affiliate,
          result.affiliate if result else "")
    check("the product identity is kept with it",
          bool(result) and "pid=ITM123" in result.affiliate,
          result.affiliate if result else "")
    check("while the session noise is still dropped",
          bool(result) and "lid=" not in result.affiliate
          and "otracker=" not in result.affiliate,
          result.affiliate if result else "")
    check("and the link is materially shorter than it came in",
          bool(result) and len(result.affiliate) < len(long_fk),
          str(len(result.affiliate)) if result else "")


def test_echo_and_foreign_links_are_refused():
    # The API echoing the source's own affiliate link is not our commission.
    echoed = "https://www.flipkart.com/x/p/itm1?affid=someoneelse"
    session = FakeSession([json.dumps({"success": 1, "data": echoed})])
    aff = Aff(session)
    check("an echoed source link is refused",
          asyncio.run(aff.convert(echoed, False)) is None)
    # A bare, non-store domain never reaches the network at all.
    session2 = FakeSession([])
    aff2 = Aff(session2)
    check("a non-shop link is not sent to the API",
          asyncio.run(aff2.convert("https://t.me/somechannel", False)) is None
          and not session2.posts)


# ---------------------------------------------------------------------------
# 3. The API key: account it pays, and the guards built on it
# ---------------------------------------------------------------------------
def test_token_claims():
    # The token literal lives next to the env block at the top: the suite must not
    # read a real .env, so it hands the bot its own token.
    check("the suite supplies its own token (no real .env is read)",
          bot.EK_KEY == TOKEN, "the suite is reading a real .env")
    check("and the bot derives OUR publisher from it", bot.OUR_EK_ID == "5478322",
          bot.OUR_EK_ID)
    claims = bot.earnkaro_token_claims(TOKEN)
    check("the token's publisher is read", claims.get("earnkaro") == "5478322", str(claims))
    check("the token's issue date is read", claims.get("iat") == 1790273710, str(claims))
    check("a non-JWT key decodes to nothing", bot.earnkaro_token_claims("plain-api-key") == {})
    check("garbage does not raise", bot.earnkaro_token_claims("a.b.c") == {})
    # What the bot pins the foreign-publisher guard to.
    old = bot.OUR_EK_ID
    try:
        bot.OUR_EK_ID = claims["earnkaro"]
        check("a Flipkart link carrying a stranger's publisher is refused",
              not bot.AffiliateClient.valid_generated(
                  "https://www.flipkart.com/x/p/itm1?affExtParam2=9999999"))
        check("a Flipkart link carrying OUR publisher is accepted",
              bot.AffiliateClient.valid_generated(
                  "https://www.flipkart.com/x/p/itm1?affExtParam2=5478322"))
    finally:
        bot.OUR_EK_ID = old


# ---------------------------------------------------------------------------
# 4. Amazon: EarnKaro first by default, native tag as the fallback
# ---------------------------------------------------------------------------
def test_amazon_policy():
    old_tag, old_switch = bot.OUR_TAG, bot.AMAZON_VIA_EARNKARO
    try:
        bot.OUR_TAG = "mama086-21"
        bot.AMAZON_VIA_EARNKARO = True
        session = FakeSession([json.dumps({"success": 1, "data": "https://ekaro.in/enkramz1"})])
        aff = Aff(session)
        result = asyncio.run(aff.convert("https://www.amazon.in/dp/B0AMZEK001?psc=1&tag=thief-21", False))
        check("Amazon converts through EarnKaro by default",
              bool(result) and "ekaro.in" in result.affiliate, repr(result))
        check("the Amazon URL is sent with no stranger tag on it",
              "tag=" not in session.posts[0]["json"]["deal"], session.posts[0]["json"]["deal"])

        # The network has nothing for it -> the native tagged link, never a
        # dropped deal and never an untagged one.
        session2 = FakeSession([json.dumps({"success": 0, "message": "Url not found in post!"})])
        aff2 = Aff(session2)
        result2 = asyncio.run(aff2.convert("https://www.amazon.in/dp/B0AMZEK002", False))
        check("a conversion miss falls back to the native tagged link",
              bool(result2) and "amazon.in/dp/B0AMZEK002" in result2.affiliate
              and "tag=mama086-21" in result2.affiliate, repr(result2))
        check("no Bitly quota is spent on a single native Amazon fallback",
              all(p["url"] != "https://api-ssl.bitly.com/v4/shorten" for p in session2.posts))

        # AMAZON_VIA_EARNKARO=false = the 2026-09-06 behaviour, untouched.
        bot.AMAZON_VIA_EARNKARO = False
        session3 = FakeSession([])
        aff3 = Aff(session3)
        result3 = asyncio.run(aff3.convert("https://www.amazon.in/dp/B0AMZEK003?psc=1", False))
        check("AMAZON_VIA_EARNKARO=false posts the native tagged link",
              bool(result3) and "amazon.in/dp/B0AMZEK003" in result3.affiliate
              and "tag=mama086-21" in result3.affiliate, repr(result3))
        check("and spends no EarnKaro call on it", not session3.posts, str(session3.posts))
    finally:
        bot.OUR_TAG, bot.AMAZON_VIA_EARNKARO = old_tag, old_switch


def test_stale_native_amazon_cache_rows_are_reconverted():
    """Turning AMAZON_VIA_EARNKARO on must not be defeated by the link cache.

    A row cached while Amazon was tagged natively lives for LINK_CACHE_DAYS
    (14), so without this the channel would keep posting a link that earns
    nothing for a fortnight.
    """
    old_tag, old_switch = bot.OUR_TAG, bot.AMAZON_VIA_EARNKARO
    try:
        bot.OUR_TAG, bot.AMAZON_VIA_EARNKARO = "mama086-21", True
        source = "https://www.amazon.in/dp/B0CACHE0001?psc=1"
        asyncio.run(bot.store.cache_link(
            source, "https://www.amazon.in/dp/B0CACHE0001?tag=mama086-21",
            "https://www.amazon.in/dp/B0CACHE0001", "ASIN:B0CACHE0001"))
        session = FakeSession([json.dumps({"success": 1, "data": "https://ekaro.in/enkrcache1"})])
        result = asyncio.run(Aff(session).convert(source, False))
        check("a cached NATIVE Amazon row is re-converted, not served",
              bool(result) and "ekaro.in" in result.affiliate, repr(result))
        check("the re-conversion really asked the API", len(session.posts) == 1, str(session.posts))

        # An EarnKaro row is served from cache: no second API call for the same deal.
        session2 = FakeSession([])
        again = asyncio.run(Aff(session2).convert(source, False))
        check("the freshly cached EarnKaro row is reused",
              bool(again) and "ekaro.in" in again.affiliate, repr(again))
        check("no API call was made for the cached row", not session2.posts, str(session2.posts))
    finally:
        bot.OUR_TAG, bot.AMAZON_VIA_EARNKARO = old_tag, old_switch


def test_review_channel_expansion_keeps_our_tag():
    """The reviewed channel must show the DIRECT tagged Amazon link."""
    old_tag = bot.OUR_TAG
    try:
        bot.OUR_TAG = "mama086-21"
        aff = Aff(FakeSession([]))
        aff._short_to_long = {"https://ekaro.in/enkramz9": "https://www.amazon.in/dp/B0REVIEW01"}
        expanded = asyncio.run(aff.expand_our_short_links("Deal\nhttps://ekaro.in/enkramz9"))
        check("our earned link expands back to the native product page",
              "amazon.in/dp/B0REVIEW01" in expanded and "ekaro.in" not in expanded, expanded)
        check("and the native page carries our tag",
              "tag=mama086-21" in expanded, expanded)
    finally:
        bot.OUR_TAG = old_tag


# ---------------------------------------------------------------------------
# 5. The three new first-preference sources
# ---------------------------------------------------------------------------
def test_new_first_preference_sources():
    links = list(bot.FIRST_PREFERENCE_SOURCES)
    check("three new sources are configured", len(links) == 3, str(links))
    check("they are the channels the user named",
          set(links) == {"https://t.me/+O3j4ghbtJzhjZjJl",
                         "https://t.me/+8KzU3P58MJ9jN2M1",
                         "https://t.me/+6LA1ljXGlbNmMjA1"}, str(links))
    for link in links:
        targets = bot.SOURCE_TO_TARGETS.get(link)
        check(f"{link} routes to every non-Tricks main target",
              targets == bot.NO_TRICKS_TARGETS, str(targets))
        check(f"{link} never reaches the Tricks channel",
              bot.TRICKS_TARGET not in (targets or []), str(targets))
        check(f"{link} is a first-preference source", bot.is_priority_source(link), link)
        check(f"{link} is claimed before ordinary sources",
              bot.normalize_source_name(link) in bot.PREFERRED_SOURCES, link)
        # Invite hashes carry uppercase characters; the queue stores whatever it
        # received, and the SQL side compares lower(source).
        check(f"{link} is recognised whatever case it arrives in",
              bot.is_priority_source(link.upper()) and bot.is_priority_source("@" + link), link)
    check("the trick sources stay on the Tricks channel",
          bot.SOURCE_TO_TARGETS["TrickXpert"] == [bot.TRICKS_TARGET],
          str(bot.SOURCE_TO_TARGETS["TrickXpert"]))
    check("no new source is a channel we own",
          not any(bot.is_own_channel_source(link) for link in links), "owned-source guard")
    check("the earlier first preference is not lost",
          "dealsunder99_com" in bot.PREFERRED_SOURCES and bot.is_priority_source("pricehistory")
          and bot.is_priority_source("under_99_loot_deals"), str(sorted(bot.PREFERRED_SOURCES)))


def test_first_preference_wins_the_claim():
    """A queue holding both kinds: the new source is claimed first."""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            local = bot.Store(Path(td) / "claim.sqlite3")
            original = bot.store
            bot.store = local
            try:
                for source, text, age in (
                        ("SomeOrdinarySource", "Boat earphones \u20b9499\nhttps://www.flipkart.com/x/p/itm2", 30),
                        (bot.FIRST_PREFERENCE_SOURCES[0], "Boat earphones \u20b9499\nhttps://www.flipkart.com/x/p/itm1", 5)):
                    await local.enqueue(-1001, hash(source) % 10_000, source, text)
                # Older job first: only the preference tier can reorder them.
                local.conn.execute("UPDATE queue SET created_at = created_at - ? WHERE source = ?",
                                   (age, bot.FIRST_PREFERENCE_SOURCES[0]))
                local.conn.execute("UPDATE queue SET created_at = created_at + ? WHERE source = ?",
                                   (age, "SomeOrdinarySource"))
                local.conn.commit()
                row = await local.claim_job()
                return row["source"] if row else None
            finally:
                bot.store = original

    claimed = asyncio.run(run())
    check("the first-preference source is claimed ahead of an older ordinary post",
          claimed == bot.FIRST_PREFERENCE_SOURCES[0], repr(claimed))


def test_first_preference_backfill_is_bounded():
    """A first connect must not fire the whole backfill at once.

    These sources are claimed ahead of everything else, so recovering 50 posts
    each on the first connect would burst ~150 posts into the channels before a
    single ordinary deal could go out. The tail is recovered instead
    (FIRST_PREFERENCE_BACKFILL_LIMIT, 12 by default) and live events carry the
    rest.
    """
    import datetime

    class Msg:
        def __init__(self, idx):
            self.id = 1000 + idx
            self.date = datetime.datetime.now(datetime.timezone.utc)
            self.message = f"Deal {idx} \u20b9{99 + idx}\nhttps://www.flipkart.com/x/p/itm{idx}"
            self.text = self.message
            self.entities = []
            self.reply_markup = None
            self.media = None
            self.reply_to = None

    class Client:
        def __init__(self, messages):
            self.messages = messages
            self.asked_limit = None

        async def get_messages(self, entity, limit=None):
            self.asked_limit = limit
            return self.messages[:limit]

    class Entity:
        id = 1234567

    async def run():
        with tempfile.TemporaryDirectory() as td:
            local = bot.Store(Path(td) / "backfill.sqlite3")
            original = bot.store
            bot.store = local
            try:
                client = Client([Msg(i) for i in range(40)])
                await bot.backfill_source(client, Entity(), bot.FIRST_PREFERENCE_SOURCES[2])
                queued = local.conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
                return client.asked_limit, queued
            finally:
                bot.store = original

    limit, queued = asyncio.run(run())
    check("a first-preference source asks for a short tail, not the whole backfill",
          limit == int(os.getenv("FIRST_PREFERENCE_BACKFILL_LIMIT", "12")), str(limit))
    check("and only that many posts are queued", queued == limit, f"queued={queued} limit={limit}")
    check("an ordinary source still recovers its normal backfill",
          "BACKFILL_LIMIT" in (Path(bot.__file__).read_text(encoding="utf-8")
                               .split("async def backfill_source")[1][:900]), "wiring changed")


def test_priority_boost_is_applied():
    """enqueue() gives a first-preference source the +1 tier boost."""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            local = bot.Store(Path(td) / "boost.sqlite3")
            original = bot.store
            bot.store = local
            try:
                text = "Boat earphones \u20b9499\nhttps://www.flipkart.com/x/p/itm3"
                await local.enqueue(-1002, 7, "SomeOrdinarySource", text)
                await local.enqueue(-1002, 8, bot.FIRST_PREFERENCE_SOURCES[1], text + "\n")
                rows = {r["source"]: r["priority"] for r in
                        local.conn.execute("SELECT source, priority FROM queue")}
                return rows
            finally:
                bot.store = original

    rows = asyncio.run(run())
    boosted = rows.get(bot.FIRST_PREFERENCE_SOURCES[1])
    ordinary = rows.get("SomeOrdinarySource")
    check("both jobs were queued", boosted is not None and ordinary is not None, str(rows))
    check("the first-preference job carries a higher priority tier",
          boosted > ordinary, str(rows))


def test_checker_proves_whose_link():
    """`ops/earnkaro_check.py` must be able to say WHOSE account a link pays.

    "perefctgaa na links gaa" is not answered by "HTTP 200": the converted link
    has to be OUR link. The checker expands short links and reads the visible
    attribution, so it can tell ours from somebody else's. Pinned here so the
    tool cannot silently go back to only counting HTTP 200s.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "earnkaro_check", Path(__file__).parent / "ops" / "earnkaro_check.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    publisher = bot.OUR_EK_ID
    check("the bot's own publisher comes from the token", bool(publisher), publisher)

    ours = ("https://www.flipkart.com/boat-airdopes/p/itm123?pid=ACCFHBDD6HYQZ6AZ"
            f"&affExtParam1=1234&affExtParam2={publisher}")
    theirs = ours.replace(f"affExtParam2={publisher}", "affExtParam2=999999")
    # OUR_TAG may be empty in this test's env; the tag we own is the constant.
    our_tag = sorted(bot.OUR_AMAZON_TAGS)[0]
    amazon_ours = f"https://www.amazon.in/dp/B0FPDD9WKP?tag={our_tag}"
    amazon_theirs = "https://www.amazon.in/dp/B0FPDD9WKP?tag=deals0911-21"
    hypd_destination = ("https://www.meesho.com/cotton-saree/p/abc12345"
                        "?affid=infhypd&affExtParam1=6ab13e1eb6ce5677d060574c"
                        "&affExtParam2=daoli7dtm6mc5h7k1ffg")

    verdict, where = checker.whose_link(ours, publisher, bot.OUR_TAG, 5)
    check("a converted Flipkart link with our publisher proves OUR link",
          verdict == "ours", f"{verdict}: {where}")
    verdict, _ = checker.whose_link(theirs, publisher, bot.OUR_TAG, 5)
    check("and a link carrying a foreign publisher is refused",
          verdict == "foreign", verdict)
    verdict, _ = checker.whose_link(amazon_ours, publisher, our_tag, 5)
    check("an Amazon link with our tag proves OUR link", verdict == "ours", verdict)
    verdict, _ = checker.whose_link(amazon_theirs, publisher, our_tag, 5)
    check("a source's Amazon tag is not ours", verdict == "foreign", verdict)
    verdict, where = checker.whose_link(hypd_destination, publisher, our_tag, 5)
    # A page carrying HYPD's own attribution is NOT an EarnKaro link; the check
    # must never call it ours (and must name the token it saw).
    check("a HYPD-attributed page is never reported as OUR EarnKaro link",
          verdict != "ours" and "daoli7dtm6mc5h7k1ffg" in where, f"{verdict}: {where}")
    hypd_share = "https://hypd.store/93944/afflink/daoli7dtm6mc5h7k1ffg"
    verdict, where = checker.whose_link(hypd_share, publisher, our_tag, 5)
    check("and OUR hypd share link is reported as not-an-EarnKaro-link",
          verdict != "ours", f"{verdict}: {where}")

    # The user-facing result line must name the account, not just "200 OK".
    source = (Path(__file__).parent / "ops" / "earnkaro_check.py").read_text(encoding="utf-8")
    check("the checker's verdict names the paying account",
          "PAYS US ({publisher})" in source, "")
    check("and it expands short links before judging them",
          "def expand(" in source and "is_short_link" in source, "")


def test_checker_proves_our_hypd_links_too():
    """One command must answer BOTH questions: EarnKaro key AND our hypd links."""
    source = (Path(__file__).parent / "ops" / "earnkaro_check.py").read_text(encoding="utf-8")
    for token in ("daoli7dtm6mc5h7k1ffg", "daol5bac45l0tc0oo5rg", "daol52dtm6mc5h7k1ejg"):
        check(f"the checker probes our hypd link {token}", token in source, "")
    check("it shorts our hypd link the way the bot does (Bitly v4)",
          "api-ssl.bitly.com/v4/shorten" in source, "")
    check("and proves the short link comes back to OUR store",
          "lands_on_ours" in source and "OURS" in source, "")
    check("it can be run alone (--hypd-only) and skipped (--skip-hypd)",
          "--hypd-only" in source and "--skip-hypd" in source, "")
    check("a network-less box is not reported as a broken link",
          'status.startswith("unreachable")' in source, "")


def test_status_report_answers_are_we_converting():
    """"anni perfectga convert chesthunnava ledaa?" must be answerable from data.

    ops/conversion_report.py classifies every link the bot produced by route and
    prints a verdict per route plus what needs attention. Pinned here so the
    answer cannot drift back to guesswork.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "conversion_report", Path(__file__).parent / "ops" / "conversion_report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)

    cases = (
        ("https://ekaro.in/abc123",
         "https://www.flipkart.com/x/p/itm1?affExtParam2=5478322", "earnkaro"),
        ("https://bit.ly/amz",
         "https://www.amazon.in/dp/B0FPDD9WKP?tag=mama086-21", "amazon"),
        ("https://bit.ly/hypd",
         "https://hypd.store/93944/afflink/daoli7dtm6mc5h7k1ffg", "hypd"),
        ("https://www.myntra.com/tshirt/buy", "https://www.myntra.com/tshirt/buy", "passthrough"),
    )
    for affiliate, resolved, want in cases:
        got = report.route_of(affiliate, resolved)
        check(f"the report classifies {want} correctly", got == want, f"{got} != {want}")

    cfg = {"earnkaro_token": True, "earnkaro_publisher": "5478322", "earnkaro_issued": "x",
           "earnkaro_endpoint": "e", "convert_option": "convert_only",
           "amazon_via_earnkaro": "true", "amazon_tag": "mama086-21",
           "hypd_store": "93944", "hypd_slug": "smartdeals", "hypd_always_bitly": "true",
           "hypd_merchants": "meesho.com", "bitly_tokens": 1}
    healthy = {"routes": {"earnkaro": {"count": 5, "examples": []},
                          "hypd": {"count": 2, "examples": []}, "passthrough": {"count": 0}},
               "hypd_learned": [{"afflink": "x"}], "hypd_wanted": [], "posted_deals": 7}
    lines, problems = report.verdict(cfg, healthy, {"markers": {}})
    check("a healthy window reports nothing to fix", not problems, str(problems))
    check("and it says EarnKaro is working and which account is paid",
          any("WORKING" in l and "5478322" in l for l in lines), str(lines))
    check("it says our hypd links are working",
          any(l.startswith("HYPD") and "WORKING" in l for l in lines), str(lines))

    broken = dict(healthy)
    broken["hypd_wanted"] = [{"product": "https://www.meesho.com/kurtis/p/none123"}]
    broken["routes"] = {"passthrough": {"count": 3, "examples": []}}
    lines, problems = report.verdict(cfg, broken,
                                     {"markers": {"EK AUTH": {"count": 2, "last": "now"}}})
    joined = " ".join(problems)
    check("a refused EarnKaro token is reported as BROKEN", "AUTH" in joined, joined)
    check("products waiting for a hypd link are reported", "hypd link" in joined.lower(), joined)
    check("unmonetized deals are reported", "clean merchant link" in joined, joined)

    auth_lines, _ = report.verdict(cfg, healthy, {"markers": {"EK AUTH": {"count": 1}}})
    check("and the verdict names the fix when the key is refused",
          any("BROKEN" in l for l in auth_lines), str(auth_lines))


def main():
    test_response_shapes()
    test_request_contract()
    test_generated_links_keep_their_attribution()
    test_echo_and_foreign_links_are_refused()
    test_token_claims()
    test_amazon_policy()
    test_stale_native_amazon_cache_rows_are_reconverted()
    test_review_channel_expansion_keeps_our_tag()
    test_new_first_preference_sources()
    test_first_preference_wins_the_claim()
    test_first_preference_backfill_is_bounded()
    test_priority_boost_is_applied()
    test_checker_proves_whose_link()
    test_checker_proves_our_hypd_links_too()
    test_status_report_answers_are_we_converting()
    print(f"\nEARNKARO CONVERSION + SOURCE TESTS PASS ({PASS} checks)")


main()
