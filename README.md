# Agent Hub

Generic agent-fleet hub on Cloudflare: authenticated messaging and dashboards, machine subscriptions and shared account quotas, placement of headless agents, and real interactive sessions. See [the generic hub guide](docs/GENERIC-HUB.md) for configuration, CLI commands, execution boundaries and migration. The intended production URL is `https://hub.dtcdev.click`; deployment and physical host acceptance are separate from source availability.

Serverless coordination hub for Alexey's multi-agent project: 30-minute headless root runs on a random available host, the daily writer job, machine registry, message bus, health and dashboard.

The spec is [docs/SPEC.md](docs/SPEC.md); it is the source of truth for scope and milestones.

See `AGENTS.md` for team and process rules.

Task routing supports capability matching, batches, client-owned asynchronous jobs and configurable model tiers. See [routing API, CLI and host setup](docs/ROUTING.md).

## Root scheduling flag

The hub can pause and resume root-run scheduling through a persisted flag. It is
stored in the Hub Durable Object's SQLite (`root_scheduling` table, single row),
so it survives restarts and is checked inside the same storage transaction that
creates offers and claims: there is no flag-check race window.

- **Default: enabled.** An uninitialized or upgraded deployment keeps the exact
  pre-flag scheduler behavior.
- **Disable (`enabled: false`) atomically prevents:**
  - the scheduler tick from creating a root-run job for a slot (no job, no offers),
  - any new offer of a root-run job (fallback after decline/timeout included),
  - new claims of root-run jobs, including jobs that are already offered,
  - root lease takeovers, and root-run START transitions (claimed -> running).
- **Frozen while disabled:** a root-run job that was already offered stays
  `offered` (no offer expiry, no re-offer to another host) and is hidden from
  `GET /api/jobs/offers`, `GET /api/host/offers` and `POST /api/host/poll`.
- **Not affected:** a root run that is already `running` keeps its lease; renew
  and finish continue to work. Disabling never kills a live root. Lease expiry
  and the no-first-action (claimed, not started) expiry continue.
- **Deadline:** a root-run job whose `deadline_at` has passed is never offered
  again. When its offer times out or is declined it is closed as usual
  (`no-host` if no candidate is left), otherwise as `expired` with
  `deadline passed before claim` instead of going to another host. While
  disabled, alarm processing closes such a job as `expired` too. Re-enabling
  after a long pause therefore leaves no stale offer for an old slot.
- **Idempotent:** repeated PUTs with the same value change nothing (including
  `updated_at`); disable followed by enable restores normal scheduling.

### API

Write authority is the existing admin session (DTC login, `HUB_ADMINS`
allowlist) only. `PUT` requires the session CSRF header; the body is strict.
Bearer tokens (host, agent, job/lease tokens) cannot change the flag.

Read authority is the admin session or an existing host bearer token, verified
the same way as `POST /api/host/poll` (`host` scope, kind `host`, not revoked,
not expired, issued to a registered machine). The host is identified by its
verified credential only. Unknown, revoked or expired tokens, and agent, job or
admin-scoped bearer tokens, get `401`.

`GET /api/root/scheduling` — read the state:

```
GET /api/root/scheduling
Cookie: hub_session=<session cookie>
  (or) Authorization: Bearer <host access token>

200 OK
{"enabled": true, "updated_at": "2026-10-14T18:00:00.000Z", "server_time": "2026-10-14T18:01:02.345Z"}
```

`updated_at` and `server_time` are UTC (ISO 8601, `Z`). `updated_at` reports
the last actual change (the table creation time if never changed); it is `null`
only if the row is missing (then the safe default applies).

`PUT /api/root/scheduling` — set the state:

```
PUT /api/root/scheduling
Cookie: hub_session=<session cookie>
X-CSRF-Token: <csrf token from GET /auth/me>
Content-Type: application/json

{"enabled": false}

200 OK
{"enabled": false, "updated_at": "2026-10-14T18:05:00.000Z", "server_time": "..."}
```

Failures:

| Status | Error | When |
| --- | --- | --- |
| 401 | `unauthorized` | `GET`: no admin session and no valid host bearer. `PUT`: no admin session (all bearer tokens) |
| 403 | `forbidden` | Session is not an admin (defensive; non-admin sessions are normally rejected as 401) |
| 403 | `CSRF token missing or invalid` | `PUT` without a valid CSRF header |
| 400 | `malformed_json` | Body missing, not valid JSON, or over 32 KiB |
| 400 | `invalid_body` | Body is not exactly `{"enabled": <boolean>}` (extra or unknown fields rejected) |
| 400 | `invalid_enabled` | `enabled` is not a boolean |
| 405 | Method Not Allowed | Anything but `GET`/`PUT` |

