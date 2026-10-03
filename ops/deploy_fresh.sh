#!/usr/bin/env bash
# deploy_fresh.sh — ONE command: ship everything, then PROVE it, then show what
# still needs your hands. "merge chedi deploy cheyyama malli okasari fresh gaa."
#
#   ./ops/deploy_fresh.sh                 # deploy + verify + teach + report
#   ./ops/deploy_fresh.sh --dry-run       # print the plan, change nothing
#   ./ops/deploy_fresh.sh --no-tests      # skip the pre-ship suites (faster)
#   ./ops/deploy_fresh.sh --no-learn      # do not learn the configured Meesho HYPD links
#   ./ops/deploy_fresh.sh --verify-only   # deploy nothing; just prove + report
#
# What it chains (each step is idempotent - re-running is always safe):
#   1. ops/deploy_and_verify.sh --with-tests   tests -> repack -> deploy -> restart
#                                              -> hash/version proof
#   2. ops/hypd_links.py  <candidate HYPD shares>  store only verified Meesho mappings
#   3. ops/earnkaro_check.py                   live: EarnKaro key pays US +
#                                              our hypd links come back to our store
#   4. ops/conversion_report.py                per-route verdict from the live DB+log
#
# Everything it prints is either OK or CHECK. A CHECK line always names the fix.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

DO_TESTS=1
DO_LEARN=1
DO_DEPLOY=1
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)     DRY_RUN=1 ;;
    --no-tests)    DO_TESTS=0 ;;
    --no-learn)    DO_LEARN=0 ;;
    --verify-only) DO_DEPLOY=0; DO_TESTS=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Candidate share link from our HYPD store (93944 / smartdeals). The known
# Shopsy link is intentionally absent; this candidate is learned only if
# ops/hypd_links.py verifies a Meesho destination.
OUR_HYPD_LINKS=(
  "https://hypd.store/93944/afflink/daoll7ltm6mc5h7k1fq0"
)

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python

echo "=============================================================================="
echo " FRESH DEPLOY — $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================================="
echo "repo      : $REPO"
echo "commit    : $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?') $(git -C "$REPO" branch --show-current 2>/dev/null)"
echo "mode      : deploy=$DO_DEPLOY tests=$DO_TESTS learn_hypd=$DO_LEARN dry_run=$DRY_RUN"
echo

if [[ "$DRY_RUN" == "1" ]]; then
  echo "PLAN (dry run - nothing will be changed)"
  echo "  1. ( cd $REPO && git pull --ff-only )"
  if [[ "$DO_DEPLOY" == "1" ]]; then
    [[ "$DO_TESTS" == "1" ]] && echo "  2. ops/deploy_and_verify.sh --with-tests   # suites -> bundles -> deploy -> restart"
    [[ "$DO_TESTS" == "0" ]] && echo "  2. ops/deploy_and_verify.sh --no-pull      # bundles -> deploy -> restart"
  else
    echo "  2. (deploy skipped: --verify-only)"
  fi
  if [[ "$DO_LEARN" == "1" ]]; then
    echo "  3. $PY ops/hypd_links.py ${OUR_HYPD_LINKS[0]} \\"
    for link in "${OUR_HYPD_LINKS[@]:1}"; do echo "                              $link \\"; done
    echo "       # the tool stores only links verified to land on Meesho"
  fi
  echo "  4. $PY ops/earnkaro_check.py        # live: key pays US + hypd links round-trip via Bitly"
  echo "  5. $PY ops/conversion_report.py    # per-route verdict from the live DB + log"
  echo
  echo "Nothing was changed. Run again without --dry-run to do it for real."
  exit 0
fi

