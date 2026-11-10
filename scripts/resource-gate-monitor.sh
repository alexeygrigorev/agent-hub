#!/usr/bin/env bash
# Check the public Resource Gate site from RMTHZ and page dev@ on failure.
set -euo pipefail

HEALTH_URL="${RESOURCE_GATE_HEALTH_URL:-https://gate.dtcdev.click/}"
HEALTH_MARKER="${RESOURCE_GATE_HEALTH_MARKER:-<h1>Resource Gate</h1>}"
STATE_DIR="${RESOURCE_GATE_STATE_DIR:-/var/lib/resource-gate-monitor}"
STATE_FILE="$STATE_DIR/state"
ALERT_TO="${ALERT_TO:-dev@dtcdev.click}"
ALERT_OPS_TO="${ALERT_OPS_TO:-dev@dtcdev.click}"
ALERT_FROM="${ALERT_FROM:-alerts@dtcdev.click}"
ALERT_MSMTP_ACCOUNT="${ALERT_MSMTP_ACCOUNT:-alerts}"
ALERT_REPEAT_SECONDS="${ALERT_REPEAT_SECONDS:-900}"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 2; }

[[ "$ALERT_REPEAT_SECONDS" =~ ^[0-9]+$ ]] && (( ALERT_REPEAT_SECONDS > 0 )) \
  || fail "ALERT_REPEAT_SECONDS must be a positive integer"
for command_name in curl msmtp flock date; do
  command -v "$command_name" >/dev/null 2>&1 || fail "required command is missing: $command_name"
done

mkdir -p "$STATE_DIR" || fail "cannot create state directory"
umask 077
exec 9>"$STATE_DIR/check.lock" || fail "cannot open monitor lock"
if ! flock -n 9; then
  log "SKIP overlapping Resource Gate check"
  exit 0
fi

LAST_STATUS=unknown
LAST_ALERT=0
if [[ -e "$STATE_FILE" ]]; then
  IFS=' ' read -r LAST_STATUS LAST_ALERT < "$STATE_FILE" \
    || fail "monitor state cannot be read"
  [[ "$LAST_STATUS" == unknown || "$LAST_STATUS" == ok || "$LAST_STATUS" == fail ]] \
    || fail "monitor state status is invalid"
  [[ "$LAST_ALERT" =~ ^[0-9]+$ ]] || fail "monitor state alert time is invalid"
fi

TEST_PATH_GUARD=0
case "$STATE_DIR" in
  /tmp|/tmp/*|/var/tmp|/var/tmp/*) TEST_PATH_GUARD=1 ;;
esac
EFFECTIVE_TO="$ALERT_TO"
if (( TEST_PATH_GUARD )); then
  EFFECTIVE_TO="$ALERT_OPS_TO"
  log "GUARD test state path: routing mail to ops recipient only"
fi

write_state() {
  local status="$1" last_alert="$2" temporary="$STATE_FILE.tmp.$$"
  printf '%s %s\n' "$status" "$last_alert" > "$temporary" \
    || fail "cannot write monitor state"
  chmod 600 "$temporary"
  mv -f -- "$temporary" "$STATE_FILE" || fail "cannot install monitor state"
}

send_alert() {
  local subject="$1" body="$2"
  {
    printf 'From: %s\n' "$ALERT_FROM"
    printf 'To: %s\n' "$EFFECTIVE_TO"
    printf 'Subject: %s\n' "$subject"
    printf 'Date: %s\n\n' "$(date -R)"
    printf '%s\n' "$body"
  } | msmtp -a "$ALERT_MSMTP_ACCOUNT" -f "$ALERT_FROM" -- "$EFFECTIVE_TO" 2>/dev/null
}

NOW="$(date +%s)"
FAILURE=""
if RESPONSE="$(curl -fsS --connect-timeout 8 --max-time 15 "$HEALTH_URL" 2>/dev/null)"; then
  if [[ "$RESPONSE" != *"$HEALTH_MARKER"* ]]; then
    FAILURE="HTTPS response did not contain the Resource Gate page marker"
  fi
else
  FAILURE="HTTPS health request failed"
fi

if [[ -z "$FAILURE" ]]; then
  if [[ "$LAST_STATUS" == fail ]]; then
    printf -v BODY 'Resource Gate recovered at %s.\nURL: %s' \
      "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$HEALTH_URL"
    if ! send_alert "[RECOVERED] Resource Gate is reachable" "$BODY"; then
      log "CRITICAL Resource Gate is healthy, but the recovery email failed; will retry"
      exit 2
    fi
    log "RECOVERED Resource Gate reachable; email sent to $EFFECTIVE_TO"
  fi
  write_state ok "$LAST_ALERT"
  log "OK Resource Gate TLS page is reachable at $HEALTH_URL"
  exit 0
fi

SHOULD_ALERT=0
if [[ "$LAST_STATUS" != fail ]] || (( NOW - LAST_ALERT >= ALERT_REPEAT_SECONDS )); then
  SHOULD_ALERT=1
fi
if (( SHOULD_ALERT )); then
  printf -v BODY 'CRITICAL Resource Gate is unreachable at %s.\nURL: %s\nFailure: %s\nRMTHZ monitor state: %s' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$HEALTH_URL" "$FAILURE" "$STATE_FILE"
  if ! send_alert "[CRITICAL] Resource Gate is unreachable" "$BODY"; then
    write_state fail "$LAST_ALERT"
    log "CRITICAL Resource Gate is unreachable; email delivery failed, retrying next check"
    exit 2
  fi
  LAST_ALERT="$NOW"
  log "CRITICAL Resource Gate is unreachable; failure=$FAILURE; email sent to $EFFECTIVE_TO"
else
  log "CRITICAL Resource Gate is still unreachable; failure=$FAILURE; repeat email is not due yet"
fi

write_state fail "$LAST_ALERT"
exit 1
