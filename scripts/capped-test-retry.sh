#!/usr/bin/env bash
# Capacity-hold retry for heavy-test steps (intakes 747d5bdf, 9432731e).
#
# Usage: scripts/capped-test-retry.sh [--attempts N] [--base-delay S] [--max-delay S] -- COMMAND...
#        scripts/capped-test-retry.sh [options] -- scripts/test-full-local.sh --priority final
#
# Runs COMMAND via scripts/capped-test-run.sh. The full runner
# (scripts/test-full-local.sh) is run directly instead: it takes its own slot
# per heavy phase, and an outer slot could deadlock it.
#
# Only a genuine capacity-gate refusal is a HOLD: exit 3 plus the refusal
# marker that the admission gate (or the full runner, for a pure hold) writes
# to AGENT_HUB_HEAVY_TEST_REFUSAL_FILE. A HOLD waits with exponential backoff
# and retries. A command's own exit 3 (e.g. pytest INTERNALERROR) leaves no
# marker: it is a FAIL, reported as exit 1 and never retried. Every other
# exit code passes through immediately with no retry.
#
# Defaults: attempts 5, base delay 30s, max delay 600s. Delays double per
# attempt (30, 60, 120, ...) capped at max-delay. CAPPED_TEST_RETRY_SLEEP
# names the sleep command (tests inject a fake clock).
#
# After the final refusal prints a HOLD diagnostic and exits 3. Callers must
# not abandon an uncommitted fix: append a Hub note and a followup event
# (see scripts/require-clean-exit.sh) and keep the worktree.
set -uo pipefail

USAGE="Usage: scripts/capped-test-retry.sh [--attempts N] [--base-delay S] [--max-delay S] -- COMMAND..."
ATTEMPTS=5
BASE_DELAY=30
MAX_DELAY=600

while [ "$#" -gt 0 ]; do
  case "$1" in
    --attempts|--base-delay|--max-delay)
      if [ "$#" -lt 2 ]; then
        echo "capped-test-retry: $1 needs a value" >&2
        echo "$USAGE" >&2
        exit 2
      fi
      case "$1" in
        --attempts) ATTEMPTS="$2" ;;
        --base-delay) BASE_DELAY="$2" ;;
        --max-delay) MAX_DELAY="$2" ;;
      esac
      shift 2 ;;
    -h|--help) echo "$USAGE"; exit 0 ;;
    --) shift; break ;;
    --*) echo "capped-test-retry: unknown option $1" >&2; echo "$USAGE" >&2; exit 2 ;;
    *) break ;;
  esac
done

valid_int() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
if ! valid_int "$ATTEMPTS" || [ "$ATTEMPTS" -lt 1 ] || [ "$ATTEMPTS" -gt 100 ]; then
  echo "capped-test-retry: invalid --attempts $ATTEMPTS" >&2; exit 2
fi
if ! valid_int "$BASE_DELAY" || [ "$BASE_DELAY" -gt 3600 ]; then
  echo "capped-test-retry: invalid --base-delay $BASE_DELAY" >&2; exit 2
fi
if ! valid_int "$MAX_DELAY" || [ "$MAX_DELAY" -lt 1 ] || [ "$MAX_DELAY" -gt 3600 ]; then
  echo "capped-test-retry: invalid --max-delay $MAX_DELAY" >&2; exit 2
fi
if [ "$#" -eq 0 ]; then
  echo "$USAGE" >&2
  exit 2
fi

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/heavy-phase-status.sh
. "${SCRIPTS_DIR}/heavy-phase-status.sh"
SLEEP="${CAPPED_TEST_RETRY_SLEEP:-sleep}"

if [ "$(basename -- "$1")" = "test-full-local.sh" ]; then
  RUN=("$@")
else
  RUN=("${SCRIPTS_DIR}/capped-test-run.sh" -- "$@")
fi

TMP_PARENT="${TMPDIR:-${SCRIPTS_DIR}/../.local/tmp}"
mkdir -p "${TMP_PARENT}" || exit 1
MARKER_DIR="$(mktemp -d -p "${TMP_PARENT}" retry-XXXXXX)" || exit 1
trap 'rm -rf -- "${MARKER_DIR}"' EXIT
MARKER="${MARKER_DIR}/refused"

attempt=1
while :; do
  rm -f -- "${MARKER}"
  AGENT_HUB_HEAVY_TEST_REFUSAL_FILE="${MARKER}" "${RUN[@]}"
  rc=$?
  if [ "$rc" -eq 3 ] && ! is_capacity_hold "$rc" "${MARKER}"; then
    echo "capped-test-retry: FAIL: command exited 3 on its own (no capacity-gate refusal marker); not a HOLD, not retried" >&2
    exit 1
  fi
  if [ "$rc" -ne 3 ]; then
    exit "$rc"
  fi
  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    echo "capped-test-retry: HOLD: capacity gate refused after ${ATTEMPTS} attempt(s) (exit 3); do not abandon the fix — wait for load to clear, retry, and if you must stop, append a Hub note plus a followup event and keep the worktree (see scripts/require-clean-exit.sh)" >&2
    exit 3
  fi
  delay="$BASE_DELAY"
  i=1
  while [ "$i" -lt "$attempt" ] && [ "$delay" -lt "$MAX_DELAY" ]; do
    delay=$((delay * 2))
    i=$((i + 1))
  done
  if [ "$delay" -gt "$MAX_DELAY" ]; then delay="$MAX_DELAY"; fi
  echo "capped-test-retry: capacity hold ($(cat -- "${MARKER}" 2>/dev/null)), attempt ${attempt}/${ATTEMPTS}; retrying in ${delay}s with backoff instead of exiting" >&2
  if [ "$delay" -gt 0 ]; then "$SLEEP" "$delay"; fi
  attempt=$((attempt + 1))
done
