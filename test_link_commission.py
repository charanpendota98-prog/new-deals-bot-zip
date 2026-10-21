#!/usr/bin/env python3
"""EVERY published link must be OURS - 100+ checks, run 100 times over.

The user's report, twice (2026-10-21 and again the next day):

    "ilaga invalid url link vasthunnai mana channels lo"
    -> https://is.gd/c3Gl9d  -> amazon.in/dp/B0HNPB6QBS   (no ?tag=)
    -> https://is.gd/bO79HA  -> amazon.in/dp/B097GNDJ37   (no ?tag=)

Both links OPENED, so nothing looked broken. Both paid nobody. And the standing
rule is not "never post a bad link", it is:

    "commission asalu miss avvakunda" - convert it properly, check 100+ times,
    then post OUR link.

So this suite tests the rule in that order:

  1. TAG MATRIX   - every Amazon shape (host x path x tag state) comes out with
                    exactly ONE tag, and it is ours. 125 shapes.
  2. HOP MATRIX   - every tokenless shortener host is repaired (stored
                    destination) or unwrapped live (no stored destination), and
                    published as OUR tagged destination. 30 checks.
  3. MERCHANT     - a bare merchant page is converted, not published free.
  4. NEVER FREE   - family links, service offers and share intents are left
                    alone (they are not affiliate destinations and never earn).
  5. THE REPORT   - the two reported URLs, end to end through the bot's own
                    final tidy pass, plus the pre-send gate accepting the
                    repaired text and refusing the original.
  6. 100 ROUNDS   - the same corpus re-checked 100 times: identical verdicts,
                    no drift, no link left paying nobody.

No Telegram, no internet: the link book and every network call are fakes.
"""
import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# The tag era, exactly as production runs it.
os.environ["AMAZON_TAG"] = "mama086-21"
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
os.environ.setdefault("EARNKARO_PUBLISHER_ID", "5478322")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))

import main_bot_new as bot  # noqa: E402

TAG = "mama086-21"
PUB = "5478322"
ASIN = "B0HNPB6QBS"

FAILS: list[str] = []
COUNT = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global COUNT
    COUNT += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


# --------------------------------------------------------------------------- #
# the fakes: a link book and a network, both answer-only
# --------------------------------------------------------------------------- #
class Book:
    """`link_cache` + `owned_link_proofs`, without a database."""

    def __init__(self, destinations: dict[str, str] | None = None,
                 owned: set[str] | None = None):
        self.destinations = dict(destinations or {})
        self.owned = set(owned or ())
        self.written: list[tuple[str, str, str, str]] = []

    async def cached_link_is_owned(self, url: str, _resolved: str = "") -> bool:
        """The proof row `convert()` writes: OUR publisher id, bound to this URL."""
        return bot.clean_url(url) in self.owned

    async def affiliate_destination(self, url: str) -> str | None:
        return self.destinations.get(bot.clean_url(url))

    async def cache_link(self, source: str, affiliate: str, resolved: str, key: str) -> None:
        self.written.append((source, affiliate, resolved, key))


@dataclass
class Converted:
    source: str
    resolved: str
    affiliate: str
    deal_key: str


def make_client(book: Book | None = None, *, resolves: dict[str, str] | None = None,
                converts: dict[str, str] | None = None):
    """An AffiliateClient whose only I/O is the dictionaries passed in."""
    class NoSession:
        pass

    aff = bot.AffiliateClient(NoSession())  # type: ignore[arg-type]
    aff.repair_book = book
    table = dict(resolves or {})

    async def fake_resolve(url: str) -> str:
        return table.get(bot.clean_url(url), "")

    async def fake_convert(source: str, _multi, resolved: str | None = None):
        target = (converts or {}).get(bot.clean_url(resolved or source), "")
        if not target:
            return None
        return Converted(source, resolved or source, target, bot.product_key(target))

    aff.resolve = fake_resolve          # type: ignore[assignment]
    aff.convert = fake_convert          # type: ignore[assignment]
    return aff


def tag_of(url: str) -> list[str]:
    return parse_qs(urlparse(url).query).get("tag", [])


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def repair(aff, text: str, *args, **kwargs):
    return asyncio.run(aff.repair_unowned_links(text, *args, **kwargs))


