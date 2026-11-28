#!/usr/bin/env bash
# Shared control for the two interactive ops sessions.
#   session-ctl.sh <orchestrator|oncall> <start|ensure|check|status|restart|handoff-restart|skills-sync|reconcile-only>
# Watchdog (check) restarts a dead or tick-stalled session and proves every restart by a fresh
# tick (intake 500fd60b); Hub scheduled prompts provide the engine-neutral scheduler.
# Needs: a (aplexer), git, gh, python3. Never prints secrets.
set -uo pipefail

ROLE="${1:-}"; CMD="${2:-status}"
case "$ROLE" in
  orchestrator) TAG=orchestrator-main; REPORTS="$HOME/tmp/agent-reports/orchestrator"; RUNDIR="$HOME/git/agent-hub-wt-orchestrator-run"; SUB=ops/orchestrator; INTAKE=67847fde-a00a-4689-8460-fbad45599f99 ;;
  oncall)       TAG=oncall-engineer;   REPORTS="$HOME/tmp/agent-reports/oncall";        RUNDIR="$HOME/git/agent-hub-wt-oncall-run";       SUB=ops/oncall;        INTAKE=67847fde-a00a-4689-8460-fbad45599f99 ;;
  *) echo "usage: $0 <orchestrator|oncall> <start|ensure|check|status|restart|handoff-restart|skills-sync|reconcile-only>" >&2; exit 64 ;;
esac
INTAKE="${OPS_INTAKE:-$INTAKE}"  # explicit owner-selected intake; default remains the existing role contract
BASE_REPO="${BASE_REPO:-$HOME/git/agent-hub}"           # donor clone for the run worktree
[ -f "$HOME/.local/share/agent-hub-ops/guard-bin/env.sh" ] && . "$HOME/.local/share/agent-hub-ops/guard-bin/env.sh"  # agent rm guard (504d662b): sessions inherit it via --setenv=PATH
SKILLS_REPO="${SKILLS_REPO:-$HOME/git/bots-management-ops}"   # read-only clone of alexeygrigorev/bots-management (bot-skills is archived)
SKILLS_REMOTE="${SKILLS_REMOTE:-alexeygrigorev/bots-management}"
SKILLS_DIR="${SKILLS_DIR:-$SKILLS_REPO/.agents/skills}"   # skills live under .agents/skills
if [ "$ROLE" = oncall ]; then
  # The persistent oncall coordinator prefers the daily Muse model on OpenCode Go,
  # and falls back to another configured non-Claude/non-Astra engine (e.g. Antigravity
  # Hydrogen Medium) when OpenCode Go has no quota headroom.
  ENGINE="${ONCALL_ENGINE:-${ENGINE:-opencode}}"
  FALLBACK_ENGINE="${ONCALL_FALLBACK_ENGINE:-antigravity}"
else
  ENGINE="${ENGINE:-${ORCH_ENGINE:-antigravity}}"
  FALLBACK_ENGINE="${ORCH_FALLBACK_ENGINE:-antigravity}"  # second recovery path prefers Ag; selection excludes the failed route
fi
TICKS="$HOME/tmp/agent-reports/orchestrator-ticks.jsonl"; MAX_TICK_AGE="${MAX_TICK_AGE:-1800}"
BRIEF="$HOME/tmp/briefs/orchestrator-main.md"
MIN_RESTART_GAP="${MIN_RESTART_GAP:-600}"
PROOF_WINDOW="${PROOF_WINDOW:-600}"                      # a restart must produce a fresh tick within 10 min
MAX_ONCALL_INBOX_AGE="${MAX_ONCALL_INBOX_AGE:-1200}"
ENGINE_EXCLUDE=""
ENGINE_SELECTED="${ENGINE_SELECTED:-0}"
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
failures() {
  local n=0
  if [ -r "$STATE/restart_failures" ]; then read -r n < "$STATE/restart_failures" || n=0; fi
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  echo "$n"
}

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
  local started due engine="${2:-$ENGINE}"
  started=$(cat "$STATE/last_start" 2>/dev/null || date +%s); due=$((started + PROOF_WINDOW))
  echo "$due $1 $engine" > "$STATE/proof_due"
  arm_timer proof "$((due - $(date +%s) + 1))"
  log "recovery path $1 started on $engine; proof due: fresh tick within ${PROOF_WINDOW}s"
}

