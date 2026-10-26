# Agent Hub Deploy Runbook

## Current deployment (15 October 2026)

- Worker `agent-hub` (DO class `Hub`, instance `hub`) is deployed from `main` at `https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev`, crons `*/30` (root slot, root scheduling switched **off**) and `*/5` (scheduled checks). Previous version for rollback: `1ec25f7c-0f00-4087-a4d6-c621ad34fba5` (`npx wrangler rollback <id>`).
- Secrets: `SESSION_SECRET`, `HUB_ADMIN_TOKEN_SHA256` (admin token kept at `~/.config/agent-hub/admin-token` on Hetzner, mode 0600). `HUB_ADMINS` is unset because DTC sign-in only admits `@datatalks.club` Google accounts and the right address has not been confirmed; set it with `wrangler secret put HUB_ADMINS` to enable OIDC sign-in.
- Optional: `HUB_ALERT_TO` (comma-separated bus actors) and `HUB_ALERT_WEBHOOK` (secret).
- Deploy: `npx --no-install wrangler deploy` with `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` from `~/.config/cloudflare/agent-branches.env`. Never deploy from a checkout that has a `.dev.vars`.

## Domain cutover: hub.dtcdev.click

`dtcdev.click` is a Route 53 zone (`Z05963572WVWFHDQZH5NE`, managed by `DataTalksClub/aws-infra` `sandbox/common/domain`). A Worker custom domain needs the hostname in an active Cloudflare zone, and the only Cloudflare zone in this account is `aishippingblog.com`. Cloudflare for SaaS (custom hostnames, free for up to 100) on that zone serves `hub.dtcdev.click` without moving the DTC zone:

1. Cloudflare token: create one with, for zone `aishippingblog.com`, *Zone: DNS Edit*, *Zone: SSL and Certificates Edit*, *Zone: Custom Hostnames Edit* (part of SSL for SaaS) and *Zone: Workers Routes Edit*. The existing account token can deploy Workers but has no zone permissions.
2. In `aishippingblog.com`: add a proxied `AAAA hub-origin 100::` record and set it as the custom-hostname fallback origin.
3. Create custom hostname `hub.dtcdev.click` (DV certificate, TXT validation). Note the ownership TXT (`_cf-custom-hostname.hub`) and certificate validation TXT values.
4. In Route 53 (`dtcdev.click`): `CNAME hub -> hub-origin.aishippingblog.com` plus the two TXT records. Add them to the `common/domain` Terraform root and apply (needs AWS credentials; AWS Gate was closed on 15 October).
5. Worker route on zone `aishippingblog.com`: `hub.dtcdev.click/*` -> `agent-hub` (add `"routes": [{"pattern": "hub.dtcdev.click/*", "zone_name": "aishippingblog.com"}]` to `wrangler.jsonc`).
6. Optional OIDC on the new domain: add `https://hub.dtcdev.click/auth/callback` and `https://hub.dtcdev.click/` to the AgentHubClient callback/logout URLs in `aws-infra/sandbox/auth/template.yaml` (CI deploys `dtcdev-shared-auth` on push to main) and set `AUTH_CALLBACK_URL` accordingly. Until then, token sign-in works on the new domain.
7. Verify: unauthenticated `/`, `/fleet.html`, `/fleet.js` redirect to sign-in; `/api/*` return 401; token sign-in works; `/api/liveness` returns data.

Rollback: remove the Worker route and custom hostname; the Worker, its Durable Object and the workers.dev URL are unaffected.

Public authentication bootstrap endpoints (login, callback and device pairing) are necessary to obtain credentials; they must not disclose dashboard, registry or agent data. The protected dashboard must not be published as a separate unguarded static site.

Rollback: restore the prior standalone Worker deployment and callback configuration, and remove only the new hub hostname binding if required. Retain the DO and all messages, account labels, launch reservations and uncertain effects. Do not delete or recreate the data store, modify the contest demo or restart the retired Win35 root authority.