# --------------------------------------------------------------------------- #
# 1. TAG MATRIX: 125 Amazon shapes, one tag, and it is ours
# --------------------------------------------------------------------------- #
def test_amazon_tag_matrix():
    print("\n== 1. every Amazon shape comes out with exactly ONE tag: ours ==")
    hosts = ["www.amazon.in", "amazon.in", "m.amazon.in", "www.amazon.com", "amazon.com"]
    paths = ["/dp/{a}", "/gp/product/{a}", "/gp/aw/d/{a}", "/exec/obidos/ASIN/{a}",
             "/dp/{a}/ref=sr_1_1"]
    tails = ["", "?tag={ours}", "?tag=theirs-21", "?tag={ours}&tag=theirs-21",
             "?psc=1&smid=A1X&tag={ours}"]
    aff = make_client()
    for host in hosts:
        for path in paths:
            for tail in tails:
                url = f"https://{host}" + path.format(a=ASIN) + tail.format(ours=TAG)
                form = aff.provable_form(url)
                ok = bool(form) and tag_of(form) == [TAG]
                check(f"{host}{path.format(a='')} {tail or '(no tag)'} -> one tag, ours",
                      ok, f"{url} -> {form}")
    # a handful of shapes that must NOT be rewritten into ours
    for url in ["https://amzn.to/3xYzAbc", "https://amzn.in/d/abc123",
                "https://www.amazon.in/s?k=blanket&tag=" + TAG]:
        form = aff.provable_form(url)
        check(f"a wrapper/search page is not turned into a product tag - {host_of(url)}",
              form == url or form is None, f"{url} -> {form}")


# --------------------------------------------------------------------------- #
# 2. HOP MATRIX: every tokenless host is repaired, never published
# --------------------------------------------------------------------------- #
TOKENLESS = ["is.gd", "v.gd", "tinyurl.com", "tiny.cc", "rb.gy", "cutt.ly", "t.ly",
             "shorturl.at", "spoo.me", "da.gd", "surl.li", "shrtco.de", "cli.re",
             "v.ht", "snip.ly"]
REPORTED = {"https://is.gd/c3Gl9d": "https://www.amazon.in/dp/B0HNPB6QBS",
            "https://is.gd/bO79HA": "https://www.amazon.in/dp/B097GNDJ37"}


def test_hop_matrix():
    print("\n== 2. every tokenless hop is repaired from our own book ==")
    for host in TOKENLESS:
        hop = f"https://{host}/abc123"
        book = Book({hop: "https://www.amazon.in/dp/" + ASIN})
        aff = make_client(book)
        out, notes = repair(aff, f"Deal \u20b9100 {hop}", store_obj=book)
        check(f"{host}: the stored destination is what gets posted, with our tag",
              out == f"Deal \u20b9100 https://www.amazon.in/dp/{ASIN}?tag={TAG}", out)
        check(f"{host}: the hop is gone from the text", hop not in out, out)
        check(f"{host}: the repair is recorded as 'repaired', not 'kept'",
              notes and notes[0]["status"] == "repaired", str(notes[:1]))
        check(f"{host}: the repaired link is bound in the book (no repeat lookup)",
              book.written and book.written[0][1].endswith(f"tag={TAG}"), str(book.written[:1]))

    print("\n== 2b. a hop with no stored destination is unwrapped live ==")
    for host in TOKENLESS[:8]:
        hop = f"https://{host}/nobook"
        aff = make_client(resolves={hop: "https://www.amazon.in/dp/" + ASIN})
        out, notes = repair(aff, f"Deal {hop}", store_obj=Book())
        check(f"{host}: live unwrap -> our tagged destination",
              out == f"Deal https://www.amazon.in/dp/{ASIN}?tag={TAG}", out)
        check(f"{host}: the note names the destination it found",
              notes and notes[0]["destination"].endswith(ASIN), str(notes[:1]))

    print("\n== 2c. a hop that cannot be unwrapped is never published as ours ==")
    for host in TOKENLESS[:8]:
        hop = f"https://{host}/dead"
        aff = make_client(resolves={})
        out, notes = repair(aff, f"Deal {hop}", store_obj=Book())
        check(f"{host}: left as-is (the send gate removes it), flagged unrepairable",
              hop in out and notes and notes[0]["status"] == "unrepairable", str(notes[:1]))
        check(f"{host}: the reason says the hop proves nothing",
              "prove" in (notes[0]["why"] if notes else ""), str(notes[:1]))


