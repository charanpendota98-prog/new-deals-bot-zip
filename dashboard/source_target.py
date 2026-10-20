"""Source → Target: what our pipeline will do with THIS source post, before it posts.

The operator pastes a source channel's post (and, if it is not obvious, which
source it came from) and gets the whole chain answered in one screen:

  * the link plan — every URL, what kind of wrapper it is, whether it is ours,
    what the bot does with it (resolve → our tag → our shortener), and the
    product id the dedupe will use;
  * the facts — price / MRP / discount / coupon, read by the SAME reader the
    bot routes on (`deal_facts`), so "₹60k" is sixty thousand here too;
  * the shelf — category, the evidence behind it, and which of our shelves
    accept or withhold it;
  * the safety verdict — the public-channel content policy, same file as the
    bot and the bridge;
  * the targets — which of OUR channels receive it and WHY (main feed,
    price band, premium, a dashboard shelf), and which are excluded and why.

Honesty rules
-------------
* Everything here is read-only and OFFLINE. The affiliate converter is not
  called: whether the network really returns an owned link is decided at send
  time and is shown as "verified at send", never faked here.
* The routing map comes from the bot's own `routing_snapshot.json`
  (`source_targets`, `price_targets`, `category_filters`), published by
  `main_bot_new`. The dashboard does not re-implement routing; a preview can
  therefore never disagree with the router it is describing. When a key is
  missing (an older bot), the plan says so instead of guessing.
* `test_source_to_target.py` pins the constants that ARE mirrored here
  (Amazon short hosts, our shorteners) to the bot's own sets.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

try:  # running as `dashboard.source_target` (dashboard, ops CLI, tests)
    from deal_facts import (deal_facts, deal_title, premium_score,
                            eligible_for_premium)
    from category_classifier import classify_category, load_rules
    from safety_filter import scan as safety_scan, policy_summary
except ImportError:  # pre-send/whole-repo imports without bestgaa/ on sys.path
    from bestgaa.deal_facts import (deal_facts, deal_title, premium_score,
                                    eligible_for_premium)
    from bestgaa.category_classifier import classify_category, load_rules
    from bestgaa.safety_filter import scan as safety_scan, policy_summary

URL_RE = re.compile(r"https?://[^\s<>\[\](){}\"']+", re.I)
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)
SHORT_PATH_RE = re.compile(r"^/[A-Za-z0-9_-]{2,24}/?$")

# Mirrored from main_bot_new (asserted equal in test_source_to_target.py).
AMAZON_SHORT_HOSTS = frozenset({"amzn.to", "amzn.eu", "amzn.in", "a.co",
                                "link.amazon", "link.amazon.in"})
AMAZON_HOSTS = frozenset({"amazon.in", "amazon.com"})
OUR_SHORTENERS = frozenset({"bitli.in", "ekaro.in", "clnk.in", "clnk.app", "ekaro.app",
                            "fktr.in", "myntr.it", "ajiio.in"})
# Domains we MINT at runtime: a bit.ly/is.gd link can equally be a stranger's,
# so it is kept only when its destination and attribution verify (the bot does
# exactly that); the planner says so instead of calling it ours.
RUNTIME_SHORTENERS = frozenset({"bit.ly", "is.gd"})
MERCHANTS = frozenset({"flipkart.com", "myntra.com", "ajio.com", "meesho.com", "shopsy.in",
                       "tatacliq.com", "nykaa.com", "croma.com", "reliancedigital.in",
                       "jiomart.com", "firstcry.com", "snapdeal.com", "decathlon.in",
                       "pepperfry.com", "limeroad.com", "paytmmall.com", "hypd.store"})
TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me"})
# The rest of the wrappers loot channels paste; the bot follows a known shortener
# by name, and an unknown host whose path is a single slug.
KNOWN_SHORTENERS = frozenset({"bitly.com", "tinyurl.com", "bitly.com", "j.mp", "t.co", "goo.gl", "buff.ly", "ow.ly",
                              "tidd.ly", "geni.us", "tinyurl.com", "cutt.ly", "rb.gy",
                              "t.ly", "tiny.cc", "shorturl.at", "is.gd", "v.gd", "snip.ly",
                              "linkredirect.in", "fkrt.co", "fkrt.cc", "fkrt.in", "myntr.in",
                              "myntr.it", "ajiio.co", "msho.in", "meesho.app", "bylink.in",
                              "mylink.store", "applink.adjust.com", "spoo.me", "da.gd",
                              "surl.li", "shrtco.de", "cli.re", "v.ht", "shorte.st",
                              "bilty.co", "bitl.in", "bittli.in"}) | OUR_SHORTENERS | RUNTIME_SHORTENERS | AMAZON_SHORT_HOSTS


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def _in(host: str, domains) -> bool:
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def _role(url: str) -> tuple[str, str]:
    """(role, one-line explanation of what the pipeline does with this link)."""
    host = _host(url)
    if not host:
        return "invalid", "not a usable link — it is never published"
    if _in(host, TELEGRAM_HOSTS):
        return "telegram", "a channel/group link, not a product: kept as text, never converted"
    if _in(host, OUR_SHORTENERS):
        return "ours", "already ours: verified (destination + publisher) and kept"
    if _in(host, RUNTIME_SHORTENERS):
        return ("runtime_shortener",
                "our runtime shortener: kept only if the destination and our attribution "
                "verify; a stranger's bit.ly/is.gd link is otherwise refused")
    if _in(host, AMAZON_SHORT_HOSTS):
        return ("amazon_short",
                "Amazon's own short wrapper: followed to the real /dp/ page, then our "
                "Associates tag is applied and the result is shortened with our shortener. "
                "The wrapper itself is never published (its code belongs to whoever made it)")
    if _in(host, AMAZON_HOSTS):
        if ASIN_RE.search(url):
            return ("amazon_product",
                    "an Amazon product page: our tag is stamped on it (and it is shortened)")
        return "amazon_other", "Amazon but not a product page: kept only if it resolves to a product"
    if _in(host, MERCHANTS):
        return ("merchant",
                "a known merchant page: converted through the affiliate network; if the "
                "network has no campaign, it is attributed to our publisher id or skipped")
    if _in(host, KNOWN_SHORTENERS):
        return ("wrapper",
                "a short wrapper: resolved to the real store page, then converted to ours")
    if SHORT_PATH_RE.match(urlparse(url).path or ""):
        return ("unknown_wrapper",
                "an unknown host with a shortener's shape: followed to the real page, then "
                "converted to ours (a brand-new wrapper still ends up as our link)")
    return ("direct", "a plain link: converted if the merchant is supported, otherwise skipped")


def _product_id(url: str) -> str:
    match = ASIN_RE.search(url)
    if match:
        return match.group(1).upper()
    return ""


def analyze(text: str, source: str = "", links=None, snapshot=None, channels=None,
            rules=None, now: float | None = None) -> dict:
    """One source post in, one full source→target verdict out. Pure and offline."""
    text = str(text or "")
    source = str(source or "").strip()
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    rules = rules if isinstance(rules, dict) else load_rules()
    urls = [u for u in dict.fromkeys(list(links or []) + URL_RE.findall(text))]

    facts = deal_facts(text) if text.strip() else {}
    price = facts.get("price") if facts else None
    discount = facts.get("discount") if facts else None
    result: dict = {
        "generated_at": now if now is not None else time.time(),
        "input": {"text": text, "source": source, "chars": len(text),
                  "links_found": len(urls)},
        "facts": {
            "price": price,
            "mrp": facts.get("mrp"),
            "discount": discount,
            "coupon": facts.get("coupon"),
            "final": facts.get("final"),
            "title": deal_title(text) if text.strip() else "",
            "quality_score": premium_score(text, price, discount),
            "premium_eligible": eligible_for_premium(text, price, discount),
        },
        "safety": {"allowed": True, "severity": "clean", "category": "",
                   "label": "", "reason": "", "version": ""},
        "links": [],
        "category": {"key": None, "label": "", "reason": "no_evidence", "evidence": [],
                     "accepted_by": [], "withheld_by": []},
        "identity": {"product_ids": [], "note": ""},
        "plan": {"known_source": bool(source), "targets": [], "excluded": [], "notes": []},
        "warnings": [],
    }

    # ---- 1. content safety (same policy as the bot and the bridge) ----------
    try:
        verdict = safety_scan(text)
        result["safety"] = {"allowed": not verdict["blocked"],
                            "severity": verdict["severity"],
                            "category": verdict["category"],
                            "label": verdict["label"],
                            "reason": verdict["reason"],
                            "version": verdict["version"]}
    except Exception as exc:  # noqa: BLE001 - the panel must still render
        result["safety"] = {"allowed": None, "severity": "unknown", "category": "",
                            "label": "", "reason": f"policy unavailable: {exc}", "version": ""}

    # ---- 2. the link plan --------------------------------------------------
    product_ids: list[str] = []
    for url in urls:
        role, why = _role(url)
        pid = _product_id(url)
        if pid:
            product_ids.append(pid)
        result["links"].append({
            "url": url, "host": _host(url), "role": role, "why": why,
            "product_id": pid,
            "published_as": "ours (verified at send)" if role == "ours" else
                            "our converted link (verified at send)",
            "never_published_as": role in {"amazon_short", "wrapper", "unknown_wrapper"},
        })
    result["identity"]["product_ids"] = list(dict.fromkeys(product_ids))
    if product_ids:
        result["identity"]["note"] = ("dedupe by product id: the same product from another "
                                      "source is recognised and never posted twice")
    elif urls:
        result["identity"]["note"] = ("the link is a wrapper, so the product id only exists "
                                      "after the bot resolves it — dedupe then uses that id; "
                                      "until then the post is deduped by source/message")
    else:
        result["identity"]["note"] = "no link in this post: nothing to convert or dedupe by product"

    # ---- 3. the shelf ------------------------------------------------------
    link_context = [u for u in urls]
    classification = classify_category(text, link_context, rules)
    category_key = classification.get("category")
    labels = {c["key"]: c.get("label", c["key"]) for c in rules.get("categories", [])}
    result["category"] = {
        "key": category_key,
        "label": labels.get(category_key, "") if category_key else "",
        "reason": classification.get("reason", ""),
        "evidence": classification.get("evidence", []),
        "scores": classification.get("scores", {}),
        "accepted_by": [], "withheld_by": [],
    }
    if not category_key:
        result["warnings"].append(
            "no category evidence: the post reaches the main/price feeds but is withheld "
            "from every category shelf (the classifier refuses to guess)")

    # ---- 4. the target plan ------------------------------------------------
    _plan(result, text, source, category_key, price, discount, snapshot, channels)

    # ---- 5. anything the operator should know ------------------------------
    if not text.strip():
        result["warnings"].append("empty post")
    if text.strip() and not urls:
        result["warnings"].append("no link: the bot cannot monetize this, so it is skipped")
    for link in result["links"]:
        if link["role"] == "telegram":
            result["warnings"].append("a Telegram channel link in the copy is not a product "
                                      "link and is not converted")
    if result["safety"]["allowed"] is False:
        result["warnings"].append(
            f"BLOCKED by the content policy ({result['safety']['category']}): this post "
            "never enters the queue — the refusal is recorded with this reason")
    if price is None and text.strip():
        result["warnings"].append(
            "no price could be read: price-band channels (Under99 / Under499) and premium "
            "ranking stay off for this post")
    return result


def _plan(result: dict, text: str, source: str, category_key, price, discount,
          snapshot: dict, channels) -> None:
    """Fill result['plan'] from the bot's published routing map."""
    price_targets = snapshot.get("price_targets") or {}
    source_targets = snapshot.get("source_targets") or {}
    tricks_sources = {str(s).lower() for s in (snapshot.get("tricks_sources") or [])}
    raw_filters = snapshot.get("category_filters") or {}
    filters = {str(name).lower(): {str(c).lower() for c in cats}
               for name, cats in raw_filters.items()}
    # Keep the channel's real spelling for display ("ElectroShelf", not "electroshelf").
    display_name = {str(name).lower(): str(name) for name in raw_filters}
    known_targets = [str(t) for t in (snapshot.get("targets") or [])]
    plan = result["plan"]

    def add(name: str, phase: str, why: str) -> None:
        if not name:
            return
        if any(t["name"] == name for t in plan["targets"]):
            return
        plan["targets"].append({"name": name, "phase": phase, "why": why})

    def drop(name: str, why: str) -> None:
        if name and not any(t["name"] == name for t in plan["excluded"]):
            plan["excluded"].append({"name": name, "why": why})

    if not snapshot:
        plan["notes"].append(
            "no routing_snapshot.json on this host: the bot publishes it at startup "
            "(main_bot_new), so the target plan cannot be predicted here. Everything "
            "else on this page is still exact.")
        plan["known_source"] = False
        return

    is_tricks = source.lower() in tricks_sources
    if source:
        mapped = next((v for k, v in source_targets.items() if str(k).lower() == source.lower()),
                      None)
        plan["known_source"] = mapped is not None
        if mapped is None:
            plan["notes"].append(
                f"'{source}' is not a configured source: the bot never reads it, so this "
                "post cannot post anywhere until the source is added to a channel")
            return
        for target in mapped:
            add(str(target), "source route", "this source is routed here")
    else:
        plan["known_source"] = False
        plan["notes"].append(
            "no source named: showing the price/category plan only. Send the source "
            "channel to see the exact route.")

    # The main feed is guaranteed for every non-Tricks post (user rule).
    if is_tricks:
        add(price_targets.get("tricks", ""), "guaranteed",
            "Tricks sources always post to the Tricks channel")
    else:
        add("LootZoneIndia11", "guaranteed",
            "every accepted deal reaches the main feed, whatever its price")
        main = price_targets.get("power", "")
        if main:
            add(main, "extra", "PowerLoots takes every deal from a non-Tricks source")

    # Price bands.
    under99, under499 = price_targets.get("under99", ""), price_targets.get("under499", "")
    if price is not None:
        if price <= 99:
            add(under99, "price band", f"₹{price} is at or below ₹99")
            add(under499, "price band", f"₹{price} is at or below ₹499")
        elif price <= 499:
            add(under499, "price band", f"₹{price} is at or below ₹499")
        else:
            drop(under99, f"₹{price} is above ₹99")
            drop(under499, f"₹{price} is above ₹499")
    else:
        plan["notes"].append("no price: the ₹-band channels are not added for a single deal")

    # Premium.
    premium = price_targets.get("premium", "")
    if premium:
        if eligible_for_premium(text, price, discount):
            add(premium, "premium", "an 80%+ discount, a sub-₹99 deal with a real discount, "
                                    "or a strong ₹100–499 deal")
        else:
            drop(premium, "not premium-grade: needs 80%+ off, a sub-₹99 deal with 50%+ off, "
                          "or ₹100–499 with 70%+ off / a lowest-price claim")

    # Category shelves (dashboard channels with a category filter).
    if filters:
        for name in list(dict.fromkeys(known_targets + list(raw_filters))):
            wanted = filters.get(str(name).lower())
            if not wanted:
                continue
            label = display_name.get(str(name).lower(), str(name))
            if category_key and str(category_key).lower() in wanted:
                add(label, "shelf", f"a {category_key} shelf")
            else:
                drop(label, f"withholds this post: the shelf takes "
                           f"{', '.join(sorted(wanted))} and this is "
                           f"{category_key or 'unclassified'}")

    # A price/category exclusion beats a generic source route: the router adds the
    # priced bands to the source map, then removes them again when the deal is
    # outside the band. Showing both would read as a contradiction.
    excluded_names = {t["name"] for t in plan["excluded"]}
    plan["targets"] = [t for t in plan["targets"]
                       if t["name"] not in excluded_names or t["phase"] == "guaranteed"]

    if plan["targets"]:
        result["category"]["accepted_by"] = [t["name"] for t in plan["targets"]
                                             if t["phase"] == "shelf"]
        result["category"]["withheld_by"] = [{"name": t["name"], "why": t["why"]}
                                             for t in plan["excluded"]
                                             if "shelf" in t["why"] or "withholds" in t["why"]]
    plan["notes"].append(
        "predicted from the bot's published routing map; the send-time gates (our link "
        "verified, duplicate product, channel access, daily caps) still apply")


def load_snapshot(path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def default_snapshot() -> dict:
    """The snapshot the running bot publishes, next to channels.json."""
    here = Path(__file__).resolve().parent.parent
    candidates = []
    env = os.getenv("CHANNELS_JSON_PATH", "").strip()
    if env:
        candidates.append(Path(env).parent / "routing_snapshot.json")
    candidates.append(here / "bestgaa" / "routing_snapshot.json")
    for path in candidates:
        snapshot = load_snapshot(path)
        if snapshot:
            return snapshot
    return {}


def policy() -> dict:
    try:
        return policy_summary()
    except Exception as exc:  # noqa: BLE001
        return {"version": "", "categories": [], "error": str(exc)}
