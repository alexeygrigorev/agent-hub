# Agent Hub

See [SCOPE.md](SCOPE.md) for ownership and product boundaries.

Generic agent-fleet hub on Cloudflare: authenticated messaging and dashboards, machine subscriptions and shared account quotas, placement of headless agents, and real interactive sessions. See [the generic hub guide](docs/GENERIC-HUB.md) for configuration, CLI commands, execution boundaries and migration. The intended production URL is `https://hub.dtcdev.click`; deployment and physical host acceptance are separate from source availability.

Add an engine account once with `hub engines add --file <private-bundle.json>`; enrolled machines install its binary and isolated aplexer profile automatically. See [account provisioning and the Docker lifecycle test](docs/ENGINE-PROVISIONING.md).

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
  `GET /api/jobs/offers` and `POST /api/host/poll`.
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

## Operations

- Hub DB off-Cloudflare backup (daily export to Hetzner + Storagebox,
  staleness alert, restore drill): [docs/HUB-DB-BACKUP.md](docs/HUB-DB-BACKUP.md).
- Inbound webhooks delivering payloads into Hub sessions (authenticated
  routes, rate limits, size caps, durable queue): [docs/WEBHOOKS.md](docs/WEBHOOKS.md).
- Per-engine/per-account concurrency caps and live usage, changed live
  without a redeploy (`hub limits show`, `hub limits set <engine> <n>`):
  [docs/FLEET-LIMITS.md](docs/FLEET-LIMITS.md).

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
| `send/reply/inbox/show/ack/accept/complete/activity` | `bus_send/reply/inbox/show/ack/accept/complete`, `bus_activity` (read-only work-activity visibility; see [docs/WORK-ACTIVITY.md](docs/WORK-ACTIVITY.md)) |
| `workclaim-sync` | local-only native work-claim bridge one-shot (see [docs/WORK-CLAIM.md](docs/WORK-CLAIM.md)) |
| `claim/claims/release` | `claims_create/list/release` |
| `submit`, `routing-machines`, `model-policy`, `jobs/job/task-result/artifact` | `tasks_submit`, `routing_machines`, `model_policy`, `jobs_list/show`, `task_result`, `artifact` |
| `intake create/list/show/history/links/context/continue/update/append/assign/rules/grants/grantees/revoke` | corresponding `intake_*` tools; `intake_rules_write` and `intake_grants_write` perform explicit operator mutations |
| `handoff` | `handoff_page`: revision-pinned pages; follow `next` with the same `revision` and `authorization_valid_until`. A page is explicitly incomplete; the CLI's bounded full-snapshot aggregation is client-side. Restart the entire snapshot on HTTP 409. |
| `conversations list/participants/create/get/members/history/send/read` | corresponding `conversations_*` tools; use `conversations_members_add/remove` for membership writes |
| Conversation web actions | `conversations_archive/unarchive`, `conversations_links_search/links/link_show/link_create/link_remove`, `conversations_automation/automation_remove` |
| Workspace claim policy web actions | `fleet_claim_policy`, `fleet_claim_policy_write` |
| `metrics get` and fleet dashboard | `metrics`, `agents`, `liveness`, `checks`, `checks_write/run`, `accounts_write`, `monitoring_disk/health` |

Commands intentionally kept outside tools: `login`, `auth login/logout` manage transport credentials through CLI/device approval or OAuth; `run`, `fleet-host`, `sessions host-receive`, `bridge` and `conversations bridge` execute locally on enrolled hosts; `bridge-resume` edits a local journal; `subscriptions` and `metrics post` read local host files and publish as that host; `task-artifact` uploads a reviewed local PNG that may exceed the bounded MCP request size (download is exposed). MCP does not install another executor, expose credential-issuing endpoints as tools, or read arbitrary host files. Browser-only machine registry/drain and root scheduling controls retain their cookie/CSRF-only API policy; fleet data exposes machine state but MCP bearer credentials do not gain those browser-only permissions.

### Verification and deployment

Run `scripts/test-affected-local.sh` on Hetzner before a push (targeted; the full suite runs in deploy CI after the push, see `docs/PREPUSH-TARGETED-TESTS.md`; `scripts/test-full-local.sh` remains for broad infrastructure changes and final gates). New protocol, OAuth, authorization and CLI parity tests are in `test/mcp.test.ts` and `test/test_mcp_parity.py`; the required deployment CI includes the MCP tests. The Worker uses stateless JSON responses (MCP versions 2025-03-26, 2025-06-18 and 2025-11-25); standalone SSE/GET is intentionally unsupported (405), and every call requires its bearer. Requests are bounded to 32 KiB. No `Mcp-Session-Id` persistence or second scheduler is required.

`scripts/test-mcp-live.py` is an opt-in real HTTPS MCP client: it uses a private token file, verifies initialize/list/call, and optionally drives a newly created headless session through start, send, capture and stop with durable acknowledgement checks. It never targets a pre-existing session. Run it only with available quota and explicit lifecycle-test authorization; use a private evidence directory. The ordinary GitHub main CI deploys the same Hub Worker after its required checks pass.

For real lifecycle tests, pass an **exact host-configured workspace and canonical Hub engine**. A local aplexer alias is not automatically a Hub route. For example, Hetzner currently maps local `flash` to Antigravity; use `--engine antigravity --workspace /home/alexey/git/agent-hub` when that route has capacity. `capability_unavailable` can mean an unknown workspace label. A configured absolute workspace path can be used directly. `account_unknown` (including failed quota authentication), `account_capacity`, stale machine records and unqualified execution backends continue to hold launches. Refresh the plan and quota rather than increasing limits or controlling another session.

