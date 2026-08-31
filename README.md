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

# Behaviour tests (no network; expects all green):
python3 test_render_job.py       # 137 checks: routing, formatting, conversion
python3 test_rescan.py            # ingest dead-man's switch + idempotency
python3 test_pipeline_fixes.py   # 49 checks: immediacy, zero duplicates, post quality
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
  **rescan dead-man's switch**: every `SOURCE_RESCAN_SECONDS` (default **120s**,
  was 600s) the bot re-scans each LIVE source's recent messages
  (`SOURCE_RESCAN_LIMIT`=40, was 20), **concurrently** across
  `SOURCE_RESCAN_CONCURRENCY`=4 sources, and enqueues anything new — the queue's
  `UNIQUE(chat_key,msg_id)` makes repeats free, so even a silently dead Telethon
  event stream recovers deals within one short cycle instead of losing them until
  the next restart. Every id convention is now matched (raw entity id, Telethon's
  `-100…` marked id, legacy `-chat` id), so a source can no longer be "live" in
  the map yet never receive its posts. `INGEST SILENCE` is logged after 30 min
  without a source event, ingest failures log `INGEST FAIL`, and a duplicate
  refused at intake logs `INGEST DUP`.
- **Immediate dispatch (v16 — `ops/IMMEDIACY_DEDUP_FIX_NOTES.txt`):** the queue
  is *woken* the moment a deal is inserted (`QUEUE_WAKE`, no more 1s blind poll),
  every URL of a post is resolved/converted/health-checked **concurrently**
  (a 4-link list renders in ~0.5s instead of 4 serial 18–37s round trips), link
  health verdicts are cached for `LINK_HEALTH_CACHE_SECONDS` so the same URL is
  probed once per job instead of three times, the pre-send health pass is
  time-boxed by `PRESEND_CHECK_BUDGET_SECONDS` (a hanging merchant site can no
  longer stall the queue), oversized/slow source media is skipped
  (`MAX_MEDIA_MB`, `MEDIA_DOWNLOAD_TIMEOUT_SECONDS`) instead of freezing a
  worker, and job retries back off to `JOB_RETRY_MAX_SECONDS` (20s) instead of
  5 minutes.
- **Dispatch order:** priority tiers (mega 4+ link lists → card offer/≥80% →
  ≥75% → photo/video → ordinary, plus the `PRIORITY_SOURCES` boost) then
  **newest-first**, which is now actually implemented (it was documented but the
  SQL still said `created_at ASC`, so a fresh deal waited behind every hours-old
  row in the backlog). Over-age pending work is dropped, not posted late, by
  `MAX_JOB_AGE_HOURS` (default 6h, logged as `STALE DROP`).
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
- **Zero duplicates, five layers, every send path:** (1) intake — a source
  message is queued at most once because the queue key is the *canonical*
  `(raw chat id, msg_id)` pair backed by a real `UNIQUE` index
  (`ux_queue_chat_key`; a DB that already contains double rows for one message
  is collapsed once at startup, `DEDUP MIGRATION`), and a campaign whose
  content fingerprint was already posted is never even inserted
  (`INTAKE DEDUP`); (2) product dedup window (below); (3) a product already
  waiting in the queue from another source is not queued twice; (4) every send
  path (strict, special, mega-list, rotation) re-checks the exact key AND the
  content fingerprint immediately before sending; (5) a retry resumes with the
  **stored rendered bytes** (`has_partial_delivery`), so a re-cut of the chunk
  boundaries can never post a line twice. The same campaign posted via a
  different shortlink is still recognized as a duplicate.
- **Claims never expire while a job is alive:** `reserve()` and `maintenance()`
  used to delete any `deal_claims` row older than 20 min / 1 h, including rows
  held by a *pending* job (a Premium-deferred or backlogged job is exactly
  that). The lock vanished and the identical deal from a second source got
  posted again. Both now only purge claims whose job is finished.
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
- **No random junk, no promo, no residue (both sides):** a random mixed
  letter+digit token glued to or spaced after a price (`₹122oya`, `₹185 VN7z`)
  is source corruption and is stripped, while real quantities/units
  (`2pcs`, `500ml`, `65w`, `750W`) always survive. A line whose ONLY links are
  channel/social links (`Join our WhatsApp Channel: https://whatsapp.com/…`,
  `Visit https://t.me/someotherchannel`) is dropped as a WHOLE line instead of
  being half-stripped — that leak used to publish junk like
  `.com/channel/0029` or `ps://broken`. `strip_url_residue()` sweeps any
  leftover protocol/domain fragment (real links are masked first, so a
  generated affiliate URL can never be damaged), referral/app-install spam
  lines are removed even when they mention ₹, and a CTA label whose link
  collapsed (`Link 👉` with nothing after it) is deleted
  (`remove_dangling_cta_lines`). Covered by `test_pipeline_fixes.py`.
