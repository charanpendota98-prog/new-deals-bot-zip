#!/usr/bin/env bash
set -Eeuo pipefail
HOME_DIR=/home/ubuntu
BESTGAA_DIR="$HOME_DIR/bestgaa-bot/bestgaa-bot"
BRIDGE_DIR="$HOME_DIR/tg-wa-bridge"
HERE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"

echo "[1/7] Checking packages..."
[[ -f "$HERE/bestgaa_final_bundle.zip" && -f "$HERE/tg_wa_bridge_bundle.zip" ]] || { echo "Missing inner bundles" >&2; exit 1; }

echo "[2/7] Updating Telegram BestGAA safely..."
cp "$HERE/bestgaa_final_bundle.zip" "$BESTGAA_DIR/"
cd "$BESTGAA_DIR"
unzip -o bestgaa_final_bundle.zip >/dev/null
chmod +x deploy_bestgaa.sh

# Stop the old process before changing attribution so no message can slip out
# during deployment with a previously rejected Amazon Store ID.
sudo systemctl stop bestgaa || true
if [[ -f .env ]]; then
  cp .env ".env.before-amazon-tag.$STAMP"
  python3 - <<'PY'
from pathlib import Path
p=Path('.env'); lines=p.read_text().splitlines(); out=[]; seen=set()
updates={'AMAZON_TAG':'mama086-21','PRICE_DEDUP_SECONDS':'0',
         # EarnKaro / Affiliaters converter token. Without a VALID token every
         # conversion answers 401 and the bot posts clean, UNTAGGED merchant
         # links (zero commission) - exactly what "the EarnKaro links are not
         # converting" looks like. The token is a SECRET (see the Security
         # section of the README), so it is never committed here: it is read
         # from $EARNKARO_API_KEY or from ops/.earnkaro_key (gitignored) when
         # one of them exists, and the server's existing value is left alone
         # otherwise. Rotate it with:  ./set_earnkaro_key.sh '<token>'
         'EARNKARO_API_URL':'https://ekaro-api.affiliaters.in/api/converter/public',
         'EARNKARO_PUBLISHER_ID':'5478322',
         # The documented converter mode: convert the link, do nothing else.
         'EARNKARO_CONVERT_OPTION':'convert_only',
         # USER RULE (2026-09-24): Amazon converts through EarnKaro like every
         # other store (Associates is still rejecting the account, so the native
         # ?tag= link earns nothing). The native tagged link stays the fallback,
         # and the reviewed channel still shows the native tagged product page.
         # Set to 'false' to restore pure native tagging.
         'AMAZON_VIA_EARNKARO':'true',
         # Night quiet 02:00-06:00 IST: posting pauses, deals queue, 06:00 flush.
         'POST_QUIET_START':'02:00','POST_QUIET_END':'06:00',
         # User-supplied Bitly token: best shortening for long links.
         'BITLY_TOKENS':'0cb6a376353a7a5ecd93ee07bf0149e2d3f3aa22',
         # Highest commission wins: Amazon always direct Associates tag
         # (EarnKaro takes a cut in the middle; 0.0 = never route via EK).
         'AMAZON_EARNKARO_RATIO':'0.0'}
for line in lines:
    key=line.split('=',1)[0] if '=' in line else ''
    if key in updates:
        out.append(f'{key}={updates[key]}'); seen.add(key)
    else: out.append(line)
for key,value in updates.items():
    if key not in seen: out.append(f'{key}={value}')
p.write_text('\n'.join(out)+'\n')
PY
  # The EarnKaro token, when the operator has provided one: from the
  # environment, or from ops/.earnkaro_key (gitignored) so a local checkout can
  # carry it to the server without ever committing it. With neither present the
  # server keeps the token it already has, and the line below says so.
  EK_TOKEN="${EARNKARO_API_KEY:-}"
  if [[ -z "$EK_TOKEN" && -f "$HERE/.earnkaro_key" ]]; then
    EK_TOKEN="$(tr -d '[:space:]' < "$HERE/.earnkaro_key")"
  fi
  if [[ -n "$EK_TOKEN" ]]; then
    if grep -qE '^EARNKARO_API_KEY=' "$ENV_FILE"; then
      sed -i "s|^EARNKARO_API_KEY=.*|EARNKARO_API_KEY=$EK_TOKEN|" "$ENV_FILE"
    else
      echo "EARNKARO_API_KEY=$EK_TOKEN" >> "$ENV_FILE"
    fi
    echo "      EarnKaro API key: updated from the deploy environment"
  else
    echo "      EarnKaro API key: unchanged (not in the environment, no ops/.earnkaro_key)."
    echo "      If conversion is failing, run:  ./set_earnkaro_key.sh '<token>'"
  fi
  chmod 600 .env
