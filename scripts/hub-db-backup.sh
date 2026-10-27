#!/usr/bin/env bash
# Hub DO SQLite off-Cloudflare daily export (intake c1241bea).
#
# Fetches the operator-only read-only snapshot from
#   GET /api/backup/export
# and writes it OFF Cloudflare (Hetzner local + Storagebox mirror):
#   - Hetzner local dir  (BACKUP_DIR, default /var/backups/hub-do)
#   - Storagebox mirror  (STORAGEBOX_DIR, default ~/storagebox/backups/hub-do)
# as gzip files with mode 600. Retention keeps every copy younger than
# RETENTION_DAYS (default 30); nothing younger is ever deleted.
#
# Auth: the operator token is read from a mode-600 file
# (HUB_ADMIN_TOKEN_FILE) and passed to curl via a mode-600 --config file, so
# it never appears in the environment, the shell history, or process argv
# (ps aux). No secrets are printed or logged.
# The export itself contains no secrets beyond what Hub already stores
# (credential rows carry hashes, never plaintext tokens).
#
# Layout (machine-management S3 canonical pattern, local form):
#   <root>/YYYY/MM/DD/hub-do-<STAMP>.json.gz
# where STAMP is Europe/Berlin %Y%m%dT%H%M%z.
#
# Export size is bounded server-side (per-table/total row caps, HTTP 413);
# this script treats any non-200 as a failed run and writes nothing.
#
# Exit code: 0 only if fetch+validate+store+mirror all succeeded.
set -euo pipefail

ENV_FILE="${HUB_DB_BACKUP_ENV_FILE:-/etc/hub-db-backup.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail() { log "ERROR: $*"; exit 1; }

HUB_BASE_URL="${HUB_BASE_URL:-https://hub.dtcdev.click}"
HUB_ADMIN_TOKEN_FILE="${HUB_ADMIN_TOKEN_FILE:-/home/alexey/.config/agent-hub/admin-token}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/hub-do}"
STORAGEBOX_DIR="${STORAGEBOX_DIR:-/home/alexey/storagebox/backups/hub-do}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
LOCK_FILE="${LOCK_FILE:-/var/lock/hub-db-backup.lock}"
CURL_TIMEOUT="${CURL_TIMEOUT:-60}"

