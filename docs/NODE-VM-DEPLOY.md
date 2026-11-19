# Node VM deployment (agent-hub port, 2026-11-07)

Agent Hub (hub2) runs as a **plain Node.js server** on the GCP VM in project
`dtcdev-click`, with the databases as **SQLite files on the machine**. There is
no Cloudflare anywhere in the production path: no Workers runtime, no wrangler,
no BudgetDO cost checker (retired with the Cloudflare budget checkers), and the
public URL is served from AWS (`aws-infra` IaC), not from Cloudflare.

Previous runbooks: `docs/GCP-PORT.md` (wrangler-era VM port, superseded) and
the Cloudflare deploy docs (superseded). The pre-Node snapshot is the repo
state at the `pre-gcp-port-20261107` tag lineage.

## Production topology

```
browser/host ──HTTPS──> CloudFront (EYWL8OW5EM1UL, ACM hub.dtcdev.click)
                            │  origin: agent-hub-vm.dtcdev.click:80, http-only
                            ▼
                        caddy (VM :80)
                            ▼  reverse proxy
                        agent-hub-hub2.service
                        node --experimental-transform-types hub2/node/main.ts
                            │
                            ▼
                        /var/lib/agent-hub/hub2-db/*.sqlite   (one file per DO)
```

| Piece | Where | IaC |
| --- | --- | --- |
| Route53 `agent-hub-vm` A + `hub` alias records | aws-infra `sandbox/agent-hub` (terraform, us-east-1 state) | terraform |
| CloudFront distribution + ACM cert, origin = VM | aws-infra `sandbox/hub-edge` (CloudFormation, us-east-1) | change-set only |
| VM service, secrets, caddy, backups | GCP VM `agent-hub`, `/opt/agent-hub` checkout | `deploy/vm/install-hub2-node.sh` |
| Deploy from CI | GitHub `deploy.yml` `production` environment | SSH key `AGENT_HUB_VM_DEPLOY_KEY` |

Caddy ownership is split. The shared `/etc/caddy/Caddyfile` is owned by the
aws-infra repo (the infra owner), not by Hub deploys, and must contain
`import /etc/caddy/sites/*.caddy`. That import is a prerequisite: the Hub
installer and the deploy transaction fail closed if it is missing, and the fix
is an aws-infra change, never a manual edit by a Hub deploy. A Hub deploy
installs only `/etc/caddy/sites/hub.caddy` (from `deploy/hub.caddy`: the `:80`
site proxying the CloudFront Hub2 origin to port 8787), validates it and the
shared config, then reloads Caddy. It never writes the shared Caddyfile or any
other site file. Rollback restores only `hub.caddy` from the transaction
snapshot. Hub-only releases (`hub_only=true`) skip Caddy entirely.

CI/CD needs **no GCP and no Cloudflare credentials**: deploys run `git fetch +
npm ci + install + restart` over SSH, then verify
`https://hub.dtcdev.click/health`. The VM is reachable with any key in the
`deploy` user's `authorized_keys`; the GitHub key is scoped to that.

## The Node runtime (hub2/node)

`hub2/node/` hosts the exact hub2 worker source on plain Node (24.x):

- `compat.ts` — global shims: `Response` initialized with `status: 101,
  webSocket` (undici cannot build 101s), request-instance forwarding, and the
  `WebSocketPair` global. `installCompatGlobals()` must run before any hub2
  module import.
- `runtime.ts` — the Durable Object host: one lazily created instance per
  (binding, name), namespaces (`stub.fetch`), a 1 s alarm sweep, and the
  `ctx.getWebSockets()/acceptWebSocket()` registry.
- `sqlite-storage.ts` — DO storage on `node:sqlite`: the `SqlStorage` cursor
  surface consumed by `MeteredDb`, KV `get/put/delete/list`, alarms, and
  `transactionSync`. One SQLite file per instance:
  `<Class>-<name>-<hash>.sqlite` under `dataDir`.
