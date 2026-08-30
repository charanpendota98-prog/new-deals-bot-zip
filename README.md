# charanaffi — BestGAA deal pipeline (Telegram affiliate bot + WhatsApp Channel bridge)

Source repo for the BestGAA system: a **Python Telegram affiliate deal bot**
(`bestgaa/`) and a **Node.js Telegram → WhatsApp Channel bridge**
(`tg-wa-bridge/`), plus the server ops tooling that deploys both (`ops/`).

Both services run as **separate systemd units** on the Oracle host
(`129.159.229.134`, user `ubuntu`) so a WhatsApp logout/ban never stops
Telegram monitoring, conversion, or posting.

```
Telegram source channels
        │
        ▼
bestgaa.service (Python, Telethon)          ← affiliate conversion (EarnKaro),
        │                                     dedup, SQLite queue, posts to
        │                                     owned Telegram targets
        ▼
owned Telegram targets (Under99Deals11, under499loots, LootZoneIndia11,
                        SecretLootIndia1, PowerLoots1, Premiumlootsdeals, ...)
        │
        ▼
tg-wa-bridge.service (Node, Baileys 7.0.0-rc14)   ← own BotFather listener,
        │                                            durable queue, pacing/warm-up,
        ▼                                            strict source-only formatting
WhatsApp Channel (unofficial Baileys client — NOT the Meta Business API)
```

## Repository layout

| Path | What it is |
|---|---|
| `bestgaa/` | Telegram affiliate bot v15 (`main_bot_new.py`), deploy script, legacy-`.env` migrator, systemd unit |
| `tg-wa-bridge/` | Telegram → WhatsApp Channel bridge (`bridge.js`), installer, number-switch script, systemd unit |
| `ops/` | `apply_dual_hotfix.sh` (one-shot server deploy of both services), `install_bestgaa.sh` (first-time bot installer), `repack_bundles.sh` (rebuild deploy zips from source), routing + media-fix notes |
| `archive/` | Original uploaded hotfix zip, kept for provenance |

## Quick checks (no credentials needed)

```bash
# Python bot — syntax + import smoke test
python3 -m py_compile bestgaa/main_bot_new.py bestgaa/migrate_legacy_env.py

# Bridge — deps + built-in contract tests (expects: "bridge self-test PASS").
# Env values are arbitrary — the self-test makes no network calls:
cd tg-wa-bridge && npm install \
  && TELEGRAM_BOT_TOKEN=x WA_PHONE=919876543210 WA_CHANNEL=x@newsletter node bridge.js --self-test
```

