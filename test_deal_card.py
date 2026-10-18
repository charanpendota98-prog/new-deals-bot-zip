"""Deal-card studio: the bot's own reading of a caption, drawn as a card.

A competitor's post is a photo plus a caption - discount pill, struck M.R.P.,
coupon box, green final-price bar. Our posts are text, so the same deal looks
poorer on the same phone. ops/deal_card.py renders that card from a caption we
already have and says which destinations would receive card + caption.

These checks pin what must not drift:

* the card reads prices with the BOT's parsers - literally the same function
  objects, so a caption can never mean two different prices on two surfaces;
* the coupon maths can never promise a free product (coupon >= price keeps the
  deal price) and an "MRP" at or below the deal price is not shown struck;
* the PNG is a real PNG at the requested width, draws the photo when one is
  given, and never shows a missing-glyph box where the rupee sign belongs;
* a link the ledger cannot prove ours is WITHHELD from every caption, because a
  card that prints a stranger's tag is a card that pays a stranger;
* the forward plan agrees with channels.json + the env-declared shelves about
  who receives what, and says "nowhere to go" when nothing is configured.

Run: python3 test_deal_card.py   (from the repo root)
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.update(
    TELEGRAM_API_ID="1", TELEGRAM_API_HASH="x",
    EARNKARO_API_KEY="k", AMAZON_TAG="",
)
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT / "ops"))

import deal_card as card          # noqa: E402
import deal_facts as facts        # noqa: E402
import main_bot_new as bot        # noqa: E402

FAILURES: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASSED
    if ok:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name}  <- {detail}")


CAPTION = "\n".join([
    "🔥 Headphone Bluetooth Neckband Wireless Sport In-ear Earphone with Mic (Black)",
    "Deal ₹78  M.R.P.: ₹120  (35% off)",
    "Apply 4% coupon at checkout",
    "Min Buy Qnty = 2",
    "Lowest price ever on this model",
    "Buy: https://amzn.lt/3xYz9",
])
OUR_TAG = sorted(bot.OUR_AMAZON_TAGS)[0]
STRANGER_TAG = "deals0911-21"
# What the bot publishes is the TAGGED link; link_cache keeps the source's raw
# link, the tagged affiliate link and the untagged resolved destination, and the
# card may only print the tagged one - the raw source link earns nothing until
# the bot re-tags it, which is exactly what the money engine already says.
OUR_LINK = f"https://www.amazon.in/dp/B0DEAL1?tag={OUR_TAG}"
THEIR_LINK = f"https://www.amazon.in/dp/B0DEAL2?tag={STRANGER_TAG}"


def make_link_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE link_cache (
      source_url TEXT PRIMARY KEY, affiliate_url TEXT NOT NULL,
      resolved_url TEXT NOT NULL DEFAULT '', deal_key TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL DEFAULT 0);
    CREATE TABLE owned_link_proofs (
      affiliate_url TEXT PRIMARY KEY, destination_url TEXT NOT NULL,
      proof_type TEXT NOT NULL DEFAULT '', publisher_id TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL DEFAULT 0);
    """)
    conn.execute("INSERT INTO link_cache(source_url, affiliate_url, resolved_url) VALUES (?,?,?)",
                 ("https://amzn.lt/ours", OUR_LINK, "https://www.amazon.in/dp/B0DEAL1"))
    conn.execute("INSERT INTO link_cache(source_url, affiliate_url, resolved_url) VALUES (?,?,?)",
                 ("https://amzn.lt/theirs", THEIR_LINK, "https://www.amazon.in/dp/B0DEAL2"))
    conn.commit()
    conn.close()


def temp_env(tmp: Path) -> Path:
    env = tmp / "studio.env"
    env.write_text("\n".join([
        "TG_CATEGORY_ELECTRONICS_TARGET=@electroshelf",
        "WA_CHANNEL_ELECTRONICS=12034567890@g.us",
        "WA_CHANNEL_UNDER99=12098765432@g.us",
        "EARNKARO_API_KEY=k",
    ]) + "\n", encoding="utf-8")
    return env


def temp_channels(tmp: Path) -> Path:
    path = tmp / "channels.json"
    path.write_text(json.dumps({
        "version": 1,
        "channels": [
            {"name": "@electroshelf", "enabled": True, "categories": ["electronics"],
             "sources": ["deals"], "amazon_tag": True},
            {"name": "@mainloots", "enabled": True, "categories": [], "sources": ["*"],
             "amazon_tag": True},
            {"name": "@offshelf", "enabled": False, "categories": ["grocery"],
             "sources": ["deals"], "amazon_tag": False},
        ],
    }), encoding="utf-8")
    return path


