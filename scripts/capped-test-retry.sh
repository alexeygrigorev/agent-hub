#!/usr/bin/env bash
# Capacity-hold retry for heavy-test steps (intake 747d5bdf AC1).
#
# Usage: scripts/capped-test-retry.sh [--attempts N] [--base-delay S] [--max-delay S] -- COMMAND...
#
# Runs COMMAND via scripts/capped-test-run.sh. Exit 3 means the host
# capacity gate refused (no heavy-test slot within the wait budget, or
# admission refused). That is a HOLD, not a code failure: wait with
# exponential backoff and retry instead of exiting the session.
# Any other exit code passes through immediately with no retry.
#
# Defaults: attempts 5, base delay 30s, max delay 600s. Delays double per
# attempt (30, 60, 120, ...) capped at max-delay. Set base-delay 0 for
# tests that only need retry counting without sleeping.
#
# On final refusal prints a HOLD diagnostic and exits 3. Callers must not
# abandon an uncommitted fix: append a Hub note and a followup event
# (see scripts/require-clean-exit.sh) and keep the worktree.
set -uo pipefail

ATTEMPTS=5
BASE_DELAY=30
MAX_DELAY=600

while [ "$#" -gt 0 ]; do
  case "$1" in
    --attempts) ATTEMPTS="$2"; shift 2 ;;
    --base-delay) BASE_DELAY="$2"; shift 2 ;;
    --max-delay) MAX_DELAY="$2"; shift 2 ;;
    --) shift; break ;;
    --*) echo "capped-test-retry: unknown option $1" >&2; exit 2 ;;
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
  echo "Usage: scripts/capped-test-retry.sh [--attempts N] [--base-delay S] [--max-delay S] -- COMMAND..." >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="${REPO_ROOT}/capped-test-run.sh"

attempt=1
while :; do
  "$WRAPPER" -- "$@"
  rc=$?
  if [ "$rc" -ne 3 ]; then
    exit "$rc"
  fi
  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    echo "capped-test-retry: HOLD: capacity gate refused after ${ATTEMPTS} attempt(s) (exit 3); do not abandon the fix — wait for load to clear, retry, and if you must stop, append a Hub note plus a followup event and keep the worktree (see scripts/require-clean-exit.sh)" >&2
    exit 3
  fi
  delay="$BASE_DELAY"
  shift_count=$((attempt - 1))
  i=0
  while [ "$i" -lt "$shift_count" ] && [ "$delay" -lt "$MAX_DELAY" ]; do
    delay=$((delay * 2))
    if [ "$delay" -gt "$MAX_DELAY" ]; then delay="$MAX_DELAY"; fi
    i=$((i + 1))
  done
  echo "capped-test-retry: capacity hold (exit 3), attempt ${attempt}/${ATTEMPTS}; retrying in ${delay}s with backoff instead of exiting" >&2
  if [ "$delay" -gt 0 ]; then sleep "$delay"; fi
  attempt=$((attempt + 1))
done
