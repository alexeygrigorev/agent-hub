# Hub 2.0 architecture

Status: architecture proposal, 4 November 2026. This is step 1 only: no implementation and no production changes.

## Goal and boundaries

Hub 2.0 preserves the Hub workflows that the current operator skills and the aplexer host bridge actually use, with an authored production-code budget of 5,700 lines including the host agent and dashboard. The stricter service target is fewer than 20,000 Durable Object SQLite row operations per normal day, reads plus writes across all Hub objects.

The existing Hub puts the registry, queue, sessions, messages, quotas, health, intake, and dashboard projections behind one global Durable Object. Hub 2.0 keeps strong consistency inside the small object that owns a particular decision, while moving high-volume state into deterministic shards: one per project, machine, account, or recipient. The global control object handles identity, small configuration, and cross-shard dispatch steps; it does not receive host heartbeats, message bodies, session output, or full-list queries.

The Worker never runs model processes. A host agent receives an authorized offer and invokes the existing Agent Quota Launcher and aplexer installation on that host. Aplexer remains the native session runtime. Hub does not replace Agent Branches, quota gates, or the launcher.

The product remains on Cloudflare Workers Free and uses SQLite-backed Durable Objects. The 20,000 combined-row-operation target is deliberately much lower than the current platform limits. Cloudflare's published pricing counts rows scanned by SQL reads, index rows written, deletes, and alarm writes; a SQL statement count is not a useful cost estimate. See [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [Durable Object limits](https://developers.cloudflare.com/durable-objects/platform/limits/), and [hibernatable WebSockets](https://developers.cloudflare.com/durable-objects/best-practices/websockets/).

## Components

| Component | Responsibility | State ownership |
| --- | --- | --- |
| Worker edge | Static dashboard, API and MCP entry points, bounded request validation, OAuth callback routing, Cron entry point | Stateless |
| ControlDO | Human and client identities, device-pairing approval, small global policy, actor routing metadata | One small global object |
| ProjectDO | One project's intakes, acceptance criteria, append-only task events, dependencies, active work claims, autonomous queue and project-level schedule rows | One object per canonical project |
| MachineDO | One host's capabilities, live connection, launch offers, session command delivery and host-reported lifecycle changes | One object per enrolled machine |
| AccountDO | One provider account's encrypted credential bundle, quota snapshot, refresh generation and capacity lease | One object per provider account |
| MailboxDO | One actor's durable inbox, message threads, delivery receipts and deduplication keys | One object per human, agent, or session recipient |
| SessionDirectoryDO | Compact session-id to project/machine routing index; no message bodies or output | One object, bounded indexed rows |
| Host agent | Outbound connection, local resource/quota recheck, launch/stop/capture command execution, aplexer hooks and state/message bridge | Host-local files and processes |
| CLI and MCP | Stable operator and agent interfaces over the same authorized API contracts | No independent Hub state |
| Dashboard | Static phone-friendly inbox, intake, session, fleet, quota, and health views | Reads bounded API projections |

Each shard has its own SQLite database. A start that needs more than one shard uses an idempotent saga: ProjectDO records a launch intent; the dispatcher reserves an account slot and a machine slot using the same operation id and increasing fencing epoch; it records each step; any failed reservation releases prior holds; the machine acknowledges its launch before the project task becomes running. Retries resume the recorded step instead of creating another native session. No cross-object transaction is assumed.

Machine state is event-driven. The host opens an outbound hibernatable WebSocket. Keepalives and unchanged resource samples do not write SQLite rows. A capability, drain, health, or connection-state transition writes only the affected machine row. Session commands and lifecycle events go to the owning MachineDO. The Hub uses the native session UUID and a fencing epoch; it never treats a queued command or a TUI keystroke as proof that work happened.

## Data model and indexes

Tables are intentionally small, normalized around their owner shard, and queried by primary key or a listed composite index. JSON payload columns are bounded and reserved for small details, not transcript or output storage.

