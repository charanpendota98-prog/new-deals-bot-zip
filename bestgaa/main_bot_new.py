#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BestGAA Production Bot v15.0

Durable Telegram deal pipeline:
- SQLite queue and delivery ledger (restart-safe)
- EarnKaro conversion with retry/cache/circuit breaker
- strict generated-link-only output
- per-product 10-hour cross-source dedup
- 1-hour same-price fallback only when product identity is unavailable
- dedup committed only after at least one target post succeeds
- Buy Now/entity/button URL support
- long caption/message chunking
- Under-99 / Under-499 price routing
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import logging.handlers
import os
import random
import re
import signal
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse

import aiohttp
from telethon import TelegramClient, events
from telethon.helpers import add_surrogate, del_surrogate
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import (
    KeyboardButtonUrl,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageMediaInvoice,
    MessageMediaWebPage,
    ReplyInlineMarkup,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def load_env_file(path: Path) -> None:
    """Load a simple KEY=VALUE file without overriding real environment vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_env_file(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(BASE_DIR / "bestgaa.sqlite3")))
SESSION_PATH = str(BASE_DIR / os.getenv("TELEGRAM_SESSION", "bestgaa_fresh"))


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


API_ID = int(env_required("TELEGRAM_API_ID"))
API_HASH = env_required("TELEGRAM_API_HASH")
EK_KEY = env_required("EARNKARO_API_KEY")
EK_API = os.getenv("EARNKARO_API_URL", "https://ekaro-api.affiliaters.in/api/converter/public")
OUR_TAG = env_required("AMAZON_TAG")
OUR_EK_ID = os.getenv("EARNKARO_PUBLISHER_ID", "").strip()
BITLY_TOKENS = [x.strip() for x in os.getenv("BITLY_TOKENS", "").split(",") if x.strip()]
# USER RULE: "ekkuva commission edi vunte ade pettu" — direct Amazon
# Associates pays the full commission (EarnKaro takes a cut in the middle),
# so Amazon links default to OUR Associates tag directly. Set
# AMAZON_EARNKARO_RATIO above 0 only to route some share back via EarnKaro.
try:
    AMAZON_EARNKARO_RATIO = min(1.0, max(0.0, float(os.getenv("AMAZON_EARNKARO_RATIO", "0.0"))))
except ValueError:
    AMAZON_EARNKARO_RATIO = 0.0

PRODUCT_DEDUP_SECONDS = int(os.getenv("PRODUCT_DEDUP_SECONDS", str(10 * 3600)))
PRICE_DEDUP_SECONDS = max(0, int(os.getenv("PRICE_DEDUP_SECONDS", "0")))
QUEUE_WORKERS = max(1, int(os.getenv("QUEUE_WORKERS", "6")))
EK_MAX_CONCURRENCY = max(1, int(os.getenv("EK_MAX_CONCURRENCY", "6")))
POST_RETRIES = max(1, int(os.getenv("POST_RETRIES", "3")))
SOURCE_REFRESH_SECONDS = max(300, int(os.getenv("SOURCE_REFRESH_SECONDS", "900")))
# Dead-man's switch: re-scan every live source's recent messages on a short
# cycle so a silently dead event stream never loses deals until a restart.
SOURCE_RESCAN_SECONDS = max(300, int(os.getenv("SOURCE_RESCAN_SECONDS", "600")))
SOURCE_RESCAN_LIMIT = max(5, int(os.getenv("SOURCE_RESCAN_LIMIT", "20")))
# Timestamp of the last ingested source event (live ingest health signal).
LAST_INGEST_AT = time.time()
BACKFILL_HOURS = max(0, int(os.getenv("BACKFILL_HOURS", "6")))
BACKFILL_LIMIT = max(0, int(os.getenv("BACKFILL_LIMIT", "50")))
STATE_RETENTION_DAYS = max(2, int(os.getenv("STATE_RETENTION_DAYS", "7")))
LINK_CACHE_DAYS = max(2, int(os.getenv("LINK_CACHE_DAYS", "14")))
MAINTENANCE_SECONDS = max(3600, int(os.getenv("MAINTENANCE_SECONDS", "21600")))
OZ_SOURCE = "https://t.me/+vZKuuHCZcX44M2I1"

UNDER99_TARGET = "Under99Deals11"
UNDER499_TARGET = "under499loots"
POWER_FILTER_TARGET = "PowerLoots1"
PREMIUM_TARGET = "Premiumlootsdeals"
SECRET_TARGET = "SecretLootIndia1"
TRICKS_TARGET = "LootzoneTricks"
MAIN_TARGETS = [SECRET_TARGET, "LootZoneIndia11", TRICKS_TARGET, POWER_FILTER_TARGET]
NO_TRICKS_TARGETS = [SECRET_TARGET, "LootZoneIndia11", POWER_FILTER_TARGET]
LZI_SECRET = ["LootZoneIndia11", SECRET_TARGET]
TRICKS_SOURCES = {"TrickXpert", "Offerzone_deals"}
OUR_FOLDER_LINK = "https://t.me/addlist/5V7_ViAGDxAwNTI1"
# Every channel owner controls. Card/bank-offer posts fan out across all of
# these so a bank/card deal is never missed, and get the folder link appended.
ALL_OWNED_TARGETS = list(dict.fromkeys(
    [SECRET_TARGET, "LootZoneIndia11", POWER_FILTER_TARGET, PREMIUM_TARGET,
     UNDER99_TARGET, UNDER499_TARGET, TRICKS_TARGET]
))
OUR_MAIN_CHANNEL_LINKS = ("https://t.me/SecretLootIndia1", "https://t.me/LootZoneIndia11")
PREMIUM_MAX_PER_NIGHT = 12
PREMIUM_GAP_MIN_SECONDS = 15 * 60
PREMIUM_GAP_MAX_SECONDS = 35 * 60
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Night quiet window (IST): TARGET posting pauses between POST_QUIET_START and
# POST_QUIET_END (user setting: 02:00-06:00). Ingest, rescan and rendering all
# keep running, so every deal that arrives at night is queued and delivered
# the moment the window ends — nothing is lost, posting just sleeps.
# Set both values equal (e.g. 00:00/00:00) to disable the window.
# ---------------------------------------------------------------------------
def _parse_clock_minutes(value: str, fallback: str) -> int:
    try:
        hour, minute = value.strip().split(":")
        return (int(hour) % 24) * 60 + (int(minute) % 60)
    except Exception:
        hour, minute = fallback.split(":")
        return int(hour) * 60 + int(minute)


POST_QUIET_START_MIN = _parse_clock_minutes(os.getenv("POST_QUIET_START", "02:00"), "02:00")
POST_QUIET_END_MIN = _parse_clock_minutes(os.getenv("POST_QUIET_END", "06:00"), "06:00")


def in_post_quiet(now: datetime | None = None) -> bool:
    """True while target posting must pause (02:00-06:00 IST by default)."""
    if POST_QUIET_START_MIN == POST_QUIET_END_MIN:
        return False
    current = now.astimezone(IST) if now else datetime.now(IST)
    minute = current.hour * 60 + current.minute
    if POST_QUIET_START_MIN <= POST_QUIET_END_MIN:
        return POST_QUIET_START_MIN <= minute < POST_QUIET_END_MIN
    return minute >= POST_QUIET_START_MIN or minute < POST_QUIET_END_MIN


def born_in_post_quiet(timestamp: float) -> bool:
    """True when the job was created inside the night quiet window."""
    if POST_QUIET_START_MIN == POST_QUIET_END_MIN:
        return False
    return in_post_quiet(datetime.fromtimestamp(float(timestamp), IST))

# Latest routing requirements. Empty base targets use price routing only.
SOURCE_TO_TARGETS: dict[str, list[str]] = {
    "deals": list(LZI_SECRET),
    "powerloot": list(LZI_SECRET),
    "loot_alerts": list(MAIN_TARGETS),
    "TeluguTechworld": list(MAIN_TARGETS),
    "icoolzTricks": list(MAIN_TARGETS),
    "SB_Loots_And_Deals": list(LZI_SECRET),
    "Flipkarthiik": list(MAIN_TARGETS),
    "HiddenDeals1": ["SecretLootIndia1", "LootzoneTricks"],
    "idoffers": ["SecretLootIndia1", "LootzoneTricks"],
    "Magixdeals_Magix": list(LZI_SECRET),
    "dealsvelocity": list(NO_TRICKS_TARGETS),
    "pricehistory": [],
    "telugutechtvdeals": [],
    "indian_online_offer": [],
    "idoffers2": [],
    "DealsUnder99_com": [],
    "under_99_loot_deals": [],
    "https://t.me/+LP6MYEpCwi0zOGYx": [],
    "Mobile_phone_offers_tv_ac_deals": list(MAIN_TARGETS),
    "Mobile_phone_offers_tv_ac_dealsk": list(LZI_SECRET),
    "https://t.me/+qhlEwwkhb2hlNWZl": list(LZI_SECRET),
    "techglaredeals": list(MAIN_TARGETS),
    "iamprasadtech": ["LootZoneIndia11"],  # source only, never forced target
    "https://t.me/+uV5wcTkUWJEwM2Y1": ["LootzoneTricks"],
    "hidden_loot_deals_amazon": ["LootzoneTricks"],
    "dealdost": list(NO_TRICKS_TARGETS),
    "https://t.me/+WvEWEYf7j3MyYzNl": list(LZI_SECRET),
    "https://t.me/+t--iQ-QFeJZiNmVl": ["LootzoneTricks"],
    "https://t.me/+ky8g5O5KTr5mZmQ9": list(LZI_SECRET),
    "https://t.me/+LNRQ0Y1-9RkzZDRl": list(LZI_SECRET),
    "rapiddeals_unlimited": list(LZI_SECRET),
    "amazinglootsdealsoffers": list(LZI_SECRET),
    "https://t.me/+-mv6ttVsltczNzFl": list(NO_TRICKS_TARGETS),
    "realearnkaro": list(LZI_SECRET),
    "CKoffers": list(LZI_SECRET),
    "LootDealsPortal": list(LZI_SECRET),
    "LOOTS_DEAL_OFFER_ONLINE_SHOPPING": list(LZI_SECRET),
    "RealShoppingDeals": list(LZI_SECRET),
    "rebeldealss": list(LZI_SECRET),
    "Only_discount_Deals": list(LZI_SECRET),
    "GrabOnIndiaOfficial": list(LZI_SECRET),
    "myntra_Ajio_Sale_Deals_offers_li": list(LZI_SECRET),
    "BiggestLootDeals": list(LZI_SECRET),
    "lootping": list(LZI_SECRET),
    "Shopping_Loot_Fashion_Deals": list(LZI_SECRET),
    "msho_shpsy_offers": list(LZI_SECRET),
    "Myntra_Ajio_Deals_Shopsy": list(LZI_SECRET),
    "https://telegram.me/+sRe5uRLTK55kZWU1": list(LZI_SECRET),
}

# Dedicated Tricks feed: remove every legacy route into LootzoneTricks, then
# allow only the two user-approved trick/video sources.
for _targets in SOURCE_TO_TARGETS.values():
    while TRICKS_TARGET in _targets:
        _targets.remove(TRICKS_TARGET)

# Newly requested deal sources feed the non-Tricks main targets. Existing keys
# are deliberately overwritten to make this final routing authoritative.
for _source in (
    "vaasutechdeals", "SB_Loots_And_Deals", "dealsvelocity",
    "Magixdeals_Magix", "techglaredeals", "lootsxpert",
    "https://t.me/+WvEWEYf7j3MyYzNl", "https://t.me/+FpXKV70NYNY0NzQ1",
    "https://t.me/+vZKuuHCZcX44M2I1", "https://telegram.me/+sRe5uRLTK55kZWU1",
    "Only_discount_Deals", "amazinglootsdealsoffers", "FlashDealsUnlimited",
):
    SOURCE_TO_TARGETS[_source] = list(NO_TRICKS_TARGETS)

SOURCE_TO_TARGETS["TrickXpert"] = [TRICKS_TARGET]
SOURCE_TO_TARGETS["Offerzone_deals"] = [TRICKS_TARGET]
# Latest explicit non-Tricks main-source routing.
SOURCE_TO_TARGETS["idoffers"] = list(NO_TRICKS_TARGETS)
SOURCE_TO_TARGETS["SB_Loots_And_Deals"] = list(NO_TRICKS_TARGETS)
SOURCE_TO_TARGETS["loot_alerts"] = list(NO_TRICKS_TARGETS)
# OZ Loot Bazaar private source: all non-Tricks main targets. Premiumlootsdeals
# is added dynamically only when its premium filter passes.
SOURCE_TO_TARGETS[OZ_SOURCE] = list(NO_TRICKS_TARGETS)

# These sources must contain true sub-₹99 deals; valid posts go to both price targets.
UNDER99_SOURCES = {
    "under_99_loot_deals",
    "DealsUnder99_com",
    "https://t.me/+LP6MYEpCwi0zOGYx",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("bestgaa")
log.setLevel(logging.INFO)
log.handlers.clear()
log.propagate = False
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console = logging.StreamHandler(sys.stdout)
console.setFormatter(formatter)
rotating = logging.handlers.RotatingFileHandler(
    LOG_DIR / "bot.log", maxBytes=8_000_000, backupCount=5, encoding="utf-8"
)
rotating.setFormatter(formatter)
log.addHandler(console)
log.addHandler(rotating)

# ---------------------------------------------------------------------------
# Domain and parsing helpers
# ---------------------------------------------------------------------------
URL_RE = re.compile(r"https?://[^\s<>\[\](){}\"']+", re.I)
INTENT_RE = re.compile(r"intent://[^\s]+", re.I)
ASIN_RE = re.compile(r"(?:/dp/|/gp/product/|/gp/aw/d/)([A-Z0-9]{10})(?:[/?#]|$)", re.I)

SHORT_DOMAINS = {
    "amzn.to", "amzn.in", "amazn.lt", "link.amazon", "fkrt.co", "fkrt.cc", "fkrt.in",
    "fktr.in", "myntr.it", "myntr.in", "ajiio.co", "bit.ly", "bitli.in", "bitl.in",
    "bittli.in", "bilty.co", "tinyurl.com", "cutt.ly", "rb.gy", "t.ly", "tiny.cc",
    "shorturl.at", "is.gd", "v.gd", "snip.ly", "linkredirect.in", "ekaro.in",
    "clnk.in", "clnk.app", "ekaro.app", "l.ead.me",
}
OUR_SHORTENER_DOMAINS = {
    "ekaro.in", "clnk.in", "clnk.app", "ekaro.app", "fktr.in", "myntr.it",
    "ajiio.in", "bitli.in", "j.mp", "cuelinks.com", "l.ead.me", "affiliaters.in",
}
OUR_RUNTIME_SHORTENER_DOMAINS = {"bit.ly", "is.gd"}
FOREIGN_ECHO_DOMAINS = {
    "amzn.to", "amzn.in", "amazn.lt", "link.amazon", "fkrt.co", "fkrt.cc", "fkrt.in",
    "myntr.in", "ajiio.co", "tinyurl.com", "cutt.ly", "rb.gy", "t.ly", "tiny.cc",
    "shorturl.at", "is.gd", "v.gd", "snip.ly", "linkredirect.in", "bilty.co", "bitly.co",
}
AMAZON_DOMAINS = {"amazon.in", "www.amazon.in", "amazon.com", "www.amazon.com"}
KNOWN_MERCHANT_DOMAINS = {
    "amazon.in", "amazon.com", "flipkart.com", "myntra.com", "ajio.com",
    "myntr.in", "myntr.it", "ajiio.co", "fkrt.co", "fkrt.cc", "fktr.in",
    "meesho.com", "shopsy.in", "tatacliq.com", "nykaa.com", "croma.com",
    "reliancedigital.in", "jiomart.com", "firstcry.com", "snapdeal.com",
    "decathlon.in", "pepperfry.com", "limeroad.com", "paytmmall.com",
}
TRACKING_QUERY_KEYS = {
    "tag", "ref", "ref_", "linkcode", "camp", "creative", "creativeasin",
    "ascsubtag", "affid", "affextparam1", "affextparam2", "aff_id", "affiliate",
    "shareid", "source", "subid", "sub_id", "clickid", "irclickid", "irgwc",
    "referral_code", "referral", "refcode", "invite_code", "invitecode",
    "gclid", "fbclid", "msclkid",
}
# Amazon-only junk: search/session attribution that bloats /s? links to 300+
# chars (btn_ref=srctok-..., ds=, qid=...). Meaningful filters (k, i, rh, s,
# rnid keeps the filter group) are preserved so Men/Women/Girls/Boys category
# links stay distinct.
AMAZON_JUNK_QUERY_KEYS = {
    "btn_ref", "btn_type", "ds", "dc", "qid", "sprefix", "crid", "sr",
    "pd_rd_r", "pd_rd_w", "pd_rd_wg", "pf_rd_i", "pf_rd_m", "pf_rd_p",
    "pf_rd_r", "pf_rd_s", "pf_rd_t", "content-id", "spla", "qu", "srs",
}
NON_STORE_DOMAINS = {
    "t.me", "telegram.org", "youtube.com", "youtu.be", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "reddit.com", "whatsapp.com", "discord.com", "pinterest.com",
    "linkedin.com", "github.com", "google.com", "drive.google.com", "imgur.com",
}
BROKEN_PAGE_MARKERS = (
    "just a quick repair needed",
    "we're doing everything we can to fix this",
    "we’re doing everything we can to fix this",
    "the page you requested cannot be found",
    "this page could not be found",
    "product is no longer available",
)


def looks_broken_page(status: int, body: str) -> bool:
    """Recognize explicit dead/error destinations without mistaking bot-blocks for dead deals."""
    text = (body or "").lower()
    if any(marker in text for marker in BROKEN_PAGE_MARKERS):
        return True
    return status in {400, 404, 410, 451}


def host_matches(host: str, domain: str) -> bool:
    host = (host or "").lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def in_domains(host: str, domains: Iterable[str]) -> bool:
    return any(host_matches(host, d) for d in domains)


# Long-link threshold for shortening: anything longer than this (a messy
# merchant URL with filters/tracking) gets a Bitly/is.gd short link so Telegram
# posts stay clean. A tidy Amazon /dp link with our tag (~52 chars) is under it
# and posts direct (no quota); lists always shorten (multi_link). Overridable.
SHORTEN_MIN_LEN = int(os.getenv("SHORTEN_MIN_LEN", "70"))


def should_use_bitly(resolved_url: str, multi_link: bool) -> bool:
    """USER RULE: Bitly ONLY for lists (2+ links) and very long links —
    normal single Amazon links post with our Associates tag directly, so the
    Bitly monthly quota never runs out on ordinary product deals. (The very
    long single links are caught by the caller's length check.)"""
    return multi_link


def apply_amazon_tag(link: str) -> str:
    """Force the configured Associates Store ID on direct Amazon API output."""
    try:
        parsed = urlparse(clean_url(link))
        host = (parsed.hostname or "").lower()
        if not in_domains(host, AMAZON_DOMAINS):
            return clean_url(link)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key.lower() != "tag"]
        query.append(("tag", OUR_TAG))
        return parsed._replace(query=urlencode(query, doseq=True)).geturl()
    except Exception:
        return clean_url(link)


def compact_amazon_product_link(link: str) -> str:
    """Collapse an Amazon PRODUCT (/dp/ASIN or /gp/product/ASIN) URL to its
    shortest native form carrying ONLY our Associates tag:
      https://www.amazon.in/dp/ASIN?tag=deals0911-21   (~48 chars)
    All noise params (psc/smid/th/ref/linkCode/...) are dropped, so product
    links in a list are short WITHOUT spending Bitly quota. Amazon SEARCH /
    category links (/s?...) have no single ASIN and are returned untouched so
    the final shorten pass can Bitly them. Respects any explicit ref= tag the
    source set (kept), and preserves the Amazon TLD."""
    try:
        raw = clean_url(link)
        m = re.search(r"(?:/dp/|/gp/(?:product|aw/d)/)([A-Z0-9]{10})(?:[/?#]|$)", raw, re.I)
        if not m:
            return raw
        asin = m.group(1).upper()
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if not in_domains(host, AMAZON_DOMAINS):
            return raw
        tag = OUR_TAG
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() == "tag" and value:
                tag = value
                break
        netloc = host if host.startswith("www.") else "www." + host
        # Keep a ref node if the source explicitly set one (marketing node);
        # otherwise drop everything for the shortest clean link.
        ref = None
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() == "ref" and value:
                ref = value
        query = urlencode([("tag", tag)] + ([("ref", ref)] if ref else []))
        return f"https://{netloc}/dp/{asin}?{query}"
    except Exception:
        return clean_url(link)


def clean_url(value: str) -> str:
    # Telegram/Markdown copies can contain HTML-escaped query separators (`&amp;`).
    # Decode before parsing so foreign affiliate wrappers are always unwrapped.
    return html.unescape(value or "").strip().rstrip(".,;:!?\"')*]>}\n\r\t ")


def canonical_url(value: str) -> str:
    try:
        p = urlparse(clean_url(value))
        query = parse_qs(p.query)
        keep = {}
        for key in ("pid", "productid", "productId", "asin", "node"):
            if key in query:
                keep[key] = query[key]
        q = urlencode(keep, doseq=True)
        path = re.sub(r"/+", "/", p.path).rstrip("/")
        return p._replace(scheme="https", netloc=(p.hostname or "").lower(), path=path,
                          query=q, fragment="").geturl()
    except Exception:
        return clean_url(value).lower()


def merchant_url(value: str) -> str:
    """Remove attribution noise but preserve merchant filters such as Men/Women."""
    try:
        p = urlparse(clean_url(value))
        kept = []
        host = (p.hostname or "").lower()
        is_amazon = in_domains(host, AMAZON_DOMAINS)
        for key, item in parse_qsl(p.query, keep_blank_values=True):
            lower = key.lower()
            if lower.startswith("utm_") or lower in TRACKING_QUERY_KEYS:
                continue
            # Amazon search/session junk (btn_ref=srctok, ds=, qid=, sprefix=)
            # is stripped so category-filter links shrink to their real filters.
            if is_amazon and lower in AMAZON_JUNK_QUERY_KEYS:
                continue
            kept.append((key, item))
        kept.sort(key=lambda pair: (pair[0].lower(), pair[1]))
        path = re.sub(r"/+", "/", p.path).rstrip("/")
        return p._replace(
            scheme="https", netloc=(p.hostname or "").lower(), path=path,
            query=urlencode(kept, doseq=True), fragment="",
        ).geturl()
    except Exception:
        return clean_url(value)


def parse_intent(value: str) -> str | None:
    if not value.startswith("intent://"):
        return None
    match = re.search(r"S\.browser_fallback_url=([^;]+)", value)
    if match:
        target = unquote(match.group(1))
        return target if target.startswith("http") else None
    match = re.search(r"intent://([^#;]+)", value)
    return "https://" + match.group(1).lstrip("/") if match else None


def extract_product_id(value: str) -> str | None:
    url = clean_url(value)
    match = ASIN_RE.search(url)
    if match:
        return "ASIN:" + match.group(1).upper()
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if query.get("asin"):
            return "ASIN:" + query["asin"][0].upper()
        if query.get("pid"):
            return "PID:" + query["pid"][0].upper()
        host = (parsed.hostname or "").lower()
        if host_matches(host, "myntra.com"):
            match = re.search(r"/(\d{6,})(?:[/?#]|$)", parsed.path + "?")
            if match:
                return "MYNTRA:" + match.group(1)
        if host_matches(host, "ajio.com"):
            match = re.search(r"/p/([A-Z0-9]+)", parsed.path, re.I)
            if match:
                return "AJIO:" + match.group(1).upper()
    except Exception:
        pass
    return None


def content_deal_key(text: str) -> str | None:
    """Cross-source fingerprint to block same deal even when short URLs differ."""
    cleaned = clean_source_text(text)
    cleaned = URL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"(?i)\b(?:buy\s*now|shop\s*now|grab\s*fast)\b", " ", cleaned)
    cleaned = re.sub(r"[^\w₹%\n]+", " ", cleaned, flags=re.UNICODE)
    lines = [re.sub(r"\s+", " ", line).strip().lower()
             for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return None
    headline = lines[0]
    # A strong headline blocks reposted campaigns even when a later source has
    # fewer/more variant links. Short/generic headlines fall back to full text.
    basis = headline if len(headline) >= 18 and len(headline.split()) >= 3 else " ".join(lines)
    if len(basis) < 18 or len(basis.split()) < 3:
        return None
    return "CONTENT:" + hashlib.sha256(basis.encode()).hexdigest()


def product_key(value: str) -> str:
    return extract_product_id(value) or "URL:" + hashlib.sha256(merchant_url(value).encode()).hexdigest()


def parse_price(text: str) -> int | None:
    """Prefer explicit deal/effective price; avoid MRP/coupon/bank discount values."""
    patterns = [
        r"(?:deal|effective|offer|final)\s*price\s*[:@-]?\s*(?:rs\.?|₹)?\s*([\d,]+)",
        r"(?:only|at|@)\s*(?:rs\.?|₹)?\s*([\d,]+)",
        r"(?:rs\.?|₹)\s*([\d,]+)(?!\s*(?:off|coupon|cashback))",
        r"^\s*([\d,]{2,})\s+(?:https?://|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", re.I | re.M)
        if match:
            value = int(match.group(1).replace(",", ""))
            if 1 <= value <= 1_000_000:
                return value
    return None


def parse_discount(text: str, price: int | None = None) -> int | None:
    """Return the strongest explicit/derived product discount percentage."""
    value = text or ""
    found: list[int] = []
    patterns = (
        r"(?:up\s*to|upto|flat|save|get)?\s*([1-9]\d?|100)\s*%\s*(?:off|discount)",
        r"(?:off|discount)\s*(?:up\s*to|upto|flat)?\s*([1-9]\d?|100)\s*%",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, value, re.I):
            percent = int(match.group(1))
            if 1 <= percent <= 100:
                found.append(percent)

    mrp_matches = re.findall(r"\bMRP\s*[:@-]?\s*(?:rs\.?|₹)?\s*([\d,]+)", value, re.I)
    deal_price = price if price is not None else parse_price(value)
    if mrp_matches and deal_price:
        with contextlib.suppress(ValueError):
            mrp = max(int(raw.replace(",", "")) for raw in mrp_matches)
            if mrp > deal_price > 0:
                found.append(round((mrp - deal_price) * 100 / mrp))
    return max(found) if found else None


def classify_priority(text: str, has_media: bool = False) -> int:
    """Priority for the Telegram delivery queue.

    Order (highest first):
      5  = best-list / mega product list (>=4 distinct merchant links)
      4  = credit card / bank offer, OR super deal >=80% off
      3  = strong deal, 75%+ off
      2  = photo/video post
      1  = ordinary deal
    """
    value = text or ""
    link_count = len(set(clean_url(u) for u in URL_RE.findall(value)))
    if link_count >= 4:
        return 5
    if has_card_offer(value):
        return 4
    # Zomato/Swiggy/Zepto/movie/card-app offers are best-tier for subscribers.
    if has_service_offer(value):
        return 4
    discount = parse_discount(value)
    if discount is not None and discount >= 80:
        return 4
    if discount is not None and discount >= 75:
        return 3
    if has_media:
        return 2
    return 1


def has_card_offer(text: str) -> bool:
    value = text or ""
    return bool(re.search(
        r"\b(?:credit\s*card|debit\s*card|bank\s*offer|card\s*offer|"
        r"(?:pnb|hdfc|icici|sbi|axis|kotak|idfc|au\s*bank).{0,60}"
        r"(?:off|discount|cashback|card))\b",
        value, re.I | re.S,
    ))


# Service / lifestyle offers: food delivery, movies, quick commerce, payment
# apps and bank/card promo pages. They are NOT EarnKaro-monetizable store
# products, so their links pass through unconverted (and never poison the
# EarnKaro conversion of store links in the same post) while the post itself
# is fanned out to every owned channel like a bank offer.
SERVICE_OFFER_DOMAINS = {
    "zomato.com", "zomato.in", "zom.to", "swiggy.com", "swiggy.in",
    "zepto.com", "dominos.in", "pizzahut.co.in", "pizzahut.com",
    "bookmyshow.com", "pvr.in", "inoxmovies.com", "amctheatres.in",
    "bigcinema.com", "phonepe.com", "amazonpay.in", "paytm.com",
    "magicpin.in",
}
SERVICE_OFFER_RE = re.compile(
    r"\b(?:zomato|swiggy|zepto|bookmyshow|domino(?:s)?|pizza\s*hut|"
    r"movie\s*(?:tickets?|shows?|offers?|booking)|cinema\s*(?:offers?|tickets?)|"
    r"pvr|inox|amc\s*theatres?|bigcinema|phonepe|amazon\s*pay|paytm|magicpin)\b",
    re.I,
)


def has_service_offer(text: str) -> bool:
    return bool(SERVICE_OFFER_RE.search(text or ""))


def automatic_price_targets(price: int | None) -> list[str]:
    targets = []
    if price is not None and price <= 99:
        targets.extend([UNDER99_TARGET, UNDER499_TARGET])
    elif price is not None and price <= 499:
        targets.append(UNDER499_TARGET)
    return targets


def eligible_for_under499_best(text: str, price: int | None, discount: int | None,
                               card_offer: bool) -> bool:
    """under499loots is the main feed: best deals from every configured source.

    Price routing already covers everything at or below ₹499. This adds the
    strong deals whose price the source never printed in a parseable form, so a
    genuine loot is not skipped merely because no "₹" appeared in the text. A
    post with a visible price above ₹499 is still excluded, keeping the channel
    name honest.
    """
    if price is not None:
        return price <= 499
    if discount is not None and discount >= 80:
        return True
    if has_lowest_price_claim(text) and (discount or 0) >= 60:
        return True
    return bool(card_offer and discount is not None and discount >= 70)


def eligible_for_power_loots(price: int | None, discount: int | None,
                             card_offer: bool = False) -> bool:
    """PowerLoots1 gets <=₹499, >=70%-off, or explicit bank/card offers."""
    return (
        (price is not None and price <= 499)
        or (discount is not None and discount >= 70)
        or card_offer
    )


def format_power_loot(text: str, price: int | None, discount: int | None,
                      card_offer: bool = False) -> str:
    badges = []
    if price is not None and price <= 499:
        badges.append(f"💸 UNDER ₹499 • ₹{price}")
    if discount is not None and discount >= 70:
        badges.append(f"🔥 {discount}% OFF")
    if card_offer:
        badges.append("💳 BANK / CARD OFFER")
    header = "⚡ POWER LOOT ALERT ⚡\n" + "\n".join(badges)
    return f"{header}\n\n{text.strip()}\n\n⏳ Grab Fast • Price/stock may change"


def has_lowest_price_claim(text: str) -> bool:
    return bool(re.search(
        r"\b(?:lowest(?:\s+ever)?\s+price|all[ -]?time\s+low|price\s+drop|"
        r"lower\s+than\s+market|historical\s+low|best\s+price)\b",
        text or "", re.I,
    ))


def append_folder_link(text: str) -> str:
    """Clean, source-derived tail: the Loots Family folder link only."""
    return f"{text.rstrip()}\n\n📂 All Loot Channels — One Tap\n👉 {OUR_FOLDER_LINK}"


def eligible_for_secret(text: str, price: int | None, discount: int | None,
                        multi_product_list: bool, card_offer: bool) -> bool:
    """High-value Secret channel: fewer, stronger deals instead of every post."""
    if multi_product_list:
        return True
    if discount is not None and discount >= 80:
        return True
    if price is not None and price <= 99 and discount is not None and discount >= 60:
        return True
    if price is not None and price <= 499 and has_lowest_price_claim(text):
        return True
    return bool(card_offer and discount is not None and discount >= 70)


def premium_score(text: str, price: int | None, discount: int | None) -> int:
    """Balanced premium ranking: strongest discounts and low verified prices first."""
    score = min(discount or 0, 100) * 10
    if price is not None and price <= 99:
        score += 650
    elif price is not None and price <= 499:
        score += 250
    if has_lowest_price_claim(text):
        score += 300
    return score


def eligible_for_premium(text: str, price: int | None, discount: int | None) -> bool:
    """PREMIUM channel = only the BEST deals (user rule):
      * 80%+ discount (any price) — a true top-tier deal,
      * a sub-₹99 price — the hottest low-price loot,
      * ₹100–₹499 with a 70%+ discount OR an explicit lowest-price claim.
    Best multi-product LISTS and credit/bank-card offers are added to Premium
    directly by the router regardless of this gate."""
    if discount is not None and discount >= 80:
        return True
    if price is not None and price <= 99:
        return True
    return bool(
        price is not None and 100 <= price <= 499
        and ((discount is not None and discount >= 70) or has_lowest_price_claim(text))
    )


def format_premium_loot(text: str, price: int | None, discount: int | None) -> str:
    badges = []
    if discount is not None:
        badges.append(f"🔥 {discount}% OFF")
    if price is not None:
        badges.append(f"💎 DEAL PRICE ₹{price}")
    if has_lowest_price_claim(text):
        badges.append("📉 LOWEST-PRICE ALERT")
    return (
        "👑 PREMIUM LOOT PICK 👑\n"
        + "\n".join(badges)
        + f"\n\n{text.strip()}\n\n✨ Handpicked • Verified Link • Grab Fast"
    )


def tricks_footer() -> str:
    return (
        "🔥 JOIN OUR COMPLETE LOOT FAMILY\n\n"
        f"📂 All Loot Channels — One Tap\n👉 {OUR_FOLDER_LINK}\n\n"
        f"🔒 Secret Loot India\n👉 {OUR_MAIN_CHANNEL_LINKS[0]}\n\n"
        f"⚡ Loot Zone India\n👉 {OUR_MAIN_CHANNEL_LINKS[1]}"
    )


def extract_deal_prices(text: str) -> list[int]:
    values = []
    for line in (text or "").splitlines():
        if re.search(r"\b(?:mrp|regular\s*price|coupon|cashback|bank)\b", line, re.I):
            continue
        for raw in re.findall(r"₹\s*([\d,]+)", line):
            value = int(raw.replace(",", ""))
            if 1 <= value <= 1_000_000:
                values.append(value)
    return values


def extract_urls(msg) -> list[str]:
    values: list[str] = []
    text = msg.text or msg.message or ""
    surrogate_text = add_surrogate(text)
    values.extend(URL_RE.findall(text))
    for intent in INTENT_RE.findall(text):
        parsed = parse_intent(intent)
        if parsed:
            values.append(parsed)
    for entity in getattr(msg, "entities", None) or []:
        if isinstance(entity, MessageEntityTextUrl) and entity.url:
            values.append(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            # Telegram offsets are UTF-16 code units. Python indexes Unicode
            # code points, so emoji before a URL otherwise shifts/slices it.
            values.append(del_surrogate(
                surrogate_text[entity.offset:entity.offset + entity.length]
            ))
    markup = getattr(msg, "reply_markup", None)
    if isinstance(markup, ReplyInlineMarkup):
        for row in markup.rows:
            for button in row.buttons:
                if isinstance(button, KeyboardButtonUrl) and button.url:
                    values.append(button.url)
    seen, result = set(), []
    for value in values:
        value = clean_url(value)
        if value.startswith("http") and value not in seen:
            seen.add(value)
            result.append(value)
    return result

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
CK_FOOTER_RE = re.compile(
    r"\n?#Flipkart\s*\n[-—_ ]*\n💰\s*Want Real Cash Back Too\?.*?"
    r"Forward to\s*@cashkarolink_?bot\s*\n[-—_ ]*",
    re.I | re.S,
)
SOURCE_NOISE_LINE_RE = re.compile(
    r"(?im)^\s*(?:"
    r".*deal\s*time\s*:.*(?:IST)?|"
    r".*\bloot\s+fa+s+\s*t+\b.*|"
    r".*(?:@GrabOnIndiaOfficial|50\+\s*loots\s*daily).*|"
    r"\s*(?:h|ht|htt|https?|ttp|ttps|tps?://|s://|://|uy)\s*|"
    r"👉.*(?:https\s*:\s*are)|"
    r"💰?\s*want\s+real\s+cash\s*back\s+too\??|"
    r"forward\s+to\s+@cashkarolink_?bot|"
    r"#(?:myntra|flipkart|amazon|ajio)"
    r")\s*$"
)
# Source-channel promo / navigation boilerplate (join/follow/share/notify CTAs,
# t.me self-promo, deal-time, @handles, emoji-only lines) that is NOT part of a
# deal. Stripped only when the line has no real deal content (no link, price,
# discount % or 3+ product words), mirroring the WhatsApp bridge cleanup.
PROMO_PATTERNS_PY = [
    re.compile(r"t\.me/|telegram\.(?:me|dog)/", re.I),
    re.compile(r"whatsapp\.com/(?:channel|invite)|wa\.me/", re.I),
    re.compile(r"\bjoin\b", re.I),
    re.compile(r"\bsubscribe\b|\bfollow\s+(?:us|our)\b", re.I),
    re.compile(r"\bshare\b[^.\n]{0,40}\b(with|your|everyone|friends?|groups?|channel|family)\b", re.I),
    re.compile(r"\bforward\b[^.\n]{0,30}\b(to|our|everyone|friends?|groups?)\b", re.I),
    re.compile(r"\b(click|tap)\s+(?:here|link|below|on\s+the\s+link)\b", re.I),
    re.compile(r"\b(turn\s+on|enable|activate)\b[^.\n]{0,25}\bnotifications?\b", re.I),
    re.compile(r"\b(?:don'?t|do\s+not|never)\s+miss\b|\bmiss\s+(?:it|this|out)\b", re.I),
    re.compile(r"\bdeal\s*time\s*[:\-]", re.I),
    re.compile(r"\bgrab\s+(?:it|fast|now|your|this)\b", re.I),
    re.compile(r"\bhurry?\s*up\b", re.I),
    re.compile(r"\bstay\s+(?:tuned|connected|updated)\b", re.I),
    re.compile(r"📢|🔔|📣|⏰"),
]
_PROMO_WORD_RE = re.compile(
    r"t\.me|telegram|whatsapp|join|subscribe|follow|share|forward|click|tap|"
    r"notification|notifications|hurry|grab|miss|deal|deals|offer|offers|loot|loots|"
    r"channel|group|friends?|everyone|family|buy|shop|order|now|here|link|below|"
    r"this|that|your|our|us|out|on|off|and|the|for|get|more|new|daily|fast|time|"
    r"turn|enable|activate|don'?t|do|not|never", re.I)


def is_promo_noise_line(line: str) -> bool:
    """True only for pure promo/navigation boilerplate (no deal content)."""
    t = (line or "").strip()
    if not t:
        return False
    if URL_RE.search(t):
        return False
    if re.search(r"[₹$]|\b(?:rs\.?|inr|mrp)\b", t, re.I):
        return False
    if re.search(r"\d+\s*%", t):
        return False
    if re.match(r"^[@#][\w]+$", t):
        return True
    if not re.search(r"[A-Za-z0-9\u0900-\u097F\u0C00-\u0C7F]", t):
        return True
    content_words = [w for w in _PROMO_WORD_RE.sub(" ", t).split() if re.search(r"[A-Za-z0-9]", w)]
    if len(content_words) >= 3:
        return False
    return any(rx.search(t) for rx in PROMO_PATTERNS_PY)


# STRICT global CTA/promo phrases removed ANYWHERE on a source line (not just
# the end). Only unambiguous channel boilerplate is listed; genuine deal words
# ("use code X", "free shipping", coupon/cashback/product terms) are untouched.
# Runs only on SOURCE text (our own headers/footers are added afterwards, so our
# branding is never affected).
GLOBAL_CTA_PATTERNS_PY = (
    r"\b(?:buy|shop|order|grab|get)\s+(?:it\s+)?now\b[!^0-9]*",
    r"\bgrab\s+(?:it|this|your|yours|fast)\s*(?:now|fast|soon)?\b[!^0-9]*",
    r"\bget\s+yours?\b[!^0-9]*",
    r"\bbuy\s+(?:it\s+)?here\b", r"\bshop\s+here\b", r"\border\s+here\b",
    r"\b(?:click|tap)\s+(?:here|the\s+link|on\s+(?:the\s+)?link|below|to\s+(?:buy|order|shop))\b[^.\n|]*",
    r"\b(?:don'?t|do\s+not|never)\s+miss\s+(?:it|this|out|the\s+deal|this\s+deal)\b[^.\n|]*",
    r"\bhurry\s*up?\b[!^0-9]*",
    r"\bturn\s+on\s+notifications?\b[^.\n|]*",
    r"\bstay\s+tuned\b[^.\n|]*",
    r"\blink\s+(?:in\s+(?:bio|comments?|description)|below)\b[^.\n|]*",
    r"\bcheck\s+(?:link|bio|description|comments?|pinned|our\s+channel)\b[^.\n|]*",
    r"\bshare\s+(?:it\s+)?(?:with|to)\s+[^.\n|]*\b(?:friends?|family|groups?|everyone)\b[^.\n|]*",
    r"\bforward\s+to\s+@?\w+[^.\n|]*",
    r"\b(?:visit|open)\s+(?:our\s+)?(?:channel|t\.me/\S+|whatsapp\s+channel)\b[^.\n|]*",
    r"\b(?:join|subscribe|follow)\s+(?:our\s+)?(?:us\s+)?(?:channel|telegram|whatsapp\s+channel|group|now)\b[^.\n|₹$]*?(?=$|[.\n|])",
    r"\b(?:join|subscribe|follow)\s+(?:our\s+)?(?:us\s+)?(?:on|via)?\s*t\.me/\S+",
    r"\b(?:for\s+more|more\s+)(?:loot|deal|update|offer)s?\b[^.\n|₹$]*$",
    r"\bt\.me/\S+", r"\bwhatsapp\.com/(?:channel|invite)/\S+", r"\bwa\.me/\S+",
)


def strip_inline_cta(line: str) -> str:
    """Remove CTA/promo from a deal line so channel boilerplate never appears
    next to the price. Global phrases are stripped anywhere on the line; a
    trailing CTA is trimmed to the sentence boundary. Product name, price, MRP,
    coupon ("use code X") and specs are left intact."""
    out = (line or "").replace("*", " ")
    for pattern in GLOBAL_CTA_PATTERNS_PY:
        out = re.sub(pattern, " ", out, flags=re.I)
    patterns = (
        r"\b(?:buy|shop|order|grab|get|check|add\s+to\s+cart)\s+"
        r"(?:it|now|fast|soon|today|yours?|this|the\s+deal|deal|fast\s+guys?|guys?)\b[^.|\n]*$",
        r"\b(?:click|tap)\s+(?:here|link|below|on\s+(?:the\s+)?link|to\s+(?:buy|order|shop))\b[^.|\n]*$",
        r"\b(?:don'?t|do\s+not|never)\s+miss\b[^.|\n]*$",
        r"\bmiss\s+(?:it|this|out|the\s+deal)\b[^.|\n]*$",
        r"\b(?:hurry?\s*up?|grab\s+(?:it|fast|now|your|this)|loot\s+fast|"
        r"deal\s+time[^\n]*|limited(?:\s*time)?\s+offer)\b[^.|\n]*$",
        # Social/channel CTAs (join/follow/share/notifications/t.me) are handled
        # by the bounded global patterns above so a following price is never eaten.
        r"\b(?:link\s+in\s+bio|link\s+below|check\s+(?:link|bio|description|comments?|pinned))\b[^.|\n]*$",
    )
    for pattern in patterns:
        out = re.sub(pattern, " ", out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out)
    return re.sub(r"[\s|*•:,\-]+$", "", out).strip()


def strip_promo_lines(text: str) -> str:
    """Drop source promo/navigation lines from a Telegram/WhatsApp body, and
    strip a CTA fragment glued to the end of a real deal line (price/discount
    lines are kept as lines, only the channel junk on them is removed)."""
    kept = []
    for ln in (text or "").splitlines():
        if is_promo_noise_line(ln):
            continue
        kept.append(strip_inline_cta(ln))
    return "\n".join(kept)


BUTTON_CHROME_RE = re.compile(r"(?i)\b(?:buy\s+(?:now|here)|shop\s+now)\b|🛒|>>+")
# Broken Telegram/Markdown entity remnants occasionally appear as standalone
# lines such as `B]()` / `]()` / `Buy]()` after a hidden Buy Now URL is rebuilt.
ORPHAN_MARKDOWN_LINE_RE = re.compile(
    r"(?im)^\s*(?:(?:b|bu|buy|buy\s+now)\s*)?\]?\s*\(\s*\)\s*$"
)


ORPHAN_URL_FRAGMENTS = {
    "h", "ht", "htt", "http", "https", "ttp", "ttps",
    "tps://", "tp://", "s://", "://", "uy",
}


def remove_orphan_url_fragment_lines(text: str) -> str:
    kept = []
    for line in (text or "").splitlines():
        # Ignore emojis/arrows/zero-width/formatting around tiny broken pieces,
        # e.g. `👉h`, `htt`, or `tps://` left by malformed Telegram entities.
        core = re.sub(r"[^A-Za-z:/]", "", line).lower()
        if core in ORPHAN_URL_FRAGMENTS:
            continue
        kept.append(line)
    return "\n".join(kept)


def normalize_nested_link_markup(text: str) -> str:
    """Normalise a FORWARDED/malformed source whose links are wrapped in nested
    markdown and double HTML-escaping, e.g.
      [[https://..&amp;amp;psc=1...](https://..&amp;psc=1...)](https://..&psc=1...)
    Leaves ONE clean URL per bracket group and decodes &amp;amp; -> & fully."""
    if not text:
        return text
    out = text
    # Fully decode double/triple HTML escapes (&amp;amp; -> &amp; -> &).
    for _ in range(4):
        decoded = html.unescape(out)
        if decoded == out:
            break
        out = decoded
    # Markdown link: [LABEL](URL). Collapse "[URL](URL)" (label is itself a URL)
    # and "[junk](URL)" down to the single URL, repeatedly (nested wrappers).
    for _ in range(4):
        prev = out
        out = re.sub(r"\[\s*(https?://[^\s\]\[]+?)\s*\]\s*\(\s*https?://[^\s)]+?\s*\)",
                     r"\1", out, flags=re.I)
        out = re.sub(r"\[[^\]\[]*?\]\s*\(\s*(https?://[^\s)]+?)\s*\)",
                     r"\1", out, flags=re.I)
        if out == prev:
            break
    # Leftover stray square brackets are noise.
    out = re.sub(r"[\[\]]+", "", out)
    # Collapse the SAME product link stacked several times in one line (nested
    # copies) down to one, keeping a short text prefix if there is one.
    lines = []
    for line in out.splitlines():
        stripped = line.strip().strip("()").strip()
        urls = URL_RE.findall(stripped)
        if len(urls) > 1:
            canon, seen = [], set()
            for u in urls:
                key = extract_product_id(u) or canonical_url(u)
                if key not in seen:
                    seen.add(key)
                    canon.append(clean_url(u).rstrip("()"))
            if canon:
                prefix = re.sub(r"https?://\S+", "", stripped)
                prefix = re.sub(r"[()]+", "", prefix).strip(" \t|")
                stripped = (((prefix + "\n") if prefix else "") + "\n".join(canon)).strip()
        lines.append(stripped)
    return "\n".join(lines)


def clean_source_text(text: str) -> str:
    text = (text or "").replace("\x00", "")
    text = normalize_nested_link_markup(text)
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    text = CK_FOOTER_RE.sub("\n", text)
    text = SOURCE_NOISE_LINE_RE.sub("", text)
    text = remove_orphan_url_fragment_lines(text)
    # Remove URL-tail garbage accidentally glued to a price by malformed source
    # entities (e.g. `₹122oya`) without changing normal words elsewhere.
    text = re.sub(r"(₹\s*[\d,]+)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{2,12}\b", r"\1", text)
    # Random mixed letter+digit token AFTER a price ("₹185 VN7z") is source
    # corruption, never real content. Real units (2pcs, 500ml, 65w...) survive.
    text = re.sub(
        r"(₹\s*[\d,]+)[ \t]+"
        r"(?!\d+(?:pcs?|packs?|pairs?|kg|gm?|ml|ltrs?|l|cm|mm|mah|gb|tb|w|v|inch(?:es)?)\b)"
        r"(?=[A-Za-z\d]*\d)(?=\d*[A-Za-z])[A-Za-z\d]{3,14}(?=[ \t]|$)",
        r"\1", text, flags=re.M | re.I,
    )
    text = ORPHAN_MARKDOWN_LINE_RE.sub("", text)
    text = re.sub(r"(?im)^\s*[^\w\n]*(?:sent\s+via|via\s+\w+\s+admin).*$", "", text)
    text = re.sub(r"(?im)^\s*(?:follow|join|subscribe).*@\w+.*$", "", text)
    # Drop source promo/navigation lines (join/follow/share/notify, t.me
    # self-promo, deal-time, handles, emoji-only) that carry no deal content.
    text = strip_promo_lines(text)
    return text.strip()


def rebuild_text(text: str, entities, mapping: dict[str, str]) -> str:
    """Replace URLs using Telegram's UTF-16 entity offsets safely."""
    text = text or ""
    surrogate_text = add_surrogate(text)
    spans: list[tuple[int, int, str, str]] = []
    for entity in entities or []:
        if isinstance(entity, MessageEntityTextUrl) and entity.url:
            spans.append((entity.offset, entity.offset + entity.length, entity.url, "texturl"))
        elif isinstance(entity, MessageEntityUrl):
            source = clean_url(del_surrogate(
                surrogate_text[entity.offset:entity.offset + entity.length]
            ))
            spans.append((entity.offset, entity.offset + entity.length, source, "url"))
    spans.sort()
    # Remove overlapping formatting/entity spans safely.
    filtered: list[tuple[int, int, str, str]] = []
    for span in spans:
        if not filtered or span[0] >= filtered[-1][1]:
            filtered.append(span)

    def replace_literal(segment: str) -> str:
        def repl(match) -> str:
            raw = match.group(0)
            return mapping.get(clean_url(raw), "")
        return URL_RE.sub(repl, segment)

    out, pos = [], 0
    for start, end, source_url, kind in filtered:
        out.append(replace_literal(surrogate_text[pos:start]))
        aff = mapping.get(clean_url(source_url), "")
        label = del_surrogate(surrogate_text[start:end])
        if aff:
            out.append(aff if kind == "url" or BUTTON_CHROME_RE.search(label) else f"{label}: {aff}")
        elif kind == "texturl" and not BUTTON_CHROME_RE.search(label):
            out.append(label)
        pos = end
    out.append(replace_literal(surrogate_text[pos:]))
    return del_surrogate("".join(out))


def tidy_post(text: str) -> str:
    """Remove source chrome without ever mutating a generated affiliate URL."""
    text = ORPHAN_MARKDOWN_LINE_RE.sub("", text or "")
    protected: list[str] = []

    def mask(match) -> str:
        protected.append(clean_url(match.group(0)))
        return f"\x01URL{len(protected)-1}\x02"

    # Protect links first: underscores, parentheses and tracking query values are valid URL data.
    masked = URL_RE.sub(mask, text)
    masked = BUTTON_CHROME_RE.sub(" ", masked)
    # Remove malformed source-link text glued into labels/prices while genuine
    # generated URLs are safely masked above.
    masked = re.sub(r"(?i)\bhtt[A-Za-z0-9/:._-]*", "", masked)
    masked = re.sub(r"\bh(?=[A-Z][a-z])", "", masked)
    masked = re.sub(r"(₹\s*[\d,]+)[A-Za-z]{1,8}\b", r"\1", masked)
    masked = masked.replace("*", "").replace("_", "")
    masked = re.sub(r"[\[\]()]", " ", masked)
    masked = re.sub(r"[ \t]+", " ", masked)
    masked = re.sub(r"(?m)^\s*[:>-]+\s*$", "", masked)
    for index, url in enumerate(protected):
        masked = masked.replace(f"\x01URL{index}\x02", url)

    # Remove orphan link bullets with no actual URL after them, e.g. a leftover
    # `🔗` or `🔗 ` line whose source URL was collapsed/deduped away. A bullet
    # alone is not a link and should never appear in the final post.
    masked = re.sub(r"(?m)^\s*(?:🔗|👉|➡️|🔎|🔍|•|▪️|-|–|—|\*)?\s*(?:https?://\s*)?\s*$", "", masked)

    # Dedupe all URLs globally while preserving the first occurrence and line layout.
    seen: set[str] = set()
    lines: list[str] = []
    for original_line in masked.splitlines():
        line = original_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        for raw in URL_RE.findall(line):
            url = clean_url(raw)
            if url in seen:
                line = line.replace(raw, "", 1)
            else:
                seen.add(url)
                line = line.replace(raw, url, 1)
        line = re.sub(r"\s{2,}", " ", line)
        line = re.sub(r"\s+(?:\+|\||->|--+)\s*$", "", line).strip(" :")
        if line:
            lines.append(line)

    while lines and lines[-1] == "":
        lines.pop()
    result: list[str] = []
    for line in lines:
        if line == "" and (not result or result[-1] == ""):
            continue
        result.append(line)
    return "\n".join(result).strip()


def remove_source_url_residue(text: str, source_urls: Iterable[str],
                              protected_urls: Iterable[str]) -> str:
    """Remove malformed source-shortlink tails while protecting generated links."""
    protected = list(dict.fromkeys(clean_url(url) for url in protected_urls))
    masked = text or ""
    for index, url in enumerate(protected):
        masked = masked.replace(url, f"\x01AFF{index}\x02")

    identifiers: set[str] = set()
    for source in source_urls:
        try:
            parsed = urlparse(clean_url(source))
            material = parsed.path + "&" + parsed.query
            for token in re.findall(r"[A-Za-z0-9_-]{4,}", material):
                if token.lower() not in {"product", "search", "visitretailer", "source", "default"}:
                    identifiers.add(token.lower())
        except Exception:
            pass

    output = []
    for line in masked.splitlines():
        words = line.split()
        rebuilt = []
        for word in words:
            lower = word.lower()
            malformed_domain = re.search(
                r"(?:bitl+i|myntr|fkrt|fktr|aji+o)\.in/", lower
            )
            contains_id = any(identifier in lower for identifier in identifiers)
            if malformed_domain or contains_id:
                price = re.search(r"₹\s*[\d,]+", word)
                if price:
                    rebuilt.append(price.group(0))
                continue
            rebuilt.append(word)
        cleaned_line = " ".join(rebuilt).strip()
        if re.fullmatch(r"(?i)(?:use\s*code|link|p\.c)\s*:?\s*", cleaned_line):
            continue
        output.append(cleaned_line)
    masked = "\n".join(output)
    for index, url in enumerate(protected):
        masked = masked.replace(f"\x01AFF{index}\x02", url)
    return masked


MEANINGFUL_SINGLE_LABELS = {
    "men", "mens", "women", "womens", "unisex", "blue", "black", "white",
    "red", "green", "yellow", "brown", "orange", "pink", "purple", "grey",
    "gray", "beige", "gold", "silver", "small", "medium", "large",
}


def remove_trailing_url_tokens(text: str) -> str:
    """Drop random short IDs glued after an otherwise complete generated URL."""
    output = []
    for line in (text or "").splitlines():
        # Source entity corruption commonly leaves an ASIN/short-code tail after
        # the replaced URL, e.g. `https://...tag=ours 0GLY3Q2X`.
        line = re.sub(
            r"(https?://[^\s]+)(?:\s+[A-Za-z0-9_-]{2,16})+\s*$",
            r"\1",
            line,
        )
        output.append(line)
    return "\n".join(output)


def strict_orphan_token_cleanup(text: str) -> str:
    """Remove any isolated random short token, not just previously seen examples."""
    text = remove_trailing_url_tokens(text)
    # A source post can carry a dangling protocol fragment right next to a real
    # link on the same line, e.g. `https://ajio.in/XXX https://`. Mask the real
    # URLs, drop the orphan protocol/domain fragment, then restore the links so
    # a genuine generated URL is never mutated.
    real_urls = list(dict.fromkeys(clean_url(u) for u in URL_RE.findall(text or "")))
    masked = text
    for i, url in enumerate(real_urls):
        masked = masked.replace(url, f"\x01U{i}\x02")
    masked = re.sub(r"\bhttps?://[^\s\x01]*", "", masked)
    masked = re.sub(r"\s*\b(?:https?|htt|ftp)\b", "", masked)
    for i, url in enumerate(real_urls):
        masked = masked.replace(f"\x01U{i}\x02", url)

    lines = (masked or "").splitlines()
    kept = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        # A line that is only a link-bullet emoji / bullet punctuation with no
        # URL, letters or digits is a dangling link slot and must be removed.
        if not URL_RE.search(stripped) and not re.search(r"[A-Za-z0-9]", stripped):
            if re.fullmatch(r"[\s🔗👉➡️🔎🔍•▪️\-–—_|:*]+", stripped):
                continue
        token_match = re.fullmatch(r"[A-Za-z0-9_-]{1,12}", stripped)
        if token_match:
            lower = stripped.lower()
            # Broken URL fragments (h / htt / uy / ...) are never real labels.
            # Drop them even when adjacent to a URL — the adjacency exception
            # exists for genuine labels (sizes/colors) near links, and keeping
            # junk fragments there leaked raw 'h'/'htt' lines into final posts.
            if lower in ORPHAN_URL_FRAGMENTS:
                continue
            previous = lines[index - 1].strip() if index else ""
            next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
            adjacent_url = bool(URL_RE.fullmatch(previous) or URL_RE.fullmatch(next_line))
            coupon_context = bool(re.search(r"(?i)use\s*code\s*: ?$", previous))
            if lower not in MEANINGFUL_SINGLE_LABELS and not adjacent_url and not coupon_context:
                continue
        # Remove malformed protocol-like words wherever they appear outside a
        # real URL (real URLs are normally on their own and fail this pattern).
        if not URL_RE.fullmatch(stripped):
            stripped = re.sub(r"(?i)\b(?:htt|ttp|tps|s)[:/][A-Za-z0-9/:._-]*", "", stripped)
            stripped = re.sub(r"\s{2,}", " ", stripped).strip(" :|+-")
            if not stripped:
                continue
        kept.append(stripped)
    result = "\n".join(kept)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def format_visible_source_product_pairs(raw_text: str,
                                        mapping: dict[str, str]) -> str | None:
    """Rebuild visible product-list posts by source order, not fragile entity spans."""
    lines = clean_source_text(raw_text).splitlines()
    used_label_lines: set[int] = set()
    pairs: list[tuple[str, str]] = []
    used_affiliates: set[str] = set()

    for index, line in enumerate(lines):
        raw_urls = URL_RE.findall(line)
        if not raw_urls:
            continue
        for raw_url in raw_urls:
            affiliate = mapping.get(clean_url(raw_url))
            if not affiliate or affiliate in used_affiliates:
                continue
            prefix = line.split(raw_url, 1)[0]
            prefix = re.sub(r"^[\s🔗👉➡️:|*-]+|[\s:|*-]+$", "", prefix).strip()
            label = prefix
            label_index = index
            if not label:
                for previous in range(index - 1, -1, -1):
                    candidate = lines[previous].strip()
                    if (not candidate or previous in used_label_lines
                            or URL_RE.search(candidate)):
                        continue
                    candidate = re.sub(r"^[\s🔗👉➡️:|*-]+|[\s:|*-]+$", "", candidate).strip()
                    if candidate:
                        label, label_index = candidate, previous
                        break
            if label:
                label = tidy_post(label)
                if label:
                    pairs.append((label, affiliate))
                    used_label_lines.add(label_index)
                    used_affiliates.add(affiliate)

    if len(pairs) < 3:
        return None
    output = []
    for label, affiliate in pairs:
        output.extend([label, affiliate, ""])
    return "\n".join(output).strip()


def format_clustered_product_list(text: str) -> str:
    """Pair 3+ product/price labels with a trailing block of generated URLs."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    url_positions = [i for i, line in enumerate(lines) if URL_RE.fullmatch(line)]
    if len(url_positions) < 3:
        return text
    first_url = url_positions[0]
    # Only fix posts whose generated URLs are all grouped at the bottom.
    if any(not URL_RE.fullmatch(line) for line in lines[first_url:]):
        return text
    labels = [line for line in lines[:first_url] if re.search(r"₹\s*[\d,]+", line)]
    if len(labels) < 3:
        return text
    urls = [lines[i] for i in url_positions]
    pair_count = min(len(labels), len(urls))
    first_label_index = min(lines.index(label) for label in labels)
    preamble = [line for line in lines[:first_label_index] if line not in labels]
    output = list(preamble)
    for index in range(pair_count):
        if output:
            output.append("")
        output.extend([labels[index], urls[index]])
    return "\n".join(output).strip()


def _premium_windows(around: datetime) -> list[tuple[datetime, datetime, str]]:
    windows = []
    for day_offset in (-1, 0, 1, 2):
        day = (around + timedelta(days=day_offset)).date()
        base = datetime.combine(day, datetime.min.time(), tzinfo=IST)
        windows.extend([
            (base.replace(hour=6), base.replace(hour=8), f"{day.isoformat()}-AM"),
            (base.replace(hour=13), base.replace(hour=14), f"{day.isoformat()}-PM"),
            (base.replace(hour=18), (base + timedelta(days=1)).replace(hour=1), f"{day.isoformat()}-EVE"),
        ])
    return sorted(windows, key=lambda item: item[0])


def premium_time_status(now_ts: float | None = None) -> tuple[bool, str, float]:
    """Premium windows: 06–08, 13–14, and 18–01 IST."""
    now = datetime.fromtimestamp(now_ts or time.time(), IST)
    windows = _premium_windows(now)
    for index, (start, end, key) in enumerate(windows):
        if start <= now < end:
            next_start = next((item[0] for item in windows[index + 1:] if item[0] >= end), end)
            return True, key, next_start.timestamp()
    for start, _end, key in windows:
        if start > now:
            return False, key, start.timestamp()
    fallback = now + timedelta(days=1)
    return False, fallback.date().isoformat() + "-AM", fallback.replace(
        hour=6, minute=0, second=0, microsecond=0
    ).timestamp()


def chunks(text: str, limit: int) -> list[str]:
    """Split at line boundaries; never silently truncate Telegram content."""
    if len(text) <= limit:
        return [text]
    result, current = [], ""
    for line in text.splitlines(True):
        if len(line) > limit:
            if current:
                result.append(current.rstrip())
                current = ""
            result.extend(line[i:i + limit] for i in range(0, len(line), limit))
        elif len(current) + len(line) > limit:
            result.append(current.rstrip())
            current = line
        else:
            current += line
    if current.strip():
        result.append(current.rstrip())
    return result

# ---------------------------------------------------------------------------
# SQLite durable state
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self._schema()

    def _schema(self) -> None:
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS queue (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL,
          msg_id INTEGER NOT NULL,
          source TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0,
          rendered_text TEXT,
          targets_json TEXT,
          deal_keys_json TEXT,
          premium_score INTEGER,
          last_error TEXT,
          created_at REAL NOT NULL,
          UNIQUE(chat_id,msg_id)
        );
        CREATE TABLE IF NOT EXISTS posted_deals (
          deal_key TEXT PRIMARY KEY,
          price INTEGER,
          posted_at REAL NOT NULL,
          queue_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deal_claims (
          deal_key TEXT PRIMARY KEY,
          queue_id INTEGER NOT NULL,
          claimed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_posts (
          price INTEGER PRIMARY KEY,
          posted_at REAL NOT NULL,
          queue_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deliveries (
          queue_id INTEGER NOT NULL,
          target TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          chunks_sent INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          PRIMARY KEY(queue_id,target)
        );
        CREATE TABLE IF NOT EXISTS link_cache (
          source_url TEXT PRIMARY KEY,
          affiliate_url TEXT NOT NULL,
          resolved_url TEXT,
          deal_key TEXT,
          created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS premium_state (
          id INTEGER PRIMARY KEY CHECK(id=1),
          night_key TEXT,
          sent_count INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0,
          claim_queue_id INTEGER,
          claim_at REAL NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO premium_state(id) VALUES(1);
        """)
        # Backward-compatible migrations.
        delivery_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(deliveries)")}
        if "chunks_sent" not in delivery_columns:
            self.conn.execute("ALTER TABLE deliveries ADD COLUMN chunks_sent INTEGER NOT NULL DEFAULT 0")
        queue_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(queue)")}
        if "premium_score" not in queue_columns:
            self.conn.execute("ALTER TABLE queue ADD COLUMN premium_score INTEGER")
        if "priority" not in queue_columns:
            self.conn.execute("ALTER TABLE queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 1")
        # A previous crash must not leave work permanently stuck.
        self.conn.execute("UPDATE queue SET status='pending' WHERE status='processing'")
        self.conn.commit()
        self._migrate_legacy_json()

    @staticmethod
    def _legacy_time(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                from datetime import datetime
                return datetime.fromisoformat(value).timestamp()
        return 0.0

    def _migrate_legacy_json(self) -> None:
        """Import recent v13/v14 JSON history once, preventing night→morning reposts."""
        legacy = BASE_DIR / "dedup_data.json"
        marker = BASE_DIR / ".dedup_json_migrated"
        if marker.exists() or not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            cutoff = time.time() - PRODUCT_DEDUP_SECONDS
            pids = data.get("pid") or data.get("asin") or {}
            urls = data.get("url") or {}
            prices = data.get("price") or {}
            for raw_key, raw_time in pids.items():
                posted_at = self._legacy_time(raw_time)
                if posted_at < cutoff:
                    continue
                key = str(raw_key)
                if re.fullmatch(r"B[A-Z0-9]{9}", key, re.I):
                    key = "ASIN:" + key.upper()
                self.conn.execute(
                    "INSERT OR IGNORE INTO posted_deals VALUES(?,?,?,?)",
                    (key, None, posted_at, 0),
                )
            for raw_url, raw_time in urls.items():
                posted_at = self._legacy_time(raw_time)
                if posted_at >= cutoff:
                    key = "URL:" + hashlib.sha256(canonical_url(str(raw_url)).encode()).hexdigest()
                    self.conn.execute(
                        "INSERT OR IGNORE INTO posted_deals VALUES(?,?,?,?)",
                        (key, None, posted_at, 0),
                    )
            for raw_price, raw_time in prices.items():
                posted_at = self._legacy_time(raw_time)
                if posted_at >= time.time() - PRICE_DEDUP_SECONDS:
                    with contextlib.suppress(ValueError):
                        self.conn.execute(
                            "INSERT OR IGNORE INTO price_posts VALUES(?,?,?)",
                            (int(float(raw_price)), posted_at, 0),
                        )
            self.conn.commit()
            marker.write_text(str(time.time()), encoding="utf-8")
            log.info("Migrated recent legacy dedup history into SQLite")
        except Exception as exc:
            log.warning("Legacy dedup migration skipped: %s", exc)

    async def enqueue(self, chat_id: int, msg_id: int, source: str,
                      text: str = "", has_media: bool = False) -> bool:
        # Best-lists first, then super discounts, then photos — so the highest
        # value Telegram post is rendered/delivered ahead of ordinary ones.
        priority = classify_priority(text, has_media)
        async with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO queue(chat_id,msg_id,source,created_at,priority) "
                "VALUES(?,?,?,?,?)",
                (chat_id, msg_id, source, time.time(), priority),
            )
            self.conn.commit()
            return cur.rowcount > 0

    async def claim_job(self) -> sqlite3.Row | None:
        async with self.lock:
            now = time.time()
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT * FROM queue WHERE status='pending' AND next_at<=? "
                "ORDER BY priority DESC, created_at ASC LIMIT 1", (now,)
            ).fetchone()
            if row:
                # next_at doubles as the claim timestamp while processing, so a
                # job orphaned mid-flight (kill -9, OOM) can be reclaimed later.
                self.conn.execute("UPDATE queue SET status='processing', next_at=? WHERE id=?", (now, row["id"]))
            self.conn.commit()
            return row

    async def reclaim_stuck_jobs(self, max_age_seconds: float = 1800) -> int:
        """Return orphaned 'processing' rows to 'pending'.

        Startup already resets processing rows, but a job abandoned while the
        process stays up (task killed mid-await, watchdog timeout) would
        otherwise stay 'processing' forever and its deal would never post.
        """
        async with self.lock:
            cur = self.conn.execute(
                "UPDATE queue SET status='pending' WHERE status='processing' AND next_at<?",
                (time.time() - max_age_seconds,),
            )
            self.conn.commit()
            return cur.rowcount

    async def save_render(self, queue_id: int, text: str, targets: list[str], keys: list[str],
                          premium_rank: int | None = None) -> None:
        async with self.lock:
            self.conn.execute(
                "UPDATE queue SET rendered_text=?,targets_json=?,deal_keys_json=?,premium_score=? WHERE id=?",
                (text, json.dumps(targets), json.dumps(keys), premium_rank, queue_id),
            )
            for target in targets:
                self.conn.execute(
                    "INSERT OR IGNORE INTO deliveries(queue_id,target) VALUES(?,?)", (queue_id, target)
                )
            self.conn.commit()

    async def cached_link(self, source_url: str) -> sqlite3.Row | None:
        async with self.lock:
            return self.conn.execute(
                "SELECT * FROM link_cache WHERE source_url=?", (canonical_url(source_url),)
            ).fetchone()

    async def cache_link(self, source_url: str, affiliate: str, resolved: str, key: str) -> None:
        async with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO link_cache VALUES(?,?,?,?,?)",
                (canonical_url(source_url), affiliate, resolved, key, time.time()),
            )
            self.conn.commit()

    @staticmethod
    def is_our_amazon_tag_link(url: str) -> bool:
        """An Amazon URL carrying OUR Associates tag is self-proving.

        The tag can only have been added by us (direct-Associates conversion or
        a direct tag write), so the link monetises to this account regardless
        of how it reached the rendered text. link_cache only stores conversion-
        API output, so our own direct Amazon links may legitimately be absent —
        right after deploy (cache invalidated), after an old-row prune, or
        whenever a post renders a tagged link produced outside the convert()
        path. The tag check (exactly as strict as valid_generated) is therefore
        sufficient provenance; a DB lookup here would only raise false
        "final provenance recheck failed" drops of good deals.
        """
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if not in_domains(host, AMAZON_DOMAINS):
                return False
            query = {str(k).lower(): v for k, v in parse_qs(parsed.query).items()}
            return query.get("tag", [""])[0] == OUR_TAG
        except Exception:
            return False

    async def verify_generated_text(self, text: str,
                                    allowed_external: Iterable[str] = ()) -> bool:
        """Recheck every URL against conversion cache or an exact owned-link allowlist."""
        urls = list(dict.fromkeys(clean_url(x) for x in URL_RE.findall(text or "")))
        if not urls:
            return False
        allowed = {clean_url(x) for x in allowed_external}
        check_urls = [url for url in urls if url not in allowed]
        # Service/lifestyle offer links (Zomato/Swiggy/Zepto/movies/cards) are
        # not EarnKaro-monetized and never reach link_cache: exempt them while
        # every monetized store link keeps the full generated-link check.
        # Amazon links carrying OUR Associates tag are self-proving too (see
        # is_our_amazon_tag_link): the cache stores conversion-API output, not
        # our direct Associates links, so the DB check must not gate them.
        check_urls = [
            url for url in check_urls
            if not in_domains((urlparse(url).hostname or "").lower(), SERVICE_OFFER_DOMAINS)
            and not self.is_our_amazon_tag_link(url)
        ]
        # linkredirect.in is a source affiliate wrapper, never an allowed final URL.
        if any(in_domains((urlparse(url).hostname or "").lower(), FOREIGN_ECHO_DOMAINS)
               and not in_domains((urlparse(url).hostname or "").lower(), OUR_RUNTIME_SHORTENER_DOMAINS)
               for url in check_urls):
            return False
        async with self.lock:
            for url in check_urls:
                found = self.conn.execute(
                    "SELECT 1 FROM link_cache WHERE affiliate_url=? LIMIT 1", (url,)
                ).fetchone()
                if not found:
                    return False
        return True

    async def preview_allowed(self, text: str) -> bool:
        """Keep Amazon product previews; suppress Flipkart's generic redirect card."""
        urls = list(dict.fromkeys(clean_url(x) for x in URL_RE.findall(text or "")))
        async with self.lock:
            for url in urls:
                row = self.conn.execute(
                    "SELECT resolved_url FROM link_cache WHERE affiliate_url=? LIMIT 1", (url,)
                ).fetchone()
                resolved = row[0] if row and row[0] else url
                host = (urlparse(resolved).hostname or "").lower()
                if host_matches(host, "flipkart.com"):
                    return False
        return True

    async def reserve(self, queue_id: int, keys: list[str], price: int | None,
                      has_identity: bool, content_key: str | None = None) -> tuple[bool, list[str]]:
        """Atomically reserve new products plus an optional strict content fingerprint."""
        async with self.lock:
            now = time.time()
            cutoff = now - PRODUCT_DEDUP_SECONDS
            claim_cutoff = now - 20 * 60
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DELETE FROM posted_deals WHERE posted_at<?", (cutoff,))
            self.conn.execute("DELETE FROM deal_claims WHERE claimed_at<?", (claim_cutoff,))
            if content_key:
                content_posted = self.conn.execute(
                    "SELECT 1 FROM posted_deals WHERE deal_key=? AND posted_at>=?", (content_key, cutoff)
                ).fetchone()
                content_claimed = self.conn.execute(
                    "SELECT 1 FROM deal_claims WHERE deal_key=? AND queue_id<>?", (content_key, queue_id)
                ).fetchone()
                if content_posted or content_claimed:
                    self.conn.rollback()
                    return False, []
                self.conn.execute(
                    "INSERT OR REPLACE INTO deal_claims VALUES(?,?,?)", (content_key, queue_id, now)
                )
            new_keys = []
            for key in dict.fromkeys(keys):
                posted = self.conn.execute(
                    "SELECT 1 FROM posted_deals WHERE deal_key=? AND posted_at>=?", (key, cutoff)
                ).fetchone()
                claim = self.conn.execute(
                    "SELECT 1 FROM deal_claims WHERE deal_key=? AND queue_id<>?", (key, queue_id)
                ).fetchone()
                if not posted and not claim:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO deal_claims VALUES(?,?,?)", (key, queue_id, now)
                    )
                    new_keys.append(key)
            # User rule: regardless of ASIN/PID, the exact same detected price
            # must not be posted again from any source during the one-hour window.
            if price is not None and PRICE_DEDUP_SECONDS > 0:
                pp = self.conn.execute(
                    "SELECT 1 FROM price_posts WHERE price=? AND posted_at>=?",
                    (price, now - PRICE_DEDUP_SECONDS),
                ).fetchone()
                if pp:
                    self.conn.rollback()
                    return False, []
            if not new_keys:
                self.conn.rollback()
                return False, []
            self.conn.commit()
            return True, new_keys

    async def claim_content_key(self, queue_id: int, content_key: str | None) -> bool:
        """Backfill strict content dedup for jobs rendered by older deployments."""
        if not content_key:
            return True
        async with self.lock:
            now = time.time()
            cutoff = now - PRODUCT_DEDUP_SECONDS
            self.conn.execute("BEGIN IMMEDIATE")
            posted = self.conn.execute(
                "SELECT queue_id FROM posted_deals WHERE deal_key=? AND posted_at>=?",
                (content_key, cutoff),
            ).fetchone()
            if posted and int(posted[0]) != queue_id:
                self.conn.rollback()
                return False
            claimed = self.conn.execute(
                "SELECT queue_id FROM deal_claims WHERE deal_key=?", (content_key,)
            ).fetchone()
            if claimed and int(claimed[0]) != queue_id:
                self.conn.rollback()
                return False
            self.conn.execute(
                "INSERT OR REPLACE INTO deal_claims VALUES(?,?,?)", (content_key, queue_id, now)
            )
            row = self.conn.execute(
                "SELECT deal_keys_json FROM queue WHERE id=?", (queue_id,)
            ).fetchone()
            keys = json.loads((row[0] if row else None) or "[]")
            if content_key not in keys:
                keys.append(content_key)
                self.conn.execute(
                    "UPDATE queue SET deal_keys_json=? WHERE id=?", (json.dumps(keys), queue_id)
                )
            self.conn.commit()
            return True

    async def pending_targets(self, queue_id: int) -> list[str]:
        async with self.lock:
            rows = self.conn.execute(
                "SELECT target FROM deliveries WHERE queue_id=? AND status!='sent'", (queue_id,)
            ).fetchall()
            return [r[0] for r in rows]

    async def delivery_progress(self, queue_id: int, target: str) -> int:
        async with self.lock:
            row = self.conn.execute(
                "SELECT chunks_sent FROM deliveries WHERE queue_id=? AND target=?",
                (queue_id, target),
            ).fetchone()
            return int(row[0]) if row else 0

    async def set_delivery_progress(self, queue_id: int, target: str, chunks_sent: int) -> None:
        async with self.lock:
            self.conn.execute(
                "UPDATE deliveries SET chunks_sent=MAX(chunks_sent,?) WHERE queue_id=? AND target=?",
                (chunks_sent, queue_id, target),
            )
            self.conn.commit()

    async def delivery(self, queue_id: int, target: str, ok: bool, error: str = "") -> None:
        async with self.lock:
            self.conn.execute(
                "UPDATE deliveries SET status=?,attempts=attempts+1,last_error=? WHERE queue_id=? AND target=?",
                ("sent" if ok else "pending", error[:500], queue_id, target),
            )
            self.conn.commit()

    async def claim_premium(self, queue_id: int) -> tuple[bool, float]:
        """Atomically gate highest-score premium job for the evening window."""
        async with self.lock:
            now = time.time()
            inside, night_key, next_window = premium_time_status(now)
            self.conn.execute("BEGIN IMMEDIATE")
            state = self.conn.execute("SELECT * FROM premium_state WHERE id=1").fetchone()
            if state["night_key"] != night_key:
                self.conn.execute(
                    "UPDATE premium_state SET night_key=?,sent_count=0,next_at=0,claim_queue_id=NULL,claim_at=0 WHERE id=1",
                    (night_key,),
                )
                state = self.conn.execute("SELECT * FROM premium_state WHERE id=1").fetchone()
            if not inside:
                self.conn.commit()
                return False, next_window
            if state["sent_count"] >= PREMIUM_MAX_PER_NIGHT:
                self.conn.commit()
                return False, next_window
            if state["claim_queue_id"] and state["claim_queue_id"] != queue_id and state["claim_at"] >= now - 600:
                self.conn.commit()
                return False, max(now + 60, float(state["next_at"] or 0))
            top = self.conn.execute(
                """SELECT q.id FROM queue q JOIN deliveries d ON d.queue_id=q.id
                   WHERE d.target=? AND d.status!='sent' AND q.premium_score IS NOT NULL
                     AND q.status IN ('pending','processing')
                   ORDER BY q.premium_score DESC,q.created_at ASC LIMIT 1""",
                (PREMIUM_TARGET,),
            ).fetchone()
            if top and int(top[0]) != queue_id:
                self.conn.commit()
                return False, now + 60
            if now < float(state["next_at"] or 0):
                self.conn.commit()
                return False, float(state["next_at"])
            self.conn.execute(
                "UPDATE premium_state SET claim_queue_id=?,claim_at=? WHERE id=1",
                (queue_id, now),
            )
            self.conn.commit()
            return True, now

    async def complete_premium(self, queue_id: int, ok: bool) -> None:
        async with self.lock:
            state = self.conn.execute("SELECT claim_queue_id FROM premium_state WHERE id=1").fetchone()
            if not state or state[0] != queue_id:
                return
            if ok:
                next_at = time.time() + random.randint(PREMIUM_GAP_MIN_SECONDS, PREMIUM_GAP_MAX_SECONDS)
                self.conn.execute(
                    "UPDATE premium_state SET sent_count=sent_count+1,next_at=?,claim_queue_id=NULL,claim_at=0 WHERE id=1",
                    (next_at,),
                )
            else:
                self.conn.execute(
                    "UPDATE premium_state SET claim_queue_id=NULL,claim_at=0 WHERE id=1"
                )
            self.conn.commit()

    async def finish(self, row: sqlite3.Row, successes: int, price: int | None,
                     premium_defer_until: float | None = None) -> None:
        async with self.lock:
            queue_id = row["id"]
            now = time.time()
            # `row` was claimed before save_render(); refresh mutable fields so
            # successful delivery really commits the rendered product keys.
            fresh = self.conn.execute(
                "SELECT deal_keys_json,attempts FROM queue WHERE id=?", (queue_id,)
            ).fetchone()
            keys = json.loads((fresh["deal_keys_json"] if fresh else None) or "[]")
            attempts = int(fresh["attempts"] if fresh else row["attempts"])
            if successes:
                for key in keys:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO posted_deals VALUES(?,?,?,?)",
                        (key, price, now, queue_id),
                    )
                if price is not None and PRICE_DEDUP_SECONDS > 0:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO price_posts VALUES(?,?,?)", (price, now, queue_id)
                    )
                self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (queue_id,))
            remaining_rows = self.conn.execute(
                "SELECT target FROM deliveries WHERE queue_id=? AND status!='sent'", (queue_id,)
            ).fetchall()
            remaining_targets = [r[0] for r in remaining_rows]
            only_scheduled_premium = (
                remaining_targets == [PREMIUM_TARGET] and premium_defer_until is not None
            )
            if only_scheduled_premium:
                self.conn.execute(
                    "UPDATE queue SET status='pending',next_at=? WHERE id=?",
                    (max(now + 30, premium_defer_until), queue_id),
                )
            elif remaining_targets and attempts < 8:
                delay = min(300, 5 * (2 ** attempts))
                self.conn.execute(
                    "UPDATE queue SET status='pending',attempts=attempts+1,next_at=? WHERE id=?",
                    (now + delay, queue_id),
                )
            else:
                self.conn.execute("UPDATE queue SET status='done' WHERE id=?", (queue_id,))
                if not successes:
                    self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (queue_id,))
            self.conn.commit()

    async def fail_job(self, row: sqlite3.Row, error: str) -> None:
        async with self.lock:
            attempts = row["attempts"] + 1
            if attempts >= 8:
                status, next_at = "failed", 0
                self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (row["id"],))
            else:
                status, next_at = "pending", time.time() + min(300, 5 * (2 ** attempts))
            self.conn.execute(
                "UPDATE queue SET status=?,attempts=?,next_at=?,last_error=? WHERE id=?",
                (status, attempts, next_at, error[:1000], row["id"]),
            )
            self.conn.commit()

    async def mark_done(self, queue_id: int, reason: str = "") -> None:
        async with self.lock:
            self.conn.execute(
                "UPDATE queue SET status='done',last_error=? WHERE id=?", (reason[:1000], queue_id)
            )
            self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (queue_id,))
            self.conn.commit()

    async def maintenance(self) -> dict[str, int]:
        """Bound disk growth without touching live/pending work."""
        async with self.lock:
            now = time.time()
            queue_cutoff = now - STATE_RETENTION_DAYS * 86400
            cache_cutoff = now - LINK_CACHE_DAYS * 86400
            old_ids = [row[0] for row in self.conn.execute(
                "SELECT id FROM queue WHERE status IN ('done','failed') AND created_at<?",
                (queue_cutoff,),
            ).fetchall()]
            deleted_deliveries = 0
            deleted_queue = 0
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                deleted_deliveries = self.conn.execute(
                    f"DELETE FROM deliveries WHERE queue_id IN ({placeholders})", old_ids
                ).rowcount
                deleted_queue = self.conn.execute(
                    f"DELETE FROM queue WHERE id IN ({placeholders})", old_ids
                ).rowcount
            deleted_cache = self.conn.execute(
                "DELETE FROM link_cache WHERE created_at<?", (cache_cutoff,)
            ).rowcount
            self.conn.execute(
                "DELETE FROM posted_deals WHERE posted_at<?", (now - PRODUCT_DEDUP_SECONDS,)
            )
            self.conn.execute(
                "DELETE FROM price_posts WHERE posted_at<?", (now - PRICE_DEDUP_SECONDS,)
            )
            self.conn.execute("DELETE FROM deal_claims WHERE claimed_at<?", (now - 3600,))
            self.conn.execute(
                "DELETE FROM deliveries WHERE queue_id NOT IN (SELECT id FROM queue)"
            )
            self.conn.commit()
            with contextlib.suppress(sqlite3.OperationalError):
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Compact only when the durable DB becomes unusually large; normal
            # maintenance avoids frequent blocking VACUUM operations.
            with contextlib.suppress(OSError, sqlite3.OperationalError):
                if DB_PATH.exists() and DB_PATH.stat().st_size > 128 * 1024 * 1024:
                    self.conn.execute("VACUUM")
            return {
                "queue": max(0, deleted_queue),
                "deliveries": max(0, deleted_deliveries),
                "cache": max(0, deleted_cache),
            }


