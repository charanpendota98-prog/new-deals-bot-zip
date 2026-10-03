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

run 'render and routing' "$PYTHON" test_render_job.py
run 'rescan and recovery' "$PYTHON" test_rescan.py
run 'pipeline fixes' "$PYTHON" test_pipeline_fixes.py
run 'best-copy quality audit' "$PYTHON" test_best_copy.py
run 'duplicate handling' "$PYTHON" test_duplicate_sim.py
run 'EarnKaro conversion contract' "$PYTHON" test_earnkaro_conversion.py
run 'HYPD link handling' "$PYTHON" test_hypd_links.py
run 'line fidelity and Telegram/WhatsApp identity parity' "$PYTHON" test_line_fidelity.py
run 'WhatsApp bridge contract' bash -c 'cd tg-wa-bridge && TELEGRAM_BOT_TOKEN=x WA_PHONE=919876543210 WA_CHANNEL=x@newsletter node bridge.js --self-test'
run 'Python syntax checks' "$PYTHON" -m py_compile bestgaa/main_bot_new.py bestgaa/migrate_legacy_env.py ops/*.py

printf '\nALL OFFLINE CHECKS PASSED\n'
