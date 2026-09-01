"""Round-10 line fidelity: nothing the source wrote may be LOST, nothing of ours
may be ADDED, and no figure may appear more often than the source stated it.

The user's three complaints this file pins down:
  (a) "unwanted text assalu ravoddu"        - we must not bolt our own
      header/footer/folder-link onto a post;
  (b) "source lo neat ga unna text mana targets lo miss avuthundhi" - cleaning
      used to eat whole lines (a coupon clause glued after "more offers:") and
      used to delete characters (the ")" of a numbered list, the closing bracket
      of "(78% off)");
  (c) price repeated inside one post (our invented badge next to the source's
      own price line).

Run: python3 test_line_fidelity.py     (from the repo root)
"""
import asyncio
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


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra[:400]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


class FakeMsg:
    def __init__(self, text: str, msg_id: int):
        self.text = self.message = text
        self.id = msg_id
        self.media = None
        self.entities: list = []
        self.reply_to = None
        self.reply_markup = None


class FakeClient:
    def __init__(self, text: str):
        self.text = text

    async def get_messages(self, chat_id, ids=None):
        return FakeMsg(self.text, int(ids or 1))


class MerchantAffiliate:
    """Expands a shortener to a merchant page and monetizes only Amazon, the way
    the real client does; everything else passes through clean."""

    async def resolve(self, url):
        slug = url.rstrip("/").split("/")[-1]
        if "amzn" in url:
            return f"https://www.amazon.in/dp/B0{slug[:8].upper()}"
        return url

    async def convert(self, source, multi_link, resolved=None):
        target = resolved or source
        if "amazon.in" not in target:
            return None
        link = re.sub(r"[?#].*$", "", target) + f"?tag={bot.OUR_TAG}"
        return bot.LinkResult(source, target, link, bot.product_key(target))

    async def cache_link(self, *args, **kwargs):
        return None

    async def shorten_long_urls_in_text(self, text):
        return text

    async def link_not_broken(self, url):
        return True

    async def rendered_links_not_broken(self, text):
        return True


async def render(text: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "fid.sqlite3")
        old = bot.store
        bot.store = store
        try:
            store.conn.execute(
                "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key)"
                "VALUES(?,?,?,?,?,?)", (-100777001, 4242, "dealsvelocity", time.time(), 1, "777001"))
            store.conn.commit()
            row = store.conn.execute("SELECT * FROM queue WHERE msg_id=4242").fetchone()
            _msg, rendered, _price = await bot.render_job(FakeClient(text), MerchantAffiliate(), row)
            return rendered
        finally:
            bot.store = old


def strip_links(line: str) -> str:
    """A post's links are OURS by design, so link text is never compared."""
    return bot.URL_RE.sub(" ", re.sub(r"\S*(?:amzn|fktr|flipkart|myntra|ajio|croma|bitly|bitli)\S*", " ", line))


def source_lines(text: str) -> list[str]:
    """Lines a subscriber is entitled to see, with their links removed: a line
    that survives without its link survived (the link itself is replaced on
    purpose - that is the monetization rule)."""
    out = []
    for line in text.splitlines():
        body = strip_links(line).strip(" \t|:\u279c\ufe0f\U0001f449-")
        if body:
            out.append(body)
    return out


def amounts_of(text: str) -> dict[str, int]:
    """Every amount and percentage a text states, with links masked out.

    Only ₹-figures and percentages count: an ASIN, a tracking id or a list
    number is not a claim about the deal, and our own link text would otherwise
    swamp the comparison."""
    body = bot.URL_RE.sub(" ", text or "")
    found = re.findall(r"[\u20b9$]\s*\d[\d,.]*|\b\d{1,3}(?:\.\d+)?\s*(?:%|percent)", body)
    counts: dict[str, int] = {}
    for hit in found:
        key = re.sub(r"[^\d]", "", hit)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


