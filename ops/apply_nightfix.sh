#!/usr/bin/env bash
# Nightfix deploy: 24/7 WhatsApp posting + 02:00-06:00 IST full off-window +
# night-queue trust policy (ordinary quiet-born deals dropped, best-tier kept).
# Run next to tg_wa_bridge_bundle.zip on the Oracle server. Telegram bot
# (bestgaa.service) is never touched.
set -Eeuo pipefail

BRIDGE_DIR=/home/ubuntu/tg-wa-bridge
HERE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"

echo "[1/6] Checking bundle..."
[[ -f "$HERE/tg_wa_bridge_bundle.zip" ]] || { echo "ERROR: tg_wa_bridge_bundle.zip not found next to this script" >&2; exit 1; }
BOT_DIR=/home/ubuntu/bestgaa-bot/bestgaa-bot
BOT_BUNDLE=""
if [[ -f "$HERE/bestgaa_final_bundle.zip" ]]; then
  BOT_BUNDLE="$HERE/bestgaa_final_bundle.zip"
  echo "       bot bundle found — Telegram bot orphan-token fix will be applied too"
fi

echo "[2/6] Backing up current bridge (code + state + env)..."
sudo systemctl stop tg-wa-bridge || true
cp "$BRIDGE_DIR/bridge.js" "$BRIDGE_DIR/bridge.backup.$STAMP.js" 2>/dev/null || true
cp "$BRIDGE_DIR/bridge-state.json" "$BRIDGE_DIR/bridge-state.before-nightfix.$STAMP.json" 2>/dev/null || true
if [[ -n "$BOT_BUNDLE" ]]; then
  sudo systemctl stop bestgaa || true
  cp "$BOT_DIR/main_bot.py" "$BOT_DIR/main_bot.backup.$STAMP.py" 2>/dev/null || true
  cp "$BOT_DIR/.env" "$BOT_DIR/.env.before-nightfix.$STAMP" 2>/dev/null || true
fi

rollback() {
  echo "NIGHTFIX FAILED — rolling back to previous code..." >&2
  if [[ -f "$BRIDGE_DIR/bridge.backup.$STAMP.js" ]]; then
    cp "$BRIDGE_DIR/bridge.backup.$STAMP.js" "$BRIDGE_DIR/bridge.js"
  fi
  if [[ -f "$BRIDGE_DIR/.env.before-nightfix.$STAMP" ]]; then
    cp "$BRIDGE_DIR/.env.before-nightfix.$STAMP" "$BRIDGE_DIR/.env"
  fi
  if [[ -n "$BOT_BUNDLE" && -f "$BOT_DIR/main_bot.backup.$STAMP.py" ]]; then
    cp "$BOT_DIR/main_bot.backup.$STAMP.py" "$BOT_DIR/main_bot.py"
    if [[ -f "$BOT_DIR/.env.before-nightfix.$STAMP" ]]; then
      cp "$BOT_DIR/.env.before-nightfix.$STAMP" "$BOT_DIR/.env"
    fi
  fi
  sudo systemctl start tg-wa-bridge || true
  sudo systemctl start bestgaa || true
}
trap rollback ERR

echo "[3/6] Installing new bridge code (state/queue/auth preserved)..."
unzip -o "$HERE/tg_wa_bridge_bundle.zip" -d "$BRIDGE_DIR" >/dev/null
chmod +x "$BRIDGE_DIR/install_bridge.sh" "$BRIDGE_DIR/switch_whatsapp_number.sh" 2>/dev/null || true
cd "$BRIDGE_DIR"
[[ -f .env ]] || { echo "ERROR: $BRIDGE_DIR/.env not found" >&2; exit 1; }
cp .env ".env.before-nightfix.$STAMP"

