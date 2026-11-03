#!/usr/bin/env bash
# Tiny restart-if-dead watchdog. NOT a scheduler: Claude Code's /loop inside each session does the periodic work.
set -uo pipefail
CTL="$(dirname "$(readlink -f "$0")")/session-ctl.sh"
rc=0
for role in orchestrator oncall; do "$CTL" "$role" check || rc=1; done

# Incident 504d662b AC3: ~/.local/bin vanished once and nothing noticed for
# minutes. Run AFTER the session checks with bounded timeouts so a slow NSS or
# msmtp never delays orchestrator/oncall recovery. Alert once per episode
# (recovery resets); only record 'fail' after the alert succeeds so a transient
# alerter failure retries on the next tick instead of muting the episode.
bin_watch() {
  local self_dir guard alerter cand msg state_dir state_file prev
  self_dir=$(dirname "$(readlink -f "$0")")
  guard=""
  for cand in "$self_dir/guard-bin/check-local-bin" "$self_dir/../guard-bin/check-local-bin"; do
    [ -x "$cand" ] && guard=$cand && break
  done
  [ -n "$guard" ] || return 0  # not installed here: never fail the watchdog on our account
  state_dir=${BIN_WATCH_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-hub-ops}
  mkdir -p "$state_dir" 2>/dev/null || return 0
  state_file=$state_dir/bin-watch.state
  if msg=$(timeout "${BIN_WATCH_GUARD_TIMEOUT:-10}" "$guard" 2>&1); then
    printf 'ok %s\n' "$(date -u +%FT%TZ)" > "$state_file"
    echo "$msg"
    return 0
  fi
  [ -n "$msg" ] || msg="bin-watch: ALERT: check-local-bin timed out or failed"
  prev=$(cat "$state_file" 2>/dev/null || true)
  echo "$msg" >&2
  case "$prev" in
    "fail "*)
      printf 'fail %s\n' "$(date -u +%FT%TZ)" > "$state_file"
      return 1
      ;;
  esac
  alerter=""
  for cand in "${BIN_WATCH_ALERTER:-}" \
              "$self_dir/guard-bin/principal-alert.sh" \
              "$self_dir/../guard-bin/principal-alert.sh" \
              "$HOME/.local/share/agent-hub-ops/guard-bin/principal-alert.sh" \
              "$HOME/.local/bin/principal-alert.sh"; do
    [ -n "$cand" ] && [ -x "$cand" ] && alerter=$cand && break
  done
  if [ -n "$alerter" ] && timeout "${BIN_WATCH_ALERT_TIMEOUT:-20}" "$alerter" "agent-hub bin-watch: $msg"; then
    printf 'fail %s\n' "$(date -u +%FT%TZ)" > "$state_file"
  else
    printf 'fail-unalerted %s\n' "$(date -u +%FT%TZ)" > "$state_file"
  fi
  return 1
}
bin_watch || rc=1

exit $rc