fi
# Old Amazon Bitly links conceal the previous tag, so invalidate Amazon cache
# rows before the new process starts.
python3 - <<'PY'
import sqlite3
p='bestgaa.sqlite3'
db=sqlite3.connect(p)
db.execute("DELETE FROM link_cache WHERE lower(resolved_url) LIKE '%amazon.in%' OR lower(resolved_url) LIKE '%amazon.com%'")
db.execute("""DELETE FROM link_cache
              WHERE lower(COALESCE(resolved_url,'')) LIKE '%referral_code=%'
                 OR lower(COALESCE(resolved_url,'')) LIKE '%refcode=%'
                 OR lower(COALESCE(resolved_url,'')) LIKE '%invite_code=%'""")
db.commit(); db.close()
PY
./deploy_bestgaa.sh

echo "[3/7] Backing up WhatsApp bridge code/state..."
mkdir -p "$BRIDGE_DIR"
cp "$BRIDGE_DIR/bridge.js" "$BRIDGE_DIR/bridge.backup.$STAMP.js" 2>/dev/null || true
cp "$BRIDGE_DIR/bridge-state.json" "$BRIDGE_DIR/bridge-state.before-hotfix.$STAMP.json" 2>/dev/null || true

echo "[4/7] Updating bridge; preserving secrets/auth/queue and applying schedule..."
sudo systemctl stop tg-wa-bridge || true
unzip -o "$HERE/tg_wa_bridge_bundle.zip" -d "$BRIDGE_DIR" >/dev/null
cd "$BRIDGE_DIR"
chmod +x install_bridge.sh switch_whatsapp_number.sh
if [[ -f .env ]]; then
  cp .env ".env.before-schedule.$STAMP"
  python3 - <<'PY'
from pathlib import Path
p=Path('.env')
updates={'QUIET_START':'02:00','QUIET_END':'06:00','HYBRID_QUIET':'false','WA_DAY_CAP':'2000','WA_ORDINARY_MAX_AGE_MINUTES':'150','STRICT_SOURCE_ONLY':'true','CURATE_TOP_DEALS':'true','MAX_JOB_AGE_HOURS':'12','MIN_WA_MESSAGE_GAP_SECONDS':'60','TG_SOURCE_USERNAMES':'Under99Deals11,under499loots,LootZoneIndia11,SecretLootIndia1,PowerLoots1,Premiumlootsdeals','AMAZON_TAG':'mama086-21','WA_PRIMARY_SOURCE':'under499loots','WA_MEDIA_FIRST':'true','NEWSLETTER_MEDIA_FIX':'true','WA_WARMUP_DONE':'true','WA_PROMOTE_AFTER_MINUTES':'45','WA_BITLY_TOKENS':'0cb6a376353a7a5ecd93ee07bf0149e2d3f3aa22',
 # Channel-only for now: groups are skipped (no group join/invite calls) so a
 # flaky group can never trigger WhatsApp disconnects. Set to 'false' and re-run
 # this script to turn group fan-out back on.
 'WA_CHANNEL_ONLY':'true',
 # Second WhatsApp Channel: ONLY Under-₹99 products (and best-discount /
 # majority-under-99 lists) go here; everything still posts to WA_CHANNEL.
 'WA_CHANNEL_UNDER99':'https://whatsapp.com/channel/0029VbDI3WbG8l5JhOsmDg3V',
 # Advanced curation: WhatsApp channels carry only TOP deals. A photo alone no
 # longer passes; expensive weak-discount and signal-less posts are rejected.
 'WA_BEST_GATE':'true','WA_QUALITY_MIN_SCORE':'3','WA_QUALITY_STRONG_DISCOUNT':'70',
 'WA_QUALITY_EXPENSIVE_PRICE':'1999','WA_QUALITY_WEAK_DISCOUNT':'40',
 # Under-99 lists: feature sub-₹99 products AND (60%+ / special / photo list OR
 # a majority of items ≤ ₹99).
 'WA_UNDER99_LIST_MIN_DISCOUNT':'60'}
lines=p.read_text().splitlines(); out=[]; seen=set()
for line in lines:
    key=line.split('=',1)[0] if '=' in line else ''
    if key in updates:
        out.append(f'{key}={updates[key]}'); seen.add(key)
    else: out.append(line)
for key,value in updates.items():
    if key not in seen: out.append(f'{key}={value}')
p.write_text('\n'.join(out)+'\n')
PY
  chmod 600 .env
