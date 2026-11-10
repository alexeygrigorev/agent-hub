#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/resource-gate-monitor.sh"
TMP_ROOT="$ROOT/.local/tmp"
mkdir -p "$TMP_ROOT"
TMP="$(mktemp -d "$TMP_ROOT/resource-gate-monitor.XXXXXX")"
cleanup() {
  python3 - "$TMP" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1])
PY
}
trap cleanup EXIT
mkdir -p "$TMP/bin" "$TMP/dataops-config"

cat > "$TMP/bin/curl" <<'CURL'
#!/usr/bin/env bash
set -euo pipefail
out=""
config=""
url=""
while (($#)); do
  case "$1" in
    --output) out="$2"; shift 2 ;;
    --config) config="$2"; shift 2 ;;
    --write-out|--connect-timeout|--max-time) shift 2 ;;
    --silent|--show-error) shift ;;
    *) url="$1"; shift ;;
  esac
done
[[ -n "$out" && -n "$url" ]] || exit 90
code=200
body=''
case "$url" in
  https://gate.dtcdev.click/)
    if [[ "${CURL_MODE:-success}" == gate-fail || "${CURL_MODE:-success}" == all-fail ]]; then
      code=503; body='unavailable'
    else
      body='<h1>Resource Gate</h1>'
    fi
    ;;
  'https://gate.dtcdev.click/v1/check?resource=browser&scope=desktop')
    if [[ -z "$config" ]] || ! grep -Fq 'Authorization: Bearer synthetic-monitor-token' "$config"; then
      code=401; body='{"error":"unauthorized"}'
    elif [[ "${CURL_MODE:-success}" == grant-fail || "${CURL_MODE:-success}" == all-fail ]]; then
      code=503; body='{"error":"unavailable"}'
    else
      body='{"granted":false}'
    fi
    ;;
  https://hub.dtcdev.click/health)
    case "${CURL_MODE:-success}" in
      hub-fail|all-fail) code=503; body='unavailable' ;;
      wrong-hub) body='{"ok":false,"service":"hub2"}' ;;
      *) body='{"ok":true,"service":"hub2"}' ;;
    esac
    ;;
  *) code=404; body='{}' ;;
esac
printf '%s' "$body" > "$out"
printf '%s' "$code"
CURL

cat > "$TMP/bin/node" <<'NODE'
#!/usr/bin/env bash
set -euo pipefail
[[ "$2" == remote && "$3" == health && "$4" == desktop ]] || exit 90
case "${REMOTE_MODE:-success}" in
  success|grant-inactive)
    printf '%s\n' '{"ok":false,"service":{"ok":true,"devicesOnline":1},"device":"desktop","desktop":"online","grant":{"active":false,"reason":"No active grant"}}'
    exit 1
    ;;
  grant-active)
    printf '%s\n' '{"ok":true,"service":{"ok":true,"devicesOnline":1},"device":"desktop","desktop":"online","grant":{"active":true}}'
    exit 0
    ;;
  desktop-offline)
    printf '%s\n' '{"ok":false,"service":{"ok":true,"devicesOnline":0},"device":"desktop","desktop":"offline","grant":{"active":false}}'
    exit 1
    ;;
  auth-failed)
    printf '%s\n' '{"ok":false,"service":{"ok":true,"devicesOnline":1},"device":"desktop","desktop":"online","grant":{"active":false},"error":"unauthorized"}'
    exit 1
    ;;
  *) exit 91 ;;
esac
NODE

cat > "$TMP/bin/dataops" <<'DATAOPS'
#!/usr/bin/env bash
set -euo pipefail
[[ -n "${MONITOR_DATAOPS_CAPTURE:-}" ]] || exit 92
[[ "$(command -v node)" == "$NODE_BIN" ]] || exit 94
case "$1 $2" in
  'tasks create')
    printf 'create\t%s\n' "$*" >> "$MONITOR_DATAOPS_CAPTURE"
    printf '%s\n' '{"id":"test-resource-gate-incident-0001"}'
    ;;
  'tasks note')
    printf 'note\t%s\n' "$*" >> "$MONITOR_DATAOPS_CAPTURE"
    ;;
  'tasks done')
    printf 'done\t%s\n' "$*" >> "$MONITOR_DATAOPS_CAPTURE"
    ;;
  *) exit 93 ;;
esac
DATAOPS

cat > "$TMP/bin/msmtp" <<'MSMTP'
#!/usr/bin/env bash
set -euo pipefail
[[ -n "${MONITOR_MAIL_CAPTURE:-}" ]] || exit 94
{
  printf 'ARGS\t%s\n' "$*"
  cat
  printf '\n---MESSAGE-END---\n'
} >> "$MONITOR_MAIL_CAPTURE"
MSMTP