# List failures are errors, never evidence that no session exists.
matching_sessions() {
  if [ -e "$STATE/managed/current.json" ] || [ -L "$STATE/managed/current.json" ]; then
    managed inspect; return $?
  fi
  # Explicit legacy READ only; this does not adopt a Hub task by friendly tag.
  t a list --json | python3 -c '
import json,sys
w,t,live=sys.argv[1:4]
prefix=w.rstrip("/")+"/"
for s in json.load(sys.stdin):
    ws=s.get("workspace") or ""
    if (ws==w or ws.startswith(prefix)) and s.get("tag")==t:
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

# Every scheduled check runs skills-sync first (hourly-check.md step 0). Move an
# existing clean run checkout to origin/main too, so instruction fixes (e.g. the
# DataOps credential routing, intake fc36c32b) reach a session that is already
# running instead of waiting for the next restart. A dirty checkout is kept.
refresh_rundir() {
  [ -e "$RUNDIR/.git" ] || return 0
  T=120 t git -C "$BASE_REPO" fetch -q origin main || log "fetch failed (using local origin/main)"
  local before after
  before=$(git -C "$RUNDIR" rev-parse --short HEAD 2>/dev/null)
  git -C "$RUNDIR" checkout -q --detach origin/main 2>/dev/null || { log "worktree not clean; keeping current checkout"; return 0; }
  after=$(git -C "$RUNDIR" rev-parse --short HEAD 2>/dev/null)
  [ "$before" = "$after" ] || log "run checkout updated $before -> $after"
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

screen() {
  if [ -e "$STATE/managed/current.json" ] || [ -L "$STATE/managed/current.json" ]; then
    managed capture "$1"; return $?
  fi
  a capture "$1" --screen --plain 2>/dev/null
}

# Prefer a configured route whose Hub quota is fresh and above its provider
# reserve; when Hub quota is unavailable, use the locally configured engine
# list. Oncall has a narrower allowlist and a pinned model so a recovery cannot
# inherit Antigravity's Argon default or pick an unknown high-effort model.
quota_engine() {  # $1 preferred, $2 excluded route, $3 fallback preference
  local quota engines
  quota=$(HUB_CONFIG_FILE="${HUB_CONFIG_FILE:-$HOME/.config/agent-hub/cli.json}" T=30 t hub quota --json 2>/dev/null) || return 2
  engines=$(T=15 t a engines --json 2>/dev/null) || return 2
  printf '%s\n%s\n' "$quota" "$engines" | python3 -c '
import json, math, sys

preferred, excluded, fallback, role = sys.argv[1:5]
raw = sys.stdin.read()
decoder = json.JSONDecoder()
try:
    first = raw.lstrip()
    quota, end = decoder.raw_decode(first)
    engines_doc, _ = decoder.raw_decode(first[end:].lstrip())
except Exception:
    sys.exit(2)
if not isinstance(quota, dict) or "error" in quota or not isinstance(quota.get("pools"), list):
    # A 503 release_bootstrap stub and other unavailable/malformed responses do
    # not prove quota exhaustion. Let select_engine use the local engine list.
    sys.exit(2)

# Hub currently labels the Google quota pool "gemini". That is quota metadata;
# the only Google-family engine this watchdog selects is Antigravity.
allowed = {"antigravity": "gemini", "grok": "grok",
           "opencode": "go", "zcodex": "zai"}
aliases = {"gemini": "antigravity"}
def normalize_engine(value):
    value = value.strip().lower()
    return aliases.get(value, value)

configured = set()
def collect(value):
    if isinstance(value, dict):
        for key in ("name", "id", "engine"):
            item = value.get(key)
            if isinstance(item, str):
                configured.add(normalize_engine(item))
        for child in value.values():
            if isinstance(child, (dict, list)):
                collect(child)
    elif isinstance(value, list):
        for child in value:
            collect(child)
collect(engines_doc)

pools = quota.get("pools", []) if isinstance(quota, dict) else []
routes = {engine: [] for engine in allowed}
for pool in pools if isinstance(pools, list) else []:
    if not isinstance(pool, dict) or pool.get("routing") == "disabled":
        continue
    names = []
    for key in ("harness", "engines"):
        value = pool.get(key)
        if isinstance(value, list):
            names.extend(value)
    for value in names:
        if isinstance(value, str) and normalize_engine(value) in allowed:
            engine = normalize_engine(value)
            provider = pool.get("provider")
            if isinstance(provider, str) and provider.lower() == allowed[engine] and pool not in routes[engine]:
                routes[engine].append(pool)

def has_headroom(engine, pool):
    if pool.get("state") != "fresh" or pool.get("routing") == "disabled":
        return False
    # `depleted` describes burn pace, not exhausted quota. A fresh pool with
    # positive remaining quota is still usable even when usage is ahead of
    # its linear schedule; `limit_reached` and the remaining reserve are the
    # exhaustion signals for recovery routing.
    if pool.get("limit_reached") is True:
        return False
    remaining = pool.get("remaining_percent")
    if remaining is None and (pool.get("burn_status") == "unmetered" or pool.get("source") == "unmetered-engine"):
        return True
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
        return False
    reserve = 5 if allowed[engine] == "grok" else 0
    return remaining > reserve

order = []
default_order = ("opencode", "antigravity", "grok", "zcodex") if role == "oncall" else ("antigravity", "opencode", "grok", "zcodex")
for candidate in (preferred, fallback, *default_order):
    candidate = normalize_engine(candidate)
    if candidate in allowed and candidate not in order:
        order.append(candidate)
excluded = normalize_engine(excluded)
for candidate in order:
    if candidate == excluded or candidate not in configured:
        continue
    matching = routes.get(candidate, [])
    # Provider families can contain multiple account pools. Any usable pool
    # is sufficient for routing to that engine.
    if matching and any(has_headroom(candidate, pool) for pool in matching):
        print(candidate)
        sys.exit(0)
sys.exit(1)
' "$1" "$2" "$3" "$ROLE"
}

local_engine() {  # $1 preferred, $2 excluded route, $3 fallback preference, $4 exact-only
  local engines
  engines=$(T=15 t a engines --json 2>/dev/null) || return 1
  printf '%s\n' "$engines" | python3 -c '
import json, sys

preferred, excluded, fallback, exact, role = sys.argv[1:6]
allowed = {"antigravity", "grok", "opencode", "zcodex"}
aliases = {"gemini": "antigravity"}
def normalize_engine(value):
    value = value.strip().lower()
    return aliases.get(value, value)

try:
    engines_doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)

configured = set()
def collect(value):
    if isinstance(value, dict):
        for key in ("name", "id", "engine"):
            item = value.get(key)
            if isinstance(item, str):
                configured.add(normalize_engine(item))
        for child in value.values():
            if isinstance(child, (dict, list)):
                collect(child)
    elif isinstance(value, list):
        for child in value:
            collect(child)
collect(engines_doc)

preferred = normalize_engine(preferred)
if exact == "1":
    order = [preferred]
else:
    order = []
    default_order = ("opencode", "antigravity", "grok", "zcodex") if role == "oncall" else ("antigravity", "opencode", "grok", "zcodex")
    for candidate in (preferred, fallback, *default_order):
        candidate = normalize_engine(candidate)
        if candidate in allowed and candidate not in order:
            order.append(candidate)

excluded = normalize_engine(excluded)
# Prefer a different route after a failed recovery proof. If the only local
# engine is the excluded one, still recover instead of holding indefinitely.
for allow_excluded in (False, True):
    for candidate in order:
        if candidate not in allowed or candidate not in configured:
            continue
        if candidate == excluded and not allow_excluded:
            continue
        print(candidate)
        sys.exit(0)
sys.exit(1)
' "$1" "$2" "$3" "$4" "$ROLE"
}

enforce_oncall_route() {
  [ "$ROLE" = oncall ] || return 0
  case "$ENGINE" in
    opencode|antigravity|grok|zcodex) ;;
    *)
      log "oncall route '$ENGINE' is disallowed; using OpenCode Muse Spark"
      ENGINE=opencode
      ENGINE_SELECTED=0
      ;;
  esac
  case "$FALLBACK_ENGINE" in
    opencode|antigravity|grok|zcodex) ;;
    *) FALLBACK_ENGINE=antigravity ;;
  esac
}

