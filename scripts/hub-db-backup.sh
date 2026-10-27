#!/usr/bin/env bash
# Hub DO SQLite off-Cloudflare daily export (intake c1241bea).
#
# Fetches the operator-only read-only snapshot CHUNKED (bounded rows per
# request, so giant tables/exports never exceed DO memory):
#   GET /api/backup/status                 (table names + row counts)
#   GET /api/backup/export?schema=1        (full DDL, no rows)
#   GET /api/backup/export?table=X&offset=N&limit=M  (row slices, 429/413/500 fail-closed)
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
# Export size is bounded server-side (per-table/total row caps, HTTP 413) and
# each chunked read is capped (CHUNK_ROWS, endpoint enforces <= 5000);
# this script treats any non-200 as a failed run and writes nothing.
#
# Chunk consistency: each chunk is internally consistent (single server-side
# transaction); hot tables may drift between the pre/post status reads and
# the chunk loop. Assembly is accepted when every table's assembled count is
# within tolerance of the post-read status (see validation below); larger
# drift fails the run and cron retries the next day.
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
LOCK_FILE="${LOCK_FILE:-/var/lib/hub-db-backup/hub-db-backup.lock}"
CURL_TIMEOUT="${CURL_TIMEOUT:-60}"
CHUNK_ROWS="${CHUNK_ROWS:-500}"

[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || fail "RETENTION_DAYS must be an integer (got ${RETENTION_DAYS})"
(( RETENTION_DAYS >= 30 )) || fail "RETENTION_DAYS=${RETENTION_DAYS} violates AC: retention >= 30 days"
[[ "$CHUNK_ROWS" =~ ^[0-9]+$ ]] && (( CHUNK_ROWS >= 1 && CHUNK_ROWS <= 5000 )) || fail "CHUNK_ROWS must be an integer 1..5000 (got ${CHUNK_ROWS})"
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
TMP_PARTS="$(mktemp -d -p "$TMP_ROOT" hub-do-parts-XXXXXX)"
chmod 700 "$TMP_PARTS"
chmod 600 "$TMP_CURL_CFG"
cleanup() { rm -f -- "$TMP_RAW" "$TMP_GZ" "$TMP_CURL_ERR" "$TMP_CURL_CFG"; rm -rf -- "$TMP_PARTS"; }
trap cleanup EXIT INT TERM

TOKEN="$(cat "$HUB_ADMIN_TOKEN_FILE")"
[[ -n "$TOKEN" ]] || fail "token file is empty"
# Bearer header via --config so the token never appears in process argv.
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$TMP_CURL_CFG"
TOKEN=""

# One authenticated GET; prints the HTTP code to stdout and stores the body.
# Only the code is logged; bodies never are.
api_get() {
  local path="$1" out="$2"
  local code
  code="$(curl -sS -o "$out" -w '%{http_code}' -K "$TMP_CURL_CFG" \
    --max-time "$CURL_TIMEOUT" \
    -H 'User-Agent: agent-hub-host/1' -H 'Accept: application/json' \
    "${HUB_BASE_URL%/}${path}" 2>"$TMP_CURL_ERR" || true)"
  rm -f "$TMP_CURL_ERR"
  printf '%s' "$code"
}

# 1. Status: table inventory + row counts (cheap liveness proof).
PRE_STATUS="${TMP_PARTS}/status-pre.json"
CODE="$(api_get "/api/backup/status" "$PRE_STATUS")"
[[ "$CODE" == "200" ]] || fail "status fetch failed HTTP ${CODE}"

# 2. Schema: full replayable DDL (tables, indexes, triggers, views).
SCHEMA_JSON="${TMP_PARTS}/schema.json"
CODE="$(api_get "/api/backup/export?schema=1" "$SCHEMA_JSON")"
[[ "$CODE" == "200" ]] || fail "schema fetch failed HTTP ${CODE}"

# 3. Rows: per-table chunk loop (bounded memory per request). Each chunk is
# internally consistent; hot tables may drift across chunks (tolerated at
# assembly within a small bound; larger drift fails the run).
TABLES_LIST="${TMP_PARTS}/tables.txt"
python3 - "$PRE_STATUS" "$TABLES_LIST" <<'PY'
import json, sys
body = json.load(open(sys.argv[1], encoding="utf-8"))
tables = body.get("tables")
assert isinstance(tables, list) and tables, "status tables missing/empty"
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(sorted(tables)) + "\n")
PY
[[ -s "$TABLES_LIST" ]] || fail "empty table list"

CHUNKS_DIR="${TMP_PARTS}/chunks"
mkdir -p "$CHUNKS_DIR"
while IFS= read -r table; do
  [[ -n "$table" ]] || continue
  offset=0
  while :; do
    part="${CHUNKS_DIR}/${table}.${offset}.json"
    CODE="$(api_get "/api/backup/export?table=${table}&offset=${offset}&limit=${CHUNK_ROWS}" "$part")"
    if [[ "$CODE" != "200" ]]; then
      log "ERROR: chunk fetch failed table=${table} offset=${offset} HTTP ${CODE} (no body stored)"
      exit 1
    fi
    # Stop when the server returns fewer rows than requested (last page).
    # An exactly-full last page yields one more empty page; cap iterations.
    got="$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1], encoding='utf-8')).get('rows', [])))" "$part")"
    [[ "$got" =~ ^[0-9]+$ ]] || fail "unparseable chunk table=${table} offset=${offset}"
    (( offset += got ))
    (( got < CHUNK_ROWS )) && break
    (( offset > 1000000 )) && fail "chunk loop exceeded 1M rows table=${table} (endpoint changed?)"
  done
  log "table ok ${table} rows=${offset}"
