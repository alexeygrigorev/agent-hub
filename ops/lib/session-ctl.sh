#!/usr/bin/env bash
# Shared control for the two interactive Claude ops sessions.
#   session-ctl.sh <orchestrator|oncall> <start|ensure|check|status|restart|handoff-restart|skills-sync>
# Watchdog (check) restarts a dead or tick-stalled session and proves every restart by a fresh
# tick (intake 500fd60b); the in-session /loop is the scheduler.
# Needs: a (aplexer), git, gh, python3. Never prints secrets.
set -uo pipefail

ROLE="${1:-}"; CMD="${2:-status}"
case "$ROLE" in
  orchestrator) TAG=orchestrator-main; REPORTS="$HOME/tmp/agent-reports/orchestrator"; RUNDIR="$HOME/git/agent-hub-wt-orchestrator-run"; SUB=ops/orchestrator; INTAKE=67847fde-a00a-4689-8460-fbad45599f99 ;;
  oncall)       TAG=oncall-engineer;   REPORTS="$HOME/tmp/agent-reports/oncall";        RUNDIR="$HOME/git/agent-hub-wt-oncall-run";       SUB=ops/oncall;        INTAKE=67847fde-a00a-4689-8460-fbad45599f99 ;;
  *) echo "usage: $0 <orchestrator|oncall> <start|ensure|check|status|restart|handoff-restart|skills-sync>" >&2; exit 64 ;;
esac
BASE_REPO="${BASE_REPO:-$HOME/git/agent-hub}"           # donor clone for the run worktree
SKILLS_REPO="${SKILLS_REPO:-$HOME/git/bot-skills-ops}"   # read-only clone of alexeygrigorev/bot-skills (~/git/bot-skills is NOT a git repo)
if [ "$ROLE" = orchestrator ]; then ENGINE="${ORCH_ENGINE:-opus}"; LOOP_DEFAULT=15m; else ENGINE="${ORCH_ENGINE:-sonnet}"; LOOP_DEFAULT=1h; fi
LOOP_INTERVAL="${LOOP_INTERVAL:-$LOOP_DEFAULT}"
TICKS="$HOME/tmp/agent-reports/orchestrator-ticks.jsonl"; MAX_TICK_AGE="${MAX_TICK_AGE:-1800}"
BRIEF="$HOME/tmp/briefs/orchestrator-main.md"
MIN_RESTART_GAP="${MIN_RESTART_GAP:-600}"
PROOF_WINDOW="${PROOF_WINDOW:-600}"                      # a restart must produce a fresh tick within 10 min
FALLBACK_ENGINE="${ORCH_FALLBACK_ENGINE:-sonnet}"        # second recovery path: new session, other engine
STATE="$REPORTS/state"; mkdir -p "$REPORTS/inbox/processed" "$REPORTS/history" "$STATE"
log() { echo "[$(date -u +%FT%TZ)] $ROLE: $*"; }
# fd 9 holds the per-role lock (role_lock). No child may inherit it: a detached aplexer worker,
# git daemon or session would keep the lock for its lifetime and block every later check.
t() { timeout "${T:-60}" "$@" 9>&-; }
a() { command a "$@" 9>&-; }
git() { command git "$@" 9>&-; }
gh() { command gh "$@" 9>&-; }
tick_mtime() { stat -c %Y "$TICKS" 2>/dev/null || echo 0; }
# Proof needs the tick history, not the newest mtime: is any tick line's "ts" in (start, due]?
tick_in_window() {  # $1 = start epoch, $2 = due epoch; prints the first in-window tick epoch
  [ -r "$TICKS" ] || return 1
  tail -n 2000 "$TICKS" | python3 -c '
import json,sys,datetime
lo,hi=int(sys.argv[1]),int(sys.argv[2])
for line in sys.stdin:
    try:
        ts=json.loads(line)["ts"]
        t=datetime.datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
    except Exception:
        continue
    if lo < t <= hi:
        print(int(t)); sys.exit(0)
sys.exit(1)' "$1" "$2"
}
failures() { local n=0; read -r n < "$STATE/restart_failures" 2>/dev/null || n=0; [[ "$n" =~ ^[0-9]+$ ]] || n=0; echo "$n"; }