select_engine() {
  local selected quota_status
  enforce_oncall_route
  if [ "$ENGINE_SELECTED" = 1 ]; then
    selected=$(local_engine "$ENGINE" "" "" 1) || {
      log "preselected engine is not a configured non-Claude aplexer engine; recovery held"
      return 1
    }
    ENGINE="$selected"
    return 0
  fi
  selected=$(quota_engine "$ENGINE" "$ENGINE_EXCLUDE" "$FALLBACK_ENGINE")
  quota_status=$?
  if [ "$quota_status" -eq 2 ]; then
    selected=$(local_engine "$ENGINE" "$ENGINE_EXCLUDE" "$FALLBACK_ENGINE" 0) || {
      log "Hub quota unavailable and no configured non-Claude aplexer engine is available; recovery held"
      return 1
    }
    log "Hub quota unavailable; using local engine $selected"
  elif [ "$quota_status" -ne 0 ]; then
    log "no configured non-Claude engine has fresh Hub quota headroom; recovery held"
    return 1
  elif [ "$selected" != "$ENGINE" ]; then
    log "engine preference unavailable; Hub quota selected $selected"
  fi
  ENGINE="$selected"
  ENGINE_SELECTED=1
}

usage_limit_screen() {
  local captured
  captured=$(screen "$1") || return 1
  grep -qiE 'hit your weekly limit|weekly[[:space:]]+limit|limit to reset|usage limit' <<<"$captured"
}

