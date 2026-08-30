#!/usr/bin/env bash
# ============================================================================
# TELEGRAM BOT POSTING CHECK — run on the server:  bash telegram_check.sh
# Sources ARE getting posts but nothing lands in Telegram channels? This shows
# exactly WHERE posts die: render/provenance/eligibility vs. actual SEND to
# each target (admin removed? entity unresolved? flood?).
# ============================================================================
set -uo pipefail
DB="/home/ubuntu/bestgaa-bot/bestgaa-bot/bestgaa.sqlite3"
LOG="/home/ubuntu/bestgaa-bot/bestgaa-bot/logs/bot.log"

echo "==== A) QUEUE STATUS (recent 15 jobs) ===="
sqlite3 -header -column "$DB" "
SELECT id, source, status, attempts,
       datetime(created_at,'unixepoch','+5 hours','+30 minutes') AS created_ist,
       substr(COALESCE(last_error,''),1,60) AS last_error
FROM queue ORDER BY id DESC LIMIT 15;" 2>/dev/null \
  || echo "  (could not read queue — is sqlite3 installed / DB at $DB ?)"

echo ""
echo "==== B) JOBS GROUPED BY last_error (WHY they are not posting) ===="
sqlite3 -header -column "$DB" "
SELECT COUNT(*) AS n, status, substr(COALESCE(last_error,'(none)'),1,70) AS reason
FROM queue GROUP BY reason, status ORDER BY n DESC LIMIT 15;" 2>/dev/null

echo ""
echo "==== C) PER-TARGET DELIVERY RESULTS (is the SEND itself failing?) ===="
echo "   target                       | status  | n | last error"
echo "   -----------------------------+---------+---+--------------------------------"
sqlite3 -column "$DB" "
SELECT substr(target,1,28) AS target, status, COUNT(*) AS n,
       substr(COALESCE(MAX(last_error),''),1,40) AS last_error
FROM deliveries GROUP BY target, status ORDER BY n DESC;" 2>/dev/null

echo ""
echo "==== D) HOW TO READ IT ===="
echo "  * last_error 'no eligible targets' / 'under99 source invalid price'"
echo "      -> posts filtered by the price/discount routing (raise WA_BEST or check routing)."
echo "  * last_error 'final provenance recheck failed' / 'foreign URL survived'"
echo "      -> provenance gate dropping (deploy the latest code with the our-tag fix)."
echo "  * deliveries status != 'sent' with errors like 'ChatAdminRequired',"
echo "    'ChannelPrivate', 'USERNAME_NOT_OCCUPIED', 'Could not find the input entity',"
echo "    'FloodWait' -> the bot can't POST to the channel (admin removed / username"
echo "    changed / flood). THIS is the usual cause when sources ingest but channels stay empty."
echo "  * All 'sent' but nothing visible -> checking the wrong channels / account."

echo ""
echo "==== E) LIVE SKIP/FAIL/DELIVERY LINES (last 30) ===="
sudo journalctl -u bestgaa -n 600 --no-pager 2>/dev/null \
  | grep -E "SKIP \||JOB FAIL|JOB [0-9]+ \||sent=|BROKEN LINK|target unresolved|delivery|FloodWait|Admin|Private" \
  | tail -30
[[ -f "$LOG" ]] && { echo "  --- from $LOG ---"; grep -E "SKIP \||JOB FAIL|sent=|target unresolved|FloodWait" "$LOG" | tail -20; }
