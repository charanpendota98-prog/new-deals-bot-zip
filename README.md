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

## Current strict monetization policy

The active policy is **our verified earning links only** (`STRICT_OUR_LINKS` is
pinned on in both services and is not an environment opt-out):

- Telegram publishes only affiliate URLs returned and verified by the configured
  converter, our native Amazon Associates link, or another recorded owned link.
  Service/lifestyle URLs and unsupported/no-campaign merchant links are skipped;
  a transient API exception retries, then the post is skipped if ownership still
  cannot be verified. There is no clean merchant-URL fallback.
- The WhatsApp bridge does not trust `bit.ly`, `fktr.in`, `ekaro.in`, or any other
  shortener hostname or BestGAA `link_cache` row by itself. Links need our native
  Amazon tag, an accepted native publisher ID, account-bound `owned_link_proofs`,
  or an exact verified HYPD-to-Meesho mapping. Foreign wrappers, service links,
  and unsupported/unattributed destinations are withheld.
- Amazon Associates links are target-specific. If a Telegram destination is not
  configured to receive our tag, only that target copy is skipped; the bot never
  removes the tag and publishes a zero-commission Amazon URL. Declare a channel
  in Associates Central first, then configure `AMAZON_TAG_TARGETS` or the
  category allowlist. WhatsApp category copies are likewise withheld until their
  Amazon-tag allowance is explicitly configured.
- Product-list items keep their name, price, and link together. If strict
  verification cannot provide our own link for an item, the affected post/list is
  skipped as a unit rather than leaving an orphaned name or price behind.

Category mirrors use both product text and link-derived clues: readable URL path
slugs and selected search/title query terms from incoming links and known resolved
product destinations. An opaque ASIN/product ID by itself is not a category clue,
so unclear or mixed-product posts stay on Main. The classifier does not fetch or
inspect merchant-page HTML; it routes only when the available text/link terms are
confident and unambiguous.

The historical `PASSTHROUGH_UNMONETIZED` setting is retained only so old test and
migration fixtures remain readable; it cannot override the pinned strict policy.
The source does not auto-deploy or restart services; production rollout is a
separate operator-controlled step.

## Repository layout

| Path | What it is |
|---|---|
| `bestgaa/` | Telegram affiliate bot v15 (`main_bot_new.py`), deploy script, legacy-`.env` migrator, systemd unit |
| `tg-wa-bridge/` | Telegram → WhatsApp Channel bridge (`bridge.js`), installer, number-switch script, systemd unit |
| `ops/` | `deploy_fresh.sh` (the one-command fresh deploy: ship → teach → prove → report), `conversion_report.py` (per-route status of what is actually converting, from the live DB + log), `apply_dual_hotfix.sh` (one-shot server deploy of both services), `install_bestgaa.sh` (first-time bot installer), `repack_bundles.sh` (rebuild deploy zips from source), `earnkaro_check.py` (prove the EarnKaro API key converts — token claims + live probes + raw API bodies; `--plan --deal-url <link>` runs the bot's OWN pipeline and prints the exact link the channel will carry), `set_earnkaro_key.sh` (write/rotate that key into the server `.env`, pin the publisher from the token, restart, verify), `coverage_audit.py` (source-vs-channel coverage report + `--heal` re-queue of posts that never went out), `diagnose.sh` (deployed-fix markers + coverage in one command), `sync_identity.py` (regenerates the auditor's copy of the product-identity rule; `--check` is a test), `identity_probe.py` (ask, from the CLI, whether two posts are the same product on both services), `hypd_links.py` (list/learn OUR HYPD share links and look up which product they cover), `test_all.sh` (one-command offline regression gate, also used by GitHub Actions), routing + media-fix notes |
| `archive/` | Original uploaded hotfix zip, kept for provenance |

## Quick checks (no credentials needed)

The complete offline regression gate runs the bot behavior suites, cross-service product-identity checks, the WhatsApp bridge self-test, and Python syntax checks. GitHub Actions runs this same command on every push and pull request.

```bash
# Local setup (once)
python3 -m venv .venv
.venv/bin/python -m pip install -r bestgaa/requirements.txt
npm install --prefix tg-wa-bridge --no-package-lock --ignore-scripts

# Run all offline checks (no Telegram, WhatsApp, or affiliate credentials required)
PYTHON=.venv/bin/python ./ops/test_all.sh

# On the deployed server: prove what is live
python3 ops/deploy_and_verify.sh --verify-only

# Deploy from a checkout WITHOUT fearing a dropped SSH (2026-10-04: two drops
# killed the deploy mid-suites). Runs deploy_fresh.sh inside tmux, logged:
./ops/deploy_stable.sh            # watch: tmux attach -t bestgaa-deploy
./ops/deploy_stable.sh --status   # or: ./ops/deploy_stable.sh --log

# Audit a real or copied database (read-only; --strict exits non-zero on findings)
python3 ops/quality_audit.py --db bestgaa/state/bot_state.sqlite3 --limit 200 --strict

# "idi manadenaa link?" - one URL in, one verdict out (no database needed)
python3 ops/quality_audit.py --publisher 5478322 --tag "" --check-url "<url>"
```

To run an individual behavior suite, use `python3 test_render_job.py`,
`python3 test_rescan.py`, `python3 test_pipeline_fixes.py`,
`python3 test_best_copy.py`, `python3 test_duplicate_sim.py`,
`python3 test_earnkaro_conversion.py`, `python3 test_hypd_links.py`, or
`python3 test_line_fidelity.py` from the repository root. The bridge contract
can also be run directly with `cd tg-wa-bridge && TELEGRAM_BOT_TOKEN=x
WA_PHONE=919876543210 WA_CHANNEL=x@newsletter node bridge.js --self-test`.

The Python module requires `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`EARNKARO_API_KEY` and `AMAZON_TAG` at import time — it exits with
`Missing required environment variable` until a real `.env` exists. The offline
test suites set dummy values for their isolated checks; see deployment below for
live credentials.

## Deploying to the server (services OFF until you approve)

The operator-controlled rollout used for the PR #15 server update ("mundu
backup, dashboard secure, bot services OFF, approval tarvata posting"):

```bash
./ops/deploy_r15_keep_off.sh --dry-run     # print the plan
./ops/deploy_r15_keep_off.sh               # backup -> deploy -> dashboard -> verify (posting OFF)
# ...review the verify panel, open the dashboard over an SSH tunnel...
sudo bash ops/approve_posting.sh --i-approve-posting   # ONLY then posting starts
```

`ops/backup_live_state.sh` snapshots the live DB, `channels.json`, the Telethon
`*.session`, and the WhatsApp `auth/` + `bridge-state*.json` into
`~/bestgaa-backups/pre-r15-<stamp>.tar.gz` (mode 600 + sha256). The deploy runs
with `BESTGAA_KEEP_OFF=1`, which leaves `bestgaa` and `tg-wa-bridge` stopped
**and disabled** (reboot-safe OFF). `ops/install_dashboard_secure.sh` generates
the `DASHBOARD_TOKEN`, serves the dashboard on `127.0.0.1` only, and routes its
restart button through `/usr/local/sbin/bestgaa-restart-gate`, which refuses
until `/etc/bestgaa/POSTING_APPROVED` exists. Full steps: `ops/DEPLOY_R15_RUNBOOK.txt`.

## Deploying to the server (one command, fresh)

```bash
git pull                       # main has everything now
./ops/deploy_fresh.sh          # tests -> bundles -> deploy -> restart -> teach -> prove -> report
./ops/deploy_fresh.sh --dry-run      # print that plan, change nothing
./ops/deploy_fresh.sh --verify-only  # deploy nothing; only prove + report
./ops/deploy_fresh.sh --no-tests     # skip the pre-ship suites (faster)
```

It chains, in order: `ops/deploy_and_verify.sh --with-tests` (suites → repack →
deploy both services → restart → sha256 + boot-version proof), then
`ops/hypd_links.py` with configured HYPD candidates (it stores only links whose
destination is verified Meesho), then `ops/earnkaro_check.py` (live: the
EarnKaro key pays US, and verified Meesho HYPD links round-trip through Bitly),
then `ops/conversion_report.py`
(per-route verdict from the live DB + log). Every line it prints is `OK` or
`CHECK`, and every `CHECK` names its own fix; exit code 1 if anything needs
attention. Re-running it is always safe.

## Deploying to the server (manual steps)

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

## EarnKaro conversion, the API key, and the Amazon policy (2026-09-24)

The user reported that links were **not converting perfectly** and supplied a
fresh EarnKaro/Affiliaters API token. Three things were wrong or missing, and
all three are now covered by `test_earnkaro_conversion.py`:

1. **The request was incomplete.** The public converter is called as
   `POST https://ekaro-api.affiliaters.in/api/converter/public` with
   `Authorization: Bearer <token>` and a body of
   `{"deal": "<clean merchant url>", "convert_option": "convert_only"}`.
   `convert_option` was omitted, which let the account-level default decide the
   *response shape*; the bot then failed to read answers that were not a bare
   URL. It is now sent explicitly (and is itself configurable through
   `EARNKARO_CONVERT_OPTION`).
