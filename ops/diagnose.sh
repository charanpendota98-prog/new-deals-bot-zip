#!/usr/bin/env bash
# ============================================================================
# ONE-COMMAND DIAGNOSIS: run on the server ->  bash diagnose.sh
# Prints exactly WHY posting issues happen: old code? service down? stuck
# queue? FloodWait ban? provenance skips? missing env? groups not configured?
# night window active? intake stopped?
#
# Telegram bot (bestgaa) and WhatsApp bridge (tg-wa-bridge) are INDEPENDENT:
# the bot posts to Telegram on its own; the bridge only reads the bot's posts
# (and raw sources) to mirror them to WhatsApp. WhatsApp can never block the
# Telegram bot. If Telegram is silent, the cause is in the bestgaa service,
# queue or Telegram account — sections 1,2,6,8,9 cover it.
# ============================================================================
set -uo pipefail
HOME_DIR=/home/ubuntu
BESTGAA_DIR="$HOME_DIR/bestgaa-bot/bestgaa-bot"
BRIDGE_DIR="$HOME_DIR/tg-wa-bridge"
BOT_LOG="$BESTGAA_DIR/logs/bot.log"
G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; N='\033[0m'
ok()   { echo -e "  ${G}OK${N}  $*"; }
bad()  { echo -e "  ${R}PROBLEM${N}  $*"; }
warn() { echo -e "  ${Y}CHECK${N}  $*"; }

echo "==== 1. SERVICES RUNNING? ===="
for svc in bestgaa tg-wa-bridge; do
  if systemctl is-active --quiet "$svc"; then ok "$svc is running"
  else bad "$svc is NOT running -> sudo systemctl restart $svc && sudo journalctl -u $svc -n 40 --no-pager"; fi
done
echo -n "  bestgaa restart count: "; systemctl show -p NRestarts --value bestgaa 2>/dev/null || echo "?"
echo -n "  bestgaa since: "; systemctl show -p ActiveEnterTimestamp --value bestgaa 2>/dev/null || echo "?"

echo ""
echo "==== 2. IS THE NEW CODE ACTUALLY DEPLOYED? (the #1 reason issues persist) ===="
if grep -q "AMAZON_EARNKARO_RATIO" "$BESTGAA_DIR/main_bot.py" 2>/dev/null \
   || grep -q "AMAZON_EARNKARO_RATIO" "$BESTGAA_DIR/main_bot_new.py" 2>/dev/null; then
  ok "Telegram bot has the NEW code"
else
  bad "Telegram bot is running OLD code -> cd ops && ./repack_bundles.sh && ./apply_dual_hotfix.sh"
fi
# Provenance-leak fix markers (our-tag amazon links skip the link_cache gate).
if grep -q "is_our_amazon_tag_link" "$BESTGAA_DIR/main_bot.py" 2>/dev/null \
   || grep -q "is_our_amazon_tag_link" "$BESTGAA_DIR/main_bot_new.py" 2>/dev/null; then
  ok "Telegram bot has the our-tag provenance fix (false provenance drops fixed)"
else
  warn "Telegram bot is MISSING the our-tag provenance fix -> deploy; old code may drop direct-Amazon posts with 'final provenance recheck failed'"
fi
if grep -q "isOurAmazonTagLink" "$BRIDGE_DIR/bridge.js" 2>/dev/null; then
  ok "WhatsApp bridge has the our-tag provenance fix + full invite links"
else
  bad "WhatsApp bridge is running OLD code -> cd ops && ./repack_bundles.sh && ./apply_dual_hotfix.sh"