- **v17 — the price line is exactly what the source wrote:** the junk-token
  patterns were length-capped (12/14 chars), so a real-world masked-shortener
  fragment as long as `₹260tG7oChgiQuTgS25b` survived into published posts. All
  three cleanup passes now share one helper pair — `strip_price_junk` (mixed
  letter+digit tails up to 64 chars) and `strip_link_fragment_tokens` (a stand-alone
  12+ char run that mixes lower+upper+digits, skipped on any line mentioning
  code/coupon/voucher/referral so real coupon codes survive).
- **v17 — nothing unclean can leave the bot:** `sanitize_outbound_text` runs as a
  LAST GATE inside `deliver` (and at render/persist time, idempotently) on every
  target: it collapses `[url](url)` markdown debris left by broken entities and
  the link-substitution step, removes empty `()` pairs and stray brackets, and
  re-runs the junk sweeps. It never rewrites a verified link (lines holding a URL
  are left byte-identical), never strips `*bold*` (that is our own WhatsApp
  formatting), and never deletes a price line. `_drop_emphasis_part` also stopped
  eating `_` inside URLs, which had silently corrupted our own folder invite.
- **v17 — no post is silently swallowed:** the campaign fingerprint now includes
  every price/discount and ignores a generic banner line, so products that share
  a repeated header are no longer mistaken for duplicates of the first one; a
  store link the affiliate network cannot monetize (or a link whose conversion
  still fails on the FINAL attempt) is published as a clean untagged merchant
  link (`PASSTHROUGH`/`DEGRADED POST`) instead of burning the retry budget and
  being dropped; one dead destination removes only that link; and a source post
  that was **edited** before any target received it is re-queued (`EDIT REVIVE`)
  instead of staying lost.
- **Source link → OUR link, exactly once:** after `render_job`, every URL in
  the post must be one we generated (affiliate/tagged link, our own folder /
  channel links, or a pass-through service offer). A leftover source/foreign
  link used to throw the whole deal away with `foreign URL survived`; it is now
  **repaired out** (`LINK REPAIR`) so the post still goes out with our link —
  and the deal is only skipped when nothing verified remains.
- **Smart random pacing:** the bridge already posts with randomized gaps,
  hourly break patterns and jitter; the bot spaces multi-target fan-out with a
  short random gap (`TARGET_FANOUT_GAP_MIN`/`TARGET_FANOUT_GAP_MAX`, default
  0.4–1.2s; the code never actually had the 3–9s earlier docs claimed) and
  never before the first target, so a hot deal is not delayed.
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
- **One-file night deploy:** `ops/bestgaa_nightfix.zip` carries
  `apply_nightfix.sh` + the bridge bundle — download, scp to the server, unzip,
  run. Telegram bot is never touched by it. **This bundle predates the v16
  immediacy/dedup fixes — do not use it to deploy them.** Use
  `ops/repack_bundles.sh` + `ops/apply_dual_hotfix.sh` (bot + bridge) or
  `bestgaa/deploy_bestgaa.sh` (bot only) so both services come from tracked
  source.
- **Link safety:** every outgoing URL must exist as an authenticated generated `affiliate_url` in BestGAA's SQLite `link_cache`; foreign/shortener links are blocked.

## Posting-immediacy / dedup knobs (`.env`, all optional, all clamped)

Defaults are the tuned values; a typo in `.env` cannot make the bot slow again
because every number is clamped into a safe range.

