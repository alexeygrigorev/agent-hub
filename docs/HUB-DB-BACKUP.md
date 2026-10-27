# Hub DB backup (intake c1241bea)

Independent daily export of the Hub Durable Object SQLite (Cloudflare Worker
`agent-hub`, DO `Hub`) to OFF-Cloudflare storage (Hetzner local + Storagebox
mirror), with staleness alerting and a restore drill.

This is **not** a duplicate of intake 955e9381 (`955e9381-c06a-405f-a29a-74ce9cf2b498`,
no-hard-delete + recovery of ~273 hard-deleted rows). That intake owns
`src/intake.ts` deletion guards; this intake owns `src/backup.ts`,
`scripts/hub-db-backup*.sh`, and this doc. No shared files are edited by both
intakes; no deletions are performed by this task.

## 1. Export endpoint (read-only, operator-only, rate-limited)

Two routes, wired in `src/hub.ts` behind the existing `sessionControlAuth`
(operator-token fallback included). `BackupService` (`src/backup.ts`) refuses
every non-admin caller with 403 and only serves GET:

- `GET /api/backup/export` — consistent JSON dump of EVERY user table's rows
  plus the full replayable DDL schema (tables, indexes, triggers, views —
  including the append-only `intake_audit` guard and projection triggers)
  and per-table `row_counts`, inside one storage transaction.
  `Cache-Control: private, no-store`. Ephemeral `backup_rate` state and
  platform internals (`sqlite_%`, Cloudflare `_cf_%` such as `_cf_METADATA`,
  which is unreadable through the normal query path) are excluded — they
  would leak caller keys or break the consistent read. Oversize exports fail
  closed with 413 (`export_too_large`) instead of risking DO memory;
  unexpected per-table failures fail closed with 500
  `{error:export_failed, stage, table?}` (table name only, never row content).
  Values JSON cannot natively encode (BigInts, raw binary) become explicit
  lossless markers (`{"__hub_backup_bigint":"123"}`,
  `{"__hub_backup_blob_b64":"..."}`, counted in top-level `value_markers`).
  Anything else unserializable (lazy-throwing accessors, unknown host
  objects, over-deep/circular shapes — none expected from SQLite rows)
  becomes an explicit `__hub_backup_unreadable`/`__hub_backup_circular`
  marker, also counted, instead of failing the export: the backup never
  500s on production value shapes, and restore reverses the lossless ones
  (`BigInt(s)`, `Buffer.from(b64,'base64')`) while reporting the rest.
- `GET /api/backup/status` — table names + row counts only (cheap liveness
  proof for the restore drill without shipping the dataset).

Rate limits are per-caller sliding-window (caller actor, IP fallback),
tracked in the `backup_rate` table (the only write the service performs):
30 exports/hour, 120 statuses/hour; excess returns 429. Non-GET returns
405. Anonymous/wrong-token returns 401; mixed `Authorization`+`Cookie`,
non-admin agent/host/job credentials return 403.

Covered by `test/backup.test.ts`: full export shape (tables + triggers in
schema, `backup_rate` excluded, security headers, no plaintext token),
status shape, auth refusals (anonymous/wrong/mixed/non-admin), read-only
(business tables byte-identical across a GET; only `backup_rate` moves),
rate limiting (export 429 then status still 200).

## 2. Off-Cloudflare daily job

`scripts/hub-db-backup.sh` (cron `scripts/hub-db-backup.cron`,
`20 4 * * *` Europe/Berlin) on the Hetzner operator host:

1. Reads the operator token from a mode-600 file (`HUB_ADMIN_TOKEN_FILE`,
   default `/home/alexey/.config/agent-hub/admin-token`; the script refuses
   to run unless the file is exactly 600) and passes it to curl via a
   mode-600 `--config` file, so the token never appears in env, history,
   or process argv (`ps aux`).
2. `GET /api/backup/export` with `Authorization: Bearer`, UA `agent-hub-host/1`
   (https only; cleartext hosts refused without an explicit local-drill opt-out).
3. Validates JSON: `snapshot == hub-do-sqlite`, `tables`/`row_counts` key sets
   match, `schema` covers every table, each count matches its row array
   length, core tables (`intake_tasks`, `session_control_sessions`,
   `claims`, `audit`) exist, and `backup_rate` is absent.
4. `gzip -c`, `chmod 600`, stores to both trees:
   - `BACKUP_DIR/YYYY/MM/DD/hub-do-<STAMP>.json.gz` (Hetzner local)
   - `STORAGEBOX_DIR/YYYY/MM/DD/hub-do-<STAMP>.json.gz` (Storagebox sshfs mirror)
5. Prunes ONLY files with age >= `RETENTION_DAYS`+1 whole days
   (`find -mtime +N`; `+30` keeps the full 30-day window plus today).
   `RETENTION_DAYS` default 30, enforced `>= 30` — the script fails closed
   otherwise. Temp files live under absolute `$TMPDIR` (default
   `/var/lib/hub-db-backup/tmp`, never `/tmp`), created 700.

S3 (`dtc-coolify-backups` under a new prefix) is intentionally NOT used:
the existing `coolify-s3-backup-writer` credential is scoped to `coolify/*`
only, and this job has no approval to use `main-temp` or any static AWS key.
Hetzner + Storagebox satisfies AC1 ("Hetzner + Storagebox, and/or S3").

