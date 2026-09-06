#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BestGAA Production Bot v18.3

Durable Telegram deal pipeline — "source lo post rattane, mana target lo
immediately, exactly once, clean":
- SQLite queue and delivery ledger (restart-safe), workers woken instantly on
  insert, dispatched newest-first with an age-out sweeper (never posts stale)
- concurrent resolve / convert / health-check / shorten per post with cached
  verdicts and hard time budgets, so one slow site cannot delay everything
- EarnKaro conversion with retry/cache/circuit breaker
- a dead short link, an unresolvable destination or an oversized photo costs the post
  NOTHING: the offending link is cut and the deal goes out complete (never a retry-until-
  stale, never a skip)
- a source post with a photo and NO link is still a post: it goes out as written (deal
  terms required, nothing invented); a caption with no deal terms is not a deal and stays out
- the source's photos are posted the way the source posted them: a multi-photo album
  arrives as ONE album (the grid), and only the source's own links are ever replaced
- exactly one line of ours may sit above the deal - our own channel link, added on the
  way out so the stored copy stays the source's - and never a second one
- a post that reached the intake ALWAYS reaches the targets: a share button in
  the source text, a merchant page that 404s to our datacenter IP, or a coupon
  code glued to a price can veto nothing - they are cleaned or ignored, never a
  reason to retry a job until the deal expires
- strict generated-link-only output (source links are always replaced by ours;
  an unverified leftover link is repaired out instead of dropping the deal)
- best-copy selection: when several sources post the same product (exact merchant
  id) the queue publishes the STRONGER deal, as one row, with the displaced copy
  restored if the better one cannot render; a numeric fidelity gate drops prices
  or discounts the source never printed (never the post)
- zero artificial waiting on Telegram: channel fan-out is back-to-back and one
  merchant page probe is capped (a slow site can no longer hold a live deal)
- per-product 10-hour cross-source dedup, backed by a canonical
  (chat_key,msg_id) unique index so one source message can never queue twice
- 1-hour same-price fallback only when product identity is unavailable
- dedup committed only after at least one target post succeeds
- Buy Now/entity/button URL support
- source promo / channel links / URL residue / dangling CTA labels stripped
- v17: a final outbound guard so a published post can never contain markdown
  debris ("[url](url)"), a shortener fragment glued to a price
  ("₹260tG7oChgiQuTgS25b"), an empty "()" or a corrupted link of our own
- v17: campaign fingerprint includes the prices, so products that share one
  banner line are no longer mistaken for duplicates of each other
- v17: links the affiliate network cannot monetize post as clean untagged
  merchant links instead of burning the retry budget and vanishing; a genuine
  API outage degrades the same way on the last attempt (never a missing post)
- v17: an edited source post that never reached a target is re-queued
- long caption/message chunking
- Under-99 / Under-499 price routing: single deals are band-checked, a product
  LIST reaches both price channels whatever its item prices, bank/card offers fan
  out to every owned channel, and Premium keeps only the best-scored deals
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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse

import aiohttp
from telethon import TelegramClient, events, utils as tl_utils
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
# USER RULE (2026-09-06): "kothaga thiskunna mama086-21 idi manade". The
# previously seen tag (deals0911-21) belonged to a SOURCE, so it was pinned
# empty to stop a stale .env re-enabling somebody else's tag. We now own
# mama086-21, so the tag is read from the environment again - but a tag that
# is not ours must still never come back, so any value is checked against the
# tags we actually own before it is used.
OUR_AMAZON_TAGS = {"mama086-21"}
_configured_tag = os.getenv("AMAZON_TAG", "").strip()
if _configured_tag and _configured_tag.lower() not in {t.lower() for t in OUR_AMAZON_TAGS}:
    # A source's tag in our .env would credit THEM for our sales and, worse,
    # put an undeclared tag on our channels. Refuse it loudly rather than post.
    logging.getLogger("bestgaa").warning(
        "AMAZON_TAG %r is not one of ours %s - ignoring it and posting tagless",
        _configured_tag, sorted(OUR_AMAZON_TAGS))
    _configured_tag = ""
OUR_TAG = _configured_tag
OUR_EK_ID = os.getenv("EARNKARO_PUBLISHER_ID", "").strip()
BITLY_TOKENS = [x.strip() for x in os.getenv("BITLY_TOKENS", "").split(",") if x.strip()]
# USER RULE (2026-09-03, FINAL): Amazon Associates keeps rejecting the account,
# so direct-tagging pays nothing. EVERY Amazon link goes through EarnKaro like
# every other store — no ratio, no env knob, no direct-Associates branch. A
# stale AMAZON_EARNKARO_RATIO= line in a server .env can never re-enable it.
AMAZON_EARNKARO_RATIO = 1.0  # kept only so external tooling reading it sees "always EarnKaro"

PRODUCT_DEDUP_SECONDS = 24 * 3600  # STRICT 24h: same product never reposts within 24h — pinned, env override blocked (user rule 2026-09-04)
# v17.8 SAME PRODUCT, ONE CHANNEL, ONE TIME. `posted_deals` keys on a merchant
# product id, which is exact but blind while a short link has not been resolved
# yet - so the same earphones re-posted by a second source under a different
# bitli link used to reach LootZoneIndia11 twice. `product_signature()` below
# recognises the product from its own words instead, and the channel that has
# already carried it stays quiet for this window (0 turns the rule off). A
# strictly better copy of the product (cheaper or deeper discount) still posts.
SAME_PRODUCT_SKIP_SECONDS = 24 * 3600  # STRICT 24h: asalu ravoddu same product 24h — pinned (user rule 2026-09-04)
# How much deeper a discount has to be before a repeat of an already-posted
# product counts as news (percentage points).
try:
    SAME_PRODUCT_DISCOUNT_MARGIN = 15  # STRICT: only 15%+ better discount can override 24h (prevents 5% loophole spam)
except ValueError:
    SAME_PRODUCT_DISCOUNT_MARGIN = 5
# User rule (round 7, restated round 10): a post carries what the SOURCE wrote,
# and nothing we invent. Our own "join the loot family" footer and the folder
# link are therefore OFF by default; set ADD_OUR_CHANNEL_FOOTER=true to put them
# back on ordinary deal posts (the Tricks channel always keeps its footer).
ADD_OUR_CHANNEL_FOOTER = os.getenv("ADD_OUR_CHANNEL_FOOTER", "false").strip().lower() in ("1", "true", "yes", "on")
PRICE_DEDUP_SECONDS = max(0, int(os.getenv("PRICE_DEDUP_SECONDS", "0")))
# The one-hour same-price gate is a FALLBACK for posts with no product identity
# (no ASIN/PID). Set it to true to also block a different product that happens
# to carry an already-posted price — kept configurable because the strict
# version suppresses legitimate distinct deals ("source lo vasthe mana channel
# lo raavatam" is the primary rule).
PRICE_DEDUP_IGNORES_IDENTITY = os.getenv("PRICE_DEDUP_IGNORES_IDENTITY", "false").strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, fallback: float, low: float, high: float) -> float:
    """Read a numeric env var and clamp it into a safe range."""
    try:
        value = float(os.getenv(name, ""))
    except (TypeError, ValueError):
        value = fallback
    return min(high, max(low, value))


# --- Immediacy (USER RULE: "source lo post rattane target lo post avvali") ---
# The pipeline is event-driven end to end: a queued deal is woken up instantly
# (no idle polling delay), rendered with concurrent HTTP, and retried within
# seconds instead of minutes. Every knob below is clamped so a typo in .env can
# never turn the bot back into a slow/batched poster.
QUEUE_WORKERS = max(1, int(os.getenv("QUEUE_WORKERS", "8")))
QUEUE_IDLE_POLL_SECONDS = _num("QUEUE_IDLE_POLL_SECONDS", 0.5, 0.1, 10)
# Newest-first: a brand-new deal must never queue behind hours-old inventory
# (that is exactly what made posts look random/late). Old inventory drains by
# age-out instead of being posted stale.
QUEUE_ORDER = "newest" if os.getenv("QUEUE_ORDER", "newest").strip().lower() != "oldest" else "oldest"
# Stale loot is never posted: pending jobs older than this are dropped (logged),
# matching the "prices do not survive" trust policy instead of flushing them late.
MAX_JOB_AGE_HOURS = _num("MAX_JOB_AGE_HOURS", 6, 0.25, 72)
# Job-level retry backoff. The old 5*2**attempts (capped at 5 minutes) was the
# main cause of "the bot posts whenever it feels like it".
JOB_RETRY_BASE_SECONDS = _num("JOB_RETRY_BASE_SECONDS", 3, 1, 60)
JOB_RETRY_MAX_SECONDS = _num("JOB_RETRY_MAX_SECONDS", 20, 2, 300)
JOB_MAX_ATTEMPTS = max(3, int(os.getenv("JOB_MAX_ATTEMPTS", "10")))
# When the affiliate network has no campaign for a store, publish the CLEAN
# merchant link (tracking/affiliate params stripped, registered as ours) instead
# of losing the post: "source lo unna post mana channel lo undali" outranks the
# commission on those links. Set to false to restore the old behaviour (retry the
# job and skip the deal when nothing is monetizable).
PASSTHROUGH_UNMONETIZED = os.getenv("PASSTHROUGH_UNMONETIZED", "true").strip().lower() not in ("0", "false", "no", "off")

# SOURCE FIDELITY (the user's standing rule): publish what the source published,
# its own hype header included - "🔥🔥 TOP DEAL OF THE DAY 🔥🔥", "⚡️ 11 PM FLASH SALE ⚡️"
# are part of how that channel writes a deal, so they are NOT junk and stay. What
# IS removed: other-channel branding/signature, join/follow/share promo,
# referral & app-install farming, CTA filler, glued random tokens, markdown
# debris, URL residue - and every merchant/source link is swapped for OUR link.
# Set STRIP_CAMPAIGN_BANNERS=true only if you ever want those hype lines gone too.
STRIP_CAMPAIGN_BANNERS = os.getenv("STRIP_CAMPAIGN_BANNERS", "false").strip().lower() in ("1", "true", "yes", "on")
EK_MAX_CONCURRENCY = max(1, int(os.getenv("EK_MAX_CONCURRENCY", "8")))
POST_RETRIES = max(1, int(os.getenv("POST_RETRIES", "3")))
# Every outbound HTTP probe gets a real budget instead of the 18-30s default,
# and repeated probes of the same URL are cached, so one slow merchant site can
# no longer hold up the whole queue.
HTTP_TOTAL_TIMEOUT_SECONDS = _num("HTTP_TOTAL_TIMEOUT_SECONDS", 12, 3, 45)
LINK_CHECK_ATTEMPTS = max(1, int(os.getenv("LINK_CHECK_ATTEMPTS", "2")))
LINK_CHECK_RETRY_SLEEP_SECONDS = _num("LINK_CHECK_RETRY_SLEEP_SECONDS", 0.4, 0, 10)
LINK_HEALTH_CACHE_SECONDS = _num("LINK_HEALTH_CACHE_SECONDS", 900, 0, 86400)
PRESEND_CHECK_BUDGET_SECONDS = _num("PRESEND_CHECK_BUDGET_SECONDS", 6, 0, 240)
# Hard cap for ONE merchant page probe. Without it a slow site held the whole post
# for 2 x 12s (the single-link fast path had no budget at all) - which the user
# sees as "the bot posts late". A site that does not answer in time is
# inconclusive, not dead: the deal goes out.
LINK_PROBE_BUDGET_SECONDS = _num("LINK_PROBE_BUDGET_SECONDS", 3.5, 0.5, 60)
# A probe is allowed to make a post HONEST, never to make it disappear: DROP_DEAD_LINKS
# is therefore false by default (post with an unverified link) and only an operator who
# would rather lose the deal than risk a dead page turns it on.
DROP_DEAD_LINKS = os.getenv("DROP_DEAD_LINKS", "false").strip().lower() in ("1", "true", "yes", "on")
# USER RULE (2026-09-05): "asalu link yeh ledu" - a deal published with no link at
# all is useless to the reader and earns nothing, so a post whose every link died
# is skipped rather than sent as a linkless teaser. A source post that never had a
# link but DOES carry the product photo is unaffected (round 13's rule). Set
# REQUIRE_LINK_IN_POST=false to restore the old "post it anyway" behaviour.
REQUIRE_LINK_IN_POST = os.getenv(
    "REQUIRE_LINK_IN_POST", "true").strip().lower() not in ("0", "false", "no", "off")

# A huge source video must never freeze a worker; oversized/slow media is
# posted as text so the deal itself goes out on time.
MAX_MEDIA_MB = _num("MAX_MEDIA_MB", 45, 1, 2048)
# USER RULE (round 13): the source posts its photos as ONE album - a grid inside a
# single bubble - so our channel has to show the same grid, every photo in the source's
# order, and never only the first image. 10 is Telegram's own album ceiling.
MAX_ALBUM_PHOTOS = int(_num("MAX_ALBUM_PHOTOS", 10, 1, 10))
# USER RULE (round 13): one neat line at the TOP with our own channel link, so a reader
# on any of our channels can reach the rest of the family instead of wandering off to
# someone else's. It is the ONLY thing of ours that may sit above the deal text, it never
# appears twice, and the Tricks path (its own footer) is left exactly as it was.
ADD_OUR_CHANNEL_LINK_TOP = os.getenv(
    "ADD_OUR_CHANNEL_LINK_TOP", "false").strip().lower() in ("1", "true", "yes", "on")  # TAGLESS: default false – never auto-add our folder link
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = _num("MEDIA_DOWNLOAD_TIMEOUT_SECONDS", 120, 5, 900)
SOURCE_REFRESH_SECONDS = _num("SOURCE_REFRESH_SECONDS", 180, 60, 3600)
# Dead-man's switch: re-scan every live source's recent messages on a short
# cycle so a silently dead event stream never loses deals until a restart.
SOURCE_RESCAN_SECONDS = _num("SOURCE_RESCAN_SECONDS", 120, 30, 3600)
SOURCE_RESCAN_LIMIT = max(5, int(os.getenv("SOURCE_RESCAN_LIMIT", "40")))
# How many sources are probed at once by the rescan cycle (a 20+ source list
# used to take minutes to walk serially, so a missed post could wait a long
# time before the next cycle found it).
SOURCE_RESCAN_CONCURRENCY = max(1, int(os.getenv("SOURCE_RESCAN_CONCURRENCY", "4")))
# Short random gap between the fan-out targets of ONE job (Telegram never waits
# for a human-like rhythm, this only spreads channel-to-channel delivery).
# USER RULE (restated in round 9): a source post must reach our channels the
# instant it arrives - so the fan-out gap DEFAULTS TO ZERO and the delivery loop
# does not sleep at all. The knobs stay for the rare case a target starts
# rate-limiting: set TARGET_FANOUT_GAP_MIN/MAX (seconds) to space the copies out.
TARGET_FANOUT_GAP_MIN = _num("TARGET_FANOUT_GAP_MIN", 0, 0, 30)
TARGET_FANOUT_GAP_MAX = max(TARGET_FANOUT_GAP_MIN, _num("TARGET_FANOUT_GAP_MAX", 0, 0, 60))
# Queue wake-up event, created in main(); workers stop on it instantly instead
# of only noticing new work at the end of their idle poll.
QUEUE_WAKE: asyncio.Event | None = None


def notify_queue() -> None:
    """Wake the idle workers the moment new work exists (best-effort)."""
    with contextlib.suppress(Exception):
        if QUEUE_WAKE is not None:
            QUEUE_WAKE.set()


def job_retry_delay(attempts: int) -> float:
    """Prompt, bounded retry backoff — never minutes for a hot deal."""
    delay = JOB_RETRY_BASE_SECONDS * (2 ** max(0, int(attempts)))
    return min(JOB_RETRY_MAX_SECONDS, max(1.0, delay)) + random.uniform(0, 1.5)


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
# USER RULE (2026-09-05): the channel submitted for Amazon Associates review.
# It carries the same deals in a programme-safe form (product name, price,
# link - no loot/urgency wording, no "buy max quantity", no unverifiable claim).
# Defined here so routing and the safe-copy renderer share one name.
SHOPPING_TARGET = "smartbuyhub11"
MAIN_TARGETS = [SECRET_TARGET, "LootZoneIndia11", TRICKS_TARGET, POWER_FILTER_TARGET]
NO_TRICKS_TARGETS = [SECRET_TARGET, "LootZoneIndia11", POWER_FILTER_TARGET]
LZI_SECRET = ["LootZoneIndia11", SECRET_TARGET]
TRICKS_SOURCES = {"TrickXpert", "Offerzone_deals", "offers_deals_xyz"}
# Sources the user wants posted FIRST on every non-Tricks channel. Their queue
# jobs get a +1 priority boost (capped at the top tier) so an equivalent deal
# from one of these sources renders/delivers ahead of the same tier from any
# other source. "pricehistory" = user's explicit first-preference source.
PRIORITY_SOURCES = {"pricehistory"}
# USER RULE (2026-09-04): t.me/under_99_loot_deals is the FIRST-PREFERENCE
# source for the price channels - its posts go out ahead of the same tier from
# any other source; the rest follow when it has nothing new.
PRIORITY_SOURCES.add("under_99_loot_deals")
OUR_FOLDER_LINK = "https://t.me/addlist/5V7_ViAGDxAwNTI1"
# Every channel owner controls. Card/bank-offer posts fan out across all of
# these so a bank/card deal is never missed, and get the folder link appended.
# USER RULE (2026-09-06): the user added the new channel's own link as a
# SOURCE. A channel that is both a source and a target feeds its own posts back
# into the queue: the bot would re-read what it just published, re-shorten the
# link, and post it again - the exact "multiple times" the user wants gone. Any
# channel we OWN is therefore never accepted as a source of deals.
def is_own_channel_source(source: str) -> bool:
    """True when a queue job came from a channel we publish to ourselves."""
    name = (source or "").strip().lstrip("@").lower()
    if not name:
        return False
    owned = {SHOPPING_TARGET, SECRET_TARGET, "LootZoneIndia11", TRICKS_TARGET,
             POWER_FILTER_TARGET, PREMIUM_TARGET, UNDER99_TARGET, UNDER499_TARGET}
    return name in {t.lower() for t in owned}


ALL_OWNED_TARGETS = list(dict.fromkeys(
    [SECRET_TARGET, "LootZoneIndia11", POWER_FILTER_TARGET, PREMIUM_TARGET,
     UNDER99_TARGET, UNDER499_TARGET, TRICKS_TARGET]
))
# The shopping channel takes PRODUCT deals only: a trick/recharge/app-promo post
# is not a shoppable product and must never be shown to a marketplace reviewer.
# Set SHOPPING_TARGET_ENABLED=false to hold the channel back entirely.
SHOPPING_TARGET_ENABLED = os.getenv(
    "SHOPPING_TARGET_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
# AMAZON ASSOCIATES COMPLIANCE (2026-09-05).
# Amazon's Operating Agreement requires that EVERY site/channel carrying your
# Associates links is declared in Associates Central, and explicitly warns that
# links appearing on undeclared channels can close the account. So the tag must
# NOT simply appear everywhere the moment AMAZON_TAG is set: it is restricted to
# the channels actually declared to Amazon. Every other channel keeps posting the
# same deal with an untagged (or EarnKaro) link - no deal is lost, the account is
# not exposed. Comma-separated usernames; defaults to the review channel only.
# USER RULE (2026-09-06): "tag ni anni channels lo use cheyu okavela approve
# vasthadi" - once the account is approved, earn on every channel, not just one.
# Amazon's own rule is that a channel carrying the tag must be DECLARED in
# Associates Central. Both can be true at once, so this is one switch:
#
#   AMAZON_TAG_TARGETS=all   -> tag on every owned channel. Use this only AFTER
#                               the account is approved AND every channel below
#                               is listed in Associates Central. Undeclared
#                               channels carrying the tag can close the account.
#   (unset)                  -> tag on the review channel only (safe default,
#                               correct while the application is pending).
#   a,b,c                    -> exactly those channels.
_tag_targets_env = os.getenv("AMAZON_TAG_TARGETS", "").strip()
if _tag_targets_env.lower() in ("all", "*"):
    AMAZON_TAG_TARGETS = {SHOPPING_TARGET, *ALL_OWNED_TARGETS}
elif _tag_targets_env:
    AMAZON_TAG_TARGETS = {t.strip().lstrip("@") for t in _tag_targets_env.split(",") if t.strip()}
else:
    AMAZON_TAG_TARGETS = {SHOPPING_TARGET}
# --- Review-channel posting policy (t.me/smartbuyhub11) ---------------------
# USER RULE (2026-09-05): "daily 20-30 posts, Amazon vi veyu, reject cheyakunda".
# Three programme rules decide what this channel may publish:
#
#  1. LINK-LEVEL DISCLOSURE. Amazon/FTC require a clear disclosure NEXT TO the
#     link on every post - the channel bio alone is not enough. Amazon names
#     "(paid link)", "#ad" and "#CommissionsEarned" as acceptable. Missing this
#     is one of the most common rejection reasons, so it is appended to every
#     post automatically and can never be forgotten.
#  2. AMAZON ONLY. The channel is submitted for the AMAZON programme, so it
#     carries Amazon deals only; a Flipkart/Myntra link on the reviewed channel
#     is off-programme noise. Other stores keep going to the other channels.
#  3. A DAILY CAP. 20-30 quality posts a day reads like a curated shop; a
#     200-post firehose reads like spam and is a rejection risk.
SHOPPING_DISCLOSURE = os.getenv("SHOPPING_DISCLOSURE", "#ad (paid link)").strip()
SHOPPING_AMAZON_ONLY = os.getenv(
    "SHOPPING_AMAZON_ONLY", "true").strip().lower() not in ("0", "false", "no", "off")
SHOPPING_DAILY_CAP = max(0, int(os.getenv("SHOPPING_DAILY_CAP", "50")))
# 7. NO SHORTENER ON THE REVIEWED CHANNEL. USER RULE (2026-09-06): "review
#    channelo shorten ga marchatam bitly use cheyaku". A bit.ly/is.gd hop hides
#    where the link goes; the reviewer must see the store domain in the post.
#    Amazon product links are already collapsed natively to
#    https://www.amazon.in/dp/ASIN?tag=... (~48 chars), so this costs no
#    neatness. Every other channel keeps using the shortener as before.
# 8. HUMAN PACING. USER RULE (2026-09-06): "oka human laga daily oka 50 posts".
#    A burst of 50 posts at 3am reads as an automated feed; the same 50 spread
#    across the day reads as a person curating a shop. Posts are spaced across
#    an active window with jitter, so the timing is never machine-regular.
SHOPPING_HUMAN_PACING = os.getenv(
    "SHOPPING_HUMAN_PACING", "true").strip().lower() not in ("0", "false", "no", "off")
SHOPPING_ACTIVE_START = max(0, min(23, int(os.getenv("SHOPPING_ACTIVE_START", "8"))))
SHOPPING_ACTIVE_END = max(1, min(24, int(os.getenv("SHOPPING_ACTIVE_END", "23"))))
SHOPPING_NATIVE_LINKS = os.getenv(
    "SHOPPING_NATIVE_LINKS", "true").strip().lower() not in ("0", "false", "no", "off")
# 4. NO IMAGES. The 2026-09-06 rejection cited "images (screenshots/screen
#    recordings)" of Amazon. A forwarded deal photo is almost always an Amazon
#    product-page screenshot, which is exactly the trademarked use that gets an
#    application killed. The review channel therefore posts TEXT ONLY; every
#    other channel keeps its media untouched.
SHOPPING_TEXT_ONLY = os.getenv(
    "SHOPPING_TEXT_ONLY", "true").strip().lower() not in ("0", "false", "no", "off")
# 5. NO AMAZON TRADEMARKS IN THE COPY. Verbatim rejection reason (2026-09-06):
#    "unapproved use of Amazon trademarked words, images ... or reviews (which
#    may include variations or misspellings)". Writing "Amazon", "Prime",
#    "Great Indian Festival" etc. in the post body is an unlicensed use of the
#    mark. LINKING to amazon.in is fine and stays; naming it in the text does
#    not. A post that needs the word is simply not shown to the reviewer - it
#    still goes to every other channel unchanged.
_AMAZON_MARK_RE = re.compile(
    r"(?i)(?:\bam[ae]z[o0]?n\w*|\bamzn?\b|\bamz\b|\bprime\s*(?:day|deals?|sale|member\w*|video|music)\b"
    r"|\bgreat\s+indian\s+festival\b|\bgif\s+sale\b|\balexa\b|\bkindle\b|\becho\s*(?:dot|show)?\b"
    r"|\bfire\s*(?:tv|stick)\b|\baudible\b|\bamazon\s*basics\b|\bamazonbasics\b"
    r"|\bab\s*deals?\b|\bmini\s*tv\b|\bpantry\b|\bsubscribe\s*&?\s*save\b)")
# 6. NO POINTERS TO THE LOOT CHANNELS. The rejection email named
#    "https://t.me/LootZoneIndia11" as its worked example, so a reviewer who
#    finds any route from this channel to a loot channel fails the whole
#    application. Nothing on t.me may survive into the review copy.
_TELEGRAM_POINTER_RE = re.compile(r"(?i)(?:https?://)?(?:t\.me|telegram\.(?:me|dog))/\S+|@[A-Za-z]\w{3,}")


def has_amazon_trademark(text: str) -> bool:
    """True when the COPY names an Amazon mark (links are exempt and fine)."""
    return bool(_AMAZON_MARK_RE.search(URL_RE.sub(" ", text or "")))


def has_telegram_pointer(text: str) -> bool:
    """True when the copy points at another Telegram channel (our loot ones)."""
    return bool(_TELEGRAM_POINTER_RE.search(text or ""))


def add_link_disclosure(text: str) -> str:
    """Append the link-level affiliate disclosure exactly once."""
    body = (text or "").strip()
    if not body or not SHOPPING_DISCLOSURE:
        return body
    # Already disclosed (any of the accepted forms)? Never say it twice.
    if re.search(r"(?i)#ad\b|\bpaid link\b|#commissionsearned\b|"
                 r"amazon associate i earn", body):
        return body
    return f"{body}\n\n{SHOPPING_DISCLOSURE}"


def is_amazon_only_post(text: str) -> bool:
    """True when every link in the post is an Amazon link (and there is one)."""
    urls = [clean_url(u) for u in dict.fromkeys(URL_RE.findall(text or ""))]
    if not urls:
        return False
    return all(in_domains((urlparse(u).hostname or "").lower(), AMAZON_DOMAINS)
               for u in urls)


def strip_amazon_tag_for_undeclared(text: str, target: str, affiliate=None) -> str:
    """Remove OUR Associates tag from Amazon links bound for an undeclared channel.

    THE BUG THIS FIXES (found by the user, 2026-09-06): "LootZoneIndia11 ... anni
    tags tho post ayyayi so reject chesaru". Links are SHORTENED in render_job
    before this runs at delivery, so by the time we got here the post read
    "https://bit.ly/3xYz" - no amazon.in hostname to match, nothing stripped, and
    our tag rode into an undeclared loot channel INSIDE the redirect. Amazon sees
    the referrer, and that is the violation that killed the application.

    So a short link WE minted is resolved back to its destination first; if that
    destination is a tagged Amazon URL, the untagged native URL is published
    instead. A source's own short link is left alone - we cannot know where it
    goes, and it was never carrying our tag anyway.

    Only our own tag is touched. The product, the price and the link still work,
    so the reader of that channel loses nothing.
    """
    if not text or not OUR_TAG or target in AMAZON_TAG_TARGETS:
        return text
    out = text
    # Pass 1: our tag hidden behind a shortener we created.
    reverse = dict(getattr(affiliate, "_short_to_long", {}) or {})
    for short, long_url in reverse.items():
        if short not in out:
            continue
        host = (urlparse(clean_url(long_url)).hostname or "").lower()
        if not in_domains(host, AMAZON_DOMAINS):
            continue
        if OUR_TAG.lower() not in long_url.lower():
            continue
        parsed = urlparse(clean_url(long_url))
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() != "tag"]
        out = out.replace(short, parsed._replace(query=urlencode(query, doseq=True)).geturl())
    # Pass 2: the plainly visible amazon.in links.
    for raw in dict.fromkeys(URL_RE.findall(out)):
        url = clean_url(raw)
        host = (urlparse(url).hostname or "").lower()
        if not in_domains(host, AMAZON_DOMAINS):
            continue
        parsed = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() != "tag"]
        untagged = parsed._replace(query=urlencode(query, doseq=True)).geturl()
        if untagged != raw:
            out = out.replace(raw, untagged)
    return out
