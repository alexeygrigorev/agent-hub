# Hub 2.0 feature inventory

Status: source and usage inventory, 4 November 2026. Labels:

- KEEP: preserve an externally used or stated core Hub capability.
- MERGE: preserve its behavior in a smaller owner module or simpler projection.
- DROP: exclude from Hub 2.0 because the examined active skills and aplexer integration provide no positive usage evidence, or another maintained system owns it.

## Evidence boundary

The inventory is based on the current Hub source and host package, the current skill references in bots-management, and the native aplexer bridge. The main references examined were:

- ~/git/bots-management/.agents/skills/hub-sessions/SKILL.md
- ~/git/bots-management/.agents/skills/aplexer/SKILL.md
- ~/git/bots-management/.agents/skills/desktop-tasks-via-hub/SKILL.md
- ~/git/bots-management/.agents/skills/dataops/SKILL.md
- ~/git/bots-management/.agents/skills/grok-limits-playbook/SKILL.md
- Current MCP allowlist: src/mcp-tools.ts.
- Current API and state: src/worker.ts, src/hub.ts, src/schema.ts, src/session_control.ts, src/fleet.ts, src/quota.ts, src/intake.ts, src/bus.ts, src/provisioning.ts, src/launch_schedules.ts, src/scheduled_prompts.ts.
- Current host/aplexer seams: host/agent_hub_host/session_control.py, host/agent_hub_host/aplexer_client.py, host/agent_hub_host/bridge.py, host/agent_hub_host/workclaim.py, host/agent_hub_host/state_hook_commands.py, host/agent_hub_host/provisioning.py.

The skills name concrete commands, API/MCP tools, schedules, and fallback behavior. All skill files were searched for Hub command and MCP references; the concrete product references were in the files listed above. Aplexer claims and mailbox events are read and mirrored by the host bridge. There is no complete usage-analytics export in this task's evidence set. Therefore DROP means “not found in the current skill/aplexer usage path,” not proof that no operator has ever used a button. Before implementation removes any DROP item, check current API telemetry and confirm there is no active consumer.

## Feature decisions

