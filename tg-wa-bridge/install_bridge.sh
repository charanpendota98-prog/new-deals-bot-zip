#!/usr/bin/env bash
set -Eeuo pipefail

APP=/home/ubuntu/tg-wa-bridge
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "[1/8] Preparing separate bridge directory..."
mkdir -p "$APP"
if [[ "$HERE" != "$APP" ]]; then
  cp "$HERE/bridge.js" "$HERE/package.json" "$HERE/tg-wa-bridge.service" "$HERE/switch_whatsapp_number.sh" "$APP/"
fi
cd "$APP"

NODE_MAJOR="$(node -p 'process.versions.node.split(`.`)[0]' 2>/dev/null || echo 0)"
if (( NODE_MAJOR < 20 )); then
  echo "[2/8] Installing Node.js 20..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
else
  echo "[2/8] Node.js $(node -v) ready"
fi
if ! command -v tesseract >/dev/null 2>&1; then
  echo "Installing free photo-text detector (Tesseract OCR)..."
  sudo apt-get update -qq
  sudo apt-get install -y tesseract-ocr
else
  echo "Tesseract OCR $(tesseract --version 2>&1 | head -n1) ready"
fi

echo "[3/8] Enter secrets locally (they will not be printed)"
if [[ -f .env ]]; then
  cp .env ".env.backup.$(date +%s)"
  echo "Existing .env backed up. Press Enter to keep an existing value only by re-entering it from your password manager."
fi
read -rsp "New BotFather token: " TG_TOKEN; echo
read -rp "Dedicated WhatsApp number with country code (digits only, e.g. 9198...): " WA_PHONE
read -rp "WhatsApp Channel invite link OR ...@newsletter ID: " WA_CHANNEL
read -rp "WhatsApp groups to ALSO post to (group JID / number / invite code, comma separated, optional): " WA_GROUPS
read -rsp "Bitly tokens for shortening long WhatsApp links (comma separated, optional - without it the is.gd fallback is used): " WA_BITLY_TOKENS; echo
read -rp "Direct source channels to ALSO watch (safety net for deals Telegram missed; Enter = default list, 'none' = disable): " TG_DIRECT_INPUT

if [[ -z "$TG_TOKEN" || -z "$WA_PHONE" || -z "$WA_CHANNEL" ]]; then
  echo "ERROR: all three values are required" >&2
  exit 1
fi
# Direct sources: omit the key for the built-in default list, write it empty to
# disable, or write the custom list the user typed.
DIRECT_SOURCES_LINE=""
if [[ "$TG_DIRECT_INPUT" == "none" ]]; then
  DIRECT_SOURCES_LINE="TG_DIRECT_SOURCES="
elif [[ -n "$TG_DIRECT_INPUT" ]]; then
  DIRECT_SOURCES_LINE="TG_DIRECT_SOURCES=$TG_DIRECT_INPUT"
fi
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TG_SOURCE_USERNAMES=Under99Deals11,under499loots,LootZoneIndia11,SecretLootIndia1,PowerLoots1,Premiumlootsdeals
$DIRECT_SOURCES_LINE
WA_PHONE=$(printf '%s' "$WA_PHONE" | tr -cd '0-9')
WA_CHANNEL=$WA_CHANNEL
WA_GROUPS=$WA_GROUPS
WA_BITLY_TOKENS=$WA_BITLY_TOKENS
WA_PRODUCT_DEDUP_HOURS=10
WA_BEST_GATE=true
WA_BEST_MIN_DISCOUNT=30
WA_BEST_MAX_PRICE=499
TZ_NAME=Asia/Calcutta
# Night quiet 02:00-06:00 IST: WhatsApp posting pauses; deals queue and go out
# at 06:00. HYBRID_QUIET=false = full pause (user's requirement).
QUIET_START=02:00
QUIET_END=06:00
HYBRID_QUIET=false
STRICT_SOURCE_ONLY=true
CURATE_TOP_DEALS=true
AMAZON_TAG=deals0911-21
EARNKARO_PUBLISHER_ID=5478322
BESTGAA_DB_PATH=/home/ubuntu/bestgaa-bot/bestgaa-bot/bestgaa.sqlite3
ROTATION_JITTER_MIN_SECONDS=20
ROTATION_JITTER_MAX_SECONDS=90
SPECIAL_JITTER_MIN_SECONDS=30
SPECIAL_JITTER_MAX_SECONDS=90
LARGE_LIST_MIN_LINKS=4
MAX_JOB_AGE_HOURS=12
MIN_WA_MESSAGE_GAP_SECONDS=60
WA_PRIMARY_SOURCE=under499loots
WA_MEDIA_FIRST=true
WA_PROMOTE_AFTER_MINUTES=45
WA_WARMUP_DONE=true
NEWSLETTER_MEDIA_FIX=true
DIGEST_MAX_CHARS=3800
LOG_LEVEL=info
EOF
chmod 600 .env

echo "[4/8] Installing pinned bridge dependencies..."
npm install --omit=dev --no-audit --no-fund
chmod 700 "$APP"; chmod 700 switch_whatsapp_number.sh
mkdir -p auth media; chmod 700 auth media

echo "[5/8] Verifying Telegram bot token..."
CHECK="$(curl -fsS "https://api.telegram.org/bot${TG_TOKEN}/getMe")"
if ! grep -q '"ok":true' <<<"$CHECK"; then
  echo "ERROR: BotFather token verification failed" >&2
  exit 1
fi
unset TG_TOKEN CHECK

echo
read -rp "Confirm: BotFather bot is ADMIN in @Under99Deals11, @under499loots and @LootZoneIndia11? Type YES: " CONFIRM
[[ "$CONFIRM" == "YES" ]] || { echo "Add bot as admin first, then rerun installer."; exit 1; }

echo "[6/8] Pairing WhatsApp linked device..."
echo "When PAIRING CODE appears: Phone WhatsApp > Linked devices > Link a device > Link with phone number."
npm run pair

echo "[7/8] Installing independent systemd service..."
sudo cp tg-wa-bridge.service /etc/systemd/system/tg-wa-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-wa-bridge
sleep 8

echo "[8/8] Status"
sudo systemctl status tg-wa-bridge --no-pager -l || true
echo
echo "INSTALL COMPLETE"
echo "Logs: journalctl -u tg-wa-bridge -f --no-pager"
echo "Telegram deal bot remains independent: systemctl is-active bestgaa"