# Hub-side liveness (src/ops-liveness.ts) watches these reports independently of this
# script and of the session; a failed report never changes the local decision.
hub_report() {  # $1 = state
  [ "$ROLE" = orchestrator ] || return 0
  [ "${OPS_HUB_REPORT:-1}" = 1 ] || return 0
  local tk; tk=$(tick_mtime)
  local args=(ops heartbeat --role "$ROLE" --kind watchdog --state "$1" --recovery-failures "$(failures)")
  [ "$tk" -gt 0 ] && args+=(--tick-at "$(date -u -d "@$tk" +%FT%TZ)")
  T=30 t hub "${args[@]}" >/dev/null 2>&1 || log "hub ops report failed (state=$1)"
}

# Exact-time checks: the 10-min timer alone would bound detection at ~41 min. Each transient
# timer runs this role's check directly, not watchdog.sh, so it never waits behind the other
# role or the shared service lock. The per-role lock (role_lock) is held only by one
# orchestrator check at a time, so a deadline waits at most for an in-flight check of the
# same session (seconds; a minute or more only while a restart is already recovering it).
SELF="$(readlink -f "$0")"
LOCK_DIR="${OPS_LOCK_DIR:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"
role_lock() {
  exec 9>"$LOCK_DIR/ops-ctl-$ROLE.lock" || return 1
  flock -w "${ROLE_LOCK_WAIT:-300}" 9 || { log "role lock busy for ${ROLE_LOCK_WAIT:-300}s; next timer retries"; return 1; }
}
arm_timer() {  # $1 = kind (deadline|proof), $2 = seconds from now
  [ "${OPS_TIMERS:-1}" = 1 ] || return 0
  local unit="ops-$1-$ROLE" delay="$2"; [ "$delay" -lt 1 ] && delay=1
  systemctl --user stop "$unit-*.timer" >/dev/null 2>&1 || true
  systemd-run --user --collect -q --on-active="$delay" --timer-property=AccuracySec=1s --unit "$unit-$(date +%s%N)" \
    --setenv=PATH="$PATH" --setenv=HUB_CONFIG_FILE="${HUB_CONFIG_FILE:-}" --setenv=APLEXER_LAUNCH_SYSTEM_SCOPE=system \
    /usr/bin/timeout 900 "$SELF" "$ROLE" check \
    >/dev/null 2>&1 9>&- || log "$1 timer not armed; the 10-min timer will check"
}

# After a restart the new session must append a tick within PROOF_WINDOW of the recorded
# start (last_start is written before launch and prompt readiness, so the wait counts).
arm_proof() {  # $1 = recovery path (1 = restart, 2 = new session on the fallback engine)
  [ "$ROLE" = orchestrator ] || return 0
  local started due; started=$(cat "$STATE/last_start" 2>/dev/null || date +%s); due=$((started + PROOF_WINDOW))
  echo "$due $1" > "$STATE/proof_due"
  arm_timer proof "$((due - $(date +%s) + 1))"
  log "recovery path $1 started; proof due: fresh tick within ${PROOF_WINDOW}s"
}

# List failures are errors, never evidence that no session exists.
matching_sessions() {
  t a list --json | python3 -c '
import json,sys
w,t,live=sys.argv[1:4]
for s in json.load(sys.stdin):
    if s.get("workspace")==w and s.get("tag")==t:
        if live == "all" or (s.get("phase")=="running" and s.get("worker_alive") is True):
            print(s["id"])' "$RUNDIR/$SUB" "$TAG" "${1:-live}"
}
find_session() { matching_sessions | head -1; }

