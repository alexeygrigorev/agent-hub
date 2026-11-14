#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
fake="$HOME/session-ctl-fixture"
bin="$fake/bin"
mkdir -p "$bin" "$fake/skills/.agents/skills" "$fake/skills/.git" "$fake/locks"

export PATH="$bin:$PATH"
export BASE_REPO="$fake/base"
export SKILLS_REPO="$fake/skills"
export SKILLS_DIR="$fake/skills/.agents/skills"
export OPS_LOCK_DIR="$fake/locks"
export OPS_TIMERS=0 OPS_HUB_REPORT=0 MIN_RESTART_GAP=0 MAX_ONCALL_INBOX_AGE=1200
export FAKE_SESSIONS="$fake/sessions.json"
export FAKE_SCREEN="$fake/screen.txt"
export FAKE_QUOTA="$fake/quota.json"
export FAKE_KILLED="$fake/killed.log"
export FAKE_START_ENGINE="$fake/start-engine"
export FAKE_START_MODEL="$fake/start-model"
export FAKE_SEND_LOG="$fake/send.log"
unset ENGINE ENGINE_SELECTED ORCH_ENGINE ORCH_FALLBACK_ENGINE ONCALL_ENGINE

cat > "$bin/a" <<'APLEXER'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  engines)
    printf '[{"name":"antigravity"},{"name":"grok"},{"name":"opencode"},{"name":"zcodex"},{"name":"codex"},{"name":"claude"}]\n'
    ;;
  list)
    cat "$FAKE_SESSIONS"
    ;;
  capture)
    cat "$FAKE_SCREEN"
    ;;
  kill)
    printf '%s\n' "${2:-}" >> "$FAKE_KILLED"
    printf '[]\n' > "$FAKE_SESSIONS"
    ;;
  start)
    workspace=""; tag=""; engine=""; model=""
    while (($#)); do
      case "$1" in
        --workspace) workspace="$2"; shift 2 ;;
        --tag) tag="$2"; shift 2 ;;
        --engine) engine="$2"; shift 2 ;;
        --model) model="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    printf '%s\n' "$engine" > "$FAKE_START_ENGINE"
    printf '%s' "$model" > "$FAKE_START_MODEL"
    printf '[{"id":"new-%s","workspace":"%s","tag":"%s","phase":"running","worker_alive":true}]\n' "$tag" "$workspace" "$tag" > "$FAKE_SESSIONS"
    printf '%s\n' "${FAKE_START_SCREEN:-❯}" > "$FAKE_SCREEN"
    ;;
  send)
    printf '%s\n' "$*" >> "$FAKE_SEND_LOG"
    if [[ " $* " == *" --hex "* && -n "${FAKE_AFTER_HEX_SCREEN:-}" ]]; then
      printf '%s\n' "$FAKE_AFTER_HEX_SCREEN" > "$FAKE_SCREEN"
    fi
    ;;
  *)
    echo "unexpected fake a command: $1" >&2
    exit 2
    ;;
esac
APLEXER

cat > "$bin/hub" <<'HUB'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == quota && "${2:-}" == --json ]] || exit 2
case "${FAKE_HUB_MODE:-healthy}" in
  fail) exit 1 ;;
  timeout) exit 124 ;;
  stub) printf '{"error":"release_bootstrap","dispatch_enabled":false}\n' ;;
  healthy) cat "$FAKE_QUOTA" ;;
  *) exit 2 ;;
esac
HUB

cat > "$bin/systemd-run" <<'SYSTEMD_RUN'
#!/usr/bin/env bash
set -euo pipefail
while (($#)); do
  if [[ "$1" == -- ]]; then
    shift
    exec "$@"
  fi
  shift
done
exit 2
SYSTEMD_RUN

cat > "$bin/git" <<'GIT'
#!/usr/bin/env bash
set -euo pipefail
dir=""
if [[ "${1:-}" == -C ]]; then dir="$2"; shift 2; fi
case "${1:-} ${2:-}" in
  "worktree add")
    target=""
    for value in "$@"; do
      [[ "$value" == "$HOME"/git/agent-hub-wt-*-run ]] && target="$value"
    done
    [[ -n "$target" ]] || exit 2
    mkdir -p "$target/.git/info"
    : > "$target/.git/info/exclude"
    ;;
  "rev-parse --git-path") printf '%s\n' "$dir/.git/info/exclude" ;;
  "rev-parse --short") printf 'abc1234\n' ;;
  *) : ;;
