# shellcheck shell=bash
# Sourced by scripts/test-full-local.sh and scripts/capped-test-retry.sh.
# Capacity HOLD versus FAIL for capped heavy phases (intakes 747d5bdf, 9432731e).
#
# Exit 3 alone is ambiguous: capped-test-run.sh refuses with 3, but the
# admitted command may also exit 3 (pytest INTERNALERROR). A refusal is a
# HOLD only when the admission gate wrote its marker file
# (AGENT_HUB_HEAVY_TEST_REFUSAL_FILE, see scripts/heavy-test-admission.py).
# A command's own exit 3 leaves no marker and is a FAIL, never retried.

FAILURES="${FAILURES:-0}"
HOLDS="${HOLDS:-0}"

# is_capacity_hold RC MARKER: true only for exit 3 plus the gate's marker.
is_capacity_hold() {
  [ "$1" -eq 3 ] && [ -n "$2" ] && [ -f "$2" ]
}

# hold_or_fail RC LABEL MARKER [FAIL_HINT]: report one phase and count it.
hold_or_fail() {
  local rc="$1" label="$2" marker="$3" hint="${4:-}"
  if [ "$rc" -eq 0 ]; then
    echo "PASS: ${label} passed"
  elif is_capacity_hold "$rc" "$marker"; then
    echo "HOLD: ${label} refused by the host capacity gate ($(cat -- "$marker" 2>/dev/null)); retry with scripts/capped-test-retry.sh -- scripts/test-full-local.sh --priority final"
    FAILURES=$((FAILURES + 1))
    HOLDS=$((HOLDS + 1))
  else
    echo "FAIL: ${label} exited with code ${rc}${hint:+ (${hint})}"
    FAILURES=$((FAILURES + 1))
  fi
}

# phase_summary: 0 all passed, 3 every failure was a capacity hold, else 1.
phase_summary() {
  if [ "${FAILURES}" -eq 0 ]; then
    echo "ALL LOCAL VERIFICATION SUITES PASSED."
    return 0
  fi
  if [ "${HOLDS}" -eq "${FAILURES}" ]; then
    echo "HOLD: ${HOLDS} suite(s) refused by the host capacity gate. Retry with backoff: scripts/capped-test-retry.sh -- scripts/test-full-local.sh --priority final; do not abandon an uncommitted fix (see scripts/require-clean-exit.sh)."
    return 3
  fi
  local held=""
  if [ "${HOLDS}" -gt 0 ]; then held=" (${HOLDS} capacity hold(s) among them)"; fi
  echo "FAILED: ${FAILURES} suite(s) reported failures${held}. Check logs above."
  return 1
}
