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
os.environ.setdefault("AMAZON_TAG", "")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
import main_bot_new as bot  # noqa: E402

# This suite is about the TEXT of a post, and it renders with a fake affiliate that only
# monetizes Amazon. It therefore needs unmonetized store links to survive the pipeline,
# which is exactly what PASSTHROUGH_UNMONETIZED controls. Pin it here (that knob's own
# behaviour is tested in test_pipeline_fixes / test_duplicate_sim), so running this file
# under PASSTHROUGH_UNMONETIZED=false compares the same text instead of erroring out.
bot.PASSTHROUGH_UNMONETIZED = True

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
    def __init__(self, text: str, media=None):
        self.text = text
        self.media = media

    async def get_messages(self, chat_id, ids=None):
        msg = FakeMsg(self.text, int(ids or 1))
        msg.media = self.media
        return msg


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
    "price glued to a coupon code": (
        "Lizol Floor Cleaner Shakti Disinfectant with Jasmine Fragrance 900ml (Pack of 2)\n"
        "\u2705Deal Price: \u20b9 199HFJF\n"
        "\u274cMRP: \u20b9 270\n"
        "Discount: 26%\n"
        "Use code HFJF for Extra Discount\n"
        "\U0001f449 [https://www.amazon.in/dp/B0GH2374K3](https://amzn.to/lizol77)\n"),
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
        # "199HFJF" and "199 HFJF" are the same content: a price that was glued to a
        # code in the source is intentionally un-glued by cleaning, so the comparison
        # splits digit/letter junctions on BOTH sides instead of demanding the glue.
        def _unglue(value: str) -> str:
            return re.sub(r"(?<=[0-9])(?=[A-Za-z])|(?<=[A-Za-z])(?=[0-9])", " ", value)
        words = [w.strip("().,:;").lower() for w in _unglue(line).split() if any(c.isalpha() for c in w)]
        numbers = re.findall(r"\d[\d,.]*", line)
        ok_words = all(w in _unglue(rendered.lower()) for w in words if len(w) > 2)
        ok_numbers = all(n in rendered for n in numbers)
        if not (ok_words and ok_numbers):
            missing.append(line)
    check("every source line survives in full", not missing, str(missing))

    check("the post is never cut with an ellipsis", "\u2026" not in rendered)
    check("the post's blank lines are the source's own",
          "\n\n" in "\n".join(source_lines(text)) or "\n\n" not in rendered, repr(rendered[:80]))
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
         f"boAt Airdopes 141 True Wireless Earbuds, 42H Playtime\nMRP {R}4,990 Now {R}1,099\nhttps://www.amazon.in/dp/B0BS1KJ"),
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
        f"boAt Airdopes 141 True Wireless Earbuds, 42H Playtime\nMRP {R}4,990 Now {R}1,099\nhttps://www.amazon.in/dp/B0BS1KJ",
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
    url = "https://www.amazon.in/dp/B0ABCDEFGHI"
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
    # USER RULE (round 13): text glued onto a price is UNWANTED, whatever it looks like
    # - the live channel's price line arrives as "₹85h" / "₹ 199HFJF" / "₹85jsjd".
    # It is cut, nothing of ours is written in its place, and the LINE survives with its
    # price. What the source wrote APART (a labelled code, a unit, an ordinary word) is
    # left exactly as written - cleaning must never eat real text.
    for line, price, must_not_survive in [
        ("\u2705Deal Price: \u20b9 199HFJF", 199, "HFJF"),
        ("Deal \u20b91,099PEOPLE200", 1099, "PEOPLE200"),
        ("\U0001f525 Hair Clips at \u20b985h", 85, "h"),
        ("\u274cMRP: \u20b9270jsjd", 270, "jsjd"),
    ]:
        out = bot.strip_price_junk(line)
        check(f"glued scrap is cut: '{line[:24]}\u2026'", must_not_survive not in out, out)
        check(f"price survives clean: '{line[:24]}\u2026'", bot.parse_price(out) == price, out)
        check(f"nothing of ours is added: '{line[:24]}\u2026'",
              all(w.strip("().,:;") in line for w in out.split()), out)
    check("a spaced, labelled code is real text and stays",
          bot.strip_price_junk("Use code HFJF for \u20b9199 off") == "Use code HFJF for \u20b9199 off")
    check("a spaced all-caps code is left alone",
          bot.strip_price_junk("Price \u20b9199 SAVE200") == "Price \u20b9199 SAVE200")
    check("units and ordinary words after a price stay",
          bot.strip_price_junk("500ml \u20b9260 offer") == "500ml \u20b9260 offer")
    check("torn shortener debris glued to a price is cut",
          bot.strip_price_junk("\u20b9260tG7oChgiQuTgS25b") == "\u20b9260")
    check("a glued lowercase tail is cut as before", bot.strip_price_junk("\u20b9122oya") == "\u20b9122")
    check("un-gluing never fuses two lines",
          bot.strip_price_junk("\u274cMRP: \u20b9 270\nDiscount: 26%").splitlines()
          == ["\u274cMRP: \u20b9 270", "Discount: 26%"])
    check("cutting a glued scrap is idempotent",
          bot.strip_price_junk("\u2705Deal Price: \u20b9 199") == "\u2705Deal Price: \u20b9 199")


def test_layout_is_the_sources_blank_lines_only():
    """A line we deleted must not survive as a blank line, and a blank line the source
    wrote must not be squeezed out. 'exactly like the source' includes the spacing."""
    check("no blank line the source did not write",
          bot.keep_source_spacing("A\n\nB\n\nC", "A\nB\nC") == "A\nB\nC",
          bot.keep_source_spacing("A\n\nB\n\nC", "A\nB\nC"))
    check("the source's own spacing is kept exactly",
          bot.keep_source_spacing("A\n\nB", "A\n\nB") == "A\n\nB")
    check("empty text is not turned into a line", bot.keep_source_spacing("", "A") == "")


async def render_media(text: str, with_media: bool = True) -> str:
    """Same render path, on a message that carries a photo and no link at all."""
    with tempfile.TemporaryDirectory() as td:
        store = bot.Store(Path(td) / "fid-media.sqlite3")
        old_store = bot.store
        bot.store = store
        try:
            store.conn.execute(
                "INSERT INTO queue(chat_id,msg_id,source,created_at,priority,chat_key)"
                "VALUES(?,?,?,?,?,?)", (-100777002, 4243, "dealsvelocity", time.time(), 2, "777002"))
            store.conn.commit()
            row = store.conn.execute("SELECT * FROM queue WHERE msg_id=4243").fetchone()
            client = FakeClient(text, None if not with_media else object())
            # (render_job re-fetches the message, so the media flag lives on the client)   # render_job re-fetches the msg
            _m, rendered, _p = await bot.render_job(client, MerchantAffiliate(), row)
            return rendered
        finally:
            bot.store = old_store


def test_a_photo_post_with_no_link_is_still_posted():
    """Round 13: the last swallow was structural, not about links - a source post with a
    photo and no URL at all ("link in the photo", "link in the comments") used to die at
    `no URLs`. The source published it, so our channels must have it too, as posted.
    A photo with no deal terms is not a deal, and a text-only line with nothing to buy
    stays skipped - publishing those would be the unwanted-text bug in the other direction."""
    import asyncio as _aio
    clip = "🔥 (Pack Of 20) Multicolor Hair Clips at ₹85 only, 90% off"
    got = _aio.run(render_media(clip))
    check("a photo post with a price and no link is published",
          "Multicolor Hair Clips" in got and "₹85" in got and "90%" in got, repr(got))
    check("no link is invented for a link-free post", not bot.URL_RE.search(got), repr(got))
    check("the source's own price line survives untouched", "at ₹85 only" in got, repr(got))
    skipped = None
    try:
        _aio.run(render_media("📸 just look at this photo, nothing to buy here"))
    except bot.PermanentSkip as exc:
        skipped = str(exc)
    check("a photo with no deal terms is still not posted", skipped == "no URLs", repr(skipped))
    skipped = None
    try:
        _aio.run(render_media("🔥 Hair Clips ₹85 only", with_media=False))
    except bot.PermanentSkip as exc:
        skipped = str(exc)
    check("a text-only line with nothing to buy is still not posted", skipped == "no URLs",
          repr(skipped))


def test_markdown_link_with_a_url_label_keeps_the_merchant_url():
    """[https://amazon.in/dp/X](https://some-shortener/y) must publish X, not y.

    The label is the canonical merchant page, which our own provenance layer turns
    into our affiliate link; the href is a third-party shortener we never publish.
    Deciding this once here stops the two passes from disagreeing later.
    """
    line = ("\U0001f449 [https://www.amazon.in/dp/B0GH2374K3]"
            "(https://new.growseek.io/45934/abc?shortlink=6328a1d4)")
    cleaned = bot.clean_source_text(line)
    check("a URL label beats a foreign shortener href",
          "amazon.in/dp/B0GH2374K3" in cleaned and "growseek" not in cleaned, cleaned)
    check("and no markdown brackets are left behind",
          "[" not in cleaned and "]" not in cleaned, cleaned)
    text_only = "\U0001f449 [Buy Now](https://new.growseek.io/45934/abc)"
    check("a plain text label keeps the href it points at",
          "growseek.io/45934/abc" in bot.clean_source_text(text_only), bot.clean_source_text(text_only))


