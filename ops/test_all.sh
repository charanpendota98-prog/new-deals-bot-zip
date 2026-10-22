#!/usr/bin/env bash
# Run the same offline regression gate locally and in GitHub Actions.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
# The jsdom suites shell out to "$PYTHON" to read the served dashboard HTML, and
# resolve jsdom through NODE_PATH. Both must be in the environment, not just in
# this shell.
export PYTHON

# What documentation drift costs. README publishes the hashes, the bot version
# and per-suite check counts, so drift means the docs describe a build nobody
# has. Default is to fail on it. A live deploy sets 'warn': a production rollout
# must be TOLD the docs are stale, never blocked by them.
#   check (default) - drift fails the gate
#   warn            - drift is printed loudly, gate still exits 0
#   off             - the freshness step is skipped, and says so
GATE_DOC_MODE="${GATE_DOC_MODE:-check}"
case "$GATE_DOC_MODE" in
  check|warn|off) ;;
  *) echo "ERROR: GATE_DOC_MODE must be one of: check, warn, off (got '$GATE_DOC_MODE')" >&2; exit 2 ;;
esac
export GATE_DOC_MODE

# --- self-logging wrapper ---------------------------------------------------
# The gate publishes its own per-suite check counts in README, so it needs the
# complete log of a finished run to prove them. Re-exec once under tee, then
# verify the documentation against that log. _BESTGAA_GATE_INNER guards the
# recursion: the inner run never wraps again.
if [[ -z "${_BESTGAA_GATE_INNER:-}" ]]; then
  export _BESTGAA_GATE_INNER=1
  GATE_LOG_FILE="${GATE_LOG_FILE:-$(mktemp "${TMPDIR:-/tmp}/bestgaa-gate-log-XXXXXX")}"
  set +e
  bash "$ROOT/ops/test_all.sh" "$@" 2>&1 | tee "$GATE_LOG_FILE"
  inner_rc=${PIPESTATUS[0]}
  set -e
  if [[ "$inner_rc" -ne 0 ]]; then
    printf '\nGATE FAILED (exit %s); documentation freshness not evaluated.\n' "$inner_rc"
    printf 'full log kept at: %s\n' "$GATE_LOG_FILE"
    exit "$inner_rc"
  fi
  if [[ "$GATE_DOC_MODE" == "off" ]]; then
    printf '\n===== README freshness SKIPPED (GATE_DOC_MODE=off) =====\n'
    rm -f "$GATE_LOG_FILE"
    printf '\nALL OFFLINE CHECKS PASSED (README freshness not evaluated)\n'
    exit 0
  fi
  printf '\n===== README freshness (hashes, version label, per-suite counts) =====\n'
  doc_rc=0
  "$PYTHON" ops/doc_freshness.py --check --gate-log "$GATE_LOG_FILE" || doc_rc=$?
  if [[ "$doc_rc" -ne 0 && "$GATE_DOC_MODE" == "warn" ]]; then
    printf '\nDOCUMENTATION DRIFT (GATE_DOC_MODE=warn): reported, not fatal.\n'
    printf 'Repair with: %s ops/doc_freshness.py --write --gate-log %s\n' "$PYTHON" "$GATE_LOG_FILE"
    rm -f "$GATE_LOG_FILE"
    printf '\nALL OFFLINE CHECKS PASSED (README is stale - see above)\n'
    exit 0
  fi
  if [[ "$doc_rc" -ne 0 ]]; then
    printf '\nDOCUMENTATION DRIFT: the gate passed but README no longer describes it.\n'
    printf 'The log of the run that just finished is kept so you can repair the\n'
    printf 'per-suite counts from real output rather than by hand:\n\n'
    printf '  %s ops/doc_freshness.py --write --gate-log %s\n' "$PYTHON" "$GATE_LOG_FILE"
    printf '  %s ./ops/test_all.sh        # re-run; the log is removed when it is green\n\n' "$PYTHON"
    exit "$doc_rc"
  fi
  rm -f "$GATE_LOG_FILE"
  printf '\nALL OFFLINE CHECKS PASSED (including README freshness)\n'
  exit 0
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "ERROR: Python executable '$PYTHON' was not found (override with PYTHON=...)." >&2
  exit 2
