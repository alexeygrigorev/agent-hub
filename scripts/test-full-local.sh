#!/usr/bin/env bash
set -uo pipefail

# Full local test suite verification for Agent Hub (Alexey 2026-10-17 22:54)
# Executes all build, validation, Vitest discovery, and Python discovery suites.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

mkdir -p .local/tmp || exit 1
export TMPDIR="${REPO_ROOT}/.local/tmp"

# Disposable HOME to ensure hermeticity against personal dotfiles / checkouts
DISPOSABLE_HOME="$(mktemp -d -p "${TMPDIR}" home-hermetic-XXXXXX)" || exit 1
cleanup() {
  rm -rf "${DISPOSABLE_HOME}"
}
trap cleanup EXIT

FAILURES=0

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
npx vitest run --no-file-parallelism --maxWorkers 1 || VITEST_RC=$?
if [ "${VITEST_RC}" -ne 0 ]; then
  echo "FAIL: Vitest discovery suite exited with code ${VITEST_RC}"
  FAILURES=$((FAILURES + 1))
else
  echo "PASS: Vitest discovery suite passed"
fi

echo "=== 5/6: Python Discovery Suite (disposable HOME, CI-matched env) ==="
DTC_AUTH_PATH="${DTC_AUTH_HOME:-/home/alexey/git/auth}"
PYTEST_BIN="${PYTEST_BIN:-/home/alexey/.local/bin/pytest}"
ORIGINAL_USER_BASE="${PYTHONUSERBASE:-$(python3 -m site --user-base 2>/dev/null || echo "/home/alexey/.local")}"

PYTEST_RC=0
HOME="${DISPOSABLE_HOME}" \
PYTHONUSERBASE="${ORIGINAL_USER_BASE}" \
DTC_AUTH_HOME="${DTC_AUTH_PATH}" \
PYTHONPATH="host:host/tests" \
"${PYTEST_BIN}" -q || PYTEST_RC=$?

if [ "${PYTEST_RC}" -ne 0 ]; then
  echo "FAIL: Python discovery suite exited with code ${PYTEST_RC}"
  FAILURES=$((FAILURES + 1))
else
  echo "PASS: Python discovery suite passed"
fi

echo "=== 6/6: Summary ==="
if [ "${FAILURES}" -gt 0 ]; then
  echo "FAILED: ${FAILURES} suite(s) reported failures. Check logs above."
  exit 1
else
  echo "ALL LOCAL VERIFICATION SUITES PASSED."
  exit 0
fi