fi
# v17 markers - these are the fixes for "posts missing" and "random junk next
# to the price". If they are absent the server is still on an older build, and
# no amount of waiting will change what the channels print: redeploy.
for marker in "sanitize_outbound_text:final outbound junk guard (no glued tokens, no [url](url) debris)" \
              "GENERIC_HEADLINE_RE:campaign fingerprint no longer swallows a source sharing one banner line" \
              "keep_passthrough:unmonetizable store links post clean instead of being dropped" \
              "revive_edited_job:an edited source post that never went out is re-queued" \
              "remember_passthrough:pass-through links carry provenance"; do
  name="${marker%%:*}"; desc="${marker#*:}"
  if grep -q "$name" "$BESTGAA_DIR/main_bot.py" 2>/dev/null \
     || grep -q "$name" "$BESTGAA_DIR/main_bot_new.py" 2>/dev/null; then
    ok "v17 bot: $desc"
  else
    bad "v17 bot fix MISSING: $name -> redeploy (the bug this fixes is still live)"
  fi
done
if grep -q "function sanitizeOutbound" "$BRIDGE_DIR/bridge.js" 2>/dev/null; then
  ok "v17 bridge: outbound junk guard present"
else
  bad "v17 bridge guard MISSING -> redeploy the bridge bundle"
fi
if grep -qE "BestGAA Production Bot v1[7-9]" "$BOT_LOG" 2>/dev/null; then
  ok "bot log shows a v17+ startup banner (the new build actually restarted)"
else
  warn "no v17 startup banner in $BOT_LOG -> service was not restarted after the deploy"
fi

echo ""
echo "==== 2b. SOURCE COVERAGE: did every source post reach a channel? ===="
AUDIT="$BESTGAA_DIR/coverage_audit.py"
[[ -f "$AUDIT" ]] || AUDIT="$BESTGAA_DIR/../new-deals-bot-zip/ops/coverage_audit.py"
[[ -f "$AUDIT" ]] || AUDIT="$(dirname "$(readlink -f "$0")")/coverage_audit.py"
if [[ -f "$AUDIT" ]]; then
  python3 "$AUDIT" --hours 12 2>&1 | sed 's/^/  /'
  echo "    (repair the recoverable ones with: python3 $AUDIT --hours 12 --heal)"
else
  warn "coverage_audit.py not found - run it from the repo: python3 ops/coverage_audit.py --hours 12"
fi

echo ""
echo "==== 3. WHATSAPP GROUPS CONFIGURED? ===="
GROUPS_LINE=$(grep -E '^WA_GROUPS=' "$BRIDGE_DIR/.env" 2>/dev/null | cut -d= -f2-)
if [[ -n "${GROUPS_LINE:-}" ]]; then
  ok "WA_GROUPS is set: $(echo "$GROUPS_LINE" | tr ',' '\n' | wc -l) group(s) (JIDs/phones/codes/full chat.whatsapp.com links)"
else
  bad "WA_GROUPS is EMPTY -> groups posting is OFF. Add to $BRIDGE_DIR/.env: WA_GROUPS=<number/invite-link/jid,comma separated> then: sudo systemctl restart tg-wa-bridge"
fi

echo ""
echo "==== 4. KEY .ENV VALUES PRESENT? ===="
for pair in "BITLY_TOKENS:$BESTGAA_DIR/.env" "AMAZON_TAG:$BESTGAA_DIR/.env" "EARNKARO_API_KEY:$BESTGAA_DIR/.env" "TELEGRAM_API_ID:$BESTGAA_DIR/.env" "TELEGRAM_API_HASH:$BESTGAA_DIR/.env" "WA_BITLY_TOKENS:$BRIDGE_DIR/.env" "QUIET_START:$BRIDGE_DIR/.env" "POST_QUIET_START:$BESTGAA_DIR/.env" "HYPD_STORE_ID:$BESTGAA_DIR/.env" "HYPD_ALWAYS_BITLY:$BESTGAA_DIR/.env" "HYPD_STORES:$BRIDGE_DIR/.env"; do
  key="${pair%%:*}"; file="${pair#*:}"
  if grep -qE "^${key}=.+" "$file" 2>/dev/null; then ok "$key set in $(basename "$(dirname "$file")")/.env"
  else warn "$key missing/empty in $file"; fi
done