def test_photos_arrive_as_the_source_posted_them():
    """A loot channel sends its photos as ONE album (a grid in one bubble). Our channel
    has to show the same grid - not the first image and nothing else - and a photo that
    cannot be fetched, or a Telegram call that refuses, must never cost us the deal."""
    import asyncio as _aio
    import tempfile as _tf
    from types import SimpleNamespace

    class FakePhoto:
        def __init__(self, index, grouped=True):
            self.id = 900 + index
            self.photo = f"PHOTO{index}"
            self.media = object()
            self._index = index
            self.grouped_id = 77 if grouped else None

        async def get_grouped_items(self):
            return [FakePhoto(i) for i in range(3)]

    class RecClient:
        def __init__(self, fail=(), no_refs=False, no_raw_call=False):
            self.fails, self.calls, self.files = set(fail), [], []
            self.no_refs, self.no_raw_call = no_refs, no_raw_call

        async def download_media(self, msg, file=None):
            if getattr(msg, "_index", 0) in self.fails:
                raise OSError("photo vanished")
            path = f"{file}_{getattr(msg, '_index', 0)}"
            Path(path).write_bytes(b"xx")
            self.files.append(path)
            return path

        async def get_input_entity(self, entity):
            return "peer"

        async def upload_file(self, path):
            self.calls.append("upload_file")
            return SimpleNamespace(id=1, parts=1, name="p", md5_checksum="")

        async def __call__(self, request):
            name = type(request).__name__
            if self.no_raw_call:
                raise RuntimeError("not a real client")
            if name == "UploadMediaRequest":
                return SimpleNamespace(media=SimpleNamespace(photo="UPLOADED"))
            self.calls.append(name)
            self.last = request
            return SimpleNamespace(id=1)

        async def send_file(self, entity, media, caption=None, parse_mode=None):
            self.calls.append("send_file")
            self.files_sent = getattr(self, "files_sent", []) + [(media, caption)]

    wanted = min(3, bot.MAX_ALBUM_PHOTOS)          # the cap is an operator knob
    async def drive():
        with _tf.TemporaryDirectory() as td:
            base = Path(td) / "post"
            client = RecClient()
            paths, refs = await bot.download_album(client, FakePhoto(0), base, 1)
            check("an album is downloaded IN FULL (up to MAX_ALBUM_PHOTOS), in the source's order",
                  len(paths) == wanted and len(refs) == wanted and paths[0].endswith("_0"),
                  f"{paths} cap={bot.MAX_ALBUM_PHOTOS}")
            # a photo that will not download is left out; the rest still go
            # The cap applies FIRST (only the first MAX_ALBUM_PHOTOS photos are looked
            # at), so the survivors are those of THOSE that downloaded.
            partial_paths, partial_refs = await bot.download_album(
                RecClient(fail=(1,)), FakePhoto(0), base, 1)
            expected_partial = wanted - (1 if wanted > 1 else 0)
            check("a photo that will not download is left out, the rest still go",
                  len(partial_paths) == expected_partial and len(partial_refs) == expected_partial,
                  f"{partial_paths} cap={bot.MAX_ALBUM_PHOTOS}")
            solo_paths, _ = await bot.download_album(RecClient(), FakePhoto(0, grouped=False), base, 1)
            check("a single-photo post keeps its own path (no album machinery)",
                  len(solo_paths) == 1, str(solo_paths))

            sent = RecClient()
            await bot.send_media_item(sent, "entity", paths, "deal text", refs)
            req = getattr(sent, "last", None)
            if wanted >= 2:
                check("2+ photos go out as ONE album (grid), caption on the first item",
                      type(req).__name__ == "SendMultiMediaRequest"
                      and len(req.multi_media) == wanted
                      and req.multi_media[0].message == "deal text"
                      and req.multi_media[1].message == "", str(sent.calls))
            else:
                # MAX_ALBUM_PHOTOS=1 is "behave like before the album existed" - and that
                # has to be a real promise: one file with the caption, no grouped call.
                check("MAX_ALBUM_PHOTOS=1 means one photo with the caption, no album at all",
                      req is None and sent.calls == ["send_file"], str(sent.calls))
            check("the album reuses the source's photo refs - nothing is re-uploaded",
                  "upload_file" not in sent.calls, str(sent.calls))

            if wanted >= 2:
                uploaded = RecClient(no_refs=True)
                ok = await bot.send_media_group(uploaded, "entity", "cap", paths, [None] * len(paths))
                check("without usable refs the photos are uploaded once each, then grouped",
                      ok and uploaded.calls.count("upload_file") == len(paths), str(uploaded.calls))
            else:
                check("with a one-photo cap nothing is grouped, so nothing is uploaded either",
                      bot.outbound_parts("t", paths)[0][0] == "file" and len(paths) == 1, str(paths))

            fallback = RecClient(no_raw_call=True)
            await bot.send_media_item(fallback, "entity", paths, "deal text", refs)
            check("a client that cannot group sends the first photo WITH the caption, "
                  "then the rest, so no image is lost",
                  len(fallback.files_sent) == wanted
                  and fallback.files_sent[0][1] == "deal text"
                  and all(cap is None for _, cap in fallback.files_sent[1:]), str(fallback.files_sent))

            one = RecClient()
            await bot.send_media_item(one, "entity", paths[:1], "deal text", refs[:1])
            check("one photo is sent as one photo, not as a one-item album",
                  one.calls == ["send_file"] and not hasattr(one, "last"), str(one.calls))
            parts = bot.outbound_parts("a" * 10, paths)
            check("an album is ONE resumable part, never six",
                  len(parts) == 1 and parts[0][0] == "file", str(parts))
            check("the cap never lets more photos out than the operator allowed",
                  wanted <= bot.MAX_ALBUM_PHOTOS, f"{wanted} vs {bot.MAX_ALBUM_PHOTOS}")
    _aio.run(drive())


def test_lists_are_shortened_with_our_bitly_and_stay_neat():
    """USER RULE: several links in one post (a list) go out as OUR short links, one per
    line; a tidy single Amazon link stays direct so the Bitly quota is never spent on it;
    and if Bitly is down or out of quota the deal still posts, never waits, never vanishes.
    """
    class NoSession:  # never used: the shortener itself is stubbed below
        pass

    aff = bot.AffiliateClient(NoSession())  # type: ignore[arg-type]
    calls: list[str] = []

    async def fake_bitly(url):
        calls.append(url)
        return "https://bit.ly/OURSHORT"

    aff.bitly = fake_bitly
    # The threshold itself is a knob, so the fixtures are BUILT from it: a link padded
    # past the current SHORTEN_MIN_LEN must be shortened, one under it must not. A test
    # that hard-codes 65 would cry wolf the moment an operator moves the knob (and the
    # last round's knob matrix showed exactly that class of false alarm in the bridge).
    min_len = int(bot.SHORTEN_MIN_LEN)
    base = "https://www.amazon.in/dp/B0TIF1"
    tail = "&ref_=" + "s" * max(0, min_len + 40 - len(base) - len("&ref_="))
    long_one = base + tail
    assert len(long_one) > min_len, (len(long_one), min_len)
    long_two = (f"1) Steel Tiffin \u20b9399 {long_one}\n"
                f"2) Casserole \u20b9499 {long_one.replace('B0TIF1', 'B0CASS2')}")
    out = asyncio.run(aff.shorten_long_urls_in_text(long_two))
    check("every long link in a list becomes our short link",
          out.count("https://bit.ly/OURSHORT") == 2 and "amazon.in/dp/B0TIF1" not in out, out)
    check("and each item keeps its own line, so the list reads neat",
          all(line.startswith(("1)", "2)")) for line in out.splitlines() if line.strip()), out)
    check("the rule is lists + long urls, not every post",
          bot.should_use_bitly("https://www.amazon.in/dp/B0X", True) is True
          and bot.should_use_bitly("https://www.amazon.in/dp/B0X", False) is False,
          str(bot.SHORTEN_MIN_LEN))
    tidy_link = "https://www.amazon.in/dp/B0AIR1"
    tidy = f"Boat Airdopes \u20b91,099 {tidy_link}"
    if len(tidy_link) > min_len:
        check("a single link OVER the threshold is shortened too",
              "bit.ly/OURSHORT" in asyncio.run(aff.shorten_long_urls_in_text(tidy)), tidy)
    else:
        check("a short single link stays direct (no quota spent)",
              asyncio.run(aff.shorten_long_urls_in_text(tidy)) == tidy, tidy)

    async def out_of_quota(url):
        raise RuntimeError("Bitly quota exhausted")

    aff.bitly = out_of_quota
    # Fresh URLs: a shortener that already answered for a link keeps that answer (that
    # is the cache doing its job), so the "Bitly is down" case needs unseen links.
    other = (f"1) Pressure Cooker \u20b9899 {'https://www.amazon.in/dp/B0COOK1' + tail}\n"
             f"2) Water Bottle \u20b9299 {'https://www.amazon.in/dp/B0BOTT2' + tail}")
    kept = asyncio.run(aff.shorten_long_urls_in_text(other))
    check("Bitly down: the deal still goes out with the tagged merchant links",
          "amazon.in/dp/B0COOK1" in kept and "bit.ly" not in kept, kept[:140])


def test_our_channel_link_sits_on_the_top_line_once():
    """The one thing of ours a reader sees above the deal (their ask), and nothing else."""
    body = "Lizol Floor Cleaner 900ml\n\u2705Deal Price: \u20b9 199\nhttps://www.amazon.in/dp/B0X"
    topped = bot.prepend_channel_header(body)
    first = topped.splitlines()[0]
    check("the top line is our family link", first.startswith("\U0001f449") and bot.OUR_FOLDER_LINK in first, first)
    check("the deal text below is untouched", topped.endswith(body), topped[:80])
    check("it is never added twice", bot.prepend_channel_header(topped) == topped)
    check("no other channel is linked",
          [u for u in re.findall(r"https?://\S+", topped) if "t.me" in u] == [bot.OUR_FOLDER_LINK], topped)
    # and the stored copy stays the source's: the header is a delivery-time line only
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    gate = src[src.index("def render_job"):]
    check("render_job never writes the header into the stored post",
          "prepend_channel_header" not in gate[:gate.index("async def process_job")], "header in render path")


