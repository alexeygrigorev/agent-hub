# Agent Hub Deploy Runbook

This runbook describes the deployment process for Agent Hub to the Cloudflare Workers Free plan.
No deploy, create, or login actions should be run without explicit root and Alexey approval.

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

### Windows
Installed as a Task Scheduler logon task with User-only ACLs (Run only when user is logged on). Actual Windows 47 ACL is PROVISIONAL/unaccepted. Plain actual Windows has NOT been executed.

```cmd
cd %USERPROFILE%\git\agent-hub
py -3 -m venv .local\venv
.local\venv\Scripts\python.exe -m pip install .
.local\venv\Scripts\hub.exe login https://<hub-domain>
```

Create a Basic Task in Task Scheduler:
- Trigger: When I log on
- Action: Start a program
Resolve your actual user profile path using `echo %USERPROFILE%` (CMD) or `$env:USERPROFILE` (PowerShell). Replace `<USERPROFILE>` below with the absolute result.
- Program/script: `<USERPROFILE>\git\agent-hub\.local\venv\Scripts\hub.exe`
- Add arguments: `run`
- Start in: `<USERPROFILE>\git\agent-hub`