def main() -> int:
    print("deal facts come from one definition, shared with the money pipeline")
    bot_src = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8")
    for name in ("parse_price", "parse_discount", "has_lowest_price_claim"):
        ours = getattr(facts, name)
        theirs = getattr(bot, name)
        where = lambda fn: (str(Path(fn.__globals__["__file__"]).resolve()),
                            fn.__code__.co_firstlineno)
        check(f"the money file no longer defines {name}", f"def {name}(" not in bot_src)
        check(f"and routes with the same {name} the card prints", where(ours) == where(theirs),
              f"{where(ours)} != {where(theirs)}")
    check("the money file imports the shared reader",
          "from bestgaa.deal_facts import" in bot_src, "")
    check("the card renderer exposes the shared reader",
          card.deal_facts is facts.deal_facts)

    print("the reference deal reads exactly as the screenshot says")
    got = facts.deal_facts(CAPTION)
    check("deal price is the deal price, not the MRP", got["price"] == 78, got)
    check("the M.R.P. is read separately", got["mrp"] == 120, got)
    check("the discount comes from the caption", got["discount"] == 35, got)
    check("the coupon percent is read", (got["coupon"] or {}).get("percent") == 4, got["coupon"])
    check("4% of Rs78 saves Rs3.12, rounded to Rs3 for the integer maths",
          got["coupon_saving"] == 3, got["coupon_saving"])
    check("the final price is what the buyer pays after the coupon",
          got["final"] == 75, got["final"])
    check("Min Buy Qnty is read", got["min_qty"] == 2, got)
    check("a lowest-price claim is noticed", got["lowest_price_claim"] is True, got)
    check("the title is the product line", got["title"].startswith("🔥 Headphone Bluetooth"),
          got["title"])

    print("coupon and MRP edge cases can never invent money")
    coupon = facts.parse_coupon("Use code SAVE50 and get a ₹50 coupon")
    check("a named coupon code is read", coupon.get("code") == "SAVE50", coupon)
    check("a rupee coupon amount is read", coupon.get("amount") == 50, coupon)
    check("the code's saving is its amount", facts.coupon_saving(199, coupon) == 50, "")
    big = facts.final_price(40, {"amount": 90})
    check("a coupon bigger than the price does not make a free product", big == 40, big)
    check("a coupon equal to the price keeps the deal price",
          facts.final_price(40, {"amount": 40}) == 40, "")
    check("an MRP at or below the deal price is not an MRP",
          facts.deal_facts("Deal ₹99 MRP ₹99")["mrp"] is None, "")
    check("a price-less caption has no final price",
          facts.deal_facts("freebie giveaway, join now friends")["final"] is None, "")
    check("min qty 150 is a typo, not a condition", facts.parse_min_qty("Min Buy Qnty = 150") is None, "")
    check("min qty 2 is a condition", facts.parse_min_qty("Min Buy Qnty = 2") == 2, "")

    print("captions are plain text the bot could send with parse_mode=None")
    caps = card.captions(got, link=OUR_LINK, link_owned=True)
    check("telegram keeps exactly one fire emoji",
          caps["telegram"].count("🔥") == 1, caps["telegram"][:60])
    check("telegram prints the link we own", OUR_LINK in caps["telegram"], "")
    check("the coupon line tells the buyer what to do",
          "Apply 4% coupon (saves ₹3.12)" in caps["telegram"], caps["telegram"])
    check("the minimum quantity survives into the caption",
          "Min Buy Qnty = 2" in caps["telegram"], "")
    check("whatsapp drops the emoji arrow", "🔗" not in caps["whatsapp"], caps["whatsapp"])
    check("the premium caption is shorter than the telegram one",
          len(caps["premium"].splitlines()) < len(caps["telegram"].splitlines()), "")
    withheld = card.captions(got, link=THEIR_LINK, link_owned=False,
                             link_why="another publisher's tag")
    check("a link we cannot prove is withheld from the caption",
          THEIR_LINK not in withheld["telegram"], withheld["telegram"])
    check("and the caption says why", "withheld" in withheld["telegram"], withheld["telegram"])
    check("no markdown or HTML reaches a caption",
          "<" not in withheld["telegram"] and "**" not in withheld["telegram"], "")

    print("the raster card is a real PNG and draws what the caption says")
    png = card.card_png(got)
    check("the bytes are a PNG", png[:8] == b"\x89PNG\r\n\x1a\n", png[:8])
    from PIL import Image
    import io as _io
    with Image.open(_io.BytesIO(png)) as shot:
        check("the card is 1080 wide by default", shot.width == 1080, shot.size)
        check("the card is tall enough for photo, price and final bar",
              500 < shot.height < 900, shot.size)
        placeholder = shot.getpixel((60, 60))
    check("without a photo the slot is the honest placeholder gradient",
          placeholder[0] > 230 and placeholder[2] > 200, placeholder)
    with Image.new("RGB", (400, 400), (38, 65, 94)) as fake:
        buffer = _io.BytesIO()
        fake.save(buffer, format="PNG")
        photo_bytes = buffer.getvalue()
    png2 = card.card_png(got, photo=photo_bytes)
    with Image.open(_io.BytesIO(png2)) as shot2:
        drawn = shot2.getpixel((60, 60))
    check("a supplied photo fills the photo slot", drawn == (38, 65, 94), drawn)
    narrow = card.card_png(got, width=720)
    with Image.open(_io.BytesIO(narrow)) as shot3:
        check("the width is honoured", shot3.width == 720, shot3.size)
    fonts = card.Fonts()
    check("the money formatter agrees with the font it will draw with",
          fonts.money(78) in ("₹78", "Rs.78") and fonts.money(78, paise=True) in ("₹78.00", "Rs.78.00"),
          fonts.money(78))
    if fonts.truetype:
        check("DejaVu Sans really draws the rupee sign", fonts.rupee is True, "")
        check("the rupee test distinguishes a drawn glyph from a missing one",
              card.rupee_supported(fonts.get("regular", 24)) is True, "")
    else:
        check("without a truetype font the card falls back to Rs. instead of a box",
              fonts.money(78) == "Rs.78", fonts.money(78))
    check("emoji are stripped for the raster only", card.plain_title("🔥 Deal 🔥") == "Deal",
          card.plain_title("🔥 Deal 🔥"))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "links.sqlite3"
        make_link_db(db)
        env = temp_env(tmp_path)
        channels = temp_channels(tmp_path)

        print("a link is printable only when the ledger proves it is ours")
        ours = card.link_verdict(OUR_LINK, db=str(db))
        check("the tagged link we published is printable", ours["owned"] is True, ours)
        check("and it is called owned, not guessed", ours["bucket"] == "owned", ours)
        raw = card.link_verdict("https://amzn.lt/ours", db=str(db))
        check("the source's RAW link is not printable until the bot re-tags it",
              raw["owned"] is False and raw["route"] == "amazon_untagged", raw)
        theirs = card.link_verdict(THEIR_LINK, db=str(db))
        check("a stranger's tag is not printable", theirs["owned"] is False, theirs)
        check("and it is labelled foreign_tag, paying a stranger",
              theirs["bucket"] == "foreign_tag", theirs)
        blind = card.link_verdict("https://bit.ly/3abcDEF", db=str(db))
        check("a shortener with no proof row cannot be attributed",
              blind["owned"] is False and blind["bucket"] in ("unattributed", "earns_nothing"),
              blind)
        missing = card.link_verdict("https://bit.ly/3abcDEF",
                                    db=str(tmp_path / "nope.sqlite3"))
        check("an unreadable database never makes a short link printable",
              missing["owned"] is False and missing["bucket"] == "unattributed", missing)
        check("and the verdict says the ledger was unreadable",
              "not readable" in missing["why"], missing["why"])
        tag_visible = card.link_verdict(OUR_LINK, db=str(tmp_path / "nope.sqlite3"))
        check("a visibly-our-tagged Amazon link stays printable from the tag alone",
              tag_visible["owned"] is True and "not readable" in tag_visible["why"], tag_visible)
        none = card.link_verdict("")
        check("no link means nothing printable", none["owned"] is False and none["bucket"] == "none", none)

        print("the forward plan reads channels.json and the env shelves")
        plan = card.forward_plan(CAPTION, link=OUR_LINK,
                                 channels_path=str(channels), env_files=[str(env)], db=str(db))
        check("the deal classifies as electronics", plan["category"] == "electronics", plan["category"])
        kinds = {(row["surface"], row["destination"]): row for row in plan["rows"]}
        check("the unfiltered Telegram target receives it",
              kinds[("telegram", "mainloots")]["accepts"] is True, kinds)
        check("the electronics shelf receives it",
              kinds[("telegram", "electroshelf")]["accepts"] is True, kinds)
        check("a disabled grocery shelf does not",
              kinds[("telegram", "offshelf")]["accepts"] is False, kinds)
        check("the WhatsApp electronics channel receives card + caption",
              kinds[("whatsapp", "12034567890@g.us")]["format"] == "card + caption", kinds)
        check("the core under-99 WhatsApp channel receives every deal",
              kinds[("whatsapp", "12098765432@g.us")]["accepts"] is True, kinds)
        check("the plan names the Telegram carrier",
              "send_file" in kinds[("telegram", "mainloots")]["carrier"], kinds)
        check("the plan names the WhatsApp carrier",
              "broadcastMediaItem" in kinds[("whatsapp", "12034567890@g.us")]["carrier"], kinds)
        check("every accepted row explains itself",
              all(row["why"] for row in plan["rows"] if row["accepts"]), plan["rows"])
        check("the link verdict rides along with the plan",
              plan["link_ok"] is True, plan["link_ok"])
        bad_plan = card.forward_plan(CAPTION, link=THEIR_LINK,
                                     channels_path=str(channels), env_files=[str(env)], db=str(db))
        check("a stranger's tag warns instead of printing",
              any("link not printable" in w for w in bad_plan["warnings"]), bad_plan["warnings"])
        empty_plan = card.forward_plan(CAPTION, channels_path=str(tmp_path / "none.json"),
                                       env_files=[str(tmp_path / "nope.env")], db=str(db))
        check("with no config the plan says nowhere to go",
              empty_plan["rows"] == [] and
              any("nowhere to go" in w for w in empty_plan["warnings"]), empty_plan["warnings"])

        print("the studio payload and the CLI front door")
        payload = card.studio(CAPTION, link=OUR_LINK, photo_path="",
                              channels_path=str(channels), env_files=[str(env)], db=str(db))
        check("the payload carries facts, captions, plan and card state",
              all(key in payload for key in ("facts", "captions", "plan", "card", "link")),
              sorted(payload))
        check("the card rendered inside the studio", payload["card"]["available"] is True,
              payload["card"])
        check("the studio lists every surface",
              payload["surfaces"] == ["telegram", "whatsapp", "premium", "text"], payload["surfaces"])
        run = subprocess.run([sys.executable, str(ROOT / "ops" / "deal_card.py"),
                              "--text", CAPTION, "--json", "--link", OUR_LINK,
                              "--channels", str(channels), "--env-file", str(env),
                              "--db", str(db)],
                             capture_output=True, text=True, cwd=str(ROOT))
        check("the CLI exits 0", run.returncode == 0, run.stderr[:400])
        cli_payload = json.loads(run.stdout)
        check("the CLI prints the same facts the module computed",
              cli_payload["facts"]["final"] == payload["facts"]["final"], cli_payload["facts"])
        run_png = subprocess.run([sys.executable, str(ROOT / "ops" / "deal_card.py"),
                                  "--text", CAPTION, "--png", "-", "--db", str(db)],
                                 capture_output=True, cwd=str(ROOT))
        check("--png - writes the PNG to stdout and nothing else",
              run_png.stdout[:8] == b"\x89PNG\r\n\x1a\n", run_png.stdout[:8])
        run_surface = subprocess.run([sys.executable, str(ROOT / "ops" / "deal_card.py"),
                                      "--text", CAPTION, "--surface", "whatsapp",
                                      "--db", str(db)],
                                     capture_output=True, text=True, cwd=str(ROOT))
        check("--surface prints only the chosen caption",
              "caption: whatsapp" in run_surface.stdout
              and "caption: telegram" not in run_surface.stdout, run_surface.stdout[-200:])
        run_empty = subprocess.run([sys.executable, str(ROOT / "ops" / "deal_card.py"),
                                    "--text", "   "],
                                   capture_output=True, text=True, cwd=str(ROOT))
        check("an empty caption is refused with exit 2", run_empty.returncode == 2,
              run_empty.returncode)

    print(f"\ntest_deal_card: {PASSED} checks passed, {len(FAILURES)} failed")
    if FAILURES:
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