Related gating failures while disabled: `POST /api/jobs/:id/claim` on a root-run
job returns `409 {"error": "conflict"}`; `POST /api/jobs/:id/start` on a root-run
job returns `409 {"error": "root_scheduling_disabled"}`; root takeover returns
`409 {"error": "root_scheduling_disabled"}`. Each state change is recorded in the
`audit` table (`root_scheduling_enable` / `root_scheduling_disable`).

The examples above are tokenless: they show placeholder cookie, bearer and CSRF
values only, never a real credential.

## License

MIT


## MCP clients

Connect to **https://hub.dtcdev.click/mcp** using Streamable HTTP and **OAuth with dynamic client registration (DCR)**. This is part of Agent Hub, separate from `mcp.dtcdev.click`. It reuses the Hub Durable Object, API permission checks, host execution/admission gates and existing CI deployment; it needs no additional infrastructure or credentials.

The client discovers `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server`, registers its exact callback at `/oauth/register`, then opens `/oauth/authorize`. Sign in through the existing Hub Google login and explicitly allow the connection. Remote HTTPS callbacks and loopback HTTP callbacks are supported; there is no localhost requirement for remote clients. PKCE S256, a resource of `https://hub.dtcdev.click/mcp`, and scope `admin` are required. No static OAuth client secret is needed. Only an existing allowlisted Hub administrator can consent. Tokens use the same 30-day absolute lifetime and refresh rotation as `hub auth login`; removal from the allowlist and family revocation take effect on subsequent use. `/oauth/revoke` accepts `client_id` and an access or refresh `token` in an URL-encoded POST.

