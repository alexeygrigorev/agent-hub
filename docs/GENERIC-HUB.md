# Agent Hub

Agent Hub is a Cloudflare control plane for personal agent fleets. One Worker and one SQLite-backed Durable Object hold the registry, authenticated bus, schedules, launch intents and dashboard. Model processes run on enrolled machines through their existing native tools.

The generic fleet endpoints and dashboard are a source increment. Installing a host configuration and verifying its admission adapter are separate from deploying the Worker. Unknown execution support produces a hold, not a simulated launch.

Host releases use the operator's canonical `~/.config/agent-hub/subscriptions.json` and managed-engine directory. A release override must preserve those paths, the existing credential configuration and journal; task recovery files must not replace the subscription source. Absolute workspace starts require a configured `workspace_roots` entry. Add the narrow existing worktrees root needed by the project; labels and absolute paths use the same admission and realpath confinement.

For an existing native Claude or ZAI account, configure `admission_argv` as the release Python executable followed by `-m agent_hub_host.native_admission --service claude --account shared-claude --engine claude` (ZAI: `--service zai --account shared-zai --engine zcodex`). These explicit, unprofiled routes use native authentication through fresh `quse`; no credentials are imported. Configure the maintained `launcher_provider` and model for headless execution as well. The native ZAI `zcodex` route resolves the policy's `glm-flash` model just like the managed `codex`/`zcodex` profile. A successful admission probe clears only an earlier `admission_unconfigured` hold; it cannot clear an authentication-expiry hold. Grok remains disabled for execution by policy, and read-only placement diagnostics do not start a Grok worker.

`hub sessions stop` cancels a held, queued or unclaimed offered intent when the ledger proves no native start was authorized. Its receipt says `cancelled` with `effect=no_native_start`, including with `--wait`. Stops of bound sessions still require confirmed native exit. Neither cancellation nor a missing binding is exit proof for a claimed launch.

Claude can report a known unused five-hour window as 100% remaining with an explicit null reset. Admission preserves that inactive-window observation and still requires fresh quota plus a valid timed sibling window. It never synthesizes a reset or accepts an unknown reset for a partly used window or another provider.

## Start work

```sh
# Choose a machine and engine; headless is the default.
hub start-agent --workspace project --prompt-file goal.txt --summary "Implement the parser" --key parser-001

# Pin a headless agent to a machine. It will not fall back to another machine.
hub start-agent --machine server --workspace project --prompt-file goal.txt --summary "Run the checks" --key checks-001

# Create a real interactive terminal at an empty prompt.
hub start-session --machine desktop --workspace project --summary "Interactive review" --key review-001

# Preview placement without reserving capacity.
hub plan-agent --workspace project --prompt-file goal.txt
hub fleet
```

Supply `--parent HUB_LAUNCH_ID` when one hub agent spawns another. The parent must be active and owned by the same authenticated requester; attaching an unrelated or finished parent is refused. Every row records the authenticated requester, requester kind, parent ID (null for a root request), time, summary, machine, engine, profile, account label and mode. Requester identity comes from authentication; the caller cannot override it. Native session reports include workspace, tag, engine, profile and state, without copying commands, environments or transcripts. Legacy scheduler job credentials do not grant generic spawn authority.

The dashboard at `/` requires an authenticated administrator. It shows machines and their subscriptions, shared account capacity, hub agents and parent relationships, registered agents and reported aplexer sessions. It refreshes every 30 seconds. A successful queue response means offered work, not started work.

## Account registry

Upload a JSON registry through the authenticated dashboard. Account labels identify shared provider capacity; they are not authentication tokens. A separate account uses a different label even when both accounts belong to the same service on one machine.

```json
{
  "accounts": [
    {"label":"shared-codex","provider":"codex","max_concurrency":1,"reserve_percent":15,"enabled":true},
    {"label":"desktop-codex","provider":"codex","max_concurrency":1,"reserve_percent":15,"enabled":true}
  ],
  "routes": [
    {"machine_id":"server","engine":"codex","account":"shared-codex","profile":null},
    {"machine_id":"desktop","engine":"codex","account":"desktop-codex","profile":"personal"}
  ]
}
```

Use enrolled machine IDs. Registry replacement is refused while active launches hold capacity and invalidates retained subscription reports. Codex reserves at least 15%; Grok at least 5%. Placement combines fresh machine admission, resource floors, capabilities, measured load, pending work and conservative account headroom. A shared account's slots are counted across all machines. The selected machine must supply fresh account evidence; a fresh report on another machine does not qualify it. Default CLI quota cannot qualify a named profile. Expired, missing or contradictory observations fail closed.

