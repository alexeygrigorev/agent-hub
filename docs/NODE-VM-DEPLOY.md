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
.local/tmp/db --dev-vars hub2/.dev.vars` (TLA; needs Node ≥ 22.13).

## Rollout steps (per revision)

1. Merge to main (full hub2 + distribution CI green).
2. CI deploy (manual dispatch, `dry_run=false`) or on the VM:
   `cd /opt/agent-hub && git fetch && git checkout --detach origin/main &&
   npm ci --omit=dev && sudo bash deploy/vm/install-hub2-node.sh`.
3. `systemctl status agent-hub-hub2` + `curl -s http://127.0.0.1:8787/health`.

## Bootstrap → active

Same contract as before: `/health` shows
`{"status":"bootstrap","dispatch_enabled":false}` until the operator pairs
(device flow at `https://hub.dtcdev.click/device`, Cognito login — allowlist and
callback origin are the non-secret defaults in `hub2/node/config.ts`) and hosts
enroll; then set `HUB2_RELEASE_MODE=active` in `/opt/agent-hub/.dev.vars` and
restart the service. The clean-start model holds: the VM DB initializes fresh;
the CLI/hosts re-pair once. `HUB2_BOOTSTRAP_TOKEN` in `.dev.vars` is carried for
out-of-band host bootstrap only; the operator device flow does not use it.

## Ephemeral VM IP self-heal (DNS updater)

The VM external IP is ephemeral: a stop/start can change it. The unit
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
  VM (`/etc/hub2-db-backup-staleness.env`), cron `20 4 * * *` root.
- VM cron also mirrors artifacts off-box when the second tree is configured;
  Hetzner-side staleness alerts keep the working msmtp path.

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

Rollback of traffic = revert the hub-edge change set (origin back to the
Cloudflare Workers endpoint, which stays deployed but idle). Rollback of code
= `git checkout --detach <previous-ref>` on the VM + restart; the DB schema is
additive per hub2's schema versions, and `deploy/vm/install-hub2-node.sh` is
idempotent.

## EPHEMERAL IP (known gap)

`35.198.99.237` is an **ephemeral** GCP address. If the VM restarts under a
new IP: update `vm_ipv4` in `aws-infra/sandbox/agent-hub` and `terraform
apply` (TTL 300 s). Reserving a static IP needs GCP access, which this setup
deliberately does not require.