| Owner | Tables and essential keys | Required indexes |
| --- | --- | --- |
| ControlDO | users(sub), credentials(id, token_hash, family_id, scopes, expires_at, revoked_at), device_pairings(code_hash, user_code, status, expires_at), global_policy(key, version, value), actor_route(actor, object_name, kind) | credentials(token_hash UNIQUE), credentials(family_id), device_pairings(user_code UNIQUE), actor_route(actor UNIQUE) |
| ProjectDO | intakes(id, title, text, acceptance_criteria, project, status, priority, version, created_at, updated_at); intake_events(task_id, seq, actor, kind, body, at); dependencies(task_id, prerequisite_id); claims(id, scope, holder, work_key, mode, expires_at, released_at); dispatch_queue(seq, item_id, ready_at, idempotency_key, payload); dispatch_sagas(operation_id, session_id, state, epoch, updated_at); schedules(id, enabled, next_due_at, cron, timezone, kind, payload) | intakes(project, status, updated_at, id); intake_events(task_id, seq); dependencies(prerequisite_id, task_id); claims(scope, expires_at) WHERE released_at IS NULL; dispatch_queue(ready_at, seq) WHERE ready_at IS NOT NULL; dispatch_queue(idempotency_key UNIQUE); dispatch_sagas(state, updated_at); schedules(enabled, next_due_at, id) |
| MachineDO | machine(id, alias, os, arch, capability_revision, drain_state, last_transition_at); machine_labels(label, machine_id); sessions(id, operation_id, intake_id, native_id, state, engine, tier, created_at, started_at, updated_at, epoch, lease_expires_at); session_commands(session_id, seq, key, kind, payload, state, acked_at) | machine_labels(label, machine_id); sessions(intake_id, created_at); sessions(state, updated_at, id); sessions(native_id UNIQUE); sessions(operation_id UNIQUE); session_commands(session_id, seq); UNIQUE(session_commands.session_id, session_commands.key) |
| AccountDO | account(id, provider, enabled, max_concurrency, generation, ciphertext); quota_snapshot(account_id, captured_at, remaining, reset_at, source, confidence); reservations(operation_id, session_id, epoch, expires_at, released_at) | quota_snapshot(captured_at); reservations(operation_id UNIQUE); reservations(expires_at) |
| MailboxDO | messages(id, sender, recipient, kind, thread_id, reply_to, body, key, state, created_at, delivered_at, read_at, acked_at, expires_at); delivery_events(message_id, seq, stage, actor, at) | messages(sender, key UNIQUE); messages(recipient, state, created_at, id); messages(thread_id, created_at, id); delivery_events(message_id, seq) |
| SessionDirectoryDO | session_route(session_id, project_id, machine_id, created_at, ended_at) | session_route(session_id PRIMARY KEY) |

The queue selects one eligible row with a B-tree seek on ready_at and sequence, then deletes that row and records the dispatch saga in one owner-shard transaction. It does not count the queue, sort an unindexed set, or walk completed rows. The scheduler writes the next occurrence only when a recurring job is enabled. An alarm is armed only for the earliest queued item, scheduled wake, expiry, or enabled recurring job. There is no alarm and no background database scan when none of those rows exists. An alarm pops only the first eligible queue item by its index.

Inbox reads use recipient plus state and a keyset cursor. Session and intake lists are also keyset-paged with a hard page cap. Histories are fetched by (owner id, sequence). We do not use unbounded COUNT, offset pagination, JSON-text searches, or full-table expiry sweeps on request paths.

## API, CLI, and MCP surface

The compatibility surface keeps the commands evidenced in the current skills:

| CLI group | v2 commands |
| --- | --- |
| Auth | auth login, status, logout; device approval for operator |
| Intake | create, list, show, context, history, append, update, assign, depend, blocker, forgotten |
| Sessions | start, list, show, send, commands, capture, stop, wake, shell-audit, count, tree |
| Messages | msg send, inbox, show, reply, ack, status, readdress |
| Fleet and quota | machines available, host-capabilities, routing-machines, quota, quota banked, model-policy |
| Operations | status, schedules list/create/pause/resume, driver start/status |

The REST API exposes these as versioned JSON resources under /api/intakes, /api/sessions, /api/messages, /api/machines, /api/fleet, /api/quota, /api/schedules, and /api/status. MCP is a narrow registry over the same handlers: intake_create/list/show/context/history/append/update/assign/depend/blocker; sessions_start/list/show/send/commands/capture/stop/wake; msg_send/inbox/show/reply/ack/status; machines_available, routing_machines, quota, model_policy, and status. Schedule administration remains in the operator CLI and dashboard. MCP does not expose host-local filesystem operations or administrator-only credential minting.

The existing hub sessions send --wait and hub msg inbox --wait options remain bounded waits over the hibernation WebSocket API. MCP inbox calls remain short, paged reads. The Durable Object never holds an HTTP long-poll request open. Hibernatable WebSockets preserve an idle host or inbox connection without keeping the object active; the runtime restores per-connection identity from a bounded attachment.

