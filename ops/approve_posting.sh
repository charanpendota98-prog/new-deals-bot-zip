#!/usr/bin/env bash
# approve_posting.sh — the ONE command that turns posting ON. "Naa approval
# tarvata posting start cheyyi" means: nothing starts until YOU run this, with
# the explicit flag, on purpose.
#
#   sudo bash ops/approve_posting.sh --i-approve-posting
#   sudo bash ops/approve_posting.sh --i-approve-posting --skip-backup-check
#
# What it does:
#   1. refuses to run without --i-approve-posting (no accidents)
#   2. requires a fresh backup (today's, < 24h) unless --skip-backup-check
#   3. writes the approval mark the restart gate checks
#   4. enables + starts bestgaa + tg-wa-bridge (posting begins)
#   5. proves both services came up on the shipped code
set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${HOME:-/home/ubuntu}"
BACKUP_DIR="${BACKUP_OUT_DIR:-$HOME_DIR/bestgaa-backups}"
SERVICE_BOT="${SERVICE_BOT:-bestgaa}"
SERVICE_BRIDGE="${SERVICE_BRIDGE:-tg-wa-bridge}"
APPROVAL_MARK="${APPROVAL_MARK:-/etc/bestgaa/POSTING_APPROVED}"
PENDING_MARK="${PENDING_MARK:-/etc/bestgaa/POSTING_PENDING}"

APPROVED=0
SKIP_BACKUP_CHECK=0
for arg in "$@"; do
  case "$arg" in
    --i-approve-posting) APPROVED=1 ;;
    --skip-backup-check) SKIP_BACKUP_CHECK=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SUDO=""
[[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mCHECK\033[0m  %s\n' "$*"; }

echo "=============================================================================="
echo " POSTING APPROVAL — $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================================="

if [[ "$APPROVED" != "1" ]]; then
  echo
  echo "REFUSED. This command STARTS LIVE POSTING to Telegram + WhatsApp."
  echo "If that is what you want, run:"
  echo "    sudo bash $0 --i-approve-posting"
  exit 1
fi

echo
echo "==== 1. FRESH BACKUP EXISTS? ===="
if [[ "$SKIP_BACKUP_CHECK" == "1" ]]; then
  warn "backup freshness check SKIPPED (--skip-backup-check)"
else
  LATEST="$(ls -1t "$BACKUP_DIR"/pre-r15-*.tar.gz 2>/dev/null | head -1 || true)"
  if [[ -z "$LATEST" ]]; then
    echo "REFUSED: no backup found in $BACKUP_DIR." >&2
    echo "         Run: bash $HERE/backup_live_state.sh   (then re-approve)" >&2
    exit 1
  fi
  AGE=$(( ( $(date +%s) - $(stat -c %Y "$LATEST") ) / 3600 ))
  if (( AGE > 24 )); then
    echo "REFUSED: newest backup is ${AGE}h old (>24h): $LATEST" >&2
    echo "         Take a fresh one: bash $HERE/backup_live_state.sh" >&2
    exit 1
  fi
  ok "fresh backup ${AGE}h old: $LATEST"
fi

echo
echo "==== 2. WRITING APPROVAL MARK ===="
$SUDO mkdir -p "$(dirname "$APPROVAL_MARK")"
echo "$(date '+%Y-%m-%d %H:%M:%S %Z') by $(id -un) via approve_posting.sh" | $SUDO tee "$APPROVAL_MARK" >/dev/null
$SUDO chmod 644 "$APPROVAL_MARK"
$SUDO rm -f "$PENDING_MARK"
ok "approval mark written: $APPROVAL_MARK (restart gate now allows restarts)"

echo
echo "==== 3. STARTING SERVICES (posting goes LIVE now) ===="
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_BOT" "$SERVICE_BRIDGE" >/dev/null 2>&1 || true
if $SUDO systemctl start "$SERVICE_BOT" "$SERVICE_BRIDGE"; then
  ok "systemctl start $SERVICE_BOT $SERVICE_BRIDGE"
else
  warn "systemctl start reported a problem - inspect: journalctl -u $SERVICE_BOT -u $SERVICE_BRIDGE -n 80"
fi

echo
echo "==== 4. PROOF BOTH CAME UP ===="
sleep 12
for svc in "$SERVICE_BOT" "$SERVICE_BRIDGE"; do
  if $SUDO systemctl is-active --quiet "$svc"; then
    ok "$svc is active"
  else
    warn "$svc is NOT active - journalctl -u $svc -n 80"
  fi
  if $SUDO systemctl is-enabled --quiet "$svc" 2>/dev/null; then
    ok "$svc is enabled (survives reboots)"
  fi
done
WANT_VERSION="$(grep -m1 -oE 'BestGAA Production Bot v[0-9.]+' "$HERE/../bestgaa/main_bot_new.py" 2>/dev/null | sed 's/^BestGAA Production Bot //' || true)"
BOOT_LINE="BestGAA Production Bot ${WANT_VERSION:-v} starting"
if $SUDO journalctl -u "$SERVICE_BOT" -n 200 --no-pager 2>/dev/null | grep -qF "BestGAA Production Bot"; then
  ok "bot boot line in the journal: $($SUDO journalctl -u "$SERVICE_BOT" -n 200 --no-pager | grep -oE 'BestGAA Production Bot v[0-9.]+ starting' | tail -1)"
else
  warn "no bot boot line yet - journalctl -u $SERVICE_BOT -n 80"
fi

echo
echo "==== 5. RECENT LOGS ===="
$SUDO journalctl -u "$SERVICE_BOT" -n 12 --no-pager -l 2>/dev/null | sed 's/^/  bot   | /' || true
$SUDO journalctl -u "$SERVICE_BRIDGE" -n 12 --no-pager -l 2>/dev/null | sed 's/^/  bridge| /' || true

echo
echo "=============================================================================="
echo " POSTING IS LIVE"
echo "=============================================================================="
echo "  next (optional, from the repo checkout):"
echo "      python3 ops/conversion_report.py          # per-route verdict"
echo "      python3 ops/quality_audit.py --limit 100  # last links clean?"
echo "  logs:"
echo "      journalctl -u $SERVICE_BOT -f"
echo "      journalctl -u $SERVICE_BRIDGE -f"