oldest_oncall_inbox_item() {
  find "$REPORTS/inbox" -mindepth 1 -maxdepth 1 -type f -name '*.md' -printf '%T@\n' 2>/dev/null | sort -n | head -1 | cut -d. -f1
}

oncall_inbox_stalled() {
  [ "$ROLE" = oncall ] || return 1
  [[ "$MAX_ONCALL_INBOX_AGE" =~ ^[0-9]+$ ]] || return 1
  local oldest ls_ts now
  oldest=$(oldest_oncall_inbox_item)
  [[ "$oldest" =~ ^[0-9]+$ ]] || return 1
  ls_ts=$(cat "$STATE/last_start" 2>/dev/null || echo 0)
  [[ "$ls_ts" =~ ^[0-9]+$ ]] || ls_ts=0
  [ "$oldest" -lt "$ls_ts" ] && oldest=$ls_ts
  now=$(date +%s)
  [ $((now-oldest)) -ge "$MAX_ONCALL_INBOX_AGE" ]
}

start() {
  local current prompt
  prompt="Run the $ROLE check now: follow $RUNDIR/$SUB/hourly-check.md, starting with step 0 (skills sync, handoff, inbox). Hub scheduled prompts continue the recurring work for this role."
  [ "$ROLE" = orchestrator ] && prompt="Run the orchestrator tick now: follow $BRIEF (durable brief) and $RUNDIR/$SUB/CLAUDE.md; append one line to $TICKS. Hub scheduled prompts continue the recurring work for this role."
  if [ -e "$STATE/managed/current.json" ] || [ -L "$STATE/managed/current.json" ]; then
    # A prior possible submission is reconciled only by its original exact ID;
    # start never repeats that POST or renews the generation's progress clock.
    printf '%s' "$prompt" | managed start
    return $?
  fi
  current=$(find_session) || { log "legacy session census unconfirmed; no start"; return 1; }
  [ -z "$current" ] || { log "already running legacy session: $current; owner handoff required for adoption"; return 0; }
  prep_worktree || return 1
  printf '%s' "$prompt" | managed start || { log "Hub startup/delivery unconfirmed; request retained"; return 1; }
  echo "$prompt" > "$STATE/loop_command"
  log "Hub-managed startup brief delivered; Hub schedules provide recurring work"
}