| Existing feature | Decision | Usage evidence and reason | Hub 2.0 owner |
| --- | --- | --- | --- |
| Verbatim request intake, one acceptance criterion per outcome, searchable/listable tasks, task context/history, append-only notes, priority and status | KEEP | hub-sessions requires intake create/append/list/show before work and verifies the intake before ending a turn (lines 42, 89–95, 290, 318); dataops creates an intake for CLI gaps (line 243). Implemented in src/intake.ts and intake MCP tools. | ProjectDO: intakes and intake_events |
| Assigning an intake only when it has nonempty acceptance criteria | KEEP | hub-sessions explicitly forbids start/assign without AC (line 108); current API rejects acceptance_criteria_required in src/mcp-tools.ts. | ProjectDO and Dispatch |
| Intake blockers, dependency links, decomposition, forgotten-task recovery, queued retry | KEEP | hub-sessions requires a blocker follow-up and dependency (section “Blocker → follow-up intake”, t1775u–t1776u), says every ask must enter and move through the queue (section “Every ask goes into the queue”, t1763u–t1764u), and current MCP exposes intake_blocker, intake_depend, intake_decompose, intake_forgotten. | ProjectDO queue and dependency index |
| Session start, routing policy, interactive/headless modes, status/list/detail, cancellation, stop and lifecycle evidence | KEEP | hub-sessions names sessions list/show/start/capture/stop/commands and says held is not running (lines 159–164, 184, 300–304); desktop-tasks-via-hub uses capability-based placement and verifies machine binding (lines 19–57). | ProjectDO, MachineDO, Dispatch, Host agent |
| Interactive send, bounded capture, command delivery ledger and acknowledgement | KEEP | hub-sessions uses send, capture and commands, requires acknowledgement, and forbids new requirements to a headless session (lines 174–190). | MachineDO command ledger; sessions API |
| Durable session wake and recurring session prompts | KEEP | The workspace rules require durable wakes for external events. grok-limits-playbook identifies scheduled Hub prompts as the replacement for /loop (line 30); the active workspace instructions require durable sessions wake records. | ProjectDO schedules and indexed queue |
| Operator-only remote shell and audit | KEEP | hub-sessions has the exact shell start/send/capture/shell-audit/stop path and says agent credentials cannot use it (lines 193–201). | MachineDO command path with operator-only scope |
| Capability-based machine registry, machine availability, OS/browser/tool labels, host enrollment and check-in | KEEP | desktop-tasks-via-hub targets the Windows desktop with os, browser, phone, and tool labels and checks machine availability (lines 19–57). hub-sessions uses routing-machines and host preflight (lines 300–304). | MachineDO, ControlDO actor/route records |
| Human login, host/agent pairing, and MCP authorization | KEEP | hub-sessions documents human hub auth login on the operator host (line 157); the current API has DTC sessions, device pairing, bearer scopes, and MCP OAuth in src/auth/, src/host_enrollment.ts, and src/mcp-oauth.ts. | ControlDO and auth.ts |
| Quota snapshots, quota banked view, model tiers, automatic account and engine choice | KEEP | hub-sessions directs quota and quota-bank reads through Hub, names model-policy and tier aliases, and prohibits casual engine override (lines 25, 235–239, 324). | AccountDO and Dispatch |
| Hub-owned provider login, credential refresh, and safe host provisioning | KEEP | hub-sessions says engine logins are centralized and new hosts receive managed accounts (line 324). Current code has encrypted bundles, generations, account refresh, and host acknowledgement in src/credentials.ts and src/provisioning.ts. | AccountDO; host installer and provisioner |
| Cross-host message send, inbox, show, reply, ack, delivery status, idempotent retry and readdress | KEEP | hub-sessions specifies all these commands, actor addresses, same-key retries, and MCP tools (lines 208–221). | MailboxDO; API and CLI |
| Aplexer native session binding, send bridge, state-report hooks, stop confirmation and reconnect reconciliation | KEEP | aplexer is the primary execution substrate when Hub works; hub-sessions defines it as backup only when Hub is down (aplexer skill lines 5–11). Current host code uses native UUIDs, state hooks, command delivery, and bridge/reconcile modules. | MachineDO and host/agent_hub_host/aplexer bridge |
| Aplexer work declarations mirrored as Hub claims, with scope conflict checks and release | KEEP | This workspace's aplexer instructions require a work join before editing and a work leave on completion. Current host/agent_hub_host/workclaim.py mirrors native work.* events; src/work_claim.ts and src/bus.ts hold claim state. | ProjectDO claims and host bridge |
| 30-minute root run, daily writer job, one-command long-running driver, and explicit recurring operator prompts | KEEP | The current product instructions name root and writer as Hub jobs. hub-sessions requires all routines through Hub; grok-limits-playbook identifies scheduled prompts as the replacement for /loop (line 30). | ProjectDO schedules, Dispatch, host job runner |
| Hub health, machine status, live session state, quota coverage, phone dashboard and inbox | KEEP | The current product instructions make health and dashboard core. Skills use status/show/capture and the inbox page; the desktop skill uses availability and rejection details. | MachineDO plus bounded Status projections and static public dashboard |
| Authenticated webhook wake for existing inbound wake sources | KEEP | grok-limits-playbook describes Hub webhook-to-session routing for the Dapier/dev mailbox wake (lines 32–33), and hub-sessions says webhook wakes are the exception to Hub-managed recurring routines (section “All routines go through Hub”, t1961u). Preserve authenticated enqueue/wake behavior, not the whole management console. | Worker ingress, ProjectDO queue |
| Session ping/loop and schedules as separate systems | MERGE | The skills require recurring prompts but do not require two implementations. The old API has both session pings and schedules. | One schedule model and queue; keep CLI aliases where needed |
| Ask ledger as an independent write model | MERGE | Skills require verbatim asks and explicit AC mapping, but their operational source of truth is the intake. | Intake creation plus append-only task events; no duplicate asks table |
| Separate jobs, starts, sessions, and dispatch queues | MERGE | The skills speak in sessions and intake assignments; start must be idempotent and tracked through native completion. | One dispatch intent and session lifecycle, with owner-shard rows |
| Host health-event streams, liveness tick tables, session audit heuristics, and metrics | MERGE | Operators need current health and the dashboard, not the existing collection of background scanners. | State-transition events plus bounded dashboard projections and Cloudflare telemetry |
| Banked quota / reset visibility | MERGE | hub-sessions explicitly uses quota banked, but does not need every reset-credit workflow. | Small sanitized quota snapshot fields in AccountDO |
| Conversation rooms and member/automation/link graph | DROP | No current Hub skill references conversations; durable message threads cover the evidenced agent and human exchange. Check current API telemetry before removing. | None; message thread_id remains |
| Repository scanner, repo metadata editor, Hub worktree register/repair commands | DROP | The examined skills use the separate Agent Branches tool and aplexer workspaces; none asks Hub to scan host repositories. | Agent Branches remains the workspace system |
| TBM reward-pool, earned-reset-credit automation, and detailed account-credit ledger | DROP | No active skill command found for these operator workflows. Preserve only the sanitized quota/banked view evidenced above. Check telemetry before removal. | None beyond quota snapshot |
| Hub-managed engine canary/version rollout console | DROP | The skills require host provisioning and engine account availability but contain no engine rollout/cancel command. Keep pinned host installation and credential sync; remove the separate fleet rollout state machine if telemetry confirms it is unused. | Host release manifest and AccountDO provisioning |
| Token/cost estimates or analytics without source proof | DROP | The inspected rules explicitly distinguish quota percentages from token use and cost. No usage evidence supports fabricated values. | No token/cost fields; unknown stays unknown |
| Arbitrary scheduled check actions and conversation automations | DROP | No skill uses these routes; they create a second generic automation surface. Required root/writer jobs, prompts, wakes, and authenticated webhook wakes remain. | Explicit schedule kinds and typed queue payloads only |
| Webhook route admin UI, payload injection tests, and verbose delivery inventory | DROP | Only inbound wake behavior is evidenced. No skill asks models to create/test/delete routes. | Operator-managed environment/config; no model-facing write tools |
| Full historical resource samples and broad audit/debug projections | DROP | No cited skill consumes these unbounded histories; host admission and session completion evidence remain required. | Current-state rows, append-only task/session transitions, bounded retention |

## Keep-feature to module map

Every KEEP item above is assigned to a v2 owner. In shorthand:

- Intake, AC, blocker, dependency, queue, and recovery → ProjectDO.
- Session lifecycle, commands, native binding, and host health → MachineDO and SessionDirectoryDO.
- Capability placement, account capacity, and the dispatch saga → Dispatch, ControlDO, MachineDO, and AccountDO.
- Quota, model policy, encrypted provider credentials → AccountDO.
- Human, host, agent, and MCP authentication → ControlDO and auth.ts.
- Messages and aplexer mailbox relay → MailboxDO and the host bridge.
- Work claims → ProjectDO and the host bridge.
- Root, writer, recurring prompts, wakes, and authenticated webhook wakes → ProjectDO schedules, Worker ingress, and Dispatch.
- Status, dashboard and health views → bounded Status projections and public static assets.
- Operator shell → MachineDO command path with an operator-only credential check.

The exact line allocation and database/index contracts are in ARCHITECTURE.md.
