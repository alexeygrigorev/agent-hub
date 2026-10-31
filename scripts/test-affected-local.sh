#!/usr/bin/env bash
set -uo pipefail

# Targeted pre-push verification (intake cb5c362e, Alexey 2026-10-31).
# Runs typecheck, scope guard and only the Vitest/pytest files affected by the
# change vs origin/main, inside ONE short heavy-test slot. The FULL suite runs
# once after the push in the deploy CI job; do not run test-full-local.sh before
# every push. Changes to shared test infrastructure (dependencies, runner config,
# conftest, the admission gate) are "broad" and still need test-full-local.sh.
# Usage: scripts/test-affected-local.sh [--base REF] [--dry-run]

if [ "${AGENT_HUB_HEAVY_TEST_HELD:-}" = "1" ]; then
  echo "test-affected-local: do not wrap in capped-test-run.sh; it takes its own slot" >&2
  exit 2
fi
BASE="origin/main"; DRY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "Usage: scripts/test-affected-local.sh [--base REF] [--dry-run]" >&2; exit 2 ;;
  esac
done
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1
unset APLEXER_TAG APLEXER_WORKSPACE HUB_WORKER_CONFIG_FILE HUB_CONFIG_FILE

PLAN="$(python3 scripts/ci/affected_tests.py "${BASE}")" || exit 1
field() { printf '%s' "${PLAN}" | python3 -c "import json,sys;d=json.load(sys.stdin);v=d['$1'];print(' '.join(v) if isinstance(v,list) else int(v))"; }
BROAD="$(field broad)"; VITEST_FILES="$(field vitest)"; PYTEST_FILES="$(field pytest)"
echo "=== Affected tests vs ${BASE} ==="
echo "vitest: ${VITEST_FILES:-none}"; echo "pytest: ${PYTEST_FILES:-none}"
if [ -n "${BROAD}" ]; then
  echo "BROAD: shared test infrastructure changed (${BROAD}); run scripts/test-full-local.sh --priority final" >&2
  exit 4
fi
[ "${DRY}" = 1 ] && exit 0

python3 scripts/local-dependencies.py || exit 1
export UV_CACHE_DIR="${UV_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/uv}"
TMP_PARENT="${TMPDIR:-.local/tmp}"; mkdir -p "${TMP_PARENT}" || exit 1
RUN_TMP="$(mktemp -d -p "${TMP_PARENT}" a-XXXXXX)" || exit 1
trap 'rm -rf -- "${RUN_TMP}"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
RUN_TMP_ABS="$(cd "${RUN_TMP}" && pwd)"; mkdir "${RUN_TMP_ABS}/home" || exit 1
export TMPDIR="${RUN_TMP_ABS}" AGENT_HUB_HEAVY_TEST_DIR="${AGENT_HUB_HEAVY_TEST_DIR:-/run/user/$(id -u)/agent-hub/heavy-test}"

FAILURES=0
phase() { echo "=== $1 ==="; }
phase "scope guard"
python3 scripts/ci/scope_guard.py --working || { echo "FAIL: scope guard"; FAILURES=$((FAILURES + 1)); }
if [ "$(field workflows)" = 1 ]; then
  phase "workflow validation"
  python3 scripts/ci/validate-workflow.py && python3 scripts/ci/test_ci.py || { echo "FAIL: workflows"; FAILURES=$((FAILURES + 1)); }
fi

# Everything heavy shares one slot, taken for this block only.
export AFFECTED_TYPECHECK="$(field typecheck)" AFFECTED_BUILD="$(field build)"
export AFFECTED_VITEST="${VITEST_FILES}" AFFECTED_PYTEST="${PYTEST_FILES}" AFFECTED_HOME="${RUN_TMP_ABS}/home"
export AFFECTED_UV="$(command -v uv)" AFFECTED_USERBASE="${PYTHONUSERBASE:-$(python3 -c 'import site; print(site.getuserbase())' 2>/dev/null || echo "${HOME}/.local")}"
export AFFECTED_AUTH="${DTC_AUTH_HOME:-/home/alexey/git/auth}"
PY_ENV=(env)
while IFS= read -r name; do
  case "$name" in
    AGENT_HUB_HEAVY_TEST_HELD|AFFECTED_*) ;;
    APLEXER_*|HUB_*|AGENT_HUB_*|XDG_CONFIG_HOME|XDG_DATA_HOME|XDG_STATE_HOME|XDG_RUNTIME_DIR|GH_CONFIG_DIR) PY_ENV+=(-u "$name") ;;
  esac
done < <(compgen -e)
export AFFECTED_PYENV="${PY_ENV[*]}"
MARKER="${RUN_TMP_ABS}/refused"
AGENT_HUB_HEAVY_TEST_REFUSAL_FILE="${MARKER}" "${REPO_ROOT}/scripts/capped-test-run.sh" -- bash -c '
rc=0
if [ "$AFFECTED_TYPECHECK" = 1 ]; then echo "=== typecheck ==="; npm run typecheck || { echo "FAIL: typecheck"; rc=1; }; fi
if [ -n "$AFFECTED_VITEST" ]; then
  echo "=== vitest (affected) ==="
  UV_THREADPOOL_SIZE=1 GOMAXPROCS=1 NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--v8-pool-size=1" \
    npx --no-install vitest run --configLoader runner --no-file-parallelism --maxWorkers 1 --disableConsoleIntercept $AFFECTED_VITEST || { echo "FAIL: vitest"; rc=1; }
fi
if [ -n "$AFFECTED_PYTEST" ]; then
  echo "=== pytest (affected, disposable HOME) ==="
  uv sync --frozen >/dev/null || rc=1
  $AFFECTED_PYENV HOME="$AFFECTED_HOME" PYTHONUSERBASE="$AFFECTED_USERBASE" DTC_AUTH_HOME="$AFFECTED_AUTH" \
    PYTHONPATH="host:host/tests" CODEX_LAUNCH_WRAPPER="" \
    "$AFFECTED_UV" run --frozen --with pytest python -m pytest -q --basetemp "$TMPDIR/pytest" \
    -o tmp_path_retention_count=0 -o tmp_path_retention_policy=none $AFFECTED_PYTEST || { echo "FAIL: pytest"; rc=1; }
fi
if [ "$AFFECTED_BUILD" = 1 ]; then echo "=== worker dry-run build ==="; npm run build >/dev/null || { echo "FAIL: build"; rc=1; }; fi
exit $rc'
RC=$?
if [ "${RC}" -eq 3 ] && [ -f "${MARKER}" ]; then
  echo "HOLD: $(cat "${MARKER}"); retry later"; exit 3
fi
[ "${RC}" -ne 0 ] && FAILURES=$((FAILURES + 1))
if [ "${FAILURES}" -eq 0 ]; then echo "PASS: affected tests passed (full suite runs post-push in CI)"; exit 0; fi
echo "FAIL: ${FAILURES} affected-test phase(s) failed"; exit 1