fi
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js 20 or newer is required for the WhatsApp bridge tests." >&2
  exit 2
fi
node -e 'if (Number(process.versions.node.split(".")[0]) < 20) process.exit(1)' || {
  echo "ERROR: Node.js 20 or newer is required for the WhatsApp bridge tests." >&2
  exit 2
}

if ! "$PYTHON" -c 'import aiohttp, telethon' >/dev/null 2>&1; then
  echo "ERROR: Python test dependencies are missing; run: $PYTHON -m pip install -r bestgaa/requirements.txt" >&2
  exit 2
fi
if ! (cd tg-wa-bridge && node --input-type=module -e "await Promise.all(['baileys', 'pino', 'qrcode-terminal'].map(name => import(name)))") >/dev/null 2>&1; then
  echo "ERROR: WhatsApp bridge dependencies are missing; run: npm install --prefix tg-wa-bridge --no-package-lock" >&2
  exit 2
fi

run() {
  printf '\n===== %s =====\n' "$1"
  shift
  "$@"
}

# Every shell script here is executed on a production host, several as root's
# neighbour during a deploy. A syntax error there is a failed deploy hours after
# the edit, so the gate parses all of them (git-tracked when available, so
# node_modules/.venv copies are never mistaken for ours).
shell_syntax_check() {
  local files f count=0
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # --others --exclude-standard: a script written five minutes ago and not yet
    # committed must be parsed too, or the newest deploy script in the tree is
    # the one thing the check never looks at.
    files=$(git ls-files --cached --others --exclude-standard -- '*.sh')
  else
    files=$(find . -name '*.sh' -not -path '*/node_modules/*' -not -path './.venv/*' -print)
  fi
  for f in $files; do
    bash -n "$f" || return 1
    count=$((count + 1))
  done
  printf '  %s shell script(s) parse cleanly (bash -n)\n' "$count"
}

# Importing the bot creates logs/ and (on first write) a default DB next to the
# source. Keep the gate's artifacts out of the checkout: a checkout full of test
# logs is exactly what made conversion_report describe a build that was not live
# (2026-10-04). ALWAYS override inherited live paths: this is an offline gate,
# never an operator DB test. Individual tests can create further private paths.
GATE_TMP="$(mktemp -d /tmp/bestgaa-gate-XXXXXX)"
export BOT_DB_PATH="$GATE_TMP/gate.sqlite3"
export BOT_LOG_DIR="$GATE_TMP/logs"
export BOT_MEDIA_DIR="$GATE_TMP/media"
unset CHANNELS_JSON_PATH
export TELEGRAM_SESSION="$GATE_TMP/test-session"
export BESTGAA_DB_PATH="$GATE_TMP/bridge-provenance.sqlite3"
trap 'rm -rf "$GATE_TMP"' EXIT

# The bridge self-test uses a private DB with three cases: an account-bound
# converter proof, a cache-only legacy row (must fail), and a verified Meesho HYPD
# mapping. It never reads or modifies an operator's live database.
"$PYTHON" - "$BESTGAA_DB_PATH" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.executescript("""
CREATE TABLE link_cache(source_url TEXT PRIMARY KEY, affiliate_url TEXT NOT NULL,
 resolved_url TEXT, deal_key TEXT, created_at REAL NOT NULL);
CREATE TABLE owned_link_proofs(affiliate_url TEXT PRIMARY KEY, destination_url TEXT NOT NULL,
 proof_type TEXT NOT NULL, publisher_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE hypd_links(token TEXT PRIMARY KEY, store TEXT NOT NULL, afflink_url TEXT NOT NULL,
 resolved_url TEXT, product_key TEXT, created_at REAL NOT NULL);
""")
conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)", (
    "https://source.invalid/1", "https://bit.ly/self-test-owned",
    "https://www.flipkart.com/product/p/itmOWNED?pid=1", "flipkart:itmowned", 1))