def test_a_product_list_pairs_every_deal_with_its_own_link():
    """USER RULE (2026-09-05, "neatga ravali"): the source writes four deals and then
    dumps four links at the bottom. Read that way nobody can tell which link is which
    deal. Each block must carry its own link, in the source's order, with nothing
    added, dropped or reordered - and the price spelling is the SOURCE's ("at 258",
    "@167", "Rs 180"), not just the rupee sign the old ₹-only test demanded."""
    raw = ("Premium Afghani Anjeer 500 gms at 258\nBuy Max Quantity\n"
           "Premium Mix Dry Fruits 500 gms at 258\nBuy Max Quantity\n"
           "Methi Dana 500 gms at 180\nBuy Max Quantity\n"
           "Yellow mustard seeds 500gms at 220\nBuy Max Quantity\n"
           "https://fkrt.co/sYKjVp\nhttps://fkrt.co/41Tz5Y\n"
           "https://fkrt.co/4JArbD\nhttps://fkrt.co/Nv2fnb")
    out = bot.format_clustered_product_list(raw)
    blocks = [b.splitlines() for b in out.split("\n\n") if b.strip()]
    check("every deal became its own block", len(blocks) == 4, repr(out))
    check("each block ends with its OWN link, in source order",
          [b[-1] for b in blocks] == ["https://fkrt.co/sYKjVp", "https://fkrt.co/41Tz5Y",
                                      "https://fkrt.co/4JArbD", "https://fkrt.co/Nv2fnb"], repr(out))
    check("each block keeps its product name and price",
          all(any(x in b[0] for x in ("Anjeer", "Mix Dry Fruits", "Methi", "mustard")) for b in blocks),
          repr(out))
    check("the source's own note line travels with its deal",
          all("Buy Max Quantity" in b for b in blocks), repr(out))
    check("no link and no line was lost",
          all(u in out for u in ("sYKjVp", "41Tz5Y", "4JArbD", "Nv2fnb"))
          and out.count("Buy Max Quantity") == 4, repr(out))
    check("a post that is not a bottom-dumped list is left alone",
          bot.format_clustered_product_list("One deal \u20b9199\nhttps://a.in/x")
          == "One deal \u20b9199\nhttps://a.in/x")


def test_the_shopping_channel_copy_is_programme_safe():
    """USER RULE (2026-09-05): t.me/smartbuyhub11 is under Amazon Associates review, so
    it gets the SAME deal with only the product name, the price the source printed and
    our link - no loot/grab banner, no "Buy Max Quantity", no unverifiable claim, no
    discount percentage. Nothing is invented: every word left is the source's own."""
    loot = ("Loot : Nutriburst Collagen powder 299 (70% off)\nReg 599\n#Uk no 1 brand\n"
            "Buy Max Quantity\nHurry up!\nhttps://www.amazon.in/dp/B0X")
    safe = bot.affiliate_safe_text(loot)
    check("the product name and its price survive",
          "Nutriburst Collagen powder" in safe and "299" in safe, repr(safe))
    check("our link survives", "https://www.amazon.in/dp/B0X" in safe, repr(safe))
    for banned in ("Loot", "Buy Max Quantity", "Hurry", "70%", "no 1 brand", "Reg 599"):
        check(f"the reviewer never sees {banned!r}", banned.lower() not in safe.lower(), repr(safe))
    check("no number the source never printed appears",
          all(n in loot for n in re.findall(r"\d+", safe)), repr(safe))
    grab = "GRAB : Gym / Motivational Bottle, 2000ml @167.\nhttps://fkrt.co/x"
    safe2 = bot.affiliate_safe_text(grab)
    check("a GRAB banner becomes a plain product line",
          safe2.splitlines()[0].startswith("Gym / Motivational Bottle") and "167" in safe2, repr(safe2))
    check("the safe rewrite is idempotent", bot.affiliate_safe_text(safe2) == safe2, repr(safe2))
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    check("the safe copy is a DELIVERY-time rewrite, never the stored post",
          "affiliate_safe_text" not in src[src.index("def render_job"):src.index("async def process_job")],
          "safe rewrite leaked into render_job")
    check("the shopping channel is not in the card-offer fan-out list",
          bot.SHOPPING_TARGET not in bot.ALL_OWNED_TARGETS, str(bot.ALL_OWNED_TARGETS))

    # The review channel must never carry OUR OWN promo. A card offer has the
    # folder link appended into the stored copy, and ADD_OUR_CHANNEL_LINK_TOP
    # prepends a "All Loot Channels" header on the way out - a reviewer who taps
    # either one lands in a channel called "Loot Zone", which is exactly what
    # this channel exists to avoid.
    carded = bot.append_folder_link("HDFC Bank Card Offer 10% off\nhttps://www.amazon.in/dp/B0Y")
    safe_card = bot.affiliate_safe_text(carded)
    check("our folder link never reaches the reviewer",
          bot.OUR_FOLDER_LINK not in safe_card and "t.me" not in safe_card, repr(safe_card))
    check("but the card deal itself still arrives",
          "HDFC" in safe_card and "https://www.amazon.in/dp/B0Y" in safe_card, repr(safe_card))
    banner = "\U0001f525 MEGA LOOT \U0001f525\nJoin our channel https://t.me/LootZoneIndia11\nSocks at 99\nhttps://fkrt.co/s"
    safe_banner = bot.affiliate_safe_text(banner)
    check("a decorated pure-hype banner line is dropped whole",
          "loot" not in safe_banner.lower() and "mega" not in safe_banner.lower(), repr(safe_banner))
    check("and a foreign channel invite never survives",
          "t.me" not in safe_banner, repr(safe_banner))
    check("the actual product still goes out",
          "Socks at 99" in safe_banner and "https://fkrt.co/s" in safe_banner, repr(safe_banner))
    src_top = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    deliver_block = src_top[src_top.index("async def process_job"):]
    check("the family header is explicitly skipped for the review channel",
          "and target != SHOPPING_TARGET" in deliver_block, "header not gated")



def test_a_deal_with_no_working_link_is_not_posted():
    """USER RULE (2026-09-05, "asalu link yeh ledu"): a deal a reader cannot click is
    useless and earns nothing, so when every link the source wrote is dead the post is
    skipped. The photo-post rule above is untouched: a source post that never HAD a
    link but shows the product still goes out."""
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    check("the default is 'a post needs a link'", bot.REQUIRE_LINK_IN_POST is True)
    gate = src[src.index("if not URL_RE.search(rendered):", src.index("def render_job")):]
    check("a dead-link-only post raises a skip",
          "nothing buyable to post" in gate[:2600], gate[:400])
    check("a link-free PHOTO post is still published",
          "publishing it exactly as posted" in gate[:2600], gate[:400])


def test_the_same_deal_under_two_banner_words_is_one_post():
    """USER RULE (2026-09-05): the sources publish one product twice under two
    different banner words - "Loot : X 299 (70% off)" and "Grab : X @ 299 70% off",
    "Grab \u20b9298 (10KG Detergent Powder)" and "Loot : 10KG Detergent Powder @298".
    The banner used to be read as the BRAND and the price as a MODEL NUMBER, so the
    two copies signed differently and both went out. They are one product now."""
    link = "\nhttps://fkrt.co/z"
    same = [
        ("Loot : Nutriburst Collagen powder 299 (70% off)",
         "Grab : Nutriburst Collagen powder @ 299 70% off"),
        ("Grab \u20b9298 (10KG Detergent Powder)", "Loot : 10KG Detergent Powder @298"),
        ("Deal : Prestige Pressure Cooker 5L at 1499", "Prestige Pressure Cooker 5L Rs 1499"),
    ]
    for first, second in same:
        sig_a = bot.product_signature(first + link)
        sig_b = bot.product_signature(second + link)
        check(f"one product, one signature: {first[:34]!r}",
              bool(sig_a) and sig_a == sig_b, f"{sig_a} vs {sig_b}")
    # The other direction matters more: a signature that matches too eagerly SKIPS a
    # real deal. Two genuinely different products must never collapse together.
    differ = [
        ("boAt Airdopes 141 at 999", "boAt Airdopes 131 at 999"),
        ("Samsung Galaxy S23 128GB at 45999", "Samsung Galaxy S23 256GB at 45999"),
        ("Prestige 5 Burner Gas Stove at 4999", "Prestige 3 Burner Gas Stove at 4999"),
        ("Nutriburst Collagen powder at 299", "Nutriburst Biotin powder at 299"),
        ("Nivea Men Face Wash 100ml at 199", "Nivea Women Face Wash 100ml at 199"),
    ]
    for first, second in differ:
        sig_a = bot.product_signature(first + link)
        sig_b = bot.product_signature(second + link)
        check(f"two products stay two: {first[:32]!r}", sig_a != sig_b, f"{sig_a} vs {sig_b}")
    check("a bare category phrase still gets no signature at all",
          bot.product_signature("Nice offer today" + link) is None)


