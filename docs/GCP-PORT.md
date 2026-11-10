> **SUPERSEDED (2026-11-07).** hub2 no longer runs under wrangler/workerd; the
> budget cron and envelopes described below are retired. Current topology,
> cutover record and operations live in [docs/NODE-VM-DEPLOY.md](NODE-VM-DEPLOY.md).
> Kept for the port history and the GCP VM facts (machine type, firewall,
> secrets layout). Its `gcloud`/VM-rebuild steps are history, not an update
> path: updates ship only through CI/CD (NODE-VM-DEPLOY.md, "Shipping an update").

# GCP VM port (port-gcloud, 2026-11-07)

Agent Hub (hub2) now runs on a GCP Compute Engine VM in project
`dtcdev-click`, with the Durable Object SQLite databases stored on the
machine itself. The Cloudflare Worker deployment stays in place untouched
(rollback path) until the DNS cutover below. The pre-port source snapshot
is branch `pre-gcp-port-20261107`; port work continues on `main`.

## Provisioned infrastructure

| Piece | Value |
| --- | --- |
| VM | `agent-hub`, zone `europe-west3-b`, machine type `e2-small` (2 shared vCPU, 2 GB RAM + 2 GB swapfile) |
| External IP | `35.198.99.237` (ephemeral — reserve a static IP at cutover) |
| Boot disk | 25 GB pd-balanced, Ubuntu 24.04 LTS, Node 22, caddy 2.6, sqlite3 |
| Firewall | `agent-hub-web`: tcp/80+443 → tag `agent-hub` on network `default`; SSH via `default-allow-ssh` |
| Existing workload | `agent-browser-relay` VM untouched (separate network) |

Runtime model: the exact hub2 production Worker (`hub2/production-worker.ts`
via root `wrangler.jsonc`, release modes intact) runs under `wrangler dev`
(workerd) as `agent-hub-worker.service`, bound to `127.0.0.1:8787`, with
`--persist-to /var/lib/agent-hub/do-state` — **the DB is the DO SQLite
storage on the machine**. The 15-minute budget-alert cron fires through
`agent-hub-cron.timer` → `/__scheduled`. Caddy fronts `:80 → 127.0.0.1:8787`;
after the DNS cutover it terminates TLS with an automatic ACME certificate.
Rebuild the VM from scratch with `deploy/gcp-vm-setup.sh` (idempotent;
secrets are generated on the VM only).

`wrangler` was bumped 4.149.0 → 4.158.0 (exact) in `package.json` /
`package-lock.json`: the older pinned workerd supported compatibility dates
only through 2026-10-13, while the config (and Cloudflare production)
require 2026-11-01.

## Secrets (never in git)

`/opt/agent-hub/.dev.vars` (mode 600, on the VM only), read by `wrangler dev`:

- `HUB2_RELEASE_MODE=bootstrap` — flip to `active` at cutover (below).
- `HUB2_CREDENTIAL_KEY` — fresh 64-hex key, encrypts provider credentials.
- `HUB2_BOOTSTRAP_TOKEN` — bootstrap-mode pairing.
- `HUB2_BUDGET_ENVELOPES` + `HUB2_BUDGET_ENVELOPES_PART2` — one JSON
  document split at a UTF-8 boundary (each ≤5000 bytes), version:1,
  normal+safety plans, all 14 BudgetDO metric units. The installed values
  are **initial port sizing** (provenance says so); per
  `docs/hub2/COST-CONTROL.md` the operator must re-approve production
  sizing — the checked-in test envelope is explicitly never loaded.

AUTH_* vars (Cognito issuer `auth.dtcdev.click`, callback
`hub.dtcdev.click/auth/callback`, JWKS) stay as in `wrangler.jsonc`: the
callback origin is unchanged by this port because the cutover keeps the
same hostname, so the existing Cognito app client keeps working without
touching DTC AWS.

## Bootstrap → active

The VM database is fresh (hub2's clean-start model — see
`docs/hub2/FEATURES.md` §53; no hub1 data migration, backup artifacts
preserved offline). Current state: bootstrap mode, `/health` →
`{"status":"bootstrap","dispatch_enabled":false}`, DO storage initialized,
budget cron live.