- `server.ts` — node:http front; WebSocket upgrades are bridged into the DO
  surface (`ws` handshake deferred until the DO approves, so failed upgrades
  return real HTTP errors). SQL receipt headers (`x-hub2-sql-*`) are stripped
  from egress here.
- `config.ts` — wrangler.jsonc `vars` defaults + `.dev.vars` override
  (HUB2_RELEASE_MODE, HUB2_CREDENTIAL_KEY, HUB2_BOOTSTRAP_TOKEN, AUTH_*).
- `main.ts` — entry: parse flags, load env, mount `production-worker.ts`
  (release modes and bootstrap allowlist intact) with the five DO classes.

Run locally: `node --experimental-transform-types hub2/node/main.ts --data-dir
.local/tmp/db --dev-vars hub2/.dev.vars` (TLA). Production requires the
provisioned Node 24 runtime at `/usr/bin/node`; Node 22 and older are refused
by the deploy guards (see below).

## Node runtime provisioning (Node24, CI-owned)

The VM service unit selects the absolute `/usr/bin/node`. Production requires
major 24 there, and production CI owns that upgrade. There is one deploy-lock
owner for the whole mutation window: the deploy job stages the exact candidate
`deploy/vm/provision-node24.sh` bytes to a SHA-bound path under
`/opt/agent-hub/.local/tmp` over the normal deploy-key SSH (never a remote
curl setup script), then invokes the transaction once with the trailing
`provision-node24` token and the Node24-qualified previous release SHA. The
transaction derives the staged path from the validated deploy SHA alone, binds
the VM's under-lock `HEAD` to the qualified previous release SHA, runs all
read-only candidate/shared Caddy and schema rollback preflights under the held
`.deploy-transaction.lock`, and only then verifies the staged bytes against the
immutable candidate Git object `${deploy_sha}:deploy/vm/provision-node24.sh`
(plus regular-file, non-symlink, invoking-user ownership, non-group/other-writable,
size-bounded, and syntax checks, written completely, fsynced, and read back
for exact length and digest verification in a private `0600` copy) and runs
the provisioner INSIDE the same `.deploy-transaction.lock`, before any checkout,
npm dependency, transaction-state, configuration or systemd-unit mutation. A competing transaction therefore cannot acquire the lock
between package verification and the application stage: custody is continuous,
never nested, and never trusted from an environment flag or mere contention —
the provisioner proves inheritance of the parent's actual open-file-description
(`AGENT_HUB_NODE24_LOCK_FD`) bound to the fixed `.deploy-transaction.lock`
inode while an independent acquisition fails with `EAGAIN`/`EWOULDBLOCK`.
The provisioner:

- supports exactly Ubuntu 24.04 (`noble`) and Debian 12 (`bookworm`) on
  `amd64`/`arm64`, read from `/etc/os-release` and `dpkg`, and refuses any
  other OS or architecture before it has any effect;
- binds one reviewed selection: the signed NodeSource repository
  `https://deb.nodesource.com/node_24.x` (`nodistro`/`main`), the approved
  signing key (`https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key`,
  pinned primary fingerprint) and the exact `nodejs` package version with its
  per-architecture SHA-256;
- is idempotent: when `/usr/bin/node` already reports major 24 and the exact
  pinned, package-owned identity, it verifies and no-ops; otherwise it
  validates the downloaded key and installs/keeps the keyring and apt source
  at their dedicated owned paths. An existing keyring or apt source that is
  foreign, mutated, a symlink or otherwise non-regular is preserved byte for
  byte and refused — never overwritten (operator reconciliation required);
- refreshes the signed package index, binds the candidate version to the
  approved origin, downloads the artifact, re-checks its SHA-256 at
  consumption and installs exactly that verified local file in one bounded
  `apt-get install` — the selected package is never re-resolved or
  re-downloaded from another byte source, while dependency resolution stays
  with the signed nodistro index — then re-verifies `/usr/bin/node`;
