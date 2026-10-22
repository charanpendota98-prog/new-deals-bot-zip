#!/usr/bin/env bash
# Where is the dashboard, is it up, and what exactly do I open?
#
# The dashboard listens on 127.0.0.1:8080 on purpose: it can restart the bot and
# change routing, so it is never exposed to the internet. The operator reaches
# it through an SSH tunnel, which is why "what link do I open?" needs a tool
# instead of a wiki page.
#
#   ./ops/dashboard_link.sh              # service state + tunnel command + link
#   ./ops/dashboard_link.sh --paste      # same, one line that sets up the tunnel
#   ./ops/dashboard_link.sh --nginx      # optional public reverse-proxy config
#   ./ops/dashboard_link.sh --json       # machine-readable (no token, ever)
#   ./ops/dashboard_link.sh --user ubuntu --server 1.2.3.4 --port 8080
#
# It prints the token MASKED (last 4 characters) and never in full, never in
# --json, and never in a log it writes (it writes nothing). Nothing is started,
# restarted or changed: this is a read-only convenience around `systemctl`,
# `curl` and the environment file.
#
# Exit codes:  0 reachable/healthy   1 service not running   2 could not check
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="${DASHBOARD_SERVICE:-bestgaa-dashboard}"
ENV_FILE="${DASHBOARD_ENV_FILE:-/etc/bestgaa/dashboard.env}"
HOST="${DASHBOARD_REMOTE_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8080}"
SSH_USER="${DASHBOARD_SSH_USER:-${USER:-ubuntu}}"
SERVER=""
MODE="human"
LOCAL_PORT=""

usage() { sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) MODE="json" ;;
    --nginx) MODE="nginx" ;;
    --paste) MODE="paste" ;;
    --user) SSH_USER="${2:-}"; shift ;;
    --server) SERVER="${2:-}"; shift ;;
    --port) PORT="${2:-}"; shift ;;
    --local-port) LOCAL_PORT="${2:-}"; shift ;;
    --env-file) ENV_FILE="${2:-}"; shift ;;
    --service) SERVICE="${2:-}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown option: %s (try --help)\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

# ---- who is this machine, from the operator's point of view -----------------
if [[ -z "$SERVER" ]]; then
  SERVER="${DASHBOARD_SERVER_HOST:-}"
fi
if [[ -z "$SERVER" ]]; then
  # First non-loopback IPv4 address. `hostname -I` is coreutils, not everywhere.
  SERVER="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
[[ -z "$SERVER" ]] && SERVER="<server-ip>"
LOCAL_PORT="${LOCAL_PORT:-$PORT}"

# ---- service state ---------------------------------------------------------
state="unknown"
detail="systemctl is not available on this host"
if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files "${SERVICE}.service" >/dev/null 2>&1 \
     && systemctl cat "${SERVICE}.service" >/dev/null 2>&1; then
    state="$(systemctl is-active "${SERVICE}" 2>/dev/null || true)"
    [[ -z "$state" ]] && state="inactive"
    detail="$(systemctl is-enabled "${SERVICE}" 2>/dev/null || true)"
  else
    state="not-installed"
    detail="no ${SERVICE}.service - run ops/install_dashboard_secure.sh"
  fi
fi

# ---- token: presence and last 4 characters, never the value ----------------
token=""
if [[ -r "$ENV_FILE" ]]; then
  token="$(sed -n 's/^[[:space:]]*DASHBOARD_TOKEN=//p' "$ENV_FILE" | tail -n 1 | tr -d '\r' | sed 's/^"//; s/"$//')"
fi
if [[ -z "$token" && -n "${DASHBOARD_TOKEN:-}" ]]; then
  token="${DASHBOARD_TOKEN}"
fi
if [[ -n "$token" ]]; then
  token_state="present"
  token_tail="…${token: -4}"
else
  token_state="missing"
  token_tail=""
fi

# ---- is it actually answering? --------------------------------------------
http=""
if command -v curl >/dev/null 2>&1 && [[ -n "$token" ]]; then
  http="$(curl -s -o /dev/null -m 4 -w '%{http_code}' \
            -H "X-Dashboard-Token: ${token}" \
            "http://${HOST}:${PORT}/api/status" 2>/dev/null || true)"
fi
case "$http" in
  200) health="healthy" ;;
  401) health="rejecting-token" ;;
  000|"") health="not-answering" ;;
  *) health="http-${http}" ;;
esac