chmod 755 "$TMP/bin/curl" "$TMP/bin/node" "$TMP/bin/dataops" "$TMP/bin/msmtp"
printf 'synthetic-monitor-token\n' > "$TMP/gate-token"
printf '{}\n' > "$TMP/remote-profile.json"
printf '// synthetic test file\n' > "$TMP/agent-browser.js"
: > "$TMP/mailbox"
: > "$TMP/dataops-capture"

export PATH="$TMP/bin:$PATH"
export NODE_BIN="$TMP/bin/node"
export DATAOPS_BIN="$TMP/bin/dataops"
export DATAOPS_CONFIG_DIR="$TMP/dataops-config"
export MONITOR_DATAOPS_CAPTURE="$TMP/dataops-capture"
export MONITOR_MAIL_CAPTURE="$TMP/mailbox"
export RESOURCE_GATE_STATE_DIR="$TMP/state"
export RESOURCE_GATE_TOKEN_FILE="$TMP/gate-token"
export AGENT_BROWSER_REMOTE_CONFIG="$TMP/remote-profile.json"
export AGENT_BROWSER_CLI="$TMP/agent-browser.js"
export HUB_HEALTH_URL='https://hub.dtcdev.click/health'
export ALERT_TO='resource-gate-test@example.invalid'
export ALERT_CC='orin@dtcdev.click'
export ALERT_OPS_TO='resource-gate-ops@example.invalid'
export RESOURCE_GATE_TEST_CC='resource-gate-oncall@example.invalid'
export RESOURCE_GATE_TEST_ALLOW_DATAOPS=1

grep -Fq 'PATH="$(dirname "$NODE_BIN"):/home/alexey/.local/bin:$PATH"' "$SCRIPT" \
  || fail "Node.js and DataOps runtime directories are not available in the monitor service PATH"
grep -Fq 'PATH="$RUNUSER_PATH" DATAOPS_CONFIG_DIR=/home/alexey/.config/dataops' \
  "$ROOT/scripts/install-resource-gate-monitor.sh" \
  || fail "installer does not expose Node.js to the DataOps secret preflight"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }
mail_count() { awk '/---MESSAGE-END---/ { n++ } END { print n+0 }' "$MONITOR_MAIL_CAPTURE"; }
cc_count() { awk '/^Cc: resource-gate-oncall@example\.invalid$/ { n++ } END { print n+0 }' "$MONITOR_MAIL_CAPTURE"; }
cc_envelope_count() { awk '/^ARGS/ && /resource-gate-ops@example\.invalid resource-gate-oncall@example\.invalid/ { n++ } END { print n+0 }' "$MONITOR_MAIL_CAPTURE"; }
dataops_count() { awk -F '\t' -v action="$1" '$1 == action { n++ } END { print n+0 }' "$MONITOR_DATAOPS_CAPTURE"; }
run_check() {
  local curl_mode="$1" remote_mode="$2" expected_rc="$3" actual_rc=0
  if CURL_MODE="$curl_mode" REMOTE_MODE="$remote_mode" bash "$SCRIPT" > "$TMP/output" 2>&1; then
    actual_rc=0
  else
    actual_rc=$?
  fi
  [[ "$actual_rc" -eq "$expected_rc" ]] \
    || fail "mode $curl_mode/$remote_mode returned $actual_rc, expected $expected_rc: $(cat "$TMP/output")"
}

run_check success grant-inactive 0
[[ "$(mail_count)" -eq 0 ]] || fail "healthy checks sent email"
[[ "$(dataops_count create)" -eq 0 ]] || fail "healthy checks created an incident task"
grep -Fq 'authenticated Resource Gate browser grant check for DESKTOP-FP4OP26: inactive' "$TMP/output" \
  || fail "inactive time-limited grant was not treated as a healthy read"
pass "gate, desktop grant-state, and Hub reads pass while no user grant is active"

run_check gate-fail grant-inactive 1
[[ "$(mail_count)" -eq 0 && "$(dataops_count create)" -eq 0 ]] \
  || fail "single failed check alerted before the two-check threshold"
pass "one failed check is held without creating noise"

run_check gate-fail grant-inactive 1
[[ "$(mail_count)" -eq 1 ]] || fail "second consecutive failure did not send one alert"
[[ "$(dataops_count create)" -eq 1 ]] || fail "second consecutive failure did not create one DataOps task"
grep -Fq 'Subject: [CRITICAL] Resource Gate and Hub availability is degraded' "$MONITOR_MAIL_CAPTURE" \
  || fail "critical alert subject was not present"