echo ""
echo "==== 4b. EARNKARO TOKEN: WHICH ACCOUNT EARNS? ===="
EK_TOKEN_LINE="$(grep -E '^EARNKARO_API_KEY=' "$BESTGAA_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')"
EK_PUB_LINE="$(grep -E '^EARNKARO_PUBLISHER_ID=' "$BESTGAA_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')"
if [[ -z "$EK_TOKEN_LINE" ]]; then
  bad "EARNKARO_API_KEY is empty -> every conversion answers 401 and the deals go out with UNTAGGED merchant links (zero commission). Fix: ./set_earnkaro_key.sh '<token>'"
else
  read -r EK_PUB CLAIMED < <(python3 - "$EK_TOKEN_LINE" <<'PY' 2>/dev/null || true
import base64, json, sys
try:
    payload = sys.argv[1].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    print(f"{claims.get('earnkaro') or 'NONE'} {int(claims.get('iat') or 0)}")
except Exception:
    print("NOT_A_JWT 0")
PY
)
  if [[ "${EK_PUB:-}" == "NOT_A_JWT" ]]; then
    bad "EARNKARO_API_KEY is not a JWT carrying a publisher id -> the API will refuse it (401). Fix: ./set_earnkaro_key.sh '<token>'"
  else
    ok "token is a JWT; EarnKaro publisher ${EK_PUB} earns every converted link (issued $(date -u -d "@${CLAIMED:-0}" +%Y-%m-%d 2>/dev/null || echo unknown))"
    if [[ -n "$EK_PUB_LINE" && "$EK_PUB_LINE" != "$EK_PUB" ]]; then
      warn "EARNKARO_PUBLISHER_ID=$EK_PUB_LINE in .env disagrees with the token ($EK_PUB) - the token wins; the foreign-publisher guard should be pinned from it"
    fi
  fi
fi
echo "  AMAZON_VIA_EARNKARO=$(grep -E '^AMAZON_VIA_EARNKARO=' "$BESTGAA_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' || echo '<unset: EarnKaro default>')  (true = Amazon converts via EarnKaro with the native tag as fallback)"
echo "  live conversion proof: python3 ops/earnkaro_check.py --env-file $BESTGAA_DIR/.env"

echo ""
echo "==== 5. NIGHT QUIET WINDOW ACTIVE RIGHT NOW? ===="
HOUR=$(TZ=Asia/Kolkata date +%H)
if (( HOUR >= 2 && HOUR < 6 )); then
  warn "It is $(TZ=Asia/Kolkata date +%H:%M) IST -> inside 02:00-06:00 quiet window. NO posting now is CORRECT, not a bug (deals queue and flush after 06:00)."
else
  ok "Outside the quiet window ($(TZ=Asia/Kolkata date +%H:%M) IST) - posting should be active"
fi

echo ""
echo "==== 6. TELEGRAM BOT QUEUE HEALTH (real columns: status / next_at) ===="
DB="$BESTGAA_DIR/bestgaa.sqlite3"
if [[ -f "$DB" ]]; then
  echo "  queue rows by status:"
  sqlite3 -line "$DB" "SELECT status, COUNT(*) AS n FROM queue GROUP BY status;" 2>/dev/null | grep -E "status|n" | sed 's/^/    /' || warn "could not read queue table"
  # A job is 'processing' while a worker holds it; claim timestamp = next_at.
  STUCK=$(sqlite3 "$DB" "SELECT COUNT(*) FROM queue WHERE status='processing' AND next_at < strftime('%s','now')-1800;" 2>/dev/null || echo "?")
  PENDING=$(sqlite3 "$DB" "SELECT COUNT(*) FROM queue WHERE status='pending' AND next_at <= strftime('%s','now');" 2>/dev/null || echo "?")
  [[ "$STUCK" == "0" ]] && ok "no stuck processing jobs" || warn "$STUCK job(s) stuck 'processing' >30min (reclaimed on restart: sudo systemctl restart bestgaa)"
  echo "  pending jobs ready to post NOW: $PENDING"
  echo "  most recent queue activity (last 5):"
  sqlite3 -header -column "$DB" "SELECT id, source, status, attempts, datetime(created_at,'unixepoch','+5 hours','+30 minutes') AS created_ist, substr(COALESCE(last_error,''),1,48) AS last_error FROM queue ORDER BY id DESC LIMIT 5;" 2>/dev/null | sed 's/^/    /' || true
