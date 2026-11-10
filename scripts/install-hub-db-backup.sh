#!/usr/bin/env bash
# Hub1 export API and scheduled backups retired at the Hub2 cutover.
# Fail before changing scripts, cron, credentials, state or backup trees.
set -euo pipefail
cat >&2 <<'NOTICE'
RETIRED: Hub1 DB backup was replaced by Hub2 snapshots on 2026-11-08.
Do not reinstall hub-db-backup or hub-db-backup-staleness cron jobs.
Use the active Hub2 pipeline documented in docs/HUB-DB-BACKUP.md.
Existing Hub1 exports and credentials must be preserved.
NOTICE
exit 1
