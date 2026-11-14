#!/usr/bin/env bash
# Hub DB backup staleness alert (intake c1241bea; mail hygiene 9806fc14).
#
# Newest export older than STALE_AFTER_HOURS (default 36) -> email to ALERT_TO
# (default "dev@dtcdev.click") via the working msmtp
# alerts path (sendmail first, msmtp fallback — same order as
# machine-management monitors/coolify-healthcheck.sh).
#
# Checks both trees (Hetzner local BACKUP_DIR and Storagebox STORAGEBOX_DIR);
# the freshest copy across both decides. Missing directory or no exports at
# all counts as stale.
#
# Mail hygiene (intake 9806fc14 — a 2026-10-27 manual verification ran this
# script against empty /tmp dirs with the real env and paged alexey@):
#   1. Test-path guard: when BACKUP_DIR, STORAGEBOX_DIR or STATE_DIR is under
#      /tmp or /var/tmp (test values), EVERY send — including RECOVERED — is
#      routed to ALERT_OPS_TO only, never ALERT_TO. Real recipients require
#      real paths.
#   2. Drill mode: HUB_DB_STALE_DRILL=1 forces one stale FAIL that is
#      NEVER mailed (standing rule t1940u/e27e0f9d: drill and test alerts
#      are never emailed, not even to dev@); it writes one "[DRILL]" log
#      line. It records a drill marker in the state file with LAST_ALERT=0,
#      so a real staleness episode in the following window is NOT deduped
#      away; the closing RECOVERED is also one "[DRILL]" log line, no mail.
#      Re-run without the flag to clear.
#   3. --dry-run / --no-mail / --check-only still send nothing.
#
# Same-run failure page (intake e27e0f9d): hub-db-backup.sh calls
#   hub-db-backup-staleness.sh --backup-failed "<reason>" [--drill]
# on any non-zero exit. It mails "[FAIL] Hub DB backup run failed" with the
# reason and the newest surviving export to ALERT_TO immediately (no 26h/36h
# wait, no dedupe: the job runs once a day), honoring the test-path guard.
# --drill (HUB_DB_BACKUP_FAIL_DRILL=1 in the backup script, or
# HUB_DB_STALE_DRILL=1) NEVER sends mail: it writes one "[DRILL]" log line
# and no MTA is invoked. It never reads or writes the staleness state file.
# Exit 0 after the attempt.
#
# Exit codes: 0 fresh, 2 stale (mail attempted unless --no-mail / DRY_RUN=1),
# 1 on usage/config error. --dry-run prints what would happen without mailing.
# State dir dedupes repeat pages: at most one mail per ALERT_REPEAT_HOURS
# (default 24) while continuously stale; a RECOVERED note goes out once the
# backup is fresh again after a stale episode.
set -euo pipefail

