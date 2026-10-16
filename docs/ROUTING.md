# Capability tasks and model tiers

Task routing extends the existing Agent Hub and its account registry, fleet launches, host poller and quota launcher. It uses the same Worker and Durable Object. `agent-coordination` is the legacy transport repository (deprecated by commit `762c349`).

## Client commands

Install the existing stdlib Python package in a local virtual environment (`python3 -m venv .local/venv`, then `.local/venv/bin/pip install .`). Use the same commands on the Grok Bot Linux box and Hetzner. `HUB_URL` selects the deployment; `HUB_TOKEN` carries a scoped client credential. Load the credential from a private file in your process environment; never paste it into command arguments, prompts, chat or reports.

```sh
hub submit --needs browser:orin,phone-sms --prompt-file x.md --key invoice-check-001
hub submit --tier daily --difficulty normal --prompt-file x.md --workspace tasks
hub submit --tier hard --runner gemini --prompt-file x.md
hub submit --model luna --prompt-file concrete.md
hub jobs
hub job JOB_UUID
hub routing-machines
hub model-policy
```

The entire prompt is the exact approved action by default. To distinguish context from authorization, supply `--approved-action-file action.md`. The agent is instructed to execute only that action and stop on captcha/passkey. Do not submit credentials or codes as task context. Hosts retain prompts privately for execution; list/get responses omit them.

`--machine` and `--runner` are preferences, with safe fallback to another eligible route in the same tier. `--model` is an exact configured alias or model ID. Without a tier, an explicit model implies its tier; otherwise the default is daily. An unavailable tier/model remains queued; it never silently upgrades or downgrades.

`--workspace` is a label mapped to a real local directory on each eligible host; the default is `tasks`. No client-supplied shell commands, runner flags, local paths or account credentials become execution configuration.

Use a stable `--key` when retrying: the same client, key and request return the original job. Changing the request with that key returns HTTP 409. Store the key before making an unreliable network request. List/get show the most recent 100 client-owned jobs and do not start agents.

## Batches

`hub submit --needs browser:orin --batch-file batch.json --key batch-001` accepts one JSON list:

```json
[
  {"prompt":"Read the first record.","approved_action":"Read record A and report its state."},
  {"prompt":"Read the second record.","approved_action":"Read record B and report its state."}
]
```

A batch has one shared capability set, workspace, tier and model preference. All actions run in one agent. Split batches that need different capabilities or models. Limits: 16 tasks, 12,000 serialized task characters, 32 KiB HTTP body.

## API

All routes require authentication. A client token grants `routing:submit` only; it cannot use generic fleet spawning, read other clients' tasks, report results, administer machines or issue credentials. Use `Authorization: Bearer <client token>` over HTTPS. Responses use `Cache-Control: private, no-store`.

| Method/path | Purpose |
|---|---|
| `POST /api/fleet/tasks` | Submit one task or a batch; returns 201 and a job ID even if no machine is available |
| `GET /api/fleet/tasks` | List the caller's latest 100 jobs (admins can list all) |
| `GET /api/fleet/tasks/:id` | Status, selected machine/runner/model, report and sanitized artifacts |
| `GET /api/fleet/routing/machines` | Machine capabilities, advertised model routes, concurrency, resources and freshness |
| `GET /api/fleet/routing/policy` | Tier table, aliases and current rules |
| `PUT /api/fleet/routing/capabilities` | Host credential only: advertise this host's capabilities, exact runner/model/effort/account routes and max agents |
| `POST /api/fleet/tasks/:id/artifacts` | Assigned host only: upload a sanitized PNG (`name`, `media_type`, `data_base64`, `sanitized: true`) |
| `GET /api/fleet/tasks/:id/artifacts/:name` | Job owner/admin: retrieve PNG data as base64 JSON |
| `POST /api/fleet/tasks/:id/result` | Assigned host credential only: progress/final result |
| `POST /api/fleet/routing/clients/:name` | Admin only: issue a 30-day per-client credential; token returned once |
| `DELETE /api/fleet/routing/clients/:name` | Admin only: revoke all credentials for that client name |

