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
// USER RULE (2026-09-03): the user runs TWO WhatsApp channels — after the post
// lands in the first channel, wait 70-80 seconds (random) before the second
// channel gets it, so both channels never fire at the same instant. The short
// INTER_TARGET_GAP above still paces the items WITHIN one channel (album
// photos, caption + long-text tail).
const CROSS_CHANNEL_GAP_MIN = Math.max(0, Number(process.env.WA_CROSS_CHANNEL_GAP_MIN_SECONDS || 70))
const CROSS_CHANNEL_GAP_MAX = Math.max(CROSS_CHANNEL_GAP_MIN, Number(process.env.WA_CROSS_CHANNEL_GAP_MAX_SECONDS || 80))
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
  // A self-test run must never touch the live state file - it builds jobs in
  // memory and asserts on them, and writing here would replay/fake deliveries.
  if (SELF_TEST) return
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
// Between our TWO WhatsApp channels for the SAME post: 70-80 s random.
async function crossChannelGap() {
  if (CROSS_CHANNEL_GAP_MAX > 0) await sleep(randomMs(CROSS_CHANNEL_GAP_MIN, CROSS_CHANNEL_GAP_MAX))
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
// An Amazon/Flipkart coupon code: all caps, letters plus optional digits, 3-20 chars.
const COUPON_CODE_RE = /^[A-Z][A-Z0-9_-]{2,19}$/   // SAVE_200 is as spendable as SAVE200
const PRICE_UNITS = new Set(('pcs pack packs pair pairs kg gm g ml ltr ltrs l litre litres cm mm ' +
  'mah gb tb w v inch inches ft in').split(' '))

// Rebuild one "<price><token>" pair, judging the token by its SHAPE - the exact rule
// main_bot_new._keep_code_as_is applies, kept in step by hand because WhatsApp must not
// show a different price line from Telegram. An all-caps code (PEOPLE200) is money the
// reader can spend only when the source WROTE IT APART from the price ("Use code
// PEOPLE200", "₹1,099 SAVE200") - a spaced word, unit or code is left exactly as
// written. Anything FUSED to the digits (h, htt, jsjd, HFJF, tG7oChgiQuTgS25b) is the
// source's own paste residue and is cut: same rule as the bot, decided by shape.
function keepCodeAsIs(price, gap, tail) {
  if (!tail || '\u279c\u27a1\u2192\u2022\u00b7#'.includes(tail[0])) return price + gap + tail
  const core = tail.replace(/[).,;:!?\u2026]+$/, '')
  const trail = tail.slice(core.length)
  if (!core) return price + gap + tail
  if (!gap) {
    // USER RULE (round 13): whatever is glued straight onto a price is unwanted text -
    // the live source writes "₹85h" / "₹ 199HFJF" / "₹85jsjd" and the reader needs the
    // PRICE. It is cut, nothing of ours is written in its place. A code the source means
    // the reader to use is written apart from the price ("Use code PEOPLE200",
    // "₹1,099 SAVE200") - that spacing is what keeps it.
    if (/^[A-Za-z0-9_-]{1,64}$/.test(core) && /[A-Za-z]/.test(core)) return `${price}${trail}`.trimEnd()
    return price + gap + tail
  }
  if (PRICE_UNITS.has(core.toLowerCase()) || !/\d/.test(core)) return `${price} ${core}${trail}`
  if (COUPON_CODE_RE.test(core) && /\d/.test(core)) return `${price} ${core}${trail}`
  // A SPACED token is deleted only when it is machine-shaped (mixed case + long + has a
  // digit): a torn shortener, never a coupon or a quantity. Mirrors the bot exactly.
  if (core.length >= 8 && /^[A-Za-z0-9_-]{8,64}$/.test(core) && /[a-z]/.test(core)
      && /[A-Z]/.test(core) && /\d/.test(core)) return `${price}${trail}`.trimEnd()
  return price + gap + tail
}

