#!/usr/bin/env bash
# Cron watchdog for the email poller: the script docs/PRELAUNCH_DRILLS_RUNBOOK.md
# 5.1.3 has the operator type in, shipped so it can be copied instead:
#
#   sudo cp scripts/check-health-cron.sh /usr/local/bin/bestteam-health-check.sh
#   sudo chmod +x /usr/local/bin/bestteam-health-check.sh
#
# `admin check-health` has to run from outside the container: every in-app
# alert is delivered by the poll loop itself, so a stalled poller cannot report
# its own stall. Writes to the log only when the check fails -- an empty log
# means "fine every time" -- and passes the exit code through unchanged.
#
# Optional: with BESTTEAM_OPS_WEBHOOK_URL set (in the crontab line, or exported
# by the caller), a failure is also POSTed there as JSON carrying both `text`
# and `content`, so a Slack- or Discord-style incoming webhook shows it with no
# translation layer. Delivery is best-effort and never changes the exit code.
set -uo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/bestteam}"
LOG="${LOG:-/var/log/bestteam-health.log}"
# cron's PATH is short; `command -v docker` says where yours is.
DOCKER="${DOCKER:-/usr/bin/docker}"

cd "$DEPLOY_DIR" || { echo "=== $(date -Is) cannot cd to $DEPLOY_DIR" >> "$LOG"; exit 1; }
# -T: cron has no terminal, and without it `exec` fails on TTY allocation --
# every alert would then be a false one.
output=$("$DOCKER" compose exec -T backend python -m ui.backend.admin check-health 2>&1)
status=$?

if [ "$status" -ne 0 ]; then
  {
    echo "=== $(date -Is) check-health exit=$status"
    echo "$output"
  } >> "$LOG"
  if [ -n "${BESTTEAM_OPS_WEBHOOK_URL:-}" ]; then
    # Header plus the first lines: the webhook is the pager, the log is the record.
    summary=$(printf 'bestteam check-health FAILED (exit %s) on %s at %s\n%s' \
      "$status" "$(hostname)" "$(date -Is)" "$(printf '%s\n' "$output" | head -n 12)")
    payload=$(printf '%s' "$summary" | python3 -c 'import json, sys; s = sys.stdin.read(); print(json.dumps({"text": s, "content": s}))')
    curl -fsS -m 10 -X POST -H 'Content-Type: application/json' -d "$payload" "$BESTTEAM_OPS_WEBHOOK_URL" > /dev/null 2>&1 \
      || echo "    (webhook delivery failed)" >> "$LOG"
  fi
fi
exit "$status"