Single task body:

```json
{
  "idempotency_key":"check-001",
  "workspace":"tasks",
  "prompt":"Read the account settings page and report whether export is enabled.",
  "approved_action":"Read settings only. Stop if login requires a captcha or passkey.",
  "needs":["browser:orin"],
  "tier":"daily",
  "preferred_machine":"win35"
}
```

For a batch replace `prompt` and `approved_action` with `tasks` (the array above). Optional fields: `preferred_runner`, `preferred_machine`, `tier`, `model`, `difficulty` (`hard`, `normal`, `concrete`). Unknown fields are rejected.

Statuses: `queued`, `running`, `waiting-on-human`, `stuck`, `done`, `failed`. A placement/launch offer still counts as queued until there is a verified running receipt. Uncertain effects and starts without updates for 30 minutes appear stuck and retain capacity. Captcha/passkey is a human wait, never an automatic retry. `done`/`failed` require confirmed process exit. Completion webhooks are not enabled; poll the API or CLI.

Host result body:

```json
{"status":"done","report":"The setting is enabled.","artifacts":[{"name":"check-log","text":"Read-only check completed."}],"confirmed_exit":true}
```

Or use `hub task-result JOB_UUID --status waiting-on-human --report-file report.txt`. Only the assigned host credential can report. Final results are immutable. Text artifacts are limited to eight entries of 4,000 characters each; reports to 16,000 characters. URLs, bearer credentials, common secret formats, named secret values and 4–8 digit codes are redacted before persistence/return. Redaction is a second barrier: producers must omit secrets in the first place. Raw screenshots stay in private host storage until reviewed and sanitized. The assigned host can then explicitly upload a PNG with `hub task-artifact JOB_UUID --name screenshot --file sanitized.png --sanitized`. The flag attests that both visible pixels and metadata contain no secrets; the hub cannot automatically redact image content. Limit: four PNGs per job, 192 KiB each. Clients retrieve them with `hub artifact JOB_UUID screenshot --output screenshot.png`; the file is created privately without overwriting an existing path. Job list/get returns image metadata, never base64 image contents.

## Getting a client token

An administrator signs in with the existing hub admin login, then calls `POST /api/fleet/routing/clients/grok-bot` (or a different name per client). Cookie-authenticated POST/DELETE requests require `X-CSRF-Token` from `GET /auth/me`. The response contains `token`, `expires_at`, `client`. Save the token directly to a mode-0600 file; never log or print the response. The helper `scripts/routing-client-token.py` performs this exchange using the admin-token file and writes a new private client-token file without displaying secrets:

```sh
python3 scripts/routing-client-token.py --url https://hub.dtcdev.click \
  --admin-token-file ~/.config/agent-hub/admin-token \
  --client grok-bot --output ~/.config/agent-hub/grok-bot-token
```

The output file must not already exist. Transfer it through the existing approved private credential channel. Revoke through the DELETE endpoint if a client machine is lost. New client tokens expire after 30 days; renew by issuing a new credential and replacing the private client file. This does not grant provider credentials or create provider accounts.

## Host setup

Preserve the existing machine IDs and per-machine account mappings. Expected physical identities: Win35 `DESKTOP-FP4OP26`, Hetzner `RMTHZ`, laptop `LAPTOP-PHA6TCNC`. These names are not enrollment or proof of capabilities. Hosts authenticate through their own existing paired host credentials; never invent online machines or browser/phone support.

Extend each host's private `subscriptions.json` with `capabilities` and `max_agents`:

```json
{
  "version":1,
  "capabilities":["browser:orin","browser:main","phone-sms"],
  "max_agents":1,
  "workspaces":{"tasks":"/absolute/path/to/approved/task-workspace"},
  "subscriptions":[
    {"service":"zai","account_label":"shared-zai","engine":"zcodex","profile":null,
     "launcher_provider":"zai","launcher_model":"glm-5.3-flash","launcher_effort":"high",
     "quota_argv":["/absolute/path/to/account-quota-adapter"],
     "admission_argv":["/absolute/path/to/verified-admission-adapter"]}
  ],
  "launcher":{"argv":["python3","-m","launcher"],"root":"/absolute/path/to/agent-quota-launcher","config_dir":"/private/launcher-state"}
}
```

