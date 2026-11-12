#!/usr/bin/env bash
# Shared control for the two interactive ops sessions.
#   session-ctl.sh <orchestrator|oncall> <start|ensure|check|status|session-id|restart|handoff-restart|skills-sync>
# Watchdog (check) restarts a dead or tick-stalled session and proves every restart by a fresh
# tick (intake 500fd60b); Hub scheduled prompts provide the engine-neutral scheduler.
# Needs: a (aplexer), hub (maintained Hub CLI), git, gh, python3. Never prints secrets.
# Coordinators start only through `hub sessions start`; there is no direct native start path.
set -uo pipefail

ROLE="${1:-}"; CMD="${2:-status}"
case "$ROLE" in
  orchestrator) TAG=orchestrator-main; REPORTS="$HOME/tmp/agent-reports/orchestrator"; RUNDIR="$HOME/git/agent-hub-wt-orchestrator-run"; SUB=ops/orchestrator; OTHER_ROLE=oncall ;;
  oncall)       TAG=oncall-engineer;   REPORTS="$HOME/tmp/agent-reports/oncall";        RUNDIR="$HOME/git/agent-hub-wt-oncall-run";       SUB=ops/oncall;        OTHER_ROLE=orchestrator ;;
  *) echo "usage: $0 <orchestrator|oncall> <start|ensure|check|status|session-id|restart|handoff-restart|skills-sync>" >&2; exit 64 ;;
esac
BASE_REPO="${BASE_REPO:-$HOME/git/agent-hub}"           # donor clone for the run worktree
[ -f "$HOME/.local/share/agent-hub-ops/guard-bin/env.sh" ] && . "$HOME/.local/share/agent-hub-ops/guard-bin/env.sh"  # agent rm guard (504d662b): sessions inherit it via --setenv=PATH
SKILLS_REPO="${SKILLS_REPO:-$HOME/git/bots-management-ops}"   # read-only clone of alexeygrigorev/bots-management (bot-skills is archived)
SKILLS_REMOTE="${SKILLS_REMOTE:-alexeygrigorev/bots-management}"
SKILLS_DIR="${SKILLS_DIR:-$SKILLS_REPO/.agents/skills}"   # skills live under .agents/skills
if [ "$ROLE" = oncall ]; then
  # The persistent oncall coordinator stays on the explicitly named daily Muse model.
  # Do not inherit the old shared Antigravity preference or the orchestrator fallback.
  ENGINE="${ONCALL_ENGINE:-${ENGINE:-opencode}}"
  FALLBACK_ENGINE="opencode"
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

