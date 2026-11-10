#!/usr/bin/env bash
# External RMTHZ monitor for the Resource Gate, desktop grant path, and Hub API.
set -euo pipefail

HEALTH_URL="${RESOURCE_GATE_HEALTH_URL:-https://gate.dtcdev.click/}"
HEALTH_MARKER="${RESOURCE_GATE_HEALTH_MARKER:-<h1>Resource Gate</h1>}"
# Use the underlying read-only grant check so monitoring survives rollback to
# a valid older Caddyfile; the deploy smoke separately enforces /v1/health.
GATE_CHECK_URL="${RESOURCE_GATE_CHECK_URL:-https://gate.dtcdev.click/v1/check?resource=browser&scope=desktop}"
GATE_TOKEN_FILE="${RESOURCE_GATE_TOKEN_FILE:-/var/lib/resource-gate-monitor/gate-token}"
TOOLS_REMOTE_CONFIG="${AGENT_BROWSER_REMOTE_CONFIG:-/home/alexey/.agent-browser/remotes.json}"
AGENT_BROWSER_CLI="${AGENT_BROWSER_CLI:-/home/alexey/opt/agent-browser/bin/cli.js}"
NVM_DIR="${NVM_DIR:-/home/alexey/.nvm}"
NODE_BIN="${NODE_BIN:-}"
if [[ -z "$NODE_BIN" ]]; then
  if [[ -r "$NVM_DIR/nvm.sh" ]]; then
    NODE_BIN="$(NVM_DIR="$NVM_DIR" /bin/bash -c '. "$NVM_DIR/nvm.sh" >/dev/null 2>&1 && nvm which default' 2>/dev/null)" \
      || fail "cannot resolve Node.js from the configured NVM default"
  else
    NODE_BIN="$(command -v node || true)"
  fi
fi
PATH="$(dirname "$NODE_BIN"):/home/alexey/.local/bin:$PATH"
export PATH
HUB_HEALTH_URL="${HUB_HEALTH_URL:-https://hub.dtcdev.click/health}"
STATE_DIR="${RESOURCE_GATE_STATE_DIR:-/var/lib/resource-gate-monitor}"
STATE_FILE="$STATE_DIR/state"
ALERT_TO="${ALERT_TO:-dev@dtcdev.click}"
ALERT_OPS_TO="${ALERT_OPS_TO:-dev@dtcdev.click}"
ALERT_FROM="${ALERT_FROM:-alerts@dtcdev.click}"
ALERT_MSMTP_ACCOUNT="${ALERT_MSMTP_ACCOUNT:-alerts}"
DATAOPS_BIN="${DATAOPS_BIN:-/home/alexey/.local/bin/dataops}"
DATAOPS_CONFIG_DIR="${DATAOPS_CONFIG_DIR:-/home/alexey/.config/dataops}"
FAILURE_THRESHOLD=2
RECOVERY_THRESHOLD=2

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 2; }

for command_name in curl msmtp flock date python3 timeout "$NODE_BIN"; do
  command -v "$command_name" >/dev/null 2>&1 || fail "required command is missing: $command_name"
done
[[ -x "$NODE_BIN" ]] || fail "Node.js runtime is not executable: ${NODE_BIN:-unset}"
[[ -x "$DATAOPS_BIN" ]] || fail "DataOps CLI is missing or not executable: $DATAOPS_BIN"
[[ -r "$AGENT_BROWSER_CLI" ]] || fail "Agent Browser CLI is not readable: $AGENT_BROWSER_CLI"

mkdir -p "$STATE_DIR" || fail "cannot create monitor state directory"
umask 077
exec 9>"$STATE_DIR/check.lock" || fail "cannot open monitor lock"
if ! flock -n 9; then
  log "SKIP overlapping Resource Gate and Hub check"
  exit 0
fi