# --------------------------------------------------------------------------- #
# 3. MERCHANT PAGES: convert or it pays nobody
# --------------------------------------------------------------------------- #
MERCHANTS = {
    "https://www.flipkart.com/tee/p/itm1?pid=P1": "https://ekaro.in/enkrFLIP1?affExtParam2=" + PUB,
    "https://dl.flipkart.com/s/abc?lid=x": "https://ekaro.in/enkrFLIP2?affExtParam2=" + PUB,
    "https://www.myntra.com/tshirt/1/buy": "https://ekaro.in/enkrMYN1?affExtParam2=" + PUB,
    "https://www.shopsy.in/pen/p/1": "https://ekaro.in/enkrSHOP1?affExtParam2=" + PUB,
    "https://www.meesho.com/saree/p/xyz": "https://ekaro.in/enkrMEE1?affExtParam2=" + PUB,
}


def test_merchants():
    print("\n== 3. a bare merchant page is CONVERTED, never published free ==")
    for page, converted in MERCHANTS.items():
        aff = make_client(converts={page: converted})
        book = Book(owned={converted})          # what convert() proves by minting it
        out, notes = repair(aff, f"Deal \u20b9399 {page}", store_obj=book)
        check(f"{host_of(page)}: converted to our publisher id, page kept out",
              converted in out and notes and notes[0]["status"] == "repaired", out)
        check(f"{host_of(page)}: never left bare", page not in out, out)

    page = "https://www.flipkart.com/tee/p/itm1?pid=P1"
    aff = make_client(converts={})            # converter down / unsupported
    out, notes = repair(aff, f"Deal {page}", store_obj=Book())
    check("converter unavailable: the page is NOT blessed as ours",
          notes and notes[0]["status"] == "unrepairable", str(notes[:1]))

    print("\n== 3b. a page that already carries our attribution is left alone ==")
    ours = [
        f"https://www.amazon.in/dp/{ASIN}?tag={TAG}",
        f"https://www.flipkart.com/tee/p/itm1?pid=P1&affExtParam2={PUB}&affExtParam1=conv1",
    ]
    for url in ours:
        aff = make_client()
        out, notes = repair(aff, f"Deal {url}", store_obj=Book())
        check(f"{host_of(url)}: already ours -> untouched, no notes",
              out.endswith(url) and not notes, out)


# --------------------------------------------------------------------------- #
# 4. NEVER FREE: non-affiliate links stay out of the repair path
# --------------------------------------------------------------------------- #
def test_non_affiliate_untouched():
    print("\n== 4. family links, service offers and share intents are not affiliate ==")
    untouched = [
        bot.OUR_FOLDER_LINK,
        *bot.OUR_MAIN_CHANNEL_LINKS,
        "https://www.zomato.com/offer/50-off",
        "https://www.swiggy.com/offers",
        "https://www.phonepe.com/offers",
        "https://in.bookmyshow.com/movies",
        "https://www.amazon.in/dp/" + ASIN + "?tag=" + TAG + "&utm_share=1",
    ]
    aff = make_client()
    for url in untouched:
        if bot.is_share_intent(url):
            continue
        out, notes = repair(aff, f"post {url}", store_obj=Book())
        check(f"{host_of(url)}: left exactly as written",
              out == f"post {url}" and not notes, f"{url} -> {out} / {notes[:1]}")

    shared = "https://www.amazon.in/dp/" + ASIN + "?tag=" + TAG + "&utm_source=whatsapp"
    aff = make_client()
    out, notes = repair(aff, f"post {shared}", store_obj=Book())
    check("a share link keeps our tag and stays a link (never blanked)",
          out.startswith("post https://www.amazon.in/dp/") and out != "post ", out)


