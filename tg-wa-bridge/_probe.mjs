#!/usr/bin/env node
import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import os from 'node:os'
import { Readable } from 'node:stream'
import { spawn } from 'node:child_process'
import process from 'node:process'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  encodeBase64EncodedStringForUpload,
  encodeNewsletterMessage,
  generateMessageIDV2,
  prepareWAMessageMedia,
  DEFAULT_ORIGIN,
} from 'baileys'

const BASE = path.dirname(new URL(import.meta.url).pathname)
const ENV_FILE = path.join(BASE, '.env')
const STATE_FILE = path.join(BASE, 'bridge-state.json')
const STATE_BACKUP_FILE = path.join(BASE, 'bridge-state.backup.json')
const AUTH_DIR = path.join(BASE, 'auth')
const MEDIA_DIR = path.join(BASE, 'media')
const PAIR_BY_CODE = process.argv.includes('--pair')
const PAIR_BY_QR = process.argv.includes('--qr')
const PAIR_ONLY = PAIR_BY_CODE || PAIR_BY_QR
fs.mkdirSync(MEDIA_DIR, { recursive: true })

function loadEnv(file) {
  if (!fs.existsSync(file)) return
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#') || !trimmed.includes('=')) continue
    const at = trimmed.indexOf('=')
    const key = trimmed.slice(0, at).trim()
    let value = trimmed.slice(at + 1).trim()
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1)
    if (!(key in process.env)) process.env[key] = value
  }
}
loadEnv(ENV_FILE)

const required = name => {
  const value = (process.env[name] || '').trim()
  if (!value) throw new Error(`Missing ${name} in .env`)
  return value
}
const TG_TOKEN = required('TELEGRAM_BOT_TOKEN')
const WA_PHONE = required('WA_PHONE').replace(/\D/g, '')
const WA_CHANNEL = required('WA_CHANNEL')
// Optional SECOND WhatsApp Channel that receives ONLY Under-₹99 products (and
// the best-discount under-99 lists). Everything else still goes to WA_CHANNEL.
// Accepts an @newsletter JID or a https://whatsapp.com/channel/CODE invite.
// Empty = single main channel.
const WA_CHANNEL_UNDER99 = (process.env.WA_CHANNEL_UNDER99 || '').trim()
// The user runs TWO WhatsApp channels. By default the second one is the
// Under-₹99 shelf and only receives under-₹99 content. Set
// WA_CHANNEL_ALL_POSTS=true to mirror EVERY post to both channels instead
// (the under-₹99 gate is then ignored, digests included).
const CHANNEL_ALL_POSTS = (process.env.WA_CHANNEL_ALL_POSTS || 'false').toLowerCase() === 'true'
// ---------------------------------------------------------------------------
// THE USER'S CHANNEL MATRIX - each WhatsApp channel is a POLICY, not just a JID.
// Configure a channel via env and its rule applies automatically:
//   WA_CHANNEL            -> main: every curated best deal (quality gate applies)
//   WA_CHANNEL_UNDER99    -> under-₹99 products + ANY multi-product list
//   WA_CHANNEL_UNDER499   -> under-₹499 products + ANY multi-product list
//   WA_CHANNEL_BEST_OF    -> only "the best of the moment": a deal that is not a
//                            clear best-tier pick is SKIPPED on this channel
// Lists go to BOTH price channels even when their prices sit above the band
// ("list of products vachinappudu price tho sambandam lekunda 2 channels lo"), and
// credit/bank-card offers are posted wherever they are configured.
const WA_CHANNEL_UNDER499 = (process.env.WA_CHANNEL_UNDER499 || '').trim()
// The --self-test run must never rewrite the operator's live state file.
const SELF_TEST = process.argv.includes('--self-test')
const WA_CHANNEL_BEST_OF = (process.env.WA_CHANNEL_BEST_OF || '').trim()
// "aa time best ga em vundo adi post cheyali": the best-of channel posts the
// SINGLE top deal available at that moment (ten deals may be queued - only the
// winner goes there). WA_BEST_OF_COOLDOWN_SECONDS spaces the picks out if you
// want fewer of them; 0 (default) means "pick a winner for every post".
const BEST_OF_COOLDOWN_SECONDS = Math.max(0, Number(process.env.WA_BEST_OF_COOLDOWN_SECONDS || 0))
// Source fidelity (the user's rule): the channel's own hype header
// ("🔥🔥 TOP DEAL OF THE DAY 🔥🔥", "⚡️ 11 PM FLASH SALE ⚡️") is part of the post and
// is KEPT. Set WA_STRIP_CAMPAIGN_BANNERS=true only to drop those lines too.
// Branding from OTHER channels, join/follow promo, referral & app-install
// farming, CTA filler and URL residue are always removed, and links become ours.
const STRIP_CAMPAIGN_BANNERS = (process.env.WA_STRIP_CAMPAIGN_BANNERS || 'false').toLowerCase() === 'true' 
const UNDER99_MAX_PRICE = Number(process.env.WA_UNDER99_MAX_PRICE || 99)
// A LIST makes the Under-₹99 channel when it actually features under-₹99
// products AND is either a best-discount list (this % or a flagged special/
// photo list) OR is MAJORITY under-₹99 (most of its products are ≤ ₹99 — a
// list does not need every item to be ≤ ₹99, but the under-₹99 items must
// dominate or the discount must be best). Ordinary/expensive lists skip.
const UNDER99_LIST_MIN_DISCOUNT = Number(process.env.WA_UNDER99_LIST_MIN_DISCOUNT || 60)
const SOURCES = new Set((process.env.TG_SOURCE_USERNAMES || 'Under99Deals11,under499loots,LootZoneIndia11,SecretLootIndia1,PowerLoots1,Premiumlootsdeals')
  .split(',').map(x => x.trim().replace(/^@/, '').toLowerCase()).filter(Boolean))
const BUCKETS = [
  {
    source: 'under99deals11', threshold: 5, flushMin: 2, flushAfter: 30 * 60_000,
    header: '',
  },
  {
    source: 'under499loots', threshold: 5, flushMin: 2, flushAfter: 35 * 60_000,
    header: '',
  },
  {
    source: 'lootzoneindia11', threshold: 10, flushMin: 2, flushAfter: 45 * 60_000,
    header: '',
  },
]
const TZ = process.env.TZ_NAME || 'Asia/Calcutta'
// Night quiet window (IST): posting is PAUSED 02:00-06:00 (deals queue and
// flush at 06:00; ordinary night-born deals expire, only lists survive). This
// default matches the Telegram bot (POST_QUIET_START/END) so both sides are
// silent for exactly the same window even before .env is applied. Set both to
// 00:00 to disable.
const QUIET_START = process.env.QUIET_START || '02:00'
const QUIET_END = process.env.QUIET_END || '06:00'
const HYBRID_QUIET = (process.env.HYBRID_QUIET || 'false').toLowerCase() === 'true'
const STRICT_SOURCE_ONLY = (process.env.STRICT_SOURCE_ONLY || 'true').toLowerCase() === 'true'
const CURATE_TOP_DEALS = (process.env.CURATE_TOP_DEALS || 'true').toLowerCase() === 'true'
const AMAZON_TAG = process.env.AMAZON_TAG || 'deals0911-21'
const PUBLISHER_ID = process.env.EARNKARO_PUBLISHER_ID || '5478322'
const BESTGAA_DB_PATH = process.env.BESTGAA_DB_PATH || '/home/ubuntu/bestgaa-bot/bestgaa-bot/bestgaa.sqlite3'
const ROTATION_JITTER_MIN = Number(process.env.ROTATION_JITTER_MIN_SECONDS || 8)
const ROTATION_JITTER_MAX = Number(process.env.ROTATION_JITTER_MAX_SECONDS || 20)
const DIGEST_MAX_CHARS = Number(process.env.DIGEST_MAX_CHARS || 3800)
// A special/4+ link list waits this long only so the album parts / extra links
// of the SAME source post can join it. A 30-90s hold looked like "posts arrive
// late and at random"; a short settle keeps the batching without the lag.
const SPECIAL_JITTER_MIN = Number(process.env.SPECIAL_JITTER_MIN_SECONDS || 10)
const SPECIAL_JITTER_MAX = Number(process.env.SPECIAL_JITTER_MAX_SECONDS || 18)
const LARGE_LIST_MIN_LINKS = Number(process.env.LARGE_LIST_MIN_LINKS || 4)
const MAX_JOB_AGE_MS = Number(process.env.MAX_JOB_AGE_HOURS || 12) * 3600_000
// Subscriber-trust policy for stale deals (USER RULE):
//  - A deal born inside the night off-window (QUIET_START..QUIET_END, default
//    02:00-06:00 IST) is NEVER posted at the 06:00 resume — night loot is
//    dead by morning. ONLY multi-product LISTS earn the morning slot.
//  - An ordinary day deal older than WA_ORDINARY_MAX_AGE_MINUTES is dropped
//    the same way instead of being posted hours late.
const ORDINARY_MAX_AGE_MS = Number(process.env.WA_ORDINARY_MAX_AGE_MINUTES || 150) * 60_000
const MIN_WA_MESSAGE_GAP_SECONDS = Math.max(15, Number(process.env.MIN_WA_MESSAGE_GAP_SECONDS || 30))
// One post, TWO WhatsApp channels: the second channel must not pay a full
// anti-flood gap. Between the targets of the SAME broadcast (and between the
// photos of the SAME album) a short fixed gap is used - the long gap only
// applies between separate posts. Tune down only if the number is not new.
const INTER_TARGET_GAP_SECONDS = Math.max(3, Number(process.env.WA_INTER_TARGET_GAP_SECONDS || 6))
// 24/7 throughput: hour/day caps must never park the queue for hours. These
// are safety ceilings only, and are sized so a hard 60s floor stays reachable.
const HOUR_CAP_OVERRIDE = Number(process.env.WA_HOUR_CAP || 0)
const DAY_CAP_OVERRIDE = Number(process.env.WA_DAY_CAP || 0)
// The primary source the user wants represented first on WhatsApp.
const PRIMARY_SOURCE = (process.env.WA_PRIMARY_SOURCE || 'under499loots').toLowerCase()
// Media is the user's top display preference; a photo/video job may jump the
// queue when the previous update was text-only.
const MEDIA_FIRST = (process.env.WA_MEDIA_FIRST || 'true').toLowerCase() === 'true'
// Anti-starvation window: a waiting deal joins the lead tiers after this long.
const PROMOTE_AFTER_MINUTES = Math.max(10, Number(process.env.WA_PROMOTE_AFTER_MINUTES || 45))
// Newsletter (Channel) media upload path fix. Baileys <=7.0.0-rc14 uploads
// channel media to /mms/* which returns an /o1/ directPath; WhatsApp then
// silently drops the media (ack error 479) even though the send "succeeds".
const NEWSLETTER_MEDIA_FIX = (process.env.NEWSLETTER_MEDIA_FIX || 'true').toLowerCase() === 'true'
// ---------------------------------------------------------------------------
// WhatsApp groups (optional fan-out targets).
// Every verified post goes to the Channel AND to each group here. Accepts
// group JIDs (123456789-123456@g.us), full phone digits (919876543210),
// invite codes OR full invite links (https://chat.whatsapp.com/CODE).
// Empty = Channel only (previous behaviour).
// ---------------------------------------------------------------------------
const WA_GROUPS = (process.env.WA_GROUPS || '').split(',').map(x => x.trim()).filter(Boolean)
// Channel-only mode: ignore WA_GROUPS entirely (no group resolution, no group
// fan-out). Flip to false / unset to re-enable groups. Group invite resolution
// is also cached for the life of the process + persisted in state, so the
// "join invite" query is never repeated on every reconnect (which WhatsApp can
// read as group-spam and answer by disconnecting/limiting the account).
// Groups are OFF by default — the user runs the TWO WhatsApp Channels only.
// Set WA_CHANNEL_ONLY=false (and configure WA_GROUPS) to ever re-enable groups.
const CHANNEL_ONLY = (process.env.WA_CHANNEL_ONLY || 'true').toLowerCase() === 'true'
const EFFECTIVE_GROUP_TARGETS = CHANNEL_ONLY ? [] : WA_GROUPS
// ---------------------------------------------------------------------------
// Best-deal quality gate — VERIFY BEFORE SENDING, SKIP WHEN NOT A BEST DEAL.
// A post is only sent when it has a readable deal NAME and a real deal
// signal: >= WA_BEST_MIN_DISCOUNT % off, OR price <= WA_BEST_MAX_PRICE, OR a
// special (card/bank/80%+/service) offer, OR a 4+ link mega list, OR product
// media. Anything else is skipped (never sent) with the reason logged.
// ---------------------------------------------------------------------------
const BEST_DEAL_GATE = (process.env.WA_BEST_GATE || 'true').toLowerCase() === 'true'
const BEST_MIN_DISCOUNT = Number(process.env.WA_BEST_MIN_DISCOUNT || 30)
const BEST_MAX_PRICE = Number(process.env.WA_BEST_MAX_PRICE || 499)
const BEST_MIN_NAME_CHARS = Number(process.env.WA_BEST_MIN_NAME_CHARS || 6)
// ---------------------------------------------------------------------------
// ADVANCED QUALITY SCORE — "only TOP deals go to the WhatsApp channels".
// Telegram carries every source post; WhatsApp is curated, so a deal must
// earn a minimum quality SCORE across discount + price + media + usefulness,
// or be an outright best-tier post. A photo alone is NOT enough any more
// (that loophole let weak "photo deals" through), and an expensive product
// with a weak discount is rejected even with a photo. Tune with env:
//   WA_QUALITY_MIN_SCORE       total score a borderline deal needs (default 3)
//   WA_QUALITY_STRONG_DISCOUNT discount that always passes (default 70)
//   WA_QUALITY_EXPENSIVE_PRICE price above which a weak deal is a reject
//                              (default 1999)
//   WA_QUALITY_WEAK_DISCOUNT   discount below which an expensive deal is a
//                              reject (default 40)
// ---------------------------------------------------------------------------
const QUALITY_MIN_SCORE = Number(process.env.WA_QUALITY_MIN_SCORE || 3)
const QUALITY_STRONG_DISCOUNT = Number(process.env.WA_QUALITY_STRONG_DISCOUNT || 70)
const QUALITY_EXPENSIVE_PRICE = Number(process.env.WA_QUALITY_EXPENSIVE_PRICE || 1999)
const QUALITY_WEAK_DISCOUNT = Number(process.env.WA_QUALITY_WEAK_DISCOUNT || 40)
// Commission-aware ordering: high-commission merchants/categories (fashion,
// beauty, Ajio/Myntra/Nykaa) and a healthy order value rank ahead, so the
// channel maximises payout per post. Set WA_COMMISSION_RANKING=false to disable.
const COMMISSION_RANKING = (process.env.WA_COMMISSION_RANKING || 'true').toLowerCase() === 'true'
// ---------------------------------------------------------------------------
// Product-level duplicate protection: the SAME product (ASIN / product slug,
// not just the exact URL or text) must not be posted again within this window.
// Floor requested by the user: 3 hours. Default 10 hours — the same window the
// Telegram bot already uses (PRODUCT_DEDUP_SECONDS=36000), so WhatsApp and
// Telegram stay consistent.
// ---------------------------------------------------------------------------
const PRODUCT_DEDUP_HOURS = Number(process.env.WA_PRODUCT_DEDUP_HOURS || 10)
// ---------------------------------------------------------------------------
// Bitly shortening for WhatsApp display only. After provenance + health
// checks (always on the ORIGINAL link), any link longer than this is
// shortened before posting. USER RULE: long links must be shortened — the
// default is low (65) so single long merchant/affiliate links are shortened
// too, not just mega lists; a clean short amazon /dp link (~52 chars, our
// tag) is already under it and stays as-is (no quota wasted). Lists shorten
// unconditionally. With no WA_BITLY_TOKENS the tokenless is.gd fallback /
// posting-as-is guarantees a deal is never lost.
const BITLY_TOKENS = (process.env.WA_BITLY_TOKENS || '').split(',').map(x => x.trim()).filter(Boolean)
const SHORTEN_MIN_LEN = Number(process.env.WA_SHORTEN_MIN_LEN || 65)
const ALREADY_SHORT_HOSTS = new Set([
  'bit.ly', 'bitli.in', 'is.gd', 'clnk.in', 'clnk.app', 'ekaro.in', 'ekaro.app',
  'j.mp', 'cuelinks.com', 'l.ead.me', 'fktr.in', 'myntr.it', 'ajiio.in',
  'amzn.to', 'amzn.in', 'fkrt.co', 'fkrt.cc', 'fkrt.in', 'myntr.in',
  'ajiio.co', 'tinyurl.com', 'cutt.ly', 'rb.gy', 't.ly', 'tiny.cc',
  'shorturl.at', 'v.gd', 'snip.ly', 'zom.to',
])
// ---------------------------------------------------------------------------
// DIRECT SOURCE SAFETY NET: watch the bot's raw deal sources too, so a deal
// the Telegram bot skipped/missed still reaches WhatsApp. The same best-deal
// gate + product dedup apply, so anything already posted via our own channels
// is never double-posted. Our generated affiliate links keep the provenance
// check; raw source links are resolved (redirect follow) and posted as-is —
// raw Amazon product pages get our tag appended so they stay monetized.
// Requires the bridge's Telegram bot to be an admin in those channels.
// TG_DIRECT_SOURCES: unset = default list below; empty value = disabled.
// ---------------------------------------------------------------------------
const DEFAULT_DIRECT_SOURCES = 'deals,powerloot,loot_alerts,TeluguTechworld,icoolzTricks,SB_Loots_And_Deals,Flipkarthiik,HiddenDeals1,idoffers,Magixdeals_Magix,dealsvelocity,pricehistory,telugutechtvdeals,indian_online_offer,idoffers2,DealsUnder99_com,under_99_loot_deals,Mobile_phone_offers_tv_ac_deals,Mobile_phone_offers_tv_ac_dealsk,techglaredeals,iamprasadtech,hidden_loot_deals_amazon,dealdost,rapiddeals_unlimited,amazinglootsdealsoffers,realearnkaro,CKoffers,LootDealsPortal,LOOTS_DEAL_OFFER_ONLINE_SHOPPING,RealShoppingDeals,rebeldealss,Only_discount_Deals,GrabOnIndiaOfficial,myntra_Ajio_Sale_Deals_offers_li,BiggestLootDeals,lootping,Shopping_Loot_Fashion_Deals,msho_shpsy_offers,Myntra_Ajio_Deals_Shopsy,vaasutechdeals,lootsxpert'
const DIRECT_SOURCES = new Set((process.env.TG_DIRECT_SOURCES === undefined ? DEFAULT_DIRECT_SOURCES : process.env.TG_DIRECT_SOURCES)
  .split(',').map(x => x.trim().replace(/^@/, '').toLowerCase()).filter(Boolean))
// Hosts safe to follow for redirect resolution on direct-source links (known
// shorteners/redirectors only — never arbitrary merchant pages).
const REDIRECT_FOLLOW_HOSTS = new Set([
  'amzn.to', 'amzn.in', 'fkrt.co', 'fkrt.cc', 'fkrt.in', 'myntr.it', 'myntr.in',
  'ajiio.co', 'ajiio.in', 'bit.ly', 'bitli.in', 'tinyurl.com', 'cutt.ly',
  'rb.gy', 't.ly', 'tiny.cc', 'shorturl.at', 'is.gd', 'v.gd', 'snip.ly',
  'linkredirect.in', 'ekaro.in', 'clnk.in', 'clnk.app', 'ekaro.app',
  'l.ead.me', 'zom.to', 'j.mp', 'cuelinks.com', 'fktr.in',
])
// Hosts that only ever carry OUR generated affiliate links (bot output).
const OUR_LINK_HOSTS = new Set([
  'ekaro.in', 'clnk.in', 'clnk.app', 'ekaro.app', 'fktr.in', 'myntr.it',
  'ajiio.in', 'bitli.in', 'j.mp', 'cuelinks.com', 'l.ead.me', 'affiliaters.in',
  'bit.ly', 'is.gd',
])
// ---------------------------------------------------------------------------
// Service / lifestyle offers: Zomato, Swiggy, Zepto, movie tickets, quick
// commerce, payment-app and card offers. They are not EarnKaro-monetized
// store products, so their links pass through without affiliate provenance
// checks and the posts are treated as special offers (best tier).
// ---------------------------------------------------------------------------
const SERVICE_OFFER_DOMAINS = (process.env.WA_SERVICE_DOMAINS ||
  'zomato.com,zomato.in,zom.to,swiggy.com,swiggy.in,zepto.com,dominos.in,pizzahut.co.in,pizza-hut.co.in,bookmyshow.com,pvr.in,inoxmovies.com,amctheatres.in,bigcinema.com,phonepe.com,amazonpay.in,paytm.com,magicpin.in')
  .split(',').map(x => x.trim().toLowerCase()).filter(Boolean)
