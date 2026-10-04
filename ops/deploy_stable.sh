#!/usr/bin/env bash
# ============================================================================
# STABLE DEPLOY — run the whole deploy in tmux so a dropped SSH cannot kill it.
#
# WHY THIS EXISTS (2026-10-04): the operator's SSH session disconnected TWICE in
# the middle of `ops/deploy_fresh.sh` ("client_loop: send disconnect: Connection
# reset"). A shell command dies with its controlling terminal, so the deploy was
# killed mid-suites every time - and a half-finished deploy looks like "the
# deploy ran but nothing changed".
#
#   ./ops/deploy_stable.sh            # deploy_fresh.sh inside tmux, logged
#   ./ops/deploy_stable.sh --attach   # start it and attach straight away
#   ./ops/deploy_stable.sh --status   # is the deploy still running?
#   ./ops/deploy_stable.sh --log      # follow the log
#
# No tmux installed? It falls back to nohup, which survives a disconnect too.
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SESSION="bestgaa-deploy"
LOG="/tmp/deploy-$(date +%Y%m%d_%H%M%S).log"

case "${1:-}" in
  --status)
    if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "deploy RUNNING in tmux session '$SESSION'"
      tmux capture-pane -pt "$SESSION" | tail -25
    else
      echo "no tmux deploy session. Latest logs:"
      ls -1t /tmp/deploy-*.log 2>/dev/null | head -3
    fi
    exit 0
    ;;
  --log)
    LATEST="$(ls -1t /tmp/deploy-*.log 2>/dev/null | head -1)"
    [[ -n "$LATEST" ]] || { echo "no deploy log yet in /tmp" >&2; exit 1; }
    echo "following $LATEST  (Ctrl-C stops watching, the deploy keeps running)"
    tail -n 40 -f "$LATEST"
    exit 0
    ;;
esac

cd "$REPO" || exit 1

if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" \
    "cd '$REPO' && ./ops/deploy_fresh.sh 2>&1 | tee '$LOG'; echo; echo \"DEPLOY FINISHED - exit \$? - log: $LOG\"; sleep 120"
  echo "Started in tmux session '$SESSION' (SSH drop cannot kill it)."
  echo "  watch : tmux attach -t $SESSION        (detach: Ctrl-B then D)"
  echo "  or    : tail -f $LOG"
  echo "  status: ./ops/deploy_stable.sh --status"
  [[ "${1:-}" == "--attach" ]] && exec tmux attach -t "$SESSION"
  exit 0
fi

echo "tmux is not installed - using nohup instead (also survives a disconnect)."
nohup bash -c "cd '$REPO' && ./ops/deploy_fresh.sh" >"$LOG" 2>&1 &
echo "Started with nohup (pid $!)."
echo "  follow: tail -f $LOG"