esac
GIT

cat > "$bin/gh" <<'GH'
#!/usr/bin/env bash
set -euo pipefail
exit 2
GH
chmod +x "$bin/a" "$bin/hub" "$bin/systemd-run" "$bin/git" "$bin/gh"

write_quota() {
  cat > "$FAKE_QUOTA"
}

write_live() {
  local role="$1" pane="$2" tag workspace sub
  if [[ "$role" == orchestrator ]]; then tag=orchestrator-main; sub=orchestrator
  else tag=oncall-engineer; sub=oncall; fi
  workspace="$HOME/git/agent-hub-wt-${role}-run/ops/$sub"
  mkdir -p "$workspace" "$HOME/tmp/agent-reports/$sub/inbox"
  printf '[{"id":"old-%s","workspace":"%s","tag":"%s","phase":"running","worker_alive":true}]\n' "$role" "$workspace" "$tag" > "$FAKE_SESSIONS"
  printf '%s\n' "$pane" > "$FAKE_SCREEN"
  : > "$FAKE_KILLED"
  : > "$FAKE_SEND_LOG"
  rm -f "$FAKE_START_ENGINE" "$FAKE_START_MODEL" "$HOME/tmp/agent-reports/$sub/state/last_start"
}

write_dead() {
  local role="$1" tag sub
  if [[ "$role" == orchestrator ]]; then tag=orchestrator-main; sub=orchestrator
  else tag=oncall-engineer; sub=oncall; fi
  mkdir -p "$HOME/git/agent-hub-wt-${role}-run/ops/$sub" "$HOME/tmp/agent-reports/$sub/inbox"
  printf '[]\n' > "$FAKE_SESSIONS"
  printf 'normal ready prompt\n' > "$FAKE_SCREEN"
  : > "$FAKE_KILLED"
  : > "$FAKE_SEND_LOG"
  rm -f "$FAKE_START_ENGINE" "$FAKE_START_MODEL" "$HOME/tmp/agent-reports/$sub/state/last_start"
}

write_default_quota() {
  # Hub's provider-family quota key is still "gemini"; its runnable engine is
  # Antigravity. Keep the provider label while exposing only the Agy harness.
  write_quota <<'JSON'
{"pools":[
 {"provider":"gemini","state":"fresh","remaining_percent":44,"harness":["antigravity"]},
 {"provider":"go","state":"fresh","remaining_percent":36,"harness":["opencode"]},
 {"provider":"grok","state":"fresh","remaining_percent":32,"harness":["grok"]},
 {"provider":"zai","state":"fresh","remaining_percent":50,"harness":["zcodex"]},
 {"provider":"claude","state":"exhausted","remaining_percent":0,"harness":["claude","sonnet","opus","haiku"]}
]}
JSON
}

run_ctl() { "$repo/ops/lib/session-ctl.sh" "$@"; }
assert_contains() { [[ "$1" == *"$2"* ]] || { echo "expected output to contain: $2" >&2; echo "$1" >&2; exit 1; }; }
assert_not_contains() { [[ "$1" != *"$2"* ]] || { echo "unexpected output text: $2" >&2; echo "$1" >&2; exit 1; }; }

# A recent inbox item remains healthy and does not cause a restart.
write_default_quota
write_live oncall 'normal ready prompt'
printf 'recent alert\n' > "$HOME/tmp/agent-reports/oncall/inbox/recent.md"
touch -d '10 minutes ago' "$HOME/tmp/agent-reports/oncall/inbox/recent.md"
output="$(run_ctl oncall check 2>&1)"
assert_contains "$output" 'OK session=old-oncall'
[[ ! -e "$FAKE_START_ENGINE" ]]

# A stale inbox item is a progress failure and selects a quota-backed engine.
write_default_quota
write_live oncall 'normal ready prompt'
printf 'unprocessed alert\n' > "$HOME/tmp/agent-reports/oncall/inbox/old.md"
touch -d '25 minutes ago' "$HOME/tmp/agent-reports/oncall/inbox/old.md"
output="$(run_ctl oncall check 2>&1)"
assert_contains "$output" 'STALLED session=old-oncall: oncall inbox'
assert_not_contains "$output" 'OK session='
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
[[ "$(<"$FAKE_START_MODEL")" == opencode-go/muse-spark-1.3-contributor ]]
assert_contains "$(<"$FAKE_SEND_LOG")" 'Run the oncall check now:'
assert_not_contains "$(<"$FAKE_SEND_LOG")" '/loop'