The Python module requires `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`EARNKARO_API_KEY` and `AMAZON_TAG` at import time — it exits with
`Missing required environment variable` until a real `.env` exists. That is
expected on a dev machine; see deployment below.

## Deploying to the server

The dual-hotfix deployer expects the two inner bundles next to itself and
applies both services in one shot (extract → migrate `.env` → compile →
install deps → restart → verify hashes → prove config → roll back on failure):

```bash
cd ops
./repack_bundles.sh          # rebuild bestgaa_final_bundle.zip + tg_wa_bridge_bundle.zip
./apply_dual_hotfix.sh       # run on the Oracle server (needs sudo + systemctl)
```

Individual deploys:

- **Bot, first-time install (fresh machine):** `bash ops/install_bestgaa.sh` (run from the repo layout — it finds the sources in `../bestgaa`). Creates the app dir, checks python3/pip, writes `.env` (keeps an existing complete one, or migrates a legacy bot passed as argument 1, or prompts interactively), does the one-time interactive Telegram login (phone + code) so the session file exists, then installs `bestgaa.service` and verifies live-file == shipped-file hashes + the startup marker. Refuses to clobber an existing install.
- **Bot only (update):** upload `bestgaa/` contents to `~/bestgaa-bot/bestgaa-bot/`, then `./deploy_bestgaa.sh` (auto-extracts creds from the old working bot, backs up, compiles, restarts `bestgaa.service`, rolls back on failure).
- **Bridge only:** upload `tg-wa-bridge/` contents to `~/tg-wa-bridge/`, then `./install_bridge.sh` (prompts locally for BotFather token, WhatsApp number, Channel invite, and optional WhatsApp groups for fan-out; installs Node 20 + Tesseract if missing; pairs; starts `tg-wa-bridge.service`).
- **New WhatsApp number, same Channel:** `./switch_whatsapp_number.sh` (stops only the bridge, backs up auth, preserves queue + state).
- **First-time `.env` from legacy hardcoded creds:** `python3 migrate_legacy_env.py /path/to/old_main_bot.py`.

## Key behaviour (see `ops/` notes for full detail)

- **Routing** (`ops/LATEST_ROUTING_NOTES.txt`): per-target source allowlists, dedicated LootzoneTricks sources, Premium target with ≥85% OFF / ≤₹99 / ₹100–499 ≥70% filter, IST posting windows.
- **WhatsApp media fix** (`ops/WHATSAPP_MEDIA_FIX_NOTES.txt`): works around the Baileys 7.0.0-rc14 newsletter-media bug (issue #2199) with a custom `/newsletter/newsletter-*` upload path + `server_thumb_gen=1`, ack watching so "sent but invisible" can't happen silently, text-only fallback after 3 failed media attempts. Kill switch: `NEWSLETTER_MEDIA_FIX=false`.
- **Queue liveness:** mature warm-up tier pinned by default (`WA_WARMUP_DONE=true`), plausibility ceiling on `nextAllowedAt`, per-step timeouts, connection watchdog, heartbeat logs.
- **Source resilience (Telegram ingest never silently loses deals):** startup
  backfill per source (`BACKFILL_HOURS`=6 / `BACKFILL_LIMIT`=50 default) + a
  **rescan dead-man's switch**: every `SOURCE_RESCAN_SECONDS` (default 600s)
  the bot re-scans each LIVE source's recent messages
  (`SOURCE_RESCAN_LIMIT`=20) and enqueues anything new — the queue's
  `UNIQUE(chat_id,msg_id)` makes repeats free, so even a silently dead
  Telethon event stream recovers deals within one cycle instead of losing
  them until the next restart. `INGEST SILENCE` is logged after 30 min
  without a source event, and ingest failures log `INGEST FAIL` instead of
  dying quietly inside the event handler.
- **Dispatch order (v569b7415):** under499loots primary feed → media → ≤₹99/₹199/₹499 → multi-link lists → highest discount → card/bank offers boosted → categories → **newest-first** (a brand-new deal never waits behind hours-old inventory; old jobs age out via `MAX_JOB_AGE_HOURS`).
- **Structured WhatsApp posts (source-faithful, nothing missed):** every
  WhatsApp post leads with the bold deal **name** (extracted from the source
  post), then price/discount badges (💰 ₹price | 🔥 N% OFF), then the source
  content — single deals get the link on its own line at the bottom;
  **multi-link lists keep each link UNDER its product label in source order**
  (no more pooled "mystery links" — every product goes with its own link).
  No source line is ever dropped (the old trailing-words trim that could eat
  the line after a link is fixed) and each link appears exactly once, in its
  display (possibly shortened) form.
- **Best-deal gate (verify before sending):** before ANY WhatsApp send the post
  is verified as a best deal — it needs a readable deal name AND a real signal
  (≥`WA_BEST_MIN_DISCOUNT`% off, price ≤ `WA_BEST_MAX_PRICE`, a special
  card/bank/80%+/service offer, a 4+ link mega list, or product media). Posts
  that fail are **skipped, never sent**, with the reason logged
  (`best-deal gate: ...`). Toggles: `WA_BEST_GATE`, `WA_BEST_MIN_DISCOUNT`,
  `WA_BEST_MAX_PRICE`, `WA_BEST_MIN_NAME_CHARS`.
- **Zero duplicates, four layers, every send path:** (1) intake — an exact
  deal key or campaign-content fingerprint that was already posted is never
  even queued; (2) product dedup window (below); (3) a product already waiting
  in the queue from another source is not queued twice; (4) every send path
  (strict, special, mega-list, rotation) re-checks the exact key AND the
  content fingerprint immediately before sending. The same campaign posted via
  a different shortlink is still recognized as a duplicate.
- **Night quiet window 02:00–06:00 IST (both sides), stale loot never posted:**
  the Telegram bot (`POST_QUIET_START`/`POST_QUIET_END`) and the WhatsApp
  bridge (`QUIET_START`/`QUIET_END`, `HYBRID_QUIET=false`) both pause
  **posting** between 02:00 and 06:00 IST; ingest/rescan keep running. USER
  RULE for deals that ARRIVE inside the window: they are **dead by 06:00**
  (loot prices do not survive four hours) and are skipped with a logged
  reason (`NIGHT SKIP` / `night-born deal expired`), **never posted late** —
  ONLY multi-product **lists** earn the morning flush. Day deals born before
  02:00 keep their normal freshness budget. Set both clock values equal to
  disable the window.
- **No random junk beside prices (both sides):** a random mixed letter+digit
  token glued to or spaced after a price (`₹122oya`, `₹185 VN7z`) is source
  corruption and is stripped before posting, while real quantities/units
  (`2pcs`, `500ml`, `65w`, `750W`) always survive — covered by unit tests.
- **Smart random pacing:** the bridge already posts with randomized gaps,
  hourly break patterns and jitter; the bot now also spaces multi-target
  fan-out with a human-like 3–9s random delay between targets (never before
  the first target, so a hot deal is not delayed).
- **Never-stall pipeline:** bot workers are never-die (a claim or job error is
  logged and the loop continues — a dead worker can no longer silently stop
  all posting), and jobs orphaned in `processing` (kill -9, OOM, watchdog
  timeout) are reclaimed back to `pending` every rescan cycle
  (`RECLAIMED` log line). The bridge supervises its poller/worker/watchdog
  loops (auto-restart in 10s) and both services run under systemd
  `Restart=always`.
- **No product repeats (≥3h, default 10h):** the SAME product — by ASIN or
  product slug, not just the exact URL/text — is never posted again inside the
  `WA_PRODUCT_DEDUP_HOURS` window (default 10h = the bot's own product dedup).
  Enforced at intake (never queued) and again at the pre-send gate; a job with
  a repeated product is dropped with the reason logged.
- **Short links on WhatsApp (lists included):** after a link passes provenance
  + health checks, any verified link longer than `WA_SHORTEN_MIN_LEN` chars —
  in single deals AND mega lists — is swapped for a short link at display
  time: Bitly when `WA_BITLY_TOKENS` is set (comma separated), else the
  tokenless is.gd fallback (same one the bot uses). Already-short and service
  links are never touched; max 20 shortenings per post protects the quota; a
  failed shortening keeps the verified long link (a deal is never lost over
  cosmetics). Short links are cached in state so the shortener is called at
  most once per URL.
- **Direct source safety net (Telegram missed it → WhatsApp still gets it,
  smartly):** besides our own channels, the bridge also watches the bot's raw
  deal sources directly (`TG_DIRECT_SOURCES`; unset = built-in list of the
  bot's public sources, empty = disabled — the bridge's BotFather bot must be
  an **admin** in a source channel to receive its posts). A deal the Telegram
  bot skipped/missed is picked up from the source and posted to WhatsApp with
  the same best-deal gate and dedup: the product dedup runs on the **resolved**
  merchant page, so anything already posted via our own channels (or already
  waiting in the queue) is never double-posted. Our generated affiliate links
  keep full provenance verification; raw source shorteners (amzn.to, fkrt.co,
  linkredirect.in `?dl=`…) are resolved to the merchant page and posted
  unconverted — raw **Amazon product pages get our `tag=` appended**, so they
  stay monetized; a foreign Amazon tag is always blocked.
- **Our-tag Amazon links skip the `link_cache` provenance check (both sides):**
  an Amazon URL carrying our Associates `tag=` is self-proving — that tag can
  only have been added by us (the bot's direct-Associates path or the bridge
  tagging a raw source page), and the link still monetises to this account even
  if someone re-posts it. The SQLite `link_cache` only stores links produced by
  a conversion-API call, so our direct Amazon links legitimately may not be in
  it (bridge-tagged pages never went through the bot; a fresh/pruned/deploy-
  cleared cache loses even the bot's own direct rows). Gatekeeping them on a DB
  row only produced false "Provenance mismatch"/"provenance recheck" drops of
  good deals, so they are exempt in both the bridge (`urlsForProvenance`) and
  the bot (`verify_generated_text`); every other link keeps full verification.
- **Channel + group fan-out:** every verified post goes to the WhatsApp
  Channel **and** each group in `WA_GROUPS` (group JIDs / phone numbers /
  invite codes **or full invite links** `https://chat.whatsapp.com/CODE`,
  comma separated — paste the copied group invite URL directly). The Channel
  always uses the ack-verified newsletter path; invite codes/links are resolved
  to JIDs via Baileys at connect (not joined); a failing group is logged and
  never blocks the other targets; per-target send marks prevent duplicates on
  retry.
