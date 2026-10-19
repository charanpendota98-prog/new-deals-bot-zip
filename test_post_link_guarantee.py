"""The post-link guarantee: what a channel RECEIVES is always OUR link.

USER RULE (2026-10-19): "perefctga change chesthunada mana link gaa ... full
check and then post" - the question is not what the code intends, it is what a
subscriber actually gets. So this suite does not test the pieces; it runs the
REAL worker (`Store.claim_job` -> `process_job` -> per-target send) in
strict-all mode, records every message a channel would receive, and then audits
those bytes independently:

    for every URL in every sent message:
        it must be our family link, OR carry our Amazon tag, OR carry the
        converter's conversion id AND our publisher, OR be a short link with
        stored proof that WE minted it for this account.

Anything else - a foreign tag, a bare merchant page, a source's own short link,
and above all "our number simply appended to somebody else's long link" (the
user's own warning) - must never appear, no matter which path produced it.

Three scenarios, because they are the three ways a wrong link used to leak:

  G1  the normal deal: converted -> posted, and the audit must PASS.
  G2  the converter hands back a STRANGER'S link: nothing of theirs is published.
  G3  the converter hands back a page with only OUR NUMBER on it (no conversion
      id): nothing of it is published - our number alone is not a commission.

Plus: the audit is proven able to FAIL on each bad shape. A guard that cannot
fail is not a guard.

Run: python3 test_post_link_guarantee.py   (from the repo root, offline)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
# The WORKING SHAPE of the converter token (a JWT whose payload names the
# publisher that gets paid) with a deliberately fake signature: the real token is
# a secret and never belongs in the repository. The suite supplies it so the
# publisher id this file pins (5478322) is the one the bot derives.
os.environ.setdefault(
    "EARNKARO_API_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJfaWQiOiI2YTZmOTA0OTZkZmY5NjY1Njk2ZmM2MjQiLCJlYXJua2FybyI6IjU0NzgzMjIiLCJpYXQiOjE3OTAyNzM3MTB9."
    "not-a-real-signature-only-the-claims-matter")
os.environ.setdefault("AMAZON_TAG", "mama086-21")
_KEEP = Path(tempfile.mkdtemp(prefix="post-guarantee-"))
os.environ.setdefault("BOT_DB_PATH", str(_KEEP / "state.sqlite3"))
os.environ.setdefault("BOT_LOG_DIR", str(_KEEP / "logs"))
os.environ.setdefault("BOT_MEDIA_DIR", str(_KEEP / "media"))

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
import main_bot_new as bot  # noqa: E402

R = "\u20b9"
OUR_TAG = "mama086-21"
SOURCE = "dealsvelocity"          # fans out to every non-Tricks channel
FAILS: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  <- {extra[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# the harness (real worker, fake wire - the same shape test_duplicate_sim uses)
# ---------------------------------------------------------------------------
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
    """Records exactly what a subscriber would see."""

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


class Converter:
    """A converter double: it returns whatever `output` says, and caches it."""

    def __init__(self, output: str, resolved: str, key: str = "ASIN:B0GUARANT1"):
        self.output, self.resolved, self.key = output, resolved, key

    async def resolve(self, url):
        return self.resolved

    async def convert(self, source, multi_link, resolved=None):
        await self.cache_link(source, self.output, self.resolved, self.key)
        return bot.LinkResult(source, self.resolved, self.output, self.key)

    async def cache_link(self, source_url, affiliate, resolved, key):
        await bot.store.cache_link(source_url, affiliate, resolved, key)

    async def shorten_long_urls_in_text(self, text):
        return text

    async def link_not_broken(self, url):
        return True

    async def rendered_links_not_broken(self, text):
        return True


# ---------------------------------------------------------------------------
# THE AUDIT: run on the bytes a channel received, not on the code's intentions
# ---------------------------------------------------------------------------
def audit_published_text(text: str) -> list[str]:
    """Every URL that is NOT provably ours (empty list = the post is perfect)."""
    family = {bot.clean_url(bot.OUR_FOLDER_LINK),
              *(bot.clean_url(x) for x in bot.OUR_MAIN_CHANNEL_LINKS)}
    bad: list[str] = []
    for raw in bot.URL_RE.findall(text or ""):
        url = bot.clean_url(raw)
        if url in family:
            continue                      # our own family promo, not a deal link
        if bot.Store.has_native_owned_attribution(url):
            continue                      # our tag, or conversion id + our publisher
        if bot.conversion_signature_missing(url):
            bad.append(url)               # our NUMBER on their page
            continue
        if bot.is_our_hypd_link(url):
            continue
        proof = bot.store.conn.execute(
            "SELECT 1 FROM owned_link_proofs WHERE affiliate_url=? LIMIT 1", (url,)).fetchone()
        if proof:
            continue                      # a hop WE minted and recorded
        bad.append(url)
    return bad


async def run_job(converter, text: str):
    """Enqueue -> claim -> the REAL process_job -> every channel's bytes."""
    store = bot.Store(Path(tempfile.mktemp(suffix="-guarantee.sqlite3")))
    old_store = bot.store
    bot.store = store
    try:
        await store.enqueue(-1009100, 2101, SOURCE, text, False)
        store.conn.commit()
        names = {SOURCE, *bot.SOURCE_TO_TARGETS.get(SOURCE, [])}
        names.update(bot.MAIN_TARGETS + [bot.UNDER99_TARGET, bot.UNDER499_TARGET,
                                        bot.PREMIUM_TARGET])
        tmap = {name: Ent(name) for name in names}
        client = FakeClient({(-1009100, 2101): FakeMsg(text, 2101)})
        row = await store.claim_job()
        await bot.process_job(client, converter, tmap, row)
        status, last_error = store.conn.execute(
            "SELECT status, last_error FROM queue").fetchone()
        return client, status, last_error or "", store
    finally:
        bot.store = old_store


