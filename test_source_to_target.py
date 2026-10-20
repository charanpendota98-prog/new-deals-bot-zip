"""Source → Target: the chain a source post takes, pinned end to end.

The operator's report (2026-10-20) was one post:

    Samsung neo qled 55” @ ₹60k
    https://link.amazon/B0gWibFuA

Nothing about it worked: the price read as ₹60 (sixty rupees for a 60,000-rupee
TV), the caption named no "TV" word so no category shelf would take it, and
`link.amazon` was not recognised as an Amazon short wrapper at all. This suite
is that post, plus the arithmetic and routing around it.

Offline: no Telegram, WhatsApp, affiliate API or database. `main_bot_new` is
imported for the constants that are MIRRORED in the dashboard planner, so the
two can never drift.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
os.environ.setdefault("EARNKARO_PUBLISHER_ID", "5478322")
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))

from deal_facts import parse_price, expand_money_suffixes  # noqa: E402
from dashboard import source_target as planner  # noqa: E402
import main_bot_new as bot  # noqa: E402

FAILS: list[str] = []
POST = 'Samsung neo qled 55” @ ₹60k\n\nhttps://link.amazon/B0gWibFuA'
SNAP = {
    "sources": ["DealSauce"],
    "targets": ["LootZoneIndia11", "PowerLoots1", "Under99Deals11", "under499loots",
                "Premiumlootsdeals", "ElectroShelf", "FashionShelf"],
    "source_targets": {"DealSauce": ["LootZoneIndia11", "PowerLoots1", "Under99Deals11",
                                     "under499loots", "Premiumlootsdeals"]},
    "price_targets": {"under99": "Under99Deals11", "under499": "under499loots",
                      "premium": "Premiumlootsdeals", "power": "PowerLoots1",
                      "secret": "SecretLootIndia1", "tricks": "LootzoneTricks"},
    "tricks_sources": ["TrickXpert"],
    "category_filters": {"ElectroShelf": ["electronics"], "FashionShelf": ["fashion"]},
}


def check(name, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def test_the_operator_post():
    print("\n== the reported post: Samsung neo qled 55\" @ ₹60k ==")
    data = planner.analyze(POST, "DealSauce", snapshot=SNAP)
    check("the price is sixty thousand, not sixty",
          data["facts"]["price"] == 60000, str(data["facts"]["price"]))
    check("the post is classified into electronics",
          data["category"]["key"] == "electronics", str(data["category"]))
    check("the evidence names the TV shape the caption actually used",
          any(e["phrase"] == "neo qled" for e in data["category"]["evidence"]),
          str(data["category"]["evidence"]))
    check("the content policy allows it", data["safety"]["allowed"] is True,
          str(data["safety"]))
    link = data["links"][0]
    check("the link is seen as Amazon's own short wrapper",
          link["role"] == "amazon_short", str(link))
    check("the wrapper is never published as-is", link["never_published_as"] is True, str(link))
    check("the plan explains resolution -> our tag -> our shortener",
          "followed to the real /dp/ page" in link["why"] and "our" in link["why"], link["why"])
    names = {t["name"]: t for t in data["plan"]["targets"]}
    check("the main feed receives it", "LootZoneIndia11" in names, str(list(names)))
    check("PowerLoots receives every non-Tricks deal", "PowerLoots1" in names, str(list(names)))
    check("the electronics shelf receives it", "ElectroShelf" in names, str(list(names)))
    excluded = {t["name"]: t["why"] for t in data["plan"]["excluded"]}
    check("the ₹99 channel is withheld (and says why)",
          "Under99Deals11" in excluded and "above ₹99" in excluded["Under99Deals11"],
          str(excluded))
    check("the ₹499 channel is withheld (and says why)",
          "under499loots" in excluded and "above ₹499" in excluded["under499loots"],
          str(excluded))
    check("premium is withheld for a full-price TV",
          "Premiumlootsdeals" in excluded, str(excluded))
    check("the fashion shelf is withheld for an electronics post",
          "FashionShelf" in excluded and "fashion" in excluded["FashionShelf"], str(excluded))
    check("a channel is never listed as both receiving and withholding",
          not (set(names) & set(excluded)), str((list(names), list(excluded))))
    check("the deduction is labelled as a prediction, not a promise",
          any("send-time gates" in note for note in data["plan"]["notes"]),
          str(data["plan"]["notes"]))


def test_price_arithmetic():
    print("\n== price arithmetic: Indian magnitude suffixes ==")
    cases = {
        "Samsung neo qled 55” @ ₹60k": 60000,
        'Samsung neo qled 55" @ ₹60 K': 60000,
        "Samsung TV at 60k": 60000,
        "Samsung TV ₹60k only": 60000,
        "MRP ₹89,990 Deal ₹60k": 60000,
        "Samsung TV ₹60,000": 60000,
        "₹1.5 lakh laptop": 150000,
        "₹2 lac car": 200000,
        "boAt 141 @ ₹899": 899,          # "At" inside "boAt" is not a price label
        "boAt Airdopes 141 @ ₹899": 899,
        "1.5L bottle @ ₹199": 199,        # a capacity is not a price
        "190L fridge ₹12,999": 12999,
        "Prestige 5L cooker ₹1,299": 1299,
        "2 kg rice ₹199": 199,
        "https://link.amazon/B0gWibFuA": None,   # a link is not money
    }
    for text, expected in cases.items():
        got = parse_price(text)
        check(f"price({text[:42]!r}) == {expected}", got == expected, f"got {got}")
    check("expanding leaves real links untouched",
          expand_money_suffixes("₹60k https://x.com/60k") == "₹60000 https://x.com/60k",
          expand_money_suffixes("₹60k https://x.com/60k"))
    check("the bot's own reader agrees with the shared one",
          bot.parse_price(POST) == 60000, str(bot.parse_price(POST)))


def test_amazon_short_wrapper():
    print("\n== link.amazon is an Amazon wrapper (not an unknown foreign host) ==")
    check("the bot treats link.amazon as an Amazon short host",
          bot.is_amazon_short_host("link.amazon") is True)
    check("an Amazon-only post is recognised as Amazon",
          bot.is_amazon_only_post(POST) is True)
    check("an unresolved wrapper is cut, never published",
          bot.cut_amazon_short_links(f"deal {POST}")[1] == ["https://link.amazon/B0gWibFuA"],
          str(bot.cut_amazon_short_links(f"deal {POST}")[1]))
    from category_classifier import category_words_from_url
    check("the classifier gets no category words out of an opaque wrapper",
          category_words_from_url("https://link.amazon/B0gWibFuA") == "",
          category_words_from_url("https://link.amazon/B0gWibFuA"))
    check("the claim that our tag rides on link.amazon is never made on the wrapper",
          bot.Store.is_our_amazon_tag_link("https://link.amazon/B0gWibFuA") is False)
    # The planner's mirrored host sets must equal the bot's, or a preview lies.
    check("planner mirror == bot AMAZON_SHORT_DOMAINS",
          set(planner.AMAZON_SHORT_HOSTS) == set(bot.AMAZON_SHORT_DOMAINS),
          str(set(planner.AMAZON_SHORT_HOSTS) ^ set(bot.AMAZON_SHORT_DOMAINS)))
    check("planner mirror == bot AMAZON_DOMAINS (www-insensitive)",
          set(planner.AMAZON_HOSTS)
          == {h for h in bot.AMAZON_DOMAINS if not h.startswith("www.")},
          str(set(planner.AMAZON_HOSTS) ^ set(bot.AMAZON_DOMAINS)))
    check("planner shorteners are all hosts the bot follows",
          set(planner.KNOWN_SHORTENERS)
          <= set(bot.SHORT_DOMAINS) | set(bot.AMAZON_SHORT_DOMAINS),
          str(set(planner.KNOWN_SHORTENERS) - set(bot.SHORT_DOMAINS)
              - set(bot.AMAZON_SHORT_DOMAINS)))


def test_link_roles():
    print("\n== every link shape is described honestly ==")
    roles = {
        "https://bitli.in/abc123": "ours",
        "https://amzn.to/4dnF9lU": "amazon_short",
        "https://www.amazon.in/dp/B0G9WXYZ12?tag=someone-21": "amazon_product",
        "https://www.flipkart.com/p/itm123": "merchant",
        "https://bit.ly/x1": "runtime_shortener",
        "https://some-new-wrapper.example/Loot9": "unknown_wrapper",
        "https://t.me/LootZoneIndia11": "telegram",
    }
    for url, expected in roles.items():
        data = planner.analyze("deal ₹499 " + url, "DealSauce", snapshot=SNAP)
        got = data["links"][0]["role"]
        check(f"{url} -> {expected}", got == expected, got)
    data = planner.analyze(
        "deal ₹499 https://www.amazon.in/dp/B0G9WXYZ12?tag=someone-21",
        "DealSauce", snapshot=SNAP)
    check("a product page yields the product id dedupe uses",
          data["identity"]["product_ids"] == ["B0G9WXYZ12"], str(data["identity"]))
    data = planner.analyze(POST, "DealSauce", snapshot=SNAP)
    check("a wrapper says the id only exists after resolution",
          "after the bot resolves it" in data["identity"]["note"], data["identity"]["note"])


def test_plan_edges():
    print("\n== plan edges: unknown source, no snapshot, unsafe post ==")
    unknown = planner.analyze(POST, "NotAConfiguredSource", snapshot=SNAP)
    check("an unknown source routes nowhere and says so",
          not unknown["plan"]["targets"]
          and any("not a configured source" in n for n in unknown["plan"]["notes"]),
          str(unknown["plan"]))
    bare = planner.analyze(POST, "DealSauce", snapshot={})
    check("without a snapshot the facts are still exact",
          bare["facts"]["price"] == 60000 and bare["category"]["key"] == "electronics",
          str(bare["facts"]))
    check("without a snapshot the plan refuses to guess",
          bare["plan"]["targets"] == []
          and any("routing_snapshot" in n for n in bare["plan"]["notes"]),
          str(bare["plan"]))
    unsafe = planner.analyze("S.E.X video leaked https://link.amazon/B0gWibFuA",
                             "DealSauce", snapshot=SNAP)
    check("a blocked post is marked blocked, with the policy category",
          unsafe["safety"]["allowed"] is False and unsafe["safety"]["category"],
          str(unsafe["safety"]))
    check("a blocked post carries the explicit warning",
          any("never enters the queue" in w for w in unsafe["warnings"]), str(unsafe["warnings"]))
    cheap = planner.analyze("Trimmer @ ₹59 https://amzn.to/abc", "DealSauce", snapshot=SNAP)
    cheap_names = {t["name"] for t in cheap["plan"]["targets"]}
    check("a sub-₹99 deal reaches both price channels",
          "Under99Deals11" in cheap_names and "under499loots" in cheap_names,
          str(cheap_names))
    nolink = planner.analyze("Samsung neo qled 55” @ ₹60k", "DealSauce", snapshot=SNAP)
    check("a post with no link says it cannot be monetized",
          any("no link" in w for w in nolink["warnings"]), str(nolink["warnings"]))


def test_cli():
    print("\n== the CLI (ops/source_to_target.py) ==")
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "routing_snapshot.json"
        snap.write_text(json.dumps(SNAP), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "ops/source_to_target.py", "--json", "--source", "DealSauce",
             "--snapshot", str(snap), "--text", POST],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            data = {}
        check("--json prints a full analysis", proc.returncode == 0 and bool(data),
              (proc.stdout or proc.stderr)[-200:])
        check("the CLI agrees with the library",
              data.get("facts", {}).get("price") == 60000
              and data.get("category", {}).get("key") == "electronics",
              json.dumps(data.get("facts"))[:200])
        text = subprocess.run(
            [sys.executable, "ops/source_to_target.py", "--source", "DealSauce",
             "--snapshot", str(snap), "--text", POST],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        check("the human output names the price, the shelf and the channels",
              all(s in text.stdout for s in ("₹60,000", "electronics", "@ElectroShelf")),
              text.stdout[-400:])


if __name__ == "__main__":
    test_the_operator_post()
    test_price_arithmetic()
    test_amazon_short_wrapper()
    test_link_roles()
    test_plan_edges()
    test_cli()
    print("\n" + ("SOURCE→TARGET: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_source_to_target: all checks PASS"))
    sys.exit(1 if FAILS else 0)