# A failed Hub quota command falls back to the preferred locally available
# engine, allowing a dead oncall session to start.
write_default_quota
write_dead oncall
export FAKE_HUB_MODE=fail
output="$(run_ctl oncall start 2>&1)"
unset FAKE_HUB_MODE
assert_contains "$output" 'Hub quota unavailable; using local engine opencode'
assert_not_contains "$output" 'recovery held'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
[[ "$(<"$FAKE_START_MODEL")" == opencode-go/muse-spark-1.3-contributor ]]

# The Hub release_bootstrap stub falls back during a real oncall recovery.
write_live oncall 'You have hit your weekly limit - resets Nov 5'
export FAKE_HUB_MODE=stub
output="$(run_ctl oncall check 2>&1)"
unset FAKE_HUB_MODE
assert_contains "$output" 'provider usage limit screen'
assert_contains "$output" 'Hub quota unavailable; using local engine opencode'
assert_not_contains "$output" 'recovery held'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
[[ "$(<"$FAKE_START_MODEL")" == opencode-go/muse-spark-1.3-contributor ]]

# An HTTP-command timeout (the same nonzero result as timeout(1)) also uses
# the local route instead of holding recovery.
write_live oncall 'You have hit your weekly limit - resets Nov 5'
export FAKE_HUB_MODE=timeout
output="$(run_ctl oncall check 2>&1)"
unset FAKE_HUB_MODE
assert_contains "$output" 'Hub quota unavailable; using local engine opencode'
assert_not_contains "$output" 'recovery held'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
[[ "$(<"$FAKE_START_MODEL")" == opencode-go/muse-spark-1.3-contributor ]]

# A healthy quota document still wins over the local preference.
write_quota <<'JSON'
{"pools":[{"provider":"go","state":"fresh","remaining_percent":36,"harness":["opencode"]}]}
JSON
write_live oncall 'You have hit your weekly limit - resets Nov 5'
output="$(run_ctl oncall check 2>&1)"
assert_not_contains "$output" 'Hub quota unavailable'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
[[ "$(<"$FAKE_START_MODEL")" == opencode-go/muse-spark-1.3-contributor ]]

# When OpenCode Go is exhausted and Antigravity (Gemini pool) has fresh quota,
# oncall recovers onto Antigravity with its pinned Hydrogen Medium model (never Claude).
write_quota <<'JSON'
{"pools":[
 {"provider":"claude","state":"fresh","remaining_percent":60,"harness":["claude","sonnet","opus","haiku"]},
 {"provider":"go","state":"exhausted","remaining_percent":0,"harness":["opencode"]},
 {"provider":"gemini","state":"fresh","remaining_percent":97,"harness":["antigravity"]}
]}
JSON
write_live oncall 'You have hit your weekly limit - resets Nov 5'
output="$(run_ctl oncall check 2>&1)"
assert_contains "$output" 'provider usage limit screen'
assert_contains "$output" 'engine preference unavailable; Hub quota selected antigravity'
[[ "$(<"$FAKE_START_ENGINE")" == antigravity ]]
[[ "$(<"$FAKE_START_MODEL")" == gemini-4-hydrogen-medium ]]

# When oncall inbox is stale and OpenCode Go + Antigravity are exhausted,
# recovery selects the next non-Claude/non-Astra engine with fresh quota (grok).
write_quota <<'JSON'
{"pools":[
 {"provider":"go","state":"exhausted","remaining_percent":0,"harness":["opencode"]},
 {"provider":"gemini","state":"exhausted","remaining_percent":0,"harness":["antigravity"]},
 {"provider":"grok","state":"fresh","remaining_percent":40,"harness":["grok"]}
]}
JSON
write_live oncall 'normal ready prompt'
printf 'unprocessed alert\n' > "$HOME/tmp/agent-reports/oncall/inbox/old.md"
touch -d '25 minutes ago' "$HOME/tmp/agent-reports/oncall/inbox/old.md"
output="$(run_ctl oncall check 2>&1)"
assert_contains "$output" 'STALLED session=old-oncall: oncall inbox'
[[ "$(<"$FAKE_START_ENGINE")" == grok ]]
[[ -z "$(<"$FAKE_START_MODEL")" ]]
rm -f "$HOME/tmp/agent-reports/oncall/inbox/old.md"

