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
`20 4 * * *` Europe/Berlin) on the Hetzner operator host. Because a single
monolithic export can exceed DO memory on production data (and a single
giant JSON string cannot name the failing table), the script fetches
**chunked** — bounded rows per request, each chunk internally consistent:

1. Reads the operator token from a mode-600 file (`HUB_ADMIN_TOKEN_FILE`,
   default `/home/alexey/.config/agent-hub/admin-token`; the script refuses
   to run unless the file is exactly 600) and passes it to curl via a
   mode-600 `--config` file, so the token never appears in env, history,
   or process argv (`ps aux`).
2. `GET /api/backup/status` (table inventory + counts), then
   `GET /api/backup/export?schema=1` (full DDL, no rows).
3. Per table, until a short page (`CHUNK_ROWS`, default 500, endpoint caps
   at 5000):
   - rowid tables on API v5+: keyset
     `GET /api/backup/export?table=X&after=first|<rowid>&limit=M` (rowid
     order; the response carries `cursor:"rowid"`, `after`, `next_after`).
     Cursors are exact signed 64-bit decimal strings (`CAST` in SQL, never
     a JS number), and `after=first` has no lower bound, so no sentinel can
     hide a row at the int64 floor. Inserts
     land after the cursor and deletes never shift unread rows, so a hot
     table can neither skip nor duplicate rows across chunks.
   - `WITHOUT ROWID` tables (listed in status `without_rowid`) and API v4
     servers: `GET /api/backup/export?table=X&offset=N&limit=M` (ordered:
     `ORDER BY rowid`, or primary-key order for WITHOUT ROWID).
   Each table is then checked against **its own read**: assembled rows vs
   the `total` (COUNT(*)) of its final chunk, within `max(10, 2%)`. A miss
   re-reads that table, up to `TABLE_ATTEMPTS` (default 3); only persistent
   drift fails the run. Chunked reads share a generous per-caller budget
   (2000/hour); the unbounded full dump keeps the tight 30/hour budget.
4. Assembles the canonical export document (same shape as the monolithic
   endpoint) and validates: `snapshot == hub-do-sqlite`, every table
   present exactly once, per-table DDL matches the schema dump, core tables
   (`intake_tasks`, `session_control_sessions`, `claims`, `audit`) exist,
   `backup_rate`/`sqlite_%`/`_cf_%` absent, `value_markers` shape, keyset
   cursor continuity (`after` == previous `next_after`, strictly
   increasing), and each table's count within `max(10, 2%)` of its own
   final-chunk COUNT(*). The post-read status is a presence check only
   (see the 2026-10-29 incident below).
5. `gzip -c`, `chmod 600`, stores to both trees:
   - `BACKUP_DIR/YYYY/MM/DD/hub-do-<STAMP>.json.gz` (Hetzner local)
   - `STORAGEBOX_DIR/YYYY/MM/DD/hub-do-<STAMP>.json.gz` (Storagebox sshfs mirror)
6. Prunes ONLY files with age >= `RETENTION_DAYS`+1 whole days
   (`find -mtime +N`; `+30` keeps the full 30-day window plus today).
   `RETENTION_DAYS` default 30, enforced `>= 30` — the script fails closed
   otherwise. Temp files live under absolute `$TMPDIR` (default
   `/var/lib/hub-db-backup/tmp`, never `/tmp`), created 700.

