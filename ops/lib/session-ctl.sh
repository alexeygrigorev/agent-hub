#!/usr/bin/env bash
# Shared control for the two interactive Claude ops sessions.
#   session-ctl.sh <orchestrator|oncall> <start|ensure|check|status|restart|handoff-restart|skills-sync>
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
ENGINE="${ORCH_ENGINE:-sonnet}"                          # cost config: sonnet, explicit
LOOP_INTERVAL="${LOOP_INTERVAL:-1h}"
STALE_SEC="${STALE_SEC:-7200}"                           # latest.md older than this => hung
MIN_RESTART_GAP="${MIN_RESTART_GAP:-1800}"
STATE="$REPORTS/state"; mkdir -p "$REPORTS/inbox/processed" "$REPORTS/history" "$STATE"
log() { echo "[$(date -u +%FT%TZ)] $ROLE: $*"; }
t() { timeout "${T:-60}" "$@"; }

find_session() {  # prints "id" of the running session in RUNDIR with TAG
  a list --json 2>/dev/null | python3 -c '
import json,sys
w,t=sys.argv[1:3]
for s in json.load(sys.stdin):
    if s.get("workspace")==w and s.get("tag")==t and s.get("phase")=="running":
        print(s["id"]); break' "$RUNDIR" "$TAG"
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
  prep_worktree || return 1
  [ -n "$(find_session)" ] && { log "already running: $(find_session)"; return 0; }
  date +%s > "$STATE/last_start"
  a start --workspace "$RUNDIR" --cwd "$RUNDIR/$SUB" --tag "$TAG" --engine "$ENGINE" \
    --label "ops.role=$ROLE" --label "hub.intake=$INTAKE" >/dev/null || { log "a start failed"; return 1; }
  local id; id=$(find_session); [ -n "$id" ] || { log "session not found after start"; return 1; }
  log "started session $id; waiting for prompt"
  local i s
  for i in $(seq 1 60); do
    sleep 3; s=$(screen "$id")
    if grep -qi -E 'Yes, I accept|Bypass Permissions mode' <<<"$s" && grep -q 'No, exit' <<<"$s"; then a send "$id" --raw --hex 32 >/dev/null; sleep 1; a send "$id" --raw --hex 0d >/dev/null; continue; fi
    if grep -qi -E 'trust this folder|Do you trust|Yes, proceed' <<<"$s"; then a send "$id" --raw --hex 0d >/dev/null; continue; fi
    grep -q -E '❯|> $|for shortcuts' <<<"$s" && break
  done
  local prompt="/loop $LOOP_INTERVAL Run the $ROLE check: follow $RUNDIR/$SUB/hourly-check.md, starting with step 0 (skills sync, handoff, inbox)."
  a send "$id" "$prompt" --enter >/dev/null; local rc=$?
  echo "$prompt" > "$STATE/loop_command"
  log "loop issued (send rc=$rc): $prompt"
}

stop_old() { local id; id=$(find_session); [ -n "$id" ] && { a kill "$id" >/dev/null 2>&1; sleep 3; log "killed old session $id"; }; }

make_handoff() {  # $1 = reason; keeps a hand-written handoff.md if it is newer than the last start
  local h="$REPORTS/handoff.md" ls_ts; ls_ts=$(cat "$STATE/last_start" 2>/dev/null || echo 0)
  if [ -f "$h" ] && [ "$(stat -c %Y "$h")" -gt "$ls_ts" ]; then log "using existing fresh handoff.md"; return; fi
  { echo "# Auto handoff ($(date -u +%FT%TZ)) reason: $1"
    echo "A previous $TAG session was restarted by the liveness script. Re-read CLAUDE.md, finish unprocessed inbox items, continue the loop."
    echo; echo "## Previous latest.md"; cat "$REPORTS/latest.md" 2>/dev/null || echo "(none)"
    echo; echo "## Previous NEEDS_ALEXEY.md"; cat "$REPORTS/NEEDS_ALEXEY.md" 2>/dev/null || echo "(none)"
    echo; echo "## Unprocessed inbox"; ls "$REPORTS/inbox" 2>/dev/null | grep -v '^processed$' || true
  } > "$h"
}

restart() {  # $1 = reason
  local now last; now=$(date +%s); last=$(cat "$STATE/last_start" 2>/dev/null || echo 0)
  if [ $((now-last)) -lt "$MIN_RESTART_GAP" ] && [ "${FORCE:-0}" != 1 ]; then log "restart suppressed: last start $((now-last))s ago"; return 0; fi
  make_handoff "${1:-manual}"; stop_old; start
}

check() {  # liveness: alive and not hung. Nothing else.
  local id age; id=$(find_session)
  if [ -z "$id" ]; then log "DEAD (no running session)"; restart "pane dead/missing"; return; fi
  local alive; alive=$(a status "$id" --json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("worker_alive"))' 2>/dev/null)
  [ "$alive" = "True" ] || { log "DEAD (worker not alive)"; restart "worker dead"; return; }
  local ls_ts now; now=$(date +%s); ls_ts=$(cat "$STATE/last_start" 2>/dev/null || echo 0)
  age=$(( now - $(stat -c %Y "$REPORTS/latest.md" 2>/dev/null || echo 0) ))
  if [ $((now-ls_ts)) -gt "$STALE_SEC" ] && [ "$age" -gt "$STALE_SEC" ]; then
    log "HUNG (latest.md ${age}s old, limit ${STALE_SEC}s)"; restart "hung: latest.md ${age}s old"; return
  fi
  log "OK session=$id latest.md_age=${age}s"
}

case "$CMD" in
  start) start ;;
  ensure|check) check ;;
  status) id=$(find_session); [ -n "$id" ] && { log "running $id"; echo "attach: a attach $id   (or: a open $TAG --workspace $RUNDIR)"; } || { log "not running"; exit 1; } ;;
  restart) FORCE=1 restart "manual" ;;
  handoff-restart)  # called BY the session itself after writing handoff.md; detaches so it survives the kill
    FORCE=1 setsid nohup bash -c "sleep 5; '$0' $ROLE restart" >>"$STATE/restart.log" 2>&1 < /dev/null & log "restart scheduled in 5s" ;;
  skills-sync) skills_sync ;;
  *) echo "unknown command $CMD" >&2; exit 64 ;;
esac
