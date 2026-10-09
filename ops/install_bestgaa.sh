#!/usr/bin/env bash
# FIRST-TIME installer for the BestGAA Telegram affiliate bot (bestgaa.service).
#
# Runs on the Oracle server (user: ubuntu). Upload this script next to the
# bestgaa source files, or keep the repo layout (clone/scp the whole repo) —
# source files are found next to this script first, then in ../bestgaa:
#
#   scp -r . ubuntu@SERVER:~/bestgaa-upload/
#   ssh ubuntu@SERVER
#   cd ~/bestgaa-upload && bash ops/install_bestgaa.sh
#
# .env — picked in priority order:
#   1. an existing complete .env in the app dir (kept, timestamped backup)
#   2. ./install_bestgaa.sh /path/to/old_main_bot.py  (zero-touch migration
#      from a legacy bot with hardcoded API_ID/API_HASH/EK_KEY/OUR_TAG)
#   3. interactive prompts (secret values are never printed)
#
# On a completely fresh machine the script also does an interactive one-time
# Telegram login (phone + code) so the session file exists before systemd
# starts the bot.
#
# Updates to an ALREADY INSTALLED bot: use bestgaa/deploy_bestgaa.sh, not
# this installer (it refuses to clobber a running install).
#
set -Eeuo pipefail

APP="/home/ubuntu/bestgaa-bot/bestgaa-bot"
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVICE="bestgaa"

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { awk 'NR>1{if($0 ~ /^#/) print substr($0, 2); else exit}' "$0"; exit 0; }

SRC=""
for cand in "$HERE" "$HERE/../bestgaa"; do
  if [[ -f "$cand/main_bot_new.py" && -f "$cand/requirements.txt" && -f "$cand/bestgaa.service" ]]; then
    SRC="$cand"
    break
  fi
done
if [[ -z "$SRC" ]]; then
  echo "ERROR: bestgaa source files not found next to this script or in ../bestgaa" >&2
  echo "       Needed: main_bot_new.py, requirements.txt, bestgaa.service, migrate_legacy_env.py" >&2
  exit 1
fi

echo "[1/8] Preparing fresh install directory..."
if [[ -f "$APP/main_bot.py" ]]; then
  echo "ERROR: bestgaa already installed at $APP" >&2
  echo "       This is the FIRST-TIME installer. To update the running bot, upload the new" >&2
  echo "       bestgaa files into that directory and run ./deploy_bestgaa.sh instead." >&2
  exit 1
fi
mkdir -p "$APP/logs" "$APP/media"
cp "$SRC/main_bot_new.py" "$SRC/requirements.txt" "$SRC/bestgaa.service" "$APP/"
if [[ -f "$SRC/migrate_legacy_env.py" ]]; then
  cp "$SRC/migrate_legacy_env.py" "$APP/"
fi

echo "[2/8] Checking system packages..."
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found (sudo apt-get install -y python3)" >&2; exit 1; }
echo "       $(python3 --version 2>&1)"
if ! python3 -m pip --version >/dev/null 2>&1; then
  echo "       python3-pip missing — installing..."
  sudo apt-get update -qq
  sudo apt-get install -y python3-pip
fi

echo "[3/8] Creating .env (secrets are not printed)..."
ENV_FILE="$APP/.env"
NEED_CREDS=1
if [[ -f "$ENV_FILE" ]] \
  && ! grep -q "REPLACE_WITH_NEW_" "$ENV_FILE" \
  && grep -qE "^TELEGRAM_API_ID=.+" "$ENV_FILE" \
  && grep -qE "^TELEGRAM_API_HASH=.+" "$ENV_FILE" \
  && grep -qE "^EARNKARO_API_KEY=.+" "$ENV_FILE" \
  && grep -qE "^AMAZON_TAG=.+" "$ENV_FILE"; then
  cp "$ENV_FILE" "$ENV_FILE.kept.$(date +%Y%m%d_%H%M%S)"
  echo "       Existing complete .env kept (timestamped backup made)."
  NEED_CREDS=0