conn.execute("INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)", (
    "https://bit.ly/self-test-owned", "https://ekaro.in/enkr-self-test",
    "earnkaro_api", "5478322", 1))
conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)", (
    "https://source.invalid/2", "https://bit.ly/self-test-cache-only",
    "https://www.flipkart.com/product/p/itmBARE?pid=2", "flipkart:itmbare", 1))
conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)", (
    "https://source.invalid/3", "https://bit.ly/self-test-wrong-account",
    "https://www.flipkart.com/product/p/itmOTHER?pid=3", "flipkart:itmother", 1))
conn.execute("INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)", (
    "https://bit.ly/self-test-wrong-account", "https://ekaro.in/enkr-other-account",
    "earnkaro_api", "9999999", 1))
conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)", (
    "https://source.invalid/4", "https://bit.ly/self-test-amazon",
    "https://www.amazon.in/dp/B0TAG", "amazon:B0TAG", 1))
conn.execute("INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)", (
    "https://bit.ly/self-test-amazon", "https://www.amazon.in/dp/B0TAG?tag=mama086-21",
    "amazon_native", "", 1))
conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)", (
    "https://source.invalid/5", "https://bit.ly/self-test-stale-amazon",
    "https://www.amazon.in/dp/B0OLD", "amazon:B0OLD", 1))
conn.execute("INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)", (
    "https://bit.ly/self-test-stale-amazon", "https://www.amazon.in/dp/B0OLD?tag=oldtag-21",
    "amazon_native", "", 1))
conn.execute("INSERT INTO hypd_links VALUES(?,?,?,?,?,?)", (
    "self-test-token", "93944", "https://hypd.store/93944/afflink/self-test-token",
    "https://www.meesho.com/product/p/SELFTEST", "PID:www.meesho.com:selftest", 1))
# HYPD proof matrix: a known product does NOT prove an arbitrary short URL.
share = "https://hypd.store/93944/afflink/self-test-token"
product = "https://www.meesho.com/product/p/SELFTEST"
for name, destination, kind, proof_share, issuer in [
    ("exact", product, "hypd", share, "93944"),
    ("unproven", product, "", "", ""),
    ("wrong-product", "https://www.meesho.com/product/p/WRONG123", "hypd", share, "93944"),
    ("wrong-store", product, "hypd", share, "88888"),
    ("legacy-ek", product, "earnkaro_api", "https://ekaro.in/legacy", "5478322"),
]:
    short = "https://bit.ly/hypd-matrix-" + name
    conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)", (short,short,destination,"PID:www.meesho.com:selftest",1))
    if kind:
        conn.execute("INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)", (short,proof_share,kind,issuer,1))
for name, owner, key in [("foreign", "88888", "PID:www.meesho.com:selftest"),
                         ("bad-key", "93944", "PID:www.meesho.com:wrong123")]:
    link = f"https://hypd.store/{owner}/afflink/matrix-{name}"
    conn.execute("INSERT INTO hypd_links VALUES(?,?,?,?,?,?)", (f"{owner}:matrix-{name}",owner,link,product,key,1))
conn.commit()
conn.close()
PY

