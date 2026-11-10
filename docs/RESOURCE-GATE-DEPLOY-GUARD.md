# Resource Gate and Hub deployment guard

Production Hub2 releases use the `Hub 2 production` workflow in
`.github/workflows/deploy.yml`. The workflow is the only supported SSH path to
the GCP VM; it streams the reviewed `deploy/vm/deploy-release-transaction.sh`
script and passes only a full Git commit SHA plus the allowlisted `bootstrap` or
`active` release mode.

Before changing the VM, the transaction script fetches the requested commit,
checks that the Hub site candidate (`deploy/hub.caddy`) proxies to the Hub
upstream `127.0.0.1:8787`, that the shared `/etc/caddy/Caddyfile` (owned by
aws-infra, never written by Hub) still imports `/etc/caddy/sites/*.caddy`, then
runs `caddy validate` on both. The Resource Gate site, its `127.0.0.1:8791`
upstream and the `/v1/health` alias are owned by aws-infra; the public Gate
smokes below still verify them after install. A domain-only Caddy site address enables automatic
HTTPS and uses port 443 by default; the production smoke also verifies the
public HTTPS page after install. See [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https)
and [Caddy validate](https://caddyserver.com/docs/command-line#caddy-validate).

The transaction snapshots the current commit, the Hub site file `hub.caddy` and `.dev.vars` under
`/var/lib/agent-hub/deploy-rollback` with root-only permissions. The snapshot
stays in place until all public checks pass. It refuses to start if the current
`.dev.vars` is absent because that would make an exact rollback impossible:

- `https://gate.dtcdev.click/` returns HTTP 200 and the Resource Gate page marker.
- Authenticated, non-mutating `GET
  https://gate.dtcdev.click/v1/health` returns
  HTTP 200 and a boolean `granted` value. An inactive time-limited grant is a
  healthy response between approved desktop tasks. The Actions secret
  `RESOURCE_GATE_PROBE_TOKEN` is mirrored from the canonical DataOps secret
  `resource-gate.agent-token`; the value never enters Git or workflow output.
- `GET https://hub.dtcdev.click/health` reports the selected Hub2 release mode.

The Resource Gate API implements the authenticated, non-mutating
`/v1/check?resource=browser&scope=desktop` endpoint, which returns HTTP 200 and
a boolean grant state. The `gate.dtcdev.click` Caddy site maps `/v1/health` to
that endpoint so monitors and deploy checks can use a stable health URL.

Both live Gate HTTP probes run by default (`workflow_dispatch.verify_gate=true`).
A separate Hub-only release whose intake explicitly forbids contacting or
changing Gate may dispatch with `verify_gate=false`. That uses the isolated
Hub-only installer path: it skips Gate probes, candidate Caddy validation,
Hub site installation, and Caddy reload/restart. The Hub API smoke remains
mandatory, and rollback restores the prior Hub release without touching Caddy.
The t1881u production deployment must keep `verify_gate=true`.

The existing `resource-gate-monitor.timer` runs on RMTHZ outside the GCP VM
every minute. It checks the public gate TLS page, reads the authenticated
desktop grant state, reads the authenticated Agent Browser `/browser/devices`
health for the `desktop` device (DESKTOP-FP4OP26), and reads the Hub API health
endpoint used by clients. After two consecutive failed checks, it sends alerts
to `dev@dtcdev.click`, whose Dapier `dev-mailbox` workflow posts to the
Developer webhook. It creates one DataOps incident task and adds a recovery
note/email. Inactive grants are expected between approved browser tasks. The
test command after installation is `sudo -u alexey /usr/local/sbin/resource-gate-monitor.sh --test-alert`;
it sends a clearly labelled test email to `dev@dtcdev.click`, which should
produce a successful `http_request` run in Dapier's `dev-mailbox` workflow and
wake Developer, and creates/closes a test DataOps Task.

An install or smoke failure runs the fixed rollback operation. It checks out
the previous commit, restores `.dev.vars`, reapplies that release's installer,
restores only the prior `/etc/caddy/sites/hub.caddy` (the shared Caddyfile is
never touched), reloads Caddy and Hub2, and verifies local
Hub health with a bounded readiness retry for service startup. Rollback validates
the shared config with `caddy validate`. The workflow remains failed
after rollback so CI reports the release failure. If a runner is interrupted
before it can roll back, the protected snapshot remains and the next deploy
refuses to proceed; dispatch the same workflow with `rollback_pending=true` to
restore it before trying another release.

The focused transaction regression is `bash deploy/vm/test-deploy-guard.sh`.
It exercises a successful staged apply, a manual rollback of the Hub site file,
automatic rollback after dependency or installer failure, rejection of a
candidate without the Hub upstream, and fail-closed refusal when the shared
Caddyfile lacks the sites import. `deploy/vm/test-hub-site-ownership.sh` proves
a Hub deploy changes no other site block or the shared file. Both run in the
always-on `deploy-guards` workflow.
