"""A price channel keeps the promise in its own name.

USER REPORT (2026-10-23): a Dreame robot vacuum advertised at ₹39,699 was
DELIVERED to Under99Deals11 (queue 448354, deliveries table: `Under99Deals11
sent`). The bot knew the price - `parse_price` read "@39699" - and still routed
it there, because the ICICI bank-offer fan-out appends EVERY owned channel and
the price channels rode along without anyone asking whether the item fits.

These checks pin the rule and its two documented exceptions:

  * a single product above the band never enters that channel, whichever route
    added it (card fan-out, dashboard channel, watched source);
  * an UNKNOWN price counts as a miss - "@39699" with no ₹ figure, or a coupon
    line, is not evidence of an under-₹99 deal;
  * a multi-product LIST still goes into both price channels (the operator's own
    rule), and a service offer (Zomato/Swiggy/movies/app promos) still reaches
    every owned channel because it has no product price at all.
"""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="routing-bands-")
os.environ.update(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="test",
                  EARNKARO_API_KEY="test",
                  BOT_DB_PATH=str(Path(_TMP.name) / "bot.sqlite3"),
                  BOT_LOG_DIR=str(Path(_TMP.name) / "logs"),
                  BOT_MEDIA_DIR=str(Path(_TMP.name) / "media"),
                  CHANNELS_JSON_PATH=str(Path(_TMP.name) / "channels.json"))
from bestgaa import main_bot_new as bot  # noqa: E402

FAILS = []


def check(name, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


# The live post, verbatim (queue 448354). Note what is money here: the ₹ figures
# are a coupon and a bank offer, and the prices are written with @.
LIVE_POST = ("GRAB : Flat ₹300 Coupon Off On Dreame Robot Vacuum & Mop "
             "Starting @39699.\n\n"
             "D30 Ultra @39699 : https://www.amazon.in/dp/B0H2YVXLBR?tag=mama086-21\n\n"
             "L50 Ultra CE @49199 : https://www.amazon.in/dp/B0GX5HFRHZ?tag=mama086-21\n\n"
             "Upto ₹5000 Off With ICICI Bank Credit Card.")

ALL_TARGETS = list(bot.ALL_OWNED_TARGETS) + ["smartbuyhub11"]


def test_the_live_misroute():
    print("\n== the post that went to the ₹99 channel (queue 448354) ==")
    price = bot.parse_price(LIVE_POST)
    check("the bot reads the product price (not the coupon) from that post",
          price == 39699, str(price))
    check("the post really does look like a bank/card offer", bot.has_card_offer(LIVE_POST))
    kept, dropped = bot.enforce_price_bands(ALL_TARGETS, price)
    names = [name for name, _band in dropped]
    check("₹39,699 is kept OUT of the ₹99 channel", bot.UNDER99_TARGET in names, str(dropped))
    check("₹39,699 is kept OUT of the ₹499 channel too",
          bot.UNDER499_TARGET in names, str(dropped))
    check("the main feed is untouched - the deal is never lost",
          "LootZoneIndia11" in kept and bot.POWER_FILTER_TARGET in kept, str(kept))
    check("the drop names the band it broke",
          dict(dropped).get(bot.UNDER99_TARGET) == 99, str(dropped))


def test_bands_hold_whatever_the_route():
    print("\n== the band is checked on the FINAL list, for every route ==")
    import asyncio

    class Row(dict):
        def keys(self):
            return list(super().keys())

    row = Row(id=1, source="loot_alerts", status="pending", chat_id=-1001, msg_id=2,
              attempts=0, source_text="", rendered_text="", targets_json="[]",
              last_error="", created_at=0, deal_keys_json="", premium_score=None,
              next_at=0, priority=None, chat_key=None, quality=None)
    # The exact shape that took 448354 to the price channel: the card-offer
    # fan-out appends every owned channel, then the guard has the last word.
    fanout = list(bot.ALL_OWNED_TARGETS)
    kept, dropped = bot.enforce_price_bands(fanout, 39699)
    check("a card-offer fan-out no longer puts a ₹39,699 item in @Under99Deals11",
          bot.UNDER99_TARGET not in kept and dict(dropped).get(bot.UNDER99_TARGET) == 99,
          str(dropped))
    check("and every non-price channel still gets it",
          all(t in kept for t in bot.ALL_OWNED_TARGETS
              if t not in (bot.UNDER99_TARGET, bot.UNDER499_TARGET)), str(kept))

    check("a ₹79 deal DOES go to the ₹99 channel",
          bot.UNDER99_TARGET in bot.enforce_price_bands(fanout, 79)[0])
    check("a ₹499 deal goes to the ₹499 channel but not the ₹99 one",
          bot.UNDER499_TARGET in bot.enforce_price_bands(fanout, 499)[0]
          and bot.UNDER99_TARGET not in bot.enforce_price_bands(fanout, 499)[0])
    check("an unknown price is not evidence of a cheap deal",
          bot.UNDER99_TARGET not in bot.enforce_price_bands(fanout, None)[0])
    check("a target that is not a price channel is never dropped",
          bot.enforce_price_bands(["LootZoneIndia11"], 39699) == (["LootZoneIndia11"], []))
    del row, asyncio


def test_the_documented_exceptions():
    print("\n== the operator's own rules survive ==")
    kept, dropped = bot.enforce_price_bands(ALL_TARGETS, 39699, listy=True)
    check("a multi-product LIST still belongs in both price channels",
          bot.UNDER99_TARGET in kept and bot.UNDER499_TARGET in kept and not dropped,
          str(dropped))
    kept, dropped = bot.enforce_price_bands(ALL_TARGETS, None, service=True)
    check("a service offer has no product price and still reaches every channel",
          bot.UNDER99_TARGET in kept and not dropped, str(dropped))


def test_routing_is_wired():
    print("\n== the guard is in the send path, not just a helper ==")
    source = (Path(__file__).resolve().parent / "bestgaa" / "main_bot_new.py").read_text()
    after_scope = source.split("base_targets = scope_dashboard_targets(row[\"source\"], base_targets)", 1)
    check("the guard runs after the final target list is scoped",
          len(after_scope) == 2 and "enforce_price_bands(" in after_scope[1][:1200],
          after_scope[1][:160] if len(after_scope) == 2 else "scope call missing")
    check("a dropped target is logged with its reason (never silent)",
          "PRICE BAND DROP" in source)
    check("the card fan-out says what it is doing",
          "CARD FANOUT" in source)


def main():
    test_the_live_misroute()
    test_bands_hold_whatever_the_route()
    test_the_documented_exceptions()
    test_routing_is_wired()
    print("\n" + ("PRICE BAND TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_routing_bands: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        bot.store.conn.close()
        _TMP.cleanup()