- **Service & lifestyle offers (Zomato/Swiggy/Zepto/movies/cards/pizza):**
  these pass through the bot **unconverted** (they are not
  EarnKaro-monetizable — a Zomato link in a mixed post no longer poisons the
  EarnKaro conversion of the store links), are fanned out to every owned
  channel like bank/card offers (bot priority 4), and the bridge treats them
  as special offers (best tier). Their links skip the `link_cache` provenance
  check but keep the broken-link health check. Domain lists:
  `SERVICE_OFFER_DOMAINS` (bot) / `WA_SERVICE_DOMAINS` (bridge).
- **Telegram queue priority (new):** mega 4+ link lists (P5) → card offer / ≥80% off (P4) → ≥75% off (P3) → photo/video (P2) → ordinary (P1), stored in a new `queue.priority` column.
- **Dangling link cleanup:** a bare `🔗`/bullet line with no URL after it is removed in both bot and bridge; real URLs are never touched.
- **Orphan-token fix (bot):** broken URL fragments (`h`, `ht`, `htt`, `uy`,
  `H` uppercase too) that sit on their own line **next to a real link** used to
  survive the adjacency exception in `strict_orphan_token_cleanup` and leak
  into final Telegram posts. They are now matched against
  `ORPHAN_URL_FRAGMENTS` before that exception; genuine labels near links
  (`Blue`, `XL`, `408`) are still preserved.
