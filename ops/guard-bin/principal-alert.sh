#!/usr/bin/env bash
# Alert hook for ops watchdog and principal-ping (PRINCIPAL_ALERT_CMD).
# Lives outside ~/.local/bin (in ops/guard-bin and ~/.local/share/agent-hub-ops/guard-bin)
# so it remains reachable even when ~/.local/bin itself vanishes (incident 504d662b).
# Delivery: e-mail via the user's msmtp (~/.msmtprc, never in git) to ops, plus a
# local log line so an alert is never silently lost.
set -u
msg="${PING_ALERT_MESSAGE:-${1:-principal-ping alert (no message)}}"
to="${PRINCIPAL_ALERT_TO:-dev@dtcdev.click}"
log="${PRINCIPAL_ALERT_LOG:-$HOME/.local/state/principal-ping/alerts.log}"
mkdir -p "$(dirname "$log")" 2>/dev/null || true
ts=$(date -u +%FT%TZ)
printf '%s %s\n' "$ts" "$msg" >> "$log" 2>/dev/null || true
if [ -n "${ALERT_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$ALERT_LOG"
  exit 0
fi
if command -v msmtp >/dev/null 2>&1; then
  printf 'To: %s\nSubject: [principal-ping] %s\n\n%s\n\nhost: %s\ntime: %s\n' \
    "$to" "${msg:0:120}" "$msg" "$(hostname)" "$ts" | msmtp -- "$to" \
    || { echo "$ts msmtp failed" >> "$log" 2>/dev/null || true; exit 1; }
else
  echo "$ts msmtp missing; alert only logged" >> "$log" 2>/dev/null || true; exit 1
fi
