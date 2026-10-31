#!/usr/bin/env bash
set -uo pipefail

# Full local test suite verification for Agent Hub (Alexey 2026-10-17 22:54)
# Executes all build, validation, Vitest discovery, and Python discovery suites.

# The full runner acquires a slot for each heavy phase. An outer wrapper would
# retain a slot while these phases wait for another, potentially deadlocking.
# scripts/capped-test-retry.sh runs this runner directly (no outer slot) and
# retries only when it reports a capacity hold through the refusal marker.
if [ "${AGENT_HUB_HEAVY_TEST_HELD:-}" = "1" ]; then
  echo "test-full-local: do not wrap this runner in capped-test-run.sh; run scripts/test-full-local.sh --priority final directly, or scripts/capped-test-retry.sh -- scripts/test-full-local.sh --priority final to retry capacity holds" >&2
  exit 2
fi
# The caller's refusal marker reports a whole-run capacity hold; each heavy
# phase below gets its own marker so a command's own exit 3 stays a FAIL.
OUTER_REFUSAL_FILE="${AGENT_HUB_HEAVY_TEST_REFUSAL_FILE:-}"
unset AGENT_HUB_HEAVY_TEST_REFUSAL_FILE
if [ "$#" -gt 0 ]; then
  if [ "$#" -ne 2 ] || [ "$1" != "--priority" ] || { [ "$2" != "normal" ] && [ "$2" != "final" ]; }; then
    echo "Usage: scripts/test-full-local.sh [--priority normal|final]" >&2
    exit 2
  fi
  export AGENT_HUB_HEAVY_TEST_PRIORITY="$2"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

# Aplexer session identity leaks into config resolution: worker_auth and the
# hub CLI resolve config paths from APLEXER_TAG/APLEXER_WORKSPACE even under a
# disposable HOME, so fixture tests running unpatched would overwrite that
# session's LIVE worker credential (observed 2026-10-28: live
# hub-worker-auth/<session>.json files clobbered with pytest fixture data).
# The suite must never touch a real profile.
unset APLEXER_TAG APLEXER_WORKSPACE HUB_WORKER_CONFIG_FILE HUB_CONFIG_FILE

python3 scripts/local-dependencies.py || exit 1

# Capture the shared cache before the Python suite switches to a disposable HOME.
export UV_CACHE_DIR="${UV_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/uv}"
TMP_PARENT="${TMPDIR:-.local/tmp}"
mkdir -p "${TMP_PARENT}" || exit 1
# Python socket fixtures need short paths, even in a long checkout path.
RUN_TMP="$(mktemp -d -p "${TMP_PARENT}" f-XXXXXX)" || exit 1
cleanup() {
  # Retention applies only to this run's newly created temporary files.
  rm -rf -- "${RUN_TMP}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="${RUN_TMP}"
RUN_TMP_ABSOLUTE="$(cd "${RUN_TMP}" && pwd)" || exit 1
DISPOSABLE_HOME="${RUN_TMP_ABSOLUTE}/home"
mkdir "${DISPOSABLE_HOME}" || exit 1

export AGENT_HUB_HEAVY_TEST_DIR="${AGENT_HUB_HEAVY_TEST_DIR:-/run/user/$(id -u)/agent-hub/heavy-test}"

FAILURES=0
HOLDS=0
# shellcheck source=scripts/heavy-phase-status.sh
. "${REPO_ROOT}/scripts/heavy-phase-status.sh"

echo "=== Scope boundary (all new commits and working changes) ==="
if ! python3 scripts/ci/scope_guard.py --working || ! python3 -m unittest discover -s scripts/ci -p "test_scope_guard.py" || ! python3 scripts/test_local_dependencies.py || ! python3 scripts/test_full_local_storage.py || ! python3 -m unittest discover -s scripts/ci -p "test_affected_tests.py"; then
  echo "FAIL: Scope boundary checks failed"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 1/8: TypeScript Typecheck ==="
if ! npm run typecheck; then
  echo "FAIL: Typecheck failed"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 2/8: Validate CI/CD Workflows ==="
if ! python3 scripts/ci/validate-workflow.py || ! python3 scripts/ci/test_ci.py; then
  echo "FAIL: CI/CD workflow validation failed"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 3/8: Hub CLI zipapp build & Worker dry-run build ==="
if ! python3 scripts/build-hub-cli.py --out-dir public/cli || ! npm run build; then
  echo "FAIL: Build / dry-run verification failed"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 4/8: Vitest Discovery Suite (all test files, bounded concurrency) ==="
VITEST_RC=0
# Serial files still have Node/V8 and esbuild background pools. Bound those
# pools inside the existing CPU cap rather than competing for its quota.
VITEST_NODE_OPTIONS="${NODE_OPTIONS:-}"
case "${VITEST_NODE_OPTIONS}" in
  *--v8-pool-size*) ;;
  *) VITEST_NODE_OPTIONS="${VITEST_NODE_OPTIONS:+${VITEST_NODE_OPTIONS} }--v8-pool-size=1" ;;
