#!/usr/bin/env bash
# One command to ship this repo to the live bot + bridge and PROVE it took effect:
# deploy -> restart -> version/hash check -> contract self-test -> immediacy and
# quality report from the live logs. Run it after every fix; a green run is the
# evidence that the guarantees are actually live, not just committed.
#
#   ./ops/deploy_and_verify.sh              # pull, repack, deploy, restart, verify
#   ./ops/deploy_and_verify.sh --verify-only # deploy nothing, just prove what runs
#   ./ops/deploy_and_verify.sh --no-pull      # deploy the working tree as-is
#   ./ops/deploy_and_verify.sh --with-tests    # run the repo suites first, then ship
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BESTGAA_DIR="${BESTGAA_DIR:-$HOME/bestgaa-bot/bestgaa-bot}"
BRIDGE_DIR="${BRIDGE_DIR:-$HOME/tg-wa-bridge}"
BOT_LOG="${BOT_LOG:-$BESTGAA_DIR/logs/bot.log}"
SERVICE_BOT="${SERVICE_BOT:-bestgaa}"
SERVICE_BRIDGE="${SERVICE_BRIDGE:-tg-wa-bridge}"
DO_PULL=1; DO_DEPLOY=1; DO_RESTART=1; DO_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --verify-only) DO_PULL=0; DO_DEPLOY=0; DO_RESTART=0 ;;
    --no-pull)     DO_PULL=0 ;;
    --no-restart)  DO_RESTART=0 ;;
    --with-tests)  DO_TESTS=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