grep -Fq 'To: resource-gate-ops@example.invalid' "$MONITOR_MAIL_CAPTURE" \
  || fail "test path did not route mail to the ops-only recipient"
grep -Fq 'Cc: resource-gate-oncall@example.invalid' "$MONITOR_MAIL_CAPTURE" \
  || fail "test-safe CC recipient was not added to the alert"
grep -Fq 'resource-gate-ops@example.invalid resource-gate-oncall@example.invalid' "$MONITOR_MAIL_CAPTURE" \
  || fail "msmtp did not receive both test-safe mail recipients"
if grep -Fq 'resource-gate-test@example.invalid' "$MONITOR_MAIL_CAPTURE"; then
  fail "test-path guard allowed mail to the normal recipient"
fi
if grep -Fq 'orin@dtcdev.click' "$MONITOR_MAIL_CAPTURE"; then
  fail "test-path guard allowed mail to the production CC recipient"
fi
pass "two consecutive failures send one email and open one DataOps incident task"

for ((failure_number=3; failure_number<=32; failure_number++)); do
  run_check gate-fail grant-inactive 1
done
# Simulate an outage that has lasted past the old 15-minute repeat deadline.
# A future accidental ALERT_REPEAT_SECONDS branch must not pass this test just
# because these fixture invocations happen within a few seconds.
read -r _status _failures _last_alert incident_id task_id _alert _recovery_mail _recovery_note _recovery_ok \
  < "$RESOURCE_GATE_STATE_DIR/state"
printf 'fail %s 1 %s %s 1 0 0 %s\n' \
  "$_failures" "$incident_id" "$task_id" "${_recovery_ok:-0}" > "$RESOURCE_GATE_STATE_DIR/state"
run_check gate-fail grant-inactive 1
[[ "$(mail_count)" -eq 1 && "$(dataops_count create)" -eq 1 ]] \
  || fail "long outage repeated its critical email or duplicated its DataOps task"
pass "one outage through 33 failures and an expired repeat deadline sends one critical email and creates one task"

# Migrate the currently deployed eight-field state, then keep one healthy
# probe and a following failure inside the same incident.
read -r _status _failures last_alert incident_id task_id _alert _recovery_mail _recovery_note _recovery_ok \
  < "$RESOURCE_GATE_STATE_DIR/state"
printf 'fail %s %s %s %s 1 0 0\n' "$_failures" "$last_alert" "$incident_id" "$task_id" \
  > "$RESOURCE_GATE_STATE_DIR/state"
run_check success grant-inactive 0
[[ "$(mail_count)" -eq 1 && "$(dataops_count note)" -eq 0 ]] \
  || fail "one healthy probe prematurely closed the incident"
grep -Fq 'RECOVERY_PENDING healthy check 1/2' "$TMP/output" \
  || fail "the first healthy probe was not held by recovery hysteresis"
pass "a single healthy probe stays within the open incident"

run_check gate-fail grant-inactive 1
[[ "$(mail_count)" -eq 1 && "$(dataops_count create)" -eq 1 ]] \
  || fail "a flapping failure created a second critical alert or DataOps task"
pass "a failed probe resets recovery hysteresis without starting a second incident"

run_check success grant-inactive 0
[[ "$(mail_count)" -eq 1 && "$(dataops_count note)" -eq 0 ]] \
  || fail "one of two recovery probes sent a premature recovery"
run_check success grant-inactive 0
[[ "$(mail_count)" -eq 2 ]] || fail "two consecutive healthy probes did not send one recovery email"
[[ "$(dataops_count note)" -eq 1 ]] || fail "recovery was not appended to the incident task"
grep -Fq 'Subject: [RECOVERED] Resource Gate and Hub API are healthy' "$MONITOR_MAIL_CAPTURE" \
  || fail "recovery subject was not present"
run_check success grant-inactive 0
[[ "$(mail_count)" -eq 2 && "$(dataops_count note)" -eq 1 ]] \
  || fail "steady healthy state repeated the recovery"
pass "two healthy probes send one recovery, update the DataOps task, and then stay quiet"

printf 'ok 0 0 - - 0 0 0 0\n' > "$RESOURCE_GATE_STATE_DIR/state"
run_check hub-fail grant-inactive 1
run_check success grant-inactive 0
[[ "$(mail_count)" -eq 2 && "$(dataops_count create)" -eq 1 ]] \
  || fail "one-off Hub API failure was not cleared without alert"