- never runs a general OS upgrade, never pipes a remote script into a shell,
  never downgrades an installed package, and refuses on any mismatch
  (fingerprint, origin, version, hash, ownership) before installing.

Both the direct installer (`install-hub2-node.sh`) and the transaction
(`deploy-release-transaction.sh`, after the in-lock provisioning phase and
before any checkout/npm/state mutation) fail closed when the selected service
runtime or the npm build toolchain is not Node 24. Because the provisioning
phase precedes the application stages inside the same locked transaction, the
guard pair upgrades the VM from Node 22 to Node 24 in the first deploy of this
feature instead of blocking it.

The forward workflow also proves rollback compatibility before rollout: early
in the gated `deploy` job — before the transaction and ahead of the Python
host-test wait — it reads the actual deployed release identity from the VM
through the transaction's read-only `current-sha` operation (no deploy lock;
`none` before the first release), checks out exactly that revision on the
runner and rebuilds its dependencies and boots its clean start under Node 24
(`npm ci` + `npm run build`), then restores the candidate checkout. A failing
compatibility proof fails the deploy job before rollout. This is a
source/runtime compatibility proof only — never an OS rollback.

## Runtime failure and rollback semantics

Application rollback stays code/config-only. The deployment transaction does
not snapshot, restore or downgrade OS packages: a rollback restores the
previous Hub2 source, dependencies and protected configuration and keeps the
installed Node runtime. A provisioned Node 24 therefore survives application
rollbacks (the restored release rebuilds and runs on 24), and a Node runtime
failure is never repaired — or claimed repaired — by an application rollback.

Runtime provisioning failures are honest incidents inside the locked apply,
before any application mutation: a failed key download, key/fingerprint
mismatch, existing foreign keyring or apt source, index refresh failure,
origin/version/hash mismatch, a cache archive that changed or vanished between
verification and consumption, or a failed install exits nonzero with a
receipt while no application code, npm state, configuration or unit was
touched and no transaction state exists. There is no automatic retry, no
automatic OS rollback, no blind downgrade to Node 22 and no repeated install;
recovery is package-manager custody (inspect `apt-get`/`dpkg` state on the VM
as the operator).

## Rollout steps (per revision)

1. Obtain independent review of the exact PR head and green required CI, then
   integrate with `gh pr merge --auto --rebase --match-head-commit <reviewed-sha>`.
2. Every push to `main` starts the production workflow. Its deploy job runs
   after the required test, Hub2, distribution and Python checks pass. Coordinate
   integration with the designated deploy owner; pull requests never deploy.
3. The CI transaction stages an immutable release, provisions Node 24 under
   its deploy lock before application mutation, applies the accepted release,
   and checks public readiness and the live start canary. Follow the CI result;
   a successful health response alone does not establish launch acceptance.