2. **Every response shape is read.** `data` arrives as the link, as the whole
   deal text with the link inside it, as a list, or as an object of link
   fields; failures carry a sentence (`"Url not found in post!"`) with
   `success: 0`. `parse_earnkaro_response()` accepts all of them and, when it
   cannot, logs the API's own words — an unreadable answer is a **lost
   commission**, never a lost post.
3. **The key is set up properly.** The token is a JWT whose payload names the
   account (`earnkaro: 5478322`). The bot decodes it at startup and rejects a
   Flipkart URL carrying somebody else's `affExtParam2`; a refused token
   (HTTP 401/403) is logged as `EK AUTH`.

**Important: the token alone does not select the affiliate network.** The
Affiliaters account must also have EarnKaro connected under [Networks](https://docs.affiliaters.in/settings/affiliate-settings/networks),
and the [Selections](https://docs.affiliaters.in/settings/affiliate-settings/selections)
for Amazon, Flipkart, and Others must point to a network that supports that
store. Save those settings in Affiliaters. Unsupported stores/campaigns can
still legitimately return no link; the bot cannot manufacture a campaign.

```bash
# On the server — set/rotate the key (never printed by the script):
cd ~/bestgaa-bot/bestgaa-bot   # or wherever ops/ lives
./set_earnkaro_key.sh '<fresh-token>'
python3 ops/earnkaro_check.py --offline

# Diagnose the ACTUAL URLs that failed to convert (repeat --deal-url as needed):
python3 ops/earnkaro_check.py --skip-hypd \
  --deal-url 'https://www.flipkart.com/real-product-url' \
  --deal-url 'https://www.amazon.in/dp/REALASIN'

# Stream distinct outcomes; no token is logged:
journalctl -u bestgaa -f | grep -E 'EK SUCCESS|EK MISS|EK AUTH|EK REJECT|EK HTTP|EK NETWORK|EK FALLBACK|UNMONETIZED'
```

`EK SUCCESS` means an attributable affiliate output passed validation;
`EK MISS` means there was no usable output (including a no-link response or an
API source echo); `EK REJECT` means an extracted output failed attribution
validation. An Amazon
native-tag fallback is logged as `EK FALLBACK` and is **not** counted as an
EarnKaro conversion. `UNMONETIZED LINK` still marks deals posted with a clean
merchant URL and no affiliate attribution.

`ops/EARNKARO_KEY_AND_SOURCES_2026-09-24.txt` is the same thing as a runbook
(what to run on the server, in order).

### Fresh key (2026-10-04): prove the EXACT link the channel will carry

The token currently in this checkout is `ops/.earnkaro_key` (**gitignored**,
never committed): it decodes to EarnKaro publisher `5478322`, issued
2026-10-04 05:20 UTC. `ops/apply_dual_hotfix.sh` writes exactly that file into
the server `.env`, and `ops/earnkaro_check.py` now also *reads* it when no
`--key`/`--env-file`/`bestgaa/.env` provides one — so the key a deploy would
use is the key a check tests.

Asking "will this link convert, shorten and pay us?" about a real source link
used to need two tools and some faith. One command now answers it by running
**the bot's own pipeline** (`bestgaa/main_bot_new.py`, not a re-implementation
of its rules: resolve → convert → shorten), and prints the link the post will
carry:

```bash
python3 ops/earnkaro_check.py --plan --deal-url 'https://www.flipkart.com/real-product-url'
python3 ops/earnkaro_check.py --plan --multi --deal-url '<link 1>' --deal-url '<link 2>'   # a list post
```

```
[Actual post URL 1]
  source    : https://www.flipkart.com/real-product-url
  resolve   : https://www.flipkart.com/real-product-url?pid=...
  posted as : https://ekaro.in/enkr...
  long link : https://www.flipkart.com/...
  length    : SHORT LINK on ekaro.in (our short domain; never re-shortened)
  attribution: affExtParam2=5478322 -> https://www.flipkart.com/...
  verdict   : OURS - this post pays our EarnKaro account 5478322
```

The verdicts are deliberately blunt: **UNMONETIZED** (no owned affiliate link
was produced, so strict-all skips that product/post; older database rows may
still appear in historical reports), **NATIVE AMAZON TAG** (our Associates tag
is the earning attribution and is verified separately from EarnKaro), **WRONG
ACCOUNT** (it pays somebody else), **OUR HYPD creator-store link** (store 93944,
Meesho only), and a length line that admits when a cosmetic shortener was
unavailable while the verified affiliate destination remained intact. Exit code
1 if any link needs attention; every line names its own fix. The plan run uses a
throwaway queue database and points `EARNKARO_API_KEY` at the key under test, so
it can never disturb live state.

The plan mode itself is pinned by `test_earnkaro_conversion.py` (fake client, no
network): the link it prints, the account it proves, the UNMONETIZED, native-tag
fallback and WRONG ACCOUNT verdicts, the HYPD case, and that a transient API
failure is reported as *retryable* rather than as a deal that earns nothing.

### "Mana links perfectga post chesthunda?" — one number, from data (v18.4/v18.5)

Two tools answer it using the same strict proof model: native Amazon/EarnKaro
attribution, exact `owned_link_proofs` records, or verified HYPD-to-Meesho mappings.
A `link_cache` row or shortener hostname alone is not proof; the publisher ID comes
from the token / `ops/.earnkaro_key` when available.

```bash
# every link in the last N posts, classified; --strict exits 1 on any finding
python3 ops/quality_audit.py --db bestgaa/bestgaa.sqlite3 --limit 200 --strict
#   QUALITY AUDIT | db=… posts=N findings=0
#     LINK PERFECTION | links=N our-short=N our-publisher=N unattributed=0 clean-merchant=0 FOREIGN=0
#     every published link has verified owned attribution; no unmonetized or foreign links reached a channel

# the live window, next to the per-route verdict
python3 ops/conversion_report.py
#   LINKS    : VERIFIED - N published link(s) all have our account attribution
```

`our-short` = an exact link backed by a durable converter/shortener proof or a
verified HYPD mapping; hostname and `link_cache` presence alone never qualify.
`our-publisher` = an Amazon link carrying our exact configured tag or a supported
native EarnKaro publisher ID, `unattributed` = a **Flipkart/Myntra page with no
publisher ID** (strict-all skips the post), and `clean-merchant` = a clean-looking
merchant page with no earning proof (strict-all refuses it just like any other
unowned link). `FOREIGN` = a source short link, foreign affiliate tag/ID, family
promo or other unverified wrapper. An otherwise familiar `bit.ly` URL is refused
unless its exact URL has durable proof; a cache row by itself is not enough.

**Why this changed:** hostname allowlists and bare `link_cache` rows can falsely
claim an affiliate URL earns for this account. Strict-all now requires persisted
proof for minted links and exact native attribution for merchant links; unsupported
products are skipped instead of sent with a clean-but-unmonetized URL. Both
`test_best_copy.py` and `test_earnkaro_conversion.py` pin the positive and negative
cases, including the CLI's summary and exit code.

**And when the converter answers on a domain this build has never seen** — an
EarnKaro/Affiliaters redirect the bot cannot name — the deal is no longer
written off as unmonetized: the output is followed and accepted only when its
destination proves it (`EK VERIFIED`), and refused when it leads back to the
source, to a foreign shortener, or to a foreign `affExtParam2`. The negatives
are pinned too (see `test_earnkaro_conversion.py`, section 2b).

**Amazon — Associates APPROVED (2026-10-04).** Our own tag pays in FULL now, so
`AMAZON_DIRECT_ASSOCIATES=true` (default) publishes the **native tagged link
first**: `amazon.in/dp/ASIN?tag=mama086-21`, shortened with our own shortener
(`AMAZON LINK SHORTENED`), recorded in `_short_to_long` so the channel under
Amazon review still expands to the direct tagged product page at delivery. No
network cut is taken. `AMAZON_VIA_EARNKARO=true` keeps EarnKaro as the **second
chance** for an Amazon shape the native path cannot build (no usable ASIN), so
no Amazon deal loses its monetization. `AMAZON_VIA_EARNKARO=false` means "never
EarnKaro for Amazon", and `AMAZON_DIRECT_ASSOCIATES=false` restores the
2026-09-24 network-first order (EarnKaro first, native tagged link as the
fallback). A cached network row for an Amazon source is upgraded to our own
tagged link (`AMAZON CACHE UPGRADED`) instead of being served for its full
14-day life.

**"Anni perfectga convert chesthunnava ledaa?" — answer it from DATA**:**

```bash
python3 ops/conversion_report.py            # last 24h: config, routes, log markers, verdict
python3 ops/conversion_report.py --hours 72
python3 ops/conversion_report.py --json     # for cron/alerting
```

It reads the bot's own database and log and prints route health based on
separate `EK SUCCESS`, `EK MISS`, `EK AUTH`, and `EK REJECT` markers. EarnKaro can
report `WORKING`, `PARTIAL`, `NOT CONVERTING`, `NEEDS REVIEW`, or `IDLE`; it no
longer counts every `EK CONVERT` line as a failed conversion. Amazon's native
fallback count is shown separately and is never labelled EarnKaro commission.
It also classifies links in the window (`passthrough` identifies legacy clean-merchant
rows, which strict-all refuses to publish) and ends with `NEEDS ATTENTION` + suggested fixes. Exit code 1 when something needs
attention, so a cron job can alert on it.

**Prove it in one command** (on the server, which has internet):

```bash
python3 ops/earnkaro_check.py            # token claims + live probes
python3 ops/earnkaro_check.py --offline  # token only, no network
```

A healthy run prints `EarnKaro publisher: 5478322 <- every converted link pays
THIS account` and then `MONETIZED - PAYS US (5478322)` per store: the returned
short link is **expanded** and its visible attribution checked, so "HTTP 200" is
never mistaken for "our link". The same command then proves the OTHER route too —
it resolves OUR hypd share links, Bitly-shortens them exactly like the bot, and
follows the short link back to our store (`--hypd-only` runs just that part). `WRONG ACCOUNT` means the token belongs to
somebody else (or the response echoed someone else's attribution) - replace the
key with `./ops/set_earnkaro_key.sh '<token>'`. A store with no campaign is
normal and posts a clean link; Meesho is covered by OUR HYPD link (see
the next section).

**Three new first-preference sources** (2026-09-24, "e three channel source ga
pettu 1st preference ivvu"): `t.me/+O3j4ghbtJzhjZjJl`,
`t.me/+8KzU3P58MJ9jN2M1`, `t.me/+6LA1ljXGlbNmMjA1`. They fan out to every
non-Tricks main target (Under-99 / Under-499 / card / Premium / review routes
layer on by price as for every other source) and are FIRST PREFERENCE: claimed
from the queue ahead of everything else and given the +1 priority boost, so an
equivalent deal from one of them is rendered and delivered first. Invite hashes
carry uppercase characters, so source matching is normalised
(`normalize_source_name()`), which is also what the claim SQL's `lower(source)`
already did.

## OUR HYPD link: Meesho only (store 93944)

HYPD is configured to use **our** creator-store ID `93944` (`smartdeals`) for
verified Meesho products only. Shopsy is deliberately excluded. An HYPD share
link is learned or posted only after its destination is verified as a Meesho
product page; old Shopsy mappings are ignored and removed when encountered.

**What a HYPD share link is.** It is not a wrapper to be unwrapped: the
attribution lives in the token itself (`affid=infhypd`, `affExtParam1=<our HYPD
account>`, `affExtParam2=<the token>`), so resolving/replacing it throws the
commission away. It is a **final, monetized link** and is treated like one.

Behaviour (pinned by `test_hypd_links.py`):

| Situation | What the bot does |
|---|---|
| A source posts our `hypd.store/93944/afflink/…` link and it resolves to Meesho | Published as **our** link: never stripped, not sent to EarnKaro, and Bitly-shortened |
| A bare Meesho product has a matching, verified HYPD mapping | The bot swaps in our curated Meesho HYPD link and shortens it |
| A Meesho product has no known HYPD mapping | It is recorded as `HYPD MISSING` / `HYPD WANTED`; strict-all skips the product/post until a verified link is curated |
| Shopsy, or an HYPD link whose destination is Shopsy | **Never** routed through HYPD; it follows the ordinary affiliate path and is skipped if no owned link can be verified |
| An HYPD link has an unknown destination | Not learned or published as a Meesho link; the tool reports that it could not verify the merchant |
| Another creator's `hypd.store/<other-store>/afflink/…` | **Never** published as ours; it would pay them |

**Teaching the bot your HYPD links.** This repository does **not** implement
an automatic HYPD link-creation API. Create an original share in your HYPD
creator app for the product, then teach the bot that exact link. Never substitute
a store ID into somebody else's token. These are operator commands, not steps
run automatically by this change; live learning requires approval:

```bash
python3 ops/hypd_links.py                                   # what the bot knows
python3 ops/hypd_links.py 'https://hypd.store/93944/afflink/<token>' ...  # learn these live
python3 ops/hypd_links.py --wanted                          # products still earning NOTHING (curate these)
python3 ops/hypd_links.py --lookup 'https://www.meesho.com/.../p/...'     # is this product covered?
python3 ops/hypd_links.py --dry-run 'https://hypd.store/...'              # resolve, store nothing

# and the live end-to-end proof of the whole route (ours -> Bitly -> back to ours):
python3 ops/earnkaro_check.py --hypd-only
```

Learning one link maps the **product** (`hypd_links` table: token → afflink,
merchant page, product key), so every later post of that product — however the
source spells the URL (with or without HYPD's tracking parameters) — can use
our link when the same-product redirect check succeeds. Actual payouts depend
on the platform's attribution and eligibility rules. Merchant pages are stored clean (tracking stripped) so a bare link
matches.

Pinned HYPD scope: `HYPD_STORE_ID=93944`, `HYPD_STORE_SLUG=smartdeals`,
`HYPD_ALWAYS_BITLY=true`, and `HYPD_MERCHANT_DOMAINS=meesho.com` (Meesho only;
Shopsy is excluded). Reserved `HYPD_API_URL`/`HYPD_API_TOKEN` are unused;
no automatic share-token creation is implemented. The bridge mirrors the store ids via `HYPD_STORES`
(`93944,smartdeals`); for direct-source posts it verifies Meesho destinations,
blocks foreign/unknown HYPD destinations, and does not publish clean Shopsy
fallbacks in strict mode.


### Category evidence and HYPD hardening (2026-10-14)

- **Categories are evidence-based, not an AI confidence guarantee.** Shared
  taxonomy v3 distinguishes product phrases such as `saree organizer` → Home,
  `laptop backpack` → Bags, and `green tea face wash` → Beauty. Independent
  conflicting items still withhold the category (e.g. `saree and saree organizer`).
  Unknown/mixed evidence is not guessed into a category channel. Existing
  identity, price/link-association, routing and duplicate gates still apply.
- URL evidence excludes opaque shortener/share tokens and IDs after `/p/`,
  `/dp/`, `/product/` and `/item/`. Separate path/query fields cannot manufacture
  a product phrase (`/rice?q=cooker` is not evidence for “rice cooker”).
  Classification does not fetch merchant HTML. Search-word evidence alone
  does not satisfy the separate publishable-product identity gate.
- **Meesho needs an identified product and our original HYPD share.** Shares
  must have the exact HTTPS host/path shape, current configured store, and a
  product-key-consistent stored mapping. Learned mappings, including cache
  hits, get a fresh redirect check before bot conversion. Changed/unverifiable
  destinations are withheld; stale mappings are invalidated. Missing products
  remain in the curation queue, not an unrelated EarnKaro fallback.
- A Bitly/is.gd URL needs **its own exact short → HYPD share proof**. Another
  mapping for the same product does not establish ownership of an arbitrary
  short URL. Telegram, WhatsApp DB provenance and the quality auditor reject
  foreign, stale-account, inconsistent-product and unproven legacy records.
  Old HYPD shorts without exact proofs are intentionally withheld until
  reconverted through the verified route. No blind backfill is performed.
- HYPD wrapper parsing rejects HTTP errors and multiple product destinations;
  merchant HTML is not inspected. Store configuration is an operator assertion,
  not independent verification of account access or actual commission payout.

Offline validation: `PYTHON=/path/to/venv/bin/python bash ops/test_all.sh`;
142 Python/JavaScript category parity cases, HYPD regression tests, bridge proof
matrix, existing routing/dedup/one-hour/WhatsApp checks and syntax/lint gates.
**No production deployment, restart, live posting or commission verification**
is part of this change. Production must remain OFF until explicitly approved.

### Further reliability audit (2026-10-14)

This pass strengthens failure handling, not a claim of 100% correctness:

- **Category boundaries:** URLs, real/encoded line breaks and separate query
  fields cannot join words into a synthetic product phrase. Invalid encodings,
  credentials and parser-dependent dot-segment URLs contribute no category
  evidence. A separate suite adds 274 adversarial/metamorphic cases to the 142
  existing Python/JavaScript parity cases, including repetition invariance.
- **Conflicting HYPD records:** final Telegram render/send gates, WhatsApp
  provenance and the auditor reject inconsistent cached product pairings for
  the same share/short URL. They cannot pick an arbitrary first/latest row or
  rescue a HYPD conflict using unrelated native-tag evidence. The bridge reads
  one read-only SQLite snapshot; a missing DB fails closed instead of creating it.
  Bot startup also creates an affiliate-URL cache index for these lookups; the
  regression suite checks that SQLite uses it. No live index migration ran here.
- **Honest delivery reporting:** `conversion_report.py` separates `rendered_links`
  from `links` belonging to queues with recorded successful sends. `delivery_summary`
  reports complete target sends, policy skips (including old `sent` + error rows),
  partial target sends and queues without send evidence. Missing/legacy delivery
  schemas do not imply success. Partial sends require receipt inspection, not
  blind replay. Links are counted once per queue, not multiplied by target count;
  these are saved renders, not a reconstruction of final per-target text. The
  report window follows queue creation time, not live Telegram receipts.
- **Diagnostic safety:** HYPD probes require an identified Meesho product,
  reject error pages/ambiguous wrappers and do not inspect merchant HTML.
  Shortener return checks require the exact original share, not a store-name
  substring or another token in the same store. An unobservable redirect chain
  is inconclusive, not proof of ownership. Live probes may create external
  short links; `--plan` can write bot caches. They were not run live here.
- **Test isolation:** `ops/test_all.sh` always overrides inherited database,
  log, media and Telegram-session paths, and clears the inherited channel-config
  path. Both its isolation block and a complete gate run were checked with
  sentinel operator files; those files remained unchanged.

Verification: the complete offline gate, 116 HYPD checks, six diagnostic safety
regressions (with subcases), dashboard DOM suites, syntax/lint and deploy bundle
source parity. Rebuild ignored bundles with `bash ops/repack_bundles.sh`.
Production remains OFF; no deploy/restart, live sends or affiliate payout tests
were performed. Existing one-hour/per-channel duplicate rules are unchanged.

Server runbook (steps, verification, knobs): `ops/HYPD_OUR_LINKS_2026-09-24.txt`.

**Nothing earns nothing silently.** A Meesho product we have no HYPD
link for is counted and listed (`HYPD MISSING`, `HYPD WANTED`, then
`ops/hypd_links.py --wanted`), so the fix — curate it once in the HYPD app — is
one command away. Learning a link removes it from that list automatically.

Log markers: `HYPD LINK` (converted, with the Bitly URL and the page behind it),
`HYPD MISSING` / `HYPD WANTED` (product needs a HYPD link; on the to-do list),
`BITLY unavailable; posting OUR HYPD link as it is` (outage fallback).

## Key behaviour (see `ops/` notes for full detail)

> **Historical release notes:** the version entries below describe behavior at the time
> of each release. The current strict monetization policy at the top of this file
> supersedes legacy acceptance of clean merchant pass-throughs, family/channel promo
> URLs, and other non-earning links; those are now refused or skipped.

- **Post-deploy repeats fix (2026-10-11):** right after a deploy some queues
  posted up to three times to the same target. The delivery checkpoint (a ledger
  write) sat *inside* the Telegram send-retry loop, so one locked-database
  moment re-sent an already-sent chunk instead of retrying the write. The
  checkpoint now runs once per sent chunk *outside* the loop (a failure logs
  `CHECKPOINT FAILED` and never re-sends), ledger writes absorb transient
  `locked`/`busy` contention with their own retry (`sqlite3` `timeout=30s`),
  policy skips are stored as `status='skipped'` (terminal like `'sent'`, never
  retried) instead of `'sent'` with the reason in `last_error`, both audits
  count only empty-reason `'sent'` rows as real sends (`SENDS vs SKIPS` in
  `quality_audit.py`; skip-aware `posted`/`dedup` in `coverage_audit.py`),
  strict-all dedup keys on verified product identity only and skips a post with
  no trustworthy identity as a unit, and only a real send blocks an edit
  revive. Pinned by `test_postdeploy_repeats.py` in the offline gate.
- **Category shelves (2026-10-13):** a dashboard channel can be a themed shelf
  - tick Electronics (or Clothing, Footwear, Home & Kitchen, Beauty, Grocery, Bags & Accessories)
  and it keeps ONLY posts classified into those categories, from every main
  source or selected ones. Enforced at delivery time in `render_job`, pinned
  by shelf tests in `test_dashboard.py`.
- **Operator dashboard (2026-10-13):** add posting channels from a browser
  instead of editing code - name, sources (or every main source), category
  shelf, Amazon-tag
  opt-in, enable/disable/remove. Channels live in `channels.json` next to the
  bot database (a redeploy never wipes them) and apply on the next bot restart;
  the dashboard shows a pending badge plus one-click restart when allowed.
  Strictly additive (new names only, built-ins untouched), the tag opt-in
  warns that Associates Central must list the channel first, and the same page
  shows queue depth, today's sends-vs-skips, recent jobs, and the live quality
  audit. `DASHBOARD_TOKEN='...' python3 -m dashboard.app`; localhost + SSH
  tunnel only, never public. Pinned by `test_dashboard.py` in the gate.
- **Same-product repeats, advanced guard (2026-10-12, user rule "1 hour lopala
  malli post cheyoddu"):** the per-channel repeat guard now matches ANY of the
  post's identity keys - the headline key plus the exact link identities
  behind its URLs (merchant PIDs, same-link hashes) - so the same product in
  different words across sources still matches and waits. Inside the 1-hour
  cool-down the repeat is ALWAYS skipped, even a cheaper copy; past it a
  MEANINGFULLY better copy still posts (a real ≥5% price drop, or a deeper
  discount at a lower price - token wobbles, same-price MRP-math percent
  bumps, and higher prices never repost). Past the 24h window a genuine
  re-offer posts again. Separately, strict mode re-verifies EVERY link of the
  final per-target text right before the send (`PRESEND LINK GUARD`): a source
  short link, foreign tag, or clean merchant page stops that target instead of
  posting, so their links never reach a channel and no commission is missed.
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
- **v18.6 — an Amazon SHORT link is a wrapper, never a post; and Amazon links are short
  in the channel.** User report (2026-10-04): *"https://amzn.to/4dnF9lU?tag=mama086-21 …
  IDI OPNE CHESTHE VERE VALLA LINK VASTHUNDI … LONG LINK lo MARCHI SHORTEN ga CHESI POST
  CHEYALIGAA"*, plus a raw `amazon.in/…?tag=dv12399-21`. An `amzn.to` code belongs to
  whoever created it — our tag glued on the end does not change where it goes (their
  attribution travels in the redirect) and we earn nothing. Now: `is_amazon_short_host()`
  marks amzn.to/amzn.in/amzn.eu/a.co as wrappers, the link is resolved to the long
  amazon.in product page, re-tagged with OUR Associates tag and **shortened with our own
  shortener** (`SHORTEN_AMAZON_LINKS=true`, applies to single deals too — the compact
  tagged page is the fallback when the shortener is unavailable); `cut_amazon_short_links()`
  runs at delivery so a post rendered by an older build still cannot publish a short code
  (the link is cut, the deal goes out, the log says `AMAZON SHORT CUT`); the bridge resolves
  or BLOCKS every Amazon short link before the policy gate (`prepareAmazonShortLinks()`, an
  unresolved one is a permanent refusal) and shortens the ones carrying our tag. A stranger's
  tag never survives the rebuild, and only a link that really carries OUR tag may be shortened
  (cloaking an untagged page stays refused). The review channel still sees the direct tagged
  product page: every short link is registered and expanded back on that one target.
  Two more commission holes were closed in the same pass: an **HYPD share link that turns out
  not to point at Meesho** used to publish its verified merchant page BARE — it now carries our
  publisher id when the page accepts one (`LINK ATTRIBUTED | HYPD out of scope …`); and the
  **WhatsApp bridge** now stamps a bare Flipkart/Myntra page found in a *bot-fed* post (a row
  rendered before v18.5, or the degraded-post path) at display time
  (`attributeBareMerchantPages()`, `LINK ATTRIBUTED | bare merchant page … not free`), while
  leaving our own links, a stranger's id, our own short links and Meesho pages untouched.
  Deploying both runtimes is one command — `ops/repack_bundles.sh && ops/apply_dual_hotfix.sh`
  restarts `bestgaa` **and** `tg-wa-bridge` and refuses to claim success unless the live
  `bridge.js` sha256 equals the bundled one.
- **v18.5 — a link that opens but pays nobody can no longer leave the building.**
  User report (2026-10-04): *"CHALA VARAKU MANAM MISS AVUTHUNNAM COMMSION ASALU
  RAVATLEUD … PRODUCT OPEN AVUTHUNDI KAANI ADI MANA LINKS KADU"* — the page opened and
  the commission never came. Two paths published a BARE merchant page when the network
  answered "no campaign": `keep_passthrough()` in the bot (Telegram channels) and the
  bridge's direct-source safety-net jobs (WhatsApp). Bare = nobody's id = worth zero.
  For the **Flipkart family** the network's own attribution parameter works on the
  product page itself — `affExtParam2=<publisher>` is exactly the shape the EarnKaro
  converter returns for this account — so both runtimes now **stamp our publisher id**
  on the resolved page (`attributeWithOurPublisher()` in the bridge,
  `attribute_with_our_publisher()` in the bot), replacing any stranger's id instead of
  carrying it. Amazon keeps its tag, Meesho keeps HYPD, other stores keep the clean-page
  policy; hosts are `EK_ATTRIBUTION_HOSTS` (`flipkart.com,myntra.com`, extend only after
  EarnKaro confirms a store) and the stamp can be turned off with
  `ATTRIBUTE_PASSTHROUGH_LINKS=false`. The auditor grew a matching bucket and finding:
  `unattributed=N` counts the old posts that opened and paid nobody
  (`can carry our publisher id (…) but was posted without it`), so the live DB shows how
  many clicks were lost and the number stops growing once v18.5 runs. Logs:
  `LINK ATTRIBUTED | queue=… the product page is posted WITH our publisher id (5478322)`.
- **v18.4 — "mana links perfectga post chesthunda?" is now a number, and a new network
  domain can no longer cost a commission.** Two changes, one on each side of the
  question "does the post carry OUR link?":
  1. **The bot verifies an unrecognized converter output instead of refusing it.**
     `earnkaro_output_kind()` can only call a link ours when it recognizes the network's
     short domain, and Affiliaters adds and renames them. An unknown one used to be
     `EK REJECT` + an unmonetized post — a lost commission that looks *exactly* like
     "this store has no campaign". A short-link-SHAPED output is now followed
     (`EK VERIFIED`) and accepted only on evidence: its destination must be a real store
     page (or carry OUR publisher id / our Amazon tag), and it is still refused when it
     leads back to the source URL, to a foreign shortener, or to somebody else's
     `affExtParam2`. A bare merchant-page echo keeps its old treatment (`looks_like_shortener()`
     is the shape test `is_unresolvable_short_link()` now delegates to, so nothing else moved).
  2. **Historical v18.4 behavior — superseded by the strict proof model above.**
     The quality auditor stopped treating every `bit.ly` URL as foreign and reported
     link ownership using the then-current cache/publisher model. That cache-row-only
     part is no longer valid: current ownership requires native attribution, an
     account-bound proof record, or a verified HYPD-to-Meesho mapping. The report's
     current verified/unowned semantics are documented in the section above.
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
  printed (URL digits are masked on both sides, so our own tag `mama086-21` is
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
  back-to-back fan-out (`TARGET_FANOUT_GAP_MIN`/`TARGET_FANOUT_GAP_MAX` default
  0/0 — zero artificial waiting; the knobs are only an escape hatch) and
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
- **Link safety (superseded by strict-all):** the earlier gate required an `affiliate_url` cache row. Current publishing requires account-bound durable converter proof or exact native attribution; a cache row/shortener hostname alone is never sufficient.

## Posting-immediacy / dedup knobs (`.env`, all optional, all clamped)

Defaults are the tuned values; a typo in `.env` cannot make the bot slow again
because every number is clamped into a safe range.

| Knob | Default | Meaning |
|---|---|---|
| `QUEUE_WORKERS` | 8 | concurrent render/deliver workers |
| `PRICE_DEDUP_SECONDS` / `PRICE_DEDUP_IGNORES_IDENTITY` | `0` / `false` | the same-price gate is **off** (a common price must never eat a real deal); set `3600` to restore the one-hour hold as a **fallback for posts with no ASIN/PID**, and `true` to also block a *different* product that happens to share an already-posted price |
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
| `STRICT_OUR_LINKS` | pinned `true` | publish only links verified to earn for this account; unsupported merchant/service links and unsupported items/posts are skipped |
| `PASSTHROUGH_UNMONETIZED` | legacy-only | cannot override strict-all; clean unmonetized merchant links are not published |
| `DROP_DEAD_LINKS` / `WA_DROP_DEAD_LINKS` | false | a link probe may make a post honest, never make it disappear: `true` refuses a post whose every destination answered as a confirmed dead merchant page (the pre-v18.0 behaviour) |
| `ADD_OUR_CHANNEL_LINK_TOP` | legacy-only | strict-all never injects family/folder promo URLs because they do not earn; the setting applies only when strict mode is explicitly disabled in isolated legacy tests |
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
> `arena/01a06dad-new-deals-bot-zip`; the hashes below describe the *deployed*
> build only. Deploy the repo source (`ops/repack_bundles.sh` →
> `ops/apply_dual_hotfix.sh`, or `bestgaa/deploy_bestgaa.sh` for the bot alone),
> then refresh this table with the new hashes.

| Artifact | SHA-256 |
|---|---|
| Hotfix zip (`archive/bestgaa_whatsapp_hotfix.zip`) | `569b7415c375de0fddc2a3d27653f11e3dd764ce4c7cb922a373de5936150128` |
| `bestgaa/main_bot_new.py` (= server `main_bot.py`) | `087d227516e4e9392a4efce8ce7da09f470428a56a0088adf804029c1b0294f6` |
| `tg-wa-bridge/bridge.js` (= server `bridge.js`) | `3faf9856dacd84e3f57347c7699ecd93c767d71d2b936fa11bcb4506ac2c5407` |

Current **repo source** on this branch (v19.0 — optional category-wise Telegram + WhatsApp
mirrors and strict-all link-provenance hardening; **not yet deployed to a server**).
The category classifier is shared by both services through matching
`category_rules.json` files and uses readable incoming/resolved URL slugs and
search terms as well as post text; opaque IDs alone and mixed/unclear products
stay on Main. It does not inspect merchant-page HTML. New Amazon category
channels remain tagless until explicitly declared. Strict-all accepts only
account-bound converter proof, exact native attribution, or the verified
HYPD-to-Meesho mapping; cache rows/shortener hostnames alone do not prove
ownership. Until `bash ops/deploy_and_verify.sh` is run on the host, live
channels still run the previous build. Hashes below are refreshed for this
source snapshot:

| File | SHA-256 |
|---|---|
| `bestgaa/main_bot_new.py (v19.0)` | `661555e234d3f5c93aa05d63c94c534728db38298a3feaba62d61b1b3326038e` |
| `bestgaa/category_rules.json` | `e957c89924db528c075b9d22d1e83ac948333afc1d7a738188c4b8edc7dfca81` |
| `tg-wa-bridge/bridge.js` | `01d9711beddd55fb938685052f6dc59e77db54ebadbe446b48b3f1a531bb16ae` |
| `tg-wa-bridge/category_rules.json` | `e957c89924db528c075b9d22d1e83ac948333afc1d7a738188c4b8edc7dfca81` |
| `ops/coverage_audit.py` | `98fa0cc3cb91575b5d57e65547352fe05883b58c8eb9423510373666af64a66f` |
| `ops/quality_audit.py` | `8ad8d347554bd794ffb305b7006bd8115a0792f48c258e0c485125666331e08f` |
| `ops/conversion_report.py` | `ea2655376f7697b4d6fb727f922459e723d710388122e2dc285db4a40206fd85` |
| `ops/test_all.sh` | `1a20ecae5161a130a27e1580e9dcd5ab731c411ac4a2468603090fd5db68de62` |
| `ops/repack_bundles.sh` | `8cceff22911b52dd9168137c4d7523e8f31c474c40f0a03b9d7742da37b2045f` |
| `ops/sync_identity.py` | `c26dbbf19a0673bba01ce0547972f5ab2150eea4b2fa1057c083bb561da172cd` |
| `ops/deploy_and_verify.sh` | `7b784afd12fc72c36b8c8f30a49700dc9612c8e4c4bb9f8497bd10a4bf99ae7` |
| `test_render_job.py` | `e583c796b093a786840900b02d858fb42a51e7a3d5229488b7ba56e29a5122ee` |
| `test_line_fidelity.py` | `e648cb84378191865257250a94d239141980d1634989e09953daacaf9b2b01fa` |
| `test_pipeline_fixes.py` | `cc042699c2cd8537b0522b4751d3260964a52554d35b2ce2b0339d4d220b71a4` |
| `test_duplicate_sim.py` | `de827a979773ee933037de16c8e1fe494e69f983c2e538ddd5e9e6f6f62b06bd` |
| `test_best_copy.py` | `c8f18d762262c0c92353f9270fba37b1a068cc8f5fed789a22363c81eaddcb57` |
| `test_earnkaro_conversion.py` | `771f22a147f1b9ce929ea75cdd5cf4ddb6fae1760b16665e525932f358b6eec0` |
| `test_hypd_links.py` | `71c8f3e6f8c44871a1ac92e26146936108b938a2dfe28d84e0a05a086406a229` |

**Offline verification on this source tree (2026-10-11):** `PYTHON=.venv/bin/python bash ops/test_all.sh` completed successfully. The gate covers category-taxonomy parity; 211 render/routing checks (including readable URL slugs, a shortened link resolved to a product slug, ambiguous/mixed items, and name-price-link fidelity); rescan/recovery, pipeline, best-copy, duplicate, EarnKaro, HYPD, and line-fidelity suites; the WhatsApp bridge self-test; plus Python compilation and JavaScript syntax validation. Its private SQLite bridge fixture exercises account-bound converter proof, cache-only rejection, wrong-account proof, current/stale Amazon attribution, and HYPD-to-Meesho mapping. The run is offline: it does not validate live affiliate credentials, live channel access, or a deployed service. No deployment or service restart was performed.

| Suite | Current coverage |
|---|---|
| `test_render_job.py` | Strict-all skips unsupported/unmonetized items; readable incoming/resolved URL clues route only confident products; mixed/opaque links remain unguessed; lists preserve each name, price, and correct link. |
| `test_earnkaro_conversion.py` | Converter response validation, identity-bound durable proof, native tag rotation, and the report/auditor's matching ownership rules. |
| `test_best_copy.py` | Durable proof and strict link-audit regressions, alongside product-copy and fidelity checks. |
| `test_duplicate_sim.py` | Real worker-path duplicate/recovery scenarios; fake converter output is persisted so the final provenance gate is still exercised. |
| `node tg-wa-bridge/bridge.js --self-test` | Bridge provenance and routing contract, including link-derived category routing and ambiguous-link refusal. |

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

### Strict category channels — rules v2

Create the destination Telegram channel first, give the bot/session account posting
permission, then **Dashboard → Guided setup → select the category**. This configures
routing; it does not create a Telegram or WhatsApp channel on those services.

| Picker label | Stable key | Examples |
| --- | --- | --- |
| Clothing | `fashion` | Kurtis, shirts, jeans, sarees, socks |
| Electronics | `electronics` | Earbuds, laptops, smartphones, USB chargers |
| Home & Kitchen | `home_kitchen` | Rice cookers, washing machines, bedsheets, shoe racks |
| Footwear | `footwear` | Shoes, sandals, slippers |
| Beauty | `beauty` | Skincare, hair conditioner, grooming |
| Grocery | `grocery` | Rice, flour, snacks, peanut butter |
| Bags & Accessories | `accessories` | Handbags, backpacks, wallets, jewellery |

**Migration:** the existing `fashion` key now means clothing only. Bags/wallets
move to `accessories`. Select both keys on a dashboard channel if you want its old
broader scope. Existing non-category/built-in routing is unchanged. Empty category
selection in stored configuration means unrestricted. The setup wizard requires
at least one category; the existing-channel editor offers an explicit unrestricted
mode with confirmation. Multiple selected
categories accept either category, but never authorize mixed-product roundups.

**Conservative, not infallible:** this is an offline evidence classifier, not a
merchant-catalog lookup or an accuracy guarantee. It abstains on unknown, weak,
mixed or conflicting evidence. It ignores opaque short-link tokens and tracking
parameters, caps repeated aliases, and suppresses nested words only at the same
occurrence: “shoe rack” is Home, but “shoes and shoe rack” is withheld. Compound
phrases distinguish rice cookers from rice and body butter from grocery butter.
Affiliate attribution, verified identity, duplicate cooldown and delivery checks
remain separate mandatory gates. No merchant HTML is fetched.

Use **Test a category before posting** to see a match/withheld reason and the text
or URL phrases supporting it. Preview never sends, fetches URLs or changes routing.
It uses the installed code; the running bot adopts changes only after an approved
restart. **Edit shelf** changes an existing channel's filter. Saved edits likewise
require restart. Malformed filters are rejected, not silently made unrestricted.
`CATEGORY WITHHELD` log lines explain dashboard targets removed during rendering.

WhatsApp uses the same rules and a parity-tested JavaScript engine. Configure
`WA_CHANNEL_<KEY>` for an existing WhatsApp Channel; dashboard channel CRUD is
Telegram-only. Built-in Telegram category destinations use
`TG_CATEGORY_<KEY>_TARGET`. Amazon-tag opt-in and channel approval requirements
still apply. Deploy both classifier modules with the bot/bridge (bundles include
them); changing only the main script is insufficient.

Run `PYTHON=/path/to/python bash ops/test_all.sh` for the complete offline gate,
including shared Python/JS classification fixtures, real render routing and
authenticated dashboard preview/config tests. Production must remain stopped
until deployment/restart is explicitly approved.

### Category test lab and safer editing

The dashboard now supports **bulk dry-runs** (up to 20 deals and 60,000 combined
text/link characters). Paste complete posts separated by a line containing `---`,
and choose their exact source. Or select JSON mode to supply per-deal sources,
resolved URLs and expected categories:

```json
[
  {"text": "Cotton kurti ₹499", "source": "deals", "expected_category": "fashion"},
  {"text": "Shoes and shoe rack", "source": "deals", "expected_category": null}
]
```

Results show category evidence, withheld reasons, optional expectation mismatches,
and **saved dashboard channel** checks. Filter withheld/mismatching cases or
download the report as JSON. Caption text is rendered as text, never executable
HTML. Tests do not fetch links, enqueue jobs, send posts or write configuration.

- **Candidate:** the saved category and source filters match. This is NOT approval
  to send: identity, attribution, duplicate, service-promotion, source-access and
  final delivery gates still apply. Built-in/WhatsApp destinations are not simulated.
- **Blocked:** disabled channel, category mismatch/withheld, or source excluded.
- **Undetermined:** source missing, or an older bot snapshot cannot confirm the
  wildcard main-source list. `*` excludes Tricks sources, just like the live merge.
- Reports include installed rules version, config revision and pending-restart
  status. Saved config can differ from the running bot. No classifier output is
  claimed to be a calibrated probability or guaranteed accuracy.

**Edit shelf** opens a checkbox editor. Strict mode requires a category;
unrestricted mode requires confirmation. The editor captures the config revision
and sends `If-Match`: an intervening save returns HTTP 409 instead of overwriting
newer filters. Close the editor, refresh and reopen to review the new state.
This guards clients using one dashboard process, not arbitrary external file writers.
Older API clients that omit `If-Match` remain compatible.

API: authenticated `POST /api/category-batch` accepts `{"items": [...]}`.
`POST /api/category-preview` also accepts `source` and returns channel checks.
Neither endpoint stores submitted captions. Downloaded reports may contain your
submitted deal text/URLs; keep them private as appropriate.

The full offline gate now includes batch/source parity and HTTP concurrency tests,
plus a syntax check of the actual served JavaScript. Optional DOM workflow test:

```bash
npm install --prefix /tmp/category-ui-test --no-package-lock jsdom
NODE_PATH=/tmp/category-ui-test/node_modules PYTHON=/path/to/python node test_dashboard_ui.cjs
```

Run the dashboard from the repository (`python -m dashboard.app`); bot/bridge deploy
bundles do not package the dashboard UI or its Python helpers. No production restart
or deployment is performed by these tests.


### Easy setup: link → category → channel

The redesigned **Channel Studio** opens with a three-step setup wizard:

1. **Category:** paste a product URL and click **Identify category**, or select a
   category card directly. A readable `wireless-earbuds` slug can suggest Electronics;
   `cotton-kurti` can suggest Clothing. **Use this category** selects that shelf.
2. **Channel details:** enter the username of a Telegram channel you already
   control and confirm posting permission. These are operator confirmations, not
   automated Telegram checks. Sources default to every main source; Amazon-tag
   opt-in defaults OFF. Optional settings are under Advanced.
3. **Review & save:** verify name, categories, sources and tag policy before saving.
   Saving config does not directly send a post, create a Telegram channel or restart the bot.
   On the upgraded running bot, new channels can activate live after access checks.
   Duplicate clicks are blocked. Stale config returns a conflict and preserves the
   form for review. Restart now also requires an explicit confirmation in the UI.

The inspector is **offline**: it reads only supplied URL/title text, never merchant
HTML or redirect destinations fetched from the network. An opaque ASIN/PID or an
`amzn.to`/`bit.ly` token cannot reveal the product category. The UI asks for the
**original source product title** or a **user-supplied resolved URL**, rather than
inventing a product name. Conflicting title/link evidence is withheld. A pasted
resolved URL is not verified redirect provenance, and classification is not proof
of affiliate ownership, identity or commission eligibility.

Authenticated `POST /api/inspect-link` accepts:

```json
{"url": "https://amzn.to/example", "title": "Cotton printed kurti", "resolved_url": ""}
```

Responses include a suggestion or `needs_details`/`withheld` verdict, evidence,
next-step guidance and existing enabled shelves for that category. Existing-shelf
matches are not delivery approval. Inspection never writes config or queue data.
URL/title inputs are bounded; non-web URLs and embedded credentials are rejected.

The responsive UI template is now `dashboard/index.html`, loaded by `dashboard/app.py`.
Copy the entire `dashboard/` folder when updating a standalone dashboard install.
The full offline gate includes inspector regressions; optional setup DOM tests:

```bash
NODE_PATH=/tmp/category-ui-test/node_modules PYTHON=/path/to/python node test_dashboard_setup.cjs
```

### Quick add + live activation (new destinations only)

**Public channel link → choose a category → select source channels → Save & request activation.**
The dashboard accepts `https://t.me/ChannelName`, `t.me/ChannelName`, or `@ChannelName`.
They normalize to the same username, so aliases cannot create duplicate config.
Private invite links, message links and non-Telegram links are rejected. Create the
channel first and grant the Telethon session account **Post Messages** permission.
Quick add defaults to every main source, one strict category, and Amazon-tag opt-in
OFF. The source library sits beside the channel details on desktop (below on mobile):
search the 60 bundled source entries by channel title/handle, select multiple
checkboxes, select visible search results, or clear selections. Selected chips and
counts remain intact across search changes and dashboard polling. Real Telegram
titles come from the running bot's access checks; unverified names are shown as
handles/private-source placeholders, never invented titles.

Paste a public source link, `@handle`, or private invite into **Add a source channel
link**. Use **Add & select sources**, or save with the pasted link still present;
both paths validate and include it. Invalid/unadded links block saving rather than
being silently ignored. Choosing a custom source while "every main source" is on
materializes the main-source selection plus the new link. At most 100 sources per
channel are accepted; excess input is rejected, not truncated. Sources, categories
and notes can also be reviewed in **edit sources & category** (existing policy edits
still require an approved restart). Declare new affiliate channels before opting their Amazon tag in via advanced
settings; otherwise strict affiliate policy may withhold Amazon deals.

A **one-time approved deployment/start of this upgraded bot** is needed to install
live activation. After that, a running bot checks config about every five seconds:

- Only NEW destinations are added live. Existing routing, queues and deliveries
  are not restarted or replayed. Eligible jobs rendered after activation may use
  the new destination; this is not historical backfill.
- New channels must resolve as broadcast channels and report creator/posting-admin
  rights. No test message is sent. Source/destination loops and duplicate channel
  IDs are refused. An already-joined new source can be activated live after its
  broadcast-channel type and session membership are checked. Public links and
  private invites are supported for sources, not for destination invites. **Join
  with the same Telegram account used by the bot session**, not a different
  personal account. New-source verification uses check-only requests, never
  ImportChatInvite/JoinChannel. Missing access stays pending/needs-attention and
  retries with backoff; a saved row is not proof of activation.
- Resolution runs separately from posting workers with an 8-second per-channel
  timeout, at most three channel/source lookup attempts per pass and 30-second failure backoff.
  Larger explicit selections are staged across passes; no partial source mapping
  is installed while remaining selected new sources are unchecked. Telegram
  FloodWait retry windows pause all new access checks for the requested period. A failing
  new destination does not stop existing destinations.
- New custom intake is **channel-local**. It feeds only explicitly selected
  destination(s), after their category filters; automatic main/price/premium/card/
  shopping fan-out is capped to that selection. Built-in source routes remain
  unchanged. Wildcard main-source selections do not silently inherit custom feeds,
  regardless of config order. Event ingestion and rescan recovery ignore messages
  before a new custom source's activation cutoff; no historical backfill is started.
- The source list is available even while the bot is OFF, via the bundled baseline
  catalog plus saved/snapshot sources. Catalog presence does not mean membership
  or a live subscription. Source titles and runtime state come from the snapshot.
- The config revision is checked again after the Telegram lookup. A save/remove
  during that lookup cannot accidentally activate obsolete configuration.
- **Edits, disabling and deleting already-active destinations intentionally need
  an approved restart. Until then the old live policy CONTINUES.** This protects
  existing queues from silently changed routing. The UI warns before these actions.
- Malformed/unreadable config preserves existing routes. Startup configuration
  still uses the existing startup merge; live permission preflight applies to new
  additions after startup. `active` means routing loaded, not a successful send or
  a guarantee about Telegram availability, prices, stock or affiliate eligibility.

The dashboard displays the watcher heartbeat and per-channel activation states:
`active`, `checking_access`, `needs_attention`, `waiting_sources`, `disabled`, or
`restart_required`. Status is only shown for the observed config revision and a
fresh heartbeat (under 40 seconds). Missing/stale heartbeat means **not confirmed**,
never "running". Saving does not start a stopped service. The preview demo has no
Telegram connection and correctly shows activation as unconfirmed.

`bestgaa/live_channels.py` is included in bot deploy bundles and checked by the
installer. New targets still pass existing product-identity, category, owned-link,
per-channel duplicate and delivery checks. Live product processing uses the existing
resolver/conversion pipeline, including resolved URL context for classification.
The dashboard inspector remains offline: it does not open merchant HTML, verify
stock/prices, or invent names from opaque links. Link resolution alone is not proof
of product identity or category accuracy.

Regression gate: `test_live_channels.py` covers atomic/idempotent additions,
permission failures, preservation of existing routes, deferred edits/removals,
malformed config, source/target aliases, backoff and configuration races. Production
is not restarted or redeployed by development tests.

### Source-selection verification (2026-10-14)

`ops/test_all.sh` includes `test_source_setup.py` (normalization, 100-source limits,
real isolated HTTP saves, atomic staged activation, startup access repair, aliases,
loops, rate limits and config races), `test_source_runtime.py` (catalog parity,
check-only membership, no joins/history, activation cutoffs and channel-local scope),
and render integration proving matching custom-source deals reach only their
selected shelf while wrong-category deals are withheld. Existing affiliate,
one-hour/per-channel duplicate, WhatsApp and dashboard suites remain enabled.

Optional DOM regression:
```bash
NODE_PATH=/path/to/jsdom/node_modules PYTHON=/path/to/python node test_dashboard_sources.cjs
```
It covers 60 named feeds, multi-select/search, paste-on-save, preserved selections,
per-form isolation, POST/PATCH payloads, revision conflicts, safe titles and focus.
Desktop/mobile Chromium checks used an isolated demo with masked private invites,
no Telegram connection and restart disabled. No production deployment or service
restart was performed. Bundles are regenerated from source, not auto-deployed.

### Duplicate-delivery hardening (2026-10-14; deployment requires approval)

Existing channel/source/category settings are unchanged. New-channel setup stays
**channel link → category → select joined sources → Save**. Saving configuration
still cannot start a stopped bot. The existing access checks, channel-local
source scope, affiliate ownership checks and strict category filters still apply.

This update closes additional **reproducible code paths**, not a diagnosis of an
unseen production duplicate:

- Telegram records a durable publication intent **before** each network send.
  A lost response, interrupted send or failed receipt write is held for review,
  not automatically retried. Confirmed permission/rate-limit rejections can retry.
- Durable per-target product claims prevent concurrent workers from sending the
  same product. The absolute one-hour cooldown is rechecked atomically at the
  final claim, including cheaper copies. A genuine better offer can still pass
  the existing policy after that hour.
- Confirmed chunks survive process restart independently of the in-memory
  settlement map. A saved delivery-layout fingerprint prevents a missing photo
  or changed formatting from shifting chunk boundaries during resume.
- Album fallback is allowed only if preparation failed **before publication**.
  A lost album-publication response never triggers a second album/single-photo copy.
- Every product in a roundup participates in per-target dedup: the previous
  seven-key/twelve-URL truncation is removed. Known www/mobile/root merchant-key
  aliases also match legacy Telegram history without changing the product ID.
- WhatsApp dedup now uses verified provenance/resolved/HYPD destination IDs
  even when the displayed URL is a different short code (while retaining old
  exact-wrapper keys). No destination fetch or merchant HTML is needed for this
  identity check.
- WhatsApp uses persisted write-before-send intents and per-target product fences
  for text, media and groups. Uncertain media cannot fall back to duplicate text.
  Healthy targets can still be attempted when another target is held. Both state
  snapshots retain send fences; corrupt existing snapshots no longer silently
  reset the bridge to empty history. Run only **one bridge writer** per state file.
  A selected job is frozen before async conversion/send so intake cannot replace
  its product/price with a different source's “better copy” mid-flight.
- Reports/dashboard distinguish **held**, **skipped**, **partial** and confirmed
  sends. Telegram `JOB sent=` counts new completed target publications; write-only
  recovery is logged separately as `settlement_retries`, not another send.

**Hold policy:** this is conservative at-most-once handling, not a guarantee of
remote exactly-once delivery. A hold can represent a message that was accepted,
not accepted, or only partly delivered. Inspect the destination's actual history
before any intervention. Do not blindly reset a job, clear dedup history or delete
claims to force a resend. Held product claims intentionally do not expire: an
unresolved outcome must not become permission to publish another copy later.
Other products/targets remain eligible. A Telegram hold appears in the dashboard
Held column and `DELIVERY_HELD` audit findings; WhatsApp holds are retained in its
state file's `sendIntents` / `targetProductFences` (even if the queue job is removed).
No automatic “retry held” button is provided. Confirmed WhatsApp fence history
for jobs no longer queued is retained for seven days; unresolved/active-job
records are never removed by that cleanup.

The safety records are additive SQLite tables (`send_attempts`,
`target_product_claims`) and additive WhatsApp JSON fields; old channel settings
and posting history are retained. Do not downgrade to a writer that ignores these
records while uncertain sends exist. This does not retroactively protect sends
made by an older running build, and cannot prove commissions or delivery without
live receipts.

Offline regression coverage includes lost acknowledgements, crashes/restarts,
failed intent/receipt/settlement writes, simultaneous workers, album fallback,
partial-resume layout changes, long-list tail identities, alias history, healthy
target continuation, one-hour/no-price-exception rules and distinct-product controls:

```bash
PYTHON=/path/to/venv/bin/python bash ops/test_all.sh
# Focused, isolated/no-Telegram tests:
/path/to/venv/bin/python test_delivery_fences.py
node --test test_send_guard.mjs
```