LAST_STATUS=unknown
FAILURE_COUNT=0
LAST_ALERT=0
INCIDENT_ID=-
DATAOPS_TASK_ID=-
ALERT_SENT=0
RECOVERY_MAIL_SENT=0
RECOVERY_TASK_NOTED=0
RECOVERY_OK_COUNT=0

if [[ -e "$STATE_FILE" ]]; then
  read -r -a STATE_FIELDS < "$STATE_FILE" || fail "monitor state cannot be read"
  if (( ${#STATE_FIELDS[@]} == 2 )); then
    # Migrate the state written by the original one-endpoint monitor.
    LAST_STATUS="${STATE_FIELDS[0]}"
    LAST_ALERT="${STATE_FIELDS[1]}"
    if [[ "$LAST_STATUS" == fail ]]; then
      FAILURE_COUNT=2
      INCIDENT_ID="$LAST_ALERT"
      ALERT_SENT=1
    else
      FAILURE_COUNT=0
      LAST_STATUS=ok
    fi
  elif (( ${#STATE_FIELDS[@]} == 8 || ${#STATE_FIELDS[@]} == 9 )); then
    LAST_STATUS="${STATE_FIELDS[0]}"
    FAILURE_COUNT="${STATE_FIELDS[1]}"
    LAST_ALERT="${STATE_FIELDS[2]}"
    INCIDENT_ID="${STATE_FIELDS[3]}"
    DATAOPS_TASK_ID="${STATE_FIELDS[4]}"
    ALERT_SENT="${STATE_FIELDS[5]}"
    RECOVERY_MAIL_SENT="${STATE_FIELDS[6]}"
    RECOVERY_TASK_NOTED="${STATE_FIELDS[7]}"
    if (( ${#STATE_FIELDS[@]} == 9 )); then
      RECOVERY_OK_COUNT="${STATE_FIELDS[8]}"
    fi
  else
    fail "monitor state has an unsupported format"
  fi
  [[ "$LAST_STATUS" == unknown || "$LAST_STATUS" == ok || "$LAST_STATUS" == pending || "$LAST_STATUS" == fail ]] \
    || fail "monitor state status is invalid"
  [[ "$FAILURE_COUNT" =~ ^[0-9]+$ && "$LAST_ALERT" =~ ^[0-9]+$ ]] \
    || fail "monitor state counters are invalid"
  [[ "$RECOVERY_OK_COUNT" =~ ^[0-9]+$ ]] \
    || fail "monitor recovery counter is invalid"
  [[ "$ALERT_SENT" =~ ^[01]$ && "$RECOVERY_MAIL_SENT" =~ ^[01]$ && "$RECOVERY_TASK_NOTED" =~ ^[01]$ ]] \
    || fail "monitor state delivery flags are invalid"
fi

write_state() {
  local status="$1" failures="$2" last_alert="$3" incident="$4" task_id="$5"
  local alert_sent="$6" recovery_mail_sent="$7" recovery_task_noted="$8" recovery_ok_count="$9"
  local temporary="$STATE_FILE.tmp.$$"
  printf '%s %s %s %s %s %s %s %s %s\n' \
    "$status" "$failures" "$last_alert" "$incident" "$task_id" \
    "$alert_sent" "$recovery_mail_sent" "$recovery_task_noted" "$recovery_ok_count" > "$temporary" \
    || fail "cannot write monitor state"
  chmod 600 "$temporary"
  mv -f -- "$temporary" "$STATE_FILE" || fail "cannot install monitor state"
}

TEST_PATH_GUARD=0
case "$STATE_DIR" in
  /tmp|/tmp/*|/var/tmp|/var/tmp/*|*/.local/tmp|*/.local/tmp/*) TEST_PATH_GUARD=1 ;;
esac
EFFECTIVE_TO="$ALERT_TO"
DATAOPS_ENABLED=1
if (( TEST_PATH_GUARD )); then
  EFFECTIVE_TO="$ALERT_OPS_TO"
  [[ "${RESOURCE_GATE_TEST_ALLOW_DATAOPS:-0}" == 1 ]] || DATAOPS_ENABLED=0
  log "GUARD test state path: routing mail to ops recipient only"
fi
ALERT_RECIPIENT_SUMMARY="$EFFECTIVE_TO"

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

create_dataops_task() {
  local description="$1" comment="$2" response task_id
  (( DATAOPS_ENABLED )) || return 1
  response="$(DATAOPS_CONFIG_DIR="$DATAOPS_CONFIG_DIR" "$DATAOPS_BIN" tasks create \
    --description "$description" \
    --assignee me \
    --tags "resource-gate,hub-uptime,monitoring" \
    --browser headless \
    --comment "$comment" \
    --link "https://hub.dtcdev.click/" \
    --json 2>/dev/null)" || return 1
  task_id="$(python3 -c 'import json,sys; obj=json.load(sys.stdin); value=obj.get("id", "") if isinstance(obj,dict) else ""; print(value if isinstance(value,str) else "")' <<< "$response" 2>/dev/null)" || return 1
  [[ "$task_id" =~ ^[A-Za-z0-9_-]{8,}$ ]] || return 1
  printf '%s\n' "$task_id"
}

note_dataops_task() {
  local task_id="$1" note="$2"
  (( DATAOPS_ENABLED )) || return 1
  DATAOPS_CONFIG_DIR="$DATAOPS_CONFIG_DIR" "$DATAOPS_BIN" tasks note "$task_id" "$note" \
    >/dev/null 2>&1
}

if [[ "${1:-}" == --test-alert ]]; then
  [[ $# -eq 1 ]] || fail "--test-alert takes no additional arguments"
  printf -v TEST_BODY 'TEST ALERT: Resource Gate and Hub monitor email delivery was tested at %s UTC.\n\nThis message confirms the configured alert route; no outage was declared.' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  send_alert "[TEST] Resource Gate and Hub uptime monitor" "$TEST_BODY" \
    || fail "test alert email failed to send to $ALERT_RECIPIENT_SUMMARY"
  TEST_TASK_ID="$(create_dataops_task \
    "TEST: Resource Gate and Hub uptime alert delivery" \
    "Monitor test at $(date -u '+%Y-%m-%dT%H:%M:%SZ'): alert email accepted by msmtp for $ALERT_RECIPIENT_SUMMARY.")" \
    || fail "test alert email was sent, but DataOps test task creation failed"
  DATAOPS_CONFIG_DIR="$DATAOPS_CONFIG_DIR" "$DATAOPS_BIN" tasks done "$TEST_TASK_ID" \
    --comment "Closed after confirming the Resource Gate and Hub test alert was sent to $ALERT_RECIPIENT_SUMMARY." \
    >/dev/null 2>&1 \
    || fail "test alert was sent and task $TEST_TASK_ID was created, but the test task could not be closed"
  log "TEST alert email sent to $ALERT_RECIPIENT_SUMMARY; DataOps test task $TEST_TASK_ID created and closed"
  exit 0
fi
[[ $# -eq 0 ]] || fail "usage: resource-gate-monitor.sh [--test-alert]"

AUTH_FILE=""
BODY_FILE=""
REMOTE_FILE=""
cleanup() {
  [[ -z "$AUTH_FILE" || ! -e "$AUTH_FILE" ]] || unlink "$AUTH_FILE"
  [[ -z "$BODY_FILE" || ! -e "$BODY_FILE" ]] || unlink "$BODY_FILE"
  [[ -z "$REMOTE_FILE" || ! -e "$REMOTE_FILE" ]] || unlink "$REMOTE_FILE"
}
trap cleanup EXIT

make_auth_file() {
  local token
  [[ -r "$GATE_TOKEN_FILE" ]] || return 1
  token="$(tr -d '\r\n' < "$GATE_TOKEN_FILE")"
  [[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || return 1
  AUTH_FILE="$(mktemp "$STATE_DIR/.gate-auth.XXXXXX")" || return 1
  chmod 600 "$AUTH_FILE"
  printf 'header = "Authorization: Bearer %s"\n' "$token" > "$AUTH_FILE"
  unset token
}

http_get() {
  local url="$1" output="$2" auth_file="${3:-}" code
  local -a arguments=(--silent --show-error --connect-timeout 5 --max-time 12 --output "$output" --write-out '%{http_code}')
  [[ -z "$auth_file" ]] || arguments+=(--config "$auth_file")
  if ! code="$(curl "${arguments[@]}" "$url" 2>/dev/null)"; then
    return 1
  fi
  [[ "$code" =~ ^[0-9]{3}$ ]] || return 1
  printf '%s\n' "$code"
}

FAILURES=()
record_failure() { FAILURES+=("$1"); }

BODY_FILE="$(mktemp "$STATE_DIR/.probe-body.XXXXXX")" || fail "cannot create probe response file"

# 1. Public TLS entry point and expected Resource Gate page.
HTTP_CODE=""
if HTTP_CODE="$(http_get "$HEALTH_URL" "$BODY_FILE")" \
  && [[ "$HTTP_CODE" == 200 ]] \
  && grep -Fq -- "$HEALTH_MARKER" "$BODY_FILE"; then
  log "OK Resource Gate TLS page is reachable at $HEALTH_URL"
else
  record_failure "gate HTTPS page check failed (HTTP ${HTTP_CODE:-no response})"
fi

# 2. Authenticated, non-mutating grant evaluation for the desktop's browser
#    device. The device's service name is `desktop`; it maps to DESKTOP-FP4OP26.
GATE_GRANT_STATE=unknown
if make_auth_file; then
  : > "$BODY_FILE"
  HTTP_CODE=""
  if HTTP_CODE="$(http_get "$GATE_CHECK_URL" "$BODY_FILE" "$AUTH_FILE")" \
    && [[ "$HTTP_CODE" == 200 ]]; then
    if GATE_GRANT_STATE="$(python3 - "$BODY_FILE" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get("granted")
except Exception:
    raise SystemExit(2)
if type(value) is not bool:
    raise SystemExit(2)
print("active" if value else "inactive")
PY
    )"; then
      log "OK authenticated Resource Gate browser grant check for DESKTOP-FP4OP26: $GATE_GRANT_STATE"
    else
      record_failure "authenticated Resource Gate grant check returned an invalid response"
    fi
  else
    record_failure "authenticated Resource Gate grant check failed (HTTP ${HTTP_CODE:-no response})"
  fi
else
  record_failure "Resource Gate probe credential is missing or malformed"
fi

# 3. Authenticated gateway read checks the selected desktop and its current
#    grant state. An inactive grant is normal between approved browser tasks;
#    transport/auth failure or an offline desktop is an outage.
REMOTE_FILE="$(mktemp "$STATE_DIR/.desktop-health.XXXXXX")" || fail "cannot create desktop health file"
REMOTE_EXIT=0
AGENT_BROWSER_REMOTE_CONFIG="$TOOLS_REMOTE_CONFIG" timeout 25 "$NODE_BIN" "$AGENT_BROWSER_CLI" \
  remote health desktop > "$REMOTE_FILE" 2>/dev/null || REMOTE_EXIT=$?
if (( REMOTE_EXIT > 1 )); then
  record_failure "authenticated tools gateway or DESKTOP-FP4OP26 health check failed (exit $REMOTE_EXIT)"
elif DESKTOP_GRANT_STATE="$(python3 - "$REMOTE_FILE" <<'PY'
import json, sys
try:
    result = json.load(open(sys.argv[1], encoding="utf-8"))
    service = result.get("service")
    grant = result.get("grant")
    if (result.get("device") != "desktop" or result.get("desktop") != "online"
            or not isinstance(service, dict) or service.get("ok") is not True
            or not isinstance(grant, dict) or type(grant.get("active")) is not bool
            or result.get("error")):
        raise SystemExit(2)
except Exception:
    raise SystemExit(2)
print("active" if grant["active"] else "inactive")
PY
)"; then
  log "OK authenticated tools gateway and DESKTOP-FP4OP26 health; browser grant is $DESKTOP_GRANT_STATE"
else
  record_failure "authenticated tools gateway or DESKTOP-FP4OP26 health check failed (exit $REMOTE_EXIT)"
fi

# 4. Public, read-only Hub API health endpoint used on the same hostname as
#    Hub clients. Require the API's healthy JSON response, not just a TCP/TLS
#    handshake.
: > "$BODY_FILE"
HTTP_CODE=""
if HTTP_CODE="$(http_get "$HUB_HEALTH_URL" "$BODY_FILE")" \
  && [[ "$HTTP_CODE" == 200 ]] \
  && python3 - "$BODY_FILE" <<'PY'
import json, sys
try:
    response = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if response.get("ok") is True and isinstance(response.get("service"), str) else 1)
PY
then
  log "OK Hub API health read succeeded at $HUB_HEALTH_URL"
else
  record_failure "Hub API health read failed (HTTP ${HTTP_CODE:-no response})"
fi

if (( ${#FAILURES[@]} == 0 )); then
  if [[ "$LAST_STATUS" == fail ]]; then
    RECOVERY_OK_COUNT=$((RECOVERY_OK_COUNT + 1))
    if (( RECOVERY_OK_COUNT < RECOVERY_THRESHOLD )); then
      write_state fail "$FAILURE_COUNT" "$LAST_ALERT" "$INCIDENT_ID" "$DATAOPS_TASK_ID" \
        "$ALERT_SENT" "$RECOVERY_MAIL_SENT" "$RECOVERY_TASK_NOTED" "$RECOVERY_OK_COUNT"
      log "RECOVERY_PENDING healthy check $RECOVERY_OK_COUNT/$RECOVERY_THRESHOLD; incident remains open"
      exit 0
    fi

    if (( RECOVERY_MAIL_SENT == 0 )); then
      printf -v BODY 'Resource Gate, desktop grant path, and Hub API recovered at %s UTC.\nIncident: %s' \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$INCIDENT_ID"
      if send_alert "[RECOVERED] Resource Gate and Hub API are healthy" "$BODY"; then
        RECOVERY_MAIL_SENT=1
        log "RECOVERED email sent to $EFFECTIVE_TO"
      else
        log "CRITICAL service checks recovered, but the recovery email failed; will retry"
      fi
    fi
    if [[ "$DATAOPS_TASK_ID" == - ]]; then
      if CREATED_TASK_ID="$(create_dataops_task \
        "Resource Gate/Hub uptime alert ($INCIDENT_ID)" \
        "Two or more consecutive external checks failed before recovery at $(date -u '+%Y-%m-%dT%H:%M:%SZ').")"; then
        DATAOPS_TASK_ID="$CREATED_TASK_ID"
      else
        log "CRITICAL outage recovered, but DataOps incident task creation failed; will retry"
      fi
    fi
    if [[ "$DATAOPS_TASK_ID" != - && "$RECOVERY_TASK_NOTED" == 0 ]]; then
      if note_dataops_task "$DATAOPS_TASK_ID" \
        "Recovered at $(date -u '+%Y-%m-%dT%H:%M:%SZ') UTC. External gate, desktop grant path, and Hub API reads are healthy."; then
        RECOVERY_TASK_NOTED=1
        log "DataOps incident task $DATAOPS_TASK_ID updated with recovery"
      else
        log "CRITICAL services recovered, but DataOps recovery note failed; will retry"
      fi
    fi
    if (( RECOVERY_MAIL_SENT == 1 && RECOVERY_TASK_NOTED == 1 )); then
      write_state ok 0 "$LAST_ALERT" - - 0 0 0 0
      log "OK Resource Gate, desktop grant path, and Hub API are healthy"
      exit 0
    fi
    write_state fail "$FAILURE_COUNT" "$LAST_ALERT" "$INCIDENT_ID" "$DATAOPS_TASK_ID" \
      "$ALERT_SENT" "$RECOVERY_MAIL_SENT" "$RECOVERY_TASK_NOTED" "$RECOVERY_OK_COUNT"
    exit 2
  fi

  write_state ok 0 "$LAST_ALERT" - - 0 0 0 0
  log "OK Resource Gate, desktop grant path, and Hub API are healthy"
  exit 0
fi

FAILURE_SUMMARY="$(printf '%s; ' "${FAILURES[@]}")"
FAILURE_SUMMARY="${FAILURE_SUMMARY%; }"
RECOVERY_OK_COUNT=0
if [[ "$LAST_STATUS" == pending || "$LAST_STATUS" == fail ]]; then
  FAILURE_COUNT=$((FAILURE_COUNT + 1))
else
  FAILURE_COUNT=1
fi

if (( FAILURE_COUNT < FAILURE_THRESHOLD )); then
  write_state pending "$FAILURE_COUNT" "$LAST_ALERT" - - 0 0 0 0
  log "SUSPECT external health check failure 1/$FAILURE_THRESHOLD: $FAILURE_SUMMARY"
  exit 1
fi

if [[ "$LAST_STATUS" != fail ]]; then
  INCIDENT_ID="$(date -u '+%Y%m%dT%H%M%SZ')"
  DATAOPS_TASK_ID=-
  ALERT_SENT=0
  RECOVERY_MAIL_SENT=0
  RECOVERY_TASK_NOTED=0
fi

DELIVERY_FAILED=0
if (( ALERT_SENT == 0 )); then
  printf -v BODY 'CRITICAL: external Resource Gate, desktop grant path, or Hub API checks failed at %s UTC.\nConsecutive failed checks: %s\nIncident: %s\nFailures: %s' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$FAILURE_COUNT" "$INCIDENT_ID" "$FAILURE_SUMMARY"
  if send_alert "[CRITICAL] Resource Gate and Hub availability is degraded" "$BODY"; then
    ALERT_SENT=1
    LAST_ALERT="$(date +%s)"
    log "CRITICAL outage email sent to $EFFECTIVE_TO after $FAILURE_COUNT consecutive failures"
  else
    DELIVERY_FAILED=1
    log "CRITICAL outage email delivery failed; will retry"
  fi
fi

if [[ "$DATAOPS_TASK_ID" == - ]]; then
  if CREATED_TASK_ID="$(create_dataops_task \
    "Resource Gate/Hub uptime alert ($INCIDENT_ID)" \
    "Two consecutive external checks failed at $(date -u '+%Y-%m-%dT%H:%M:%SZ') UTC. Checks: $FAILURE_SUMMARY")"; then
    DATAOPS_TASK_ID="$CREATED_TASK_ID"
  else
    DELIVERY_FAILED=1
    log "CRITICAL outage detected, but DataOps incident task creation failed; will retry"
  fi
  if [[ "$DATAOPS_TASK_ID" != - ]]; then
    log "DataOps incident task $DATAOPS_TASK_ID created"
  fi
fi

write_state fail "$FAILURE_COUNT" "$LAST_ALERT" "$INCIDENT_ID" "$DATAOPS_TASK_ID" \
  "$ALERT_SENT" "$RECOVERY_MAIL_SENT" "$RECOVERY_TASK_NOTED" "$RECOVERY_OK_COUNT"
if (( DELIVERY_FAILED )); then
  exit 2
fi
exit 1