- **24/7 posting with a night off-window (current policy):** WhatsApp posts
  around the clock; only **02:00–06:00 IST is fully OFF** (`QUIET_START=02:00`,
  `QUIET_END=06:00`, `HYBRID_QUIET=false` — nothing is sent in that window).
  The 700/day mature-tier ceiling is lifted via `WA_DAY_CAP=2000` (safety net
  against runaway sends, far above real deal volume). Queue backlog drains
  newest-first during active hours; jobs older than `MAX_JOB_AGE_HOURS` are
  dropped rather than posted stale.
- **Night-queue trust policy (USER RULE):** deals **born during the
  02:00–06:00 off-window are dropped, never flushed at 06:00** — night loot
  is dead by morning and posting it costs subscriber trust. ONLY mega
  multi-product LISTS survive to the morning slot. Ordinary day deals older
  than `WA_ORDINARY_MAX_AGE_MINUTES` (150) are dropped for the same reason.
  Dropping is visible in logs as `night-born deal expired by 06:00 (lists
  only)` / `expired ordinary deals dropped (trust policy)`.
- **Service independence:** `bestgaa.service` (Telegram) and
  `tg-wa-bridge.service` (WhatsApp) are separate units; a WhatsApp
  pause/logout/ban never stops Telegram posting, and vice versa.
- **One-file night deploy:** `ops/bestgaa_nightfix.zip` (built from tracked
  source) carries `apply_nightfix.sh` + the bridge bundle — download, scp to
  the server, unzip, run. Telegram bot is never touched by it.
- **Link safety:** every outgoing URL must exist as an authenticated generated `affiliate_url` in BestGAA's SQLite `link_cache`; foreign/shortener links are blocked.

## Current deployed version (server-verified)

Matches what ran on the Oracle server after the 2026-08-23 14:21 UTC deploy:

| Artifact | SHA-256 |
|---|---|
| Hotfix zip (`archive/bestgaa_whatsapp_hotfix.zip`) | `569b7415c375de0fddc2a3d27653f11e3dd764ce4c7cb922a373de5936150128` |
| `bestgaa/main_bot_new.py` (= server `main_bot.py`) | `087d227516e4e9392a4efce8ce7da09f470428a56a0088adf804029c1b0294f6` |
| `tg-wa-bridge/bridge.js` (= server `bridge.js`) | `3faf9856dacd84e3f57347c7699ecd93c767d71d2b936fa11bcb4506ac2c5407` |

> Note: the hash list at the bottom of `ops/WHATSAPP_MEDIA_FIX_NOTES.txt`
> (`51c3791b…` / `5d8e1f50…` / `28656d74…`) predates this build and is stale.

## Security

Never commit or paste `.env`, `auth/`, BotFather tokens, or pairing codes/QRs.
`.gitignore` already excludes them. Back up `.env` and `auth/` off-server.

> Reality check (from the shipped docs): the WhatsApp side is an unofficial
> Baileys client, not Meta's official Business API. It is not unlimited,
> ban-proof, or guaranteed to keep working. Use only a dedicated number and a
> Channel you control; no unsolicited private messaging.
