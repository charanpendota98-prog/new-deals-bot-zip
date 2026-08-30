#!/usr/bin/env bash
set -Eeuo pipefail
APP=/home/ubuntu/tg-wa-bridge
cd "$APP"

[[ -f .env ]] || { echo "ERROR: $APP/.env not found" >&2; exit 1; }
echo "This switches only the WhatsApp linked number."
echo "Telegram BestGAA service, Telegram channels, durable queue and rotation state are not deleted."
read -rp "Confirm the NEW number is already owner/admin of the SAME WhatsApp Channel. Type YES: " CONFIRM
[[ "$CONFIRM" == "YES" ]] || { echo "Cancelled."; exit 1; }
read -rp "New WhatsApp number with country code (digits only): " NEW_PHONE
NEW_PHONE="$(printf '%s' "$NEW_PHONE" | tr -cd '0-9')"
[[ ${#NEW_PHONE} -ge 10 ]] || { echo "ERROR: invalid phone number" >&2; exit 1; }

STAMP="$(date +%Y%m%d_%H%M%S)"
echo "[1/6] Stopping only WhatsApp bridge..."
sudo systemctl stop tg-wa-bridge || true

echo "[2/6] Backing up auth and config; preserving queue..."
cp .env ".env.backup.$STAMP"
if [[ -d auth ]]; then mv auth "auth.backup.$STAMP"; fi
mkdir -m 700 auth
cp bridge-state.json "bridge-state.manual-backup.$STAMP.json" 2>/dev/null || true
chmod 600 "bridge-state.manual-backup.$STAMP.json" 2>/dev/null || true

echo "[3/6] Updating only WA_PHONE..."
python3 - "$NEW_PHONE" <<'PY'
from pathlib import Path
import sys
p=Path('.env')
phone=sys.argv[1]
lines=p.read_text().splitlines()
out=[]
found=False
for line in lines:
    if line.startswith('WA_PHONE='):
        out.append('WA_PHONE='+phone); found=True
    else:
        out.append(line)
if not found: out.append('WA_PHONE='+phone)
p.write_text('\n'.join(out)+'\n')
PY
chmod 600 .env

echo "[4/6] Pairing new linked number..."
echo "Enter the displayed code on the NEW phone: WhatsApp > Linked devices > Link with phone number."
if ! npm run pair; then
  echo "PAIRING FAILED. Service remains stopped. Old auth is safe at auth.backup.$STAMP" >&2
  exit 1
fi

echo "[5/6] Starting bridge with preserved queue and same Channel..."
sudo systemctl start tg-wa-bridge
sleep 8

echo "[6/6] Verifying both independent services..."
printf 'bestgaa (Telegram): '; sudo systemctl is-active bestgaa || true
printf 'tg-wa-bridge: '; sudo systemctl is-active tg-wa-bridge || true
sudo systemctl status tg-wa-bridge --no-pager -l | tail -n 25 || true
echo
echo "SWITCH COMPLETE. Logs: journalctl -u tg-wa-bridge -f --no-pager"