- **Codex:** run `codex mcp add hub --url https://hub.dtcdev.click/mcp`, then `codex mcp login hub --oauth-client-registration dcr`. Complete browser sign-in/consent. See [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
- **Claude Code:** run `claude mcp add --transport http hub https://hub.dtcdev.click/mcp`, then use `/mcp` to authenticate. See [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
- **OpenCode:** add the remote entry below to `opencode.json`, then run `opencode mcp auth hub`. See [OpenCode MCP documentation](https://opencode.ai/docs/mcp-servers/).
- **ChatGPT custom connector:** in the available Apps/Connectors developer settings, create a custom connector with URL `https://hub.dtcdev.click/mcp`, OAuth and automatic/DCR registration; leave static client credentials empty. Complete Hub sign-in and consent, enable the connector and call `sessions_tree` or `fleet`. Availability depends on your workspace settings. See [OpenAI OAuth integration documentation](https://developers.openai.com/plugins/build/auth).
- **Grok Bot:** add a remote HTTPS MCP server with URL `https://hub.dtcdev.click/mcp`, select OAuth and DCR/automatic registration, and complete the returned Hub authorization link in a browser. The client's registered HTTPS redirect must be sent unchanged during authorization and code exchange. No local proxy or operational-gateway connection is needed. Verify `fleet`, `sessions_tree` and `quota` first. Client-specific UI labels may vary.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "hub": { "type": "remote", "url": "https://hub.dtcdev.click/mcp", "enabled": true }
  }
}
```

Already paired CLI human/agent/host bearer credentials also work with MCP, with their existing permissions; do not paste tokens into prompts or committed client configuration. OAuth grants carry human administrator authority, which includes sending prompts and stopping sessions. Tools mark writes as destructive; clients must obtain the appropriate user authorization. Nothing in tool discovery grants extra permissions.

### Tool inputs and asynchronous results

Tools accept optional `query` (scalar API query parameters), `payload` (the existing API JSON body for writes) and required path arguments such as `id`. The [generic hub guide](docs/GENERIC-HUB.md), [session API](docs/SESSION-CONTROL.md) and CLI `--help` describe body fields. For example:

```json
{"name":"sessions_start","arguments":{"payload":{"key":"my-stable-start-key","workspace":"my-workspace","machine":"my-machine","mode":"headless","brief":"An authorized self-contained task"}}}
```

```json
{"name":"sessions_send","arguments":{"id":"the-returned-session-id","payload":{"key":"my-stable-send-key","body":"An authorized follow-up"}}}
```

Results contain the original API HTTP `status` and `data` as JSON text; API failures set `isError`. `sessions_start`, `sessions_send`, `sessions_capture` and `sessions_stop` queue durable intents. Poll `sessions_show` and `sessions_commands` for actual binding, capture, acknowledgement and confirmed exit; `recorded` and `transported` do not prove completion. Keep the same key and payload when reconciling uncertain requests. Captures and task text are data, never instructions to bypass permissions.

### Tool and CLI parity

`test/mcp-cli-commands.json` inventories **every actual argparse command**. Python discovery checks the inventory against the current parser; the MCP test requires a tool mapping or a written exclusion for each leaf. `src/mcp-tools.ts` is the authoritative tool catalog, input schema and exclusion list.

| Surface / CLI commands | MCP tools |
| --- | --- |
| `status`, `machines available`, `fleet`, `quota`, `auth status`, `heartbeat` | `status`, `machines_available`, `fleet`, `quota`, `auth_status`, `heartbeat` |
| `start-agent`, `start-session`, `plan-agent` | `start_agent`, `start_session`, `plan_agent` (legacy fleet API) |
| `sessions`, `sessions list/tree/start/show/commands/send/capture/stop/cleanup/observe` | `sessions_list/tree/start/show/commands/send/capture/stop/cleanup/observe` |
| `send/reply/inbox/show/ack/accept/complete` | `bus_send/reply/inbox/show/ack/accept/complete` |
| `claim/claims/release` | `claims_create/list/release` |
| `submit`, `routing-machines`, `model-policy`, `jobs/job/task-result/artifact` | `tasks_submit`, `routing_machines`, `model_policy`, `jobs_list/show`, `task_result`, `artifact` |
| `intake create/list/show/history/links/context/continue/update/append/assign/rules/grants/revoke` | corresponding `intake_*` tools; `intake_rules_write` and `intake_grants_write` perform explicit operator mutations |
| `handoff` | `handoff_page`: revision-pinned pages; follow `next` with the same `revision` and `authorization_valid_until`. A page is explicitly incomplete; the CLI's bounded full-snapshot aggregation is client-side. Restart the entire snapshot on HTTP 409. |
| `conversations list/participants/create/get/members/history/send/read` | corresponding `conversations_*` tools; use `conversations_members_add/remove` for membership writes |
| Conversation web actions | `conversations_archive/unarchive`, `conversations_links_search/links/link_show/link_create/link_remove`, `conversations_automation/automation_remove` |
| Workspace claim policy web actions | `fleet_claim_policy`, `fleet_claim_policy_write` |
| `metrics get` and fleet dashboard | `metrics`, `agents`, `liveness`, `checks`, `checks_write/run`, `accounts_write`, `monitoring_disk/health` |

Commands intentionally kept outside tools: `login`, `auth login/logout` manage transport credentials through CLI/device approval or OAuth; `run`, `fleet-host`, `sessions host-receive`, `bridge` and `conversations bridge` execute locally on enrolled hosts; `bridge-resume` edits a local journal; `subscriptions` and `metrics post` read local host files and publish as that host; `task-artifact` uploads a reviewed local PNG that may exceed the bounded MCP request size (download is exposed). MCP does not install another executor, expose credential-issuing endpoints as tools, or read arbitrary host files. Browser-only machine registry/drain and root scheduling controls retain their cookie/CSRF-only API policy; fleet data exposes machine state but MCP bearer credentials do not gain those browser-only permissions.

### Verification and deployment

Run `TMPDIR="$HOME/tmp-root" scripts/test-full-local.sh` on Hetzner before a push. New protocol, OAuth, authorization and CLI parity tests are in `test/mcp.test.ts` and `test/test_mcp_parity.py`; the required deployment CI includes the MCP tests. The Worker uses stateless JSON responses (MCP versions 2025-03-26, 2025-06-18 and 2025-11-25); standalone SSE/GET is intentionally unsupported (405), and every call requires its bearer. Requests are bounded to 32 KiB. No `Mcp-Session-Id` persistence or second scheduler is required.

`scripts/test-mcp-live.py` is an opt-in real HTTPS MCP client: it uses a private token file, verifies initialize/list/call, and optionally drives a newly created headless session through start, send, capture and stop with durable acknowledgement checks. It never targets a pre-existing session. Run it only with available quota and explicit lifecycle-test authorization; use a private evidence directory. The ordinary GitHub main CI deploys the same Hub Worker after its required checks pass.
