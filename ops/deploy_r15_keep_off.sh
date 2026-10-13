#!/usr/bin/env bash
# deploy_r15_keep_off.sh — PR #15 rollout, SAFE MODE: everything is deployed and
# verified while the bot services stay OFF. "Bot services OFF lone unchi verify
# cheyyi; naa approval tarvata posting start cheyyi."
#
#   ./ops/deploy_r15_keep_off.sh                 # the whole safe rollout
#   ./ops/deploy_r15_keep_off.sh --with-tests    # + full offline suites before shipping
#   ./ops/deploy_r15_keep_off.sh --dry-run       # print the plan, change nothing
#
# Order (exactly what was asked):
#   1. STOP + DISABLE bestgaa & tg-wa-bridge        (posting cannot start - even after a reboot)
#   2. BACKUP db + channels.json + Telegram session + WhatsApp state   (ops/backup_live_state.sh)
#   3. DEPLOY the merged PR #15 code without starting anything
#      (bundles -> .env migration -> deps -> syntax/self-tests -> hash proof)
#   4. DASHBOARD secure setup: token + localhost-only unit + restart gate
#   5. VERIFY with services OFF and print the approval step.
#
# Posting starts ONLY with:  sudo bash ops/approve_posting.sh --i-approve-posting
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

HOME_DIR="${HOME:-/home/ubuntu}"
BESTGAA_DIR="${BESTGAA_DIR:-$HOME_DIR/bestgaa-bot/bestgaa-bot}"
BRIDGE_DIR="${BRIDGE_DIR:-$HOME_DIR/tg-wa-bridge}"
SERVICE_BOT="${SERVICE_BOT:-bestgaa}"
SERVICE_BRIDGE="${SERVICE_BRIDGE:-tg-wa-bridge}"
PENDING_MARK="${PENDING_MARK:-/etc/bestgaa/POSTING_PENDING}"