def test_a_flipkart_product_link_goes_out_short():
    """USER RULE (2026-09-05, "shortga ravali ga"): a dl.flipkart.com product link
    arrives ~177 characters (lid / marketplace / srno session noise). It is compacted
    natively to slug + item id + pid - free, no shortener quota - so the post is neat
    even when Bitly is rate-limited. pid is the product identity and is always kept."""
    long_fk = ("https://dl.flipkart.com/dl/bellerbird-motivational-bottle-2000ml-black-"
               "2000-ml-plastic/p/itm0613a6a4f24de?lid=LSTBOTHEXH9ZG8XAJTNABEVPU"
               "&marketplace=FLIPKART&pid=BOTHEXH9ZG8XAJTN")
    compact = bot.compact_flipkart_product_link(long_fk)
    check("the link got materially shorter", len(compact) < len(long_fk) - 40,
          f"{len(long_fk)} -> {len(compact)}")
    check("it still points at the same item and product",
          "itm0613a6a4f24de" in compact and "pid=BOTHEXH9ZG8XAJTN" in compact, compact)
    check("the session/tracking noise is gone",
          "lid=" not in compact and "marketplace=" not in compact, compact)
    check("the app-redirect wrapper became the real product page",
          compact.startswith("https://www.flipkart.com/") and "/dl/" not in compact, compact)

    class NoSession:
        pass

    aff = bot.AffiliateClient(NoSession())  # type: ignore[arg-type]

    async def no_shortener(url):
        return None

    aff.bitly = no_shortener
    aff.shorten = no_shortener
    out = asyncio.run(aff.shorten_long_urls_in_text("GRAB : Bottle @167\n" + long_fk))
    check("even with the shortener DOWN the posted link is short",
          max(len(u) for u in bot.URL_RE.findall(out)) < 140, out)
    check("and the deal text is untouched", "GRAB : Bottle @167" in out, out)
    search = "https://www.flipkart.com/search?q=bottle&sid=abc&otracker=xyz"
    check("a search/category link is left for the shortener, not mangled",
          bot.compact_flipkart_product_link(search) == bot.clean_url(search),
          bot.compact_flipkart_product_link(search))


def test_our_amazon_tag_only_rides_on_declared_channels():
    """Amazon's Operating Agreement: every channel carrying your Associates links must
    be DECLARED in Associates Central, and links on undeclared channels can close the
    account. So the tag is restricted to AMAZON_TAG_TARGETS (the review channel by
    default). Every other channel still posts the SAME deal - just untagged, so a
    working deal is never lost and the account is never exposed."""
    old_tag = bot.OUR_TAG
    bot.OUR_TAG = "mytag-21"
    try:
        text = "Socks at 99\nhttps://www.amazon.in/dp/B0X?tag=mytag-21"
        kept = bot.strip_amazon_tag_for_undeclared(text, bot.SHOPPING_TARGET)
        check("the declared review channel keeps our tag (that is where we earn)",
              "tag=mytag-21" in kept, kept)
        for undeclared in ("LootZoneIndia11", "SecretLootIndia1", "Under99Deals11"):
            out = bot.strip_amazon_tag_for_undeclared(text, undeclared)
            check(f"@{undeclared} posts the deal WITHOUT our tag",
                  "tag=" not in out and "amazon.in/dp/B0X" in out, out)
            check(f"@{undeclared} still gets the product and the price",
                  "Socks at 99" in out, out)
        foreign = "Deal\nhttps://www.amazon.in/dp/B0Y?tag=thief-21"
        check("a FOREIGN tag is not ours to protect and is left to the normal gates",
              bot.strip_amazon_tag_for_undeclared(foreign, bot.SHOPPING_TARGET) == foreign)
        nonamazon = "Deal\nhttps://fkrt.co/x"
        check("non-Amazon links are never touched",
              bot.strip_amazon_tag_for_undeclared(nonamazon, "LootZoneIndia11") == nonamazon)
    finally:
        bot.OUR_TAG = old_tag
    check("with no tag configured the text is returned untouched",
          bot.strip_amazon_tag_for_undeclared("x https://www.amazon.in/dp/B0Z", "LootZoneIndia11")
          == "x https://www.amazon.in/dp/B0Z")
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    deliver = src[src.index("async def process_job"):]
    check("the strip runs on the delivery path, per target",
          "strip_amazon_tag_for_undeclared(target_text, target" in deliver, "not wired in")


def test_the_review_channel_copy_is_built_from_an_allowlist():
    """USER RULE (2026-09-05): the new channel is under EarnKaro/Amazon review, so it
    must follow the programme rules 100% - the other channels are untouched. A
    blocklist is the wrong tool (the one hype word nobody listed is the one the
    reviewer sees), so the review copy is REBUILT from an allowlist: product line +
    price the source printed + link. Nothing else survives, nothing is invented."""
    cases = [
        ("\U0001f525\U0001f525 PRICE ERROR LOOT \U0001f525\U0001f525\nBoat Airdopes 141 at 899\n"
         "Order fast before it ends!!\nhttps://fkrt.co/a",
         ["Boat Airdopes 141 at 899", "https://fkrt.co/a"]),
        ("Dhamaka offer!! Jackpot deal\nPrestige Cooker 5L at 1499\nBuy Max Quantity\n"
         "https://fkrt.co/c", ["Prestige Cooker 5L at 1499", "https://fkrt.co/c"]),
        ("Samsung Galaxy M14 5G at 9999\nMRP 16999\nSave 41%\nhttps://www.amazon.in/dp/B0M14",
         ["Samsung Galaxy M14 5G at 9999", "https://www.amazon.in/dp/B0M14"]),
    ]
    for raw, expected in cases:
        out = bot.affiliate_safe_text(raw)
        check(f"only product+link survive: {expected[0][:30]!r}",
              out.splitlines() == expected, repr(out))
    banned = ("loot", "price error", "dhamaka", "jackpot", "order fast",
              "buy max quantity", "mrp", "41%", "\U0001f525")
    joined = " ".join(bot.affiliate_safe_text(raw) for raw, _ in cases).lower()
    for word in banned:
        check(f"the reviewer never sees {word!r}", word not in joined, joined[:120])
    check("the price stays EXACTLY as the source printed it",
          "at 899" in bot.affiliate_safe_text(cases[0][0]), "price reformatted")

    # A post that is only a trick/glitch/cashback line has NO product to review.
    # After the rebuild nothing but a link is left, and the delivery guard must
    # refuse it for this channel (the other channels still get it as written).
    def would_post(text: str) -> bool:
        safe = bot.affiliate_safe_text(text)
        return bool(safe and bot.URL_RE.search(safe)
                    and re.search(r"[A-Za-z]{3}", bot.URL_RE.sub(" ", safe)))

    check("a cashback TRICK post never reaches the review channel",
          not would_post("Cashback trick: pay via CRED get 200 off\nhttps://fkrt.co/b"))
    check("a 'free money glitch' post never reaches it either",
          not would_post("Free money glitch\nhttps://fkrt.co/d"))
    check("but a real product deal does",
          would_post("Boat Airdopes 141 at 899\nhttps://fkrt.co/a"))
    check("the strict rebuild is idempotent",
          bot.affiliate_safe_text(bot.affiliate_safe_text(cases[0][0]))
          == bot.affiliate_safe_text(cases[0][0]))
    check("and it is ON by default", bot.SAFE_STRICT_REBUILD is True)
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    render = src[src.index("def render_job"):src.index("async def process_job")]
    check("the strict copy is delivery-only: other channels keep the source's post",
          "affiliate_safe_text" not in render and "_strict_review_copy" not in render,
          "review rewrite leaked into the shared render path")


def test_review_channel_posts_are_disclosed_amazon_only_and_capped():
    """USER RULE (2026-09-05): "daily 20-30 posts, Amazon vi veyu, reject cheyakunda".
    Three programme rules the review channel must satisfy on EVERY post."""
    deal = "Boat Airdopes 141 at 899\nhttps://www.amazon.in/dp/B0X"

    # 1. Link-level disclosure. Amazon/FTC require it NEXT TO the link on every
    # post - the channel bio alone is not enough, and missing it is one of the
    # most common rejection reasons.
    disclosed = bot.add_link_disclosure(bot.affiliate_safe_text(deal))
    check("every post carries a link-level disclosure",
          "#ad" in disclosed or "paid link" in disclosed.lower(), repr(disclosed))
    check("the product and price are still there",
          "Boat Airdopes 141 at 899" in disclosed, repr(disclosed))
    check("the disclosure is never doubled",
          bot.add_link_disclosure(disclosed) == disclosed, repr(disclosed))
    check("a post that already discloses is left alone",
          bot.add_link_disclosure("X at 99 #ad\nhttps://www.amazon.in/dp/B0Y").count("#ad") == 1)

    # 2. Amazon only - the channel is submitted for the AMAZON programme.
    check("an Amazon-only deal qualifies", bot.is_amazon_only_post(deal) is True)
    check("a Flipkart deal does not",
          bot.is_amazon_only_post("X at 99\nhttps://fkrt.co/a") is False)
    check("a MIXED post does not either (an off-programme link would ride along)",
          bot.is_amazon_only_post(deal + "\nhttps://fkrt.co/a") is False)
    check("a post with no link at all does not qualify",
          bot.is_amazon_only_post("Just a note") is False)
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    deliver = src[src.index("async def process_job"):]
    check("the Amazon-only gate is enforced at delivery, not just at routing",
          "not is_amazon_only_post(safe_text)" in deliver, "gate missing")
    check("the disclosure is added at delivery, after every cleaning pass",
          "add_link_disclosure(safe_text)" in deliver, "disclosure not wired")

    # 3. A daily cap, so the channel reads as a curated shop and not a firehose.
    async def run_quota():
        with tempfile.TemporaryDirectory() as td:
            store = bot.Store(Path(td) / "quota.sqlite3")
            old_cap, bot.SHOPPING_DAILY_CAP = bot.SHOPPING_DAILY_CAP, 3
            try:
                first = await store.shopping_quota_left()
                for _ in range(3):
                    await store.note_shopping_sent()
                after = await store.shopping_quota_left()
                # survives a restart: a fresh Store on the same file keeps the count
                reopened = bot.Store(Path(td) / "quota.sqlite3")
                return first, after, await reopened.shopping_quota_left()
            finally:
                bot.SHOPPING_DAILY_CAP = old_cap

    start, after, restarted = asyncio.run(run_quota())
    check("the day starts with the full allowance", start == 3, str(start))
    check("the cap stops further posts once it is reached", after == 0, str(after))
    check("and the count survives a restart (no reset-to-zero loophole)",
          restarted == 0, str(restarted))
    check("the shipped default matches the user's 40-50/day", bot.SHOPPING_DAILY_CAP == 50,
          str(bot.SHOPPING_DAILY_CAP))
    check("the cap is enforced on the delivery path",
          "shopping_quota_left()" in deliver, "cap not wired")


