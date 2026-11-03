#!/usr/bin/env bash
# Shared control for the two interactive Claude ops sessions.
#   session-ctl.sh <orchestrator|oncall> <start|ensure|check|status|restart|handoff-restart|skills-sync>
# Watchdog (check) only restarts a dead session. The in-session /loop is the scheduler.
# Needs: a (aplexer), git, gh, python3. Never prints secrets.
set -uo pipefail

ROLE="${1:-}"; CMD="${2:-status}"
case "$ROLE" in
  orchestrator) TAG=orchestrator-main; REPORTS="$HOME/tmp/agent-reports/orchestrator"; RUNDIR="$HOME/git/agent-hub-wt-orchestrator-run"; SUB=ops/orchestrator; INTAKE=67847fde-a00a-4689-8460-fbad45599f99 ;;
  oncall)       TAG=oncall-engineer;   REPORTS="$HOME/tmp/agent-reports/oncall";        RUNDIR="$HOME/git/agent-hub-wt-oncall-run";       SUB=ops/oncall;        INTAKE=67847fde-a00a-4689-8460-fbad45599f99 ;;
  *) echo "usage: $0 <orchestrator|oncall> <start|ensure|check|status|restart|handoff-restart|skills-sync>" >&2; exit 64 ;;
esac
BASE_REPO="${BASE_REPO:-$HOME/git/agent-hub}"           # donor clone for the run worktree
[ -f "$HOME/.local/share/agent-hub-ops/guard-bin/env.sh" ] && . "$HOME/.local/share/agent-hub-ops/guard-bin/env.sh"  # agent rm guard (504d662b): sessions inherit it via --setenv=PATH
SKILLS_REPO="${SKILLS_REPO:-$HOME/git/bots-management-ops}"   # read-only clone of alexeygrigorev/bots-management (bot-skills is archived)
SKILLS_REMOTE="${SKILLS_REMOTE:-alexeygrigorev/bots-management}"
SKILLS_DIR="${SKILLS_DIR:-$SKILLS_REPO/.agents/skills}"   # skills live under .agents/skills
ENGINE=sonnet
LOOP_DEFAULT=1h
LOOP_INTERVAL="${LOOP_INTERVAL:-$LOOP_DEFAULT}"
BRIEF="$HOME/tmp/briefs/orchestrator-main.md"
STATE="$REPORTS/state"; mkdir -p "$REPORTS/inbox/processed" "$REPORTS/history" "$STATE"
log() { echo "[$(date -u +%FT%TZ)] $ROLE: $*"; }
# fd 9 holds the per-role lock (role_lock). No child may inherit it: a detached aplexer worker,
# git daemon or session would keep the lock for its lifetime and block every later check.
t() { timeout "${T:-60}" "$@" 9>&-; }
a() { command a "$@" 9>&-; }
git() { command git "$@" 9>&-; }
gh() { command gh "$@" 9>&-; }
LOCK_DIR="${OPS_LOCK_DIR:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"
role_lock() {
  exec 9>"$LOCK_DIR/ops-ctl-$ROLE.lock" || return 1
  flock -w "${ROLE_LOCK_WAIT:-300}" 9 || { log "role lock busy for ${ROLE_LOCK_WAIT:-300}s; next timer retries"; return 1; }
}

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
    T=180 t gh repo clone "$SKILLS_REMOTE" "$SKILLS_REPO" >/dev/null 2>&1 || { log "skills clone FAILED"; return 1; }
  fi
  T=90 t git -C "$SKILLS_REPO" pull --ff-only -q 2>&1 | tail -2 || log "skills pull failed (using last checkout)"
  # link every skill dir into the session's .claude/skills so Claude Code auto-discovers it
  local d="$RUNDIR/.claude/skills" n=0
  [ -d "$RUNDIR" ] || return 0
  mkdir -p "$d"
  for s in "$SKILLS_DIR"/*/; do
    s="${s%/}"; [ -f "$s/SKILL.md" ] || continue
    ln -sfn "$s" "$d/$(basename "$s")"; n=$((n+1))
  done
  grep -qxF '.claude/skills' "$(git -C "$RUNDIR" rev-parse --git-path info/exclude)" 2>/dev/null || \
    echo '.claude/skills' >> "$(git -C "$RUNDIR" rev-parse --git-path info/exclude)"
  log "skills synced: $n linked from $SKILLS_DIR @ $(git -C "$SKILLS_REPO" rev-parse --short HEAD)"
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

quota_gate() {
  T=45 t hub quota --provider claude --json 2>/dev/null | python3 -c '
import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not data.get("coverage",{}).get("fresh"):
    sys.exit(1)
for pool in data.get("pools",[]):
    if not isinstance(pool,dict) or pool.get("provider") != "claude":
        continue
    remaining=pool.get("remaining_percent")
    if (pool.get("depleted") is False and isinstance(remaining,(int,float))
            and not isinstance(remaining,bool) and remaining > 0
            and pool.get("state") not in {"unknown","stale","unavailable","exhausted"}):
        sys.exit(0)
sys.exit(1)'
}

launch_gate() {
  quota_gate || { log "Claude quota gate unavailable or exhausted; refusing to start Sonnet (no credits)"; return 1; }
  local hostload
  hostload=$(bash "$SKILLS_DIR/hub-sessions/hostload.sh" 2>/dev/null) || { log "host capacity check unavailable; refusing launch"; return 1; }
  [[ "$hostload" == *"verdict=free"* ]] || { log "host CPU capacity gate refused launch"; return 1; }
  awk '$1 == "MemAvailable:" { found=1; if ($2 < 10485760) exit 1 } END { if (!found) exit 1 }' /proc/meminfo || {
    log "MemAvailable is below 10 GiB or unavailable; refusing launch"; return 1;
  }
  python3 - "$BASE_REPO/config/resource-management.json" "$RUNDIR" <<'PY' || {
import json,os,sys
from pathlib import Path
try:
    config=json.load(open(sys.argv[1],encoding="utf-8"))
    floor=config.get("disk_floor_gb")
    if isinstance(floor,bool) or not isinstance(floor,int) or floor < 1:
        raise ValueError("invalid disk floor")
    probe=Path(sys.argv[2])
    while not probe.exists() and probe != probe.parent:
        probe=probe.parent
    stat=os.statvfs(probe)
    available=stat.f_bavail * stat.f_frsize
    if available < floor * 1024**3:
        sys.exit(1)
except Exception:
    sys.exit(1)
PY
    log "configured disk floor is missing, invalid, or not met; refusing launch"; return 1;
  }
}

start() {
  local current
  current=$(find_session) || { log "session census failed"; return 1; }
  if [ -n "$current" ] && [ "${RESTART_FORCE:-0}" != 1 ]; then log "already running: $current"; return 0; fi
  launch_gate || return 1
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

restart() {  # $1 = reason; called only by an explicit handoff or dead-session check
  make_handoff "${1:-manual}"; RESTART_FORCE=1 start
}

check() {
  local id
  id=$(find_session) || { log "session census failed; no recovery attempted"; return 1; }
  if [ -n "$id" ]; then
    log "OK session=$id (live process; /loop owns cadence)"
    return 0
  fi
  log "DEAD (no live process); restarting from handoff"
  restart "session process dead/missing" || { log "restart failed; see this service journal"; return 1; }
  id=$(find_session) || { log "session census failed after restart"; return 1; }
  [ -n "$id" ] || { log "restart completed without a verified live session"; return 1; }
  log "restarted session=$id"
}

case "$CMD" in
  start) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; start ;;
  ensure|check) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; check ;;
  status) id=$(find_session); [ -n "$id" ] && { log "running $id"; echo "attach: a attach $id   (or: a open $TAG --workspace $RUNDIR)"; } || { log "not running"; exit 1; } ;;
  restart) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; restart "manual" ;;
  handoff-restart)  # called BY the session itself after writing handoff.md; detaches so it survives the kill
    setsid nohup bash -c "sleep 5; '$0' $ROLE restart" >>"$STATE/restart.log" 2>&1 < /dev/null 9>&- & log "restart scheduled in 5s" ;;
  skills-sync) skills_sync ;;
  *) echo "unknown command $CMD" >&2; exit 64 ;;
esac