# Optional unqualified metadata only; source-session contract V2.
_BACKUP_ALERT_DIRTY=0
_BACKUP_ALERT_DECIDED=0
_BACKUP_ALERT_CLAIM=""
_BACKUP_ALERT_READ=""
_BACKUP_ALERT_WRITE=""
_BACKUP_ALERT_PID=""
_BACKUP_ALERT_SEQ=0
_BACKUP_ALERT_ROLE=backup_notifier
_BACKUP_ALERT_PARENT=none
backup_alert_close() {
  local discard all_jobs listed_pid child_listed
  local IFS=$' \t\n'
  if [[ -n "$_BACKUP_ALERT_WRITE" ]]; then exec {_BACKUP_ALERT_WRITE}>&-; fi
  if [[ -n "$_BACKUP_ALERT_READ" ]]; then
    IFS= read -r -t 0.15 -n 513 -u "$_BACKUP_ALERT_READ" discard 2>/dev/null || true
    exec {_BACKUP_ALERT_READ}<&-
  fi
  _BACKUP_ALERT_READ=""; _BACKUP_ALERT_WRITE=""
  # The one all-jobs snapshot includes every state, so a stopped->running
  # transition cannot disappear between separate filtered observations.
  # This PID was captured from our own coprocess. Reap only after a coherent
  # successful snapshot proves it is no longer listed; unknown stays no-wait.
  if [[ "$_BACKUP_ALERT_PID" =~ ^[0-9]+$ ]] && all_jobs="$(jobs -p 2>/dev/null)"; then
    child_listed=0
    for listed_pid in $all_jobs; do
      if [[ ! "$listed_pid" =~ ^[0-9]+$ || "$listed_pid" == "$_BACKUP_ALERT_PID" ]]; then
        child_listed=1; break
      fi
    done
    if [[ "$child_listed" == 0 ]]; then
      wait "$_BACKUP_ALERT_PID" 2>/dev/null || true
    fi
  fi
  _BACKUP_ALERT_PID=""
}
backup_alert_reply() {
  # One bounded local FD+JSON parser; it imports no Client and opens no socket.
  # The persistent source-session helper remains the sole broker peer.
  timeout --signal=KILL 0.15s python3 -I -S -c '# JSON-only local-FD reader. No client imports, sockets, peer identity or counts.
import json,os,re,select,sys,time,uuid
fd,kind,role,parent,expected=int(sys.argv[1]),*sys.argv[2:]
end=time.monotonic()+.145
os.set_blocking(fd,False)
raw=bytearray()
while True:
 left=end-time.monotonic()
 if left<=0 or not select.select([fd],[],[],left)[0]:raise SystemExit(2)
 part=os.read(fd,1)
 if not part:raise SystemExit(2)
 if part==b'"'"'\n'"'"':break
 raw.extend(part)
 if len(raw)>512:raise SystemExit(2)
def unique(pairs):
 result={}
 for key,value in pairs:
  if key in result:raise ValueError()
  result[key]=value
 return result
value=json.loads(bytes(raw).decode('"'"'utf-8'"'"'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
if type(value) is not dict:raise SystemExit(2)
if kind=='"'"'ready'"'"':
 if set(value)!={'"'"'kind'"'"','"'"'state'"'"','"'"'role'"'"','"'"'admitted'"'"','"'"'source_claim_id'"'"','"'"'parent_source_claim'"'"'}:raise SystemExit(2)
 claim=value['"'"'source_claim_id'"'"']
 if (value['"'"'kind'"'"']!='"'"'hub_alert_source_session_v1'"'"' or value['"'"'state'"'"']!='"'"'ready_unqualified'"'"'
   or value['"'"'role'"'"']!=role or value['"'"'admitted'"'"'] is not False
   or type(claim) is not str or re.fullmatch('"'"'[0-9a-f]{32}'"'"',claim) is None
   or uuid.UUID(hex=claim).version!=4
   or value['"'"'parent_source_claim'"'"']!=(None if parent=='"'"'none'"'"' else parent)):raise SystemExit(2)
 print(claim)
else:
 if set(value)!={'"'"'kind'"'"','"'"'seq'"'"','"'"'local_ok'"'"','"'"'broker_ok'"'"','"'"'observer_ok'"'"','"'"'admitted'"'"'}:raise SystemExit(2)
 if (value['"'"'kind'"'"']!='"'"'hub_alert_source_session_ack_v1'"'"' or type(value['"'"'seq'"'"']) is not int
   or value['"'"'seq'"'"']!=int(expected) or not 0<=value['"'"'seq'"'"']<8 or value['"'"'admitted'"'"'] is not False
   or any(type(value[k]) is not bool for k in ('"'"'local_ok'"'"','"'"'broker_ok'"'"','"'"'observer_ok'"'"'))):raise SystemExit(2)
 print(*(int(value[k]) for k in ('"'"'local_ok'"'"','"'"'broker_ok'"'"','"'"'observer_ok'"'"')))
' \
    0 "$1" "$_BACKUP_ALERT_ROLE" "$_BACKUP_ALERT_PARENT" "$2" <&"$_BACKUP_ALERT_READ" 2>/dev/null
}
backup_alert_start() {
  local client="$(dirname "${BASH_SOURCE[0]}")/hub_alert_client.py"
  local socket="${HUB_BACKUP_ALERT_BROKER_SOCKET:-}" expected="${HUB_BACKUP_ALERT_CLIENT_SHA256:-}"
  local ready original_read original_write
  local -a parent_args=()
  [[ "$socket" == /* && "$expected" =~ ^[0-9a-f]{64}$ ]] || return 0
  if [[ -n "${HUB_BACKUP_ALERT_PARENT_CLAIM:-}" ]]; then
    [[ "$_BACKUP_ALERT_ROLE" == backup_notifier && "$HUB_BACKUP_ALERT_PARENT_CLAIM" =~ ^[0-9a-f]{32}$ ]] || return 0
    _BACKUP_ALERT_PARENT="$HUB_BACKUP_ALERT_PARENT_CLAIM"
    parent_args=(--parent-source-claim "$_BACKUP_ALERT_PARENT")
  fi
  # One immutable FD read/hash and execution in the SAME persistent Python peer.
  # No helper ever dispatches, wraps or recursively reruns original business.
  coproc BACKUP_ALERT_SESSION {
    exec python3 -I -S -c '# Read/hash exactly one selected immutable client into the persistent peer.
import hashlib,os,re,stat,sys
from pathlib import Path
path=Path(sys.argv[1]);expected=sys.argv[2];args=sys.argv[3:]
if not path.is_absolute() or '"'"'..'"'"' in path.parts or re.fullmatch('"'"'[0-9a-f]{64}'"'"',expected) is None:raise SystemExit(2)
directory=os.open('"'"'/'"'"',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC);source=None
try:
 for part in path.parts[1:-1]:
  nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=directory)
  os.close(directory);directory=nxt;s=os.fstat(directory)
  if s.st_uid not in (0,os.getuid()) or s.st_mode&0o002:raise SystemExit(2)
 source=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=directory)
 before=os.fstat(source)
 if not stat.S_ISREG(before.st_mode) or before.st_uid not in (0,os.getuid()) or before.st_nlink!=1 or before.st_mode&0o022 or not 0<before.st_size<=65536:raise SystemExit(2)
 data=os.read(source,65537);stamp=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
 if len(data)!=before.st_size or hashlib.sha256(data).hexdigest()!=expected or stamp(os.fstat(source))!=stamp(before) or stamp(os.stat(path.name,dir_fd=directory,follow_symlinks=False))!=stamp(before):raise SystemExit(2)
finally:
 if source is not None:os.close(source)
 os.close(directory)
sys.argv=[str(path),*args]
exec(compile(data,'"'"'<verified-backup-alert-client>'"'"','"'"'exec'"'"'),{'"'"'__name__'"'"':'"'"'__main__'"'"','"'"'__file__'"'"':str(path)})
' "$client" "$expected" \
      source-session --socket "$socket" --role "$_BACKUP_ALERT_ROLE" "${parent_args[@]}" 2>/dev/null
  }
  _BACKUP_ALERT_PID="${BACKUP_ALERT_SESSION_PID:-}"
  original_read="${BACKUP_ALERT_SESSION[0]:-}"; original_write="${BACKUP_ALERT_SESSION[1]:-}"
  if [[ -z "$original_read" || -z "$original_write" ]]; then backup_alert_close; return 0; fi
  if ! { exec {_BACKUP_ALERT_READ}<&"$original_read"; exec {_BACKUP_ALERT_WRITE}>&"$original_write"; } 2>/dev/null; then
    backup_alert_close; return 0
  fi
  exec {original_read}<&-; exec {original_write}>&-
  if ready="$(backup_alert_reply ready 0)"; then
    _BACKUP_ALERT_CLAIM="$ready"
  else
    _BACKUP_ALERT_DIRTY=1; backup_alert_close
  fi
  return 0
}
backup_alert_event() {
  [[ -n "$_BACKUP_ALERT_WRITE" ]] || return 0
  local reply local_ok broker_ok observer_ok sequence="$_BACKUP_ALERT_SEQ"
  # At most three bounded fixed-schema lines; only this subshell sees SIGPIPE.
  if ! (printf '%s\n' "$1" >&"$_BACKUP_ALERT_WRITE") 2>/dev/null; then
    _BACKUP_ALERT_DIRTY=1; backup_alert_close; return 0
  fi
  _BACKUP_ALERT_SEQ=$(( _BACKUP_ALERT_SEQ + 1 ))
  if ! reply="$(backup_alert_reply ack "$sequence")"; then
    _BACKUP_ALERT_DIRTY=1; backup_alert_close; return 0
  fi
  read -r local_ok broker_ok observer_ok <<< "$reply"
  if [[ "$local_ok" != 1 ]]; then
    _BACKUP_ALERT_DIRTY=1; backup_alert_close
  elif [[ "$_BACKUP_ALERT_DIRTY" == 1 && ( "$broker_ok" == 1 || "$observer_ok" == 1 ) ]]; then
    # Typed but healing ACK is not accepted. Original work still runs once.
    backup_alert_close
  elif [[ "$broker_ok" != 1 || "$observer_ok" != 1 ]]; then
    _BACKUP_ALERT_DIRTY=1
  fi
  return 0
}
backup_alert_bool() { if [[ "$1" == 1 ]]; then printf true; else printf false; fi; }
backup_alert_terminal() {
  local original_rc="$1" success=false
  [[ "$original_rc" != 0 ]] || success=true
  backup_alert_event "{\"phase\":\"terminal\",\"flags\":{\"exit_success\":$success,\"decision_seen\":$(backup_alert_bool "$_BACKUP_ALERT_DECIDED")}}"
  backup_alert_close
  return 0
}
trap 'backup_alert_terminal "$?"' EXIT
backup_alert_start
backup_alert_event '{"phase":"begin","flags":{}}'


ENV_FILE="${HUB_DB_BACKUP_ENV_FILE:-/etc/hub-db-backup.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

BACKUP_DIR="${BACKUP_DIR:-/var/backups/hub-do}"
STORAGEBOX_DIR="${STORAGEBOX_DIR:-/home/alexey/storagebox/backups/hub-do}"
STALE_AFTER_HOURS="${STALE_AFTER_HOURS:-36}"
ALERT_TO="${ALERT_TO:-dev@dtcdev.click}"
ALERT_OPS_TO="${ALERT_OPS_TO:-dev@dtcdev.click}"
ALERT_FROM="${ALERT_FROM:-noreply@datatalks.club}"
ALERT_REPEAT_HOURS="${ALERT_REPEAT_HOURS:-24}"
# msmtp account for the working alerts path on this box. The default msmtp
# account cannot send as ALERT_FROM (SES 554 on the sandbox identity), while
# the 'ses' account demonstrably delivers noreply@datatalks.club to the
# exact ALERT_TO recipients (see msmtp log 250 Ok). Plain msmtp (default
# account) remains the fallback.
MSMTP_ACCOUNT="${MSMTP_ACCOUNT:-ses}"
STATE_DIR="${STATE_DIR:-/var/lib/hub-db-backup}"
DRY_RUN="${DRY_RUN:-0}"
HUB_DB_STALE_DRILL="${HUB_DB_STALE_DRILL:-0}"
# Artifact name glob. Defaults to the hub1 REST export shape; the GCP VM
# deployment (port-gcloud) sets BACKUP_GLOB='hub2-db-*.tar.gz' for the
# file-snapshot artifacts written by scripts/hub2-db-backup.sh.
BACKUP_GLOB="${BACKUP_GLOB:-hub-do-*.json.gz}"

NO_MAIL=0
BACKUP_FAILED=0
BACKUP_FAILED_REASON=""
FAIL_DRILL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-failed)
      [[ $# -ge 2 ]] || { echo "--backup-failed needs a reason" >&2; exit 1; }
      BACKUP_FAILED=1; BACKUP_FAILED_REASON="$2"; shift 2 ;;
    --drill) FAIL_DRILL=1; shift ;;
    --no-mail) NO_MAIL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --check-only) NO_MAIL=1; DRY_RUN=1; shift ;;
    -h|--help)
      echo "Usage: hub-db-backup-staleness.sh [--no-mail] [--dry-run] [--check-only] [--backup-failed REASON [--drill]]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Either drill selector makes a --backup-failed alert log-only: no
# combination of drill flags/env may reach send_mail.
if [[ "$BACKUP_FAILED" -eq 1 && "$HUB_DB_STALE_DRILL" == "1" ]]; then
  FAIL_DRILL=1
fi
# Explicit source checks are drills even if the caller forgot --drill.
# The 2026-11-10 source check used empty fixture trees with a real MTA;
# neither its fabricated failure nor its "none found" result may page.
if [[ "$BACKUP_FAILED" -eq 1 && "$BACKUP_FAILED_REASON" =~ ^[[:space:]]*\[TEST\]([[:space:]]|$) ]]; then
  FAIL_DRILL=1
fi
if [[ "$FAIL_DRILL" -eq 1 && "$BACKUP_FAILED" -ne 1 ]]; then
  echo "--drill is only valid with --backup-failed (use HUB_DB_STALE_DRILL=1 for the staleness drill)" >&2
  exit 1
fi
[[ "$STALE_AFTER_HOURS" =~ ^[0-9]+$ ]] || { log "ERROR: STALE_AFTER_HOURS must be an integer"; exit 1; }
[[ "$ALERT_REPEAT_HOURS" =~ ^[0-9]+$ ]] || { log "ERROR: ALERT_REPEAT_HOURS must be an integer"; exit 1; }

# Test-path guard (9806fc14): any test-valued dir forces ops-only recipients.
TEST_PATH_GUARD=0
for _guard_path in "$BACKUP_DIR" "$STORAGEBOX_DIR" "$STATE_DIR"; do
  case "$_guard_path" in
    /tmp|/tmp/*|/var/tmp|/var/tmp/*) TEST_PATH_GUARD=1 ;;
  esac
done
unset _guard_path

newest_epoch() {
  local root="$1" newest=""
  if [[ -d "$root" ]]; then
    newest="$(find "$root" -type f -name "$BACKUP_GLOB" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n 1 || true)"
  fi
  if [[ -n "$newest" ]]; then
    printf '%s' "${newest%% *}" | cut -d. -f1
  fi
}

NOW="$(date +%s)"
LOCAL_NEWEST="$(newest_epoch "$BACKUP_DIR")"
BOX_NEWEST="$(newest_epoch "$STORAGEBOX_DIR")"
NEWEST=0
NEWEST_PATH=""
if [[ -n "$LOCAL_NEWEST" && "$LOCAL_NEWEST" -gt "$NEWEST" ]]; then
  NEWEST="$LOCAL_NEWEST"
  NEWEST_PATH="$(find "$BACKUP_DIR" -type f -name "$BACKUP_GLOB" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n 1 | cut -d' ' -f2-)"
fi
if [[ -n "$BOX_NEWEST" && "$BOX_NEWEST" -gt "$NEWEST" ]]; then
  NEWEST="$BOX_NEWEST"
  NEWEST_PATH="$(find "$STORAGEBOX_DIR" -type f -name "$BACKUP_GLOB" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n 1 | cut -d' ' -f2-)"
fi

if [[ "$NEWEST" -le 0 ]]; then
  AGE_S=-1
else
  AGE_S=$(( NOW - NEWEST ))
fi
STALE_AFTER_S=$(( STALE_AFTER_HOURS * 3600 ))
IS_STALE=1
if [[ "$AGE_S" -ge 0 && "$AGE_S" -le "$STALE_AFTER_S" ]]; then
  IS_STALE=0
fi

mkdir -p "$STATE_DIR" 2>/dev/null || true
chmod 700 "$STATE_DIR" 2>/dev/null || true
STATE_FILE="${STATE_DIR%/}/staleness.state"
LAST_ALERT=0
WAS_STALE=0
WAS_DRILL=0
if [[ -f "$STATE_FILE" ]]; then
  # Parse numeric/flag fields without sourcing (state dir must not be code).
  # `|| true` keeps pipefail from killing the run when a field is absent:
  # pre-9806fc14 state files have no DRILL= line, and a hard exit here would
  # silently disable the staleness cron (exit 1, no mail). The normalization
  # below defaults empty/garbage values.
  LAST_ALERT="$(grep -E '^LAST_ALERT=[0-9]+$' "$STATE_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2 || true)"
  WAS_STALE="$(grep -E '^WAS_STALE=[01]$' "$STATE_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2 || true)"
  WAS_DRILL="$(grep -E '^DRILL=[01]$' "$STATE_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2 || true)"
  [[ "$LAST_ALERT" =~ ^[0-9]+$ ]] || LAST_ALERT=0
  [[ "$WAS_STALE" =~ ^[01]$ ]] || WAS_STALE=0
  [[ "$WAS_DRILL" =~ ^[01]$ ]] || WAS_DRILL=0
fi

write_state() {
  # $1=WAS_STALE $2=LAST_ALERT $3=DRILL. Never mutate the real state machine
  # on dry-run / no-mail runs (tests must not seed dedupe for prod).
  if [[ "$DRY_RUN" -eq 1 || "$NO_MAIL" -eq 1 ]]; then return 0; fi
  printf 'WAS_STALE=%s\nLAST_ALERT=%s\nDRILL=%s\n' "$1" "$2" "$3" > "$STATE_FILE" 2>/dev/null || true
}

# Drills (standing rule, t1940u/e27e0f9d): drill and test alerts are never
# mailed to anyone. Each drill writes exactly ONE log line to this run's log
# (stdout, appended to the cron log): "<UTC ts> [DRILL] <what> result=<...>
# mail=none". No MTA is invoked and no other file is written.
drill_log() {
  # $1=what $2=result. Each part is flattened to one line and bounded on its
  # own, so the mandatory "mail=none" suffix always survives.
  local what result
  what="$(printf '%s' "$1" | tr '\r\n' '  ' | cut -c1-120)"
  result="$(printf '%s' "$2" | tr '\r\n' '  ' | cut -c1-900)"
  log "[DRILL] ${what} result=${result} mail=none"
}

effective_to() {
  # Real recipients, unless the test-path guard forces ops-only (9806fc14).
  if [[ "$TEST_PATH_GUARD" -eq 1 ]]; then
    printf '%s' "$ALERT_OPS_TO"
  else
    printf '%s' "$ALERT_TO"
  fi
}

send_mail() {
  # $1=subject $2=body [$3=to_csv override — the drill passes ALERT_OPS_TO]
  local subject="$1" body="$2"
  local to_csv="${3:-$(effective_to)}"
  local IFS=','
  # shellcheck disable=SC2206
  local to_args=($to_csv)
  local sent=""
  if command -v sendmail >/dev/null 2>&1; then
    if {
      echo "From: ${ALERT_FROM}"
      echo "To: ${to_csv//,/, }"
      echo "Subject: ${subject}"
      echo ""
      printf '%s\n' "$body"
    } | sendmail -t 2>/dev/null; then
      sent="sendmail"
    else
      log "WARN: sendmail failed"
    fi
  fi
  if [[ -z "$sent" ]] && command -v msmtp >/dev/null 2>&1; then
    if {
      echo "From: ${ALERT_FROM}"
      echo "To: ${to_csv//,/, }"
      echo "Subject: ${subject}"
      echo ""
      printf '%s\n' "$body"
    } | msmtp -a "${MSMTP_ACCOUNT}" -- "${to_args[@]}" 2>/dev/null; then
      sent="msmtp:${MSMTP_ACCOUNT}"
    elif {
      echo "From: ${ALERT_FROM}"
      echo "To: ${to_csv//,/, }"
      echo "Subject: ${subject}"
      echo ""
      printf '%s\n' "$body"
    } | msmtp -- "${to_args[@]}" 2>/dev/null; then
      sent="msmtp:default"
    else
      log "WARN: msmtp failed (account ${MSMTP_ACCOUNT} and default)"
    fi
  fi
  if [[ -z "$sent" ]]; then
    log "WARN: no MTA (sendmail/msmtp) — alert logged only; install msmtp"
  fi
  local guard_note=""
  [[ "$TEST_PATH_GUARD" -eq 1 ]] && guard_note=" guard=test-paths"
  log "ALERT stale=${IS_STALE} subject='${subject}' via=${sent:-none} to=${to_csv}${guard_note}"
}

# Partial logical condition report precedes drill/no-mail/dedup/SMTP. It
# cannot turn a source claim into admitted counts or complete coverage.
backup_alert_decision() {
  local condition=fresh decision=observation is_test=0 recipient=none repeat_due=1
  local recipients=",$(effective_to | tr -d '[:space:]'),"
  [[ "$recipients" != *",dev@dtcdev.click,"* ]] || recipient=dev
  if [[ "$FAIL_DRILL" == 1 || "$HUB_DB_STALE_DRILL" == 1 ]]; then
    is_test=1; condition=drill
  elif [[ "$BACKUP_FAILED" == 1 ]]; then
    condition=backup_failed
    [[ "$recipient" != dev ]] || decision=alert
  elif [[ "$IS_STALE" == 1 ]]; then
    condition=stale
    if [[ "$LAST_ALERT" -gt 0 && "$WAS_STALE" == 1 && "$WAS_DRILL" == 0 ]] \
      && (( (NOW - LAST_ALERT) / 3600 < ALERT_REPEAT_HOURS )); then repeat_due=0; fi
    if [[ "$recipient" == dev ]]; then
      if [[ "$repeat_due" == 1 ]]; then decision=alert; else decision=alert_dedup; fi
    fi
  elif [[ "$WAS_STALE" == 1 ]]; then
    condition=recovery_pending
    if [[ "$WAS_DRILL" == 1 ]]; then is_test=1
    elif [[ "$recipient" == dev ]]; then decision=recovery; fi
  fi
  backup_alert_event "{\"phase\":\"decision\",\"decision\":\"$decision\",\"condition\":\"$condition\",\"recipient_class\":\"$recipient\",\"flags\":{\"test\":$(backup_alert_bool "$is_test"),\"drill\":$(backup_alert_bool "$FAIL_DRILL"),\"stale_drill\":$(backup_alert_bool "$HUB_DB_STALE_DRILL"),\"no_mail\":$(backup_alert_bool "$NO_MAIL"),\"dry_run\":$(backup_alert_bool "$DRY_RUN"),\"prior_stale\":$(backup_alert_bool "$WAS_STALE"),\"prior_drill\":$(backup_alert_bool "$WAS_DRILL"),\"repeat_due\":$(backup_alert_bool "$repeat_due")}}"
  _BACKUP_ALERT_DECIDED=1
}
backup_alert_decision

if [[ "$TEST_PATH_GUARD" -eq 1 ]]; then
  log "GUARD: test paths in BACKUP_DIR/STORAGEBOX_DIR/STATE_DIR — all mail routes to ${ALERT_OPS_TO} only (9806fc14)"
fi

if [[ "$BACKUP_FAILED" -eq 1 ]]; then
  # Keep the reason to one bounded line (it reaches a mail subject/body).
  BACKUP_FAILED_REASON="$(printf '%s' "$BACKUP_FAILED_REASON" | tr -d '\r' | tr '\n' ' ' | cut -c1-400)"
  if [[ "$AGE_S" -lt 0 ]]; then
    LAST_GOOD="none found"
  else
    LAST_GOOD="${NEWEST_PATH} (age ~$(( AGE_S / 3600 ))h)"
  fi
  FAIL_SUBJECT="[FAIL] Hub DB backup run failed on $(hostname)"
  FAIL_BODY="Hub DB backup run FAILED $(date -u '+%Y-%m-%dT%H:%M:%SZ') on $(hostname).
Reason: ${BACKUP_FAILED_REASON}
Last good export: ${LAST_GOOD}
Log: /var/log/hub-db-backup.log
Rerun: /usr/local/sbin/hub-db-backup.sh (read-only on the Hub DB); see docs/HUB-DB-BACKUP.md."
  if [[ "$FAIL_DRILL" -eq 1 ]]; then
    # Drill: exactly one log line; never send_mail or any MTA.
    drill_log "backup-failed alert suppressed" "backup_run_failed reason='${BACKUP_FAILED_REASON}' last_good=${LAST_GOOD}"
    exit 0
  fi
  log "BACKUP_FAILED reason='${BACKUP_FAILED_REASON}' last_good=${LAST_GOOD}"
  if [[ "$DRY_RUN" -eq 1 || "$NO_MAIL" -eq 1 ]]; then
    log "would-alert backup-failed to=$(effective_to) (mail suppressed by flag)"
    exit 0
  fi
  send_mail "$FAIL_SUBJECT" "$FAIL_BODY"
  exit 0
fi

if [[ "$HUB_DB_STALE_DRILL" == "1" ]]; then
  if [[ "$DRY_RUN" -eq 1 || "$NO_MAIL" -eq 1 ]]; then
    drill_log "staleness alert suppressed (state untouched by flag)" "forced_stale age_s=${AGE_S}"
    exit 2
  fi
  drill_log "staleness alert suppressed" "forced_stale age_s=${AGE_S} path=${NEWEST_PATH:-none}"
  write_state 1 0 1
  exit 2
fi

if [[ "$IS_STALE" -eq 0 ]]; then
  if [[ "${WAS_STALE:-0}" -eq 1 && "${WAS_DRILL:-0}" -eq 1 && "$NO_MAIL" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
    # Close a drill: exactly one [DRILL] log line (no separate "fresh:" line)
    # and no mail; the forced FAIL never pages anyone backward.
    drill_log "staleness drill cleared, RECOVERED suppressed" "fresh age_s=${AGE_S} path=${NEWEST_PATH}"
    write_state 0 "${LAST_ALERT:-0}" 0
    exit 0
  fi
  AGE_H="$(python3 -c "print(f'{$AGE_S/3600:.1f}')" 2>/dev/null || echo '?')"
  log "fresh: newest export age ${AGE_S}s (~${AGE_H}h) path=${NEWEST_PATH}"
  if [[ "${WAS_STALE:-0}" -eq 1 && "$NO_MAIL" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
    send_mail "RECOVERED: Hub DB backup fresh again" \
      "Hub DB backup is fresh again. Newest export: ${NEWEST_PATH} (age ${AGE_S}s)."
  fi
  write_state 0 "${LAST_ALERT:-0}" 0
  exit 0
fi

# Stale branch.
if [[ "$AGE_S" -lt 0 ]]; then
  AGE_DESC="no exports found"
else
  AGE_DESC="newest export age ${AGE_S}s (~$(( AGE_S / 3600 ))h), threshold ${STALE_AFTER_HOURS}h path=${NEWEST_PATH:-none}"
fi
log "STALE: ${AGE_DESC}"

if [[ "$DRY_RUN" -eq 1 || "$NO_MAIL" -eq 1 ]]; then
  local_guard_note=""
  [[ "$TEST_PATH_GUARD" -eq 1 ]] && local_guard_note=" (guard: ops-only)"
  log "would-alert to=$(effective_to)${local_guard_note} (mail suppressed by flag)"
  exit 2
fi

ELAPSED_H=999999
if [[ "$LAST_ALERT" -gt 0 ]]; then
  ELAPSED_H=$(( (NOW - LAST_ALERT) / 3600 ))
fi
if [[ "${WAS_STALE:-0}" -eq 1 && "${WAS_DRILL:-0}" -eq 0 && "$ELAPSED_H" -lt "$ALERT_REPEAT_HOURS" ]]; then
  log "stale but repeat window not reached (${ELAPSED_H}h < ${ALERT_REPEAT_HOURS}h); mail suppressed"
  exit 2
fi
# A pending drill marker never suppresses a real stale page: the drill
# records LAST_ALERT=0, so the window check above already lets it through,
# and this state write drops the DRILL marker.

send_mail "[STALE] Hub DB backup older than ${STALE_AFTER_HOURS}h" \
"Hub DB backup is STALE.
${AGE_DESC}
Local dir: ${BACKUP_DIR}
Storagebox dir: ${STORAGEBOX_DIR}
Run scripts/hub-db-backup.sh and see docs/HUB-DB-BACKUP.md."
write_state 1 "$NOW" 0
exit 2