else
  warn "bot DB not found at $DB (bot may never have started / wrong path)"
fi

echo ""
echo "==== 7. WHATSAPP BRIDGE QUEUE + CONNECTION ===="
if [[ -f "$BRIDGE_DIR/bridge-state.json" ]]; then
  python3 - "$BRIDGE_DIR/bridge-state.json" <<'PY' 2>/dev/null || echo "  (state parse failed)"
import json,sys,time
s=json.load(open(sys.argv[1]))
jobs=s.get('jobs',[])
print(f"  queued jobs waiting: {len(jobs)}")
sent=s.get('sentTimes',[])
recent=[t for t in sent if t > (time.time()-3600)*1000]
print(f"  WhatsApp posts in the last hour: {len(recent)}")
PY
else
  warn "bridge-state.json not found (bridge may not have started)"
fi
# Both WhatsApp channels fed? The second channel is either an Under-₹99 shelf
# (tiered) or a full mirror (WA_CHANNEL_ALL_POSTS=true). A channel that fails to
# resolve is retried every 10 minutes - this prints which mode is live.
SEC_LINE=$(grep -E '^WA_CHANNEL_UNDER99=' "$BRIDGE_DIR/.env" 2>/dev/null | cut -d= -f2-)
if [[ -n "${SEC_LINE:-}" ]]; then
  if grep -qE '^WA_CHANNEL_ALL_POSTS=true' "$BRIDGE_DIR/.env" 2>/dev/null; then
    ok "2nd WhatsApp channel configured AND mirroring every post (WA_CHANNEL_ALL_POSTS=true)"
  else
    ok "2nd WhatsApp channel configured as the Under-₹99 shelf (set WA_CHANNEL_ALL_POSTS=true to mirror everything)"
  fi
  sudo journalctl -u tg-wa-bridge -n 400 --no-pager 2>/dev/null | grep -q "could not be resolved" \
    && warn "a WhatsApp channel failed to resolve at least once (it retries every 10 min; check the invite link/JID)" \
    || true
else
  warn "no 2nd WhatsApp channel (WA_CHANNEL_UNDER99 empty) - only ONE channel is being posted to"
fi

sudo journalctl -u tg-wa-bridge -n 200 --no-pager 2>/dev/null | grep -q "WhatsApp connected" \
  && ok "bridge connected at least once (see: sudo journalctl -u tg-wa-bridge -n 40 --no-pager)" \
  || warn "no 'WhatsApp connected' in recent logs -> run with sudo: sudo journalctl -u tg-wa-bridge -n 60 --no-pager"

echo ""
echo "==== 8. TELEGRAM BOT: LOGIN + INTAKE + FLOOD/BAN (why Telegram is silent) ===="
# 8a. Did the bot log in and map sources this boot?
if sudo journalctl -u bestgaa -n 400 --no-pager 2>/dev/null | grep -q "LIVE | sources="; then
  sudo journalctl -u bestgaa -n 400 --no-pager 2>/dev/null | grep "LIVE | sources=" | tail -1 | sed 's/^/  /'
  ok "bot logged in and mapped sources"
else
  bad "no 'LIVE | sources=' marker -> bot never finished login/startup. Check for API_ID/API_HASH/session errors:"
  sudo journalctl -u bestgaa -n 60 --no-pager 2>/dev/null | grep -iE "error|traceback|exception|auth|login|api|missing" | tail -8 | sed 's/^/    /' || true