7. Any non-zero exit pages on the same run (section 3, "Same-run failure
   alert").

### 2026-10-29 incident (intake e27e0f9d)

The 04:20 run failed with `row drift too large
table=session_control_commands assembled=1710 post=1748 bound=34`. The
table was read completely at 03:20:47 (1710 rows, matching its COUNT(*));
at 03:20:57 a burst of `capture` session-control commands (~8/s) started,
and the run-wide post status at ~03:21:02 counted 1748. The 38 rows were
appended after the table had been read, so nothing was lost, but the old
validation compared every table with that later post status, and 38 > the
2% bound (34). Hot tables could fail any night a burst landed in the
seconds between a table's read and the end of the run. Separately, the
unordered `LIMIT/OFFSET` paging could silently skip a row whenever rows were
deleted from an already-read range mid-table, and the drift bound would
have hidden that. Fix: keyset paging, per-table validation against the
table's own COUNT(*), bounded per-table retry, same-run failure alert.
Regression tests: `test/backup.test.ts` (keyset vs offset under churn) and
`host/tests/test_hub_db_backup_script.py` (replays the 10-29 pattern
against the real script with a fake Hub; also row loss, retry, v4 fallback,
alert and drill). The failed run's assembled count is reproduced exactly by
the old script in that test.

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

### Same-run failure alert (intake e27e0f9d)

`hub-db-backup.sh` traps EXIT: any non-zero exit (config, fetch, drift,
validation, store) runs `ALERT_CMD` (default: the sibling
`hub-db-backup-staleness.sh --backup-failed "<reason>"`), which mails
`[FAIL] Hub DB backup run failed on <host>` to `ALERT_TO` right away, with
the reason (table names and counts only) and the newest surviving export.
No 36h wait and no dedupe (the job runs once a day); the test-path guard
still applies, and it never touches the staleness state file. Disable only
for local experiments with `ALERT_ON_FAIL=0`.

Drill: `HUB_DB_BACKUP_FAIL_DRILL=1` makes the backup script fail at once
(no Hub request) and passes `--drill`. **Drills never send mail** (standing
rule: tests and drills never send real mail; a 2026-10-29 05:25 Berlin drill
of an earlier revision mailed dev@ and was corrected). The fully rendered
alert is appended to a local mode-600 sink (`HUB_DB_DRILL_SINK`, default
`/var/lib/hub-db-backup/drill-mail.log`) and no MTA is invoked. This proves
trap -> alert command -> message rendering; the transport is the same
`send_mail` used by the staleness alerts.

```bash
sudo -u alexey HUB_DB_BACKUP_FAIL_DRILL=1 /usr/local/sbin/hub-db-backup.sh; echo $?   # expect 1
tail -n 8 /var/lib/hub-db-backup/drill-mail.log   # "[DRILL] [FAIL] ...", "not sent"
```

Covered by `host/tests/test_hub_db_backup_script.py`
(`test_backup_failed_drill_never_sends_mail`,
`test_full_backup_drill_chain_never_sends_mail`: stub MTAs must capture
nothing).

### Mail hygiene (intake 9806fc14)

A 2026-10-27 manual verification ran this script against empty `/tmp` dirs
with the real env and no `--dry-run`, paging alexey@ with test paths
(`/tmp/stale-empty-*`). Since then:

- **Test-path guard**: when `BACKUP_DIR`, `STORAGEBOX_DIR` or `STATE_DIR` is
  under `/tmp` or `/var/tmp`, EVERY send (including RECOVERED) is routed to
  `ALERT_OPS_TO` (`dev@dtcdev.click`) only — never `ALERT_TO`. The run logs
  `GUARD: test paths ...` and marks `guard=test-paths` on the ALERT line.
- **Drill**: `HUB_DB_STALE_DRILL=1` forces one FAIL rendered to the local
  drill sink (`HUB_DB_DRILL_SINK`, default
  `/var/lib/hub-db-backup/drill-mail.log`, mode 600) and **never mailed**
  (standing rule since e27e0f9d: tests and drills never send real mail, not
  even to dev@). It records `DRILL=1` + `LAST_ALERT=0` in the state file,
  so a real staleness episode in between is NOT deduped away; the closing
  RECOVERED is sink-only too. The sink refuses symlinks/non-regular files
  and is forced to 600 before writing; any failure exits 1 without mailing.
- `--dry-run` / `--no-mail` / `--check-only` still send nothing.

Regression suite: `scripts/test-hub-db-backup-staleness.sh` (stub MTAs,
non-routable `*.invalid.test` recipients, fail-closed capture dir).

Manual tests (no real mail):

```bash
# fresh path (uses the real trees; staleness runs as alexey too)
sudo -u alexey /usr/local/sbin/hub-db-backup-staleness.sh --dry-run; echo $?
# stale path (throwaway dirs, no mail)
mkdir -p /tmp/stale-test-local /tmp/stale-test-box /tmp/stale-test-state
BACKUP_DIR=/tmp/stale-test-local STORAGEBOX_DIR=/tmp/stale-test-box \
  STATE_DIR=/tmp/stale-test-state scripts/hub-db-backup-staleness.sh --dry-run; echo $?
# expect exit 2 + "would-alert"
# drill: FAIL rendered to /var/lib/hub-db-backup/drill-mail.log, no mail;
# cleared on the next fresh run (RECOVERED also sink-only)
sudo -u alexey HUB_DB_STALE_DRILL=1 /usr/local/sbin/hub-db-backup-staleness.sh
```

Never mail real recipients from a test: real `ALERT_TO` recipients require
real backup dirs outside `/tmp` and no `--dry-run`/`--no-mail` flag.

## 4. Restore drill (throwaway, read-only)

Goal: prove the latest export loads locally and row counts match live
within expected drift. Nothing is written back to prod.

Scripted drill (intake e27e0f9d), throwaway SQLite under
`~/.local/hub-db-restore-drill/drill-<stamp>/`: replays tables, loads every
row (bigint/blob markers decoded), then indexes/triggers/views, runs
`PRAGMA integrity_check`, and compares each table with the export
(`row_counts`, must be equal) and live `/api/backup/status` (drift reported):

```bash
python3 scripts/hub-db-restore-drill.py            # newest export
python3 scripts/hub-db-restore-drill.py FILE.json.gz --no-live
# expect: integrity_check ok, ddl_errors=0, 145/145 equal, RESTORE_DRILL OK
```

2026-10-29 result (export `hub-do-20261029T0522+0100.json.gz`): 145/145
tables equal, 82228 rows, integrity ok; `intake_tasks` 764 and
`session_control_sessions` 4110 equal to live.

Manual equivalent:

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