Advertise only capabilities physically verified on that host. Hetzner normally advertises `[]`; browser profiles and phone/ADB belong on hosts where they actually work. CPU, available memory, disk, freshness and load come from the existing machine/heartbeat registry. No Windows inbound port is needed: hosts poll outbound.

`hub fleet-host --config PATH --cycles 10 --interval 60` registers, heartbeats and publishes exact supported runner/model/effort/account routes before asking for offers. Capability advertisements expire after 3 minutes. Machine admission and quota evidence remain mandatory; shared account reservations span machines. Both task routing and the fleet transaction enforce the machine concurrency limit. Hetzner is preferred among eligible machines, except that explicit machine preference wins. Required browser/phone capabilities filter candidates before preference.

The admission adapter must prove `model_supported: true`, `model` and `effort` matching the selected model, in addition to the existing fresh account/resource/profile gate. The configured maintained launcher plan must identify the same account/profile/provider/model; missing proof holds the task. Existing older host configs continue legacy fleet polling but do not advertise routed-task execution.

The headless agent writes a bounded structured final report to its private `.local/hub-results/LAUNCH_UUID.json`. The host forwards the report only after a genuine exit receipt. Missing/malformed reports fail the task. Live/uncertain launcher receipts retain capacity; a host can reconcile through `task-result` after verifying native exit. This avoids replaying potentially completed side effects after disconnects.

## Model policy

The checked-in table and rules live in `config/model-routing.json`; the HTTP policy endpoint shows the deployed version. Change configuration and redeploy to change policy. The hub never installs or silently substitutes models.

| Tier | Models | Aliases |
|---|---|---|
| `deep` | Codex Astra, Gemini Argon, Claude Opus | `smart`, `hard`, `hard-reasoning` |
| `daily` | GPT Sol, Claude Sonnet, Z.ai **glm-5.3-flash**, OpenCode Muse Spark 1.3, OpenCode deepseek-v4.1-flash; Grok as eligible fallback | `everyday`, `good`, `volume` |
| `quick` | Claude Haiku, GPT-6 Luna at **max** effort | `concrete`, `no-thinking` |

Current standing rules: Z.ai and OpenCode preferred for volume; Grok allowed by hub policy; no new Claude sessions; Codex only if explicitly requested by runner or model. Asking for a tier alone does not authorize Codex. Quick tasks therefore remain queued until an allowed exact route exists or Codex is explicitly requested. No full `glm-5.3` route exists; OpenCode has only Muse and DeepSeek entries. GLM uses ZCode. Exact IDs are configurable and must match actual host/launcher support; a table entry alone does not prove account access.

The ranking formula is adapted from `agent-quota-launcher/launcher/ranking.py`: after hard freshness/quota/resource gates, score uses bottleneck remaining quota, health/load, task-tier fit, bounded reset urgency and provider preference. Both 5-hour and weekly windows participate; the tightest remaining window wins. Reset urgency is `remaining * clamp((24-hours_to_reset)/24,0,1)`, with a multiplier in `[1,3]`. Unknown evidence never creates capacity. Missing reset times add no urgency. Codex reserves 15%, Grok 5%. Z.ai/OpenCode get a configurable 1.2 preference. Placement uses a deterministic highest score rather than random selection so retries and explanations stay stable.

Task difficulty maps to tiers in config. Failover is within a tier and only before an execution is claimed; starting/uncertain work is never duplicated. The cost rule uses existing subscriptions only, with no credit purchases, metered paid fallback or claimed dollar estimates. Actual launcher admission remains authoritative: if an installed launcher has an older freeze or unsupported provider/model, the hub holds rather than bypassing it.