def test_the_review_channel_carries_no_amazon_marks_photos_or_channel_pointers():
    """REJECTION 2026-09-06 (deals0911-21), verbatim: "unapproved use of Amazon
    trademarked words, images (screenshots/screen recordings), or reviews (which
    may include variations or misspellings)", with t.me/LootZoneIndia11 given as
    the worked example. Each of those three is now closed off in code."""
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    deliver = src[src.index("async def process_job"):]

    # 1. The MARK must not appear in the copy. Linking to the store is fine.
    for named in ("Amazon Loot Deal\nBoat Airdopes 141 at 899",
                  "Amzn Prime Day sale\nSamsung M14 at 9999",
                  "Great Indian Festival\nSony headphones at 1999",
                  "AmazonBasics cable at 199",
                  "Alexa Echo Dot at 3499"):
        body = named + "\nhttps://www.amazon.in/dp/B0X"
        safe = bot.affiliate_safe_text(body)
        check("the mark is gone from the review copy of %r" % named.splitlines()[0],
              not bot.has_amazon_trademark(safe), repr(safe))
    check("a bare amazon.in LINK is not treated as a trademark use",
          bot.has_amazon_trademark("Boat 141 at 899\nhttps://www.amazon.in/dp/B0X") is False)
    check("misspellings and variations are caught too",
          all(bot.has_amazon_trademark(w) for w in ("amazn deal", "AMAZON sale", "amzn offer")))
    check("an ordinary product name is not a false positive",
          bot.has_amazon_trademark("Boat Airdopes 141 at 899") is False)
    check("the trademark gate runs on the delivery path",
          "has_amazon_trademark(safe_text)" in deliver, "gate not wired")

    # 2. No route from the review channel to a loot channel - the email's example.
    body = "Nutriburst Collagen powder 299\nJoin @LootZoneIndia11\nhttps://www.amazon.in/dp/B0Y"
    safe = bot.affiliate_safe_text(body)
    check("the loot-channel pointer is stripped",
          not bot.has_telegram_pointer(safe), repr(safe))
    check("but the product and price survive",
          "Nutriburst Collagen powder 299" in safe, repr(safe))
    check("a t.me url counts as a pointer",
          bot.has_telegram_pointer("see https://t.me/LootZoneIndia11") is True)
    check("the pointer gate runs on the delivery path",
          "has_telegram_pointer(safe_text)" in deliver, "gate not wired")

    # 3. No images: a forwarded deal photo is nearly always a store screenshot.
    check("the review channel is text-only by default", bot.SHOPPING_TEXT_ONLY is True)
    check("media is dropped for the review channel only",
          "target_media = []" in deliver and "target == SHOPPING_TARGET and SHOPPING_TEXT_ONLY" in deliver,
          "text-only not wired")
    check("every other channel still gets its media",
          "target_media = media_path" in deliver, "media dropped globally")

    # USER RULE (2026-09-06): "daily oka 40-50 cheyali e okka channelo lo".
    check("the daily cap now allows 40-50 posts", bot.SHOPPING_DAILY_CAP == 50,
          str(bot.SHOPPING_DAILY_CAP))


def test_a_roundup_list_cannot_be_posted_twice_from_two_sources():
    """USER RULE (2026-09-06): "anni vere channels lo anni multiple times
    rakudna and duplicates lekunda".

    A single-product post has always had a repeat guard. A ROUNDUP (3+ merchant
    links) had NO identity at all, so the same list arriving again from a second
    source - different shortener, different banner, items in a different order -
    was invisible to the guard and the channel carried it twice.
    """
    first = ("Deal of the day\nBoat Airdopes 141 at 899\nhttps://amzn.to/a\n"
             "Samsung M14 5G at 9999\nhttps://amzn.to/b\n"
             "Noise Buds VS104 at 799\nhttps://amzn.to/c")
    # Same three products: other source, other shortener, other banner, shuffled.
    repeat = ("LOOT!!\nSamsung M14 5G at 9999\nhttps://fkrt.co/y\n"
              "Noise Buds VS104 at 799\nhttps://fkrt.co/z\n"
              "Boat Airdopes 141 at 899\nhttps://fkrt.co/x")
    bigger = first + "\nRedmi Note 13 at 14999\nhttps://amzn.to/d"

    sig_first = bot.product_signature(first)
    check("a roundup now HAS an identity (it used to be None)", bool(sig_first), repr(sig_first))
    check("the same list from another source matches it",
          sig_first == bot.product_signature(repeat),
          "%r vs %r" % (sig_first, bot.product_signature(repeat)))
    check("a list with an extra product is NOT the same post (coverage kept)",
          sig_first != bot.product_signature(bigger))
    check("single-product posts keep their own identity scheme",
          str(bot.product_signature("Boat Airdopes 141 at 899\nhttps://amzn.to/a")).startswith("SIG:"))
    check("a one-item 'list' is too thin to key on",
          bot._roundup_signature("Boat Airdopes 141 at 899\nhttps://amzn.to/a") is None)
    check("a post with no products at all gets no list key",
          bot._roundup_signature("Deal of the day\nhttps://amzn.to/a") is None)

    # The guard the signature feeds is per channel and still lets a cheaper
    # repeat through, so a genuine price drop is never swallowed.
    async def run():
        with tempfile.TemporaryDirectory() as td:
            store = bot.Store(Path(td) / "dup.sqlite3")
            await store.mark_product_posted("LootZoneIndia11", sig_first, 899, 40)
            same, _ = await store.product_already_posted("LootZoneIndia11", sig_first, 899, 40)
            other, _ = await store.product_already_posted("Under99Deals11", sig_first, 899, 40)
            cheaper, _ = await store.product_already_posted("LootZoneIndia11", sig_first, 799, 40)
            return same, other, cheaper

    same, other, cheaper = asyncio.run(run())
    check("the repeat is skipped on the channel that already carried it", same is True)
    check("but a channel that has NOT carried it still gets it", other is False)
    check("and a cheaper repeat is still allowed through", cheaper is False)


def test_our_new_associates_tag_and_the_self_source_loop():
    """USER RULE (2026-09-06): "kothaga thiskunna mama086-21 idi manade" and
    "nenu source ga ana kotha channel link ichanu"."""
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")

    # 1. The tag we now own is usable again - it had been pinned empty because
    # the only tag ever seen (deals0911-21) belonged to a SOURCE, not to us.
    check("mama086-21 is registered as a tag we own",
          "mama086-21" in bot.OUR_AMAZON_TAGS, str(bot.OUR_AMAZON_TAGS))
    check("the tag is read from the environment, not pinned empty",
          'os.getenv("AMAZON_TAG"' in src, "AMAZON_TAG no longer read")
    check("a tag that is NOT ours can never be used",
          "deals0911-21" not in {t.lower() for t in bot.OUR_AMAZON_TAGS})

    # 2. It may ride ONLY on the channel declared to Amazon.
    check("the declared-channel allowlist is still just the review channel",
          bot.AMAZON_TAG_TARGETS == {bot.SHOPPING_TARGET}, str(bot.AMAZON_TAG_TARGETS))
    if bot.OUR_TAG:
        tagged = "Boat 141 at 899\nhttps://www.amazon.in/dp/B0X?tag=" + bot.OUR_TAG
        check("the tag survives on the declared channel",
              bot.OUR_TAG in bot.strip_amazon_tag_for_undeclared(tagged, bot.SHOPPING_TARGET))
        check("the tag is stripped everywhere else",
              bot.OUR_TAG not in bot.strip_amazon_tag_for_undeclared(tagged, "LootZoneIndia11"))

    # 3. A channel we publish to must never be read back as a source, or the bot
    # republishes its own posts forever.
    for own in ("smartbuyhub11", "@smartbuyhub11", "LootZoneIndia11",
                "Under99Deals11", "Premiumlootsdeals"):
        check("our own channel %r is refused as a source" % own,
              bot.is_own_channel_source(own) is True)
    for real in ("pricehistory", "under_99_loot_deals", "TrickXpert", ""):
        check("a real source %r is still accepted" % real,
              bot.is_own_channel_source(real) is False)
    deliver = src[src.index("async def process_job"):]
    check("the self-source loop is broken before anything is posted",
          "is_own_channel_source(row[\"source\"])" in deliver, "guard not wired")

    # 4. The user asked for 30-50 posts a day on the new channel.
    check("the daily cap covers the 30-50 the user asked for",
          30 <= bot.SHOPPING_DAILY_CAP <= 50, str(bot.SHOPPING_DAILY_CAP))


