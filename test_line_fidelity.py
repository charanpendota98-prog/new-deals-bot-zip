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
import subprocess
import shutil
import json
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

    # ---- the hard cases, i.e. the ones that decide whether this rule helps ----
    # Same product, caption written differently by a different channel.
    pairs_same = [
        ("boat earbuds across two sources",
         f"\U0001f525 boAt Airdopes 141 TWS Earbuds with ENx\n{R}1,099 (78% off)\nhttps://www.croma.com/b1",
         f"boAt Airdopes 141 True Wireless Earbuds, 42H Playtime\nMRP {R}4,990 Now {R}1,099\nhttps://www.amazon.in/dp/B0BS1KJ?tag=deals0911-21"),
        ("iPhone, one copy names the storage twice, the other once",
         f"Apple iPhone 13 (Blue, 128 GB)\n{R}48,999\nhttps://www.flipkart.com/apple-iphone-13-blue/p/x",
         f"Apple iPhone 13 (Blue, 128 GB Storage)\n{R}48,999 | MRP {R}56,900 (13% off)\nhttps://www.smartprix.com/go/y"),
        ("Sony headphones, upper case and a price suffix",
         "Sony WH-CH720N Noise Cancelling Headphones\n\u20b95,990 (40% off)\nhttps://www.croma.com/sony-1",
         "SONY WH-CH720N Wireless Noise Cancelling Headphones - \u20b95990 only\nhttps://www.buyhatke.com/go/sony"),
    ]
    for label, a, b in pairs_same:
        check(f"same product with a different caption: {label}",
              bot.product_signature(a) is not None
              and bot.product_signature(a) == bot.product_signature(b),
              f"{bot.product_signature(a)} vs {bot.product_signature(b)}")

    pairs_different = [
        ("model number differs by two digits",
         f"boAt Airdopes 141 TWS Earbuds\n{R}1,099\nhttps://www.croma.com/b1",
         f"boAt Airdopes 131 TWS Earbuds\n{R}999\nhttps://www.croma.com/b2"),
        ("storage variant",
         f"Samsung Galaxy S23 FE 5G (128 GB)\n{R}49,999\nhttps://www.flipkart.com/s23fe-128/p/x",
         f"Samsung Galaxy S23 FE 5G (256 GB)\n{R}55,999\nhttps://www.flipkart.com/s23fe-256/p/y"),
        ("a variant qualifier (FE)",
         f"Samsung Galaxy S23 5G\n{R}65,999\nhttps://www.flipkart.com/s23/p/x",
         f"Samsung Galaxy S23 FE 5G\n{R}49,999\nhttps://www.flipkart.com/s23fe/p/y"),
        ("ram differs",
         f"LG 8 Kg Fully Automatic Top Load Washing Machine\n{R}31,990\nhttps://www.croma.com/w1",
         f"LG 7 Kg Fully Automatic Top Load Washing Machine\n{R}26,990\nhttps://www.croma.com/w2"),
    ]
    for label, a, b in pairs_different:
        sa, sb = bot.product_signature(a), bot.product_signature(b)
        check(f"a different product is never collapsed: {label}",
              sa is not None and sb is not None and sa != sb, f"{sa} vs {sb}")

    # The eight-word slice was the other half of the bug: two laptops whose names
    # differ only past word eight used to collapse, so the cheaper-looking one was
    # skipped as a duplicate and a real deal was lost.
    lap1 = (f"Lenovo IdeaPad Slim 3 15.6-inch FHD IPS Laptop (Intel i5-1235U/8GB/512GB SSD)\n"
            f"{R}38,990\nhttps://www.flipkart.com/ideapad-a")
    lap2 = (f"Lenovo IdeaPad Slim 3 15.6-inch FHD IPS Laptop (Intel i5-12450H/16GB/512GB SSD)\n"
            f"{R}38,990\nhttps://www.flipkart.com/ideapad-b")
    lap3 = (f"Lenovo IdeaPad Slim 3 15.6-inch FHD IPS Laptop (Intel i5-1235U/8GB/512GB SSD)\n"
            f"{R}38,990 only\nhttps://fktr.in/xyz9")
    check("two laptops differing past word eight are two deals",
          bot.product_signature(lap1) != bot.product_signature(lap2),
          f"{bot.product_signature(lap1)} vs {bot.product_signature(lap2)}")
    check("and the same laptop at another link is still one product",
          bot.product_signature(lap1) == bot.product_signature(lap3)
          and bot.product_signature(lap1) is not None,
          f"{bot.product_signature(lap1)} vs {bot.product_signature(lap3)}")

    # Nothing with a name but no model number may be keyed from a short phrase.
    check("a one-word brand plus fluff is not an identity",
          bot.product_signature(f"Running Shoes\n{R}1,299\nhttps://www.amazon.in/dp/B0SHOES1") is None)