fi
npm install --omit=dev --no-audit --no-fund
node --check bridge.js
# Prove the Channel media contract and dispatcher guards before the service
# is allowed to start. A failing self-test aborts the deployment.
node bridge.js --self-test

# CRITICAL: confirm the LIVE bridge.js is the freshly shipped file. If the
# service daemon or a stale copy keeps running an older bridge.js, the fixes
# never take effect and old bugs keep reappearing. Abort loudly if mismatch.
# Hash the bridge.js embedded inside the bundle (source of truth), not the
# working copy, so this is a real check rather than comparing a file to itself.
BRIDGE_BUNDLED="$(unzip -p "$HERE/tg_wa_bridge_bundle.zip" bridge.js | sha256sum | awk '{print $1}')"
BRIDGE_LIVE="$(sha256sum ${BRIDGE_DIR}/bridge.js 2>/dev/null | awk '{print $1}')"
echo "       bundled bridge.js sha256  = $BRIDGE_BUNDLED"
echo "       live    bridge.js sha256  = $BRIDGE_LIVE"
if [[ "$BRIDGE_LIVE" != "$BRIDGE_BUNDLED" ]]; then
  echo "ERROR: live bridge.js != bundled bridge.js; old code is running." >&2
  exit 1
fi
bash -n switch_whatsapp_number.sh
sudo cp tg-wa-bridge.service /etc/systemd/system/tg-wa-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-wa-bridge
sleep 10

echo "[5/7] Service status + live PIDs..."
printf 'bestgaa: '; sudo systemctl is-active bestgaa
echo "  pid: $(pgrep -f 'main_bot.py' || echo 'NONE')"
printf 'tg-wa-bridge: '; sudo systemctl is-active tg-wa-bridge
echo "  pid: $(pgrep -f 'node bridge.js' || echo 'NONE')"

# Confirm the RUNNING bridge process actually started with the shipped code.
# A running process whose file no longer matches its on-disk replacement means
# the service was never actually restarted — the classic reason old behavior
# keeps showing up after every "successful" deploy.
BRIDGE_LIVE="$(sha256sum ${BRIDGE_DIR}/bridge.js 2>/dev/null | awk '{print $1}')"
BRIDGE_BUNDLED="$(unzip -p "$HERE/tg_wa_bridge_bundle.zip" bridge.js | sha256sum | awk '{print $1}')"
if [[ "$BRIDGE_LIVE" == "$BRIDGE_BUNDLED" ]]; then
  echo "RESULT: tg-wa-bridge code = shipped code (NEW behavior live)"
else
  echo "RESULT: tg-wa-bridge code != shipped code (OLD code still running!)" >&2
fi

echo "[6/7] Applied WhatsApp config (proof)..."
grep -E '^(TG_SOURCE_USERNAMES|WA_PRIMARY_SOURCE|WA_MEDIA_FIRST|NEWSLETTER_MEDIA_FIX|WA_WARMUP_DONE|WA_PROMOTE_AFTER_MINUTES|MIN_WA_MESSAGE_GAP_SECONDS|STRICT_SOURCE_ONLY|QUIET_START|QUIET_END|HYBRID_QUIET|MAX_JOB_AGE_HOURS|AMAZON_TAG)=' \
  "$BRIDGE_DIR/.env" 2>/dev/null | sed 's/=.*/=***/; s/^/  /' || echo "  (no .env keys matched)"

echo "[6/7] Recent Telegram bot logs..."
tail -n 15 "$BESTGAA_DIR/logs/bot.log" || true

echo "[7/7] Recent bridge logs..."
journalctl -u tg-wa-bridge -n 25 --no-pager -l || true

# Keep only the newest five deployment backups of each type.
prune_backups() {
  local dir="$1" pattern="$2"
  mapfile -t files < <(find "$dir" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\n' 2>/dev/null | sort -rn | cut -d' ' -f2-)
  if (( ${#files[@]} > 5 )); then
    printf '%s\0' "${files[@]:5}" | xargs -0 -r rm -f --
  fi
}
prune_backups "$BESTGAA_DIR" 'main_bot.backup.*.py'
prune_backups "$BESTGAA_DIR" '.env.backup.*'
prune_backups "$BESTGAA_DIR" '.env.before-amazon-tag.*'
prune_backups "$BRIDGE_DIR" 'bridge.backup.*.js'
prune_backups "$BRIDGE_DIR" 'bridge-state.before-hotfix.*.json'
prune_backups "$BRIDGE_DIR" '.env.before-schedule.*'

echo "DUAL HOTFIX SUCCESS"