fi
if (( NEED_CREDS )); then
  cd "$APP"
  if [[ -n "${1:-}" ]]; then
    echo "       Migrating credentials from legacy bot: $1"
    python3 "$APP/migrate_legacy_env.py" "$1"
  else
    [[ -t 0 ]] || { echo "ERROR: interactive terminal required for credential prompts (or pass a legacy bot path as argument 1)" >&2; exit 1; }
    read -rp "Telegram API_ID (my.telegram.org): " TG_API_ID
    read -rsp "Telegram API_HASH: " TG_API_HASH; echo
    # The deploy tooling hands the key in through the environment
    # (EARNKARO_API_KEY=... ./install_bestgaa.sh); interactively it is asked for.
    # Either way the value is validated below by decoding it.
    if [[ -n "${EARNKARO_API_KEY:-}" ]]; then
      EK_KEY="$EARNKARO_API_KEY"
      echo "EarnKaro API key: using EARNKARO_API_KEY from the environment"
    else
      read -rsp "EarnKaro API key: " EK_KEY; echo
    fi
    # USER RULE (2026-09-06): "kothaga thiskunna mama086-21 idi manade". The old
    # default here was deals0911-21 - the SOURCE's tag - so a fresh install used
    # to write a stranger's tag into .env, and the very next deploy_bestgaa.sh
    # run aborted on it. The default is now OUR tag, and anything the operator
    # types is checked against the same allowlist the deploy guard enforces.
    OUR_AMAZON_TAGS="mama086-21"
    read -rp "Amazon associate tag [mama086-21]: " AMZ_TAG; AMZ_TAG="${AMZ_TAG:-mama086-21}"
    until [[ ",${OUR_AMAZON_TAGS}," == *",$(echo "$AMZ_TAG" | tr '[:upper:]' '[:lower:]'),"* ]]; do
      echo "ERROR: '$AMZ_TAG' is not one of ours (${OUR_AMAZON_TAGS}) - a source's tag would credit them for our sales." >&2
      read -rp "Amazon associate tag [mama086-21]: " AMZ_TAG; AMZ_TAG="${AMZ_TAG:-mama086-21}"
    done
    # The account that earns is named INSIDE the token, so the publisher id is
    # read from it instead of being typed: a fresh install can no longer write a
    # publisher id that disagrees with the key (which silently disabled the
    # bot's foreign-publisher guard, and pointed the Flipkart affExtParam2 check
    # at the wrong account).
    TOKEN_PUBLISHER="$(python3 - "$EK_KEY" <<'PY2' 2>/dev/null || true
import base64, json, sys
try:
    payload = sys.argv[1].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    print(json.loads(base64.urlsafe_b64decode(payload.encode("ascii"))).get("earnkaro") or "")
except Exception:
    print("")
PY2
)"
    if [[ -z "${TOKEN_PUBLISHER//[[:space:]]/}" ]]; then
      echo "WARNING: the EarnKaro key is not a JWT carrying a publisher id - the bot" >&2
      echo "         will refuse to publish a converted link that carries a foreign" >&2
      echo "         Flipkart publisher. Get the token from the Affiliaters/EarnKaro" >&2
      echo "         API page (see ops/set_earnkaro_key.sh)." >&2
    fi
    EK_PUB="${TOKEN_PUBLISHER:-}"
    read -rp "EarnKaro publisher ID [${EK_PUB:-5478322}]: " EK_PUB_ANSWER
    EK_PUB="${EK_PUB_ANSWER:-${EK_PUB:-5478322}}"
    read -rp "Session name [bestgaa_fresh]: " SESSION_NAME; SESSION_NAME="${SESSION_NAME:-bestgaa_fresh}"
    read -rp "Bitly tokens, comma separated (optional): " BITLY
    if [[ -z "$TG_API_ID" || -z "$TG_API_HASH" || -z "$EK_KEY" || -z "$AMZ_TAG" ]]; then
      echo "ERROR: API_ID, API_HASH, EarnKaro key and Amazon tag are all required" >&2
      exit 1
    fi
    TMP="$ENV_FILE.tmp"
    cat > "$TMP" <<EOF