OUR_MAIN_CHANNEL_LINKS = ("https://t.me/SecretLootIndia1", "https://t.me/LootZoneIndia11")
PREMIUM_MAX_PER_NIGHT = max(1, int(os.getenv("PREMIUM_MAX_PER_NIGHT", "12")))
# The Premium channel is curated, not a firehose — but the gap between its
# picks is now a *setting* instead of a hard-coded 15-35 minutes, so the
# "everything goes out late" feeling can be tuned without a code change.
PREMIUM_GAP_MIN_SECONDS = int(_num("PREMIUM_GAP_MIN_SECONDS", 900, 0, 4 * 3600))
PREMIUM_GAP_MAX_SECONDS = int(max(PREMIUM_GAP_MIN_SECONDS,
                                  _num("PREMIUM_GAP_MAX_SECONDS", 2100, 0, 8 * 3600)))
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
    # USER RULE (2026-09-04): under_99_loot_deals feeds LootZone and PowerLoots
    # too; the Under99/Under499 price channels layer on automatically by price
    # (a >₹99 item from it must not reach the Under-99 channel - that guard
    # stays), and dedup still means exactly one copy per channel.
    "under_99_loot_deals": ["LootZoneIndia11", POWER_FILTER_TARGET],
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
    # Full user source list (2026-08-30): every deal source fans out to the
    # main non-Tricks targets — best deals ALWAYS reach LootZoneIndia11, the
    # rest (Secret, PowerLoots1) get their share; premium/under-99/under-499/
    # card routes are layered on automatically. Previously several of these
    # had EMPTY target lists (dead sources) or an older partial route.
    "deals", "powerloot", "pricehistory", "telugutechtvdeals",
    "indian_online_offer", "idoffers2", "https://t.me/+LP6MYEpCwi0zOGYx",
    "iamprasadtech",
    # 2026-08-30 (source list pt.2): fresh sources + the +qhlEwwkhb2hlNWZl
    # private channel (user wants it feeding LootZone + PowerLoots + the
    # under-₹99 / under-₹499 price channels, which layer on automatically).
    "tech24deals", "Offer_Xpress", "https://t.me/+qhlEwwkhb2hlNWZl",
):
    SOURCE_TO_TARGETS[_source] = list(NO_TRICKS_TARGETS)

SOURCE_TO_TARGETS["TrickXpert"] = [TRICKS_TARGET]
SOURCE_TO_TARGETS["Offerzone_deals"] = [TRICKS_TARGET]
# USER (2026-09-04): t.me/offers_deals_xyz is a TRICKS source - its content goes
# ONLY to the Tricks channel, never to the product channels.
SOURCE_TO_TARGETS["offers_deals_xyz"] = [TRICKS_TARGET]
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
# Same pattern but with a CAPTURE group: re.split drops the separator itself,
# while a capturing group keeps every URL inside the split parts (odd index),
# so text-side cleanup can mask URLs and never touch query chars like '+'.
URL_KEEP_RE = re.compile(r"(https?://[^\s<>\[\](){}\"']+)", re.I)
INTENT_RE = re.compile(r"intent://[^\s]+", re.I)
ASIN_RE = re.compile(r"(?:/dp/|/gp/product/|/gp/aw/d/)([A-Z0-9]{10})(?:[/?#]|$)", re.I)