Every state-changing operation carries an idempotency key. Start requires a nonempty intake acceptance-criteria list. A model tier is an alias resolved by one policy table; the Hub chooses the engine/account. An explicit override requires a reason. Message identity is derived from the credential; an actor cannot claim another sender. Session-specific credentials cannot read another session's intake or inbox.

## Session, machine, quota, and intake flow

1. The requester records the verbatim ask and one acceptance criterion per required outcome in ProjectDO. Creating or reading an intake never launches a process.
2. sessions start validates the intake and AC, capability needs, machine freshness, account quota confidence, account capacity, host admission, idempotency key, and any engine override reason.
3. ProjectDO stores a queued launch intent. The queue index picks the oldest eligible item. The dispatcher records a saga, reserves a provider account slot and a machine slot with the same operation id and fencing epoch, and sends one bounded offer over the host's WebSocket.
4. The host rechecks current quota and local memory/disk gates. It launches through the maintained launcher and native aplexer. A native UUID plus Hub session UUID is returned as evidence. The host cannot assert completion without confirmed native exit and a result record.
5. State-report hooks send only working, idle, or waiting transitions. Aplexer mailbox messages are bridged to the recipient's MailboxDO; delivery keys and acknowledgements prevent duplicate sends and expose recorded, transported, acknowledged, read, and completed states separately.
6. The task records the session and final evidence. A distinct reviewer can record acceptance. On the next wake, the intake queue can resume a dependency or assign another queued task.
7. Quota views and placement use sanitized snapshots. A stale snapshot is refreshed only on a quota view or placement request. Provider credential refresh is on demand and single-owner per account generation; hosts receive only their approved encrypted bundle and report its generation.

Root runs every 30 minutes, daily writer runs, user-created schedules, recurring prompts, and durable session wakes are explicit queued work. They are not empty-queue polling. A scheduled occurrence creates one indexed queue row and advances that schedule. When no work, wake, expiry, or enabled schedule is due, there is no Hub alarm.

## Authentication and secrets

Humans authenticate through DTC OIDC authorization-code plus PKCE, an allowlisted operator identity, a secure session cookie, and CSRF protection on browser mutations. Host and agent identities use a one-time device approval, least-privilege scopes, hashed access/refresh credentials, rotation, and revocation. MCP uses OAuth authorization and the same scopes. A session token is bound to its intake, machine, workspace, and native session.

Provider OAuth material is encrypted at rest with an account-specific generation and a Worker-held encryption key. It is returned only to an authenticated, approved host during a signed provisioning request; it is never included in a UI, MCP response, error, audit body, or message. Operational logs contain fixed reason codes and bounded identifiers, not prompts, credentials, quota raw responses, transcripts, or command output. Device and host tokens are never included in migration reports or Git.

## Deployment

The deploy kit targets Workers Free, SQLite-backed Durable Objects, Cron only for explicit recurring schedules, and static assets. No D1, KV, R2, or paid plan is required. DO class versions and shard identifiers are introduced additively; an implementation release first runs only local Miniflare/Wrangler dry-run validation and a seeded migration test. Production deployment, traffic cutover, or Cloudflare resource changes require Alexey's later approval. This architecture step performs none of them.

The current source includes cost instrumentation in src/rows-meter.ts and event-shaped session, intake, bus, quota, and provisioning modules in src/. Hub 2.0 keeps bounded route-level row counters and reports them through Cloudflare telemetry or one batched nonzero daily summary. It does not write one meter row per API call.

## Normal-load Durable Object row-operation budget

This is a conservative design estimate, not a measurement. “Row operation” means a SQLite row read or write, including index rows, across every shard. Normal workload assumes 48 root runs/day, one daily writer, 20 additional sessions, four hosts, six provider accounts, 20 intake tasks, 100 messages, and 200 operator/dashboard reads. The per-session rows round the resulting 69 scheduled and user sessions up to 70. Empty queue checks and idle WebSocket keepalives make zero SQLite row operations.