const log = pino({ level: process.env.LOG_LEVEL || 'info' })

const defaultState = () => ({
  telegramOffset: 0,
  jobs: [],
  sent: {},
  sentContent: {},
  sentNamePrice: {},
  sentTimes: [],
  warmupStartedAt: Date.now(),
  nextAllowedAt: Date.now() + randomMs(15, 55),
  postsUntilBreak: randomInt(7, 13),
  breakState: null,
  rotationIndex: 0,
  bucketReadyAt: {},
  burstCount: 0,
  burstThreshold: 0,
  antibanRestUntil: 0,
})
let state = loadState()
let wa = null
let waReady = false
let targetJid = null
let under99Jid = null
let under499Jid = null
let bestOfJid = null
// jid -> policy, rebuilt whenever a channel resolves (targetsFor reads it).
const CHANNEL_POLICY_OF_JID = new Map()
let groupJids = []
let shuttingDown = false

function loadState() {
  for (const file of [STATE_FILE, STATE_BACKUP_FILE]) {
    try {
      const loaded = { ...defaultState(), ...JSON.parse(fs.readFileSync(file, 'utf8')) }
      if (file === STATE_BACKUP_FILE) log.warn('Primary queue state was unreadable; recovered from backup')
      return loaded
    } catch {}
  }
  return defaultState()
}
function saveState() {
  const tmp = `${STATE_FILE}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2), { mode: 0o600 })
  if (fs.existsSync(STATE_FILE)) {
    try { fs.copyFileSync(STATE_FILE, STATE_BACKUP_FILE); fs.chmodSync(STATE_BACKUP_FILE, 0o600) } catch {}
  }
  fs.renameSync(tmp, STATE_FILE)
}
function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)) }
function randomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min }
function randomMs(minSeconds, maxSeconds) { return randomInt(minSeconds, maxSeconds) * 1000 }
async function interMessageGap() {
  await sleep(randomMs(MIN_WA_MESSAGE_GAP_SECONDS + 5, MIN_WA_MESSAGE_GAP_SECONDS + 30))
}
// Between the targets of one broadcast / the items of one album.
async function interTargetGap() {
  await sleep(randomMs(INTER_TARGET_GAP_SECONDS, INTER_TARGET_GAP_SECONDS + 5))
}
function sha(value) { return crypto.createHash('sha256').update(value).digest('hex') }
// A hung WhatsApp/Telegram promise must never freeze the dispatcher. Every
// network step is wrapped so the worker can retry instead of stalling for hours.
function withTimeout(promise, ms, label) {
  let timer
  return Promise.race([
    Promise.resolve(promise).finally(() => clearTimeout(timer)),
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out after ${Math.round(ms / 1000)}s`)), ms) }),
  ])
}
function cleanUrl(value) { return value.replace(/&amp;/gi, '&').replace(/[.,;:!?"')()\]}>]+$/g, '') }
function urlsIn(text) { return [...new Set((text || '').match(/https?:\/\/[^\s<>\[\](){}"']+/gi)?.map(cleanUrl) || [])] }
// Source-channel promo / navigation / boilerplate that is NOT part of the deal
// and must never reach the WhatsApp post. A line is noise only when it carries
// NO real deal content (no link, no price, no %, no 3+ product words). Join /
// follow / share / forward CTAs, channel self-promo, deal-time, tag-only lines,
// link-bullets and emoji-only lines are all stripped.
const PROMO_PATTERNS = [
  /t\.me\/|telegram\.(?:me|dog)\//i,
  /whatsapp\.com\/(?:channel|invite)|wa\.me\//i,
  /\bjoin\b/i,
  /\bsubscribe\b|\bfollow\s+(?:us|our)\b/i,
  /\bshare\b[^.\n]{0,40}\b(with|your|everyone|friends?|groups?|channel|family)\b/i,
  /\bforward\b[^.\n]{0,30}\b(to|our|everyone|friends?|groups?)\b/i,
  /\b(click|tap)\s+(?:here|link|below|on\s+the\s+link)\b/i,
  /\b(turn\s+on|enable|activate)\b[^.\n]{0,25}\bnotifications?\b/i,
  /\b(?:don'?t|do\s+not|never)\s+miss\b|\bmiss\s+(?:it|this|out)\b/i,
  /\bdeal\s*time\s*[:\-]/i,
  /\bgrab\s+(?:it|fast|now|your|this)\b/i,
  /\bhurry?\s*up\b/i,
  /\bstay\s+(?:tuned|connected|updated)\b/i,
  /\b(?:buy|shop|order)\s+now\b/i,
  /\bcash\s*?back\b[^.\n]{0,20}(?:@\S+|bot)\b/i,
  /[\u{1F4E2}\u{1F514}\u{1F4E3}\u{23F0}]/u,
]
// A link that is only social/channel navigation is never deal content. A line
// carrying nothing but such links is boilerplate and must go as a WHOLE line:
// partially stripping the link used to leave residue like `.com/channel/0029`
// or `.me/someotherchannel` sitting in the published post.
const PROMO_ONLY_URL_HOSTS = [
  't.me', 'telegram.me', 'telegram.dog', 'telegram.org', 'whatsapp.com', 'wa.me',
  'instagram.com', 'facebook.com', 'twitter.com', 'x.com', 'discord.com',
  'discord.gg', 'pinterest.com', 'linkedin.com', 'reddit.com', 'github.com',
  'youtube.com', 'youtu.be', 'imgur.com',
]
function isPromoOnlyUrl(value) {
  try {
    const host = new URL(cleanUrl(String(value))).hostname.toLowerCase()
    return PROMO_ONLY_URL_HOSTS.some(d => host === d || host.endsWith('.' + d))
  } catch { return false }
}
const CHANNEL_INVITE_URL_RE = /t\.me\/(?:addlist\/|joinchat\/|\+)|telegram\.me\/(?:\+|joinchat\/)|whatsapp\.com\/(?:channel|group)\//i
const PROMO_INTENT_RE = /\b(?:join|subscribe|follow|unfollow|share|forward|visit|open|check|notify|notifications?|turn\s+on|enable|activate)\b|\bfor\s+more\b|\bmore\s+(?:loot|deal|update)s?\b|\bour\s+(?:channel|group|whatsapp|telegram)\b/i
// Referral/invite farming is junk even when a ₹ amount sits in the sentence.
const REFERRAL_SPAM_RE = /\b(?:refer|invite)\s+(?:a\s+)?(?:friend|mate|user|family|one)\b|\b(?:earn|win|get)\s+(?:\u20B9|rs\.?|inr\s?)?\s*\d+\s*(?:each|per\s+user)?\s*(?:on|for|by|after|in)?\s*(?:referral|referrals|refer|invite|signup|sign\s*-?\s*up)\b|\b(?:referral|invite)\s+code\b|\binstall\s+(?:the\s+|our\s+|this\s+)?(?:app|apk)\b|\b(?:refer|invite)\b[^.\n]{0,40}\b(?:friends?|mates?|budd(?:y|ies)|users?)\b|\b(?:referral|invite|sign\s*-?\s*up|joining)\s*(?:bonus|reward|cashback|incentive)\b/i

function isPromoNoiseLine(line) {
  const t = (line || '').trim()
  if (!t) return false
  // Keep anything carrying real deal content: a genuine merchant link, a price,
  // a discount %, or a 3+ word product description -- never strip a real deal
  // line. A line made only of channel/social links is the exception: promo
  // wording plus an invite link means the whole line is boilerplate.
  const lineUrls = t.match(/https?:\/\/\S+/g) || []
  if (lineUrls.length && lineUrls.some(u => !isPromoOnlyUrl(u))) return false
  if (lineUrls.length && (CHANNEL_INVITE_URL_RE.test(t) || PROMO_INTENT_RE.test(t))) return true
  if (REFERRAL_SPAM_RE.test(t)) return true
  if (/[\u20B9$]|\b(?:rs\.?|inr|mrp)\b/i.test(t)) return false
  if (/\d+\s*%/.test(t)) return false
  // Word count for "is this a real product description?" ignores promo/CTA
  // vocabulary, so "Turn on notifications" / "Don't miss this deal" (3 raw
  // words but all CTA) are still stripped while a real 3+ word product line
  // ("Premium cotton fabric slim fit") is kept.
  const stripped = t
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/@?[\p{L}\p{N}_]*(?:t\.me|telegram|whatsapp|join|subscribe|follow|share|forward|click|tap|notification|notifications|hurry|grab|miss|deal|deals|offer|offers|loot|loots|channel|group|friends?|everyone|family|buy|shop|order|now|here|link|below|this|that|your|our|us|out|on|off|and|the|for|get|more|new|daily|fast|time|turn|enable|activate|don'?t|do|not|never)\b/gi, ' ')
  const contentWords = stripped.replace(/[^\p{L}\p{N}]/gu, ' ').split(/\s+/).filter(Boolean)
  if (contentWords.length >= 3) {
    return false
  }
  // Bare @handle / #hashtag channel tag.
  if (/^[@#][\p{L}\p{N}_]+$/u.test(t)) return true
  // Emoji/bullet/symbol-only line (no letters or digits at all).
  if (!/[\p{L}\p{N}]/u.test(t)) return true
  return PROMO_PATTERNS.some(re => re.test(t))
}
// Fully decode double/triple HTML escaping in a forwarded post (&amp;amp; ->
// &amp; -> &) so a nested copy carries clean query separators.
function unescapeAmp(text) {
  let out = String(text || '')
  for (let i = 0; i < 4; i++) {
    const next = out.replace(/&amp;/gi, '&')
    if (next === out) break
    out = next
  }
  return out
}
// Collapse FORWARDED/nested markdown link wrappers ("[[url](url)](url)") and
// double-escaped queries down to ONE clean URL per product per line.
function normalizeNestedLinks(text) {
  let out = unescapeAmp(text || '')
  for (let i = 0; i < 4; i++) {
    const prev = out
    out = out.replace(/\[\s*(https?:\/\/[^\s\]\[]+?)\s*\]\s*\(\s*https?:\/\/[^)]+?\s*\)/gi, '$1')
    out = out.replace(/\[[^\]\[]*?\]\s*\(\s*(https?:\/\/[^)]+?)\s*\)/gi, '$1')
    if (out === prev) break
  }
  out = out.replace(/[\[\]]+/g, '')
  // Markdown emphasis debris (++, **, __ and lone */_) left around collapsed
  // links is source formatting, never deal content. Strip runs of 2+ emphasis
  // chars and lone */_ everywhere; URLs are masked so legitimate '+' query
  // chars inside Amazon URLs survive untouched.
  out = out.split(/\r?\n/).map(line =>
    line.split(/(https?:\/\/[^\s<>\[\](){}"']+)/)
      // A real URL is only trimmed at its edges: t.me/addlist/… and merchant
      // paths legitimately contain "_", and deleting it turned OUR OWN folder
      // link into a dead invite. Text parts lose emphasis runs/markers.
      .map(p => /^https?:\/\//i.test(p)
        ? p.replace(/^[*_+~]+|[*_+~]+$/g, '')
        : p.replace(/[*_+~]{2,}/g, '').replace(/(?<![\w])[*_](?=\w)/g, '').replace(/(?<=\w)[*_](?![\w])/g, ''))
      .join('')
      .replace(/[ \t]{2,}/g, ' ')
      .trim()
  ).join('\n')
  return out.split(/\r?\n/).map(line => {
    // Parentheses carry real deal text ("Price ₹260 (75% OFF)") and must never
    // be shaved off a line end, so an unmatched/empty pair is repaired only on
    // lines without a link. On a URL line only the OUTER wrapper is unwrapped -
    // the inner parens of a glued forward fragment ("url1(url2(url3))") are the
    // separators the per-line dedup below counts on and must stay untouched.
    const bare = line.trim()
    const stripped = /https?:\/\//i.test(bare)
      ? bare.replace(/^[()]+|[()]+$/g, '').trim()
      : fixUnbalancedParens(bare)
    // Count RAW occurrences (not de-duplicated): the same product URL stacked
    // 2-3x in one broken markdown/forward fragment must collapse to ONE line.
    const raw = (stripped.match(/https?:\/\/[^\s<>\[\](){}"']+/gi) || [])
    if (raw.length <= 1) return stripped
    const seen = new Set(), canon = []
    for (const u of raw.map(cleanUrl)) {
      const id = productIdentity(u) || canonicalUrlKey(u) || u
      if (!seen.has(id)) { seen.add(id); canon.push(u) }
    }
    const prefix = stripped.replace(/https?:\/\/\S+/g, '').replace(/[()\[\]]+/g, '').replace(/\s{2,}/g, ' ').trim().replace(/[\s|]+$/, '')
    return ((prefix ? prefix + '\n' : '') + canon.join('\n')).trim()
  }).join('\n')
}

// ---------------------------------------------------------------------------
// Junk-token killers, mirrored from the Telegram bot so both delivery paths
// publish the same clean text: a masked-shortener fragment glued to a price
// ("₹260tG7oChgiQuTgS25b") or left on its own line, "[url](url)" markdown
// debris, an empty "()" from promo stripping. The old length-capped rules missed
// any fragment longer than 12-14 chars, which is exactly how those appeared in
// front of subscribers. WhatsApp formatting is deliberately left alone
// ("*bold*" headers are ours, only the source's broken markdown is repaired).
function stripPriceJunk(text) {
  if (!text) return ''
  // Real links are masked first: a link glued straight onto a price
  // ("₹260https://bitli.in/x") must keep its protocol - cutting "https" as if it
  // were a junk token used to leave "₹260://bitli.in/x" behind.
  const urls = [...new Set(String(text).match(/https?:\/\/[^\s<>\[\](){}"']+/gi) || [])]
  let masked = String(text)
  urls.forEach((u, i) => { masked = masked.split(u).join(`\u0002P${i}\u0003`) })
  let out = masked
    .replace(/(₹\s*[\d,]+)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{2,12}\b/g, '$1')
    .replace(/(₹\s*[\d,]+)(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{2,64}\b/g, '$1')
    .replace(/(₹\s*[\d,]+)[ \t]+(?!\d+(?:pcs?|packs?|pairs?|kg|gm?|ml|ltrs?|l|cm|mm|mah|gb|tb|w|v|inch(?:es)?)\b)(?=[A-Za-z\d]*\d)(?=\d*[A-Za-z])[A-Za-z\d]{3,64}(?=[ \t]|$)/gmi, '$1')
  urls.forEach((u, i) => { out = out.split(`\u0002P${i}\u0003`).join(u) })
  // Keep a glued link apart from the word/price in front of it.
  return out.replace(/([\w₹)\]>"'])(?=https?:\/\/)/g, '$1 ')
}
function fixUnbalancedParens(line) {
  if (!line) return line
  let out = line.replace(/\(\s*\)/g, ' ').replace(/[ \t]{2,}/g, ' ').trim()
  const count = (s, re) => (s.match(re) || []).length
  while (count(out, /\(/g) > count(out, /\)/g)) out = out.replace('(', '')
  while (count(out, /\)/g) > count(out, /\(/g)) {
    const at = out.lastIndexOf(')')
    if (at < 0) break
    out = out.slice(0, at) + out.slice(at + 1)
  }
  return out.replace(/[ \t]{2,}/g, ' ').trim()
}
function stripLinkFragmentTokens(text) {
  if (!text) return ''
  const urls = [...new Set(String(text).match(/https?:\/\/[^\s<>\[\](){}"']+/gi) || [])]
  let masked = String(text)
  urls.forEach((u, i) => { masked = masked.split(u).join('\u0001K' + i + '\u0002') })
  const lines = masked.split(/\r?\n/).map(line => {
    // A coupon/referral code is real content: never swept as "random" text.
    if (/code|coupon|kupon|voucher|referral|refer\b|promo|pin\b|deal\s*id/i.test(line)) return line
    return line
      .replace(/(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]{12,64}(?![A-Za-z0-9_-]))(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{12,64}(?![A-Za-z0-9_-])/g, '')
      .replace(/[ \t]{2,}/g, ' ')
      .trim()
      .replace(/[ \t\u279c\u27a1\u2192\u2022•➜➡🔗👉–—:]+$/u, '')
      .trim()
  })
  urls.forEach((u, i) => {
    for (let n = 0; n < lines.length; n++) lines[n] = lines[n].split('\u0001K' + i + '\u0002').join(u)
  })
  return lines.join('\n')
}
// LAST GATE for every string handed to WhatsApp (text, caption or group send).
function sanitizeOutbound(text) {
  if (!text) return ''
  let out = String(text)
  for (let i = 0; i < 4; i++) {
    const prev = out
    out = out.replace(/\[\s*(https?:\/\/[^\s\]\[]+?)\s*\]\s*\(\s*https?:\/\/[^)\s]+?\s*\)/gi, '$1')
    out = out.replace(/\[([^\]\[]*?)\]\s*\(\s*(https?:\/\/[^)\s]+?)\s*\)/gi, (_m, label, url) =>
      String(label).replace(/\s/g, '') === String(url).replace(/\s/g, '') ? url : label + ' ' + url)
    if (out === prev) break
  }
  out = stripPriceJunk(out)
  out = stripLinkFragmentTokens(out)
  // A link glued to the word/price before it prints as one unreadable token
  // ("₹260https://…"). Query-nested links stay intact: the separator must be a
  // word char, never "=", "&", "?" or "/".
  out = out.replace(/([\w\u20b9)\]>"'])(?=https?:\/\/)/g, '$1 ')
  // A line that carries a link is left exactly as it is: its parentheses may be
  // part of a real merchant path, and editing them would break the link.
  out = out.split(/\r?\n/)
    .map(line => /https?:\/\//i.test(line) ? line : fixUnbalancedParens(line))
    .join('\n')
  out = out.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n')
  return out.trim()
}
function canonicalUrlKey(url) {
  try {
    const u = new URL(url)
    const m = u.pathname.match(/(?:\/dp\/|\/gp\/(?:product|aw\/d)\/)([A-Z0-9]{10})/i)
    if (m) return 'asin:' + m[1].toUpperCase()
    return u.hostname + u.pathname
  } catch { return url }
}
// Loot channels open with a pure campaign banner ("🔥🔥 TOP DEAL OF THE DAY 🔥🔥",
// "⚡️ 11 PM FLASH SALE ⚡️") that says nothing about the product. Those lines are
// decoration and must not sit on top of our post - but they are droppable ONLY
// because a real product line exists in the same post. So the vocabulary is
// deliberately limited to generic hype words: one product/spec word keeps the
// line, and if every text line is a banner the first one stays as the headline
// (never hand the reader a wall of bare links). Mirrors _product_label /
// is_campaign_banner_line / strip_promo_lines in bestgaa/main_bot_new.py.
const BANNER_NOISE_WORDS = new Set((`
top tops best hot mega super ultra dhamaka dhamal amazing awesome superb mind
blowing daily latest new today yesterday deal deals dealz offer offers loot loots
sale sales steal stealer alert alerts save savings price prices drop drops shocker
shocking free gift gifts bonus grab hurry limited time slot hours day nights night
of the a an for you your ours only off on in india indian
weekend special weekday flash live now just don miss
`).split(/\s+/).filter(Boolean))
const TIME_OF_DAY_RE = /\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)/gi

function isCampaignBannerLine(line) {
  // Brand/store names are not in the vocabulary on purpose: "Myntra Mega Sale" is
  // a real store header (content), "TOP DEAL OF THE DAY" is hype (noise).
  const t = line || ''
  if (!t.trim() || /https?:\/\/\S+/i.test(t)) return false
  if (/[\u20B9$%]|\b(?:rs\.?|inr|mrp|discount|cod)\b/i.test(t)) return false
  let stripped = t.replace(TIME_OF_DAY_RE, ' ')
  stripped = stripped.replace(/\b(?:\d{1,2}(?:st|nd|rd|th)?|\d{1,2}\s*(?:am|pm))\b/gi, ' ')
  const words = stripped.replace(/[^\p{L}\p{N}\s]/gu, ' ').split(/\s+/).filter(Boolean)
  if (!words.length) return true // pure decoration (emoji / rules) is noise
  return words.every(word => BANNER_NOISE_WORDS.has(word.toLowerCase()))
}

// Another channel's BRANDING (its name/signature/"join for more" line) is not
// deal content and the user wants it removed, while everything that channel wrote
// ABOUT the deal - including its own hype header - stays verbatim (fidelity).
// Mirrors is_branding_line / BRANDING_* in bestgaa/main_bot_new.py: a promo verb
// or @handle plus no product word, or a "Powered by X" signature, or an ALL-CAPS
// title naming a channel brand. Generic hype words alone are never enough.
const BRANDING_LINE_RE = /\b(?:join|follow|subscribe|share|forward|turn\s+on)\b|\b(?:telegram|whatsapp)\s*(?:channel|group)?\b|\b(?:channel|group)\s*(?:name|link)?\b|\b(?:edited|posted|powered|made|managed)\s+by\b|\bfor\s+more\b|\bmore\s+(?:loots?|deals?|offers?|updates?|dhamaka)\b|\b(?:stay|keep)\s+(?:tuned|updated|connected)\b/i
const BRANDING_DEAL_EVIDENCE_RE = /[\u20B9$]|\b(?:mrp|rs\.?|inr|cod|discount|size|colou?r|pack|pcs|pair)\b/i
const BRANDING_DEAL_NUM_RE = /\d+\s*%|\b\d+\s*(?:off|days?|years?|months?|gb|tb|mah)\b/i
const HANDLE_RE = /@(?![A-Za-z]{1,3}\b)[A-Za-z][A-Za-z0-9_]{3,}/
const SIGNATURE_PREFIX_RE = /^[^\p{L}\p{N}\n]*(?:powered|edited|posted|made|managed|written|curated|created|shared|sent)\s+by\b/i
const BRANDING_NOUNS = new Set(('zone hub india official world point adda team squad daily store shop '
  + 'mart bazaar channel group telegram whatsapp admin edit edits powered managed updates update').split(/\s+/))
const BRANDING_WORDS = new Set([...BRANDING_NOUNS, ...BANNER_NOISE_WORDS,
  'more', 'for', 'with', 'and', 'our', 'us', 'on', 'in', 'the', 'a', 'an', 'by', 'at', 'to', 'of',
  'also', 'join', 'follow', 'subscribe', 'share', 'forward', 'turn', 'notifications', 'notification',
  'stay', 'tuned', 'connected', 'edited', 'posted', 'powered', 'made', 'managed', 'only', 'now',
  'here', 'this', 'that', 'new', 'best'])

function isBrandingLine(line) {
  const t = (line || '').trim()
  if (!t || /https?:\/\/\S+/i.test(t)) return false
  if (BRANDING_DEAL_EVIDENCE_RE.test(t) || BRANDING_DEAL_NUM_RE.test(t)) return false
  const words = t.replace(/[^\p{L}\p{N}\s]/gu, ' ').split(/\s+/).filter(Boolean)
  if (!words.length) return false
  const lowered = words.map(w => w.toLowerCase())
  const brandNouns = lowered.filter(w => BRANDING_NOUNS.has(w))
  if (!brandNouns.length && !HANDLE_RE.test(t)) return false
  if (HANDLE_RE.test(t) || SIGNATURE_PREFIX_RE.test(t)) return true
  const inBrandVocab = lowered.every(w => BRANDING_WORDS.has(w))
  return inBrandVocab && (BRANDING_LINE_RE.test(t) || brandNouns.length >= 2)
}

// Same never-a-wall-of-links guard as the banner rule: if the ONLY text lines are
// branding, the first one stays so the post still has a headline.
function dropBrandingLines(lines) {
  const flags = lines.map(isBrandingLine)
  if (!flags.some(Boolean)) return lines
  const hasRealText = lines.some((line, i) => line.trim() && !/https?:\/\/\S+/i.test(line) && !flags[i])
  if (hasRealText) return lines.filter((line, i) => !flags[i])
  const first = flags.findIndex(Boolean)
  return lines.filter((line, i) => !flags[i] || i === first)
}

// Drop the banner lines, keeping the FIRST one only when nothing else can act as
// the post's headline.
function dropCampaignBanners(lines) {
  const flags = lines.map(isCampaignBannerLine)
  const hasRealHeadline = lines.some((line, i) => line.trim()
    && !/https?:\/\/\S+/i.test(line) && !flags[i])
  if (hasRealHeadline) return lines.filter((line, i) => !flags[i])
  const firstBanner = flags.findIndex(Boolean)
  return firstBanner < 0 ? lines : lines.filter((line, i) => !flags[i] || i === firstBanner)
}

function cleanDealText(text) {
  const noise = /^(?:\s*(?:🔥\s*LOOT\s+ZONE\s*[—-]\s*India|🚨\s*SPECIAL\s+OFFER|✅\s*Verified\s*•\s*Enjoy\s*\(Grab\s*fast\)|.*deal\s*time\s*:.*(?:IST)?|.*\bloot\s+fa+s+\s*t+\b.*|.*(?:@GrabOnIndiaOfficial|50\+\s*loots\s*daily).*|(?:h|ht|htt|https?|ttp|ttps|tps?:\/\/|s:\/\/|:\/\/|uy)|👉.*(?:https\s*:\s*are)|💰?\s*want\s+real\s+cash\s*back\s+too\??|forward\s+to\s+@cashkarolink_?bot|#(?:myntra|flipkart|amazon|ajio))\s*)$/i
  const fragments = new Set(['h','ht','htt','http','https','ttp','ttps','tps://','tp://','s://','://','uy'])
  const meaningful = new Set(['men','mens','women','womens','unisex','blue','black','white','red','green','yellow','brown','orange','pink','purple','grey','gray','beige','gold','silver','small','medium','large'])
  const sourceLines = normalizeNestedLinks(text).replace(/[\u200b-\u200f\u2060\ufeff]/g, '').split(/\r?\n/)
  // Branding always goes; the channel's own hype header only when the operator
  // opted into WA_STRIP_CAMPAIGN_BANNERS.
  const prepared = STRIP_CAMPAIGN_BANNERS
    ? dropCampaignBanners(dropBrandingLines(sourceLines))
    : dropBrandingLines(sourceLines)
  const cleaned = prepared
    .filter(line => {
      const core = line.replace(/[^A-Za-z:/]/g, '').toLowerCase()
      const punctuationOnly = /^\s*(?:[-–—|:>]+|[👉👆]+)\s*$/.test(line)
      return !noise.test(line) && !fragments.has(core) && !punctuationOnly && !/^\s*now\s*$/i.test(line)
        && !isPromoNoiseLine(line)
    })
    .join('\n')
    .replace(/^\s*➜\s*(https?:\/\/)/gm, '$1')
    .replace(/(https?:\/\/[^\s]+)(?:[ \t]+[A-Za-z0-9_-]{2,16})+[ \t]*$/gm, '$1')
    .replace(/(₹\s*[\d,]+)(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{2,64}\b/g, '$1')
    // Random mixed letter+digit token AFTER a price ("₹185 VN7z") is source
    // corruption, never real content. Real units (2pcs, 500ml...) survive.
    .replace(/(₹\s*[\d,]+)[ \t]+(?!\d+(?:pcs?|packs?|pairs?|kg|gm?|ml|ltrs?|l|cm|mm|mah|gb|tb|w|v|inch(?:es)?)\b)(?=[A-Za-z\d]*\d)(?=\d*[A-Za-z])[A-Za-z\d]{3,64}(?=[ \t]|$)/gmi, '$1')
    .replace(/deal\s*price\s*:\s*(₹\s*[\d,]+)\s+(₹\s*[\d,]+)/gi, 'Deal Price: $1\nMRP: $2')
    .replace(/regular\s*price\s*:\s*-?\s*(₹\s*[\d,]+)/gi, 'MRP: $1')
    .replace(/^\s*(₹\s*[\d,]+)\s*\|\s*$/gm, 'Deal Price: $1')
  // A source post can carry a dangling protocol fragment next to a real link,
  // e.g. `https://ajio.in/... https://` on one line. Those bare `https://`
  // shares are source corruption, not links. Mask real URLs first so a genuine
  // generated link is never touched, drop the orphan protocol/domain fragment,
  // then restore the real URLs.
  const realUrls = [...new Set((cleaned.match(/https?:\/\/[^\s<>"]+/gi) || []).map(u => u.replace(/[.,;:!?")]>]+$/g, '')))]
  let protocolCleaned = cleaned
  realUrls.forEach((url, i) => { protocolCleaned = protocolCleaned.split(url).join(`\x01U${i}\x02`) })
  protocolCleaned = protocolCleaned
    .replace(/\bhttps?:\/\/[^\s\x01]*/gi, '')      // orphan or truncated protocol line
    // Half-stripped promo links leave TLD/protocol residue (`ps://broken`,
    // `.com/channel/0029`): real URLs are masked out above, so only residue
    // can match these.
    .replace(/[A-Za-z0-9_-]*\.(?:me|com|in|net|org|io|co|html?)\b[\\/]\S*/gi, '')
    .replace(/\b(?:https?|httpsx|ht|htt|ftp|tps|ttp|tp|ps|hs|sp)[:/ ]{0,2}[/\\]{2,}\S*/gi, '')
    .replace(/(?:\s*\b(?:https?|htt|ftp)\b)/gi, '')
    .replace(/\x01U\d+\x02/g, m => realUrls[Number(m.slice(2, -1))])
  // A source post whose duplicate links collapsed to one can leave dangling
  // link-bullet lines (e.g. `🔗` on its own). A bullet with no URL is not a
  // link and must never appear. Real URLs survive untouched.
  // Drop any line that is a bare link-bullet (optionally followed by an empty
  // or truncated http) and nothing else. Run per-line so consecutive bullets
  // are all removed, and never touch a line that carries a real URL.
  const withoutDangling = (protocolCleaned || '').split(/\r?\n/)
    .map(line => {
      const trimmed = line.trim()
      if (!trimmed) return '' // keep blank line; collapse below
      const isDangling = !/https?:\/\/\S/.test(line) &&
        /^[\s🔗👉➡️🔎🔍•▪­up‑–—*]+$/.test(trimmed.replace(/^https?:\/\/\s*$/i, '').trim())
      return isDangling ? '' : line
    })
    .join('\n')
    .replace(/\n{3,}/g, '\n\n').trim()
  const cleaned2 = withoutDangling.replace(/\n{2,}/g, '\n\n')

  const lines = cleaned2.split(/\r?\n/)
  const strict = lines.filter((line, index) => {
    const token = line.trim()
    if (!/^[A-Za-z0-9_-]{1,12}$/.test(token)) return true
    const lower = token.toLowerCase()
    const previous = index ? lines[index - 1].trim() : ''
    const next = index + 1 < lines.length ? lines[index + 1].trim() : ''
    const adjacentUrl = /^https?:\/\//i.test(previous) || /^https?:\/\//i.test(next)
    const couponContext = /use\s*code\s*:?$/i.test(previous)
    return meaningful.has(lower) || adjacentUrl || couponContext
  })
  return strict.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}
function normalizedText(text) { return cleanDealText(text).replace(/https?:\/\/[^\s]+/gi, '').replace(/\s+/g, ' ').trim().toLowerCase() }
function contentFingerprint(text) {
  const withoutUrls = cleanDealText(text).replace(/https?:\/\/[^\s]+/gi, ' ')
  const lines = withoutUrls.split(/\r?\n/)
    .map(line => line.replace(/[^\p{L}\p{N}₹%]+/gu, ' ').replace(/\s+/g, ' ').trim().toLowerCase())
    .filter(Boolean)
  if (!lines.length) return null
  const headline = lines[0]
  const basis = headline.length >= 18 && headline.split(' ').length >= 3 ? headline : lines.join(' ')
  if (basis.length < 18 || basis.split(' ').length < 3) return null
  return sha(basis)
}
function dealKey(text) {
  const urls = urlsIn(text).map(url => {
    try {
      const u = new URL(url)
      for (const key of [...u.searchParams.keys()]) {
        if (/^(utm_|aff|tag|ref|share)/i.test(key)) u.searchParams.delete(key)
      }
      u.hash = ''
      return u.toString()
    } catch { return url }
  }).sort()
  return sha(`${urls.join('|')}|${normalizedText(text)}`)
}
function pruneState() {
  const now = Date.now()
  const cutoff = now - 36 * 3600_000
  for (const [key, ts] of Object.entries(state.sent)) if (ts < cutoff) delete state.sent[key]
  state.sentContent ||= {}
  for (const [key, ts] of Object.entries(state.sentContent)) if (ts < now - 24 * 3600_000) delete state.sentContent[key]
  state.sentTimes = state.sentTimes.filter(ts => ts > now - 24 * 3600_000)
  // Digest fan-out marks are a rolling anti-duplicate window only.
  if (state.textMarks && state.textMarks.length > 400) state.textMarks = state.textMarks.slice(-200)
  // Product dedup map: keep a 24h memory (the dedup window is shorter).
  state.sentProducts ||= {}
  for (const [id, ts] of Object.entries(state.sentProducts)) if (ts < now - 24 * 3600_000) delete state.sentProducts[id]
  state.sentNamePrice ||= {}
  for (const [k, ts] of Object.entries(state.sentNamePrice)) if (ts < now - 24 * 3600_000) delete state.sentNamePrice[k]
  // Short-link cache is capped so the state file stays small.
  state.shortLinks ||= {}
  const shortKeys = Object.keys(state.shortLinks)
  if (shortKeys.length > 1500) {
    for (const key of shortKeys.slice(0, shortKeys.length - 1500)) delete state.shortLinks[key]
  }
  const before = state.jobs.length
  let trustDropped = 0
  const seenContent = new Set()
  state.jobs = state.jobs
    .filter(job => Number(job.createdAt || now) >= now - MAX_JOB_AGE_MS)
    .filter(job => {
      const reason = ordinaryJobExpiryReason(job, now)
      if (reason) { trustDropped += 1; return false }
      return true
    })
    .filter(job => {
      const content = contentFingerprint(job.text)
      if (!content) return true
      if (state.sentContent[content] || seenContent.has(content)) return false
      seenContent.add(content)
      return true
    })
  if (trustDropped) {
    log.warn({ dropped: trustDropped }, 'expired ordinary deals dropped (trust policy)')
  }
  if (state.jobs.length < before) {
    log.warn({ removed: before - state.jobs.length }, 'stale/duplicate WhatsApp queue jobs removed')
  }
}
function localParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date)
  return Object.fromEntries(parts.map(p => [p.type, p.value]))
}
function minuteOfDay(ts) {
  const p = localParts(ts === undefined ? undefined : new Date(ts))
  return Number(p.hour) * 60 + Number(p.minute)
}
function parseClock(value) {
  const [h, m] = value.split(':').map(Number)
  return h * 60 + m
}
function isQuiet() {
  const now = minuteOfDay(), start = parseClock(QUIET_START), end = parseClock(QUIET_END)
  return start <= end ? now >= start && now < end : now >= start || now < end
}
function warmupPolicy() {
  // This number is already warmed. A lost/rebuilt bridge-state.json used to
  // reset warmupStartedAt to "today", dropping the account back to the day-1
  // tier (8/hour, 18/day) — which is exactly how the Channel goes quiet after a
  // handful of late-night posts. WA_WARMUP_DONE pins the mature tier.
  const warmupDone = (process.env.WA_WARMUP_DONE || 'true').toLowerCase() === 'true'
  const day = warmupDone
    ? 8
    : Math.max(1, Math.floor((Date.now() - state.warmupStartedAt) / 86400_000) + 1)
  // An established channel no longer needs a 1-2 minute wait between posts -
  // that is what made the mirror look like "it posts late and at random". The
  // mature tier is tunable (WA_MATURE_GAP_MIN/MAX_SECONDS) with a 15s floor;
  // the anti-ban structures around it (burst rest, one irregular hourly break,
  // occasional long idle, caps) stay, because a WhatsApp number posted at
  // machine speed gets banned - unlike the Telegram bot account.
  const matureMin = Math.max(15, Number(process.env.WA_MATURE_GAP_MIN_SECONDS || 30))
  const matureMax = Math.max(matureMin, Number(process.env.WA_MATURE_GAP_MAX_SECONDS || 60))
  let policy
  if (day <= 2) policy = { day, min: 120, max: 180, hourCap: 8, dayCap: 18 }
  else if (day <= 7) policy = { day, min: matureMax, max: matureMax * 2, hourCap: 30, dayCap: 400 }
  else policy = { day, min: matureMin, max: matureMax, hourCap: 45, dayCap: 700 }
  if (HOUR_CAP_OVERRIDE > 0) policy.hourCap = HOUR_CAP_OVERRIDE
  if (DAY_CAP_OVERRIDE > 0) policy.dayCap = DAY_CAP_OVERRIDE
  return policy
}
function withinCaps() {
  pruneState()
  const p = warmupPolicy(), now = Date.now()
  const hourCount = state.sentTimes.filter(ts => ts > now - 3600_000).length
  const today = localParts(), todayKey = `${today.year}-${today.month}-${today.day}`
  const dayCount = state.sentTimes.filter(ts => {
    const d = localParts(new Date(ts))
    return `${d.year}-${d.month}-${d.day}` === todayKey
  }).length
  const effectiveHourCap = isQuiet() && HYBRID_QUIET ? Math.min(p.hourCap, 4) : p.hourCap
  return {
    ok: hourCount < effectiveHourCap && dayCount < p.dayCap,
    hourCount, dayCount, ...p, hourCap: effectiveHourCap,
  }
}
function acceleratedNextAllowed(now, lastSent, readyCount, quietNow, currentNext) {
  if (quietNow || readyCount < 2) return currentNext
  return Math.min(currentNext, Math.max(now, lastSent + MIN_WA_MESSAGE_GAP_SECONDS * 1000))
}
function scheduleNext() {
  const p = warmupPolicy()
  let { gap, rested } = smartGapMs(p)
  const cap = withinCaps()
  if (cap.hourCount >= Math.floor(p.hourCap * 0.65)) gap = Math.floor(gap * 1.5)
  const min = minuteOfDay()
  if (min >= 7 * 60 && min < 8 * 60 + 30) gap = Math.floor(gap * 1.25)
  if (isQuiet() && HYBRID_QUIET) gap = Math.floor(gap * 1.5)

  // User-selected safety pattern: if enough posts exist, take one irregular
  // long break per IST clock hour (4-9 min, randomised so it never looks like a
  // fixed timer). Sparse hours do not create dummy posts merely to trigger it.
  const lp = localParts()
  const hourKey = `${lp.year}-${lp.month}-${lp.day}-${lp.hour}`
  if (!state.breakState || state.breakState.hourKey !== hourKey) {
    state.breakState = {
      hourKey, planned: 1, taken: 0,
      posts: 0, nextTrigger: randomInt(5, 10),
    }
  }
  state.breakState.posts += 1
  if (state.breakState.taken < state.breakState.planned &&
      state.breakState.posts >= state.breakState.nextTrigger) {
    gap += randomMs(ANTIBAN_HOUR_BREAK_MIN, ANTIBAN_HOUR_BREAK_MAX)
    state.breakState.taken += 1
    state.breakState.nextTrigger += randomInt(5, 10)
    rested = true
  }
  state.nextAllowedAt = Date.now() + gap
  // Pin a scheduled anti-ban rest so the queue accelerator cannot truncate it:
  // a long human-like pause must be honoured even when many deals are ready.
  state.antibanRestUntil = rested ? state.nextAllowedAt : 0
  saveState()
}

// ---------------------------------------------------------------------------
// ADVANCED ANTI-BAN TIMING — human-like, never a fixed cadence.
// Each gap is a wide randomised interval; after a short "burst" of posts the
// bridge takes a longer rest (mimicking a person who sends a few deals then
// puts the phone down), and an occasional long idle keeps the pattern organic.
// All bounds are env-tunable; throughput stays well under the hour/day caps.
// ---------------------------------------------------------------------------
const ANTIBAN_BURST_MIN = Math.max(2, Number(process.env.WA_ANTIBAN_BURST_MIN || 3))
const ANTIBAN_BURST_MAX = Math.max(ANTIBAN_BURST_MIN, Number(process.env.WA_ANTIBAN_BURST_MAX || 6))
const ANTIBAN_REST_MIN = Number(process.env.WA_ANTIBAN_REST_MIN_SECONDS || 150)  // ~2.5 min
const ANTIBAN_REST_MAX = Number(process.env.WA_ANTIBAN_REST_MAX_SECONDS || 480)  // ~8 min
const ANTIBAN_HOUR_BREAK_MIN = Number(process.env.WA_ANTIBAN_HOUR_BREAK_MIN || 240) // ~4 min
const ANTIBAN_HOUR_BREAK_MAX = Number(process.env.WA_ANTIBAN_HOUR_BREAK_MAX || 540) // ~9 min
const ANTIBAN_LONG_IDLE_EVERY = Math.max(4, Number(process.env.WA_ANTIBAN_LONG_IDLE_EVERY || 14)) // ~1 in N gaps
const ANTIBAN_LONG_IDLE_MIN = Number(process.env.WA_ANTIBAN_LONG_IDLE_MIN || 600)  // ~10 min
const ANTIBAN_LONG_IDLE_MAX = Number(process.env.WA_ANTIBAN_LONG_IDLE_MAX || 1100) // ~18 min
function smartGapMs(policy) {
  // Base human gap: policy range x a 0.85-1.4 random multiplier, so consecutive
  // gaps never repeat a number (a fixed interval is a bot tell).
  const base = randomMs(policy.min, policy.max)
  const jitter = 0.85 + Math.random() * 0.55
  let gap = Math.floor(base * jitter)
  let rested = false
  // Burst rest: after a small random run of posts, pause like a person.
  state.burstCount = (Number(state.burstCount) || 0) + 1
  if (!state.burstThreshold || state.burstCount >= state.burstThreshold) {
    gap += randomMs(ANTIBAN_REST_MIN, ANTIBAN_REST_MAX)
    state.burstCount = 0
    state.burstThreshold = randomInt(ANTIBAN_BURST_MIN, ANTIBAN_BURST_MAX)
    rested = true
  }
  // Occasional long idle (roughly one long coffee break every ~14 sends).
  if (Math.random() < 1 / ANTIBAN_LONG_IDLE_EVERY) {
    gap += randomMs(ANTIBAN_LONG_IDLE_MIN, ANTIBAN_LONG_IDLE_MAX)
    rested = true
  }
  // Hard floor/ceiling keep the queue healthy and the caps reachable.
  const floor = MIN_WA_MESSAGE_GAP_SECONDS * 1000
  const ceil = Math.max(floor, ANTIBAN_LONG_IDLE_MAX * 1000 * 1.4)
  return { gap: Math.min(ceil, Math.max(floor, gap)), rested }
}

// ---------------------------------------------------------------------------
// WhatsApp Channel (newsletter) media delivery
//
// Baileys 7.0.0-rc14 uploads newsletter media through the regular /mms/* path.
// WhatsApp answers with a directPath under /o1/..., which the Channel renderer
// refuses; the stanza is acked with error 479 and subscribers see nothing even
// though sendMessage() resolves successfully. The official client uploads
// channel media to /newsletter/newsletter-<type> with server_thumb_gen=1 and
// receives an /m1/... directPath. We replicate that here without patching
// node_modules: build the message with a custom upload function, then relay it.
// Upstream reference: WhiskeySockets/Baileys issue #2199, PR #2434.
// ---------------------------------------------------------------------------
const NEWSLETTER_MEDIA_PATH_MAP = {
  image: '/newsletter/newsletter-image',
  video: '/newsletter/newsletter-video',
  document: '/newsletter/newsletter-document',
  audio: '/newsletter/newsletter-audio',
  gif: '/newsletter/newsletter-gif',
  ptt: '/newsletter/newsletter-ptt',
  ptv: '/newsletter/newsletter-ptv',
  sticker: '/newsletter/newsletter-sticker-pack',
  'thumbnail-link': '/newsletter/newsletter-image',
}

async function newsletterUpload(sock, filePath, { mediaType, fileEncSha256B64, timeoutMs }) {
  const conn = await sock.refreshMediaConn(false)
  const token = encodeBase64EncodedStringForUpload(fileEncSha256B64)
  const auth = encodeURIComponent(conn.auth)
  const mediaPath = NEWSLETTER_MEDIA_PATH_MAP[mediaType] || `/mms/${mediaType}`
  let lastError
  for (const { hostname } of conn.hosts) {
    let url = `https://${hostname}${mediaPath}/${token}?auth=${auth}&token=${token}&server_thumb_gen=1`
    if (mediaType === 'video' || mediaType === 'gif' || mediaType === 'ptv') url += '&server_transcode=1'
    try {
      const body = Readable.toWeb(fs.createReadStream(filePath))
      const response = await fetch(url, {
        method: 'POST', body, duplex: 'half',
        headers: { 'Content-Type': 'application/octet-stream', Origin: DEFAULT_ORIGIN },
        signal: AbortSignal.timeout(timeoutMs || 120_000),
      })
      const result = await response.json().catch(() => undefined)
      if (result?.url || result?.direct_path) {
        return {
          mediaUrl: result.url || result.direct_path,
          directPath: result.direct_path,
          thumbnailDirectPath: result.thumbnail_info?.thumbnail_direct_path,
          thumbnailSha256: result.thumbnail_info?.thumbnail_sha256,
        }
      }
      lastError = new Error(`newsletter upload rejected: ${JSON.stringify(result)?.slice(0, 200)}`)
    } catch (error) {
      lastError = error
    }
    log.warn({ hostname, err: lastError?.message }, 'newsletter media upload host failed; trying next')
  }
  throw lastError || new Error('newsletter media upload failed on all hosts')
}

// Watches the ack for a specific stanza id. Newsletter media that WhatsApp
// refuses comes back as error 479 (smax-invalid) within a couple of seconds.
function watchAck(sock, messageId, windowMs = 9000) {
  return new Promise(resolve => {
    let done = false
    const finish = value => { if (!done) { done = true; sock.ev.off('messages.update', handler); resolve(value) } }
    const handler = updates => {
      for (const update of updates) {
        if (update?.key?.id !== messageId) continue
        const status = update?.update?.status
        if (status === 0) return finish({ ok: false, error: update?.update?.messageStubParameters?.[0] || 'ack-error' })
      }
    }
    sock.ev.on('messages.update', handler)
    setTimeout(() => finish({ ok: true }), windowMs)
  })
}

// Sends photo/video to a WhatsApp Channel using the corrected upload path and
// verifies the ack. Throws when WhatsApp rejects the stanza, so the caller can
// fall back to a text-only update instead of silently posting nothing.
async function sendNewsletterMedia(sock, jid, { buffer, type, mimetype, caption: rawCaption }) {
  const caption = sanitizeOutbound(rawCaption)
  const isVideo = type === 'video'
  const mediaType = isVideo ? 'video' : 'image'
  const content = isVideo
    ? { video: buffer, mimetype: mimetype || 'video/mp4', caption: caption || undefined }
    : { image: buffer, caption: caption || undefined }

  if (!NEWSLETTER_MEDIA_FIX) {
    const sent = await withTimeout(sock.sendMessage(jid, content), 150_000, 'sendMessage(media)')
    const ack = await watchAck(sock, sent?.key?.id)
    if (!ack.ok) throw new Error(`WhatsApp rejected Channel media (ack ${ack.error})`)
    return sent
  }

  let thumbnailInfo = {}
  const message = await withTimeout(prepareWAMessageMedia(content, {
    jid,
    logger: log,
    mediaUploadTimeoutMs: 150_000,
    upload: async (filePath, opts) => {
      const uploaded = await newsletterUpload(sock, filePath, opts)
      thumbnailInfo = {
        thumbnailDirectPath: uploaded.thumbnailDirectPath,
        thumbnailSha256: uploaded.thumbnailSha256,
      }
      return uploaded
    },
  }), 180_000, 'prepareWAMessageMedia')

  const node = message[`${mediaType}Message`]
  if (!node?.directPath) throw new Error('newsletter media upload returned no directPath')
  // The official client sends channel media with directPath only; a stale
  // /o1/ url field is what makes the Channel renderer drop the attachment.
  node.url = undefined
  if (thumbnailInfo.thumbnailDirectPath) node.thumbnailDirectPath = thumbnailInfo.thumbnailDirectPath
  if (thumbnailInfo.thumbnailSha256) node.thumbnailSha256 = Buffer.from(thumbnailInfo.thumbnailSha256, 'base64')

  const messageId = generateMessageIDV2(sock.user?.id)
  await withTimeout(sock.sendNode({
    tag: 'message',
    attrs: { to: jid, id: messageId, type: 'media' },
    content: [{ tag: 'plaintext', attrs: { mediatype: mediaType }, content: encodeNewsletterMessage(message) }],
  }), 60_000, 'sendNode(media)')

  const ack = await watchAck(sock, messageId)
  if (!ack.ok) throw new Error(`WhatsApp rejected Channel media (ack ${ack.error})`)
  log.info({ mediaType, directPath: String(node.directPath).slice(0, 12) }, 'Channel media accepted')
  return { key: { id: messageId, remoteJid: jid, fromMe: true } }
}

async function sendNewsletterText(sock, jid, rawText) {
  // LAST GATE for every Channel post: markdown debris, glued random fragments
  // and empty brackets are removed here, after all formatting decisions, so
  // nothing unclean can reach a subscriber even if an earlier pass missed it.
  const text = sanitizeOutbound(rawText)
  const sent = await withTimeout(sock.sendMessage(jid, { text }), 90_000, 'sendMessage(text)')
  const ack = await watchAck(sock, sent?.key?.id, 6000)
  if (!ack.ok) throw new Error(`WhatsApp rejected Channel text (ack ${ack.error})`)
  return sent
}

async function tg(method, body = {}) {
  let lastError
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const response = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/${method}`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
        signal: AbortSignal.timeout(65_000),
      })
      const data = await response.json()
      if (data.ok) return data.result
      const error = new Error(`Telegram ${method}: ${data.description || response.status}`)
      if (response.status < 500 && response.status !== 429) throw Object.assign(error, { permanent: true })
      throw error
    } catch (error) {
      lastError = error
      if (error.permanent || attempt === 3) throw error
      await sleep(randomMs(2 ** attempt, 2 ** attempt + 2))
    }
  }
  throw lastError
}
async function telegramFile(fileId) {
  let lastError
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const info = await tg('getFile', { file_id: fileId })
      const response = await fetch(`https://api.telegram.org/file/bot${TG_TOKEN}/${info.file_path}`, {
        signal: AbortSignal.timeout(60_000),
      })
      if (!response.ok) throw new Error(`Telegram file download HTTP ${response.status}`)
      return Buffer.from(await response.arrayBuffer())
    } catch (error) {
      lastError = error
      if (attempt === 3) throw error
      await sleep(randomMs(2 ** attempt, 2 ** attempt + 2))
    }
  }
  throw lastError
}
async function ocrImage(buffer) {
  return await new Promise((resolve, reject) => {
    const child = spawn('tesseract', ['stdin', 'stdout', '-l', 'eng'], { stdio: ['pipe', 'pipe', 'pipe'] })
    const output = [], errors = []
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('OCR timeout')) }, 25_000)
    child.stdout.on('data', chunk => output.push(chunk))
    child.stderr.on('data', chunk => errors.push(chunk))
    child.on('error', error => { clearTimeout(timer); reject(error) })
    child.on('close', code => {
      clearTimeout(timer)
      if (code === 0) resolve(Buffer.concat(output).toString('utf8'))
      else reject(new Error(`OCR exit ${code}: ${Buffer.concat(errors).toString('utf8').slice(0, 160)}`))
    })
    child.stdin.end(buffer)
  })
}
async function classifyMediaOffers() {
  const jobs = state.jobs.filter(job => !job.ocrChecked && job.media?.some(item => item.type === 'image') && job.availableAt <= Date.now())
  for (const job of jobs.slice(0, 2)) {
    job.ocrChecked = true
    try {
      const image = job.media.find(item => item.type === 'image')
      const detected = await ocrImage(await telegramFile(image.fileId))
      if (isSpecialOffer(detected)) {
        job.special = true
        job.batchReadyAt = Math.min(job.batchReadyAt || Infinity, Date.now() + randomMs(SPECIAL_JITTER_MIN, SPECIAL_JITTER_MAX))
        log.info({ id: job.id }, 'special offer detected from photo OCR')
      }
    } catch (error) {
      log.warn({ id: job.id, err: error.message }, 'photo OCR inconclusive')
    }
    saveState()
  }
}
function mediaFromPost(post) {
  if (post.photo?.length) return { type: 'image', fileId: post.photo.at(-1).file_id }
  if (post.video) return { type: 'video', fileId: post.video.file_id, mimetype: post.video.mime_type || 'video/mp4' }
  return null
}
// 'source' = one of our own bot channels (strict provenance).
// 'direct'  = a raw deal source watched directly (safety net for deals the
//             Telegram bot skipped/missed). null = ignore.
function classifySource(post) {
  const username = (post.chat?.username || '').toLowerCase()
  if (SOURCES.has(username)) return 'source'
  if (DIRECT_SOURCES.has(username)) return 'direct'
  return null
}
function acceptPost(post) {
  return classifySource(post) !== null
}
// Our generated affiliate links (bot output): full provenance applies. Raw
// source links on direct jobs are resolved + posted unconverted instead.
function isOurGeneratedLink(url) {
  try {
    const u = new URL(url)
    const host = u.hostname.toLowerCase()
    if (OUR_LINK_HOSTS.has(host)) return true
    if (host === 'amazon.in' || host.endsWith('.amazon.in') || host === 'amazon.com' || host.endsWith('.amazon.com')) {
      return u.searchParams.get('tag') === AMAZON_TAG
    }
    if (host === 'flipkart.com' || host.endsWith('.flipkart.com')) {
      return u.searchParams.get('affExtParam2') === PUBLISHER_ID
    }
    return false
  } catch {
    return false
  }
}
// Amazon links carrying our Associates tag are SELF-PROVING: the tag can only
// have been added by us (the bot's direct-Associates path, or this bridge
// tagging a raw source product/search page on a safety-net job). The BestGAA
// link_cache only stores links produced by a conversion-API call, so it
// legitimately may not contain them — bridge-tagged pages were never processed
// by the bot, and a fresh/pruned/deploy-cleared cache loses even the bot's own
// direct links. A foreign link bearing our tag still monetises to us, so the
// tag is both necessary and sufficient proof of provenance; the DB check would
// only manufacture false "Provenance mismatch" rejections and drop good deals.
function isOurAmazonTagLink(url) {
  try {
    const u = new URL(url)
    const host = u.hostname.toLowerCase()
    if (host === 'amazon.in' || host.endsWith('.amazon.in') || host === 'amazon.com' || host.endsWith('.amazon.com')) {
      return u.searchParams.get('tag') === AMAZON_TAG
    }
    return false
  } catch {
    return false
  }
}
// Follow known shortener/redirector hosts to the final merchant page (max 5
// hops). linkredirect.in embeds the destination in its ?dl= param, so it is
// decoded without any network call. Returns null when resolution fails.
async function resolveUrl(url, fetchFn = fetch) {
  let current = url
  for (let hop = 0; hop < 5; hop++) {
    const host = hostOf(current)
    if (host === 'linkredirect.in') {
      try {
        const dl = new URL(current).searchParams.get('dl')
        if (dl && dl.startsWith('http')) { current = decodeURIComponent(dl); continue }
      } catch { /* fall through to fetch */ }
    }
    if (!REDIRECT_FOLLOW_HOSTS.has(host)) break
    try {
      const response = await fetchFn(current, {
        method: 'GET', redirect: 'follow', signal: AbortSignal.timeout(15_000),
        headers: { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131 Safari/537.36' },
      })
      const final = response.url || current
      try { await response.body?.cancel?.() } catch { /* noop */ }
      if (final === current) break
      current = final
    } catch {
      return current === url ? null : current
    }
  }
  return current
}
// Direct-source jobs carry RAW source links (fkrt.co, amzn.to, linkredirect.in,
// bare merchant pages) — not our generated affiliate links. Smart handling:
//   * our generated links keep the normal provenance verification
//   * raw Amazon product URLs get our tag appended -> still monetized
//   * everything else is resolved (redirect follow) and posted unconverted, so
//     the deal reaches WhatsApp even when the bot never posted it to Telegram
async function prepareDirectJob(job, fetchFn = fetch) {
  job.resolvedLinks ||= {}
  for (const url of urlsIn(job.text || '')) {
    if (job.resolvedLinks[url] || isOurGeneratedLink(url)) continue
    let resolved = url
    if (REDIRECT_FOLLOW_HOSTS.has(hostOf(url))) {
      resolved = (await resolveUrl(url, fetchFn)) || url
    }
    try {
      const u = new URL(resolved)
      const host = u.hostname.toLowerCase()
      const isAmazon = host === 'amazon.in' || host.endsWith('.amazon.in') || host === 'amazon.com' || host.endsWith('.amazon.com')
      const isProductPage = /\/(?:dp|gp\/product|gp\/aw\/d)\/[A-Z0-9]{10}(?:[/?#]|$)/i.test(u.pathname)
      // Search/category pages (/s?k=puma&rh=...) are monetizable too — the
      // user's Puma Men/Women/Girls/Boys style lists arrive as /s links.
      const isSearchPage = u.pathname === '/s' || u.pathname.startsWith('/s/')
      if (isAmazon && !u.searchParams.get('tag') && (isProductPage || isSearchPage)) {
        resolved += (resolved.includes('?') ? '&' : '?') + `tag=${AMAZON_TAG}`
      }
    } catch { /* keep resolved as-is */ }
    if (resolved !== url) job.resolvedLinks[url] = resolved
  }
}
function isSpecialOffer(text) {
  const value = (text || '').replace(/&amp;/gi, '&')
  const highDiscount = /\b(?:8\d|9\d|100)\s*%\s*(?:off|discount)\b/i.test(value)
  const explicitCard = /\b(?:credit\s*card|debit\s*card|bank\s*offer|card\s*offer)\b/i.test(value)
  const bankNearOffer = /\b(?:pnb|hdfc|icici|sbi|axis|kotak|idfc|au\s*bank)\b[\s\S]{0,60}\b(?:off|discount|cashback|card)\b|\b(?:off|discount|cashback|card)\b[\s\S]{0,60}\b(?:pnb|hdfc|icici|sbi|axis|kotak|idfc|au\s*bank)\b/i.test(value)
  // Zomato/Swiggy/Zepto/movie/card offers are best-tier for subscribers even
  // without a huge % off: they are time-sensitive lifestyle specials.
  const serviceOffer = isServiceOffer(value)
  return highDiscount || explicitCard || bankNearOffer || serviceOffer
}
function classifyPost(text, hasMedia = false) {
  const largeList = urlsIn(text).length >= LARGE_LIST_MIN_LINKS
  return {
    largeList,
    special: isSpecialOffer(text) && (!largeList || hasMedia),
  }
}
function explicitDiscount(text) {
  const values = [...(text || '').matchAll(/\b([1-9]\d?|100)\s*%\s*(?:off|discount)\b/gi)]
    .map(match => Number(match[1]))
  return values.length ? Math.max(...values) : null
}
function isPreferredShoppingCategory(text) {
  return /\b(?:shirts?|t-?shirts?|topwear|pants?|jeans|trousers?|trackpants?|dresses?|kurtas?|sarees?|jackets?|hoodies?|clothing|fashion|socks?|belts?|wallets?|sunglasses?|slippers?|sandals?|shoes?|footwear|sneakers?|loafers?|bags?|luggage|trolleys?|watches?|mobiles?|phones?|chargers?|cables?|power\s*banks?|earphones?|headphones?|headsets?|speakers?|smartwatches?|laptops?|tablets?|televisions?|tv|electronics?|appliances?|fridges?|refrigerators?|fans?|mixers?|irons?|containers?|storage|mats?|bedsheets?|towels?|cleaning|furniture|bottles?|cookware|kitchen|home|fitness|dumbbells?|yoga|toys?|grocery|food|snacks?|personal\s*care|beauty|deodorants?|deos?|perfumes?|soaps?|shampoos?|moisturizers?|body\s*washes?|daily\s*essentials?)\b/i.test(text || '')
}
function isWomenAttractiveCategory(text) {
  return /\b(?:women|womens|ladies|girls?|sarees?|kurtas?|kurtis?|dresses?|gowns?|tops?|leggings?|night\s*suits?|night\s*dresses?|lingerie|handbags?|purses?|jewellery|jewelry|earrings?|necklaces?|bangles?|makeup|cosmetics?|skincare|beauty|perfumes?|sandals?|slippers?|home\s*decor|bedsheets?|kitchen|storage|containers?)\b/i.test(text || '')
}
// Price of the DEAL (what the buyer pays), never the MRP/struck price, a
// discount percent, or a bank cashback. Mirrors the bot's parse_price:
//  1) an explicit Deal/Effective/Offer/Final Price wins;
//  2) "only/at/from ₹X" wins when it is not an "X% off" / cashback value;
//  3) otherwise the LOWEST plain ₹/Rs price is the deal price (MRP is the
//     higher struck price, so min picks the selling price and skips MRP lines).
function detectedPrice(text) {
  const t = text || ''
  const num = m => Number((m[1] || '').replace(/,/g, ''))
  const valid = n => Number.isFinite(n) && n >= 1 && n <= 1_000_000
  let explicit = t.match(/(?:deal|effective|offer|final|selling|price)\s*[:\-]?\s*(?:rs\.?|₹)?\s*([\d,]+)/i)
  if (explicit && valid(num(explicit))) return num(explicit)
  let at = t.match(/(?:only|price|at|from|just)\s*[:\-]?\s*(?:rs\.?|₹)\s*([\d,]+)\s*(?!\s*(?:%|off|discount|cashback))/i)
  if (at && valid(num(at))) return num(at)
  const prices = []
  for (const m of t.matchAll(/(?:rs\.?|inr|₹)\s*([\d,]+)/gi)) {
    // Skip a price that is an MRP/struck/was value or a discount/cashback.
    const before = t.slice(Math.max(0, m.index - 12), m.index)
    const after = t.slice(m.index + m[0].length, m.index + m[0].length + 14)
    if (/\b(mrp|was|struck|regular|original|list)\b\s*:?\s*$/i.test(before)) continue
    if (/^\s*(?:off|discount|cashback|%|coupon)/i.test(after)) continue
    const v = num(m)
    if (valid(v)) prices.push(v)
  }
  return prices.length ? Math.min(...prices) : null
}
// ---------------------------------------------------------------------------
// Service offers + deal-name extraction + best-deal gate
// ---------------------------------------------------------------------------
function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase() } catch { return '' }
}
function isServiceUrl(url) {
  const host = hostOf(url)
  return SERVICE_OFFER_DOMAINS.some(domain => host === domain || host.endsWith('.' + domain))
}
function isServiceOffer(text) {
  if (urlsIn(text).some(isServiceUrl)) return true
  return /\b(?:zomato|swiggy|zepto|bookmyshow|domino(?:s)?|pizza\s*hut|movie\s*(?:tickets?|shows?|offers?|booking)|cinema\s*(?:offers?|tickets?)|pvr|inox|amc\s*theatres?|bigcinema|phonepe|amazon\s*pay|paytm|magicpin)\b/i.test(text || '')
}
// A line that can serve as the readable deal NAME: real words, not a pure
// price/discount/CTA/header/URL line. Telugu + Latin scripts both count.
function scoreNameLine(line) {
  const withoutUrl = (line || '').replace(/https?:\/\/\S+/g, ' ').trim()
  if (!withoutUrl) return 0
  if (/^(?:use\s*code|code\s*:|buy\s*now|shop\s*now|order\s*now|click\s*(?:here|now)|tap\s*here|hurry\s*up|grab\s*fast|verified|enjoy|deal\s*price|mrp|price\s*:|deal\s*time|loot\s*fast)\b/i.test(withoutUrl)) return 0
  if (/^\s*(?:deals?|offers?|loots?)\s+(?:of|on|for|in)?\s*(?:the\s+)?(?:day|today|week|weekend|month|india)\b/i.test(withoutUrl) && !/\d/.test(withoutUrl)) return 0
  const alphaCount = (withoutUrl.match(/[A-Za-z\u0900-\u097F\u0C00-\u0C7F]/g) || []).length
  if (alphaCount < BEST_MIN_NAME_CHARS) return 0
  const alphaWords = withoutUrl.split(/\s+/).filter(word => /[A-Za-z\u0900-\u097F\u0C00-\u0C7F]/.test(word)).length
  if (alphaWords < 2 && alphaCount < 10) return 0
  // A channel/promo HEADER is not a deal name, even when it has enough words
  // ("DEALS OF THE DAY", "LOOT ZONE INDIA", "TODAY SPECIAL OFFER", "GRAB FAST
  // GUYS", "PRICE DROP DEAL"). Such a line would become the random unwanted
  // bold title sitting on top of the price badges; refuse it so a real product
  // line becomes the name. A header line that ALSO names a product is kept.
  const promoWord = /\b(deal|deals|offer|offers|loot|loots|sale|dhamaka)\b/i.test(withoutUrl)
  // A merchant/brand name ("Myntra Mega Sale", "Flipkart Fashion Deals") makes
  // a promo-styled line a real list/store header, not a generic channel CTA.
  const brandWord = /\b(myntra|flipkart|amazon|ajio|nykaa|meesho|croma|reliance|tatacliq|tata\s*cliq|snapdeal|jiomart|zepto|swiggy|zomato|boat|boAt|nike|adidas|puma|levi'?s|h&m|zara|westside|lifestyle|shoppers?\s*stop|decathlon|bata|crocs|wildcraft|fastrack|titan|noise|fire-?boltt|mi|realme|samsung|oneplus|oppo|vivo|redmi|portronics|mivia?|philips|havells|bajaj|prestige|pigeon|lifelong|cello|milton|borosil|treo|urban\s*company|mamaearth|wow|minimalist|dove|nivea|lakme|maybelline|mcaffeine|beardo|set\s*wet|park\s*avenue|engage|fog|villain|skinn|titan\s*rage|sonata|casio|fossil|daniel\s*wellington|timex)\b/i.test(withoutUrl)
  if (promoWord && !isPreferredShoppingCategory(withoutUrl) && !isWomenAttractiveCategory(withoutUrl) && !brandWord) return 0
  return alphaCount
}
function findDealNameLine(text) {
  const lines = (cleanDealText(text) || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  for (const line of lines) if (scoreNameLine(line) > 0) return line
  return null
}
// Clean a chosen name line into a title: remove inline CTA/promo and leading
// decorative bullets/arrows/emojis so the bold name starts with the product
// word, and mask any URLs out (an inline link is not part of the title and is
// re-added at the bottom). No length cap (a one-line post is shown in full);
// digest labels apply their own cap via extractDealName().
function cleanTitle(line) {
  let name = String(line || '').replace(/https?:\/\/\S+/gi, ' ')
  name = stripInlineCta(name)
  name = name
    .replace(/\s{2,}/g, ' ')
    .replace(/[\s.]*$/g, '')
    .replace(/\b(?:https?|tps?|s)?\s*:?\/?\/?\s*$/gi, '')
    .trim()
  name = name.replace(/^[^\p{L}\p{N}]+/u, '').trim()
  return name
}
function extractDealName(text) {
  const line = findDealNameLine(text)
  if (!line) return null
  const name = cleanTitle(line)
  return name ? name.slice(0, 90) : null
}
// v17.5 ONE neat WhatsApp post = the source post itself: cleaned source lines in
// source order, our links one per line, and nothing added by us.
// Long affiliate URLs never sit inline in the text anymore.
// One neat WhatsApp post that mirrors the source:
//  * Single deals: bold deal NAME, price/discount badges, the remaining source
//    lines, then the link on its own line at the bottom.
//  * Multi-link lists: every link stays UNDER its product label (source order)
//    so nobody has to guess which link is which; label-less links go at the
//    bottom in source order.
// No source line is ever dropped and each link appears exactly once, in its
// display (possibly Bitly/is.gd-shortened) form.
function cleanBodyLine(line, urls) {
  let rest = line
  // Mask every URL (including ones discovered mid-line) so a trailing/truncated
  // link never leaves a "ly/abc" / "https:/" fragment glued to the deal text.
  const allUrls = urls && urls.length ? urls : urlsIn(rest)
  const hadUrl = allUrls.length > 0
  allUrls.forEach(url => { rest = rest.split(url).join(' \x00 ') })
  // A CTA lead-in that pointed at the link itself ("Buy at <link>", "Shop on
  // <link>", "click here: <link>") is channel decoration around OUR replaced
  // link, never deal content. It is removed while the link is still a
  // placeholder, so it works mid-line too (the end-of-line patterns in
  // stripInlineCta cannot see it there).
  rest = rest.replace(/\s*\b(?:buy|shop|order|grab|get|check|click|tap|visit|see)\b[^\x00\n]{0,24}?\x00/gi, ' \x00')
  rest = stripInlineCta(rest)
  rest = rest.replace(/\x00/g, ' ')
  if (hadUrl) {
    // Only on a link-bearing line: remove a dangling protocol/path fragment left
    // by an inline link (e.g. "ly/shortlink"), never a product spec/size token.
    rest = rest
      .replace(/\bhttps?(?::?\/?\/?)?(?=\s|$)/gi, ' ')
      .replace(/\b[a-z]{2,3}\/[A-Za-z0-9_-]{2,}(?=\s|$)/g, ' ')
  }
  return rest.replace(/\s{2,}/g, ' ').replace(/^[\s:|*•-]+|[\s:|*•-]+$/g, '').trim()
}
// Remove a CTA / promo fragment glued INSIDE a real deal line, so random
// channel text never appears next to the price (e.g. "₹499 grab fast now",
// "Deal Price ₹499 Buy now link below", "75% off don't miss"). The deal line
// itself (name + price + MRP + discount) is kept verbatim.
// STRICT global CTA/promo phrases — removed ANYWHERE in a line (not just at the
// end). Only unambiguous channel boilerplate is listed; genuine deal content
// ("use code X", "free shipping", "limited stock", coupon/cashback/product
// words) is never touched, so a real product description can't be mangled.
const GLOBAL_CTA_PATTERNS = [
  /\b(?:buy|shop|order|grab|get)\s+(?:it\s+)?now\b[!^1-9]*/gi,
  /\bgrab\s+(?:it|this|your|yours|fast)\s*(?:now|fast|soon)?\b[!^1-9]*/gi,
  /\bget\s+yours?\b[!^1-9]*/gi,
  /\bbuy\s+(?:it\s+)?here\b/gi, /\bshop\s+here\b/gi, /\border\s+here\b/gi,
  /\b(?:click|tap)\s+(?:here|the\s+link|on\s+(?:the\s+)?link|below|to\s+(?:buy|order|shop))\b[^.\n|]*/gi,
  /\b(?:don'?t|do\s+not|never)\s+miss\s+(?:it|this|out|the\s+deal|this\s+deal)\b[^.\n|]*/gi,
  /\bhurry\s*up?\b[!^1-9]*/gi,
  /\b(?:join|subscribe|follow)\s+(?:our\s+)?(?:us\s+)?(?:channel|telegram|whatsapp\s+channel|group|now)\b[^.\n|₹$]*?(?=$|[.\n|])/gim,
  /\b(?:join|subscribe|follow)\s+(?:our\s+)?(?:us\s+)?(?:on|via)?\s*t\.me\/\S+/gi,
  /\b(?:for\s+more|more\s+)(?:loot|deal|update|offer)s?\b[^.\n|₹$]*$/gim,
  /\bturn\s+on\s+notifications?\b[^.\n|]*/gi,
  /\bstay\s+tuned\b[^.\n|]*/gi,
  /\blink\s+(?:in\s+(?:bio|comments?|description)|below)\b[^.\n|]*/gi,
  /\bcheck\s+(?:link|bio|description|comments?|pinned|our\s+channel)\b[^.\n|]*/gi,
  /\bshare\s+(?:it\s+)?(?:with|to)\s+[^.\n|]*\b(?:friends?|family|groups?|everyone)\b[^.\n|]*/gi,
  /\bforward\s+to\s+@?\w+[^.\n|]*/gi,
  /\b(?:visit|open)\s+(?:our\s+)?(?:channel|t\.me\/\S+|whatsapp\s+channel)\b[^.\n|]*/gi,
  /\bt\.me\/\S+/gi, /\bwhatsapp\.com\/(?:channel|invite)\/\S+/gi, /\bwa\.me\/\S+/gi,
  /[\u{1F4E2}\u{1F514}\u{1F4E3}\u{23F0}\u{1F6A8}]/gu, // 📢🔔📣⏰🚨 announcement bells
]
function stripGlobalCta(line) {
  let out = String(line || '')
  for (const re of GLOBAL_CTA_PATTERNS) out = out.replace(re, ' ')
  return out
}
function stripInlineCta(line) {
  let out = stripGlobalCta(line || '').replace(/\*+/g, ' ')
  out = out
    // A trailing CTA that once pointed at the now-removed link: "Buy at",
    // "Shop here", "Order from" left dangling after the URL is extracted.
    .replace(/\s*\b(?:buy|shop|order|grab|get)\s+(?:at|here|from|now|it|fast|below)\b\s*[.!]?\s*$/gi, ' ')
    .replace(/\b(?:buy|shop|order|grab|get|check|add\s+to\s+cart)\s+(?:it|now|fast|soon|today|yours?|this|the\s+deal|deal|fast\s+guys?|guys?|at|here)\b[^.|\n!]*[.!]?\s*$/gi, ' ')
    .replace(/\b(?:click|tap)\s+(?:here|link|below|on\s+(?:the\s+)?link|to\s+buy|to\s+order|to\s+shop)\b[^.|\n!]*[.!]?\s*$/gi, ' ')
    .replace(/\b(?:don'?t|do\s+not|never)\s+miss\b[^.|\n!]*[.!]?\s*$/gi, ' ')
    .replace(/\bmiss\s+(?:it|this|out|the\s+deal)\b[^.|\n!]*[.!]?\s*$/gi, ' ')
    .replace(/\b(?:hurry?\s*up?|grab\s+(?:it|fast|now|your|this)|loot\s+fast|deal\s+time[^\n]*|limited(?:\s*time)?\s+offer)\b[^.|\n]*$/gi, ' ')
    // Social/channel CTAs (join/subscribe/follow/share/notifications/t.me) are
    // handled by the STRICT global patterns above (which never eat a following
    // price); no greedy end-of-line social strip here.
    .replace(/\b(?:link\s+in\s+bio|link\s+below|check\s+(?:link|bio|description|comments?|pinned))\b[^.|\n]*$/gi, ' ')
  return out.replace(/\s{2,}/g, ' ').replace(/[\s|*•:,\-]+$/g, '').trim()
}
// A body line that says ONLY the deal price (e.g. "Deal Price: ₹499",
// "₹499", "Price: ₹499", "Only ₹499"). When the 💰 price badge is shown this
// line just repeats that number lower in the post, so it is dropped. Lines
// carrying product/quantity words ("T-Shirt ₹499", "₹499 for 2 pcs") survive,
// and MRP lines (₹999 struck price for the discount) always survive.
function isRedundantPriceLine(rest) {
  // Strip CTA/promo words FIRST, so a line that is only the price plus channel
  // junk ("₹499 grab now", "Price ₹499 buy now link") is still recognised as a
  // redundant price repeat and dropped, not leaked as random text by the badge.
  const line = stripInlineCta((rest || '').replace(/\*/g, '').trim())
  if (!line) return true
  if (/mrp|strike|was\s*[:₹]/i.test(line)) return false
  if (!/₹|rs\.?\s*\d|\binr\b/i.test(line)) return false
  const words = line
    .replace(/₹\s*[\d,]+/gi, ' ')
    .replace(/\b\d[\d,]*(?:\.\d+)?\s*(?:pcs?|packs?|pairs?|kg|gm?|ml|ltrs?|l|gb|tb|mah|w|v|inch(?:es)?)?\b/gi, ' ')
    .replace(/\b\d+\s*%\s*(?:off|discount)?\b/gi, ' ')
    .replace(/\b(?:deal|price|only|just|at|from|for|rs\.?|inr|cost|rate|rs|mrp|offer|lowest|flat|off|discount)\b/gi, ' ')
    .replace(/[^A-Za-z\u0900-\u097F\u0C00-\u0C7F]/g, ' ')
    .split(/\s+/).filter(Boolean)
  return words.length === 0
}
// A body line that ONLY repeats the headline discount ("75% OFF", "75% OFF",
// "Flat 60% off") — the 🔥 badge already shows it. Lines carrying a product
// name or a price survive; only the bare percent repeat is dropped so the
// discount never appears twice.
function isRedundantDiscountLine(rest, discount) {
  if (!discount) return false
  const line = stripInlineCta((rest || '').replace(/\*/g, '').trim())
  if (!line) return true
  if (/₹|rs\.?\s*\d|\binr\b|\bmrp\b/i.test(line)) return false
  const words = line
    .replace(/\b\d+\s*%\s*(?:off|discount)?\b/gi, ' ')
    .replace(/\b(?:flat|off|discount|only|just|at|get|upto|up\s*to|deal|offer|loot|save)\b/gi, ' ')
    .replace(/[^A-Za-z\u0900-\u097F\u0C00-\u0C7F]/g, ' ')
    .split(/\s+/).filter(Boolean)
  return words.length === 0
}
// Split a one-line post into its clauses ("Name. Deal Price ₹499. MRP ₹1999.
// 75% OFF. Free shipping. Buy now!") so each piece can be kept/dropped by the
// same price/discount/CTA rules a multi-line post already uses.
function splitClauses(text) {
  return String(text || '')
    .split(/\s*[.!|]+\s*|\n+/)
    .map(c => stripInlineCta(c).replace(/\s{2,}/g, ' ').trim())
    .filter(Boolean)
}
function formatPostBody(job, { includeLinks = true, bodyMax = 0 } = {}) {
  // v17.5 - the WhatsApp post IS the source post: cleaned, never rewritten.
  //
  // The user's rule is literal: nothing of ours may be added; only what the
  // source wrote must appear. So there is no bold title hoisted out of the
  // middle of the post, no synthetic "💰 ₹499  |  75% OFF" badge line (whose
  // presence used to make the real "Deal Price ₹499" source line look
  // redundant and get dropped), no invented "Latest deal" caption for a post
  // that cleaned down to nothing, and no reordering of the source's own lines.
  //
  // What still happens: genuine junk goes (another channel's branding,
  // referral/app-install farming, CTA filler, markdown debris, glued random
  // tokens, orphan URL fragments) via cleanDealText + cleanBodyLine, and every
  // link becomes OUR monetized link, one per line.
  const raw = job.text || ''
  const cleaned = cleanDealText(raw) || ''
  const out = []
  for (const lineRaw of cleaned.split(/\r?\n/)) {
    const line = lineRaw.trim()
    if (!line) continue
    const lineUrls = urlsIn(line)
    // cleanBodyLine strips the inline CTA and link fragments; with the URLs
    // masked it never touches the product name, price, MRP or spec text.
    const text = cleanBodyLine(line, lineUrls)
    if (text) out.push(text)
    if (includeLinks) for (const url of lineUrls) out.push(displayUrl(job, url))
  }
  if (includeLinks) {
    // A link must never vanish because a cleanup pass swallowed the line it sat
    // on: re-add OUR version of any source link missing from the body.
    const written = out.join('\n')
    for (const url of urlsIn(cleaned)) {
      const ours = displayUrl(job, url)
      if (!written.includes(ours)) out.push(ours)
    }
  }
  let body = out.join('\n').replace(/\n{3,}/g, '\n\n').trim()
  if (bodyMax > 0 && body.length > bodyMax) body = `${body.slice(0, bodyMax - 1).trim()}…`
  return body
}

function formatWhatsAppPost(job, options) { return formatPostBody(job, options) }
// Advanced quality score for ONE post. Higher = better deal. Combines the
// discount ladder, the price ladder (cheap = more useful to more people),
// media, product usefulness and mega-list value. A negative price score
// actively penalises expensive weak-discount products.
function dealQualityScore(job) {
  const text = job.text || ''
  const urls = urlsIn(text)
  const discount = explicitDiscount(text) || 0
  const price = detectedPrice(text)
  const hasMedia = Boolean(job.media?.length)
  const special = Boolean(job.special) || isSpecialOffer(text)
  const megaList = Boolean(job.largeList) || urls.length >= LARGE_LIST_MIN_LINKS
  const useful = isPreferredShoppingCategory(text)
  let score = 0
  // Discount ladder (strongest signal).
  if (discount >= 80) score += 4
  else if (discount >= 70) score += 3
  else if (discount >= 50) score += 2
  else if (discount >= BEST_MIN_DISCOUNT) score += 1
  // Price ladder: under-₹99 / under-₹499 products serve the most people.
  if (price != null) {
    if (price <= 99) score += 3
    else if (price <= 499) score += 2
    else if (price <= 999) score += 1
    else if (price >= QUALITY_EXPENSIVE_PRICE) score -= 1
  }
  if (special) score += 2
  if (megaList) score += 2
  if (hasMedia) score += 1
  if (useful) score += 1
  return { score, discount, price, hasMedia, special, megaList, useful }
}
// ---------------------------------------------------------------------------
// COMMISSION-AWARE RANKING — maximise affiliate payout per post.
// Approximate 2026 EarnKaro/network CPS rates per merchant/category. Fashion &
// beauty pay the most (Ajio ~15%, Myntra/Nykaa ~10%, M&S ~13%); electronics &
// mobiles pay the least (~1-4%). Estimated commission = rate * deal price, so a
// mid-price fashion deal outranks both a cheap trinket and a pricey gadget.
// ---------------------------------------------------------------------------
const HIGH_COMMISSION_MERCHANTS = [
  { re: /ajio\.com|ajio\.in|\bajio\b/i, rate: 0.15, name: 'ajio' },
  { re: /marksandspencer|marks\s*&\s*spencer|\bm&s\b|faballey|\bnnn?ow\b|urbanic/i, rate: 0.13, name: 'fashion-brand' },
  { re: /myntra\.com|\bmyntra\b/i, rate: 0.10, name: 'myntra' },
  { re: /nykaa\.com|\bnykaa\b/i, rate: 0.10, name: 'nykaa' },
  { re: /flipkart\.com|\bflipkart\b/i, rate: 0.08, name: 'flipkart' },
  { re: /meesho\.com|\bmeesho\b/i, rate: 0.12, name: 'meesho' },
  { re: /tatacliq|tata\s*cliq/i, rate: 0.05, name: 'tatacliq' },
  { re: /amazon\.(in|com)/i, rate: 0.04, name: 'amazon' },
]
// Category-level rates for posts whose merchant link is a shortened/affiliate
// URL (fktr.in / earnkaro / bitly carry no host) — detect from the text.
function categoryCommissionRate(text) {
  const t = text || ''
  if (/\b(?:beauty|cosmetics?|makeup|lipstick|kajal|foundation|serum|moisturizers?|cream|lotion|perfumes?|deodorants?|deos?|shampoo|conditioner|skincare|mamaearth|wow|minimalist|nivea|lakme|maybelline|mcaffeine|beardo|dove)\b/i.test(t)) return { rate: 0.10, cat: 'beauty' }
  if (/\b(?:sarees?|kurtas?|kurtis?|dresses?|gowns?|tops?|leggings?|t-?shirts?|shirts?|jeans|pants?|trousers?|trackpants?|hoodies?|jackets?|sweaters?|shorts?|fashion|ethnic|lehenga|blouse|lingerie|night\s*suits?|footwear|sandals?|slippers?|heels?|loafers?|sneakers?|shoes?|handbags?|purses?|jewellery|jewelry|earrings?|necklaces?|bangles?|sunglasses?|watches?)\b/i.test(t)) return { rate: 0.09, cat: 'fashion' }
  if (/\b(?:home|kitchen|cookware|bedsheets?|towels?|curtains?|containers?|bottles?|storage|decor|furniture|mats?|lamps?|lights?|blankets?|pillows?|cushions?)\b/i.test(t)) return { rate: 0.07, cat: 'home' }
  if (/\b(?:mobiles?|phones?|laptops?|tablets?|tv|televisions?|fridges?|refrigerators?|washing\s*machines?|ac\b|air\s*conditioners?|camera|earphones?|headphones?|earbuds|speakers?|smartwatches?|chargers?|power\s*banks?|electronics?|appliances?|geys?er|purifier|router|monitor|printer|drone)\b/i.test(t)) return { rate: 0.03, cat: 'electronics' }
  return null
}
function dealCommissionInfo(job) {
  const text = job?.text || ''
  const price = detectedPrice(text)
  // Prefer the merchant from the visible/display links; fall back to text brand.
  let rate = 0.05 // generic default
  let via = 'default'
  const allUrls = urlsIn(text).map(u => `${u} ${displayUrl(job, u)}`).join(' ')
  const merchant = HIGH_COMMISSION_MERCHANTS.find(m => m.re.test(allUrls) || m.re.test(text))
  if (merchant) { rate = merchant.rate; via = merchant.name }
  else {
    const cat = categoryCommissionRate(text)
    if (cat) { rate = cat.rate; via = cat.cat }
  }
  // Service offers (Zomato/Swiggy/movies/cards) are time-sensitive specials;
  // rank on their own tier regardless of CPS.
  const isService = isServiceOffer(text)
  const estCommission = price != null ? rate * price : null
  // Ranking tier 0..4 for the priority vector.
  let tier = 0
  if (isService) tier = 3
  else if (estCommission != null) {
    if (rate >= 0.12) tier = Math.max(tier, estCommission >= 120 ? 4 : 3)
    else if (rate >= 0.09) tier = Math.max(tier, estCommission >= 150 ? 4 : estCommission >= 60 ? 3 : 2)
    else if (rate <= 0.04) tier = Math.max(tier, estCommission >= 400 ? 3 : estCommission >= 150 ? 2 : 1)
    else tier = Math.max(tier, estCommission >= 200 ? 3 : estCommission >= 80 ? 2 : 1)
  }
  return { rate, via, estCommission, tier, isService }
}
// Best-deal gate verdict: checked before ANY WhatsApp send. Failing posts are
// skipped (never sent) and the reason is logged. WhatsApp is CURATED — a photo
// alone no longer passes; a deal must be a clear best-tier post or earn the
// quality minimum, and expensive weak-discount junk is explicitly rejected.
function passesBestDealGate(job) {
  if (!BEST_DEAL_GATE) return { ok: true, reason: '' }
  const text = job.text || ''
  const urls = urlsIn(text)
  if (!urls.length) return { ok: false, reason: 'no link in post' }
  if (!extractDealName(text)) return { ok: false, reason: 'no readable deal name' }
  // No repeats: the same product does not come again inside the dedup window.
  const dupReason = duplicateProductReason(text, job)
  if (dupReason) return { ok: false, reason: dupReason }
  const q = dealQualityScore(job)
  const { score, discount, price, hasMedia, special, megaList } = q
  const priceTag = price != null ? `₹${price}` : 'none'
  // --- Hard auto-pass: unquestionably a top deal. ---
  if (special) return { ok: true, reason: '' }                                   // card/bank/80%+/service offer
  if (megaList) return { ok: true, reason: '' }                                  // curated multi-product list
  if (discount >= QUALITY_STRONG_DISCOUNT) return { ok: true, reason: '' }       // 70%+ off
  if (price != null && price <= UNDER99_MAX_PRICE) return { ok: true, reason: '' } // ₹99-or-less product
  if (discount >= 60) return { ok: true, reason: '' }                            // strong sale
  // --- Hard reject: not good enough for the WhatsApp channels. ---
  if (price == null && !discount) {
    return { ok: false, reason: `no price/discount signal (photo-only junk? media=${hasMedia})` }
  }
  if (price != null && price >= QUALITY_EXPENSIVE_PRICE && discount < QUALITY_WEAK_DISCOUNT) {
    return { ok: false, reason: `expensive weak deal (₹${price} @ ${discount}%)` }
  }
  if (score < QUALITY_MIN_SCORE) {
    return { ok: false, reason: `below top-deal quality score ${score}/${QUALITY_MIN_SCORE} (discount=${discount}%, price=${priceTag})` }
  }
  return { ok: true, reason: '' }
}
// Service URLs are not EarnKaro-monetized, so they must never be sent to the
// BestGAA link_cache provenance check; on direct-source jobs only OUR generated
// links are in the link_cache (raw source links are resolved + pass through).
// Amazon links carrying OUR Associates tag are self-proving (see
// isOurAmazonTagLink) and are also exempt — the cache is for conversion-API
// output, not for our direct Associates links, so a DB check would only ever
// reject good deals. Everything else keeps full verification.
function urlsForProvenance(job, urls) {
  return urls.filter(url => {
    if (isServiceUrl(url) || isServiceUrl(displayUrl(job, url))) return false
    if (isOurAmazonTagLink(url) || isOurAmazonTagLink(displayUrl(job, url))) return false
    if (job?.direct && !isOurGeneratedLink(url)) return false
    return true
  })
}
// ---------------------------------------------------------------------------
// Product-level dedup identity: the actual PRODUCT behind a link, so the same
// deal via a different source/shortener/price line still counts as a dup.
// ---------------------------------------------------------------------------
function productIdentity(url) {
  try {
    const u = new URL(url)
    const host = u.hostname.toLowerCase()
    if (host === 'amazon.in' || host.endsWith('.amazon.in') || host === 'amazon.com' || host.endsWith('.amazon.com')) {
      const match = u.pathname.match(/(?:\/dp\/|\/gp\/product\/|\/gp\/aw\/d\/)([A-Z0-9]{10})(?:[/?#]|$)/i)
      if (match) return `amazon:${match[1].toUpperCase()}`
    }
    if (host === 'flipkart.com' || host.endsWith('.flipkart.com')) {
      const match = u.pathname.match(/\/(?:products|p)\/([A-Za-z0-9_-]+)/i)
      if (match) return `flipkart:${match[1].toLowerCase()}`
    }
    if (host === 'myntra.com' || host.endsWith('.myntra.com') || host === 'ajio.com' || host.endsWith('.ajio.com')) {
      const match = u.pathname.match(/\/([A-Za-z0-9_-]+)\/([A-Za-z0-9_-]+)/)
      if (match) return `${host.replace(/^www\./, '')}:${match[1].toLowerCase()}:${match[2].toLowerCase()}`
    }
    const segs = u.pathname.split('/').filter(Boolean).map(s => s.toLowerCase()).slice(-2)
    if (segs.length >= 1 && segs.join('/').length >= 6) return `${host.replace(/^www\./, '')}:${segs.join('/')}`
    return null
  } catch {
    return null
  }
}
function duplicateProductReason(text, job = null) {
  const now = Date.now()
  const windowMs = PRODUCT_DEDUP_HOURS * 3600_000
  // Direct-source jobs dedup on the RESOLVED merchant page (the real product
  // behind an amzn.to/fkrt.co shortener), so a deal already posted via our own
  // channels is never double-posted from the raw source.
  const ids = new Set(urlsIn(text).map(url => productIdentity(displayUrl(job, url))).filter(Boolean))
  for (const id of ids) {
    const last = state.sentProducts?.[id]
    if (last && now - last <= windowMs) return `duplicate product within ${PRODUCT_DEDUP_HOURS}h (${id})`
  }
  // Shortlink products have no ASIN/slug — the name+price key catches them.
  return namePriceDupReason(text, job)
}
// Record a successfully sent job's products so the same product is not posted
// again inside the dedup window (intake + gate both consult this map).
function markProductSent(job) {
  state.sentProducts ||= {}
  const now = Date.now()
  const ids = new Set(urlsIn(job.text || '').map(url => productIdentity(displayUrl(job, url))).filter(Boolean))
  for (const id of ids) state.sentProducts[id] = now
  const keys = Object.keys(state.sentProducts)
  if (keys.length > 1000) {
    keys.sort((a, b) => state.sentProducts[a] - state.sentProducts[b])
    for (const key of keys.slice(0, keys.length - 1000)) delete state.sentProducts[key]
  }
  markNamePriceSent(job)
  saveState()
}
// ---------------------------------------------------------------------------
// Name+price dedup: a SECOND product identity for affiliate shortlinks that
// carry no ASIN/slug (fktr.in / earnkaro / bitly), where productIdentity()
// returns nothing. The SAME product re-posted by a source (different
// shortener/campaign, rewritten caption) with the SAME name + SAME price still
// counts as a duplicate. The price is part of the key on purpose: a genuine
// price DROP on the same product is a new deal and is allowed through.
// ---------------------------------------------------------------------------
function namePriceKey(text) {
  const name = extractDealName(text)
  const price = detectedPrice(text)
  if (!name || price == null) return null
  const norm = name
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/[^a-z0-9\u0900-\u097F\u0C00-\u0C7F]+/g, ' ')
    .split(/\s+/).filter(Boolean)
    .filter(w => !/^(deal|deals|offer|offers|loot|loots|sale|price|mrp|off|discount|only|just|rs|inr|the|a|an|new|best|top|today|day|grab|fast|hurry|link|buy|shop|now)$/.test(w))
    .slice(0, 8)
    .join(' ')
  if (norm.split(' ').length < 2) return null
  return `${norm}#${price}`
}
function namePriceDupReason(text, job = null) {
  const key = namePriceKey(text)
  if (!key) return null
  const last = state.sentNamePrice?.[key]
  if (last && Date.now() - last <= PRODUCT_DEDUP_HOURS * 3600_000) {
    return `same deal (name+price) within ${PRODUCT_DEDUP_HOURS}h`
  }
  return null
}
function markNamePriceSent(job) {
  const key = namePriceKey(job.text || '')
  if (!key) return
  state.sentNamePrice ||= {}
  state.sentNamePrice[key] = Date.now()
  const keys = Object.keys(state.sentNamePrice)
  if (keys.length > 1500) {
    keys.sort((a, b) => state.sentNamePrice[a] - state.sentNamePrice[b])
    for (const k of keys.slice(0, keys.length - 1500)) delete state.sentNamePrice[k]
  }
}
// ---------------------------------------------------------------------------
// Bitly shortening (WhatsApp display). Verification always happens on the
// original link; the short link is a cosmetic swap applied at format time.
// Results are cached in state so Bitly is never called twice for one URL.
// ---------------------------------------------------------------------------
function needsShortening(url, isList = false) {
  if (isServiceUrl(url)) return false
  const host = hostOf(url)
  if ([...ALREADY_SHORT_HOSTS].some(domain => host === domain || host.endsWith('.' + domain))) return false
  // USER RULE: Bitly quota is precious — spend it ONLY where raw links look
  // ugly: product LISTS (2+ links) and genuinely long links. A normal single
  // short amazon dp link posts as-is with our Associates tag.
  if (isList) return true
  return url.length > SHORTEN_MIN_LEN
}
async function shortenLongUrl(url) {
  state.shortLinks ||= {}
  const cached = state.shortLinks[url]
  if (cached) return cached
  // Bitly first (when WA_BITLY_TOKENS is configured)...
  for (const token of BITLY_TOKENS) {
    try {
      const response = await fetch('https://api-ssl.bitly.com/v4/shorten', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ long_url: url }),
        signal: AbortSignal.timeout(12_000),
      })
      if (response.status === 429) continue
      if (response.status === 200 || response.status === 201) {
        const data = await response.json()
        if (typeof data.link === 'string' && data.link.startsWith('http')) {
          state.shortLinks[url] = data.link
          saveState()
          return data.link
        }
      }
    } catch (error) {
      log.warn({ url: url.slice(0, 60), err: error.message }, 'Bitly shorten failed')
    }
  }
  // ...then the tokenless is.gd fallback (the same one the bot uses), so long
  // links get shortened even without a Bitly account.
  try {
    const response = await fetch(`https://is.gd/create.php?format=simple&url=${encodeURIComponent(url)}`, {
      signal: AbortSignal.timeout(12_000),
    })
    if (response.status === 200) {
      const link = (await response.text()).trim()
      if (link.startsWith('https://is.gd/')) {
        state.shortLinks[url] = link
        saveState()
        return link
      }
    }
  } catch (error) {
    log.warn({ url: url.slice(0, 60), err: error.message }, 'is.gd shorten failed; keeping the long link')
  }
  return null
}
// Amazon search/session junk (btn_ref=srctok-..., ds=, qid=, sprefix=, crid=)
// bloats /s? category links to 300-400 chars. Strip it BEFORE shortening so
// the Bitly points at a clean link; meaningful filters (k, i, rh, s, rnid)
// and our ?tag= stay, so Men/Women/Girls/Boys links remain distinct and
// monetized.
const AMAZON_JUNK_QUERY_KEYS = new Set([
  'btn_ref', 'btn_type', 'ds', 'dc', 'qid', 'sprefix', 'crid', 'sr',
  'pd_rd_r', 'pd_rd_w', 'pd_rd_wg', 'pf_rd_i', 'pf_rd_m', 'pf_rd_p',
  'pf_rd_r', 'pf_rd_s', 'pf_rd_t', 'content-id', 'spla', 'qu', 'srs', 'ref',
  'ref_',
])
function stripAmazonJunk(url) {
  try {
    const u = new URL(url)
    const host = u.hostname.toLowerCase()
    const isAmazon = host === 'amazon.in' || host.endsWith('.amazon.in') || host === 'amazon.com' || host.endsWith('.amazon.com')
    if (!isAmazon) return url
    for (const key of [...u.searchParams.keys()]) {
      if (AMAZON_JUNK_QUERY_KEYS.has(key.toLowerCase())) u.searchParams.delete(key)
    }
    return u.toString()
  } catch {
    return url
  }
}
// The URL actually shown to subscribers: shortened form first, then the
// resolved merchant page (direct-source jobs), then the original.
function displayUrl(job, url) {
  return job?.shortLinks?.[url] || job?.resolvedLinks?.[url] || url
}

function queuedJobPriorityVector(job) {
  const text = job.text || ''
  const price = detectedPrice(text)
  const priceRank = price != null && price <= 99 ? 3
    : price != null && price <= 199 ? 2
      : price != null && price <= 499 ? 1 : 0
  const mediaRank = job.media?.length ? 1 : 0
  const linkCount = urlsIn(text).length
  const listRank = job.largeList || linkCount >= 4 ? 2 : linkCount >= 2 ? 1 : 0
  const discountRank = explicitDiscount(text) || 0
  // Credit card / bank offers are high-value, time-sensitive: boost them.
  const cardRank = isSpecialOffer(text) ? 1 : 0
  const womenRank = isWomenAttractiveCategory(text) ? 1 : 0
  const categoryRank = (isPreferredShoppingCategory(text) || /\bshops?y\b/i.test(text)) ? 1 : 0
  // Recency rank: NEWER deals are posted FIRST. A fresh deal must not wait
  // behind hours-old inventory, so an older job never gets a head start.
  // Older jobs still age out of the queue via MAX_JOB_AGE, so nothing lingers
  // to be posted hours later unless nothing newer is ready.
  const recency = Math.max(0, 130 - Math.floor(Math.max(0, Date.now() - Number(job.createdAt || Date.now())) / 60_000))
  // t.me/under499loots is the user's main WhatsApp feed. It leads the order,
  // then photo/video, then price/list/discount ladder, then newest first.
  const primaryRank = (job.source || '').toLowerCase() === PRIMARY_SOURCE ? 1 : 0
  const mediaLead = MEDIA_FIRST ? mediaRank : 0
  // Commission-aware ordering: among the same primary/media tier, high-payout
  // deals (fashion/beauty, high-commission merchants, healthy order value) go
  // first so the channel earns the most per slot.
  const commissionTier = COMMISSION_RANKING ? dealCommissionInfo(job).tier : 0
  return [primaryRank, mediaLead, commissionTier, priceRank, mediaRank, listRank, discountRank, cardRank, womenRank, categoryRank, recency]
}
function compareQueuedJobs(a, b) {
  const left = queuedJobPriorityVector(a)
  const right = queuedJobPriorityVector(b)
  for (let index = 0; index < left.length; index++) {
    if (left[index] !== right[index]) return right[index] - left[index]
  }
  // Equal priority tiers: newest first.
  return Number(b.createdAt || 0) - Number(a.createdAt || 0)
}
function isNightUrgent(job) {
  const discount = explicitDiscount(job.text) || 0
  const linkCount = urlsIn(job.text).length
  return Boolean(
    job.special || job.largeList || discount >= 80 ||
    (discount >= 70 && job.media?.length) || linkCount >= 4
  )
}
function isQuietMinute(minute, start = parseClock(QUIET_START), end = parseClock(QUIET_END)) {
  return start <= end ? minute >= start && minute < end : minute >= start || minute < end
}
// True when the job's creation moment falls inside the night off-window.
// The window params default to the live QUIET_* env but accept explicit
// values so the trust policy is unit-testable without touching the clock.
function wasBornInQuietWindow(ts, start = QUIET_START, end = QUIET_END) {
  return isQuietMinute(minuteOfDay(Number(ts)), parseClock(start), parseClock(end))
}
// Best-tier = worth a later slot even if slightly aged: mega lists (4+ links),
// flagged specials/large lists, 80%+ super deals, 70%+ with media.
function isBestTierJob(job) {
  return Boolean(job.special || job.largeList || isNightUrgent(job))
}
// Returns a drop reason when an ORDINARY job must never be posted, else null.
function ordinaryJobExpiryReason(job, now = Date.now()) {
  const created = Number(job.createdAt || now)
  // USER RULE: a deal born inside the 02:00-06:00 pause is DEAD by 06:00 —
  // loot prices do not survive four hours. Only mega LISTS are worth the
  // morning flush; every other quiet-born deal is skipped, never posted late.
  if (wasBornInQuietWindow(created) && !isQuietMinute(minuteOfDay(now))) {
    const isList = Boolean(job.largeList) || urlsIn(job.text || '').length >= LARGE_LIST_MIN_LINKS
    if (!isList) return 'night-born deal expired by 06:00 (lists only)'
  }
  if (isBestTierJob(job)) return null
  if (now - created > Math.min(ORDINARY_MAX_AGE_MS, MAX_JOB_AGE_MS)) return 'ordinary deal too old to trust'
  return null
}
function shouldKeepForWhatsApp(source, text, hasMedia, special, largeList) {
  // Do not discard a valid shopping candidate at intake. Curation controls
  // dispatch order only; provenance/health/dedup decide whether it can send.
  return true
}
// Product identities pending in the queue (cached per job), so a direct-source
// copy of a deal that is already queued from our own channels never enters.
function pendingProductIds(job) {
  if (!job._ids) job._ids = urlsIn(job.text || '').map(url => productIdentity(displayUrl(job, url))).filter(Boolean)
  return job._ids
}
function enqueuePost(post) {
  const kind = classifySource(post)
  if (!kind) return
  const direct = kind === 'direct'
  const text = post.caption || post.text || ''
  const media = mediaFromPost(post)
  const group = post.media_group_id
  const id = group ? `${post.chat.id}:album:${group}` : `${post.chat.id}:${post.message_id}`
  let job = state.jobs.find(x => x.id === id)
  if (!job && text) {
    // ZERO duplicates, layer 1 (intake): exact deal key or campaign-content
    // fingerprint already sent? Never even queue it.
    const key = dealKey(text)
    const content = contentFingerprint(text)
    if (state.sent[key] || (content && state.sentContent?.[content])) {
      log.info({ id, source: post.chat.username }, 'intake skip: already posted (exact/content match)')
      return
    }
    // Layer 2: same product already posted inside the dedup window?
    const dupReason = duplicateProductReason(text)
    if (dupReason) {
      log.info({ id, source: post.chat.username, reason: dupReason }, 'intake skip: duplicate product')
      return
    }
    // Layer 3: same product already waiting in the queue (from any source)?
    const incomingIds = urlsIn(text).map(productIdentity).filter(Boolean)
    if (incomingIds.length && state.jobs.some(other => pendingProductIds(other).some(pid => incomingIds.includes(pid)))) {
      log.info({ id, source: post.chat.username }, 'intake skip: product already queued')
      return
    }
  }
  if (!job) {
    const createdAt = Date.now()
    // A text-only 4+ link sale (for example "Up to 83% Off" with categories)
    // is a MEGA LIST, not a one-card special. If it includes media, send the
    // special photo first and then continue the full mega list after the gap.
    const { largeList, special } = classifyPost(text, Boolean(media))
    if (!shouldKeepForWhatsApp(post.chat.username, text, Boolean(media), special, largeList)) {
      log.info({ id, source: post.chat.username }, 'curated WhatsApp skip: not a preferred/top deal')
      return
    }
    job = {
      id, source: post.chat.username, direct, text, media: [], createdAt, special, largeList,
      availableAt: createdAt + (group ? 5000 : 0), attempts: 0, nextMedia: 0, largeChunkIndex: 0,
      batchReadyAt: (special || largeList)
        ? createdAt + randomMs(SPECIAL_JITTER_MIN, SPECIAL_JITTER_MAX)
        : 0,
    }
    state.jobs.push(job)
  }
  if (text && !job.text) {
    job.text = text
    job._ids = null // pending-product cache: recompute now that text exists
    const classified = classifyPost(text, Boolean(media) || job.media.length > 0)
    const newlyLarge = classified.largeList
    const newlySpecial = classified.special
    if (newlySpecial || newlyLarge) {
      job.special ||= newlySpecial
      job.largeList ||= newlyLarge
      job.batchReadyAt = Math.min(job.batchReadyAt || Infinity, Date.now() + randomMs(SPECIAL_JITTER_MIN, SPECIAL_JITTER_MAX))
    }
  }
  if (media && !job.media.some(x => x.fileId === media.fileId)) job.media.push(media)
  if (group) job.availableAt = Date.now() + 5000
  saveState()
  log.info({ id, source: post.chat.username, media: job.media.length }, 'queued Telegram channel post')
}
async function pollTelegram() {
  while (!shuttingDown) {
    try {
      const updates = await tg('getUpdates', {
        offset: state.telegramOffset, timeout: 50, limit: 100,
        allowed_updates: ['channel_post'],
      })
      for (const update of updates) {
        state.telegramOffset = Math.max(state.telegramOffset, update.update_id + 1)
        if (update.channel_post) enqueuePost(update.channel_post)
      }
      pruneState()
      saveState()
    } catch (error) {
      log.error({ err: error.message }, 'Telegram polling error')
      await sleep(5000)
    }
  }
}

function validateAffiliateText(job, text) {
  const direct = Boolean(job?.direct)
  const urls = urlsIn(text)
  if (!urls.length) throw new Error('No URL in post')
  const sourceShorteners = new Set([
    'linkredirect.in', 'fkrt.cc', 'fkrt.co', 'fkrt.in', 'amzn.to', 'amzn.in',
    'ajiio.co', 'myntr.in', 'tinyurl.com', 'cutt.ly', 'rb.gy', 't.ly',
  ])
  for (const raw of urls) {
    const u = new URL(raw)
    const host = u.hostname.toLowerCase()
    // Direct-source jobs legitimately carry raw source shorteners: they are
    // resolved to the merchant page before posting (prepareDirectJob).
    if (!direct && [...sourceShorteners].some(domain => host === domain || host.endsWith(`.${domain}`))) {
      throw new Error('Foreign/source shortener blocked')
    }
    if (host === 'amazon.in' || host.endsWith('.amazon.in') || host === 'amazon.com' || host.endsWith('.amazon.com')) {
      // Raw Amazon pages on direct jobs get OUR tag appended at resolve time;
      // a link carrying someone else's tag is still always blocked.
      const tag = u.searchParams.get('tag')
      if (!direct && tag !== AMAZON_TAG) throw new Error('Amazon tag mismatch')
      if (direct && tag && tag !== AMAZON_TAG) throw new Error('Amazon tag mismatch')
    }
    const visiblePublisher = u.searchParams.get('affExtParam2')
    if (visiblePublisher && visiblePublisher !== PUBLISHER_ID) throw new Error('Publisher ID mismatch')
  }
  return urls
}
async function notBroken(url) {
  try {
    const response = await fetch(url, {
      redirect: 'follow', signal: AbortSignal.timeout(18_000),
      headers: { 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131 Safari/537.36' },
    })
    const body = (await response.text()).slice(0, 600_000).toLowerCase()
    if ([400, 404, 410, 451].includes(response.status)) return false
    return !body.includes('just a quick repair needed') &&
      !body.includes("we're doing everything we can to fix this") &&
      !body.includes('we’re doing everything we can to fix this') &&
      !body.includes('product is no longer available')
  } catch (error) {
    log.warn({ url, err: error.message }, 'link health inconclusive; merchant may block server checks')
    return true
  }
}
async function verifyBestGaaProvenance(urls) {
  const script = [
    'import json,sqlite3,sys',
    'urls=json.load(sys.stdin)',
    'db=sqlite3.connect(sys.argv[1])',
    'found=set()',
    'chunk=400',
    'for i in range(0,len(urls),chunk):',
    ' p=urls[i:i+chunk]',
    ' q=",".join("?" for _ in p)',
    ' found.update(r[0] for r in db.execute(f"SELECT affiliate_url FROM link_cache WHERE affiliate_url IN ({q})",p))',
    'print(json.dumps([u for u in urls if u not in found]))',
  ].join('\n')
  return await new Promise((resolve, reject) => {
    const child = spawn('python3', ['-c', script, BESTGAA_DB_PATH], { stdio: ['pipe', 'pipe', 'pipe'] })
    const output = [], errors = []
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('provenance DB timeout')) }, 12_000)
    child.stdout.on('data', chunk => output.push(chunk))
    child.stderr.on('data', chunk => errors.push(chunk))
    child.on('error', error => { clearTimeout(timer); reject(error) })
    child.on('close', code => {
      clearTimeout(timer)
      if (code !== 0) return reject(new Error(`provenance DB error: ${Buffer.concat(errors).toString('utf8').slice(0, 200)}`))
      try { resolve(JSON.parse(Buffer.concat(output).toString('utf8'))) }
      catch (error) { reject(new Error(`provenance DB response invalid: ${error.message}`)) }
    })
    child.stdin.end(JSON.stringify(urls))
  })
}
async function verifyJob(job) {
  // Direct-source jobs: resolve raw links to the merchant page first, so the
  // gate/dedup/health checks all run against the real product.
  if (job.direct) await prepareDirectJob(job)
  const urls = validateAffiliateText(job, job.text)
  // Service/lifestyle links (Zomato/Swiggy/Zepto/movies/cards) are not in the
  // BestGAA link_cache by design; only monetized store links get the provenance
  // DB check. Every link still gets the broken-destination health check.
  const toVerify = urlsForProvenance(job, urls)
  const missing = toVerify.length ? await verifyBestGaaProvenance(toVerify) : []
  if (missing.length) throw new Error(`Provenance mismatch: ${missing[0]}`)
  for (const url of urls) {
    const target = job?.resolvedLinks?.[url] || url
    if (!(await notBroken(target))) throw new Error(`Broken destination: ${target}`)
  }
  // All links verified — now swap very long DISPLAY links for shorts (Bitly
  // when WA_BITLY_TOKENS is set, else the tokenless is.gd fallback). Cap per
  // post protects the shortener quota on mega lists. A failed shortening keeps
  // the verified long link (never lose a deal).
  job.shortLinks ||= {}
  let shortenedCount = 0
  // A post with 2+ links is a product LIST: raw links look ugly stacked
  // together, so every list link earns a Bitly. Single-link posts only
  // shorten genuinely long links (quota protection).
  const isList = urls.length >= 2
  for (const url of urls) {
    if (shortenedCount >= 20) break
    if (job.shortLinks[url]) continue
    // Long Amazon category/search links: strip session junk first so the
    // Bitly target (and any unshortened fallback) is already clean and short.
    const display = stripAmazonJunk(job?.resolvedLinks?.[url] || url)
    if (!needsShortening(display, isList)) {
      // Junk-stripping alone made it short/clean — still show the clean form.
      if (display !== (job?.resolvedLinks?.[url] || url)) job.shortLinks[url] = display
      continue
    }
    const short = await shortenLongUrl(display)
    if (short) {
      job.shortLinks[url] = short
      shortenedCount++
    } else if (display !== (job?.resolvedLinks?.[url] || url)) {
      // Shortener down: at least post the junk-stripped clean long link.
      job.shortLinks[url] = display
    }
  }
}
function isPermanent(error) {
  // "Best-deal gate" = the post is not a best deal: retrying can never help,
  // so the job is dropped immediately (the user asked for skip, not retry).
  // "duplicate product" = same product inside the dedup window: dropping is
  // the correct behaviour too (a retry hours later would still be a dup).
  return /No URL|Foreign\/source shortener|tag mismatch|Publisher ID mismatch|Provenance mismatch|Broken destination|Best-deal gate|duplicate product/i.test(error.message)
}

async function resolveNewsletterJid(sock, target) {
  const value = (target || '').trim()
  if (!value) return null
  if (value.endsWith('@newsletter')) return value
  const match = value.match(/whatsapp\.com\/channel\/([A-Za-z0-9_-]+)/i)
  const code = match?.[1] || value
  if (typeof sock.newsletterMetadata !== 'function') throw new Error('Installed Baileys build has no newsletterMetadata support')
  const metadata = await sock.newsletterMetadata('invite', code)
  if (!metadata?.id) throw new Error(`Cannot resolve WhatsApp Channel invite: ${value}`)
  return metadata.id
}
async function resolveTargetJid(sock) {
  return resolveNewsletterJid(sock, WA_CHANNEL)
}
// TRUE when a post belongs in the Under-₹99 channel.
//  * single deal  : its detected price is <= ₹99 (MRP/percent excluded by the
//    robust detectedPrice); or it's an explicit "under 99 / below 99" deal.
//  * product LIST : only a BEST-discount list (70%+ / special) that actually
//    features sub-₹99 products — ordinary or expensive lists are skipped, per
//    the "only best discount + under ₹99" rule.
function dealPrices(text) {
  const prices = []
  for (const m of (text || '').matchAll(/(?:rs\.?|inr|₹)\s*([\d,]+)/gi)) {
    const before = (text.slice(Math.max(0, m.index - 10), m.index) || '').toLowerCase()
    const after = (text.slice(m.index + m[0].length, m.index + m[0].length + 12) || '').toLowerCase()
    if (/\b(mrp|was|struck|regular|original|list)\b\s*:?\s*$/.test(before)) continue
    if (/^\s*(?:off|discount|cashback|%|coupon)/.test(after)) continue
    const v = Number((m[1] || '').replace(/,/g, ''))
    if (Number.isFinite(v) && v >= 1 && v <= 1_000_000) prices.push(v)
  }
  return prices
}
// Count per-line PRODUCTS in a list: how many priced lines are ≤ ₹99 vs the
// total priced product lines (a link anchors a product line). Lines whose only
// price is an MRP/struck/percent value are ignored by dealPrices.
function under99LineItems(text) {
  const lines = String(text || '').split(/\r?\n/).map(l => l.trim()).filter(Boolean)
  let under = 0, total = 0
  for (const line of lines) {
    if (!/https?:\/\//i.test(line)) continue
    const prices = dealPrices(line)
    if (!prices.length) continue
    total += 1
    if (Math.min(...prices) <= UNDER99_MAX_PRICE) under += 1
  }
  return { under, total }
}
function under99Eligible(job) {
  const text = job?.text || ''
  const urls = urlsIn(text)
  const isList = Boolean(job?.largeList) || urls.length >= LARGE_LIST_MIN_LINKS
  const special = Boolean(job?.special) || isSpecialOffer(text)
  const hasMedia = Boolean(job?.media?.length)
  const discount = explicitDiscount(text) || 0
  const prices = dealPrices(text)
  const cheapest = prices.length ? Math.min(...prices) : null
  const explicitUnder99 = /\b(?:under|below|less\s*than|only)\s*[₹]?\s*99\b/i.test(text)
  const priceOk = (cheapest != null && cheapest <= UNDER99_MAX_PRICE) || explicitUnder99
  if (isList) {
    // STRICT USER RULE: the Under-₹99 channel features ONLY under-₹99
    // products. A list qualifies only when it actually contains under-₹99
    // items AND the MAJORITY of its priced items are under-₹99. A "best
    // discount" headline, special flag or photos never justify posting a
    // mostly-expensive list on the under-₹99 channel.
    const { under, total } = under99LineItems(text)
    // A list that explicitly says "under/below/only 99" but prints no
    // per-item prices is an under-₹99 feature by definition.
    if (explicitUnder99 && total === 0) return true
    if (under < 1) return false
    if (total <= 1) return true // the single priced item IS under-₹99
    return under >= Math.ceil(total / 2)
  }
  // Single deal: a best signal helps, but an explicit ≤₹99 price is enough.
  if (priceOk) return true
  return special && discount >= 80 && cheapest != null && cheapest <= UNDER99_MAX_PRICE
}
// Targets for a post: main Channel + (groups) + Under-₹99 channel when eligible.
// Digests/rotational batches pass job=null and go to the main channel only.
// ---------------------------------------------------------------------------
// Channel policy evaluation. The facts are computed once per job so every rule
// is readable, testable and identical across channels.
// ---------------------------------------------------------------------------
function channelFacts(job) {
  const text = (job && job.text) || ''
  const urls = urlsIn(text)
  const isList = Boolean(job && job.largeList) || urls.length >= LARGE_LIST_MIN_LINKS
  const discount = explicitDiscount(text) || 0
  const price = detectedPrice(text)
  const prices = dealPrices(text)
  const cardOffer = /\b(?:credit\s*card|debit\s*card|bank\s*offer|card\s*offer|no\s*cost\s*emi)\b/i.test(text)
  const special = Boolean(job && job.special) || isSpecialOffer(text)
  return { isList, discount, price, prices, cardOffer, special }
}
function eligibleForChannel(job, policy) {
  const facts = channelFacts(job)
  if (policy === 'main') return true
  if (policy === 'under99') {
    if (facts.isList) return true // lists reach both price channels, price-agnostic
    if (facts.cardOffer) return true
    return facts.price != null && facts.price <= UNDER99_MAX_PRICE
  }
  if (policy === 'under499') {
    if (facts.isList) return true
    if (facts.cardOffer) return true
    return facts.price != null && facts.price <= BEST_MAX_PRICE
  }
  if (policy === 'bestOf') {
    // "the best vi smart ga, kada ledu ante skip"
    if (facts.cardOffer || facts.special) return true
    if (facts.isList) return facts.prices.some(p => p <= BEST_MAX_PRICE) || facts.discount >= 60
    if (facts.price == null) return facts.discount >= QUALITY_STRONG_DISCOUNT
    if (facts.price <= UNDER99_MAX_PRICE) return facts.discount >= BEST_MIN_DISCOUNT
    return facts.price <= BEST_MAX_PRICE && facts.discount >= 60
  }
  return false
}
/**
 * Is THIS job the best deal the best-of channel could post right now? Compared
 * against every ready job that also qualifies for that channel, so a burst of ten
 * deals produces exactly ONE winner there and the rest stay on the paced main
 * feed. Ties break to the newer post (matching the queue's newest-first policy).
 */
function isBestOfMoment(job) {
  if (!bestOfJid || !job) return true
  const cooldown = BEST_OF_COOLDOWN_SECONDS * 1000
  if (cooldown && Date.now() - (Number(state.bestOfLastPickAt) || 0) < cooldown) return false
  const mine = dealQualityScore(job).score
  for (const other of state.jobs) {
    if (!other || other.id === job.id) continue
    if (Number(other.availableAt || 0) > Date.now()) continue
    if (!eligibleForChannel(other, 'bestOf')) continue
    const theirs = dealQualityScore(other).score
    if (theirs > mine || (theirs === mine && Number(other.createdAt || 0) > Number(job.createdAt || 0))) return false
  }
  return true
}
function noteBestOfPick(job) {
  if (!job || job._bestOfNoted) return
  job._bestOfNoted = true
  state.bestOfLastPickAt = Date.now()
  if (!SELF_TEST) saveState() // never touch the live state file from --self-test
}

function secondaryEligible(job) {
  if (!under99Jid) return false
  // Mirror mode: both channels get everything (digests included).
  if (CHANNEL_ALL_POSTS) return true
  // Tiered mode (default): the second channel is the Under-₹99 shelf, so only
  // under-₹99 content belongs there; a digest stays a main-channel item.
  return Boolean(job) && under99Eligible(job)
}
function targetsFor(job) {
  const list = []
  if (targetJid) list.push(targetJid)
  for (const jid of [under99Jid, under499Jid, bestOfJid]) {
    if (!jid || list.includes(jid)) continue
    const policy = CHANNEL_POLICY_OF_JID.get(jid) || 'under99'
    // A digest (no job) stays a main-channel item unless everything is mirrored.
    if (!CHANNEL_ALL_POSTS && !job) continue
    if (!CHANNEL_ALL_POSTS && !eligibleForChannel(job, policy)) continue
    if (policy === 'bestOf' && !isBestOfMoment(job)) continue
    if (policy === 'bestOf') noteBestOfPick(job)
    list.push(jid)
  }
  for (const jid of groupJids) if (!list.includes(jid)) list.push(jid)
  return list
}
async function resolveGroupTarget(sock, value) {
  const raw = (value || '').trim()
  if (!raw) throw new Error('empty group target')
  // Persisted cache: a JID/phone/invite resolved once is stored in state, so a
  // reconnect never calls groupAcceptInvite again (that API is a join/invite
  // query; repeating it on every reconnect looks like group-spam and can make
  // WhatsApp disconnect/limit the account).
  state.groupJidCache ||= {}
  if (state.groupJidCache[raw]) return state.groupJidCache[raw]
  let v = raw
  let jid = null
  if (/^[0-9][0-9-]*@g\.us$/i.test(v)) jid = v.toLowerCase()
  else if (/^\d{8,15}$/.test(v)) jid = `${v}@g.us`
  else {
    // Full invite link (https://chat.whatsapp.com/CODE) — extract the code so
    // WA_GROUPS can be filled with a copied group-invite URL directly.
    const inviteLink = v.match(/(?:chat\.)?whatsapp\.com\/(?:invite\/)?([A-Za-z0-9_-]{10,})/i)
    if (inviteLink) v = inviteLink[1]
    // Invite code: resolve to a JID exactly once, then cache it.
    if (typeof sock.groupAcceptInvite !== 'function') throw new Error('Installed Baileys build has no groupAcceptInvite support')
    const resolved = await sock.groupAcceptInvite(v)
    if (resolved && /@g\.us$/i.test(resolved)) jid = String(resolved).toLowerCase()
  }
  if (!jid) throw new Error(`cannot resolve group target: ${raw}`)
  state.groupJidCache[raw] = jid
  saveState()
  return jid
}
// Every fan-out target for a post (newsletters first, then groups). job=null
// (rotational digest) = main channel only.
function allTargets(job = null) {
  return targetsFor(job)
}
// Newsletters use the ack-verified upload path; everything else is a normal
// WA send (groups). Both the main channel and the Under-₹99 channel are
// newsletters.
function isNewsletterTarget(jid) {
  return jid === targetJid || (!!under99Jid && jid === under99Jid)
    || (!!under499Jid && jid === under499Jid) || (!!bestOfJid && jid === bestOfJid)
    || /@newsletter$/i.test(jid || '')
}
// Per-target send marks survive retries so a failed group never causes a
// duplicate on the targets that already received the message. Jobs carry
// their own marks; digest batches (no single job) use the persisted state list.
function marksFor(job) {
  if (job && typeof job === 'object') {
    job.sentTargets ||= []
    return job.sentTargets
  }
  state.textMarks ||= []
  return state.textMarks
}
// Sends one text message to ALL targets (Channel via the ack-verified
// newsletter path, groups via the normal send). A Channel failure propagates
// (existing retry/fallback logic applies); a group failure is logged and the
// remaining targets are still served.
async function broadcastText(sock, job, tag, text) {
  const targets = allTargets(job)
  if (!targets.length) throw new Error('no WhatsApp target resolved')
  const marks = marksFor(job)
  for (const jid of targets) {
    const mark = `${tag}:${jid}`
    if (marks.includes(mark)) continue
    if (isNewsletterTarget(jid)) {
      await sendNewsletterText(sock, jid, text)
    } else {
      try {
        await withTimeout(sock.sendMessage(jid, { text: sanitizeOutbound(text) }), 90_000, `group text send ${jid}`)
      } catch (error) {
        log.warn({ jid, tag, err: error.message }, 'group text delivery failed; other targets unaffected')
        continue
      }
    }
    marks.push(mark)
    state.sentTimes.push(Date.now())
    saveState()
    if (jid !== targets[targets.length - 1]) await interTargetGap()
  }
}
// Sends one photo/video to ALL targets. Channel media uses the corrected
// newsletter upload path (ack-verified); groups use the normal media send.
async function broadcastMediaItem(sock, job, item, caption) {
  const data = await telegramFile(item.fileId)
  const isVideo = item.type === 'video'
  const groupCaption = sanitizeOutbound(caption || '') || undefined
  const groupContent = isVideo
    ? { video: data, mimetype: item.mimetype || 'video/mp4', caption: groupCaption }
    : { image: data, caption: groupCaption }
  const targets = allTargets(job)
  const marks = marksFor(job)
  for (const jid of targets) {
    const mark = `media:${item.fileId}:${jid}`
    if (marks.includes(mark)) continue
    if (isNewsletterTarget(jid)) {
      await sendNewsletterMedia(sock, jid, { buffer: data, type: item.type, mimetype: item.mimetype, caption })
    } else {
      try {
        await withTimeout(sock.sendMessage(jid, groupContent), 150_000, `group media send ${jid}`)
      } catch (error) {
        log.warn({ jid, err: error.message }, 'group media delivery failed; other targets unaffected')
        continue
      }
    }
    marks.push(mark)
    state.sentTimes.push(Date.now())
    saveState()
    if (jid !== targets[targets.length - 1]) await interTargetGap()
  }
}
function formatDigestItem(job, number, bodyMax = 220) {
  const urls = urlsIn(job.text)
  let label = extractDealName(job.text) || ''
  if (!label) {
    label = cleanDealText(job.text)
    for (const url of urls) label = label.replace(url, '')
    label = label
      .split('\n').map(ln => stripInlineCta(ln)).join('\n')
      .replace(/\b(?:buy\s+now|shop\s+now)\b/gi, '')
      .replace(/^[-–—_=]{3,}$/gm, '')
      .replace(/[ \t]+$/gm, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim()
  }
  // v17.5: no invented "Latest deal" text - a numbered item with just its
  // link is still truthful; a fabricated headline is not.
  if (!label) {
    const only = urls.map(url => displayUrl(job, url)).join('\n')
    return `*${number}.*\n${only}`
  }
  const price = detectedPrice(job.text)
  const discount = explicitDiscount(job.text)
  const badges = []
  if (price != null) badges.push(`₹${price.toLocaleString('en-IN')}`)
  if (discount) badges.push(`${discount}% OFF`)
  const badgeSuffix = badges.length ? `  (${badges.join(' • ')})` : ''
  if (label.length > bodyMax) label = `${label.slice(0, Math.max(1, bodyMax - badgeSuffix.length - 1)).trim()}…`
  // v17.5: the link goes on its own line bare - an "➜" we add is a character the
  // source never wrote.
  return `*${number}.* ${label}${badgeSuffix}\n${urls.map(url => displayUrl(job, url)).join('\n')}`
}

function buildBucketDigest(bucket, selected) {
  for (const bodyMax of [220, 160, 100, 60]) {
    // v17.5: the digest no longer opens with a "DEALS OF THE DAY / LOOT ZONE"
    // banner of our own making - the numbered deals and their links are the post.
    let digest = bucket.header ? `${bucket.header}\n\n` : ''
    selected.forEach((job, index) => {
      const separator = index ? '\n\n━━━━━━━━━━━━━━━━━━\n\n' : ''
      digest += separator + formatDigestItem(job, index + 1, bodyMax)
    })
    digest = digest.replace(/^\n+/, '')
    if (digest.length <= DIGEST_MAX_CHARS) return digest
  }
  return null
}

async function prepareRotationalDigest(quietOnlyUnder99 = false) {
  state.bucketReadyAt ||= {}
  const start = Number(state.rotationIndex || 0) % BUCKETS.length

  for (let offset = 0; offset < BUCKETS.length; offset++) {
    const bucketIndex = (start + offset) % BUCKETS.length
    const bucket = BUCKETS[bucketIndex]
    if (quietOnlyUnder99 && bucket.source !== 'under99deals11') continue
    const raw = state.jobs
      .filter(job => !job.special && !job.largeList && (job.source || '').toLowerCase() === bucket.source && job.availableAt <= Date.now())
      .sort((a, b) => a.createdAt - b.createdAt)

    const candidates = []
    const localKeys = new Set()
    for (const job of raw) {
      const key = dealKey(job.text)
      const content = contentFingerprint(job.text)
      // ZERO duplicates: exact key, in-batch repeat AND campaign-content
      // fingerprint (same list re-posted with a different shortlink) all skip.
      if (state.sent[key] || localKeys.has(key) || (content && (state.sentContent?.[content] || localKeys.has(content)))) {
        state.jobs = state.jobs.filter(x => x.id !== job.id)
        log.info({ id: job.id, bucket: bucket.source }, 'rotational duplicate skipped')
        continue
      }
      job.key = key
      candidates.push(job)
      localKeys.add(key)
      if (content) localKeys.add(content)
    }

    let desiredCount = bucket.threshold
    if (candidates.length < bucket.threshold) {
      const oldestAge = candidates.length ? Date.now() - candidates[0].createdAt : 0
      if (candidates.length >= bucket.flushMin && oldestAge >= bucket.flushAfter) {
        desiredCount = candidates.length
      } else if (candidates.length >= 1 && oldestAge >= 90 * 60_000) {
        desiredCount = 1
      } else {
        delete state.bucketReadyAt[bucket.source]
        continue
      }
    }
    if (!state.bucketReadyAt[bucket.source]) {
      state.bucketReadyAt[bucket.source] = Date.now() + randomMs(ROTATION_JITTER_MIN, ROTATION_JITTER_MAX)
      log.info({ bucket: bucket.source, count: candidates.length, targetCount: desiredCount }, 'bucket ready; smart settle started')
      continue
    }
    if (Date.now() < state.bucketReadyAt[bucket.source]) continue

    const selected = []
    for (const job of candidates) {
      try {
        if (job.direct) await prepareDirectJob(job) // resolve first: gate dedups on the real product
        const verdict = passesBestDealGate(job)
        if (!verdict.ok) {
          state.jobs = state.jobs.filter(x => x.id !== job.id)
          log.info({ id: job.id, bucket: bucket.source, reason: verdict.reason }, 'best-deal gate: rotational item skipped')
          continue
        }
        await verifyJob(job)
        selected.push(job)
      } catch (error) {
        job.attempts = (job.attempts || 0) + 1
        job.lastError = error.message
        if (isPermanent(error) || job.attempts >= 10) {
          state.jobs = state.jobs.filter(x => x.id !== job.id)
          log.error({ id: job.id, bucket: bucket.source, err: error.message }, 'unverified rotational item blocked')
        } else {
          job.availableAt = Date.now() + Math.min(300_000, 5000 * 2 ** Math.min(job.attempts, 6))
        }
      }
      if (selected.length === desiredCount) break
    }
    if (selected.length < desiredCount) {
      delete state.bucketReadyAt[bucket.source]
      saveState()
      continue
    }

    const digest = buildBucketDigest(bucket, selected)
    if (!digest) {
      log.error({ bucket: bucket.source }, 'required product list exceeds safe WhatsApp text size; held for review')
      continue
    }
    saveState()
    return { selected, digest, bucket, bucketIndex }
  }
  saveState()
  return null
}

function formatSpecialCaption(job) {
  // v17.5: this caption used to lead with "🔥 *LOOT ZONE — India*" and
  // "🚨 *SPECIAL OFFER*" and close with "✅ Verified • Enjoy (Grab fast)". That
  // is text the source never wrote - and it is exactly the branding boilerplate
  // cleanDealText strips out of a SOURCE post, so publishing it ourselves was
  // inconsistent as well as unwanted. A special now carries the cleaned source
  // post and our links, nothing else.
  const urls = urlsIn(job.text)
  const multi = urls.length >= 2
  let body = formatPostBody(job, { includeLinks: multi })
    .replace(/^[-–—_=]{3,}$/gm, '').replace(/\n{3,}/g, '\n\n').trim()
  const links = multi ? '' : urls.map(url => displayUrl(job, url)).join('\n')
  const bodyLimit = Math.max(120, 1000 - links.length - 2)
  if (body.length > bodyLimit) body = `${body.slice(0, bodyLimit - 1).trim()}…`
  return [body, links].filter(Boolean).join('\n\n')
}


async function sendSpecialOffer(job) {
  if (job.direct) await prepareDirectJob(job) // resolve first: gate dedups on the real product
  const verdict = passesBestDealGate(job)
  if (!verdict.ok) throw new Error(`Best-deal gate: ${verdict.reason}`)
  // ZERO duplicates: exact-deal key AND campaign-content fingerprint are both
  // checked BEFORE any network verify (same protection the strict path has).
  const key = dealKey(job.text)
  const content = contentFingerprint(job.text)
  if (state.sent[key] || (content && state.sentContent?.[content])) return 'duplicate'
  await verifyJob(job)
  const caption = formatSpecialCaption(job)
  const item = job.media[0]
  if (item) {
    await broadcastMediaItem(wa, job, item, caption)
    job.nextMedia = 1
  } else {
    await broadcastText(wa, job, 'special', caption)
  }
  const sentAt = Date.now()
  state.sent[key] = sentAt
  state.sentContent ||= {}
  if (content) state.sentContent[content] = sentAt
  markProductSent(job)
  return 'sent'
}

function compactLargeLine(line, job, max = 650) {
  const urls = urlsIn(line)
  if (!urls.length) {
    const clean = stripInlineCta(line)
    return clean.length <= max ? clean : `${clean.slice(0, max - 1).trim()}…`
  }
  let label = line
  for (const url of urls) label = label.replace(url, '')
  label = stripInlineCta(label).replace(/\s*[:\-–—]+\s*$/, '').trim()
  const links = urls.map(url => displayUrl(job, url)).join('\n')
  const labelMax = Math.max(30, max - links.length - 1)
  if (label.length > labelMax) label = `${label.slice(0, labelMax - 1).trim()}…`
  return `${label ? `${label}\n` : ''}${links}`.trim()
}
function buildLargeListChunks(job) {
    // v17.5: the list used to be wrapped in our own "🔥 *LOOT ZONE — India* /
  // 🛍️ *MEGA DEAL LIST* / ✅ Verified deals • Enjoy (Grab fast)" banner. The
  // source's own first line (whatever it wrote) is the headline now; nothing of
  // ours is bolted on. `header` stays as an empty string so the chunking loop
  // below keeps working unchanged.
  const header = ''
  const cleaned = cleanDealText(job.text)
    .replace(/\b(?:buy\s+now|shop\s+now)\b/gi, '')
    .replace(/^[-–—_=]{3,}$/gm, '').replace(/\*\*/g, '').replace(/\n{3,}/g, '\n\n').trim()
  const lines = cleaned.split(/\n/)
    .map(line => compactLargeLine(line.trim(), job))
    .filter(line => line && line.trim() && !/^➜\s*$/.test(line.trim()))
  const chunks = []
  let current = header
  for (const line of lines) {
    const addition = `${current === header ? '' : '\n'}${line}`
    if (current.length + addition.length > DIGEST_MAX_CHARS - 32) {
      chunks.push(current.trim())
      current = `${header}${line}`
    } else {
      current += addition
    }
  }
  if (current.trim() !== header.trim()) chunks.push(current.trim())
  return chunks
}
async function sendLargeListPart(job) {
  const key = job.key || dealKey(job.text)
  job.key = key
  const index = Number(job.largeChunkIndex || 0)
  const content = contentFingerprint(job.text)
  if (index === 0) {
    // ZERO duplicates: exact key AND campaign-content fingerprint, before verify.
    if ((state.sent[key] || (content && state.sentContent?.[content])) && !job.specialSent) return { result: 'duplicate', done: true }
    // VERIFY BEFORE SENDING: even a mega list must clear the top-deal gate
    // (readable name, no dup, quality). A weak/expensive list is dropped, not
    // posted to the WhatsApp channels.
    const verdict = passesBestDealGate(job)
    if (!verdict.ok) throw new Error(`Best-deal gate: ${verdict.reason}`)
    await verifyJob(job)
  }
  const chunks = buildLargeListChunks(job)
  if (!chunks.length) throw new Error('Large list formatting produced no content')
  if (index >= chunks.length) return { result: 'sent', done: true }
  const text = chunks.length > 1 ? `${chunks[index]}\n\n📄 Part ${index + 1}/${chunks.length}` : chunks[index]
  await broadcastText(wa, job, `chunk-${index}`, text)
  job.largeChunkIndex = index + 1
  state.sent[key] = Date.now() // Prevent a duplicate source copy while later chunks are pending.
  if (index === 0) {
    markProductSent(job) // Mark once per job, on its first chunk.
    state.sentContent ||= {}
    if (content) state.sentContent[content] = Date.now()
  }
  return { result: 'sent', done: job.largeChunkIndex >= chunks.length, part: index + 1, total: chunks.length }
}

async function sendStrictSourceJob(job) {
  // VERIFY BEFORE SENDING: a post that is not a best deal is skipped, never
  // sent (the user's explicit requirement). The reason is logged and the job
  // is dropped as permanent.
  if (job.direct) await prepareDirectJob(job) // resolve first: gate dedups on the real product
  const verdict = passesBestDealGate(job)
  if (!verdict.ok) throw new Error(`Best-deal gate: ${verdict.reason}`)
  await verifyJob(job)
  const key = job.key || dealKey(job.text)
  const content = contentFingerprint(job.text)
  job.key = key
  // Fully sent on an earlier dispatch? Nothing to do. A PARTIAL send — the
  // photo/text reached the Channel (or some targets) but the job then crashed
  // or a group failed — is NOT a duplicate: per-target marks (sentTargets /
  // nextMedia / strictTextSent) skip what already went out and resume the rest,
  // so a deal is never double-posted AND never lost. state.sent is written only
  // after a successful send; each target's mark is saved right after it is
  // delivered, which is what actually protects a restarted dispatch.
  if ((state.sent[key] || (content && state.sentContent?.[content]))
      && !(job.nextMedia > 0 || job.strictTextSent)) return 'duplicate'
  // The post is the cleaned source text in source order, with our links on
  // their own lines (v17.5 - no synthetic title/badges).
  const body = formatWhatsAppPost(job)
  const media = job.media || []
  // The user wants photo + text + link in a SINGLE neat post. WhatsApp captions
  // carry up to ~1024 chars; keep the whole structured body on the photo so the
  // deal is never split, and only spill extra long text to a follow-up after a
  // gap (never lost, still source-derived). When there is no photo we send the
  // structured text alone.
  const captionMax = 1024
  const captionFits = body.length > 0 && body.length <= captionMax
  let mediaDelivered = false

  // Photo/video first: the user's top display preference. The Channel gets the
  // corrected newsletter upload path (ack-verified); every group gets the
  // normal media send.
  for (let index = Number(job.nextMedia || 0); index < media.length; index++) {
    const item = media[index]
    // First photo gets the whole structured text+link as its caption (one neat
    // post). Subsequent album photos are sent with no duplicate caption.
    const caption = index === 0 && captionFits ? body : ''
    try {
      await broadcastMediaItem(wa, job, item, caption)
      mediaDelivered = true
      job.nextMedia = index + 1
      saveState()
    } catch (error) {
      job.mediaFailures = (job.mediaFailures || 0) + 1
      log.warn({ id: job.id, attempt: job.mediaFailures, err: error.message }, 'Channel media delivery failed')
      // Retry the upload a few times; after that never lose the deal — post the
      // structured text with the affiliate links so nothing is dropped.
      if (job.mediaFailures < 3) throw error
      log.error({ id: job.id }, 'media unavailable after retries; posting text-only fallback so the deal is not lost')
      job.nextMedia = media.length
      saveState()
      break
    }
    if (index + 1 < media.length) await interTargetGap()
  }

  // There are exactly three cases, all source-derived and none dropped:
  //   a) photo + text fits caption -> photo carries the whole deal, no follow-up
  //   b) photo + text too long     -> photo carries caption; long tail sent as a
  //      separate text message after the inter-message gap (still one deal)
  //   c) no photo                  -> structured text + links sent alone
  const textOnPhoto = mediaDelivered && captionFits
  const stillNeedsText = body && !job.strictTextSent && !textOnPhoto
  if (stillNeedsText) {
    if (mediaDelivered) await interTargetGap()
    await broadcastText(wa, job, 'strict-text', body)
    job.strictTextSent = true
    saveState()
  }
  if (!mediaDelivered && !job.strictTextSent && !body) throw new Error('empty post after cleaning')

  const sentAt = Date.now()
  state.sent[key] = sentAt
  state.sentContent ||= {}
  if (content) state.sentContent[content] = sentAt
  markProductSent(job)
  saveState()
  // sentTimes is pushed per delivered target inside the broadcasts.
  return 'sent'
}

async function worker() {
  while (!shuttingDown) {
    try {
      if (!waReady || !targetJid) {
        await sleep(5000); continue
const L = '\U0001f525\U0001f525 TOP DEAL OF THE DAY \U0001f525\U0001f525'
console.log('isPromoNoiseLine      :', isPromoNoiseLine(L))
console.log('isBrandingLine        :', typeof isBrandingLine === 'function' ? isBrandingLine(L) : 'n/a')
console.log('isCampaignBannerLine  :', typeof isCampaignBannerLine === 'function' ? isCampaignBannerLine(L) : 'n/a')
console.log('cleanDealText         :', JSON.stringify(cleanDealText(L)))
console.log('promo on product line :', isPromoNoiseLine('Top Loading Washing Machine Cover @ \u20b9260 (74% OFF)'))