Install (operator box, never overwrites env; the job runs as user `alexey`,
not root, because root cannot write the Storagebox sshfs mount):

```bash
sudo scripts/install-hub-db-backup.sh
# fill /etc/hub-db-backup.env (0600 alexey), then as alexey:
sudo -u alexey /usr/local/sbin/hub-db-backup.sh
```

Env placeholders: `scripts/hub-db-backup.env.example` (no secrets in git).

## 3. Staleness alert

`scripts/hub-db-backup-staleness.sh` (cron every 15 min,
`scripts/hub-db-backup-staleness.cron`). Freshest `hub-do-*.json.gz` across
both trees decides; missing dir or zero exports counts as stale.

- Fresh (age <= 36h): exit 0, log `fresh:`; sends one `RECOVERED` mail if the
  previous run was stale.
- Stale (age > 36h): exit 2, mails
  `To: alexey@datatalks.club,dev@dtcdev.click` via sendmail-first/msmtp-fallback
  (same order as `machine-management/monitors/coolify-healthcheck.sh`),
  `From: noreply@datatalks.club`. On the Hetzner box the mail goes out via
  the msmtp `ses` account first (`MSMTP_ACCOUNT`, default account as
  fallback): the default account's sandbox identity is SES-denied for
  `noreply@datatalks.club` (554), while `ses` delivers it (250 Ok in the
  msmtp log). Repeat pages deduped to one per 24h.

Manual tests (no real mail):

```bash
# fresh path (uses the real trees; staleness runs as alexey too)
sudo -u alexey /usr/local/sbin/hub-db-backup-staleness.sh --dry-run; echo $?
# stale path (throwaway dirs, no mail)
mkdir -p /tmp/stale-test-local /tmp/stale-test-box /tmp/stale-test-state
BACKUP_DIR=/tmp/stale-test-local STORAGEBOX_DIR=/tmp/stale-test-box \
  STATE_DIR=/tmp/stale-test-state scripts/hub-db-backup-staleness.sh --dry-run; echo $?
# expect exit 2 + "would-alert"
```

## 4. Restore drill (throwaway, read-only)

Goal: prove the latest export loads locally and row counts match live
within expected drift. Nothing is written back to prod.

```bash
LATEST=$(ls -t /var/backups/hub-do/*/*/*/hub-do-*.json.gz \
  ~/storagebox/backups/hub-do/*/*/*/hub-do-*.json.gz 2>/dev/null | head -n 1)
echo "$LATEST"
# Work in a private 0700 dir as a non-root user; never world-readable /tmp.
RESTORE_DIR=~/.local/hub-db-restore-drill
mkdir -p "$RESTORE_DIR" && chmod 700 "$RESTORE_DIR"
gunzip -c "$LATEST" > "$RESTORE_DIR/hub-do-latest.json"
chmod 600 "$RESTORE_DIR/hub-do-latest.json"
python3 - <<'PY'
import json, os
body = json.load(open(os.path.expanduser('~/.local/hub-db-restore-drill/hub-do-latest.json')))
print('exported_at', body['exported_at'])
for name in sorted(body['row_counts']):
    print(f"{name:40s} {body['row_counts'][name]}")
print('schema objects:', len(body['schema']))
PY
# Per-table SQLite load (throwaway): tables first, then indexes/triggers/views.
python3 - <<'PY'
import json, os, sqlite3
d = os.path.expanduser('~/.local/hub-db-restore-drill')
body = json.load(open(d + '/hub-do-latest.json'))
con = sqlite3.connect(d + '/hub-do-restore-drill.sqlite')
cur = con.cursor()
def kind(ddl):
    u = ddl.upper()
    if 'CREATE TABLE' in u: return 0
    if 'CREATE INDEX' in u or 'CREATE UNIQUE INDEX' in u: return 1
    return 2
for name, ddl in sorted(body['schema'].items(), key=lambda kv: kind(kv[1])):
    try: cur.execute(ddl)
    except Exception as e: print('ddl skip', name, e)
con.commit()
print('schema objects replayed')
PY
# Compare against live status (operator token required):
curl -sS -H "Authorization: Bearer $(cat ~/.config/agent-hub/admin-token)" \
  -H 'User-Agent: agent-hub-host/1' \
  https://hub.dtcdev.click/api/backup/status | python3 -m json.tool | head -n 60
```

Expected: every table in the export exists in live `/api/backup/status`
with equal row counts ± drift from writes between export and check
(intakes/sessions grow constantly; a few rows of drift is normal, a missing
table or a zero-vs-nonzero flip is not). Record the export filename,
`exported_at`, per-table counts, and the live status output in the intake
DONE note and in `~/tmp/agent-reports/hub-db-backup-2026-10-27.md`.

## 5. Live proof checklist (for the DONE note)

- [ ] `GET /api/backup/status` 200 with operator token on prod
- [ ] Real export file exists in BOTH `/var/backups/hub-do/...` and
      `~/storagebox/backups/hub-do/...`, mode 600, `*.json.gz`
- [ ] Staleness script: fresh run exit 0, stale-simulation exit 2 would-alert
- [ ] Restore drill row counts recorded
- [ ] `scripts/test-full-local.sh` green
- [ ] Different-model review recorded
- [ ] `HEAD:main` pushed, prod deploy run linked, post-deploy health check run
- [ ] Report at `~/tmp/agent-reports/hub-db-backup-2026-10-27.md`
