#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/home/ubuntu/bestgaa-bot/bestgaa-bot"
SERVICE="bestgaa"
# BESTGAA_KEEP_OFF=1: ship + verify the code but NEVER start/restart the service
# (used by ops/deploy_r15_keep_off.sh so posting stays off until the operator
# approves with ops/approve_posting.sh).
KEEP_OFF="${BESTGAA_KEEP_OFF:-0}"
NEW_FILE="$APP_DIR/main_bot_new.py"
LIVE_FILE="$APP_DIR/main_bot.py"
ENV_FILE="$APP_DIR/.env"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$APP_DIR/main_bot.backup.$STAMP.py"

cd "$APP_DIR"

echo "[1/9] Checking uploaded files..."
[[ -f "$NEW_FILE" ]] || { echo "ERROR: main_bot_new.py not found"; exit 1; }
# deal_facts.py is a HARD import of main_bot_new.py (price/MRP/discount reading)
# and post_buttons.py is imported in the send path - a host missing either boots
# into ImportError or fails every send, so the deploy refuses to start without
# them (DEPLOY GAP found 2026-10-19 while preparing the perfect-deploy pass).
for helper in category_classifier.py channels_config.py channel_directory.py live_channels.py hypd_policy.py source_catalog.json deal_facts.py post_buttons.py; do
  [[ -f "$APP_DIR/$helper" ]] || { echo "ERROR: $helper missing; upload it beside main_bot_new.py"; exit 1; }
done
[[ -f "$APP_DIR/category_rules.json" ]] || { echo "ERROR: category_rules.json missing; upload it beside main_bot_new.py"; exit 1; }
[[ -f "$APP_DIR/migrate_legacy_env.py" ]] || { echo "ERROR: migrate_legacy_env.py not found"; exit 1; }

# Zero-manual-config path: create .env locally from the currently working
# hardcoded v13/v14 bot. Secret values are never printed.
if [[ ! -f "$ENV_FILE" ]] || grep -q "REPLACE_WITH_NEW_" "$ENV_FILE"; then
  echo "[2/9] Creating .env from existing working bot..."
  [[ -f "$LIVE_FILE" ]] || { echo "ERROR: old working main_bot.py not found for migration"; exit 1; }
  python3 "$APP_DIR/migrate_legacy_env.py" "$LIVE_FILE"
else
  echo "[2/9] Existing .env found"
fi

for key in TELEGRAM_API_ID TELEGRAM_API_HASH EARNKARO_API_KEY AMAZON_TAG; do
  grep -qE "^${key}=.+" "$ENV_FILE" || { echo "ERROR: $key missing in .env"; exit 1; }
done

# USER RULE (2026-09-06): "kothaga thiskunna mama086-21 idi manade". A wrong
# AMAZON_TAG would credit a SOURCE for our sales on every channel we own, so the
# deploy refuses to ship a tag that is not ours. Keep this in step with the bot's
# OUR_AMAZON_TAGS in main_bot_new.py. The check runs BEFORE anything below can
# mutate .env, so a misconfigured tag never half-upgrades a server.
OUR_AMAZON_TAGS="mama086-21"
CONFIGURED_TAG="$(grep -E '^AMAZON_TAG=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
CONFIGURED_TAG_LOWER="$(printf '%s' "${CONFIGURED_TAG:-}" | tr '[:upper:]' '[:lower:]')"
if [[ -n "$CONFIGURED_TAG" ]] && [[ ",${OUR_AMAZON_TAGS}," != *",${CONFIGURED_TAG_LOWER},"* ]]; then
  echo "ERROR: AMAZON_TAG '${CONFIGURED_TAG}' is not one of ours (${OUR_AMAZON_TAGS}) - a source's tag would credit them for our sales. Deploy aborted."
  exit 1
fi

# USER DECISION (2026-09-06): the Associates tag earns on EVERY owned channel,
# not only the reviewed one. An existing .env is never rewritten by the
# installer, so the switch is added here if it is missing - otherwise the tag
# would silently stay on smartbuyhub11 alone after an upgrade.
if ! grep -qE "^AMAZON_TAG_TARGETS=" "$ENV_FILE"; then
  echo "AMAZON_TAG_TARGETS=all" >> "$ENV_FILE"
  echo "      added AMAZON_TAG_TARGETS=all to .env"
