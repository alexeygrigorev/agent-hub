#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/resource-gate-monitor.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

cat > "$TMP/bin/curl" <<'CURL'
#!/usr/bin/env bash
[[ "$*" == *"https://gate.dtcdev.click/"* ]] || exit 90
case "${CURL_MODE:-fail}" in
  success) printf '%s\n' '<h1>Resource Gate</h1>' ;;
  hub2) printf '%s\n' '{"ok":true,"service":"hub2"}' ;;
  fail) exit 7 ;;
  *) exit 91 ;;
esac
CURL
cat > "$TMP/bin/msmtp" <<'MSMTP'
#!/usr/bin/env bash
[[ -n "${MONITOR_MAIL_CAPTURE:-}" ]] || exit 92
{
  printf 'ARGS\t%s\n' "$*"
  cat
  printf '\n---MESSAGE-END---\n'
} >> "$MONITOR_MAIL_CAPTURE"
MSMTP
chmod 755 "$TMP/bin/curl" "$TMP/bin/msmtp"
export PATH="$TMP/bin:$PATH"
export RESOURCE_GATE_STATE_DIR="$TMP/state"
export ALERT_TO='resource-gate-test@example.invalid'
export ALERT_OPS_TO='resource-gate-ops@example.invalid'
export MONITOR_MAIL_CAPTURE="$TMP/mailbox"
: > "$MONITOR_MAIL_CAPTURE"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }
mail_count() { awk '/---MESSAGE-END---/ { n++ } END { print n+0 }' "$MONITOR_MAIL_CAPTURE"; }
run_check() {
  local mode="$1" expected_rc="$2" actual_rc=0
  if CURL_MODE="$mode" bash "$SCRIPT" > "$TMP/output" 2>&1; then
    actual_rc=0
  else
    actual_rc=$?
  fi
  [[ "$actual_rc" -eq "$expected_rc" ]] \
    || fail "mode $mode returned $actual_rc, expected $expected_rc: $(cat "$TMP/output")"
}

run_check success 0
[[ "$(mail_count)" -eq 0 ]] || fail "initial healthy check sent email"
pass "healthy Resource Gate marker passes without mail"

run_check fail 1
[[ "$(mail_count)" -eq 1 ]] || fail "first outage did not send one email"
grep -Fq 'Subject: [CRITICAL] Resource Gate is unreachable' "$MONITOR_MAIL_CAPTURE" \
  || fail "outage subject was not critical"
grep -Fq 'To: resource-gate-ops@example.invalid' "$MONITOR_MAIL_CAPTURE" \
  || fail "test-path guard did not use the ops-only recipient"
if grep -Fq 'To: resource-gate-test@example.invalid' "$MONITOR_MAIL_CAPTURE"; then
  fail "test-path guard allowed mail to the normal recipient"
fi
pass "first outage pages and temporary state routes only to the ops test recipient"

run_check fail 1
[[ "$(mail_count)" -eq 1 ]] || fail "repeat email was not deduplicated"
pass "ongoing outage is deduplicated before the repeat interval"

printf 'fail 1\n' > "$RESOURCE_GATE_STATE_DIR/state"
run_check fail 1
[[ "$(mail_count)" -eq 2 ]] || fail "long outage did not repeat the alert"
pass "ongoing outage repeats after the configured interval"

run_check success 0
[[ "$(mail_count)" -eq 3 ]] || fail "recovery transition did not send one email"
grep -Fq 'Subject: [RECOVERED] Resource Gate is reachable' "$MONITOR_MAIL_CAPTURE" \
  || fail "recovery subject was not present"
run_check success 0
[[ "$(mail_count)" -eq 3 ]] || fail "steady healthy state sent another email"
pass "recovery is emailed once and steady health stays quiet"

run_check hub2 1
[[ "$(mail_count)" -eq 4 ]] || fail "Hub2 response did not trigger an outage page"
grep -Fq 'HTTPS response did not contain the Resource Gate page marker' "$TMP/output" \
  || fail "wrong-service response was not identified"
pass "Hub2 health body cannot satisfy the Resource Gate probe"

grep -Fq 'ALERT_TO="${ALERT_TO:-dev@dtcdev.click}"' "$SCRIPT" \
  || fail "production recipient default is not dev@dtcdev.click"
pass "production outage recipient defaults to dev@dtcdev.click"
printf 'Resource Gate monitor tests passed.\n'