run 'shared HYPD identity policy parity' cmp bestgaa/hypd_policy.py tg-wa-bridge/hypd_policy.py
run 'shared category taxonomy parity' cmp bestgaa/category_rules.json tg-wa-bridge/category_rules.json
run 'strict category evidence and cross-language parity' "$PYTHON" test_category_classifier.py
run 'read-only diagnostics and receipt evidence' "$PYTHON" test_safety_diagnostics.py
run 'category adversarial and metamorphic invariants' "$PYTHON" test_category_invariants.py
run 'render and routing' "$PYTHON" test_render_job.py
run 'rescan and recovery' "$PYTHON" test_rescan.py
run 'pipeline fixes' "$PYTHON" test_pipeline_fixes.py
run 'best-copy quality audit' "$PYTHON" test_best_copy.py
run 'duplicate handling' "$PYTHON" test_duplicate_sim.py
run 'durable delivery fences and restart recovery' "$PYTHON" test_delivery_fences.py
run 'WhatsApp durable send guard' node --test test_send_guard.mjs
run 'post-deploy repeat guard' "$PYTHON" test_postdeploy_repeats.py
run 'link category inspector' "$PYTHON" test_link_inspector.py
# USER REPORT 2026-10-21: two is.gd links in a channel resolving to UNTAGGED
# Amazon pages. The doctor answers "valid? ours?" per link and prints the
# repaired link; the same suite pins that no unprovable hop is minted or posted.
run 'link doctor (valid? ours? repaired link) + no unprovable hop' "$PYTHON" test_link_doctor.py
# 258 checks, 100 rounds: every Amazon shape one tag of ours, every tokenless hop
# repaired from our own book or unwrapped live, a bare merchant page converted -
# "commission asalu miss avvakunda" as an executable rule.
run 'link commission (every published link is OURS, 100 rounds)' "$PYTHON" test_link_commission.py
run 'category batch dry-run' "$PYTHON" test_category_preview.py
run 'category-wise shelf reporting (DB + log + config)' "$PYTHON" test_category_report.py
run 'deal card studio (parsers shared, PNG, captions, plan)' "$PYTHON" test_deal_card.py
run 'post buttons (validation, atomic config, send-path no-op)' "$PYTHON" test_post_buttons.py
run 'channel-local source setup' "$PYTHON" test_source_setup.py
run 'source runtime membership and replay guards' "$PYTHON" test_source_runtime.py
run 'live dashboard additions' "$PYTHON" test_live_channels.py
run 'watch a source channel' "$PYTHON" test_watch_source.py
# "Telegram lo vunna channel anni chupinchi": the whole account's channels,
# searchable + multi-select + categories, saved once and watched live.
run 'channel directory + bulk source studio' "$PYTHON" test_channel_directory.py
run 'operator dashboard' "$PYTHON" test_dashboard.py
run 'pipeline integrity: nothing dropped' "$PYTHON" test_pipeline_integrity.py
run 'dashboard link: tunnel, token, one-click' "$PYTHON" test_dashboard_link.py
run 'ops insight: health, timeline, trace, search, scorecard' "$PYTHON" test_dashboard_ops.py
# "commission asalu miss avvakunda": one queue id, every link and rewrite, with
# proof - and a hole (no delivery row) is an error, never a quiet zero.
run 'deal forensics: links, rewrites and proof per post' "$PYTHON" test_deal_forensics.py

# jsdom DOM workflow suites: the only checks that drive the JavaScript the
# operator actually clicks (guided setup, source picker, category studio) in a
# real DOM, with the API mocked. jsdom is NOT a dependency of the deployed
# bridge, so it is resolved from an isolated prefix and the suites report a loud
# SKIPPED when it is absent instead of passing quietly. CI installs it.
JSDOM_PREFIX="${JSDOM_PREFIX:-/tmp/category-ui-test}"
export NODE_PATH="${NODE_PATH:-$JSDOM_PREFIX/node_modules}"
if node -e "require('jsdom')" >/dev/null 2>&1; then
  run 'dashboard DOM: category studio' node test_dashboard_ui.cjs
  run 'dashboard DOM: guided setup' node test_dashboard_setup.cjs
  run 'dashboard DOM: source picker' node test_dashboard_sources.cjs
  run 'dashboard DOM: category panel' node test_dashboard_categories.cjs
  run 'dashboard DOM: card studio' node test_dashboard_card_studio.cjs
  run 'dashboard DOM: source -> target + safety' node test_dashboard_source_target.cjs
  run 'dashboard DOM: ops console' node test_dashboard_ops.cjs
  run 'dashboard DOM: watch a source' node test_dashboard_watch.cjs
  run 'dashboard DOM: deals from any channel (bulk source studio)' node test_deals_dom.cjs
  run 'dashboard DOM: deal dossier (links + rewrites)' node test_deal_dossier.cjs
  run 'dashboard DOM: pipeline integrity' node test_dashboard_pipeline.cjs
  run 'dashboard DOM: tunnel link + token' node test_dashboard_link.cjs