def test_the_review_copy_is_clean_on_real_source_shapes():
    """USER RULE (2026-09-06): "anni advanced gaa ... approve vachelanga post
    cheyu". Run the REAL shapes our sources actually send and require the
    finished post to be exactly: product, price, link, disclosure."""
    import re as _re

    def finished(raw):
        safe = bot.affiliate_safe_text(raw)
        if (not safe or not bot.URL_RE.search(safe)
                or not _re.search(r"[A-Za-z]{3}", bot.URL_RE.sub(" ", safe))
                or bot.has_amazon_trademark(safe) or bot.has_telegram_pointer(safe)):
            return None                      # withheld from the review channel
        return bot.add_link_disclosure(safe)

    hype = ("\U0001f525 LOOT \U0001f525\nboAt Rockerz 255 Pro+ Neckband\n"
            "\U0001f4b0 Price - \u20b9899 (MRP \u20b92990)\n70% OFF \u2705\n"
            "https://amzn.to/aaa\n@LootZoneIndia11")
    out = finished(hype)
    check("the product line survives", "boAt Rockerz 255 Pro+ Neckband" in out, repr(out))
    check("the DEAL price is salvaged off the MRP line", "899" in out, repr(out))
    check("the MRP anchor never ships", "2990" not in out, repr(out))
    check("the percent-off claim never ships", "70%" not in out, repr(out))
    check("the loot pointer never ships", "LootZone" not in out, repr(out))

    # "or reviews" is named in the rejection - ratings are Amazon-owned content.
    rated = ("SAMSUNG Galaxy M14 5G (128 GB)\nDeal Price: Rs.9999\n"
             "Rating 4.2 \u2b50 | 12,453 reviews\nhttps://amzn.to/ddd")
    out = finished(rated)
    check("the star rating never ships", "4.2" not in out, repr(out))
    check("the review count never ships",
          "review" not in out.lower() and "12,453" not in out, repr(out))
    check("the price still ships", "9999" in out, repr(out))

    # A price written on its own line, and a coupon condition.
    out = finished("*Nutriburst Collagen Powder 200g*\n@ 299/-\nBuy now \U0001f449 https://amzn.to/bbb")
    check("a lone '@ 299/-' line is kept as the price", "299" in out, repr(out))
    check("the call-to-action never ships", "Buy now" not in out, repr(out))

    out = finished("\u26a1 Apply 10% coupon \u26a1\nPigeon Kettle 1.5L at 549\nhttps://amzn.to/eee")
    check("the coupon condition never ships", "coupon" not in out.lower(), repr(out))
    check("a price already in the product line is NOT repeated",
          out.count("549") == 1, repr(out))

    # Every finished post has the four required parts and nothing else.
    for raw in (hype, rated, "Milton Bottle 1L - 89\nhttps://amzn.to/c1"):
        out = finished(raw)
        check("the post ends with the disclosure", out.rstrip().endswith("#ad (paid link)"), repr(out))
        check("the post has exactly one link", len(bot.URL_RE.findall(out)) == 1, repr(out))
        check("no line is repeated in the post",
              len(out.splitlines()) == len(dict.fromkeys(out.splitlines())), repr(out))

    # Naming the marketplace withholds the post from THIS channel only.
    check("a post naming the marketplace is withheld",
          finished("Amazon Great Indian Festival deal\nhttps://amzn.to/f") is None)
    check("a trick post is withheld", finished("Cashback trick\nhttps://amzn.to/x") is None)
    check("but the product under a marketplace banner still ships",
          "Sony WH-1000XM4" in (finished(
              "Amazon Great Indian Festival\nSony WH-1000XM4\n\u20b919990\nhttps://amzn.to/fff") or ""))


def test_native_links_on_review_channel_and_the_tag_switch():
    """USER RULE (2026-09-06): "review channelo shorten ga marchatam bitly use
    cheyaku" and "tag ni anni channels lo use cheyu okavela approve vasthadi"."""
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    deliver = src[src.index("async def process_job"):]

    # 1. NO SHORTENER on the reviewed channel. A bit.ly hop hides the
    # destination; the reviewer must see the store domain in the post itself.
    class FakeAffiliate:
        def __init__(self):
            self._short_to_long = {}
        expand_our_short_links = bot.AffiliateClient.expand_our_short_links

    affiliate = FakeAffiliate()
    native = "https://www.amazon.in/dp/B0FPDD9WKP?tag=mama086-21"
    affiliate._short_to_long["https://bit.ly/3xYz"] = native
    post = "boAt Rockerz 255\nPrice: \u20b9899\nhttps://bit.ly/3xYz"
    expanded = affiliate.expand_our_short_links(post)
    check("our short link is swapped back to the native store URL",
          native in expanded and "bit.ly" not in expanded, repr(expanded))
    check("the store domain is visible to the reviewer",
          "amazon.in" in expanded, repr(expanded))
    check("our tag survives the swap", "tag=mama086-21" in expanded, repr(expanded))
    check("a SOURCE's own short link is never rewritten (we cannot know it)",
          affiliate.expand_our_short_links("Deal\nhttps://fkrt.co/x") == "Deal\nhttps://fkrt.co/x")
    check("the native link is still short and neat", len(native) < 60, str(len(native)))
    check("native links are on by default", bot.SHOPPING_NATIVE_LINKS is True)
    check("the swap runs on the review path only",
          "affiliate.expand_our_short_links(target_text)" in deliver, "not wired")
    render = src[src.index("async def render_job"):src.index("async def process_job")]
    check("every other channel keeps its shortened link",
          "expand_our_short_links" not in render, "shared path was changed")

    # 2. The tag switch: safe while pending, everywhere once approved.
    def tag_targets_for(value):
        owned = {bot.SHOPPING_TARGET, *bot.ALL_OWNED_TARGETS}
        if value.lower() in ("all", "*"):
            return owned
        if value:
            return {t.strip().lstrip("@") for t in value.split(",") if t.strip()}
        return {bot.SHOPPING_TARGET}

    check("unset = the review channel only (safe while the application is pending)",
          tag_targets_for("") == {bot.SHOPPING_TARGET})
    every = tag_targets_for("all")
    check("'all' puts the tag on every owned channel once approved",
          bot.SHOPPING_TARGET in every and "LootZoneIndia11" in every and len(every) >= 8,
          str(sorted(every)))
    check("an explicit list is honoured exactly",
          tag_targets_for("smartbuyhub11,LootZoneIndia11") == {"smartbuyhub11", "LootZoneIndia11"})
    check("the 'all' switch is documented as approval-only",
          "AMAZON_TAG_TARGETS=all" in src and "AFTER" in src, "not documented")


def test_the_whatsapp_bridge_cannot_go_quiet():
    """USER RULE (2026-09-06): "whatsapp lo bestga post cheyali ... agipoindi
    ippudu alaga agipovaddu". Three ways the bridge used to stop for good."""
    bridge = (ROOT / "tg-wa-bridge" / "bridge.js").read_text(encoding="utf-8")

    # 1. A logout used to `return` in silence - the channel simply stopped and
    # the logs said nothing more, which is how it was found days later.
    check("a logout keeps reporting itself instead of going silent",
          "still logged out" in bridge, "silent logout")
    check("the logout message names the fix to run",
          "switch_whatsapp_number.sh" in bridge, "no remedy in the message")

    # 2. The watchdog only fired when deals were WAITING. A socket that died
    # while the queue was empty stayed dead, so the next deal hours later landed
    # on a broken connection.
    check("an idle socket is refreshed even with an empty queue",
          "readyCount === 0 && idleFor > 90" in bridge, "idle refresh missing")

    # 3. If the socket was never ready, nothing retried it at all.
    check("a cold socket is retried by the watchdog",
          "watchdog cold retry failed" in bridge, "cold retry missing")
    check("the cold retry is driven by the last successful connection",
          "lastConnectionOpenAt" in bridge, "no connection timestamp")

    # The identity probe is a pure function and must not need live credentials -
    # demanding them is why this contract silently never ran for many rounds.
    check("the identity probe runs without WhatsApp credentials",
          "IDENTITY_PROBE" in bridge and "if (IDENTITY_PROBE) return ''" in bridge,
          "probe still needs a provisioned .env")


def test_the_review_channel_posts_at_a_human_pace():
    """USER RULE (2026-09-06): "oka human laga daily oka 50 posts".

    50 posts fired in one burst at 3am is what a reviewer reads as an automated
    feed. The same 50 spread across the day reads as a person curating a shop.
    """
    check("human pacing is on by default", bot.SHOPPING_HUMAN_PACING is True)
    check("the active window is daytime, not 24h",
          0 <= bot.SHOPPING_ACTIVE_START < bot.SHOPPING_ACTIVE_END <= 24
          and (bot.SHOPPING_ACTIVE_END - bot.SHOPPING_ACTIVE_START) <= 16,
          "%s-%s" % (bot.SHOPPING_ACTIVE_START, bot.SHOPPING_ACTIVE_END))

    async def run():
        with tempfile.TemporaryDirectory() as td:
            store = bot.Store(Path(td) / "pace.sqlite3")
            first = await store.shopping_pace_wait()
            await store.note_shopping_sent()
            second = await store.shopping_pace_wait()
            left = await store.shopping_quota_left()
            # the gap must survive a restart, like the counter does
            reopened = bot.Store(Path(td) / "pace.sqlite3")
            after_restart = await reopened.shopping_pace_wait()
            return first, second, left, after_restart

    first, second, left, after_restart = asyncio.run(run())
    active_hours = bot.SHOPPING_ACTIVE_END - bot.SHOPPING_ACTIVE_START
    ideal_gap = active_hours * 3600 / max(1, bot.SHOPPING_DAILY_CAP)

    check("the first post of the day goes out immediately", first == 0.0, str(first))
    if bot.SHOPPING_ACTIVE_START <= bot.datetime.now(bot.IST).hour < bot.SHOPPING_ACTIVE_END:
        check("a second post is held back so the two are not a burst",
              second > 0, str(second))
        check("the gap is roughly the day's allowance spread over the window",
              0.5 * ideal_gap <= second <= 1.3 * ideal_gap,
              "gap %.0fs vs ideal %.0fs" % (second, ideal_gap))
        check("the pacing survives a restart (no burst after a deploy)",
              after_restart > 0, str(after_restart))
    check("the daily counter still moves", left == bot.SHOPPING_DAILY_CAP - 1, str(left))

    # A paced deal must be DEFERRED, never dropped - and only for this channel.
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    deliver = src[src.index("async def process_job"):]
    check("the pace gate runs on the review path", "shopping_pace_wait()" in deliver)
    paced = deliver[deliver.index("shopping_pace_wait()"):]
    paced = paced[:paced.index("continue") + len("continue")]
    check("a paced deal is left pending, not closed as delivered",
          "store.delivery(" not in paced, paced[-200:])
    render = src[src.index("async def render_job"):src.index("async def process_job")]
    check("no other channel is paced", "shopping_pace_wait" not in render)


