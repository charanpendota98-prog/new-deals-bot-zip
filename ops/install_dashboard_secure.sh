#!/usr/bin/env bash
# install_dashboard_secure.sh — "Dashboard secure ga setup cheyyi."
#
#   sudo bash ops/install_dashboard_secure.sh
#
# What it does (idempotent - re-running preserves the existing token):
#   1. generates a strong DASHBOARD_TOKEN once (kept in /etc/bestgaa/dashboard.env,
#      root:ubuntu mode 640 - never in Git, never printed in full)
#   2. installs the approval gate at /usr/local/sbin/bestgaa-restart-gate so the
#      dashboard's restart button cannot start posting before your approval
#   3. installs + starts bestgaa-dashboard.service, bound to 127.0.0.1 ONLY
#   4. proves: port listens on 127.0.0.1, no token -> 401, with token -> 200
#
# Reach it with an SSH tunnel from your laptop (NEVER expose it publicly):
#   ssh -N -L 8080:127.0.0.1:8080 ubuntu@<server>
#   then open http://127.0.0.1:8080 and paste the token:
#   sudo cat /etc/bestgaa/dashboard.env
set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
UNIT_TEMPLATE="$REPO_ROOT/dashboard/bestgaa-dashboard.service"
ENV_DIR="/etc/bestgaa"
ENV_FILE="$ENV_DIR/dashboard.env"
GATE_DST="/usr/local/sbin/bestgaa-restart-gate"
SERVICE="bestgaa-dashboard"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"