else
  printf '\n===== dashboard DOM suites SKIPPED (jsdom not reachable) =====\n'
  printf '  install once : npm install --prefix %s --no-package-lock jsdom\n' "$JSDOM_PREFIX"
  printf '  then re-run  : PYTHON=%s ./ops/test_all.sh\n' "$PYTHON"
  printf '  (or override : JSDOM_PREFIX=/path/with/node_modules ./ops/test_all.sh)\n'
  printf '  CI installs jsdom, so this only skips on a fresh local checkout.\n'
fi
run 'EarnKaro conversion contract' "$PYTHON" test_earnkaro_conversion.py
# The end-to-end guarantee: what a channel RECEIVES is provably our link.
run 'post-link guarantee (real worker, audited output)' "$PYTHON" test_post_link_guarantee.py
# PUBLIC-CHANNEL SAFETY: one policy JSON, executed by the bot AND the bridge.
# The suite asserts the two verdicts are identical, so a policy edit that lands
# on only one side fails the gate instead of reaching a channel.
run 'public-channel content safety (Python == JavaScript)' "$PYTHON" test_safety_filter.py
# SOURCE -> TARGET: the reported post (neo qled @ ₹60k on a link.amazon wrapper)
# priced, classified, routed and gated end to end, including the dashboard's
# planner and the ops CLI.
run 'source -> target planner (the reported post)' "$PYTHON" test_source_to_target.py
run 'HYPD link handling' "$PYTHON" test_hypd_links.py
run 'line fidelity and Telegram/WhatsApp identity parity' "$PYTHON" test_line_fidelity.py
run 'WhatsApp bridge contract' bash -c 'cd tg-wa-bridge && TELEGRAM_BOT_TOKEN=x WA_PHONE=919876543210 WA_CHANNEL=x@newsletter BESTGAA_DB_PATH="$BESTGAA_DB_PATH" EARNKARO_PUBLISHER_ID=5478322 AMAZON_TAG=mama086-21 SELF_TEST_PROVENANCE_URL=https://bit.ly/self-test-owned SELF_TEST_CACHE_ONLY_URL=https://bit.ly/self-test-cache-only SELF_TEST_HYPD_URL=https://hypd.store/93944/afflink/self-test-token SELF_TEST_WRONG_ACCOUNT_URL=https://bit.ly/self-test-wrong-account SELF_TEST_AMAZON_PROOF_URL=https://bit.ly/self-test-amazon SELF_TEST_STALE_AMAZON_URL=https://bit.ly/self-test-stale-amazon node bridge.js --self-test'
run 'bridge syntax check (dependency-free)' node --check tg-wa-bridge/bridge.js
run 'bridge safety gate syntax' node --check tg-wa-bridge/safety_filter.js
run 'shell script syntax (bash -n)' shell_syntax_check
run 'deploy bundle ships the tree (bot + bridge)' "$PYTHON" test_deploy_zip.py
run 'Python syntax checks' "$PYTHON" -m py_compile bestgaa/main_bot_new.py bestgaa/deal_facts.py bestgaa/safety_filter.py bestgaa/category_classifier.py bestgaa/channels_config.py bestgaa/live_channels.py bestgaa/hypd_policy.py dashboard/app.py dashboard/preview.py dashboard/source_target.py dashboard/insight.py dashboard/pipeline.py dashboard/trends.py bestgaa/migrate_legacy_env.py ops/*.py
if "$PYTHON" -c 'import pyflakes' >/dev/null 2>&1; then
  run 'Python lint (pyflakes)' "$PYTHON" -m pyflakes bestgaa/main_bot_new.py bestgaa/deal_facts.py bestgaa/safety_filter.py bestgaa/category_classifier.py bestgaa/channels_config.py bestgaa/live_channels.py bestgaa/hypd_policy.py dashboard/app.py dashboard/preview.py dashboard/source_target.py dashboard/insight.py bestgaa/migrate_legacy_env.py ops/*.py test_*.py
fi

run 'repo self-consistency (gate coverage + README proof tables)' "$PYTHON" test_repo_consistency.py

printf '\nALL OFFLINE CHECKS PASSED\n'