| Flow | Daily assumption and allowance | Row operations/day |
| --- | --- | ---: |
| Session placement and cross-shard reservation | 70 starts × 40 reads/writes, including saga, account and machine leases | 2,800 |
| Session state and command lifecycle | 70 sessions × four transitions × five row touches | 1,400 |
| Intake writes and reads | 20 tasks × 12 plus 50 indexed lists/details × 10 rows | 740 |
| Messages and delivery receipts | 100 messages × eight rows for insert, inbox, delivery, and ack | 800 |
| Native work claims | 50 create/check/release cycles × four rows | 200 |
| On-demand quota snapshots | Up to 48 stale placement/view batches × six accounts × four read/write row touches | 1,152 |
| Scheduled root/writer queue | 48 root occurrences plus one writer × two queue/schedule touches | 98 |
| Root and writer API activity | 49 runs × 10 bounded calls × about five returned/index rows | 2,450 |
| Operator, MCP, and dashboard reads | 200 calls × 11 rows including authorization and bounded projection | 2,200 |
| Retry/expiry work | 20 due or retrying rows × six indexed touches | 120 |
| Index, metering, audit, and cross-shard reserve | Explicit 13% allowance for extra index and saga rows | 1,800 |
| Machine event transitions | Four hosts × two actual state transitions/day × four rows | 32 |
| Total forecast | Rounded upward from 13,792 | 14,000 |

The forecast leaves 6,000 row operations/day below the strict 20,000 combined target. A real implementation must meter rows_read plus rows_written by shard and route, alert at 15,000/day, stop optional summaries/retries above 18,000/day, and fail the load-test gate at 20,000/day. The estimate assumes indexed keyset reads, no per-heartbeat SQL, no periodic quota polling, and no unbounded retention work; if measured data disproves any assumption, revise the architecture before production.

## Production line budget and planned tests

The line budget covers authored production code, excluding tests, generated bundles, vendored assets, and documentation. It includes src/ plus the host CLI/agent, dashboard, and deploy scripts so the whole rewrite stays below 6,000 lines.

| Module | Budget |
| --- | ---: |
| src/worker.ts: edge routes, assets, cron | 160 |
| src/control-do.ts: auth directory, config, dispatch saga | 220 |
| src/repositories.ts: schemas, indexes, transactions, bounded query helpers | 400 |
| src/auth.ts: DTC, device pairing, scopes, MCP OAuth | 420 |
| src/project-do.ts: intakes, events, dependencies, queue | 420 |
| src/machine-do.ts: host presence, sessions, commands, lifecycle | 500 |
| src/dispatch.ts: route choice, lease fencing, retries | 380 |
| src/fleet.ts: machine registry and capability matching | 300 |
| src/account-do.ts: quota, model policy, sealed credentials | 450 |
| src/mailbox-do.ts: messages, receipts, aplexer relay | 380 |
| src/schedules.ts: root, writer, prompts, session wakes | 220 |
| src/status.ts: health and bounded dashboard projections | 150 |
| src/api-mcp.ts: REST validation and allowlisted MCP registry | 400 |
| src subtotal | 4,400 |
| host/agent_hub_host/: stdlib CLI, receiver, installer/update, aplexer bridge | 800 |
| public/: phone-friendly static dashboard | 350 |
| scripts/: local build, migration, and deploy-kit helpers | 150 |
| Total production-code budget | 5,700 |
| Unallocated ceiling | 300 |

Planned tests, not run as part of this architecture task:

- Contract tests for every kept CLI and MCP command, JSON/API compatibility, page limits, and typed refusals.
- SQLite tests assert each list, inbox, queue-pop, retention, and status query uses its named index; test row counts from cursor rowsRead/rowsWritten rather than SQL statement counts.
- Queue tests cover FIFO among due items, delayed items, idempotent retries, partial cross-shard reservation rollback, stale fencing epochs, and no alarm on an empty queue.
- Placement tests combine machine capabilities, online state, quota freshness, account caps, and local host admission without routing to an unadvertised tool or OS.
- Aplexer integration tests cover native UUID binding, state-report transitions, command delivery/ack, inbox/reply threading, duplicate suppression, stop confirmation, and disconnected-host recovery.
- Security tests cover DTC PKCE, CSRF, device-code one-time use, scope ceilings, refresh rotation, revocation, MCP grants, credential encryption, and secret-free errors.
- Migration tests round-trip representative intakes, dependencies, schedules, machines, accounts, sessions, and message threads with IDs and event order preserved.
- A load-budget test replays the normal workload above and fails if aggregate SQLite rows_read plus rows_written reaches 20,000/day; idle tests assert zero DB reads/writes and no alarm when no explicit work exists.
