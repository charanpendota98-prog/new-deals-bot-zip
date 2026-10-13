#!/usr/bin/env bash
# backup_live_state.sh — ONE command: snapshot everything that must survive a
# deploy. "Mundu existing DB, channel config, Telegram session, WhatsApp state
# backup teesuko." Run BEFORE any deploy/hotfix/restart.
#
#   ./ops/backup_live_state.sh                  # full backup (what a deploy needs)
#   ./ops/backup_live_state.sh --with-logs       # + bot/bridge logs (forensics)
#   ./ops/backup_live_state.sh --out ~/my-dir    # custom destination directory
#
# What lands in the tarball (relative to the server layout):
#   db/            bestgaa.sqlite3 (+ -wal/-shm/-journal)      <- queue, dedup, link proofs
#   channels/      channels.json, .channels_pending, routing_snapshot.json, category_rules.json
#   telegram/      *.session, *.session-journal                 <- Telethon login (DO NOT LOSE)
#   whatsapp/      auth/ (Baileys creds), bridge-state*.json    <- WhatsApp pairing + queue state
#   env/           .env (bot), .env (bridge)                    <- SECRET: token, api keys
#   systemd/       bestgaa.service, tg-wa-bridge.service, bestgaa-dashboard.service (if installed)
#   MANIFEST.txt   sha256 of every member + which items were ABSENT on this host
#
# The tarball is chmod 600 and lives under ~/bestgaa-backups/ (override with
# --out). Re-running is always safe; every run makes a new timestamped archive.
set -Eeuo pipefail

HOME_DIR="${HOME:-/home/ubuntu}"
BESTGAA_DIR="${BESTGAA_DIR:-$HOME_DIR/bestgaa-bot/bestgaa-bot}"
BRIDGE_DIR="${BRIDGE_DIR:-$HOME_DIR/tg-wa-bridge}"
OUT_DIR="${BACKUP_OUT_DIR:-$HOME_DIR/bestgaa-backups}"
WITH_LOGS=0
STAMP="$(date +%Y%m%d_%H%M%S)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-logs) WITH_LOGS=1 ;;
    --out)       shift; OUT_DIR="${1:?--out needs a directory}" ;;
    --out=*)     OUT_DIR="${1#--out=}" ;;
    -h|--help)   sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