fi
# 8b. FloodWait / banned / not-admin = a hard Telegram-side block on posting.
FLOOD=$(sudo journalctl -u bestgaa -n 800 --no-pager 2>/dev/null | grep -icE "floodwait|flood wait|too many requests|chatadminrequired|channelprivate|user.*banned|peer.*invalid|could not find the input" || echo 0)
if [[ "$FLOOD" -gt 0 ]]; then
  bad "$FLOOD FloodWait/permission/banned hits in recent bot logs -> Telegram is rate-limiting or the account is not admin in a target:"
  sudo journalctl -u bestgaa -n 800 --no-pager 2>/dev/null | grep -iE "floodwait|flood wait|chatadminrequired|channelprivate|banned|could not find the input" | tail -6 | sed 's/^/    /'
  echo "    -> Wait out the flood (seconds shown in the log), ensure the bot/admin is still admin in every target channel."
else
  ok "no FloodWait/ban/permission errors in recent bot logs"
fi
# 8c. Is intake still seeing source posts? (QUEUED lines) -> event stream health.
QUEUED_LAST=$(sudo journalctl -u bestgaa -n 400 --no-pager 2>/dev/null | grep -c "QUEUED |" || echo 0)
if [[ "$QUEUED_LAST" -gt 0 ]]; then
  ok "bot is ingesting source posts ($QUEUED_LAST QUEUED in recent logs) -> problem is downstream (render/provenance/send), see section 9"
else
  warn "no QUEUED lines recently -> the bot is not receiving source posts (not admin in source channels, or session/event stream dead). Restart fixes: sudo systemctl restart bestgaa"
fi

echo ""
echo "==== 9. LAST REASONS A DEAL DID NOT POST (both sides) ===="
echo "  -- Telegram bot (SKIP / DEDUP / FAIL / provenance), last 12 --"
sudo journalctl -u bestgaa -n 800 --no-pager 2>/dev/null | grep -E "SKIP \||DEDUP \||JOB FAIL|JOB [0-9]+ \|" | tail -12 | sed 's/^/    /' || echo "    (none in recent journal; checking log file)"
if [[ -f "$BOT_LOG" ]]; then
  grep -E "SKIP \||DEDUP \||JOB FAIL|provenance|FloodWait" "$BOT_LOG" 2>/dev/null | tail -12 | sed 's/^/    [log] /' || true
  echo "  -- bot log last lines --"
  tail -n 8 "$BOT_LOG" 2>/dev/null | sed 's/^/    /'
fi
echo "  -- WhatsApp bridge (skip/err), last 8 --"
sudo journalctl -u tg-wa-bridge -n 800 --no-pager 2>/dev/null | grep -oE '"(reason|err|msg)":"[^"]{0,110}"' | tail -8 | sed 's/^/    /' || true

echo ""
echo "==== 10. POST QUALITY AUDIT (read-only proof of the four guarantees) ===="
# Nothing invented, our links only, no product twice, nothing lost - the auditor
# checks exactly that on the live queue and prints the offending queue ids.
AUDIT="$(dirname "$0")/quality_audit.py"
# the deploy bundle drops it next to main_bot.py, so look there too
[[ -f "$AUDIT" ]] || AUDIT="$BESTGAA_DIR/quality_audit.py"
if [[ -f "$AUDIT" && -f "$DB" ]]; then
  python3 "$AUDIT" --db "$DB" --limit 120 2>&1 | sed 's/^/    /'
  python3 "$AUDIT" --db "$DB" --limit 120 --strict >/dev/null 2>&1 \
    && ok "recent posts are clean" || warn "quality audit found posts that broke a guarantee - the list above has the queue ids"
elif [[ ! -f "$DB" ]]; then
  echo "    no queue database at $DB yet - nothing to audit"
else
  echo "    ops/quality_audit.py not found (checked $(dirname "$0") and $BESTGAA_DIR) - copy it over to get this check"
fi

echo ""
echo "==== QUICK FIXES ===="
echo "  * Telegram totally silent?  sudo systemctl restart bestgaa && sleep 12 && sudo journalctl -u bestgaa -n 30 --no-pager"
echo "  * Deploy latest fixes:       cd $(dirname "$0") && ./repack_bundles.sh && ./apply_dual_hotfix.sh"
echo "  * Section 2 shows OLD code = THE reason for almost every issue."
echo "  * Telegram and WhatsApp post independently — WhatsApp NEVER blocks Telegram."