CORPUS = {
    "banner + numbered list + coupon + mrp": (
        "\U0001F525\U0001F525 TOP DEAL OF THE DAY \U0001F525\U0001F525\n"
        "1) boAt Airdopes 141 TWS \u2013 \u20b91,099 (78% off)\n"
        "https://www.flipkart.com/boat-airdopes-141/p/itmaaa1111\n"
        "2) Noise ColorFit Pro 4 Smartwatch \u2013 \u20b91,499 (62% off)\n"
        "https://www.flipkart.com/noise-colorfit-pro4/p/itmbbb2222\n"
        "3) HP 15s Core i5 Laptop \u2013 \u20b941,990\n"
        "https://www.flipkart.com/hp-15s-core-i5/p/itmbbb3333\n"
        "Use code: LOOT100 for extra \u20b9100 off\n"
        "More offers: Apply coupon HPNONT10 on the laptop\n"),
    "single product with headings and notes": (
        "\u26a1\ufe0f 11 PM FLASH SALE \u26a1\ufe0f\n"
        "Apple iPhone 15 (Blue, 128GB)\n"
        "\u20b958,999 | MRP \u20b969,900 (16% OFF)\n"
        "Extra \u20b92,500 off on Exchange\n"
        "Note: Free delivery tomorrow\n"
        "Buy \U0001F449 https://amzn.to/3xApple15\n"),
    "bank offer plus code": (
        "\U0001F6A8 PRICE DROP \U0001F6A8\n"
        "Sony WH-CH720N Wireless Headphones\n"
        "Now \u20b95,990 (MRP \u20b910,990)\n"
        "SBI Card: 5% cashback up to \u20b92,500\n"
        "Coupons: CH720OFF\n"
        "More deals here: https://amzn.to/ch720deal\n"),
}


def run_case(name: str, text: str):
    print(f"\n== {name} ==")
    rendered = asyncio.run(render(text))
    print("  ---- post ----")
    for ln in rendered.split("\n"):
        print("  |", ln)
    print("  ----------------")

    missing = []
    # With STRIP_CAMPAIGN_BANNERS=true (off by default, opt-in) the source's hype
    # lines are *meant* to be dropped, so they must not be reported as lost text.
    strip_banners = bool(getattr(bot, "STRIP_CAMPAIGN_BANNERS", False))
    for line in source_lines(text):
        if strip_banners and bot.is_campaign_banner_line(line):
            continue
        # A line counts as survived when its words and its figures are still
        # there. Channel boilerplate words are excluded on purpose: removing
        # "buy now" or "more deals here" is the cleaning doing its job, and the
        # check must not turn that into a "lost text" false alarm.
        line = re.sub(r"(?i)\b(?:buy|shop|order|grab|get|check|click|tap|join|subscribe|follow|"
                      r"share|forward|notify|notifications|hurry|stay|tuned|miss|now|here|below|"
                      r"more|for|deals?|offers?|loots?|updates?|link)\b", " ", line)
        if not re.search(r"[A-Za-z]{3}", line):
            continue                       # nothing but boilerplate to begin with
        words = [w.strip("().,:;").lower() for w in line.split() if any(c.isalpha() for c in w)]
        numbers = re.findall(r"\d[\d,.]*", line)
        ok_words = all(w in rendered.lower() for w in words if len(w) > 2)
        ok_numbers = all(n in rendered for n in numbers)
        if not (ok_words and ok_numbers):
            missing.append(line)
    check("every source line survives in full", not missing, str(missing))

    check("the post is never cut with an ellipsis", "\u2026" not in rendered)
    if bot.ADD_OUR_CHANNEL_FOOTER:
        # Opt-in mode: the family footer may appear (only where the code puts it -
        # card/bank offers), so the promise becomes: never a second block of it,
        # and never our tricks footer on an ordinary loot post.
        check("with the footer opt-in the footer is added at most once and no tricks footer",
              rendered.count(bot.OUR_FOLDER_LINK) <= 1 and "tricks" not in rendered.lower())
    else:
        check("no channel-branded footer of ours was added",
              "JOIN OUR COMPLETE LOOT FAMILY" not in rendered
              and "All Loot Channels" not in rendered
              and bot.OUR_FOLDER_LINK not in rendered)
    check("no separator/bullet characters were invented",
          "\u2501" not in rendered and not re.search(r"^[ \t]*[\u279c\u2192\u27a4][ \t]*$", rendered, re.M))

    # A figure may appear at most as often as the source wrote it. This is the
    # "two prices in one post" bug, stated so it cannot be gamed.
    source_amounts = amounts_of(text)
    post_amounts = amounts_of(rendered)
    dupes = [f"{value}: source said it {source_amounts.get(value, 0)}x, the post says it {count}x"
             for value, count in post_amounts.items() if count > source_amounts.get(value, 0)]
    check("no amount or percentage is printed more often than the source wrote it",
          not dupes, str(dupes))
    check("and every figure the source stated is still there",
          all(post_amounts.get(value) == count for value, count in source_amounts.items()),
          str({v: (source_amounts[v], post_amounts.get(v)) for v in source_amounts
               if post_amounts.get(v) != source_amounts[v]}))

    # Every URL in the post is one we control or a clean merchant page - never a
    # source shortener and never another channel's link.
    for url in bot.URL_RE.findall(rendered):
        host = (url.split("/")[2] if url.count("/") >= 2 else "").lower()
        if host in {"t.me", "telegram.me"} and bot.ADD_OUR_CHANNEL_FOOTER:
            # The folder/channel link we add on purpose is allowed; someone else's is not.
            check("our own t.me link is the only Telegram link",
                  url.split("?")[0] in {bot.OUR_FOLDER_LINK, *bot.OUR_MAIN_CHANNEL_LINKS},
                  url)
            continue
        check(f"{host} is not a foreign short link or another channel",
              host not in {"t.me", "telegram.me", "bit.ly", "amzn.to", "tinyurl.com",
                           "www.amzn.to", "bitly.com"})