TELEGRAM_API_ID=$TG_API_ID
TELEGRAM_API_HASH=$TG_API_HASH
TELEGRAM_SESSION=$SESSION_NAME
EARNKARO_API_KEY=$EK_KEY
EARNKARO_API_URL=https://ekaro-api.affiliaters.in/api/converter/public
EARNKARO_PUBLISHER_ID=$EK_PUB
# The documented converter mode: convert the link and do nothing else. Sending
# it explicitly keeps the RESPONSE shape predictable (an unreadable response is
# a post that goes out unmonetized, not a lost post).
EARNKARO_CONVERT_OPTION=convert_only
# USER DECISION (2026-10-04): the Amazon Associates account is APPROVED for our
# tag, so the NATIVE tagged link (?tag=mama086-21) is the FIRST choice - the
# whole commission is ours, no network share. EarnKaro is the second chance for
# an Amazon shape the native path cannot build (no usable ASIN).
AMAZON_DIRECT_ASSOCIATES=true
# 'false' means "never EarnKaro for Amazon" (2026-09-06 behaviour); setting
# AMAZON_DIRECT_ASSOCIATES=false above restores the 2026-09-24 network-first
# order (EarnKaro first, native tagged link as the fallback).
AMAZON_VIA_EARNKARO=true
AMAZON_TAG=$AMZ_TAG
# USER DECISION (2026-09-06, final): "anni channels amazon tag tho cheyu, not
# only review channel". The tag earns on every owned channel.
#
# YOUR SIDE OF THIS: every channel below must be listed in Associates Central,
# with its FULL URL (t.me/LootZoneIndia11, not t.me), and must be public.
#   https://affiliate-program.amazon.in/home/account/profile/sitelist
# Amazon reads the traffic source from the tag itself, so a channel carrying
# the tag but missing from that list is the exact violation that caused the
# earlier rejection. See ops/AMAZON_SITELIST_BEFORE_TAG_EVERYWHERE.txt.
#
# The bot now protects every tagged channel automatically: copied star ratings
# and review counts are removed and "#ad (paid link)" is appended, because a
# tagged channel is a channel Amazon reviews.
AMAZON_TAG_TARGETS=all

BITLY_TOKENS=$BITLY
BOT_DB_PATH=$APP/bestgaa.sqlite3
# (No PRODUCT_DEDUP_SECONDS: the 24h same-product window is pinned in code -
# env override blocked by user rule 2026-09-04 - so a value here would be dead.)
# Same-price fallback is OFF (0): at common prices (99/499/...) the 1-hour hold
# ate real unidentified deals. Identity dedup + the pinned 24h product window
# remain the guards. Set 3600 to restore the hold as an opt-in.
PRICE_DEDUP_SECONDS=0
# Same-price gate is a fallback for posts with no ASIN/PID. Set true to
# also block a different product sharing an already-posted price.
PRICE_DEDUP_IGNORES_IDENTITY=false
QUEUE_WORKERS=8
EK_MAX_CONCURRENCY=8
POST_RETRIES=3
# Immediate dispatch (v16). Everything here is optional and clamped by the bot;
# delete a line to take the built-in default (they are the same values).
QUEUE_ORDER=newest
MAX_JOB_AGE_HOURS=6
JOB_RETRY_MAX_SECONDS=20
HTTP_TOTAL_TIMEOUT_SECONDS=12
LINK_HEALTH_CACHE_SECONDS=900
# Pre-send link check is capped at seconds (a slow page must never hold a deal).
PRESEND_CHECK_BUDGET_SECONDS=6
SOURCE_RESCAN_SECONDS=120
SOURCE_RESCAN_LIMIT=40
SOURCE_REFRESH_SECONDS=180
MAX_MEDIA_MB=45
# Zero artificial waiting: channel fan-out is back-to-back by default.
TARGET_FANOUT_GAP_MIN=0
TARGET_FANOUT_GAP_MAX=0
# v17: publish a store link EarnKaro cannot monetize as a clean untagged
# merchant link instead of losing the post. false = old behaviour (retry, then
# skip - that deal never reaches the channels).
PASSTHROUGH_UNMONETIZED=true
# Source fidelity: publish what the source published (its own hype header
# included). Set true to ALSO drop pure campaign banner lines.
STRIP_CAMPAIGN_BANNERS=false
# Night quiet 02:00-06:00 IST: Telegram posting pauses; deals keep queueing
# and go out at 06:00. Set both equal (00:00/00:00) to disable.
POST_QUIET_START=02:00
POST_QUIET_END=06:00
EOF
    chmod 600 "$TMP"
    mv "$TMP" "$ENV_FILE"
    unset TG_API_HASH EK_KEY
    echo "       .env written."
  fi