SHORT_DOMAINS = {
    "amzn.to", "amzn.in", "amazn.lt", "link.amazon", "fkrt.co", "fkrt.cc", "fkrt.in",
    "fktr.in", "myntr.it", "myntr.in", "ajiio.co", "bit.ly", "bitli.in", "bitl.in",
    "bittli.in", "bilty.co", "tinyurl.com", "cutt.ly", "rb.gy", "t.ly", "tiny.cc",
    "shorturl.at", "is.gd", "v.gd", "snip.ly", "linkredirect.in", "ekaro.in",
    "clnk.in", "clnk.app", "ekaro.app", "l.ead.me",
    # USER RULE (2026-09-04): WHATEVER shortener the source used, the link is
    # resolved to its real store page and monetized as OUR link. These are the
    # rest of the wrappers the loot channels actually paste:
    "bitly.com", "j.mp", "t.co", "goo.gl", "buff.ly", "ow.ly", "tidd.ly",
    "geni.us", "spoo.me", "da.gd", "surl.li", "shrtco.de", "9qr.de", "cli.re",
    "shorte.st", "v.ht", "y2u.be", "dl.flipkart.com", "msho.in", "meesho.app",
    "extp.in", "wishlink.com", "hypd.store", "bylink.in", "mylink.store",
    "applink.adjust.com", "app.ajio.com", "ajiio.in", "tatacliq.app",
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
    # LOOKALIKES of our EarnKaro shortener (bitli.in): "bitl.in" / "bittli.in"
    # are other channels' short domains. An API result on one of these is a
    # foreign echo, never our commission link - reject it like fkrt.co.
    "bitl.in", "bittli.in",
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
# A share/forward/invite link is not a destination: nothing is bought at
# "wa.me/?text=..." or "t.me/share/url", and a link to another channel's post is
# that channel's promotion, not our reader's deal. These hosts are still excluded from
# the merchant destination list on purpose - and the reason matters. A source post
# carrying a WhatsApp share button used to be retried forever ("affiliate conversion
# returned no link: wa.me") and then dropped when the price went stale: the source
# channel had the deal, our channels never saw it. So a share link is never converted,
# never health-checked, and above all never a reason to swallow a post.
# A host whose only job is redirecting. Enumerating them is hopeless (a source channel
# posts bit.ly today and bitly.com/74265 tomorrow), so this list is only the part we are
# SURE about - is_unresolvable_short_link() below adds the shape test and is what decides.
# Anything here whose link never resolves is cut from the copy; the deal still goes out.
SHORTENER_HOSTS = (FOREIGN_ECHO_DOMAINS | OUR_RUNTIME_SHORTENER_DOMAINS
                   | OUR_SHORTENER_DOMAINS
                   | {"bit.ly", "j.mp", "bitly.com", "t.co", "buff.ly", "ow.ly",
                      "tiny-urls", "short.link", "go.link", "spl.ink", "tini.go",
                      "tidd.ly", "geni.us", "amzn.to", "amzn.in", "s.click"})


def is_unresolvable_short_link(host: str, url: str) -> bool:
    """True for a link that exists only to redirect and gave us nothing.

    Such a link is not a destination: it has no product page behind it that we could
    publish, monetize or verify. Retrying the WHOLE post for it (the old behaviour) is how
    a source post vanished - so the caller cuts the link and posts the deal. The shape does
    the work here, not a blocklist: any host that is not a known merchant or service store
    and carries a single short slug is a shortener as far as we are concerned.
    """
    if not host or in_domains(host, KNOWN_MERCHANT_DOMAINS) or in_domains(host, SERVICE_OFFER_DOMAINS):
        return False
    if in_domains(host, SHORTENER_HOSTS):
        return True
    path = (urlparse(str(url or "")).path or "").strip("/")
    return bool(path) and "/" not in path and len(path) <= 24


SHARE_INTENT_DOMAINS = NON_STORE_DOMAINS | {
    "wa.me", "api.whatsapp.com", "chat.whatsapp.com", "web.whatsapp.com", "wa.link",
    "telegram.me", "telegram.dog", "tl.me", "addtoany.com", "sharethis.com",
    "getpocket.com", "pocket.co", "vk.com", "twitter.com", "x.com", "m.me", "facebook.net",
    "viber.com", "line.me",
}


def is_share_intent(url: str) -> bool:
    """True for share / forward / invite links, which are not merchant pages."""
    raw = str(url or "")
    if raw.startswith(("tg://", "whatsapp://", "viber://", "line://", "sms:", "mailto:", "tel:")):
        return True
    if "wa.me/" in raw.lower() and "/sendtext=" in raw.lower():
        return True
    host = (urlparse(raw).hostname or "").lower()
    if not host:
        return False
    if in_domains(host, SHARE_INTENT_DOMAINS):
        return True
    # t.me/<channel>/<id> and t.me/+invite are channel links, never product pages.
    return host in ("t.me", "telegram.me", "telegram.dog", "telegram.org", "tl.me")


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
        # TAGLESS: only add our tag if configured; pinned empty means strip stranger tags
        if OUR_TAG:
            query.append(("tag", OUR_TAG))
        return parsed._replace(query=urlencode(query, doseq=True)).geturl()
    except Exception:
        return clean_url(link)


def compact_amazon_product_link(link: str) -> str:
    """Collapse an Amazon PRODUCT (/dp/ASIN or /gp/product/ASIN) URL to its
    shortest native form. TAGLESS: no tag is kept/added – clean /dp/ASIN only
      https://www.amazon.in/dp/ASIN   (tagless)
    All noise params (psc/smid/th/ref/linkCode/tag ...) are dropped, so product
    links in a list are short WITHOUT spending Bitly quota. Amazon SEARCH /
    category links (/s?...) have no single ASIN and are returned untouched so
    the final shorten pass can Bitly them. Preserves the Amazon TLD."""
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
        netloc = host if host.startswith("www.") else "www." + host
        # TAGLESS: never keep source tag; only our tag if configured
        if OUR_TAG:
            ref = None
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                if key.lower() == "ref" and value:
                    ref = value
            query = urlencode([("tag", OUR_TAG)] + ([("ref", ref)] if ref else []))
            return f"https://{netloc}/dp/{asin}?{query}"
        # Tagless clean form – no tag, no ref
        return f"https://{netloc}/dp/{asin}"
    except Exception:
        return clean_url(link)


# A Flipkart product URL is "slug + /p/ + item id + pid": lid, marketplace, srno,
# ssid, otracker and the rest are session/tracking noise that triples the length.
# USER RULE (2026-09-05, "shortga ravali"): a link of ours must never go out 177
# characters long just because the shortener was busy, so a product link is first
# compacted natively (free, no quota) and only then handed to the shortener.
# The leading "/dl" of a dl.flipkart.com app link is part of the wrapper, not of
# the product path: www.flipkart.com/<slug>/p/<item id> is the canonical page.
FLIPKART_ITEM_RE = re.compile(r"(?i)^(?:/dl)?(?P<path>/.*?/p/itm[a-z0-9]+)")
FLIPKART_HOSTS = {"flipkart.com", "www.flipkart.com", "dl.flipkart.com", "m.flipkart.com"}


def compact_flipkart_product_link(link: str) -> str:
    """Collapse a Flipkart product URL to slug + item id + pid.

    Only a PRODUCT page (/p/itm...) is touched, and only tracking parameters are
    dropped - `pid` is the product identity and is always kept, so the link still
    opens exactly the item the source linked. A search/category/offer URL has no
    item id and is returned untouched for the shortener to handle.
    """
    try:
        raw = clean_url(link)
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host not in FLIPKART_HOSTS:
            return raw
        match = FLIPKART_ITEM_RE.match(parsed.path or "")
        if not match:
            return raw
        pid = ""
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() == "pid" and value:
                pid = value
                break
        # A dl.* link is Flipkart's own app-redirect wrapper for the same page.
        netloc = "www.flipkart.com"
        return parsed._replace(
            scheme="https", netloc=netloc, path=match.group("path"),
            query=urlencode([("pid", pid)]) if pid else "", fragment="", params="",
        ).geturl()
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
        # v17.8: the SAME product reaches us from several sources, each with its
        # own link shape (a slug link, a variant link, an extra ?size= parameter).
        # Keying those on the URL hash made them look like different deals, so the
        # channel posted the same product three times. Every merchant below has a
        # stable product id in its path - take that instead of the whole URL.
        for pattern, label in (
            (r"/p/([A-Za-z0-9]{6,})", "PID"),                  # flipkart /p/ITM..
            (r"/product(?:s)?/[A-Za-z0-9_-]*?-?([A-Za-z0-9]{6,})(?:[/?#]|$)", "PROD"),
            (r"/it/([A-Za-z0-9_-]{4,})", "IT"),                 # croma / reliancetrend
            (r"/([A-Za-z0-9_-]{3,})-([A-Za-z0-9]{6,})\.html", "SKU"),
            (r"/products?/([A-Za-z0-9_-]{6,})", "PROD"),
        ):
            match = re.search(pattern, parsed.path, re.I)
            if match:
                return f"{label}:{host}:{'/'.join(g.lower() for g in match.groups())}"
    except Exception:
        pass
    return None


# Pure hype/banner lines ("TOP DEAL OF THE DAY", "🔥🔥 FLASH SALE ⚡⚡") that a
# source repeats above every single post. They say nothing about the product, so
# they must never be used as a campaign identity on their own.
GENERIC_HEADLINE_RE = re.compile(
    r"(?i)^(?:[\W_]+|top|best|hot|mega|super|dhamaka|dhamal|amazing|awesome|superb|daily|"
    r"latest|new|today|deal|deals|offer|offers|loot|loots|sale|steal|alert|alerts|save|savings|"
    r"price|prices|drop|drops|crash|shocker|shocking|free|gift|bonus|grab|hurry|limited|time|"
    r"of|the|a|an|for|you|your|only|off|in|india|amazon|flipkart|myntra)]+$"
)


def content_deal_key(text: str) -> str | None:
    """Cross-source fingerprint to block the same deal even when short URLs differ.

    The prices/discounts are part of the identity on purpose. Keying on the
    banner line alone (the old behaviour) made EVERY post from a source that
    reuses a hype prefix collapse onto one fingerprint, so after the first post
    the whole source looked "already posted" for ten hours and silently
    disappeared - the exact "source has the posts, our channels do not" gap.
    """
    cleaned = clean_source_text(text)
    cleaned = URL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"(?i)\b(?:buy\s*now|shop\s*now|grab\s*fast)\b", " ", cleaned)
    signals = sorted({re.sub(r"\s+", "", s).lower() for s in re.findall(
        r"₹\s*\d[\d,]*|\d{1,3}\s*%|\d+(?:\.\d+)?\s*off", cleaned)})
    cleaned = re.sub(r"[^\w₹%\n]+", " ", cleaned, flags=re.UNICODE)
    lines = [re.sub(r"\s+", " ", line).strip().lower()
             for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return None
    headline = lines[0]
    # A product-specific headline blocks reposted campaigns even when a later
    # source carries fewer/more variant links. Generic or short banners fall
    # back to the body lines so different products never share a fingerprint.
    specific = (len(headline) >= 18 and len(headline.split()) >= 3
                and not GENERIC_HEADLINE_RE.fullmatch(headline))
    basis = headline if specific else " ".join(lines[:10])
    basis = " ".join(basis.split())
    if signals:
        basis = f"{basis} || {' '.join(signals)}"
    if len(basis) < 18 or len(basis.split()) < 3:
        return None
    return "CONTENT:" + hashlib.sha256(basis.encode()).hexdigest()
# ---------------------------------------------------------------------------
# v17.6 "best copy" selection + numeric fidelity gate
# ---------------------------------------------------------------------------
# Many sources post the SAME product within minutes at different prices. The
# intake order used to decide which copy reached our channels, so a 78%-off
# post could be replaced in the queue by a weaker 65% copy from another channel
# (or the better copy silently dropped by the ledger). Now the queue keeps the
# BEST copy of an exactly-matching product id - never a second row, so nothing
# is duplicated, and the copy it displaced is remembered so the deal is still
# posted if the better copy turns out to be unrenderable.
PRODUCT_ID_RE = re.compile(
    r"(?:/dp/|/gp/product/|/gp/aw/d/|/product[iI]d/|[?&]pid=|/ip/)([A-Za-z0-9]{6,20})")


RUPEE = "\u20b9"  # the only currency symbol these sources publish


def product_identities(text: str) -> list[str]:
    """Merchant product ids a post names, readable WITHOUT the network.

    Only exact ids (/dp/ASIN, flipkart pid, ...) count: a shortener-only post
    yields nothing and is therefore never re-pointed, so a fuzzy match can never
    drop a deal.
    """
    found = []
    for raw in dict.fromkeys(URL_RE.findall(text or "")):
        match = PRODUCT_ID_RE.search(raw)
        if match:
            found.append(match.group(1).upper())
    return list(dict.fromkeys(found))


def comparable_product_ids(items) -> set[str]:
    """Normalise intake ids and rendered deal keys onto the same bare id."""
    out: set[str] = set()
    for item in items or []:
        value = str(item).strip()
        match = re.fullmatch(r"(?:pid|asin|amazon|flipkart):([A-Za-z0-9]{6,20})", value, re.I)
        if match:
            out.add(match.group(1).upper())
        elif re.fullmatch(r"[A-Za-z0-9]{6,20}", value) and re.search(r"\d", value):
            out.add(value.upper())
    return out


def deal_quality_score(text: str) -> int:
    """How good this copy of the deal is (discount-led, price aware)."""
    price = parse_price(text)
    return premium_score(text, price, parse_discount(text, price))


def numeric_tokens(text: str) -> set[str]:
    """Every number a post states, with URLs masked out.

    URL masking matters: our own affiliate tag ("deals0911-21"), an ASIN and a
    shortener slug are digits the source text never claimed, and they must not
    be confused with prices or discounts.
    """
    without_urls = URL_RE.sub(" ", text or "")
    return {token.replace(",", "") for token in re.findall(r"\d[\d,]*", without_urls)}


def enforce_numeric_fidelity(source_text: str, rendered: str) -> tuple[str, list[str]]:
    """Gate: a published post may not contain a price/discount figure the source
    never printed. Legitimate text is never touched - only a number that appears
    NOWHERE in the source (URL digits excluded on both sides) is removed, because
    every such number so far has been corruption (a glued shortener token, a
    half-stripped "MRP", a mangled percent). The post itself is kept: dropping a
    live deal to protect formatting is the worse bug.
    """
    allowed = numeric_tokens(source_text)
    notes: list[str] = []
    lines = (rendered or "").split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        # Spans are measured on the ORIGINAL line and URL ranges are excluded, so a
        # figure can never be cut out of the middle of a link (`/50%-sale`) while
        # the same digits in the deal text are judged normally.
        spans = [(m.start(), m.end()) for m in URL_RE.finditer(line)]
        cuts: list[tuple[int, int]] = []
        for pattern in (rf"{RUPEE}\s*\d[\d,]*", r"\b\d{1,3}\s*%"):
            for match in re.finditer(pattern, line):
                if any(match.start() < end and match.end() > start for start, end in spans):
                    continue
                digits = re.sub(r"\D", "", match.group(0))
                if not digits or digits in allowed:
                    continue
                cuts.append(match.span())
                notes.append(f"line {index + 1}: {match.group(0).strip()!r}")
        if cuts:
            scrubbed = line
            for start, end in sorted(cuts, reverse=True):
                scrubbed = scrubbed[:start] + " " + scrubbed[end:]
            lines[index] = re.sub(r"[ \t]{2,}", " ", scrubbed).strip(" ,:;|")
    return "\n".join(lines), notes


# Words that name the PRODUCT, not the campaign. A signature is only trusted
# when the headline survives this filter with substance left over, so a shared
# "TOP DEAL OF THE DAY" can never make two different products look identical.
# >>> IDENTITY-START (ops/quality_audit.py mirrors this block; run
#     python3 ops/sync_identity.py after editing it, and test_line_fidelity.py
#     fails if the two copies ever differ)
_SIG_STOP_WORDS = frozenset("""
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
_SIG_VARIANT_UNITS = (
    "gb tb mb kb l ltr liter liters litre litres ml kg ton tons star stars inch "
    "inches in ft hp kva burner burners slice slices tray trays door doors person "
    "persons blade blades"
).split()
_SIG_SPEC_UNITS = (
    "h hr hrs hour hours min mins sec secs mah wh w kw a v p k g fps hz px mm cm m "
    "db rpm mp nit nits lumen lumens mbps gbps byte bytes watt watts"
).split()
_SIG_VARIANT_RE = re.compile(
    r"\b(\d{1,4}(?:\.\d+)?)[\s_-]*("
    + "|".join(map(re.escape, sorted(_SIG_VARIANT_UNITS, key=len, reverse=True))) + r")\b", re.I)
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
_SIG_VARIANTS = frozenset("""
pro plus max ultra lite neo fe se mini prime classic edge fold flip turbo
men mens women womens kids boys girls unisex
""".split())
# Words that put a number in front of them into a model name: "Pro 4" and "Model
# 2600" are the product, "2023" at the end of a headline is the launch year.
_SIG_QUALIFIERS = _SIG_VARIANTS | frozenset("model series gen generation version".split())
# "by <word>" names the brand ("Airdopes 141 by boAt") EXCEPT when the word in
# front says otherwise: "powered by Helio" names a chipset, not the maker.
_SIG_BY_NON_BRAND = frozenset(
    "powered brought inspired sponsored posted shared sent curated verified".split())
_SIG_AMOUNT_RE = re.compile(
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
_SIG_PRICE_RE = re.compile(
    r"(?i)(?:[\u20b9$]|\brs\.?|\binr|@|\bat\b)\s*([\d,]{2,})(?:\.\d+)?\b"
    r"|\b([\d,]{2,})\s*/-"
    r"|\b([\d,]{2,})(?=\s*\(?\s*\d{1,3}\s*(?:%|percent))")


def _sig_line_price(line: str) -> int | None:
    match = _SIG_PRICE_RE.search(line or "")
    if not match:
        return None
    raw = next((g for g in match.groups() if g), "")
    try:
        value = int(str(raw).replace(",", ""))
    except ValueError:
        return None
    return value if 1 <= value <= 1_000_000 else None


def _sig_tokens(line: str) -> list[str]:
    """Lower-case tokens with punctuation trimmed off the ENDS only.

    Trimming ends instead of deleting every symbol keeps a model number whole:
    "WH-CH720N" stays one token and "1.5 Ton" stays one number, instead of
    splitting into a stray "1" and "5" that read as two different products.
    """
    named = _SIG_AMOUNT_RE.sub(" ", line or "")
    named = re.sub(r"(?i)\b(?:%|percent|off)\b", " ", named)
    out = []
    for token in named.split():
        token = re.sub(r"^[^\w.]+", "", token)
        token = re.sub(r"[^\w.]+$", "", token).strip(".")
        if token and re.search(r"[A-Za-z0-9]", token):
            out.append(token.lower())
    return out


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
    raw = [re.sub(r"[-_]", "", tok) for tok in _sig_tokens(line)]
    words = [w for w in raw if not w.isdigit() and w not in _SIG_STOP_WORDS]
    if len(words) < 2:
        return None
    spec_units = frozenset(_SIG_SPEC_UNITS)
    variant_units = frozenset(_SIG_VARIANT_UNITS)
    ids = {f"{number}{unit}".lower().replace(" ", "").replace("-", "")
           for number, unit in _SIG_VARIANT_RE.findall(line or "")}
    digit_cores = {re.sub(r"\D", "", value) for value in ids}
    models = set()
    for index, token in enumerate(raw):
        if token in _SIG_STOP_WORDS:
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
            if previous in _SIG_QUALIFIERS:
                models.add(previous + digits)   # "Pro 4" / "Model 2600" is the model
                continue
            if len(digits) < 2 or (len(digits) == 4 and 1900 <= int(digits) <= 2099):
                continue                   # a quantity, or a launch year - not an id
            if line_price is not None and int(digits) == line_price:
                continue                   # that is the price, however it was typed
            if digits in digit_cores:
                continue                   # the number is already inside a size id
            models.add(token)
    variants = {t for t in raw if t in _SIG_VARIANTS}
    if not ids and not models:
        # No number to hold on to, so every product word has to agree. THREE
        # words is enough when they are long enough to be a real product name
        # ("Nutriburst Collagen powder"): demanding four made a named product
        # with no model number un-dedupable, which is how the same collagen
        # powder went out twice under two different banner words. A short
        # category phrase ("hair oil set") still fails the length test below.
        if len(words) < 3:
            return None                    # a category phrase is not an identity
        basis = " ".join(sorted(set(words)))
        return None if len(basis) < 18 else ("W", basis)
    # The brand is normally the first product word, but "Airdopes 141 by boAt"
    # and "boAt Airdopes 141" are ONE product: an explicit "by <maker>" names
    # the brand outright and wins over word order, so the reordered copy can
    # never slip past dedup as a second post. "powered by Helio" and friends
    # name a component, not the maker, and are ignored.
    brand = words[0]
    for index, token in enumerate(raw[:-1]):
        if token == "by" and (index == 0 or raw[index - 1] not in _SIG_BY_NON_BRAND):
            candidate = raw[index + 1]
            if candidate in words:
                brand = candidate
                break
    parts = ["M", brand, " ".join(sorted(models | ids))]
    if variants:
        parts.append(" ".join(sorted(variants)))
    return tuple(parts)


# <<< IDENTITY-END

def product_signature(text: str) -> str | None:
    """A conservative "which product is this?" key, used ONLY to skip a repeat.

    Why not the link: two sources paste the same product through two different
    shorteners, and until each one is resolved the merchant ids simply look
    different - which is how a channel carried the same earphones twice. So the
    identity here is the product's OWN words - brand, model number, variant,
    capacity - and nothing else: identical for an /dp/ASIN post and a bitli post
    of the same item, different for 128GB vs 256GB and for 141 vs 131.

    Deliberately narrow so a real deal is never lost on a false match:
      * a single-product post only (3+ merchant links is a roundup, and roundups
        legitimately share items with each other);
      * a headline naming a brand plus a model number (or four product words);
      * a merchant link must be present at all (a pure text blurb gets no key).
    The key never swaps, reorders or rewrites anything: its whole job is to skip
    a product this channel has already shown - which is what the user asked for.
    """
    if not text:
        return None
    urls = [clean_url(u) for u in dict.fromkeys(URL_RE.findall(text))]
    merchant_links = 0
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if host and not in_domains(host, NON_STORE_DOMAINS):
            merchant_links += 1
    if merchant_links == 0:
        return None
    if merchant_links >= 3:
        # A ROUNDUP. Until now these got no key at all, so the same list
        # arriving again from a second source - different shortener, different
        # banner - was invisible to the per-channel repeat guard and the channel
        # carried it twice. That is the "multiple times" the user reported.
        # A roundup's identity is the SET of products it lists: order-independent
        # (sources shuffle the order), link-independent (shorteners differ),
        # banner-independent. Two roundups that list the same items ARE the same
        # post; a roundup that adds or drops an item is a different one and still
        # goes out.
        return _roundup_signature(text)
    headline = None
    for line in clean_source_text(text).splitlines():
        line = line.strip()
        if not line or URL_RE.search(line):
            continue
        if TIME_OF_DAY_RE.search(line) or GENERIC_HEADLINE_RE.fullmatch(line.lower()):
            continue                       # a hype banner is not a product name
        if not re.search(r"[A-Za-z]", line):
            continue                       # a bare price / separator line
        # USER RULE (2026-09-05): "Loot : X" and "Grab : X" are the SAME product
        # wearing two different banner words, and the banner used to be read as
        # the brand - so the identity differed and the same deal was published
        # twice. The banner prefix is source decoration; strip it before the
        # product's own words are read. (_SAFE_PREFIX_RE is the same list the
        # programme-safe rewrite uses, so the two can never drift apart.)
        line = _SAFE_PREFIX_RE.sub("", line).strip() or line
        identity = _product_identity(line)
        if identity:
            headline = identity
            break
    if not headline:
        return None
    basis = "|".join(headline)
    return "SIG:" + hashlib.sha256(basis.encode()).hexdigest()[:32]


def _roundup_signature(text: str) -> str | None:
    """Identity of a multi-product list: the set of products it names.

    Built from the same _product_identity() the single-product path uses, so a
    roundup and its repeat agree even when the two sources write different
    banners, different prices formats and different short links.
    """
    items: set[str] = set()
    for line in clean_source_text(text or "").splitlines():
        line = line.strip()
        if not line or URL_RE.fullmatch(line):
            continue
        line = URL_RE.sub(" ", line).strip()
        if not line or TIME_OF_DAY_RE.search(line) or GENERIC_HEADLINE_RE.fullmatch(line.lower()):
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        line = _SAFE_PREFIX_RE.sub("", line).strip() or line
        identity = _product_identity(line)
        if identity:
            items.add("|".join(identity))
    # Two items is the floor: below that this is not a list, and a one-item
    # match is too thin to justify dropping a live deal.
    if len(items) < 2:
        return None
    basis = "\n".join(sorted(items))
    return "LIST:" + hashlib.sha256(basis.encode()).hexdigest()[:32]


def product_key(value: str) -> str:
    return extract_product_id(value) or "URL:" + hashlib.sha256(merchant_url(value).encode()).hexdigest()


def parse_price(text: str) -> int | None:
    """Prefer explicit deal/effective price; avoid MRP/coupon/bank discount values.

    Deep-audit fixes (2026-09-04): "MRP ₹1,999 Deal ₹899" used to return the
    MRP (routing a ₹899 deal as if it cost ₹1,999); "INR 799", "Price: 349/-"
    and "349/-" (the Indian price suffix) were not read at all. MRP-labelled
    amounts are masked before the generic currency scan, so the generic match
    can only ever see the DEAL price. Bare numbers with no currency marker are
    still ignored on purpose - a model number is not a price."""
    value = text or ""
    # Mask "MRP ₹1,999" style amounts so no later pattern can mistake the MRP
    # for the deal price. The deal-price labels below never say MRP.
    masked = re.sub(r"(?i)\b(?:mrp|m\.r\.p\.?|list\s*price|regular\s*price)\s*[:@-]?\s*(?:rs\.?|₹|inr)?\s*[\d,]+",
                    " ", value)
    patterns = [
        r"(?:deal|effective|offer|final)\s*price\s*[:@-]?\s*(?:rs\.?|₹|inr)?\s*([\d,]+)",
        r"(?:deal|offer|now|today)\s*[:@-]?\s*(?:rs\.?|₹|inr)\s*\.?\s*([\d,]+)",
        r"(?:only|at|@)\s*(?:rs\.?|₹|inr)?\s*([\d,]+)",
        r"(?:rs\.?|₹|inr)\s*\.?\s*([\d,]+)(?!\s*(?:off|coupon|cashback))",
        # Indian "/-" price suffix: "Price: 349/-", "349/-" is explicit money.
        r"(?:price\s*[:@-]?\s*)?([\d,]{2,})\s*/-",
        # Suffix currency: "749 rs" / "249Rs." / "1,299 INR" - money named AFTER
        # the number is still explicit money.
        r"([\d,]{2,})\s*(?:rs\.?|inr)\b",
        r"^\s*([\d,]{2,})\s+(?:https?://|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, masked, re.I | re.M)
        if match:
            price = int(match.group(1).replace(",", ""))
            if 1 <= price <= 1_000_000:
                return price
    # No deal price anywhere: an MRP-only line ("MRP: ₹270") is still the only
    # money on the post, so it is the best available answer (old behaviour).
    # The masking above only stops MRP from SHADOWING a real deal price.
    mrp = re.search(r"(?i)\b(?:mrp|m\.r\.p\.?|list\s*price|regular\s*price)\s*[:@-]?\s*(?:rs\.?|₹|inr)?\s*([\d,]+)", value)
    if mrp:
        price = int(mrp.group(1).replace(",", ""))
        if 1 <= price <= 1_000_000:
            return price
    return None


def parse_discount(text: str, price: int | None = None) -> int | None:
    """Return the strongest explicit/derived product discount percentage."""
    value = text or ""
    found: list[int] = []
    patterns = (
        r"(?:up\s*to|upto|flat|save|get)?\s*([1-9]\d?|100)\s*%\s*(?:off|discount)",
        # "save 25 %" / "get 40 %" with a space before the sign and no
        # off/discount word after it — the label BEFORE the number is what
        # makes it a discount, so plain "25 %" alone still never matches.
        r"(?:save|get|upto|up\s*to|flat)\s+([1-9]\d?|100)\s*%",
        # "Discount: 26%" is how a loot channel actually writes it - the colon after
        # the label used to lose the whole percentage, which matters because the
        # discount decides which price-tier channel a deal is routed to.
        r"(?:off|discount|savings?)\s*[:=-]?\s*(?:up\s*to|upto|flat)?\s*[:=-]?\s*([1-9]\d?|100)\s*(?:%|percent\b)",
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
    """PREMIUM channel = only the BEST deals (user rule, Aug 2026 restated:
    "premium dantlo best ga ... highest discount vunte post cheyali alantivi"):
      * 80%+ discount (any price) - a true top-tier deal,
      * a sub-₹99 price that ALSO carries a real discount (50%+) or an explicit
        lowest-price claim - cheap alone is not "the highest discount",
      * ₹100-₹499 with a 70%+ discount or a lowest-price claim.
    Best multi-product LISTS and credit/bank-card offers are added to Premium
    directly by the router regardless of this gate."""
    if discount is not None and discount >= 80:
        return True
    if price is not None and price <= 99 and ((discount or 0) >= 50 or has_lowest_price_claim(text)):
        return True
    return bool(
        price is not None and 100 <= price <= 499
        and ((discount is not None and discount >= 70) or has_lowest_price_claim(text))
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
    # msg.message is the raw text the entity offsets are measured on; msg.text
    # may carry parse-mode markdown ("**") that shifts every offset after it.
    text = msg.message or msg.text or ""
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
    # Referral / app-install spam that carries no deal content (user rule: no
    # unwanted words in our posts).
    re.compile(r"\brefer\s+(?:a\s+)?(?:friend|mate|user)s?\b", re.I),
    re.compile(r"\b(?:earn|win|get)\s+(?:₹|rs\.?|inr\s?)\s*\d+\s*(?:on|for|by|each)?\s*"
               r"\b(?:referral|refer|sign\s*-?\s*up|signup|invite)\b", re.I),
    re.compile(r"\binstall\s+(?:the\s+|our\s+|this\s+)?(?:app|apk)\b", re.I),
    re.compile(r"\b(?:sent|posted|powered)\s+via\s+\S+|\bvia\s+\w+\s+admin\b", re.I),
]
_PROMO_WORD_RE = re.compile(
    r"t\.me|telegram|whatsapp|join|subscribe|follow|share|forward|click|tap|"
    r"notification|notifications|hurry|grab|miss|deal|deals|offer|offers|loot|loots|"
    r"channel|group|friends?|everyone|family|buy|shop|order|now|here|link|below|"
    r"this|that|your|our|us|out|on|off|and|the|for|get|more|new|daily|fast|time|"
    r"turn|enable|activate|don'?t|do|not|never", re.I)


def is_promo_only_url(url: str) -> bool:
    """True for social/channel/navigation links — never merchant deal content."""
    try:
        host = (urlparse(clean_url(url)).hostname or "").lower()
    except Exception:
        return True
    if not host:
        return True
    return in_domains(host, NON_STORE_DOMAINS)


# Channel invite/forward links: a line that carries one of these is boilerplate
# by definition (we always replace source self-promo with OUR folder/channel).
CHANNEL_INVITE_URL_RE = re.compile(
    r"(?i)t\.me/(?:addlist/|joinchat/|\+)|telegram\.me/(?:\+|joinchat/)"
    r"|whatsapp\.com/(?:channel|group)/"
)
# Wording that marks a line as channel navigation rather than deal content.
PROMO_INTENT_RE = re.compile(
    r"(?i)\b(?:join|subscribe|follow|unfollow|share|forward|visit|open|check|"
    r"notify|notifications?|turn\s+on|enable|activate)\b"
    r"|\bfor\s+more\b|\bmore\s+(?:loot|deal|update)s?\b"
    r"|\bour\s+(?:channel|group|whatsapp|telegram)\b|\b(?:our|the)\s+official\s+channel\b"
)


# Referral/invite farming is never deal content, even when a ₹ amount appears
# in the sentence — dropped ahead of the price guard on purpose.
REFERRAL_SPAM_RE = re.compile(
    r"(?i)\b(?:refer|invite)\s+(?:a\s+)?(?:friend|mate|user|family|one)\b"
    r"|\b(?:earn|win|get)\s+(?:₹|rs\.?|inr\s?)?\s*\d+\s*(?:each|per\s+user)?\s*"
    r"(?:on|for|by|after|in)?\s*(?:referral|referrals|refer|invite|signup|sign\s*-?\s*up)\b"
    r"|\b(?:referral|invite)\s+code\b"
    # Loot channels staple an app-install / refer-N-friends block under the real
    # deal ("Get Flipkart App - Refer 3 friends and \u20b9100 referral bonus"). Such
    # a line carries a \u20b9 amount, so the price guard alone used to keep it alive.
    r"|\b(?:refer|invite)\b[^.\n]{0,40}\b(?:friends?|mates?|budd(?:y|ies)|users?)\b"
    r"|\b(?:referral|invite|sign\s*-?\s*up|joining)\s*(?:bonus|reward|cashback|incentive)\b"
    r"|\binstall\s+(?:the\s+|our\s+|this\s+)?(?:app|apk)\b"
)


def is_promo_noise_line(line: str) -> bool:
    """True only for pure promo/navigation boilerplate (no deal content)."""
    t = (line or "").strip()
    if not t:
        return False
    if REFERRAL_SPAM_RE.search(t):
        return True
    urls = URL_RE.findall(t)
    if urls:
        # A real merchant link is always deal content. But a line whose ONLY
        # links are social/channel links is boilerplate: dropping the whole line
        # is also what stops half-stripped residue (`.com/channel/0029`,
        # `.me/someotherchannel`) from leaking into our posts.
        if any(not is_promo_only_url(url) for url in urls):
            return False
        if CHANNEL_INVITE_URL_RE.search(t) or PROMO_INTENT_RE.search(t):
            return True
        return any(rx.search(t) for rx in PROMO_PATTERNS_PY)
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
# v17.8 A CTA clause may only ever swallow PLAIN WORDS. The tails used to be
# `[^.\n|]*$` - "everything up to the end of the line" - so a source line like
# "More offers: Apply coupon PEOPLE200" lost its coupon code and "…(78% off) buy
# now ₹199" lost the deal. A price, a digit, a percentage, a code-looking token
# or a link now ends the clause, and the rest of the line is published verbatim.
CTA_TAIL_PY = (
    r"(?:[ \t]+(?![₹$%])(?![A-Za-z0-9+/.*\-]*\d)(?![A-Za-z0-9+/.\-]*%)"
    r"(?![A-Za-z]*://)[^\s₹$%|]+)*"
)

GLOBAL_CTA_PATTERNS_PY = (
    r"\b(?:buy|shop|order|grab|get)\s+(?:it\s+)?now\b[!^0-9]*",
    r"\bgrab\s+(?:it|this|your|yours|fast)\s*(?:now|fast|soon)?\b[!^0-9]*",
    r"\bget\s+yours?\b[!^0-9]*",
    r"\bbuy\s+(?:it\s+)?here\b", r"\bshop\s+here\b", r"\border\s+here\b",
    rf"\b(?:click|tap)\s+(?:here|the\s+link|on\s+(?:the\s+)?link|below|to\s+(?:buy|order|shop))\b{CTA_TAIL_PY}",
    rf"\b(?:don'?t|do\s+not|never)\s+miss\s+(?:it|this|out|the\s+deal|this\s+deal)\b{CTA_TAIL_PY}",
    # "Hurry up guys 🏃 limited stock" — the hype words AFTER the CTA are the
    # same boilerplate and go with it; the tail stops at any price/digit/link,
    # and the fidelity guard restores the line if real deal payload was taken.
    # A standalone "Limited stock" (availability info on a coupon/deal line)
    # is deal CONTENT and stays — only the hurry-up clause takes it along.
    # "up" is optional: "Hurry!!", "Hurry limited period deal!!!" and
    # "Hurry, grab fast" are the same channel hype as "Hurry up guys".
    rf"\bhurry(?:\s*up)?\b[!,.]*{CTA_TAIL_PY}",
    rf"\bloot\s+(?:it\s+)?fa+s*t+\b!*{CTA_TAIL_PY}",
    rf"\bturn\s+on\s+notifications?\b{CTA_TAIL_PY}",
    rf"\bstay\s+tuned\b{CTA_TAIL_PY}",
    rf"\blink\s+(?:in\s+(?:bio|comments?|description)|below)\b{CTA_TAIL_PY}",
    rf"\bcheck\s+(?:link|bio|description|comments?|pinned|our\s+channel)\b{CTA_TAIL_PY}",
    rf"\bshare\s+(?:it\s+)?(?:with|to)\s+[^.\n|]*\b(?:friends?|family|groups?|everyone)\b{CTA_TAIL_PY}",
    r"\bforward\s+to\s+@?\w+[^.\n|]*",
    rf"\b(?:visit|open)\s+(?:our\s+)?(?:channel|t\.me/\S+|whatsapp\s+channel)\b{CTA_TAIL_PY}",
    # "... from our telegram channel" style attribution glued inside a deal
    # line: the phrase goes, the price next to it stays.
    r"\b(?:from|on|via|in|at)\s+our\s+(?:official\s+)?(?:telegram|whatsapp|t\.me)\s*(?:channel|group|bot|link)?\b",
    rf"\b(?:join|subscribe|follow)\s+(?:our\s+)?(?:us\s+)?(?:channel|telegram|whatsapp\s+channel|group|now)\b{CTA_TAIL_PY}",
    r"\b(?:join|subscribe|follow)\s+(?:our\s+)?(?:us\s+)?(?:on|via)?\s*t\.me/\S+",
    rf"\b(?:for\s+more|more\s+)(?:loot|deal|update|offer)s?\b{CTA_TAIL_PY}",
    r"\bt\.me/\S+", r"\bwhatsapp\.com/(?:channel|invite)/\S+", r"\bwa\.me/\S+",
)


def deal_payload_signature(text: str) -> tuple[set[str], set[str], set[str]]:
    """(prices, discounts, code-like tokens) a line carries - what cleaning must
    never take away from a source post."""
    t = text or ""
    prices = {re.sub(r"[^\d]", "", p) for p in re.findall(r"₹\s*\d[\d,.]*", t)}
    percents = {m for m in re.findall(r"(\d{1,3})\s*(?:%|percent)", t, re.I)}
    codes = {c.upper() for c in re.findall(
        r"\b(?:[A-Z]{2,}\d[\dA-Z]*|\d+[A-Z]{2,}[\dA-Z]*)\b", t) if len(c) >= 5}
    return prices, percents, codes


def strip_inline_cta(line: str) -> str:
    """Remove CTA/promo from a deal line so channel boilerplate never appears
    next to the price. Global phrases are stripped anywhere on the line; a
    trailing CTA is trimmed to the sentence boundary. Product name, price, MRP,
    coupon ("use code X") and specs are left intact."""
    out = (line or "").replace("*", " ")
    for pattern in GLOBAL_CTA_PATTERNS_PY:
        out = re.sub(pattern, " ", out, flags=re.I)
    patterns = (
        rf"\b(?:buy|shop|order|grab|get|check|add\s+to\s+cart)\s+"
        rf"(?:it|now|fast|soon|today|yours?|this|the\s+deal|deal|fast\s+guys?|guys?)\b{CTA_TAIL_PY}",
        rf"\b(?:click|tap)\s+(?:here|link|below|on\s+(?:the\s+)?link|to\s+(?:buy|order|shop))\b{CTA_TAIL_PY}",
        rf"\b(?:don'?t|do\s+not|never)\s+miss\b{CTA_TAIL_PY}",
        rf"\bmiss\s+(?:it|this|out|the\s+deal)\b{CTA_TAIL_PY}",
        r"\b(?:hurry(?:\s*up)?|grab\s+(?:it|fast|now|your|this)|loot\s+fast|"
        r"deal\s+time[^\n]*|limited(?:\s*time)?\s+offer)\b[^.|\n]*$",
        # Social/channel CTAs (join/follow/share/notifications/t.me) are handled
        # by the bounded global patterns above so a following price is never eaten.
        rf"\b(?:link\s+in\s+bio|link\s+below|check\s+(?:link|bio|description|comments?|pinned))\b{CTA_TAIL_PY}",
    )
    for pattern in patterns:
        out = re.sub(pattern, " ", out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out)
    # A removed clause must not leave its own punctuation behind: stripping
    # "More offers" out of "More offers: Apply coupon X" used to publish a line
    # that OPENED with a colon, which reads exactly like bot damage.
    out = re.sub(r"^[\s|*•:,;\-\u2013\u2014~]+|[\s|*•:,;\-\u2013\u2014~]+$", "", out).strip()
    # Fidelity rule (user, round 10): a lost line is as bad as an added one. If a
    # CTA clause took the price, the discount or the coupon code with it, the
    # clause removal is undone for that line - the source wrote those words.
    before = deal_payload_signature(line)
    after = deal_payload_signature(out)
    if before != after and (before[0] - after[0] or before[1] - after[1] or before[2] - after[2]):
        return re.sub(r"\s{2,}", " ", (line or "").replace("*", " ")).strip()
    return out


# Loot channels open with a pure campaign banner ("🔥🔥 TOP DEAL OF THE DAY 🔥🔥",
# "⚡️ 11 PM FLASH SALE ⚡️") that is not about any product. Those lines are decoration
# and must not reach our channels - but the ONLY thing that makes them droppable is
# that the post also contains a real product line. A list post whose headline is the
# product must never lose it, so the vocabulary below is deliberately limited to
# generic hype words: one product/spec word in the line keeps the line.
BANNER_NOISE_WORDS = frozenset("""
top tops best hot mega super ultra dhamaka dhamal amazing awesome superb mind
blowing daily latest new today yesterday deal deals dealz offer offers loot loots
sale sales steal stealer alert alerts save savings price prices drop drops shocker
shocking free gift gifts bonus grab hurry limited time slot hours day nights night
of the a an for you your ours only off on in india indian
weekend special weekday flash live now just don miss
""".split())
TIME_OF_DAY_RE = re.compile(r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)", re.I)


def is_campaign_banner_line(line: str) -> bool:
    """True for a pure hype/banner line with no product, price or link in it.

    Brand/store names are deliberately NOT in the vocabulary: "Myntra Mega Sale" is
    a real store header (content), "TOP DEAL OF THE DAY" is hype (noise).
    """
    t = line or ""
    if not t.strip() or URL_RE.search(t):
        return False
    if re.search(r"[\u20b9$%]|\b(?:rs\.?|inr|mrp|discount|cod|off\s*[:=])", t, re.I):
        return False
    stripped = TIME_OF_DAY_RE.sub(" ", t)
    stripped = re.sub(r"\b(?:\d{1,2}(?:st|nd|rd|th)?|\d{1,2}\s*(?:am|pm))\b", " ", stripped, flags=re.I)
    words = [w for w in re.sub(r"[^\w\s]", " ", stripped, flags=re.U).split() if w]
    if not words:
        return True  # pure decoration (emoji / rules) - noise by any definition
    return all(w.lower() in BANNER_NOISE_WORDS for w in words)


# Another channel's BRANDING (name/signature/watermark) is not deal content and
# the user wants it gone, while everything the posting channel wrote ABOUT the
# deal (including its own hype header) must stay verbatim. Two safe signals:
#   * a promo verb / handle on the line ("Join ... for more loot", "@xyz",
#     "Edited by Admin", "Subscribe"), or
#   * an ALL-CAPS title line carrying a brand-suffix noun ("ZONE", "HUB",
#     "INDIA", "OFFICIAL", "TELEGRAM"...), which is how these signatures read.
# Both require that no word of the line describes a product/price/spec, so a
# real deal sentence can never be mistaken for branding. Generic hype words are
# deliberately NOT enough on their own: "🔥🔥 TOP DEAL OF THE DAY 🔥🔥" is fidelity.
BRANDING_LINE_RE = re.compile(
    r"(?i)\b(?:join|follow|subscribe|share|forward|turn\s+on)\b"
    r"|\b(?:telegram|whatsapp)\s*(?:channel|group)?\b|\b(?:channel|group)\s*(?:name|link)?\b"
    r"|@[A-Za-z][A-Za-z0-9_]{3,}|\b(?:edited|posted|powered|made|managed)\s+by\b"
    r"|\bfor\s+more\b|\bmore\s+(?:loots?|deals?|offers?|updates?|dhamaka)\b"
    r"|\b(?:stay|keep)\s+(?:tuned|updated|connected)\b")
# Only these count as "this line names a channel/brand" - plain hype words
# (deal/deals/offer/loot/sale/day) are deliberately absent, because a channel's
# own "🔥🔥 TOP DEAL OF THE DAY 🔥🔥" header is fidelity, not branding.
BRANDING_NOUNS = frozenset("""
zone hub india official world point adda team squad daily store shop mart bazaar
channel group telegram whatsapp admin edit edits powered managed updates update
""".split())
# Words that prove the line talks about a real product/price -> never branding.
# A line that OPENS with a signature marker names whoever made the post, which is
# the point of the line - the brand word itself is expected, so the vocabulary
# check does not apply to it.
SIGNATURE_PREFIX_RE = re.compile(
    r"(?i)^[^\w\n]*(?:powered|edited|posted|made|managed|written|curated|created|shared|sent)"
    r"\s+by\b")

BRANDING_DEAL_EVIDENCE_RE = re.compile(
    r"(?i)[\u20b9$]|\b(?:mrp|rs\.?|inr|cod|discount|size|color|colour|pack|pcs|pair)\b"
    r"|\d+\s*%|\b\d+\s*(?:off|days?|years?|months?|gb|tb|mah)\b")


BRANDING_WORDS = BRANDING_NOUNS | BANNER_NOISE_WORDS | frozenset(
    "more for with and our us on in the a an by at to of also join follow subscribe "
    "share forward turn notifications notification stay tuned connected edited "
    "posted powered made managed only now here this that new best".split())
HANDLE_RE = re.compile(r"@(?![A-Za-z]{1,3}\b)[A-Za-z][A-Za-z0-9_]{3,}")


def is_branding_line(line: str) -> bool:
    """True for another channel's name/signature/promo line, never for deal text."""
    t = (line or "").strip()
    if not t or URL_RE.search(t):
        return False
    if BRANDING_DEAL_EVIDENCE_RE.search(t):
        return False
    words = [w for w in re.sub(r"[^\w\s]", " ", t, flags=re.U).split() if w]
    if not words:
        return False
    lowered = [w.lower() for w in words]
    brand_nouns = [w for w in lowered if w in BRANDING_NOUNS]
    # One product/spec word anywhere means this is a deal line, not a signature.
    if not brand_nouns and not HANDLE_RE.search(t):
        return False
    if HANDLE_RE.search(t) or SIGNATURE_PREFIX_RE.match(t):
        return True                       # a bare "@handle" / "Powered by X" credit line
    if all(w in BRANDING_WORDS for w in lowered):
        return bool(BRANDING_LINE_RE.search(t)) or len(brand_nouns) >= 2
    return False                          # real content with a stray CTA


def strip_promo_lines(text: str) -> str:
    """Drop source promo/navigation lines from a Telegram/WhatsApp body, and
    strip a CTA fragment glued to the end of a real deal line (price/discount
    lines are kept as lines, only the channel junk on them is removed).

    Fidelity first: a channel's own campaign banner ("🔥🔥 TOP DEAL OF THE DAY 🔥🔥")
    is part of the source post and is KEPT by default. Only with
    STRIP_CAMPAIGN_BANNERS=true is it treated as decoration, and even then a
    banner that is the post's only text line stays, so cleaning can never leave a
    wall of bare links behind.
    """
    lines = (text or "").splitlines()
    banners = [is_campaign_banner_line(ln) for ln in lines] if STRIP_CAMPAIGN_BANNERS else []
    # A post is NOT allowed to become a wall of bare links: if every text line is
    # a banner (some channels put the product name nowhere else), the first one is
    # kept as the headline and only the extra banner lines go away.
    if banners:
        has_real_headline = any(ln.strip() and not URL_RE.search(ln) and not flag
                                for ln, flag in zip(lines, banners))
        first_banner = None if has_real_headline else next(
            (i for i, ln in enumerate(lines) if ln.strip() and banners[i]), None)
    else:
        first_banner = None
    branding = [is_branding_line(ln) for ln in lines]
    # Same guard as the banner rule: if the ONLY text lines of the post are
    # branding, the first one stays - a wall of bare links is never an
    # improvement, and the product/price line is what fidelity is about.
    if any(branding) and not any(ln.strip() and not URL_RE.search(ln) and not flag
                                for ln, flag in zip(lines, branding)):
        branding[next(i for i, flag in enumerate(branding) if flag)] = False
    kept = []
    for index, ln in enumerate(lines):
        if is_promo_noise_line(ln):
            continue
        # Branding of another channel always goes (fidelity is about the DEAL,
        # not about whoever wants a subscriber for it).
        if branding[index]:
            continue
        if banners and banners[index] and index != first_banner:
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
    "h", "hh", "ht", "hht", "htt", "httt", "hhtt", "http", "https", "https",
    "htps", "ttp", "ttps", "tps",
    "tps://", "tp://", "s://", "://", "uy",
}

# USER RULE (2026-09-03): "h", "ht", "hht" price pakkana kuda add avvakudadu.
# A stand-alone half-URL token ANYWHERE on a line ("Deal Price: ₹499 h",
# "₹1,299 hht", "₹350 👉h") is the residue of a stripped link, never content.
# The token must be a whole word on its own: "42H playtime", "H&M", "hot" and a
# real "https://..." link are untouched (the lookarounds refuse letters, digits,
# ':' '/' '.' '&' on either side).
_INLINE_ORPHAN_FRAGMENT_RE = re.compile(
    r"(?<![\w:/.&-])(?:h{1,2}t{1,3}p{0,2}s{0,2}|tt?ps?|h{1,2})(?![\w:/.&-])", re.I)


def strip_inline_orphan_fragments(text: str) -> str:
    """Remove bare half-URL tokens (h/ht/htt/hht/https…) glued near prices or
    dangling anywhere, while every real URL stays byte-identical."""
    if not text:
        return text
    real = list(dict.fromkeys(URL_RE.findall(text)))
    masked = text
    for index, url in enumerate(real):
        masked = masked.replace(url, f"\x01R{index}\x02")
    masked = _INLINE_ORPHAN_FRAGMENT_RE.sub(" ", masked)
    # Emptied wrappers/arrows the token was sitting in: "(h)" -> "()", "👉h" -> "👉".
    masked = re.sub(r"[(\[{]\s*[)\]}]", " ", masked)
    masked = re.sub(r"[ \t]{2,}", " ", masked)
    masked = re.sub(r"[ \t]+$", "", masked, flags=re.M)
    for index, url in enumerate(real):
        masked = masked.replace(f"\x01R{index}\x02", url)
    return masked


def remove_orphan_url_fragment_lines(text: str) -> str:
    kept = []
    for line in (text or "").splitlines():
        # Ignore emojis/arrows/zero-width/formatting around tiny broken pieces,
        # e.g. `👉h`, `htt`, or `tps://` left by malformed Telegram entities.
        core = re.sub(r"[^A-Za-z:/]", "", line).lower()
        if core in ORPHAN_URL_FRAGMENTS:
            # A line whose letters are ONLY the fragment but that still carries
            # a price/number ("₹799 ttp") must keep the price: strip the token,
            # never the line (dropping the price was worse than the residue).
            if re.search(r"[\d\u20b9]", line):
                kept.append(strip_inline_orphan_fragments(line))
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
    # Markdown emphasis debris (++, **, __ and lone */_) left around the
    # collapsed links is source formatting, never deal content. Strip it in
    # the TEXT parts only (URLs are masked) so legitimate '+' query chars
    # inside Amazon URLs survive untouched.
    def _drop_emphasis_part(part: str) -> str:
        # A real URL is only ever trimmed at its edges: "https://t.me/addlist/"
        # links and merchant paths legitimately contain underscores, and an
        # interior "_" used to be deleted (it silently corrupted our own folder
        # link into a dead join URL). Trailing emphasis glued to a URL is debris.
        if re.match(r"(?i)https?://", part):
            return re.sub(r"^[*_+~]+|[*_+~]+$", "", part)
        # Text parts: runs of 2+ markers (** __ ++ ~~) plus lone emphasis
        # markers are markdown debris. "*" never carries meaning in a plain-text
        # deal post (and Telegram does not render it), so every one of them
        # goes. "_" inside a word is kept: coupon codes ("SAVE_200") and SKUs
        # are real content.
        part = re.sub(r"[*_+~]{2,}", "", part)
        part = part.replace("*", "")
        part = re.sub(r"(?<![\w])_(?=\w)", "", part)
        return re.sub(r"(?<=\w)_(?![\w])", "", part)
    out = "\n".join(
        re.sub(r"[ \t]{2,}", " ", " ".join(
            _drop_emphasis_part(p) for p in URL_KEEP_RE.split(line)
        )).strip()
        for line in out.splitlines()
    )
    # Collapse the SAME product link stacked several times in one line (nested
    # copies) down to one, keeping a short text prefix if there is one.
    # A URL wrapped in parentheses is cleaner as the bare link (the emphasis
    # pass above splits on the brackets, leaving "( url )" spacing behind).
    # The spaces are REQUIRED: a broken forward can glue "url1(url2(url3" together
    # and those inner parens are the only thing separating the duplicates - the
    # per-line dedup below needs them, so they must not be touched here.
    out = re.sub(r"\(\s+(https?://[^\s()]+)\s+\)", r"\1", out, flags=re.I)
    lines = []
    for line in out.splitlines():
        # Parentheses are real deal text - "Price ₹260 (75% OFF)" must survive
        # untouched. Only a line that is itself a URL wrapped in parens (an
        # artefact of forwarded/bot-posted entities) gets its wrapper removed.
        bare = line.strip()
        if URL_RE.search(bare):
            # Only the outer wrapper comes off a URL line: the inner parentheses
            # of a glued forward fragment ("url1(url2(url3))") are the separators
            # the dedup below counts on, so they must survive untouched.
            bare = bare.strip("()").strip()
        else:
            bare = fix_unbalanced_parens(bare)
        stripped = bare
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
                prefix = re.sub(r"[()]{2,}", " ", prefix).strip(" \t|()")
                prefix = fix_unbalanced_parens(prefix)
                stripped = (((prefix + "\n") if prefix else "") + "\n".join(canon)).strip()
        lines.append(stripped)
    return "\n".join(lines)


# Half-stripped promo links and broken entity fragments are the main source of
# "unwanted words" in a post: removing a `t.me/...` CTA used to leave
# `.com/channel/0029`, `.me/someotherchannel` or `ps://broken` behind. These
# patterns only ever match URL *residue* (real links are masked out first), so a
# generated affiliate URL can never be damaged by this pass.
URL_RESIDUE_RES = (
    r"(?i)\b(?:https?|httpsx|ht|htt|httpx|ftp|tps|ttp|tp|ps|hs|sp)[:/ ]{0,2}[/\\]{2,}\S*",
    r"(?i)[A-Za-z0-9_-]*\.(?:me|com|in|net|org|io|co|html?)\b[/\\]\S*",
    r"(?i)\b(?:t|telegram|whatsapp|wa)\s*[:.]\s*(?:me|com|dog)\b\S*",
)


def strip_url_residue(text: str) -> str:
    """Drop broken/half-stripped URL fragments while leaving real links intact."""
    if not text:
        return text
    real = list(dict.fromkeys(clean_url(u) for u in URL_RE.findall(text)))
    masked = text
    for index, url in enumerate(real):
        masked = masked.replace(url, f"\x01R{index}\x02")
    for pattern in URL_RESIDUE_RES:
        masked = re.sub(pattern, " ", masked)
    masked = re.sub(r"[ \t]{2,}", " ", masked)
    # A line that lost everything except bullets/emoji/arrows is a dead slot.
    lines = [
        line for line in masked.splitlines()
        if re.search(r"[A-Za-z0-9\u0900-\u097F\u0C00-\u0C7F]", line)
        or URL_RE.search(line)
        or not re.fullmatch(r"[\s🔗👉➡️🔎🔍•▪️\-–—_|:*+]+", line or "")
    ]
    masked = "\n".join(lines)
    for index, url in enumerate(real):
        masked = masked.replace(f"\x01R{index}\x02", url)
    return re.sub(r"\n{3,}", "\n\n", masked).strip()


# ---------------------------------------------------------------------------
# Junk-token killers. Sources are forwarded bot output: masked shortener tails
# (".../tG7oChgiQuTgS25b") get glued to the price or left on a line of their
# own, and broken Telegram entities leave "[url](url)" markdown debris. The
# previous rules were length-capped at 12-14 characters, so a 17-character
# fragment sailed straight into the published post. These three helpers are the
# single source of truth: clean_source_text, tidy_post and the final outbound
# guard all share them, so a token cannot survive by dodging one of the passes.

# An Amazon/Flipkart coupon code: all caps, letters plus optional digits, 3-20 chars.
_COUPON_CODE_RE = re.compile(r"[A-Z][A-Z0-9_-]{2,19}")   # SAVE_200 is as spendable as SAVE200
# The gap is [ \t]* and never \s*: a token on the NEXT line is not glued to this
# price, and rewriting the pair must not swallow the newline that keeps the source's
# layout (an earlier draft joined "MRP: ₹ 270" and "Discount: 26%" into one line).
# "@2764" and "Rs.449" are prices exactly like "\u20b9449" - the live channels
# write all three - so link debris glued to ANY of them is cut the same way
# ("LG 24 Inches @2764ldkf" was published with the "ldkf" scrap attached).
_PRICE_WITH_TAIL = re.compile(r"((?:\u20b9\s*|@\s*|\bRs\.?\s*)[\d,]+)([ \t]*)(\S+)", re.I)


def _keep_code_as_is(price: str, gap: str, tail: str) -> str:
    """Rebuild one "<price><token>" pair, judging the token by its SHAPE.

    The three shapes a source post actually contains, in order of how much they
    matter: an all-caps code (PEOPLE200, HFJF) is money the reader can spend, so it
    is UN-GLUED with a space and kept - deleting it was losing the best part of the
    deal; a real unit ("500ml", "2pcs") spaced after the price is legitimate text and
    stays; a mixed-case run ("tG7oChgiQuTgS25b") is what a broken shortener leaves
    behind and must go, because "\u20b9260://bitli.in/x" is a dead link, not a discount.
    A glued token with a letter in it is never real text - the price ended and the
    paste ran on - so it is cut, exactly as before this code existed.
    """
    if not tail or tail[0] in "\u279c\u27a1\u2192\u2022\u00b7#":
        return f"{price}{gap}{tail}"
    core = tail.rstrip(").,;:!?\u2026")
    trail = tail[len(core):]
    if not core:
        return f"{price}{gap}{tail}"
    # "@2pm" / "@11am" is a TIME the source wrote (sale start), not a price with
    # junk on it - the glued cut below must never turn "Sale @2pm" into "Sale @2".
    if price.lstrip().startswith("@") and core.lower() in ("am", "pm", "a.m", "p.m", "a.m.", "p.m."):
        return f"{price}{gap}{tail}"
    if not gap:
        # USER RULE (round 13): a token glued straight onto a price is UNWANTED text,
        # whatever it looks like - the live channel's price line arrives as
        # "₹85h" / "₹ 199HFJF" / "₹85jsjd" and the reader needs the PRICE, not the
        # scrap. It is cut, and nothing of ours is written in its place. A code the
        # source really wants the reader to use is written separately ("Use code
        # PEOPLE200", "₹1,099 SAVE200"), and that spacing is what keeps it: this walk
        # only ever touches what is fused to the digits.
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", core) and re.search(r"[A-Za-z]", core):
            return f"{price}{trail}".rstrip()
        return f"{price}{gap}{tail}"
    if core.lower() in _PRICE_UNITS or not re.search(r"\d", core):
        return f"{price} {core}{trail}"                  # words and units stay put
    if _COUPON_CODE_RE.fullmatch(core) and re.search(r"\d", core):
        return f"{price} {core}{trail}"                    # spaced code: keep verbatim
    # A SPACED token is deleted only when it is machine-shaped - mixed case, long, and
    # holding a digit, which is what a torn shortener looks like when the paste broke
    # ("₹260 tG7oChgiQuTgS25b"). No human writes a coupon or a quantity that way, so
    # nothing of the source's own words is ever lost here.
    if (len(core) >= 8 and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", core)
            and re.search(r"[a-z]", core) and re.search(r"[A-Z]", core)
            and re.search(r"\d", core)):
        return f"{price}{trail}".rstrip()
    return f"{price}{gap}{tail}"


_PRICE_UNITS = frozenset(
    "pcs pack packs pair pairs kg gm g ml ltr ltrs l litre litres cm mm mah gb tb "
    "w v inch inches ft in".split())


def strip_price_junk(text: str) -> str:
    """Un-glue a coupon code from a \u20b9 price and cut real link debris off it.

    Two different things look the same in a source post: "\u20b91,099PEOPLE200" is a
    price plus the code that saves the reader \u20b9200, while "\u20b9260tG7oChgiQuTgS25b"
    is a half-torn shortener URL. The first must survive (deleting it was losing the
    deal's best part), the second must go. The shape tells them apart, so this walks
    every "<price><glued token>" pair once and decides per token - never per regex,
    which is how the old three-pass version ate codes along with the debris.

    Real links are masked first so a glued link keeps its protocol: cutting "https"
    out of "\u20b9260https://bitli.in/x" used to leave "\u20b9260://bitli.in/x".
    """
    if not text:
        return text
    urls = list(dict.fromkeys(URL_RE.findall(text)))
    for index, url in enumerate(urls):
        text = text.replace(url, f"\x02P{index}\x03")
    text = _PRICE_WITH_TAIL.sub(lambda m: _keep_code_as_is(m.group(1), m.group(2), m.group(3)), text)
    for index, url in enumerate(urls):
        text = text.replace(f"\x02P{index}\x03", url)
    # Keep a glued link apart from the word/price in front of it.
    out = re.sub(r'([\w\u20b9)\]>"\'])(?=https?://)', r"\1 ", text)
    return re.sub(r"[ \t]{2,}", " ", out)


def strip_link_fragment_tokens(text: str) -> str:
    """Drop masked-shortener fragments that stand alone inside a line.

    A 12+ character run that mixes lower case, upper case and digits is link
    residue (a coupon is never random-case like that, and coupon/referral lines
    are skipped entirely), so this kills the "edo edo random" words no source
    actually wrote.
    """
    if not text:
        return text
    urls = list(dict.fromkeys(URL_RE.findall(text)))
    masked = text
    for index, url in enumerate(urls):
        masked = masked.replace(url, f"\x01K{index}\x02")
    lines = []
    for line in masked.splitlines():
        if not re.search(r"(?i)code|coupon|kupon|voucher|referral|refer\b|promo|pin\b|deal\s*id", line):
            line = re.sub(
                r"(?<![A-Za-z0-9_-])"
                r"(?=[A-Za-z0-9_-]{12,64}(?![A-Za-z0-9_-]))"
                r"(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)"
                r"[A-Za-z0-9_-]{12,64}(?![A-Za-z0-9_-])",
                "", line)
            line = re.sub(r"[ \t]{2,}", " ", line).strip()
            # Only an orphan bullet left behind by the sweep (":" is preserved so
            # "Use code:" labels survive, and no bracket can be eaten). A trailing
            # variation selector is emoji, not residue: "\u26a1\ufe0f\u26a1\ufe0f 11 PM FLASH SALE
            # \u26a1\ufe0f\u26a1\ufe0f" must come out exactly as the source wrote it, so only an
            # ORPHAN \ufe0f (nothing in front of it but space/start) is dropped.
            line = re.sub(r"[\s\u279c\u27a1\u2192\U0001f517\U0001f449\u2022\u2013\u2014-]+$", "", line).strip()
            line = re.sub(r"(?<=\s)\ufe0f+(?=\s*$)", "", line)
            line = re.sub(r"^\ufe0f+", "", line)
        lines.append(line)
    for index, url in enumerate(urls):
        lines = [line.replace(f"\x01K{index}\x02", url) for line in lines]
    return "\n".join(lines)


def fix_unbalanced_parens(line: str) -> str:
    """Drop empty/unmatched parentheses, never a real "(75% OFF)" pair."""
    if not line:
        return line
    # Stripping a promo URL out of "Price ₹21,999 (from our channel ...)" can
    # leave an empty bracket pair: that is cleanup residue, not deal content.
    stripped = re.sub(r"\(\s*\)", " ", line)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped).strip(" \t|")
    if stripped != line:
        line = stripped
    if line.count("(") == line.count(")"):
        return line
    # v17.8: only a bracket that STANDS ALONE is cleanup residue. The old loop
    # deleted characters until the counts matched, which ate the ")" of a
    # numbered list ("1) boAt Airdopes …") and the closing bracket of
    # "(78% off)" - real source text destroyed on every list post. When the
    # leftovers are not standalone the line is published as the source wrote it.
    opens, closes = line.count("("), line.count(")")
    char = "(" if opens > closes else ")"
    excess = abs(opens - closes)
    out: list[str] = []
    removed = 0
    for index, char_here in enumerate(line):
        if char_here == char and removed < excess:
            alone_before = index == 0 or line[index - 1].isspace()
            alone_after = index == len(line) - 1 or line[index + 1].isspace()
            if alone_before and alone_after:
                removed += 1
                continue
        out.append(char_here)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def _collapse_markdown_link(match: re.Match) -> str:
    """`[label](url)` printed as plain text is debris; keep what is content.

    Posts go out with no parse_mode, so brackets are shown literally. When the
    label is itself the URL (or just punctuation/empty) the whole group is one
    duplicated link and the bare URL is all that must remain. A label carrying
    real words is deal content, so it is kept and the link follows it.
    """
    label = (match.group(1) or "").strip()
    url = (match.group(2) or "").strip()
    if not label or URL_RE.fullmatch(label) or not re.search(r"[A-Za-z0-9]", label):
        return url
    return f"{label} {url}"