def test_our_tag_is_live_now_on_the_review_channel():
    """USER RULE (2026-09-06): "approve ayyedaka wait cheyaku amazon associate
    tag vundi gaa danne use cheyu"."""
    if not bot.OUR_TAG:
        return
    compact = bot.compact_amazon_product_link(
        "https://www.amazon.in/dp/B0FPDD9WKP?psc=1&smid=X&ref_=abc")
    check("our tag is on the link right now, no waiting",
          "tag=" + bot.OUR_TAG in compact, compact)
    check("the noise params are gone", "psc" not in compact and "smid" not in compact, compact)
    check("the review channel keeps the tag",
          bot.OUR_TAG in bot.strip_amazon_tag_for_undeclared("x\n" + compact, bot.SHOPPING_TARGET))
    check("an UNDECLARED channel still posts the deal, just untagged",
          bot.OUR_TAG not in bot.strip_amazon_tag_for_undeclared("x\n" + compact, "LootZoneIndia11")
          and "B0FPDD9WKP" in bot.strip_amazon_tag_for_undeclared("x\n" + compact, "LootZoneIndia11"))


def test_our_tag_cannot_ride_into_an_undeclared_channel_inside_a_short_link():
    """THE ACTUAL REJECTION CAUSE, found by the user on 2026-09-06:

        "LootZoneIndia11 adi nenu source ga ichnau danni check chesanu so andulo
         anni photos antha anni tags tho post ayyayi so reject chesaru"

    Links are SHORTENED in render_job, BEFORE the tag-strip runs at delivery. So
    the strip saw "https://bit.ly/3xYz" - no amazon.in hostname to match, nothing
    stripped - and our Associates tag rode into an undeclared loot channel inside
    the redirect. Amazon reads the referrer on every click, which is exactly how
    the application was failed.
    """
    if not bot.OUR_TAG:
        return

    class FakeAffiliate:
        _short_to_long = {
            "https://bit.ly/3xYz": "https://www.amazon.in/dp/B0FPDD9WKP?tag=" + bot.OUR_TAG,
            "https://bit.ly/flip": "https://www.flipkart.com/x/p/itm1",
        }

    affiliate = FakeAffiliate()
    post = "boAt 141\nhttps://bit.ly/3xYz"

    loot = bot.strip_amazon_tag_for_undeclared(post, "LootZoneIndia11", affiliate)
    check("the tag no longer hides behind a shortener on a loot channel",
          bot.OUR_TAG not in loot and "bit.ly" not in loot, repr(loot))
    check("the loot channel still gets a working product link",
          "B0FPDD9WKP" in loot and "amazon.in" in loot, repr(loot))

    review = bot.strip_amazon_tag_for_undeclared(post, bot.SHOPPING_TARGET, affiliate)
    check("the DECLARED channel is untouched", review == post, repr(review))

    check("a non-Amazon short link of ours is left alone",
          "bit.ly/flip" in bot.strip_amazon_tag_for_undeclared(
              "x\nhttps://bit.ly/flip", "LootZoneIndia11", affiliate))
    check("a SOURCE's own short link is never rewritten",
          "fkrt.co/own" in bot.strip_amazon_tag_for_undeclared(
              "x\nhttps://fkrt.co/own", "LootZoneIndia11", affiliate))

    # A plainly visible tagged URL must still be stripped (the original path).
    visible = "boAt 141\nhttps://www.amazon.in/dp/B0X?tag=" + bot.OUR_TAG
    check("a visible tagged link is still stripped for a loot channel",
          bot.OUR_TAG not in bot.strip_amazon_tag_for_undeclared(
              visible, "LootZoneIndia11", affiliate))

    # The strip must be wired WITH the affiliate client, or pass 1 is dead code.
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    deliver = src[src.index("async def process_job"):]
    check("delivery passes the affiliate client so short links can be resolved",
          "strip_amazon_tag_for_undeclared(target_text, target, affiliate)" in deliver,
          "affiliate not passed - the shortener hole is still open")

    # The other half of the user's finding: the photos.
    check("the review channel sends no images at all", bot.SHOPPING_TEXT_ONLY is True)
    check("media is dropped for the review channel only",
          "target == SHOPPING_TARGET and SHOPPING_TEXT_ONLY" in deliver
          and "target_media = media_path" in deliver, "text-only not wired")


def test_the_four_defects_the_user_photographed():
    """USER REPORT (2026-09-06), four separate defects on the LOOT channels."""
    import re as _re

    # 1. "same products double times in same groups" - the Ergonomic Dustpan
    # went out twice. Two-word product names had NO identity (the floor was
    # three words), so the per-channel repeat guard never ran on them at all.
    post = "\U0001f525 Ergonomic Dustpan @ \u20b955\nhttps://www.amazon.in/dp/B0C8JPD1KL"
    sig = bot.product_signature(post)
    check("a two-word product now has an identity", bool(sig), repr(sig))
    check("the repeat of the very same post keys the same",
          sig == bot.product_signature(post))
    check("a different product does not collide",
          sig != bot.product_signature("\U0001f525 Ergonomic Mop @ \u20b955\nhttps://www.amazon.in/dp/B0X"))
    for phrase in ("hair oil", "phone case", "Milton Bottle"):
        check("a short category phrase %r is still NOT keyed" % phrase,
              bot._product_identity(phrase) is None)

    # 2. "ilaga link pamplkudnaa just names vasthunndi" - a priced LIST with no
    # link at all was published. A reader cannot buy any of it.
    listing = ("Cello Feast Deluxe Kids Lunch Box @264.\n\nApply 31% Off Coupon\n\n"
               "2 Containers Lunch Box, 1024 ml @517.\n\nApply 30% Off Coupon")
    priced = [ln for ln in listing.splitlines()
              if bot.parse_price(ln) is not None and _re.search(r"[A-Za-z]{3}", ln)]
    check("the offending post is recognised as a multi-product list",
          len(priced) >= 2, str(priced))
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    check("a link-free MULTI-product list can no longer be published",
          "len(_priced_lines) < 2" in src, "the link-free list hole is still open")
    check("a single link-free product (photo deal) is still allowed",
          "_priced_lines" in src and "has_media_now" in src)

    # 3. "flipkart earnkaro tho change cheyatledu shortenga" - a 112-char link
    # went out whole because its path said /p/item, not /p/itm<id>.
    long_fk = ("https://www.flipkart.com/flipkart/p/item?lid=LSTSOPH2D7GGSYYMVFPST5QWO"
               "&marketplace=FLIPKART&pid=SOPH2D7GGSYYMVFP")
    compact = bot.compact_flipkart_product_link(long_fk)
    check("the /p/item spelling is now compacted",
          len(compact) < len(bot.clean_url(long_fk)), "%s -> %s" % (len(long_fk), len(compact)))
    check("the product identity (pid) is kept", "SOPH2D7GGSYYMVFP" in compact, compact)
    check("the tracking noise is gone",
          "lid=" not in compact and "marketplace=" not in compact, compact)
    check("the usual /p/itm spelling still works",
          bot.compact_flipkart_product_link(
              "https://www.flipkart.com/soap/p/itmabc123?lid=X&pid=PID9").endswith("pid=PID9"))
    check("a search URL is still left for the shortener",
          bot.compact_flipkart_product_link("https://www.flipkart.com/search?q=soap")
          == "https://www.flipkart.com/search?q=soap")

    # 4. "shopsy ani kuda exted avuthundi alaga avoddu" - Telegram unfurled a
    # generic "Shopsy Store" card under a pen-stand deal, because an
    # unresolved bitli.in link passed the preview test.
    async def run_preview():
        with tempfile.TemporaryDirectory() as td:
            store = bot.Store(Path(td) / "prev.sqlite3")
            unresolved = await store.preview_allowed(
                "Decorative Boat Pen Stand at \u20b9261\nhttps://bitli.in/hlqmyQS")
            amazon = await store.preview_allowed(
                "Ergonomic Dustpan @ \u20b955\nhttps://www.amazon.in/dp/B0C8JPD1KL")
            return unresolved, amazon

    unresolved, amazon = asyncio.run(run_preview())
    check("an unresolved shortener gets NO preview card", unresolved is False)
    check("a real Amazon product link keeps its preview", amazon is True)