DEAL = ("Noise ColorFit Pulse 2 Smart Watch\n"
        f"\u2705Deal Price: {R}1,299\n\u274cMRP: {R}3,999 (67% off)\n"
        "https://amzn.to/sourceShort9")


async def test_guarantee():
    print("== the post-link guarantee: what a channel receives is OUR link ==")
    bot.STRICT_OUR_LINKS = True
    old_tag = bot.OUR_TAG
    bot.OUR_TAG = OUR_TAG
    try:
        # The shape the converter really mints for our account (the user's own
        # live link): affid + affExtParam1 + affExtParam2=<our publisher>.
        ours = ("https://www.shopsy.in/noise-colorfit-pulse-2/p/itmguarantee1?pid=PIDG1"
                "&marketplace=FLIPKART&affid=deals101&cmpid=AFF_deals101"
                "&affExtParam1=ENKR20261019GUARANTEE&affExtParam2=5478322")
        resolved = "https://www.shopsy.in/noise-colorfit-pulse-2/p/itmguarantee1?pid=PIDG1"

        # ---- G1: the ordinary deal -----------------------------------------
        client, status, error, _ = await run_job(Converter(ours, resolved), DEAL)
        bodies = "\n".join(text for _chat, text in client.sent)
        check("[G1] the deal reached the channels", bool(client.sent),
              str(client.sent[:1]))
        check("[G1] the product and price are on the post",
              "Noise ColorFit Pulse 2" in bodies and R + "1,299" in bodies, bodies[:240])
        check("[G1] EVERY published link is provably ours (independent audit)",
              audit_published_text(bodies) == [], str(audit_published_text(bodies)))
        check("[G1] the source's own short link never went out",
              "amzn.to/sourceShort9" not in bodies, bodies[:240])
        check("[G1] the link on the post is the CONVERTED one, not a bare page",
              "affExtParam1=ENKR20261019GUARANTEE" in bodies
              and "affExtParam2=5478322" in bodies, bodies[:240])
        check("[G1] the job finished clean", status == "done", f"{status} {error}")

        # ---- G2: the converter returns a STRANGER'S link --------------------
        foreign = "https://www.amazon.in/dp/B0GUARANT2?tag=rivalpub-21"
        client2, status2, error2, _ = await run_job(
            Converter(foreign, "https://www.amazon.in/dp/B0GUARANT2", "ASIN:B0GUARANT2"),
            DEAL)
        bodies2 = "\n".join(text for _chat, text in client2.sent)
        check("[G2] a stranger's link is never published", "rivalpub-21" not in bodies2,
              bodies2[:240])
        check("[G2] and the audit agrees the channels got nothing of theirs",
              audit_published_text(bodies2) == [], str(audit_published_text(bodies2)))

        # ---- G3: only OUR NUMBER on the page (no conversion id) -------------
        stamped = ("https://www.flipkart.com/noise-pulse-2/p/itmguarantee1?pid=PIDG1"
                   "&affExtParam2=5478322")
        client3, status3, error3, _ = await run_job(
            Converter(stamped, "https://www.flipkart.com/noise-pulse-2/p/itmguarantee1?pid=PIDG1",
                      "PID:PIDG1"),
            DEAL)
        bodies3 = "\n".join(text for _chat, text in client3.sent)
        check("[G3] our NUMBER alone never reaches a channel",
              "affExtParam2=5478322" not in bodies3, bodies3[:240])
        check("[G3] and nothing unowned was published in its place",
              audit_published_text(bodies3) == [], str(audit_published_text(bodies3)))

        # ---- the audit itself must be able to FAIL --------------------------
        for bad in (
            "https://www.amazon.in/dp/B0X?tag=rivalpub-21",                    # foreign tag
            "https://www.flipkart.com/x/p/itm1?pid=1",                         # bare page
            "https://bitli.in/someoneelse",                                    # stranger hop
            "https://www.flipkart.com/x/p/itm1?pid=1&affExtParam2=5478322",    # our number only
        ):
            check(f"the audit rejects {bad[:52]}", audit_published_text(bad) == [bad],
                  str(audit_published_text(bad)))
        good = ("https://www.flipkart.com/x/p/itm1?pid=1&affid=deals101"
                "&affExtParam1=ENKR20261019TEST&affExtParam2=5478322")
        check("the audit accepts a REAL conversion", audit_published_text(good) == [],
              str(audit_published_text(good)))
        check("the audit accepts our own family link",
              audit_published_text(bot.OUR_MAIN_CHANNEL_LINKS[0]) == [],
              str(audit_published_text(bot.OUR_MAIN_CHANNEL_LINKS[0])))
    finally:
        bot.OUR_TAG = old_tag
        bot.STRICT_OUR_LINKS = False


if __name__ == "__main__":
    asyncio.run(test_guarantee())
    print("\n" + ("POST LINK GUARANTEE: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_post_link_guarantee: all checks PASS"))
    sys.exit(1 if FAILS else 0)
