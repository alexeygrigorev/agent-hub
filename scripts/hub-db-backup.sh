#!/usr/bin/env bash
# Hub DO SQLite off-Cloudflare daily export (intake c1241bea).
#
# Fetches the operator-only read-only snapshot CHUNKED (bounded rows per
# request, so giant tables/exports never exceed DO memory):
#   GET /api/backup/status                 (table names + row counts)
#   GET /api/backup/export?schema=1        (full DDL, no rows)
#   GET /api/backup/export?table=X&after=R&limit=M   (keyset row slices, API v5+)
#   GET /api/backup/export?table=X&offset=N&limit=M  (offset row slices)
# (429/413/500 fail-closed) and writes it OFF Cloudflare (Hetzner local +
# Storagebox mirror):
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
# Chunk consistency (intake e27e0f9d): each chunk is internally consistent
# (single server-side transaction). Rowid tables are paged with a keyset
# cursor (?after=<rowid>, API v5+), so concurrent inserts/deletes can neither
# skip nor duplicate rows; WITHOUT ROWID tables (and API v4 servers) use
# ordered offset paging. Every table is validated against ITS OWN read: the
# assembled count must match the COUNT(*) the server reported in the table's
# final chunk within max(10, 2%). A table that misses is re-read up to
# TABLE_ATTEMPTS times; only persistent drift fails the run. The run-wide
# post status is only a presence check: comparing an early-read table with a
# count taken later is what failed 2026-10-29 (38 rows legitimately appended
# to session_control_commands after it had been read).
#
# Failure alerting: any non-zero exit (config, fetch, validation, store)
# pages on the same run via ALERT_CMD (default: the sibling
# hub-db-backup-staleness.sh --backup-failed: same mail path and test-path
# guard). HUB_DB_BACKUP_FAIL_DRILL=1 forces an immediate failure whose alert
# goes to ALERT_OPS_TO only; no Hub request is made.
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
FAIL_REASON=""
fail() { FAIL_REASON="$*"; log "ERROR: $*"; exit 1; }