done < "$TABLES_LIST"

# 4. Post-read status for drift tolerance, then assemble + validate.
POST_STATUS="${TMP_PARTS}/status-post.json"
CODE="$(api_get "/api/backup/status" "$POST_STATUS")"
[[ "$CODE" == "200" ]] || fail "post status fetch failed HTTP ${CODE}"

# Assemble the chunked parts into the canonical export document (same shape
# as the monolithic endpoint: exported_at/snapshot/schema/row_counts/
# value_markers/tables) and validate. Cross-chunk drift on hot tables is
# tolerated within max(10 rows, 2%); larger drift fails the run.
python3 - "$PRE_STATUS" "$POST_STATUS" "$SCHEMA_JSON" "$CHUNKS_DIR" "$TABLES_LIST" "$TMP_RAW" <<'PY' || fail "export assembly/validation failed"
import glob
import json
import os
import sys

_, pre_path, post_path, schema_path, chunks_dir, tables_path, out_path = sys.argv
pre = json.load(open(pre_path, encoding="utf-8"))
post = json.load(open(post_path, encoding="utf-8"))
schema_body = json.load(open(schema_path, encoding="utf-8"))
tables = [t for t in open(tables_path, encoding="utf-8").read().split() if t]

for label, body in (("pre", pre), ("post", post)):
    assert body.get("snapshot") == "hub-do-sqlite", f"{label} snapshot marker mismatch"
    assert isinstance(body.get("tables"), list) and body["tables"], f"{label} tables missing"
    assert isinstance(body.get("row_counts"), dict), f"{label} row_counts missing"
schema = schema_body.get("schema")
assert isinstance(schema, dict) and schema, "schema missing/empty"

assembled: dict = {}
markers = {"bigint": 0, "blob": 0, "unreadable": 0}
for table in tables:
    rows: list = []
    offset = 0
    while True:
        part = os.path.join(chunks_dir, f"{table}.{offset}.json")
        if not os.path.isfile(part):
            if offset == 0:
                raise AssertionError(f"missing first chunk table={table}")
            break
        body = json.load(open(part, encoding="utf-8"))
        assert body.get("snapshot") == "hub-do-sqlite", f"chunk snapshot mismatch {table}@{offset}"
        assert body.get("table") == table, f"chunk table mismatch {part}"
        assert body.get("offset") == offset, f"chunk offset mismatch {part}"
        chunk_rows = body.get("rows")
        assert isinstance(chunk_rows, list), f"chunk rows not a list {part}"
        chunk_markers = body.get("value_markers", {})
        for k in markers:
            markers[k] += int(chunk_markers.get(k, 0) or 0)
        # Table DDL rides the first chunk; cross-check it against the schema dump.
        if offset == 0:
            ddl = body.get("ddl", "")
            assert isinstance(ddl, str) and "CREATE TABLE" in ddl.upper(), f"chunk ddl missing {table}"
            if table in schema:
                assert schema[table].strip() == ddl.strip(), f"chunk ddl differs from schema dump {table}"
        rows.extend(chunk_rows)
        if len(chunk_rows) == 0:
            break
        offset += len(chunk_rows)
        if offset > 1000000:
            raise AssertionError(f"assembly exceeded 1M rows table={table}")
    assembled[table] = rows

post_counts = post.get("row_counts", {})
for table, rows in assembled.items():
    n = len(rows)
    post_n = post_counts.get(table)
    if post_n is None:
        raise AssertionError(f"table vanished from post status: {table}")
    drift = abs(n - int(post_n))
    bound = max(10, int(int(post_n) * 0.02))
    if drift > bound:
        raise AssertionError(f"row drift too large table={table} assembled={n} post={post_n} bound={bound}")

# Core Hub datasets must be present: intake tasks, sessions, claims, audit.
for required in ("intake_tasks", "session_control_sessions", "claims", "audit"):
    assert required in assembled, f"required table missing: {required}"
# Ephemeral rate-limit state is never exported (would leak caller keys).
assert "backup_rate" not in assembled, "backup_rate must be excluded from export"
assert "backup_rate" not in schema, "backup_rate must be excluded from schema"
for name in list(assembled) + list(schema):
    assert not name.startswith("sqlite_") and not name.startswith("_cf_"), f"internal table leaked: {name}"

body = {
    "exported_at": post.get("exported_at"),
    "snapshot": "hub-do-sqlite",
    "schema": schema,
    "row_counts": {t: len(r) for t, r in assembled.items()},
    "value_markers": markers,
    "tables": assembled,
}
assert body["exported_at"], "post exported_at missing"
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(body, fh)
print(f"validated {len(assembled)} tables, {sum(len(r) for r in assembled.values())} rows, "
      f"{len(schema)} schema objects, markers={markers}")
PY

gzip -c "$TMP_RAW" > "$TMP_GZ"
chmod 600 "$TMP_GZ"
SIZE="$(stat -c%s "$TMP_GZ" 2>/dev/null || stat -f%z "$TMP_GZ")"
# Backstop against truncation: validation above already proves the assembled
# document is complete (non-empty tables, required tables, counts match), so
# the floor stays low enough for small-but-valid exports; integrity is
# verified by test-decompressing.
[[ "$SIZE" -gt 200 ]] || fail "export gzip suspiciously small (${SIZE}B)"
gzip -t "$TMP_GZ" || fail "export gzip failed integrity check"

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