| Knob | Default | Meaning |
|---|---|---|
| `QUEUE_WORKERS` | 8 | concurrent render/deliver workers |
| `PRICE_DEDUP_SECONDS` / `PRICE_DEDUP_IGNORES_IDENTITY` | server `.env` (3600) / `false` | the one-hour same-price gate is a **fallback for posts with no ASIN/PID**; set it to `true` to also block a *different* product that happens to share an already-posted price |
| `QUEUE_ORDER` | `newest` | `newest` = a fresh deal is dispatched ahead of backlog; `oldest` restores FIFO |
| `MAX_JOB_AGE_HOURS` | 6 | pending work older than this is dropped (`STALE DROP`), never posted late |
| `JOB_RETRY_BASE_SECONDS` / `JOB_RETRY_MAX_SECONDS` | 3 / 20 | retry backoff for an undelivered job (was up to 300s) |
| `JOB_MAX_ATTEMPTS` | 10 | attempts before a job is marked `failed` |
| `HTTP_TOTAL_TIMEOUT_SECONDS` | 12 | per-request budget for resolve/health/EarnKaro |
| `LINK_CHECK_ATTEMPTS` / `LINK_CHECK_RETRY_SLEEP_SECONDS` | 2 / 0.4 | health-probe behaviour |
| `LINK_HEALTH_CACHE_SECONDS` | 900 | a URL is probed once per job, not once per stage |
| `PRESEND_CHECK_BUDGET_SECONDS` | 25 | hard wall-clock cap on the pre-send link check |
| `MAX_MEDIA_MB` / `MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | 45 / 120 | oversized or slow source media is skipped (text still posts) |
| `SOURCE_RESCAN_SECONDS` / `SOURCE_RESCAN_LIMIT` / `SOURCE_RESCAN_CONCURRENCY` | 120 / 40 / 4 | ingest dead-man's switch cadence |
| `SOURCE_REFRESH_SECONDS` | 180 | retry joining sources that failed at startup |
| `TARGET_FANOUT_GAP_MIN` / `TARGET_FANOUT_GAP_MAX` | 0.4 / 1.2 | random gap between the fan-out targets of one job |
| `PASSTHROUGH_UNMONETIZED` | true | publish a store link the affiliate network cannot monetize as a clean untagged merchant link (false = retry then skip, i.e. lose the deal) |
| `PREMIUM_MAX_PER_NIGHT` / `PREMIUM_GAP_MIN_SECONDS` / `PREMIUM_GAP_MAX_SECONDS` | 12 / 900 / 2100 | Premium channel curation (the only place the bot intentionally waits) |
| `WA_BEST_GATE` etc. | see bridge | WhatsApp best-deal gate, unchanged |

Log lines that prove it is working: `QUEUED` (ingest → queue in the same
second), `RECOVERED`/`RESCAN` (event-stream gap healed within one short cycle),
`RETRY` (job retried in seconds), `INTAKE DEDUP` / `DEDUP` (a copy refused
before it could ever post), `LINK REPAIR` (a source link replaced by ours
instead of dropping the deal), `PASSTHROUGH` / `DEGRADED POST` (an unmonetizable link
still posts clean instead of vanishing), `EDIT REVIVE` (an edited source post
that had not gone out is re-queued), `NIGHT SKIP` / `STALE DROP` (nothing stale is
posted late).

## Current deployed version (server-verified)

Matches what ran on the Oracle server after the 2026-08-23 14:21 UTC deploy.

> **The tracked source is now ahead of this table.** `bestgaa/main_bot_new.py`
> (v17: verbatim price lines, outbound junk guard, a dedup fingerprint that no
> longer swallows posts, unmonetizable-link pass-through, edited-post revive) and
> `tg-wa-bridge/bridge.js` (the same junk/markdown guards, with WhatsApp
> `*bold*` formatting left intact) changed in
> `arena/01a0583b-new-deals-bot-zip`; the hashes below describe the *deployed*
> build only. Deploy the repo source (`ops/repack_bundles.sh` →
> `ops/apply_dual_hotfix.sh`, or `bestgaa/deploy_bestgaa.sh` for the bot alone),
> then refresh this table with the new hashes.

| Artifact | SHA-256 |
|---|---|
| Hotfix zip (`archive/bestgaa_whatsapp_hotfix.zip`) | `569b7415c375de0fddc2a3d27653f11e3dd764ce4c7cb922a373de5936150128` |
| `bestgaa/main_bot_new.py` (= server `main_bot.py`) | `087d227516e4e9392a4efce8ce7da09f470428a56a0088adf804029c1b0294f6` |
| `tg-wa-bridge/bridge.js` (= server `bridge.js`) | `3faf9856dacd84e3f57347c7699ecd93c767d71d2b936fa11bcb4506ac2c5407` |

Current **repo source** on this branch (v17 — not yet deployed to a server):

| File | SHA-256 |
|---|---|
| `bestgaa/main_bot_new.py` | `3776bd53486926f6004f65d4de3bfebb18fb15a82a36ac4043a2c7c3d833d2e6` |
| `tg-wa-bridge/bridge.js` | `5311a3ee7209ea963b20178ca145453a6409229ad80c07e7ba99540014f19422` |

> Note: the hash list at the bottom of `ops/WHATSAPP_MEDIA_FIX_NOTES.txt`
> (`51c3791b…` / `5d8e1f50…` / `28656d74…`) predates this build and is stale.

## Security

Never commit or paste `.env`, `auth/`, BotFather tokens, or pairing codes/QRs.
`.gitignore` already excludes them. Back up `.env` and `auth/` off-server.

> Reality check (from the shipped docs): the WhatsApp side is an unofficial
> Baileys client, not Meta's official Business API. It is not unlimited,
> ban-proof, or guaranteed to keep working. Use only a dedicated number and a
> Channel you control; no unsolicited private messaging.