# Same-run failure alert (intake e27e0f9d). Registered before any check so
# config failures page too. The reason carries table names and counts only,
# never row content or secrets.
HUB_DB_BACKUP_FAIL_DRILL="${HUB_DB_BACKUP_FAIL_DRILL:-0}"
ALERT_ON_FAIL="${ALERT_ON_FAIL:-1}"
ALERT_CMD="${ALERT_CMD:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/hub-db-backup-staleness.sh}"
cleanup() { :; }
on_exit() {
  local rc=$?
  trap - EXIT
  cleanup
  if (( rc != 0 )) && [[ "$ALERT_ON_FAIL" == "1" ]]; then
    local reason="${FAIL_REASON:-exit ${rc} (see /var/log/hub-db-backup.log)}"
    local args=(--backup-failed "$reason")
    [[ "$HUB_DB_BACKUP_FAIL_DRILL" == "1" ]] && args+=(--drill)
    if [[ -x "$ALERT_CMD" ]]; then
      "$ALERT_CMD" "${args[@]}" || log "WARN: failure alert command exited non-zero: ${ALERT_CMD}"
    else
      log "WARN: failure alert command missing/not executable: ${ALERT_CMD}"
    fi
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$HUB_DB_BACKUP_FAIL_DRILL" == "1" ]]; then
  fail "DRILL: forced backup failure (HUB_DB_BACKUP_FAIL_DRILL=1); no Hub request made"
fi

HUB_BASE_URL="${HUB_BASE_URL:-https://hub.dtcdev.click}"
HUB_ADMIN_TOKEN_FILE="${HUB_ADMIN_TOKEN_FILE:-/home/alexey/.config/agent-hub/admin-token}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/hub-do}"
STORAGEBOX_DIR="${STORAGEBOX_DIR:-/home/alexey/storagebox/backups/hub-do}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
LOCK_FILE="${LOCK_FILE:-/var/lib/hub-db-backup/hub-db-backup.lock}"
CURL_TIMEOUT="${CURL_TIMEOUT:-60}"
CHUNK_ROWS="${CHUNK_ROWS:-500}"
TABLE_ATTEMPTS="${TABLE_ATTEMPTS:-3}"

[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || fail "RETENTION_DAYS must be an integer (got ${RETENTION_DAYS})"
(( RETENTION_DAYS >= 30 )) || fail "RETENTION_DAYS=${RETENTION_DAYS} violates AC: retention >= 30 days"
[[ "$CHUNK_ROWS" =~ ^[0-9]+$ ]] && (( CHUNK_ROWS >= 1 && CHUNK_ROWS <= 5000 )) || fail "CHUNK_ROWS must be an integer 1..5000 (got ${CHUNK_ROWS})"
[[ "$TABLE_ATTEMPTS" =~ ^[0-9]+$ ]] && (( TABLE_ATTEMPTS >= 1 && TABLE_ATTEMPTS <= 10 )) || fail "TABLE_ATTEMPTS must be an integer 1..10 (got ${TABLE_ATTEMPTS})"
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
TMP_PY_ERR="$(mktemp -p "$TMP_ROOT" hub-do-py-err-XXXXXX.txt)"
TMP_PARTS="$(mktemp -d -p "$TMP_ROOT" hub-do-parts-XXXXXX)"
chmod 700 "$TMP_PARTS"
chmod 600 "$TMP_CURL_CFG"
cleanup() { rm -f -- "$TMP_RAW" "$TMP_GZ" "$TMP_CURL_ERR" "$TMP_CURL_CFG" "$TMP_PY_ERR"; rm -rf -- "$TMP_PARTS"; }

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

# 3. Rows: per-table chunk loop (bounded memory per request). Plan lines are
# "<table> <rowid|offset>": keyset for rowid tables on API v5+, ordered
# offset otherwise. Chunk files are numbered by sequence: <table>.<seq>.json.
TABLES_LIST="${TMP_PARTS}/tables.txt"
python3 - "$PRE_STATUS" "$TABLES_LIST" <<'PY' || fail "status table list unreadable"
import json, sys
body = json.load(open(sys.argv[1], encoding="utf-8"))
tables = body.get("tables")
assert isinstance(tables, list) and tables, "status tables missing/empty"
keyset = int(body.get("api_version") or 0) >= 5 and isinstance(body.get("without_rowid"), list)
no_rowid = set(body.get("without_rowid") or [])
lines = [f"{t} {'rowid' if keyset and t not in no_rowid else 'offset'}" for t in sorted(tables)]
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
[[ -s "$TABLES_LIST" ]] || fail "empty table list"

# Lowest JS safe integer: below any rowid the Hub hands out.
KEYSET_START="-9007199254740991"
CHUNKS_DIR="${TMP_PARTS}/chunks"
mkdir -p "$CHUNKS_DIR"

# Fetch one table into <table>.<seq>.json chunks; prints the row count.
# Runs in a command substitution, so failures exit that subshell only and
# the caller re-raises with FAIL_REASON from the reason file.
FETCH_REASON="${TMP_PARTS}/fetch-reason.txt"
fetch_table() {
  local table="$1" mode="$2" seq=0 rows=0 pos got next part query
  fetch_fail() { printf '%s' "$*" > "$FETCH_REASON"; log "ERROR: $*" >&2; exit 1; }
  if [[ "$mode" == "rowid" ]]; then pos="$KEYSET_START"; else pos=0; fi
  while :; do
    part="${CHUNKS_DIR}/${table}.${seq}.json"
    if [[ "$mode" == "rowid" ]]; then query="after=${pos}"; else query="offset=${pos}"; fi
    CODE="$(api_get "/api/backup/export?table=${table}&${query}&limit=${CHUNK_ROWS}" "$part")"
    [[ "$CODE" == "200" ]] || fetch_fail "chunk fetch failed table=${table} ${query} HTTP ${CODE} (no body stored)"
    # "<rows> <next_after|->" from the chunk.
    read -r got next < <(python3 -c "
import json, sys
b = json.load(open(sys.argv[1], encoding='utf-8'))
n = b.get('next_after')
print(len(b.get('rows', [])), '-' if n is None else int(n))" "$part" 2>/dev/null) || true
    [[ "${got:-}" =~ ^[0-9]+$ ]] || fetch_fail "unparseable chunk table=${table} ${query}"
    # NOTE: plain assignments, not ((..)) arithmetic (which returns non-zero
    # for a zero result and would trip set -e on empty tables).
    rows=$((rows + got))
    seq=$((seq + 1))
    # Stop when the server returns fewer rows than requested (last page).
    # An exactly-full last page yields one more empty page; cap iterations.
    if (( got < CHUNK_ROWS )); then break; fi
    if [[ "$mode" == "rowid" ]]; then
      [[ "$next" =~ ^-?[0-9]+$ ]] || fetch_fail "keyset chunk without next_after table=${table}"
      (( next > pos )) || fetch_fail "keyset cursor did not advance table=${table} after=${pos} next=${next}"
      pos="$next"
    else
      pos=$((pos + got))
    fi
    if (( rows > 1000000 )); then fetch_fail "chunk loop exceeded 1M rows table=${table} (endpoint changed?)"; fi
  done
  printf '%s\n' "$rows"
}

# Self-consistency of one fetched table: assembled rows vs the COUNT(*) in
# its final chunk (same server transaction as the last read). Prints
# "ok|drift <assembled> <final_total> <bound>".
check_table() {
  python3 - "$CHUNKS_DIR" "$1" <<'PY'
import json, os, sys
d, table = sys.argv[1], sys.argv[2]
seq, n, total = 0, 0, None
while os.path.isfile(os.path.join(d, f"{table}.{seq}.json")):
    b = json.load(open(os.path.join(d, f"{table}.{seq}.json"), encoding="utf-8"))
    n += len(b.get("rows", []))
    total = int(b.get("total"))
    seq += 1
assert total is not None, f"no chunks table={table}"
bound = max(10, int(total * 0.02))
print("ok" if abs(n - total) <= bound else "drift", n, total, bound)
PY
}

while read -r table mode; do
  [[ -n "$table" ]] || continue
  attempt=1
  while :; do
    rm -f -- "${CHUNKS_DIR}/${table}".*.json
    rows="$(fetch_table "$table" "$mode")" || fail "$(cat "$FETCH_REASON" 2>/dev/null || echo "fetch failed table=${table}")"
    verdict="" n="" total="" bound=""
    read -r verdict n total bound < <(check_table "$table") || true
    [[ -n "$verdict" ]] || fail "table check failed table=${table}"
    if [[ "$verdict" == "ok" ]]; then break; fi
    if (( attempt >= TABLE_ATTEMPTS )); then
      fail "row drift too large table=${table} assembled=${n} final_total=${total} bound=${bound} after ${attempt} attempts (cursor=${mode})"
    fi
    log "WARN: drift table=${table} assembled=${n} final_total=${total} bound=${bound} attempt=${attempt}/${TABLE_ATTEMPTS}; re-reading"
    attempt=$((attempt + 1))
  done
  log "table ok ${table} rows=${rows} cursor=${mode} attempts=${attempt}"
done < "$TABLES_LIST"

# 4. Post-read status (presence check only), then assemble + validate.
POST_STATUS="${TMP_PARTS}/status-post.json"
CODE="$(api_get "/api/backup/status" "$POST_STATUS")"
[[ "$CODE" == "200" ]] || fail "post status fetch failed HTTP ${CODE}"

# Assemble the chunked parts into the canonical export document (same shape
# as the monolithic endpoint: exported_at/snapshot/schema/row_counts/
# value_markers/tables) and validate: cursor continuity per chunk, and each
# table's count against its own final-chunk COUNT(*) within max(10, 2%).
if ! python3 - "$PRE_STATUS" "$POST_STATUS" "$SCHEMA_JSON" "$CHUNKS_DIR" "$TABLES_LIST" "$TMP_RAW" 2>"$TMP_PY_ERR" <<'PY'
import json
import os
import sys

_, pre_path, post_path, schema_path, chunks_dir, tables_path, out_path = sys.argv
pre = json.load(open(pre_path, encoding="utf-8"))
post = json.load(open(post_path, encoding="utf-8"))
schema_body = json.load(open(schema_path, encoding="utf-8"))
plan = [line.split() for line in open(tables_path, encoding="utf-8").read().splitlines() if line.strip()]
tables = [t for t, _ in plan]
modes = dict(plan)

for label, body in (("pre", pre), ("post", post)):
    assert body.get("snapshot") == "hub-do-sqlite", f"{label} snapshot marker mismatch"
    assert isinstance(body.get("tables"), list) and body["tables"], f"{label} tables missing"
    assert isinstance(body.get("row_counts"), dict), f"{label} row_counts missing"
schema = schema_body.get("schema")
assert isinstance(schema, dict) and schema, "schema missing/empty"

KEYSET_START = -9007199254740991
assembled: dict = {}
final_totals: dict = {}
markers = {"bigint": 0, "blob": 0, "unreadable": 0}
for table in tables:
    rows: list = []
    offset = 0
    after = KEYSET_START
    seq = 0
    while True:
        part = os.path.join(chunks_dir, f"{table}.{seq}.json")
        if not os.path.isfile(part):
            if seq == 0:
                raise AssertionError(f"missing first chunk table={table}")
            break
        body = json.load(open(part, encoding="utf-8"))
        assert body.get("snapshot") == "hub-do-sqlite", f"chunk snapshot mismatch {table}#{seq}"
        assert body.get("table") == table, f"chunk table mismatch {part}"
        if modes[table] == "rowid":
            assert body.get("cursor") == "rowid", f"chunk cursor mismatch {part}"
            assert body.get("after") == after, f"chunk keyset gap {part}"
        else:
            assert body.get("offset") == offset, f"chunk offset mismatch {part}"
        final_totals[table] = int(body.get("total"))
        chunk_rows = body.get("rows")
        assert isinstance(chunk_rows, list), f"chunk rows not a list {part}"
        chunk_markers = body.get("value_markers", {})
        for k in markers:
            markers[k] += int(chunk_markers.get(k, 0) or 0)
        # Table DDL rides the first chunk; cross-check it against the schema dump.
        if seq == 0:
            ddl = body.get("ddl", "")
            assert isinstance(ddl, str) and "CREATE TABLE" in ddl.upper(), f"chunk ddl missing {table}"
            if table in schema:
                assert schema[table].strip() == ddl.strip(), f"chunk ddl differs from schema dump {table}"
        rows.extend(chunk_rows)
        seq += 1
        if len(chunk_rows) == 0:
            break
        offset += len(chunk_rows)
        if modes[table] == "rowid":
            nxt = body.get("next_after")
            assert isinstance(nxt, int) and nxt > after, f"keyset cursor did not advance {part}"
            after = nxt
        if offset > 1000000:
            raise AssertionError(f"assembly exceeded 1M rows table={table}")
    assembled[table] = rows

post_counts = post.get("row_counts", {})
for table, rows in assembled.items():
    n = len(rows)
    if post_counts.get(table) is None:
        raise AssertionError(f"table vanished from post status: {table}")
    # Validate against the table's own read, never the later post status:
    # rows appended after a table was read are not missing from it.
    total = final_totals[table]
    bound = max(10, int(total * 0.02))
    if abs(n - total) > bound:
        raise AssertionError(f"row drift too large table={table} assembled={n} final_total={total} bound={bound}")

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
keyset_tables = sum(1 for m in modes.values() if m == "rowid")
print(f"validated {len(assembled)} tables ({keyset_tables} keyset), {sum(len(r) for r in assembled.values())} rows, "
      f"{len(schema)} schema objects, markers={markers}")
PY
then
  cat "$TMP_PY_ERR" >&2
  # Assertion text names tables and counts only, never row content.
  fail "export assembly/validation failed: $(grep -E 'Error' "$TMP_PY_ERR" | tail -n 1 | cut -c1-300)"
fi

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
mkdir -p "$(dirname "$LOCAL_DEST")" "$(dirname "$STORAGE_DEST")" || fail "cannot create day dirs"
cp -- "$TMP_GZ" "$LOCAL_DEST" || fail "local copy failed: ${LOCAL_DEST}"
chmod 600 "$LOCAL_DEST"
cp -- "$TMP_GZ" "$STORAGE_DEST" || fail "storagebox copy failed: ${STORAGE_DEST}"
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
