#!/usr/bin/env bash
# bestgaa_restart_gate.sh — the ONLY restart command the dashboard is allowed
# to run while posting is awaiting operator approval.
#
# Installed as /usr/local/sbin/bestgaa-restart-gate and pointed at by
# DASHBOARD_RESTART_CMD in bestgaa-dashboard.service. While /etc/bestgaa/
# POSTING_APPROVED is absent, ANY restart request is refused: a stray click on
# the dashboard's "restart" badge can never start posting before the operator
# has said yes (ops/approve_posting.sh --i-approve-posting).
set -Eeuo pipefail

APPROVAL_MARK="${APPROVAL_MARK:-/etc/bestgaa/POSTING_APPROVED}"

if [[ ! -f "$APPROVAL_MARK" ]]; then
  echo "REFUSED: posting is locked until operator approval." >&2
  echo "         Nothing was restarted. To unlock:" >&2
  echo "         sudo bash ops/approve_posting.sh --i-approve-posting" >&2
  exit 1
fi

echo "approval mark present ($(cat "$APPROVAL_MARK" 2>/dev/null || echo '?')) - restarting bestgaa"
exec systemctl restart bestgaa