# a forget explicitly does NOT stop surviving workloads. Require fresh evidence
# that both recorded processes and any recorded containment have drained first.
forget_dead() {
  t a status "$1" --json | python3 -c '
import json,os,pathlib,sys
s=json.load(sys.stdin)
if s.get("worker_alive") is not False:
    sys.exit(1)
for k in ("worker_pid", "workload_pid"):
    pid=s.get(k)
    if not isinstance(pid,int) or pid <= 0:
        sys.exit(1)
    try:
        os.kill(pid,0)
    except ProcessLookupError:
        continue
    sys.exit(1)
for k in ("worker_cgroup", "workload_cgroup"):
    cg=s.get(k)
    if cg:
        root=pathlib.Path("/sys/fs/cgroup") / cg.lstrip("/")
        if root.exists():
            for f in root.rglob("cgroup.procs"):
                if f.read_text().strip():
                    sys.exit(1)
' || { log "refusing forget $1: processes alive or death unverified"; return 1; }
  t a forget "$1" --force >/dev/null || { log "forget failed: $1"; return 1; }
  log "forgot verified dead session $1"
}

skills_sync() {
  if [ ! -d "$SKILLS_REPO/.git" ]; then
    T=180 t gh repo clone alexeygrigorev/bot-skills "$SKILLS_REPO" >/dev/null 2>&1 || { log "skills clone FAILED"; return 1; }
  fi
  T=90 t git -C "$SKILLS_REPO" pull --ff-only -q 2>&1 | tail -2 || log "skills pull failed (using last checkout)"
  # link every skill dir into the session's .claude/skills so Claude Code auto-discovers it
  local d="$RUNDIR/.claude/skills" n=0
  [ -d "$RUNDIR" ] || return 0
  mkdir -p "$d"
  for s in "$SKILLS_REPO"/*/; do
    s="${s%/}"; [ -f "$s/SKILL.md" ] || continue
    ln -sfn "$s" "$d/$(basename "$s")"; n=$((n+1))
  done
  grep -qxF '.claude/skills' "$(git -C "$RUNDIR" rev-parse --git-path info/exclude)" 2>/dev/null || \
    echo '.claude/skills' >> "$(git -C "$RUNDIR" rev-parse --git-path info/exclude)"
  log "skills synced: $n linked from $SKILLS_REPO @ $(git -C "$SKILLS_REPO" rev-parse --short HEAD)"
}

prep_worktree() {
  T=120 t git -C "$BASE_REPO" fetch -q origin main || log "fetch failed (using local origin/main)"
  if [ ! -e "$RUNDIR/.git" ]; then
    git -C "$BASE_REPO" worktree add --detach "$RUNDIR" origin/main >/dev/null || { log "worktree add failed"; return 1; }
  else
    git -C "$RUNDIR" checkout -q --detach origin/main 2>/dev/null || log "worktree not clean; keeping current checkout"
  fi
  skills_sync
}

screen() { a capture "$1" --screen --plain 2>/dev/null; }

start() {
  local current
  current=$(find_session) || { log "session census failed"; return 1; }
  [ -n "$current" ] && { log "already running: $current"; return 0; }
  # Clear stale names before launch; never leave orphan workloads behind.
  stop_old || return 1
  prep_worktree || return 1
  date +%s > "$STATE/last_start"
  # own transient scope so the session outlives the (oneshot) watchdog unit cgroup
  systemd-run --user --scope --collect -q -- a start --workspace "$RUNDIR/$SUB" --tag "$TAG" --engine "$ENGINE" \
    --label "ops.role=$ROLE" --label "hub.intake=$INTAKE" >/dev/null 9>&- || { log "a start failed"; return 1; }
  local id; id=$(find_session); [ -n "$id" ] || { log "session not found after start"; return 1; }
  log "started session $id; waiting for prompt"
  local i s ready=0
  for i in $(seq 1 60); do
    sleep 3; s=$(screen "$id")
    if grep -qi -E 'Yes, I accept|Bypass Permissions mode' <<<"$s" && grep -q 'No, exit' <<<"$s"; then a send "$id" --raw --hex 32 >/dev/null; sleep 1; a send "$id" --raw --hex 0d >/dev/null; continue; fi
    if grep -qi -E 'trust this folder|Do you trust|Yes, proceed' <<<"$s"; then a send "$id" --raw --hex 0d >/dev/null; continue; fi
    if grep -q -E '❯|> $|for shortcuts' <<<"$s"; then ready=1; break; fi
  done
  [ "$ready" = 1 ] || { log "prompt readiness failed: $id"; return 1; }
  local prompt="/loop $LOOP_INTERVAL Run the $ROLE check: follow $RUNDIR/$SUB/hourly-check.md, starting with step 0 (skills sync, handoff, inbox)."
  [ "$ROLE" = orchestrator ] && prompt="/loop $LOOP_INTERVAL Run the orchestrator tick: follow $BRIEF (durable brief) and $RUNDIR/$SUB/CLAUDE.md; append one line to $TICKS every tick."
  if [ "${LOOP_INTERVAL}" = off ]; then log "LOOP_INTERVAL=off: scheduler is Hub scheduled prompts; no /loop issued"; return 0; fi
  a send "$id" "$prompt" --enter >/dev/null; local rc=$?
  echo "$prompt" > "$STATE/loop_command"
  log "loop issued (send rc=$rc): $prompt"
  return "$rc"
}

stop_old() {
  local ids id
  ids=$(matching_sessions all) || { log "session census failed"; return 1; }
  for id in $ids; do
    if t a kill "$id" >/dev/null 2>&1; then
      log "stopped old session $id"
    else
      log "a kill refused $id; checking for verified dead record"
      forget_dead "$id" || return 1
    fi
  done
}

make_handoff() {  # $1 = reason; keeps a hand-written handoff.md if it is newer than the last start
  local h="$REPORTS/handoff.md" ls_ts; ls_ts=$(cat "$STATE/last_start" 2>/dev/null || echo 0)
  if [ -f "$h" ] && [ "$(stat -c %Y "$h")" -gt "$ls_ts" ]; then log "using existing fresh handoff.md"; return; fi
  { echo "# Auto handoff ($(date -u +%FT%TZ)) reason: $1"
    echo "A previous $TAG session was restarted by the watchdog. Re-read CLAUDE.md, finish unprocessed inbox items, continue the loop."
    echo; echo "## Previous latest.md"; cat "$REPORTS/latest.md" 2>/dev/null || echo "(none)"
    echo; echo "## Previous NEEDS_ALEXEY.md"; cat "$REPORTS/NEEDS_ALEXEY.md" 2>/dev/null || echo "(none)"
    echo; echo "## Unprocessed inbox"; ls "$REPORTS/inbox" 2>/dev/null | grep -v '^processed$' || true
  } > "$h"
}

restart() {  # $1 = reason
  local now last; now=$(date +%s); last=$(cat "$STATE/last_start" 2>/dev/null || echo 0)
  if [ $((now-last)) -lt "$MIN_RESTART_GAP" ] && [ "${FORCE:-0}" != 1 ]; then log "restart suppressed: last start $((now-last))s ago"; return 1; fi
  make_handoff "${1:-manual}"; stop_old || return 1; start
}

restart_failed() {  # $1 = Hub report state
  local n; n=$(( $(failures) + 1 )); echo "$n" > "$STATE/restart_failures"
  log "recovery FAILED ($n consecutive)"
  if [ "$n" -ge 2 ]; then
    local alert="ops-watchdog: $TAG automatic recovery failed $n consecutive times; inspect $STATE and journalctl --user -u ops-watchdog.service"
    # Append: preserve unrelated human requests. Durable inbox works even while
    # both sessions are dead; no unsolicited keystrokes into a busy agent pane.
    printf '\n[%s] %s\n' "$(date -u +%FT%TZ)" "$alert" >> "$HOME/tmp/agent-reports/orchestrator/NEEDS_ALEXEY.md"
    printf '%s\n' "$alert" > "$HOME/tmp/agent-reports/oncall/inbox/watchdog-$ROLE-$(date +%s)-$$.md"
    log "ALERT: $alert"
  fi
  hub_report "${1:-restart_failed}"
  return 1
}

# Start recovery path $1 and arm its proof. Path 2 is a different new session: the
# fallback engine plus a fresh handoff, for a session that starts but never ticks.
recover() {  # $1 = path, $2 = reason
  local path="$1" reason="$2" saved="$ENGINE" rc=0
  [ "$path" = 2 ] && ENGINE="$FALLBACK_ENGINE"
  if FORCE=1 restart "$reason (recovery path $path, engine $ENGINE)"; then arm_proof "$path"; hub_report recovering
  else rc=1; fi
  ENGINE="$saved"
  [ "$rc" = 0 ] || restart_failed restart_failed
}

# Returns 0 when no proof is pending or it just passed; 2 while still inside the window;
# 1 after handling a failed proof (next recovery path started).
check_proof() {
  [ -f "$STATE/proof_due" ] || return 0
  local due path now ls_ts first; read -r due path < "$STATE/proof_due"; now=$(date +%s)
  ls_ts=$(cat "$STATE/last_start" 2>/dev/null || echo 0)
  if first=$(tick_in_window "$ls_ts" "${due:-0}"); then
    rm -f "$STATE/proof_due"; echo 0 > "$STATE/restart_failures"
    log "recovery proven: fresh tick $((first-ls_ts))s after start (path ${path:-1})"; return 0
  fi
  local id; id=$(find_session) || id=""
  if [ "$now" -le "${due:-0}" ] && [ -n "$id" ]; then log "recovery pending proof: session=$id, $((due-now))s left"; hub_report proof_pending; return 2; fi
  # No tick inside the window (none, or only a late one): the recovery failed its proof
  # and the next recovery path starts, as AC2 requires.
  rm -f "$STATE/proof_due"
  log "recovery proof FAILED (path ${path:-1}): no tick within ${PROOF_WINDOW}s after start${id:+ (session $id alive)}"
  restart_failed proof_failed
  local next=2; [ "${path:-1}" = 2 ] && next=1
  recover "$next" "no tick within ${PROOF_WINDOW}s after recovery path ${path:-1}" || true
  return 1
}

check() {
  mkdir -p "$HOME/tmp/agent-reports/orchestrator" "$HOME/tmp/agent-reports/oncall/inbox"
  local id
  id=$(find_session) || { log "session census failed"; restart_failed census_failed; return 1; }
  if [ "$ROLE" = orchestrator ]; then
    check_proof; case $? in 1) return 1 ;; 2) return 0 ;; esac
    id=$(find_session) || { log "session census failed"; restart_failed census_failed; return 1; }
  fi
  if [ -n "$id" ] && [ "$ROLE" = orchestrator ]; then
    # progress check: a live process is not proof of work; the tick log must be fresh
    # (grace = MAX_TICK_AGE after a start)
    local now ls_ts tk; now=$(date +%s); ls_ts=$(cat "$STATE/last_start" 2>/dev/null || echo 0)
    tk=$(tick_mtime); [ "$tk" -lt "$ls_ts" ] && tk=$ls_ts
    if [ $((now-tk)) -ge "$MAX_TICK_AGE" ]; then
      log "STALLED session=$id: no tick for $((now-tk))s"
      recover 1 "no tick for $((now-tk))s (stalled)"; return
    fi
    arm_timer deadline "$((tk + MAX_TICK_AGE - now))"
  fi
  if [ -n "$id" ]; then
    echo 0 > "$STATE/restart_failures"
    log "OK session=$id"; hub_report ok; return 0
  fi
  log "DEAD (no live worker)"
  if [ "$ROLE" = orchestrator ]; then recover 1 "session process dead/missing"; return; fi
  if restart "session process dead/missing"; then
    id=$(find_session) || id=""
    if [ -n "$id" ]; then
      echo 0 > "$STATE/restart_failures"
      log "restarted session=$id"; return 0
    fi
  fi
  restart_failed
}

case "$CMD" in
  start) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; start ;;
  ensure|check) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; check ;;
  status) id=$(find_session); [ -n "$id" ] && { log "running $id"; echo "attach: a attach $id   (or: a open $TAG --workspace $RUNDIR)"; } || { log "not running"; exit 1; } ;;
  restart) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; FORCE=1 restart "manual" ;;
  handoff-restart)  # called BY the session itself after writing handoff.md; detaches so it survives the kill
    FORCE=1 setsid nohup bash -c "sleep 5; '$0' $ROLE restart" >>"$STATE/restart.log" 2>&1 < /dev/null 9>&- & log "restart scheduled in 5s" ;;
  skills-sync) skills_sync ;;
  *) echo "unknown command $CMD" >&2; exit 64 ;;
esac
