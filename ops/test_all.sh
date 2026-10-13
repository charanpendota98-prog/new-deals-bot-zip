#!/usr/bin/env bash
# Run the same offline regression gate locally and in GitHub Actions.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"

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
run 'post-deploy repeat guard' "$PYTHON" test_postdeploy_repeats.py
run 'link category inspector' "$PYTHON" test_link_inspector.py
run 'category batch dry-run' "$PYTHON" test_category_preview.py
run 'channel-local source setup' "$PYTHON" test_source_setup.py
run 'source runtime membership and replay guards' "$PYTHON" test_source_runtime.py
run 'live dashboard additions' "$PYTHON" test_live_channels.py
run 'operator dashboard' "$PYTHON" test_dashboard.py
run 'EarnKaro conversion contract' "$PYTHON" test_earnkaro_conversion.py
run 'HYPD link handling' "$PYTHON" test_hypd_links.py
run 'line fidelity and Telegram/WhatsApp identity parity' "$PYTHON" test_line_fidelity.py
run 'WhatsApp bridge contract' bash -c 'cd tg-wa-bridge && TELEGRAM_BOT_TOKEN=x WA_PHONE=919876543210 WA_CHANNEL=x@newsletter BESTGAA_DB_PATH="$BESTGAA_DB_PATH" EARNKARO_PUBLISHER_ID=5478322 AMAZON_TAG=mama086-21 SELF_TEST_PROVENANCE_URL=https://bit.ly/self-test-owned SELF_TEST_CACHE_ONLY_URL=https://bit.ly/self-test-cache-only SELF_TEST_HYPD_URL=https://hypd.store/93944/afflink/self-test-token SELF_TEST_WRONG_ACCOUNT_URL=https://bit.ly/self-test-wrong-account SELF_TEST_AMAZON_PROOF_URL=https://bit.ly/self-test-amazon SELF_TEST_STALE_AMAZON_URL=https://bit.ly/self-test-stale-amazon node bridge.js --self-test'
run 'bridge syntax check (dependency-free)' node --check tg-wa-bridge/bridge.js
run 'Python syntax checks' "$PYTHON" -m py_compile bestgaa/main_bot_new.py bestgaa/category_classifier.py bestgaa/channels_config.py bestgaa/live_channels.py bestgaa/hypd_policy.py dashboard/app.py dashboard/preview.py bestgaa/migrate_legacy_env.py ops/*.py
if "$PYTHON" -c 'import pyflakes' >/dev/null 2>&1; then
  run 'Python lint (pyflakes)' "$PYTHON" -m pyflakes bestgaa/main_bot_new.py bestgaa/category_classifier.py bestgaa/channels_config.py bestgaa/live_channels.py bestgaa/hypd_policy.py dashboard/app.py dashboard/preview.py bestgaa/migrate_legacy_env.py ops/*.py test_*.py
fi

printf '\nALL OFFLINE CHECKS PASSED\n'
