#!/usr/bin/env bash
# set_earnkaro_key.sh — put a new EarnKaro/Affiliaters API token on the server,
# the right way, in one command.
#
#   ./set_earnkaro_key.sh 'eyJhbGciOiJIUzI1NiIs...'      # the token
#   EARNKARO_API_KEY='eyJ...' ./set_earnkaro_key.sh       # or from the env
#   ./set_earnkaro_key.sh --from-file ../bestgaa/.env     # or from a .env
#
# What it does, in order:
#   1. DECODES the token and prints which EarnKaro publisher gets paid (the
#      token's own `earnkaro` claim) - a token for another account is refused,
#      because every converted link would pay that account instead.
#   2. Backs up .env, then sets EARNKARO_API_KEY and pins
#      EARNKARO_PUBLISHER_ID to the token's publisher so the bot's
#      foreign-publisher guard checks the right account.
#   3. chmod 600, restarts bestgaa, and (unless --no-verify) runs
#      ops/earnkaro_check.py against the live endpoint.
#
# The token is NEVER printed by this script.
set -Eeuo pipefail

HOME_DIR=/home/ubuntu
BESTGAA_DIR="${BESTGAA_DIR:-$HOME_DIR/bestgaa-bot/bestgaa-bot}"
HERE="$(cd "$(dirname "$0")" && pwd)"
EXPECTED_PUBLISHER="${EARNKARO_PUBLISHER_ID:-5478322}"
STAMP="$(date +%Y%m%d_%H%M%S)"
RESTART=1
VERIFY=1
FROM_FILE=""
KEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) BESTGAA_DIR="$2"; shift 2 ;;
    --publisher) EXPECTED_PUBLISHER="$2"; shift 2 ;;
    --from-file) FROM_FILE="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --no-verify) VERIFY=0; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) KEY="$1"; shift ;;
  esac
done

if [[ -z "$KEY" && -n "${EARNKARO_API_KEY:-}" ]]; then KEY="$EARNKARO_API_KEY"; fi
if [[ -z "$KEY" && -n "$FROM_FILE" ]]; then
  KEY="$(grep -E '^EARNKARO_API_KEY=' "$FROM_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
fi
if [[ -z "$KEY" ]]; then
  echo "ERROR: no token given." >&2
  echo "Usage: $0 'eyJhbGciOiJIUzI1NiIs...'   (or EARNKARO_API_KEY=... $0, or --from-file <env>)" >&2
  exit 1
fi

ENV_FILE="$BESTGAA_DIR/.env"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE not found (use --dir to point at the bot directory)" >&2; exit 1; }

# --- 1. decode + verify the token belongs to OUR account --------------------
read -r CLAIMED_PUBLISHER ISSUED < <(python3 - "$KEY" <<'PY'
import base64, json, sys, datetime
token = sys.argv[1]
try:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
except Exception as exc:
    print(f"NOT_A_JWT {exc}")
    raise SystemExit(0)
issued = ""
try:
    issued = datetime.datetime.fromtimestamp(
        float(claims.get("iat")), datetime.timezone.utc).strftime("%Y-%m-%d")
except Exception:
    issued = "unknown"
print(f"{claims.get('earnkaro') or ''} {issued}")
PY
)

if [[ "$CLAIMED_PUBLISHER" == "NOT_A_JWT" || -z "$CLAIMED_PUBLISHER" ]]; then
  echo "ERROR: that does not look like the EarnKaro/Affiliaters API token." >&2
  echo "       Expected a JWT (eyJhbGciOi... . ... . ...) whose payload carries the" >&2
  echo "       'earnkaro' publisher id. Refusing to write it - a wrong key silently" >&2
  echo "       turns every conversion into an unmonetized link." >&2
  exit 1
fi
echo "token decoded : EarnKaro publisher $CLAIMED_PUBLISHER (issued ${ISSUED:-unknown})"
if [[ -n "$EXPECTED_PUBLISHER" && "$CLAIMED_PUBLISHER" != "$EXPECTED_PUBLISHER" ]]; then
  echo "ERROR: this token pays EarnKaro publisher $CLAIMED_PUBLISHER, not $EXPECTED_PUBLISHER." >&2
  echo "       Every converted link would earn on the WRONG account." >&2
  echo "       Re-run with --publisher $CLAIMED_PUBLISHER if that is genuinely the account you want." >&2
  exit 1
fi

# --- 2. write it into .env (backup first, key never echoed) ------------------
cp "$ENV_FILE" "$ENV_FILE.before-earnkaro-key.$STAMP"
python3 - "$ENV_FILE" "$KEY" "$CLAIMED_PUBLISHER" <<'PY'
import sys
from pathlib import Path
path, token, publisher = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
updates = {"EARNKARO_API_KEY": token, "EARNKARO_PUBLISHER_ID": publisher,
           "EARNKARO_API_URL": "https://ekaro-api.affiliaters.in/api/converter/public"}
lines = path.read_text(encoding="utf-8").splitlines()
out, seen = [], set()
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line else ""
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
chmod 600 "$ENV_FILE"
echo "written       : $ENV_FILE (backup: $(basename "$ENV_FILE").before-earnkaro-key.$STAMP)"

# --- 3. restart + verify -----------------------------------------------------
if [[ "$RESTART" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
  sudo systemctl restart bestgaa || true
  sleep 4
  sudo systemctl is-active bestgaa >/dev/null 2>&1 \
    && echo "service       : bestgaa restarted and active" \
    || echo "WARNING: bestgaa is not active - check: journalctl -u bestgaa -n 50" >&2
  # The startup line proves the bot read the new token.
  sudo journalctl -u bestgaa -n 200 --no-pager 2>/dev/null | grep -E "EarnKaro token|EARNKARO_API_KEY does not look" | tail -2 || true
fi

if [[ "$VERIFY" == "1" ]]; then
  echo
  echo "verifying the key against the live endpoint..."
  python3 "$HERE/earnkaro_check.py" --env-file "$ENV_FILE" || {
    echo
    echo "The key is written and the bot will use it, but the API did not convert" >&2
    echo "everything the checker probes - read the raw bodies above." >&2
    exit 1
  }
fi

echo
echo "DONE. Watch the first converted deals in the log:"
echo "  journalctl -u bestgaa -f | grep -E 'EK CONVERT|EK AUTH|EK REJECT|UNMONETIZED'"