# A disallowed engine override (such as claude or astra) cannot widen the oncall
# allowlist, even when it is marked preselected. It normalizes to the primary route.
write_default_quota
write_dead oncall
output="$(ENGINE=claude ENGINE_SELECTED=1 "$repo/ops/lib/session-ctl.sh" oncall start 2>&1)"
assert_not_contains "$output" 'recovery held'
assert_contains "$output" 'oncall route '\''claude'\'' is disallowed'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
[[ "$(<"$FAKE_START_MODEL")" == opencode-go/muse-spark-1.3-contributor ]]

# Immediately after a restart, oncall gets a MAX_ONCALL_INBOX_AGE grace window from last_start
# to finish draining pre-existing inbox items instead of being killed mid-triage.
rm -f "$FAKE_START_ENGINE" "$FAKE_START_MODEL"
output="$(run_ctl oncall check 2>&1)"
assert_contains "$output" 'OK session=new-oncall-engineer'
[[ ! -e "$FAKE_START_ENGINE" ]]
rm -f "$HOME/tmp/agent-reports/oncall/inbox/old.md"

# Unmetered OpenCode Go pool (remaining_percent: null, burn_status: unmetered) is accepted,
# folder trust uses --hex without --raw, and OpenCode TUI prompt is recognized as ready.
write_quota <<'JSON'
{"pools":[
 {"provider":"claude","state":"exhausted","remaining_percent":0,"harness":["claude","sonnet","opus","haiku"]},
 {"provider":"go","state":"fresh","source":"unmetered-engine","burn_status":"unmetered","depleted":false,"remaining_percent":null,"engines":["opencode"],"harness":["opencode"]}
]}
JSON
write_live oncall 'You have hit your weekly limit - resets Nov 5'
export FAKE_START_SCREEN='Do you trust the contents of this project? Yes, proceed'
export FAKE_AFTER_HEX_SCREEN='Ask anything…  ctrl+p commands'
output="$(run_ctl oncall check 2>&1)"
unset FAKE_START_SCREEN FAKE_AFTER_HEX_SCREEN
assert_contains "$output" 'provider usage limit screen'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
[[ "$(<"$FAKE_START_MODEL")" == opencode-go/muse-spark-1.3-contributor ]]
assert_contains "$(<"$FAKE_SEND_LOG")" 'new-oncall-engineer --hex 0d'
assert_not_contains "$(<"$FAKE_SEND_LOG")" '--raw --hex'
assert_contains "$(<"$FAKE_SEND_LOG")" 'Run the oncall check now:'