fi
for key in TELEGRAM_API_ID TELEGRAM_API_HASH EARNKARO_API_KEY AMAZON_TAG; do
  grep -qE "^${key}=.+" "$ENV_FILE" || { echo "ERROR: $key missing in .env" >&2; exit 1; }
done
chmod 600 "$ENV_FILE"

echo "[4/8] Installing Python dependencies..."
# Newer Ubuntu (24.04+) marks the system Python as PEP 668 externally-managed
# and rejects plain pip. --user installs only into ~/.local, so retrying with
# --break-system-packages is safe (it only lifts the PEP 668 refusal).
if ! python3 -m pip install --user --quiet -r "$APP/requirements.txt"; then
  echo "       PEP 668 managed environment — retrying into user site..."
  python3 -m pip install --user --quiet --break-system-packages -r "$APP/requirements.txt"
fi

echo "[5/8] Activating bot file + compiling..."
cp -a "$APP/main_bot_new.py" "$APP/main_bot.py"
python3 -m py_compile "$APP/main_bot.py"

echo "[6/8] Telegram session..."
SESSION_NAME="$(grep -E '^TELEGRAM_SESSION=' "$ENV_FILE" | cut -d= -f2-)"
SESSION_NAME="${SESSION_NAME:-bestgaa_fresh}"
if [[ -f "$APP/$SESSION_NAME.session" ]]; then
  echo "       Session file $SESSION_NAME.session present — skipping one-time login."
else
  echo "       FIRST RUN: Telegram will ask for the bot account phone number and login code."
  python3 - "$APP" "$SESSION_NAME" <<'PY'
import asyncio
import sys
from pathlib import Path

app = Path(sys.argv[1])
session_name = sys.argv[2]
env = {}
for line in (app / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()

from telethon import TelegramClient


async def run():
    client = TelegramClient(
        str(app / session_name),
        int(env["TELEGRAM_API_ID"]),
        env["TELEGRAM_API_HASH"],
    )
    await client.start()
    me = await client.get_me()
    print(f"SESSION OK: {me.first_name} (@{me.username})")
    await client.disconnect()


asyncio.run(run())
PY
fi

echo "[7/8] Installing systemd service..."
on_error() {
  echo "INSTALL FAILED — stopping the new service (first-time install, nothing to roll back to)..."
  sudo systemctl stop "$SERVICE" 2>/dev/null || true
  [[ -f "$APP/logs/bot.log" ]] && tail -n 20 "$APP/logs/bot.log" || true
  exit 1
}
trap on_error ERR

sudo cp "$APP/bestgaa.service" "/etc/systemd/system/$SERVICE.service"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sleep 12
if ! sudo systemctl is-active --quiet "$SERVICE"; then
  echo "ERROR: $SERVICE not active after start — recent journal log:" >&2
  sudo journalctl -u "$SERVICE" -n 30 --no-pager -l || true
  exit 1
fi

echo "[8/8] Verifying installed code + startup marker..."
sha256sum "$APP/main_bot.py"     | awk '{print "       live    main_bot.py sha256   = " $1}'
sha256sum "$APP/main_bot_new.py" | awk '{print "       shipped main_bot_new.py sha256 = " $1}'
if ! cmp -s "$APP/main_bot.py" "$APP/main_bot_new.py"; then
  echo "ERROR: live main_bot.py != shipped main_bot_new.py" >&2
  exit 1
fi
if tail -n 300 "$APP/logs/bot.log" | grep -Eq "BestGAA Production Bot v1[0-9] starting|LIVE \| sources="; then
  echo "       startup marker found in bot.log"
else
  echo "       WARNING: startup marker not yet visible in last 300 lines"
  echo "         (service may still be connecting). Check the logs below."
fi
trap - ERR

sudo systemctl status "$SERVICE" --no-pager --lines=10
tail -n 10 "$APP/logs/bot.log" 2>/dev/null || true
echo
echo "INSTALL COMPLETE"
echo "Live logs:     tail -f $APP/logs/bot.log"
echo "Bot only:      systemctl is-active $SERVICE"
echo "Bridge (separate unit, same pattern): tg-wa-bridge/install_bridge.sh"
echo "Future bot updates: bestgaa/deploy_bestgaa.sh — NOT this installer."
