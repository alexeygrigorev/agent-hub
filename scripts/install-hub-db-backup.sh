#!/usr/bin/env bash
# Install the Hub DB off-Cloudflare backup on the Hetzner operator host.
# Copies scripts (never env), installs cron, never overwrites existing env.
# Usage: scripts/install-hub-db-backup.sh [--prefix /usr/local/sbin]
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

for f in "$SRC_BACKUP" "$SRC_STALE" "$SRC_CRON" "$SRC_STALE_CRON" "$SRC_ENV_EXAMPLE"; do
  [[ -f "$f" ]] || { echo "missing: $f" >&2; exit 1; }
done

install -m 0755 "$SRC_BACKUP" "$PREFIX/hub-db-backup.sh"
install -m 0755 "$SRC_STALE" "$PREFIX/hub-db-backup-staleness.sh"

if [[ -f "$ENV_FILE" ]]; then
  echo "keep existing env: $ENV_FILE (not overwritten)"
else
  install -m 0600 "$SRC_ENV_EXAMPLE" "$ENV_FILE"
  echo "installed placeholder env: $ENV_FILE (fill real values, chmod 600)"
fi

install -m 0644 "$SRC_CRON" /etc/cron.d/hub-db-backup
install -m 0644 "$SRC_STALE_CRON" /etc/cron.d/hub-db-backup-staleness

echo "installed. Next:"
echo "  1. fill $ENV_FILE (HUB_ADMIN_TOKEN_FILE must point at a 600 operator token file)"
echo "  2. sudo $PREFIX/hub-db-backup.sh  # first manual run, expect ALL_DONE"
echo "  3. $PREFIX/hub-db-backup-staleness.sh --dry-run  # expect fresh"
echo "  4. cat /etc/cron.d/hub-db-backup /etc/cron.d/hub-db-backup-staleness"