def test_auditor_mirrors_the_bot():
    print("\n== the auditor uses the SAME identity rule as the bot ==")
    # The auditor hashes nothing and the bot does, so the two are compared on what
    # actually matters: which posts get an identity at all, and which pairs of
    # posts are judged to be the same product. A finding list that disagrees with
    # the bot's behaviour would only accuse the bot of bugs it does not have.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "ops"))
    import importlib
    auditor = importlib.import_module("quality_audit")
    texts = [
        f"boAt Airdopes 141 TWS Earbuds with ENx\n{R}1,099 (78% off)\nhttps://www.croma.com/b1",
        f"boAt Airdopes 141 True Wireless Earbuds, 42H Playtime\nMRP {R}4,990 Now {R}1,099\nhttps://www.amazon.in/dp/B0BS1KJ?tag=deals0911-21",
        f"boAt Airdopes 131 TWS Earbuds\n{R}999\nhttps://www.croma.com/b2",
        f"Apple iPhone 13 (Blue, 128 GB)\n{R}48,999\nhttps://www.flipkart.com/x",
        f"Apple iPhone 13 (Blue, 128 GB Storage)\n{R}48,999 | MRP {R}56,900\nhttps://www.smartprix.com/y",
        "1) Shirt A \u20b9599 https://a.com/x\n2) Shirt B \u20b9699 https://a.com/y\n3) Shirt C \u20b9799 https://a.com/z",
        f"Men Cotton Shirt\n{R}599\nhttps://www.amazon.in/dp/B0X1Y2Z3AB",
        f"Samsung Galaxy S23 FE 5G (256 GB)\n{R}55,999\nhttps://www.flipkart.com/z",
    ]
    keyed_bot = [bot.product_signature(t) is not None for t in texts]
    keyed_audit = [bool(auditor.product_signature(t)) for t in texts]
    check("both decide the same set of posts have an identity", keyed_bot == keyed_audit,
          f"{keyed_bot} vs {keyed_audit}")
    mismatched = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            same_bot = bot.product_signature(texts[i]) == bot.product_signature(texts[j])
            same_audit = (auditor.product_signature(texts[i])
                          == auditor.product_signature(texts[j]))
            if keyed_bot[i] and keyed_bot[j] and same_bot != same_audit:
                mismatched.append((i, j))
    check("both judge every pair the same way (same product or not)",
          not mismatched, str(mismatched))
    # The auditor's rule is GENERATED from the bot's block, so a real drift is only
    # possible when someone edits one copy and forgets to re-sync. Catch that here,
    # where the failure message says what to run.
    import subprocess
    sync = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "ops" / "sync_identity.py"),
                           "--check"], capture_output=True, text=True)
    check("the auditor's identity block is in sync with the bot's",
          sync.returncode == 0, (sync.stdout + sync.stderr).strip())


def test_chunking_never_cuts_a_link():
    print("\n== an over-long line is packed, never bisected ==")
    url = "https://www.amazon.in/dp/B0ABCDEFGHI?tag=deals0911-21"
    line = "Prestige PIC-MAD 2600 5 Burner Manual Stainless Steel " + "x" * 4200 + " " + url
    parts = bot.chunks(line, 4096)
    check("every part fits Telegram", all(len(part) <= 4096 for part in parts),
          str([len(x) for x in parts]))
    whole = [part for part in parts if url in part]
    check("the link stays whole in exactly one part", len(whole) == 1, str(len(whole)))
    check("no part ends with half a link",
          not any(re.search(r"https?://\S*$", part) and url not in part for part in parts),
          "\n".join(part[-60:] for part in parts))
    squash = lambda t: re.sub(r"\s+", "", t)
    check("the split loses nothing", squash("".join(parts)) == squash(line))
    # A single monster token (no spaces at all) still has to come out complete.
    parts = bot.chunks("q" * 9000, 4096)
    check("an impossible token is still emitted in full", squash("".join(parts)) == "q" * 9000)
    # Ordinary posts keep splitting on lines, exactly as before.
    ordinary = "\n".join(f"Item {i} \u20b9{i * 10} https://a.test/{i}" for i in range(300))
    parts = bot.chunks(ordinary, 4096)
    check("a long list keeps every item and every link",
          squash("".join(parts)) == squash(ordinary)
          and sum(part.count("https://a.test/") for part in parts) == 300,
          f"{len(parts)} parts")


