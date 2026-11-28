#!/usr/bin/env bash
# RMTHZ owns off-VM copies and mail. Never copy credentials between hosts.
# --no-mail is mandatory for manual verification; cron uses the real alert path.
set -euo pipefail

# Optional unqualified metadata only; source-session contract V2.
_BACKUP_ALERT_DIRTY=0
_BACKUP_ALERT_DECIDED=0
_BACKUP_ALERT_CLAIM=""
_BACKUP_ALERT_READ=""
_BACKUP_ALERT_WRITE=""
_BACKUP_ALERT_PID=""
_BACKUP_ALERT_SEQ=0
_BACKUP_ALERT_ROLE=backup_pull
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
  timeout --signal=KILL 0.75s python3 -I -S -c '# JSON-only local-FD reader. No client imports, sockets, peer identity or counts.
import json,os,re,select,sys,time,uuid
fd,kind,role,parent,expected=int(sys.argv[1]),*sys.argv[2:]
end=time.monotonic()+.7
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

ENV_FILE="${HUB2_BACKUP_PULL_ENV_FILE:-/etc/hub2-backup-pull.env}"
source "$ENV_FILE"
NO_MAIL=0
case "${1:-}" in
  --no-mail) NO_MAIL=1 ;;
  '') ;;
  *) echo 'Usage: hub2-backup-pull.sh [--no-mail]' >&2; exit 1 ;;
esac
: "${VM_HOST:?}" "${SSH_KEY:?}" "${BACKUP_DIR:?}" "${STORAGEBOX_DIR:?}" "${STATE_DIR:?}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ && "$RETENTION_DAYS" -ge 30 ]]
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
exec 9>"$STATE_DIR/pull.lock"
if ! flock -n 9; then
  backup_alert_event '{"phase":"decision","decision":"observation","condition":"lock_busy","recipient_class":"none","flags":{}}'
  _BACKUP_ALERT_DECIDED=1
  exit 0
fi
alert() {
  rc="$1"
  if [[ "$rc" != 0 ]]; then
    args=(--backup-failed "Hub2 snapshot/off-VM replication failed (exit $rc); see /var/log/hub2-backup-pull.log")
    [[ "$NO_MAIL" == 0 ]] || args+=(--no-mail)
    HUB_BACKUP_ALERT_PARENT_CLAIM="$_BACKUP_ALERT_CLAIM" HUB_DB_BACKUP_ENV_FILE="$ENV_FILE" "${STALENESS_SCRIPT:-/usr/local/sbin/hub2-db-backup-staleness.sh}" "${args[@]}" || true
  fi
  backup_alert_event "{\"phase\":\"decision\",\"decision\":\"observation\",\"condition\":\"pull_terminal\",\"recipient_class\":\"none\",\"flags\":{}}"
  _BACKUP_ALERT_DECIDED=1
  backup_alert_terminal "$rc"
}
trap 'alert "$?"' EXIT
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes -i "$SSH_KEY")
# Explicit non-secret configuration: no VM mail transport. This coordinator
# reports failures through RMTHZ instead. VACUUM is online; no service restart.
"${SSH[@]}" "$VM_HOST" 'sudo -n env HUB2_DB_BACKUP_ENV_FILE=/dev/null MIRROR_DIR=/var/backups/hub-do-mirror ALERT_CMD=/bin/true /usr/local/sbin/hub2-db-backup.sh'
for tree in "$BACKUP_DIR" "$STORAGEBOX_DIR"; do
  mkdir -p "$tree"
  rsync -rt --chmod=F600 --rsync-path='sudo -n rsync' \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes -i $SSH_KEY" \
    --include='*/' --include='hub2-db-*.tar.gz' --exclude='*' \
    "$VM_HOST:/var/backups/hub-do/" "$tree/"
  # Never prune the retained Hub1 exports, or use rsync --delete.
  find "$tree" -type f -name 'hub2-db-*.tar.gz' -mtime "+$RETENTION_DAYS" -delete
done
HUB_BACKUP_ALERT_PARENT_CLAIM="$_BACKUP_ALERT_CLAIM" HUB_DB_BACKUP_ENV_FILE="$ENV_FILE" "${STALENESS_SCRIPT:-/usr/local/sbin/hub2-db-backup-staleness.sh}" --check-only
echo 'OK: Hub2 snapshot and RMTHZ/Storagebox copies complete'