tunnel="ssh -N -L ${LOCAL_PORT}:${HOST}:${PORT} ${SSH_USER}@${SERVER}"
link="http://127.0.0.1:${LOCAL_PORT}/?token=<DASHBOARD_TOKEN>#pipeline"
link_plain="http://127.0.0.1:${LOCAL_PORT}/#pipeline"
token_cmd="sudo sed -n 's/^DASHBOARD_TOKEN=//p' ${ENV_FILE}"
pipeline_cmd="cd ${ROOT} && python3 ops/pipeline_check.py --hours 24"

# ---- the optional public link (only if the operator explicitly asks) -------
if [[ "$MODE" == "nginx" ]]; then
  cat <<EOF
# OPTIONAL public link. The service itself stays on 127.0.0.1 - this is a
# reverse proxy in front of it, with its own password AND the dashboard token.
# Only do this if you truly need a URL that works without an SSH tunnel; the
# page can change routing and restart the bot.
#
#   sudo apt-get install -y nginx apache2-utils
#   sudo htpasswd -c /etc/nginx/bestgaa.htpasswd operator
#
# /etc/nginx/sites-available/bestgaa-dashboard:
server {
    listen 443 ssl http2;
    server_name dashboard.example.com;              # your DNS name
    ssl_certificate     /etc/letsencrypt/live/dashboard.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dashboard.example.com/privkey.pem;
    client_max_body_size 2m;
    location / {
        auth_basic "BestGAA";
        auth_basic_user_file /etc/nginx/bestgaa.htpasswd;
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_read_timeout 60s;
    }
}
#
#   sudo ln -s /etc/nginx/sites-available/bestgaa-dashboard /etc/nginx/sites-enabled/
#   sudo certbot --nginx -d dashboard.example.com
#   sudo nginx -t && sudo systemctl reload nginx
#
# Then open:  https://dashboard.example.com/?token=<DASHBOARD_TOKEN>#pipeline
# Keep the token out of screenshots and out of chat; rotate it by editing
# ${ENV_FILE} and running: sudo systemctl restart ${SERVICE}
EOF
  exit 0
fi

if [[ "$MODE" == "paste" ]]; then
  # One line for the operator's laptop: tunnel in the background, then open.
  printf '%s\n' "$tunnel"
  exit 0
fi

# ---- human output ----------------------------------------------------------
if [[ "$MODE" == "json" ]]; then
  # `http` is a string on purpose: curl's "000" is not a valid JSON number.
  cat <<EOF
{"service":"${SERVICE}","state":"${state}","enabled":"${detail}","host":"${HOST}","port":${PORT},"health":"${health}","http":"${http:-000}","token":"${token_state}","tunnel":"${tunnel}","link":"${link}","link_without_token":"${link_plain}","token_command":"${token_cmd}","pipeline_command":"${pipeline_cmd}","env_file":"${ENV_FILE}"}
EOF
  [[ "$health" == "healthy" ]] && exit 0
  [[ "$state" == "active" ]] && exit 1
  exit 1
fi

printf '\n  BestGAA dashboard\n  ─────────────────────────────────────────────────────────────\n'
printf '  service : %s (%s%s)\n' "$SERVICE" "$state" "${detail:+, $detail}"
printf '  address : http://%s:%s  (localhost only, by design)\n' "$HOST" "$PORT"
printf '  token   : %s%s  [%s]\n' "$token_state" "${token_tail:+ $token_tail}" "$ENV_FILE"
printf '  health  : %s\n\n' "$health"

case "$health" in
  healthy) printf '  The service is up and this token works.\n\n' ;;
  rejecting-token) printf '  The service answers but refused the token: read it again from %s\n\n' "$ENV_FILE" ;;
  not-answering) printf '  Nothing is answering there. Start it:  sudo systemctl start %s\n\n' "$SERVICE" ;;
esac

cat <<EOF
  1) On YOUR laptop, open the tunnel (keep this window open):
     ${tunnel}

  2) Then open the link. One click, token included:
     ${link}

     (After the first load the token is remembered in that tab and removed from
      the address bar. Without it: ${link_plain} and paste the token.)

  3) Copy the token itself (server, your terminal only - never into chat):
     ${token_cmd}

  Proof that nothing is dropped, on the same server:
     ${pipeline_cmd}
EOF

if [[ "$state" == "active" ]]; then
  exit 0
fi
printf '\n  NOTE: the service is %s, so the tunnel will connect to nothing.\n' "$state"
exit 1
