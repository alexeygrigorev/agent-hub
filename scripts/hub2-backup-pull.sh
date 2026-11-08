#!/usr/bin/env bash
# RMTHZ owns off-VM copies and mail. Never copy credentials between hosts.
# --no-mail is mandatory for manual verification; cron uses the real alert path.
set -euo pipefail
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
flock -n 9 || exit 0
alert() {
  rc=$?
  if [[ "$rc" != 0 ]]; then
    args=(--backup-failed "Hub2 snapshot/off-VM replication failed (exit $rc); see /var/log/hub2-backup-pull.log")
    [[ "$NO_MAIL" == 0 ]] || args+=(--no-mail)
    HUB_DB_BACKUP_ENV_FILE="$ENV_FILE" "${STALENESS_SCRIPT:-/usr/local/sbin/hub2-db-backup-staleness.sh}" "${args[@]}" || true
  fi
}
trap alert EXIT
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
HUB_DB_BACKUP_ENV_FILE="$ENV_FILE" "${STALENESS_SCRIPT:-/usr/local/sbin/hub2-db-backup-staleness.sh}" --check-only
echo 'OK: Hub2 snapshot and RMTHZ/Storagebox copies complete'