`available_slots` is the remaining account pool. `placeable_slots` counts that pool only when at least one fresh, undrained machine advertises a registered engine route. `placement_reasons` explains missing routes (`no_routes`), stale/drained machines (`machine_unavailable`), unadvertised engines (`engine_unavailable`), and a full pool (`account_capacity`). Reasons for unavailable sibling routes can appear alongside usable capacity. These fields describe account capacity and route liveness; the request's plan still checks quota, admission, resources, workspace, mode and tier. An account with free slots and no routes cannot launch until an authorized, configured host serves it; increasing concurrency does not repair that condition.

Interactive goal placement requires the exact account, engine and profile in the host’s `fleet_goal_routes` advertisement. The host publishes only Codex-family routes whose effective subscription carries fresh-admission configuration (`admission_argv`, supplied by managed state for installed managed accounts) plus a trusted startup command (explicit `startup_argv` or the managed `execute` entrypoint for a managed label). A native ZAI route without `startup_argv` and without a managed label stays ineligible for goals. Pre-claim refusals retain a bounded reason, fixed message and stage in the fleet journal and launch diagnostic, which `sessions start --wait` and `sessions show` display. The pre-start Hub preamble for empty interactive sessions is queued as a same-workspace send (no `--workspace` flag); a `--workspace` send to the not-yet-existing native tag is rejected as a cross-workspace queued target before native binding exists.

Capacity-only starts return a waitable `202 queued` intent. Host polling and capacity releases re-plan the same intent automatically. Routes outside the requested tier or without requested goal support remain visible in `rejected`, but do not turn eligible capacity waits into a `409`. Those excluded routes alone never queue; capability, quota, admission and resource failures still reject. Queue promotion preserves tier and goal constraints, and same-key retries observe the original intent.

## Machine subscriptions

Each machine owns `~/.config/agent-hub/subscriptions.json`, or `%APPDATA%\agent-hub\subscriptions.json` on Windows. It contains metadata and local command references. Keep provider credentials in the provider's native store.

```json
{
  "version":1,
  "workspaces":{"project":"/absolute/path/to/project"},
  "git":{
    "user.name":"Alexey Grigorev",
    "user.email":"alexey.s.grigoriev@gmail.com",
    "push.default":"simple"
  },
  "subscriptions":[
    {"service":"codex","account_label":"shared-codex","plan":"pro","engine":"codex","profile":null,"admission_argv":["/absolute/path/to/maintained-admission-adapter"]},
    {"service":"codex","account_label":"second-codex","plan":null,"engine":"codex","profile":"secondary","quota_argv":["/absolute/path/to/secondary-quota-adapter"],"admission_argv":["/absolute/path/to/secondary-admission-adapter"]}
  ]
}
```

Git identity (`user.name`, `user.email`, optional signing keys or push defaults) is configured under `"git"` in `subscriptions.json` (or `host.json`). During host/engine bootstrap, Hub writes `.gitconfig` to each managed engine's `HOME` and re-applies on re-bootstrap or home recreation. Launched sessions also receive `GIT_AUTHOR_*` and `GIT_COMMITTER_*` environment variables as a fallback.

Labels and configured plans are explicit declarations. The default Claude tier can be detected through `claude auth status --json`. The default Codex tier can be detected through local app-server `account/read` with token refresh disabled. Neither probe copies email addresses, account IDs or tokens into reports. Other plans remain unknown unless their status adapter reports a recognized tier or the local configuration supplies one.

Quota collection uses the existing `quse SERVICE --json` for an unambiguous default account. Multiple accounts or a non-default profile need a separate local `quota_argv`; default quota is never copied across them. The quota adapter returns the quse shape for that account. Collection whitelists booleans, remaining percentages and reset times and drops raw error bodies and credentials. Used allowance percentage is distinct from token usage or cost; unknown token usage stays unknown.

```sh
hub subscriptions --config /path/to/subscriptions.json
hub subscriptions --config /path/to/subscriptions.json --report
hub fleet-host --config /path/to/subscriptions.json --cycles 10 --interval 60
```

`fleet-host` is opt-in and bounded. It registers capabilities, reports heartbeats, subscriptions and native sessions, and polls its own offers. It installs no service or timer. Preserve existing supervisors until the replacement has completed physical host tests.

## Host execution contract

An admission adapter receives JSON on stdin with mode, engine, profile, account and workspace labels. It must run the maintained provider/resource checks and return `launch_allowed`, `profile_supported`, `account_label`, and a timezone-aware `observed_at`. The report must be no older than 30 seconds. No provider invocation or shell command comes from a hub request.

Interactive work uses native `aplexer start`, a configured engine/profile, a 1500 MiB memory cap and a process limit. Its receipt must match the workspace and generated tag. Prompt injection into an existing terminal is not part of fleet launch.

