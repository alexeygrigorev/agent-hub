#!/usr/bin/env bash
# Hub2 DB backup for the GCP VM deployment (port-gcloud, 2026-11-07).
#
# Hub2 on the GCP VM stores every Durable Object SQLite database locally
# under the Node runtime data dir (one SQLite file per object instance):
#   <HUB2_DB_DIR>/<ClassName>-<instance>.sqlite
# plus -wal/-shm siblings. The DB lives on the machine, so the daily
# backup is a consistent FILE snapshot instead of the hub1 REST export:
# each db.sqlite is copied with `VACUUM INTO` (online, WAL-safe, takes a
# read transaction — never stops the worker), packed with a counts manifest
# into one gzip tarball, and stored with the SAME tree layout, retention
# floor and failure alerting as scripts/hub-db-backup.sh:
#
#   <BACKUP_DIR>/YYYY/MM/DD/hub2-db-<STAMP>.tar.gz       (copy #1)
#   <MIRROR_DIR>/YYYY/MM/DD/hub2-db-<STAMP>.tar.gz       (copy #2, optional)
#
# STAMP is Europe/Berlin %Y%m%dT%H%M%z. Artifacts get mode 600. Retention
# prunes ONLY files with age >= RETENTION_DAYS+1 whole days (`find -mtime
# +N`); RETENTION_DAYS is enforced >= 30 and fails closed otherwise. Each
# tree is pruned independently.
#
# Manifest (inside the tarball as manifest.json): for every database, its
# tarball-relative path, PRAGMA integrity_check result, schema_version
# (PRAGMA user_version), and per-table row counts. The sibling
# scripts/hub2-db-restore-drill.py replays the tarball into a throwaway
# directory and verifies integrity + exact counts against the manifest.
#
# Auth/secrets: none needed — the DB is local. Nothing is printed except
# paths and counts; table contents never leave the tarball.
#
# Failure alerting mirrors hub-db-backup.sh: any non-zero exit (config,
# snapshot, validation, store, mirror) runs ALERT_CMD (default: the sibling
# hub-db-backup-staleness.sh --backup-failed "<reason>", same mail path and
# test-path guard). HUB2_DB_BACKUP_FAIL_DRILL=1 forces an immediate failure
# whose alert is NEVER mailed: the run logs exactly one "[DRILL]" line and
# touches no database and no backup tree.
#
# Locking: flock on LOCK_FILE (default /var/lib/hub2-db-backup/hub2-db-backup.lock);
# a second concurrent run exits 0 with a "SKIP" log line (daily cron + manual
# runs must never race the same trees). Temp files live under STATE_DIR/tmp
# (default /var/lib/hub2-db-backup/tmp), never /tmp, created 700.
#
# Install (GCP VM, runs as root via cron or systemd timer):
#   /etc/hub2-db-backup.env holds overrides (mode 600), see the env block.
#   cron: 20 4 * * * root  /usr/local/sbin/hub2-db-backup.sh >>/var/log/hub2-db-backup.log 2>&1
#
# Exit code: 0 only if snapshot+validate+store+mirror all succeeded.
set -euo pipefail

ENV_FILE="${HUB2_DB_BACKUP_ENV_FILE:-/etc/hub2-db-backup.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

HUB2_DB_DIR="${HUB2_DB_DIR:-/var/lib/agent-hub/hub2-db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/hub-do}"
MIRROR_DIR="${MIRROR_DIR:-}"                       # empty = single-tree mode
RETENTION_DAYS="${RETENTION_DAYS:-30}"
LOCK_FILE="${LOCK_FILE:-/var/lib/hub2-db-backup/hub2-db-backup.lock}"
STATE_DIR="${STATE_DIR:-/var/lib/hub2-db-backup}"
ALERT_CMD="${ALERT_CMD:-}"
DRILL="${HUB2_DB_BACKUP_FAIL_DRILL:-0}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

fail() { # reason
  log "FAIL: $1"
  if [[ "$DRILL" == "1" ]]; then
    log "[DRILL] backup-failed alert suppressed result=hub2_backup_run_failed reason='$1' mail=none"
  else
    if [[ -z "$ALERT_CMD" ]]; then
      ALERT_CMD="$(dirname "${BASH_SOURCE[0]}")/hub-db-backup-staleness.sh"
    fi
    if [[ -x "$ALERT_CMD" || -f "$ALERT_CMD" ]]; then
      "$ALERT_CMD" --backup-failed "$1" >>"${STATE_DIR}/alert.log" 2>&1 || true
      log "alert dispatched result=hub2_backup_run_failed reason='$1'"
    else
      log "alert skipped (no ALERT_CMD at $ALERT_CMD)"
    fi
  fi
  exit 1
}

# ---- config validation (fail closed) ---------------------------------------
if [[ "$RETENTION_DAYS" -lt 30 ]]; then
  fail "RETENTION_DAYS must be >= 30 (got $RETENTION_DAYS)"
fi
if [[ ! -d "$HUB2_DB_DIR" ]]; then
  fail "HUB2_DB_DIR missing: $HUB2_DB_DIR"
