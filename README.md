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
| `ops/` | `apply_dual_hotfix.sh` (one-shot server deploy of both services), `install_bestgaa.sh` (first-time bot installer), `repack_bundles.sh` (rebuild deploy zips from source), `coverage_audit.py` (source-vs-channel coverage report + `--heal` re-queue of posts that never went out), `diagnose.sh` (deployed-fix markers + coverage in one command), `sync_identity.py` (regenerates the auditor's copy of the product-identity rule; `--check` is a test), `identity_probe.py` (ask, from the CLI, whether two posts are the same product on both services), routing + media-fix notes |
| `archive/` | Original uploaded hotfix zip, kept for provenance |

## Quick checks (no credentials needed)

```bash
# Python bot — deps (a fresh box needs aiohttp/telethon) + smoke test
pip install --break-system-packages aiohttp telethon
python3 -m py_compile bestgaa/main_bot_new.py bestgaa/migrate_legacy_env.py \
                      ops/quality_audit.py

# Bridge — deps + built-in contract tests (expects: "bridge self-test PASS").
# Env values are arbitrary — the self-test makes no network calls (and, since
# v17.6, it cannot write the live state file either):
cd tg-wa-bridge && npm install \
  && TELEGRAM_BOT_TOKEN=x WA_PHONE=919876543210 WA_CHANNEL=x@newsletter node bridge.js --self-test

# Behaviour tests (no network; expects all green):
python3 test_render_job.py        # 160 checks: routing, formatting, conversion
python3 test_rescan.py            # ingest dead-man's switch + idempotency
python3 test_pipeline_fixes.py    # 225 checks: immediacy, zero duplicates, quality
python3 test_best_copy.py         # best copy of a product, fidelity gate, auditor
python3 test_duplicate_sim.py     # real worker path: one copy per channel, always
python3 ops/deploy_and_verify.sh --verify-only   # on the server: proves what is live

# Prove the guarantees on a real (or copied) database — read-only, exit 1 with
# --strict so cron/systemd can alert on it:
python3 ops/quality_audit.py --db bestgaa/state/bot_state.sqlite3 --limit 200
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
- **v17.1 — a list post never loses its headline:** the reported shape (one
  product line with the price, then a `➜` + link per variant) came out of our
  channel as four bare links. `format_visible_source_product_pairs()` accepted a
  decorated bullet line as a *product label*, so four variants produced four bogus
  pairs, the real headline was consumed as a label and discarded, and the
  dangling-bullet cleanup then deleted the bullets. Label extraction is now one
  classifier, `_product_label()` — decoration stripped **by class** (never by an
  emoji list, because an omitted emoji silently turns a bullet into a product),
  real words required, a URL never a label — and a post with fewer than 3 genuine
  products falls back to `rebuild_text`, which keeps every source line. Cleaning
  may only remove junk: dropping a product name or price line is the same class of
  bug as printing junk, and `test_list_post_shapes()` is the tripwire.
- **v17.1 — a glued link is separated, and the junk sweeper never eats a URL:**
  `₹260https://bitli.in/x` used to publish as `₹260://bitli.in/x` because
  `strip_price_junk` cut the `https` off as if it were a junk token. It now masks
  real URLs before cutting, and `tidy_post` (mirrored by `sanitizeOutbound` in
  the bridge) inserts one space before a glued link, so the price reads exactly
  as published (`₹260`) and the link stays intact and clickable.
- **v17.1 — a source's own hype never enters our channel:** the channel banners
  loot channels paste above every list (`🔥🔥 TOP DEAL OF THE DAY 🔥🔥`,
  `⚡️ 11 PM FLASH SALE ⚡️`) used to survive cleaning because `is_promo_noise_line`
  only reacts to CTA verbs. `is_campaign_banner_line()` drops a line only when
  *every* word of it is generic campaign vocabulary and it carries no link, no
  `₹` amount and no percentage — brand/store names are excluded from the
  vocabulary on purpose, so a real store header (`Myntra Mega Sale`) and any
  product line stay. And if every text line of the post is a banner, the first one
  is kept as the headline: a wall of bare links is worse than a headline with
  hype on it. Mirrored in the bridge (`dropCampaignBanners` in `cleanDealText`).
- **v18.3 — a post is not its source message, and the bridge's own tests stopped
  depending on the clock.** Two fixes of the same class as v18.2's, both found by asking
  "can a post that reached intake still fail to appear?" of paths nobody had probed:
  1. **A deleted source message used to delete the deal.** `render_job` refused to render
     without the live Telegram message, so when a channel removed (or re-posted) its own
     message, the row burned all `JOB_MAX_ATTEMPTS` on it and was dropped - while the queue
     had held the full text (`source_text`) the whole time. It now renders from that copy and
     logs `SOURCE MESSAGE GONE`. Media is *not* invented (`.media`/`.photo`/`.entities`/
     `.reply_to` stay absent, so the photo-only-caption rule still skips what it should skip),
     a FloodWait/network failure still gets its retries first because the next attempt may
     bring the photos back, and a row with no stored copy keeps the old retryable error
     instead of posting something made up.
  2. **A ready-to-send job died on an optional fetch.** The already-rendered path re-fetched
     the message only to sharpen residue cleaning and re-attach media; a throwing fetch
     (`Could not find the referenced message`) killed the whole job. It now degrades to
     "no message", which the existing branches already treat as "post the stored copy".
  3. **`bridge.js --self-test` was time-of-day and timezone dependent.** Its trust-policy
     fixtures were built from `Date.now() - 3h`, so run at 08:00 IST "three hours ago" is
     05:00 - quiet-born - and a healthy bridge reported `best-tier deal wrongly expired` in
     the morning, exactly when `ops/deploy_and_verify.sh` runs. The fixtures now pass their
     own `now`, size the age from `WA_ORDINARY_MAX_AGE_MINUTES`/`MAX_JOB_AGE_HOURS`, and take
     the quiet/free minute from the live `QUIET_*` window (asserting the 24-hour-pause and
     no-pause cases too), so the self-test means the same thing at any hour, in any zone,
     under any window. 22/22 modes verified.
- **v18.2 — the last structural swallow, and the test suites stopped ignoring their own
  knobs**:
  1. **A photo post with no link is a post.** `PermanentSkip("no URLs")` deleted every source
     message that carried no URL — which is exactly how a channel posts a price inside the
     image, or writes "link in the first comment". Now such a post is published as the source
     wrote it (its own caption + its own album, `LINK-FREE POST | the source posted this with
     no link at all`), with the one line of ours on top so the reader still has a way in.
     Two guards keep this from becoming noise: the message must carry media, and it must state
     real deal terms (a price, a discount, a card or a service offer). A photo with no deal
     terms and a text-only line with nothing to buy are still skipped — the curated WhatsApp
     gate also still skips a link-free post, out loud, with the reason in the log.
  3. **A repair must not crash the post it is repairing.** The layout rule added in this round
     read `row["text"]` — a column the queue table does not have — and `sqlite3.Row` raises on a
     missing key, so a post being repaired (a dead or unverified link) failed with
     `JOB FAIL 1: No item with that key` and stayed pending forever. Fixed at the root: the queue
     now stores the source text (`source_text`, added through the normal migration path and kept
     in step by the best-copy swap), an unknown source text means **hands off** (never re-flow),
     and `test_row_columns_exist` reads the module with `ast` to check every `row["…"]` against
     the real `PRAGMA table_info` of the table that function queries — so this class cannot come
     back. `test_a_live_database_from_an_older_build_is_upgraded` proves the upgrade on the
     server's existing DB, and S12a/S12b in the duplicate sim drive both repair paths for real.
  2. **Every suite now respects the knob it depends on.** Running the suites across 20+
     environment values produced failures that were the TEST's fault, not the code's: an
     assertion that hard-codes a default (`SHORTEN_MIN_LEN`, `MAX_MEDIA_MB`,
     `JOB_MAX_ATTEMPTS`, `PASSTHROUGH_UNMONETIZED`, `SAME_PRODUCT_DISCOUNT_MARGIN`, the
     bridge's `WA_BEST_GATE` / `WA_PRODUCT_DEDUP_HOURS`) cries wolf the moment an operator
     moves the knob — and a false alarm is how a real bug gets ignored. The fixtures and
     expectations are now derived from the live values (and, for the duplicate rule, from the
     store's own "is this copy better" comparison), so `PASSTHROUGH_UNMONETIZED=false` asserts
     the documented skip, `WA_BEST_GATE=false` asserts that nothing is skipped, and a link is
     judged against the threshold that is actually set.
- **v18.1 — the user's ruling on the same example: `unwanted text` must go, and the
  post must look like the source's** (four things, all on the same live text):
  1. **A glued scrap after a price is CUT, not un-glued.** v18.0 separated `₹ 199HFJF`
     into `₹ 199 HFJF` and kept it; the answer from the channel owner was that this is
     not a coupon at all — `h`, `htt`, `jsjd` are paste residue from a short link, and
     nothing that is not in the source may sit in our post. So both engines now delete
     whatever is FUSED to the digits (`_keep_code_as_is` / `keepCodeAsIs`) and write
     nothing in its place. What the source wrote APART is still real text and survives
     untouched: `Use code HFJF for ₹199 off`, `Price ₹199 SAVE200`, `500ml ₹260 offer`.
     A spaced token is deleted only if it is machine-shaped (mixed case, long, with a
     digit — a torn shortener), never a word, a unit or an upper-case code.
  2. **Third swallow, closed: a dead short link used to kill the post.** `render_job`
     turns every link into a destination; when `https://bit.ly/…` no longer resolves, the
     affiliate returns nothing, the destination is unknown, and the whole job raised
     `conversion retry required: … bit.ly` — retried until the price went stale, then
     dropped. `SHORTENER_HOSTS` now recognises that case: the link is cut out of the copy
     (`LINK DROPPED | queue=… posting the deal without it`), the deal is posted complete,
     and a post left without any link is still published rather than skipped
     (`LINK-FREE POST`) — provenance has nothing to prove when there is no URL. Two
     follow-on gates that used to call a key-less post a "duplicate" were fixed the same way.
  3. **Photos arrive as the source posted them.** A loot channel sends six images as ONE
     album (a grid in one bubble); the bot used to download the first photo only.
     `download_album()` walks the album through `grouped_id`, keeps each photo's Telegram
     reference, and `send_media_group()` posts the grid with the deal text as the first
     item's caption — no re-upload, no waiting. Every step is optional in the honest
     sense: one bad photo is left out, and a client or a Telegram that refuses the grid
     falls back to the first photo with the caption and the rest after it, so a photo
     layout can never be the reason a deal is missing (`MAX_ALBUM_PHOTOS`, default 10).
  6. **Fifth swallow, closed: a post the source published with NO link.** A photo/video
     post whose caption carries the deal (`🔥 (Pack Of 20) Multicolor Hair Clips at ₹85,
     90% off`, the link only inside the image) reached the queue and died at
     `PermanentSkip: no URLs`. It is published now — text and media exactly as posted,
     nothing invented (`LINK-FREE POST | the source posted this with no link at all`).
     The two guards that keep this from becoming junk: the message must carry media, and it
     must state real deal terms (a price, a discount, a card or service offer). A photo with
     no deal terms and a text-only line with nothing to buy are still skipped, and the
     curated WhatsApp gate still skips a link-free post out loud.
  5. **Spacing is the source's, not ours.** A line our own passes deleted (a foreign
     channel's share button, a stripped CTA clause, a cut link) used to remain as an empty
     line, which reads like the bot padding the post. `keep_source_spacing()` (and
     `keepSourceSpacing()` in the bridge) collapses a blank line only when the source wrote
     none — where the source *did* space its blocks apart, that layout survives untouched.
  4. **Our own channel link, neatly, on the TOP line** — the user asked for a line that
     lets a reader reach the rest of our channels instead of wandering to somebody else's.
     `prepend_channel_header()` writes exactly `👉 All Loot Channels: <folder link>` as
     the first line, never twice, never on the Tricks path (its own footer stays as it
     was), and it is added **on the way out**, so the stored post, the auditor's input and
     the chunk-resume logic keep seeing the source's text alone (`ADD_OUR_CHANNEL_LINK_TOP=false`
     turns it off). The Bitly rule the user re-confirmed is verified by a test too: several
     links in one post (or any long link) go out as our short links one per line, a tidy
     single `/dp/` link stays direct so the quota is never spent, and Bitly being down
     posts the tagged links instead of losing the deal (item 5 covers the layout).
- **v18.0 — a post that arrived must reach our channels: three ways it was being
  swallowed, all closed (proved by running the user's own live post through the real
  `process_job`, not by reading the code):**
  1. **A share button is not a destination.** The source post carried
     `👉 [https://t.me/loots/156757](https://wa.me/?text=…)` — a "forward this on
     WhatsApp" link. `render_job` builds its destination list from the *raw* message,
     tried to monetize `wa.me`, got nothing back, and raised
     `conversion retry required: affiliate conversion returned no link: wa.me`, which
     retried the whole job until the price went stale and then dropped it: nothing was
     posted, on any channel. `SHARE_INTENT_DOMAINS` + `is_share_intent()` now keep
     share/invite/app-scheme links (`wa.me`, `api.whatsapp.com`, `t.me/…`, `tg://`,
     addtoany, sharethis, getpocket, `vk.com`, `m.me`, …) out of the destination list
     *and* out of the health gate — a link we do not publish can veto nothing.
  2. **A link that reads dead is not a dead deal.** When every destination answered as
     a merchant repair page, `render_job` raised
     `PermanentSkip("every destination is a confirmed dead merchant page")`, and the
     bridge threw `Broken destination:`, which `isPermanent` treats as fatal. Amazon
     serves exactly that page to a datacenter IP and to any freshly-created ASIN, so a
     live deal was being silenced by a bot check. Both services now log
     `LINK UNVERIFIED | queue=… posting anyway` (bridge: `link health says dead;
     posting anyway`) and deliver; the hard block is opt-in
     (`DROP_DEAD_LINKS=true` / `WA_DROP_DEAD_LINKS=true`).
  3. **Cleaning had started eating money.** `✅Deal Price: ₹ 199HFJF` lost `HFJF`,
     because the price cleaner deleted whatever was glued after a `₹` amount — right for
     a torn shortener (`₹260tG7oChgiQuTgS25b`), wrong for a coupon code, which is the
     reader's discount. Both engines shared one token-shape rule that UN-GLUED an
     all-caps code and kept it. *(Superseded in v18.1: the channel owner ruled that this
     shape is paste residue, not a code, so a glued token is cut and only what the source
     wrote APART is kept — see the v18.1 bullet.)*
  One more form the sources write and the parsers did not read: `Discount: 26%` (colon
  after the label) returned None from `parse_discount` and 0 from the bridge's
  `explicitDiscount`, which is what decides the best-pick and the price-tier routing —
  both read it now, and `Extra 5% cashback` is still not mistaken for a discount.
  `test_duplicate_sim.py` S9 posts the user's exact text and requires every routed
  channel to receive it complete, with `199 HFJF` and no `199HFJF`;
  `test_line_fidelity.py` pins the un-glue rule, the "never fuse two lines" guard, and
  that a markdown link whose label is itself a URL keeps the **label** (the canonical
  merchant page we can monetize) rather than the third-party shortener in its href.
- **v17.9 — the same-product rule was still wrong, and it is fixed at the root:**
  auditing v17.8 rather than trusting it found that the product signature hashed the
  **first eight words of the headline in order**, which fails in both directions:
  `boAt Airdopes 141` and `Airdopes 131` produced the SAME key (digit-only model
  numbers were dropped as "not words", so a genuinely different product was skipped
  as a repeat - a lost deal), while one product written by two channels with any
  extra adjective (`…TWS Earbuds` vs `…True Wireless Earbuds, 42H Playtime`)
  produced DIFFERENT keys (the duplicate the user complained about, still posted).
  Identity is now a **set of meaningful tokens**: brand word + model numbers
  (`141`, `s23`, `wh-ch720n`, `pro4`, `model2600`) + variant qualifiers
  (`pro/fe/max` - so S23 and S23 FE stay two products) + the numbers that change the
  product (128GB, 1.5 ton, 5 star, 55 inch, 5 burner); specs that channels type
  inconsistently (42H, 5000mAh, 1080p, 4K, 5G) and a launch year (`(2023)`) are
  ignored, punctuation is trimmed off token ENDS only (so `1.5` is one number and
  `WH-CH720N` is one model), and a headline with no number at all still has to match
  word for word. Same rule, three services, no drift: `ops/sync_identity.py`
  GENERATES the auditor's copy of the rule (`--check` runs in `test_line_fidelity.py`),
  and the bridge answers `--identity-probe` on stdin so the Python and JavaScript
  rules are compared on the same headlines on every run. Two more places where text
  or money was being lost the same way are fixed too: `chunks()` cut an over-long
  line at a character index, so a long list line could print
  `https://www.amazon.in/dp/B0AB` in one message and the rest in the next - a dead
  affiliate link, an unpaid sale - and the WhatsApp caption splitter did the same at
  its 1024-character limit; both now break at a space *outside* any URL, and
  `test_line_fidelity.py` / the bridge self-test fail if a link is ever bisected.

- **v17.8 — the same product once per channel, and not one source line lost:** the
  user's three remaining complaints were all real. (1) *Duplicates.* Identity used
  to be the link, so the same air conditioner linked through two different
  shorteners — or the same Flipkart page written as a slug link by one source and a
  `/p/<id>` link by another — looked like two products and the channel carried it
  twice. `extract_product_id()` now reads the product id out of every merchant link
  shape (Flipkart/Myntra/Ajio paths, `/product/<id>`, `/it/<id>`, an SKU tail), and
  on top of that a **product signature** (the product's own words plus any
  size/capacity token, price and MRP masked out because those change between copies
  of the same deal) is ledgered per channel in `posted_products`
  (`SAME_PRODUCT_SKIP_SECONDS`, default 3 days): a channel that has carried the
  product stays quiet, while a **strictly better** copy (cheaper, or ≥5 points
  deeper discount — a 1-point difference is measurement noise) still posts, so
  coverage is never traded for tidiness. The list is deliberately narrow: no
  identity for a roundup, nothing that looks like a category phrase, nothing for
  the Tricks path. `ops/quality_audit.py` counts it as `SAME-PRODUCT` when a channel
  carries a product twice at a same-or-worse price. (2) *Lost text.* Cleaning used
  to eat real source text: the CTA patterns swallowed "everything to the end of the
  line", so `More offers: Apply coupon PEOPLE200` lost the coupon and
  `More deals here: <link>` lost *the whole post* (no link left → permanent skip);
  `fix_unbalanced_parens` then deleted characters until the brackets counted even,
  mangling every numbered list (`1)` → `1`) and every `(78% off)`. A CTA clause may
  now only swallow **plain words** — a price, a digit, a percentage, a code or a
  link ends it — both cleaners trim the punctuation a removal leaves behind, the
  bracket fixer only touches a bracket that stands alone, and `strip_inline_cta`
  undoes itself whenever a figure disappears. `test_line_fidelity.py` renders a
  corpus through the real pipeline and asserts every source line survives, that no
  `…` appears, and that no amount or percentage is printed more often than the
  source wrote it. (3) *WhatsApp lost the most text and showed the price twice*:
  a digest item was a 90-character name plus an invented `₹price • x% OFF` badge
  (that badge *is* the second price) cut to 220/160/100/60 characters to fit one
  message; a special offer's caption was cut at 1000 characters; a long list line
  was cut at 650. Digest items are now the source post itself, numbered — nothing
  invented, nothing cut — and when the bucket holds more deals than fit, the digest
  carries **fewer complete posts** and the rest ride in the next one. A caption too
  long for a photo is split at a line boundary and the remainder follows it
  (`splitCaptionForMedia`), and the bridge gained the same product-identity rule
  (`nameOnlyKey`/`state.sentNames`, `WA_SAME_PRODUCT_HOURS`, `WA_SAME_PRODUCT_MARGIN`)
  plus the same "cleaning may not delete a fact" guard, so the WhatsApp channels
  recognise a product the way the Telegram ones do. (4) Our own footer
  (`🔥 JOIN OUR COMPLETE LOOT FAMILY`) and the folder link on card offers were text
  *we* added to someone else's post — both are off by default now and come back only
  with `ADD_OUR_CHANNEL_FOOTER=true`; the Tricks channel keeps its footer untouched.
  `ops/deploy_and_verify.sh --with-tests` runs every suite on the machine that will
  be shipped from, and the bridge self-test now proves the digest/caption fidelity
  rules on the deployed copy too.
- **v17.7 — nothing waits, nothing leaks, and the deploy is provable:** the last
  artificial pause on the Telegram path is gone — channel fan-out used to sleep a
  random 0.4–1.2 s between targets (up to ~6 s across the whole matrix), which the
  user rightly calls a bug; `TARGET_FANOUT_GAP_MIN/MAX` now default to **0** and the
  loop only sleeps if an operator sets them. The bigger latency hole was the pre-send
  link check: a single-link post had *no* budget at all, so one slow merchant page
  held a live deal for `2 × 12 s`; every probe is now capped by
  `LINK_PROBE_BUDGET_SECONDS` (3.5 s) and a page that does not answer in time is
  treated as *inconclusive, not dead* — the deal goes out. The old "every
  destination unverified, retry later" loop (which re-rendered the whole job 3–20 s
  at a time) is replaced by a single verdict, and a page is only refused when it
  carries an explicit merchant "this offer is gone" signature. `ops/quality_audit.py`
  learned what "our link" actually means (our shortener, our EarnKaro output, an
  Amazon page **tagged with our tag**, a clean page for a store we cannot monetize,
  our own folder/channel links) so it stops flagging legitimate posts while still
  catching a source `amzn.to`, a third-party shortener, or our tag glued to
  somebody else's publisher id. `ops/deploy_and_verify.sh` is now the one command to
  ship and *prove* it: hash-compare the deployed files, require the boot line for
  the version we committed, re-run the bridge contract self-test on the server copy,
  measure **median/p95 seconds from `QUEUED` to posted** from the live log, and run
  the quality audit over the real queue. `test_duplicate_sim.py` drives the real
  worker path (`claim_job` + `process_job` + `deliver` + `finish`) over planted
  traffic — the same message ingested twice, the same product from three sources,
  two copies claimed concurrently, a crash mid-delivery plus restart, the
  best-copy rollback, identical campaign text from another source, and a
  two-product post — and asserts one copy per channel and nothing lost in every
  case; `QUEUED | queue=N` now carries the id so the immediacy number is computable
  at all.
- **v17.6 — the queue publishes the BEST copy of a product, plus a numeric
  fidelity gate and an offline auditor:** many sources post the same item minutes
  apart at different prices, and until now *whichever copy arrived first* decided
  what our channels showed — a 78%-off post could be skipped because a 65% copy
  from another channel was queued seconds earlier. Both sides now re-point the
  still-undelivered queue row at the stronger deal (Python `swap_in_better_copy`,
  bridge `adoptBetterCopy`), so one product stays ONE row/job — the duplicate
  guarantee is untouched — and the copy it displaced is remembered and re-queued
  (`BEST COPY ROLLBACK`) if the better one cannot be rendered, so the selection can
  never cost coverage. Matching is deliberately exact: only merchant product ids
  (`/dp/ASIN`, `/gp/product/`, `?pid=`) match, so a shortener-only or
  name-similar post is never re-pointed, and lists, card/bank offers and the Tricks
  path are excluded on both sides. On top of that, `enforce_numeric_fidelity`
  refuses to publish a `₹` amount or a discount percentage the source never
  printed (URL digits are masked on both sides, so our own tag `deals0911-21` is
  never mistaken for a price) — the bad *number* is removed, the *post* is kept.
  The bridge also stops dropping a whole roundup because one of its items was
  already queued (lists are coverage; only a single-product copy is "the same
  deal"). `ops/quality_audit.py` proves all four guarantees on the live database —
  invented text, foreign links, link-only posts, price-band misroutes, lost jobs
  and double posts of one product — and `ops/diagnose.sh` now runs it in section 10
  (`--strict` for cron), so a regression is visible the day it appears.
- **v17.5 — NOTHING of ours is added to a post (WhatsApp side):** the bridge used
  to *re-layout* every deal — it hoisted a product line into a bold title, printed
  its own `💰 ₹499  |  🔥 75% OFF` badge line, dropped the source's real price line
  as "redundant" with that badge, wrapped specials in `🔥 *LOOT ZONE — India*` /
  `🚨 *SPECIAL OFFER*` / `✅ Verified • Enjoy (Grab fast)`, pasted
  `🛍️ MEGA DEAL LIST` above big lists, opened digests with `🔥 DEALS OF THE DAY`,
  added a `➜` in front of every link and fell back to an invented `Latest deal`
  caption. Every one of those is **text the source never wrote**, so all of it is
  gone: a WhatsApp post is now the cleaned source post — source lines in source
  order, one OUR link per line, nothing added, nothing re-ordered. Junk still goes
  (another channel's branding, referral/app-install farming, CTA filler, markdown
  `**` debris, glued tokens), and if a source's own line is `🔥🔥 TOP DEAL OF THE DAY
  🔥🔥` that line prints **exactly as written** (only `WA_STRIP_CAMPAIGN_BANNERS=true`
  removes hype, and even then the real deal text stays). Telegram gained the same
  promise: the dead `format_premium_loot()` wrapper (which would have written
  `👑 PREMIUM LOOT PICK 👑` + `✨ Handpicked • Verified Link • Grab Fast`) is deleted,
  and `tidy_post` no longer shaves `_` out of the middle of a word — `Use code
  SAVE_200` used to publish as `SAVE200`, a code that does not work. Routing per the
  user's channel list: **LootZone = every deal, PowerLoots = every deal** (its old
  ≤₹499/70%-off filter silently dropped posts), **Secret = only the strong picks**
  (`eligible_for_secret`), **Premium = highest-discount only** (a sub-₹99 item now
  needs a real discount too), Under99 = ≤₹99, Under499 = ≤₹499, **any 3+ product
  list reaches every non-Tricks channel**, and the Tricks pipeline is untouched.
  One post is still exactly one copy per channel.
- **v17.4 — each WhatsApp channel gets its own content, and the best-of channel
  posts only the winner:** `WA_CHANNEL_UNDER99`, `WA_CHANNEL_UNDER499` and
  `WA_CHANNEL_BEST_OF` are resolved to JIDs (a channel that fails to resolve is
  retried every 10 min instead of staying missing until the next reconnect) and
  mapped to a policy in `CHANNEL_POLICY_OF_JID`, so `targetsFor()` *asks each
  channel what it accepts* instead of hard-coding "the second channel is under-99":
  under-₹99 = deals ≤₹99, card/bank offers and **every product list**; under-₹499 =
  the same with a ₹499 band, pulled from all sources; best-of = only a clear best
  pick (`eligibleForChannel`: 60%+ off in band, `BEST_MIN_DISCOUNT` at ≤₹99, 70%+
  when the source printed no price, card/service posts auto-pass) **and** only
  while it is the top-ranked ready deal — `isBestOfMoment()` compares
  `dealQualityScore` across the queue (ties to the newer post), so ten deals at
  once produce ONE best-of post and the other nine keep flowing on the paced main
  feed; a deal that is not the best is skipped there, not delayed and dumped.
  `WA_BEST_OF_COOLDOWN_SECONDS` (default 0) spaces the picks out if you want fewer;
  `WA_CHANNEL_ALL_POSTS=true` still mirrors everything to every channel, and a
  digest stays a main-channel item otherwise. On the Telegram side the list rule is
  now the same instruction: a 3+ product list is a curated roundup, so it fans out
  to `Under99Deals11` **and** `under499loots` whatever the item prices are (it used
  to require an in-band price), single deals keep their band check, `Premiumlootsdeals`
  keeps only best-scored deals, and card/service offers still fan out to every owned
  channel. Dedup is untouched by all of this - one post means one copy per channel.
- **v17.3 — SOURCE FIDELITY is the rule (the user reversed the v17.1 banner
  strip):** `🔥🔥 TOP DEAL OF THE DAY 🔥🔥` / `⚡️ 11 PM FLASH SALE ⚡️` are how the
  source channel writes a deal, so they are published exactly as the source wrote
  them — emoji presentation selectors included (`strip_link_fragment_tokens`
  stopped eating a trailing `\ufe0f`). What still goes, always: **another channel's
  branding** (`is_branding_line` / `isBrandingLine`: "🔥 LOOT ZONE INDIA — Join for
  more loot", "Loot Zone India", "Powered by …", "@handle" lines), referral and
  app-install farming, CTA filler, glued random tokens, markdown debris, URL
  residue — and every merchant link is swapped for OUR link. The classifier is
  kept behind `STRIP_CAMPAIGN_BANNERS` / `WA_STRIP_CAMPAIGN_BANNERS` (both
  default `false`) for anyone who later wants the hype lines gone; even then a
  banner that is the post's only text line stays, so a wall of bare links is
  impossible. Two related list-fidelity bugs fixed on both sides: a pure banner
  can no longer be consumed as a product label (`_product_label` rejects it,
  independent of the knob), and the multi-product rebuild now emits **every**
  source line in source order (it used to print only the label→link pairs and
  silently drop a second header or an MRP/shipping note; the bridge had the same
  flaw, where text lines sitting above a label were thrown away).
- **v17.2 — both WhatsApp channels, and no referral farming ever:** the
  `Get Flipkart App - Refer 3 friends and ₹100 referral bonus` block some sources
  staple under a deal *was* reaching the WhatsApp post, because a `₹` amount on a
  line made `is_promo_noise_line` protect the whole line. `REFERRAL_SPAM_RE` now
  matches refer-N-friends / referral-bonus / install-the-app phrasing on both
  sides, so those lines go while a real coupon line (`Use code SAVE200 for extra
  ₹200 off`) and every product/price line stay. The second channel is a first-class
  target: `WA_CHANNEL_ALL_POSTS=true` mirrors **every** post to both channels
  (default keeps it as the Under-₹99 shelf, digests on the main one), a channel
  that failed to resolve is retried every 10 minutes instead of staying missing
  until the next reconnect, and the two channels are now `WA_INTER_TARGET_GAP_SECONDS`
  (6s) apart instead of a full anti-flood gap — so the 2nd channel no longer looks
  a minute behind. Mature pacing dropped to 30-60s between posts (`WA_MATURE_GAP_*`),
  and the list/special settle from 30-90s to 10-18s: that random hold is what made
  WhatsApp "post slowly and at random". The burst rest, the one irregular hourly
  break and the night window stay on purpose - a WhatsApp number driven at machine
  speed gets banned, and the user's own 02:00-06:00 rule says night loot is dead.
  `install_bridge.sh` now asks for the 2nd channel and the mirror choice, and
  `diagnose.sh` prints which mode is live.
- **v17.1 — coverage is auditable, not assumed:** `ops/coverage_audit.py` reads
  the live DB and reports per source how many posts arrived vs reached a target,
  separating correct refusals (dedup / night window / stale) from `LOST` rows
  (conversion retry, no verified link, provenance, unresolved target, silent
  "no monetizable URLs"). `--heal` re-queues only the LOST rows after copying the
  DB, clearing just their live `deal_claims` and never `posted_deals` — so a
  healed deal another source already published is refused as a duplicate instead
  of posting twice. `ops/diagnose.sh` runs it and also greps the v17.1 markers in
  the files that are really in production, because "you fixed it and I still see
  it" almost always means the server never restarted on the new build.
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
| `PRESEND_CHECK_BUDGET_SECONDS` | 6 | hard wall-clock cap on the pre-send link check; **0 switches the extra pass off** (it used to serialise every probe, which was slower than checking at all) |
| `MAX_MEDIA_MB` / `MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | 45 / 120 | oversized or slow source media is skipped (text still posts) |
| `SOURCE_RESCAN_SECONDS` / `SOURCE_RESCAN_LIMIT` / `SOURCE_RESCAN_CONCURRENCY` | 120 / 40 / 4 | ingest dead-man's switch cadence |
| `SOURCE_REFRESH_SECONDS` | 180 | retry joining sources that failed at startup |
| `TARGET_FANOUT_GAP_MIN` / `TARGET_FANOUT_GAP_MAX` | 0 / 0 (max clamped to 60, min to 30) | no gap by default — Telegram never waits (round 9); these exist only as an escape hatch for a target that rate-limits us |
| `PASSTHROUGH_UNMONETIZED` | true | publish a store link the affiliate network cannot monetize as a clean untagged merchant link (false = retry then skip, i.e. lose the deal) |
| `DROP_DEAD_LINKS` / `WA_DROP_DEAD_LINKS` | false | a link probe may make a post honest, never make it disappear: `true` refuses a post whose every destination answered as a confirmed dead merchant page (the pre-v18.0 behaviour) |
| `ADD_OUR_CHANNEL_LINK_TOP` | true | the ONE line of ours that may sit above a deal: our own folder/channel link on the top line, added at delivery, never twice, never on Tricks posts |
| `MAX_ALBUM_PHOTOS` | 10 | how many photos of a source album we repost as one grid (Telegram's own album ceiling) |
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
> (v17.1: list posts keep their headline, glued links separated, verbatim price
> lines, outbound junk guard, a dedup fingerprint that no longer swallows posts,
> unmonetizable-link pass-through, edited-post revive; v17 items unchanged) and
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

Current **repo source** on this branch (v18.3 — **not yet deployed to a server**;
until `bash ops/deploy_and_verify.sh` is run on the host, the live channels keep
printing exactly what the older build was coded to print):

| File | SHA-256 |
|---|---|
| `bestgaa/main_bot_new.py` | `447644b52906e02906b37df117e0a750f7069d4c0909b58c6d0a7f63677cdf0b` |
| `tg-wa-bridge/bridge.js` | `c8a893cbfce434f19f657183241f0854b6db9107b93c297c405fac0e2df0b68c` |
| `ops/coverage_audit.py` | `38e7d3973b1f693aac46653306b35eec0fd2ff7335ee442c3a7298115bf78e9c` |
| `ops/quality_audit.py` | `29a38074fb9116c6f974c6c55688e500560a235ed0288ff1c06006bb690d2096` |
| `ops/sync_identity.py` | `c26dbbf19a0673bba01ce0547972f5ab2150eea4b2fa1057c083bb561da172cd` |
| `ops/deploy_and_verify.sh` | `6da0caa6912de691328c0d3f3b7bc5e41516d18b346ecb327e03ab748bfbb565` |
| `test_line_fidelity.py` | `befcae7378610c8004930c36d84a359281ae004d092c69774d39e7e3bc769346` |
| `test_pipeline_fixes.py` | `f5c274ddf3d444f921c678c176d62a6e96b4c00e8a5725d608a0777e9c520cd0` |
| `test_duplicate_sim.py` | `aef8d0fa7d0234436a7188ef5922cd5a94f687f28ef938a401b3225576a24298` |
| `test_best_copy.py` | `9b47df6c18148cb59a8c82dff3748195afb39c6da482a985ad360727aa3ee2cc` |

Verified on this tree — **every suite × every knob, 108 runs green** (18 modes:
`SHORTEN_MIN_LEN=1|300`, `MAX_ALBUM_PHOTOS=1|2`, `DROP_DEAD_LINKS=true`,
`STRIP_CAMPAIGN_BANNERS=true`, `SAME_PRODUCT_SKIP_SECONDS=0`,
`PASSTHROUGH_UNMONETIZED=false`, `ADD_OUR_CHANNEL_LINK_TOP=false`,
`ADD_OUR_CHANNEL_FOOTER=true`, `PRESEND_CHECK_BUDGET_SECONDS=0`,
`PRODUCT_DEDUP_SECONDS=0`, `MAX_MEDIA_MB=1`, `JOB_MAX_ATTEMPTS=3`,
`STATE_RETENTION_DAYS=1`, `SAME_PRODUCT_DISCOUNT_MARGIN=1`, both `POST_QUIET_*`
extremes, and everything-off together) — plus the bridge self-test in all **22**
modes listed in the table below, and
`python3 ops/sync_identity.py --check` keeps the auditor's copy of the identity rule
from ever disagreeing with the bot's:

> That matrix is how the last defect in this branch was caught: S13b assumed our
> channel top line exists, so it failed with `ADD_OUR_CHANNEL_LINK_TOP=false` — a
> lying assertion, not a broken pipeline. It now asserts the knob's own contract in
> both directions, which is the rule every test in this repo follows.

| Suite | What it pins |
|---|---|
| `test_line_fidelity.py` | every source line of a banner/coupon/MRP/numbered-list post survives the real `render_job`, no `…`, no invented footer, no amount printed more often than the source wrote it; the product signature recognises the same product through two links and two captions, keeps `141` apart from `131` and `128GB` from `256GB`, refuses to key a roundup or a bare category phrase; chunking never cuts a link; the auditor's rule and the bridge's rule are checked against the bot's | ; a coupon code glued to a price is un-glued and kept while link debris is cut, and `[…](…)` keeps the label URL ; a scrap glued to a price is cut while spaced text survives, an album of photos is posted as one grid (with its fallbacks), several links leave as our Bitly links and a dead shortener never costs the post, and the top line is our own channel link exactly once
| `test_render_job.py` | 160/160 — PowerLoots takes every deal, premium needs a real discount, a list lands in both price channels exactly once per channel, and a source message deleted after intake still renders (media never invented, transient failures retried first) |
| *Every suite above passes at the defaults AND with each knob moved*
  | (`SHORTEN_MIN_LEN`, `MAX_ALBUM_PHOTOS`, `PRESEND_CHECK_BUDGET_SECONDS=0`,
  | `PASSTHROUGH_UNMONETIZED=false`, `SAME_PRODUCT_*`, `PRODUCT/PRICE_DEDUP_SECONDS=0`,
  | `MAX_MEDIA_MB=1`, `JOB_MAX_ATTEMPTS=3`, `DROP_DEAD_LINKS=true`, the opt-in strippers,
  | and the bridge in 7 modes) — an assertion that ignores a knob it depends on is a false
  | alarm, and false alarms are how real bugs get ignored.
  | `test_pipeline_fixes.py` | 225/225 — source fidelity incl. the "nothing added by us" end-to-end test, list shapes, branding/referral junk, coverage audit, price fidelity, no-silent-loss; pacing assertions are pinned to a forced window, not the wall clock |
| `test_best_copy.py` | best-copy swap ("one row per product", "a weaker copy never downgrades", "a list is never hijacked", "the displaced copy comes back"), the numeric fidelity gate, and the quality auditor catching each defect class |
| `test_duplicate_sim.py` | one copy per channel through the real worker path in all nine duplicate-prone scenarios, incl. the same product through two unresolvable links (second copy skipped), a cheaper copy (still posted) and a different product from the same store (never skipped); S9-S11 are the user's own live posts run end to end - a share button and a 404-to-us link may not swallow a post, a dead short link is cut instead of killing the deal, and the top line is our own link exactly once |
| `test_rescan.py` | rescan/re-queue behaviour |
| `node tg-wa-bridge/bridge.js --self-test` | passes in **22** env modes — default, `WA_STRIP_CAMPAIGN_BANNERS=true`, `WA_CHANNEL_ALL_POSTS=true`, `WA_WARMUP_DONE=false`, `WA_BEST_OF_COOLDOWN_SECONDS=3600`, `WA_MEDIA_FIRST=false`, `WA_DISABLE_SMART_ANTIBAN=true`, tuned gaps (`WA_MATURE_GAP_MIN/MAX_SECONDS`), `WA_MAX_MESSAGE_GAP_SECONDS=1800`, `WA_SAME_PRODUCT_HOURS=0`, `WA_SAME_PRODUCT_MARGIN=20`, the four-channel matrix, a combination, `TZ_NAME=UTC`/`America/New_York`/`Asia/Dubai`, `QUIET_START/END` at 00:00-23:59, 12:00-12:00 and 22:00-06:00, `WA_ORDINARY_MAX_AGE_MINUTES=1` and `=900`, and everything-off together. **Since v18.3 none of the trust-policy fixtures read the wall clock**, so a morning deploy cannot fail for a healthy bridge |

`python3 ops/identity_probe.py "<post A>" "<post B>"` answers, from the CLI, whether
the bot and the bridge consider two posts the same product — the check to run when a
channel shows a repeat (or a deal goes missing) and nobody knows why.

The bridge self-test additionally asserts the channel policy matrix (₹89 vs ₹399 vs
₹24999, the list-agnostic price rule, card offers everywhere), that a 10-deal burst
produces exactly one best-of post, that no anti-ban gap can exceed
`WA_MAX_MESSAGE_GAP_SECONDS`, that a WhatsApp post never repeats a price or drops a
line, that a caption break never splits a link, that no formatter may print an
ellipsis at all, that the name-only product skipper recognises the same AC through two links and
still allows a cheaper copy, and that the user's real post reaches WhatsApp as the
source text with our 4 links — no asterisks, no badges, no banner of ours,
`(74% OFF)` brackets and `SAVE_200` intact.


> Note: the hash list at the bottom of `ops/WHATSAPP_MEDIA_FIX_NOTES.txt`
> (`51c3791b…` / `5d8e1f50…` / `28656d74…`) predates this build and is stale.

## Security

Never commit or paste `.env`, `auth/`, BotFather tokens, or pairing codes/QRs.
`.gitignore` already excludes them. Back up `.env` and `auth/` off-server.

> Reality check (from the shipped docs): the WhatsApp side is an unofficial
> Baileys client, not Meta's official Business API. It is not unlimited,
> ban-proof, or guaranteed to keep working. Use only a dedicated number and a
> Channel you control; no unsolicited private messaging.