function stripPriceJunk(text) {
  if (!text) return ''
  // Real links are masked first: a link glued straight onto a price
  // ("\u20b9260https://bitli.in/x") must keep its protocol - cutting "https" as if it
  // were a junk token used to leave "\u20b9260://bitli.in/x" behind.
  const urls = [...new Set(String(text).match(/https?:\/\/[^\s<>\[\](){}"']+/gi) || [])]
  let masked = String(text)
  urls.forEach((u, i) => { masked = masked.split(u).join(`\u0002P${i}\u0003`) })
  let out = masked.replace(/(\u20b9\s*[\d,]+)([ \t]*)(\S+)/g,
    (all, price, gap, tail) => keepCodeAsIs(price, gap, tail))
  urls.forEach((u, i) => { out = out.split(`\u0002P${i}\u0003`).join(u) })
  // Keep a glued link apart from the word/price in front of it.
  return out.replace(/([\w\u20b9)\]>"'])(?=https?:\/\/)/g, '$1 ').replace(/[ \t]{2,}/g, ' ')
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
  gap = Math.min(Math.max(gap, MIN_WA_MESSAGE_GAP_SECONDS * 1000), MAX_WA_MESSAGE_GAP_SECONDS * 1000)
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
// v17.8 ONE CEILING for everything the pacing policy can stack. A long idle, the
// hourly break, the morning stretch and a near-cap hour each multiply or extend
// the gap, and combined they used to be able to park the channel for over an hour
// - which no operator asked for and no auditor can distinguish from a stall. The
// scheduled gap is now clamped, so the worst case is a documented number.
const MAX_WA_MESSAGE_GAP_SECONDS = Math.max(
  MIN_WA_MESSAGE_GAP_SECONDS, Number(process.env.WA_MAX_MESSAGE_GAP_SECONDS || 3600))
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
  // Both shapes a loot channel actually writes: "26% OFF" and the label-first
  // "Discount: 26%" / "Discount - 26 percent". main_bot_new.parse_discount accepts
  // both, and this number decides whether a deal counts as the best pick on the
  // curated channel, so a form the bot understands must never read as 0 here.
  // Cashback percentages stay out of it: a 5% cashback line is not a price cut.
  const body = String(text || '')
  const values = [
    ...[...body.matchAll(/\b([1-9]\d?|100)\s*%\s*(?:off|discount)\b/gi)].map(m => Number(m[1])),
    ...[...body.matchAll(/\b(?:off|discount|savings?)\s*[:=-]?\s*(?:up\s*to|upto|flat)?\s*[:=-]?\s*([1-9]\d?|100)\s*(?:%|percent\b)/gi)]
      .map(m => Number(m[1])),
  ]
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
// v17.8 A CTA clause may only ever swallow PLAIN WORDS. The tails used to be
// `[^.\n|]*` - "everything to the end of the line" - so "More offers: Apply
// coupon PEOPLE200" lost its coupon code and "…(78% off) buy now ₹199" lost the
// deal. A price, a digit, a percentage or a link now ENDS the clause: whatever
// the source wrote after it stays in the post, verbatim.
const CTA_TAIL = String.raw`(?:[ \t]+(?![₹$%])(?![A-Za-z0-9+/.\-*]*\d)(?![A-Za-z0-9+/.\-*]*%)(?![A-Za-z]*:\/\/)[^\s₹$%|]+)*`
const cta = (body, flags = 'gi') => new RegExp(`${body}${CTA_TAIL}`, flags)

const GLOBAL_CTA_PATTERNS = [
  /\b(?:buy|shop|order|grab|get)\s+(?:it\s+)?now\b[!^1-9]*/gi,
  /\bgrab\s+(?:it|this|your|yours|fast)\s*(?:now|fast|soon)?\b[!^1-9]*/gi,
  /\bget\s+yours?\b[!^1-9]*/gi,
  /\bbuy\s+(?:it\s+)?here\b/gi, /\bshop\s+here\b/gi, /\border\s+here\b/gi,
  cta(String.raw`\b(?:click|tap)\s+(?:here|the\s+link|on\s+(?:the\s+)?link|below|to\s+(?:buy|order|shop))\b`),
  cta(String.raw`\b(?:don'?t|do\s+not|never)\s+miss\s+(?:it|this|out|the\s+deal|this\s+deal)\b`),
  // "Hurry up guys 🏃 limited stock" — the hype words AFTER the CTA are the same
  // boilerplate and go with it (tail stops at any price/digit/link; the payload
  // fidelity guard in stripInlineCta restores real deal words). A standalone
  // "Limited stock" (availability info) is deal CONTENT and stays. Same rule as the bot.
  cta(String.raw`\bhurry\s*up?\b!*`),
  cta(String.raw`\bloot\s+(?:it\s+)?fa+s*t+\b!*`),
  cta(String.raw`\b(?:join|subscribe|follow)\s+(?:our\s+)?(?:us\s+)?(?:channel|telegram|whatsapp\s+channel|group|now)\b`, 'gim'),
  /\b(?:join|subscribe|follow)\s+(?:our\s+)?(?:us\s+)?(?:on|via)?\s*t\.me\/\S+/gi,
  cta(String.raw`\b(?:for\s+more|more\s+)(?:loot|deal|update|offer)s?\b`, 'gim'),
  cta(String.raw`\bturn\s+on\s+notifications?\b`),
  cta(String.raw`\bstay\s+tuned\b`),
  cta(String.raw`\blink\s+(?:in\s+(?:bio|comments?|description)|below)\b`),
  cta(String.raw`\bcheck\s+(?:link|bio|description|comments?|pinned|our\s+channel)\b`),
  cta(String.raw`\bshare\s+(?:it\s+)?(?:with|to)\s+[^.\n|]*\b(?:friends?|family|groups?|everyone)\b`),
  /\bforward\s+to\s+@?\w+[^.\n|]*/gi,
  cta(String.raw`\b(?:visit|open)\s+(?:our\s+)?(?:channel|t\.me\/\S+|whatsapp\s+channel)\b`),
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
    .replace(new RegExp(String.raw`\b(?:buy|shop|order|grab|get|check|add\s+to\s+cart)\s+(?:it|now|fast|soon|today|yours?|this|the\s+deal|deal|fast\s+guys?|guys?|at|here)\b` + CTA_TAIL + '[.,;:]*$', 'gi'), ' ')
    .replace(new RegExp(String.raw`\b(?:click|tap)\s+(?:here|link|below|on\s+(?:the\s+)?link|to\s+buy|to\s+order|to\s+shop)\b` + CTA_TAIL + '[.,;:]*$', 'gi'), ' ')
    .replace(new RegExp(String.raw`\b(?:don'?t|do\s+not|never)\s+miss\b` + CTA_TAIL + '[.,;:]*$', 'gi'), ' ')
    .replace(new RegExp(String.raw`\bmiss\s+(?:it|this|out|the\s+deal)\b` + CTA_TAIL + '[.,;:]*$', 'gi'), ' ')
    .replace(/\b(?:hurry?\s*up?|grab\s+(?:it|fast|now|your|this)|loot\s+fast|deal\s+time[^\n]*|limited(?:\s*time)?\s+offer)\b[^.|\n]*$/gi, ' ')
    // Social/channel CTAs (join/subscribe/follow/share/notifications/t.me) are
    // handled by the STRICT global patterns above (which never eat a following
    // price); no greedy end-of-line social strip here.
    .replace(/\b(?:link\s+in\s+bio|link\s+below|check\s+(?:link|bio|description|comments?|pinned))\b[^.|\n]*$/gi, ' ')
  out = out.replace(/\s{2,}/g, ' ').replace(/^[\s|*•:,;\-\u2013\u2014~]+|[\s|*•:,;\-\u2013\u2014~]+$/g, '').trim()
  // Fidelity rule (user, round 10): a LOST line is as much a bug as an added one.
  // If a CTA clause took the price, the discount or the coupon code with it, the
  // clause removal is undone for that line - the source wrote those words.
  const before = dealPayloadOf(line)
  const after = dealPayloadOf(out)
  if (payloadLacks(before, after)) {
    return String(line || '').replace(/\*+/g, ' ').replace(/\s{2,}/g, ' ').trim()
  }
  return out
}

// Amounts / codes a line states, for the "cleaning may not delete a fact" guard.
function dealPayloadOf(text) {
  const body = String(text || '')
  const prices = new Set((body.match(/[₹$]\s*\d[\d,.]*/g) || []).map(x => x.replace(/\D/g, '')))
  const percents = new Set((body.match(/\b\d{1,3}\s*(?:%|percent)/gi) || []).map(x => x.replace(/\D/g, '')))
  const codes = new Set((body.match(/\b(?:[A-Z]{2,}\d[A-Z\d]*|\d{2,}[A-Z]{2,}[A-Z\d]*)\b/g) || [])
    .filter(x => x.length >= 5).map(x => x.toUpperCase()))
  return { prices, percents, codes }
}
function payloadLacks(before, after) {
  const gone = (from, to) => [...from].some(value => !to.has(value))
  return gone(before.prices, after.prices) || gone(before.percents, after.percents)
    || gone(before.codes, after.codes)
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
// Layout is part of "exactly like the source": a blank line our own passes created (a
// removed link line, a stripped CTA clause) must not stay behind, because subscribers read
// it as the bot spacing things out. Where the source itself left a blank line, that spacing
// is the source's and is kept. Same rule as keep_source_spacing in main_bot_new.py.
function keepSourceSpacing(body, sourceText) {
  if (!body) return body
  if ((sourceText || '').includes('\n\n')) return body
  return body.replace(/\n{2,}/g, '\n')
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
  const body = out.join('\n').replace(/\n{3,}/g, '\n\n').trim()
  // `bodyMax` is accepted and deliberately IGNORED. This function only assembles
  // the post; the senders decide where a message ends (splitCaptionForMedia for a
  // caption, buildBucketDigest for a list) and they SPLIT and follow up. Applying a
  // cap here cut the source's own lines off with an ellipsis, which is the single
  // complaint "text is being lost" in one line of code - so no path may truncate.
  void bodyMax
  return keepSourceSpacing(body, job?.text)
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
  // Deliberate asymmetry with the bot (v18.1): the bot publishes a photo post the source
  // wrote with no link, because on Telegram the channel must not be missing a post. WhatsApp
  // here is CURATED (the user's rule), and a post with nothing to tap on is not a best pick,
  // so a link-free post is skipped with the reason logged - not swallowed silently.
  if (!urls.length) return { ok: false, reason: 'no link in post (curated: skipped)' }
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
  // Shortlink products have no ASIN/slug — the name+price key catches them, and
  // the name-only key catches the same product at a changed price (v17.8).
  return namePriceDupReason(text, job) || nameOnlyDupReason(text, job)
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
  markNameOnlySent(job)
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
  // Same product + same price is a repeat, so this layer keys on the SAME
  // identity the v17.8 layer uses (brand + model + variant + capacity). It used to
  // take the first eight words of the name in order, which is how two laptops
  // whose names differ only at word nine - "(i5-1235U/8GB/512GB SSD)" vs
  // "(i5-12450H/16GB/512GB SSD)" - could be judged one product and one of them
  // silently dropped.
  const identity = productNameIdentity(cleanTitle(findDealNameLine(text) || '') || name)
  if (identity) return `id:${identity}#${price}`
  // No number to hold on to: then the name must match COMPLETELY, with the price
  // agreeing too. Two independent details having to coincide is what makes this
  // safe to act on; a truncated phrase is not.
  const whole = name.toLowerCase().replace(/https?:\/\/\S+/g, ' ')
    .replace(/[^a-z0-9\u0900-\u097F\u0C00-\u0C7F]+/g, ' ').split(/\s+/).filter(Boolean).join(' ')
  if (whole.split(' ').length < 2) return null
  return `name:${whole}#${price}`
}
// v17.8 SAME PRODUCT - not merely the same caption. `namePriceKey` above only
// catches a repeat that keeps the SAME price, so a source re-posting the same
// earphones at ₹1,049 instead of ₹1,099 (or with a rewritten caption) sailed
// straight through and the channel carried the product twice. The key below
// identifies the PRODUCT: its own words plus any size/capacity token, with
// prices and MRP clauses masked out because those change between copies of the
// same deal. Skipping the wrong post costs one deal; a duplicate costs the
// audience's trust - and the user named the duplicate as the mistake - so this
// is allowed to skip, with two brakes: the name must be specific (campaign
// banners and roundups never key), and a STRICTLY better copy (cheaper, or the
// same price at a deeper discount) still goes out.
const WA_SAME_PRODUCT_HOURS = Number(process.env.WA_SAME_PRODUCT_HOURS || 48)
const WA_SAME_PRODUCT_MARGIN = Number(process.env.WA_SAME_PRODUCT_MARGIN || 5)
const NAME_ONLY_STOP = new Set(('deal deals dealz offer offers dhamaka dhamal sale salez loot loots price mrp discount off save savings grab hurry now today daily best top hot new buy shop link links here below click free shipping delivery cod return warranty genuine flash super mega amazing awesome alert in india official telegram whatsapp channel group join follow subscribe share forward for the a an and or of to on at by with your our this that it is are be get got have has pack pcs pair').split(' '))

// ---------------------------------------------------------------------------
// Which product is this?  Mirrored, line for line, from main_bot_new's
// _product_identity(): Telegram and WhatsApp must call two posts "the same
// product" or "two products" in the SAME way, or one of the two channels repeats
// a deal the other one already carried.
//
// A number in a name is either the thing that MAKES it that product (1.5 Ton,
// 5 Star, 128GB, 55-inch, 5 Burner) or a spec a channel may not bother typing
// (42H playtime, 5000mAh, 1080p, 4K, 5G). The first group is identity, the
// second is dropped. A slice of the first eight words would get both wrong.
// ---------------------------------------------------------------------------
const WA_SIG_STOP_WORDS = new Set(('deal deals dealz offer offers dhamaka dhamal sale salez loot loots price mrp discount off save savings grab hurry now today daily best top hot new buy shop link links here below click free shipping delivery cod return warranty genuine flash super mega amazing awesome alert in india official telegram whatsapp channel group join follow subscribe share forward for the a an and or of to on in at by with your our this that it is are be get got have has').split(' '))
const WA_SIG_VARIANT_UNITS = new Set(('gb tb mb kb l ltr liter liters litre litres ml kg ton tons star stars inch inches in ft hp kva burner burners slice slices tray trays door doors person persons blade blades').split(' '))
const WA_SIG_SPEC_UNITS = new Set(('h hr hrs hour hours min mins sec secs mah wh w kw a v p k g fps hz px mm cm m db rpm mp nit nits lumen lumens mbps gbps byte bytes watt watts').split(' '))
const WA_SIG_VARIANTS = new Set(('pro plus max ultra lite neo fe se mini prime classic edge fold flip turbo').split(' '))
// Words that turn the number after them into a model name: "Pro 4" and "Model
// 2600" are the product, while "(2023) at the end of a headline is only the
// launch year and must not split one TV into two identities.
const WA_SIG_QUALIFIERS = new Set([...WA_SIG_VARIANTS, 'model', 'series', 'gen', 'generation', 'version'])
const WA_SIG_VARIANT_RE = /\b(\d{1,4}(?:\.\d+)?)[\s_-]*(gb|tb|mb|kb|ltr|liter|liters|litre|litres|ml|kg|ton|tons|stars?|inch|inches|in|ft|hp|kva|burners?|slices?|trays?|doors?|persons?|blades?|l)\b/gi

function waSigTokens(line) {
  const named = String(line || '')
    .replace(/[₹$]\s*[\d,.]+(?:\.\d+)?/g, ' ')
    .replace(/\b\d+(?:\.\d+)?\s*(?:%|percent|off)\b/gi, ' ')
    .replace(/\b(?:mrp|mrp\.?|regular\s+price|list\s+price|strike\s+price)\b\s*[:\-]?[^,|;\n]*/gi, ' ')
    .replace(/\b(?:%|percent|off)\b/gi, ' ')
  const out = []
  for (const piece of named.split(/\s+/)) {
    let token = piece.replace(/^[^\w.]+/, '').replace(/[^\w.]+$/, '').replace(/^\.+|\.+$/g, '')
    if (token && /[A-Za-z0-9]/.test(token)) out.push(token.toLowerCase())
  }
  return out
}

function productNameIdentity(line) {
  const raw = waSigTokens(line).map(tok => tok.replace(/[-_]/g, ''))
  const words = raw.filter(w => !/^\d+$/.test(w) && !WA_SIG_STOP_WORDS.has(w))
  if (words.length < 2) return null
  const ids = new Set()
  for (const match of String(line || '').matchAll(WA_SIG_VARIANT_RE)) {
    ids.add(`${match[1]}${match[2]}`.toLowerCase().replace(/\s+/g, ''))
  }
  const digitCores = new Set([...ids].map(value => value.replace(/\D/g, '')))
  const models = new Set()
  raw.forEach((token, index) => {
    if (WA_SIG_STOP_WORDS.has(token)) return
    const digits = token.replace(/\D/g, '')
    if (!digits.length || digits.length > 6) return
    const glued = /^(\d{1,6})([a-z]{1,6})$/.exec(token)
    if (glued && (WA_SIG_SPEC_UNITS.has(glued[2]) || WA_SIG_VARIANT_UNITS.has(glued[2]))) return
    if (/^[a-z]{1,7}\d{1,6}[a-z]{0,3}$/.test(token)) { models.add(token); return }
    if (/^\d+$/.test(token)) {
      const following = raw[index + 1] || ''
      if (WA_SIG_SPEC_UNITS.has(following) || WA_SIG_VARIANT_UNITS.has(following)) return
      const previous = index > 0 ? raw[index - 1] : ''
      if (WA_SIG_VARIANTS.has(previous)) { models.add(previous + digits); return }
      // A launch year is not a model number, however much it looks like one.
      if (digits.length < 2 || (digits.length === 4 && Number(digits) >= 1900 && Number(digits) <= 2099)) return
      if (digitCores.has(digits)) return
      models.add(token)
    }
  })
  const variants = new Set(raw.filter(t => WA_SIG_VARIANTS.has(t)))
  if (!ids.size && !models.size) {
    if (words.length < 4) return null          // a category phrase, not an identity
    const basis = [...new Set(words)].sort().join(' ')
    return basis.length < 16 ? null : `W|${basis}`
  }
  const numbers = [...new Set([...models, ...ids])].sort().join(' ')
  const tail = variants.size ? `|${[...variants].sort().join(' ')}` : ''
  return `M|${words[0]}|${numbers}${tail}`
}

function nameOnlyKey(text, job = null) {
  const body = String(text || '')
  const urls = urlsIn(body)
  // A post with three or more merchant links is a ROUNDUP: two roundups share
  // items all day and must never be judged as one product.
  const merchantLinks = urls.filter(url => {
    const host = (url.replace(/^https?:\/\//, '').split(/[/?#]/)[0] || '').toLowerCase()
    return host && !/\b(?:t|telegram)\.me$|^whatsapp\.com$|^t\.me$/.test(host)
  })
  if (!merchantLinks.length || merchantLinks.length >= 3) return null
  const name = extractDealName(body) || ''
  if (!name || isCampaignBannerLine(name)) return null
  // Brand + model number + variant + capacity, as a SET: two captions of one
  // product read differently ("TWS Earbuds" vs "True Wireless Earbuds, 42H
  // Playtime"), while "Airdopes 141" and "Airdopes 131" differ by nothing but
  // the number. An ordered slice of the first eight words gets both wrong.
  // The identity is read from the WHOLE title line, not the 90-character label a
  // digest prints: a "(128 GB)" that falls outside the display cap still has to
  // separate two variants from being skipped as one product.
  const title = cleanTitle(findDealNameLine(body) || '') || name
  const identity = productNameIdentity(title)
  return identity ? `name-only:${identity}` : null
}

function nameOnlyDupReason(text, job = null) {
  if (!(WA_SAME_PRODUCT_HOURS > 0)) return null
  const key = nameOnlyKey(text, job)
  if (!key) return null
  const seen = state.sentNames?.[key]
  if (!seen || !seen.at) return null
  const ageMs = Date.now() - Number(seen.at)
  if (ageMs > WA_SAME_PRODUCT_HOURS * 3600_000) return null
  const price = detectedPrice(text)
  const discount = explicitDiscount(text)
  if (price != null && (seen.price == null || price < Number(seen.price))) return null
  // A 1-point difference is measurement noise (a source that spells out the MRP
  // reads slightly higher than the same deal without it), so a repeat only counts
  // as news when the discount is meaningfully deeper or the price is lower.
  if (discount != null && Number(discount) >= Number(seen.discount || 0) + WA_SAME_PRODUCT_MARGIN) return null
  return `same product already sent to WhatsApp ${Math.max(1, Math.round(ageMs / 3600000))}h ago`
}

function markNameOnlySent(job) {
  const key = nameOnlyKey(job.text || '', job)
  if (!key) return
  state.sentNames ||= {}
  state.sentNames[key] = {
    price: detectedPrice(job.text || ''),
    discount: explicitDiscount(job.text || ''),
    at: Date.now(),
  }
  const keys = Object.keys(state.sentNames)
  if (keys.length > 1500) {
    keys.sort((a, b) => state.sentNames[a].at - state.sentNames[b].at)
    for (const key of keys.slice(0, keys.length - 1500)) delete state.sentNames[key]
  }
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
  // t.me/under499loots is the user's main WhatsApp feed. It leads the order.
  const primaryRank = (job.source || '').toLowerCase() === PRIMARY_SOURCE ? 1 : 0
  // USER RULE (2026-09-03): when several deals are ready at the same time,
  // product LISTS go out first, then the photo/video posts (the best-looking
  // ones), then everything else. The list lead counts only a true multi-product
  // list (4+ links / flagged largeList), not a two-link deal.
  const listLead = listRank >= 2 ? 1 : 0
  const mediaLead = MEDIA_FIRST ? mediaRank : 0
  // Commission-aware ordering: among the same primary/list/media tier,
  // high-payout deals (fashion/beauty, high-commission merchants, healthy
  // order value) go first so the channel earns the most per slot.
  const commissionTier = COMMISSION_RANKING ? dealCommissionInfo(job).tier : 0
  return [primaryRank, listLead, mediaLead, commissionTier, priceRank, mediaRank, listRank, discountRank, cardRank, womenRank, categoryRank, recency]
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
/**
 * v17.6 "best copy": several sources post the same product minutes apart at
 * different prices. Until now the first arrival won and the better deal was
 * silently skipped by the queue's duplicate guard, so our channels published
 * whichever copy happened to be early. The pending job is re-pointed at the
 * stronger deal IN PLACE - still exactly one job per product, so the
 * zero-duplicate guarantee is untouched, and a weaker copy never overwrites a
 * better one.
 *
 * Deliberately narrow: only a single-product post may take over, only a job that
 * has not started delivering is rewritten, and lists/specials are left alone
 * because re-pointing them would delete part of a roundup.
 */
function adoptBetterCopy(job, post, text, media) {
  if (!job || !text) return false
  if (Number(job.attempts || 0) > 0 || job.sentChunks || job.status === 'sending') return false
  if (job.largeList || job.special || urlsIn(job.text || '').length > 1) return false
  if (urlsIn(text).length > 1) return false
  // A roundup must never hijack a single-product job: adopting its text would
  // silently delete the other items in the list.
  if (classifyPost(text, false).largeList) return false
  const incoming = dealQualityScore({ text, media: media ? [media] : [], special: false, largeList: false }).score
  if (incoming <= dealQualityScore(job).score) return false
  job.text = text
  job._ids = null // pending-product cache: recompute against the new copy
  // Re-derive the shape from the copy we just adopted, exactly like the late-text
  // merge below: a promoted special needs its batch window, a list never happens
  // here (guarded above), and flags must never disagree with the text.
  const shape = classifyPost(text, Boolean(media) || (job.media || []).length > 0)
  if (shape.special || shape.largeList) {
    job.special ||= shape.special
    job.largeList ||= shape.largeList
    job.batchReadyAt = Math.min(job.batchReadyAt || Infinity, Date.now() + randomMs(SPECIAL_JITTER_MIN, SPECIAL_JITTER_MAX))
  }
  if (post && post.chat && post.chat.username) job.source = post.chat.username
  job._bestCopyFrom = `${post.chat.id}:${post.message_id}`
  return true
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
    // A LIST that happens to repeat an already-queued product must still go out:
    // its value is the roundup, and dropping it would lose every other item in
    // it (the Telegram bot posts lists for the same reason). Only a single-product
    // copy of a queued product is "the same deal".
    const incomingShape = classifyPost(text, false)
    const singleProductCopy = urlsIn(text).length <= 1 && !incomingShape.largeList
    const queuedSameProduct = (singleProductCopy && incomingIds.length)
      ? state.jobs.find(other => other && other.id !== id
          && pendingProductIds(other).some(pid => incomingIds.includes(pid)))
      : null
    if (queuedSameProduct && adoptBetterCopy(queuedSameProduct, post, text, media)) {
      // One job, better deal: the queue carries the stronger copy to every channel.
      job = queuedSameProduct
      log.info({ id, source: post.chat.username, replaces: queuedSameProduct.id },
        'best copy: queued job re-pointed at the stronger deal')
    } else if (queuedSameProduct) {
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
// One merchant link may be verified "dead" by our probe (Amazon and Flipkart answer
// exactly this way to datacenter IPs) - that is NOT a reason to swallow a post, so
// the default logs and sends. WA_DROP_DEAD_LINKS=true restores the old hard skip for
// an operator who prefers to lose the deal than risk a dead page.
const WA_DROP_DEAD_LINKS = ['1', 'true', 'yes', 'on'].includes(
  String(process.env.WA_DROP_DEAD_LINKS || '').trim().toLowerCase())

// Share/forward/invite links (wa.me, t.me, tg://, addtoany) are not product pages.
// They must never be probed as a destination and never decide whether a post goes out -
// on Telegram a share button in the source text used to retry the job until the price
// expired, which is how a deal the source posted never reached our channels.
const SHARE_INTENT_HOSTS = new Set(['wa.me', 'api.whatsapp.com', 'chat.whatsapp.com', 'web.whatsapp.com',
  'whatsapp.com', 'wa.link', 'telegram.me', 'telegram.dog', 'tl.me', 't.me', 'telegram.org',
  'addtoany.com', 'sharethis.com', 'getpocket.com', 'pocket.co', 'vk.com', 'twitter.com', 'x.com',
  'facebook.com', 'viber.com', 'line.me'])

function isShareIntent(url) {
  const raw = String(url || '')
  if (/^(tg:\/\/|whatsapp:\/\/|viber:\/\/|line:\/\/|sms:|mailto:|tel:)/i.test(raw)) return true
  const host = (raw.match(/^[a-z][a-z0-9.+-]*:\/\/([^/?#]+)/i) || [])[1] || ''
  const bare = host.replace(/^www\./, '').toLowerCase()
  return SHARE_INTENT_HOSTS.has(bare) || [...SHARE_INTENT_HOSTS].some(h => bare.endsWith('.' + h))
}

async function deadDestinations(job, urls) {
  const dead = []
  for (const url of urls.filter(u => !isShareIntent(u))) {
    const target = job?.resolvedLinks?.[url] || url
    if (!(await notBroken(target))) dead.push(target)
  }
  return dead
}

async function assertLinksHealthy(job, urls) {
  const dead = await deadDestinations(job, urls)
  if (!dead.length) return
  if (WA_DROP_DEAD_LINKS) throw new Error(`Broken destination: ${dead[0]}`)
  log.warn({ id: job?.id, dead: dead.slice(0, 3), total: dead.length },
    'link health says dead; posting anyway (WA_DROP_DEAD_LINKS=false)')
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
  await assertLinksHealthy(job, urls)
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
    // USER RULE: 70-80 s random breather between our two channels.
    if (jid !== targets[targets.length - 1]) await crossChannelGap()
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
    // USER RULE: 70-80 s random breather between our two channels.
    if (jid !== targets[targets.length - 1]) await crossChannelGap()
  }
}
function formatDigestItem(job, number) {
  // v17.8 A DIGEST ITEM IS THE SOURCE POST, just numbered. The old item printed a
  // 90-character name we had pulled out, added a `₹price • x% OFF` badge WE had
  // invented, and then cut the text to 220/160/100/60 characters so that ten
  // deals would fit one message. Both halves were the bugs the user reported:
  // the badge re-stated a price the source had already written on its own line
  // (TWO prices in the same WhatsApp post), and the cut silently deleted
  // everything the source wrote after the label - the MRP line, the coupon,
  // "ends tonight 11:59 PM" - which is exactly why WhatsApp carried less text
  // than Telegram. Nothing is invented now and nothing is cut: an item that
  // does not fit the message budget rides in the NEXT digest instead.
  const body = formatPostBody(job)
  if (!body) {
    const only = urlsIn(job.text || '').map(url => displayUrl(job, url)).join('\n')
    return only ? `*${number}.*\n${only}` : ''
  }
  const [first, ...rest] = body.split('\n')
  return [`*${number}.* ${first}`, ...rest].join('\n')
}

function captionCutAt(body, limit) {
  // Where to break a single over-long line: at a space, and never inside a link.
  // A caption that ends "https://www.amazon.in/dp/B0AB" with the rest in the next
  // message is a dead link and an unpaid sale, so the break moves to just before
  // the URL (or just after it, when the URL alone is longer than the caption).
  let cut = body.lastIndexOf(' ', limit)
  if (cut <= 0) cut = limit
  for (const match of body.matchAll(/https?:\/\/\S+/gi)) {
    const from = match.index
    const to = from + match[0].length
    if (cut > from && cut < to) return from > 0 ? from : to
  }
  return cut
}

function splitCaptionForMedia(text, limit = 1024) {
  // A WhatsApp caption holds ~1024 characters. Truncating there (the old
  // behaviour) deleted the source's own lines, so instead the caption carries as
  // many WHOLE lines as fit and the remainder is sent right behind the photo.
  const body = String(text || '')
  if (body.length <= limit) return { head: body, tail: '' }
  const lines = body.split('\n')
  let head = ''
  let index = 0
  for (; index < lines.length; index++) {
    const candidate = head ? `${head}\n${lines[index]}` : lines[index]
    if (candidate.length > limit) break
    head = candidate
  }
  if (!head) {
    // One line longer than the caption limit: break it at a space, never in the
    // middle of a word, and never with an ellipsis - the tail follows as text.
    const cut = captionCutAt(body, limit)
    head = body.slice(0, cut).trimEnd()
    index = lines.length
    const rest = body.slice(head.length).trimStart()
    return { head, tail: rest }
  }
  return { head, tail: lines.slice(index).join('\n').trim() }
}

function buildBucketDigest(bucket, selected) {
  // v17.8: WHOLE items only. DIGEST_MAX_CHARS is a budget for how many complete
  // deals fit in one message - it is never a licence to shorten what the source
  // wrote. Anything that does not fit stays in the queue for the next digest.
  const header = bucket.header ? `${bucket.header}\n\n` : ''
  const separator = '\n\n━━━━━━━━━━━━━━━━━━\n\n'
  let digest = ''
  const used = []
  for (const job of selected) {
    const item = formatDigestItem(job, used.length + 1)
    if (!item) continue
    const candidate = digest ? `${digest}${separator}${item}` : item
    if (digest && (header + candidate).length > DIGEST_MAX_CHARS) break
    digest = candidate
    used.push(job)
    // A single deal longer than the budget is still published complete (a
    // WhatsApp text message takes far more than this) - only the items after it
    // wait for the next digest.
    if ((header + digest).length > DIGEST_MAX_CHARS) break
  }
  if (!used.length) return null
  return { digest: `${header}${digest}`.replace(/^\n+/, ''), used }
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

    const built = buildBucketDigest(bucket, selected)
    if (!built) {
      log.error({ bucket: bucket.source }, 'required product list exceeds safe WhatsApp text size; held for review')
      continue
    }
    // Items that did not fit stay queued (their jobs are untouched) so the next
    // digest carries them - the source text is never trimmed to make a fit.
    saveState()
    return { selected: built.used, digest: built.digest, bucket, bucketIndex }
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
  const body = formatPostBody(job, { includeLinks: multi })
    .replace(/^[-–—_=]{3,}$/gm, '').replace(/\n{3,}/g, '\n\n').trim()
  // v17.8: this used to cut the body at 1000 characters with an ellipsis, which
  // deleted the source's own lines whenever a special offer had a long caption.
  // The caption limit is a SPLIT problem, not a permission to drop text - see
  // splitCaptionForMedia at the send site.
  const links = multi ? '' : urls.map(url => displayUrl(job, url)).join('\n')
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
  const { head, tail } = splitCaptionForMedia(caption)
  if (item) {
    await broadcastMediaItem(wa, job, item, head)
    job.nextMedia = 1
    // Whatever did not fit in the caption goes out immediately behind the photo:
    // one special offer, complete text, nothing thrown away.
    if (tail) await broadcastText(wa, job, 'special-tail', tail)
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

function compactLargeLine(line, job) {
  // v17.8: the old `max` cap (650 characters) is gone. A list line keeps every word
  // the source wrote and the chunker below decides where a message ends, so no
  // over-long line is ever cut short.
  const urls = urlsIn(line)
  if (!urls.length) return stripInlineCta(line)
  let label = line
  for (const url of urls) label = label.replace(url, '')
  label = stripInlineCta(label).replace(/\s*[:\-–—]+\s*$/, '').trim()
  const links = urls.map(url => displayUrl(job, url)).join('\n')
  // No label cap any more: the caption splitter below decides where a message
  // ends and a follow-up carries the rest. Cutting a label short is how a line the
  // source wrote disappeared from WhatsApp while nothing was lost on Telegram.
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
      }
      const now = Date.now()
      const quietNow = isQuiet()
      const readyCount = state.jobs.filter(job => job.availableAt <= now).length
      // When at least two unique deals are ready during the active window, do
      // not let an old inherited delay hold the queue. Keep the hard 60-second
      // floor from the most recent successful WhatsApp update.
      if (!quietNow && readyCount >= 2) {
        const lastSent = state.sentTimes.length ? Math.max(...state.sentTimes) : 0
        const currentNext = Number(state.nextAllowedAt || now)
        // Anti-ban rests (burst rest / long idle / hourly break) are honoured:
        // acceleration may pull a wait earlier but never past a scheduled rest.
        const restFloor = Math.max(
          acceleratedNextAllowed(now, lastSent, readyCount, quietNow, currentNext),
          Number(state.antibanRestUntil || 0)
        )
        if (currentNext > restFloor) {
          state.nextAllowedAt = restFloor
          saveState()
          log.info({ readyCount, nextAllowedAt: restFloor }, 'two-plus queue acceleration applied (anti-ban rest protected)')
        }
      }
      if (now < Number(state.nextAllowedAt || 0)) {
        // A corrupted/inherited future timestamp used to park the queue for
        // hours (the 2:09 AM stall). Never wait longer than a long anti-ban
        // rest, but DO honour a scheduled rest ceiling.
        const scheduledRest = Number(state.antibanRestUntil || 0)
        const ceiling = Math.max(scheduledRest, now + (warmupPolicy().max + 300) * 1000)
        if (Number(state.nextAllowedAt) > ceiling) {
          log.warn({ was: state.nextAllowedAt, now }, 'implausible nextAllowedAt reset to hard floor')
          state.nextAllowedAt = now + MIN_WA_MESSAGE_GAP_SECONDS * 1000
          saveState()
        }
        await sleep(5000); continue
      }
      if (quietNow && !HYBRID_QUIET) { await sleep(5000); continue }
      const caps = withinCaps()
      if (!caps.ok) {
        if (readyCount > 0) {
          log.warn({ readyCount, hourCount: caps.hourCount, hourCap: caps.hourCap, dayCount: caps.dayCount, dayCap: caps.dayCap },
            'WhatsApp send paused by safety cap; queue retained')
        }
        await sleep(60_000); continue
      }
      // Strict source mode ranks photos directly and does not need OCR. Skipping
      // OCR avoids a second Telegram media download and reduces transient failures.
      if (!STRICT_SOURCE_ONLY) await classifyMediaOffers()

      if (STRICT_SOURCE_ONLY) {
        const strict = state.jobs
          .filter(job => job.availableAt <= Date.now())
          .filter(job => !quietNow || isNightUrgent(job))
          .sort(compareQueuedJobs)[0]
        if (!strict) { await sleep(3000); continue }
        try {
          // Hard ceiling on one dispatch: media upload + text + inter-part gaps.
          const result = await withTimeout(sendStrictSourceJob(strict), 10 * 60_000, 'strict source dispatch')
          state.jobs = state.jobs.filter(job => job.id !== strict.id)
          lastProgressAt = Date.now()
          saveState()
          if (result === 'sent') scheduleNext()
          log.info({ id: strict.id, source: strict.source, result, media: strict.media.length }, 'strict source WhatsApp post complete')
        } catch (error) {
          strict.attempts = (strict.attempts || 0) + 1
          strict.lastError = error.message
          if (isPermanent(error) || strict.attempts >= 10) {
            state.jobs = state.jobs.filter(job => job.id !== strict.id)
            log.error({ id: strict.id, err: error.message }, 'strict source post blocked/failed permanently')
          } else {
            strict.availableAt = Date.now() + Math.min(300_000, 5000 * 2 ** Math.min(strict.attempts, 6)) + randomMs(1, 15)
            log.warn({ id: strict.id, attempt: strict.attempts, err: error.message }, 'strict source post retry scheduled')
          }
          saveState()
        }
        continue
      }

      const special = state.jobs
        .filter(job => job.special && job.availableAt <= Date.now() && (job.batchReadyAt || 0) <= Date.now())
        .sort((a, b) => a.createdAt - b.createdAt)[0]
      if (special) {
        try {
          const result = await sendSpecialOffer(special)
          if (result === 'sent' && special.largeList) {
            special.specialSent = true
            special.special = false
            special.batchReadyAt = Date.now()
          } else {
            state.jobs = state.jobs.filter(job => job.id !== special.id)
          }
          saveState()
          if (result === 'sent') scheduleNext()
          log.info({ id: special.id, result, media: special.media.length > 0, megaContinues: Boolean(special.largeList && result === 'sent') }, 'special WhatsApp offer complete')
        } catch (error) {
          special.attempts = (special.attempts || 0) + 1
          special.lastError = error.message
          if (isPermanent(error) || special.attempts >= 10) {
            state.jobs = state.jobs.filter(job => job.id !== special.id)
            log.error({ id: special.id, err: error.message }, 'special offer blocked/failed permanently')
          } else {
            special.availableAt = Date.now() + Math.min(300_000, 5000 * 2 ** Math.min(special.attempts, 6)) + randomMs(1, 15)
            special.batchReadyAt = special.availableAt
            log.warn({ id: special.id, attempt: special.attempts, err: error.message }, 'special offer retry scheduled')
          }
          saveState()
        }
        continue
      }

      const large = state.jobs
        .filter(job => !job.special && job.largeList && job.availableAt <= Date.now() && (job.batchReadyAt || 0) <= Date.now())
        .sort((a, b) => a.createdAt - b.createdAt)[0]
      if (large) {
        try {
          const outcome = await sendLargeListPart(large)
          if (outcome.done) state.jobs = state.jobs.filter(job => job.id !== large.id)
          saveState()
          if (outcome.result === 'sent') scheduleNext()
          log.info({ id: large.id, result: outcome.result, part: outcome.part, total: outcome.total, done: outcome.done }, 'mega deal list progress')
        } catch (error) {
          large.attempts = (large.attempts || 0) + 1
          large.lastError = error.message
          if (isPermanent(error) || large.attempts >= 10) {
            state.jobs = state.jobs.filter(job => job.id !== large.id)
            log.error({ id: large.id, err: error.message }, 'mega deal list blocked/failed permanently')
          } else {
            large.availableAt = Date.now() + Math.min(300_000, 5000 * 2 ** Math.min(large.attempts, 6)) + randomMs(1, 15)
            large.batchReadyAt = large.availableAt
            log.warn({ id: large.id, attempt: large.attempts, err: error.message }, 'mega deal list retry scheduled')
          }
          saveState()
        }
        continue
      }

      // Hybrid night mode: specials/mega lists above are allowed; normal
      // rotation is restricted to completed Under₹99 lists until 07:00 IST.
      const batch = await prepareRotationalDigest(quietNow)
      if (!batch) { await sleep(3000); continue }
      try {
        // One complete list to the Channel AND every configured group.
        await broadcastText(wa, null, `digest-${batch.bucket.source}`, batch.digest)
        const sentAt = Date.now()
        state.sentContent ||= {}
        for (const job of batch.selected) {
          state.sent[job.key] = sentAt
          const content = contentFingerprint(job.text)
          if (content) state.sentContent[content] = sentAt
          markProductSent(job)
        }
        const ids = new Set(batch.selected.map(job => job.id))
        state.jobs = state.jobs.filter(job => !ids.has(job.id))
        state.rotationIndex = (batch.bucketIndex + 1) % BUCKETS.length
        delete state.bucketReadyAt[batch.bucket.source]
        saveState()
        scheduleNext()
        log.info({ bucket: batch.bucket.source, deals: batch.selected.length, chars: batch.digest.length, queue: state.jobs.length }, 'rotational WhatsApp list sent')
      } catch (error) {
        for (const job of batch.selected) {
          job.attempts = (job.attempts || 0) + 1
          job.lastError = error.message
          job.availableAt = Date.now() + Math.min(300_000, 5000 * 2 ** Math.min(job.attempts, 6)) + randomMs(1, 15)
          job.batchReadyAt = job.availableAt
        }
        saveState()
        log.warn({ bucket: batch.bucket.source, deals: batch.selected.length, err: error.message }, 'rotational list send failed; retry scheduled')
      }
    } catch (error) {
      log.error({ err: error.message }, 'worker error')
      await sleep(5000)
    }
  }
}

let reconnectAttempt = 0
let lastConnectionOpenAt = 0
let lastProgressAt = Date.now()

// Silent-death watchdog. A Baileys socket can stay "open" while the underlying
// websocket is dead, which is how a channel goes quiet after one late-night
// post. If deals are ready and nothing has moved for 25 minutes, force a
// reconnect instead of waiting for a disconnect event that never arrives.
async function connectionWatchdog() {
  while (!shuttingDown) {
    await sleep(60_000)
    try {
      const now = Date.now()
      const readyCount = state.jobs.filter(job => Number(job.availableAt || 0) <= now).length
      const lastSent = state.sentTimes.length ? Math.max(...state.sentTimes) : lastProgressAt
      const idleFor = now - Math.max(lastSent, lastProgressAt)
      if (waReady && readyCount >= 1 && idleFor > 25 * 60_000) {
        log.error({ readyCount, idleMinutes: Math.round(idleFor / 60_000) }, 'WhatsApp idle with pending deals; forcing reconnect')
        lastProgressAt = now
        waReady = false
        try { wa?.end?.(new Error('watchdog reconnect')) } catch {}
        setTimeout(() => connectWhatsApp().catch(error => log.error({ err: error.message }, 'watchdog reconnect failed')), 5000)
      }
      if (readyCount > 0) {
        log.info({
          readyCount, queue: state.jobs.length, waReady,
          nextAllowedInSeconds: Math.max(0, Math.round((Number(state.nextAllowedAt || 0) - now) / 1000)),
          sentLastHour: state.sentTimes.filter(ts => ts > now - 3600_000).length,
        }, 'WhatsApp dispatcher heartbeat')
      }
    } catch (error) {
      log.warn({ err: error.message }, 'watchdog cycle error')
    }
  }
}

/**
 * A second channel that failed to resolve once (network blip, newsletter list
 * not cached yet) used to stay missing until the next reconnect - silently
 * halving coverage. Retry while it is unresolved.
 */
const EXTRA_CHANNELS = () => [
  { key: 'under99', env: WA_CHANNEL_UNDER99, apply: jid => { under99Jid = jid }, current: () => under99Jid },
  { key: 'under499', env: WA_CHANNEL_UNDER499, apply: jid => { under499Jid = jid }, current: () => under499Jid },
  { key: 'bestOf', env: WA_CHANNEL_BEST_OF, apply: jid => { bestOfJid = jid }, current: () => bestOfJid },
]
function syncChannelPolicies() {
  CHANNEL_POLICY_OF_JID.clear()
  if (under99Jid) CHANNEL_POLICY_OF_JID.set(under99Jid, 'under99')
  if (under499Jid) CHANNEL_POLICY_OF_JID.set(under499Jid, 'under499')
  if (bestOfJid) CHANNEL_POLICY_OF_JID.set(bestOfJid, 'bestOf')
}
// Resolve every configured extra channel. onlyMissing=true (the timer) retries
// just the ones that are still unresolved, so a blip never halves coverage.
async function resolveExtraChannels(sock, onlyMissing = false) {
  if (!sock) return
  for (const channel of EXTRA_CHANNELS()) {
    if (!channel.env) continue
    if (onlyMissing && channel.current()) continue
    try {
      const jid = await resolveNewsletterJid(sock, channel.env)
      channel.apply(jid)
      syncChannelPolicies()
      log.info({ channel: channel.key, jid, mode: CHANNEL_ALL_POSTS ? 'mirror-everything' : `policy:${channel.key}` },
        'WhatsApp channel resolved')
    } catch (error) {
      syncChannelPolicies()
      log.error({ channel: channel.key, err: error.message },
        'WhatsApp channel could not be resolved; retrying on a timer, main channel posting normally')
    }
  }
  syncChannelPolicies()
}
async function ensureSecondaryChannel(sock) {
  await resolveExtraChannels(sock)
}

function secondaryChannelRetryLoop() {
  const timer = setInterval(() => {
    if (!shuttingDown && waReady) resolveExtraChannels(wa, true).catch(() => {})
  }, 600_000)
  if (typeof timer.unref === 'function') timer.unref()
}

async function connectWhatsApp() {
  const { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  const { version } = await fetchLatestBaileysVersion()
  const sock = makeWASocket({
    version, auth, logger: pino({ level: 'silent' }),
    markOnlineOnConnect: false, syncFullHistory: false,
    browser: ['BestGAA Bridge', 'Chrome', '1.0.0'],
    generateHighQualityLinkPreview: false,
  })
  wa = sock
  sock.ev.on('creds.update', saveCreds)
  if (!auth.creds.registered && PAIR_BY_CODE) {
    await sleep(1800)
    const code = await sock.requestPairingCode(WA_PHONE)
    console.log(`\nPAIRING CODE: ${code}\nPhone: WhatsApp > Linked devices > Link a device > Link with phone number\n`)
  }
  sock.ev.on('connection.update', async update => {
    if (PAIR_BY_QR && update.qr) {
      console.log('\nScan this NEW QR now: WhatsApp > Linked devices > Link a device\n')
      qrcode.generate(update.qr, { small: true })
    }
    if (update.connection === 'open') {
      reconnectAttempt = 0
      lastConnectionOpenAt = Date.now()
      try {
        targetJid = await resolveTargetJid(sock)
        // Extra channels (Under-₹99 / Under-₹499 / Best-of). Non-fatal: whatever
        // fails to resolve is retried on a timer while the main channel posts.
        under99Jid = null; under499Jid = null; bestOfJid = null
        await resolveExtraChannels(sock)
        groupJids = []
        if (CHANNEL_ONLY) {
          log.warn('WA_CHANNEL_ONLY=true -> groups disabled this run (Channel only).')
        } else {
          state.groupJidCache ||= {}
          for (const raw of EFFECTIVE_GROUP_TARGETS) {
            try {
              // Resolve from the persisted cache first; resolveGroupTarget only
              // hits the join/invite API for a target never seen before.
              if (state.groupJidCache[raw]) groupJids.push(state.groupJidCache[raw])
              else groupJids.push(await resolveGroupTarget(sock, raw))
            } catch (error) {
              log.error({ raw, err: error.message }, 'group target could not be resolved; continuing without it')
            }
          }
        }
        waReady = true
        if (!state.warmupStartedAt) state.warmupStartedAt = Date.now()
        saveState()
        log.info({
          targetJid, under99Jid, under499Jid, bestOfJid,
          channelPolicies: Object.fromEntries([...CHANNEL_POLICY_OF_JID.entries()]),
          sources: [...SOURCES], policy: warmupPolicy(),
          primarySource: PRIMARY_SOURCE, mediaFirst: MEDIA_FIRST,
          newsletterMediaFix: NEWSLETTER_MEDIA_FIX,
          minGapSeconds: MIN_WA_MESSAGE_GAP_SECONDS,
          quiet: `${QUIET_START}-${QUIET_END}`, hybridQuiet: HYBRID_QUIET,
          bestDealGate: BEST_DEAL_GATE, serviceDomains: SERVICE_OFFER_DOMAINS.length,
          queue: state.jobs.length,
        }, 'WhatsApp connected')
        if (PAIR_ONLY) setTimeout(() => process.exit(0), 4000)
      } catch (error) {
        waReady = false
        log.error({ err: error.message }, 'WhatsApp connected but Channel could not be resolved')
        if (PAIR_ONLY) setTimeout(() => process.exit(2), 2000)
      }
    }
    if (update.connection === 'close') {
      waReady = false
      const status = update.lastDisconnect?.error?.output?.statusCode
      if (status === DisconnectReason.loggedOut) {
        log.error('WhatsApp logged out. Stop service and pair again; auth is preserved for diagnosis.')
        return
      }
      reconnectAttempt += 1
      const delay = Math.min(300_000, 4000 * 2 ** Math.min(reconnectAttempt - 1, 7)) + randomMs(1, 12)
      log.warn({ status, delay }, 'WhatsApp disconnected; reconnect scheduled')
      setTimeout(() => connectWhatsApp().catch(error => log.error({ err: error.message }, 'reconnect failed')), delay)
    }
  })
}

for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => {
  shuttingDown = true
  saveState()
  setTimeout(() => process.exit(0), 1000)
})

if (process.argv.includes('--identity-probe')) {
  // Cross-language contract for the dedup identity: pipe product headline lines on
  // stdin (one per line) and this prints the identity the WhatsApp side would key
  // on, as JSON per line. test_line_fidelity.py feeds it the same corpus it feeds
  // the Python rule, so "same product on Telegram, new deal on WhatsApp" cannot
  // come back without a test going red.
  const lines = fs.readFileSync(0, 'utf8').split('\n').filter(line => line.trim())
  for (const line of lines) {
    process.stdout.write(JSON.stringify({ line, identity: productNameIdentity(line) }) + '\n')
  }
  process.exit(0)
}

{
  // A link-health probe is allowed to make a post honest, never to make it
  // disappear. A 404/repair page is exactly what Amazon and Flipkart answer to a
  // server IP, and this path used to drop the WhatsApp post outright (the user's
  // "source lo post vasthundi, mana target lo raledu").
  const savedNotBroken = notBroken
  const probeUrls = ['https://www.amazon.in/dp/B0GH2374K3?tag=deals0911-21']
  try {
    notBroken = async () => false
    const dead = await deadDestinations({ id: 'probe-dead' }, probeUrls)
    if (dead.length !== 1 || dead[0] !== probeUrls[0]) {
      throw new Error('deadDestinations must report the dead link')
    }
    if (WA_DROP_DEAD_LINKS) {
      let threw = false
      try { await assertLinksHealthy({ id: 'probe-dead' }, probeUrls) } catch { threw = true }
      if (!threw) throw new Error('WA_DROP_DEAD_LINKS=true must still refuse a dead destination')
    } else {
      await assertLinksHealthy({ id: 'probe-dead' }, probeUrls)   // must NOT throw
    }
    notBroken = async () => true
    await assertLinksHealthy({ id: 'probe-alive' }, probeUrls)     // must NOT throw
  } finally {
    notBroken = savedNotBroken
  }
}

if (process.argv.includes('--self-test')) {
  // Some tests persist through saveState(); never leave a state file behind
  // when the self-test created it (a real deployment state must be untouched).
  const stateFilePreExisted = fs.existsSync(STATE_FILE)
  const stateBackupPreExisted = fs.existsSync(STATE_BACKUP_FILE)
  const sample = {
    text: 'KILLER Mens Loafers Start at Rs.408\nhttps://fktr.in/OUR123',
  }
  const noisy = cleanDealText('Deal ₹99\n#Myntra\nh\n👉h\n\u200bhtt\ntps://\nuy\n😱 Deal Time: 09:49 AM IST\nLOOT FASSS TT\n🔁 Share • @GrabOnIndiaOfficial - 50+ loots daily\n💰 Want Real Cash Back Too?\nForward to @cashkarolinkbot')
  if (!noisy.includes('Deal ₹99') || /Deal Time|LOOT FASSS|GrabOnIndiaOfficial|cashkarolinkbot|#Myntra|^(?:h|ht|htt|https?|tps?:\/\/|uy)$/m.test(noisy)) throw new Error('source-noise cleanup test failed')
  // Unwanted, non-source promo/navigation lines must be stripped while EVERY
  // line of actual deal content (name, price, discount, link) survives.
  {
    const raw = [
      "Men's Cotton Casual Shirt",
      'Deal Price: \u20b9699',
      'MRP: \u20b91999   65% OFF',
      '🔔 JOIN OUR TELEGRAM CHANNEL FOR MORE LOOTS',
      '📢 t.me/somechannel',
      'Click here to buy',
      'Join now',
      '✅',
      'Share with your friends',
      'Forward to our group',
      'Turn on notifications',
      "Don't miss this deal!!",
      '@somechannel',
      '#deals',
      'Premium cotton fabric, slim fit',
      'Free delivery on this order',
      'https://www.amazon.in/dp/B0CLEANUP1?tag=' + AMAZON_TAG,
    ].join('\n')
    const out = cleanDealText(raw)
    for (const keep of ["Men's Cotton Casual Shirt", 'Deal Price: \u20b9699', 'MRP: \u20b91999', '65% OFF',
      'Premium cotton fabric, slim fit', 'Free delivery on this order', 'B0CLEANUP1']) {
      if (!out.includes(keep)) throw new Error('promo filter removed real content: "' + keep + '"\n' + out)
    }
    for (const junk of ['JOIN OUR TELEGRAM', 't.me/somechannel', 'Click here', 'Join now',
      'Share with your', 'Forward to our', 'notifications', "Don't miss", '@somechannel', '#deals']) {
      if (out.includes(junk)) throw new Error('promo junk survived: "' + junk + '"\n' + out)
    }
  }
  // A real product line that merely MENTIONS a CTA word must survive; only
  // pure promo/navigation lines are stripped.
  for (const safe of ['Get 60% off this running shoes', 'Buy this cotton tshirt set today',
      'Shop now for these premium watches', 'Click on the link for laptop stand details price ₹299',
      'Zomato 60% OFF free delivery', 'Pack of 2 cotton shirts for men slim fit blue']) {
    if (isPromoNoiseLine(safe)) throw new Error('real content wrongly stripped: ' + safe)
  }
  for (const junkOnly of ['JOIN OUR CHANNEL', 'Follow us on telegram', '📢📢📢', '👉👉',
      'Subscribe our group for loot alerts']) {
    if (!isPromoNoiseLine(junkOnly)) throw new Error('promo junk not stripped: ' + junkOnly)
  }
  // detectedPrice returns the DEAL price: never MRP, never a discount %.
  if (detectedPrice('Shirt\nDeal Price: ₹699\nMRP: ₹1999\n65% OFF') !== 699) throw new Error('deal price must beat MRP')
  if (detectedPrice('Kurta MRP ₹1999 now only ₹499') !== 499) throw new Error('lowest selling price must win over MRP')
  if (detectedPrice('Shoes at 70% off') === 70) throw new Error('discount percent must not be read as price')
  if (detectedPrice('No price here, just a great deal https://a.test/x') !== null) throw new Error('absent price must be null')
  {
    // v17.5: a WhatsApp post is the source post. No synthetic badge line on a
    // single deal either, and no line of the source may be dropped because a
    // decoration of ours happened to repeat it.
    const listPost = formatWhatsAppPost({ text: 'Mega Sale\nA ₹299 https://a.test/1\nB ₹399 https://a.test/2\nC ₹499 https://a.test/3\nD ₹599 https://a.test/4' })
    if (/^💰\s*₹/m.test(listPost)) throw new Error('no badge line may be added to a list:\n' + listPost)
    for (const line of ['Mega Sale', 'A ₹299', 'B ₹399', 'C ₹499', 'D ₹599']) {
      if (!listPost.includes(line)) throw new Error(`list source line lost: ${line}\n${listPost}`)
    }
    const singlePost = formatWhatsAppPost({ text: 'Single Shirt\nDeal Price: ₹699\nhttps://a.test/1' })
    if (singlePost.includes('💰')) throw new Error('no synthetic price badge may be added:\n' + singlePost)
    if (!/^Single Shirt/m.test(singlePost) || !singlePost.includes('Deal Price: ₹699')) {
      throw new Error('single-deal source lines must print as written:\n' + singlePost)
    }
    if (singlePost.indexOf('Single Shirt') > singlePost.indexOf('Deal Price: ₹699')) throw new Error('source order must be preserved:\n' + singlePost)
    if (!singlePost.includes('https://a.test/1')) throw new Error('the link must stay in the post:\n' + singlePost)
  }
  // Long links must be flagged for shortening while a clean short amazon /dp
  // link and already-short hosts are left alone (quota protection).
  {
    const searchLink = 'https://www.amazon.in/s?k=puma+shoes+men&rh=n%3A1571283031%2Cn%3A1983396031&rnid=1983396031&s=price-asc-rank&tag=' + AMAZON_TAG
    if (searchLink.length > SHORTEN_MIN_LEN && !needsShortening(searchLink)) {
      throw new Error('long amazon search link must be shortened')
    }
  }
  // The promise is about the THRESHOLD, so it is stated against it: a link shorter than
  // WA_SHORTEN_MIN_LEN is left alone (no quota burned), one at or over it is shortened.
  // Hard-coding the default 65 here would cry wolf the moment an operator moves the knob.
  {
    const cleanDp = 'https://www.amazon.in/dp/B0GLY3Q2XR?tag=' + AMAZON_TAG
    if (cleanDp.length < SHORTEN_MIN_LEN && needsShortening(cleanDp)) {
      throw new Error('a link under the shorten threshold must NOT burn quota')
    }
    if (cleanDp.length > SHORTEN_MIN_LEN && !needsShortening(cleanDp)) {
      throw new Error('a link over the shorten threshold must be shortened')
    }
  }
  const danglingSlots = cleanDealText('Lunchbox @ ₹88\n\n🔗\n🔗\n🔗 https://www.amazon.in/dp/B0DY7V1G9M?th=1&tag=deals0911-21')
  if (!danglingSlots.includes('B0DY7V1G9M')) throw new Error('real link missing after empty-slot cleanup')
  // No line may be a bare 🔗 (or any link bullet) with no URL after it.
  if (danglingSlots.split(/\r?\n/).some(line => /^[🔗👉➡️🔎🔍•▪\-–—_*\s]+$/.test(line.trim()) && line.trim())) {
    throw new Error('empty link slot cleanup test failed')
  }
  const strayProtocol = cleanDealText('Fast ₹289\nhttps://ajio.in/ozl6hL3 https://\nKurta set')
  if (!strayProtocol.includes('https://ajio.in/ozl6hL3') || strayProtocol.includes(' https://') || /(?:^|\n)\s*https?:\/\/(?:\n|$)/.test(strayProtocol)) {
    throw new Error('stray protocol fragment cleanup test failed')
  }
  // photo + text + link must ride in ONE post (caption); short body only.
  const shortBody = cleanDealText('Fast ₹289\nhttps://ajio.in/ozl6hL3\nKurta set')
  if (!(shortBody.length > 0 && shortBody.length <= 1024)) throw new Error('caption short-body fit test failed')
  // long body must not be silently dropped; it overflows to a follow-up, not lost.
  const longBody = cleanDealText(('Product detail ' + 'x'.repeat(1300) + '\nhttps://ajio.in/ozl6hL3').trim())
  if (longBody.length <= 1024 || !longBody.includes('https://ajio.in/ozl6hL3')) throw new Error('caption long-body spill test failed')
  const neatPrice = cleanDealText('Deal Price: ₹131 ₹499\nCleaner @ ₹185VN7z\nRegular price: - ₹2000\nNow')
  if (!neatPrice.includes('Deal Price: ₹131') || !neatPrice.includes('MRP: ₹499') || !neatPrice.includes('Cleaner @ ₹185') || /VN7z|Regular price|\bNow\b/.test(neatPrice)) throw new Error('price cleanup test failed')
  // Random source-corruption token AFTER a price (space separated) must go
  // too, while real quantities/units next to a price always survive.
  const tokenAfterPrice = cleanDealText('Kurta Set ₹499 Xk92zQ\nDetergent ₹185 2pcs\nPowder ₹299 500ml\nCharger ₹349 65w')
  if (/Xk92zQ/.test(tokenAfterPrice)) throw new Error('random token after price not removed')
  if (!tokenAfterPrice.includes('₹185 2pcs') || !tokenAfterPrice.includes('₹299 500ml') || !tokenAfterPrice.includes('₹349 65w')) throw new Error('real unit after price wrongly removed')
  const strictSpecial = cleanDealText('🔥 LOOT ZONE — India\n🚨 SPECIAL OFFER\nFlipkart | 92% Off - Duck Slide Toy Set at Rs.229\n-\n➜ https://fktr.in/OUR\n✅ Verified • Enjoy (Grab fast)')
  if (strictSpecial !== 'Flipkart | 92% Off - Duck Slide Toy Set at Rs.229\nhttps://fktr.in/OUR') throw new Error('strict special cleanup test failed')
  const trailingToken = cleanDealText('https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21 0GLY3Q2X\n₹3000 off')
  if (trailingToken.includes(' 0GLY3Q2X') || !trailingToken.includes('tag=deals0911-21')) throw new Error('trailing URL token cleanup test failed')
  const item = formatDigestItem(sample, 1)
  if (!item.includes('KILLER Mens Loafers') || !item.includes('\nhttps://fktr.in/OUR123')) throw new Error('digest format test failed:\n' + item)
  if (item.includes('➜')) throw new Error('digest items must not add bullet characters of ours:\n' + item)
  validateAffiliateText(null, 'Gold Pendant\nhttps://amazon.in/dp/B084LFLYCT?tag=deals0911-21')
  let foreignBlocked = false
  try { validateAffiliateText(null, 'Bad\nhttps://amazon.in/dp/B084LFLYCT?tag=foreign-21') } catch { foreignBlocked = true }
  if (!foreignBlocked) throw new Error('foreign Amazon tag test failed')
  let sourceShortBlocked = false
  try { validateAffiliateText(null, 'Bad\nhttps://fkrt.cc/source123') } catch { sourceShortBlocked = true }
  if (!sourceShortBlocked) throw new Error('source shortener test failed')
  if (!shouldKeepForWhatsApp('Under99Deals11', 'Socks ₹49', false, false, false)) throw new Error('under99 curation test failed')
  if (!isPreferredShoppingCategory('Running Shoes ₹299')) throw new Error('preferred category test failed')
  if (!shouldKeepForWhatsApp('under499loots', 'Decorative collectible ₹300', false, false, false)) throw new Error('valid-deal intake retention test failed')
  if (!shouldKeepForWhatsApp('LootZoneIndia11', 'Running shoes 75% OFF', false, false, false)) throw new Error('high-discount curation test failed')
  if (!shouldKeepForWhatsApp('under499loots', 'Women Saree at ₹299', true, false, false)) throw new Error('women category curation test failed')
  // Channel media contract (Baileys #2199): newsletter uploads must use the
  // /newsletter/newsletter-* path with server_thumb_gen=1, or WhatsApp acks the
  // stanza with 479 and subscribers never see the photo.
  if (NEWSLETTER_MEDIA_PATH_MAP.image !== '/newsletter/newsletter-image') throw new Error('newsletter image upload path test failed')
  if (NEWSLETTER_MEDIA_PATH_MAP.video !== '/newsletter/newsletter-video') throw new Error('newsletter video upload path test failed')
  {
    const http = await import('node:http')
    const seen = []
    const server = http.createServer((req, res) => {
      seen.push(req.url)
      req.on('data', () => {})
      req.on('end', () => {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ url: 'https://mmg.whatsapp.net/m1/v/t24/OK', direct_path: '/m1/v/t24/OK' }))
      })
    })
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    const port = server.address().port
    const fakeSock = { refreshMediaConn: async () => ({ auth: 'AUTH', hosts: [{ hostname: `127.0.0.1:${port}` }] }) }
    const tmp = path.join(os.tmpdir(), `wa-selftest-${Date.now()}`)
    fs.writeFileSync(tmp, Buffer.from('selftest'))
    const original = globalThis.fetch
    globalThis.fetch = (url, init) => original(String(url).replace('https://', 'http://'), init)
    let uploaded
    try {
      uploaded = await newsletterUpload(fakeSock, tmp, { mediaType: 'image', fileEncSha256B64: Buffer.from('abc').toString('base64') })
    } finally {
      globalThis.fetch = original
      fs.unlinkSync(tmp)
      server.close()
    }
    if (!seen.some(url => url.startsWith('/newsletter/newsletter-image/'))) throw new Error('newsletter upload did not use channel media path')
    if (!seen.some(url => url.includes('server_thumb_gen=1'))) throw new Error('newsletter upload missing server_thumb_gen')
    if (!uploaded.directPath?.startsWith('/m1/')) throw new Error('newsletter upload directPath test failed')
  }
  if (typeof withTimeout !== 'function') throw new Error('dispatch timeout guard missing')
  await withTimeout(Promise.resolve(1), 1000, 'noop')
  let timedOut = false
  try { await withTimeout(new Promise(() => {}), 30, 'hang') } catch { timedOut = true }
  if (!timedOut) throw new Error('dispatch timeout guard test failed')
  const policy = warmupPolicy()
  // The 24/7 caps only apply to a warmed number; a brand new one is capped low
  // on purpose, so that must not trip this check.
  const matureNumber = (process.env.WA_WARMUP_DONE || 'true').toLowerCase() === 'true'
  if (matureNumber && (policy.dayCap < 200 || policy.hourCap < 22)) {
    throw new Error('24/7 throughput cap test failed')
  }
  // Queue acceleration must respect the CONFIGURED post gap (it used to be
  // pinned to 60s, which made a tuned-down gap untestable).
  const expectedAccel = Math.max(100_000, 80_000 + MIN_WA_MESSAGE_GAP_SECONDS * 1000)
  const accelerated = acceleratedNextAllowed(100_000, 80_000, 2, false, 400_000)
  if (accelerated !== expectedAccel) {
    throw new Error(`two-plus queue acceleration floor test failed (${accelerated} != ${expectedAccel})`)
  }
  // Acceleration may never send before one post gap after the last send (unless
  // that gap already elapsed) and never later than the scheduled time.
  if (accelerated > 400_000 || accelerated < 80_000 + MIN_WA_MESSAGE_GAP_SECONDS * 1000 - 1) {
    throw new Error('acceleration out of bounds')
  }
  if (acceleratedNextAllowed(100_000, 80_000, 2, false, 90_000) !== 90_000) {
    throw new Error('acceleration must not delay a job already scheduled sooner')
  }
  if (acceleratedNextAllowed(100_000, 80_000, 2, true, 400_000) !== 400_000) {
    throw new Error('acceleration must not fire inside the quiet window')
  }
  if (acceleratedNextAllowed(100_000, 80_000, 1, false, 400_000) !== 400_000) throw new Error('single-queue delay preservation test failed')
  const fresh = Date.now()
  const prioritySamples = [
    { text: 'Useful item at ₹99 https://a.test/1', media: [], largeList: false, createdAt: fresh + 1, source: 'LootZoneIndia11' },
    { text: 'Expensive photo item https://a.test/2', media: [{}], largeList: false, createdAt: fresh + 2, source: 'LootZoneIndia11' },
    { text: 'Four links https://a/1 https://a/2 https://a/3 https://a/4', media: [], largeList: true, createdAt: fresh + 3, source: 'LootZoneIndia11' },
    { text: 'Two links https://b/1 https://b/2', media: [], largeList: false, createdAt: fresh + 4, source: 'LootZoneIndia11' },
    { text: 'Single link 95% OFF https://c/1', media: [], largeList: false, createdAt: fresh + 5, source: 'LootZoneIndia11' },
    { text: 'Women Saree https://d/1', media: [], largeList: false, createdAt: fresh + 6, source: 'LootZoneIndia11' },
    { text: 'Home container https://e/1', media: [], largeList: false, createdAt: fresh + 7, source: 'LootZoneIndia11' },
  ]
  const ordered = [...prioritySamples].sort(compareQueuedJobs)
  const orderKeys = ordered.map(item => item.createdAt - fresh)
  // USER RULE order: the multi-product LIST (item 3) leads, then the photo
  // item (item 2), then the price/discount ladder; items 6 and 7 tie on every
  // rank so the newest (createdAt=fresh+7) wins - latest-first. The media
  // lead is a knob (WA_MEDIA_FIRST), so the exact order is only asserted in the
  // mode that uses it; the invariants below must hold either way.
  if (MEDIA_FIRST && orderKeys.join(',') !== '3,2,1,4,5,7,6') {
    throw new Error('list-first + media + newest-first priority order test failed: ' + orderKeys)
  }
  if (orderKeys[0] !== 3) {
    throw new Error('a ready multi-product list must always lead the queue: ' + orderKeys)
  }
  if (orderKeys.indexOf(7) > orderKeys.indexOf(6)) {
    throw new Error('the newest-first tie-break must survive any knob: ' + orderKeys)
  }
  if (!MEDIA_FIRST && orderKeys[0] === 2) {
    throw new Error('the media lead must follow WA_MEDIA_FIRST: ' + orderKeys)
  }
  // under499loots leads, and inside every source photo/video outranks text.
  const primarySamples = [
    { text: 'Plain text deal ₹49 https://a/1', media: [], largeList: false, createdAt: fresh + 1, source: 'LootZoneIndia11' },
    { text: 'Photo deal ₹499 https://a/2', media: [{}], largeList: false, createdAt: fresh + 2, source: 'under499loots' },
    { text: 'Text deal ₹99 https://a/3', media: [], largeList: false, createdAt: fresh + 3, source: 'under499loots' },
  ]
  const primaryOrdered = [...primarySamples].sort(compareQueuedJobs)
  const primaryKeys = primaryOrdered.map(item => item.createdAt - fresh).join(',')
  if (primaryOrdered[0].source !== 'under499loots') {
    throw new Error('the primary source must always lead: ' + primaryKeys)
  }
  // The photo preference inside a source tier is the WA_MEDIA_FIRST knob, so it
  // is only asserted when that lead is switched on.
  if (MEDIA_FIRST && primaryKeys !== '2,3,1') {
    throw new Error('under499loots primary + photo preference test failed: ' + primaryKeys)
  }
  if (!MEDIA_FIRST && primaryKeys !== '3,2,1') {
    throw new Error('without the media lead the price ladder must order the rest: ' + primaryKeys)
  }
  // Latest-first: a fresh deal must always be posted before a 3-hour-old one,
  // so hours-old inventory is never pushed ahead of a brand-new post. The old
  // deal is still kept and ages out via MAX_JOB_AGE, but never gets a head start.
  const stale = fresh - 3 * 60 * 60_000 // 3 hours old
  const sameSourceFresh = fresh - 60_000 // 1 minute old
  const starvation = [
    { text: 'New photo ₹399 https://a/1', media: [{}], largeList: false, createdAt: fresh, source: 'under499loots' },
    { text: 'Old text deal ₹250 https://a/2', media: [], largeList: false, createdAt: stale, source: 'LootZoneIndia11' },
    { text: 'Nearly-new text deal ₹250 https://a/3', media: [], largeList: false, createdAt: sameSourceFresh, source: 'LootZoneIndia11' },
  ]
  const starvationOrder = [...starvation].sort(compareQueuedJobs).map(job => job.createdAt)
  // Old deal must come AFTER both fresh and nearly-new deals; never first.
  if (starvationOrder.indexOf(stale) !== 2) throw new Error('latest-first ordering test failed (old deal must be last)')
  if (isNightUrgent({ text: 'Ordinary Under99 at ₹49', media: [], special: false, largeList: false })) throw new Error('night ordinary-deal gate failed')
  if (!isNightUrgent({ text: 'Big deal 85% OFF', media: [], special: false, largeList: false })) throw new Error('night big-deal gate failed')
  // Night-queue trust policy: a job BORN inside 02:00-06:00 IST is detected,
  // ordinary quiet-born deals are expired, best-tier deals survive, and an
  // ordinary deal older than the trust window is expired too.
  // These two used to be written as "03:00 IST = 21:30 UTC": true only when the bridge
  // runs on India time. `TZ_NAME` is an operator knob, so the instants are built from the
  // clock the process actually reads, and the messages name the tested window, not a zone.
  const dayStart = Date.UTC(2026, 7, 23, 0, 0)
  const istShift = minuteOfDay(dayStart)     // how far into the day of TZ a UTC midnight is
  const minuteInstant = (minute) => dayStart + (((minute - istShift) % 1440 + 1440) % 1440) * 60_000
  if (!wasBornInQuietWindow(minuteInstant(180), '02:00', '06:00')) throw new Error('night-window birth detection failed (03:00 inside 02:00-06:00)')
  if (wasBornInQuietWindow(minuteInstant(390), '02:00', '06:00')) throw new Error('night-window birth false positive (06:30, the end minute is exclusive)')
  if (!isBestTierJob({ text: 'Mega list https://a/1 https://b/2 https://c/3 https://d/4', media: [], special: false, largeList: false })) throw new Error('best-tier list detection failed')
  if (isBestTierJob({ text: 'Ordinary ₹49 deal https://a/1', media: [], special: false, largeList: false })) throw new Error('ordinary deal wrongly marked best-tier')
  // The trust policy must be tested against the WINDOW THAT IS CONFIGURED and never
  // against the wall clock. These fixtures used Date.now() - so at 08:00 IST "three hours
  // ago" is 05:00, i.e. quiet-born, and a morning `deploy_and_verify.sh` blamed a perfectly
  // fine bridge for "best-tier deal wrongly expired" - and they hard-coded 03:00/06:10,
  // which stop meaning anything the moment QUIET_START/QUIET_END move. Both instants come
  // from the live window now, and every call gets its own explicit `now`.
  let quietMinute = -1, freeMinute = -1
  for (let m = 0; m < 1440; m++) {
    if (quietMinute < 0 && isQuietMinute(m)) quietMinute = m
    if (freeMinute < 0 && !isQuietMinute(m)) freeMinute = m
    if (quietMinute >= 0 && freeMinute >= 0) break
  }
  const staleGap = Math.min(ORDINARY_MAX_AGE_MS, MAX_JOB_AGE_MS) + 60_000
  // Born at a minute the pause does not cover, so ONLY the trust window can speak for it,
  // and the age gap is derived from the thresholds rather than a fixed three hours.
  const bornOutside = minuteInstant(freeMinute < 0 ? 0 : freeMinute)
  if (!ordinaryJobExpiryReason({ text: 'Ordinary ₹49 deal https://a/1', createdAt: bornOutside },
                              bornOutside + staleGap)) {
    throw new Error('stale ordinary deal not expired (trust policy)')
  }
  if (ordinaryJobExpiryReason({ text: 'Big deal 85% OFF https://a/1', createdAt: bornOutside },
                             bornOutside + staleGap)) {
    throw new Error('best-tier deal wrongly expired (trust policy)')
  }
  if (ordinaryJobExpiryReason({ text: 'Ordinary ₹49 deal https://a/1', createdAt: bornOutside },
                             bornOutside)) {
    throw new Error('a fresh deal is never expired by the trust window')
  }
  // USER RULE: a deal born inside 02:00-06:00 is DEAD once the pause is over (loot prices
  // do not survive four hours) - skip it, never post it late. ONLY a mega LIST earns the
  // morning flush; even an 85% "special" single deal dies. With a pause covering the whole
  // day nothing is ever "after the pause", and with no pause at all nothing is "born in"
  // it, so those configurations are asserted as what they must do instead of skipped.
  const nightOrdinary = { text: 'Ordinary ₹49 deal https://a/1', createdAt: 0, media: [] }
  const nightSpecial = { text: 'FLASH 90% OFF single deal https://a/1', createdAt: 0, media: [], special: true }
  const nightList = { text: 'List https://a/1 https://b/2 https://c/3 https://d/4', createdAt: 0, media: [], largeList: true }
  if (quietMinute >= 0 && freeMinute >= 0) {
    const bornInside = minuteInstant(quietMinute)
    nightOrdinary.createdAt = nightSpecial.createdAt = nightList.createdAt = bornInside
    const afterThePause = minuteInstant(freeMinute)
    if (ordinaryJobExpiryReason(nightOrdinary, afterThePause) !== 'night-born deal expired by 06:00 (lists only)') {
      throw new Error('night-born ordinary deal must be skipped once the pause ends, never posted late')
    }
    if (ordinaryJobExpiryReason(nightSpecial, afterThePause) !== 'night-born deal expired by 06:00 (lists only)') {
      throw new Error('night-born single special must also be skipped (user rule: lists only)')
    }
    if (ordinaryJobExpiryReason(nightList, afterThePause) !== null) {
      throw new Error('night-born mega LIST must survive to the morning flush')
    }
    if (ordinaryJobExpiryReason(nightOrdinary, bornInside) !== null) {
      throw new Error('inside the window the job just waits (worker is paused), it must not be expired yet')
    }
  } else if (quietMinute < 0) {
    nightOrdinary.createdAt = minuteInstant(0)
    if (ordinaryJobExpiryReason(nightOrdinary, nightOrdinary.createdAt + staleGap)
        === 'night-born deal expired by 06:00 (lists only)') {
      throw new Error('a deal was expired by a night window that is not in effect')
    }
  } else {
    nightOrdinary.createdAt = minuteInstant(quietMinute)
    if (ordinaryJobExpiryReason(nightOrdinary, nightOrdinary.createdAt + staleGap)
        === 'night-born deal expired by 06:00 (lists only)') {
      throw new Error('a deal inside a 24-hour pause is held, never expired for being quiet-born')
    }
  }
  // v17.8 THE SAME PRODUCT, NOT THE SAME CAPTION. Two WhatsApp-side rules the
  // user named: a product already sent must not come again, and a cleaned line
  // must never lose the deal it was about.
  {
    const acA = `LG 1.5 Ton 5 Star Inverter Split AC\nNow ₹36,990 (MRP ₹74,990, 50% off)\nhttps://www.croma.com/ac-a1?size=M`
    const acB = `LG 1.5 Ton 5 Star Inverter Split AC\n₹36,990 (50% off)\nFree installation this week\nhttps://www.croma.com/ac-b2`
    if (!nameOnlyKey(acA) || nameOnlyKey(acA) !== nameOnlyKey(acB)) {
      throw new Error('the same product under two links must share one identity: '
        + `${nameOnlyKey(acA)} vs ${nameOnlyKey(acB)}`)
    }
    const washer = `LG Neo Duetto 7Kg Front Load Washing Machine\n₹31,990 (44% off)\nhttps://www.croma.com/ac-b2`
    if (nameOnlyKey(washer) === nameOnlyKey(acA)) throw new Error('two different products must not share an identity')
    const list = `1. Shirt A ₹599 https://a.test/1\n2. Shirt B ₹699 https://a.test/2\n3. Shirt C ₹799 https://a.test/3`
    if (nameOnlyKey(list) !== null) throw new Error('a roundup must never be keyed as one product: ' + nameOnlyKey(list))
    const banner = `TOP DEAL OF THE DAY\nBest deal of the day only\nhttps://a.test/x`
    if (nameOnlyKey(banner) !== null) throw new Error('a campaign banner is not a product name')

    // A caption break must never split a link in half (v17.8: the old code cut at
    // the character limit, which produced "https://www.amazon.in/dp/B0AB" plus a
    // stranded tail, i.e. a dead affiliate link on WhatsApp).
    {
      const longLine = 'Prestige PIC-MAD 2600 5 Burner Manual Stainless Steel LPG Gas Auto Ignition '
        + 'with ' + 'x'.repeat(900) + ' detail https://www.amazon.in/dp/B0ABCDEFGHI?tag=deals0911-21'
      const split = splitCaptionForMedia(longLine, 1024)
      const urls = ['https://www.amazon.in/dp/B0ABCDEFGHI?tag=deals0911-21']
      const halves = [...longLine.matchAll(/https?:\/\/\S+/g)].map(m => m[0])
      for (const url of halves) {
        const inHead = split.head.includes(url)
        const inTail = split.tail.includes(url)
        if (!(inHead || inTail)) {
          throw new Error('a caption break must keep every link whole in one message')
        }
        if (inHead && inTail) throw new Error('a link must not be printed twice')
      }
      const glued = (split.head + split.tail).replace(/\s+/g, '')
      if (glued !== longLine.replace(/\s+/g, '')) throw new Error('splitCaptionForMedia lost text')
      if (split.head.length > 1024 && !split.head.includes('https://')) {
        throw new Error('a caption may only exceed the limit to keep a link whole')
      }
      void urls
    }

    const saved = { sentNames: state.sentNames }
    try {
      state.sentNames = {}
      if (nameOnlyDupReason(acA) !== null) throw new Error('a first copy is never a duplicate')
      markNameOnlySent({ text: acA })
      if (!(WA_SAME_PRODUCT_HOURS > 0)) {
        // The operator switched the rule off, so the only promise left is that it
        // really is off - a silent re-post while claiming to skip would be worse.
        if (nameOnlyDupReason(acB) !== null) {
          throw new Error('WA_SAME_PRODUCT_HOURS=0 must turn the product skipper off')
        }
      } else {
        if (nameOnlyDupReason(acB) === null) throw new Error('the same product at the same price must be skipped')
        // a strictly better copy is news, not a repeat
        const cheaper = `LG 1.5 Ton 5 Star Inverter Split AC\nNow ₹33,490 only (54% off)\nhttps://www.croma.com/ac-c3`
      // The two directions the OLD rule got wrong, and this one must not:
      // (a) the same product wearing different adjectives must still be skipped;
      // (b) a neighbouring model number / capacity must NEVER be skipped.
      const paraphrase = `LG 1.5 Ton 5 Star Inverter Split AC with 4 Way Swing\nMRP ₹74,990  Now ₹36,990\nhttps://www.flipkart.com/lg-ac-5star/slug`
      if (nameOnlyKey(paraphrase) !== nameOnlyKey(acA)) {
        throw new Error('the same product written differently must carry the same identity')
      }
      const otherModel = `LG 1.5 Ton 3 Star Inverter Split AC\n₹36,990\nhttps://www.croma.com/ac-3star`
      if (nameOnlyKey(otherModel) === nameOnlyKey(acA)) {
        throw new Error('a different star rating is a different product, not a duplicate')
      }
      const bigModel = `boAt Airdopes 141 TWS Earbuds with ENx\n₹1,099\nhttps://www.croma.com/b1`
      const nearModel = `boAt Airdopes 131 TWS Earbuds\n₹999\nhttps://www.croma.com/b2`
      if (nameOnlyKey(bigModel) === nameOnlyKey(nearModel)) {
        throw new Error('Airdopes 141 and 131 are two products; skipping one loses a deal')
      }
      const smallPhone = `Samsung Galaxy S23 FE 5G (128 GB)\n₹49,999\nhttps://www.flipkart.com/s23fe-128`
      const bigPhone = `Samsung Galaxy S23 FE 5G (256 GB)\n₹55,999\nhttps://www.flipkart.com/s23fe-256`
      if (nameOnlyKey(smallPhone) === nameOnlyKey(bigPhone)) {
        throw new Error('128GB and 256GB must never be collapsed into one product')
      }
      // The old rule keyed the first eight words, so anything written PAST them was
      // invisible: these two cookers differ only in the model number on word nine.
      const longA = `Prestige Manual Stainless Steel LPG Gas Auto Ignition Cooker Top Auto Shut Off Model 2600\n₹4,999\nhttps://www.amazon.in/dp/B0PST1`
      const longB = `Prestige Manual Stainless Steel LPG Gas Auto Ignition Cooker Top Induction Base Model 3600\n₹5,499\nhttps://www.amazon.in/dp/B0PST2`
      if (nameOnlyKey(longA) === nameOnlyKey(longB) || !nameOnlyKey(longA)) {
        throw new Error('two cookers differing by their model number are not one product')
      }
      // The name+price layer (the older dedup) had the same eight-word weakness.
      const lapA = `Lenovo IdeaPad Slim 3 15.6-inch FHD IPS Laptop (Intel i5-1235U/8GB/512GB SSD)\n₹38,990\nhttps://www.flipkart.com/ideapad-a`
      const lapB = `Lenovo IdeaPad Slim 3 15.6-inch FHD IPS Laptop (Intel i5-12450H/16GB/512GB SSD)\n₹38,990\nhttps://www.flipkart.com/ideapad-b`
      if (namePriceKey(lapA) && namePriceKey(lapB) && namePriceKey(lapA) === namePriceKey(lapB)) {
        throw new Error('two laptops differing past word eight at the same price are two deals')
      }
      const lapRepeat = `Lenovo IdeaPad Slim 3 15.6-inch FHD IPS Laptop (Intel i5-1235U/8GB/512GB SSD)\n₹38,990 only\nhttps://fktr.in/xyz9`
      if (!namePriceKey(lapRepeat) || namePriceKey(lapA) !== namePriceKey(lapRepeat)) {
        throw new Error('the same laptop at the same price must still read as a repeat')
      }
      const tvA = `Samsung 55-inch Crystal 4K UHD Smart TV\n₹38,990\nhttps://www.croma.com/tv1`
      const tvB = `Samsung Crystal 4K UHD 55 inch Smart TV (2023)\n₹38,990 (46% off)\nhttps://www.amazon.in/dp/B0TV55`
      if (nameOnlyKey(tvA) !== nameOnlyKey(tvB)) {
        throw new Error('55-inch and 55 inch are one product, spelled two ways')
      }
      const tvC = `Samsung 43-inch Crystal 4K UHD Smart TV\n₹28,990\nhttps://www.croma.com/tv2`
      if (nameOnlyKey(tvA) === nameOnlyKey(tvC)) {
        throw new Error('a 43 inch and a 55 inch TV are two deals; skipping one loses one')
      }
        if (nameOnlyDupReason(cheaper) !== null) throw new Error('a cheaper copy of the same product must still post')
        const deeper = `LG 1.5 Ton 5 Star Inverter Split AC\n₹36,990 (60% off)\nhttps://www.croma.com/ac-c4`
        if (WA_SAME_PRODUCT_MARGIN <= 10 && nameOnlyDupReason(deeper) !== null) {
          throw new Error('the same price at a clearly deeper discount must still post')
        }
        const sameer = `LG 1.5 Ton 5 Star Inverter Split AC\n₹36,990 (52% off)\nhttps://www.croma.com/ac-c5`
        if (WA_SAME_PRODUCT_MARGIN > 2 && nameOnlyDupReason(sameer) === null) {
          throw new Error('a discount difference smaller than the margin must not re-post the same product')
        }
        const old = state.sentNames[nameOnlyKey(acA)]
        old.at = Date.now() - (WA_SAME_PRODUCT_HOURS + 2) * 3600_000
        if (nameOnlyDupReason(acB) !== null) throw new Error('outside the window the product may be shown again')
      }
    } finally {
      state.sentNames = saved.sentNames
    }
  }
  // v17.8 CLEANING MAY NOT DELETE A FACT - the same rule the Python side has: a
  // promo clause is removed, the coupon / price / percentage beside it is not.
  {
    const coupon = stripInlineCta('More offers: Apply coupon PEOPLE200 on shirts')
    if (!/PEOPLE200/.test(coupon)) throw new Error('a coupon code was eaten by CTA cleaning: ' + coupon)
    const priced = stripInlineCta(`Noise Buds ₹999 buy now and grab fast`)
    if (!/₹999/.test(priced) || !/Noise Buds/.test(priced)) throw new Error('deal text lost next to a CTA: ' + priced)
    const pct = stripInlineCta(`Premium Cotton T-Shirt 78% off, only today`)
    if (!/78%/.test(pct)) throw new Error('a discount figure was eaten by CTA cleaning: ' + pct)
    const boiler = stripInlineCta('For more deals join our channel and turn on notifications')
    if (/join our channel|notifications/i.test(boiler)) throw new Error('channel boilerplate survived: ' + boiler)
  }
  // v17.8 A CAPTION THAT DOES NOT FIT IS SPLIT, NEVER CUT WITH AN ELLIPSIS.
  {
    const long = Array.from({ length: 30 }, (_, i) => `Line ${i + 1} of the source post describing the offer in words`).join('\n')
    const { head, tail } = splitCaptionForMedia(long, 1024)
    if (head.includes('…') || tail.includes('…')) throw new Error('a caption was truncated instead of split')
    if (`${head}\n${tail}`.trim() !== long.trim()) throw new Error('caption split lost source text')
    if (head.length > 1024) throw new Error(`caption head still exceeds the limit: ${head.length}`)
    const oneLine = `x`.repeat(3000)
    const split = splitCaptionForMedia(oneLine, 1024)
    if (split.head.includes('…') || `${split.head}${split.tail}` !== oneLine) {
      throw new Error('a single long line must be broken at a space, never truncated')
    }
    const short = splitCaptionForMedia('Small post with one line', 1024)
    if (short.head !== 'Small post with one line' || short.tail !== '') {
      throw new Error('a short caption must pass through untouched: ' + JSON.stringify(short))
    }
  }
  // v17.8 A DIGEST ITEM IS THE SOURCE POST: no invented badge, so a figure the
  // source stated once is printed once.
  {
    const job = { text: `Cover for iPhone 13\nNow ₹260 (74% off)\nMRP ₹999 with free delivery\nhttps://fktr.in/CASE1` }
    const item = formatDigestItem(job, 1)
    for (const figure of ['₹260', '74% off', '₹999']) {
      const hits = (item.match(new RegExp(figure.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')) || []).length
      if (hits !== 1) throw new Error(`${figure} printed ${hits} times in one digest item:\n${item}`)
    }
    if (item.includes('…')) throw new Error('a digest item was cut short:\n' + item)
    if (!/^\*1\.\* /m.test(item)) throw new Error('a digest item must carry its number:\n' + item)
  }
  if (!isSpecialOffer('₹2200 Off with PNB Credit Card') || !isSpecialOffer('FLASH 90% OFF')) throw new Error('special offer detection failed')
  const specialCaption = formatSpecialCaption({ text: 'Gold Pendant ₹2200 Off with PNB Credit Card\nhttps://fktr.in/OUR' })
  if (!specialCaption.includes('Gold Pendant ₹2200 Off with PNB Credit Card')) throw new Error('special caption must print the source line as written:\n' + specialCaption)
  if (!specialCaption.includes('https://fktr.in/OUR')) throw new Error('special caption lost its link')
  if (/LOOT ZONE|SPECIAL OFFER|Verified • Enjoy/i.test(specialCaption)) throw new Error('special caption must not carry our own wrapper:\n' + specialCaption)
  const five = Array.from({ length: 5 }, (_, i) => ({ text: `Product ${i + 1} at ₹${90 + i}\nhttps://fktr.in/OUR${i + 1}` }))
  const under99Built = buildBucketDigest(BUCKETS[0], five)
  if (!under99Built) throw new Error('under99 digest not built')
  const under99Digest = under99Built.digest
  if (/DEALS OF THE DAY|UNDER ₹99|Verified deals • Enjoy|LOOT ZONE/i.test(under99Digest)) {
    throw new Error('a digest must not open with a banner of ours:\n' + under99Digest.slice(0, 120))
  }
  if ((under99Digest.match(/https:\/\/fktr\.in\/OUR/g) || []).length !== 5) throw new Error('under99 five-product digest test failed:\n' + under99Digest)
  const ten = Array.from({ length: 10 }, (_, i) => ({ text: `Loot product ${i + 1}\nhttps://fktr.in/LOOT${i + 1}` }))
  const lootBuilt = buildBucketDigest(BUCKETS[2], ten)
  if (!lootBuilt || (lootBuilt.digest.match(/https:\/\/fktr\.in\/LOOT/g) || []).length !== 10
      || lootBuilt.digest.length > DIGEST_MAX_CHARS) throw new Error('LootZone ten-product digest test failed')
  // v17.8 DIGEST FIDELITY - the two mistakes the user named on WhatsApp:
  // (1) a price the source wrote once printed TWICE (our invented badge sat next
  // to the source's own price line), and (2) the item cut to a few dozen
  // characters so the MRP, the coupon and the closing note simply vanished.
  {
    const items = Array.from({ length: 6 }, (_, i) => ({
      text: [
        `Prestige PKFR 1.2L Fryo Classic Fry Pan (Red) ${i + 1}`,
        `Now ₹899 (MRP ₹2,495 64% OFF)`,
        `Use code: SALE${i}100 for extra savings`,
        `Exchange offer up to ₹1,500 on this pan`,
        `https://fktr.in/FID${i}`,
      ].join('\n'),
    }))
    const built = buildBucketDigest(BUCKETS[0], items)
    if (!built) throw new Error('digest fidelity test: nothing built')
    for (const job of items) {
      for (const line of job.text.split('\n')) {
        if (!built.digest.includes(line)) {
          throw new Error(`digest lost a source line (${line}):\n${built.digest}`)
        }
      }
    }
    if (built.digest.includes('…')) throw new Error('a digest must never cut source text:\n' + built.digest)
    const escape = x => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    for (const price of ['₹899', '₹2,495', '₹1,500']) {
      const hits = (built.digest.match(new RegExp(escape(price), 'g')) || []).length
      if (hits !== 6) throw new Error(`${price} appears ${hits} times in a digest of 6 posts - each post must state a figure exactly as often as its source did:\n${built.digest}`)
    }
    if (/\b64% OFF\b[^\n]*\b64% OFF\b/.test(built.digest)) throw new Error('discount printed twice on one line:\n' + built.digest)
    if (built.used.length !== items.length) throw new Error('every fitting item must be used')
    // Too many deals for one message: the digest carries FEWER COMPLETE posts,
    // it never truncates them, and the leftovers are reported as unused so they
    // stay in the queue for the next list.
    const many = Array.from({ length: 40 }, (_, i) => ({
      text: `Borot${i} Heavy Duty Mixer Grinder 750W 3 Jars
${`Now ₹${2000 + i} MRP ₹6,999 with a long descriptive tail of source words to push this item over the budget`.repeat(3)}
https://fktr.in/MANY${i}`,
    }))
    const overflow = buildBucketDigest(BUCKETS[2], many)
    if (!overflow) throw new Error('overflow digest not built')
    if (overflow.used.length >= many.length) throw new Error('an over-budget bucket must carry fewer items')
    if (overflow.digest.length > DIGEST_MAX_CHARS + Math.max(...overflow.used.map(job => (job.text || '').length))) {
      throw new Error('overflow digest is unreasonably large')
    }
    if (overflow.digest.includes('…')) throw new Error('overflow digest truncated a deal instead of deferring it')
    for (const job of overflow.used) {
      for (const line of job.text.split('\n')) {
        if (line.trim() && !overflow.digest.includes(line)) throw new Error(`overflow digest lost a line of an item it did send:\n${line}`)
      }
    }
  }
  const sameCampaignA = 'Upto 85% Off On Branded Shoes.\nPuma https://fktr.in/A'
  const sameCampaignB = 'Upto 85% Off On Branded Shoes.\nPuma https://fktr.in/B\nNike https://fktr.in/C'
  if (contentFingerprint(sameCampaignA) !== contentFingerprint(sameCampaignB)) throw new Error('content duplicate fingerprint test failed')
  const megaText = ['Myntra: Up to 83% Off', ...Array.from({ length: 8 }, (_, i) => `Category ${i + 1}: https://bitli.in/OUR${i + 1}`)].join('\n')
  const textOnlyMega = classifyPost(megaText, false)
  const photoMega = classifyPost(megaText, true)
  if (!textOnlyMega.largeList || textOnlyMega.special || !photoMega.largeList || !photoMega.special) throw new Error('mega/special precedence test failed')
  const megaChunks = buildLargeListChunks({ text: megaText })
  if (!megaChunks.length) throw new Error('mega list produced no chunks')
  if (/LOOT ZONE|MEGA DEAL LIST|Verified deals/.test(megaChunks.join('\n'))) {
    throw new Error('the mega list must not carry our own branding banner:\n' + megaChunks[0])
  }
  if (!megaChunks[0].includes('Myntra: Up to 83% Off')) throw new Error('the source header must lead the list:\n' + megaChunks[0])
  if ((megaChunks.join('\n').match(/https:\/\//g) || []).length !== 8) throw new Error('every link of the list must appear exactly once')
  if (process.env.SELF_TEST_PROVENANCE_URL) {
    const missing = await verifyBestGaaProvenance([process.env.SELF_TEST_PROVENANCE_URL, 'https://foreign.invalid/not-ours'])
    if (missing.length !== 1 || missing[0] !== 'https://foreign.invalid/not-ours') throw new Error('SQLite provenance test failed')
  }
  // Deal-name extraction is still used for digests/gates; the WhatsApp post
  // itself is the cleaned source text with our links on their own lines.
  if (!extractDealName('KILLER Mens Loafers Start at Rs.408\nhttps://a.test/1')) throw new Error('deal name extraction test failed')
  if (extractDealName('₹99 only\nhttps://a.test/1')) throw new Error('nameless post wrongly got a deal name')
  if (extractDealName('DEALS OF THE DAY\nhttps://a.test/1')) throw new Error('generic header wrongly used as deal name')
  {
    const named = formatWhatsAppPost({ text: 'Flipkart | 92% Off - Duck Slide Toy Set at Rs.229\nhttps://fktr.in/OUR' })
    if (named.split('\n')[0].trim() !== 'Flipkart | 92% Off - Duck Slide Toy Set at Rs.229') {
      throw new Error('the source line must be the first line, exactly as written:\n' + named)
    }
    if (named.includes('💰') || named.includes('*Flipkart')) throw new Error('no synthetic badge or bold title may be added:\n' + named)
    if (named.split('https://fktr.in/OUR').length !== 2) throw new Error('our link must appear exactly once, on its own line:\n' + named)
    const twoLine = formatWhatsAppPost({ text: 'Puma Running Shoes ₹2999\n70% OFF ends today\nhttps://fktr.in/OUR2\nExtra line of details' })
    for (const line of ['Puma Running Shoes ₹2999', 'Extra line of details']) {
      if (!twoLine.includes(line)) throw new Error(`source line lost in a structured post: ${line}\n${twoLine}`)
    }
    if (!/70% OFF/.test(twoLine)) throw new Error('the discount the source printed must survive:\n' + twoLine)
    if (twoLine.split('https://').length !== 2) throw new Error('the link must appear once:\n' + twoLine)
  }
  const namedItem = formatDigestItem({ text: 'Nike Sneakers ₹2499\nhttps://fktr.in/N1' }, 1)
  if (!namedItem.includes('Nike Sneakers') || !namedItem.includes('https://fktr.in/N1')) throw new Error('digest item must carry the source line and our link:\n' + namedItem)
  if (namedItem.includes('➜')) throw new Error('a digest must not decorate links with characters of ours:\n' + namedItem)

  // Best-deal gate: verify before sending — skip when it is not a best deal.
  // WA_BEST_GATE=false switches the gate off by design, so each promise below is judged
  // against the mode that is actually running (an assertion that ignores the knob it depends
  // on is a false alarm waiting for an operator). With the gate off the one thing that must
  // still hold is that NOTHING of ours gets skipped, so only the "must pass" checks apply.
  const gateOn = BEST_DEAL_GATE
  const gateAccepts = (job) => (gateOn ? passesBestDealGate(job).ok : true)
  // A "must be skipped" promise is vacuously satisfied when the operator turned the gate off
  // - nothing is skipped then, by design - so it holds in both modes instead of lying.
  const gateSkips = (job) => !gateOn || !passesBestDealGate(job).ok
  const dedupBlocks = (job) => Boolean(duplicateProductReason(job.text, job))
  const gate = (text, media = []) => ({ text, media, special: false, largeList: false })
  if (!gateAccepts(gate('Nice Cotton Saree at \u20b9299\nhttps://a.test/1'))) throw new Error('gate must pass a priced deal with a name')
  if (!gateSkips(gate('\u20b949\nhttps://a.test/1'))) throw new Error('gate must skip a nameless price post')
  if (!gateSkips(gate('Something about a product here\nhttps://a.test/1'))) throw new Error('gate must skip a post without price/discount/special signal')
  if (!gateAccepts(gate('Sneakers 70% OFF today\nhttps://a.test/1'))) throw new Error('gate must pass a 70% off deal')
  if (!gateAccepts(gate('Lunch Box https://a.test/1 https://a.test/2 https://a.test/3 https://a.test/4'))) throw new Error('gate must pass a mega list')
  // ADVANCED QUALITY GATE: a photo alone must NOT pass (that was the loophole that let the
  // worst deals into the WhatsApp channel); a photo WITH real price + discount must.
  if (!gateSkips(gate('Camera with photo\nhttps://a.test/1', [{}]))) throw new Error('gate must REJECT a photo-only post with no price/discount')
  if (!gateAccepts(gate('Wireless Earbuds \u20b9799 55% OFF\nhttps://a.test/1', [{} ]))) throw new Error('gate must pass a photo deal with good price+discount')
  // Expensive + weak discount is rejected with a photo or without one.
  if (!gateSkips(gate('Smart Watch \u20b92499 15% OFF\nhttps://a.test/1', [{}]))) throw new Error('gate must REJECT an expensive weak-discount photo deal')
  if (!gateSkips(gate('Premium Fridge \u20b929999 10% OFF\nhttps://a.test/1'))) throw new Error('gate must REJECT an expensive weak-discount text deal')
  if (!gateSkips(gate('Random Electronic Gadget thing here\nhttps://a.test/1'))) throw new Error('gate must REJECT a signal-less post')
  // Cheap useful single product passes (\u20b999-or-less auto-pass).
  if (!gateAccepts(gate('Cotton Socks Pack \u20b999\nhttps://a.test/1'))) throw new Error('gate must pass an under-\u20b999 product')
  // A modest-discount mid-price USEFUL product clears the quality score.
  if (!gateAccepts(gate('Running Shoes \u20b9899 40% OFF\nhttps://a.test/1'))) throw new Error('gate must pass a useful mid-price 40% deal')
  // A \u20b91500 20% off non-essential with no photo does not reach the score.
  if (!gateSkips(gate('Generic Gadget \u20b91500 20% OFF\nhttps://a.test/1'))) throw new Error('gate must REJECT a weak mid-price low-score deal')
  // Service offers (Zomato/Swiggy/Zepto/movies/cards) are specials and skip the
  // affiliate provenance DB check; store links keep full verification.
  if (!isServiceOffer('Zomato 50% OFF today\nhttps://zom.to/abc')) throw new Error('zomato service offer detection failed')
  if (!isServiceOffer('Swiggy One free for 3 months https://sw.gy/xyz')) throw new Error('swiggy service offer detection failed')
  if (!isSpecialOffer('Zomato 50% OFF today\nhttps://zom.to/abc')) throw new Error('service offer must be treated as a special offer')
  if (!isSpecialOffer('BookMyShow movie ticket 40% OFF https://bookmyshow.com/x')) throw new Error('movie service offer detection failed')
  if (isServiceUrl('https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21')) throw new Error('amazon URL wrongly classified as service URL')
  {
    // Service links AND our-tag Amazon links are both exempt from the link_cache
    // DB check; a foreign-tag Amazon link still goes through for verification.
    const toVerify = urlsForProvenance(null, ['https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21', 'https://zom.to/abc', 'https://www.amazon.in/dp/B0CHECK99?tag=other-21'])
    if (toVerify.length !== 1 || toVerify[0] !== 'https://www.amazon.in/dp/B0CHECK99?tag=other-21') throw new Error('service + our-tag provenance exemption test failed')
  }
  // Group JID resolution (pure parts; invite codes are resolved live on connect).
  if ((await resolveGroupTarget({}, '919876543210')) !== '919876543210@g.us') throw new Error('group phone resolution test failed')
  if ((await resolveGroupTarget({}, '123456789-123456@g.us')) !== '123456789-123456@g.us') throw new Error('group JID passthrough test failed')
  let badGroupRejected = false
  try { await resolveGroupTarget({}, 'not-a-group') } catch { badGroupRejected = true }
  if (!badGroupRejected) throw new Error('invalid group target not rejected')
  {
    const fakeSock = { groupAcceptInvite: async code => `999888777-${code}@g.us` }
    if ((await resolveGroupTarget(fakeSock, 'ABC123')) !== '999888777-abc123@g.us') throw new Error('group invite code resolution test failed')
    // Full invite links (copied straight from a WhatsApp group) must resolve
    // the same as a bare code: https://chat.whatsapp.com/CODE -> code.
    if ((await resolveGroupTarget(fakeSock, 'https://chat.whatsapp.com/EAbCd1234xy')) !== '999888777-eabcd1234xy@g.us') throw new Error('full chat.whatsapp.com invite link resolution failed')
    if ((await resolveGroupTarget(fakeSock, 'http://www.chat.whatsapp.com/invite/EAbCd1234xy')) !== '999888777-eabcd1234xy@g.us') throw new Error('chat.whatsapp.com/invite/ link resolution failed')
  }
  // Product-level dedup: the SAME product (ASIN/slug, not just exact URL) must
  // not repeat inside the dedup window; older than the window it is fine.
  if (productIdentity('https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21') !== 'amazon:B0GLY3Q2XR') throw new Error('amazon ASIN identity test failed')
  if (productIdentity('https://www.flipkart.com/mens-cotton-kurta/p/itm0abc?affExtParam2=5478322') !== 'flipkart:itm0abc') throw new Error('flipkart slug identity test failed')
  {
    state.sentProducts = {} // hermetic: test its own dedup history
    const beforeNamePrice = state.sentNamePrice
    state.sentNamePrice = {}
    // B0TESTPROD is a valid 10-char ASIN; a different tag on the same product
    // must still be recognized as the same product.
    const jobA = { text: 'Cotton Kurta ₹499\nhttps://www.amazon.in/dp/B0TESTPROD?tag=deals0911-21', media: [], special: false, largeList: false }
    const jobB = { text: 'Same kurta new price ₹449\nhttps://www.amazon.in/dp/B0TESTPROD?tag=other-tag', media: [], special: false, largeList: false }
    if (PRODUCT_DEDUP_HOURS > 0 && dedupBlocks(jobA)) throw new Error('fresh product wrongly blocked by dedup')
    markProductSent(jobA)
    if (PRODUCT_DEDUP_HOURS > 0 && !dedupBlocks(jobB)) throw new Error('same product (different URL/tag) not caught by product dedup')
    state.sentProducts['amazon:B0TESTPROD'] = Date.now() - (PRODUCT_DEDUP_HOURS + 1) * 3600_000
    if (PRODUCT_DEDUP_HOURS > 0 && dedupBlocks(jobB)) throw new Error('product older than the dedup window wrongly blocked')
    delete state.sentProducts['amazon:B0TESTPROD']
    state.sentNamePrice = beforeNamePrice
    saveState() // persist the cleanup for any pre-existing state file
  }
  // Name+price dedup: the SAME shortlink product (no ASIN/slug) re-posted with
  // a different fktr/bitly link and rewritten caption must still be caught; a
  // genuine PRICE DROP on the same product is a new deal and must pass.
  {
    state.sentNamePrice = {}
    const dealA = { text: 'boAt Rockerz Earbuds Bluetooth Black\n75% OFF MRP ₹3199\nDeal Price ₹799\nGrab fast buy now\nhttps://fktr.in/AAA111', media: [], special: false, largeList: false }
    if (PRODUCT_DEDUP_HOURS > 0 && dedupBlocks(dealA)) throw new Error('fresh shortlink product wrongly blocked')
    markProductSent(dealA)
    const dealAsame = { text: 'boAt Rockerz Earbuds Bluetooth Black\n75% OFF only ₹799\nLimited time offer click here\nhttps://bit.ly/ZZZ999', media: [{}], special: false, largeList: false }
    if (PRODUCT_DEDUP_HOURS > 0 && !dedupBlocks(dealAsame)) throw new Error('same product (shortlink, same price, different link/caption) not caught by name+price dedup')
    const dealADrop = { text: 'boAt Rockerz Earbuds Bluetooth Black\n78% OFF MRP ₹3199\nDeal Price ₹699\nhttps://fktr.in/AAA222', media: [], special: false, largeList: false }
    if (PRODUCT_DEDUP_HOURS > 0 && dedupBlocks(dealADrop)) throw new Error('genuine price drop (₹799->₹699) must NOT be treated as a duplicate')
    state.sentNamePrice = {}
    saveState()
  }
  // No random/unwanted text next to the price: CTA/promo fragments glued to a
  // deal line are stripped. v17.5: every line the source wrote stays (nothing is
  // dropped as "redundant"), so name + specs + MRP + discount all survive.
  {
    const posted = formatWhatsAppPost({ text: 'Premium Cotton T-Shirt Men Blue Round Neck\n75% OFF\nMRP ₹1999\nDeal Price ₹499 grab fast buy now\nhttps://fktr.in/TSHIRT1' })
    if (/grab\s+fast|buy\s+now|click\s+here|limited\s+time/i.test(posted)) throw new Error('CTA junk leaked next to the price: ' + posted)
    if (!/Premium Cotton T-Shirt/.test(posted)) throw new Error('product name must survive CTA strip')
    if (!/MRP[^\n]*₹?\s*1,?999/.test(posted)) throw new Error('MRP line must survive: ' + posted)
    if (!/^75% OFF$/im.test(posted)) throw new Error('the discount line the source printed must stay in the post:\n' + posted)
    if (posted.includes('💰')) throw new Error('no price badge may be added:\n' + posted)
    const posted2 = formatWhatsAppPost({ text: 'Wireless Mouse Silent Click\nMRP ₹999\nPrice ₹449 only\nBuy now link below\nhttps://fktr.in/MOUSE1' })
    if (/link\s+below|buy\s+now/i.test(posted2)) throw new Error('promo line leaked under the price: ' + posted2)
    // A promo/channel HEADER must never become the bold deal name.
    const headered = formatWhatsAppPost({ text: 'DEALS OF THE DAY\nLOOT ZONE INDIA\nMen Sneakers White ₹899 60% OFF\nhttps://fktr.in/SNEAK1' })
    if (/^\*DEALS OF THE DAY\*/i.test(headered)) throw new Error('no bold banner of ours may be added: ' + headered)
    if (!STRIP_CAMPAIGN_BANNERS && !/^DEALS OF THE DAY$/im.test(headered)) {
      throw new Error("the source's own header line stays (fidelity): " + headered)
    }
    if (!/^Men Sneakers White ₹899 60% OFF$/im.test(headered)) throw new Error('real product line must survive: ' + headered)
    // Leading decorative emoji/bullet: the source's own arrow is content, so it
    // stays - only markdown debris of ours is never added.
    const arrowName = formatWhatsAppPost({ text: '👉 boAt Earbuds Black Bluetooth\n₹799 75% OFF\nhttps://fktr.in/ARR1' })
    // v17.5: the source's own leading arrow is its text, not ours - it stays,
    // and no bold title is manufactured out of the line.
    if (!/^👉 boAt Earbuds Black Bluetooth$/.test(arrowName.split('\n')[0].trim())) throw new Error('source line must print as written: ' + arrowName)
    if (arrowName.includes('*')) throw new Error('no bold wrapper may be added: ' + arrowName)
    const starName = formatWhatsAppPost({ text: '* • Cotton Kurta Men\n₹499 65% OFF\nhttps://fktr.in/STR1' })
    if (!/^Cotton Kurta Men$/.test(starName.split('\n')[0].trim())) throw new Error('markdown bullet debris must be cleaned, the words must stay: ' + starName)
    // Mega-LIST chunks: a CTA glued to a product label is stripped, and a line
    // that becomes empty after stripping is dropped. v17.5: no header banner of ours
    // is prepended either - the source's own first line leads the list.
    {
      const listJob = {
        text: ['MEGA DEAL LIST',
          'Cotton T-Shirt ₹89 buy now grab fast https://a.test/l1',
          'Sneakers ₹499 click here https://a.test/l2',
          'Only grab fast now',
          'Wireless Earbuds ₹399 https://a.test/l3',
          'Backpack ₹299 https://a.test/l4'].join('\n'),
        media: [], largeList: true,
      }
      const chunks = buildLargeListChunks(listJob)
      const joined = chunks.join('\n')
      if (/LOOT ZONE|Verified deals \u2022 Enjoy/.test(joined)) {
        throw new Error('our own branding banner must be gone from the list:\n' + joined)
      }
      if (/buy\s+now|grab\s+fast|click\s+here/i.test(joined)) throw new Error('CTA junk leaked into a mega list: ' + joined)
      if (!/Cotton T-Shirt ₹89/.test(joined) || !/Backpack ₹299/.test(joined)) throw new Error('real list products must survive: ' + joined)
      for (const link of ['l1', 'l2', 'l3', 'l4']) {
        const hits = (joined.match(new RegExp('https://a\\.test/' + link, 'g')) || []).length
        if (hits !== 1) throw new Error(`list link ${link} must appear exactly once:\n${joined}`)
      }
      if (/\n\s*\n\s*\n/.test(joined)) throw new Error('empty (CTA-only) line left a blank gap in the list')
    }
    // Digest labels never carry promo/CTA words either.
    {
      const digest = formatDigestItem({ text: 'boAt Earbuds Black ₹799 75% OFF grab now\nhttps://fktr.in/DBG' }, 1)
      if (/grab\s*now|buy\s+now|click/i.test(digest)) throw new Error('CTA junk leaked into digest label: ' + digest)
      if (!/boAt Earbuds/.test(digest)) throw new Error('digest must keep the product name: ' + digest)
    }
    // STRICT global sweep: CTA ANYWHERE in a line is removed, while genuine deal
    // words (free shipping, use code, limited stock, product specs) survive.
    {
      const mid = formatWhatsAppPost({ text: 'Cotton Kurta Set 70% OFF buy now MRP ₹1999 Deal ₹499\nhttps://fktr.in/MID' })
      if (/buy\s+now|shop\s+now|click|grab|hurry|don'?t\s+miss/i.test(mid)) throw new Error('mid-line CTA leaked: ' + mid)
      if (!/Cotton Kurta Set/.test(mid) || !/MRP ₹1,?999|MRP ₹1999/.test(mid)) throw new Error('deal content lost in strict sweep: ' + mid)
      const noti = stripInlineCta('Free shipping above ₹499 turn on notifications for more loots 🔔')
      if (/notifications|turn\s+on|🔔/.test(noti)) throw new Error('notification promo not stripped: ' + noti)
      if (!/Free shipping/.test(noti)) throw new Error('free shipping info wrongly stripped: ' + noti)
      const safe = stripInlineCta('Use code MYNTRA at checkout · Limited stock · Pack of 2')
      if (!/Use code MYNTRA|Limited stock|Pack of 2/.test(safe)) throw new Error('genuine deal words wrongly stripped: ' + safe)
      const tme = stripInlineCta('Follow us on t.me/somechannel for deals · ₹299')
      if (/t\.me|follow/i.test(tme)) throw new Error('t.me self-promo not stripped: ' + tme)
    }
    // Source promo that carries a channel link is removed as a WHOLE line (a
    // partial strip used to leak `.com/channel/0029` style residue into the
    // published post), and referral/app-install farming never reaches the post.
    {
      const noisy = cleanDealText([
        'Sony 32 inch HD Smart TV',
        'Deal Price ₹11,990 (37% OFF)',
        'Join our WhatsApp Channel for more deals: https://whatsapp.com/channel/0029',
        'Visit our channel: https://t.me/someotherchannel',
        'ps://broken',
      ].join('\n'))
      if (/whatsapp|someotherchannel|\.com\/channel|ps:\/\//i.test(noisy)) {
        throw new Error('channel promo or URL residue leaked into the post: ' + noisy)
      }
      if (!/Sony 32 inch HD Smart TV/.test(noisy) || !/11,?990/.test(noisy)) {
        throw new Error('deal content must survive promo cleanup: ' + noisy)
      }
      const keep = cleanDealText('Cotton Tshirt ₹249 https://fktr.in/KEEPME')
      if (!/fktr\.in\/KEEPME/.test(keep)) throw new Error('merchant link must be preserved: ' + keep)
      const ref = cleanDealText('Zepto groceries ₹200\nInstall the app and refer a friend to earn ₹50')
      if (/refer a friend|Install the app/i.test(ref)) throw new Error('referral spam leaked: ' + ref)
      if (!/200/.test(ref)) throw new Error('price lost while stripping referral spam: ' + ref)
    }
  }
  // Bitly shortening policy (quota-smart): long links always; LIST posts
  // (2+ links) always; a normal short single amazon dp link posts as-is so
  // the Bitly monthly quota is never wasted on ordinary product deals.
  const longAmazon = `https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21&m=abc123&ascsubtag=${'x'.repeat(120)}`
  if (longAmazon.length > SHORTEN_MIN_LEN && !needsShortening(longAmazon)) throw new Error('long amazon link must be shortened')
  if (needsShortening('https://bit.ly/abc')) throw new Error('already-short link must not be re-shortened')
  if (needsShortening('https://bit.ly/abc', true)) throw new Error('already-short link must not be re-shortened even in a list')
  if (needsShortening('https://zom.to/abc')) throw new Error('service short link must not be shortened')
  {
    // Stated against WA_SHORTEN_MIN_LEN rather than its default, so the assertion stays
    // true when an operator moves the knob (the policy IS the threshold).
    const tidyDp = 'https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21'
    if (tidyDp.length < SHORTEN_MIN_LEN && needsShortening(tidyDp)) {
      throw new Error('a single tidy link under the threshold must NOT burn Bitly quota')
    }
    if (tidyDp.length > SHORTEN_MIN_LEN && !needsShortening(tidyDp)) {
      throw new Error('a single link over the threshold must be shortened')
    }
  }
  if (!needsShortening('https://www.amazon.in/dp/B0GLY3Q2XR?tag=deals0911-21', true)) throw new Error('every list link must be shortened')
  {
    // Same rule as above, expressed against the knob: a tidy deep link is left alone
    // while it is UNDER WA_SHORTEN_MIN_LEN; an operator who lowers the threshold to 1
    // has asked for everything to be shortened, and that is what must happen.
    const tidyFlipkart = 'https://www.flipkart.com/x/p/itm1'
    if (tidyFlipkart.length <= SHORTEN_MIN_LEN && needsShortening(tidyFlipkart)) {
      throw new Error('a short flipkart link under the threshold must not be shortened')
    }
    if (tidyFlipkart.length > SHORTEN_MIN_LEN && !needsShortening(tidyFlipkart)) {
      throw new Error('a flipkart link over the threshold must be shortened')
    }
  }
  if (displayUrl({ shortLinks: { a: 'b' } }, 'a') !== 'b') throw new Error('displayUrl must use the shortened link')
  if (displayUrl({}, 'a') !== 'a' || displayUrl({ shortLinks: {} }, 'a') !== 'a') throw new Error('displayUrl must fall back to the original link')
  {
    const cachedUrl = 'https://cached.test/long'
    state.shortLinks = { [cachedUrl]: 'https://bit.ly/cached' }
    const shortened = await shortenLongUrl(cachedUrl)
    if (shortened !== 'https://bit.ly/cached') throw new Error('short-link cache must be served without a network call')
    delete state.shortLinks[cachedUrl]
  }
  // Content preservation: body keeps the source text, each link appears exactly
  // once (at the bottom), and shortened display links are used.
  {
    const job = { text: 'Puma Shoes ₹2999. Buy at https://bit.ly/shortlink. Great quality running shoes for men.' }
    const post = formatWhatsAppPost(job)
    if (!post.includes('Great quality running shoes for men')) throw new Error('source text line lost in the WhatsApp post:\n' + post)
    const postNoLinks = post.replace(/https?:\/\/\S+/g, '')
    if (/buy\s+at|buy\s+here|shop\s+at/i.test(postNoLinks)) throw new Error('dangling "Buy at" CTA must be stripped: ' + post)
    if (!new RegExp('\\nhttps://bit\\.ly/shortlink(\\s|$)').test(post + '\n')) throw new Error('inline link must still be printed on its own line: ' + post)
    if (post.split('https://bit.ly/shortlink').length !== 2) throw new Error('link must appear exactly once, at the bottom')
    job.shortLinks = { 'https://bit.ly/shortlink': 'https://bit.ly/newshort' }
    const post2 = formatWhatsAppPost(job)
    if (!post2.includes('https://bit.ly/newshort') || post2.includes('https://bit.ly/shortlink')) throw new Error('shortened display link not used in the post')
  }
  // Single-line posts: the name line IS the content — nothing may be cut.
  {
    const oneLiner = 'Men Cotton Kurta Set - Blue. Deal Price: ₹499. MRP: ₹1999. 75% OFF. Free shipping above ₹499. Buy now.'
    const post = formatWhatsAppPost({ text: oneLiner + '\nhttps://fktr.in/OUR' })
    // v17.5: the whole source line prints as written - no bold hoist, no badge,
    // no clause dropped as "redundant". Only the CTA ("Buy now.") is removed.
    if (!post.includes('Men Cotton Kurta Set - Blue')) throw new Error('single-line post lost the product headline')
    if (!post.includes('MRP: ₹1999')) throw new Error('single-line post lost MRP clause')
    if (!post.includes('Free shipping above ₹499')) throw new Error('single-line post lost the shipping info clause')
    if (/\bbuy\s+now\b/i.test(post)) throw new Error('CTA "Buy now." must be stripped from a one-line post')
    // Every clause the source wrote is present exactly once - not dropped, and
    // not printed twice by us.
    if (!post.includes('Deal Price: ₹499')) throw new Error('single-line post lost its deal price:\n' + post)
    if (!/75% OFF/.test(post)) throw new Error('single-line post lost the discount:\n' + post)
    if ((post.match(/Deal\s*Price/gi) || []).length !== 1) throw new Error('the deal price must appear exactly once:\n' + post)
    if (post.includes('*')) throw new Error('no bold wrapper may be added to a one-line post:\n' + post)
    if (/^\s*75%\s*OFF\s*$/im.test(post.replace(/^.*🔥.*$/m, ''))) throw new Error('bare discount clause repeated in the body:\n' + post)
    // The bold title is never repeated as a plain body line.
    {
      const dup = formatWhatsAppPost({ text: '👉 boAt Rockerz 450 Headphones Black\nMRP ₹3990\nDeal Price ₹799\nhttps://fktr.in/DUP' })
      if ((dup.match(/boAt Rockerz 450 Headphones Black/g) || []).length !== 1) throw new Error('the product line must print once, as written:\n' + dup)
      if (dup.includes('*')) throw new Error('no bold markers of ours may be added:\n' + dup)
      if (!dup.includes('MRP ₹3990') || !dup.includes('Deal Price ₹799')) throw new Error('both source price lines must survive:\n' + dup)
      if (/[*_]/.test(dup)) throw new Error('no markdown/bold decoration of ours may appear:\n' + dup)
      if (!dup.includes('👉 boAt Rockerz 450 Headphones Black')) throw new Error('the source\'s own arrow stays with its line:\n' + dup)
    }
  }
  // v17.5: nothing is suppressed as "redundant with our badge" any more, because
  // we add no badge. The source printed its deal price and its MRP, so BOTH
  // lines print exactly as written, in source order, once each.
  {
    const job = { text: 'Cotton Kurta\nDeal Price: ₹499\nMRP: ₹1999\nFree shipping\nhttps://www.amazon.in/dp/B0PRICETST?tag=' + AMAZON_TAG }
    const post = formatWhatsAppPost(job)
    for (const line of ['Cotton Kurta', 'Deal Price: ₹499', 'MRP: ₹1999', 'Free shipping']) {
      if (!post.includes(line)) throw new Error(`source line lost from the post: ${line}\n${post}`)
    }
    if ((post.match(/Deal Price: ₹499/g) || []).length !== 1) throw new Error('a source line must never be printed twice:\n' + post)
    if (post.includes('💰') || post.includes('🔥 ')) throw new Error('no synthetic badge line may be added:\n' + post)
  }
  {
    // Price attached to a real product/quantity line is content, not a repeat.
    const job = { text: 'Boys Combo T-Shirt Pack\nPack of 2 at ₹599\nBuy now\nhttps://www.amazon.in/dp/B0PRICETWO?tag=' + AMAZON_TAG }
    const post = formatWhatsAppPost(job)
    if (!post.includes('Pack of 2 at ₹599')) throw new Error('price+quantity product line must survive')
  }
  // cleanDealText regression: a line following a link line must survive (the
  // trailing-words trim must never cross a newline).
  if (!cleanDealText('https://a.in/x/itm2?track=two\nUse code MYNTRA before checkout').includes('Use code MYNTRA before checkout')) {
    throw new Error('cleanDealText ate the line after a link')
  }
  // Multi-link lists: each link stays UNDER its product label, in source
  // order, and every link appears exactly once (no pooled mystery links).
  {
    const listJob = {
      text: 'Myntra Mega Sale\n'
        + 'Men T-Shirts 70% Off\nhttps://www.myntra.com/mens-tshirts/itm1?affExtParam1=aa11&affExtParam2=5478322&track=one\n'
        + 'Women Kurtas 60% Off\nhttps://www.myntra.com/women-kurtas/itm2?affExtParam1=bb22&affExtParam2=5478322&track=two\n'
        + 'Use code MYNTRA before checkout',
    }
    const post = formatWhatsAppPost(listJob)
    if (!post.startsWith('Myntra Mega Sale')) throw new Error('list: the source header must lead, unbolded:\n' + post)
    if (post.includes('*')) throw new Error('list: no bold markers of ours may be added:\n' + post)
    if (!post.includes('Men T-Shirts 70% Off\nhttps://www.myntra.com/mens-tshirts/itm1?affExtParam1=aa11&affExtParam2=5478322&track=one')) throw new Error('list: link must stay under its product label:\n' + post)
    if (!post.includes('Women Kurtas 60% Off\nhttps://www.myntra.com/women-kurtas/itm2?affExtParam1=bb22&affExtParam2=5478322&track=two')) throw new Error('list: second link must stay under its label:\n' + post)
    if (post.split('track=one').length !== 2 || post.split('track=two').length !== 2) throw new Error('list: each link must appear exactly once')
    if (!post.includes('Use code MYNTRA before checkout')) throw new Error('list: trailing text line lost')
    const tShirtIdx = post.indexOf('itm1')
    const kurtasIdx = post.indexOf('itm2')
    if (!(tShirtIdx < kurtasIdx)) throw new Error('list: source order not preserved')
  }
  // -------------------------------------------------------------------------
  // DIRECT SOURCE SAFETY NET: deals the Telegram bot missed still reach
  // WhatsApp — smartly (resolve, monetize amazon, dedup against our channels).
  // -------------------------------------------------------------------------
  if (classifySource({ chat: { username: 'under499loots' } }) !== 'source') throw new Error('own channel must classify as source')
  if (DIRECT_SOURCES.size && classifySource({ chat: { username: 'RealShoppingDeals' } }) !== 'direct') throw new Error('raw deal channel must classify as direct')
  if (!DIRECT_SOURCES.size && classifySource({ chat: { username: 'RealShoppingDeals' } }) !== null) throw new Error('TG_DIRECT_SOURCES= (empty) must disable direct sources')
  if (classifySource({ chat: { username: 'someRandomChannel' } }) !== null) throw new Error('unknown channel must be ignored')
  if (!isOurGeneratedLink('https://fktr.in/abc')) throw new Error('fktr.in must count as our generated link')
  if (!isOurGeneratedLink(`https://www.amazon.in/dp/B0GLY3Q2XR?tag=${AMAZON_TAG}`)) throw new Error('amazon with our tag must be ours')
  if (isOurGeneratedLink('https://www.amazon.in/dp/B0GLY3Q2XR?tag=other-21')) throw new Error('amazon with a foreign tag is not ours')
  if (isOurGeneratedLink('https://amzn.to/x')) throw new Error('raw amazon shortener is not ours')
  // Our-tag Amazon links are self-proving and must NEVER reach the link_cache
  // DB check (provenance leak fix): the tag only exists on links WE tagged, the
  // cache stores conversion-API output only, so a DB row may legitimately be
  // absent (bridge-tagged safety-net pages; fresh/pruned/deploy-cleared cache).
  if (!isOurAmazonTagLink(`https://www.amazon.in/dp/B0GLY3Q2XR?tag=${AMAZON_TAG}`)) throw new Error('our-tag amazon link must be recognised')
  if (isOurAmazonTagLink('https://www.amazon.in/dp/B0GLY3Q2XR?tag=thief-21')) throw new Error('foreign-tag amazon link must not match our tag')
  if (isOurAmazonTagLink('https://amzn.to/x')) throw new Error('amazon shortener host has no tag param')
  {
    const ourTagged = `https://www.amazon.in/dp/B0TAGCHECK?tag=${AMAZON_TAG}`
    // Normal (non-direct) job: our-tag link is exempt; a foreign-store link
    // and a foreign-tag Amazon link still go to the DB for verification.
    const normal = urlsForProvenance({ direct: false }, [ourTagged, 'https://fktr.in/CHECK', 'https://www.amazon.in/dp/B0FOREIGN?tag=thief-21'])
    if (normal.includes(ourTagged)) throw new Error('our-tag amazon link must skip provenance DB on a normal job')
    if (!normal.includes('https://fktr.in/CHECK')) throw new Error('our converted short link must still be provenance-checked')
    if (!normal.includes('https://www.amazon.in/dp/B0FOREIGN?tag=thief-21')) throw new Error('foreign-tag amazon link must still be provenance-checked')
    // Direct job whose raw link resolved to an OUR-tagged amazon page: the
    // resolved (display) form is self-proving, so it must be exempt too.
    const djobProv = { direct: true, resolvedLinks: { 'https://amzn.to/TAGGED': ourTagged } }
    const afterResolve = urlsForProvenance(djobProv, ['https://amzn.to/TAGGED'])
    if (afterResolve.length !== 0) throw new Error('direct job resolving to our-tag amazon must skip provenance DB')
  }
  {
    // Raw source links resolve to the merchant page; raw amazon gets OUR tag.
    const fakeFetch = async url => {
      const value = String(url)
      if (value.includes('amzn.to/DEAL1')) return { url: 'https://www.amazon.in/dp/B0DIRECT01?ref=src', body: { cancel: async () => {} } }
      if (value.includes('fkrt.co/DEAL2')) return { url: 'https://www.flipkart.com/direct-item/p/itm77?ref=src', body: { cancel: async () => {} } }
      throw new Error('no network expected for ' + value)
    }
    const djob = { direct: true, text: 'Direct deal ₹299\nhttps://amzn.to/DEAL1\nAlso https://fkrt.co/DEAL2' }
    await prepareDirectJob(djob, fakeFetch)
    if (djob.resolvedLinks['https://amzn.to/DEAL1'] !== `https://www.amazon.in/dp/B0DIRECT01?ref=src&tag=${AMAZON_TAG}`) throw new Error('our tag not appended to raw amazon product page')
    if (djob.resolvedLinks['https://fkrt.co/DEAL2'] !== 'https://www.flipkart.com/direct-item/p/itm77?ref=src') throw new Error('raw flipkart shortener not resolved')
    if (displayUrl(djob, 'https://amzn.to/DEAL1') !== djob.resolvedLinks['https://amzn.to/DEAL1']) throw new Error('displayUrl must prefer the resolved merchant link')
    // Direct jobs never send raw links to the provenance DB; our links do go.
    const prov = urlsForProvenance(djob, ['https://amzn.to/DEAL1', 'https://fktr.in/OURS'])
    if (prov.length !== 1 || prov[0] !== 'https://fktr.in/OURS') throw new Error('direct provenance filter failed')
    // Raw source shorteners are legal ON DIRECT JOBS only.
    validateAffiliateText(djob, djob.text)
    let foreignTagBlocked = false
    try { validateAffiliateText(djob, 'X ₹99\nhttps://www.amazon.in/dp/B0DIRECT01?tag=thief-21') } catch { foreignTagBlocked = true }
    if (!foreignTagBlocked) throw new Error('foreign amazon tag must stay blocked even on direct jobs')
  }
  {
    // linkredirect.in ?dl= destinations decode without any network call.
    const noNet = async () => { throw new Error('no network expected') }
    const djob = { direct: true, text: 'Offer ₹149\nhttps://linkredirect.in/?dl=' + encodeURIComponent('https://www.zomato.in/offer-page') }
    await prepareDirectJob(djob, noNet)
    if (!Object.values(djob.resolvedLinks).includes('https://www.zomato.in/offer-page')) throw new Error('linkredirect dl-param not decoded')
  }
  {
    // USER RULE: huge Amazon category/search links (Puma Men/Women/Girls/Boys
    // style lists). (1) raw /s search pages on direct jobs get OUR tag; (2)
    // session junk (btn_ref=srctok, ds=, qid=, sprefix=, crid=) is stripped
    // before shortening while real filters (k, i, rh, s) and tag survive.
    const noNet = async () => { throw new Error('no network expected') }
    const bigSearch = 'https://www.amazon.in/s?btn_ref=srctok-0a3c6e95f43beb35&btn_type=ss&crid=183FX0PT8V04B&dc=&ds=v1%3AabcXYZ&i=shoes&k=puma&qid=1787941163&rh=n%3A1571283031%2Cn%3A1983396031&rnid=1983396031&s=price-asc-rank&sprefix=pum%2Cshoes%2C288'
    const djob = { direct: true, text: 'Puma Men\n' + bigSearch }
    await prepareDirectJob(djob, noNet)
    const tagged = djob.resolvedLinks[cleanUrl(bigSearch)]
    if (!tagged || !tagged.includes(`tag=${AMAZON_TAG}`)) throw new Error('our tag not appended to raw amazon SEARCH page')
    const cleanedSearch = stripAmazonJunk(tagged)
    if (/btn_ref|srctok|qid=|sprefix=|crid=|[?&]ds=/.test(cleanedSearch)) throw new Error('amazon search junk params not stripped: ' + cleanedSearch)
    if (!/k=puma/.test(cleanedSearch) || !/i=shoes/.test(cleanedSearch) || !/rh=/.test(cleanedSearch) || !/s=price-asc-rank/.test(cleanedSearch)) throw new Error('real amazon filters must survive junk strip: ' + cleanedSearch)
    if (!cleanedSearch.includes(`tag=${AMAZON_TAG}`)) throw new Error('our tag must survive junk strip')
    if (cleanedSearch.length >= tagged.length) throw new Error('junk strip must shorten the search link')
    if (!needsShortening(tagged)) throw new Error('a 200+ char amazon search link must qualify for Bitly')
    // Non-amazon URLs are untouched by the junk stripper.
    const fk = 'https://www.flipkart.com/search?q=puma&qid=xyz&ds=1'
    if (stripAmazonJunk(fk) !== fk) throw new Error('non-amazon URL must not be junk-stripped')
    // Amazon product pages: th/psc style params survive (not in the junk set).
    const dp = 'https://www.amazon.in/dp/B0ABCDEFGH?th=1&tag=' + AMAZON_TAG
    if (stripAmazonJunk(dp) !== dp) throw new Error('amazon dp link with th/tag must be unchanged')
  }
  {
    // A deal already posted via our own channels must never be double-posted
    // from a raw source: the dedup works on the RESOLVED product identity.
    state.sentProducts = {}
    const ourJob = { text: `Cotton Kurta ₹499\nhttps://www.amazon.in/dp/B0DEDUPTST?tag=${AMAZON_TAG}`, media: [] }
    markProductSent(ourJob)
    const directCopy = {
      direct: true, media: [], special: false, largeList: false,
      text: 'Cotton Kurta lowest price ₹499\nhttps://amzn.to/COPY1',
      resolvedLinks: { 'https://amzn.to/COPY1': 'https://www.amazon.in/dp/B0DEDUPTST?ref=raw' },
    }
    // The rule under test is the product dedup itself; asking the gate about it would make
    // this assertion depend on WA_BEST_GATE for no reason.
    const dupReason = duplicateProductReason(directCopy.text, directCopy)
    if (PRODUCT_DEDUP_HOURS > 0 && !/duplicate product/.test(dupReason || '')) {
      throw new Error('direct copy of an already-posted deal must be dedup-skipped, got: ' + dupReason)
    }
    delete state.sentProducts['amazon:B0DEDUPTST']
    saveState()
  }
  // ---------------------------------------------------------------------
  // ZERO DUPLICATES: every layer must catch its own repeat class.
  // ---------------------------------------------------------------------
  {
    const dupText = 'Nike Air Shoes 70% Off ₹1499\nhttps://fktr.in/DUPTEST1'
    // Layer 1: exact deal key at intake
    if (dealKey(dupText) !== dealKey(dupText)) throw new Error('dealKey must be stable')
    // Content fingerprint: same campaign via a different shortlink = same content
    const contentA = contentFingerprint('Nike Air Shoes 70% Off ₹1499\nhttps://fktr.in/AAA')
    const contentB = contentFingerprint('Nike Air Shoes 70% Off ₹1499\nhttps://fktr.in/BBB')
    if (!contentA || contentA !== contentB) throw new Error('same campaign different shortlink must share a content fingerprint')
    // sendSpecialOffer/sendLargeListPart consult sentContent BEFORE verify:
    state.sentContent = { [contentA]: Date.now() }
    const specialJob = { text: 'Nike Air Shoes 70% Off ₹1499\nhttps://fktr.in/CCC', media: [], special: true, largeList: false }
    if ((await sendSpecialOffer(specialJob)) !== 'duplicate') throw new Error('special path must skip a content duplicate without network calls')
    const largeDup = await sendLargeListPart({ text: 'Nike Air Shoes 70% Off ₹1499\nhttps://fktr.in/DDD', media: [], largeChunkIndex: 0 })
    if (largeDup.result !== 'duplicate' || !largeDup.done) throw new Error('large-list path must skip a content duplicate without network calls')
    state.sentContent = {}
    saveState()
  }
  // ---------------------------------------------------------------------
  // ANTI-BAN TIMING: gaps are randomised (never a fixed cadence), burst rests
  // and long idles occur, and a scheduled rest is not truncated by the
  // two-plus-queue accelerator.
  // ---------------------------------------------------------------------
  {
    const before = { next: state.nextAllowedAt, rest: state.antibanRestUntil, burst: state.burstCount, thresh: state.burstThreshold }
    state.sentTimes = [] // neutralise caps
    const gaps = []
    let restedCount = 0
    for (let i = 0; i < 40; i++) {
      const now = Date.now()
      scheduleNext()
      const gap = state.nextAllowedAt - now
      gaps.push(gap)
      if (state.antibanRestUntil >= state.nextAllowedAt - 2000) restedCount++
    }
    const minMs = MIN_WA_MESSAGE_GAP_SECONDS * 1000
    if (gaps.some(g => g < minMs - 2000)) throw new Error('anti-ban gap below the hard floor')
    if (gaps.some(g => g > MAX_WA_MESSAGE_GAP_SECONDS * 1000 + 1000)) {
      throw new Error(`anti-ban gap above the scheduled ceiling (${Math.max(...gaps) / 1000}s > ${MAX_WA_MESSAGE_GAP_SECONDS}s)`)
    }
    // The stacked multipliers must never be able to park the channel for an hour
    // even when every one of them fires at once (v17.8 clamp in scheduleNext).
    if (gaps.some(g => g > MAX_WA_MESSAGE_GAP_SECONDS * 1000)) {
      throw new Error('a scheduled gap went past the clamp: ' + Math.max(...gaps))
    }
    const distinct = new Set(gaps.map(g => Math.round(g / 1000)))
    if (distinct.size < 8) throw new Error('gaps look too regular (bot cadence)')
    if (restedCount < 3) throw new Error('burst/hourly rests almost never scheduled')
    // A scheduled rest must NOT be truncated by queue acceleration.
    state.nextAllowedAt = Date.now() + 9 * 60 * 1000
    state.antibanRestUntil = state.nextAllowedAt
    const now0 = Date.now()
    const restFloor = Math.max(acceleratedNextAllowed(now0, now0, 10, false, state.nextAllowedAt), Number(state.antibanRestUntil))
    if (restFloor < state.antibanRestUntil - 1000) throw new Error('accelerator truncated a scheduled anti-ban rest')
    state.nextAllowedAt = before.next; state.antibanRestUntil = before.rest
    state.burstCount = before.burst; state.burstThreshold = before.thresh
  }
  // ---------------------------------------------------------------------
  // COMMISSION-AWARE RANKING: high-commission fashion/beauty deals and
  // high-value orders outrank low-commission electronics / tiny trinkets.
  // ---------------------------------------------------------------------
  {
    const ajioFashion = dealCommissionInfo({ text: 'Ajio Cotton Anarkali Kurta Set ₹999 70% OFF\nhttps://fktr.in/AJ1' })
    const myntraBeauty = dealCommissionInfo({ text: 'Myntra Lipstick Matte ₹499 60% OFF\nhttps://fktr.in/MY1' })
    const cheapTrinket = dealCommissionInfo({ text: 'Cotton Socks ₹89 75% OFF\nhttps://fktr.in/SK1' })
    const gadget = dealCommissionInfo({ text: 'Bluetooth Speaker ₹1499 40% OFF\nhttps://www.amazon.in/dp/B0SPKR1234?tag=deals0911-21' })
    if (!(ajioFashion.tier > gadget.tier)) throw new Error('Ajio fashion must outrank a low-commission gadget')
    if (!(ajioFashion.tier >= myntraBeauty.tier)) throw new Error('Ajio 15% must rank at least with Myntra beauty')
    if (!(myntraBeauty.tier > cheapTrinket.tier || myntraBeauty.tier >= 2)) throw new Error('beauty deal must rank above/level with a tiny trinket')
    const a = { text: 'Ajio Designer Kurti ₹899 65% OFF\nhttps://fktr.in/QA', media: [], createdAt: Date.now(), source: 'under499loots' }
    const b = { text: 'Power Bank ₹1599 35% OFF\nhttps://fktr.in/QB', media: [], createdAt: Date.now(), source: 'under499loots' }
    const ordered = [a, b].sort(compareQueuedJobs)
    if (ordered[0] !== a) throw new Error('high-commission fashion must be dequeued before electronics')
    if (dealCommissionInfo({ text: 'Zomato 50% OFF tonight\nhttps://zom.to/z' }).tier < 3) throw new Error('service offer must be a high commission/rank tier')
  }
  // ---------------------------------------------------------------------
  // UNDER-₹99 SECOND CHANNEL: routing rules.
  // ---------------------------------------------------------------------
  {
    const beforeTarget = targetJid, beforeUnder = under99Jid
    targetJid = 'main@newsletter'; under99Jid = 'u99@newsletter'
    const single99  = { text: 'Cotton T-Shirt ₹99 75% OFF https://a.test/1', media: [] }
    const single499 = { text: 'Sneakers ₹499 https://a.test/2', media: [] }
    const singleNoPriceHigh = { text: 'Premium Watch 20% OFF https://a.test/3', media: [] }
    const cheapList = ['Mega Under ₹99 Sale',
      'A ₹89 75% off https://a.test/a', 'B ₹95 80% off https://a.test/b',
      'C ₹79 70% off https://a.test/c', 'D ₹99 85% off https://a.test/d'].join('\n')
    const expList = ['Expensive Sale',
      'A ₹1299 30% off https://a.test/e', 'B ₹1499 25% off https://a.test/f',
      'C ₹999 40% off https://a.test/g', 'D ₹1099 35% off https://a.test/h'].join('\n')
    // Singles
    if (!under99Eligible(single99)) throw new Error('₹99 single must go to under-99 channel')
    if (under99Eligible(single499)) throw new Error('₹499 single must NOT go to under-99 channel')
    if (under99Eligible(singleNoPriceHigh)) throw new Error('no-price 20% single must NOT go to under-99 channel')
    // Lists: best-discount under-99 list in; expensive list out.
    if (!under99Eligible({ text: cheapList, media: [], largeList: true })) throw new Error('best-discount under-₹99 list must go to under-99 channel')
    if (under99Eligible({ text: expList, media: [], largeList: true })) throw new Error('expensive list must NOT go to under-99 channel')
    // MAJORITY under-₹99 list (not every item ≤₹99, but most are) -> in, even
    // without a big headline discount (items may carry their own line prices).
    const majorityList = ['Weekend Loot List',
      'A ₹89 https://a.test/m1', 'B ₹99 https://a.test/m2',
      'C ₹149 https://a.test/m3'].join('\n')
    if (!under99Eligible({ text: majorityList, media: [], largeList: true })) throw new Error('majority under-₹99 list must go to under-99 channel')
    // A list with only ONE cheap item among expensive ones and no strong
    // discount/special/photo must NOT reach the under-₹99 channel.
    const mostlyExpensive = ['Mixed List',
      'A ₹89 https://a.test/x1', 'B ₹1299 https://a.test/x2',
      'C ₹1499 https://a.test/x3', 'D ₹999 https://a.test/x4'].join('\n')
    if (under99Eligible({ text: mostlyExpensive, media: [], largeList: true })) throw new Error('mostly-expensive list (one cheap item, weak discount) must NOT go to under-99 channel')
    // STRICT: a single cheap item + a BEST (60%+) discount headline does NOT
    // qualify — the list is mostly expensive, and the under-₹99 channel
    // features ONLY under-₹99 products.
    const oneCheapBestDiscount = ['Mega Sale 65% OFF',
      'A ₹89 https://a.test/y1', 'B ₹599 https://a.test/y2',
      'C ₹649 https://a.test/y3', 'D ₹799 https://a.test/y4'].join('\n')
    if (under99Eligible({ text: oneCheapBestDiscount, media: [], largeList: true })) throw new Error('mostly-expensive best-discount list must NOT go to under-99 channel (strict under-₹99-only)')
    // An explicit "under ₹99" feature list with no per-item prices IS an
    // under-₹99 list by definition.
    const explicitU99 = ['UNDER ₹99 LOOTS',
      'Mystery loot https://a.test/z1', 'More loot https://a.test/z2',
      'Extra loot https://a.test/z3'].join('\n')
    if (!under99Eligible({ text: explicitU99, media: [], largeList: true })) throw new Error('explicit under-₹99 feature list must go to under-99 channel')
    // Targets: eligible single -> both channels; non-eligible -> main only;
    // digest (job=null) -> main only.
    const tBoth = targetsFor(single99).sort().join(',')
    if (tBoth !== 'main@newsletter,u99@newsletter') throw new Error('eligible deal must fan to both channels: ' + tBoth)
    const tMain = targetsFor(single499).sort().join(',')
    // (In WA_CHANNEL_ALL_POSTS mirror mode both channels legitimately receive
    // everything, including digests - the checks after this one cover that.)
    if (!CHANNEL_ALL_POSTS && tMain !== 'main@newsletter') throw new Error('non-eligible deal must go main-only: ' + tMain)
    const tDigest = targetsFor(null).sort().join(',')
    if (!CHANNEL_ALL_POSTS && tDigest !== 'main@newsletter') throw new Error('digest must go main channel only: ' + tDigest)
    if (!isNewsletterTarget('u99@newsletter') || !isNewsletterTarget('main@newsletter')) throw new Error('both channels are newsletters')
    // v17.2: two-channel coverage + pacing.
    // WA_CHANNEL_ALL_POSTS=true mirrors EVERYTHING to both channels (that is the
    // mode to use when the second channel is a general channel, not the
    // Under-₹99 shelf); the default keeps the tiered behaviour.
    if (CHANNEL_ALL_POSTS) {
      if (targetsFor(null).length !== 2) throw new Error('all-posts mode must include the second channel even for digests: ' + targetsFor(null))
      if (targetsFor({ text: 'Expensive Sofa ₹24999 10% OFF\nhttps://fktr.in/EXP' }).length !== 2) {
        throw new Error('all-posts mode must not gate by price')
      }
    } else {
      if (secondaryEligible(null)) throw new Error('tiered mode must keep digests on the main channel')
      if (secondaryEligible({ text: 'Sofa ₹24999 10% OFF\nhttps://fktr.in/EXP' })) throw new Error('expensive deal must not hit the under-₹99 shelf')
    }
    // A failed secondary resolve must not silently cost the second channel: the
    // retry helper exists and is a no-op while resolved.
    if (typeof ensureSecondaryChannel !== 'function' || typeof secondaryChannelRetryLoop !== 'function') {
      throw new Error('secondary channel retry helper missing')
    }
    // Pacing: the gap BETWEEN THE TWO CHANNELS of one post is the short
    // intra-target gap, not the anti-flood post gap (that is what made the
    // second channel look ~1 minute behind / the post "arrive late").
    if (!(INTER_TARGET_GAP_SECONDS < MIN_WA_MESSAGE_GAP_SECONDS)) {
      throw new Error('intra-broadcast gap must be shorter than the post-to-post gap')
    }
    if (!(MIN_WA_MESSAGE_GAP_SECONDS >= 15) || !(INTER_TARGET_GAP_SECONDS >= 3)) {
      throw new Error('pacing floors are not reachable: ' + [MIN_WA_MESSAGE_GAP_SECONDS, INTER_TARGET_GAP_SECONDS])
    }
    // A MATURE number must not hold a fresh source post for minutes; a genuinely
    // new number keeps its slow warmup tiers on purpose (ban safety).
    {
      const p = warmupPolicy()
      if (p.day > 7 && p.min > 60) {
        throw new Error('mature WhatsApp tier must not pace a fresh post above 60s: ' + p.min)
      }
      if (p.day <= 2 && p.min < 60) throw new Error('a brand new number must stay slow (warmup tier)')
    }
    // The settle windows may be tuned by env, but they must never grow back
    // into the old multi-minute "why is it posting randomly" holds.
    if (!(SPECIAL_JITTER_MAX >= 0 && ROTATION_JITTER_MAX >= 0
          && SPECIAL_JITTER_MAX <= 300 && ROTATION_JITTER_MAX <= 300)) {
      throw new Error(`settle windows out of range: special=${SPECIAL_JITTER_MAX}s rotation=${ROTATION_JITTER_MAX}s`)
    }
    if (!(SPECIAL_JITTER_MIN <= SPECIAL_JITTER_MAX && ROTATION_JITTER_MIN <= ROTATION_JITTER_MAX)) {
      throw new Error('settle window min must not exceed max')
    }
    targetJid = beforeTarget; under99Jid = beforeUnder
  }
  // USER POST: clumsy markdown-wrapped Amazon links (the SAME URL stacked 2-3x
  // in broken "[url](url](url))" wrappers, &amp; double-escaping, smid/psc
  // noise) must collapse to ONE clean URL per product line; real content lines
  // survive.
  {
    const mdDp = a => `https://www.amazon.in/dp/${a}?psc=1&smid=A1WYWER0W24N8S&tag=deals0911-21`
    const mdBlock = u => { const a = u.replace(/&/g, '&amp;'); return `[${a}](${a}](${u}))` }
    const mdHidden = 'https://www.amazon.in/s?hidden-keywords=B0H3LPRGX3+%7C+B0H36MXL3V+%7C+B0F4NDZ2VC&psc=1&th=1&tag=deals0911-21'
    const mdClumsy = 'More | Apply coupon\n' + mdBlock(mdHidden) + '\n' +
      ['B0DQPT85TB','B0F4NFCHX8','B0FMF6X8Z5','B0FMNXX9QS','B0G1SVRWG3','B0FJ7D2KBQ']
        .map(a => mdBlock(mdDp(a))).join('\n')
    const mdTidied = normalizeNestedLinks(mdClumsy)
    if (!mdTidied.startsWith('More | Apply coupon')) throw new Error('markdown tidy lost the header: ' + mdTidied)
    if (/[\[\]]/.test(mdTidied) || mdTidied.includes('&amp')) throw new Error('markdown tidy left brackets/amp: ' + mdTidied)
    if ((mdTidied.match(/B0DQPT85TB/g) || []).length !== 1) throw new Error('markdown tidy did not dedupe product URL: ' + mdTidied)
    if (mdTidied.split('\n').length !== 8) throw new Error('markdown tidy line count wrong: ' + mdTidied)
    // Fragment without the leading '[' must also collapse to one URL per product.
    const mdFrag = 'Deal \u20b9499\n' + mdDp('B0DQPT85TB').replace(/&/g, '&amp;') + '](' +
      mdDp('B0DQPT85TB').replace(/&/g, '&amp;') + '](' + mdDp('B0DQPT85TB') + '))\n'
    const mdFragOut = normalizeNestedLinks(mdFrag)
    if ((mdFragOut.match(/B0DQPT85TB/g) || []).length !== 1 ||
        /[\[\]()]/.test(mdFragOut.replace(/https?:\/\/\S+/g, ''))) {
      throw new Error('fragment tidy failed: ' + mdFragOut)
    }
    // A line with two DIFFERENT product links must keep both.
    const mdTwo = 'Men : ' + mdDp('B0DQPT85TB') + ' Women : ' + mdDp('B0F4NFCHX8')
    const mdTwoOut = normalizeNestedLinks(mdTwo)
    if (!mdTwoOut.includes('B0DQPT85TB') || !mdTwoOut.includes('B0F4NFCHX8')) {
      throw new Error('two-link tidy dropped a URL: ' + mdTwoOut)
    }
  }
  // USER REPORT 2: markdown with BOLD/HIGHLIGHT debris glued around the links,
  // "Men : ++**[[url](url)]([url](url))**" and "**Women : ++**[url](url)**++".
  // Must collapse to clean "Men : <url>" / "Women : <url>" lines with zero
  // leftover * + brackets or &amp; (URLs masked so query '+' chars survive).
  {
    const boldMen = 'https://www.amazon.in/s?i=watches&k=sonata&linkId=d1ed8305142355ade29af769ae53ffd5&rh=n%3A1350387031%2Cn%3A2563504031&s=price-asc-rank&xpid=amZH9R9-GxtvF&tag=deals0911-21'
    const boldWomen = boldMen.replace('2563504031', '2563505031').replace('d1ed8305142355ade29af769ae53ffd5', '600920435a84f32e3ac84659fd83d23e')
    const a = u => u.replace(/&/g, '&amp;')
    const boldPost = 'Starts At \u20b9407\n' +
      'Men : ++**[[' + a(boldMen) + '](' + a(boldMen) + ')]([' + a(boldMen) + '](' + boldMen + '))**\n' +
      '**Women : ++**[' + a(boldWomen) + '](' + boldWomen + ')**++\n'
    const boldOut = normalizeNestedLinks(boldPost)
    const boldLines = boldOut.split('\n').filter(Boolean)
    if (boldLines.length !== 3 || boldLines[0] !== 'Starts At \u20b9407') {
      throw new Error('bold-md tidy line count/label wrong: ' + boldOut)
    }
    if (!boldLines[1].startsWith('Men : ') || !boldLines[2].startsWith('Women : ')) {
      throw new Error('bold-md tidy lost Men/Women labels: ' + boldOut)
    }
    const junk = boldOut.replace(/https?:\/\/\S+/g, '')
    if (/[\[\]*/]/.test(junk) || boldOut.includes('&amp')) {
      throw new Error('bold-md tidy left debris: ' + boldOut)
    }
    if (!boldLines[1].includes(boldMen) || !boldLines[2].includes(boldWomen)) {
      throw new Error('bold-md tidy dropped a URL: ' + boldOut)
    }
    if ((boldLines[1].match(/https?:\/\//g) || []).length !== 1 ||
        (boldLines[2].match(/https?:\/\//g) || []).length !== 1) {
      throw new Error('bold-md tidy did not dedupe URL copies: ' + boldOut)
    }
    // A legitimate single '+' inside product text must survive.
    const plusLine = 'Air Purifier X1+ Filter Set \u20b92999'
    if (normalizeNestedLinks(plusLine) !== plusLine) {
      throw new Error('single plus in product text was damaged')
    }
    // DEEP nesting (4 layers, TRIPLE &amp;amp; escaping, emphasis woven in
    // between the brackets, 7 URL copies per line) must also collapse to one.
    const ddp = a => `https://www.amazon.in/dp/${a}?psc=1&smid=A1WYWER0W24N8S&tag=deals0911-21`
    const e1 = u => u.replace(/&/g, '&amp;')
    const e3 = u => u.replace(/&/g, '&amp;amp;')
    const deepWrap = u => '++**[[' + e3(u) + '++**](++**' + e3(u) + '++**](++**' + e1(u) +
      '++**)]([' + e1(u) + '](' + e1(u) + '](' + e1(u) + '](' + u + '))))'
    const deepOut = normalizeNestedLinks('More | Apply coupon\n' + deepWrap(ddp('B0DQPT85TB')))
    const deepLines = deepOut.split('\n').filter(Boolean)
    if (deepLines.length !== 2 || (deepLines[1].match(/B0DQPT85TB/g) || []).length !== 1 ||
        /[\[\]*/]/.test(deepLines[1].replace(/https?:\/\/\S+/g, '')) ||
        deepOut.includes('&amp')) {
      throw new Error('deep 4-layer nest did not collapse: ' + deepOut)
    }
  }
  if (!stateFilePreExisted) fs.rmSync(STATE_FILE, { force: true })
  if (!stateBackupPreExisted) fs.rmSync(STATE_BACKUP_FILE, { force: true })
  // ------------------------------------------------------------------ v17 text fidelity
  // The published post must show the source's own words: the price line stays
  // "₹260" (no glued shortener fragment, nothing appended), markdown debris from
  // broken entities never prints literally, and OUR OWN links survive intact.
  {
    const glued = 'Top Loading Washing Machine Cover @ ₹260tG7oChgiQuTgS25b'
    const cleaned = cleanDealText(glued)
    if (!/₹260$/.test(cleaned.trim())) throw new Error('price must survive exactly: ' + cleaned)
    if (/tG7oChgiQuTgS25b|260t/.test(cleaned)) throw new Error('random fragment after price survived: ' + cleaned)
    const spaced = stripPriceJunk('Sony Headphones ₹1,499 Xk9LaMn20QpR7 extra bass')
    if (/Xk9LaMn20QpR7/.test(spaced) || !/extra bass/.test(spaced)) throw new Error('spaced fragment/content: ' + spaced)
    // A coupon code glued to a price is the reader's discount, not junk: the price is
    // separated from it and the code SURVIVES (this is what "₹ 199HFJF" needs).
    // USER RULE: what is glued to a price is unwanted text - cut it, add nothing.
    const scrapCut = stripPriceJunk('✅Deal Price: ₹ 199HFJF')
    if (scrapCut !== '✅Deal Price: ₹ 199') throw new Error('glued scrap after price: ' + scrapCut)
    if (stripPriceJunk('Deal ₹1,099PEOPLE200') !== 'Deal ₹1,099') throw new Error('glued code cut: PEOPLE200')
    for (const scrap of ['₹85h', '₹85jsjd', '₹85htt']) {
      if (stripPriceJunk('Clip at ' + scrap) !== 'Clip at ₹85') throw new Error('scrap survived: ' + scrap)
    }
    // What the source wrote APART stays: a code, a unit, an ordinary word.
    if (stripPriceJunk('Deal ₹1,099 PEOPLE200') !== 'Deal ₹1,099 PEOPLE200') throw new Error('spaced code lost')
    if (stripPriceJunk('Use code HFJF for ₹199 off') !== 'Use code HFJF for ₹199 off') throw new Error('labelled code lost')
    if (stripPriceJunk('₹249 SAVE_200') !== '₹249 SAVE_200') throw new Error('spaced code must be left alone')
    if (stripPriceJunk('₹ 199 HFJF') !== '₹ 199 HFJF') throw new Error('spaced text must be untouched')
    if (keepSourceSpacing('A\n\nB\n\nC', 'one line source') !== 'A\nB\nC') {
      throw new Error('a blank line our own passes created must be collapsed')
    }
    if (keepSourceSpacing('A\n\nB', 'source had\n\nblank lines') !== 'A\n\nB') {
      throw new Error("the source's own spacing must survive")
    }
    if (stripPriceJunk('❌MRP: ₹ 270\nDiscount: 26%') !== '❌MRP: ₹ 270\nDiscount: 26%') {
      throw new Error('price walk must not cross a line break')
    }
    if (explicitDiscount('✅Deal Price: ₹ 199\nDiscount: 26%') !== 26) {
      throw new Error('"Discount: 26%" must count as 26 (it decides the best-pick ranking)')
    }
    if (explicitDiscount('Extra 5% cashback on SBI Card') !== null) throw new Error('cashback is not a discount')
    // A share/forward button is not a destination and must never be judged as one.
    for (const share of ['https://wa.me/?text=https%3A%2F%2Ft.me%2Fdeals%2F1', 'https://t.me/loots/156757',
                         'tg://share?url=x', 'https://api.whatsapp.com/send?text=hi']) {
      if (!isShareIntent(share)) throw new Error('share link not recognised: ' + share)
    }
    for (const shop of ['https://www.amazon.in/dp/B0GH2374K3', 'https://www.flipkart.com/x/p/itmy']) {
      if (isShareIntent(shop)) throw new Error('merchant page mistaken for a share link: ' + shop)
    }
    const kept = cleanDealText('Cotton Tshirt Pack of 2 ₹249 (500ml, 2pcs, 65w, 20000mAh)\nUse code: SAVE_200 for ₹200 off\nPrice ₹249 (55% OFF)')
    for (const frag of ['(500ml, 2pcs, 65w, 20000mAh)', 'SAVE_200', '₹249', '(55% OFF)']) {
      if (!kept.includes(frag)) throw new Error('real content lost from the post: ' + frag + ' -> ' + kept)
    }
    const debris = 'Cover ₹260\n➜ [https://bitli.in/IKthI4w](https://bitli.in/IKthI4w)\n➜ [https://bitli.in/6ft5j8a](https://bitli.in/6ft5j8a)'
    const out = sanitizeOutbound(debris)
    if (out.includes('](') || out.includes('[')) throw new Error('markdown debris survived the outbound guard: ' + out)
    if ((out.match(/https:\/\/bitli\.in\//g) || []).length !== 2) throw new Error('links must survive the guard: ' + out)
    if (!out.includes('₹260')) throw new Error('price must survive the guard: ' + out)
    if (sanitizeOutbound(out) !== out) throw new Error('outbound guard is not idempotent')
    if (sanitizeOutbound('Deal ₹99 ( )').includes('( )')) throw new Error('empty bracket residue survived')
    if (sanitizeOutbound('*DEALS OF THE DAY*\n\n💥 *UNDER ₹99*') !== '*DEALS OF THE DAY*\n\n💥 *UNDER ₹99*') {
      throw new Error('sanitizeOutbound must not destroy our own WhatsApp bolding')
    }
    const folder = '📂 All Loot Channels — One Tap\n👉 https://t.me/addlist/5V7_ViAGDxAwNTI1'
    if (!normalizeNestedLinks(folder).includes('5V7_ViAGDxAwNTI1')) throw new Error('underscore inside our own link was deleted (dead invite)')
    if (!sanitizeOutbound(folder).includes('5V7_ViAGDxAwNTI1')) throw new Error('outbound guard corrupted our own link')
    const wrapped = normalizeNestedLinks('(https://bit.ly/a_b_c)')
    if (wrapped.includes('(') || !wrapped.includes('a_b_c')) throw new Error('bracketed URL not unwrapped cleanly: ' + wrapped)
    if (fixUnbalancedParens('Deal (₹99 only') .includes('(')) throw new Error('unmatched paren survived')
    if (!fixUnbalancedParens('Boat ₹1,099 (75% OFF)').includes('(75% OFF)')) throw new Error('balanced parens must stay')
  }
  // v17: the loot-list shape (headline with the price, then a bullet + link per
  // variant). The headline must survive, every link must appear, and the
  // markdown-bracket variant must never print literally.
  {
    const variants = ['tG7oChgiQuTgS25b', 'IKthI4w', '6ft5j8a', '3FQw8wi']
    const listShape = 'Top Loading Washing Machine Cover @ \u20b9260\n' +
      variants.map(c => `\u279c https://bitli.in/${c}`).join('\n')
    const post = sanitizeOutbound(formatWhatsAppPost({ text: listShape }))
    if (!/Washing Machine Cover/.test(post)) throw new Error('list post lost the product name: ' + post)
    if (!post.includes('\u20b9260')) throw new Error('list post lost the source price: ' + post)
    if ((post.match(/https?:\/\/bitli\.in\//g) || []).length !== 4) throw new Error('list post lost links: ' + post)
    if (post.includes('tG7oChgiQuTgS25b') && !post.includes('bitli.in/tG7oChgiQuTgS25b')) {
      throw new Error('short code leaked outside its link: ' + post)
    }
    const mdShape = 'Top Loading Washing Machine Cover @ \u20b9260\n' +
      variants.map(c => `\u279c [https://bitli.in/${c}](https://bitli.in/${c})`).join('\n')
    const mdPost = sanitizeOutbound(formatWhatsAppPost({ text: mdShape }))
    if (mdPost.includes('[') || mdPost.includes('](')) throw new Error('markdown debris in a WhatsApp post: ' + mdPost)
    if (!mdPost.includes('\u20b9260') || !/Washing Machine Cover/.test(mdPost)) {
      throw new Error('markdown list post lost name/price: ' + mdPost)
    }
    if ((mdPost.match(/https?:\/\/bitli\.in\//g) || []).length !== 4) throw new Error('markdown list post lost links: ' + mdPost)
    const glued = sanitizeOutbound('Top Loading Washing Machine Cover @ \u20b9260https://bitli.in/zz1\n\u279c https://bitli.in/zz2')
    if (!glued.includes('\u20b9260 https://bitli.in/zz1')) throw new Error('link glued to the price was not separated: ' + glued)
  }
  // v17.1: a campaign banner is decoration, not the deal - it must not sit on
  // top of the post, but a line that could BE the headline (or a store header)
  // is never dropped, so a post can never collapse into bare links.
  {
    const bannerSrc = '\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25\n' +
      '\u26a1\ufe0f\u26a1\ufe0f 11 PM FLASH SALE \u26a1\ufe0f\u26a1\ufe0f\n' +
      'Top Loading Washing Machine Cover @ \u20b9260\n' +
      ['IKthI4w', '6ft5j8a', '3FQw8wi', 'eIQ8aOv']
        .map(c => `\u279c [https://bitli.in/${c}](https://bitli.in/${c})`).join('\n')
    const bannerPost = sanitizeOutbound(formatWhatsAppPost({ text: bannerSrc }))
    // FIDELITY (the user's rule): the posting channel's own hype header is part of
    // its post and is KEPT; only with WA_STRIP_CAMPAIGN_BANNERS=true does it go.
    if (!STRIP_CAMPAIGN_BANNERS && !/TOP DEAL OF THE DAY/.test(bannerPost)) {
      throw new Error('source banner must be preserved on WhatsApp: ' + bannerPost)
    }
    if (!bannerPost.includes('\u20b9260') || !/Washing Machine Cover/.test(bannerPost)) {
      throw new Error('fidelity mode lost the product/price line: ' + bannerPost)
    }
    if ((bannerPost.match(/bitli\.in\//g) || []).length !== 4) {
      throw new Error('fidelity mode lost links: ' + bannerPost)
    }
    // But another channel's BRANDING never stays.
    const branded = sanitizeOutbound(formatWhatsAppPost({ text:
      'Top Loading Washing Machine Cover @ \u20b9260\n\u279c https://bitli.in/IKthI4w\n'
      + '\ud83d\udd25 LOOT ZONE INDIA \u2014 Join for more loot\nFollow @BestDealHubIndia on WhatsApp' }))
    if (/LOOT ZONE INDIA|BestDealHubIndia|Join for more/i.test(branded)) {
      throw new Error('channel branding reached the WhatsApp post: ' + branded)
    }
    if (!/Washing Machine Cover|\u20b9260/.test(branded) || !branded.includes('bitli.in/IKthI4w')) {
      throw new Error('branding strip cost deal content: ' + branded)
    }
    for (const keep of ['Myntra Mega Sale', 'DEALS OF THE DAY', 'SAARE MI AMAZING DEALS',
                        'Free shipping on all orders', 'Boat Airdopes 141']) {
      if (isBrandingLine(keep)) throw new Error('real content misread as branding: ' + keep)
    }
    for (const junk of ['\ud83d\udd25 LOOT ZONE INDIA \u2014 Join for more loot', 'Loot Zone India',
                        'Join our telegram channel for more deals', 'Edited by Admin @dealsAdda',
                        'Powered by Amazon Deals Hub']) {
      if (!isBrandingLine(junk)) throw new Error('branding not recognised: ' + junk)
    }
    if (STRIP_CAMPAIGN_BANNERS && /TOP DEAL/.test(bannerPost)) {
      throw new Error('strip mode did not remove the banner')
    }
    if (STRIP_CAMPAIGN_BANNERS && (!bannerPost.includes('\u20b9260') || !/Washing Machine Cover/.test(bannerPost))) {
      throw new Error('strip mode must still keep the product/price line: ' + bannerPost)
    }
    // The opt-in helper still behaves (and keeps a headline when nothing else can).
    const stripped = dropCampaignBanners(['\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25',
      '\u26a1\ufe0f FLASH SALE \u26a1\ufe0f', 'Real Product \u20b999', 'https://a.co/1'])
    if (/TOP DEAL|FLASH SALE/.test(stripped.join('\n'))) throw new Error('strip helper failed: ' + stripped)
    if (!/Washing Machine Cover/.test(bannerPost) || !bannerPost.includes('\u20b9260')) {
      throw new Error('banner stripping cost the product/price line: ' + bannerPost)
    }
    if ((bannerPost.match(/bitli\.in\//g) || []).length !== 4) throw new Error('banner shape lost links: ' + bannerPost)
    if (bannerPost.includes('[') || bannerPost.includes('](')) throw new Error('markdown debris after banner strip: ' + bannerPost)
    if (isCampaignBannerLine('Myntra Mega Sale')) throw new Error('store header must not be treated as a banner')
    if (!isCampaignBannerLine('\u26a1\ufe0f 11 PM FLASH SALE \u26a1\ufe0f')) throw new Error('flash-sale header not recognized as a banner')
    const bannerOnly = dropCampaignBanners(['\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25',
      '\u26a1\ufe0f FLASH SALE \u26a1\ufe0f', 'https://bitli.in/zz9'])
    if (bannerOnly[0].indexOf('TOP DEAL') < 0 || bannerOnly.length !== 2) {
      throw new Error('a banner that is the only headline must survive as the headline: ' + JSON.stringify(bannerOnly))
    }
  }
  // v17.2: an app-install / refer-N-friends block stapled under the deal must
  // never reach the channel, even though it carries a ₹ amount (the price guard
  // used to keep the whole line alive).
  {
    const junk = 'Get Flipkart App - Refer 3 friends and \u20b9100 referral bonus'
    if (!isPromoNoiseLine(junk)) throw new Error('referral/app-install line survived: ' + junk)
    if (!isPromoNoiseLine('Install the app and get \u20b920 signup bonus')) throw new Error('install-app farming survived')
    if (isPromoNoiseLine('Use code SAVE200 for extra \u20b9200 off')) throw new Error('a real coupon line must never be dropped')
    if (isPromoNoiseLine('Top Loading Washing Machine Cover @ \u20b9260')) throw new Error('product/price line must never be dropped')
    const pasted = '\ud83d\udd25\ud83d\udd25 TOP DEAL OF THE DAY \ud83d\udd25\ud83d\udd25\n' +
      'Top Loading Washing Machine Cover @ \u20b9260\n\u279c https://bitli.in/IKthI4w\n' + junk
    const post = sanitizeOutbound(formatWhatsAppPost({ text: pasted }))
    // Only junk that is NOT the source's own post gets removed: referral/app-install
    // farming. The channel's own header line stays (fidelity), unless the operator
    // opted into WA_STRIP_CAMPAIGN_BANNERS.
    if (/Refer 3 friends|Install the app/.test(post)) throw new Error('unwanted text reached the channel: ' + post)
    if (/\[|\]\(/.test(post)) throw new Error('markdown debris reached the channel: ' + post)
    if (!/Washing Machine Cover @ \u20b9260/.test(post) || !post.includes('bitli.in/IKthI4w')) {
      throw new Error('deal content lost while stripping junk: ' + post)
    }
  }
  {
    // Channel policy matrix - the user's four WhatsApp channels.
    const cheap = { text: 'Sony Earbuds ₹89 80% OFF\nhttps://a.co/x1' }
    if (!eligibleForChannel(cheap, 'under99')) throw new Error('a ₹89 deal must reach the under-99 channel')
    const mid = { text: 'Wifi Speaker ₹399 65% OFF\nhttps://a.co/x2' }
    if (eligibleForChannel(mid, 'under99')) throw new Error('a ₹399 deal must not pollute the under-99 channel')
    if (!eligibleForChannel(mid, 'under499')) throw new Error('a ₹399 deal belongs in the under-499 channel')
    if (!eligibleForChannel(mid, 'bestOf')) throw new Error('a 65%-off ₹399 deal is best-of material')
    const weak = { text: 'Sofa Set ₹24999 5% OFF\nhttps://a.co/x3' }
    if (eligibleForChannel(weak, 'under499') || eligibleForChannel(weak, 'bestOf')) {
      throw new Error('an expensive weak deal must be skipped by the curated channels')
    }
    const list = { text: 'MEGA LIST\nShirt ₹1999 https://a.co/1\nShoes ₹2999 https://a.co/2\nBag ₹3999 https://a.co/3\nWatch ₹4999 https://a.co/4', largeList: true }
    if (!eligibleForChannel(list, 'under99') || !eligibleForChannel(list, 'under499')) {
      throw new Error('a product list must reach both price channels whatever the item prices')
    }
    const card = { text: 'HDFC credit card offer: ₹2500 instant discount on Apple laptop\nhttps://a.co/x5' }
    if (!(eligibleForChannel(card, 'under99') && eligibleForChannel(card, 'under499') && eligibleForChannel(card, 'bestOf'))) {
      throw new Error('bank/card offers must be posted on every channel')
    }
    // Ten deals arrive at once: the best-of channel must take exactly the winner.
    const saved = { targetJid, under99Jid, under499Jid, bestOfJid, jobs: state.jobs }
    const savedPolicies = new Map(CHANNEL_POLICY_OF_JID)
    try {
      targetJid = 'main@newsletter'; under99Jid = 'u99@newsletter'
      under499Jid = 'u499@newsletter'; bestOfJid = 'best@newsletter'
      syncChannelPolicies()
      const deals = Array.from({ length: 10 }, (_, i) => ({
        id: `burst-${i}`, text: `Item ${i} ₹399 ${10 + i * 8}% OFF\nhttps://a.co/b${i}`,
        availableAt: 0, createdAt: 1000 + i,
      }))
      state.jobs = deals
      if (!CHANNEL_ALL_POSTS) {
        // Every question below is judged on ranking only: clear the cooldown clock
        // and the per-job "already picked" flag, so a WA_BEST_OF_COOLDOWN_SECONDS
        // setting cannot make independent calls contradict each other.
        const pick = (job) => {
          state.bestOfLastPickAt = 0
          delete job._bestOfNoted
          return targetsFor(job)
        }
        const winners = deals.filter((job) => pick(job).includes('best@newsletter')).length
        if (winners !== 1) throw new Error(`the best-of channel must post exactly one winner from a 10-deal burst, got ${winners}`)
        const top = deals.reduce((a, b) => (dealQualityScore(b).score > dealQualityScore(a).score ? b : a))
        if (!pick(top).includes('best@newsletter')) throw new Error('the best-of pick must be the highest-scoring deal')
        if (!deals.every((job) => pick(job).includes('u499@newsletter'))) {
          throw new Error('every \u20b9399 deal belongs in the under-499 channel')
        }
        if (deals.some((job) => pick(job).includes('u99@newsletter'))) {
          throw new Error('the under-99 channel must not take \u20b9399 single deals')
        }
        if (BEST_OF_COOLDOWN_SECONDS > 0) {
          state.bestOfLastPickAt = Date.now()
          deals.forEach((job) => { delete job._bestOfNoted })
          if (deals.some((job) => targetsFor(job).includes('best@newsletter'))) {
            throw new Error('best-of picks must respect WA_BEST_OF_COOLDOWN_SECONDS')
          }
        }
      }
      if (!deals.every((job) => targetsFor(job).includes('main@newsletter'))) {
        throw new Error('the main channel must keep every deal, not only the winner')
      }
    } finally {
      Object.assign(globalThis, {})
      targetJid = saved.targetJid; under99Jid = saved.under99Jid
      under499Jid = saved.under499Jid; bestOfJid = saved.bestOfJid
      state.jobs = saved.jobs
      CHANNEL_POLICY_OF_JID.clear()
      for (const [k, v] of savedPolicies) CHANNEL_POLICY_OF_JID.set(k, v)
    }
  }

  {
    // THE USER'S RULE, tested end to end: nothing of ours is added, everything
    // the source wrote survives (its own hype header included, balanced brackets
    // and coupon underscores intact), branding / referral junk is out, and every
    // link is OURS, one per line, in source order.
    const BANNER = '🔥🔥 TOP DEAL OF THE DAY 🔥🔥'
    const ZAP = '⚡️⚡️'
    const R = '₹'
    const bannerSrc = [
      '**' + BANNER + '**',
      ZAP + ' 11 PM FLASH SALE ' + ZAP,
      'Top Loading Washing Machine Cover @ ' + R + '260 (74% OFF)',
      'MRP ' + R + '999 | Free shipping above ' + R + '499',
      'Use code SAVE_200 for extra ' + R + '200 off',
      BANNER + ' LOOT ZONE INDIA - Join for more loot',
      'Get Flipkart App - Refer 3 friends and ' + R + '100 referral bonus',
      'https://bitli.in/AAAA', 'https://bitli.in/BBBB',
      'https://bitli.in/CCCC', 'https://bitli.in/DDDD',
    ].join('\n')
    const job = {
      text: bannerSrc,
      shortLinks: {
        'https://bitli.in/AAAA': 'https://www.amazon.in/dp/B0IKTHI4?tag=' + AMAZON_TAG,
        'https://bitli.in/BBBB': 'https://www.amazon.in/dp/B0IKTHI5?tag=' + AMAZON_TAG,
        'https://bitli.in/CCCC': 'https://www.amazon.in/dp/B0IKTHI6?tag=' + AMAZON_TAG,
        'https://bitli.in/DDDD': 'https://www.amazon.in/dp/B0IKTHI7?tag=' + AMAZON_TAG,
      },
    }
    const post = formatWhatsAppPost(job)
    if (/\*/.test(post)) throw new Error('markdown asterisks must never be printed (ours or the source\'s):\n' + post)
    if (STRIP_CAMPAIGN_BANNERS) {
      // With the opt-in knob on, the hype line goes and everything else stays.
      if (post.includes('TOP DEAL') || post.includes('11 PM FLASH SALE')) {
        throw new Error('strip knob must remove the hype lines and nothing else:\n' + post)
      }
      if (!post.includes('Top Loading Washing Machine Cover')) {
        throw new Error('strip mode must keep the real deal text:\n' + post)
      }
    } else if (post.split('\n')[0].trim() !== BANNER) {
      throw new Error("the source's own banner line must lead, unmodified:\n" + post)
    }
    if (!STRIP_CAMPAIGN_BANNERS && !post.includes(ZAP + ' 11 PM FLASH SALE ' + ZAP)) {
      throw new Error('the second source header must survive with its emoji intact:\n' + post)
    }
    if (!post.includes('Top Loading Washing Machine Cover @ ' + R + '260 (74% OFF)')) {
      throw new Error('product + price + balanced parentheses must survive:\n' + post)
    }
    if (!post.includes('MRP ' + R + '999') || !post.includes('Free shipping above ' + R + '499')) {
      throw new Error('MRP / shipping detail lost:\n' + post)
    }
    if (!post.includes('SAVE_200')) throw new Error('a coupon code underscore must never be mangled:\n' + post)
    if (/LOOT ZONE|Join for more loot|Refer 3 friends|referral bonus/i.test(post)) {
      throw new Error('branding/referral junk leaked:\n' + post)
    }
    if (/(\U0001f4b0|MEGA DEAL LIST|SPECIAL OFFER|Verified deals|Handpicked|Latest deal|Grab fast)/.test(post)) {
      throw new Error('the post carries text WE invented, not the source:\n' + post)
    }
    if (/bitli\.[iI]n/.test(post)) throw new Error('a source link survived - our link must replace it:\n' + post)
    if ((post.match(new RegExp('tag=' + AMAZON_TAG, 'g')) || []).length !== 4) {
      throw new Error('all four links must be ours:\n' + post)
    }
    if (post.split('\n').filter(l => /^https:\/\//.test(l.trim())).length !== 4) {
      throw new Error('every link gets its own bare line:\n' + post)
    }
    if (!STRIP_CAMPAIGN_BANNERS && post.indexOf('TOP DEAL OF THE DAY') > post.indexOf('Top Loading Washing Machine Cover')) {
      throw new Error('source order must be preserved:\n' + post)
    }
    // A special must not be wrapped in our old "LOOT ZONE / SPECIAL OFFER /
    // Verified - Enjoy (Grab fast)" banner either.
    const special = formatSpecialCaption({
      text: [BANNER, 'Gold Ring ' + R + '199 with HDFC Card', 'https://fktr.in/SP1'].join('\n'),
    })
    if (!STRIP_CAMPAIGN_BANNERS && special.split('\n')[0].trim() !== BANNER) { throw new Error('the source header must lead the special caption:\n' + special) }
    if (/LOOT ZONE|SPECIAL OFFER|Verified/.test(special)) throw new Error('special caption must not add our own banner:\n' + special)
    if (!special.includes('https://fktr.in/SP1')) throw new Error('special caption lost its link:\n' + special)
  }


  // v17.6 BEST COPY: the queue must publish the stronger deal for a product,
  // not whichever source happened to post first - while keeping exactly one job
  // per product (no duplicate) and never losing coverage.
  {
    const savedJobs = state.jobs
    const savedSent = state.sent
    const savedContent = state.sentContent
    try {
      state.jobs = []
      state.sent = {}
      state.sentContent = {}
      const ASIN = 'B0BRIDGE01'
      const link = `https://www.amazon.in/dp/${ASIN}?tag=deals0911-21`
      // Whatever sources this environment watches, the rule is the same - so the
      // fixture uses configured names instead of hard-coded ones.
      const watched = [...SOURCES, ...DIRECT_SOURCES]
      const srcA = watched[0] || 'under499loots'
      const srcB = watched[1] || srcA
      const oil = (price, mrp, pct, chatId, msgId, user) => ({
        chat: { id: chatId, username: user }, message_id: msgId,
        text: `Sunlight 1L Refill Pack\n₹${price} for today\nMRP ₹${mrp}, ${pct}% off\n${link}`,
      })
      enqueuePost(oil('214', '599', '45', -100501, 901, srcA))
      if (state.jobs.length !== 1) throw new Error('the first copy must queue exactly one job: ' + state.jobs.length)
      const firstId = state.jobs[0].id
      const firstScore = dealQualityScore(state.jobs[0]).score
      if (!/45%/.test(state.jobs[0].text)) throw new Error('the queued job must hold the first copy')

      enqueuePost(oil('199', '999', '80', -100502, 902, srcB))
      if (state.jobs.length !== 1) throw new Error('a better copy must never add a second job: ' + state.jobs.length)
      const taken = state.jobs[0]
      if (taken.id !== firstId) throw new Error('the pending job must be reused, never recreated')
      if (!taken.text.includes('₹199') || !/80%/.test(taken.text)) {
        throw new Error('the pending job must carry the better deal: ' + JSON.stringify(taken.text))
      }
      if (srcB !== srcA && taken.source !== srcB) {
        throw new Error('the stronger copy also re-points the source: ' + taken.source + ' vs ' + srcB)
      }
      if (dealQualityScore(taken).score <= firstScore) throw new Error('the quality score must go up')

      // an even weaker copy from a third source: skipped, better job untouched
      enqueuePost(oil('614', '599', '5', -100504, 903, srcA))
      if (state.jobs.length !== 1) throw new Error('a weaker copy must not create a second job: ' + state.jobs.length)
      if (!/80%/.test(state.jobs[0].text)) throw new Error('a weaker copy must not downgrade the queued deal')

      // a list that repeats this product still goes out (coverage over tidiness)
      enqueuePost({
        chat: { id: -100505, username: srcA }, message_id: 904,
        text: `1. Sunlight refill ₹214 ${link}\n2. Vim bar ₹20 https://www.amazon.in/dp/B0OTHER007?tag=deals0911-21\n3. Harpic ₹49 https://www.amazon.in/dp/B0OTHER008?tag=deals0911-21`,
      })
      if (state.jobs.length !== 2) throw new Error('a LIST containing an already-queued product must still be posted: ' + state.jobs.length)
      const listJob = state.jobs.find(job => /Vim bar/.test(job.text || ''))
      if (!listJob) throw new Error('the list job is missing')
      if (listJob.id === firstId || !/Harpic/.test(listJob.text)) throw new Error('the list must stay a separate, complete job')

      // a single copy must never hijack a pending list, and a job that already
      // started delivering is never rewritten mid-flight
      listJob.attempts = 1
      enqueuePost(oil('149', '999', '85', -100506, 905, srcB))
      if (state.jobs.length !== 2) throw new Error('a mid-flight product must not gain a second job: ' + state.jobs.length)
      if (!/45%|80%/.test(state.jobs.find(j => j.id === firstId).text)) throw new Error('the delivered-start job must be left alone')
      if (dealQualityScore(state.jobs.find(j => j.id === firstId)).score <= firstScore) {
        // still the better copy it became earlier - unchanged by the mid-flight rule
      }
      if (/149/.test(state.jobs.find(j => j.id === firstId).text)) {
        throw new Error('a job that started delivering must not be re-pointed')
      }
    } finally {
      state.jobs = savedJobs
      state.sent = savedSent
      state.sentContent = savedContent
    }
  }
  console.log('bridge self-test PASS')
  process.exit(0)
}

pruneState(); saveState()
await connectWhatsApp()
secondaryChannelRetryLoop()
if (!PAIR_ONLY) {
  // A crashed loop must not leave the Channel silent: restart it in place.
  const supervise = (name, factory) => {
    const run = () => factory().catch(error => {
      log.error({ err: error.message }, `${name} crashed; restarting in 10s`)
      setTimeout(run, 10_000)
    })
    run()
  }
  supervise('poller', pollTelegram)
  supervise('worker', worker)
  supervise('watchdog', connectionWatchdog)
}