SUDO=""
[[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mCHECK\033[0m  %s\n' "$*"; }
info() { printf '  ----  %s\n' "$*"; }

echo "=============================================================================="
echo " SECURE DASHBOARD SETUP — $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================================="
echo "repo: $REPO_ROOT"
echo

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 1; }
PYTHON_BIN="$(command -v python3)"
# dashboard.app needs aiohttp; reuse whatever the repo already has.
if ! "$PYTHON_BIN" -c "import aiohttp" 2>/dev/null; then
  info "aiohttp missing for $PYTHON_BIN - installing (user site)"
  "$PYTHON_BIN" -m pip install --user --quiet aiohttp || warn "pip install aiohttp failed - dashboard may not start"
fi

echo "==== 1. TOKEN (generated once, preserved on re-runs) ===="
$SUDO mkdir -p "$ENV_DIR"
if $SUDO test -f "$ENV_FILE" && $SUDO grep -q '^DASHBOARD_TOKEN=.\+' "$ENV_FILE"; then
  ok "existing DASHBOARD_TOKEN preserved ($ENV_FILE)"
else
  TOKEN="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(32))')"
  printf 'DASHBOARD_TOKEN=%s\n' "$TOKEN" | $SUDO tee "$ENV_FILE" >/dev/null
  ok "new DASHBOARD_TOKEN generated (32 random bytes)"
fi
$SUDO chown root:"$(id -un 2>/dev/null || echo ubuntu)" "$ENV_FILE" 2>/dev/null || $SUDO chown root:ubuntu "$ENV_FILE"
$SUDO chmod 640 "$ENV_FILE"
ok "token file locked down: root-readable group, mode 640"

echo
echo "==== 1b. WHICH BOT THIS DASHBOARD SERVES ===="
# The dashboard edits the RUNNING bot's .env, channels.json and database while it
# runs from this checkout. Left to guess, it wrote channels.json into the
# checkout and read a database that was not there - the panel looked empty while
# the bot posted all day. Resolve once with bestgaa/bot_paths.py, pin the answer
# in dashboard.env, and print what it resolved to.
BOT_DIR="${BESTGAA_DIR:-$(REPO_ROOT="$REPO_ROOT" python3 -c '
import os, sys
sys.path.insert(0, os.environ["REPO_ROOT"] + "/bestgaa")
import bot_paths
print(bot_paths.bot_dir())
' 2>/dev/null || true)}"
BOT_DIR="${BOT_DIR:-/home/ubuntu/bestgaa-bot/bestgaa-bot}"
if [[ ! -d "$BOT_DIR" ]]; then
  warn "bot directory $BOT_DIR does not exist - the dashboard will show no channels and no posts"
elif [[ ! -f "$BOT_DIR/.env" ]]; then
  warn "bot directory $BOT_DIR has no .env - the API-keys panel will refuse to save"
else
  ok "this dashboard serves the bot at $BOT_DIR"
fi
if $SUDO grep -q '^BESTGAA_DIR=' "$ENV_FILE" 2>/dev/null; then
  $SUDO sed -i "s|^BESTGAA_DIR=.*|BESTGAA_DIR=$BOT_DIR|" "$ENV_FILE"
else
  printf 'BESTGAA_DIR=%s\n' "$BOT_DIR" | $SUDO tee -a "$ENV_FILE" >/dev/null
fi
ok "BESTGAA_DIR pinned in $ENV_FILE (the dashboard resolves its files from it)"

echo
echo "==== 2. RESTART GATE (dashboard button cannot start posting early) ===="
if [[ -f "$HERE/bestgaa_restart_gate.sh" ]]; then
  $SUDO cp "$HERE/bestgaa_restart_gate.sh" "$GATE_DST"
  $SUDO chmod 755 "$GATE_DST"
  ok "installed $GATE_DST"
else
  warn "ops/bestgaa_restart_gate.sh missing - dashboard restart button left unguarded"
fi

echo
echo "==== 3. SYSTEMD UNIT (127.0.0.1 only) ===="
[[ -f "$UNIT_TEMPLATE" ]] || { echo "ERROR: $UNIT_TEMPLATE missing" >&2; exit 1; }
RENDERED="$(mktemp /tmp/bestgaa-dashboard.unit.XXXXXX)"
sed -e "s|@REPO_ROOT@|$REPO_ROOT|g" -e "s|@PYTHON@|$PYTHON_BIN|g" "$UNIT_TEMPLATE" >"$RENDERED"
$SUDO cp "$RENDERED" "/etc/systemd/system/$SERVICE.service"
rm -f "$RENDERED"
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE" >/dev/null 2>&1 || true
if $SUDO systemctl restart "$SERVICE"; then
  ok "$SERVICE installed and started"
else
  warn "$SERVICE failed to start - journalctl -u $SERVICE -n 40"
fi

echo
echo "==== 4. PROOF: LOCALHOST-ONLY + TOKEN-PROTECTED ===="
sleep 2
if ss -ltn 2>/dev/null | grep -qE "127\.0\.0\.1:$DASHBOARD_PORT\b"; then
  ok "listening on 127.0.0.1:$DASHBOARD_PORT (loopback only)"
else
  warn "nothing listening on 127.0.0.1:$DASHBOARD_PORT - journalctl -u $SERVICE -n 40"
fi
if ss -ltn 2>/dev/null | grep -qE "0\.0\.0\.0:$DASHBOARD_PORT\b|\*:$DASHBOARD_PORT\b"; then
  warn "DASHBOARD IS EXPOSED ON ALL INTERFACES - fix DASHBOARD_HOST immediately"
else
  ok "NOT listening on public interfaces"
fi
TOKEN_VALUE="$($SUDO grep -m1 '^DASHBOARD_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
CODE_NO_TOKEN="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$DASHBOARD_PORT/api/status" || echo 000)"
CODE_TOKEN="$(curl -s -o /dev/null -w '%{http_code}' -H "X-Dashboard-Token: $TOKEN_VALUE" "http://127.0.0.1:$DASHBOARD_PORT/api/status" || echo 000)"
if [[ "$CODE_NO_TOKEN" == "401" && "$CODE_TOKEN" == "200" ]]; then
  ok "no token -> 401, with token -> 200 (auth is enforcing)"
elif [[ "$CODE_NO_TOKEN" == "000" ]]; then
  warn "dashboard did not answer on 127.0.0.1:$DASHBOARD_PORT yet"
else
  warn "unexpected auth behaviour: no-token=$CODE_NO_TOKEN with-token=$CODE_TOKEN (want 401/200)"
fi

echo
echo "==== 5. THE DASHBOARD AND THE BOT AGREE ON THEIR FILES ===="
RESOLVED="$(BESTGAA_DIR="$(sed -n 's/^BESTGAA_DIR=//p' "$ENV_FILE" | tail -n1)" \
  python3 "$REPO_ROOT/bestgaa/bot_paths.py" 2>/dev/null || true)"
if [[ -n "$RESOLVED" ]]; then
  while IFS= read -r line; do [[ -n "$line" ]] && info "${line#  }"; done <<< "$RESOLVED"
fi
DB_PATH="$(sed -n 's/^BESTGAA_DIR=//p' "$ENV_FILE" | tail -n1)/bestgaa.sqlite3"
[[ -f "$DB_PATH" ]] && ok "database present: $DB_PATH" \
  || warn "no database at $DB_PATH - set BOT_DB_PATH in $ENV_FILE"
CH_PATH="$(sed -n 's/^BESTGAA_DIR=//p' "$ENV_FILE" | tail -n1)/channels.json"
if [[ -f "$CH_PATH" ]]; then
  ok "channels.json present: $CH_PATH"
else
  info "no channels.json yet at $CH_PATH (the bot writes it on the first save)"
fi

echo
echo "=============================================================================="
echo " DASHBOARD READY (secure)"
echo "=============================================================================="
echo "  tunnel from your laptop:"
echo "      ssh -N -L 8080:127.0.0.1:$DASHBOARD_PORT ubuntu@<server>"
echo "  browser:  http://127.0.0.1:8080"
echo "  token:    sudo cat $ENV_FILE        <- SECRET, do not share"
echo
echo "  The restart button is gated: it refuses until"
echo "  'sudo bash ops/approve_posting.sh --i-approve-posting' has been run."