def sanitize_outbound_text(text: str) -> str:
    """Final gate for the exact string handed to Telegram.

    Everything above cleans the SOURCE text; the affiliate substitution and the
    shortener pass run afterwards and can still leave "[url](url)" markup, a
    glued token or an empty bullet line. This guard is idempotent, touches only
    formatting, and never rewrites a verified link or a price the source had.
    """
    if not text:
        return text
    out = text
    for _ in range(4):
        previous = out
        out = re.sub(r"\[\s*([^\]\[]*?)\s*\]\s*\(\s*(https?://[^\s)]+?)\s*\)",
                     _collapse_markdown_link, out, flags=re.I)
        if out == previous:
            break
    out = strip_price_junk(out)
    out = strip_link_fragment_tokens(out)
    # USER RULE: "h"/"ht"/"hht" pieces next to a price must never leave the bot.
    out = strip_inline_orphan_fragments(out)
    out = re.sub(r"\(\s*\)", " ", out)
    # Any bracket left after the collapse is a fragment of broken entity markup.
    out = re.sub(r"[\[\]]", "", out)
    out = "\n".join(
        # A line carrying a link is left exactly as it is: "( )" may be part of a
        # real merchant path and the parentheses of a URL must never be edited.
        line if URL_RE.search(line) else fix_unbalanced_parens(line)
        for line in out.splitlines()
    )
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def clean_source_text(text: str) -> str:
    text = (text or "").replace("\x00", "")
    text = normalize_nested_link_markup(text)
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    text = CK_FOOTER_RE.sub("\n", text)
    text = SOURCE_NOISE_LINE_RE.sub("", text)
    text = remove_orphan_url_fragment_lines(text)
    # USER RULE: "h"/"ht"/"hht" residue next to a price is unwanted text.
    text = strip_inline_orphan_fragments(text)
    # Remove URL-tail garbage glued to a price ("₹122oya", "₹260tG7oChgiQuTgS25b")
    # and any stand-alone shortener fragment. Shared with tidy_post and the
    # outbound guard so the same junk can never be missed by one pass only.
    text = strip_price_junk(text)
    text = strip_link_fragment_tokens(text)
    text = ORPHAN_MARKDOWN_LINE_RE.sub("", text)
    text = re.sub(r"(?im)^\s*[^\w\n]*(?:sent\s+via|via\s+\w+\s+admin).*$", "", text)
    text = re.sub(r"(?im)^\s*(?:follow|join|subscribe).*@\w+.*$", "", text)
    # Drop source promo/navigation lines (join/follow/share/notify, t.me
    # self-promo, deal-time, handles, emoji-only) that carry no deal content.
    text = strip_promo_lines(text)
    # Then remove anything the CTA stripping left dangling (`.com/channel/0029`,
    # `ps://`, a bare `https://`) so no URL residue ever reaches a post.
    text = strip_url_residue(text)
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
    result = del_surrogate("".join(out))
    # SELF-HEALING GUARD (user bug, 2026-09-03): when entity offsets do not line
    # up with this text (a parse-mode copy inserted "**" marks and shifted every
    # offset), the slice cuts THROUGH a URL and publishes scrap like "BUJYEi"
    # while the affiliate link is lost. If any monetized link went missing even
    # though its source URL is literally present in the text, the entity path
    # cannot be trusted - rebuild by literal replacement, which needs no offsets.
    for source_url, aff in mapping.items():
        if aff and aff not in result and source_url in clean_url(text):
            literal = replace_literal(surrogate_text)
            return del_surrogate(literal)
    return result


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
    # A protocol-less link stump ("://bitli.in/x" glued to a price after its
    # scheme was torn off) is dead residue - the working link was already
    # masked above, so anything still shaped like this cannot be real.
    masked = re.sub(r":?//[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+(?:/[A-Za-z0-9._~%/-]*)?", " ", masked)
    masked = re.sub(r"\bh(?=[A-Z][a-z])", "", masked)
    masked = strip_price_junk(masked)
    # Markdown emphasis debris only. A '_' BETWEEN word characters is content -
    # a coupon code ("Use code SAVE_200") or an SKU - and the blanket removal
    # used to silently rewrite it into a code that does not work.
    masked = masked.replace("*", "")
    masked = re.sub(r"__([^_\n]+?)__", r"\1", masked)
    masked = re.sub(r"(?<![\w])_+(?=\w)|(?<=\w)_+(?![\w])", "", masked)
    # Square brackets are markdown debris in a plain-text channel; parentheses
    # around "(75% OFF)", "(Pack of 2)" or "(Code: X)" are the source's own
    # deal text and must be printed exactly as written. Only an unmatched paren
    # left behind by another cleanup pass is removed.
    masked = re.sub(r"[\[\]]", " ", masked)
    masked = re.sub(r"[ \t]+", " ", masked)
    masked = re.sub(r"(?m)^\s*[:>-]+\s*$", "", masked)
    for index, url in enumerate(protected):
        masked = masked.replace(f"\x01URL{index}\x02", url)
    # Final anti-residue sweep on our own output too: any fragment that is not a
    # complete link is dropped, every generated URL is masked inside the helper.
    masked = strip_url_residue(masked)
    # A link glued to the word/price in front of it ("₹260https://…") prints as
    # one unreadable token, and the fragment then looks like junk next to the
    # price. Keep the words apart. Query-nested links stay intact because the
    # separator has to be a word char, never "=", "&", "?" or "/".
    masked = re.sub(r'([\w\u20b9)\]>"\'])(?=https?://)', r"\1 ", masked)

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


# A label line that carried a link which later collapsed (two source links
# converting to the SAME product) or was stripped as promo is a dangling slot:
# `Link ` / `Buy Now` with nothing after it. Those are the most visible form of
# "unwanted words" in a finished post, so they are removed explicitly.
DANGLING_CTA_WORDS = {
    "", "link", "links", "buy", "shop", "click", "tap", "order", "grab", "here",
    "buylink", "buyhere", "clickhere", "shopnow", "buynow", "getit", "checkout",
    "orderhere", "view", "viewdeal", "deal", "offer", "purchase", "product", "url",
    "affiliate", "affiliatepartner", "ad", "ads", "promo", "sponsored",
}


def remove_dangling_cta_lines(text: str) -> str:
    """Drop pure CTA/link-label lines that ended up with no link and no price."""
    kept = []
    for line in (text or "").splitlines():
        if URL_RE.search(line) or re.search(r"[\u20b9$]|%\s*(?:off|discount)|\d", line, re.I):
            kept.append(line)
            continue
        core = (line or "").strip()
        if core and len(core) <= 28:
            probe = re.sub(r"[^A-Za-z]", "", core).lower()
            if probe in DANGLING_CTA_WORDS:
                continue
        kept.append(line)
    return "\n".join(kept)


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

    masked = remove_dangling_cta_lines(masked)
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
            # Removing a broken fragment can leave a bare CTA label behind
            # ("Link", "Buy Now"); without a link or price it is junk.
            if (len(stripped) <= 28 and not re.search(r"[\u20b9$%\d]", stripped)
                    and re.sub(r"[^A-Za-z]", "", stripped).lower() in DANGLING_CTA_WORDS):
                continue
        kept.append(stripped)
    result = "\n".join(kept)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _product_label(line: str) -> str:
    """Return the real product text of a list line, or "" when there is none.

    Decoration only (bullets/arrows/emoji/punctuation) and pure links are not a
    label: a list of variant links under one headline has ONE label, not four,
    and pretending otherwise deletes the headline (product name + price) from
    the published post.
    """
    text = re.sub(r"^[^\w\u20b9]+", "", (line or "").strip())
    text = re.sub(r"[^\w\u20b9%)]+$", "", text).strip(" \t:|-+*~")
    text = re.sub(r"^[\s\u279c\u27a1\u2192\u2193\U0001f449\U0001f517\u2022\u25aa\ufe0f:|*-]+", "", text).strip()
    if len(text) < 4 or not re.search(r"[A-Za-z]{2}", text):
        return ""
    # A line that still carries a link is a link line, not a product label (the
    # label of a same-line pair is passed in already cut before the URL).
    if URL_RE.search(text) or "http" in text.lower():
        return ""
    # A pure campaign banner is source decoration. It stays in the post (see
    # STRIP_CAMPAIGN_BANNERS), but it must never be consumed as a product label,
    # or the real product line below it loses its pairing.
    if is_campaign_banner_line(text):
        return ""
    return text


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
            # A label has to carry the product's own words. Leading decoration
            # (bullets, arrows, emoji, dashes) is stripped by CLASS, not by an
            # emoji list: "\u279c https://.." used to yield the label "\u279c", which
            # turned a bullet into a "product" and then the real title line
            # vanished from the post (name and price gone, bare links only).
            label = _product_label(prefix)
            label_index = index
            if not label:
                for previous in range(index - 1, -1, -1):
                    if previous in used_label_lines:
                        continue
                    candidate = _product_label(lines[previous])
                    if candidate:
                        label, label_index = candidate, previous
                        break
            if label:
                label = tidy_post(label)
                if label:
                    pairs.append((label, affiliate))
                    used_label_lines.add(label_index)
                    used_affiliates.add(affiliate)

    # Only a genuine multi-product list may be rebuilt this way. (Fewer than 3
    # real labels means one product with several variant links: keep the source
    # layout, which already carries the name, the price and every link.)
    if len(pairs) < 3:
        return None
    # Rebuild in SOURCE ORDER instead of emitting only the pairs: the old pair
    # dump silently dropped every line that was not a label (a second header
    # line, MRP / shipping notes), which breaks "publish what the source wrote".
    # Every product line and every link stays, each link still sits under its
    # own label, and no link is ever reordered or pooled.
    output: list[str] = []
    emitted: set[str] = set()
    for line in lines:
        raw_urls = URL_RE.findall(line)
        if not raw_urls:
            # Any text line prints where it stood in the source - a label keeps
            # its place above its link, a second header or an MRP note keeps its
            # own place too.
            kept = tidy_post(line)
            if kept:
                output.append(kept)
            continue
        head = tidy_post(_product_label(line.split(raw_urls[0], 1)[0]))
        if head:
            output.append(head)
        for raw_url in raw_urls:
            affiliate = mapping.get(clean_url(raw_url))
            if affiliate:
                if affiliate in emitted:
                    continue
                emitted.add(affiliate)
                output.append(affiliate)
            else:
                # Never swallow a link here: an unmapped source URL is handled by
                # the provenance / pass-through / repair passes in render_job.
                output.append(raw_url)
    return "\n".join(output).strip()


# A price the way these sources actually type it. "₹258" is only one of the
# spellings: "at 258", "@167", "Rs 180", "220/-" are the same money, and the old
# ₹-only test is why a four-product list ("Methi Dana 500 gms at 180") was
# published as four naked labels followed by four naked links - the "neatga
# ravali" complaint.
PRICE_LABEL_RE = re.compile(
    r"(?:₹|\brs\.?|\binr)\s*[\d,]{2,}"
    r"|(?:\bat|@)\s*[\d,]{2,}"
    r"|\b[\d,]{2,}\s*/-",
    re.I,
)


