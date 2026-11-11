#!/usr/bin/env bash
# Forced-command SSH wrapper for the Hub pre-commit live shell start canary.
# Invoked via authorized_keys:
#   restrict,command="/usr/local/sbin/hub-start-canary/hub-deploy-gate-forced.sh" ssh-ed25519 ...
# Purges the caller environment, validates SSH_ORIGINAL_COMMAND against a single
# fixed shape, acquires a single-run lock, and executes hub_start_canary.py.
set -euo pipefail

PROD_LIB="/usr/local/sbin/hub-start-canary"
HERE="$(/usr/bin/dirname -- "$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")")"

emit_reject() {
  local stage="$1" reason="$2" code="$3"
  local payload
  payload="$(/usr/bin/printf '{"cleanup": "not_needed", "deploy_gate": true, "mode": "shell", "ok": false, "reason": "%s", "session_id": null, "stage": "%s"}\n' "$reason" "$stage")"
  /usr/bin/printf '%s\n' "$payload"
  /usr/bin/printf '%s\n' "$payload" >&2
  exit "$code"
}

if (($# != 0)); then
  emit_reject "forced_command" "unexpected_positional_arguments" 2
fi

ORIG_CMD="${SSH_ORIGINAL_COMMAND:-}"
if [[ ! "$ORIG_CMD" =~ ^hub-deploy-gate\ --candidate-sha\ ([0-9a-f]{40})\ --run-token\ ([0-9a-f]{32,64})\ --helper-sha256\ ([0-9a-f]{64})$ ]]; then
  emit_reject "forced_command" "invalid_forced_command" 2
fi
CANDIDATE_SHA="${BASH_REMATCH[1]}"
RUN_TOKEN="${BASH_REMATCH[2]}"
HELPER_SHA256="${BASH_REMATCH[3]}"

USER_UID="$(/usr/bin/id -u)"
USER_NAME="$(/usr/bin/id -un)"
USER_HOME="$(/usr/bin/getent passwd "$USER_UID" | /usr/bin/cut -d: -f6)"
if [[ -z "$USER_HOME" || ! -d "$USER_HOME" ]]; then
  emit_reject "forced_command" "invalid_user_home" 1
fi

A_BIN="$USER_HOME/.local/bin/a"
HOST_RELEASE_ROOT="$USER_HOME/.local/share/agent-hub2"
WORKSPACE_DIR="$USER_HOME/work/hub-canary"
STATE_DIR="$USER_HOME/.local/state/agent-hub-canary"
EXTRA_ENV=()

if [[ "$HERE" != "$PROD_LIB" && -f "$HERE/.test-harness.env" ]]; then
  # Offline unit tests copy the scripts into an isolated temp directory and
  # write .test-harness.env there so env -i still purges caller variables
  # without touching the real user home. Unreachable in /usr/local/sbin/hub-start-canary.
  # shellcheck disable=SC1091
  source "$HERE/.test-harness.env"
fi

/usr/bin/install -d -m 700 "$STATE_DIR"
exec 9>"$STATE_DIR/deploy-gate.lock"
if ! /usr/bin/flock -w "${DEPLOY_GATE_LOCK_WAIT_S:-5}" 9; then
  emit_reject "lock" "deploy_gate_lock_busy" 1
fi

exec /usr/bin/timeout --kill-after=10s "${DEPLOY_GATE_TIMEOUT_S:-360}s" \
  /usr/bin/env -i \
  HOME="$USER_HOME" \
  USER="$USER_NAME" \
  LOGNAME="$USER_NAME" \
  XDG_RUNTIME_DIR="/run/user/$USER_UID" \
  PATH="$USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  A_BIN="$A_BIN" \
  HUB_CANARY_HOST_RELEASE_ROOT="$HOST_RELEASE_ROOT" \
  HUB_CANARY_WORKSPACE="$WORKSPACE_DIR" \
  HUB_CANARY_STATE_DIR="$STATE_DIR" \
  "${EXTRA_ENV[@]}" \
  /usr/bin/python3 "$HERE/hub_start_canary.py" \
  --mode shell \
  --deploy-gate \
  --candidate-sha "$CANDIDATE_SHA" \
  --run-token "$RUN_TOKEN" \
  --helper-sha256 "$HELPER_SHA256"