fi
echo "      Associates tag: ${CONFIGURED_TAG:-<none>} on $(grep -E '^AMAZON_TAG_TARGETS=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
if grep -q "REPLACE_WITH_NEW_" "$ENV_FILE"; then
  echo "ERROR: .env migration left placeholders"
  exit 1
fi
chmod 600 "$ENV_FILE"

echo "[3/9] Compiling new bot..."
python3 -m py_compile "$NEW_FILE"
# A half-uploaded helper used to surface as a runtime crash AFTER the restart;
# compiling every Python helper here fails the deploy BEFORE anything is moved.
for helper in category_classifier.py channels_config.py live_channels.py hypd_policy.py deal_facts.py post_buttons.py migrate_legacy_env.py; do
  python3 -m py_compile "$APP_DIR/$helper" || { echo "ERROR: $helper does not compile"; exit 1; }
done

echo "[4/9] Installing dependencies..."
python3 -m pip install --user --quiet -r "$APP_DIR/requirements.txt"

echo "[5/9] Backing up current working bot..."
if [[ -f "$LIVE_FILE" ]]; then
  cp -a "$LIVE_FILE" "$BACKUP"
  echo "Backup: $BACKUP"
fi

rollback() {
  echo "DEPLOY FAILED — rolling back..."
  if [[ -f "$BACKUP" ]]; then
    cp -a "$BACKUP" "$LIVE_FILE"
    if [[ "$KEEP_OFF" == "1" ]]; then
      echo "      (keep-off mode: file restored; service NOT started)"
    else
      sudo systemctl restart "$SERVICE" || true
    fi
  fi
}
trap rollback ERR

echo "[6/9] Activating new bot..."
cp -a "$NEW_FILE" "$LIVE_FILE"
python3 -m py_compile "$LIVE_FILE"

if [[ -f "$APP_DIR/bestgaa.service" ]]; then
  sudo cp "$APP_DIR/bestgaa.service" "/etc/systemd/system/$SERVICE.service"
fi
sudo systemctl daemon-reload
if [[ "$KEEP_OFF" == "1" ]]; then
  # keep-off: unit is installed but the service is stopped + disabled so it
  # cannot post and cannot come back on a reboot until approved.
  sudo systemctl disable "$SERVICE" >/dev/null 2>&1 || true
  sudo systemctl stop "$SERVICE" 2>/dev/null || true
  echo "[7/9] keep-off mode: service left STOPPED + DISABLED (posting OFF)"
else
  sudo systemctl enable "$SERVICE" >/dev/null
  sudo systemctl restart "$SERVICE"

  sleep 12

  echo "[7/9] Checking service..."
  sudo systemctl is-active --quiet "$SERVICE"
fi

sleep 12

echo "[8/9] Verifying installed code and startup marker..."
# Verify the LIVE file really is the code we just shipped. A hash mismatch means
# the old bot is still running and the channel would keep showing old bugs.
sha256sum "$LIVE_FILE" | awk '{print "       live main_bot.py sha256 = " $1}'
sha256sum "$NEW_FILE"    | awk '{print "       bundled main_bot_new.py sha256 = " $1}'
if ! cmp -s "$LIVE_FILE" "$NEW_FILE"; then
  echo "ERROR: live main_bot.py != bundled main_bot_new.py"
  exit 1
fi
if ! tail -n 300 "$APP_DIR/logs/bot.log" | grep -Eq "BestGAA Production Bot v1[0-9] starting|LIVE \| sources="; then
  if [[ "$KEEP_OFF" == "1" ]]; then
    echo "NOTE: no startup marker - expected in keep-off mode (service is OFF)."
  else
    echo "WARNING: startup marker not yet visible in last 300 lines"
    echo "         (service may still be starting). Continuing but check logs below."
  fi
fi

trap - ERR

echo "[9/9] DEPLOY SUCCESS (verified: live file == shipped code)"
if [[ "$KEEP_OFF" == "1" ]]; then
  printf 'bestgaa: '; sudo systemctl is-active "$SERVICE" || true
  printf 'enabled : '; sudo systemctl is-enabled "$SERVICE" 2>/dev/null || echo disabled
  echo "        posting stays OFF until: sudo bash ops/approve_posting.sh --i-approve-posting"
else
  sudo systemctl status "$SERVICE" --no-pager --lines=10
fi
printf '\nLive logs:\n  tail -f %s/logs/bot.log\n' "$APP_DIR"