def format_clustered_product_list(text: str) -> str:
    """Pair 3+ product/price labels with a trailing block of generated URLs.

    The source writes the list as "name + price" blocks (often with a note line
    such as "Buy Max Quantity") and then dumps every link at the bottom. Read
    that way it is unreadable: the reader cannot tell which link is which deal.
    Here each block keeps its own note lines and gets its own link directly
    under it, in the source's own order. Nothing is invented, nothing is
    dropped, nothing is reordered.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    url_positions = [i for i, line in enumerate(lines) if URL_RE.fullmatch(line)]
    if len(url_positions) < 3:
        return text
    first_url = url_positions[0]
    # Only fix posts whose generated URLs are all grouped at the bottom.
    if any(not URL_RE.fullmatch(line) for line in lines[first_url:]):
        return text
    head = lines[:first_url]
    # Split the head into blocks: a new block starts on every price-bearing
    # label; anything after it (a note, an MRP line) belongs to that block.
    preamble: list[str] = []
    blocks: list[list[str]] = []
    for line in head:
        if PRICE_LABEL_RE.search(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            preamble.append(line)
    if len(blocks) < 3:
        return text
    urls = [lines[i] for i in url_positions]
    pair_count = min(len(blocks), len(urls))
    output = list(preamble)
    for index in range(pair_count):
        if output:
            output.append("")
        output.extend(blocks[index])
        output.append(urls[index])
    # A list with more links than labels (variant links) keeps the extras rather
    # than losing them: a dropped link is a dropped deal.
    for extra in urls[pair_count:]:
        output.append(extra)
    for leftover in blocks[pair_count:]:
        output.append("")
        output.extend(leftover)
    return "\n".join(output).strip()


# ---------------------------------------------------------------------------
# Affiliate-programme safe copy (the shopping channel)
# ---------------------------------------------------------------------------
# USER RULE (2026-09-05): t.me/smartbuyhub11 is the channel submitted for Amazon
# Associates approval. Amazon's review rejects "loot", urgency, buy-max-quantity
# instructions and discount claims it cannot verify, so that channel receives the
# SAME deal with a plain body: product name, price, link. Nothing is invented -
# every word and every number still comes from the source post.
_SAFE_DROP_LINE_RE = re.compile(
    r"(?i)^\s*(?:\**\s*)?(?:"
    r"buy\s+max(?:imum)?\s+quantity|max\s+quantity|buy\s+max|order\s+fast|"
    # A banner line that is nothing BUT hype words ("MEGA LOOT", "🔥 SUPER LOOT 🔥",
    # "DEAL OF THE DAY"). It carries no product and no price, so dropping the whole
    # line loses nothing - and leaving it in is the single word most likely to fail
    # the review. Emoji/punctuation around it are stripped by the caller.
    r"(?:mega|super|big|huge|best|top|hot)?\s*loot\s*(?:zone|deal|alert|offer)?s?|"
    r"hurry(?:\s*up)?|limited\s+stock|stock\s+limited|fast\s+selling|"
    r"grab\s+(?:it\s+)?(?:now|fast)|buy\s+(?:it\s+)?now|shop\s+now|"
    r"deal\s+of\s+the\s+day|price\s+may\s+change|#\w+"
    r")\s*[!.:]*\s*$"
)
# A banner word in front of the product name ("Loot : X", "GRAB : X", and just
# as often "Grab ₹298 (X)" with no colon at all). It is source decoration in
# both the programme-safe rewrite and the duplicate signature, so the separator
# is optional - otherwise the SAME deal signs differently depending on whether
# that channel typed a colon.
_SAFE_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:🔥|⚡|🚨|💥|🏃|👑|💰|🔔|✅|❗|‼️|👉|➡️|•|\*|-|~)*\s*"
    r"(?:super\s*loot|mega\s*loot|price\s*drop|lowest\s*price|big\s*deal|"
    r"hot\s*deal|best\s*deal|loot|grab|deal|steal|offer|alert)"
    r"(?:\s*[:\-–]\s*|\s+(?=[₹@]|\d))"
)
_SAFE_CLAIM_RE = re.compile(
    r"(?i)\b(?:#?\s*(?:uk|india|world)?\s*no\.?\s*1\s+brand|cheapest\s+ever|"
    r"lowest\s+ever|all\s+time\s+low|biggest\s+sale|loot\s+price|"
    r"steal\s+deal|must\s+buy)\b"
)
# Our own promo, in every shape it can reach a rendered post: the folder link,
# the main-channel links, and the "All Loot Channels" caption that sits with them.
_SAFE_OWN_PROMO_LINKS = (OUR_FOLDER_LINK, *OUR_MAIN_CHANNEL_LINKS, "t.me/")
_SAFE_OWN_PROMO_TEXT_RE = re.compile(
    r"(?i)\ball\s+loot\s+channels\b|\bloot\s+zone\b|\bsecret\s+loot\b|"
    r"\bjoin\b.*\bchannel\b|\bone\s+tap\b")
_EMOJI_RE = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002190-\U000021ff" "\U00002600-\U000027bf"
    "\U00002b00-\U00002bff" "\U0000fe0f" "\U00002122" "\U000024c2" "]+"
)


def affiliate_safe_text(text: str) -> str:
    """The compliance-safe copy of a rendered deal: name, price, our link.

    Used ONLY for the channel that is under Amazon Associates review. It removes
    the promotional scaffolding a marketplace programme objects to (loot/hurry
    banners, "buy max quantity", unverifiable superlatives, emoji shouting) and
    keeps the product line, the price the source printed and the link.
    """
    out_lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            continue
        # Our own family/channel promo must never reach the reviewer: a card
        # offer has the folder link appended into the stored copy, and a
        # reviewer who taps it lands in "Loot Zone"/"Secret Loot" - exactly the
        # wording this channel exists to avoid. It is OUR line, not the
        # source's, so removing it loses no deal content.
        if any(promo in line for promo in _SAFE_OWN_PROMO_LINKS):
            continue
        if _SAFE_OWN_PROMO_TEXT_RE.search(line):
            continue
        if URL_RE.fullmatch(line):
            out_lines.append(line)
            continue
        # Emoji come off BEFORE the banner test: "🔥 MEGA LOOT 🔥" is the same
        # pure-hype line as "MEGA LOOT", and testing the raw line let the
        # decorated version straight through.
        line = _EMOJI_RE.sub(" ", line).strip()
        if not line:
            continue
        if _SAFE_DROP_LINE_RE.match(line):
            continue
        line = _SAFE_PREFIX_RE.sub("", line)
        line = _SAFE_CLAIM_RE.sub(" ", line)
        line = _EMOJI_RE.sub(" ", line)
        line = re.sub(r"(?i)\b(?:buy\s+max(?:imum)?\s+quantity|hurry(?:\s*up)?|"
                      r"limited\s+stock|grab\s+fast|buy\s+now|shop\s+now)\b[!.,]*", " ", line)
        # USER RULE: "just name petti price pettu anthe". A discount percentage
        # and a struck-through MRP are exactly the claims a marketplace reviewer
        # asks us to prove, and the source's own price is enough on its own.
        line = re.sub(r"(?i)\(?\s*(?:flat\s*|upto\s*|up\s*to\s*|save\s*)?\d{1,3}\s*%\s*"
                      r"(?:off|discount)?\s*\)?", " ", line)
        line = re.sub(r"(?i)^\s*(?:reg(?:ular)?|mrp|m\.r\.p\.?|list\s*price|was)\b\s*"
                      r"[:.\-]?\s*(?:rs\.?|₹|inr)?\s*[\d,]+\s*/?-?\s*$", " ", line)
        line = re.sub(r"[ \t]{2,}", " ", line).strip(" \t|-–—:•*~")
        if not line:
            continue
        # A line with no product words left (a lone "!!!" or a bare hash tag) is
        # scaffolding, not content.
        if not re.search(r"[A-Za-z0-9₹]", line):
            continue
        out_lines.append(line)
    body = "\n".join(out_lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if SAFE_STRICT_REBUILD:
        body = _strict_review_copy(body)
    return body


# ---------------------------------------------------------------------------
# STRICT review copy (EarnKaro / Amazon channel under review)
# ---------------------------------------------------------------------------
# USER RULE (2026-09-05): the new channel is submitted for review, so it must
# follow the programme rules 100% - the other channels stay exactly as they are.
# A blocklist ("remove the words we thought of") is the wrong tool for that: the
# ONE hype word nobody listed is the one the reviewer sees. So the review copy is
# REBUILT from an allowlist instead - only two kinds of line survive:
#     1. a product line   -> the product's own words + the price the source printed
#     2. the link
# Anything else is dropped. Nothing is ever invented: every word and every digit
# still comes from the source post.
SAFE_STRICT_REBUILD = os.getenv(
    "SAFE_STRICT_REBUILD", "true").strip().lower() not in ("0", "false", "no", "off")
# Words that must never survive into the review copy, whatever shape the line has.
# This is the LAST net, not the first: the allowlist above has already run.
_STRICT_BANNED_RE = re.compile(
    r"(?i)\b(?:loot|steal|jackpot|bumper|dhamaka|blast|crazy|insane|cheapest|"
    r"lowest|biggest|hurry|fast|urgent|limited|stock|grab|max\s*quantity|"
    r"free\s*money|guaranteed|must\s*buy|don'?t\s*miss|last\s*chance|"
    r"hot|mega|super\s*deal|price\s*error|glitch|trick|cashback\s*trick)\b")


# Lines that must never survive into the review copy, whatever else they say.
# The 2026-09-06 rejection named "reviews" explicitly alongside trademarked
# words and screenshots, and RATINGS/REVIEW COUNTS are Amazon-owned content we
# are not licensed to republish. Everything else here is either an unverifiable
# claim, a condition the reviewer reads as a catch, or pure decoration.
_STRICT_DROP_LINE_RE = re.compile(
    r"(?i)("
    r"\b\d(?:\.\d)?\s*(?:\u2b50|stars?|/\s*5)\b"          # 4.2 stars, 4.2/5
    r"|\brating[s]?\b|\breview[s]?\b|\bratings?\s*&\s*reviews?\b"
    r"|\b\d[\d,]*\s*(?:reviews?|ratings?)\b"                 # 12,453 reviews
    r"|\bbest\s*sell(?:er|ing)\b|\b#\d+\s*in\b"            # bestseller badges
    r"|\bm\.?r\.?p\.?\b|\blist\s*price\b"                  # MRP anchor pricing
    r"|\b\d{1,3}\s*%\s*(?:off|discount)?\b"                  # 70% OFF
    r"|\bsave\s*(?:rs\.?|\u20b9|inr)?\s*[\d,]+\b"
    r"|\bcoupon\b|\bapply\b|\bclip\b|\bpromo\s*code\b|\bcode\s*[:\-]"
    r"|\bcashback\b|\bbank\s*offer\b|\bemi\b|\bexchange\b"
    r"|\bbuy\s*now\b|\bshop\s*now\b|\border\s*now\b|\bclick\b|\btap\b"
    r"|\bjoin\b|\bshare\b|\bsubscribe\b|\bfollow\b"
    r"|\bsold\s*out\b|\bout\s*of\s*stock\b|\bstock\s*(?:left|over)\b"
    r"|\bqty\b|\bquantity\b|\bpieces?\s*left\b"
    r"|\bstore\b\s*$"                                          # "Under 99 Store"
    r")")


def _salvage_price_only(line: str) -> str:
    """Return "Price: <amount>" when a dropped line still carried the DEAL price.

    The amount is copied verbatim from the source - never recomputed - and any
    MRP/list-price anchor is masked out first so the higher number can never be
    mistaken for the price we publish.
    """
    masked = re.sub(
        r"(?i)\b(?:mrp|m\.r\.p\.?|list\s*price|regular\s*price)\s*[:@-]?\s*"
        r"(?:rs\.?|\u20b9|inr)?\s*[\d,]+", " ", line or "")
    # Only an explicitly-marked amount counts; a bare number is a model code.
    match = re.search(r"(?:rs\.?|\u20b9|inr)\s*\.?\s*([\d,]+)|([\d,]+)\s*/-", masked, re.I)
    if not match:
        return ""
    amount = (match.group(1) or match.group(2) or "").strip()
    if not amount or not amount.replace(",", "").isdigit():
        return ""
    return f"Price: \u20b9{amount}"


def _strict_review_copy(body: str) -> str:
    """Rebuild a post from an allowlist: product line(s) + link. Nothing else.

    The price is kept EXACTLY as the source printed it (₹298 / at 258 / @167),
    because a price we re-format is a price we could get wrong, and a wrong price
    is the one thing a marketplace programme will not forgive.
    """
    kept: list[str] = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if URL_RE.fullmatch(line):
            kept.append(line)
            continue
        if _STRICT_BANNED_RE.search(line):
            continue
        # Amazon marks and Telegram pointers are the two things the 2026-09-06
        # rejection actually named. Drop the line rather than try to reword it.
        if has_amazon_trademark(line) or has_telegram_pointer(line):
            continue
        # Ratings/review counts are Amazon-owned content ("or reviews" in the
        # rejection); MRP/percent-off/coupons are conditions and anchor claims a
        # reviewer reads as a catch. None of it is needed to sell the product.
        if _STRICT_DROP_LINE_RE.search(line):
            # "Price - Rs.899 (MRP Rs.2990)" is dropped for the MRP anchor, but
            # the DEAL price on it is the one fact the post cannot lose. Keep the
            # price alone, exactly as the source printed it, and drop the rest.
            salvaged = _salvage_price_only(line)
            if salvaged:
                kept.append(salvaged)
            continue
        # A product line has to read like a product: real words, and not a bare
        # number or a lone symbol. Two letter-words is the floor ("Methi Dana").
        words = re.findall(r"[A-Za-z][A-Za-z'&.-]+", line)
        if len(words) < 2:
            # Not a product line - but a lone "@ 299/-" or "₹899" IS the price,
            # written on its own line by many sources. Keep it as the price.
            salvaged = _salvage_price_only(line)
            if salvaged:
                kept.append(salvaged)
            continue
        # Trailing junk the earlier passes may have left on an otherwise good line.
        line = re.sub(r"\s{2,}", " ", line).strip(" \t|-–—:•*~,")
        if line:
            kept.append(line)
    # A shopper needs the price. If none of the surviving lines carries one, take
    # it from the ORIGINAL post rather than publish a product with no price.
    body_text = URL_RE.sub(" ", "\n".join(kept))
    # "Pigeon Kettle 1.5L at 549" already states the price; adding "Price: 549"
    # under it is the duplicate the user keeps reporting. So the check accepts
    # ANY amount already visible - with a currency mark, the Indian "/-" suffix,
    # or an "at/only/@" price phrase - not just a currency-marked one.
    has_price = re.search(
        r"(?i)(?:rs\.?|\u20b9|inr)\s*\.?\s*[\d,]+"
        r"|[\d,]+\s*/-"
        r"|\b(?:at|only|for|@)\s*(?:rs\.?|\u20b9|inr)?\s*[\d,]{2,}\b",
        body_text)
    if kept and not has_price:
        price = parse_price(body or "")
        if price:
            link_at = next((i for i, ln in enumerate(kept) if URL_RE.fullmatch(ln)), len(kept))
            kept.insert(link_at, f"Price: \u20b9{price:,}")
    # Never repeat the same line twice (two sources, one merged post).
    deduped = list(dict.fromkeys(kept))
    return "\n".join(deduped).strip()


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
    """Split at line boundaries; never silently truncate Telegram content.

    A line longer than one message is packed word by word, and a link is NEVER cut
    in half: a URL split across two messages is a dead link and an unpaid sale,
    which is a worse post than one with an awkward line break. (Until v17.8 this
    function bisected over-long lines by character index, so a long list line whose
    4200th character happened to be inside an affiliate link printed
    "https://www.amazon.in/dp/B0AB" in one message and "CDEF?tag=..." in the next.)
    """
    if len(text) <= limit:
        return [text]
    result: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            result.append(current.rstrip())
        current = ""

    for line in text.splitlines(True):
        if len(line) <= limit:
            if current and len(current) + len(line) > limit:
                flush()
            current += line
            continue
        flush()
        piece = ""
        for token in re.split(r"(\s+)", line):     # whitespace kept, so nothing is lost
            if not token:
                continue
            if len(token) > limit:                 # a single monster token: no link is that long
                if piece.strip():
                    result.append(piece.rstrip())
                    piece = ""
                result.extend(token[i:i + limit] for i in range(0, len(token), limit))
                continue
            if piece and len(piece) + len(token) > limit:
                result.append(piece.rstrip())
                piece = ""
            piece += token
        current = piece
    flush()
    return result


MARKED_CHANNEL_PREFIX = 1_000_000_000_000  # Telegram's "-100" channel marker


def raw_chat_id(value: Any) -> int:
    """Canonical numeric chat id for dedup, independent of ID convention.

    Telethon reports a channel as `-100<id>` (Bot API "marked" form), a resolved
    entity exposes the raw positive id, and a legacy basic chat is `-<id>`. All
    three must collapse to ONE key, otherwise the same source message can be
    queued twice (once by the live event stream, once by the rescan safety net)
    and then posted twice to every target.
    """
    try:
        if hasattr(value, "id"):
            value = value.id
        v = int(value)
    except (TypeError, ValueError):
        return 0
    if v > 0:
        return v
    v = -v
    text = str(v)
    if text.startswith("100") and len(text) > 3:
        with contextlib.suppress(ValueError):
            return int(text[3:])
    if v > MARKED_CHANNEL_PREFIX:
        return v - MARKED_CHANNEL_PREFIX
    return v


def marked_chat_id(value: Any) -> int:
    """The id Telethon needs in order to FETCH messages (marked channel form)."""
    raw = raw_chat_id(value)
    if not raw:
        return 0
    try:
        entity_id = int(getattr(value, "id", raw) or raw)
    except (TypeError, ValueError):
        entity_id = raw
    # Legacy basic chats are negated without the -100 marker.
    if entity_id < 0 and not str(-entity_id).startswith("100"):
        return entity_id
    return int(f"-100{raw}")


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
        CREATE TABLE IF NOT EXISTS posted_products (
          signature TEXT NOT NULL,
          target TEXT NOT NULL,
          price INTEGER,
          discount INTEGER,
          posted_at REAL NOT NULL,
          PRIMARY KEY(signature,target)
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
        -- Daily send counter for the review channel (IST day key), so the
        -- 20-30/day pace survives restarts instead of resetting to zero.
        CREATE TABLE IF NOT EXISTS shopping_quota (
          day_key TEXT PRIMARY KEY,
          sent_count INTEGER NOT NULL DEFAULT 0,
          last_sent_at REAL NOT NULL DEFAULT 0
        );
        """)
        # Backward-compatible migrations.
        quota_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(shopping_quota)")}
        if "last_sent_at" not in quota_columns:
            self.conn.execute("ALTER TABLE shopping_quota ADD COLUMN last_sent_at REAL NOT NULL DEFAULT 0")
        delivery_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(deliveries)")}
        if "chunks_sent" not in delivery_columns:
            self.conn.execute("ALTER TABLE deliveries ADD COLUMN chunks_sent INTEGER NOT NULL DEFAULT 0")
        queue_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(queue)")}
        if "premium_score" not in queue_columns:
            self.conn.execute("ALTER TABLE queue ADD COLUMN premium_score INTEGER")
        # v17.6: "best copy" bookkeeping (see product_identities).
        for column, statement in (
            ("quality", "ALTER TABLE queue ADD COLUMN quality INTEGER"),
            ("product_keys", "ALTER TABLE queue ADD COLUMN product_keys TEXT"),
            ("superseded_chat_id", "ALTER TABLE queue ADD COLUMN superseded_chat_id TEXT"),
            ("superseded_msg_id", "ALTER TABLE queue ADD COLUMN superseded_msg_id INTEGER"),
            # v18.2: the delivery-side repairs (a link cut because it is dead or unverified)
            # have to know whether a blank line is the SOURCE's layout or their own residue.
            # The rendered copy alone cannot answer that, so the raw source text rides along
            # on the row (bounded; it is bookkeeping, not content).
            ("source_text", "ALTER TABLE queue ADD COLUMN source_text TEXT"),
        ):
            if column not in queue_columns:
                self.conn.execute(statement)
        if "priority" not in queue_columns:
            self.conn.execute("ALTER TABLE queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 1")
        if "chat_key" not in queue_columns:
            self.conn.execute("ALTER TABLE queue ADD COLUMN chat_key TEXT")
            # Backfill the canonical key for rows written by older builds so a
            # re-encountered message still hits the unique index instead of
            # creating a second queue row (== a second post).
            legacy_rows = self.conn.execute(
                "SELECT id, chat_id FROM queue WHERE chat_key IS NULL"
            ).fetchall()
            for row in legacy_rows:
                self.conn.execute("UPDATE queue SET chat_key=? WHERE id=?",
                                  (str(raw_chat_id(row[1])), row[0]))
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_queue_chat_key ON queue(chat_key, msg_id)"
            )
        except sqlite3.IntegrityError:
            # An older build really did create two rows for one message (the
            # double-post bug). Collapse them, then enforce uniqueness for good.
            self._collapse_duplicate_queue_rows()
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_queue_chat_key ON queue(chat_key, msg_id)"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_queue_status "
            "ON queue(status, priority, created_at)"
        )
        # A previous crash must not leave work permanently stuck.
        self.conn.execute("UPDATE queue SET status='pending' WHERE status='processing'")
        self.conn.commit()
        notify_queue()
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

    def _collapse_duplicate_queue_rows(self) -> None:
        """Fold pre-existing duplicate (chat_key, msg_id) rows into one row.

        Only ever called from the schema migration: older builds keyed the queue
        on the raw chat id, so one source message could legitimately end up with
        two queue rows and therefore be posted twice.
        """
        duplicates = self.conn.execute(
            "SELECT chat_key, msg_id, GROUP_CONCAT(id) FROM queue "
            "WHERE chat_key IS NOT NULL GROUP BY chat_key, msg_id HAVING COUNT(*) > 1"
        ).fetchall()
        dropped = 0
        for row in duplicates:
            ids = [int(x) for x in (row[2] or "").split(",") if x]
            if len(ids) < 2:
                continue
            keep = min(ids)
            extra = [x for x in ids if x != keep]
            for queue_id in extra:
                self.conn.execute("DELETE FROM deliveries WHERE queue_id=?", (queue_id,))
                self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (queue_id,))
                self.conn.execute("DELETE FROM queue WHERE id=?", (queue_id,))
                dropped += 1
        if dropped:
            log.info("DEDUP MIGRATION | collapsed %s duplicate queue row(s)", dropped)

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

    async def queue_id_for(self, chat_id: int, msg_id: int) -> int | None:
        """The queue id behind a just-queued message - so the immediacy log line
        can be paired with the "JOB N | ... sent=" line by the deploy verifier."""
        key = str(raw_chat_id(chat_id))
        async with self.lock:
            row = self.conn.execute("SELECT id FROM queue WHERE chat_key=? AND msg_id=?",
                                    (key, msg_id)).fetchone()
        return int(row[0]) if row else None

    async def product_already_posted(self, target: str, signature: str,
                                     price: int | None, discount: int | None) -> tuple[bool, str]:
        """Has THIS channel already carried this product inside the skip window?

        Returns (skip, why). A copy that is strictly better - a lower price, or a
        deeper discount at the same price - is never skipped, because on a deals
        channel a cheaper repeat of the same product is a real new post.
        """
        if not signature or SAME_PRODUCT_SKIP_SECONDS <= 0:
            return False, ""
        async with self.lock:
            row = self.conn.execute(
                "SELECT price, discount, posted_at FROM posted_products "
                "WHERE signature=? AND target=? AND posted_at>=?",
                (signature, target, time.time() - SAME_PRODUCT_SKIP_SECONDS),
            ).fetchone()
        if not row:
            return False, ""
        last_price, last_discount, posted_at = row[0], row[1], row[2]
        # "Better" has to be MEANINGFULLY better. The discount is re-derived from
        # the post text, and a source that spells out an MRP can read one point
        # higher than the same deal written without it - a 1-point gap is noise,
        # and letting it through is exactly the repeat the user complained about.
        better_price = price is not None and (last_price is None or price < last_price)
        better_discount = (discount or 0) >= (last_discount or 0) + SAME_PRODUCT_DISCOUNT_MARGIN
        if better_price or better_discount:
            return False, (f"better copy (₹{price or 0} / {discount or 0}% off vs "
                           f"₹{last_price or 0} / {last_discount or 0}% off)")
        age_hours = max(1, int((time.time() - posted_at) // 3600))
        return True, f"same product posted to this channel {age_hours}h ago"

    async def mark_product_posted(self, target: str, signature: str,
                                  price: int | None, discount: int | None) -> None:
        if not signature or SAME_PRODUCT_SKIP_SECONDS <= 0:
            return
        async with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO posted_products VALUES(?,?,?,?,?)",
                (signature, target, price, discount, time.time()),
            )
            self.conn.execute(
                "DELETE FROM posted_products WHERE posted_at<?",
                (time.time() - max(SAME_PRODUCT_SKIP_SECONDS, 3 * 24 * 3600),),
            )
            self.conn.commit()

    async def seen_message(self, chat_id: int, msg_id: int) -> bool:
        """True when this exact source message is already known to the queue."""
        key = str(raw_chat_id(chat_id))
        async with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM queue WHERE chat_key=? AND msg_id=? LIMIT 1", (key, msg_id)
            ).fetchone()
        return row is not None

    async def is_duplicate_content(self, text: str, queue_id: int | None = None) -> bool:
        """Cheap intake-stage fingerprint check (no HTTP, no rendering).

        Catches the copy-paste reposts that used to burn a whole render cycle
        before the pre-send gate rejected them, and closes the window where the
        same campaign queued from two sources could both reach the send path.
        """
        if not text:
            return False
        try:
            key = content_deal_key(text)
        except Exception:
            return False
        if not key:
            return False
        now = time.time()
        async with self.lock:
            # Already posted inside the product-dedup window -> definite copy.
            posted = self.conn.execute(
                "SELECT 1 FROM posted_deals WHERE deal_key=? AND posted_at>=?",
                (key, now - PRODUCT_DEDUP_SECONDS)
            ).fetchone()
            # Or a live job from another source is already carrying this exact
            # campaign (the same test the pre-send gate does, applied earlier so
            # the duplicate never burns a render cycle). Only fresh claims of
            # still-active jobs count, so a dead claim can never lose a deal.
            claimed = self.conn.execute(
                "SELECT 1 FROM deal_claims c JOIN queue q ON q.id=c.queue_id "
                "WHERE c.deal_key=? AND c.queue_id<>? "
                "AND q.status IN ('pending','processing') AND c.claimed_at>=?",
                (key, queue_id or 0, now - max(1200.0, SOURCE_RESCAN_SECONDS * 2))
            ).fetchone()
        return bool(posted or claimed)

    async def enqueue(self, chat_id: int, msg_id: int, source: str,
                      text: str = "", has_media: bool = False) -> bool:
        # Best-lists first, then super discounts, then photos - so the highest
        # value Telegram post is rendered/delivered ahead of ordinary ones.
        priority = classify_priority(text, has_media)
        # User rule: first-preference sources (e.g. pricehistory) post ahead of
        # the same content tier from other sources on every non-Tricks channel.
        if source in PRIORITY_SOURCES:
            priority = min(priority + 1, 5)
        # Layer 1 of the zero-duplicate guarantee: a campaign whose fingerprint
        # was already posted (or is already waiting in the queue) is never even
        # inserted, so it can never reach the send path twice.
        if text and await self.is_duplicate_content(text):
            log.info("INTAKE DEDUP | source=%s msg=%s (already posted or queued)", source, msg_id)
            return False
        # v17.6: same product, better copy -> the pending row is re-pointed at the
        # stronger deal instead of a coin flip deciding what our channels post. An
        # error here must never cost coverage, so it degrades to plain insert.
        if text:
            try:
                swapped = await self.swap_in_better_copy(chat_id, msg_id, source, text, priority)
            except Exception as exc:  # noqa: BLE001 - enhancement, never a blocker
                log.warning("BEST COPY check skipped: %s", exc)
                swapped = False
            if swapped:
                return False
        async with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO queue(chat_id,msg_id,source,created_at,priority,chat_key,"
                "quality,product_keys,source_text) VALUES(?,?,?,?,?,?,?,?,?)",
                (chat_id, msg_id, source, time.time(), priority, str(raw_chat_id(chat_id)),
                 deal_quality_score(text) if text else 0,
                 json.dumps(sorted(product_identities(text))) if text else None,
                 (text or "")[:8000] or None),
            )
            self.conn.commit()
        inserted = cur.rowcount > 0
        if inserted:
            # Wake the idle workers immediately: a deal must never wait for the
            # next idle poll cycle (that polling gap was a visible posting lag).
            notify_queue()
        return inserted

    async def swap_in_better_copy(self, chat_id: int, msg_id: int, source: str,
                                  text: str, priority: int) -> bool:
        """Re-point a still-undelivered queue row at a better copy of the same product.

        Deliberately narrow, because losing a post is worse than posting the
        weaker copy: only a SINGLE-product post (one link, one price), never a
        Tricks/card/service post, and only when an exact merchant product id
        matches a row that has not been claimed yet. One row in, one row out -
        the duplicate guarantee is untouched - and the displaced copy is
        remembered so `restore_superseded` can queue it if the better one fails.
        """
        keys = product_identities(text)
        if not keys or source in TRICKS_SOURCES or has_card_offer(text):
            return False
        # A single product, by the only signals intake can see without rendering:
        # at most one link, and no list markers (these sources put one link per
        # item, so a roundup with several products is never re-pointed).
        links = list(dict.fromkeys(clean_url(u) for u in URL_RE.findall(text or "")))
        if len(links) > 1:
            return False
        if re.search(r"(?im)^\s*(?:deal\s*\d+|\d+\s*[.)])|(?:MEGA DEAL LIST|DEAL LIST)", text or ""):
            return False
        score = deal_quality_score(text)
        wanted = comparable_product_ids(keys)
        async with self.lock:
            rows = self.conn.execute(
                "SELECT id, chat_key, chat_id, msg_id, source, priority, quality, "
                "product_keys, deal_keys_json FROM queue WHERE status='pending'"
            ).fetchall()
            for row in rows:
                if (row["source"] or "") in TRICKS_SOURCES:
                    continue
                pending_keys = json.loads(row["product_keys"] or "[]")
                if len(pending_keys) > 1:
                    # A list post stands for several products; re-pointing it at one
                    # product would delete the rest of the roundup.
                    continue
                pending_ids = comparable_product_ids(pending_keys)
                if not pending_ids:
                    try:
                        pending_ids |= comparable_product_ids(json.loads(row["deal_keys_json"] or "[]"))
                    except (TypeError, ValueError):
                        pass
                if not (wanted & pending_ids):
                    continue
                if row["quality"] is None:
                    # A row created before this feature (or without text) has no
                    # score to compare against: leave it alone rather than risk a
                    # downgrade.
                    continue
                if score <= (row["quality"] or 0):
                    # The queued copy is at least as good: nothing to gain, and the
                    # render-time product ledger already blocks a duplicate post.
                    return True
                self.conn.execute(
                    "UPDATE queue SET chat_id=?, msg_id=?, source=?, quality=?, product_keys=?, source_text=?, "
                    "priority=?, rendered_text=NULL, targets_json=NULL, deal_keys_json=NULL, "
                    "attempts=0, next_at=0, superseded_chat_id=?, superseded_msg_id=? WHERE id=?",
                    (chat_id, msg_id, source, score, json.dumps(sorted(keys)),
                     (text or "")[:8000] or None,
                     max(int(priority), int(row["priority"] or 1)),
                     str(row["chat_id"]), row["msg_id"], row["id"]),
                )
                self.conn.commit()
                log.info("BEST COPY | queue=%s re-pointed at %s msg=%s for %s (score %s beats %s)",
                         row["id"], source, msg_id, ",".join(sorted(wanted)),
                         score, row["quality"] or 0)
                notify_queue()
                return True
        return False

    async def restore_superseded(self, row: sqlite3.Row) -> bool:
        """Queue the copy a "best copy" swap displaced, so a failing better post
        can never turn into a missing deal."""
        keys = row.keys()
        old_chat = row["superseded_chat_id"] if "superseded_chat_id" in keys else None
        old_msg = row["superseded_msg_id"] if "superseded_msg_id" in keys else None
        if not old_chat or not old_msg:
            return False
        async with self.lock:
            self.conn.execute(
                "UPDATE queue SET superseded_chat_id=NULL, superseded_msg_id=NULL WHERE id=?",
                (row["id"],),
            )
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO queue(chat_id,msg_id,source,created_at,priority,chat_key,quality) "
                "VALUES(?,?,?,?,?,?,?)",
                (int(old_chat), int(old_msg), row["source"], time.time(), 3,
                 str(raw_chat_id(int(old_chat))), 0),
            )
            self.conn.commit()
        if cur.rowcount:
            log.info("BEST COPY ROLLBACK | queue=%s restored the original copy (msg=%s)",
                     row["id"], old_msg)
            notify_queue()
            return True
        return False

    async def claim_job(self) -> sqlite3.Row | None:
        async with self.lock:
            now = time.time()
            self.conn.execute("BEGIN IMMEDIATE")
            order = "DESC" if QUEUE_ORDER == "newest" else "ASC"
            row = self.conn.execute(
                "SELECT * FROM queue WHERE status='pending' AND next_at<=? "
                f"ORDER BY priority DESC, created_at {order} LIMIT 1", (now,)
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
                "UPDATE queue SET status='pending', next_at=0 WHERE status='processing' AND next_at<?",
                (time.time() - max_age_seconds,),
            )
            self.conn.commit()
            if cur.rowcount:
                notify_queue()
            return cur.rowcount

    async def drop_stale_jobs(self, max_age_hours: float | None = None) -> int:
        """Drop pending work older than the freshness budget (never post it late).

        A loot price that is six hours old is already dead: posting it late both
        looks wrong and costs subscribers. Newest-first claiming needs this
        sweeper so stale inventory cannot occupy the queue indefinitely.
        """
        hours = MAX_JOB_AGE_HOURS if max_age_hours is None else float(max_age_hours)
        if hours <= 0:
            return 0
        cutoff = time.time() - hours * 3600
        async with self.lock:
            rows = self.conn.execute(
                "SELECT id, source, msg_id FROM queue WHERE status='pending' AND created_at<?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE queue SET status='done', last_error=? WHERE id=?",
                    (f"stale job dropped (older than {hours:g}h)", row["id"]),
                )
                self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (row["id"],))
            self.conn.commit()
        if rows:
            log.info("STALE DROP | %s pending job(s) older than %sh skipped instead of posted late",
                     len(rows), hours)
        return len(rows)

    async def remember_products(self, queue_id: int, keys: list[str]) -> None:
        """Persist the product ids a queue row stands for (see swap_in_better_copy)."""
        if not keys:
            return
        async with self.lock:
            self.conn.execute("UPDATE queue SET product_keys=? WHERE id=?",
                              (json.dumps(list(keys)[:12]), queue_id))
            self.conn.commit()

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
        """Keep Amazon product previews; suppress Flipkart/Myntra generic cards and wasteful list previews (user rule 2026-09-04)."""
        urls = list(dict.fromkeys(clean_url(x) for x in URL_RE.findall(text or "")))
        # Wasteful list preview: 2+ links in one message already makes it tall — extra card is waste (Myntra extend waste)
        if len(urls) >= 2:
            return False
        async with self.lock:
            for url in urls:
                row = self.conn.execute(
                    "SELECT resolved_url FROM link_cache WHERE affiliate_url=? LIMIT 1", (url,)
                ).fetchone()
                resolved = row[0] if row and row[0] else url
                host = (urlparse(resolved).hostname or "").lower()
                # linkredirect.in is source wrapper never final, Myntra/myntr.it generic shop card is waste — photo okay but extend waste
                if host_matches(host, "flipkart.com") or host_matches(host, "linkredirect.in") or host_matches(host, "myntra.com") or host_matches(host, "myntr.it"):
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
            # Expire only claims whose owning job is FINISHED. Purging a claim a
            # live job still holds is how the same product got posted twice from
            # another source: a big backlog or a Premium window can legitimately
            # keep a job pending for far longer than 20 minutes.
            self.conn.execute(
                "DELETE FROM deal_claims WHERE claimed_at<? AND queue_id NOT IN "
                "(SELECT id FROM queue WHERE status IN ('pending','processing'))",
                (claim_cutoff,),
            )
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
            # Price gate: the exact same detected price must not be posted again
            # from any source inside the window. By default this only applies when
            # the post has NO product identity (the documented fallback); a real
            # ASIN/PID match is already covered by the key check above, and two
            # different ₹99 products are not duplicates of each other.
            # PRICE_DEDUP_IGNORES_IDENTITY=true restores the strict behaviour.
            if (price is not None and PRICE_DEDUP_SECONDS > 0
                    and (PRICE_DEDUP_IGNORES_IDENTITY or not has_identity)):
                pp = self.conn.execute(
                    "SELECT 1 FROM price_posts WHERE price=? AND posted_at>=?",
                    (price, now - PRICE_DEDUP_SECONDS),
                ).fetchone()
                if pp:
                    self.conn.rollback()
                    return False, []
            if not new_keys:
                # A post with NO identifiable product at all (every link in it was a
                # dead shortener, cut above) has no keys to reserve, and calling that a
                # "duplicate deal" of nothing is how a real post went missing. The
                # content fingerprint above is the honest check for it: same text twice
                # is still refused, this text once is a first.
                if not keys and content_key:
                    self.conn.commit()
                    return True, []
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

    async def has_pending_work(self) -> bool:
        """True when a claimable job exists (checked after clearing the wake)."""
        async with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM queue WHERE status='pending' AND next_at<=? LIMIT 1",
                (time.time(),),
            ).fetchone()
        return row is not None

    async def has_partial_delivery(self, queue_id: int) -> bool:
        """True when at least one chunk of this job already reached a target."""
        async with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM deliveries WHERE queue_id=? AND chunks_sent>0 LIMIT 1", (queue_id,)
            ).fetchone()
        return row is not None

    async def remember_passthrough(self, source_url: str, url: str, deal_key: str) -> None:
        """Record a clean unmonetized destination so it can pass the provenance gate.

        A pass-through link is deliberately NOT an affiliate link: it exists only
        because the network has no campaign for that store. Registering the exact
        pair keeps the "only links we produced may be published" rule intact
        instead of weakening the check itself.
        """
        async with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO link_cache(source_url,affiliate_url,resolved_url,deal_key,created_at)"
                " VALUES(?,?,?,?,?)",
                (source_url, url, url, deal_key, time.time()),
            )
            self.conn.commit()

    async def revive_edited_job(self, chat_key: str, msg_id: int) -> str | None:
        """Re-open a source post that was edited before it reached any target.

        Deal channels publish the text first and add or repair the link seconds
        later. The first render then failed with "no monetizable URLs"/"no URLs",
        the row was closed, and the deal never appeared in our channels even
        though the edited source post is perfect - a whole class of "source has
        it, we don't". A job that already reached at least one target is left
        alone on purpose: posting the edit again would be a duplicate.
        """
        async with self.lock:
            row = self.conn.execute(
                "SELECT q.id, q.status FROM queue q WHERE q.chat_key=? AND q.msg_id=? "
                "AND NOT EXISTS (SELECT 1 FROM deliveries d "
                "WHERE d.queue_id=q.id AND d.status='sent')",
                (str(chat_key), msg_id),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "pending":
                # Still waiting for a worker: nothing to revive, the render will
                # read the edited text from Telegram anyway.
                return "pending"
            self.conn.execute(
                "UPDATE queue SET status='pending', next_at=0, attempts=0, "
                "rendered_text=NULL, last_error='' WHERE id=?",
                (row["id"],),
            )
            self.conn.commit()
        notify_queue()
        return "revived"

    async def update_rendered(self, queue_id: int, text: str) -> None:
        """Freeze the canonical rendered text so every target (and every retry
        after a restart) sends byte-identical chunks."""
        async with self.lock:
            self.conn.execute("UPDATE queue SET rendered_text=? WHERE id=?", (text, queue_id))
            self.conn.commit()

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

    async def shopping_quota_left(self) -> int:
        """How many more posts the review channel may take today (IST)."""
        if SHOPPING_DAILY_CAP <= 0:
            return 1_000_000
        day_key = datetime.now(IST).strftime("%Y-%m-%d")
        async with self.lock:
            row = self.conn.execute(
                "SELECT sent_count FROM shopping_quota WHERE day_key=?", (day_key,)
            ).fetchone()
        used = int(row["sent_count"]) if row else 0
        return max(0, SHOPPING_DAILY_CAP - used)

    async def shopping_pace_wait(self) -> float:
        """Seconds to wait before this channel may post again, 0 when ready.

        USER RULE (2026-09-06): "oka human laga daily oka 50 posts". A human
        curator does not fire 50 posts in one burst at 3am and then vanish - and
        a burst is exactly what a reviewer reads as an automated feed. So the
        day's allowance is SPREAD: posting hours only, and a minimum gap between
        posts derived from the cap, with a little jitter so the timing never
        looks machine-regular.
        """
        if not SHOPPING_HUMAN_PACING:
            return 0.0
        now = datetime.now(IST)
        # Outside posting hours nobody is shopping and nobody is curating.
        if not (SHOPPING_ACTIVE_START <= now.hour < SHOPPING_ACTIVE_END):
            tomorrow = now.replace(hour=SHOPPING_ACTIVE_START, minute=0, second=0, microsecond=0)
            if now.hour >= SHOPPING_ACTIVE_END:
                tomorrow += timedelta(days=1)
            return max(60.0, (tomorrow - now).total_seconds())
        day_key = now.strftime("%Y-%m-%d")
        async with self.lock:
            row = self.conn.execute(
                "SELECT last_sent_at FROM shopping_quota WHERE day_key=?", (day_key,)
            ).fetchone()
        last = float(row["last_sent_at"]) if row and row["last_sent_at"] else 0.0
        if not last:
            return 0.0
        active_seconds = (SHOPPING_ACTIVE_END - SHOPPING_ACTIVE_START) * 3600
        cap = SHOPPING_DAILY_CAP if SHOPPING_DAILY_CAP > 0 else 50
        # Spread the cap across the active window, then relax it slightly so a
        # busy hour can still catch up rather than silently dropping deals.
        gap = (active_seconds / max(1, cap)) * 0.7
        gap += random.uniform(0, gap * 0.4)          # never machine-regular
        waited = time.time() - last
        return max(0.0, gap - waited)

    async def note_shopping_sent(self) -> None:
        day_key = datetime.now(IST).strftime("%Y-%m-%d")
        async with self.lock:
            self.conn.execute(
                "INSERT INTO shopping_quota(day_key,sent_count,last_sent_at) VALUES(?,1,?) "
                "ON CONFLICT(day_key) DO UPDATE SET sent_count=sent_count+1, last_sent_at=?",
                (day_key, time.time(), time.time()),
            )
            # Keep the table tiny: yesterday's counters are of no further use.
            self.conn.execute("DELETE FROM shopping_quota WHERE day_key < ?",
                              ((datetime.now(IST) - timedelta(days=3)).strftime("%Y-%m-%d"),))
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
                gap_max = max(int(PREMIUM_GAP_MIN_SECONDS), int(PREMIUM_GAP_MAX_SECONDS))
                next_at = time.time() + random.randint(int(PREMIUM_GAP_MIN_SECONDS), gap_max)
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
            elif remaining_targets and attempts < JOB_MAX_ATTEMPTS:
                delay = job_retry_delay(attempts)
                self.conn.execute(
                    "UPDATE queue SET status='pending',attempts=attempts+1,next_at=? WHERE id=?",
                    (now + delay, queue_id),
                )
            else:
                self.conn.execute("UPDATE queue SET status='done' WHERE id=?", (queue_id,))
                if not successes:
                    self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (queue_id,))
            self.conn.commit()
        # A job that still owes targets must be picked up immediately, not at
        # the end of the next idle poll.
        notify_queue()

    async def fail_job(self, row: sqlite3.Row, error: str) -> None:
        async with self.lock:
            attempts = row["attempts"] + 1
            if attempts >= JOB_MAX_ATTEMPTS:
                status, next_at = "failed", 0
                self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (row["id"],))
            else:
                status, next_at = "pending", time.time() + job_retry_delay(attempts)
            self.conn.execute(
                "UPDATE queue SET status=?,attempts=?,next_at=?,last_error=? WHERE id=?",
                (status, attempts, next_at, error[:1000], row["id"]),
            )
            self.conn.commit()
        log.info("RETRY | queue=%s attempt=%s status=%s (%s)",
                 row["id"], attempts, status, error[:160])
        notify_queue()

    async def mark_done(self, queue_id: int, reason: str = "") -> None:
        async with self.lock:
            self.conn.execute(
                "UPDATE queue SET status='done',last_error=? WHERE id=?", (reason[:1000], queue_id)
            )
            self.conn.execute("DELETE FROM deal_claims WHERE queue_id=?", (queue_id,))
            self.conn.commit()
        notify_queue()

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
            # Same rule as reserve(): a claim held by a job that is still
            # pending/processing must survive housekeeping. Purging it was how a
            # deferred (Premium) or backlogged job lost its lock and the same
            # campaign from another source slipped through to a second post.
            self.conn.execute(
                "DELETE FROM deal_claims WHERE claimed_at<? AND queue_id NOT IN "
                "(SELECT id FROM queue WHERE status IN ('pending','processing'))",
                (now - 3600,),
            )
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
        # URL -> (checked_at, is_alive). The same URL is probed up to three
        # times per deal (merchant page, affiliate link, pre-send recheck);
        # without a cache each repeat cost another 18-36s of network waiting,
        # which is what made posts land minutes after the source.
        self._health: dict[str, tuple[float, bool]] = {}
        # long_url -> short link, so a retry never re-calls the shortener.
        self._short_cache: dict[str, str] = {}
        # USER RULE (2026-09-06): "review channelo shorten ga marchatam bitly use
        # cheyaku". A bit.ly/is.gd hop hides the destination, and a reviewer who
        # cannot see amazon.in in the link cannot verify the post. So the review
        # channel publishes the NATIVE store URL. Every short link we mint is
        # recorded here so the delivery step can put the real URL back.
        self._short_to_long: dict[str, str] = {}

    async def cache_link(self, source_url: str, affiliate: str, resolved: str, key: str) -> None:
        """Proxy to the global store's link cache so subclasses/tests can override."""
        await store.cache_link(source_url, affiliate, resolved, key)

    async def resolve(self, source_url: str) -> str:
        current = source_url
        for _ in range(4):
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
                # USER RULE (2026-09-04): whatever wrapper the source used must
                # end up as OUR link. Known shorteners are followed by name;
                # an UNKNOWN host whose link is a single short slug (the shape
                # every shortener has - "loot.deals/xYz9") is followed too, so
                # a brand-new wrapper domain still resolves to the real store
                # page and gets monetized instead of being posted as-is.
                if host not in SHORT_DOMAINS and not is_unresolvable_short_link(host, current):
                    break
                async with self.session.get(
                    current, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=HTTP_TOTAL_TIMEOUT_SECONDS),
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
        """Alive-check a URL, reusing the verdict of a recent identical check."""
        key = clean_url(url)
        cached = self._health.get(key)
        if cached and time.time() - cached[0] < LINK_HEALTH_CACHE_SECONDS:
            return cached[1]
        try:
            verdict = await asyncio.wait_for(self._probe_link(key),
                                             timeout=LINK_PROBE_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            log.info("LINK CHECK inconclusive | %.1fs budget for %s - posting anyway "
                     "(a slow page is not a dead deal)", LINK_PROBE_BUDGET_SECONDS, key[:90])
            verdict = True
        if LINK_HEALTH_CACHE_SECONDS > 0:
            if len(self._health) > 3000:
                self._health.clear()
            self._health[key] = (time.time(), verdict)
        return verdict

    async def _probe_link(self, url: str) -> bool:
        """Follow the URL and reject explicit merchant repair/dead-page responses."""
        for attempt in range(LINK_CHECK_ATTEMPTS):
            try:
                async with self.session.get(
                    url, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=HTTP_TOTAL_TIMEOUT_SECONDS),
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"},
                ) as response:
                    raw = await response.content.read(400_000)
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
            if attempt + 1 < LINK_CHECK_ATTEMPTS:
                await asyncio.sleep(LINK_CHECK_RETRY_SLEEP_SECONDS)
        # Do not discard a valid deal solely because Oracle was bot-blocked or
        # temporarily offline; explicit repair/dead-page signatures are blocked.
        return True

    async def rendered_links_not_broken(self, text: str) -> bool:
        """Belt-and-braces pre-send check, run CONCURRENTLY and time-boxed.

        The old serial loop could hold a worker for a minute or more on a
        multi-link post (each URL up to 2 x 18s). Every link is still checked;
        if the whole budget runs out we keep the render-path verdict instead of
        delaying a live deal, because a slow probe is not a broken deal.
        """
        if PRESEND_CHECK_BUDGET_SECONDS <= 0:
            # A zero budget is the operator saying "never hold a post for this extra
            # check" - and it used to mean the OPPOSITE (fall back to probing every link
            # SERIALLY, which is slower than the concurrent default). The render path has
            # already verified these links, so the honest reading is: skip the pre-send
            # pass, keep the post, and say nothing.
            return True
        urls = list(dict.fromkeys(clean_url(x) for x in URL_RE.findall(text or "")))
        if not urls:
            return False
        if len(urls) == 1:
            return await self.link_not_broken(urls[0])
        sem = asyncio.Semaphore(max(1, EK_MAX_CONCURRENCY))

        async def check(url: str) -> bool:
            async with sem:
                return await self.link_not_broken(url)

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(check(url) for url in urls)),
                timeout=PRESEND_CHECK_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("PRESEND CHECK | %s link(s) exceeded the %ss budget; "
                        "keeping the verified render-path links (deal is never lost over a slow probe)",
                        len(urls), int(PRESEND_CHECK_BUDGET_SECONDS))
            return True
        return all(results)

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
        # USER RULE (2026-09-03, FINAL): the Amazon Associates account keeps
        # getting rejected, so the old direct-Associates branch is GONE. Every
        # Amazon link is monetized through EarnKaro below, exactly like every
        # other store. If EarnKaro is briefly down the job retries, and the
        # final attempt still posts the clean merchant link (passthrough) —
        # a deal is never lost, it just never carries a dead Associates tag.
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
                        timeout=aiohttp.ClientTimeout(total=max(8.0, HTTP_TOTAL_TIMEOUT_SECONDS * 2)),
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
                        # A Flipkart product URL that came back long is compacted
                        # natively first (slug + item id + pid): free, no quota,
                        # and it is what keeps "shortga ravali" true even when the
                        # shortener is rate-limited.
                        if len(affiliate) > SHORTEN_MIN_LEN:
                            compact = compact_flipkart_product_link(affiliate)
                            if compact != affiliate and len(compact) < len(affiliate):
                                affiliate = result = compact
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
                    # Brief jitter, not a 8s sleep per attempt: an API blip must
                    # delay this deal by seconds, never by minutes.
                    await asyncio.sleep(min(2.5, 0.4 * (2 ** attempt) + random.random() * 0.3))
            EK_BREAKER.failure()
            raise RuntimeError(f"EarnKaro failed: {last}")

    async def shorten(self, long_url: str) -> str | None:
        # Same long URL seen again (retry, re-render, another target)? Reuse the
        # short link we already minted instead of spending another API call.
        cached_short = self._short_cache.get(long_url)
        if cached_short:
            return cached_short
        shortened = await self.bitly(long_url)
        if shortened:
            self._short_cache[long_url] = shortened
            self._short_to_long[shortened] = long_url
            return shortened
        if shortened:
            return shortened
        # Tokenless fallback for very long/multi-link posts. The authenticated
        # affiliate destination is created first; this service only redirects to it.
        try:
            async with self.session.get(
                "https://is.gd/create.php",
                params={"format": "simple", "url": long_url},
                timeout=aiohttp.ClientTimeout(total=max(6.0, HTTP_TOTAL_TIMEOUT_SECONDS)),
            ) as response:
                if response.status == 200:
                    link = (await response.text()).strip()
                    if link.startswith("https://is.gd/"):
                        log.info("SHORTENER fallback=is.gd")
                        self._short_cache[long_url] = link
                        self._short_to_long[link] = long_url
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
                    timeout=aiohttp.ClientTimeout(total=max(6.0, HTTP_TOTAL_TIMEOUT_SECONDS)),
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

    def expand_our_short_links(self, text: str) -> str:
        """Put the NATIVE store URL back wherever we minted a short link.

        Only links this process shortened are touched, so nothing is guessed and
        a source's own short link is left exactly as it arrived. Used for the
        Amazon-review channel, where a bit.ly hop hides the destination the
        reviewer has to be able to see.
        """
        out = text or ""
        for short, long_url in self._short_to_long.items():
            if short in out:
                out = out.replace(short, long_url)
        return out

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
        # TAGLESS: Pass 0 – strip any foreign Amazon tag from ALL Amazon links first
        for raw in dict.fromkeys(URL_RE.findall(out)):
            url = clean_url(raw)
            host = (urlparse(url).hostname or "").lower()
            if not in_domains(host, AMAZON_DOMAINS):
                continue
            stripped = apply_amazon_tag(url)
            if stripped and stripped != url and stripped != raw:
                out = out.replace(raw, stripped).replace(raw.replace("&", "&amp;"), stripped)
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
        # Pass 1b: native compaction of Flipkart product links (also free).
        # A dl.flipkart.com product URL arrives ~177 chars because of lid /
        # marketplace / srno session noise; slug + item id + pid is the same page
        # at about a third of the length, and it costs no shortener quota. This
        # runs BEFORE pass 2 so the shortener is only ever asked for the links
        # that genuinely cannot be compacted.
        for raw in dict.fromkeys(URL_RE.findall(out)):
            url = clean_url(raw)
            host = (urlparse(url).hostname or "").lower()
            if host not in FLIPKART_HOSTS:
                continue
            compact = compact_flipkart_product_link(url)
            if compact and compact != url and compact != raw:
                out = out.replace(raw, compact).replace(raw.replace("&", "&amp;"), compact)
                compacted += 1
        # Pass 2: Bitly/is.gd the remaining long links (search/category) — all
        # of them at once, so a 6-link list pays one round trip, not six.
        pending: list[tuple[str, str]] = []
        for raw in dict.fromkeys(URL_RE.findall(out)):
            url = clean_url(raw)
            host = (urlparse(url).hostname or "").lower()
            if len(url) <= SHORTEN_MIN_LEN:
                continue
            if in_domains(host, OUR_RUNTIME_SHORTENER_DOMAINS) or in_domains(host, OUR_SHORTENER_DOMAINS):
                continue
            pending.append((raw, url))
        shortened_all: list[str | None] = []
        if pending:
            sem = asyncio.Semaphore(max(1, min(6, EK_MAX_CONCURRENCY)))

            async def _short(url: str) -> str | None:
                async with sem:
                    return await self.shorten(url)

            shortened_all = list(await asyncio.gather(
                *(_short(url) for _, url in pending),
                return_exceptions=True,
            ))
        replacements: dict[str, str] = {}
        for (raw, url), shortened in zip(pending, shortened_all):
            if isinstance(shortened, Exception) or not shortened:
                if isinstance(shortened, Exception):
                    log.warning("SHORTEN failed %s: %s", url[:60], shortened)
                continue
            if shortened != url:
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
def fetch_chat_id(entity: Any, fallback: Any = None) -> int:
    """The chat id Telethon accepts for get_messages()/send_*() for this entity."""
    try:
        return int(tl_utils.get_peer_id(entity))
    except Exception:
        return marked_chat_id(fallback if fallback is not None else entity)


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
    # Queue under the SAME chat id the live event stream uses, otherwise the
    # (chat,msg_id) uniqueness check misses and one message is queued (and
    # posted) twice - once from backfill, once from the live update.
    chat_key = fetch_chat_id(entity, getattr(entity, "id", 0))
    try:
        messages = await client.get_messages(entity, limit=source_limit)
        for msg in reversed(messages):
            stamp = msg.date.timestamp() if getattr(msg, "date", None) else 0
            if stamp < cutoff:
                continue
            if not extract_urls(msg) and not getattr(msg, "reply_to", None):
                continue
            if await store.enqueue(chat_key, msg.id, source,
                                   (msg.message or msg.text or "")):
                queued += 1
        if queued:
            log.info("BACKFILL | source=%s candidates=%s hours=%s", source, queued, source_hours)
    except Exception as exc:
        log.warning("BACKFILL failed %s: %s", source, exc)