Headless work uses the maintained Quota Launcher. Configure a local `launcher` object with `argv`, `root` and `config_dir`, and per-subscription `launcher_provider` and `launcher_model`. The launcher's plan must prove the selected account label and profile, as well as provider and model. Older launcher plans without account attribution are held. No raw provider fallback is used. This is an outstanding integration requirement for launcher versions that lack those fields.

Claim and effect ambiguity retain the server reservation. A starting or uncertain launch is never launched again automatically. Completion requires authenticated host reconciliation and confirmed native exit. A synchronous launcher exit is recorded as exited/failed, not running or accepted work. A live headless receipt must verify first action. Automatic completion reconciliation remains a follow-up before unattended adoption.

## Heartbeats, liveness and alerts

The hub judges liveness from timestamps it already receives. Nothing on a host has to be screen-scraped.

| Subject | Source | States |
|---|---|---|
| Machine | `/api/host/poll` heartbeats (the host agent or `hub fleet-host`) | `online` (≤ 3 min), `stale` (≤ 15 min), `offline`, `never`, `drained` |
| aplexer session | `PUT /api/fleet/sessions`, including native transcript/provider progress | `working`, `idle`, `stalled` (no observed agent progress for 30 min by default, including headless sessions with live heartbeats), `exited`, `unknown` (its machine is silent) |
| Hub launch | `fleet_launches` | `stuck` (offered/starting > 10 min), `uncertain`, `missing` (interactive session vanished), `working` |
| Agent outside aplexer | `hub heartbeat --status working --summary "..."` (agent credential with `health:report`) | the reported status, or `stale` after 15 min of silence |

Thresholds are Worker vars: `HUB_MACHINE_STALE_S`, `HUB_MACHINE_OFFLINE_S`, `HUB_AGENT_STALL_S`, `HUB_HEARTBEAT_STALE_S`, `HUB_LAUNCH_STUCK_S`.

One alert opens per subject when it enters `stale`, `offline`, `stalled`, `stuck`, `uncertain` or `missing`, updates in place, and resolves on recovery. New alerts are delivered as hub inbox messages from `hub` to every actor in `HUB_ALERT_TO` (comma-separated) and, if set, POSTed as JSON to the `HUB_ALERT_WEBHOOK` secret. Failed or untargeted deliveries retry every 15 minutes while the alert is open. The dashboard shows open alerts and their delivery status. `GET /api/liveness` returns the same data.

Session progress comes from a bound transcript's write time, an unambiguous Codex rollout matching the native launch and cwd, OpenCode message events for the exact native launch, or provider state-event timestamps. Terminal chatter, polling, heartbeats, workload CPU and shared workspace edits do not refresh it. Missing or ambiguous sources never fabricate progress; the native creation time is the initial baseline. The host keeps transcript paths and content local and uploads only the bounded progress timestamp and existing liveness fields. An upgrade discards timestamps from the former CPU/file/terminal sampler while preserving cleanup deduplication. The server uses this progress timestamp even when a legacy terminal timestamp is newer; older hosts without a progress report retain their legacy fallback until upgraded. Stalls preserve the running lifecycle and capacity, report `stall_reason`, and require the existing sustained recovery interval before clearing.

## Scheduled checks

Scheduled checks are the generic form of the competition's routine agent: periodic checks that take an action. A five-minute cron runs every enabled check whose interval has elapsed, at most once per interval slot. A check is configuration, never a shell command.

| Kind | Fails when |
|---|---|
| `liveness` | any subject is in a problem state (opens per-subject alerts; seeded by default every 5 min) |
| `quota` | an enabled account is blocked or below `params.min_percent` (default 20) |
| `http` | an `https://` URL does not return `params.expect_status` (default 200) |
| `inbox_backlog` | `params.actor` has unacknowledged messages older than `params.max_age_s` |
| `conversation_backlog` | retained unread rows older than `params.max_age_s` for a canonical `params.actor`, within explicit owner grants; optional `params.conversation_id` narrows the scan (see [conversation follow-ups](CONVERSATION-PINGS.md)) |
| `routine` | never; its action runs every interval |

Actions: `{"type": "alert"}` (default), `{"type": "message", "to": "<actor>", "body": "..."}`, or `{"type": "launch", "workspace": "...", "prompt": "...", "engine"?: "...", "machine"?: "..."}`, which starts a headless agent through normal placement with requester `schedule:<name>`. `{check}` and `{detail}` are substituted in bodies and prompts. Messages and launches run on failure, or always with `"on": "always"` (the default for `routine`). Names are lowercase, at most 40 characters; intervals are 5 minutes to 7 days.