1. Pair the operator (device flow `/api/device/code` → approve with
   `HUB2_BOOTSTRAP_TOKEN` → Cognito admin login) and enroll hosts
   (`/api/enrollments/pair` → `/api/heartbeat`).
2. Flip release mode: set `HUB2_RELEASE_MODE=active` in `.dev.vars`,
   `sudo systemctl restart agent-hub-worker.service`, verify `/health` and
   `/api/status`.
3. Post the accepted result on the tracking issue.

## Backup (works with the new deployment)

The hub1 REST export (`/api/backup/*`) does not exist in hub2, and the DB
is now a local file tree, so the daily job is a file snapshot with the same
guarantees (daily, off-DB copies, ≥30-day retention, fail-closed alerts,
restore drill):

- `scripts/hub2-db-backup.sh` — `VACUUM INTO` per DO database (WAL-safe
  against the live worker), packs instances + per-class `metadata.sqlite`
  with a counts manifest into `hub2-do-<STAMP>.tar.gz` (mode 600) under
  `<BACKUP_DIR>/YYYY/MM/DD/` and `<MIRROR_DIR>/...`; prunes only
  `>=RETENTION_DAYS+1` days; `flock` single-run; `RETENTION_DAYS < 30`
  fails closed; `/tmp` trees rejected. Any failure invokes
  `ALERT_CMD --backup-failed "<reason>"`; `HUB2_DB_BACKUP_FAIL_DRILL=1`
  fails without touching anything and never mails (one `[DRILL]` line).
- `scripts/hub2-db-restore-drill.py` — extracts an artifact, re-runs
  `PRAGMA integrity_check` per database, compares every table count with
  the manifest exactly. Exit 0 = restorable.
- `scripts/hub-db-backup-staleness.sh` — new `BACKUP_GLOB` env (default
  unchanged, `hub-do-*.json.gz`); the VM uses
  `BACKUP_GLOB=hub2-do-*.tar.gz` via `/etc/hub2-db-backup-staleness.env`.
- VM cron: `20 4 * * *` root (same schedule as the hub1 job).

Verified live on the VM (2026-11-07): snapshot 7 databases → two trees,
drill PASS, fail-drill `[DRILL]` line with `mail=none`, staleness `fresh`
exit 0, 23/23 staleness suite + 7/7 new regression tests
(`host/tests/test_hub2_db_backup_script.py`).

## Cutover checklist (needs Alexey — Route 53 / AWS access)

1. Reserve a static external IP for `agent-hub` (currently ephemeral).
2. DNS: `hub.dtcdev.click` is on **AWS Route 53** (awsdns NS). Point its A
   record at the VM (direct, or via the Cloudflare proxy). This is DTC AWS
   and is deliberately not changed by this port.
3. Caddy: ask the aws-infra owner to change the shared Caddyfile (Hub deploys
   own only `/etc/caddy/sites/hub.caddy`); replace the `:80` site with the
   hostname (e.g. `hub.dtcdev.click`) → automatic ACME + HTTP→HTTPS
   redirect; `systemctl reload caddy`.
4. Complete bootstrap→active (above), verify `/health`, `/api/status`,
   dashboard login via Cognito, one host heartbeat.
5. Backup redundancy: pull `hub2-do-*.tar.gz` from the VM into Hetzner
   `/var/backups/hub-do` + Storagebox (rsync over SSH), and run the
   staleness check there (`BACKUP_GLOB=hub2-do-*.tar.gz`) so alerts use the
   working msmtp path — the VM has no MTA and its second tree is
   same-disk.
6. Keep the Cloudflare Worker deployed but idle as rollback; the `hub1`
   tag and offline backup artifacts stay untouched. Post-cutover, the
   hub1-shaped nightly REST job on Hetzner should be retired in favor of
   step 5 (against hub2, `/api/backup/*` only yields the intentional
   stub/404 path).

Rollback: point the Route 53 A record back at the previous origin. Any
requests served by the VM during the cutover window diverge from the
Cloudflare DO state (fresh databases on both sides), so cut over before
real enrollment traffic starts.