# Hub owns coordinator launches (phase 6.1). Hub names the native session
# task-hub2-<Hub session id> in the workspace Hub resolved; it never honours a
# caller-chosen tag. The binding Hub returned is the only discovery key for a
# Hub-started coordinator: {"hub_id", "native_id", "native_tag", "workspace"}.
# A record with only hub_id is a Hub session whose start was not confirmed; it
# is stopped through Hub before any new start so no orphan keeps running.
BINDING="$STATE/hub_binding.json"
# Durable per-role Hub intakes, written by the operator (not this script):
#   {"orchestrator": "<intake id>", "oncall": "<intake id>"}
ROLE_INTAKES="${OPS_ROLE_INTAKES:-$HOME/.config/agent-hub/ops-role-intakes.json}"
role_intake() {  # $1 = role; prints that role's intake id
  python3 - "$ROLE_INTAKES" "$1" <<'PY' || { log "role_intake_unconfigured: $ROLE_INTAKES has no valid intake for $1; recovery held" >&2; return 1; }
import json,re,sys
try:
    v=json.load(open(sys.argv[1])).get(sys.argv[2])
except Exception:
    sys.exit(1)
if not isinstance(v,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{5,127}",v):
    sys.exit(1)
print(v)
PY
}
HUB_CFG="${HUB_CONFIG_FILE:-$HOME/.config/agent-hub/cli.json}"
binding_get() {  # $1 = field; prints it (empty when absent); fails on a corrupt binding
  [ -e "$BINDING" ] || return 0
  python3 - "$BINDING" "$1" <<'PY'
import json,re,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if not isinstance(d,dict) or not isinstance(d.get("hub_id"),str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",d["hub_id"]):
    sys.exit(1)
v=d.get(sys.argv[2])
print(v if isinstance(v,str) else "")
PY
}

# List failures are errors, never evidence that no session exists. Matches only
# (a) the exact Hub binding: native id, tag task-hub2-<hub_id>, hub.session label
# and Hub's bound workspace; or (b) a pre-migration coordinator this script once
# started directly (legacy tag + ops.role label, never Hub-managed), so the first
# Hub start after migration stops it instead of running a duplicate.
matching_sessions() {
  local binding="" list
  if [ -e "$BINDING" ]; then
    # stderr: callers capture this function's stdout as the session id list.
    binding_get hub_id >/dev/null || { log "corrupt Hub binding $BINDING" >&2; return 1; }
    binding="$BINDING"
  fi
  list=$(t a list --json) || return 1
  printf '%s' "$list" | python3 -c '
import json,sys
w,t,role,live,binding=sys.argv[1:6]
b={}
if binding:
    b=json.load(open(binding))
want_tag="task-hub2-"+b["hub_id"] if b else None
def alive(s):
    return live == "all" or (s.get("phase")=="running" and s.get("worker_alive") is True)
rows=json.load(sys.stdin)
for s in rows:
    labels=s.get("labels") if isinstance(s.get("labels"),dict) else {}
    if (b.get("native_id") and s.get("id")==b["native_id"] and s.get("tag")==want_tag
            and b.get("native_tag")==want_tag and labels.get("hub.session")==b["hub_id"]
            and s.get("workspace")==b.get("workspace") and alive(s)):
        print(s["id"])
for s in rows:
    labels=s.get("labels") if isinstance(s.get("labels"),dict) else {}
    if (s.get("workspace")==w and s.get("tag")==t and labels.get("ops.role")==role
            and labels.get("hub.managed")!="true" and "hub.session" not in labels and alive(s)):
        print(s["id"])' "$RUNDIR/$SUB" "$TAG" "$ROLE" "${1:-live}" "$binding"
}
find_session() { matching_sessions | head -1; }

# Stop the bound Hub session through Hub (the owner of its lifecycle). Only a
# confirmed Hub stop, or positive evidence that nothing of it can still run,
# retires the binding. A Hub state alone (lost/failed/refused/cancelled) never
# proves absence, so a replacement is never started beside a surviving process:
#  - bound native: Hub reports it ended AND the native's processes and
#    containment are verified gone here;
#  - no native recorded: Hub reports refused/cancelled with no native id AND no
#    local record labelled with this Hub session is still running.
stop_bound() {
  local hub_id native show state show_native labelled id
  hub_id=$(binding_get hub_id) || { log "corrupt Hub binding $BINDING"; return 1; }
  [ -n "$hub_id" ] || return 0
  native=$(binding_get native_id)
  if HUB_CONFIG_FILE="$HUB_CFG" T=60 t hub sessions stop "$hub_id" --key "ops-stop-$hub_id" >/dev/null 2>"$STATE/hub_stop.err"; then
    log "stopped Hub session $hub_id"; rm -f "$BINDING"; return 0
  fi
  show=$(HUB_CONFIG_FILE="$HUB_CFG" T=30 t hub sessions show "$hub_id" --json 2>/dev/null) || show=""
  read -r state show_native < <(printf '%s' "$show" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin); d=d.get("session",d) if isinstance(d.get("session"),dict) else d
    n=d.get("native_id")
    print(d.get("state") or "-", n if isinstance(n,str) and n else "-")
except Exception:
    print("- -")')
  labelled=$(t a list --json 2>/dev/null | python3 -c '
import json,sys
for s in json.load(sys.stdin):
    l=s.get("labels") if isinstance(s.get("labels"),dict) else {}
    if l.get("hub.session")==sys.argv[1]: print(s["id"])' "$hub_id") || labelled="?"
  local gone=1
  if [ "$labelled" = "?" ]; then gone=0
  else for id in $native $labelled; do native_dead "$id" || { gone=0; break; }; done; fi
  if [ -n "$native" ] && [[ "$state" =~ ^(stopped|lost|failed|refused|cancelled)$ ]] && [ "$gone" = 1 ]; then
    log "Hub session $hub_id ended ($state) and native $native is verified gone"
  elif [ -z "$native" ] && [[ "$state" =~ ^(refused|cancelled)$ ]] && [ "$show_native" = - ] && [ "$gone" = 1 ]; then
    log "Hub session $hub_id never started natively ($state, no native id)"
  else
    log "Hub stop of $hub_id failed: $(diag "$STATE/hub_stop.err") (Hub state ${state/#-/unknown}${native:+, native $native}; not verified gone); recovery held"
    return 1
  fi
  rm -f "$BINDING"
}

# First line of a Hub CLI diagnostic, without control characters. The CLI never
# interpolates response bodies or credentials into these lines.
diag() { head -n 3 "$1" 2>/dev/null | tr -d '\000-\010\013-\037' | tr '\n' ' ' | cut -c1-300; }

# Fresh evidence that a native record's processes and any recorded containment
# have drained. An unknown record or unreadable status is not evidence.
native_dead() {
  t a status "$1" --json 2>/dev/null | python3 -c '
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
'
}

# a forget explicitly does NOT stop surviving workloads. Require fresh evidence
# that both recorded processes and any recorded containment have drained first.
forget_dead() {
  native_dead "$1" || { log "refusing forget $1: processes alive or death unverified"; return 1; }
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
if role == "oncall":
    allowed = {"opencode": "go"}
else:
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
default_order = ("opencode",) if role == "oncall" else ("antigravity", "opencode", "grok", "zcodex")
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
allowed = {"opencode"} if role == "oncall" else {"antigravity", "grok", "opencode", "zcodex"}
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
    default_order = ("opencode",) if role == "oncall" else ("antigravity", "opencode", "grok", "zcodex")
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
  if [ "$ENGINE" != opencode ]; then
    log "oncall route '$ENGINE' is disallowed; using OpenCode Muse Spark"
    ENGINE=opencode
    ENGINE_SELECTED=0
  fi
  # Recovery path 2 must stay on the same safe route; environment settings
  # intended for orchestrator recovery cannot widen the oncall allowlist.
  FALLBACK_ENGINE=opencode
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

# Start through the maintained Hub CLI (`hub sessions start`, which has no --tag
# and always prints its JSON result). Hub performs project/workspace resolution,
# quota/resource admission and the native launch; a refusal is terminal for
# this attempt and is never bypassed with a native start. Records the binding.
#
# An attempt whose outcome is unknown (lost response, timeout, transport) is
# pinned in $PENDING: key, engine and the exact argv. Every later attempt
# replays that identical request until Hub gives a definitive answer, so a
# changed quota, route or config can never draw a second, different start.
PENDING="$STATE/hub_start_pending"
has_pending() { [ -s "$PENDING" ]; }
hub_start() {
  local hub_engine key out err rc parsed hub_id intake other argv=() rec=()
  if has_pending; then
    mapfile -d '' rec < "$PENDING"
    [ "${#rec[@]}" -ge 3 ] && [ -n "${rec[0]}" ] || { log "corrupt pending Hub start $PENDING; recovery held"; return 1; }
    key="${rec[0]}"; ENGINE="${rec[1]}"; argv=("${rec[@]:2}")
    log "replaying unconfirmed Hub start $key unchanged (route $ENGINE)"
  else
    # Hub allows one active session per intake, so each role links its own.
    intake=$(role_intake "$ROLE") || return 1
    other=$(role_intake "$OTHER_ROLE" 2>/dev/null) || other=""
    if [ "$intake" = "$other" ]; then
      log "role_intake_shared: $ROLE and $OTHER_ROLE both name intake $intake in $ROLE_INTAKES; recovery held"
      return 1
    fi
    hub_engine="$ENGINE"
    case "$hub_engine" in opencode) hub_engine=go ;; zcodex) hub_engine=zai ;; esac  # Hub2 engine ids
    key="ops-$ROLE-$(date +%s%N)"
    argv=(sessions start --workspace "$RUNDIR/$SUB" --name "$TAG" --engine "$hub_engine" "${model_args[@]}"
      --purpose implement --interactive --reason "Pinned $ROLE coordinator route" --intake-task-id "$intake"
      --key "$key" --wait --timeout 150)
    # Publish the request durably before sending it.
    printf '%s\0' "$key" "$ENGINE" "${argv[@]}" > "$PENDING.tmp" && sync "$PENDING.tmp" \
      && mv -f "$PENDING.tmp" "$PENDING" && sync "$STATE" \
      || { log "cannot record pending Hub start; recovery held"; return 1; }
  fi
  out="$STATE/hub_start.out"; err="$STATE/hub_start.err"
  HUB_CONFIG_FILE="$HUB_CFG" T=180 t hub "${argv[@]}" >"$out" 2>"$err"; rc=$?
  # Accept only a running Hub session bound to a native session; record the
  # binding (or the bare Hub id) before anything else can fail.
  parsed=$(python3 - "$out" "$rc" "$BINDING" <<'PY'
import json,os,re,sys
out,rc,binding=sys.argv[1],int(sys.argv[2]),sys.argv[3]
safe=re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
try:
    d=json.load(open(out))
except Exception:
    d=None
if not isinstance(d,dict):
    sys.exit(3)
b=d.get("binding_wait")
s=b.get("session") if isinstance(b,dict) and isinstance(b.get("session"),dict) else b
s=s if isinstance(s,dict) else {}
hub_id=d.get("id")
if not isinstance(hub_id,str) or not safe.match(hub_id):
    sys.exit(3)
rec={"hub_id":hub_id}
native=s.get("native_id")
ws=s.get("workspace") or d.get("workspace")
ok=(rc==0 and s.get("state")=="running" and s.get("id",hub_id)==hub_id
    and isinstance(native,str) and safe.match(native) and isinstance(ws,str) and ws.startswith("/"))
if ok:
    rec.update(native_id=native,native_tag="task-hub2-"+hub_id,workspace=ws)
tmp=binding+".tmp"
with open(tmp,"w") as f: json.dump(rec,f)
os.replace(tmp,binding)
print(hub_id)
sys.exit(0 if ok else 4)
PY
)
  case $? in
    0) rm -f "$PENDING"; hub_id="$parsed"
       log "Hub session $hub_id running as native $(binding_get native_id) (tag $(binding_get native_tag))"
       return 0 ;;
    4) rm -f "$PENDING"  # Hub named the session; the binding now governs it
       log "Hub start unconfirmed: session $parsed not bound to a running native session; recovery held" ;;
    *) local reason; reason=$(diag "$err")
       if [[ "$reason" =~ session_start_failed\;\ session\ ([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\ reached ]]; then
         # Hub created the session but it did not run: remember it so it is
         # stopped through Hub before the next attempt.
         printf '{"hub_id":"%s"}\n' "${BASH_REMATCH[1]}" > "$BINDING.tmp" && mv -f "$BINDING.tmp" "$BINDING" && rm -f "$PENDING"
       fi
       # Any other failure (HTTP status included) is not proof that Hub created
       # nothing: the pending request is replayed unchanged.
       log "Hub start failed (rc=$rc): ${reason:-no diagnostic}; recovery held" ;;
  esac
  return 1
}

start() {
  local current model_args=()
  current=$(find_session) || { log "session census failed"; return 1; }
  [ -n "$current" ] && { log "already running: $current"; return 0; }
  has_pending || select_engine || return 1
  # Clear stale names before launch; never leave orphan workloads behind.
  stop_old || return 1
  prep_worktree || return 1
  date +%s > "$STATE/last_start"
  model_args=()
  if has_pending; then :
  elif [ "$ROLE" = oncall ]; then
    [ "$ENGINE" = opencode ] || { log "refusing unsafe oncall engine $ENGINE"; return 1; }
    model_args=(--model opencode-go/muse-spark-1.3-contributor)
    log "oncall model pinned to opencode-go/muse-spark-1.3-contributor"
  elif [ "$ENGINE" = antigravity ]; then
    # Preserve the orchestrator's explicit Flash default; never leak it into oncall.
    model_args=(--model "${ORCH_MODEL:-gemini-3.8-flash-high}")
  fi
  local id native; hub_start || return 1
  native=$(binding_get native_id) || { log "corrupt Hub binding $BINDING"; return 1; }
  # The native session must be observed locally under Hub's exact binding.
  id=$(find_session) || { log "session census failed after Hub start"; return 1; }
  [ -n "$id" ] && [ "$id" = "$native" ] || { log "Hub-bound native session $native not observed live on this host; recovery held"; return 1; }
  log "started session $id; waiting for prompt"
  local i s ready=0
  for i in $(seq 1 60); do
    sleep 3; s=$(screen "$id")
    if grep -qi -E 'Yes, I accept|Bypass Permissions mode' <<<"$s" && grep -q 'No, exit' <<<"$s"; then a send "$id" --hex 32 >/dev/null; sleep 1; a send "$id" --hex 0d >/dev/null; continue; fi
    if grep -qi -E 'trust this folder|Do you trust|Yes, proceed' <<<"$s"; then a send "$id" --hex 0d >/dev/null; continue; fi
    if grep -q -E '❯|> $|for shortcuts|Ask anything|ctrl\+p commands' <<<"$s"; then ready=1; break; fi
  done
  [ "$ready" = 1 ] || { log "prompt readiness failed: $id"; return 1; }
  local hub_id; hub_id=$(binding_get hub_id)
  local prompt="Run the $ROLE check now: follow $RUNDIR/$SUB/hourly-check.md, starting with step 0 (skills sync, handoff, inbox). Hub scheduled prompts continue the recurring work for Hub session $hub_id (name $TAG)."
  [ "$ROLE" = orchestrator ] && prompt="Run the orchestrator tick now: follow $BRIEF (durable brief) and $RUNDIR/$SUB/CLAUDE.md; append one line to $TICKS. Hub scheduled prompts continue the recurring work for Hub session $hub_id (name $TAG)."
  a send "$id" "$prompt" --enter >/dev/null; local rc=$?
  echo "$prompt" > "$STATE/loop_command"
  log "engine-neutral startup prompt sent (rc=$rc); Hub schedules provide recurring work"
  return "$rc"
}

stop_old() {
  local ids id bound
  ids=$(matching_sessions all) || { log "session census failed"; return 1; }
  bound=$(binding_get native_id) || { log "corrupt Hub binding $BINDING"; return 1; }
  # Hub-started sessions (and unconfirmed Hub starts) stop through Hub, even
  # when no native record is visible here.
  stop_bound || return 1
  for id in $ids; do
    [ -n "$bound" ] && [ "$id" = "$bound" ] && continue
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
  has_pending || select_engine || return 1
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
  if FORCE=1 restart "$reason (recovery path $path, engine $ENGINE)"; then arm_proof "$path" "$ENGINE"; hub_report recovering
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

case "$CMD" in
  start) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; start ;;
  ensure|check) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; check ;;
  session-id)  # exact bound (or pre-migration) live session id; 1 = none, 3 = census failed
    id=$(find_session) || exit 3; [ -n "$id" ] || exit 1; printf '%s\n' "$id" ;;
  status) id=$(find_session); [ -n "$id" ] || { log "not running"; exit 1; }
    hid=""; [ "$id" = "$(binding_get native_id)" ] && hid=$(binding_get hub_id)
    log "running $id${hid:+ (Hub session $hid)}"; echo "attach: a attach $id" ;;
  restart) if [ "$ROLE" = orchestrator ]; then role_lock || exit 75; fi; FORCE=1 restart "manual" ;;
  handoff-restart)  # called BY the session itself after writing handoff.md; detaches so it survives the kill
    FORCE=1 setsid nohup bash -c "sleep 5; '$0' $ROLE restart" >>"$STATE/restart.log" 2>&1 < /dev/null 9>&- & log "restart scheduled in 5s" ;;
  skills-sync) skills_sync ;;
  *) echo "unknown command $CMD" >&2; exit 64 ;;
esac