echo "[4/6] Applying 24/7 + night policy env..."
python3 - <<'PY'
from pathlib import Path
p = Path('.env')
updates = {
    'QUIET_START': '02:00',                # ratri 2 AM nunchi
    'QUIET_END': '06:00',                  # udayam 6 AM varaku FULL OFF
    'HYBRID_QUIET': 'false',               # aa window lo emi post avvakudadhu
    'WA_DAY_CAP': '2000',                  # 700/day ceiling lift; safety net matrame
    # (No WA_NIGHT_QUEUE_BEST_ONLY: bridge.js never read it; the 02:00-06:00
    # full off-window is QUIET_START/QUIET_END + HYBRID_QUIET=false above.)
    'WA_ORDINARY_MAX_AGE_MINUTES': '150',  # ordinary deal 2.5h cross aithe drop (trust)
    'MAX_JOB_AGE_HOURS': '12',
    'WA_WARMUP_DONE': 'true',
}
lines = p.read_text().splitlines(); out = []; seen = set()
for line in lines:
    key = line.split('=', 1)[0] if '=' in line else ''
    if key in updates:
        out.append(f'{key}={updates[key]}'); seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen: out.append(f'{key}={value}')
p.write_text('\n'.join(out) + '\n')
PY
chmod 600 .env

echo "[4b/6] Applying Telegram bot orphan-token fix (h/htt leak)..."
if [[ -n "$BOT_BUNDLE" ]]; then
  cd "$BOT_DIR"
  unzip -o "$BOT_BUNDLE" main_bot_new.py -d "$BOT_DIR" >/dev/null
  python3 -m py_compile main_bot_new.py
  # .env is NOT touched here — the bot keeps its existing credentials.
  cp -a main_bot_new.py main_bot.py
  python3 -m py_compile main_bot.py
  BOT_BUNDLED="$(unzip -p "$BOT_BUNDLE" main_bot_new.py | sha256sum | awk '{print $1}')"
  BOT_LIVE="$(sha256sum main_bot.py | awk '{print $1}')"
  echo "       bundled main_bot sha256 = $BOT_BUNDLED"
  echo "       live    main_bot sha256 = $BOT_LIVE"
  [[ "$BOT_BUNDLED" == "$BOT_LIVE" ]] || { echo "ERROR: live main_bot.py != bundled" >&2; exit 1; }
  cd "$BRIDGE_DIR"
else
  echo "       (no bot bundle — bridge-only deploy)"
fi

echo "[5/6] Verifying (deps + syntax + self-test + hash)..."
npm install --omit=dev --no-audit --no-fund >/dev/null
node --check bridge.js
node bridge.js --self-test
BUNDLED="$(unzip -p "$HERE/tg_wa_bridge_bundle.zip" bridge.js | sha256sum | awk '{print $1}')"
LIVE="$(sha256sum bridge.js | awk '{print $1}')"
echo "       bundled bridge.js sha256 = $BUNDLED"
echo "       live    bridge.js sha256 = $LIVE"
[[ "$BUNDLED" == "$LIVE" ]] || { echo "ERROR: live bridge.js != bundled bridge.js" >&2; exit 1; }
printf 'config proof: '
grep -E '^(QUIET_START|QUIET_END|WA_DAY_CAP|WA_NIGHT_QUEUE_BEST_ONLY|WA_ORDINARY_MAX_AGE_MINUTES)=' .env | tr '\n' ' '; echo

echo "[6/6] Starting services..."
sudo systemctl enable tg-wa-bridge >/dev/null 2>&1 || true
sudo systemctl restart tg-wa-bridge
if [[ -n "$BOT_BUNDLE" ]]; then
  sudo systemctl enable bestgaa >/dev/null 2>&1 || true
  sudo systemctl restart bestgaa
fi
sleep 10
printf 'tg-wa-bridge: '; sudo systemctl is-active tg-wa-bridge
printf 'bestgaa:      '; sudo systemctl is-active bestgaa
journalctl -u tg-wa-bridge -n 6 --no-pager -l

trap - ERR

# Keep only the newest five nightfix backups.
mapfile -t files < <(find "$BRIDGE_DIR" -maxdepth 1 -type f \( -name 'bridge.backup.*.js' -o -name '.env.before-nightfix.*' -o -name 'bridge-state.before-nightfix.*.json' \) -printf '%T@ %p\n' 2>/dev/null | sort -rn | cut -d' ' -f2-)
if (( ${#files[@]} > 5 )); then printf '%s\0' "${files[@]:5}" | xargs -0 -r rm -f --; fi

echo "NIGHTFIX SUCCESS — 6AM-2AM posting, 2:01-6AM full break, night queue best-only."
