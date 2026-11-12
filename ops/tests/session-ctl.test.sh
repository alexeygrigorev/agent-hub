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
export FAKE_HUB_STATE="$fake/hub-sessions.json"
export FAKE_HUB_START_LOG="$fake/hub-start.log"
export HUB_PARSER_ROOT="$repo/host"
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
    echo 'direct native start is forbidden (phase 6.1)' >&2; exit 90
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
if [[ "${1:-}" == sessions ]]; then
  printf '%s\n' "$*" >> "$FAKE_HUB_START_LOG"
  exec python3 -I - "$@" <<'PYHUB'
# Parse argv with the maintained `hub sessions` parser: unsupported flags such
# as --tag or --json exit 2 exactly as in production.
import argparse,json,os,sys
sys.path.insert(0, os.environ['HUB_PARSER_ROOT'])
from agent_hub_host import session_control
parser=argparse.ArgumentParser(prog='hub'); session_control.add_commands(parser.add_subparsers(dest='cmd'))
ns=parser.parse_args(sys.argv[1:])
path=os.environ['FAKE_HUB_STATE']
hub=json.load(open(path)) if os.path.exists(path) else {}
def save(): json.dump(hub,open(path,'w'))
if ns.sessions_cmd=='start':
    assert ns.interactive and ns.wait and ns.purpose=='implement' and ns.key and ns.intake_task_id
    hid='hub-%04d' % (len(hub)+1)
    mode=os.environ.get('FAKE_HUB_START_MODE','running')
    if mode=='transport':
        sys.exit('Session transport failed: connection or TLS failure (reconcile with --key %s via show/commands)' % ns.key)
    if mode=='refused':
        hub[hid]={'state':'refused'}; save()
        sys.exit('session_start_failed; session %s reached refused (no_capacity); reconcile with --key %s via show/events' % (hid, ns.key))
    engine={'go':'opencode','zai':'zcodex'}.get(ns.engine,ns.engine)
    open(os.environ['FAKE_START_ENGINE'],'w').write(engine+'\n')
    open(os.environ['FAKE_START_MODEL'],'w').write(ns.model or '')
    native='new-'+ns.name
    hub[hid]={'state':'running','native_id':native}; save()
    json.dump([dict(id=native,workspace=ns.workspace,tag='task-hub2-'+hid,phase='running',worker_alive=True,
                    labels={'hub.session':hid,'hub.managed':'true'})],open(os.environ['FAKE_SESSIONS'],'w'))
    open(os.environ['FAKE_SCREEN'],'w').write(os.environ.get('FAKE_START_SCREEN','❯')+'\n')
    bound=dict(id=hid,state='running',native_id=native,workspace=ns.workspace)
    print(json.dumps({**bound,'client_key':ns.key,'binding_wait':{'session':bound}},indent=2))
elif ns.sessions_cmd=='stop':
    row=hub.get(ns.id)
    if not row or row['state']!='running': sys.exit('stop_not_running; command c1; stop was not confirmed')
    row['state']='stopped'; save()
    rows=[x for x in json.load(open(os.environ['FAKE_SESSIONS'])) if x['id']!=row['native_id']]
    json.dump(rows,open(os.environ['FAKE_SESSIONS'],'w'))
elif ns.sessions_cmd=='show':
    print(json.dumps({'session':{'id':ns.id,**hub.get(ns.id,{'state':'unknown'})}}))
else:
    sys.exit(2)
PYHUB
fi
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
  printf '[{"id":"old-%s","workspace":"%s","tag":"%s","phase":"running","worker_alive":true,"labels":{"ops.role":"%s"}}]\n' "$role" "$workspace" "$tag" "$role" > "$FAKE_SESSIONS"
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

# A legacy Gemini quota route is never eligible for oncall, even when Hub
# advertises it as fresh and configured.
write_quota <<'JSON'
{"pools":[{"provider":"gemini","state":"fresh","remaining_percent":44,"harness":["gemini"]}]}
JSON
write_live oncall 'You have hit your weekly limit - resets Nov 5'
status=0
output="$(run_ctl oncall check 2>&1)" || status=$?
[[ "$status" -ne 0 ]]
assert_not_contains "$output" 'Hub quota unavailable'
assert_contains "$output" 'no configured non-Claude engine has fresh Hub quota headroom'
[[ ! -e "$FAKE_START_ENGINE" ]]

# A legacy engine override cannot widen the oncall allowlist, even when it is
# marked preselected. It normalizes to the pinned Muse route.
write_default_quota
write_dead oncall
output="$(ENGINE=antigravity ENGINE_SELECTED=1 "$repo/ops/lib/session-ctl.sh" oncall start 2>&1)"
assert_not_contains "$output" 'recovery held'
assert_contains "$output" 'oncall route '\''antigravity'\'' is disallowed'
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

# Phase 6.1 CLI contract: the fake parses argv with the maintained parser, so
# the unsupported --tag/--json of the old draft are rejected (argparse exit 2).
for bad in '--tag oncall-engineer' '--json'; do
  status=0
  # shellcheck disable=SC2086
  "$bin/hub" sessions start --workspace /w --interactive --key k --intake-task-id i $bad >/dev/null 2>&1 || status=$?
  [[ "$status" -eq 2 ]]
done

# A successful start goes through Hub only and is discovered through the exact
# returned binding (task-hub2-<Hub id>), never the old tag.
write_default_quota
write_dead oncall
: > "$FAKE_HUB_START_LOG"
output="$(run_ctl oncall start 2>&1)"
assert_contains "$output" 'started session new-oncall-engineer'
start_line="$(grep '^sessions start' "$FAKE_HUB_START_LOG")"
assert_not_contains "$start_line" '--tag'
assert_not_contains "$start_line" '--json'
assert_contains "$start_line" '--name oncall-engineer'
assert_contains "$start_line" '--engine go'
binding="$HOME/tmp/agent-reports/oncall/state/hub_binding.json"
python3 -c '
import json,sys
b=json.load(open(sys.argv[1]))
assert b["native_id"]=="new-oncall-engineer" and b["native_tag"]=="task-hub2-"+b["hub_id"], b' "$binding"
output="$(run_ctl oncall check 2>&1)"
assert_contains "$output" 'OK session=new-oncall-engineer'
# The same native without the binding is not guessed from tag or workspace.
mv "$binding" "$binding.saved"
status=0
output="$(run_ctl oncall status 2>&1)" || status=$?
[[ "$status" -ne 0 ]]
assert_contains "$output" 'not running'
mv "$binding.saved" "$binding"

# Hub refusal or transport failure never falls back to a native start; the
# typed reason is logged. The refused Hub session is stopped through Hub
# before the next attempt.
for mode in refused transport; do
  write_default_quota
  write_dead oncall
  status=0
  output="$(FAKE_HUB_START_MODE="$mode" run_ctl oncall start 2>&1)" || status=$?
  [[ "$status" -ne 0 ]]
  [[ ! -e "$FAKE_START_ENGINE" ]]
  assert_not_contains "$output" 'started session'
  assert_contains "$output" 'recovery held'
  if [[ "$mode" == refused ]]; then
    assert_contains "$output" 'reached refused (no_capacity)'
  else
    assert_contains "$output" 'already ended (refused)'
    assert_contains "$output" 'Session transport failed'
    [[ -s "$HOME/tmp/agent-reports/oncall/state/hub_start_key" ]]
  fi
done
output="$(run_ctl oncall start 2>&1)"
assert_contains "$output" 'started session'
[[ ! -e "$HOME/tmp/agent-reports/oncall/state/hub_start_key" ]]

echo 'session-ctl targeted tests passed'