RESOLVED_SOURCE_IDS: dict[str, int] = {}


async def register_source(client: TelegramClient, source_map: dict, source: str,
                          targets: list[str], do_backfill: bool = False) -> bool:
    entity = await resolve_entity(client, source)
    if not entity:
        return False
    fetch_id = fetch_chat_id(entity, getattr(entity, "id", 0))
    # Map EVERY id convention that can arrive on an update (raw entity id,
    # Telethon's marked channel id, the plain -chat id of a legacy group). A
    # source that only matched one form silently received nothing at all.
    for key in {raw_chat_id(entity), fetch_id, int(getattr(entity, "id", 0) or 0)}:
        if key:
            source_map[key] = (source, list(targets))
    RESOLVED_SOURCE_IDS[source] = fetch_id
    log.info("SOURCE OK @%s -> %s -> %s", source, fetch_id, targets)
    if do_backfill:
        await backfill_source(client, entity, source)
    return True


async def build_maps(client: TelegramClient):
    source_map: dict[int, tuple[str, list[str]]] = {}
    target_map: dict[str, Any] = {}
    all_targets = set(MAIN_TARGETS + [UNDER99_TARGET, UNDER499_TARGET, PREMIUM_TARGET,
                                      SHOPPING_TARGET])
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
    """Never-stall safety net for ingest, reclaims and stale work.

    Ingest is event-based; if the Telethon session degrades (network flap, DC
    switch, stale auth) the events silently stop while the process stays up -
    refresh_missing_sources does not help because the sources are still mapped.
    This loop therefore, every SOURCE_RESCAN_SECONDS:

      1. reclaims jobs orphaned in 'processing' (task killed mid-await),
      2. drops pending work older than MAX_JOB_AGE_HOURS (dead loot is never
         posted late), and
      3. re-scans every live source's recent messages and enqueues anything
         new (the queue dedups on the canonical (chat_key,msg_id) index, so
         re-seeing a post is free and can never create a second post).

    Sources are scanned CONCURRENTLY so a 20-source list costs one short round
    trip instead of minutes, which is what makes a recovered deal go out
    seconds after the cycle rather than a quarter of an hour later.

    NOTE: LAST_INGEST_AT is deliberately NOT refreshed here - it measures the
    live event stream, so INGEST SILENCE keeps reporting a degraded session.
    """
    sem = asyncio.Semaphore(max(1, SOURCE_RESCAN_CONCURRENCY))
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.5, SOURCE_RESCAN_SECONDS))
            break
        except asyncio.TimeoutError:
            pass
        try:
            reclaimed = await store.reclaim_stuck_jobs()
            if reclaimed:
                log.warning("RECLAIMED | %s stuck processing job(s) returned to the queue", reclaimed)
        except Exception as exc:
            log.warning("RECLAIM failed: %s", exc)
        with contextlib.suppress(Exception):
            await store.drop_stale_jobs()
        # Scan window: at least two cycles plus slack, so a slipped cycle can
        # never leave an uncovered gap. Re-seeing a post is free (dedup).
        window_start = time.time() - (SOURCE_RESCAN_SECONDS * 2 + 180)
        items: list[tuple[str, int]] = []
        seen: set[str] = set()
        for chat_id, (source, _targets) in list(source_map.items()):
            if source in seen:
                continue
            fetch_id = RESOLVED_SOURCE_IDS.get(source)
            if not fetch_id and str(chat_id).startswith("-100"):
                fetch_id = chat_id
            if not fetch_id:
                continue
            seen.add(source)
            items.append((source, int(fetch_id)))

        async def scan_one(source: str, chat_id: int) -> int:
            recovered = 0
            try:
                async with sem:
                    messages = await client.get_messages(chat_id, limit=SOURCE_RESCAN_LIMIT)
                for msg in reversed(messages or []):
                    stamp = msg.date.timestamp() if getattr(msg, "date", None) else 0
                    if stamp < window_start:
                        continue
                    # Cheap pre-filter: a message already in the queue never even
                    # reaches the fingerprint/normalisation work in enqueue.
                    if await store.seen_message(chat_id, msg.id):
                        continue
                    raw = (msg.message or msg.text or "") if msg is not None else ""
                    has_media = bool(
                        msg is not None and msg.media is not None
                        and not isinstance(msg.media, (MessageMediaWebPage, MessageMediaInvoice))
                    )
                    if not extract_urls(msg) and not has_media and not getattr(msg, "reply_to", None):
                        continue
                    if await store.enqueue(chat_id, msg.id, source, raw, has_media):
                        recovered += 1
                        log.info("RECOVERED | source=%s msg=%s (event stream gap)", source, msg.id)
            except Exception as exc:
                log.warning("RESCAN failed source=%s: %s", source, exc)
            return recovered

        results = await asyncio.gather(
            *(scan_one(source, chat_id) for source, chat_id in items),
            return_exceptions=True,
        ) if items else []
        recovered_total = sum(r for r in results if isinstance(r, int))
        silence_min = (time.time() - LAST_INGEST_AT) / 60
        if silence_min >= 30:
            log.warning(
                "INGEST SILENCE | no source events for %.0f min; the %ss rescan keeps deals flowing",
                silence_min, int(SOURCE_RESCAN_SECONDS),
            )
        if recovered_total:
            log.info("RESCAN | cycle=%ss limit=%s recovered=%s sources=%s",
                     int(SOURCE_RESCAN_SECONDS), SOURCE_RESCAN_LIMIT, recovered_total, len(items))