[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || fail "RETENTION_DAYS must be an integer (got ${RETENTION_DAYS})"
(( RETENTION_DAYS >= 30 )) || fail "RETENTION_DAYS=${RETENTION_DAYS} violates AC: retention >= 30 days"
# HUB_BASE_URL must be https (operator token bearer); localhost http is
# allowed only for local drills via explicit opt-out.
if [[ "$HUB_BASE_URL" =~ ^http://(localhost|127\.0\.0\.1)(:[0-9]+)?(/|$) ]]; then
  [[ "${HUB_ALLOW_HTTP_LOCAL:-0}" == "1" ]] || fail "refusing cleartext HUB_BASE_URL without HUB_ALLOW_HTTP_LOCAL=1: ${HUB_BASE_URL}"
elif [[ ! "$HUB_BASE_URL" =~ ^https:// ]]; then
  fail "HUB_BASE_URL must use https: ${HUB_BASE_URL}"
fi
[[ -f "$HUB_ADMIN_TOKEN_FILE" ]] || fail "token file missing: ${HUB_ADMIN_TOKEN_FILE}"
TOKEN_MODE="$(stat -c%a "$HUB_ADMIN_TOKEN_FILE" 2>/dev/null || stat -f%Lp "$HUB_ADMIN_TOKEN_FILE" 2>/dev/null || echo unknown)"
[[ "$TOKEN_MODE" == "600" ]] || fail "token file must be mode 600 (got ${TOKEN_MODE}): ${HUB_ADMIN_TOKEN_FILE}"

for bin in curl gzip python3 flock chmod; do
  command -v "$bin" >/dev/null 2>&1 || fail "missing required binary: $bin"
done

# Temp root: absolute, created, never under /tmp (AGENTS.md). Overridable for
# tests via TMPDIR; the cron default has no TMPDIR, so this resolves to the
# absolute /var/lib path (cron CWD is $HOME, where a relative .local/tmp
# would not exist).
TMP_ROOT="${TMPDIR:-/var/lib/hub-db-backup/tmp}"
case "$TMP_ROOT" in
  /tmp|/tmp/*|/var/tmp|/var/tmp/*) fail "TMPDIR must not be under /tmp or /var/tmp (got ${TMP_ROOT})" ;;
esac
mkdir -p "$TMP_ROOT" || fail "cannot create temp dir: ${TMP_ROOT}"
chmod 700 "$TMP_ROOT"

STAMP="$(TZ=Europe/Berlin date '+%Y%m%dT%H%M%z')"
DAY_PATH="$(TZ=Europe/Berlin date '+%Y/%m/%d')"
REL="hub-do-${STAMP}.json.gz"

mkdir -p "$BACKUP_DIR" "$STORAGEBOX_DIR" || fail "cannot create backup dirs"
LOCK_DIR="$(dirname "$LOCK_FILE")"
mkdir -p "$LOCK_DIR" 2>/dev/null || true
exec 9>"$LOCK_FILE" || fail "cannot open lock ${LOCK_FILE}"
flock -n 9 || fail "another hub-db-backup run is in progress (lock: ${LOCK_FILE})"

TMP_RAW="$(mktemp -p "$TMP_ROOT" hub-do-raw-XXXXXX.json)"
TMP_GZ="$(mktemp -p "$TMP_ROOT" hub-do-gz-XXXXXX.json.gz)"
TMP_CURL_ERR="$(mktemp -p "$TMP_ROOT" hub-do-curl-err-XXXXXX.txt)"
TMP_CURL_CFG="$(mktemp -p "$TMP_ROOT" hub-do-curl-cfg-XXXXXX)"
chmod 600 "$TMP_CURL_CFG"
cleanup() { rm -f -- "$TMP_RAW" "$TMP_GZ" "$TMP_CURL_ERR" "$TMP_CURL_CFG"; }
trap cleanup EXIT INT TERM

TOKEN="$(cat "$HUB_ADMIN_TOKEN_FILE")"
[[ -n "$TOKEN" ]] || fail "token file is empty"
# Bearer header via --config so the token never appears in process argv.
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$TMP_CURL_CFG"
TOKEN=""

HTTP_CODE="$(curl -sS -o "$TMP_RAW" -w '%{http_code}' -K "$TMP_CURL_CFG" \
  --max-time "$CURL_TIMEOUT" \
  -H 'User-Agent: agent-hub-host/1' -H 'Accept: application/json' \
  "${HUB_BASE_URL%/}/api/backup/export" 2>"$TMP_CURL_ERR" || true)"
rm -f -- "$TMP_CURL_CFG"
if [[ "$HTTP_CODE" != "200" ]]; then
  log "ERROR: export fetch failed HTTP ${HTTP_CODE} (curl exit logged, no body stored)"
  exit 1
fi
rm -f "$TMP_CURL_ERR"

# Validate: must be a JSON object with snapshot hub-do-sqlite, non-empty
# tables map, and row_counts matching table lengths. No secrets are printed.
python3 - "$TMP_RAW" <<'PY' || fail "export validation failed"
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
try:
    body = json.loads(raw)
except Exception as exc:
    print(f"invalid JSON: {exc}", file=sys.stderr)
    sys.exit(1)
assert isinstance(body, dict), "top-level JSON must be an object"
assert body.get("snapshot") == "hub-do-sqlite", "snapshot marker mismatch"
tables = body.get("tables")
counts = body.get("row_counts")
schema = body.get("schema")
assert isinstance(tables, dict) and tables, "tables map missing/empty"
assert isinstance(counts, dict) and set(counts) == set(tables), "row_counts mismatch"
assert isinstance(schema, dict) and set(tables) <= set(schema), "schema must cover all tables"
markers = body.get("value_markers")
assert isinstance(markers, dict), "value_markers missing"
assert set(markers) == {"bigint", "blob"} and all(isinstance(markers[k], int) for k in markers), "value_markers shape"
for name, rows in tables.items():
    assert isinstance(rows, list), f"table is not a list: {name}"
    assert counts[name] == len(rows), f"row count mismatch: {name}"
# Core Hub datasets must be present: intake tasks, sessions, claims, audit.
for required in ("intake_tasks", "session_control_sessions", "claims", "audit"):
    assert required in tables, f"required table missing: {required}"
# Ephemeral rate-limit state is never exported (would leak caller keys).
assert "backup_rate" not in tables, "backup_rate must be excluded from export"
print(f"validated {len(tables)} tables, {sum(counts.values())} rows, {len(schema)} schema objects, markers={markers}")
PY

gzip -c "$TMP_RAW" > "$TMP_GZ"
chmod 600 "$TMP_GZ"
SIZE="$(stat -c%s "$TMP_GZ" 2>/dev/null || stat -f%z "$TMP_GZ")"
[[ "$SIZE" -gt 1024 ]] || fail "export gzip suspiciously small (${SIZE}B)"

LOCAL_DEST="${BACKUP_DIR%/}/${DAY_PATH}/${REL}"
STORAGE_DEST="${STORAGEBOX_DIR%/}/${DAY_PATH}/${REL}"
mkdir -p "$(dirname "$LOCAL_DEST")" "$(dirname "$STORAGE_DEST")"
cp -- "$TMP_GZ" "$LOCAL_DEST"
chmod 600 "$LOCAL_DEST"
cp -- "$TMP_GZ" "$STORAGE_DEST"
chmod 600 "$STORAGE_DEST"

log "backup ok tables validated size=${SIZE}B local=${LOCAL_DEST} storagebox=${STORAGE_DEST}"

# Retention: delete ONLY copies strictly older than RETENTION_DAYS whole days
# (find -mtime +N matches files with age >= N+1 days, so +30 keeps a full
# 30-day window plus the current partial day). Both trees are pruned
# independently; prune failures never fail the backup itself.
for root in "$BACKUP_DIR" "$STORAGEBOX_DIR"; do
  if [[ -d "$root" ]]; then
    find "$root" -type f -name 'hub-do-*.json.gz' -mtime "+${RETENTION_DAYS}" -print -delete 2>/dev/null \
      | while IFS= read -r old; do log "pruned ${old} (older than ${RETENTION_DAYS}d)"; done || true
    find "$root" -type d -empty -delete 2>/dev/null || true
  fi
done

log "ALL_DONE local=${LOCAL_DEST}"