esac
VITEST_MARKER="${RUN_TMP_ABSOLUTE}/refused-vitest"
UV_THREADPOOL_SIZE="${UV_THREADPOOL_SIZE:-1}" GOMAXPROCS="${GOMAXPROCS:-1}" \
NODE_OPTIONS="${VITEST_NODE_OPTIONS}" AGENT_HUB_HEAVY_TEST_REFUSAL_FILE="${VITEST_MARKER}" \
"${REPO_ROOT}/scripts/capped-test-run.sh" -- npx --no-install vitest run --configLoader runner --no-file-parallelism --maxWorkers 1 --disableConsoleIntercept || VITEST_RC=$?
hold_or_fail "${VITEST_RC}" "Vitest discovery suite" "${VITEST_MARKER}"

echo "=== 5/8: Python Discovery Suite (disposable HOME, CI-matched env) ==="
DTC_AUTH_PATH="${DTC_AUTH_HOME:-/home/alexey/git/auth}"
# Resolve the published native client from uv.lock; pytest is a test-only overlay.
uv sync --frozen || exit 1
UV_BIN="$(command -v uv)"
ORIGINAL_USER_BASE="${PYTHONUSERBASE:-}"
if [ -z "${ORIGINAL_USER_BASE}" ]; then
  # `python -m site --user-base` prints the path but exits 1 when user-site
  # imports are disabled; appending a fallback would corrupt the path.
  ORIGINAL_USER_BASE="$(python3 -c 'import site; print(site.getuserbase())' 2>/dev/null)" || ORIGINAL_USER_BASE="/home/alexey/.local"
fi

PYTEST_RC=0
PYTEST_MARKER="${RUN_TMP_ABSOLUTE}/refused-pytest"
# Native worker-profile discovery bypasses HOME. Test children must not inherit
# the real session identity, credential selectors, or managed XDG config roots.
PYTHON_ENV=(env)
while IFS= read -r name; do
  case "$name" in
    AGENT_HUB_HEAVY_TEST_HELD) ;;  # keep: nested runners must not take a second slot
    APLEXER_*|HUB_*|AGENT_HUB_*|XDG_CONFIG_HOME|XDG_DATA_HOME|XDG_STATE_HOME|XDG_RUNTIME_DIR|GH_CONFIG_DIR)
      PYTHON_ENV+=(-u "$name") ;;
  esac
done < <(compgen -e)
HOME="${DISPOSABLE_HOME}" \
PYTHONUSERBASE="${ORIGINAL_USER_BASE}" \
DTC_AUTH_HOME="${DTC_AUTH_PATH}" \
PYTHONPATH="host:host/tests" \
CODEX_LAUNCH_WRAPPER="" \
AGENT_HUB_HEAVY_TEST_REFUSAL_FILE="${PYTEST_MARKER}" \
"${REPO_ROOT}/scripts/capped-test-run.sh" -- "${PYTHON_ENV[@]}" "${UV_BIN}" run --frozen --with pytest python -m pytest -q \
  --basetemp "${RUN_TMP}/pytest" \
  -o tmp_path_retention_count=0 -o tmp_path_retention_policy=none || PYTEST_RC=$?
hold_or_fail "${PYTEST_RC}" "Python discovery suite" "${PYTEST_MARKER}"

echo "=== 6/8: Host canary hermeticity (full suite vs canary HOME, pytest + unittest) ==="
# Intake 6b624e65: the suite must never touch the real user Hub profiles.
# Runs the full host suite twice with HOME pointing at a canary home holding
# human cli.json + host.json and asserts both stay byte-identical.
CANARY_RC=0
CANARY_MARKER="${RUN_TMP_ABSOLUTE}/refused-canary"
AGENT_HUB_HEAVY_TEST_REFUSAL_FILE="${CANARY_MARKER}" \
"${REPO_ROOT}/scripts/capped-test-run.sh" -- python3 scripts/ci/host_canary_check.py || CANARY_RC=$?
hold_or_fail "${CANARY_RC}" "Host canary hermeticity check" "${CANARY_MARKER}" "canary mutated or suite failed"

echo "=== 7/8: All standalone browser and static checks ==="
# Chromium resolves shared-memory files from a different working directory.
# Supply an absolute path only here; keep socket-sensitive Python paths short.
export TMPDIR="${RUN_TMP_ABSOLUTE}"
PHONE_RC=0
PHONE_MARKER="${RUN_TMP_ABSOLUTE}/refused-phone"
if [ -n "${SKIP_PHONE_UI:-}" ]; then
  echo "SKIP: SKIP_PHONE_UI is set; phone browser UI not executed."
else
  AGENT_HUB_HEAVY_TEST_REFUSAL_FILE="${PHONE_MARKER}" \
  "${REPO_ROOT}/scripts/capped-test-run.sh" -- bash -c 'node --test scripts/test-browser-local.test.mjs && node scripts/test-browser-local.mjs' || PHONE_RC=$?
  hold_or_fail "${PHONE_RC}" "Standalone browser and static checks" "${PHONE_MARKER}" "needs Playwright Chromium; see docs/TEST-COVERAGE.md"
fi

echo "=== 8/8: Summary ==="
SUMMARY_RC=0
phase_summary || SUMMARY_RC=$?
if [ "${SUMMARY_RC}" -eq 3 ] && [ -n "${OUTER_REFUSAL_FILE}" ]; then
  echo "capped-test-run refused: ${HOLDS} full-runner phase(s) held by the capacity gate" > "${OUTER_REFUSAL_FILE}"
fi
exit "${SUMMARY_RC}"
