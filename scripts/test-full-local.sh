#!/usr/bin/env bash
set -uo pipefail

# Full local test suite verification for Agent Hub (Alexey 2026-10-17 22:54)
# Executes all build, validation, Vitest discovery, and Python discovery suites.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

python3 scripts/local-dependencies.py || exit 1

# Capture the shared cache before the Python suite switches to a disposable HOME.
export UV_CACHE_DIR="${UV_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/uv}"
TMP_PARENT="${TMPDIR:-.local/tmp}"
mkdir -p "${TMP_PARENT}" || exit 1
# Keep the default relative: long absolute paths can exceed Unix socket limits.
RUN_TMP="$(mktemp -d -p "${TMP_PARENT}" f-XXXXXX)" || exit 1
cleanup() {
  # Retention applies only to this run's newly created temporary files.
  rm -rf -- "${RUN_TMP}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="${RUN_TMP}"
DISPOSABLE_HOME="$(cd "${RUN_TMP}" && pwd)/home"
mkdir "${DISPOSABLE_HOME}" || exit 1

FAILURES=0

echo "=== Scope boundary (all new commits and working changes) ==="
if ! python3 scripts/ci/scope_guard.py --working || ! python3 -m unittest discover -s scripts/ci -p "test_scope_guard.py" || ! python3 scripts/test_local_dependencies.py || ! python3 scripts/test_full_local_storage.py; then
  echo "FAIL: Scope boundary checks failed"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 1/6: TypeScript Typecheck ==="
if ! npm run typecheck; then
  echo "FAIL: Typecheck failed"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 2/6: Validate CI/CD Workflows ==="
if ! python3 scripts/ci/validate-workflow.py || ! python3 scripts/ci/test_ci.py; then
  echo "FAIL: CI/CD workflow validation failed"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 3/6: Hub CLI zipapp build & Worker dry-run build ==="
if ! python3 scripts/build-hub-cli.py --out-dir public/cli || ! npm run build; then
  echo "FAIL: Build / dry-run verification failed"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 4/6: Vitest Discovery Suite (all test files, bounded concurrency) ==="
VITEST_RC=0
npx --no-install vitest run --configLoader runner --no-file-parallelism --maxWorkers 1 || VITEST_RC=$?
if [ "${VITEST_RC}" -ne 0 ]; then
  echo "FAIL: Vitest discovery suite exited with code ${VITEST_RC}"
  FAILURES=$((FAILURES + 1))
else
  echo "PASS: Vitest discovery suite passed"
fi

echo "=== 5/6: Python Discovery Suite (disposable HOME, CI-matched env) ==="
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
HOME="${DISPOSABLE_HOME}" \
PYTHONUSERBASE="${ORIGINAL_USER_BASE}" \
DTC_AUTH_HOME="${DTC_AUTH_PATH}" \
PYTHONPATH="host:host/tests" \
CODEX_LAUNCH_WRAPPER="" \
APLEXER_TAG="" \
APLEXER_WORKSPACE="" \
APLEXER_SESSION_ID="" \
"${UV_BIN}" run --frozen --with pytest python -m pytest -q \
  --basetemp "${RUN_TMP}/pytest" \
  -o tmp_path_retention_count=0 -o tmp_path_retention_policy=none || PYTEST_RC=$?

if [ "${PYTEST_RC}" -ne 0 ]; then
  echo "FAIL: Python discovery suite exited with code ${PYTEST_RC}"
  FAILURES=$((FAILURES + 1))
else
  echo "PASS: Python discovery suite passed"
fi

echo "=== 6/7: Phone-width browser UI (390x844, light+dark) ==="
if [ -n "${SKIP_PHONE_UI:-}" ]; then
  echo "SKIP: SKIP_PHONE_UI is set; phone browser UI not executed."
elif node test/phone-ui.browser.mjs --out .local/phone-ui && node test/host-health-charts.browser.mjs && node test/session-stall.browser.mjs && node test/usage-history.browser.mjs && node test/tbm-dashboard.browser.mjs; then
  echo "PASS: Phone-width and host-history chart browser UI passed"
else
  PHONE_RC=$?
  echo "FAIL: Phone-width browser UI exited with code ${PHONE_RC} (needs Playwright Chromium; see test/phone-ui.browser.mjs)"
  FAILURES=$((FAILURES + 1))
fi

echo "=== 7/7: Summary ==="
if [ "${FAILURES}" -gt 0 ]; then
  echo "FAILED: ${FAILURES} suite(s) reported failures. Check logs above."
  exit 1
else
  echo "ALL LOCAL VERIFICATION SUITES PASSED."
  exit 0
fi