def test_signature_rules():
    print("\n== the same-product signature ==")
    a = f"LG 1.5 Ton 5 Star Inverter Split AC\nNow {R}36,990 (MRP {R}74,990)\nhttps://www.croma.com/a1"
    b = f"LG 1.5 Ton 5 Star Inverter Split AC\n{R}36,990 50% off\nhttps://www.croma.com/some/other/path"
    c = f"LG Neo Duetto 7Kg Front Load Washing Machine\n{R}31,990\nhttps://www.croma.com/a1"
    check("the same product through two different links is recognised",
          bot.product_signature(a) == bot.product_signature(b),
          f"{bot.product_signature(a)} vs {bot.product_signature(b)}")
    check("a different product is never collapsed onto it",
          bot.product_signature(a) != bot.product_signature(c))
    check("a multi-product list has no single-product identity",
          bot.product_signature("1) Shirt A \u20b9599 https://a.com/x\n2) Shirt B \u20b9699 https://a.com/y\n"
                                "3) Shirt C \u20b9799 https://a.com/z") is None)
    check("a category phrase alone is too weak to skip anything",
          bot.product_signature("Men Cotton Shirt\n" + f"{R}599\nhttps://www.amazon.in/dp/B0X1Y2Z3AB") is None)
    check("the rule is off-switchable", "SAME_PRODUCT_SKIP_SECONDS" in dir(bot))


def test_cleaning_never_eats_a_line():
    print("\n== cleaning only removes the clause, never the line ==")
    cases = [
        (f"More offers: Apply coupon PEOPLE200 on {R}599 shirts", "PEOPLE200"),
        (f"Extra {R}2,500 off with code HDFC10 - buy now", "HDFC10"),
        (f"1) boAt Airdopes 141 TWS \u2013 {R}1,099 (78% off)", "(78% off)"),
        (f"2) WROGN Shirt \u2013 {R}699 (M.R.P {R}2,199)", "(M.R.P"),
    ]
    for line, must_survive in cases:
        out = bot.strip_inline_cta(line)
        check(f"'{line[:38]}\u2026' keeps '{must_survive}'", must_survive in out, out)
        check(f"'{line[:38]}\u2026' is not emptied", out.strip() != "", out)
    hype = "For more deals join our channel and turn on notifications"
    check("pure channel boilerplate is still removed",
          "join our channel" not in bot.strip_inline_cta(hype).lower(),
          bot.strip_inline_cta(hype))
    check("a bracket left dangling by cleanup goes away",
          "(" not in bot.sanitize_outbound_text("Price \u20b9499 ("))
    check("a real bracketed pair stays", "(78% off)" in bot.sanitize_outbound_text("Deal (78% off)"))
    check("a numbered list keeps its brackets",
          "1)" in bot.sanitize_outbound_text("1) boAt Airdopes 141 TWS \u2013 \u20b91,099 (78% off)"))
    check("an empty bracket pair left by a stripped phrase goes",
          "(" not in bot.sanitize_outbound_text("Price \u20b9499 ( ) extra"))


def main() -> int:
    for name, text in CORPUS.items():
        run_case(name, text)
    test_signature_rules()
    test_cleaning_never_eats_a_line()
    print("\n" + ("LINE FIDELITY: FAILURES: " + ", ".join(FAILS) if FAILS else "test_line_fidelity: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