stop_old() {
  if [ -e "$STATE/managed/current.json" ] || [ -L "$STATE/managed/current.json" ]; then
    managed stop || { log "owned STOP unconfirmed; no replacement"; return 1; }
    return 0
  fi
  local ids
  ids=$(matching_sessions all) || { log "legacy session census failed"; return 1; }
  [ -z "$ids" ] || { log "unmanaged legacy role requires acknowledged owner retirement; no automatic kill/adoption"; return 1; }
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
recover() {  # $1 = path, $2 = reason, $3 = engine whose proof failed (optional)
  local path="$1" reason="$2" failed_engine="${3:-}" saved="$ENGINE" saved_exclude="$ENGINE_EXCLUDE" saved_selected="$ENGINE_SELECTED" rc=0
  if [ "$path" = 2 ]; then
    ENGINE="$FALLBACK_ENGINE"
    # Older proof records contain only due + path; preserve their preference-based exclusion.
    ENGINE_EXCLUDE="${failed_engine:-$saved}"
  else
    ENGINE_EXCLUDE="$failed_engine"
  fi
  ENGINE_SELECTED=0
  if FORCE=1 restart "$reason (Hub-managed recovery path $path)"; then arm_proof "$path" hub; hub_report recovering
  else rc=1; fi
  ENGINE="$saved"; ENGINE_EXCLUDE="$saved_exclude"; ENGINE_SELECTED="$saved_selected"
  [ "$rc" = 0 ] || restart_failed restart_failed
}

# Returns 0 when no proof is pending or it just passed; 2 while still inside the window;
# 1 after handling a failed proof (next recovery path started).
check_proof() {
  [ -f "$STATE/proof_due" ] || return 0
  local due path failed_engine now ls_ts first; read -r due path failed_engine < "$STATE/proof_due"; now=$(date +%s)
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
  recover "$next" "no tick within ${PROOF_WINDOW}s after recovery path ${path:-1}" "$failed_engine" || true
  return 1
}

check() {
  mkdir -p "$HOME/tmp/agent-reports/orchestrator" "$HOME/tmp/agent-reports/oncall/inbox"
  local id
  if [ -e "$STATE/managed/current.json" ] || [ -L "$STATE/managed/current.json" ]; then
    # Finish only the original keyed brief after transient binding/ACK loss;
    # this action cannot allocate a request or submit any start.
    managed resume >/dev/null
    local managed_status=$?
    if [ "$managed_status" = 10 ]; then
      log "owned terminal Hub session; qualified STOP required before replacement"
      recover 1 "owned terminal Hub session"; return
    fi
    [ "$managed_status" = 0 ] || { log "managed reconciliation unconfirmed"; restart_failed census_failed; return 1; }
  fi
  id=$(find_session) || { log "session census failed"; restart_failed census_failed; return 1; }
  if [ -n "$id" ] && usage_limit_screen "$id"; then
    log "STALLED session=$id: provider usage limit screen"
    recover 1 "provider usage limit screen"; return
  fi
  if [ -n "$id" ] && oncall_inbox_stalled; then
    local oldest; oldest=$(oldest_oncall_inbox_item)
    log "STALLED session=$id: oncall inbox has an unprocessed item aged $(( $(date +%s)-oldest ))s"
    recover 1 "oncall inbox item older than ${MAX_ONCALL_INBOX_AGE}s"; return
  fi
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

# One lifecycle implementation shared by startup, watchdog and inbox delivery.
# Absence of this ledger permits legacy READ/status only, never implicit adoption.
managed() {
  python3 - "$ROLE" "$STATE" "$RUNDIR/$SUB" "$INTAKE" "$TAG" "$@" 3<&0 9>&- <<'PY_MANAGED'
import hashlib,json,os,pathlib,re,stat,subprocess,sys,time,uuid,select
role,state,requested,intake,name,action,*arguments=sys.argv[1:]
root=pathlib.Path(state)/'managed'; current=root/'current.json'; project='alexeygrigorev/agent-hub'
deadline=time.monotonic()+(60 if action=='stop' else 150)
class Unknown(Exception):pass
def require(value):
    if not value:raise Unknown()
def uid(value):
    require(type(value) is str and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',value));return value
def stable_id(prefix,key):
    h=hashlib.sha256(json.dumps([prefix,project,intake,key],separators=(',',':')).encode()).hexdigest()
    return h[:8]+'-'+h[8:12]+'-5'+h[13:16]+'-a'+h[17:20]+'-'+h[20:32]
def trusted_parents():
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in pathlib.Path(state).parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
            st=os.fstat(fd);require(st.st_uid in (0,os.getuid()) and not st.st_mode&0o022)
    finally:os.close(fd)
def private(path,directory=False):
    s=path.lstat();require(not stat.S_ISLNK(s.st_mode) and s.st_uid==os.getuid())
    require(stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode) and s.st_nlink==1)
    require(stat.S_IMODE(s.st_mode)==(0o700 if directory else 0o600))
def syncdir():
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
def exclusive(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    except BaseException:raise
    syncdir()
def save(record):
    temporary=root/('.write-'+uuid.uuid4().hex)
    exclusive(temporary,(json.dumps(record,sort_keys=True)+'\n').encode())
    if os.path.lexists(current):private(current)
    os.replace(temporary,current);syncdir()
def load():
    private(root,True);private(current);require(current.stat().st_size<=32768)
    r=json.loads(current.read_bytes());require(type(r) is dict and r.get('v')==1)
    require((r.get('role'),r.get('intake'),r.get('requested'),r.get('project'))==(role,intake,requested,project))
    require(type(r.get('key')) is str and re.fullmatch(r'ops-[a-z]+-[0-9a-f]{32}',r['key']))
    require(r.get('id')==stable_id('hub2-start-session-v1',r['key']))
    require(r.get('operation')==stable_id('hub2-start-operation-v1',r['key']))
    return r
def call(argv,timeout=20):
    remaining=deadline-time.monotonic();require(remaining>0)
    try:p=subprocess.run(['hub',*argv],capture_output=True,timeout=min(timeout,remaining),check=False)
    except (OSError,subprocess.TimeoutExpired):raise Unknown() from None
    require(p.returncode==0 and len(p.stdout)<=65536)
    try:
        result=json.loads(p.stdout);require(type(result) is dict);return result
    except (ValueError,UnicodeError):raise Unknown() from None
def view(r):
    result=call(['sessions','show',r['id']]);s=result.get('session',result)
    require(type(s) is dict and s.get('id')==r['id'] and s.get('intake_id')==intake and s.get('project_id')==r['project'])
    require(s.get('operation_id')==r['operation'] and s.get('requested_workspace')==requested)
    require(s.get('managed') is True and type(s.get('epoch')) is int and s['epoch']>=1)
    machine=s.get('machine_id');require(machine==r.get('target_machine'));require(type(machine) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',machine))
    resolved=s.get('workspace');require(type(resolved) is str and len(resolved)<=1024 and resolved.startswith('/') and not any(ord(c)<32 for c in resolved))
    digest=hashlib.sha256(json.dumps({'intake_id':intake,'project_id':r['project']},sort_keys=True).encode()).hexdigest()
    require(resolved==requested or re.fullmatch(r'/[^\0]*?/\.local/hub-worktrees/'+digest,resolved))
    require(not any(x in ('.','..') for x in resolved.split('/')))
    native=s.get('native_id');require(native is None or uid(native))
    candidate={'machine':machine,'epoch':s['epoch'],'native':native,'resolved':resolved}
    assignment={'machine':machine,'epoch':s['epoch']}
    if r.get('assignment') is not None:require(r['assignment']==assignment)
    else:r['assignment']=assignment;save(r)
    bound=r.get('binding')
    if bound is not None:require(bound==candidate)
    elif s.get('state')=='running':
        uid(native);r['binding']=candidate;save(r)
    return s
def read_body():
    mode=os.fstat(3).st_mode;require(stat.S_ISFIFO(mode) or stat.S_ISREG(mode))
    body=bytearray()
    while True:
        remaining=deadline-time.monotonic();require(remaining>0)
        ready,_,_=select.select([3],[],[],remaining);require(ready)
        part=os.read(3,min(4096,16385-len(body)))
        if not part:break
        body.extend(part);require(len(body)<=16384)
    require(body);bytes(body).decode('utf8');return bytes(body)
def brief(r):
    f=root/(r['key']+'.brief');private(f);raw=f.read_bytes();require(len(raw)<=16384 and hashlib.sha256(raw).hexdigest()==r['brief_sha256']);return raw
def deliver(r,body,key):
    s=view(r);require(s.get('state')=='running' and r.get('binding') is not None)
    require(type(key) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}',key))
    # Body is private stdin, not a command argument or a log field.
    remaining=deadline-time.monotonic();require(remaining>0)
    try:p=subprocess.run(['hub','sessions','send',r['id'],'--body-file','-','--key',key,'--wait','--timeout',str(min(30,remaining))],input=body,capture_output=True,timeout=min(35,remaining))
    except (OSError,subprocess.TimeoutExpired):raise Unknown() from None
    require(p.returncode==0 and len(p.stdout)<=65536)
    result=json.loads(p.stdout);receipt=result.get('command_wait') or {}
    require(receipt.get('state')=='acked' and (receipt.get('ack') or {}).get('outcome')=='delivered')
    view(r)
try:
    trusted_parents()
    if action=='start':
        prompt=read_body()
        if not root.exists():root.mkdir(mode=0o700)
        private(root,True)
        if os.path.lexists(current):
            r=load()
            if r.get('retired') is True:
                exclusive(root/(r['key']+'.retired.json'),(json.dumps(r,sort_keys=True)+'\n').encode())
                current.unlink();syncdir()
            else:require(r.get('retired') is False)
        if not os.path.lexists(current):
            target=os.environ.get('OPS_HUB_MACHINE','hetzner-aplexer');require(type(target) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',target))
            key='ops-'+role+'-'+uuid.uuid4().hex
            exclusive(root/(key+'.brief'),prompt)
            r={'target_machine':target,'started_at':int(time.time()),'v':1,'role':role,'intake':intake,'requested':requested,'project':project,'key':key,'id':stable_id('hub2-start-session-v1',key),'operation':stable_id('hub2-start-operation-v1',key),'submitted':False,'retired':False,'binding':None,'assignment':None,'brief_sha256':hashlib.sha256(prompt).hexdigest(),'brief_sent':False,'stop_requested':False}
            save(r)
            marker=pathlib.Path(state)/('.last_start-'+key)
            fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(str(r['started_at'])+'\n');stream.flush();os.fsync(stream.fileno())
            os.replace(marker,pathlib.Path(state)/'last_start')
            parent=os.open(state,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
        r=load()
        if r['submitted'] is False:
            r['submitted']=True;save(r) # durable attempt BEFORE any possible start
            try:call(['sessions','start','--repo',project,'--workspace',requested,'--name',name,'--machine',r['target_machine'],'--interactive','--purpose','implement','--intake-task-id',intake,'--key',r['key'],'--no-wait'])
            except Unknown:pass # read-only keyed reconciliation, never submit again
        while True:
            s=view(r)
            if s.get('state')=='running':break
            require(s.get('state') in ('queued','offered','starting') and deadline-time.monotonic()>1)
            time.sleep(min(1,deadline-time.monotonic()))
        if not r['brief_sent']:
            deliver(r,brief(r),r['key']+'-brief');r['brief_sent']=True;save(r)
        print(r['binding']['native'])
    elif action in ('inspect','reconcile','resume'):
        r=load()
        if r['retired']:sys.exit(0)
        s=view(r)
        if action=='resume' and s.get('state') in ('failed','stopped','completed','expired'):
            # Typed control result only: a terminal row is NOT retirement proof.
            # The watchdog must still obtain a fresh owned qualified Hub STOP.
            sys.exit(10)
        require(s.get('state')=='running' and r.get('binding') is not None)
        if action=='resume' and not r['brief_sent']:
            deliver(r,brief(r),r['key']+'-brief');r['brief_sent']=True;save(r)
        if action=='inspect':require(r.get('brief_sent') is True)
        print(r['binding']['native'])
    elif action=='send':
        r=load();require(not r['retired']);body=read_body()
        deliver(r,body,arguments[0]);print('delivered')
    elif action=='capture':
        r=load();s=view(r);require(s.get('state')=='running' and r.get('binding') is not None and r['binding']['native']==arguments[0])
        result=call(['sessions','capture',r['id'],'--screen','--max-bytes','16384','--timeout','20'],25)
        receipt=result.get('command_wait') or {};ack=receipt.get('ack') or {};require(receipt.get('state')=='acked' and ack.get('outcome')=='delivered')
        text=ack.get('capture');require(type(text) is str and len(text.encode())<=16384);print(text)
    elif action=='stop':
        r=load()
        if r['retired']:sys.exit(0)
        s=view(r);require(r.get('binding') is not None or s.get('native_id') is None)
        r['stop_requested']=True;save(r)
        result=call(['sessions','stop',r['id'],'--key',r['key']+'-stop','--wait','--timeout',str(max(0.01,deadline-time.monotonic()))],60)
        stopped=result.get('stop_wait') or {};require(stopped.get('id')==r['id'] and (stopped.get('state')=='stopped' or stopped.get('stop_confirmed') is True))
        after=view(r);require((after.get('state')=='stopped' or after.get('stop_confirmed') is True) and after.get('state') in ('stopped','failed'))
        r['retired']=True;r['stopped_native']=after.get('native_id');save(r)
    else:raise Unknown()
except (Unknown,OSError,ValueError,TypeError,KeyError):
    print('managed_outcome_unconfirmed; retain owned request and reconcile, no native fallback',file=sys.stderr);sys.exit(1)
PY_MANAGED
}

case "$CMD" in
  start) role_lock || exit 75; start ;;
  ensure|check) role_lock || exit 75; check ;;
  status) role_lock || exit 75; id=$(find_session); [ -n "$id" ] && { log "running $id"; echo "attach: a attach $id   (or: a open $TAG --workspace $RUNDIR)"; } || { log "not running"; exit 1; } ;;
  restart) role_lock || exit 75; FORCE=1 restart "manual" ;;
  handoff-restart)  # called BY the session itself after writing handoff.md; detaches so it survives the kill
    FORCE=1 setsid nohup bash -c "sleep 5; '$0' $ROLE restart" >>"$STATE/restart.log" 2>&1 < /dev/null 9>&- & log "restart scheduled in 5s" ;;
  reconcile-only) role_lock || exit 75; managed reconcile ;;
  managed-send) role_lock || exit 75; managed send "${3:-}" ;;
  skills-sync) refresh_rundir; skills_sync ;;
  *) echo "unknown command $CMD" >&2; exit 64 ;;
esac