## 1. Prerequisites and Decisions Needed (Root Approval)

Before the first deployment, the following decisions must be made and approved by Alexey:
- **Cloudflare Account:** Which Cloudflare account to use for deployment.
- **Domain:** Whether to use the default `*.workers.dev` subdomain or `hub.dtcdev.click`.
- **DTC Auth Admin:** Confirm the admin email(s) for the hub (e.g., Alexey's `@datatalks.club` address).
- **Writer Schedule:** Confirm the writer schedule time (default 22:00 Berlin) and if the 09:00 standup check moves to the hub.

**NO SPENDING IS ALLOWED.** The budget is USD 5/month, and the design strictly fits the Cloudflare Workers Free plan (USD 0/month). The deployment bundle must be within the Free tier size limits (64 MiB uncompressed and no compressed size limit, as per Cloudflare changelog 2026-09-04, https://developers.cloudflare.com/changelog/post/2026-09-04-increased-worker-size-limit/). No upgrades or new keys should be made without Alexey's permission.

**Note on M1 Deployment:** Root separately deployed M1 from `af3a` with public vars supplied via `wrangler deploy --var`. Without persisting these in `wrangler.jsonc`, a future plain deployment would drop them. Root owns future approved deployment; this head/worker performs source/local testing only. Admin login is incomplete because `HUB_ADMINS` is unset (never invent an admin).

## 2. Secrets List

The following secrets must be prepared as a `.dev.vars` file for local development and uploaded to Cloudflare via `wrangler secret put` during deployment.
**NEVER store secrets in Git, issues, or the bus.**

- `SESSION_SECRET`: The session signing secret required for HMAC/encryption of the session cookies.
- `HUB_ADMINS`: Comma-separated list of admin email addresses.

**Local overrides:** For local development, `.dev.vars` overrides `AUTH_CALLBACK_URL` to a local URL, specifies `HUB_ADMINS` and `SESSION_SECRET`, and can use a fake `AUTH_ISSUER` if used. Tests use explicit miniflare fake bindings so do NOT call actual Cognito or production endpoints. No docs tests.

Note: DTC Cognito clients are public with PKCE and do not require an `AUTH_CLIENT_SECRET` unless Alexey approves a confidential client.

## 3. DTC Auth App Client Configuration

**Provisioned (11 Oct 2026, approved by Alexey).** The `agent-hub` client now exists in the shared DTC pool (DataTalksClub/aws-infra `78987cd`, stack output `AgentHubClientId`; DataTalksClub/auth `eef1ddf`). These are public, non-secret values:

| Setting | Value |
| --- | --- |
| `AUTH_BASE_URL` | `https://auth.dtcdev.click` |
| `AUTH_CLIENT_ID` | `1tplqm9l6q6545mrk0gjv6eaeh` (public PKCE client, no secret) |
| `AUTH_ISSUER` | `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_H7nJu52Bs` |
| `AUTH_JWKS_URL` | `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_H7nJu52Bs/.well-known/jwks.json` |
| `AUTH_CALLBACK_URL` (prod) | `https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/callback` |
| `AUTH_CALLBACK_URL` (dev) | `http://localhost:19130/auth/callback` |

Registered logout URLs: `https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/` and `http://localhost:19130/`. The production host is the `workers.dev` route of Worker `agent-hub` on the Cloudflare account subdomain `cloudflare-ai-agent-de9ca0`; renaming the Worker or moving to a custom domain requires a matching client update in both DTC repos. The proposal below is kept for reference.

An app client must be created for `agent-hub` in DTC's AWS Cognito.
Do not invoke these changes yourself. The diffs below are PROPOSED changes for the DataTalksClub repositories (`aws-infra` sandbox/auth and `auth` config/clients.json).

### Proposed changes to `DataTalksClub/auth` (config/clients.json):
```json
    {
      "name": "agent-hub",
      "client_id": "<AGENT_HUB_CLIENT_ID_PLACEHOLDER>",
      "callback_url": "https://<hub-host>/auth/callback",
      "logout_url": "https://<hub-host>/"
    }
```

### Proposed changes to `DataTalksClub/aws-infra` (sandbox/auth/template.yaml):
Under `Resources`:
```yaml
  AgentHubClient:
    Type: AWS::Cognito::UserPoolClient
    Properties:
      ClientName: agent-hub
      UserPoolId: !Ref UserPool
      GenerateSecret: false
      PreventUserExistenceErrors: ENABLED
      EnableTokenRevocation: true
      AllowedOAuthFlowsUserPoolClient: true
      AllowedOAuthFlows: [code]
      AllowedOAuthScopes: [openid, email, profile]
      SupportedIdentityProviders: [!Ref GoogleIdentityProvider]
      CallbackURLs: [https://<hub-host>/auth/callback]
      LogoutURLs: [https://<hub-host>/]
      AccessTokenValidity: 60
      IdTokenValidity: 60
      RefreshTokenValidity: 30
      TokenValidityUnits:
        AccessToken: minutes
        IdToken: minutes
        RefreshToken: days
```
Under `Outputs`:
```yaml
  AgentHubClientId: {Value: !Ref AgentHubClient}
```

## 4. Staged Rollout and Cutover

Migration proceeds in phases as defined in the SPEC. The current Grok root and writer schedule remain active (not replaced) during P0 and P1.

### Phase 0: Local Dev
- No deploy. Run `wrangler dev` and local tests.

### Phase 1: Deploy and Shadow
- Deploy to Cloudflare Workers Free.
- Register the Hetzner host agent.
- The Hub runs shadow root runs (`dry_run` mode, `launcher plan` only).
- **Rollback (P1):** Stop the Hetzner host agent service. The current Grok Bot 30-min routine remains unchanged and should NOT be stopped. No old services are mutated.

### Phase 2: Root Cutover
- Enable real root runs on Hetzner via the hub.
- Pause the Grok Bot 30-minute routine (only after root confirms with Alexey).
- Root runs use the hub bus, with the Hetzner host bridging aplexer mailboxes.
- **Rollback (P2):** Resume the Grok Bot 30-minute routine. Do NOT re-enable the retired Win35 root/authority; the sole current Grok root is preserved.

### Phase 3: Home Machines
- Install host agents on Win35 and laptop.
- Shift their bus traffic to the hub.
- **Rollback (P3):** Restart `agent-bus-win35-adapter.service` (:8788).

### Phase 4: Health and Dashboard
- Hub health alerts replace supervision status.
- `principal-wake` replaces `principal-ping.timer`.
- **Rollback (P4):** Re-enable `principal-ping.timer` and `experiment-supervision` status role.

### Phase 5: Writer
- Enable writer runs from the hub schedule.
- Explicit publication stays with the existing coordinator and must never be run or duplicated by the hub (the hub never runs `publish_daily.py --publish`).
- **Rollback (P5):** Revert to the manual/other existing writer schedule.

### Phase 6: Cleanup
- The role authority code is archived (service is already inactive). Docs in `cloudflare-agent-git` updated (`01-overview.md#machines`, `02-root.md`, `recovery/*`).
- **Rollback (P6):** Re-enable retired pieces. Note: do NOT re-enable the retired Win35 root/authority. Preserve the sole current Grok root.

## 5. Host Agent Installation

Note: The actual host package is still under M0/M2 development. The provisional host contract (source pinned at `47e3a400a9c862875473dde1e1a437c15c42558b`) has been verified locally, but final M7 acceptance waits for accepted M2 + integrated full source + different-model review.

### Prerequisites
Before installing the service, create an isolated `.local` virtual environment, install the `agent_hub_host` package from the repository root, and login. No third-party tools or service installation scripts should be used.

```bash
cd ~/git/agent-hub
# Preflight check: Verify python3 and ensurepip are available.
# If this fails, STOP and report to ROOT/Alexey (deployment kit pending).
# Do NOT elevate or install OS packages.
python3 -c "import ensurepip" || exit 1
python3 -m venv .local/venv
.local/venv/bin/python -m pip install .
.local/venv/bin/hub login https://<hub-domain>
```
*Note: Explicit setup-command order prepared only. Never enable the unit here.*

### Linux (Hetzner)
Installed as a user systemd service. Ensure the config file (`~/.config/agent-hub/host.json` or `$XDG_CONFIG_HOME/agent-hub/host.json`) has mode `600` and its parent directory has permissions `0700` to restrict access. The `hub` executable retains executable permissions (`700` or `755` within the private parent).
```ini
# ~/.config/systemd/user/agent-hub-host.service
[Unit]
Description=Agent Hub Host Agent
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/git/agent-hub
Environment="PATH=%h/.local/bin:%h/git/agent-hub/.local/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=%h/git/agent-hub/.local/venv/bin/hub run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```
Run: `systemctl --user daemon-reload`, `systemctl --user enable --now agent-hub-host.service`.

### Windows (Tabo session host, ARM64 under x64 emulation)

> Status: Windows host support is source-ported (supervisor/session_host Windows
> branches, arch detection, needs routing) but has NOT been physically accepted
> on Tabo yet. The PENDING-HUMAN commands in the Tabo intake report are the
> acceptance gate; do not claim physical acceptance without their output.

Prerequisites (native ARM64 unless noted): Git for Windows, gh, Node LTS
(arm64, 22+ for engine provisioning), .NET 10 SDK, PowerShell 7,
**Python 3.12 x64** (emulated on purpose: aplexer + aplexer-client ship only
`windows-amd64` v0.1.19; a native ARM64 Python cannot load the `win_amd64`
wheel), aplexer + `a` `windows-amd64` in
`%LOCALAPPDATA%\Programs\aplexer`. Clone `agent-hub` to `C:\repos\agent-hub`,
create an x64 venv, `pip install -e .` (provides `hub.exe` + aplexer-client),
then `hub login <url> --kind host --name <machine>` (device pairing approved
by Alexey in `/device`; credential lands in `%APPDATA%\agent-hub\host.json`
with an owner-only ACL, never printed).

Run the supervisor (not legacy `hub run`) under a Scheduled Task so it
survives logoff/logon, reboot and sleep/wake. The supervisor starts ONLY its
own `hub-controller-*` shell session, omits Linux cgroup flags on Windows
(Job Object containment via `containment_empty`), and restarts it on exit:

```powershell
# enroll-tabo.ps1 -Phase service does exactly this:
$TaskName = 'AgentHubSessionHost'
$runner = "$env:LOCALAPPDATA\agent-hub\run-session-host.ps1"  # wrapper loop, restart 15s
$psExe = (Get-Command powershell.exe).Source
$action = New-ScheduledTaskAction -Execute $psExe -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew -WakeToRun
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
Start-ScheduledTask -TaskName $TaskName
```

Wrapper loop (`run-session-host.ps1`): `while ($true)` runs
`scripts/session-host-supervisor.py --workspace C:\repos\agent-hub --config
%APPDATA%\agent-hub\subscriptions.json --credential-config
%APPDATA%\agent-hub\host.json --journal-dir %LOCALAPPDATA%\agent-hub\journal`,
appending to `%LOCALAPPDATA%\agent-hub\logs\session-host-YYYYMMDD.log`,
restart 15 s. Interactive (logged-on) principal on purpose: on-device UI
testing needs the user's desktop. Hosts poll outbound, so no inbound port is
needed. Logs: `%LOCALAPPDATA%\agent-hub\logs\`. Rollback:
`Unregister-ScheduledTask AgentHubSessionHost` (revoke the machine in the Hub
UI, then delete `%APPDATA%\agent-hub\host.json` only after the revoke).

Legacy `hub run` (Basic Task, `hub.exe run`) is superseded for session hosts;
keep it only for human CLI use.