def media_is_too_large(msg) -> bool:
    """True when the source media would tie up a worker longer than it is worth."""
    limit = int(MAX_MEDIA_MB * 1024 * 1024)
    if limit <= 0:
        return False
    media = getattr(msg, "media", None)
    if media is None:
        return False
    sizes: list[int] = []
    for name in ("video", "document", "voice", "round_message", "sticker", "gif", "file"):
        item = getattr(media, name, None)
        if item is not None:
            with contextlib.suppress(Exception):
                sizes.append(int(getattr(item, "size", 0) or 0))
    with contextlib.suppress(Exception):
        photo_sizes = getattr(media, "sizes", None) or []
        sizes.append(max((int(getattr(p, "size", 0) or 0) for p in photo_sizes), default=0))
    sizes = [size for size in sizes if size > 0]
    return bool(sizes) and max(sizes) > limit


def media_list(media) -> list[str]:
    """One path, a list of paths or nothing - all three arrive here; deliver as a list."""
    if not media:
        return []
    if isinstance(media, (list, tuple)):
        return [str(x) for x in media if x]
    return [str(media)]


async def album_messages(client, msg) -> list:
    """Every message of the source album, in the order the source posted them.

    A loot channel sending six product photos does NOT send six posts: it sends one
    album, and Telethon exposes the siblings through `grouped_id`. A client or a message
    without that support is just [msg], so a normal single-photo post keeps the exact
    code path it always had.
    """
    primary = [msg]
    if not getattr(msg, "grouped_id", None):
        return primary
    try:
        siblings = list(await msg.get_grouped_items())
    except Exception as exc:  # noqa: BLE001 - a missing lookup must never lose the post
        log.info("ALBUM | sibling lookup failed (%s); posting the photo we have", exc)
        return primary
    photos = [m for m in siblings if getattr(m, "photo", None) is not None]
    photos.sort(key=lambda m: getattr(m, "id", 0) or 0)
    if not photos:
        return primary
    if not any(getattr(m, "id", None) == getattr(msg, "id", None) for m in photos):
        photos.insert(0, msg)
    return photos[:max(1, MAX_ALBUM_PHOTOS)]


