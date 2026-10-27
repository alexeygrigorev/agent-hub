#!/usr/bin/env bash
# Install the Hub DB off-Cloudflare backup on the Hetzner operator host.
# Copies scripts (never env), installs cron, never overwrites existing env.
# The job runs as user alexey (not root): root cannot write the operator's
# Storagebox sshfs mount (FUSE denies root), while alexey owns the token
# file, both backup trees and the state dir.
# Usage: sudo scripts/install-hub-db-backup.sh [--prefix /usr/local/sbin]
set -euo pipefail

PREFIX="${1:---prefix}"
if [[ "$PREFIX" == "--prefix" ]]; then
  PREFIX="${2:-/usr/local/sbin}"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_BACKUP="$REPO_ROOT/scripts/hub-db-backup.sh"
SRC_STALE="$REPO_ROOT/scripts/hub-db-backup-staleness.sh"
SRC_CRON="$REPO_ROOT/scripts/hub-db-backup.cron"
SRC_STALE_CRON="$REPO_ROOT/scripts/hub-db-backup-staleness.cron"
SRC_ENV_EXAMPLE="$REPO_ROOT/scripts/hub-db-backup.env.example"
ENV_FILE="${HUB_DB_BACKUP_ENV_FILE:-/etc/hub-db-backup.env}"
RUN_USER="${HUB_DB_BACKUP_USER:-alexey}"

for f in "$SRC_BACKUP" "$SRC_STALE" "$SRC_CRON" "$SRC_STALE_CRON" "$SRC_ENV_EXAMPLE"; do
  [[ -f "$f" ]] || { echo "missing: $f" >&2; exit 1; }
done
id "$RUN_USER" >/dev/null 2>&1 || { echo "missing user: $RUN_USER" >&2; exit 1; }

install -m 0755 "$SRC_BACKUP" "$PREFIX/hub-db-backup.sh"
install -m 0755 "$SRC_STALE" "$PREFIX/hub-db-backup-staleness.sh"

if [[ -f "$ENV_FILE" ]]; then
  echo "keep existing env: $ENV_FILE (not overwritten)"
  # Fill keys added by newer revisions without touching existing values.
  for kv in "LOCK_FILE=/var/lib/hub-db-backup/hub-db-backup.lock"; do
    k="${kv%%=*}"
    grep -qE "^${k}=" "$ENV_FILE" || echo "$kv" >> "$ENV_FILE"
  done
else
  install -m 0600 -o "$RUN_USER" -g "$RUN_USER" "$SRC_ENV_EXAMPLE" "$ENV_FILE"
  echo "installed placeholder env: $ENV_FILE (fill real values, keep 0600 $RUN_USER)"
fi
# The cron user must own its env file (mode 600); /etc stays root-owned.
if [[ "$(stat -c%U:%a "$ENV_FILE" 2>/dev/null)" != "$RUN_USER:600" ]]; then
  chown "$RUN_USER:$RUN_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

# Local trees the cron user must own (defaults match hub-db-backup.env.example).
mkdir -p /var/backups/hub-do /var/lib/hub-db-backup
chown "$RUN_USER:$RUN_USER" /var/backups/hub-do /var/lib/hub-db-backup
chmod 700 /var/lib/hub-db-backup
touch /var/log/hub-db-backup.log /var/log/hub-db-backup-staleness.log
chown "$RUN_USER:$RUN_USER" /var/log/hub-db-backup.log /var/log/hub-db-backup-staleness.log

install -m 0644 "$SRC_CRON" /etc/cron.d/hub-db-backup
install -m 0644 "$SRC_STALE_CRON" /etc/cron.d/hub-db-backup-staleness

echo "installed (cron user: $RUN_USER). Next:"
echo "  1. fill $ENV_FILE (HUB_ADMIN_TOKEN_FILE must point at a 600 operator token file readable by $RUN_USER)"
echo "  2. sudo -u $RUN_USER $PREFIX/hub-db-backup.sh  # first manual run, expect ALL_DONE"
echo "  3. sudo -u $RUN_USER $PREFIX/hub-db-backup-staleness.sh --dry-run  # expect fresh"
echo "  4. cat /etc/cron.d/hub-db-backup /etc/cron.d/hub-db-backup-staleness"