`hub sessions start` returning `409 no_eligible_route` with
`capability_unavailable, machine_unavailable, provider_disabled` and no
`--engine` means placement examined every fleet route and each failed a
different gate (seen 2026-10-22 for normal Hetzner coding workspaces such as
`/home/alexey/git/ai-shipping-labs`). Read the triple per route, not as one
cause: `provider_disabled` marks routes on engines the model policy disables
(Claude/Grok — preferred engines need no override); `machine_unavailable`
marks drained or stale-checkin machines (the pre-re-enrollment `hetzner`
record last checked in 2026-10-15 is one — drain or delete it once the live
`urgent-hetzner` record is confirmed); `capability_unavailable` on the live
routes means the host's advertised workspace list does not cover the request.
The host advertises its operator `workspaces` map plus `workspace_roots`
(this covers `/home/alexey/git`, so every normal repo under it is placeable)
and re-registers every fleet cycle, picking up operator settings edits without
a restart. If the running host release predates roots advertisement, roll it
forward first; the remaining engine with live quota then places without
`--engine` (currently Antigravity on Hetzner; the shared `shared-go`
opencode account is saturated by observed native sessions, so it honestly
reports `account_capacity`). `allow_paid` stays false; no secrets are involved.

To verify both clients against **one new session**, build the CLI and run the opt-in driver:

```bash
python3 scripts/build-hub-cli.py --out-dir public/cli
TMPDIR="$HOME/tmp-root" python3 scripts/test-session-clients-live.py \
  --cli public/cli/hub.pyz --token-file /private/oauth-token.json \
  --workspace /home/alexey/git/agent-hub --engine antigravity \
  --evidence /private/session-clients-proof.json --lifecycle
```

The driver uses the same human bearer, submits keyed operations through both interfaces, and checks they refer to the same session and commands. It requires a genuine bound native UUID, the correlated reply digest, a nonempty capture from that UUID, and acknowledged stop plus stopped state through both clients. Credentials and capture output remain in private evidence. Native ZAI control additionally requires the maintained Launcher version that injects first-action validation and declares `--engine zcodex`; its operator-configured route must use the real ZAI account/model and fresh admission evidence. Task-unit identifiers never qualify as native session bindings.

### Aplexer Python client and experimental overrides

The host package (0.2+) requires Python 3.11+ and uses the published
[`aplexer-client`](https://pypi.org/project/aplexer-client/) package, imported as
`from aplexer import Client`. The PyPI package named `aplexer` distributes the CLI
binary separately. Install the production dependency versions with uv:

```bash
uv sync --frozen
uv run --frozen hub --help
```

Use uv's temporary dependency overlay for a local experimental client, without
editing `pyproject.toml` or `uv.lock`:

```bash
uv run --frozen --with ~/git/aplexer/python hub --help
# Editable source (native Rust changes still require rebuilding the extension):
uv run --frozen --with-editable ~/git/aplexer/python hub --help
# Or a prebuilt wheel matching this machine's Python/platform:
uv run --frozen --with /absolute/path/aplexer_client-0.1.19-cp311-abi3-manylinux_2_28_x86_64.whl hub --help
```

The session-host supervisor propagates its own Python interpreter to its native
host child, so start the supervisor using the uv environment too.

The same prefix works for host modules, e.g. `uv run --frozen --with
~/git/aplexer/python python -m agent_hub_host.session_host ...`. Source installs
require Rust and maturin's build prerequisites; wheels avoid a local Rust build.
Dropping `--with` restores the locked published client on the next invocation.
`uv run --frozen --with ~/git/aplexer/python python -c 'import aplexer;
print(aplexer.__file__)'` shows which client is active.

Core session listing, status, bounded history capture, stop and unwrapped Fleet
starts use the Client API. Hub's identity/generation checks and quota launcher
admission still apply. Mailbox operations (`message send/log/deliver`, `whoami`),
the mailbox bridge's paired status/rendered screen readiness check and starts wrapped in `native_start_argv` (for example
`systemd-run`) retain the native CLI because the Client does not expose these
protocols, CLI readiness enrichment or process wrappers. The bounded streaming
health probe also retains its CLI timeout/output enforcement. An explicit `aplexer_bin` path also retains that
binary transport, independently of the Python client override. There is no CLI
retry after a Client error; callers must reconcile uncertain effects.

The portable CLI zipapp keeps its native CLI transport when the Python client
is absent, so clean-machine installers and host bootstrap keep working. To use
the Client API from the zipapp, run it under `uv run --no-project --with
aplexer-client==0.1.19 python /path/to/hub.pyz ...`, or use the uv project install
above. This release changes host/CLI code only; it requires updating the host
package/environment, with no Worker deployment.

### Managed Engine Git Identity Configuration

Hub-managed engines run with an isolated `HOME` per engine bundle. To ensure `git commit` succeeds across all managed engines without relying on system-wide git config, configure git identity in host configuration (`subscriptions.json` or `host.json`):

```json
{
  "version": 1,
  "git": {
    "user.name": "Alexey Grigorev",
    "user.email": "alexey.s.grigoriev@gmail.com",
    "push.default": "simple"
  }
}
```

During host and engine bootstrap, Hub writes `.gitconfig` to each managed engine's `HOME` and re-applies on re-bootstrap or home recreation. Launched sessions additionally receive `GIT_AUTHOR_*` and `GIT_COMMITTER_*` environment variables as a fallback.