async def download_album(client, msg, base_target, queue_id) -> tuple[list[str], list]:
    """Download the album's photos with the same per-file budget as a single photo, and
    keep each photo's Telegram reference.

    A photo that is slow or missing is LEFT OUT rather than stalling the deal: six
    images are nicer than one, but a deal that goes out late is no deal at all. The
    references are what let the target post go out as one grid without re-uploading a
    single byte, which is the difference between "posted now" and "posted after six
    uploads".
    """
    paths: list[str] = []
    refs: list = []
    for index, item in enumerate(await album_messages(client, msg)):
        target = base_target if index == 0 else Path(f"{base_target}_{index}")
        try:
            saved = await asyncio.wait_for(
                client.download_media(item, file=str(target)),
                timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("MEDIA SLOW | queue=%s album item %s exceeded %ss; posting the rest",
                        queue_id, index, int(MEDIA_DOWNLOAD_TIMEOUT_SECONDS))
            saved = None
        except Exception as exc:  # noqa: BLE001 - never fail a post over one image
            log.warning("MEDIA FAILED | queue=%s album item %s: %s", queue_id, index, exc)
            saved = None
        if saved:
            paths.append(str(saved))
            refs.append(getattr(item, "photo", None))
    return paths, refs


def _random_id() -> int:
    import random
    return random.randrange(1, 2 ** 63)


async def send_media_group(client, entity, caption: str, paths: list[str], refs: list) -> bool:
    """Post the photos as ONE album - the grid a source channel's post shows.

    Telethon 1.44 has no `send_media_group` helper, so the raw `SendMultiMediaRequest`
    is used. When the source photo references are still usable nothing is uploaded at
    all; otherwise each file is uploaded first. False means "this client or Telegram
    would not do the grid", and the caller then sends plain files - a photo layout is
    never worth a missing deal.
    """
    try:
        from telethon import functions, types as tl_types
        medias: list = []
        if refs and len(refs) == len(paths) and all(ref is not None for ref in refs):
            medias = [tl_types.InputMediaPhoto(id=ref) for ref in refs]
        else:
            peer = await client.get_input_entity(entity)
            for path in paths:
                handle = await client.upload_file(path)
                uploaded = await client(functions.messages.UploadMediaRequest(
                    peer, media=tl_types.InputMediaUploadedPhoto(file=handle)))
                photo = getattr(getattr(uploaded, "media", None), "photo", None)
                if photo is None:
                    return False
                medias.append(tl_types.InputMediaPhoto(id=photo))
        await client(functions.messages.SendMultiMediaRequest(
            entity,
            multi_media=[tl_types.InputSingleMedia(
                media=media, message=caption if index == 0 else "", random_id=_random_id())
                for index, media in enumerate(medias)],
        ))
        return True
    except Exception as exc:  # noqa: BLE001 - the caller falls back to single files
        log.warning("ALBUM | grouped send not possible (%s); sending the photos as usual", exc)
        return False


async def send_media_item(client, entity, media, caption: str, refs: list | None = None) -> None:
    """One photo with its caption, or the whole album as one grid, with real fallbacks.

    If the grid cannot be sent, the first photo goes out with the caption - exactly what
    the bot always did - and the remaining photos follow as their own messages, so the
    reader still gets every image the source posted.
    """
    paths = media_list(media)
    if len(paths) <= 1:
        await client.send_file(entity, paths[0] if paths else None, caption=caption,
                               parse_mode=None)
        return
    if await send_media_group(client, entity, caption, paths, list(refs or [])):
        return
    await client.send_file(entity, paths[0], caption=caption, parse_mode=None)
    for extra in paths[1:]:
        with contextlib.suppress(Exception):
            await client.send_file(entity, extra, parse_mode=None)


def channel_header_line() -> str:
    return f"\U0001f449 All Loot Channels: {OUR_FOLDER_LINK}"


def keep_source_spacing(text: str, raw_text: str) -> str:
    """Layout is part of "exactly like the source", so a blank line WE created goes.

    A line our own passes deleted - a foreign channel's share button, a stripped CTA
    clause, a link cut because it never resolved - used to stay behind as an empty line,
    and subscribers read that as the bot adding space that the source never wrote. If the
    source had no blank line, neither does our copy; where the source DID space its blocks
    apart, that spacing is the source's own and is kept exactly as it was.
    """
    if not text or not raw_text:
        # No source text to compare against (an older queue row) means no licence to
        # re-flow anything: leave the layout exactly as the renderer wrote it.
        return text
    if "\n\n" in raw_text:
        return text
    return re.sub(r"\n{2,}", "\n", text)


def prepend_channel_header(text: str) -> str:
    """The one-line family link on top, never a second time, never on a post that
    already carries it. Nothing else is added: the deal text below stays the source's."""
    body = (text or "").strip("\n")
    if OUR_FOLDER_LINK in body:
        return body
    return f"{channel_header_line()}\n{body}" if body else channel_header_line()


def outbound_parts(text: str, media_path: str | None) -> list[tuple[str, str]]:
    """Build deterministic chunks so a retry can resume after the last sent part."""
    if media_list(media_path):
        caption_parts = chunks(text, 1024)
        parts: list[tuple[str, str]] = [("file", caption_parts[0])]
        remainder = "\n".join(caption_parts[1:]).strip()
        parts.extend(("message", part) for part in (chunks(remainder, 4096) if remainder else []))
        return parts
    return [("message", part) for part in chunks(text, 4096)]


async def deliver(client, entity, text: str, media_path: str | None, start_chunk: int = 0,
                  progress_callback=None, link_preview: bool = True,
                  media_refs: list | None = None) -> tuple[bool, str]:
    # LAST GATE: markdown debris, glued random tokens and empty bullet lines are
    # removed here - after cleaning, after shortening, after chunking decisions -
    # so whatever produced them can never reach a subscriber's screen.
    parts = outbound_parts(sanitize_outbound_text(text), media_path)
    if start_chunk > len(parts):
        return False, "invalid chunk checkpoint"
    for index in range(start_chunk, len(parts)):
        kind, content = parts[index]
        sent = False
        last_error = "unknown send failure"
        for attempt in range(POST_RETRIES):
            try:
                if kind == "file":
                    await send_media_item(client, entity, media_path, content, media_refs)
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
class StoredSourceMessage:
    """Stand-in for a source message Telegram will not hand back.

    A loot channel deletes or re-posts some of its own messages within seconds. Until
    now that killed the deal HERE: render_job refused to render without the live message,
    the job burned JOB_MAX_ATTEMPTS on a message that was never coming back, and a post
    intake had already received in full disappeared - the one thing the pipeline is not
    allowed to do. Intake keeps the text it was given (`queue.source_text`), so the post
    is now rendered from our own copy of it.

    Nothing is invented for the media: those photos really are gone, so `.media` stays
    None and every media branch of the pipeline remains exactly as unreachable as the
    message itself. The deal text, its links and its layout all survive.
    """

    def __init__(self, text: str, chat_id: int, msg_id: int, date):
        self.id = msg_id
        self.chat_id = chat_id
        self.text = text
        self.message = text
        self.entities: list = []
        self.reply_markup = None
        self.reply_to = None
        self.media = None
        self.photo = None
        self.document = None
        self.grouped_id = None
        # `.date` is a datetime everywhere else in the pipeline (backfill and rescan both
        # call msg.date.timestamp()), so hand back the intake timestamp as one rather than
        # a bare float that would only work as long as nobody reads it.
        self.date = (datetime.fromtimestamp(date, tz=timezone.utc)
                     if isinstance(date, (int, float)) and date else date)


async def render_job(client, affiliate: AffiliateClient, row: sqlite3.Row):
    # v18.3: whether the source message is still fetchable decides media and entities,
    # never WHETHER the post exists. A row whose own copy of the text is intact renders.
    stored_copy = (row["source_text"] if "source_text" in row.keys() else "") or ""
    fetch_error = None
    try:
        msg = await client.get_messages(row["chat_id"], ids=row["msg_id"])
    except Exception as exc:  # noqa: BLE001 - judged below, never swallowed here
        fetch_error, msg = exc, None
    if not msg:
        transient = isinstance(fetch_error, (FloodWaitError, ConnectionError, TimeoutError,
                                             asyncio.TimeoutError, OSError))
        # A rate limit or a broken connection says nothing about the post, and the photos
        # may still be there one retry later - so keep trying while the job has attempts
        # left, and only then fall back to the copy without media. A message that simply
        # is not there any more (Telegram returned nothing) is never coming back, so that
        # one renders from our own copy immediately.
        if transient and int(row["attempts"] or 0) + 1 < JOB_MAX_ATTEMPTS:
            raise fetch_error
        if stored_copy.strip():
            log.warning(
                "SOURCE MESSAGE GONE | queue=%s chat=%s msg=%s - rendering from the copy "
                "intake kept (%s chars); that message's media cannot be re-attached%s",
                row["id"], row["chat_id"], row["msg_id"], len(stored_copy),
                f" (fetch raised {type(fetch_error).__name__})" if fetch_error is not None else "")
            msg = StoredSourceMessage(stored_copy, row["chat_id"], row["msg_id"], row["created_at"])
        elif fetch_error is not None:
            raise fetch_error          # nothing stored to fall back on: retry like before
        else:
            # Nothing fetched AND nothing stored (a row written by an older build):
            # still a retryable failure, not a silent drop.
            raise RuntimeError("source message no longer available")
    # IMPORTANT: Telegram entity offsets refer to the ORIGINAL text. Never run
    # footer/branding cleanup before rebuilding entities, otherwise offsets shift
    # and fragments such as `uy` / `tps://` get glued to the affiliate URL.
    # msg.message is that original text; msg.text re-renders it through the
    # client's parse mode and INSERTS markdown marks ("**bold**"), shifting every
    # offset after them - which is how "BUY - <link>" was published as "BUJYEi".
    raw_text = msg.message or msg.text or ""
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
    msg_media = getattr(msg, "media", None)
    has_media_now = bool(msg_media) and not isinstance(
        msg_media, (MessageMediaWebPage, MessageMediaInvoice))
    # Only a post that shows the deal (a photo/video of it) AND states real deal terms
    # qualifies. A caption with no price, no discount and no card/service offer is a
    # screenshot, not a deal - publishing that is exactly the "unwanted text" the user
    # forbids, so it keeps the skip it always had.
    link_free_candidate = (not source_urls or not has_merchant_candidate) and has_media_now and (
        parse_price(text) is not None or parse_discount(text) is not None
        or has_card_offer(text) or has_service_offer(text))
    had_social_promo = bool(TRICK_PROMO_LINK_RE.search(raw_text)) or any(
        in_domains((urlparse(url).hostname or "").lower(), NON_STORE_DOMAINS)
        for url in source_urls
    )
    # Any source-only Telegram/WhatsApp advertisement is rewritten to our own
    # folder/main-channel promo and routed to Tricks; foreign join links never survive.
    if had_social_promo and not has_merchant_candidate:
        return await render_trick_promo(row, msg, raw_text)

    # A photo/video post with no link is still a post the source published, and the
    # reader can still see the product: the old `no URLs` skip deleted those. Such a post
    # now goes out exactly as the source wrote it (text + media, no invented link), because
    # "source lo vasthundi, mana target lo raledu" is the bug and a missing link is not a
    # reason for it. A TEXT-only message with nothing to buy stays skipped.
    link_free_post = bool(link_free_candidate)
    if not source_urls and not link_free_post:
        raise PermanentSkip("no URLs")

    # Resolve first so social/footer URLs do not inflate the 2+ Bitly threshold.
    # Every remaining merchant candidate is atomic: if even one cannot be freshly
    # converted, the whole post is retried/skipped rather than posting a partial deal.
    # Every source shortener is expanded CONCURRENTLY: a mega list used to pay
    # one 18s hop chain per link, serially, before anything could be posted.
    candidates: list[tuple[str, str]] = []
    resolved_list = (await asyncio.gather(
        *(affiliate.resolve(url) for url in source_urls), return_exceptions=True
    )) if source_urls else []
    for source_url, resolved in zip(source_urls, resolved_list):
        if isinstance(resolved, BaseException):
            log.warning("RESOLVE failed %s: %s", source_url[:60], resolved)
            resolved = source_url
        host = (urlparse(str(resolved)).hostname or "").lower()
        if is_share_intent(resolved) or in_domains(host, NON_STORE_DOMAINS):
            # A share button in the source text is content, not a destination: it is
            # left in the post (minus the cleaning that already handles it) but it can
            # never hold up or cancel the deal next to it.
            continue
        candidates.append((source_url, str(resolved)))
    if not candidates and not link_free_post:
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
    if not store_candidates and not service_pairs and not link_free_post:
        raise PermanentSkip("no monetizable URLs")

    multi_link = len(store_candidates) >= 2
    # Conversion is the slow step (shortener hops + EarnKaro + health checks),
    # so a multi-link post converts every product at once. EK_SEM still caps the
    # pressure on the API and `converted` keeps source order.
    converted: list[LinkResult] = []
    transient_errors: list[str] = []
    convert_results = (await asyncio.gather(
        *(affiliate.convert(url, multi_link, resolved) for url, resolved in store_candidates),
        return_exceptions=True,
    )) if store_candidates else []
    # Links the affiliate network simply cannot monetize (no campaign for that
    # store, e.g. Myntra/Ajio/Meesho, or an expired Flipkart program) are NOT a
    # transient failure. The old code retried them ten times and then threw the
    # whole post away - that is how source posts silently went missing. They are
    # kept as clean merchant links instead: foreign tracking/affiliate params are
    # stripped by merchant_url() and the link is registered as one we produced.
    passthrough: list[tuple[str, str]] = []

    def keep_passthrough(source_url: str, resolved: str) -> None:
        if not PASSTHROUGH_UNMONETIZED:
            return
        host = (urlparse(str(resolved or "")).hostname or "").lower()
        # Subdomain-aware (www.myntra.com must count as myntra.com).
        if not host or not in_domains(host, KNOWN_MERCHANT_DOMAINS):
            return
        clean_resolved = clean_url(merchant_url(resolved))
        if not clean_resolved or clean_resolved in {clean_url(r) for _, r in passthrough}:
            return
        passthrough.append((source_url, clean_resolved))
        # USER RULE (2026-09-05, "idi manvena?" / "mana links matharem"): a
        # passthrough link is a CLEAN merchant link, not a monetized link of
        # ours - the deal still goes out (that is the point), but it must be
        # obvious in the log which posts earn nothing, instead of quietly
        # looking like every other post. WARNING, not INFO, so an operator
        # grepping the log can actually find them.
        log.warning("UNMONETIZED LINK | queue=%s this deal posts a plain merchant "
                    "link (the affiliate network had no campaign for it): %s",
                    row["id"], clean_resolved[:70])
        log.info("PASSTHROUGH | queue=%s unmonetizable link kept clean: %s",
                 row["id"], clean_resolved[:70])

    unresolvable: list[str] = []
    for (source_url, resolved), result in zip(store_candidates, convert_results):
        if isinstance(result, BaseException):
            transient_errors.append(str(result))
            log.warning("CONVERT failed %s: %s", source_url[:60], result)
        elif result:
            converted.append(result)
        else:
            resolved_host = (urlparse(resolved).hostname or "").lower()
            before = len(passthrough)
            keep_passthrough(source_url, resolved)
            if len(passthrough) > before:
                continue
            if (is_unresolvable_short_link(resolved_host, source_url)
                    and clean_url(source_url) == clean_url(str(resolved or ""))):
                # Nothing resolved: the ONLY reason the whole deal used to disappear for
                # a "₹85 https://bit.ly/xxxx" post. A dead short link is permanent, so
                # neither retry nor skip is right - cut the link, keep the post.
                unresolvable.append(source_url)
                log.warning("LINK DROPPED | queue=%s short link never resolves, posting the "
                            "deal without it: %s", row["id"], source_url[:70])
                continue
            # Unknown destination: never publish an unvetted domain, retry.
            transient_errors.append(
                f"affiliate conversion returned no link: {resolved_host or 'unknown'}"
            )
    if transient_errors and row["attempts"] + 1 < JOB_MAX_ATTEMPTS:
        # A temporary API/Bitly/network failure must retry the whole job so a
        # monetizable link is not silently lost.
        raise RuntimeError("conversion retry required: " + "; ".join(transient_errors[:3]))
    if transient_errors:
        # Final attempt: publish the clean merchant destination for every link
        # the API never answered instead of dropping the deal. "Source has it,
        # our channel does not" was the bug; a slightly-less-monetized post is
        # the fix (only trusted merchant domains qualify).
        already = {clean_url(r.source) for r in converted}
        log.warning("DEGRADED POST | queue=%s attempt=%s %s link(s) unresolved: %s",
                    row["id"], row["attempts"] + 1, len(transient_errors),
                    "; ".join(transient_errors[:2])[:180])
        for source_url, resolved in store_candidates:
            if clean_url(source_url) not in already:
                keep_passthrough(source_url, resolved)
    if (not converted and not service_pairs and not passthrough
            and not unresolvable and not link_free_post):
        raise PermanentSkip("no monetizable URLs")

    # Preserve distinct variant links (colour/gender/size/category filters) inside
    # one source post. Collapse only an exact repeated generated URL. Product-key
    # reservation still provides global 10-hour dedup across source channels.
    converted = distinct_affiliate_results(converted)
    keys = list(dict.fromkeys(
        [product_key(resolved) for _, resolved in service_pairs]
        + [product_key(resolved) for _, resolved in passthrough]
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
    passthrough = [(s, r) for s, r in passthrough if product_key(r) in allowed]
    # Service-only posts have an empty `converted` by design (their links are
    # pass-through, already reserved via their product keys above) — only a
    # STORE-only post can be fully duplicated this way.
    if (not converted and not service_pairs and not passthrough
            and not unresolvable and not link_free_post):
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
    # Pass-through store links: swap the source shortener for the clean merchant
    # destination and register the exact pair so the provenance gate vouches for it.
    for source_url, resolved in passthrough:
        with contextlib.suppress(Exception):
            await store.remember_passthrough(clean_url(source_url), resolved, product_key(resolved))
        for variant in dict.fromkeys(v for v in (source_url, clean_url(source_url)) if v):
            if variant in rendered:
                rendered = rendered.replace(variant, resolved)
    for _source_url, resolved in passthrough:
        if resolved not in rendered:
            rendered = (rendered.rstrip() + "\n" + resolved).strip()
    # Service offer links: show the resolved clean destination (a zom.to short
    # link becomes the real offer page) and ensure each one is present.
    for source_url, resolved in service_pairs:
        clean_source = clean_url(source_url)
        if clean_source in rendered:
            rendered = rendered.replace(clean_source, resolved)
    for _source_url, resolved in service_pairs:
        if resolved not in rendered:
            rendered = (rendered.rstrip() + "\n" + resolved).strip()
    if row["source"] in TRICKS_SOURCES or (had_social_promo and ADD_OUR_CHANNEL_FOOTER):
        rendered = f"{rendered.rstrip()}\n\n{tricks_footer()}"
    rendered = tidy_post(rendered)
    multi_product_list = len(converted) >= 3
    if multi_product_list:
        rendered = format_clustered_product_list(rendered)
    rendered = strict_orphan_token_cleanup(rendered)
    # Cut the dead short links out of the copy itself. Leaving them in would publish a
    # foreign, unverified link (the provenance rule); removing them must not remove the
    # deal, so a post whose only link was dead goes out as complete text instead of
    # being skipped - "source lo vasthundi, mana target lo raledu" is the bug, a dead
    # shortener is not a reason for it.
    for dead_link in unresolvable:
        for variant in dict.fromkeys(v for v in (dead_link, clean_url(dead_link)) if v):
            rendered = rendered.replace(variant, " ")
    if unresolvable:
        rendered = sanitize_outbound_text(tidy_post(clean_source_text(rendered)))
    if not rendered or (not URL_RE.search(rendered) and not unresolvable and not link_free_post):
        raise PermanentSkip("rendered post has no affiliate URL")

    discount = parse_discount(text, price)
    card_offer = has_card_offer(text)
    list_prices = extract_deal_prices(text) if multi_product_list else []
    routing_price = max(list_prices) if list_prices else price

    # Card / bank offers: user wants them on EVERY owned channel at once plus
    # the premium channel, with the Loots Family folder link appended. These
    # are the strongest, most time-sensitive deals, so fanning out is correct.
    if card_offer and ADD_OUR_CHANNEL_FOOTER and OUR_FOLDER_LINK not in rendered:
        # "nothing of ours in the post" - the folder link is our own promo text,
        # so it only rides along when the operator asks for it. The card offer
        # itself is published complete either way.
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
    if passthrough:
        allowed_final_urls.update(resolved for _, resolved in passthrough)
    # GUARANTEE: the source's link is gone, only OUR link remains. A single
    # leftover foreign/shortener link used to throw the whole deal away
    # ("foreign URL survived") - that is how posts silently went missing. The
    # offending link is now repaired out of the post; we still refuse to publish
    # something that carries no verified link at all.
    leftover = [raw for raw in dict.fromkeys(URL_RE.findall(rendered))
                if clean_url(raw) not in allowed_final_urls]
    if leftover:
        for raw in leftover:
            rendered = rendered.replace(raw, " ")
        rendered = tidy_post(clean_source_text(rendered))
        log.info("LINK REPAIR | queue=%s removed %s unverified link(s): %s",
                 row["id"], len(leftover), ", ".join(u[:40] for u in leftover[:4]))
        # The source's own layout is the reference, and it lives on the row (v18.2) -
        # reading a column that is not there would crash the repair and lose the post.
        rendered = keep_source_spacing(rendered, row["source_text"] if "source_text" in row.keys() else "")
    if (not any(clean_url(raw) in allowed_final_urls for raw in URL_RE.findall(rendered))
            and not unresolvable and not link_free_post):
        raise PermanentSkip("no verified affiliate link survived in the rendered post")
    if unresolvable and not URL_RE.search(rendered):
        # Every link this post carried was a dead shortener, so the deal goes out as
        # complete text. Refusing it here would repeat the old sin: the source has the
        # post, our channel does not.
        log.warning("LINK-FREE POST | queue=%s posted complete, without a link "
                    "(source's short link no longer resolves)", row["id"])

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
            # 3+ product lists fan out to the curated non-Tricks destinations, and
            # POWER + PREMIUM take every list (their audience expects roundups).
            # The user's rule for the price channels (Aug 2026): a LIST roundup
            # belongs in BOTH of them whatever the item prices are - "under 99
            # channel lo under 99 products mariyu list of products undali", and the
            # under-499 channel pulls its picks from all sources. Single deals stay
            # band-checked, and dedup is untouched: every channel still receives a
            # given post exactly once, so this cannot double-post.
            for price_channel in (UNDER99_TARGET, UNDER499_TARGET):
                if price_channel not in base_targets:
                    base_targets.append(price_channel)
            for target in (POWER_FILTER_TARGET, PREMIUM_TARGET):
                if target not in base_targets:
                    base_targets.append(target)
            premium_rank = max(1, premium_score(text, routing_price, discount))
        else:
            # "powerloots lo anni cheyali" - PowerLoots1 takes EVERY deal the
            # pipeline accepts, from every non-Tricks source (the old
            # <=₹499 / >=70%-off filter made it silently miss posts). Dedup is
            # untouched, so this is still exactly one copy per channel.
            if POWER_FILTER_TARGET not in base_targets:
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
    # USER RULE (2026-09-05): the Amazon-Associates review channel. It takes the
    # same PRODUCT deals every other channel takes, in the programme-safe form
    # rendered at delivery time (name + price + link). A trick/recharge/app promo
    # is not a shoppable product, and a post with no buyable link cannot be
    # reviewed as one, so neither reaches it.
    if (SHOPPING_TARGET_ENABLED
            and row["source"] not in TRICKS_SOURCES
            and not service_pairs
            and URL_RE.search(rendered)
            and (not SHOPPING_AMAZON_ONLY or is_amazon_only_post(rendered))
            and SHOPPING_TARGET not in base_targets):
        base_targets.append(SHOPPING_TARGET)
    if not base_targets:
        raise PermanentSkip("no eligible targets")
    # FINAL safety net: no affiliate link may leave the post long — shorten any
    # long generated URL (Amazon category/search links with our tag, missed
    # multi-link shortens) before persisting/sending.
    rendered = await affiliate.shorten_long_urls_in_text(rendered)
    rendered = sanitize_outbound_text(rendered)
    # v17.6 numeric fidelity gate: a figure the source never printed is corruption,
    # so the NUMBER goes out of the post - never the post out of the channel.
    # The basis is the RAW source message (not the cleaned copy): cleaning may
    # drop a promo clause that sat next to a price, and the renderer legitimately
    # re-prints that price in the tidied header. Numbers the source itself never
    # printed are what gets removed - nothing else.
    try:
        rendered, fidelity_notes = enforce_numeric_fidelity(f"{raw_text}\n{text}", rendered)
        for note in fidelity_notes:
            log.info("PRICE FIDELITY | queue=%s removed %s", row["id"], note)
        rendered = rendered.strip() or ""
    except Exception as exc:  # noqa: BLE001 - a gate must never lose a live deal
        log.warning("PRICE FIDELITY check skipped (post kept as rendered): %s", exc)
    if not URL_RE.search(rendered):
        if link_free_post:
            # The SOURCE never wrote a link and the post carries the product's own
            # photo, so the reader can still see and search the deal. That post is
            # published exactly as posted (round 13's rule) - nothing was lost here.
            log.warning("LINK-FREE POST | queue=%s the source posted this with no link at all "
                        "- publishing it exactly as posted", row["id"])
        elif unresolvable and not REQUIRE_LINK_IN_POST:
            log.warning("LINK-FREE POST | queue=%s every link the source wrote was a dead "
                        "short link - posting the deal complete, without a link", row["id"])
        elif unresolvable:
            # USER RULE (2026-09-05): "asalu link yeh ledu" - a text deal with no
            # link at all is not a post our readers can use, and it earns nothing.
            # The source DID write links here; every one of them was a dead
            # shortener, so there is nothing to publish. It is skipped instead of
            # going out as a linkless teaser.
            raise PermanentSkip("every link the source wrote is dead - nothing buyable to post")
        else:
            raise PermanentSkip("rendered post has no affiliate URL after shorten pass")
    rendered = keep_source_spacing(rendered, raw_text)
    # Persist only the products actually rendered/reserved; never extend the
    # dedup timestamp of products filtered as already-posted.
    commit_keys = list(new_keys)
    if content_key:
        commit_keys.append(content_key)
    await store.save_render(
        row["id"], rendered, list(dict.fromkeys(base_targets)), commit_keys, premium_rank
    )
    if product_identities(text) or commit_keys:
        # Remember what products this row stands for, so a better copy arriving
        # later can find (and take over) this pending job.
        await store.remember_products(row["id"], sorted(set(product_identities(text))
                                                         | comparable_product_ids(commit_keys)))
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
    # a list of files (with their Telegram refs) when the source posted an album
    media_path: list[str] = []
    media_refs: list = []
    successes = 0
    price = None
    try:
        # A channel we publish to is never a source. The user added the new
        # channel's own link as a source; without this the bot re-reads its own
        # post and publishes it again, forever.
        if is_own_channel_source(row["source"]):
            raise PermanentSkip(
                f"@{row['source']} is our own channel - not a deal source")
        if row["rendered_text"]:
            # The post is already rendered and stored: this fetch only sharpens the
            # residue cleaning and re-attaches media. A source message that has since
            # been deleted must not abort a job that is ready to send, so a failed
            # lookup degrades to "no message" - which the branches below already treat
            # as "post the stored copy, without media".
            msg = None
            try:
                msg = await client.get_messages(row["chat_id"], ids=row["msg_id"])
            except Exception as exc:  # noqa: BLE001 - optional enrichment, never a blocker
                log.info("SOURCE MESSAGE GONE | queue=%s fetch raised %s - posting the stored copy",
                         row["id"], type(exc).__name__)
            rendered = row["rendered_text"]
            if await store.has_partial_delivery(row["id"]):
                # At least one chunk is already out. Re-cleaning would shift the
                # chunk boundaries and the resumed part would post a duplicated
                # or half-eaten line, so finish this job with the stored bytes.
                pass
            else:
                # Nothing sent yet: re-clean persisted pending work so a hotfix
                # applies to posts queued by an older formatter before delivery,
                # then freeze the result so every target gets identical text.
                if msg:
                    rendered = remove_source_url_residue(
                        rendered, extract_urls(msg), URL_RE.findall(rendered)
                    )
                rendered = tidy_post(clean_source_text(rendered))
                rendered = format_clustered_product_list(rendered)
                rendered = strict_orphan_token_cleanup(rendered)
                rendered = sanitize_outbound_text(rendered)
                if rendered != row["rendered_text"]:
                    with contextlib.suppress(Exception):
                        await store.update_rendered(row["id"], rendered)
            if not await store.claim_content_key(row["id"], content_deal_key(rendered)):
                raise DuplicateDeal("duplicate pending content")
            price = parse_price(msg.message or msg.text or "") if msg else None
        else:
            msg, rendered, price = await render_job(client, affiliate, row)
        # v17.8 ONE PRODUCT, ONE CHANNEL, ONE POST. Two sources can carry the same
        # product through two different short links, and until a link is resolved
        # the merchant ids look different - which is how a channel got the same
        # earphones twice. The product's own words (plus the merchant host and any
        # size/model token) settle it, and a repeat is skipped per channel. A
        # strictly better copy still posts, so coverage is never traded away.
        product_sig = product_signature(rendered)
        target_discount = parse_discount(rendered, price)
        if msg and getattr(msg, "media", None) and not isinstance(msg.media, (MessageMediaWebPage, MessageMediaInvoice)):
            if media_is_too_large(msg):
                log.info("MEDIA SKIPPED | queue=%s oversized media; posting the deal as text",
                         row["id"])
            else:
                # Parent exists and no fake .bin extension: Telethon preserves the
                # actual photo/video/document type so target posts render correctly.
                # An album (a grid of photos in one bubble) is downloaded IN FULL -
                # the source's own layout is what the target must show.
                media_target = MEDIA_DIR / f"{row['chat_id']}_{row['msg_id']}"
                media_path, media_refs = await download_album(client, msg, media_target, row["id"])
        # Final provenance gate runs again immediately before target delivery.
        owned_candidates = {OUR_FOLDER_LINK, *OUR_MAIN_CHANNEL_LINKS}
        owned_external = {url for url in owned_candidates if url in rendered}
        # A post with no URL at all has nothing to prove (this happens only when every
        # link the source wrote was a dead short link, cut above): the deal text still
        # goes out rather than being skipped.
        if URL_RE.search(rendered) and not await store.verify_generated_text(rendered, owned_external):
            raise PermanentSkip("final provenance recheck failed")
        # Owned Telegram promo links are not merchant destinations; all other
        # generated shopping links still receive the normal health check.
        merchant_health_text = rendered
        for owned_url in owned_external:
            merchant_health_text = merchant_health_text.replace(owned_url, "")
        # Share/forward links are excluded from the health verdict too: they can 4xx,
        # be bot-blocked, or be a plain "tg://" scheme, and none of that says anything
        # about whether the product page is live.
        for share_url in URL_RE.findall(merchant_health_text):
            if is_share_intent(share_url):
                merchant_health_text = merchant_health_text.replace(share_url, " ")
        if URL_RE.search(merchant_health_text) and not await affiliate.rendered_links_not_broken(merchant_health_text):
            # One dead probe used to throw the ENTIRE post away, i.e. another
            # silent missing post. Now only the broken destination is dropped;
            # the rest of the deal still goes out, and a post whose every link
            # is dead retries before it is finally refused.
            all_urls = list(dict.fromkeys(URL_RE.findall(merchant_health_text)))
            verdicts = await asyncio.gather(
                *(affiliate.link_not_broken(u) for u in all_urls), return_exceptions=True
            )
            dead = [u for u, ok in zip(all_urls, verdicts) if ok is False]
            survivors = [u for u in all_urls if u not in set(dead)]
            if dead and survivors:
                for url in dead:
                    rendered = rendered.replace(url, " ")
                rendered = sanitize_outbound_text(tidy_post(clean_source_text(rendered)))
                with contextlib.suppress(Exception):
                    await store.update_rendered(row["id"], rendered)
                log.warning("LINK REPAIR | queue=%s dropped %s dead destination(s); posting the rest",
                            row["id"], len(dead))
                rendered = keep_source_spacing(rendered, row["source_text"] if "source_text" in row.keys() else "")
            elif dead:
                # Every link answered as an explicit "this page is gone" page - and
                # THIS used to be a silent missing post, the exact complaint the user
                # brought back after v17.9 ("source lo post vasthundi, mana target
                # lo raledu"). It is not proof: Amazon and Flipkart serve 404/repair
                # pages to a datacenter IP all day long, and a link a human source
                # channel just published is far more likely alive than our probe is
                # right. A deal our readers can still use beats a suppressed one, so
                # the post goes out and the log says which link was unverified.
                if DROP_DEAD_LINKS:
                    raise PermanentSkip("every destination is a confirmed dead merchant page")
                log.warning("LINK UNVERIFIED | queue=%s %s destination(s) answered as a dead "
                            "page (our IP may simply be blocked); posting anyway",
                            row["id"], len(dead))
        premium_defer_until = None
        first_target_sent = False
        for target in await store.pending_targets(row["id"]):
            # USER RULE: Telegram never waits — no ban rule there, keep posting.
            # Every target goes back to back: a gap exists only if the operator
            # asks for one via TARGET_FANOUT_GAP_MIN/MAX (both default 0), so
            # fan-out to six channels costs nothing.
            if first_target_sent and TARGET_FANOUT_GAP_MAX > 0:
                await asyncio.sleep(random.uniform(TARGET_FANOUT_GAP_MIN, TARGET_FANOUT_GAP_MAX))
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
                if target == SHOPPING_TARGET:
                    # The review channel is an EXTRA destination. If the account
                    # cannot see it yet (not created, not joined, renamed), that
                    # is never a reason to hold a live deal back from the
                    # channels that DO exist - the job would otherwise sit in
                    # retry until the price went stale.
                    log.warning("SHOPPING TARGET UNRESOLVED | @%s not reachable; the deal "
                                "still goes to every other channel", target)
                    await store.delivery(row["id"], target, True, "shopping target unresolved")
                    continue
                await store.delivery(row["id"], target, False, "target unresolved")
                continue
            # v17.8 one product, one channel, one post: the exact merchant id can
            # differ between two sources that link the same product through
            # different shorteners, so the product's own words settle it here.
            target_skip_reason = ""
            if row["source"] not in TRICKS_SOURCES and product_sig:
                duplicate, why = await store.product_already_posted(
                    target, product_sig, price, target_discount)
                if duplicate:
                    target_skip_reason = why
                elif why:
                    log.info("BETTER COPY | queue=%s target=%s %s", row["id"], target, why)
            if target_skip_reason:
                log.info("DUPLICATE PRODUCT (signature) | queue=%s target=%s | %s",
                         row["id"], target, target_skip_reason)
                await store.delivery(row["id"], target, True, "duplicate product")
                continue
            start_chunk = await store.delivery_progress(row["id"], target)

            async def checkpoint(count: int, queue_id=row["id"], target_name=target):
                await store.set_delivery_progress(queue_id, target_name, count)

            # Power and Premium use filtering/scheduling only. Keep one clean,
            # source-derived post with no repetitive bot-added headers/footers.
            # USER RULE (round 13): our own family link on the TOP line - a reader of any
            # of our channels can reach the rest of the family instead of wandering into
            # somebody else's. It goes on the way OUT, after the stored copy is final: the
            # ledger, the auditor and the chunk-resume logic keep seeing exactly the
            # source-derived text, no cleaning pass can eat the line as "join my channel",
            # and the Tricks posts, which have their own footer, are left untouched.
            # (Toggling the knob between two chunks of ONE long post would shift the
            # boundaries, so it is an operator switch, not a per-post experiment.)
            target_text = rendered
            if target == SHOPPING_TARGET:
                # Amazon Associates review copy: product name, the source's own
                # price, our link. The loot/urgency scaffolding is dropped here,
                # at delivery, so the stored copy every other channel gets stays
                # exactly the source's. A post that has nothing left but a link
                # is not reviewable, so it is skipped for THIS channel only.
                # No shortener hop on the reviewed channel: a bit.ly link hides
                # the destination, and a reviewer who cannot see the store domain
                # cannot verify the post. Our own short links are swapped back to
                # the native store URL (a source's own short link is untouched).
                if SHOPPING_NATIVE_LINKS:
                    target_text = affiliate.expand_our_short_links(target_text)
                safe_text = affiliate_safe_text(target_text)
                if (not safe_text or not URL_RE.search(safe_text)
                        or not re.search(r"[A-Za-z]{3}", URL_RE.sub(" ", safe_text))):
                    log.info("SHOPPING SKIP | queue=%s nothing reviewable left after the "
                             "programme-safe rewrite", row["id"])
                    await store.delivery(row["id"], target, True, "not programme-safe")
                    continue
                # Off-programme store on the channel submitted for the AMAZON
                # programme: it belongs on the other channels, not here.
                if SHOPPING_AMAZON_ONLY and not is_amazon_only_post(safe_text):
                    log.info("SHOPPING SKIP | queue=%s not an Amazon-only deal", row["id"])
                    await store.delivery(row["id"], target, True, "not an amazon deal")
                    continue
                # A curated shop posts 20-30 times a day; a firehose reads as spam.
                if await store.shopping_quota_left() <= 0:
                    log.info("SHOPPING SKIP | queue=%s daily cap of %s reached",
                             row["id"], SHOPPING_DAILY_CAP)
                    await store.delivery(row["id"], target, True, "daily cap reached")
                    continue
                # Pace it like a person: the day's allowance is spread across
                # posting hours instead of fired off in one burst. The deal is
                # left PENDING (not closed), so it goes out on a later pass
                # rather than being lost - and every other channel has already
                # received it immediately, untouched by this gate.
                pace_wait = await store.shopping_pace_wait()
                if pace_wait > 0:
                    log.info("SHOPPING PACE | queue=%s holding %s min so the channel "
                             "posts like a human", row["id"], round(pace_wait / 60))
                    continue
                # REJECTION 2026-09-06: "unapproved use of Amazon trademarked
                # words, images ... or reviews". Naming the marketplace in the
                # copy is an unlicensed use of the mark; linking to it is not.
                # A post that cannot say what it means without the word is
                # simply withheld from this ONE channel.
                if has_amazon_trademark(safe_text):
                    log.info("SHOPPING SKIP | queue=%s copy names an Amazon trademark",
                             row["id"])
                    await store.delivery(row["id"], target, True, "amazon trademark in copy")
                    continue
                # The same email named t.me/LootZoneIndia11 as its example, so a
                # reviewer must find NO route from here to a loot channel.
                if has_telegram_pointer(safe_text):
                    log.info("SHOPPING SKIP | queue=%s copy points at another channel",
                             row["id"])
                    await store.delivery(row["id"], target, True, "telegram pointer in copy")
                    continue
                # Link-level disclosure is an Amazon/FTC requirement on EVERY post
                # (the channel bio alone is not enough) and a common rejection
                # reason. Added last so it can never be cleaned off again.
                target_text = add_link_disclosure(safe_text)
            # Associates compliance: our tag may only ride on the channels that
            # are declared to Amazon (see AMAZON_TAG_TARGETS). Everywhere else the
            # SAME deal posts with an untagged link.
            target_text = strip_amazon_tag_for_undeclared(target_text, target, affiliate)
            allow_preview = await store.preview_allowed(target_text)
            if (ADD_OUR_CHANNEL_LINK_TOP and row["source"] not in TRICKS_SOURCES
                    and target != SHOPPING_TARGET):
                # Never on the review channel: that header is a link into the
                # loot-channel folder, which is precisely what a marketplace
                # reviewer must not be shown.
                target_text = prepend_channel_header(target_text)
            # A forwarded deal photo is nearly always a marketplace screenshot -
            # the "images (screenshots/screen recordings)" the rejection cited.
            # The review channel goes text-only; every other channel keeps its
            # media, so no picture is lost anywhere else.
            target_media = media_path
            if target == SHOPPING_TARGET and SHOPPING_TEXT_ONLY:
                target_media = []
            ok, error = await deliver(
                client, entity, target_text, target_media,
                start_chunk=start_chunk, progress_callback=checkpoint,
                link_preview=allow_preview, media_refs=media_refs,
            )
            if premium_claimed:
                await store.complete_premium(row["id"], ok)
            await store.delivery(row["id"], target, ok, error)
            if ok and target == SHOPPING_TARGET:
                with contextlib.suppress(Exception):
                    await store.note_shopping_sent()
            if ok and product_sig:
                await store.mark_product_posted(target, product_sig, price, target_discount)
            successes += int(ok)
        await store.finish(row, successes, price, premium_defer_until)
        log.info("JOB %s | source=%s | sent=%s", row["id"], row["source"], successes)
    except DuplicateDeal as exc:
        log.info("DEDUP | queue=%s | %s", row["id"], exc)
        await store.mark_done(row["id"], str(exc))
    except PermanentSkip as exc:
        log.info("SKIP | queue=%s | %s", row["id"], exc)
        await store.mark_done(row["id"], str(exc))
        # If this row was re-pointed at a better copy of the same product and the
        # better copy cannot be posted, the original copy goes back in the queue.
        with contextlib.suppress(Exception):
            await store.restore_superseded(row)
    except Exception as exc:
        log.exception("JOB FAIL %s: %s", row["id"], exc)
        await store.fail_job(row, str(exc))
    finally:
        for leftover in media_list(media_path):
            with contextlib.suppress(Exception):
                Path(leftover).unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def idle_wait(stop: asyncio.Event) -> None:
    """Park a worker until new queue work exists instead of polling.

    The wake flag is cleared *after* a direct pending check, so a wake-up can
    never be lost in the gap between claiming and sleeping; the short timeout
    is only a safety net (and keeps shutdown responsive).
    """
    if QUEUE_WAKE is not None:
        QUEUE_WAKE.clear()
        if await store.has_pending_work():
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(QUEUE_WAKE.wait(), timeout=QUEUE_IDLE_POLL_SECONDS)
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=QUEUE_IDLE_POLL_SECONDS)


async def main() -> None:
    global QUEUE_WAKE
    log.info("BestGAA Production Bot v18.3 starting "
             "(immediate dispatch, no duplicates, verbatim-clean text)")
    QUEUE_WAKE = asyncio.Event()
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    timeout = aiohttp.ClientTimeout(total=max(15.0, HTTP_TOTAL_TIMEOUT_SECONDS * 2))
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
        # Accept any chat-id convention (marked channel, raw entity id, legacy
        # negative chat id) so a live event is never silently unmatched - an
        # unmatched event is exactly "the source posted, we didn't".
        entry = source_map.get(event.chat_id) or source_map.get(raw_chat_id(event.chat_id))
        if not entry:
            return
        try:
            source, _ = entry
            msg = event.message
            raw = (msg.message or msg.text or "") if msg is not None else ""
            has_media = bool(
                msg is not None and msg.media is not None and
                not isinstance(msg.media, (MessageMediaWebPage, MessageMediaInvoice))
            )
            added = await store.enqueue(event.chat_id, event.message.id, source, raw, has_media)
            global LAST_INGEST_AT
            LAST_INGEST_AT = time.time()
            if added:
                queue_id = await store.queue_id_for(event.chat_id, event.message.id)
                log.info("QUEUED | queue=%s priority=%s media=%s source=%s chat=%s msg=%s",
                         queue_id, classify_priority(raw, has_media), has_media, source,
                         event.chat_id, event.message.id)
            else:
                log.info("INGEST DUP | source=%s chat=%s msg=%s (already queued/posted)",
                         source, event.chat_id, event.message.id)
        except Exception as exc:
            # Never let one bad event die silently — a lost event means a lost
            # deal, so make the failure loud (the rescan loop still recovers it).
            log.error("INGEST FAIL source=%s chat=%s msg=%s: %s",
                      entry[0], event.chat_id,
                      event.message.id if event.message is not None else "?", exc)

    @client.on(events.MessageEdited())
    async def on_message_edited(event):
        """An edit of a source post must not be lost (see revive_edited_job)."""
        entry = source_map.get(event.chat_id) or source_map.get(raw_chat_id(event.chat_id))
        if not entry:
            return
        try:
            outcome = await store.revive_edited_job(raw_chat_id(event.chat_id), event.message.id)
            if outcome == "revived":
                log.info("EDIT REVIVE | source=%s msg=%s re-queued (no target had it yet)",
                         entry[0], event.message.id)
        except Exception as exc:
            log.error("EDIT FAIL source=%s chat=%s msg=%s: %s", entry[0], event.chat_id,
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
                    # Short poll, so posting restarts within seconds of 06:00
                    # instead of idling a full minute on top of the window.
                    await asyncio.wait_for(stop.wait(), timeout=15)
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
                await idle_wait(stop)
                continue
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