Manual dispatch remains an operator recovery/bootstrap path. Use the same CI
transaction rather than installing from a working checkout on the VM. See
[host releases](HUB2-RELEASES.md#automatic-production-deploy-after-green-main-ci-intake-bf07f437)
for updater and canary behavior.

The CI dispatch `release_mode` choice is passed to the installer. The installer
updates only `HUB2_RELEASE_MODE` in the VM's existing `.dev.vars`, preserving
the other private values; a local installer run without `--release-mode`
preserves the current mode. Select `active` after operator pairing and host
enrollment are complete.

## Bootstrap → active

Same contract as before: `/health` shows
`{"status":"bootstrap","dispatch_enabled":false}` until the operator pairs
(device flow at `https://hub.dtcdev.click/device`, Cognito login — allowlist and
callback origin are the non-secret defaults in `hub2/node/config.ts`) and hosts
enroll; then set `HUB2_RELEASE_MODE=active` in `/opt/agent-hub/.dev.vars` and
restart the service. The clean-start model holds: the VM DB initializes fresh;
the CLI/hosts re-pair once. `HUB2_BOOTSTRAP_TOKEN` in `.dev.vars` is carried for
out-of-band host bootstrap only; the operator device flow does not use it.

## VM IP self-heal (DNS updater, safety net)

The VM external IP is a reserved static address (see "Static VM IP" below), so
a restart keeps it and this unit normally no-ops; it stays enabled as a
safety net in case the address is ever released or the VM is recreated. The
unit
`agent-hub-dns-update.service` (enabled for boot by
`deploy/vm/install-hub2-node.sh`) runs `deploy/vm/hub2-dns-update.sh`, which
compares the GCP metadata external IP with the current
`agent-hub-vm.dtcdev.click` A record and Route53-UPSERTs when they differ.
Because the CloudFront origin uses that DNS name (not a bare IP), this one
update restores the whole edge path. The script no-ops (warning only) without
`/etc/agent-hub/dns-updater.env`, which holds the scoped IAM user
`agent-hub-vm-dns` keys (only `route53:ChangeResourceRecordSets` on record
`agent-hub-vm.dtcdev.click` A in zone `Z05963572WVWFHDQZH5NE`); that env file
is provisioned on the VM out of band and never lives in Git. Run
`sudo /usr/local/sbin/hub2-dns-update.sh --force` to verify the credential path.

## Dapier dev-alert direct ingest

`POST https://hub.dtcdev.click/webhooks/oncall` is served by this Hub2 Node
service on the GCP VM. It stores each authenticated alert in the ControlDO
SQLite database before returning success. Dapier keeps the existing Developer
webhook as its first destination and posts the same event to this endpoint as
its second destination. A stable `X-Delivery-Id` makes Dapier retries
idempotent.

The host session service polls `/api/oncall-alerts/offers` with a separate
poll token, then runs `~/bin/oncall-send` and `~/bin/orch-send`. Those helpers
write their inbox files before attempting session wake, so a stopped
`oncall-engineer` still leaves the alert in the inbox. Provision the token
pair once with `POST /api/oncall-alerts/config`: while release mode is
`bootstrap`, authorize with `HUB2_BOOTSTRAP_TOKEN`; in `active` mode, use an
operator-admin credential. The response displays the ingest and poll tokens
once. Store only the ingest token in Dapier's private
`dev-mailbox/oncall_webhook_token` storage value. Store the poll token in the
host's private `<journal_dir>/oncall-alerts.json` file with shape
`{"poll_token":"<one-time-token>"}` and owner-only permissions. Never commit
or include either token in reports.

## Backup (works with the Node deployment)

- `scripts/hub2-db-backup.sh` — `VACUUM INTO` per instance DB (WAL-safe
  against the live server), counts manifest, dual trees
  (`BACKUP_DIR`/`MIRROR_DIR`), ≥30-day retention that fails closed, flock,
  `HUB2_DB_DIR=/var/lib/agent-hub/hub2-db`, artifacts `hub2-db-<STAMP>.tar.gz`
  mode 600. Any failure invokes `ALERT_CMD --backup-failed "<reason>"`.
- `scripts/hub2-db-restore-drill.py` — extracts an artifact, `PRAGMA
  integrity_check` per database, compares table counts with the manifest.
- `scripts/hub-db-backup-staleness.sh` — `BACKUP_GLOB=hub2-db-*.tar.gz` on the
  VM (`/etc/hub2-db-backup-staleness.env`), log-only check at 04:50 UTC root.
  The cron explicitly selects this env and uses `--no-mail` (VM has no MTA).
- Both VM trees are local. RMTHZ `hub2-backup-pull.sh` takes an additional
  online snapshot daily at 05:35 Europe/Berlin, copies Hub2 artifacts to RMTHZ
  and Storagebox, and alerts on failure through the existing msmtp path.
  RMTHZ checks the Hub2 glob every 15 minutes (36h threshold, 24h repeats).
  See `docs/HUB-DB-BACKUP.md` for the 2026-11-08 cutover and retired Hub1 cron.

## Cutover record (2026-11-07)

1. aws-infra `sandbox/agent-hub`: created the stack, imported the existing
   hand-applied `hub` A/AAAA alias records (no diff) and created
   `agent-hub-vm.dtcdev.click A 35.198.99.237` (plan: 1 add, 0 change,
   0 destroy).
2. aws-infra `sandbox/hub-edge`: CloudFront origin
   `agent-hub.cloudflare-ai-agent-*.workers.dev` (https-only) →
   `agent-hub-vm.dtcdev.click` (http-only, X-Forwarded-Host preserved),
   applied via a reviewed change set (no replacement).
3. Verified `https://hub.dtcdev.click/health`, then bootstrap→active.

Code rollback preserves the live SQLite databases. Additive DDL still needs a
compatible reader: an older binary rejects unknown `hub2_schema` versions.
Never restore a predeploy database as an application rollback, because that
would discard later user writes, credentials, commands and outbox receipts.

The deployment transaction reads the actual schema singleton in every existing
namespace database before changing the checkout. It checks the candidate and
previous revisions against `deploy/schema-compatibility.json` and the source
schema targets. Legacy revisions without the manifest are treated conservatively.
Missing metadata, unknown namespaces, symlinks and unsupported versions refuse
deployment or rollback before checkout. The census is read-only and prints no
user rows or environment values.

A first compatible bridge deploy selects `HUB2_SCHEMA_MIGRATIONS=defer` when
the previous reader supports only Control4 / Machine6. Starts and normal task
capture retain their existing contracts; new shared-memory APIs and explicit
screen capture return `schema_migration_deferred` until their schemas are enabled.
Commit and verify that bridge before a subsequent deploy enables Control5 /
Machine7 with `apply`. The bridge reads both versions and retains new writes on
rollback. The installer persists the selected mode atomically without replacing
unrelated configuration or file permissions. Unknown future versions still fail
closed. Never enable migrations manually to bypass the transaction.

A successful `/health` alone does not prove that object migrations succeeded.
The Node startup readiness check must finish existing object initialization
before listening. Public authenticated API and canary verification remain
required before finalizing the transaction. Failure keeps protected transaction
state for recovery; rollback rechecks the current databases before selecting the
previous binary and restores only Hub-owned configuration.

## Static VM IP

`35.198.99.237` is reserved as static address `agent-hub-vm-ip`
(region `europe-west3`, project `dtcdev-click`), promoted in place from the
ephemeral address on 2026-11-07 — zero downtime, same IP. GCP charges nothing
while a static address is attached to a running VM; it bills
reserved-but-unattached addresses (~USD 3.6/month), so never release it
without attaching it elsewhere. `vm_ipv4` in `aws-infra/sandbox/agent-hub`
should therefore no longer drift after a VM restart.

GCP access is not part of operating Hub. Do not use `gcloud`, GCP console
SSH or hand edits on the VM to update or inspect it. The VM IP is static and
CI deploys over the SSH deploy key, so routine operation needs neither.

## Shipping an update (CI/CD only)

1. Open a PR against `main`. Required CI must pass on the exact final head;
   obtain independent review and enable
   `gh pr merge --auto --rebase --match-head-commit <reviewed-sha>`.
2. A push to `main` automatically deploys in active mode after the workflow's
   required gates pass, in the `production` environment. The deploy owner
   observes the serialized transaction and its canary; merging documentation
   also triggers this workflow.
3. CI uploads and verifies an immutable release bundle, applies it under the
   deploy lock, checks readiness and the live start canary, and finalizes only
   after acceptance. Failure follows the protected rollback path above.
4. For operator-directed recovery/bootstrap, manual dispatch remains available:
   `gh workflow run deploy.yml --ref main -f dry_run=false -f release_mode=active`.
   Bootstrap deliberately disables dispatch and requires the workflow's separate
   confirmation. Keep routine deployment and inspection in the maintained CI
   path; do not SSH into the VM to install or edit files.