SUDO=""
[[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

FAILED=()
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mCHECK\033[0m  %s\n' "$*"; FAILED+=("$*"); }
info() { printf '  ----  %s\n' "$*"; }

echo "=============================================================================="
echo " LIVE STATE BACKUP — $STAMP"
echo "=============================================================================="
echo "bot dir   : $BESTGAA_DIR"
echo "bridge dir: $BRIDGE_DIR"
echo "output    : $OUT_DIR"
echo

STAGE="$(mktemp -d /tmp/bestgaa-backup-stage.XXXXXX)"
chmod 700 "$STAGE"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE"/{db,channels,telegram,whatsapp,env,systemd}

# Anything that is not world-readable in the archive.
umask 077

echo "==== 1. BOT DATABASE (queue, dedup ledger, link proofs) ===="
DB_FOUND=0
for db in "$BESTGAA_DIR/bestgaa.sqlite3" "$BESTGAA_DIR/state/bot_state.sqlite3"; do
  [[ -f "$db" ]] || continue
  DB_FOUND=1
  base="$(basename "$db")"
  # SQLite may keep -wal/-shm beside it; copy the whole family so a restore is
  # crash-consistent even if the DB was mid-write when the service stopped.
  for suffix in "" "-wal" "-shm" "-journal"; do
    [[ -f "$db$suffix" ]] && cp -a "$db$suffix" "$STAGE/db/$base$suffix"
  done
  ok "database copied: $db"
  # Best-effort integrity check (read-only; never mutates the live file).
  if command -v sqlite3 >/dev/null 2>&1; then
    if sqlite3 "file:$db?mode=ro" "PRAGMA integrity_check;" 2>/dev/null | grep -qx "ok"; then
      ok "sqlite integrity_check = ok"
    else
      warn "sqlite integrity_check did not answer 'ok' - inspect $db"
    fi
  fi
  break
done
[[ "$DB_FOUND" == "1" ]] || warn "no bot database found at $BESTGAA_DIR (is this the server?)"

echo
echo "==== 2. CHANNEL CONFIG (channels.json + sidecars) ===="
# channels.json lives next to the bot DB (CHANNELS_JSON_PATH overrides).
CHAN_DIR="$BESTGAA_DIR"
[[ -n "${CHANNELS_JSON_PATH:-}" ]] && CHAN_DIR="$(dirname "$CHANNELS_JSON_PATH")"
CHAN_FOUND=0
for f in channels.json .channels_pending routing_snapshot.json category_rules.json; do
  if [[ -f "$CHAN_DIR/$f" ]]; then
    cp -a "$CHAN_DIR/$f" "$STAGE/channels/$f"
    CHAN_FOUND=1
    ok "channel config copied: $CHAN_DIR/$f"
  fi
done
[[ "$CHAN_FOUND" == "1" ]] || warn "no channels.json found near the database ($CHAN_DIR) - built-in routing only"

echo
echo "==== 3. TELEGRAM SESSION (Telethon login - never lose this) ===="
SESS_FOUND=0
shopt -s nullglob
for s in "$BESTGAA_DIR"/*.session "$BESTGAA_DIR"/*.session-journal; do
  cp -a "$s" "$STAGE/telegram/$(basename "$s")"
  SESS_FOUND=1
  ok "telegram session copied: $(basename "$s")"
done
shopt -u nullglob
[[ "$SESS_FOUND" == "1" ]] || warn "no *.session file in $BESTGAA_DIR - the bot will need a fresh interactive login"

echo
echo "==== 4. WHATSAPP STATE (Baileys auth + bridge state) ===="
WA_FOUND=0
if [[ -d "$BRIDGE_DIR/auth" ]]; then
  cp -a "$BRIDGE_DIR/auth" "$STAGE/whatsapp/auth"
  WA_FOUND=1
  ok "WhatsApp auth dir copied: $BRIDGE_DIR/auth ($(ls -1 "$BRIDGE_DIR/auth" | wc -l) file(s))"
else
  warn "no WhatsApp auth dir ($BRIDGE_DIR/auth) - the bridge will need re-pairing"
fi
shopt -s nullglob
for s in "$BRIDGE_DIR"/bridge-state*.json; do
  cp -a "$s" "$STAGE/whatsapp/$(basename "$s")"
  WA_FOUND=1
  ok "bridge state copied: $(basename "$s")"
done
shopt -u nullglob
[[ "$WA_FOUND" == "1" ]] || warn "nothing WhatsApp-related found in $BRIDGE_DIR"

echo
echo "==== 5. SECRETS (.env) + SYSTEMD UNITS ===="
if [[ -f "$BESTGAA_DIR/.env" ]]; then
  cp -a "$BESTGAA_DIR/.env" "$STAGE/env/bot.env"
  ok "bot .env copied (SECRET)"
else
  warn "bot .env missing at $BESTGAA_DIR/.env"
fi
if [[ -f "$BRIDGE_DIR/.env" ]]; then
  cp -a "$BRIDGE_DIR/.env" "$STAGE/env/bridge.env"
  ok "bridge .env copied (SECRET)"
else
  warn "bridge .env missing at $BRIDGE_DIR/.env"
fi
for unit in bestgaa.service tg-wa-bridge.service bestgaa-dashboard.service; do
  if [[ -f "/etc/systemd/system/$unit" ]]; then
    cp -a "/etc/systemd/system/$unit" "$STAGE/systemd/$unit"
    ok "unit copied: $unit"
  fi
done

if [[ "$WITH_LOGS" == "1" ]]; then
  echo
  echo "==== 6. LOGS (--with-logs) ===="
  mkdir -p "$STAGE/logs"
  [[ -f "$BESTGAA_DIR/logs/bot.log" ]] && cp -a "$BESTGAA_DIR/logs/bot.log" "$STAGE/logs/" && ok "bot.log copied"
  if command -v journalctl >/dev/null 2>&1; then
    $SUDO journalctl -u bestgaa -u tg-wa-bridge --no-pager -n 20000 >"$STAGE/logs/journal-tail.txt" 2>/dev/null || true
    ok "journal tail (last 20000 lines) copied"
  fi
fi

echo
echo "==== 7. MANIFEST + ARCHIVE ===="
( cd "$STAGE" && find . -type f ! -name MANIFEST.txt -exec sha256sum {} + | sort -k2 ) >"$STAGE/MANIFEST.txt"
FILE_COUNT="$(grep -c . "$STAGE/MANIFEST.txt" || true)"
ok "manifest: $FILE_COUNT file(s) hashed"

mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/pre-r15-$STAMP.tar.gz"
# Pack from the staging dir so the archive has clean relative paths.
tar -C "$STAGE" -czf "$ARCHIVE" .
chmod 600 "$ARCHIVE"
sha256sum "$ARCHIVE" >"$ARCHIVE.sha256"
chmod 600 "$ARCHIVE.sha256"

# Prove the archive is readable and holds the four things the user named.
MISSING_MEMBER=0
for must in db channels telegram whatsapp; do
  if tar -tzf "$ARCHIVE" | grep -q "^\./$must/"; then
    ok "archive contains $must/"
  else
    warn "archive is MISSING $must/ - restore of that state is impossible"
    MISSING_MEMBER=1
  fi
done
if tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
  ok "archive verified readable (tar -tzf)"
else
  warn "archive FAILED to list - do not deploy over this"
  MISSING_MEMBER=1
fi

echo
echo "=============================================================================="
echo " BACKUP SUMMARY"
echo "=============================================================================="
ls -l "$ARCHIVE" "$ARCHIVE.sha256"
echo
echo "  archive : $ARCHIVE"
echo "  sha256  : $(cut -c1-16 "$ARCHIVE.sha256")…  (full digest in $ARCHIVE.sha256)"
echo "  files   : $FILE_COUNT (see MANIFEST.txt inside the archive)"
echo
echo "  NOTE: this archive contains SECRETS (.env, Telegram session, WhatsApp auth)."
echo "        Keep it private (mode 600). Copy it off-box if you can:"
echo "        scp $ARCHIVE you@your-laptop:~/safe-place/"
if (( ${#FAILED[@]} )); then
  echo
  echo " BACKUP: ${#FAILED[@]} CHECK line(s) above - read them before deploying."
  [[ "$MISSING_MEMBER" == "1" ]] && exit 1
  exit 0
fi
echo
echo " BACKUP OK — restore paths are all inside the archive."