FAILURES=()
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mCHECK\033[0m %s\n' "$*"; FAILURES+=("$*"); }
step() { echo; echo "==== $* ===="; }

step "1/5 THE REPO IS WHAT WE THINK IT IS"
if [[ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]]; then
  ok "working tree has local changes (deploying what is on disk)"
fi
if git -C "$REPO" pull --ff-only >/tmp/fresh-pull.log 2>&1; then
  ok "git pull --ff-only ($(git -C "$REPO" rev-parse --short HEAD))"
else
  ok "git pull skipped (offline or diverged) - deploying the local tree"
fi
if [[ "$DO_DEPLOY" == "1" ]] && ! git -C "$REPO" log --oneline -1 2>/dev/null | grep -q .; then
  bad "cannot read the repo history - is $REPO a git checkout?"
fi

if [[ "$DO_DEPLOY" == "1" ]]; then
  step "2/5 DEPLOY BOT + BRIDGE (tests -> bundles -> restart -> hash/version proof)"
  DEPLOY_ARGS=(--no-pull)
  [[ "$DO_TESTS" == "1" ]] && DEPLOY_ARGS+=(--with-tests)
  if ( cd "$REPO" && ./ops/deploy_and_verify.sh "${DEPLOY_ARGS[@]}" ); then
    ok "deploy_and_verify.sh finished"
  else
    bad "deploy_and_verify.sh reported CHECK lines above - read them first"
  fi
else
  step "2/5 DEPLOY (skipped: --verify-only)"
fi

step "3/5 VERIFY AND LEARN OUR HYPD CANDIDATES (Meesho only)"
if [[ "$DO_LEARN" == "1" ]]; then
  if ( cd "$REPO" && "$PY" ops/hypd_links.py --list ) | grep -q "hypd.store/93944"; then
    ok "at least one of OUR hypd links is already known"
  fi
  if ( cd "$REPO" && "$PY" ops/hypd_links.py "${OUR_HYPD_LINKS[@]}" ); then
    ok "verified Meesho share links are learned (product -> OUR link)"
  else
    bad "hypd_links.py could not learn every link - run it alone to see which one"
  fi
  WANTED="$( cd "$REPO" && "$PY" ops/hypd_links.py --wanted 2>/dev/null | grep -c 'https://www\.' || true )"
  if [[ "${WANTED:-0}" -gt 0 ]]; then
    bad "$WANTED Meesho product(s) still have NO HYPD link - curate them in the HYPD app"
  else
    ok "no product is waiting for a hypd link"
  fi
else
  ok "skipped (--no-learn)"
fi

step "4/5 LIVE PROOF: DOES THE MONEY ROUTE END AT US?"
if ( cd "$REPO" && "$PY" ops/earnkaro_check.py ); then
  ok "earnkaro_check.py: key live AND our hypd links come back to our store"
else
  bad "earnkaro_check.py found something to fix (read its RESULT block)"
fi

step "5/5 PER-ROUTE STATUS FROM LIVE DATA (last 24h)"
if ( cd "$REPO" && "$PY" ops/conversion_report.py ); then
  ok "conversion_report.py: nothing needs attention"
else
  bad "conversion_report.py listed NEEDS ATTENTION items - fix those next"
fi

echo
echo "=============================================================================="
if (( ${#FAILURES[@]} == 0 )); then
  echo " FRESH DEPLOY DONE — every check passed."
  echo " Watch the first posts:"
  echo "   tail -f \${BOT_LOG:-$HOME/bestgaa-bot/bestgaa-bot/logs/bot.log} | grep -E 'EK SUCCESS|EK MISS|EK AUTH|EK REJECT|EK HTTP|EK NETWORK|EK FALLBACK|HYPD LINK|HYPD MISSING|UNMONETIZED'"
else
  echo " FRESH DEPLOY FINISHED WITH ${#FAILURES[@]} ITEM(S) TO LOOK AT:"
  for item in "${FAILURES[@]}"; do echo "   - $item"; done
  echo
  echo " Every item above names its own fix. The common ones:"
  echo "   * EarnKaro key refused      -> ./ops/set_earnkaro_key.sh '<token>'"
  echo "   * no BITLY_TOKENS on server -> add BITLY_TOKENS=<token> to bestgaa/.env, restart bestgaa"
  echo "   * products waiting for hypd -> curate in the HYPD app, then: $PY ops/hypd_links.py '<link>'"
fi
echo "=============================================================================="
exit $(( ${#FAILURES[@]} == 0 ? 0 : 1 ))