def test_only_the_review_channel_is_restricted():
    """USER RULE (2026-09-06), restated firmly: "mana top remaining channel
    gurinchi vatilo elanti issue lekunda cheyu ... only chala restricts for
    review channel only".

    Every programme rule written in rounds 5-15 must apply to SHOPPING_TARGET
    and to nothing else. This test is the guard that keeps it that way.
    """
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    render = src[src.index("async def render_job"):src.index("async def process_job")]

    # The strict machinery must not appear in the shared render path at all.
    for name in ("affiliate_safe_text", "_strict_review_copy", "add_link_disclosure",
                 "_STRICT_DROP_LINE_RE", "_salvage_price_only", "has_amazon_trademark",
                 "has_telegram_pointer", "shopping_pace_wait", "shopping_quota_left",
                 "SHOPPING_TEXT_ONLY", "SHOPPING_NATIVE_LINKS"):
        check("%s never runs on the shared path" % name, name not in render,
              "%s leaked into render_job - it would restrict every channel" % name)

    # A hype post must reach a loot channel completely untouched.
    hype = ("\U0001f525\U0001f525 LOOT DEAL \U0001f525\U0001f525\n\n"
            "boAt Rockerz 255 Pro+ Neckband\n\n"
            "\U0001f4b0 Price - \u20b9899 (MRP \u20b92990)\n"
            "70% OFF \u2705 Rating 4.2 \u2b50 | 12,453 reviews\n\n"
            "Apply 10% coupon | Hurry limited stock!\nAmazon Great Indian Festival\n\n"
            "https://amzn.to/aaa")
    strict = bot.affiliate_safe_text(hype)
    check("the review copy DID strip the hype", "LOOT" not in strict and "70%" not in strict)
    for keep in ("LOOT DEAL", "MRP", "70% OFF", "Rating 4.2", "12,453 reviews",
                 "Apply 10% coupon", "Hurry limited stock", "Amazon Great Indian Festival",
                 "\U0001f525"):
        check("a loot channel keeps %r" % keep, keep in hype,
              "the source text itself is the loot-channel copy")

    # The four round-15 bug fixes must be FIXES, not new restrictions.
    sig = bot.product_signature("\U0001f525 Ergonomic Dustpan @ \u20b955\nhttps://amzn.to/a")

    async def run():
        with tempfile.TemporaryDirectory() as td:
            store = bot.Store(Path(td) / "r.sqlite3")
            await store.mark_product_posted("LootZoneIndia11", sig, 55, 0)
            same, _ = await store.product_already_posted("LootZoneIndia11", sig, 55, 0)
            other, _ = await store.product_already_posted("Under99Deals11", sig, 55, 0)
            cheaper, _ = await store.product_already_posted("LootZoneIndia11", sig, 45, 0)
            amazon = await store.preview_allowed("x\nhttps://www.amazon.in/dp/B0C8JPD1KL")
            shortener = await store.preview_allowed("x\nhttps://bitli.in/hlqmyQS")
            return same, other, cheaper, amazon, shortener

    same, other, cheaper, amazon, shortener = asyncio.run(run())
    check("dedup blocks only an exact repeat on the SAME channel", same is True)
    check("another channel still receives the very same deal", other is False)
    check("a cheaper repeat is still published", cheaper is False)
    check("a real product preview card is kept on the loot channels", amazon is True)
    check("only the junk shortener card is suppressed", shortener is False)

    compact = bot.compact_flipkart_product_link(
        "https://www.flipkart.com/flipkart/p/item?lid=X&pid=SOPH2D7GG")
    check("the Flipkart link still opens the same product, just shorter",
          "SOPH2D7GG" in compact and "flipkart.com" in compact, compact)

    # And the per-channel policy switches are all keyed to SHOPPING_TARGET.
    deliver = src[src.index("async def process_job"):]
    strict_block = deliver[deliver.index("if target == SHOPPING_TARGET:"):]
    strict_block = strict_block[:strict_block.index("target_text = strip_amazon_tag_for_undeclared")]
    for rule in ("affiliate_safe_text", "has_amazon_trademark", "add_link_disclosure",
                 "shopping_quota_left", "shopping_pace_wait"):
        check("%s sits inside the SHOPPING_TARGET branch" % rule, rule in strict_block,
              "%s is applied outside the review-channel branch" % rule)


def test_deep_audit_prices_foreign_tags_and_nameless_posts():
    """Round 17 deep audit: four defects found by running real source shapes
    through the pipeline rather than invented examples."""
    import re as _re

    # 1. A DISCOUNT IS NOT A PRICE. "Rs.99 off" was read as a price of 9 - the
    # regex lookahead only had to fail for the shorter match, so it backtracked
    # off the last digit. A figure the source never charged is corruption.
    check("'99 off' is not a price", bot.parse_price("\u20b999 off") is None)
    check("'Save 500' is not a price", bot.parse_price("Save \u20b9500") is None)
    check("'Flat 200 off' is not a price", bot.parse_price("Flat 200 off") is None)
    check("'50 cashback' is not a price", bot.parse_price("\u20b950 cashback") is None)
    check("'upto 300 off' is not a price", bot.parse_price("upto \u20b9300 off") is None)
    # ...while every real price shape still reads correctly.
    for text, expected in (("@264.", 264), ("\u20b91,299", 1299), ("Rs.9999", 9999),
                           ("499/-", 499), ("only 89", 89), ("at 261", 261),
                           ("MRP \u20b92990 Deal \u20b9899", 899),
                           ("Deal \u20b9899 Save \u20b92091", 899),
                           ("1024 ml @517", 517)):
        check("%r still reads as %s" % (text, expected),
              bot.parse_price(text) == expected, str(bot.parse_price(text)))

    # 2. A STRANGER'S ASSOCIATES TAG MUST NEVER REACH THE REVIEWED CHANNEL.
    # To Amazon that is our declared property paying a third party.
    if bot.OUR_TAG:
        foreign = "Dustpan @ 55\nhttps://www.amazon.in/dp/B0X?tag=stranger-21"
        fixed = bot.retag_foreign_amazon_links(bot.affiliate_safe_text(foreign))
        check("a foreign tag is replaced on the review channel",
              "stranger-21" not in fixed, repr(fixed))
        check("it is replaced with OURS, not merely stripped",
              "tag=" + bot.OUR_TAG in fixed, repr(fixed))
        check("our own tag is left alone",
              bot.retag_foreign_amazon_links("x https://www.amazon.in/dp/B0X?tag=" + bot.OUR_TAG)
              == "x https://www.amazon.in/dp/B0X?tag=" + bot.OUR_TAG)
        check("a non-Amazon link is never touched",
              bot.retag_foreign_amazon_links("x https://www.flipkart.com/a/p/itmX?tag=z")
              == "x https://www.flipkart.com/a/p/itmX?tag=z")
        src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
        deliver = src[src.index("async def process_job"):]
        check("the retag runs on the review path",
              "retag_foreign_amazon_links(safe_text)" in deliver, "not wired")

    # 3. THE PRODUCT NAME WAS BEING THROWN AWAY. A one-word name, and a brand
    # plus model number, both failed the "two words" test - the reviewer was
    # left with "Price: 55" and no product at all.
    for raw, must_keep in (("Dustpan @ 55", "Dustpan"),
                           ("Kettle at 549", "Kettle"),
                           ("boAt 141\n\u20b9899", "boAt 141"),
                           ("Redmi 13C\n\u20b98999", "Redmi 13C"),
                           ("iPhone 15\n\u20b965999", "iPhone 15")):
        out = bot.affiliate_safe_text(raw + "\nhttps://amzn.to/a")
        check("the product name %r survives" % must_keep, must_keep in out, repr(out))
    check("a price stated in the product line is not duplicated underneath",
          bot.affiliate_safe_text("Dustpan @ 55\nhttps://amzn.to/a").count("55") == 1,
          bot.affiliate_safe_text("Dustpan @ 55\nhttps://amzn.to/a"))

    # 4. A POST WITH NO PRODUCT NAME MUST BE REFUSED, NOT PUBLISHED BARE.
    # The old guard looked for three letters anywhere and the word "Price"
    # satisfied it, so "Price: 99 + link" went out as a listing.
    def publishable(raw):
        safe = bot.retag_foreign_amazon_links(bot.affiliate_safe_text(raw))
        named = [ln for ln in (safe or "").splitlines()
                 if ln.strip() and not bot.URL_RE.fullmatch(ln.strip())
                 and not _re.fullmatch(r"(?i)price\s*[::]\s*[\u20b9\d,.]+", ln.strip())
                 and _re.search(r"[A-Za-z]{3}", ln)]
        return bool(safe and bot.URL_RE.search(safe) and named)

    check("a bare category word is refused", publishable("Soap\nhttps://amzn.to/a") is False)
    check("a price with no product is refused", publishable("oil @ 99\nhttps://amzn.to/a") is False)
    check("a lone amount is refused", publishable("\u20b9299\nhttps://amzn.to/a") is False)
    check("a real product still publishes", publishable("Dustpan @ 55\nhttps://amzn.to/a") is True)
    src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    check("the nameless-post guard is wired at delivery",
          "_named = [ln for ln in" in src, "guard missing")


def main() -> int:
    for name, text in CORPUS.items():
        run_case(name, text)
    test_signature_rules()
    test_auditor_mirrors_the_bot()
    test_chunking_never_cuts_a_link()
    test_whatsapp_identity_is_the_same_rule()
    test_cleaning_never_eats_a_line()
    test_markdown_link_with_a_url_label_keeps_the_merchant_url()
    test_a_photo_post_with_no_link_is_still_posted()
    test_layout_is_the_sources_blank_lines_only()
    test_lists_are_shortened_with_our_bitly_and_stay_neat()
    test_photos_arrive_as_the_source_posted_them()
    test_our_channel_link_sits_on_the_top_line_once()
    test_a_product_list_pairs_every_deal_with_its_own_link()
    test_the_shopping_channel_copy_is_programme_safe()
    test_a_deal_with_no_working_link_is_not_posted()
    test_the_same_deal_under_two_banner_words_is_one_post()
    test_a_flipkart_product_link_goes_out_short()
    test_our_amazon_tag_only_rides_on_declared_channels()
    test_the_review_channel_copy_is_built_from_an_allowlist()
    test_review_channel_posts_are_disclosed_amazon_only_and_capped()
    test_the_review_channel_carries_no_amazon_marks_photos_or_channel_pointers()
    test_a_roundup_list_cannot_be_posted_twice_from_two_sources()
    test_our_new_associates_tag_and_the_self_source_loop()
    test_the_review_copy_is_clean_on_real_source_shapes()
    test_native_links_on_review_channel_and_the_tag_switch()
    test_the_whatsapp_bridge_cannot_go_quiet()
    test_the_review_channel_posts_at_a_human_pace()
    test_our_tag_is_live_now_on_the_review_channel()
    test_our_tag_cannot_ride_into_an_undeclared_channel_inside_a_short_link()
    test_the_four_defects_the_user_photographed()
    test_only_the_review_channel_is_restricted()
    test_deep_audit_prices_foreign_tags_and_nameless_posts()
    print("\n" + ("LINE FIDELITY: FAILURES: " + ", ".join(FAILS) if FAILS else "test_line_fidelity: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