# --------------------------------------------------------------------------- #
# 5. THE REPORT: the two is.gd links, through the bot's own final tidy pass
# --------------------------------------------------------------------------- #
def test_reported_links_end_to_end():
    print("\n== 5. the two reported links, end to end ==")
    book = Book(dict(REPORTED))
    aff = make_client(book)
    post = ("VOK57 Premium Korean Mink Blanket (Brown-Single)\n"
            "Deal \u20b9100  M.R.P.: \u20b91,999\n"
            "https://is.gd/c3Gl9d\n\n"
            "Safari Imperia Trolley Duffle Bag 58 cm (Teal)\n"
            "Deal \u20b9880.01  M.R.P.: \u20b94,520\n"
            "https://is.gd/bO79HA")
    out = asyncio.run(aff.shorten_long_urls_in_text(post))
    check("no is.gd link survives the final tidy pass", "is.gd" not in out, out)
    check("both destinations are published with OUR tag",
          out.count(f"?tag={TAG}") == 2, out)
    check("the blanket's own page is what is posted",
          f"https://www.amazon.in/dp/B0HNPB6QBS?tag={TAG}" in out, out)
    check("the duffle's own page is what is posted",
          f"https://www.amazon.in/dp/B097GNDJ37?tag={TAG}" in out, out)
    check("nothing else in the post changed - the deals survive whole",
          out.startswith("VOK57 Premium Korean Mink Blanket") and "\u20b9880.01" in out, out)
    check("every link in the result is provably ours",
          all(aff.provable_form(u) == u
              for u in bot.URL_RE.findall(out)), str(bot.URL_RE.findall(out)))

    print("\n== 5b. the pre-send gate: refuses the hop, accepts the repair ==")
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "commission.sqlite3")
        try:
            bad = "Deal \u20b9100 https://is.gd/c3Gl9d"
            ok_before, url_before = asyncio.run(store.presend_links_verified(bad))
            check("an unproven hop is refused before the send", not ok_before, str(url_before))
            check("...and the refusal names the hop itself",
                  url_before == "https://is.gd/c3Gl9d", str(url_before))

            fixer = make_client(Book(dict(REPORTED)))
            fixed = asyncio.run(fixer.repair_unowned_links(
                bad, store_obj=Book(dict(REPORTED))))[0]
            ok_after, url_after = asyncio.run(store.presend_links_verified(fixed))
            check("the repaired text passes the same gate", ok_after, str(url_after))
            check("no link in the repaired text needs a database row to prove itself",
                  f"?tag={TAG}" in fixed, fixed)
        finally:
            store.conn.close()

    print("\n== 5e. the same post, against a REAL Store database (no fakes) ==")
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "live-shape.sqlite3")
        try:
            for hop, destination in REPORTED.items():
                store.conn.execute(
                    "INSERT OR REPLACE INTO link_cache VALUES(?,?,?,?,?)",
                    (hop, hop, destination, bot.product_key(destination), 1),
                )
            store.conn.commit()
            real = make_client()               # no book passed: uses repair_book below
            real.repair_book = store
            out = asyncio.run(real.shorten_long_urls_in_text(post))
            check("the database's own rows are used to repair both hops",
                  "is.gd" not in out and out.count(f"?tag={TAG}") == 2, out)
            row = store.conn.execute(
                "SELECT affiliate_url, resolved_url FROM link_cache WHERE source_url=?",
                (bot.canonical_url("https://is.gd/c3Gl9d"),)).fetchone()
            check("the repair is remembered in link_cache (no repeat network call)",
                  row is not None and f"tag={TAG}" in str(row["affiliate_url"]),
                  str(dict(row) if row else None))
            ok, bad = asyncio.run(store.presend_links_verified(out))
            check("and the pre-send gate accepts the repaired text", ok, str(bad))
        finally:
            store.conn.close()

    print("\n== 5d. one post, the same product twice (bare + tagged): no doubled tag ==")
    list_post = ("1) Blanket \u20b9100 https://www.amazon.in/dp/" + ASIN + "\n"
                 "2) Blanket \u20b9100 https://www.amazon.in/dp/" + ASIN + "?tag=" + TAG)
    doubled = asyncio.run(make_client().shorten_long_urls_in_text(list_post))
    check("a bare link beside its tagged twin never doubles the tag",
          "?tag=" + TAG + "?tag" not in doubled, doubled)
    check("both lines still carry exactly one tag each",
          all(len(tag_of(u)) == 1 for u in bot.URL_RE.findall(doubled)), doubled)
    check("the replacer itself refuses to match a URL prefix",
          bot.replace_url_everywhere("x https://a.in/dp/B1?tag=t", "https://a.in/dp/B1",
                                     "https://a.in/dp/B1?tag=t")
          == "x https://a.in/dp/B1?tag=t",
          bot.replace_url_everywhere("x https://a.in/dp/B1?tag=t", "https://a.in/dp/B1",
                                     "https://a.in/dp/B1?tag=t"))
    check("...but still replaces the bare URL when it stands alone",
          bot.replace_url_everywhere("x https://a.in/dp/B1", "https://a.in/dp/B1",
                                     "https://a.in/dp/B1?tag=t") == "x https://a.in/dp/B1?tag=t")

    print("\n== 5c. repair is idempotent and never rewrites our own work ==")
    aff = make_client(Book(dict(REPORTED)))
    once = asyncio.run(aff.shorten_long_urls_in_text(post))
    twice = asyncio.run(aff.shorten_long_urls_in_text(once))
    check("running the tidy pass again changes nothing", once == twice, twice)
    _, notes = repair(make_client(Book(dict(REPORTED))), once, store_obj=Book(dict(REPORTED)))
    check("...and it has nothing left to repair", not notes, str(notes[:2]))