```json
{"checks": [
  {"name": "liveness", "kind": "liveness", "interval_s": 300},
  {"name": "site", "kind": "http", "interval_s": 600, "params": {"url": "https://example.com/health"}},
  {"name": "grok-inbox", "kind": "inbox_backlog", "interval_s": 900, "params": {"actor": "grok-bot", "max_age_s": 3600}},
  {"name": "daily-summary", "kind": "routine", "interval_s": 86400,
   "action": {"type": "launch", "workspace": "notes", "prompt": "Summarise yesterday's agent activity", "engine": "claude"}}
]}
```

Upload it from the dashboard, or `PUT /api/checks`. `POST /api/checks/<name>/run` runs one now. A forced rerun in the same completed slot returns its stored result. Conversation message actions additionally require `conversation_id` and an expiring `grant_id` from the conversation's current human owner. Their `to` is the recipient; the reserved author is `system:conversation-checks`. Durable intent and Hub storage receipts preserve retries without acknowledging the recipient. The exact current slot gates new writes; existing retention reclaims settled receipts, reconciles invalid or removed slots, and retires obsolete grants within the shared deletion budget. Retired keys fail closed after pruning; new conversation intervals use opaque generations independent of wall-clock time. A Hub storage receipt does not confirm native inbox delivery. No conversation check is enabled by default. See [CONVERSATION-PINGS.md](CONVERSATION-PINGS.md) for delegation, bounds and recovery.

## Signing in

The dashboard and every `/api/*` route require authentication. Humans sign in with DTC OIDC (an address in `HUB_ADMINS`) on the origin registered as the Cognito callback, or anywhere with the admin token: set the Worker secret `HUB_ADMIN_TOKEN_SHA256` to the SHA-256 hex of a high-entropy token kept on your machine, then open `/auth/token`. Rotating the secret ends existing token sessions. Hosts and agents pair through the device flow and an admin approves them in the browser.

## Messaging adoption

Use `hub send`, `hub inbox`, `hub reply`, `hub ack`, `hub accept`, and `hub complete` for the cross-host bus. Keep receipt, read ACK, task acceptance and completion separate. Deduplicate by durable ID and idempotency key. A host bridge maps registered native aplexer recipients without forging their identity. `hub bridge --once` runs a finite bridge cycle; verify host enrollment and the bridge's native binding first.

Within a host, keep using `a message` and advisory `a work` scopes. Avoid SSH terminal typing for coordination. An inbox message does not wake a model by itself. Preserve busy prompts and human drafts; automatic wake-up and unattended cross-host execution require separate acceptance.

## Migration and deployment

The standalone repository is `alexeygrigorev/agent-hub`. Historical dashboard, Agent Bus, SSH coordination and role-failover sources remain available. Quota Launcher and aplexer remain execution dependencies during migration; deprecation of their old orchestration entry points does not authorize shutting them down.

The existing DTC OIDC variables in `wrangler.jsonc` identify a deployment, not generic product defaults. Keep provider accounts, machine IDs, workspace paths and role mappings in local/admin configuration. Preserve Worker name `agent-hub`, DO class `Hub` and instance name `hub` when moving the domain. Renaming them can create a different data store.

Deploy to `https://hub.dtcdev.click` after the DNS zone, Cloudflare access and OIDC callback are configured. Keep `assets.run_worker_first=true` so static assets cannot bypass authentication. See [DEPLOY.md](DEPLOY.md) for the exact domain cutover and rollback steps. Do not change the contest demo's deployment or storage.

### Intake context inside launched sessions

A session linked with `--intake-task-id` receives a separate, refreshable agent
credential before its engine starts, in both interactive and headless modes.
The host requests it from the Hub after claiming the launch; it never copies a
human or host credential into the session. The private profile contains the Hub
origin and token pair in `.local/hub-worker-auth/<launch-id>.json` (owner only).
The CLI discovers it from `APLEXER_TAG` (`hub-<id>` or `task-hub-<id>`) and
`APLEXER_WORKSPACE`, including after the agent changes directory or a managed
engine replaces HOME/XDG. The unprofiled Codex task-unit backend transports the non-secret
`HUB_WORKER_CONFIG_FILE` selector through the maintained launcher EnvironmentFile
because it has no native aplexer identity. Tokens never appear in prompts or command arguments.
A missing profile fails closed without falling back to an operator login.

Inside the session, `hub intake show <linked-id>` reads the task and its context.
`hub intake append` permits note, report and blocker events on that task.
Unrelated tasks, intake enumeration, decisions and closing remain forbidden.
The existing scoped child-session rules and seven-day grant deadline also apply.
An unlinked session has no intake grant; link the intake when requesting its start.