pass "a one-check Hub API failure clears without an alert"

printf 'ok 0 0 - - 0 0 0 0\n' > "$RESOURCE_GATE_STATE_DIR/state"
run_check grant-fail grant-inactive 1
run_check grant-fail grant-inactive 1
[[ "$(mail_count)" -eq 3 && "$(dataops_count create)" -eq 2 ]] \
  || fail "authenticated grant API failure did not trigger the two-failure alert"
pass "authenticated grant API failures trigger the same two-check alert policy"

run_check success grant-inactive 0
[[ "$(mail_count)" -eq 3 && "$(dataops_count note)" -eq 1 ]] \
  || fail "a single healthy probe recovered the grant endpoint incident"
run_check success grant-inactive 0
[[ "$(mail_count)" -eq 4 && "$(dataops_count note)" -eq 2 ]] \
  || fail "two healthy probes did not recover the grant endpoint incident"

printf 'ok 0 0 - - 0 0 0 0\n' > "$RESOURCE_GATE_STATE_DIR/state"
run_check success desktop-offline 1
run_check success desktop-offline 1
[[ "$(mail_count)" -eq 5 && "$(dataops_count create)" -eq 3 ]] \
  || fail "offline desktop did not trigger the two-failure alert"
pass "the authenticated desktop health probe detects the selected desktop offline"

run_check success grant-active 0
[[ "$(mail_count)" -eq 5 && "$(dataops_count note)" -eq 2 ]] \
  || fail "one healthy probe recovered the offline desktop incident"
run_check success grant-active 0
[[ "$(mail_count)" -eq 6 && "$(dataops_count note)" -eq 3 ]] \
  || fail "recovery after desktop availability did not notify once"
run_check success grant-active 0
[[ "$(mail_count)" -eq 6 ]] || fail "healthy active grant sent an alert"
pass "an active time-limited grant also passes without changing it"

if PATH="$TMP/bin:$PATH" RESOURCE_GATE_STATE_DIR="$TMP/test-alert-state" bash "$SCRIPT" --test-alert > "$TMP/test-alert-output" 2>&1; then
  :
else
  fail "test alert mode failed: $(cat "$TMP/test-alert-output")"
fi
[[ "$(mail_count)" -eq 7 ]] || fail "test alert mode did not send email"
[[ "$(dataops_count create)" -eq 4 && "$(dataops_count done)" -eq 1 ]] \
  || fail "test alert mode did not create and close its DataOps test task"
grep -Fq 'Cc: resource-gate-oncall@example.invalid' "$MONITOR_MAIL_CAPTURE" \
  || fail "test alert did not include the configured test-safe CC recipient"
if grep -Fq 'orin@dtcdev.click' "$MONITOR_MAIL_CAPTURE"; then
  fail "test alert path sent to the production CC recipient"
fi
grep -Fq 'Subject: [TEST] Resource Gate and Hub uptime monitor' "$MONITOR_MAIL_CAPTURE" \
  || fail "test alert subject was not present"
[[ "$(cc_count)" -eq 7 && "$(cc_envelope_count)" -eq 7 ]] \
  || fail "outage, recovery, or test messages did not include the test-safe CC in both header and SMTP envelope"
pass "safe test-alert mode exercises email and DataOps delivery without changing monitor state"

if PATH="$TMP/bin:$PATH" RESOURCE_GATE_STATE_DIR="$TMP/invalid-test-alert-state" \
  RESOURCE_GATE_TEST_CC='orin@dtcdev.click' bash "$SCRIPT" --test-alert > "$TMP/invalid-test-cc-output" 2>&1; then
  fail "test-path guard accepted a production address as a test CC override"
fi
[[ "$(mail_count)" -eq 7 ]] || fail "invalid test CC override sent an email"
grep -Fq 'test CC override must use an .invalid address' "$TMP/invalid-test-cc-output" \
  || fail "invalid test CC override was not rejected with a clear error"

grep -Fq 'ALERT_TO="${ALERT_TO:-dev@dtcdev.click}"' "$SCRIPT" \
  || fail "production outage recipient default is not dev@dtcdev.click"
grep -Fq 'ALERT_CC="${ALERT_CC:-orin@dtcdev.click}"' "$SCRIPT" \
  || fail "production secondary alert recipient default is not orin@dtcdev.click"
pass "production outage and recovery recipient defaults to dev@dtcdev.click with orin@dtcdev.click copied"
printf 'Resource Gate and Hub monitor tests passed.\n'
