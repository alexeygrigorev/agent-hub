#!/usr/bin/env bash
# Host-wide heavy-suite cap. Always wrap ad-hoc vitest, Playwright and full pytest:
#   scripts/capped-test-run.sh -- npx vitest run
# The full local runner uses this automatically. All checkouts of the same user
# share locks, including children using a disposable HOME. Linux flock releases
# a slot on exit/crash; no PID files, stale-slot deletion, or signals to sessions.
# Vitest and pytest acquire a slot on their own when not run under this wrapper
# (test/heavy-slot.global-setup.mjs, host/tests/conftest.py).
# Env defaults: AGENT_HUB_HEAVY_TEST_SLOTS=2,
# AGENT_HUB_HEAVY_TEST_WAIT_S=1800, AGENT_HUB_HEAVY_TEST_DIR (shared lock root).
set -uo pipefail
SLOTS="${AGENT_HUB_HEAVY_TEST_SLOTS:-2}"
WAIT_S="${AGENT_HUB_HEAVY_TEST_WAIT_S:-1800}"
SEMAPHORE_ROOT="${AGENT_HUB_HEAVY_TEST_DIR:-/run/user/$(id -u)/agent-hub/heavy-test}"
while [ $# -gt 0 ]; do
  case "$1" in
    --slots) SLOTS="${2:?--slots needs a value}"; shift 2 ;;
    --wait) WAIT_S="${2:?--wait needs a value}"; shift 2 ;;
    --) shift; break ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) break ;;
  esac
done
case "${SLOTS}" in
  ''|*[!0-9]*|0) echo 'capped-test-run: invalid slot count' >&2; exit 2 ;;
esac
case "${WAIT_S}" in
  ''|*[!0-9]*) echo 'capped-test-run: invalid wait seconds' >&2; exit 2 ;;
esac
if [ "$#" -eq 0 ]; then
  echo 'capped-test-run: no command given' >&2; exit 2
fi
command -v flock >/dev/null || { echo 'capped-test-run: flock required' >&2; exit 2; }
mkdir -p -- "${SEMAPHORE_ROOT}" || exit 2
deadline=$((SECONDS + WAIT_S))
run_holding_slot() {
  interrupted=0
  trap 'interrupted=130' INT
  trap 'interrupted=143' TERM
  # Runners that acquire a slot on their own (vitest globalSetup, pytest
  # conftest) see this and never ask for a second one: a nested request
  # could deadlock once every slot is held by an outer wrapper.
  AGENT_HUB_HEAVY_TEST_HELD=1 "$@" <&0 &
  command_pid=$!
  # A signal interrupts bash wait. Retain the slot until our command
  # finishes; releasing it early would admit another suite over the cap.
  while true; do
    wait "${command_pid}"
    command_rc=$?
    kill -0 "${command_pid}" 2>/dev/null || break
  done
  if [ "${interrupted}" -ne 0 ]; then exit "${interrupted}"; fi
  exit "${command_rc}"
}
round=0
announced=0
while true; do
  for ((i=0; i<SLOTS; i++)); do
    # Keep the lock in this wrapper: Node and subprocess runners can close
    # inherited descriptors. Never unlink a lock file while users may hold it.
    exec 9>"${SEMAPHORE_ROOT}/slot-${i}.lock" || exit 2
    if flock -n 9; then
      run_holding_slot "$@"
    fi
    exec 9>&-
  done
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "capped-test-run: no heavy-test slot free after ${WAIT_S}s (${SLOTS} slots)" >&2
    exit 3
  fi
  if [ "${announced}" -eq 0 ]; then
    echo "capped-test-run: all ${SLOTS} heavy-test slots busy; waiting up to ${WAIT_S}s" >&2
    announced=1
  fi
  # Block on one slot (rotating) instead of polling: a busy host can have
  # dozens of waiters, and a sleep+flock loop per waiter is a fork storm.
  exec 9>"${SEMAPHORE_ROOT}/slot-$((round % SLOTS)).lock" || exit 2
  round=$((round + 1))
  if flock -w 2 9; then
    run_holding_slot "$@"
  fi
  exec 9>&-
done
