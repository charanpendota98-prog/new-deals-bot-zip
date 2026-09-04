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
    print("\n" + ("LINE FIDELITY: FAILURES: " + ", ".join(FAILS) if FAILS else "test_line_fidelity: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