# Both roles recover from limit screens, and exhausted Claude quota is never selected.
for role in oncall orchestrator; do
  write_default_quota
  write_live "$role" 'You have hit your weekly limit - resets Nov 5'
  output="$(run_ctl "$role" check 2>&1)"
  assert_contains "$output" 'provider usage limit screen'
  assert_not_contains "$output" 'OK session='
  if [[ "$role" == oncall ]]; then
    [[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
    [[ "$(<"$FAKE_START_MODEL")" == opencode-go/muse-spark-1.3-contributor ]]
  else
    [[ "$(<"$FAKE_START_ENGINE")" == antigravity ]]
    [[ "$(<"$FAKE_START_MODEL")" == gemini-3.8-flash-high ]]
  fi
done

# Recovery path 2 prefers Antigravity but excludes its failed route, so it uses
# the next configured route, OpenCode.
write_default_quota
write_live orchestrator 'normal ready prompt'
state="$HOME/tmp/agent-reports/orchestrator/state"
mkdir -p "$state"
printf '%s\n' "$(( $(date +%s)-20 ))" > "$state/last_start"
printf '%s 1\n' "$(( $(date +%s)-1 ))" > "$state/proof_due"
if output="$(run_ctl orchestrator check 2>&1)"; then :; fi
assert_contains "$output" 'recovery path 2'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
assert_not_contains "$(<"$FAKE_SEND_LOG")" '/loop'

# A failed recovery excludes the engine that actually started, even when quota
# forced it away from the preferred primary route.
write_quota <<'JSON'
{"pools":[{"provider":"go","state":"fresh","remaining_percent":36,"harness":["opencode"]}]}
JSON
write_live orchestrator 'normal ready prompt'
state="$HOME/tmp/agent-reports/orchestrator/state"
rm -f "$state/proof_due"
printf '%s\n' "$(( $(date +%s)-3600 ))" > "$state/last_start"
output="$(run_ctl orchestrator check 2>&1)"
assert_contains "$output" 'recovery path 1 started on opencode'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]
state="$HOME/tmp/agent-reports/orchestrator/state"
read -r due path attempted_engine < "$state/proof_due"
[[ "$attempted_engine" == opencode ]]
printf '%s %s %s\n' "$(( $(date +%s)-1 ))" "$path" "$attempted_engine" > "$state/proof_due"
write_default_quota
output="$(run_ctl orchestrator check 2>&1)" || true
assert_contains "$output" 'recovery path 2 started on antigravity'
[[ "$(<"$FAKE_START_ENGINE")" == antigravity ]]
assert_not_contains "$(<"$FAKE_SEND_LOG")" '/loop'

# Claude-only, Codex-only, stale and exhausted quota are not sufficient recovery evidence.
cat > "$FAKE_QUOTA" <<'JSON'
{"pools":[
 {"provider":"claude","state":"fresh","remaining_percent":80,"harness":["claude","sonnet","opus","haiku"]},
 {"provider":"codex","state":"fresh","remaining_percent":90,"harness":["codex"]},
 {"provider":"gemini","state":"stale","remaining_percent":80,"harness":["gemini"]},
 {"provider":"go","state":"exhausted","remaining_percent":0,"harness":["opencode"]}
]}
JSON
write_live oncall 'usage limit'
status=0
output="$(run_ctl oncall check 2>&1)" || status=$?
[[ "$status" -ne 0 ]]
assert_contains "$output" 'no configured non-Claude engine has fresh Hub quota headroom'
[[ ! -s "$FAKE_KILLED" ]]
[[ ! -e "$FAKE_START_ENGINE" ]]

# Burn-pace depletion is not quota exhaustion, and one usable account pool
# keeps an engine routable when another pool in that provider family is spent.
write_quota <<'JSON'
{"pools":[
 {"provider":"go","state":"fresh","remaining_percent":0,"depleted":true,"harness":["opencode"]},
 {"provider":"go","state":"fresh","remaining_percent":27,"depleted":true,"burn_status":"depleted","harness":["opencode"]}
]}
JSON
write_live oncall 'usage limit'
output="$(run_ctl oncall check 2>&1)"
assert_contains "$output" 'provider usage limit screen'
[[ "$(<"$FAKE_START_ENGINE")" == opencode ]]

# A positive remaining percentage does not override an explicit hard limit.
write_quota <<'JSON'
{"pools":[{"provider":"go","state":"fresh","remaining_percent":27,"depleted":true,"limit_reached":true,"harness":["opencode"]}]}
JSON
write_live oncall 'usage limit'
status=0
output="$(run_ctl oncall check 2>&1)" || status=$?
[[ "$status" -ne 0 ]]
assert_contains "$output" 'no configured non-Claude engine has fresh Hub quota headroom'
[[ ! -e "$FAKE_START_ENGINE" ]]

# Both session-ctl and oncall-send recognize a running oncall-engineer session
# whose workspace is a Hub nested worktree under ops/oncall.
nested_ws="$HOME/git/agent-hub-wt-oncall-run/ops/oncall/.local/hub-worktrees/deadbeef"
mkdir -p "$nested_ws"
printf '[{"id":"nested-oncall","workspace":"%s","tag":"oncall-engineer","phase":"running","worker_alive":true}]\n' "$nested_ws" > "$FAKE_SESSIONS"
printf 'normal ready prompt\n' > "$FAKE_SCREEN"
rm -f "$FAKE_START_ENGINE" "$FAKE_START_MODEL"
: > "$FAKE_SEND_LOG"
output="$(run_ctl oncall check 2>&1)"
assert_contains "$output" 'OK session=nested-oncall'
[[ ! -e "$FAKE_START_ENGINE" ]]

install -m755 "$repo/ops/lib/send.sh" "$bin/oncall-send"
send_output="$("$bin/oncall-send" "nested worktree delivery probe" 2>&1)"
assert_contains "$send_output" 'DELIVERY=delivered to oncall-engineer (nested-oncall)'
assert_contains "$(<"$FAKE_SEND_LOG")" 'nested-oncall [INBOX '

echo 'session-ctl targeted tests passed'