SUDO=""
[[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
FAILED=()
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mCHECK\033[0m  %s\n' "$*"; FAILED+=("$*"); }
info() { printf '  ----  %s\n' "$*"; }

WANT_VERSION="$(grep -m1 -oE 'BestGAA Production Bot v[0-9.]+' "$REPO/bestgaa/main_bot_new.py" | sed 's/^BestGAA Production Bot //')"
echo "==== 0. WHAT SHOULD BE LIVE ===="
info "repo commit : $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')  ($WANT_VERSION)"
info "bot dir     : $BESTGAA_DIR"
info "bridge dir  : $BRIDGE_DIR"

if [[ "$DO_TESTS" == "1" ]]; then
  echo "==== 0.5 THE REPO PROVES ITSELF (suites must be green before shipping) ===="
  # The suites IMPORT the bot, and importing it creates logs/ and a default DB
  # next to the source. Run against the checkout (2026-10-04: the deploy did
  # exactly that) and the checkout fills with test logs/DBs - which is what
  # conversion_report then read back as "live". Isolate both.
  SUITE_TMP="$(mktemp -d /tmp/deploy-suites-XXXXXX)"
  for suite in test_pipeline_fixes test_render_job test_best_copy test_duplicate_sim \
               test_line_fidelity test_rescan test_earnkaro_conversion test_hypd_links; do
    if [[ ! -f "$REPO/$suite.py" ]]; then
      warn "$suite.py is missing from $REPO"
      continue
    fi
    if (cd "$REPO" && BOT_DB_PATH="$SUITE_TMP/$suite.sqlite3" \
        BOT_LOG_DIR="$SUITE_TMP/logs" BOT_MEDIA_DIR="$SUITE_TMP/media" \
        timeout 900 python3 "$suite.py" >"/tmp/$suite.deploy.log" 2>&1); then
      ok "$suite"
    else
      warn "$suite failed - last lines: $(tail -3 "/tmp/$suite.deploy.log" | tr '\n' ' ')"
    fi
  done
  if [[ -f "$REPO/ops/sync_identity.py" ]]; then
    # The auditor answers "did we post this product twice?" with the bot's own
    # identity rule, generated from bestgaa/main_bot_new.py. If somebody edited one
    # copy and forgot the other, every SAME-PRODUCT finding is fiction - so the
    # gate refuses to ship on a disagreement.
    if (cd "$REPO" && BOT_DB_PATH="$SUITE_TMP/sync.sqlite3" \
        BOT_LOG_DIR="$SUITE_TMP/logs" BOT_MEDIA_DIR="$SUITE_TMP/media" \
        python3 ops/sync_identity.py --check >/tmp/sync.identity.log 2>&1); then
      ok "auditor identity in sync"
    else
      warn "auditor identity is OUT OF SYNC - run: python3 ops/sync_identity.py"
    fi
  fi
  if [[ -f "$REPO/tg-wa-bridge/bridge.js" ]]; then
    # A checkout without a filled .env still has to prove its formatting/dedup
    # contract, so the self-test runs against throwaway credentials when no .env
    # exists (it never connects and never touches a live state file).
    BRIDGE_TEST_ENV=()
    [[ -f "$REPO/tg-wa-bridge/.env" ]] || BRIDGE_TEST_ENV=(
      "TELEGRAM_BOT_TOKEN=1:dummy" "WA_PHONE=910000000000" "WA_CHANNEL=@selftest")
    if (cd "$REPO/tg-wa-bridge" && env "${BRIDGE_TEST_ENV[@]}" timeout 900 node bridge.js --self-test >/tmp/bridge.repo.log 2>&1); then
      ok "bridge self-test (repo copy)"
    else
      warn "bridge self-test failed - last lines: $(tail -3 /tmp/bridge.repo.log | tr '\n' ' ')"
    fi
  fi
fi

# BUG FIX (2026-10-04): this used to read `[[ "$DO_PULL$DO_DEPLOY" == "11" ]]`,
# so `--no-pull` (which is exactly how ops/deploy_fresh.sh calls this script)
# silently skipped the ENTIRE deploy: no repack, no hotfix, no restart - and the
# only symptom was the "OLDER build" hash line further down. The deploy runs
# whenever DO_DEPLOY=1; the pull inside is already conditional.
if [[ "$DO_DEPLOY" == "1" ]]; then
  echo "==== 1. DEPLOY ===="
  if [[ "$DO_PULL" == "1" ]]; then
    if [[ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]]; then
      warn "the repo tree is dirty - deploying what is on disk, not what is committed"
    fi
    git -C "$REPO" pull --ff-only >/dev/null 2>&1 || info "git pull skipped (offline or diverged); deploying local tree"
  fi
  ( cd "$REPO/ops" && ./repack_bundles.sh >/tmp/repack.log 2>&1 ) \
    && ok "bundles repacked (/tmp/repack.log)" || warn "repack_bundles.sh failed - see /tmp/repack.log"
  ( cd "$REPO/ops" && ./apply_dual_hotfix.sh >/tmp/hotfix.log 2>&1 ) \
    && ok "hotfix applied to both services" || warn "apply_dual_hotfix.sh failed - see /tmp/hotfix.log"
  if [[ "$DO_RESTART" == "1" ]]; then
    if $SUDO systemctl restart "$SERVICE_BOT" "$SERVICE_BRIDGE" 2>/dev/null; then
      info "services restarted, giving the bot 12s to boot"
      sleep 12
    else
      warn "systemctl restart failed (unit names: $SERVICE_BOT $SERVICE_BRIDGE)"
    fi
  fi
fi

echo ""
echo "==== 2. IS IT THE CODE WE COMMITTED? ===="
for PAIR in "$REPO/bestgaa/main_bot_new.py|$BESTGAA_DIR/main_bot.py" \
            "$REPO/tg-wa-bridge/bridge.js|$BRIDGE_DIR/bridge.js"; do
  SRC="${PAIR%%|*}"; DST="${PAIR##*|}"
  if [[ ! -f "$DST" ]]; then warn "missing on server: $DST"; continue; fi
  A="$(sha256sum "$SRC" | cut -c1-16)"; B="$(sha256sum "$DST" | cut -c1-16)"
  if [[ "$A" == "$B" ]]; then ok "$(basename "$DST") matches the repo ($A…)"; else warn "$DST is an OLDER build ($B, repo $A) - the fixes are NOT live"; fi
done

echo ""
echo "==== 3. DID THE BOT COME UP AS $WANT_VERSION? ===="
  BOOT_LINE="BestGAA Production Bot $WANT_VERSION starting"
  if $SUDO journalctl -u "$SERVICE_BOT" -n 400 --no-pager 2>/dev/null | grep -qF "$BOOT_LINE" \
     || { [[ -s "$BOT_LOG" ]] && grep -qF "$BOOT_LINE" "$BOT_LOG"; }; then
    ok "$SERVICE_BOT is running $WANT_VERSION"
  else
    warn "$SERVICE_BOT never logged '$BOOT_LINE' - old code still running, or it crashed: $SUDO journalctl -u $SERVICE_BOT -n 60 --no-pager"
  fi
if [[ -f "$BRIDGE_DIR/bridge.js" ]]; then
  if command -v node >/dev/null 2>&1; then
    SELFTEST_OUT="$(cd "$BRIDGE_DIR" && TELEGRAM_BOT_TOKEN=x WA_PHONE=91 WA_CHANNEL=c node bridge.js --self-test 2>&1 || true)"
    if printf '%s' "$SELFTEST_OUT" | grep -q "bridge self-test PASS"; then
      ok "deployed bridge passes its own contract self-test"
    elif printf '%s' "$SELFTEST_OUT" | grep -q "ERR_MODULE_NOT_FOUND"; then
      warn "bridge deps missing on this host - cd $BRIDGE_DIR && npm install, then re-run"
    else
      warn "deployed bridge.js FAILS the self-test - do not trust its output until it is redeployed"
    fi
  else
    info "node not on PATH here - skipping the bridge self-test"
  fi
fi

echo ""
echo "==== 4. IMMEDIACY: source arrival -> our channel (last 60 min) ===="
LOGFILE="$(mktemp /tmp/deploy-verify-log.XXXXXX)"
if [[ -s "$BOT_LOG" ]]; then
  tail -n 40000 "$BOT_LOG" > "$LOGFILE" 2>/dev/null || true
elif command -v journalctl >/dev/null 2>&1; then
  $SUDO journalctl -u "$SERVICE_BOT" --since "-60 min" --no-pager > "$LOGFILE" 2>/dev/null || true
fi
PYFILE="$(mktemp /tmp/deploy-verify-latency.XXXXXX.py)"
cat > "$PYFILE" <<'PY'
"""Pair 'QUEUED | queue=N' with 'JOB N | ... sent=k' and report the gap."""
import datetime
import re
import statistics
import sys

pat_q = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*QUEUED \| queue=(?P<id>\d+)")
pat_j = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*JOB (?P<id>\d+) \| source=\S+ \| sent=(?P<n>\d+)")


def stamp(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


queued, samples = {}, []
try:
    lines = open(sys.argv[1], encoding="utf-8", errors="ignore").read().splitlines()
except OSError:
    lines = []
for line in lines:
    m = pat_q.match(line)
    if m:
        queued[m.group("id")] = stamp(m.group("ts"))
        continue
    m = pat_j.match(line)
    if m and m.group("id") in queued and int(m.group("n")) > 0:
        delta = (stamp(m.group("ts")) - queued[m.group("id")]).total_seconds()
        if 0 <= delta <= 3600:
            samples.append(delta)
if not samples:
    print("  ----  no QUEUED->sent pairs in the window (quiet sources, or the log moved)")
    sys.exit(0)
samples.sort()
print(f"  ----  {len(samples)} post(s): median {statistics.median(samples):.0f}s, "
      f"p95 {samples[max(0, min(len(samples) - 1, int(len(samples) * 0.95 + 0.999) - 1))]:.0f}s, "
      f"max {samples[-1]:.0f}s")
median = statistics.median(samples)
if median > 20:
    print("  WARN  posts land slowly - look for LINK PROBE / conversion stalls in the log")
elif median > 5:
    print("  NOTE  median above 5s: usually one slow merchant probe, not a queue stall")
else:
    print("  OK    posting is immediate")
PY
python3 "$PYFILE" "$LOGFILE"
rm -f "$PYFILE" "$LOGFILE"

echo ""
echo "==== 5. POST QUALITY ON LIVE DATA (nothing invented, our links, no dupes) ===="
AUDIT="$HERE/quality_audit.py"; [[ -f "$AUDIT" ]] || AUDIT="$BESTGAA_DIR/quality_audit.py"
DB="${BOT_DB:-$BESTGAA_DIR/bestgaa.sqlite3}"
[[ -f "$DB" ]] || DB="$BESTGAA_DIR/state/bot_state.sqlite3"
if [[ -f "$AUDIT" && -f "$DB" ]]; then
  if python3 "$AUDIT" --db "$DB" --limit 150 --strict; then
    ok "the last posts broke no guarantee"
  else
    warn "the audit found posts that broke a guarantee - fix before posting more"
  fi
else
  info "skipped (audit=$AUDIT db=$DB) - run it once the bundle with quality_audit.py is deployed"
fi

echo ""
if ((${#FAILED[@]})); then
  echo "DEPLOY VERIFY: ${#FAILED[@]} problem(s):"
  for f in "${FAILED[@]}"; do echo "  - $f"; done
  echo "Nothing here is fixed by restarting again - the hash/version line above names the real gap."
  exit 1
fi
echo "DEPLOY VERIFY: all good - $WANT_VERSION is live, posting immediately, and the last posts are clean."