def test_whatsapp_identity_is_the_same_rule():
    print("\n== Telegram and WhatsApp key a product identically ==")
    # A product that counts as "already posted" on Telegram but as "new" on WhatsApp
    # (or the other way round) is exactly the complaint that started this, and no
    # test could see it while the two services kept private copies of the rule. The
    # bridge can print its identity for a headline (--identity-probe), so both are
    # compared here, on the same corpus, on every run.
    node = shutil.which("node")
    bridge = Path(__file__).resolve().parent / "tg-wa-bridge" / "bridge.js"
    if not node or not bridge.exists():
        print("  SKIP  node or the bridge is not present on this box")
        return
    lines = [
        "Samsung 55-inch Crystal 4K UHD Smart TV",
        "Samsung Crystal 4K UHD 55 inch Smart TV (2023)",
        "boAt Airdopes 141 TWS Earbuds with ENx",
        "boAt Airdopes 141 True Wireless Earbuds, 42H Playtime",
        "boAt Airdopes 131 TWS Earbuds",
        "LG 1.5 Ton 5 Star Inverter Split AC",
        "LG 1.5 Ton 3 Star Inverter Split AC",
        "Apple iPhone 13 (Blue, 128 GB)",
        "Apple iPhone 13 (Blue, 128 GB Storage)",
        "Noise ColorFit Pro 4 Bluetooth Calling Smartwatch",
        "Sony WH-CH720N Noise Cancelling Headphones",
        "Men Cotton Shirt",
    ]
    env = dict(os.environ, TELEGRAM_BOT_TOKEN="1:dummy", WA_PHONE="910000000000",
               WA_CHANNEL="@selftest")
    proc = subprocess.run([node, str(bridge), "--identity-probe"],
                          input="\n".join(lines), capture_output=True, text=True,
                          env=env, cwd=str(bridge.parent))
    check("the bridge identity probe ran", proc.returncode == 0, proc.stderr[-300:])
    if proc.returncode != 0:
        return
    rows = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    mismatch = []
    for row in rows:
        mine = bot._product_identity(row["line"])
        mine = "|".join(mine) if mine else None
        if mine != row["identity"]:
            mismatch.append(f"{row['line'][:38]!r}: bot={mine} whatsapp={row['identity']}")
    check("both services key every headline the same way", not mismatch, "; ".join(mismatch))
    ids = {row["line"]: row["identity"] for row in rows}
    pairs_same = [("boAt Airdopes 141 TWS Earbuds with ENx",
                   "boAt Airdopes 141 True Wireless Earbuds, 42H Playtime"),
                  ("Samsung 55-inch Crystal 4K UHD Smart TV",
                   "Samsung Crystal 4K UHD 55 inch Smart TV (2023)"),
                  ("Apple iPhone 13 (Blue, 128 GB)", "Apple iPhone 13 (Blue, 128 GB Storage)")]
    pairs_diff = [("boAt Airdopes 141 TWS Earbuds with ENx", "boAt Airdopes 131 TWS Earbuds"),
                  ("LG 1.5 Ton 5 Star Inverter Split AC", "LG 1.5 Ton 3 Star Inverter Split AC")]
    for a, b in pairs_same:
        check(f"both call these the same product: {a[:26]}\u2026",
              ids.get(a) and ids[a] == ids[b], f"{ids.get(a)} vs {ids.get(b)}")
    for a, b in pairs_diff:
        check(f"both keep these apart: {a[:26]}\u2026",
              ids.get(a) and ids.get(b) and ids[a] != ids[b], f"{ids.get(a)} vs {ids.get(b)}")


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
    test_auditor_mirrors_the_bot()
    test_chunking_never_cuts_a_link()
    test_whatsapp_identity_is_the_same_rule()
    test_cleaning_never_eats_a_line()
    print("\n" + ("LINE FIDELITY: FAILURES: " + ", ".join(FAILS) if FAILS else "test_line_fidelity: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