store = Store(DB_PATH)

# ---------------------------------------------------------------------------
# External clients
# ---------------------------------------------------------------------------
class CircuitBreaker:
    def __init__(self, threshold: int = 5, recovery: float = 60):
        self.threshold, self.recovery = threshold, recovery
        self.failures, self.opened_at = 0, 0.0

    def allow(self) -> bool:
        if self.opened_at and time.monotonic() - self.opened_at < self.recovery:
            return False
        if self.opened_at:
            self.opened_at, self.failures = 0, 0
        return True

    def success(self) -> None:
        self.failures, self.opened_at = 0, 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


EK_BREAKER = CircuitBreaker()
EK_SEM = asyncio.Semaphore(EK_MAX_CONCURRENCY)


class AffiliateClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def cache_link(self, source_url: str, affiliate: str, resolved: str, key: str) -> None:
        """Proxy to the global store's link cache so subclasses/tests can override."""
        await store.cache_link(source_url, affiliate, resolved, key)

    async def resolve(self, source_url: str) -> str:
        current = source_url
        for _ in range(5):
            try:
                p = urlparse(current)
                host = (p.hostname or "").lower()
                if host_matches(host, "linkredirect.in"):
                    dl = parse_qs(p.query).get("dl", [None])[0]
                    if dl:
                        target = unquote(dl)
                        if target.startswith("http"):
                            current = target
                            continue
                if host not in SHORT_DOMAINS:
                    break
                async with self.session.get(
                    current, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=18),
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as response:
                    final = str(response.url)
                    if final == current:
                        break
                    current = final
            except Exception as exc:
                log.warning("RESOLVE failed %s: %s", source_url[:60], exc)
                break
        return current

    async def link_not_broken(self, url: str) -> bool:
        """Follow the URL and reject explicit merchant repair/dead-page responses."""
        for attempt in range(2):
            try:
                async with self.session.get(
                    clean_url(url), allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=18),
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"},
                ) as response:
                    raw = await response.content.read(600_000)
                    body = raw.decode(response.charset or "utf-8", errors="ignore")
                    if looks_broken_page(response.status, body):
                        log.warning("BROKEN LINK blocked | status=%s | %s", response.status, url[:100])
                        return False
                    # 401/403/429 can be merchant bot/rate protection rather than
                    # a dead customer-facing page, so they remain inconclusive.
                    if response.status < 500:
                        return True
            except Exception as exc:
                log.warning("LINK CHECK attempt %s inconclusive %s: %s", attempt + 1, url[:70], exc)
            if attempt == 0:
                await asyncio.sleep(1)
        # Do not discard a valid deal solely because Oracle was bot-blocked or
        # temporarily offline; explicit repair/dead-page signatures are blocked.
        return True

    async def rendered_links_not_broken(self, text: str) -> bool:
        urls = list(dict.fromkeys(clean_url(x) for x in URL_RE.findall(text or "")))
        return bool(urls) and all([await self.link_not_broken(url) for url in urls])

    @staticmethod
    def valid_generated(link: str) -> bool:
        """Only trust authenticated API output and reject visible foreign attribution."""
        if not link.startswith("http"):
            return False
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        query = {str(k).lower(): v for k, v in parse_qs(parsed.query).items()}
        # Flipkart exposes the publisher in affExtParam2. If present, it must be
        # ours; never accept an authenticated API echo carrying somebody else's ID.
        visible_ids = query.get("affextparam2", [])
        if OUR_EK_ID and visible_ids and any(str(v) != OUR_EK_ID for v in visible_ids):
            return False
        if in_domains(host, AMAZON_DOMAINS):
            return query.get("tag", [""])[0] == OUR_TAG
        if in_domains(host, FOREIGN_ECHO_DOMAINS):
            return False
        return bool(host)

    async def convert(self, source_url: str, multi_link: bool, resolved_hint: str | None = None) -> "LinkResult | None":
        cached = await store.cached_link(source_url)
        if cached:
            cached_url = clean_url(cached["affiliate_url"])
            cached_resolved = cached["resolved_url"] or resolved_hint or source_url
            cached_host = (urlparse(cached_url).hostname or "").lower()
            # Never reuse old/poisoned source-wrapper cache rows. Bitly rows are
            # accepted because they were saved only after our authenticated call.
            cache_is_safe = (
                clean_url(cached_url) != clean_url(source_url)
                and (in_domains(cached_host, OUR_RUNTIME_SHORTENER_DOMAINS)
                     or self.valid_generated(cached_url))
            )
            if cache_is_safe:
                # Old cache rows may predate the current "Amazon or 2+" Bitly
                # policy. Upgrade them before returning; never leak a long link.
                needs_short = should_use_bitly(cached_resolved, multi_link) or len(cached_url) > SHORTEN_MIN_LEN
                if needs_short and not in_domains(cached_host, OUR_RUNTIME_SHORTENER_DOMAINS):
                    shortened = await self.shorten(cached_url)
                    if shortened:
                        cached_url = shortened
                        await store.cache_link(source_url, cached_url, cached_resolved,
                                               cached["deal_key"] or product_key(cached_resolved))
                    else:
                        log.warning("BITLY unavailable; using verified cached EarnKaro link")
                return LinkResult(source_url, cached_resolved, cached_url,
                                  cached["deal_key"] or product_key(source_url))
        resolved = resolved_hint or await self.resolve(source_url)
        host = (urlparse(resolved).hostname or "").lower()
        if in_domains(host, NON_STORE_DOMAINS):
            return None
        # Preserve meaningful category/filter parameters (for example Men vs
        # Women) while removing only source attribution/tracking parameters.
        clean = merchant_url(resolved)
        # Do not monetize/post an expired campaign destination. This catches
        # Flipkart's HTTP-200 "Just a quick repair needed" page shown to users.
        if not await self.link_not_broken(clean):
            return None
        # USER RULE: Amazon links mix — appudappudu EarnKaro, otherwise our own
        # Amazon Associates tag directly (best commission, no middle layer).
        # The direct branch never depends on the EarnKaro API, so Amazon deals
        # keep flowing even when EarnKaro is down/rate-limited.
        if in_domains(host, AMAZON_DOMAINS) and random.random() >= AMAZON_EARNKARO_RATIO:
            direct = apply_amazon_tag(clean)
            if self.valid_generated(direct) and await self.link_not_broken(direct):
                affiliate = direct
                if should_use_bitly(resolved, multi_link) or len(direct) > SHORTEN_MIN_LEN:
                    shortened = await self.shorten(direct)
                    if shortened:
                        affiliate = shortened
                    else:
                        log.warning("BITLY unavailable; using tagged Amazon link directly")
                key = product_key(resolved)
                await store.cache_link(source_url, affiliate, resolved, key)
                log.info("AMAZON direct-associates | %s", (affiliate or "")[:80])
                return LinkResult(source_url, resolved, affiliate, key)
            # Direct build failed (broken page etc.) — fall through to EarnKaro.
        if not EK_BREAKER.allow():
            raise RuntimeError("EarnKaro circuit open")
        async with EK_SEM:
            last = None
            for attempt in range(3):
                try:
                    async with self.session.post(
                        EK_API,
                        json={"deal": clean},
                        headers={"Authorization": f"Bearer {EK_KEY}", "Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=25),
                    ) as response:
                        body = await response.text()
                        if response.status in (429, 500, 502, 503, 504):
                            raise RuntimeError(f"EarnKaro HTTP {response.status}")
                        data = json.loads(body)
                        result = data.get("data") if data.get("success") == 1 else None
                        if isinstance(result, list):
                            result = next((x for x in result if isinstance(x, str) and x.startswith("http")), None)
                        if not isinstance(result, str):
                            return None
                        result = apply_amazon_tag(result)
                        if not self.valid_generated(result):
                            return None
                        result = clean_url(result)
                        # An authenticated endpoint must return a newly generated
                        # link—not simply echo the foreign/source affiliate URL.
                        if clean_url(result) == clean_url(source_url):
                            log.error("PROVENANCE rejected echoed source URL: %s", source_url[:80])
                            return None
                        if not await self.link_not_broken(result):
                            return None
                        affiliate = result
                        # Single non-Amazon deal: preserve EarnKaro's own short link.
                        # Amazon always uses our Bitly; every 2+ link post uses Bitly
                        # for all generated links so the final post stays neat.
                        if should_use_bitly(resolved, multi_link) or len(result) > SHORTEN_MIN_LEN:
                            shortened = await self.shorten(result)
                            if shortened:
                                affiliate = shortened
                            else:
                                # Never lose a valid commission link merely because
                                # the cosmetic shortener is unavailable/rate-limited.
                                log.warning("BITLY unavailable; using verified EarnKaro fallback")
                        key = product_key(resolved)
                        await store.cache_link(source_url, affiliate, resolved, key)
                        EK_BREAKER.success()
                        return LinkResult(source_url, resolved, affiliate, key)
                except Exception as exc:
                    last = exc
                    await asyncio.sleep(min(8, (2 ** attempt) + random.random()))
            EK_BREAKER.failure()
            raise RuntimeError(f"EarnKaro failed: {last}")

    async def shorten(self, long_url: str) -> str | None:
        shortened = await self.bitly(long_url)
        if shortened:
            return shortened
        # Tokenless fallback for very long/multi-link posts. The authenticated
        # affiliate destination is created first; this service only redirects to it.
        try:
            async with self.session.get(
                "https://is.gd/create.php",
                params={"format": "simple", "url": long_url},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as response:
                if response.status == 200:
                    link = (await response.text()).strip()
                    if link.startswith("https://is.gd/"):
                        log.info("SHORTENER fallback=is.gd")
                        return link
        except Exception as exc:
            log.warning("is.gd failed: %s", exc)
        return None

    async def bitly(self, long_url: str) -> str | None:
        for token in BITLY_TOKENS:
            try:
                async with self.session.post(
                    "https://api-ssl.bitly.com/v4/shorten",
                    json={"long_url": long_url},
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as response:
                    if response.status == 429:
                        continue
                    if response.status in (200, 201):
                        data = await response.json(content_type=None)
                        link = data.get("link")
                        if isinstance(link, str) and link.startswith("http"):
                            return link
            except Exception as exc:
                log.warning("BITLY failed: %s", exc)
        return None

    async def shorten_long_urls_in_text(self, rendered: str) -> str:
        """FINAL guarantee that no link leaves the post clumsy/long:
          1. Every Amazon PRODUCT (/dp/ASIN) URL is collapsed to its shortest
             native form - https://www.amazon.in/dp/ASIN?tag=OURTAG (~48 chars) -
             dropping psc/smid/th/ref noise, at no Bitly cost and with our tag
             intact (self-proving for provenance).
          2. Any remaining long link (Amazon SEARCH / hidden-keywords lists,
             category/filter URLs) is shortened via Bitly (is.gd fallback).
        Links already on a shortener domain are never re-shortened; if the
        shortener is unavailable the original link is kept (a deal is never lost)."""
        out = rendered or ""
        # Pass 1: native compaction of Amazon /dp/ product links (free).
        compacted = 0
        for raw in dict.fromkeys(URL_RE.findall(out)):
            url = clean_url(raw)
            host = (urlparse(url).hostname or "").lower()
            if not in_domains(host, AMAZON_DOMAINS):
                continue
            compact = compact_amazon_product_link(url)
            if compact and compact != url and compact != raw:
                out = out.replace(raw, compact).replace(raw.replace("&", "&amp;"), compact)
                compacted += 1
        # Pass 2: Bitly/is.gd the remaining long links (search/category).
        replacements: dict[str, str] = {}
        for raw in dict.fromkeys(URL_RE.findall(out)):
            url = clean_url(raw)
            host = (urlparse(url).hostname or "").lower()
            if len(url) <= SHORTEN_MIN_LEN:
                continue
            if in_domains(host, OUR_RUNTIME_SHORTENER_DOMAINS) or in_domains(host, OUR_SHORTENER_DOMAINS):
                continue
            shortened = await self.shorten(url)
            if shortened and shortened != url:
                replacements[raw] = shortened
                # Register the new short link in link_cache so the FINAL
                # provenance gate (verify_generated_text queries affiliate_url)
                # recognises a link we just minted. self.cache_link writes to
                # the real store; tests can override it.
                with contextlib.suppress(Exception):
                    await self.cache_link(url, shortened, url, product_key(url))
        for raw, shortened in replacements.items():
            out = out.replace(raw, shortened)
            out = out.replace(raw.replace("&", "&amp;"), shortened)
        if compacted or replacements:
            log.info("FINAL link tidy: %d amazon product link(s) compacted, %d shortened",
                     compacted, len(replacements))
        return out


@dataclass(frozen=True)
class LinkResult:
    source: str
    resolved: str
    affiliate: str
    deal_key: str


def distinct_affiliate_results(results: Iterable[LinkResult]) -> list[LinkResult]:
    """Keep colour/gender variants; remove only exact repeated generated URLs."""
    unique: dict[str, LinkResult] = {}
    for result in results:
        unique.setdefault(result.affiliate, result)
    return list(unique.values())

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
async def resolve_entity(client: TelegramClient, identifier: str):
    # Telegram uses both t.me/+hash and telegram.me/+hash invite forms.
    if identifier.startswith(("https://t.me/+", "https://telegram.me/+")):
        invite_hash = identifier.split("+", 1)[1]
        canonical_invite = "https://t.me/+" + invite_hash
        with contextlib.suppress(UserAlreadyParticipantError):
            try:
                await client(ImportChatInviteRequest(invite_hash))
            except Exception as exc:
                log.warning("JOIN failed %s: %s", identifier, exc)
        with contextlib.suppress(Exception):
            return await client.get_entity(canonical_invite)
        with contextlib.suppress(Exception):
            return await client.get_entity(identifier)
        return None
    with contextlib.suppress(Exception):
        return await client.get_entity(identifier)
    return None


async def backfill_source(client: TelegramClient, entity, source: str) -> None:
    """Recover recent messages missed during downtime/private-source reconnects."""
    if BACKFILL_HOURS <= 0 or BACKFILL_LIMIT <= 0:
        return
    if source in TRICKS_SOURCES:
        source_hours, source_limit = 60 * 24, 5
    elif source == OZ_SOURCE:
        source_hours, source_limit = 48, max(BACKFILL_LIMIT, 50)
    else:
        source_hours, source_limit = BACKFILL_HOURS, BACKFILL_LIMIT
    cutoff = datetime.now(timezone.utc).timestamp() - source_hours * 3600
    queued = 0
    try:
        messages = await client.get_messages(entity, limit=source_limit)
        marked_chat_id = int(f"-100{int(entity.id)}")
        for msg in reversed(messages):
            stamp = msg.date.timestamp() if getattr(msg, "date", None) else 0
            if stamp < cutoff:
                continue
            if not extract_urls(msg) and not getattr(msg, "reply_to", None):
                continue
            await store.enqueue(marked_chat_id, msg.id, source)
            queued += 1
        if queued:
            log.info("BACKFILL | source=%s candidates=%s hours=%s", source, queued, source_hours)
    except Exception as exc:
        log.warning("BACKFILL failed %s: %s", source, exc)


async def register_source(client: TelegramClient, source_map: dict, source: str,
                          targets: list[str], do_backfill: bool = False) -> bool:
    entity = await resolve_entity(client, source)
    if not entity:
        return False
    bare = int(entity.id)
    source_map[bare] = (source, list(targets))
    source_map[int(f"-100{bare}")] = (source, list(targets))
    log.info("SOURCE OK @%s -> %s -> %s", source, bare, targets)
    if do_backfill:
        await backfill_source(client, entity, source)
    return True


async def build_maps(client: TelegramClient):
    source_map: dict[int, tuple[str, list[str]]] = {}
    target_map: dict[str, Any] = {}
    all_targets = set(MAIN_TARGETS + [UNDER99_TARGET, UNDER499_TARGET, PREMIUM_TARGET])
    for targets in SOURCE_TO_TARGETS.values():
        all_targets.update(targets)
    for target in sorted(all_targets):
        entity = await resolve_entity(client, target)
        if entity:
            target_map[target] = entity
            log.info("TARGET OK @%s -> %s", target, entity.id)
        else:
            log.error("TARGET FAIL @%s", target)
    for source, targets in SOURCE_TO_TARGETS.items():
        if not await register_source(client, source_map, source, targets, do_backfill=True):
            log.error("SOURCE FAIL @%s", source)
        await asyncio.sleep(0.08)
    return source_map, target_map


async def maintenance_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            stats = await store.maintenance()
            removed_media = 0
            cutoff = time.time() - 6 * 3600
            for item in MEDIA_DIR.iterdir():
                with contextlib.suppress(OSError):
                    if item.is_file() and item.stat().st_mtime < cutoff:
                        item.unlink()
                        removed_media += 1
            log.info("MAINTENANCE | queue=%s deliveries=%s cache=%s media=%s",
                     stats["queue"], stats["deliveries"], stats["cache"], removed_media)
            await asyncio.wait_for(stop.wait(), timeout=MAINTENANCE_SECONDS)
        except asyncio.TimeoutError:
            continue
        except Exception as exc:
            log.warning("MAINTENANCE failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                continue


async def refresh_missing_sources(client: TelegramClient, source_map: dict,
                                  stop: asyncio.Event) -> None:
    """Periodically recover private/invite sources that failed during startup."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=SOURCE_REFRESH_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
        mapped = {entry[0] for entry in source_map.values()}
        missing = [(source, targets) for source, targets in SOURCE_TO_TARGETS.items()
                   if source not in mapped]
        for source, targets in missing:
            if await register_source(client, source_map, source, targets, do_backfill=True):
                log.info("SOURCE RECOVERED @%s", source)
            else:
                log.warning("SOURCE STILL UNAVAILABLE @%s", source)
            await asyncio.sleep(0.2)


async def source_rescan_loop(client: TelegramClient, source_map: dict,
                             stop: asyncio.Event) -> None:
    """Dead-man's switch for the event stream.

    Ingest is event-based; if the Telethon session degrades (network flap, DC
    switch, stale auth) the events silently stop while the process stays up —
    refresh_missing_sources does not help because the sources are still mapped.
    This loop re-scans every LIVE source's recent messages every
    SOURCE_RESCAN_SECONDS and enqueues anything new (the queue dedups by
    (chat_id, msg_id), so re-seeing a post is free). A deal missed by the
    event stream is therefore recovered within one cycle instead of being
    lost until the next restart backfill.
    """
    global LAST_INGEST_AT
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=SOURCE_RESCAN_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
        # Free any job orphaned in 'processing' (task killed mid-await): the
        # deal goes back to 'pending' instead of silently never posting.
        try:
            reclaimed = await store.reclaim_stuck_jobs()
            if reclaimed:
                log.warning("RECLAIMED | %s stuck processing job(s) returned to the queue", reclaimed)
        except Exception as exc:
            log.warning("RECLAIM failed: %s", exc)
        # Cycle + 2 min drift margin: covers the whole scan window even if a
        # cycle slips; re-seeing a post is free (the queue dedups).
        window_start = time.time() - (SOURCE_RESCAN_SECONDS + 120)
        recovered = 0
        for chat_id, (source, _targets) in list(source_map.items()):
            if stop.is_set():
                break
            if not str(chat_id).startswith("-100"):
                continue  # each source is registered twice; scan the -100 form
            try:
                messages = await client.get_messages(chat_id, limit=SOURCE_RESCAN_LIMIT)
                for msg in reversed(messages or []):
                    stamp = msg.date.timestamp() if getattr(msg, "date", None) else 0
                    if stamp < window_start:
                        continue
                    raw = (msg.text or msg.message or "") if msg is not None else ""
                    has_media = bool(
                        msg is not None and msg.media is not None
                        and not isinstance(msg.media, (MessageMediaWebPage, MessageMediaInvoice))
                    )
                    if not extract_urls(msg) and not has_media and not getattr(msg, "reply_to", None):
                        continue
                    if await store.enqueue(chat_id, msg.id, source, raw, has_media):
                        recovered += 1
                        log.info("RECOVERED | source=%s msg=%s (event stream gap)", source, msg.id)
                await asyncio.sleep(0.1)
            except Exception as exc:
                log.warning("RESCAN failed source=%s: %s", source, exc)
        silence_min = (time.time() - LAST_INGEST_AT) / 60
        if silence_min >= 30:
            log.warning(
                "INGEST SILENCE | no source events for %.0f min; rescan keeps deals flowing",
                silence_min,
            )
        if recovered:
            log.info("RESCAN | cycle=%ss recovered=%s", SOURCE_RESCAN_SECONDS, recovered)


def outbound_parts(text: str, media_path: str | None) -> list[tuple[str, str]]:
    """Build deterministic chunks so a retry can resume after the last sent part."""
    if media_path:
        caption_parts = chunks(text, 1024)
        parts: list[tuple[str, str]] = [("file", caption_parts[0])]
        remainder = "\n".join(caption_parts[1:]).strip()
        parts.extend(("message", part) for part in (chunks(remainder, 4096) if remainder else []))
        return parts
    return [("message", part) for part in chunks(text, 4096)]


async def deliver(client, entity, text: str, media_path: str | None, start_chunk: int = 0,
                  progress_callback=None, link_preview: bool = True) -> tuple[bool, str]:
    parts = outbound_parts(text, media_path)
    if start_chunk > len(parts):
        return False, "invalid chunk checkpoint"
    for index in range(start_chunk, len(parts)):
        kind, content = parts[index]
        sent = False
        last_error = "unknown send failure"
        for attempt in range(POST_RETRIES):
            try:
                if kind == "file":
                    await client.send_file(entity, media_path, caption=content, parse_mode=None)
                else:
                    await client.send_message(
                        entity, content, parse_mode=None, link_preview=link_preview
                    )
                if progress_callback:
                    await progress_callback(index + 1)
                sent = True
                break
            except FloodWaitError as exc:
                # A Telegram rate-limit. Never let a very long flood (hours /
                # an account soft-ban) freeze a worker for that long — cap the
                # in-loop wait, log it loudly, and let the job fall through as
                # a retryable failure (fail_job backs off; the other workers
                # keep posting; the never-die loop + Restart=always stay alive).
                wait = min(int(getattr(exc, "seconds", 60) or 60), 300)
                log.warning("FLOOD WAIT | target=%s seconds=%s (capped sleep=%s) "
                            "— a long flood means Telegram is rate-limiting the "
                            "account; posting resumes automatically.",
                            getattr(entity, "id", entity), getattr(exc, "seconds", 60), wait)
                await asyncio.sleep(wait + 2)
            except (ChannelPrivateError, ChatAdminRequiredError) as exc:
                return False, str(exc)
            except Exception as exc:
                last_error = str(exc)
                if attempt + 1 < POST_RETRIES:
                    await asyncio.sleep(2 ** attempt)
        if not sent:
            return False, last_error
    return True, ""

# ---------------------------------------------------------------------------
# Worker pipeline
# ---------------------------------------------------------------------------
async def render_job(client, affiliate: AffiliateClient, row: sqlite3.Row):
    msg = await client.get_messages(row["chat_id"], ids=row["msg_id"])
    if not msg:
        raise RuntimeError("source message no longer available")
    # IMPORTANT: Telegram entity offsets refer to the ORIGINAL text. Never run
    # footer/branding cleanup before rebuilding entities, otherwise offsets shift
    # and fragments such as `uy` / `tps://` get glued to the affiliate URL.
    raw_text = msg.text or msg.message or ""
    text = clean_source_text(raw_text)
    source_urls = extract_urls(msg)
    # Reply-only deal support.
    reply_to = getattr(msg, "reply_to", None)
    if reply_to and getattr(reply_to, "reply_to_msg_id", None):
        with contextlib.suppress(Exception):
            replied = await client.get_messages(row["chat_id"], ids=reply_to.reply_to_msg_id)
            if replied:
                for url in extract_urls(replied):
                    if url not in source_urls:
                        source_urls.append(url)
    has_merchant_candidate = any(
        not in_domains((urlparse(url).hostname or "").lower(), NON_STORE_DOMAINS)
        for url in source_urls
    )
    had_social_promo = bool(TRICK_PROMO_LINK_RE.search(raw_text)) or any(
        in_domains((urlparse(url).hostname or "").lower(), NON_STORE_DOMAINS)
        for url in source_urls
    )
    # Any source-only Telegram/WhatsApp advertisement is rewritten to our own
    # folder/main-channel promo and routed to Tricks; foreign join links never survive.
    if had_social_promo and not has_merchant_candidate:
        return await render_trick_promo(row, msg, raw_text)

    if not source_urls:
        raise PermanentSkip("no URLs")

    # Resolve first so social/footer URLs do not inflate the 2+ Bitly threshold.
    # Every remaining merchant candidate is atomic: if even one cannot be freshly
    # converted, the whole post is retried/skipped rather than posting a partial deal.
    candidates: list[tuple[str, str]] = []
    for source_url in source_urls:
        resolved = await affiliate.resolve(source_url)
        host = (urlparse(resolved).hostname or "").lower()
        if not in_domains(host, NON_STORE_DOMAINS):
            candidates.append((source_url, resolved))
    if not candidates:
        raise PermanentSkip("no monetizable URLs")

    # Service/lifestyle offers (Zomato, Swiggy, Zepto, movies, payment-app and
    # card offers) are NOT EarnKaro-monetizable store products. Split them out:
    # they pass through unconverted (never poisoning the EarnKaro conversion of
    # the store links in the same post) and the post is still published.
    service_pairs: list[tuple[str, str]] = []
    store_candidates: list[tuple[str, str]] = []
    for source_url, resolved in candidates:
        host = (urlparse(resolved).hostname or "").lower()
        if in_domains(host, SERVICE_OFFER_DOMAINS):
            clean_resolved = clean_url(resolved)
            if clean_resolved not in {r for _, r in service_pairs}:
                service_pairs.append((source_url, clean_resolved))
        else:
            store_candidates.append((source_url, resolved))
    if not store_candidates and not service_pairs:
        raise PermanentSkip("no monetizable URLs")

    multi_link = len(store_candidates) >= 2
    converted: list[LinkResult] = []
    transient_errors: list[str] = []
    for source_url, resolved in store_candidates:
        try:
            result = await affiliate.convert(source_url, multi_link, resolved)
            if result:
                converted.append(result)
            else:
                resolved_host = (urlparse(resolved).hostname or "").lower()
                # Strict completeness: never publish a list with a missing
                # product link. Retry the entire source post instead.
                transient_errors.append(
                    f"affiliate conversion returned no link: {resolved_host or 'unknown'}"
                )
        except Exception as exc:
            transient_errors.append(str(exc))
            log.warning("CONVERT failed %s: %s", source_url[:60], exc)
    if transient_errors:
        # A temporary API/Bitly/network failure must retry the whole job so a
        # monetizable link is not silently lost.
        raise RuntimeError("conversion retry required: " + "; ".join(transient_errors[:3]))
    if not converted and not service_pairs:
        raise PermanentSkip("no monetizable URLs")

    # Preserve distinct variant links (colour/gender/size/category filters) inside
    # one source post. Collapse only an exact repeated generated URL. Product-key
    # reservation still provides global 10-hour dedup across source channels.
    converted = distinct_affiliate_results(converted)
    keys = list(dict.fromkeys(
        [product_key(resolved) for _, resolved in service_pairs]
        + [r.deal_key for r in converted]
    ))
    has_identity = any(not key.startswith("URL:") for key in keys)
    price = parse_price(text)
    content_key = content_deal_key(text)
    ok, new_keys = await store.reserve(
        row["id"], keys, price, has_identity, content_key=content_key
    )
    if not ok:
        raise DuplicateDeal("duplicate deal")
    allowed = {key for key in new_keys}
    converted = [r for r in converted if r.deal_key in allowed]
    # Service-only posts have an empty `converted` by design (their links are
    # pass-through, already reserved via their product keys above) — only a
    # STORE-only post can be fully duplicated this way.
    if not converted and not service_pairs:
        raise DuplicateDeal("all products already posted")

    mapping = {clean_url(r.source): r.affiliate for r in converted}
    # Visible product lists are rebuilt directly by source order so each label
    # keeps its own link. Fall back to entity-aware replacement for mixed/hidden posts.
    rendered = format_visible_source_product_pairs(raw_text, mapping)
    if rendered is None:
        rendered = rebuild_text(raw_text, getattr(msg, "entities", None), mapping)
        rendered = remove_source_url_residue(
            rendered, source_urls, [result.affiliate for result in converted]
        )
        rendered = clean_source_text(rendered)

    # Reply-only / hidden entity / inline-button links may not be literal in the
    # visible text. Ensure every DISTINCT converted product appears exactly once.
    for result in converted:
        if result.affiliate not in rendered:
            rendered = (rendered.rstrip() + "\n" + result.affiliate).strip()
    # Service offer links: show the resolved clean destination (a zom.to short
    # link becomes the real offer page) and ensure each one is present.
    for source_url, resolved in service_pairs:
        clean_source = clean_url(source_url)
        if clean_source in rendered:
            rendered = rendered.replace(clean_source, resolved)
    for _source_url, resolved in service_pairs:
        if resolved not in rendered:
            rendered = (rendered.rstrip() + "\n" + resolved).strip()
    if row["source"] in TRICKS_SOURCES or had_social_promo:
        rendered = f"{rendered.rstrip()}\n\n{tricks_footer()}"
    rendered = tidy_post(rendered)
    multi_product_list = len(converted) >= 3
    if multi_product_list:
        rendered = format_clustered_product_list(rendered)
    rendered = strict_orphan_token_cleanup(rendered)
    if not rendered or not URL_RE.search(rendered):
        raise PermanentSkip("rendered post has no affiliate URL")

    discount = parse_discount(text, price)
    card_offer = has_card_offer(text)
    list_prices = extract_deal_prices(text) if multi_product_list else []
    routing_price = max(list_prices) if list_prices else price

    # Card / bank offers: user wants them on EVERY owned channel at once plus
    # the premium channel, with the Loots Family folder link appended. These
    # are the strongest, most time-sensitive deals, so fanning out is correct.
    if card_offer:
        rendered = append_folder_link(rendered)

    # Final provenance guard: every URL must be an exact generated output.
    generated = {r.affiliate for r in converted}
    allowed_final_urls = set(generated)
    if row["source"] in TRICKS_SOURCES or had_social_promo:
        allowed_final_urls.update({OUR_FOLDER_LINK, *OUR_MAIN_CHANNEL_LINKS})
    if card_offer:
        allowed_final_urls.add(OUR_FOLDER_LINK)
    if service_pairs:
        allowed_final_urls.update(resolved for _, resolved in service_pairs)
    for raw in URL_RE.findall(rendered):
        if clean_url(raw) not in allowed_final_urls:
            raise PermanentSkip(f"foreign URL survived: {raw[:80]}")

    # PowerLoots1 is now a global filtered destination across ALL configured
    # sources. Remove legacy unconditional routing, then add it only when the
    # deal is <=₹499, >=70% off, or an explicit bank/card offer.
    base_targets = [
        target for target in SOURCE_TO_TARGETS.get(row["source"], [])
        if target != POWER_FILTER_TARGET
    ]
    if multi_product_list and row["source"] not in TRICKS_SOURCES:
        for main_target in LZI_SECRET:
            if main_target not in base_targets:
                base_targets.append(main_target)
    if (SECRET_TARGET in base_targets
            and not eligible_for_secret(text, routing_price, discount,
                                        multi_product_list, card_offer)):
        base_targets = [target for target in base_targets if target != SECRET_TARGET]
    # Card / bank offers are the user's highest priority: fan out to EVERY
    # owned channel, force the premium channel, and rank them as premium.
    premium_rank = None
    if card_offer:
        for target in ALL_OWNED_TARGETS:
            if target not in base_targets:
                base_targets.append(target)
        if PREMIUM_TARGET not in base_targets:
            base_targets.append(PREMIUM_TARGET)
        premium_rank = max(1, premium_score(text, routing_price, discount))
    elif service_pairs:
        # Zomato/Swiggy/Zepto/movie/card-app offers fan out to EVERY owned
        # channel like bank/card offers — the user must not miss them.
        for target in ALL_OWNED_TARGETS:
            if target not in base_targets:
                base_targets.append(target)
        premium_rank = max(1, premium_score(text, routing_price, discount))
    elif row["source"] not in TRICKS_SOURCES:
        if multi_product_list:
            # Latest rule: 3+ product lists fan out to every owned non-Tricks
            # destination regardless of individual price bands.
            for target in (
                POWER_FILTER_TARGET, UNDER99_TARGET, UNDER499_TARGET, PREMIUM_TARGET
            ):
                if target not in base_targets:
                    base_targets.append(target)
            premium_rank = max(1, premium_score(text, routing_price, discount))
        else:
            if eligible_for_power_loots(routing_price, discount, card_offer):
                base_targets.append(POWER_FILTER_TARGET)
            for price_target in automatic_price_targets(routing_price):
                if price_target not in base_targets:
                    base_targets.append(price_target)
            # under499loots is the main feed for every configured source. Price
            # routing above already handles parseable prices; this catches the
            # strong deals whose price the source never printed.
            if (UNDER499_TARGET not in base_targets
                    and eligible_for_under499_best(text, routing_price, discount, card_offer)):
                base_targets.append(UNDER499_TARGET)
            if eligible_for_premium(text, routing_price, discount):
                base_targets.append(PREMIUM_TARGET)
                premium_rank = premium_score(text, routing_price, discount)
    # USER RULE: every valid source post is published to the Telegram channels;
    # the best-deal CURATION applies to WhatsApp only. A real post that has a
    # monetizable link therefore ALWAYS reaches the main feed
    # (LootZoneIndia11) - and Tricks sources always reach the Tricks channel -
    # regardless of price/discount. The themed filters above (Secret, Power,
    # Premium, Under99/Under499 price routes) decide only the EXTRA channels;
    # they can never silence a post on the main feed.
    if row["source"] in TRICKS_SOURCES:
        guaranteed = TRICKS_TARGET
    else:
        guaranteed = "LootZoneIndia11"
        # An under-₹99 SOURCE that posts an item we cannot verify as ≤₹99 (or
        # that costs more) must not push it to the Under99 price channel - but
        # it still goes to the main feed instead of being dropped.
        if (row["source"] in UNDER99_SOURCES and not service_pairs
                and (routing_price is None or routing_price > 99)):
            base_targets = [target for target in base_targets if target != UNDER99_TARGET]
    if guaranteed not in base_targets:
        base_targets.append(guaranteed)
    if not base_targets:
        raise PermanentSkip("no eligible targets")
    # FINAL safety net: no affiliate link may leave the post long — shorten any
    # long generated URL (Amazon category/search links with our tag, missed
    # multi-link shortens) before persisting/sending.
    rendered = await affiliate.shorten_long_urls_in_text(rendered)
    if not URL_RE.search(rendered):
        raise PermanentSkip("rendered post has no affiliate URL after shorten pass")
    # Persist only the products actually rendered/reserved; never extend the
    # dedup timestamp of products filtered as already-posted.
    commit_keys = list(new_keys)
    if content_key:
        commit_keys.append(content_key)
    await store.save_render(
        row["id"], rendered, list(dict.fromkeys(base_targets)), commit_keys, premium_rank
    )
    return msg, rendered, price


def extract_urls_from_buttons(msg) -> list[str]:
    result = []
    markup = getattr(msg, "reply_markup", None)
    if isinstance(markup, ReplyInlineMarkup):
        for row in markup.rows:
            for button in row.buttons:
                if isinstance(button, KeyboardButtonUrl) and button.url:
                    result.append(clean_url(button.url))
    return result


class DuplicateDeal(Exception):
    pass


class PermanentSkip(Exception):
    """A non-retryable source message: spam/no URL/unsupported/no eligible target."""


TRICK_PROMO_LINK_RE = re.compile(
    r"(?i)(?:https?://)?(?:t|telegram)\.me/(?:addlist/|joinchat/|\+|[A-Za-z0-9_]+)[^\s|]*|"
    r"https?://(?:www\.)?whatsapp\.com/channel/[^\s|]+"
)


def sanitize_trick_promo(text: str) -> str:
    cleaned = clean_source_text(text)
    cleaned = TRICK_PROMO_LINK_RE.sub("", cleaned)
    cleaned = re.sub(
        r"(?im)^.*(?:many\s+loot\s+being\s+posted|don[’']?t\s+miss|"
        r"ye\s+channels\s+join|biggest\s+loot.*(?:buy\s+now|join\s+fast)).*?$",
        "", cleaned,
    )
    cleaned = re.sub(r"(?im)^\s*(?:link|₹\s*\d+)\s*:\s*[|\s]*$", "", cleaned)
    cleaned = re.sub(r"(?im)^\s*(?:buy\s*now|join\s*fast)\s*[👇.🔥😍😱]*\s*$", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = re.sub(r"\|{1,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip(" |\n")
    if len(cleaned) < 12:
        cleaned = "🎯 BIGGEST LOOT & SHOPPING TRICKS\n\n⚡ Fresh trick/update — check quickly"
    return cleaned


async def render_trick_promo(row: sqlite3.Row, msg, raw_text: str):
    body = sanitize_trick_promo(raw_text)
    rendered = f"{body}\n\n{tricks_footer()}"
    key = "URL:" + hashlib.sha256(("TRICK:" + body.lower()).encode()).hexdigest()
    ok, keys = await store.reserve(row["id"], [key], None, False)
    if not ok:
        raise DuplicateDeal("duplicate trick/promo")
    await store.save_render(row["id"], rendered, [TRICKS_TARGET], keys)
    return msg, rendered, None


async def process_job(client, affiliate: AffiliateClient, target_map, row: sqlite3.Row):
    media_path = None
    successes = 0
    price = None
    try:
        if row["rendered_text"]:
            msg = await client.get_messages(row["chat_id"], ids=row["msg_id"])
            # Re-clean persisted pending work too, so a hotfix applies to queued
            # posts created by an older formatter before they are delivered.
            rendered = row["rendered_text"]
            if msg:
                rendered = remove_source_url_residue(
                    rendered, extract_urls(msg), URL_RE.findall(rendered)
                )
            rendered = tidy_post(clean_source_text(rendered))
            rendered = format_clustered_product_list(rendered)
            rendered = strict_orphan_token_cleanup(rendered)
            if not await store.claim_content_key(row["id"], content_deal_key(rendered)):
                raise DuplicateDeal("duplicate pending content")
            price = parse_price(msg.text or "") if msg else None
        else:
            msg, rendered, price = await render_job(client, affiliate, row)
        if msg and getattr(msg, "media", None) and not isinstance(msg.media, (MessageMediaWebPage, MessageMediaInvoice)):
            # Parent exists and no fake .bin extension: Telethon preserves the
            # actual photo/video/document type so target posts render correctly.
            media_path = await client.download_media(
                msg, file=str(MEDIA_DIR / f"{row['chat_id']}_{row['msg_id']}")
            )
        # Final provenance gate runs again immediately before target delivery.
        owned_candidates = {OUR_FOLDER_LINK, *OUR_MAIN_CHANNEL_LINKS}
        owned_external = {url for url in owned_candidates if url in rendered}
        if not await store.verify_generated_text(rendered, owned_external):
            raise PermanentSkip("final provenance recheck failed")
        # Owned Telegram promo links are not merchant destinations; all other
        # generated shopping links still receive the normal health check.
        merchant_health_text = rendered
        for owned_url in owned_external:
            merchant_health_text = merchant_health_text.replace(owned_url, "")
        if URL_RE.search(merchant_health_text) and not await affiliate.rendered_links_not_broken(merchant_health_text):
            raise PermanentSkip("broken merchant destination blocked before send")
        premium_defer_until = None
        first_target_sent = False
        for target in await store.pending_targets(row["id"]):
            # USER RULE: Telegram never waits — no ban rule there, keep posting.
            # Only a tiny 0.5-1.5s jitter between targets so channel-to-channel
            # fan-out looks natural without ever slowing the deal down.
            if first_target_sent:
                await asyncio.sleep(random.uniform(0.5, 1.5))
            first_target_sent = True
            premium_claimed = False
            if target == PREMIUM_TARGET:
                premium_claimed, defer_until = await store.claim_premium(row["id"])
                if not premium_claimed:
                    premium_defer_until = (
                        defer_until if premium_defer_until is None
                        else min(premium_defer_until, defer_until)
                    )
                    continue
            entity = target_map.get(target)
            if not entity:
                entity = await resolve_entity(client, target)
                if entity:
                    target_map[target] = entity
            if not entity:
                if premium_claimed:
                    await store.complete_premium(row["id"], False)
                await store.delivery(row["id"], target, False, "target unresolved")
                continue
            start_chunk = await store.delivery_progress(row["id"], target)

            async def checkpoint(count: int, queue_id=row["id"], target_name=target):
                await store.set_delivery_progress(queue_id, target_name, count)

            # Power and Premium use filtering/scheduling only. Keep one clean,
            # source-derived post with no repetitive bot-added headers/footers.
            target_text = rendered
            allow_preview = await store.preview_allowed(target_text)
            ok, error = await deliver(
                client, entity, target_text, media_path,
                start_chunk=start_chunk, progress_callback=checkpoint,
                link_preview=allow_preview,
            )
            if premium_claimed:
                await store.complete_premium(row["id"], ok)
            await store.delivery(row["id"], target, ok, error)
            successes += int(ok)
        await store.finish(row, successes, price, premium_defer_until)
        log.info("JOB %s | source=%s | sent=%s", row["id"], row["source"], successes)
    except DuplicateDeal as exc:
        log.info("DEDUP | queue=%s | %s", row["id"], exc)
        await store.mark_done(row["id"], str(exc))
    except PermanentSkip as exc:
        log.info("SKIP | queue=%s | %s", row["id"], exc)
        await store.mark_done(row["id"], str(exc))
    except Exception as exc:
        log.exception("JOB FAIL %s: %s", row["id"], exc)
        await store.fail_job(row, str(exc))
    finally:
        if media_path:
            with contextlib.suppress(Exception):
                Path(media_path).unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    log.info("BestGAA Production Bot v15 starting")
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    timeout = aiohttp.ClientTimeout(total=30)
    session = aiohttp.ClientSession(timeout=timeout)
    affiliate = AffiliateClient(session)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await client.start()
    me = await client.get_me()
    log.info("LOGIN %s (@%s)", me.first_name, me.username)
    source_map, target_map = await build_maps(client)
    log.info("LIVE | sources=%d targets=%d", len(source_map) // 2, len(target_map))

    @client.on(events.NewMessage())
    async def on_message(event):
        entry = source_map.get(event.chat_id)
        if not entry:
            return
        try:
            source, _ = entry
            msg = event.message
            raw = (msg.text or msg.message or "") if msg is not None else ""
            has_media = bool(
                msg is not None and msg.media is not None and
                not isinstance(msg.media, (MessageMediaWebPage, MessageMediaInvoice))
            )
            await store.enqueue(event.chat_id, event.message.id, source, raw, has_media)
            global LAST_INGEST_AT
            LAST_INGEST_AT = time.time()
            log.info("QUEUED | priority=%s media=%s source=%s chat=%s msg=%s",
                     classify_priority(raw, has_media), has_media, source, event.chat_id, event.message.id)
        except Exception as exc:
            # Never let one bad event die silently — a lost event means a lost
            # deal, so make the failure loud (the rescan loop still recovers it).
            log.error("INGEST FAIL source=%s chat=%s msg=%s: %s",
                      entry[0], event.chat_id,
                      event.message.id if event.message is not None else "?", exc)

    async def worker(index: int):
        # NEVER-DIE worker: any unexpected error (SQLite busy, disk hiccup,
        # transient bug) is logged and the loop continues. A dead worker task
        # would silently stop all posting while the process still looks alive.
        while not stop.is_set():
            # Night quiet window (02:00-06:00 IST): pause TARGET posting only.
            # Nothing is claimed, so every night deal stays safely queued and
            # goes out the moment the window ends.
            if in_post_quiet():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                row = await store.claim_job()
            except Exception as exc:
                log.warning("WORKER %s claim failed (retrying): %s", index, exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            if not row:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                break
            # USER RULE: a deal that arrived during 02:00-06:00 is DEAD by
            # 06:00 (loot prices do not survive) — skip it, never post it
            # late. Only multi-product LISTS (3+ links) earn the morning slot.
            if born_in_post_quiet(row["created_at"]) and not in_post_quiet():
                url_count = len(URL_RE.findall(row["rendered_text"] or "")) if row["rendered_text"] else 0
                if url_count < 3:
                    with contextlib.suppress(Exception):
                        msg_probe = await client.get_messages(row["chat_id"], ids=row["msg_id"])
                        if msg_probe:
                            url_count = len(extract_urls(msg_probe))
                if url_count < 3:
                    log.info("NIGHT SKIP | queue=%s source=%s (born in quiet window; lists only after 06:00)",
                             row["id"], row["source"])
                    await store.mark_done(row["id"], "night-born deal expired by 06:00 (lists only)")
                    continue
            try:
                await process_job(client, affiliate, target_map, row)
            except Exception as exc:  # process_job catches its own errors; belt & braces
                log.exception("WORKER %s crashed on job %s (continuing): %s", index, row["id"], exc)

    workers = [asyncio.create_task(worker(i)) for i in range(QUEUE_WORKERS)]
    source_refresh_task = asyncio.create_task(refresh_missing_sources(client, source_map, stop))
    source_rescan_task = asyncio.create_task(source_rescan_loop(client, source_map, stop))
    maintenance_task = asyncio.create_task(maintenance_loop(stop))
    await stop.wait()
    # Allow pending workers a brief graceful drain; queue remains durable if interrupted.
    try:
        await asyncio.wait_for(asyncio.gather(*workers), timeout=30)
    except asyncio.TimeoutError:
        for worker_task in workers:
            worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await source_refresh_task
    with contextlib.suppress(asyncio.CancelledError):
        await source_rescan_task
    with contextlib.suppress(asyncio.CancelledError):
        await maintenance_task
    await client.disconnect()
    await session.close()
    store.conn.close()
    log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
