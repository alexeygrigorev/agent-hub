# Hub driver

The driver is an interactive orchestration session with a standard standing
order and a Hub schedule every 15 minutes. An operator manages it through the
CLI or the equivalent `driver_start`, `driver_status`, `driver_stop` and
`driver_restart` MCP tools.

```sh
hub driver start --engine codex --workspace agent-hub --key driver-first
hub driver status
hub driver restart --key driver-restart
hub driver stop --key driver-stop
```

Omit `--engine` to use placement policy. Explicit choices are `claude`, `codex`,
`antigravity`, `zai` and `opencode`; the existing host capability, account,
quota and resource gates still apply. `--host` pins a machine ID. Unsupported
interactive routes remain held or refused with the canonical session reason.

Start normally reuses the current driver, including a queued, held or uncertain
start. `start --force` replaces it through the same stop fence as restart; it
does not create a parallel orchestrator. Restart inherits the previous engine,
workspace and host unless explicitly overridden.

Keep the same key and arguments when retrying an uncertain operation. Keys bind
to the original generation: a delayed stop retry cannot stop its replacement.
The CLI waits up to 120 seconds for a pending stop, configurable with
`--wait 0..600`. A timeout leaves the intent pending. Inspect status and retry
with the same key; do not infer termination from a queued stop command.

## Delivery and lifecycle

The session starts at an empty interactive prompt. Hub queues the standing
order through its existing durable prompt-delivery service, which waits for
native binding and records delivery receipts. Status exposes this as
`startup_delivery`, alongside the canonical session and schedule with recent
delivery receipts. A created driver row, queued command or timer is not proof
that an agent received or acted on its instructions.

The schedule uses the existing session prompt scheduler (`hub schedules`,
`/api/session-schedules`) with UTC cron `*/15 * * * *`. It sends the ping into
the driver's interactive session through the shared durable delivery queue; it
does not launch a new session per tick. Stop pauses that schedule without
deleting its configuration or history and cancels unsent startup delivery.
Commands already dispatched keep their actual receipt history. Replacement
waits for confirmed session termination or confirmed cancellation before
creating a new generation. An unknown session or uncertain start retains the
singleton reservation.

The driver watches authorized intakes and sessions, advances existing work,
and files repairs with acceptance criteria and linked sessions. It appends
DONE notes with proof, never closes or deletes intakes, and never approves
production gates. `NEEDS_ALEXEY` is reserved for production approvals, secrets,
deletions and 2FA.

## Driver access

The host provisions a separate, private per-launch driver credential. It never
copies a human or host token. Its authority covers global intake/session
observation, repair intakes and the driver's own child sessions. It cannot
control another owner's sessions, close/archive intakes, make scope decisions,
manage credentials or approve production actions.

The access token refreshes within a hard seven-day driver lifetime. Status
reports `access.issued` and `access.expires_at`; an operator must restart the
driver before that deadline to continue orchestration. Stopping or superseding
the driver invalidates its role immediately. Timer delivery does not extend
the credential lifetime.

## Orchestrator migration

Start a replacement after the Hub driver API is deployed and an acknowledged
handoff is ready:

```sh
hub driver start --engine codex --workspace agent-hub
hub driver status
```

The `start` command is the complete launch operation: it creates one
interactive session, queues the standing order and registers the 15-minute
session prompt schedule. The driver API uses the existing `/api/session-schedules`
service; `hub schedule` is the separate launch-a-new-session scheduler.

Keep `orchestrator-main` and its recovery watchdog until the owner verifies two
consecutive driver operating cycles, including delivered schedule receipts and
completed useful work. Transfer its skills synchronization, inbox, reports and
handoff obligations explicitly; a managed engine's HOME is not the old
orchestrator's HOME. Retire the prior in-session cron only after that handoff is
acknowledged, and preserve watchdog supervision until replacement supervision
is verified. Do not copy human or host credentials into the driver.

For the current `orchestrator-main` handoff, require two consecutive proven
operating cycles before its owner retires the old in-session cron. Preserve
the watchdog until replacement supervision has been verified. This feature
does not create another queue-recovery loop or silently retire existing services.