fi
case "$BACKUP_DIR" in /tmp/*|/var/tmp/*|/tmp|/var/tmp)
  fail "BACKUP_DIR must not live under /tmp or /var/tmp";; esac

mkdir -p "$BACKUP_DIR" "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true
TMPROOT="$STATE_DIR/tmp"
mkdir -p "$TMPROOT"
chmod 700 "$TMPROOT"

# ---- drill mode: fail before touching anything ------------------------------
if [[ "$DRILL" == "1" ]]; then
  fail "forced by HUB2_DB_BACKUP_FAIL_DRILL=1, no database or tree touched"
fi

# ---- lock --------------------------------------------------------------------
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "SKIP: another hub2-db-backup run holds $LOCK_FILE"
  exit 0
fi

# ---- collect DO databases ----------------------------------------------------
WORK="$(mktemp -d "$TMPROOT/hub2-backup.XXXXXXXX")"
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mapfile -t DBS < <(find "$HUB2_DB_DIR" -maxdepth 1 -type f -name "*.sqlite" | sort)
if [[ "${#DBS[@]}" -eq 0 ]]; then
  fail "no DO databases found under $HUB2_DB_DIR (is the worker running with --persist-to $HUB2_DB_DIR?)"
fi

MANIFEST="$WORK/manifest.json"
{
  printf '{\n  "snapshot": "hub2-node-sqlite",\n  "generated": "%s",\n  "db_dir": %s,\n  "databases": [\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "\"$HUB2_DB_DIR\""
} >"$MANIFEST"

first=1
for db in "${DBS[@]}"; do
  rel="$(realpath --relative-to="$HUB2_DB_DIR" "$db")"
  dest="$WORK/snapshot/$rel"
  mkdir -p "$(dirname "$dest")"
  # VACUUM INTO reads through a transaction; safe against a live worker.
  if ! sqlite3 "file:$db?mode=ro" "VACUUM INTO '$dest';" 2>"$WORK/vacuum.err"; then
    fail "vacuum failed for $rel: $(cat "$WORK/vacuum.err")"
  fi
  chmod 600 "$dest"
  integrity="$(sqlite3 "$dest" 'PRAGMA integrity_check;')"
  [[ "$integrity" == "ok" ]] || fail "integrity_check not ok for $rel: $integrity"
  user_version="$(sqlite3 "$dest" 'PRAGMA user_version;')"
  tables_json="$(
    sqlite3 -json "$dest" \
      "SELECT name, (SELECT COUNT(*) FROM sqlite_master m2 WHERE m2.type='view' AND m2.name=m.name) AS is_view FROM sqlite_master m WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;" \
    | python3 -c 'import json,sys; print(json.dumps([r["name"] for r in json.load(sys.stdin) if not r["is_view"]]))' 2>/dev/null
  )" || tables_json="[]"
  counts_json="$(
    python3 - "$dest" "$tables_json" <<'PYEOF'
import json, sqlite3, sys
db, tables = sys.argv[1], json.loads(sys.argv[2])
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
counts = {}
for t in tables:
    counts[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
print(json.dumps(counts, sort_keys=True))
PYEOF
  )" || fail "count query failed for $rel"
  [[ "$first" == "1" ]] || printf ',\n' >>"$MANIFEST"
  first=0
  printf '    {"path": %s, "integrity": "ok", "user_version": %s, "tables": %s}' \
    "\"$rel\"" "$user_version" "$counts_json" >>"$MANIFEST"
done
printf '\n  ]\n}\n' >>"$MANIFEST"

# ---- assemble artifact -------------------------------------------------------
STAMP="$(TZ=Europe/Berlin date +%Y%m%dT%H%M%z)"
ARTIFACT="hub2-db-${STAMP}.tar.gz"
RELDIR="$(TZ=Europe/Berlin date +%Y/%m/%d)"
mkdir -p "$WORK/pack/$RELDIR"
# shellcheck disable=SC2211  # intentional: paths inside the tarball are relative
tar -C "$WORK" -czf "$WORK/pack/$RELDIR/$ARTIFACT" \
  --transform "s|^snapshot/||" snapshot manifest.json
chmod 600 "$WORK/pack/$RELDIR/$ARTIFACT"

stored=0
install -D -m 600 "$WORK/pack/$RELDIR/$ARTIFACT" "$BACKUP_DIR/$RELDIR/$ARTIFACT"
log "stored $BACKUP_DIR/$RELDIR/$ARTIFACT"
stored=1
if [[ -n "$MIRROR_DIR" ]]; then
  case "$MIRROR_DIR" in /tmp/*|/var/tmp/*) fail "MIRROR_DIR must not live under /tmp or /var/tmp";; esac
  mkdir -p "$MIRROR_DIR" 2>/dev/null || fail "cannot create MIRROR_DIR $MIRROR_DIR"
  install -D -m 600 "$WORK/pack/$RELDIR/$ARTIFACT" "$MIRROR_DIR/$RELDIR/$ARTIFACT" ||
    fail "mirror write failed: $MIRROR_DIR/$RELDIR/$ARTIFACT"
  log "mirrored $MIRROR_DIR/$RELDIR/$ARTIFACT"
  stored=2
fi

# ---- retention (each tree independently, floor enforced) ---------------------
prune() {
  local root="$1"
  find "$root" -type f -name 'hub2-db-*.tar.gz' -mtime "+$RETENTION_DAYS" -print -delete |
    while IFS= read -r old; do log "pruned $old"; done
}
prune "$BACKUP_DIR"
if [[ -n "$MIRROR_DIR" ]]; then prune "$MIRROR_DIR"; fi

dbs="${#DBS[@]}"
log "OK: snapshot=$ARTIFACT databases=$dbs copies=$stored retention_days=$RETENTION_DAYS"
