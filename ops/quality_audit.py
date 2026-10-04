#!/usr/bin/env python3
"""Offline quality auditor for the deal pipeline (read-only, no network).

The bot guarantees four things on every post: nothing of ours is added, the only
links are OUR monetized links, no product is posted twice, and no eligible post
is lost. This script proves it on the live database - run it any time (or from
ops/diagnose.sh) and it reports every post that broke a guarantee, with queue ids,
so a regression is visible the day it happens instead of in channel feedback.

    python3 ops/quality_audit.py                      # last 200 posts
    python3 ops/quality_audit.py --db <path> --limit 500 --json
    python3 ops/quality_audit.py --strict             # exit 1 on any finding

It opens the database read-only and never writes to it or to Telegram.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

RUPEE = "\u20b9"
OURS_MARKERS = (
    # Anything below was invented by an older template of ours. None of it may
    # appear in a post: the source's own words are the content, so a marker means
    # our old decoration came back. These phrases are also removed from SOURCE
    # posts while cleaning, so seeing one in a published post can only mean we
    # wrote it - there is no false positive to allow for.
    "Verified deals", "Join for daily loot", "Grab fast", "Enjoy Deals",
    "DEALS OF THE DAY", "MEGA DEAL LIST", "PREMIUM LOOT PICK", "Latest deal",
    "TODAY'S HOT DEALS", "POWER LOOT ALERT", "Price/stock may change",
    "UNDER ₹499 •", "BANK / CARD OFFER", "LOOT ZONE — INDIA",
)
ANY_LINK = re.compile(r"https?://\S+")
# What "our link" means for this bot, in the bot's own terms:
#   * our shortener (the one the user owns) or our EarnKaro / gop.im output,
#   * a link the bot's OWN link_cache minted for that deal (the runtime Bitly /
#     is.gd shortener pass and the EarnKaro API both register there),
#   * an Amazon page carrying OUR tag - the pipeline publishes those when no
#     shortener/affiliate route was used, and the tag IS the monetization,
#   * a page carrying OUR EarnKaro publisher id (affExtParam2=5478322),
#   * a clean merchant page for a store we cannot monetize (a deal must not be
#     lost because a campaign is missing),
#   * our own channels and the Loots Family folder.
# Everything else - the source's own short link, someone else's shortener, a link
# tagged to another publisher - is a link that should never have been sent.
#
# IMPORTANT (2026-10-04): the hardcoded pattern below is HISTORY, and the runtime
# shorteners are not. Since 2026-09-06 every list post carries OUR Bitly links by
# design, and the EarnKaro API mints fresh ekaro.in/bitli.in links per deal - none
# of which that literal can name. So a bare "bit.ly is a third-party shortener"
# rule flags healthy posts, and a false alarm is exactly how a real one gets
# ignored. Ownership is therefore judged from the DATA (link_cache + the token's
# publisher id) instead of from a domain list that can only describe yesterday.
OUR_SHORTENER = re.compile(r"https?://(?:(?:www\.)?bitli\.in/TqmFyPp|(?:www\.)?bitlyskj\.(?:com|in|net)/)", re.I)
OUR_HOSTS = re.compile(r"https?://(?:[^/\s]*earnkaro\.com/|gop\.im/)", re.I)
# Shortener hosts the BOT ITSELF mints at runtime (Bitly with OUR token, is.gd as
# the outage fallback). A link on one of these is ours only when a link_cache row
# says the bot produced it: other channels post bit.ly links too.
OUR_RUNTIME_SHORTENER_HOSTS = ("bit.ly", "is.gd")
# The affiliate network's OWN short domains (what the EarnKaro/Affiliaters
# converter mints) and our HYPD share links. These are MONETIZED links, so the
# summary must not file them under "clean merchant" - that bucket means "kept on
# purpose, earns nothing".
OUR_NETWORK_SHORT = re.compile(
    r"https?://(?:[^/\s]*\.)?(?:ekaro\.(?:in|app)|clnk\.(?:in|app)|bitli\.in|fktr\.in|"
    r"myntr\.it|ajiio\.in|cuelinks\.com|l\.ead\.me|affiliaters\.in|hypd\.store)/", re.I)
OUR_T_ME = re.compile(r"https?://t\.me/(?:addlist/|LootZoneIndia11\b|SecretLootIndia1\b)", re.I)
AMAZON_HOST = re.compile(r"^(?:www\.|m\.)?amazon\.[a-z.]{2,8}$", re.I)
AMAZON_SHORT = re.compile(r"^(?:www\.)?amzn\.(?:to|in)$", re.I)
FOREIGN_SHORTENERS = re.compile(
    r"https?://(?:[^/\s]*\.)?(?:bit\.ly|bitly\.com|tinyurl\.com|tinyurl\.net|cutt\.ly|t\.cg"
    r"|gpt\.sh|shorturl\.at|is\.gd|rb\.gy|ow\.ly|rebrand\.ly|tly\.in|s\.id/)", re.I)
# Publisher ids that are never ours. Flipkart's pid= is a PRODUCT id, so it is
# deliberately absent here.
# One capture group on purpose: callers use findall() and expect plain strings.
# The `cmpid=` branch only counts when the campaign is the AFF_ affiliate form
# (`cmpid=AFF_deals101`, seen live 2026-10-04) - a plain marketing cmpid is not
# somebody's commission. The value keeps its AFF_ prefix, which no publisher id
# ever equals, so it can only ever be read as "not ours".
AFFILIATE_ID_RE = re.compile(
    r"[?&](?:(?:affid|aff_id|pubid|publisherid|associateid|affextparam2|refid|clickid)="
    r"|cmpid=(?=aff[_-]))([^&#]*)", re.I)
FOREIGN_PROMO = re.compile(r"join\s+(?:this\s+)?channel|subscribe\s+to|t\.me/\+|startapp\.bot", re.I)
PRICE_RE = re.compile(rf"{RUPEE}\s*(\d[\d,]*)")
LIST_MARKER_RE = re.compile(r"(?im)^\s*(?:deal\s*\d+|\d+\s*[.)])")
POLICY_SKIPS = (
    "already posted", "duplicate deal", "duplicate product", "night window",
    "STALE DROP", "already covered", "no monetizable", "no eligible targets",
    "not a deal", "skip list", "superseded", "not worth", "confirmed dead merchant",
    # USER RULE (2026-09-05): a deal whose every link is a dead shortener is skipped on
    # purpose ("asalu link yeh ledu") - it is policy, not a lost post.
    "nothing buyable to post", "not programme-safe", "shopping target unresolved",
)


def link_host(url: str) -> str:
    match = re.match(r"https?://([^/\s?#]+)", url or "", re.I)
    return (match.group(1) if match else "").lower()


# USER REPORT 2026-10-04 ("product open avuthundi kaani adi mana links kaadu
# ... commission asalu ravatledu"): the Flipkart family accepts the network's
# own publisher parameter ON the merchant page - `affExtParam2=<publisher>` is
# exactly the shape the EarnKaro converter returns for this account (the bot's
# earnkaro_output_kind() accepts it as a conversion). So a Flipkart/Myntra page
# published WITHOUT it is not "a clean merchant page kept on purpose": it is a
# link that opens the product and pays nobody, and it can be fixed. Kept in one
# place so the count and the bot's own stamping rule stay in step.
EK_ATTRIBUTION_HOSTS = ("flipkart.com", "myntra.com")
FOREIGN_ATTRIBUTION_PARAMS = frozenset((
    "affid", "aff_id", "affid1", "affid2", "affextparam1", "affextparam2",
    "affiliate", "affiliate_id", "affiliateid", "subid", "sub_id",
))


def _query_pairs(url: str) -> dict[str, list[str]]:
    pairs: dict[str, list[str]] = {}
    for chunk in urlparse(url).query.split("&"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            pairs.setdefault(key.strip().lower(), []).append(unquote(value.strip()))
    return pairs


def why_unattributed(url: str, our_publisher: str = "",
                     our_links: "set[str] | tuple[str, ...]" = ()) -> str | None:
    """A merchant page that CAN pay us but was posted without our id.

    None when the link is not on an attributable host, when it already carries
    OUR publisher id, when it belongs to somebody else (the foreign rule speaks
    first), or when the bot's own link_cache minted it. Anything else on these
    hosts is a click that earns nothing - the exact reason commissions were
    missing while the product still opened.
    """
    if not our_publisher:
        return None
    if our_links and url in our_links:
        return None
    host = link_host(url)
    if not any(host == h or host.endswith("." + h) for h in EK_ATTRIBUTION_HOSTS):
        return None
    pairs = _query_pairs(url)
    values = [v for v in pairs.get("affextparam2", []) if v]
    if our_publisher in values:
        return None  # already ours
    foreign = [(key, v) for key in FOREIGN_ATTRIBUTION_PARAMS
               for v in pairs.get(key, []) if v and v != our_publisher]
    if not foreign:
        # `cmpid=AFF_<slug>` is the affiliate campaign of an account (see
        # AFFILIATE_ID_RE): the page is somebody's, not a page nobody is paid
        # for. A plain marketing cmpid (cmpid=summer-sale) is not, and the page
        # keeps counting as unattributed.
        foreign = [("cmpid", v) for v in pairs.get("cmpid", [])
                   if re.match(r"aff[_-]", v, re.I)]
    if foreign or any(v for v in pairs.get("tag", [])):
        return None  # somebody else's attribution: that is a FOREIGN link
    return ("can carry our publisher id (%s) but was posted without it - the product "
            "opens and NOBODY is paid" % our_publisher)


def why_not_our_link(url: str, our_tag: str = "", our_publisher: str = "",
                     our_links: "set[str] | tuple[str, ...]" = ()) -> str | None:
    """Why this published link is NOT one of ours (None means it is fine).

    `our_links` is the set of affiliate_urls the bot's own link_cache minted, and
    `our_publisher` is the EarnKaro account the token pays. Both make the verdict
    a fact about this install rather than a guess from a domain list; both are
    optional so a caller with only the tag still gets the old, safe answer.
    """
    if OUR_SHORTENER.match(url) or OUR_HOSTS.match(url) or OUR_T_ME.match(url):
        return None
    if our_links and url in our_links:
        # The bot minted this exact link for a deal (Bitly/is.gd/EarnKaro/HYPD/
        # pass-through) - the same table the pipeline's provenance gate consults.
        return None
    host = link_host(url)
    if AMAZON_SHORT.match(host):
        return "the source's own Amazon short link was posted instead of ours"
    if FOREIGN_SHORTENERS.match(url):
        if any(host == h or host.endswith("." + h) for h in OUR_RUNTIME_SHORTENER_HOSTS):
            return ("a %s short link with no link_cache row minting it (either it predates "
                    "the cache or it is a third-party shortener)" % host)
        return "a third-party shortener leaked into the post"
    query = urlparse(url).query.lower()
    pairs = dict()
    for chunk in query.split("&"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            pairs.setdefault(key.strip(), []).append(unquote(value.strip()))
    tags = [v for v in pairs.get("tag", []) if v]
    if not our_tag:
        # TAGLESS: clean Amazon link (no tag) is ours, any tag is foreign
        ours = not tags
    else:
        ours = our_tag.lower() in [x.lower() for x in tags]
    all_ids = [v for v in AFFILIATE_ID_RE.findall(unquote(url)) if v]
    # OUR publisher id is attribution, not a stranger's: the EarnKaro API returns
    # direct Flipkart-family URLs carrying affExtParam2=5478322, and those are
    # exactly the links we want posted.
    foreign_ids = [v for v in all_ids if not (our_publisher and v == our_publisher)]
    if AMAZON_HOST.match(host):
        if not ours:
            return "an Amazon link without our tag reached a channel"
        if foreign_ids:
            return "our tag glued to somebody else's id (%s)" % ",".join(sorted(set(foreign_ids)))[:40]
        return None
    if tags and not ours:
        return "tagged to somebody else (%s)" % ",".join(sorted(set(tags)))[:40]
    # USER QUESTION 2026-10-04 ("idi manadenaa link"): an EarnKaro-minted
    # Flipkart-family link carries affid=<the network's account> and
    # affExtParam2=<the publisher who is paid> - exactly how Flipkart documents
    # its affiliate URLs. When that publisher id is OURS the link is ours, and
    # the network's own affid/cmpid/mcn are the route the commission travels -
    # not a stranger's id. This is the same verdict whose_link() gives in
    # ops/earnkaro_check.py ("ours" when affExtParam2 == our publisher), and the
    # pipeline itself accepts such a link (earnkaro_output_kind -> publisher).
    # The audit was the only place saying otherwise, so only the audit changes.
    if foreign_ids and our_publisher and our_publisher in all_ids:
        return None
    if foreign_ids:
        return "carries somebody else's affiliate id (%s)" % ",".join(sorted(set(foreign_ids)))[:40]
    unattributed = why_unattributed(url, our_publisher, our_links)
    if unattributed:
        return unattributed
    return None  # unmonetizable store, clean page: publishing it is the policy


def classify_link(url: str, our_tag: str = "", our_publisher: str = "",
                  our_links: "set[str] | tuple[str, ...]" = ()) -> str:
    """'our-short' | 'our-publisher' | 'unattributed' | 'clean-merchant' | 'foreign'.

    This is the summary the user actually asks for ("mana links perfectga post
    chesthunda ledaa?"): every published link is either a short link WE minted,
    OUR tag/publisher id, a clean merchant page kept on purpose, or a link that
    should never have left - counted, so the answer is a number, not an opinion.
    """
    if why_unattributed(url, our_publisher, our_links):
        return "unattributed"
    if why_not_our_link(url, our_tag, our_publisher, our_links):
        return "foreign"
    if (OUR_SHORTENER.match(url) or OUR_HOSTS.match(url) or OUR_T_ME.match(url)
            or OUR_NETWORK_SHORT.match(url)):
        return "our-short"
    if our_links and url in our_links:
        return "our-short"
    if AMAZON_HOST.match(link_host(url)):
        return "our-publisher"
    query = urlparse(url).query.lower()
    pairs = {}
    for chunk in query.split("&"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            pairs.setdefault(key.strip(), []).append(unquote(value.strip()))
    if pairs.get("tag") or (our_publisher and our_publisher in [v for v in
                                                               AFFILIATE_ID_RE.findall(unquote(url)) if v]):
        return "our-publisher"
    return "clean-merchant"


def flag(kind: str, detail: str, queue_id=None, target=None) -> dict:
    out = {"kind": kind, "queue_id": queue_id, "detail": detail}
    if target:
        out["target"] = target
    return out


def body_fingerprint(text: str) -> str:
    """The post's own words, with links and decoration removed.

    Two rows whose fingerprints match are the same content published twice, which
    is exactly the defect the user never wants to see again - so it is checked on
    the way OUT as well as on the way in.
    """
    body = ANY_LINK.sub(" ", text or "")
    body = re.sub(r"\s+", " ", body)
    return re.sub(r"[^a-z0-9%\u20b9 ]", "", body.lower()).strip()


def product_line(text: str) -> str:
    """The first line that names the product - what a reader actually sees."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped and not ANY_LINK.search(stripped):
            return re.sub(r"[^a-z0-9 \u20b9%.]", "", stripped.lower()).strip()[:80]
    return ""


# Words every loot channel puts in front of a product. They carry no identity,
# so a signature built from a line that still contains them would match two
# different products - which would make the auditor invent duplicates.
# (The old NOISE_WORDS set lived here and doubled as the product-identity stop list.
# It is gone on purpose: identity stops now sit in _AUDIT_SIG_STOP_WORDS, which keeps
# "men"/"women"/"unisex" as MEANINGFUL words - a men's shirt and a women's shirt are
# two different deals, and swallowing those words is how a dedup rule eats coverage.)

# >>> BEGIN SYNCED product-identity (generated by ops/sync_identity.py)
# DO NOT EDIT THIS BLOCK BY HAND. It is copied from
# bestgaa/main_bot_new.py by ops/sync_identity.py, because an auditor with a
# different idea of "which product is this?" than the bot cannot report
# anything true. Run: python3 ops/sync_identity.py
# ---------------------------------------------------------------------------
# A number in a product name is either the thing that MAKES it that product
# (1.5 Ton, 5 Star, 128GB, 55-inch) or a spec a channel may not type at all
# (42H playtime, 5000mAh, 1080p, 4K). Identity is brand + model + variant +
# the numbers that change the product - never a slice of the phrase.
_AUDIT_SIG_STOP_WORDS = frozenset("""
deal deals dealz offer offers dhamaka dhamal sale salez loot loots price mrp discount off
save savings grab hurry now today daily best top hot new buy shop link links here below click
free shipping delivery cod return warranty genuine flash super mega amazing awesome alert
in india official telegram whatsapp channel group join follow subscribe share forward for
the a an and or of to on in at by with your our this that it is are be get got have has
""".split())
# A number in a product name is either the thing that MAKES it that product
# (1.5 Ton, 5 Star, 128GB, 55-inch, 5 Burner) or a spec a channel may or may not
# bother to type (42H playtime, 5000mAh, 1080p, 4K, 5G). The first group is the
# identity; the second is dropped - demanding that two captions of one product spell
# their battery life alike is exactly how the same deal gets posted twice.
_AUDIT_SIG_VARIANT_UNITS = (
    "gb tb mb kb l ltr liter liters litre litres ml kg ton tons star stars inch "
    "inches in ft hp kva burner burners slice slices tray trays door doors person "
    "persons blade blades"
).split()
_AUDIT_SIG_SPEC_UNITS = (
    "h hr hrs hour hours min mins sec secs mah wh w kw a v p k g fps hz px mm cm m "
    "db rpm mp nit nits lumen lumens mbps gbps byte bytes watt watts"
).split()
_AUDIT_SIG_VARIANT_RE = re.compile(
    r"\b(\d{1,4}(?:\.\d+)?)[\s_-]*("
    + "|".join(map(re.escape, sorted(_AUDIT_SIG_VARIANT_UNITS, key=len, reverse=True))) + r")\b", re.I)
# Words that name a real variant of a model line. "Galaxy S23" and "Galaxy S23 FE"
# share every number and are two different phones, so these belong to the identity.
# Only words that name a genuinely different product line. "smart"/"air"/"elite" are
# marketing adjectives here and there, and a copy that drops one must not turn the
# same watch into a different product - so the list stays short and factual.
# The gender/audience words are here for the same reason as "pro"/"fe": a
# "Nivea MEN Face Wash" and a "Nivea WOMEN Face Wash" are two different products
# at the same price, and with no model number to separate them they used to sign
# identically - so the second one was skipped as a duplicate and that deal never
# reached the channel.
_AUDIT_SIG_VARIANTS = frozenset("""
pro plus max ultra lite neo fe se mini prime classic edge fold flip turbo
men mens women womens kids boys girls unisex
""".split())
# Words that put a number in front of them into a model name: "Pro 4" and "Model
# 2600" are the product, "2023" at the end of a headline is the launch year.
_AUDIT_SIG_QUALIFIERS = _AUDIT_SIG_VARIANTS | frozenset("model series gen generation version".split())
# "by <word>" names the brand ("Airdopes 141 by boAt") EXCEPT when the word in
# front says otherwise: "powered by Helio" names a chipset, not the maker.
_AUDIT_SIG_BY_NON_BRAND = frozenset(
    "powered brought inspired sponsored posted shared sent curated verified".split())
_AUDIT_SIG_AMOUNT_RE = re.compile(
    r"(?i)[\u20b9$]\s*[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:%|percent|off)\b|"
    # "70%" followed by a space failed the trailing \b (a percent sign is not a
    # word char), so the discount stayed in the identity and two copies of one
    # deal signed differently whenever the channels typed the discount apart.
    r"\b\d{1,3}\s*%|"
    # A bare number sitting immediately in front of the discount IS the price
    # ("Collagen powder 299 (70% off)"), whatever marker the channel omitted.
    r"\b[\d,]{2,}(?=\s*\(?\s*\d{1,3}\s*(?:%|percent))|"
    # A price written WITHOUT the rupee sign is still a price, not a model
    # number: "@298", "at 167", "Rs 180", "220/-". Leaving these in made the
    # SAME product signed differently depending on how the channel typed its
    # price, so the duplicate slipped through. (A number that is genuinely part
    # of the product - "10KG", "2000ml" - carries a unit and is keyed elsewhere.)
    r"(?:@|\bat\b|\brs\.?|\binr)\s*[\d,]{2,}(?:\.\d+)?\b|\b[\d,]{2,}\s*/-|"
    r"\b(?:mrp|mrp\.?|regular\s+price|list\s+price|strike\s+price)\b\s*[:\-]?[^,|;\n]*")


# The price this LINE quotes, however the channel typed it. Deliberately a small
# self-contained reader rather than a call to parse_price(): this block is mirrored
# verbatim into ops/quality_audit.py by ops/sync_identity.py, so it must not depend
# on anything outside itself or the auditor stops importing.
_AUDIT_SIG_PRICE_RE = re.compile(
    r"(?i)(?:[\u20b9$]|\brs\.?|\binr|@|\bat\b)\s*([\d,]{2,})(?:\.\d+)?\b"
    r"|\b([\d,]{2,})\s*/-"
    r"|\b([\d,]{2,})(?=\s*\(?\s*\d{1,3}\s*(?:%|percent))")


def _sig_line_price(line: str) -> int | None:
    match = _AUDIT_SIG_PRICE_RE.search(line or "")
    if not match:
        return None
    raw = next((g for g in match.groups() if g), "")
    try:
        value = int(str(raw).replace(",", ""))
    except ValueError:
        return None
    return value if 1 <= value <= 1_000_000 else None


def _audit_sig_tokens(line: str) -> list[str]:
    """Lower-case tokens with punctuation trimmed off the ENDS only.

    Trimming ends instead of deleting every symbol keeps a model number whole:
    "WH-CH720N" stays one token and "1.5 Ton" stays one number, instead of
    splitting into a stray "1" and "5" that read as two different products.
    """
    named = _AUDIT_SIG_AMOUNT_RE.sub(" ", line or "")
    named = re.sub(r"(?i)\b(?:%|percent|off)\b", " ", named)
    out = []
    for token in named.split():
        token = re.sub(r"^[^\w.]+", "", token)
        token = re.sub(r"[^\w.]+$", "", token).strip(".")
        if token and re.search(r"[A-Za-z0-9]", token):
            out.append(token.lower())
    return out


# Ordinary descriptive words. A phrase built ONLY from these ("Men Cotton
# Shirt", "hair oil set") describes a category, not a product, so it needs the
# longer floor before it may be used to skip a repeat.
_AUDIT_SIG_GENERIC_WORDS = frozenset("""
men mens women womens kids boys girls baby unisex adult
hair face body skin lip eye nail hand foot head neck
phone mobile laptop tablet tv led smart wireless bluetooth usb
lunch dinner tea coffee water milk rice

cotton silk leather steel plastic glass wooden metal rubber silicone
shirt tshirt pant jeans saree kurti dress top jacket shoes sandals slippers
oil soap cream powder shampoo lotion gel wash paste
bottle box case cover bag pouch set combo pack piece pieces
watch band strap cable charger adapter holder stand mat mop broom
kitchen home office travel sports gaming
small medium large xl xxl free size regular fit slim
new best top premium quality original genuine
""".split())


# Ordinary product nouns and descriptive adjectives. A phrase built only from
# these plus _AUDIT_SIG_GENERIC_WORDS names a CATEGORY ("Running Shoes", "Shirt A"),
# never one product, so it must clear the length floor before it may be used
# to skip a repeat.
_AUDIT_SIG_PRODUCT_NOUNS = frozenset("""
shoes sneakers boots sandal sandals slipper slippers flipflop
shirt tshirt shirts pant pants trouser trousers jeans short shorts
kurta kurti saree dress top tops jacket coat sweater hoodie
bottle flask jar container tiffin lunchbox casserole
mixer grinder kettle cooker pan pot tawa knife spoon plate bowl
headphone headphones earphone earphones earbuds neckband speaker
watch smartwatch band tracker
bag backpack luggage trolley wallet purse belt
running walking sports casual formal party daily regular
trimmer shaver dryer straightener iron fan heater cooler lamp bulb
sheet curtain pillow blanket mattress towel mat rug carpet
""".split())


def _product_identity(line: str) -> tuple[str, ...] | None:
    """The few tokens that decide WHICH product a headline names, as a SET.

    Not a slice of the phrase: "boAt Airdopes 141 TWS Earbuds" and "boAt Airdopes
    141 True Wireless Earbuds, 42H Playtime" are one product wearing different
    adjectives, while "Airdopes 141" and "Airdopes 131" are two products differing
    by nothing but the model number. Hashing the first eight words gets BOTH wrong -
    the duplicate survives and two long-named products collapse into a lost deal.

    So the identity is: the brand word, the model numbers (with the word in front of
    them when they are short, "Pro 4" and "Pro4" being one product), the variant
    qualifiers (pro / fe / max) and the numbers that change the product (capacity,
    tonnage, star rating, size, burner count). Extra marketing words then cost
    nothing, and a different model number can never be skipped as "already posted".
    With no number to hold on to (a shirt, a handbag) every product word has to
    agree instead - the conservative answer that loses a duplicate rather than a deal.
    """
    # A bare number that is simply the line's PRICE is not a model number.
    # "Nutriburst Collagen powder 299" and "Nutriburst Collagen powder @ 299"
    # are one product; only the second spelling was recognised as money, so the
    # first signed "299" as a model and the duplicate got published twice.
    line_price = _sig_line_price(line or "")
    raw = [re.sub(r"[-_]", "", tok) for tok in _audit_sig_tokens(line)]
    # A URL is not part of the product's NAME. When the source writes the link
    # on the same line ("Shirt A 599 https://a.com/x") the host used to become
    # an identity word, which both invented an identity for a generic phrase
    # and made the same product hash differently once its link changed.
    raw = [w for w in raw
           if not w.startswith(("http", "www")) and "/" not in w and "." not in w]
    words = [w for w in raw if not w.isdigit() and w not in _AUDIT_SIG_STOP_WORDS]
    if len(words) < 2:
        return None
    spec_units = frozenset(_AUDIT_SIG_SPEC_UNITS)
    variant_units = frozenset(_AUDIT_SIG_VARIANT_UNITS)
    ids = {f"{number}{unit}".lower().replace(" ", "").replace("-", "")
           for number, unit in _AUDIT_SIG_VARIANT_RE.findall(line or "")}
    digit_cores = {re.sub(r"\D", "", value) for value in ids}
    models = set()
    for index, token in enumerate(raw):
        if token in _AUDIT_SIG_STOP_WORDS:
            continue
        digits = re.sub(r"\D", "", token)
        if not digits or len(digits) < 1 or len(digits) > 6:
            continue                       # a phone number or a year is not a model
        glued = re.match(r"^(\d{1,6})([a-z]{1,6})$", token)
        if glued:                          # 42h, 1080p, 55inch, 5g
            unit = glued.group(2)
            if unit in spec_units or unit in variant_units:
                continue                   # already handled as a spec or a size id
        if re.match(r"^[a-z]{1,7}\d{1,6}[a-z]{0,3}$", token):
            models.add(token)              # s23, ch720n, pro4, m14
            continue
        if token.isdigit():
            following = raw[index + 1] if index + 1 < len(raw) else ""
            if following in spec_units or following in variant_units:
                continue                   # "128 GB" and "42 H" are keyed above
            previous = raw[index - 1] if index > 0 else ""
            if previous in _AUDIT_SIG_QUALIFIERS:
                models.add(previous + digits)   # "Pro 4" / "Model 2600" is the model
                continue
            if len(digits) < 2 or (len(digits) == 4 and 1900 <= int(digits) <= 2099):
                continue                   # a quantity, or a launch year - not an id
            if line_price is not None and int(digits) == line_price:
                continue                   # that is the price, however it was typed
            if digits in digit_cores:
                continue                   # the number is already inside a size id
            models.add(token)
    variants = {t for t in raw if t in _AUDIT_SIG_VARIANTS}
    if not ids and not models:
        # No number to hold on to, so every product word has to agree. THREE
        # words is enough when they are long enough to be a real product name
        # ("Nutriburst Collagen powder"): demanding four made a named product
        # with no model number un-dedupable, which is how the same collagen
        # powder went out twice under two different banner words. A short
        # category phrase ("hair oil set") still fails the length test below.
        # USER REPORT (2026-09-06): "Ergonomic Dustpan @ 55" went out TWICE in
        # the same channel. Two long, specific words ARE a product name; the
        # three-word floor left every such post with no identity at all, so the
        # per-channel repeat guard never ran on them. Two words are accepted
        # when they are long and specific enough not to be a category phrase
        # ("hair oil", "phone case" stay un-keyed via the length test below).
        if len(words) < 2:
            return None                    # a category phrase is not an identity
        # REGRESSION GUARD (2026-09-06): "Cello Lunch Box" is three real product
        # words but only 15 characters, so an 18-char floor gave it no identity
        # and the same lunch box could post twice. THREE words are already
        # specific enough - the floor exists to reject two-word category phrases
        # ("hair oil"), not to reject short real names. Four+ words are always
        # specific. Only the two-word case still needs the length test.
        # The floor exists to reject GENERIC phrases ("Men Cotton Shirt",
        # "hair oil") that many different products share - keying on those
        # would suppress real deals. It must not reject a short but SPECIFIC
        # name: "Cello Lunch Box" is 15 characters and names one product, and
        # an 18-char floor left it un-dedupable, which is how the same lunch
        # box could post twice.
        # A brand-like word - one that is not an ordinary descriptive word -
        # makes the phrase specific regardless of its length.
        # The floor rejects GENERIC phrases ("Men Cotton Shirt", "hair oil")
        # that many products share; keying on those would suppress real deals.
        # It must not reject a SHORT but specific name. "Cello Box" is nine
        # characters, yet "cello" is a brand - one product, and without an
        # identity it could post twice, which is the defect the user reported.
        # So a phrase that pairs a brand-like word with a descriptive one is
        # specific enough at two words; only all-generic phrases need length.
        # The floor rejects GENERIC phrases ("Men Cotton Shirt", "Running
        # Shoes") that many products share; keying on those would suppress
        # real deals. It must not reject a SHORT but specific name: "Cello
        # Box" is nine characters, yet "cello" is a BRAND - one product - and
        # with no identity it could post twice.
        #
        # A brand is recognised as a word that is neither descriptive nor a
        # known product noun: "cello", "milton", "dabur", "nike". "Running
        # Shoes" and "Shirt A" have no such word, so they keep the floor.
        basis = " ".join(sorted(set(words)))
        # A URL is never a brand. When a source writes the link on the same
        # line as the name ("Shirt A 599 https://a.com/x"), "https" and the
        # host would otherwise read as brand words and give a generic
        # category phrase an identity it must not have.
        # A single letter is a list marker or a size ("Shirt A", "Shirt B"),
        # never a brand: treating it as one gave every row of a three-shirt
        # roundup its own identity and broke the multi-product rule.
        brandish = [w for w in words
                    if len(w) > 1
                    and w not in _AUDIT_SIG_GENERIC_WORDS and w not in _AUDIT_SIG_PRODUCT_NOUNS]
        if brandish and len(words) >= 2:
            return ("W", basis)
        floor = 15 if brandish else 18
        return None if len(basis) < floor else ("W", basis)
    # The brand is normally the first product word, but "Airdopes 141 by boAt"
    # and "boAt Airdopes 141" are ONE product: an explicit "by <maker>" names
    # the brand outright and wins over word order, so the reordered copy can
    # never slip past dedup as a second post. "powered by Helio" and friends
    # name a component, not the maker, and are ignored.
    brand = words[0]
    for index, token in enumerate(raw[:-1]):
        if token == "by" and (index == 0 or raw[index - 1] not in _AUDIT_SIG_BY_NON_BRAND):
            candidate = raw[index + 1]
            if candidate in words:
                brand = candidate
                break
    parts = ["M", brand, " ".join(sorted(models | ids))]
    if variants:
        parts.append(" ".join(sorted(variants)))
    return tuple(parts)
# <<< END SYNCED product-identity


def targets_of(row) -> list[str]:
    """The channels a queue row actually went to (targets_json, defensively)."""
    try:
        targets = json.loads(row["targets_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return [str(t) for t in targets if t]


# ---------------------------------------------------------------------------
# The same product-identity rule the bot uses (main_bot_new._product_identity).
# Mirrored on purpose: an auditor working out "did we post this product twice"
# with a DIFFERENT rule than the bot produces findings that mean nothing. The
# copy stays here so the audit runs standalone on a server without Telethon,
# and test_line_fidelity.py fails if the two rules ever disagree.
# ---------------------------------------------------------------------------
# A number in a product name is either the thing that MAKES it that product
# (1.5 Ton, 5 Star, 128GB, 55-inch, 5 Burner) or a spec a channel may or may not
# bother to type (42H playtime, 5000mAh, 1080p, 4K, 5G). The first group is the
# identity; the second is dropped - demanding that two captions of one product spell
# their battery life alike is exactly how the same deal gets posted twice.
# Marketing vocabulary: it says nothing about WHICH product this is, so a copy that
# adds or drops one of these words must not change the identity.
def product_signature(text: str) -> str:
    """What the bot itself keys a repeat on: brand + model number + variant + size."""
    body = text or ""
    named = None
    for line in body.splitlines():
        line = line.strip()
        if not line or ANY_LINK.search(line):
            continue
        identity = _product_identity(line)
        if identity and (identity[0] == "M" or len("|".join(identity)) >= 16):
            named = "|".join(identity)
            break
    if not named:
        return ""
    # No separate size clause any more: capacities are inside the identity, so
    # appending them twice could not change a match, only bloat the key.
    return named


def _token_claims(token: str) -> dict:
    """The EarnKaro/Affiliaters JWT payload, when the token is one."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def ours_publisher_from_env() -> str:
    """"The EarnKaro account OUR links must pay, read the same way the bot reads it.

    Order: the pinned EARNKARO_PUBLISHER_ID, the token in EARNKARO_API_KEY, then
    the token file the deploy scripts read (ops/.earnkaro_key). The id is never
    the secret, so printing it is safe; the token itself is never printed.
    """
    pinned = os.getenv("EARNKARO_PUBLISHER_ID", "").strip()
    if pinned:
        return pinned
    token = os.getenv("EARNKARO_API_KEY", "").strip()
    if not token:
        try:
            token = (Path(__file__).resolve().parent / ".earnkaro_key").read_text(
                encoding="utf-8").strip()
        except OSError:
            token = ""
    return str(_token_claims(token).get("earnkaro") or "").strip()


def our_minted_links(conn: sqlite3.Connection) -> set[str]:
    """Every link the bot's own link_cache says IT produced.

    The provenance gate at delivery asks this same table "did we mint this?", so
    the auditor asking it too is what keeps the two answers identical instead of
    the auditor inventing a stricter rule and crying wolf on healthy posts.
    """
    try:
        return {str(r[0]) for r in conn.execute(
            "SELECT affiliate_url FROM link_cache WHERE affiliate_url IS NOT NULL") if r[0]}
    except sqlite3.OperationalError:
        return set()


def audit(conn: sqlite3.Connection, limit: int, our_tag: str = "",
          our_publisher: str = "") -> tuple[list[dict], int, dict]:
    conn.row_factory = sqlite3.Row
    # Every recent row is inspected, including ones that produced no post at all:
    # "we lost a deal" is as much a quality defect as a badly formatted one.
    rows = conn.execute(
        "SELECT id, source, status, created_at, rendered_text, targets_json, last_error "
        "FROM queue ORDER BY id DESC LIMIT ?", (max(1, limit),),
    ).fetchall()
    try:
        sent_counts = {int(r["queue_id"]): int(r["n"]) for r in conn.execute(
            "SELECT queue_id, count(*) AS n FROM deliveries WHERE status='sent' GROUP BY queue_id")}
    except sqlite3.OperationalError:
        sent_counts = {}
    our_links = our_minted_links(conn)
    link_counts = {"links": 0, "our-short": 0, "our-publisher": 0, "unattributed": 0,
                   "clean-merchant": 0, "foreign": 0}

    findings: list[dict] = []
    for row in rows:
        text = row["rendered_text"] or ""
        sent = sent_counts.get(int(row["id"]), 0)
        error = (row["last_error"] or "").lower()
        # 5) coverage: a job that finished without sending anything and without a
        #    policy reason is a lost deal.
        if not text:
            if (row["status"] or "") in ("done", "failed") and not sent and not any(
                    marker.lower() in error for marker in POLICY_SKIPS):
                findings.append(flag("COVERAGE",
                                     f"{row['status']} with no send and no policy reason "
                                     f"({(row['last_error'] or 'no reason')[:70]})", row["id"]))
            continue
        if (row["status"] or "") == "pending":
            continue
        try:
            targets = json.loads(row["targets_json"] or "[]")
        except (TypeError, ValueError):
            targets = []
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        # 1) nothing of ours may be added: our old decorations and markdown debris
        lowered = text.lower()
        for marker in OURS_MARKERS:
            if marker.lower() in lowered:
                findings.append(flag("OURS", f"invented text {marker!r} is back in the post", row["id"]))
        if re.search(r"\*\S|\S\*\*|__\S", text):
            findings.append(flag("OURS", "markdown debris (** / __) left in the post", row["id"]))
        if FOREIGN_PROMO.search(text):
            findings.append(flag("OURS", "a join/subscribe/referral line survived cleaning", row["id"]))

        # 2) links: every link must be ours, and the post must carry at least one
        links = [u.rstrip(")】.,;'\"]") for u in ANY_LINK.findall(text)]
        if not links:
            findings.append(flag("LINKS", "published with no link at all", row["id"]))
        for url in links:
            link_counts["links"] += 1
            link_counts[classify_link(url, our_tag, our_publisher, our_links)] += 1
            why = why_not_our_link(url, our_tag, our_publisher, our_links)
            if why:
                findings.append(flag("LINKS", f"{why}: {url[:80]}", row["id"]))

        # 3) a post must carry the deal, not only links
        if lines and all(ANY_LINK.fullmatch(line) for line in lines):
            findings.append(flag("TEXTLESS", "only links, no product text", row["id"]))
        elif RUPEE in text and not PRICE_RE.search(text):
            findings.append(flag("TEXTLESS", "a rupee sign with no amount next to it", row["id"]))

        # 4) price-tier channels must not receive a post outside their band. Only
        #    single-product posts are judged (a list is a roundup by design) and the
        #    figure that matters is the CHEAPEST one, i.e. what a shopper pays.
        prices = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        priced_lines = sum(1 for line in lines if PRICE_RE.search(line))
        listy = priced_lines >= 3 or bool(LIST_MARKER_RE.search(text))
        # 3b) the two text defects the user named: text that was CUT away, and a
        #     figure printed twice inside one post (our old price badge did that).
        if "\u2026" in text:
            findings.append(flag("CUT", "the post ends mid-sentence with an ellipsis - "
                                        "source text was dropped, not deferred", row["id"]))
        if not listy and len(lines) > 1:
            per_line: dict[str, int] = {}
            for line in lines:
                for amount in re.findall(rf"{RUPEE}\s*\d[\d,.]*", line):
                    key = re.sub(r"\D", "", amount)
                    per_line[key] = per_line.get(key, 0) + 1
            twice = [f"{RUPEE}{value}" for value, hits in per_line.items() if hits > 1]
            if twice:
                findings.append(flag("DOUBLE", f"{', '.join(twice)} printed on two lines of one post",
                                     row["id"]))

        if prices and not listy:
            cheapest = min(prices)
            for target in targets:
                band = None
                lowered = str(target).lower()
                if lowered.startswith(("under99", "under-99")):
                    band = 99
                elif lowered.startswith(("under499", "under-499")):
                    band = 499
                if band is not None and cheapest > band:
                    findings.append(flag("MISROUTE",
                                         f"{RUPEE}{cheapest} sent to the {RUPEE}{band} channel",
                                         row["id"], target))

    # 6) duplicates on the way out: the same content - or the same product at the
    #    same price - delivered by two different posts within six hours.
    fresh_cutoff = time.time() - 6 * 3600
    exact: dict[str, list[int]] = {}
    near: dict[tuple, list[int]] = {}
    for row in rows:
        text = row["rendered_text"] or ""
        if not text or (row["created_at"] or 0) < fresh_cutoff or not sent_counts.get(int(row["id"])):
            continue
        exact.setdefault(body_fingerprint(text), []).append(int(row["id"]))
        prices = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        near.setdefault((product_line(text), min(prices) if prices else 0), []).append(int(row["id"]))
    matched: set[int] = set()
    same_product: dict[tuple, list[int]] = {}
    for row in rows:
        text = row["rendered_text"] or ""
        if not text or (row["created_at"] or 0) < fresh_cutoff or not sent_counts.get(int(row["id"])):
            continue
        signature = product_signature(text)
        if not signature:
            continue
        priced = [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        # A channel, not a job: "okasari mana channel lo vasthe skip cheyali".
        for target in targets_of(row):
            same_product.setdefault((signature, str(target)), []).append(
                (int(row["id"]), min(priced) if priced else 0))
    for (signature, target), entries in same_product.items():
        if len(entries) < 2:
            continue
        ids = [e[0] for e in entries]
        if set(ids) <= matched:
            continue
        entries = sorted(entries)
        first_price = entries[0][1]
        cheaper_later = [p for _queue_id, p in entries[1:] if p]
        # The bot is ALLOWED to carry a product again when the later copy is
        # strictly cheaper - that is a new deal, not a repeat. Anything else
        # (same price, a dearer price, no price at all) is the defect.
        if first_price and cheaper_later and min(cheaper_later) < first_price:
            continue
        matched.update(ids)
        findings.append(flag("SAME-PRODUCT",
                             f"{signature[:52]!r} carried twice by {target} (queues {ids})", ids[0]))

    for key, ids in exact.items():
        if len(ids) < 2 or not key:
            continue
        matched.update(ids)
        findings.append(flag("DUPLICATE",
                             f"the same post went out twice from queues {ids}: {key[:60]!r}", ids[0]))
    for key, ids in near.items():
        if len(ids) < 2 or not key[0] or set(ids) <= matched:
            continue
        matched.update(ids)
        findings.append(flag("NEAR-DUPE",
                             f"{key[0][:50]!r} at {RUPEE}{key[1]} went out from {len(ids)} posts {ids}",
                             ids[0]))

    try:
        total_posts = conn.execute(
            "SELECT count(*) FROM queue WHERE rendered_text IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        total_posts = len(rows)
    return findings, total_posts, link_counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="audit posted-deal quality (read-only)")
    default_db = Path(__file__).resolve().parent.parent / "bestgaa" / "state" / "bot_state.sqlite3"
    parser.add_argument("--db", type=Path, default=default_db)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--strict", action="store_true", help="exit 1 when anything is found")
    parser.add_argument("--tag", default=os.getenv("AMAZON_TAG", "mama086-21"),
                        help="our Amazon affiliate tag (default: $AMAZON_TAG or mama086-21)")
    parser.add_argument("--publisher", default="",
                        help="our EarnKaro publisher id (default: read from "
                             "$EARNKARO_PUBLISHER_ID, the token, or ops/.earnkaro_key)")
    args = parser.parse_args(argv)
    publisher = (args.publisher or "").strip() or ours_publisher_from_env()

    if not args.db.exists():
        print(f"QUALITY AUDIT: no database at {args.db}")
        return 0
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        findings, posts, link_counts = audit(conn, args.limit, (args.tag or "").strip(),
                                            publisher)
    except sqlite3.OperationalError as exc:  # a schema too old to judge
        print(f"QUALITY AUDIT: database not readable yet ({exc})")
        return 0
    finally:
        conn.close()

    by_kind: dict[str, list[dict]] = {}
    for item in findings:
        by_kind.setdefault(item["kind"], []).append(item)
    summary = (f"LINK PERFECTION | links={link_counts['links']} "
               f"our-short={link_counts['our-short']} "
               f"our-publisher={link_counts['our-publisher']} "
               f"unattributed={link_counts['unattributed']} "
               f"clean-merchant={link_counts['clean-merchant']} "
               f"FOREIGN={link_counts['foreign']}")
    if args.json:
        print(json.dumps({"db": str(args.db), "posts_checked": posts,
                          "link_summary": link_counts, "findings": findings}))
    else:
        print(f"QUALITY AUDIT | db={args.db} posts={posts} findings={len(findings)}")
        print(f"  {summary}")
        if link_counts["foreign"] == 0 and link_counts["unattributed"] == 0 and link_counts["links"]:
            print("  every published link is one of ours (minted by this bot, our tag, "
                  "or a clean merchant page) - nothing foreign reached a channel")
        if link_counts["unattributed"]:
            print("  -> %s link(s) could carry our publisher id and did not (Flipkart/Myntra): "
                  "they open and pay nobody. Published before the 2026-10-04 attribution fix; "
                  "new posts stamp affExtParam2 automatically." % link_counts["unattributed"])
        for kind in ("OURS", "LINKS", "TEXTLESS", "MISROUTE", "COVERAGE", "DUPLICATE", "NEAR-DUPE"):
            items = by_kind.get(kind) or []
            print(f"  {kind:<9}: {len(items)}")
            for item in items[:5]:
                where = f" queue={item['queue_id']}" if item.get("queue_id") else ""
                target = f" target={item['target']}" if item.get("target") else ""
                print(f"     {item['detail']}{where}{target}")
            if len(items) > 5:
                print(f"     ... {len(items) - 5} more")
        if not findings:
            print("  every audited post is clean: our links only, nothing invented, "
                  "no duplicate product, nothing lost")
    return 1 if (findings and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
