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
os.environ.update(
    TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x",
    EARNKARO_API_KEY="k", AMAZON_TAG="",
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
TOKEN = (
    # The WORKING SHAPE of the converter token (a JWT whose payload names the
    # publisher that gets paid) with a deliberately fake signature: the real
    # token is a secret and never belongs in the repository. The claims below
    # are the ones the live token carries, so the parsing test is real.
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJfaWQiOiI2YTZmOTA0OTZkZmY5NjY1Njk2ZmM2MjQiLCJlYXJua2FybyI6IjU0NzgzMjIiLCJpYXQiOjE3OTAyNzM3MTB9."
    "not-a-real-signature-only-the-claims-matter"
)


def test_token_claims():
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


def main():
    test_response_shapes()
    test_request_contract()
    test_echo_and_foreign_links_are_refused()
    test_token_claims()
    test_amazon_policy()
    test_review_channel_expansion_keeps_our_tag()
    test_new_first_preference_sources()
    test_first_preference_wins_the_claim()
    test_first_preference_backfill_is_bounded()
    test_priority_boost_is_applied()
    print(f"\nEARNKARO CONVERSION + SOURCE TESTS PASS ({PASS} checks)")


main()