# --------------------------------------------------------------------------- #
# 6. 100 ROUNDS: the same corpus, re-checked until the answer stops moving
# --------------------------------------------------------------------------- #
def test_hundred_rounds():
    print("\n== 6. the whole corpus, re-checked 100 times (no drift) ==")
    corpus = [f"https://www.amazon.in/dp/{ASIN}{tail}" for tail in
              ("", f"?tag={TAG}", "?tag=theirs-21", "?psc=1", f"?tag={TAG}&psc=1")]
    corpus += [f"https://{h}/r1" for h in TOKENLESS]
    corpus += list(MERCHANTS)
    corpus += [bot.OUR_FOLDER_LINK, "https://www.zomato.com/offer/x"]

    book = Book({hop: dest for hop, dest in REPORTED.items()})
    for host in TOKENLESS:
        book.destinations[f"https://{host}/r1"] = "https://www.amazon.in/dp/" + ASIN
    converts = {page: link for page, link in MERCHANTS.items()}
    text = "Deal list\n" + "\n".join(f"- {url}" for url in corpus)

    baseline = None
    drift: list[str] = []
    unowned: list[str] = []
    for _round in range(100):
        round_book = Book(book.destinations, owned=set(converts.values()))
        out, notes = repair(make_client(round_book, converts=converts),
                            text, store_obj=round_book)
        if baseline is None:
            baseline = out
        elif out != baseline:
            drift.append(out[:120])
        round_aff = make_client(round_book, converts=converts)
        for url in bot.URL_RE.findall(out):
            if (url == bot.OUR_FOLDER_LINK or bot.is_share_intent(url)
                    or bot.AffiliateClient._is_service_link(url)):
                continue                      # not an affiliate destination at all
            if not asyncio.run(round_aff.link_is_provably_ours(url, round_book)):
                unowned.append(url)
        for note in notes:
            if note["status"] == "unrepairable" and host_of(note["url"]) not in TOKENLESS:
                unowned.append(note["url"])
    check("100 rounds of the same corpus give the same text", not drift, str(drift[:2]))
    check("100 rounds leave no link that cannot prove it pays us", not unowned,
          str(sorted(set(unowned))[:5]))
    check("the corpus really was repaired, not ignored",
          baseline is not None and "is.gd" not in baseline, str(baseline)[:160])


# --------------------------------------------------------------------------- #
def main() -> int:
    print("LINK COMMISSION: every published link must pay US\n" + "=" * 68)
    test_amazon_tag_matrix()
    test_hop_matrix()
    test_merchants()
    test_non_affiliate_untouched()
    test_reported_links_end_to_end()
    test_hundred_rounds()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED of {COUNT}: " + "; ".join(FAILS[:8]))
        return 1
    print(f"link commission PASS: {COUNT} checks. Every Amazon shape carries exactly "
          f"one tag - ours; every tokenless hop is repaired from our own book or "
          f"unwrapped live; a hop that proves nothing is never published; a bare "
          f"merchant page is converted, not given away; the two reported is.gd links "
          f"come out as OUR tagged product pages; and 100 rounds of the same corpus "
          f"give the same answer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