DO_TESTS=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --with-tests) DO_TESTS=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    -h|--help)    sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SUDO=""
[[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
FAILED=()
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mCHECK\033[0m  %s\n' "$*"; FAILED+=("$*"); }
info() { printf '  ----  %s\n' "$*"; }
step() { echo; echo "==== $* ===="; }

echo "=============================================================================="
echo " PR #15 SAFE ROLLOUT (services stay OFF) — $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================================="
echo "repo      : $REPO"
echo "commit    : $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')  $(git -C "$REPO" branch --show-current 2>/dev/null || true)"
echo "bot dir   : $BESTGAA_DIR"
echo "bridge dir: $BRIDGE_DIR"
echo "mode      : tests=$DO_TESTS dry_run=$DRY_RUN  (posting: OFF until approval)"
echo

if [[ "$DRY_RUN" == "1" ]]; then
  echo "PLAN (dry run - nothing will be changed)"
  echo "  1. systemctl stop + disable $SERVICE_BOT $SERVICE_BRIDGE     # posting OFF, reboot-safe"
  echo "  2. bash ops/backup_live_state.sh                             # db + channels + TG session + WA state"
  echo "  3. BESTGAA_KEEP_OFF=1 ops/deploy_and_verify.sh --no-pull --keep-off$([[ $DO_TESTS == 1 ]] && echo ' --with-tests')"
  echo "  4. sudo bash ops/install_dashboard_secure.sh                 # token + localhost-only + restart gate"
  echo "  5. verify panel + write /etc/bestgaa/POSTING_PENDING"
  echo
  echo "  THEN (only after your approval):"
  echo "      sudo bash ops/approve_posting.sh --i-approve-posting"
  echo
  echo "Nothing was changed. Run again without --dry-run to do it for real."
  exit 0
fi

step "1/5 STOP + DISABLE POSTING SERVICES (OFF, and they stay OFF)"
for svc in "$SERVICE_BOT" "$SERVICE_BRIDGE"; do
  if $SUDO systemctl stop "$svc" 2>/dev/null; then ok "stopped $svc"; else info "$svc was not running"; fi
  if $SUDO systemctl disable "$svc" >/dev/null 2>&1; then ok "disabled $svc (will not auto-start on reboot)"; fi
  if $SUDO systemctl is-active --quiet "$svc" 2>/dev/null; then
    warn "$svc is STILL ACTIVE after stop - investigate before deploying"
  else
    ok "$svc confirmed inactive"
  fi
done

step "2/5 BACKUP (db + channel config + Telegram session + WhatsApp state)"
if bash "$HERE/backup_live_state.sh"; then
  ok "backup_live_state.sh finished - archive under $HOME_DIR/bestgaa-backups/"
else
  warn "backup reported problems - STOPPING HERE; nothing was deployed"
  exit 1
fi

step "3/5 DEPLOY PR #15 CODE (no service is started)"
DEPLOY_ARGS=(--no-pull --keep-off --no-restart)
[[ "$DO_TESTS" == "1" ]] && DEPLOY_ARGS+=(--with-tests)
if BESTGAA_KEEP_OFF=1 bash "$HERE/deploy_and_verify.sh" "${DEPLOY_ARGS[@]}"; then
  ok "deploy_and_verify.sh (--keep-off) finished"
else
  warn "deploy_and_verify.sh reported CHECK lines above - read them first"
fi

step "4/5 DASHBOARD SECURE SETUP (token, localhost-only, restart gate)"
if bash "$HERE/install_dashboard_secure.sh"; then
  ok "install_dashboard_secure.sh finished"
else
  warn "dashboard setup reported problems above"
fi

step "5/5 FINAL VERIFY (services OFF, code shipped, dashboard locked)"
echo
echo "  -- posting services must be OFF --"
for svc in "$SERVICE_BOT" "$SERVICE_BRIDGE"; do
  ACTIVE="$($SUDO systemctl is-active "$svc" 2>/dev/null || true)"
  ENABLED="$($SUDO systemctl is-enabled "$svc" 2>/dev/null || true)"
  if [[ "$ACTIVE" == "inactive" || "$ACTIVE" == "failed" || -z "$ACTIVE" ]]; then
    ok "$svc is $ACTIVE"
  else
    warn "$svc is '$ACTIVE' - posting is NOT off; stop it: sudo systemctl stop $svc"
  fi
  if [[ "$ENABLED" == "disabled" ]]; then
    ok "$svc is disabled (reboot-safe)"
  else
    warn "$svc is '$ENABLED' - disable it: sudo systemctl disable $svc"
  fi
done

echo
echo "  -- shipped code is on disk --"
for PAIR in "$REPO/bestgaa/main_bot_new.py|$BESTGAA_DIR/main_bot.py" \
            "$REPO/tg-wa-bridge/bridge.js|$BRIDGE_DIR/bridge.js"; do
  SRC="${PAIR%%|*}"; DST="${PAIR##*|}"
  if [[ -f "$DST" ]] && [[ "$(sha256sum "$SRC" | cut -c1-16)" == "$(sha256sum "$DST" | cut -c1-16)" ]]; then
    ok "$(basename "$DST") == repo $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo 'tree')"
  else
    warn "$DST does not match the repo copy - deploy did not land"
  fi
done

echo
echo "  -- dashboard locked down --"
if ss -ltn 2>/dev/null | grep -qE "127\.0\.0\.1:8080\b"; then
  ok "dashboard on 127.0.0.1:8080 only"
else
  warn "dashboard not listening on 127.0.0.1:8080"
fi
if [[ -f /etc/bestgaa/POSTING_APPROVED ]]; then
  warn "POSTING_APPROVED already exists - posting may already be allowed (remove it to re-lock)"
else
  ok "no approval mark - restart gate will refuse to start posting"
fi

echo
echo "  -- fresh backup --"
LATEST="$(ls -1t "$HOME_DIR"/bestgaa-backups/pre-r15-*.tar.gz 2>/dev/null | head -1 || true)"
if [[ -n "$LATEST" ]]; then
  ok "backup exists: $LATEST"
else
  warn "no backup archive found - this should not happen (step 2 said OK?)"
fi

# Mark the box as awaiting approval (the gate + the runbook read this).
if $SUDO mkdir -p "$(dirname "$PENDING_MARK")" 2>/dev/null; then
  echo "$(date '+%Y-%m-%d %H:%M:%S %Z') r15 deployed; posting OFF pending operator approval" | $SUDO tee "$PENDING_MARK" >/dev/null 2>&1 || true
fi

echo
echo "=============================================================================="
if (( ${#FAILED[@]} )); then
  echo " SAFE ROLLOUT: ${#FAILED[@]} CHECK line(s) to read (above)."
  echo " Posting is still OFF. Fix the CHECK lines, then re-run this script."
else
  echo " SAFE ROLLOUT: DEPLOYED AND VERIFIED — POSTING IS OFF."
fi
echo "=============================================================================="
echo
echo "  What is live:    PR #15 code on disk (bot + bridge), dashboard on 127.0.0.1:8080"
echo "  What is NOT live: posting (both services stopped + disabled)"
echo
echo "  Your move (only when you are ready for posts to go out):"
echo "      sudo bash $HERE/approve_posting.sh --i-approve-posting"
echo
echo "  Until then the dashboard restart button also refuses (approval gate)."
