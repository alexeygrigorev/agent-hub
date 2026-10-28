# Cross-device session messaging

Design for intake 4fc83e80-3a56-4295-9f3d-03830b933f61 (Alexey t1838u, 2026-10-28).
Implementation is tracked by the Tabo intake a3227276-8e38-43a6-bca6-3b5be5427c86 (AC4)
and the child intakes listed in [Hand-off](#7-hand-off).

Goal: the Developer (the Grok Bot assistant, which reaches Hub through
`ssh hetzner` + `hub` CLI and through the hub-mcp connector) and any session on any host (Hetzner, Tabo/Windows, later
others) message each other through Hub. A message to a headful (interactive)
aplexer session lands in its TUI like `a send`, with receipts, and the session
answers with `hub reply`.

Status: implemented on main by tabo-hub-t1833 (intake a3227276) except where
[§12](#12-implementation-notes) says otherwise.

## 1. What exists today (inventory)

Line references are against `a4b5d0da` (agent-hub) and aplexer `360c0ab`.

### 1.1 aplexer (local session runtime)

- aplexer is its own PTY multiplexer, not tmux. Each session has a worker that
  speaks RPC over a Unix socket (Linux) or a named pipe
  `\\.\pipe\aplexer-<sid>-<uuid>` with ConPTY and a Job Object (Windows)
  (`~/git/aplexer/docs/windows-port.md`).
- `a send`/`a capture` write to and read from the PTY through the worker RPC.
  `a rpc --stdio --protocol 1` (`~/git/aplexer/docs/rpc-stdio.md`) exposes
  `capabilities/list/status/capture/send`, payload up to 1 MiB. A send only
  proves bytes reached the PTY (`pty_written`); there is no application ack.
- Native mailbox: `aplexer message send|deliver|log|reply`. `message deliver`
  types a queued envelope into the TUI only if the harness hooks report
  `waiting|idle`; otherwise `NotReady`/`DeliveryUncertain`
  (`~/git/aplexer/src/bin/aplexer/message_deferred.rs:47-130`). Mailboxes are
  per canonical workspace (1000-message ring) and vanish with a removed
  worktree (docs/WORK-CLAIM.md:62-81).
- Native identity: session UUID, tag, canonical workspace, `created_at_ms`,
  engine. Selectors: UUID, 8+ char prefix, `workspace:tag`.
- Windows: the code path is ported but has no released Windows binary and no
  physical acceptance on Tabo (`~/git/aplexer/docs/windows-backend-dependency.md`,
  docs/DEPLOY.md:189-245).

### 1.2 Hub bus (`hub send/inbox/reply/ack`)

- CLI: `host/agent_hub_host/cli.py:439-477`, handlers `:778-830`, client
  `host/agent_hub_host/bus.py:89-142`. `reply` is `send` with `reply_to` set.
  Token: `HUB_JOB_TOKEN`/`HUB_TOKEN`, else the stored profile (`bus.py:48-56`).
- Server: routes `src/hub.ts:2242-2933`, auth `authenticateBusRequest`
  (`src/hub.ts:2085-2177`), store `src/bus.ts`, table `messages`
  (`src/schema.ts:191-209`): ULID id, from/to actor, kind, thread_id, reply_to,
  body, `UNIQUE(from_actor, idempotency_key)`, timestamps
  `delivered/read/acked/accepted/completed_at`, 30-day `expires_at`.
- Addresses (`src/bus.ts:104-127`): `tag`, `project:tag`, `role:<r>`,
  `machine:<id>`. Recipients must resolve to an `agents` or `machines` row
  (`src/bus.ts:362-375`).
- States: `delivered_at` = recipient or its host polled the inbox; `read_at` =
  `show`; ack → accept → complete, write-once, recipient-only
  (`src/bus.ts:565-625`).
- Ordering: `ORDER BY id` on ULIDs, cursor `id > ?` (`src/bus.ts:555-562`);
  same-millisecond ULIDs are not monotonic (bridge fence
  `host/agent_hub_host/bridge.py:1018-1031`).
- Limits: body 16 KiB (`src/bus.ts:455`), actor/key/thread 128 chars, inbox page
  100. No server retries. `lease_epoch` stored, never enforced.
- Bridge into sessions: `hub bridge --once|--cycles N` (foreground, not
  installed as a service) polls inboxes of tags on the host, queues
  `aplexer message send --queue` (`bridge.py:627`), checks idle + empty composer
  three times by scraping the prompt glyph (`bridge.py:73, 512-558`), then runs
  `aplexer message deliver` (`bridge.py:792`) and relays native replies as
  `bus.reply` (`bridge.py:837-965`). It injects regardless of kind, while
  docs/SPEC.md §7.5 limits prompt typing to `kind: sync_reply`.

### 1.3 Conversations

- `src/conversations.ts:39-105`: `conversations`, `conversation_members`
  (`last_read_seq`), `conversation_messages` with per-conversation `seq` and
  per-sender idempotency. Sender derived from the credential (`:145-151`).
  Body 16 KiB, 64 members, 30-day retention.
- The Developer (`human:<sub>`) is a first-class member; host and job tokens are
  refused (`:617-760`).
- `hub conversations bridge --enable-native` (agent-only, foreground) copies
  conversation messages into the native inbox (no TUI typing) and relays native
  replies (docs/CONVERSATION-BRIDGE.md, CONVERSATION-BRIDGE-GAPS.md).

### 1.4 Session control (`hub sessions send/capture`)

- The only path addressed by Hub session UUID. `POST /api/sessions/:id/send`
  (`src/session_control.ts:2540-2567`): body ≤ 16000 chars, session must be
  `running` (`:92`), optional `require_ready` against `fleet_sessions.agent_state`
  fresher than 5 min (`:90`).
- Commands: recorded → transported → claimed → acked|refused|uncertain|failed
  (`:16-26, :85`); idempotency `(session, actor, key)`; caps 2000 pending / 200
  per session; claim lease 60 s, 3 attempts (`:262-264, :1162-1185`).
- Host: polls `GET /api/session-commands/offers` (`:2638-2725`), claims, acks.
  `host-receive --once` runs inline in the serial 2 s main loop
  (`host/agent_hub_host/session_host.py:868-925`), so one slow native call
  delays every command on that host.
- Executor (`host/agent_hub_host/session_executor.py:1845-1940`):
  `send_inbox` → `aplexer message send` (`native_sessions.py:543-605`),
  `deliver_recorded` → `aplexer message deliver` (`:608-690`), then
  `verify_ack_via_reply`. A send is `acked` only after a correlated native
  reply (`validateSendProof`, `src/session_control.ts:2975-2986`); a TUI that
  never runs `a message reply` stays `uncertain/recipient_reply_pending`.
- Targets are re-qualified (tag, UUID, created_at, engine) before every effect
  (`native_sessions.py:426-468`). Windows workspace canonicalization opens a
  real handle and refuses a deleted worktree (`win32_fs.py:268-281`).

### 1.5 Claims and identity

- Native work claims are synced by the host daemon to `POST /api/bus/native-sync`
  under actor `native:<machine>:<tag>[<sid>]` (`src/work_claim.ts:54-67`); that
  actor has two colons and is not a valid bus address. Hub `claims` table
  `src/schema.ts:211-225`. Claims coordinate edit scope; they carry no messages.
- Credentials (`src/schema.ts:12`): `host|agent|job|human`. The human CLI
  login is `admin`-scoped `human:<sub>` (`src/auth/human-credential.ts:76-83`).
- The only per-session credential is the fleet worker credential
  (`src/worker-access.ts:113-147`): actor `hub-workers:worker-<launch>`,
  scopes `bus:read fleet:launch worker:own-intake`, and `/api/bus/*` is not in
  its route allowlist (`:84-108`). Interactive sessions get none; engine
  environments strip `HUB_TOKEN`, `HUB_JOB_TOKEN`, `HUB_CONFIG_FILE`
  (`host/agent_hub_host/provisioning.py:1249, :1551`).
- `audit` table (`src/schema.ts:131-139`) has three writers, none for messages.

### 1.6 Gaps for cross-device messaging

| # | Gap |
|---|-----|
| G1 | No address for a session: bus uses tags/roles, session control uses UUIDs, conversations use members. Tags are not unique across machines. |
| G2 | The Developer cannot receive bus replies: `human:*` is not a recipient. Admin callers may set any `from_actor`. |
| G3 | Sessions have no own identity: they borrow the host token (acts for any agent on the machine) or the human admin token. Sender is unverifiable. |
| G4 | Delivery into a TUI exists twice (bridge, session control) with different readiness checks and different "done" meanings; the bridge is not a running service. |
| G5 | `delivered` means "host polled", not "the TUI got it"; session-control `acked` requires a reply. Nothing reports "typed and submitted". |
| G6 | No durable offline queue: session commands fail after a 60 s lease × 3; busy or offline targets lose the message. |
| G7 | Serial host receiver: one slow `deliver` blocks capture/send on the host. |
| G8 | Windows: no proven injection; the host runs as an S4U Scheduled Task without an interactive desktop and may not reach the user's worker pipes; hook-based readiness is unverified. |
| G9 | Ordering is ULID-only; no per-recipient sequence. |
| G10 | Size limits disagree (16000 chars vs 16 KiB vs 1 MiB); no large-payload reference. |
| G11 | No message audit trail; message rows expire after 30 days. |
| G12 | No stable identity for long-lived agents (Developer, orchestrator-main, oncall). The hub-mcp connector and the Hetzner CLI login both mint `human:<sub>` admin credentials (`src/mcp-oauth.ts:220`), so the Developer is indistinguishable from Alexey; orchestrator-main/oncall are reachable only by their current session tag. |

## 2. Decisions

1. **One store: the bus `messages` table.** It already has threads, `reply_to`,
   idempotency and staged receipts. Named agents (§9) are ordinary
   recipients with their own inbox. Conversations stay the multi-party chat
   surface and may later mirror into bus threads; they are not the
   cross-device transport.
2. **One injector: the session host's command path.** Hub turns each message
   addressed to a session into a session-control command of kind
   `message`. The host reuses its offer/claim/ack transport and the existing
   `aplexer message send` + `deliver` executor. `hub bridge` is retired for
   sessions that have a Hub session id.
3. **Delivery is at-most-once into the TUI, at-least-once into the native
   inbox.** Typing a prompt twice is worse than a missed nudge; the inbox copy
   and `hub inbox` stay authoritative.
4. **Every session sends as itself.** Hub mints a per-session messaging
   credential; the sender is derived from the credential, never from `--from`.
5. **No new infrastructure.** Same Durable Object, same host polling, Workers
   Free plan, no blob store.

## 3. Addressing

| Address | Meaning | Resolution |
|---------|---------|-----------|
| `session:<hub-session-uuid>` | One Hub session (canonical) | `session_control_sessions.id`; must be bound (`running`) or queued for binding |
| `<tag>@<machine>` | Convenience | Machine id, name, hostname or alias (via `machine-alias.ts`) + tag → exactly one live Hub session; 0 → `unknown_recipient`, >1 → 409 `ambiguous_recipient` with candidates. Stored resolved as `session:<uuid>`. |
| `agent:<name>` | A named agent with a stable identity and its own inbox: `agent:developer`, `agent:orchestrator-main`, `agent:oncall` (§9) | `named_agents` row; `agent` is a reserved project name |
| `human:<sub>`, `human:me` | A human (Alexey) | `human:me` = the caller's own human actor. Only human actors with a Hub login are valid. |
| `tag`, `project:tag`, `role:<r>`, `machine:<id>` | Existing bus addresses | Unchanged. |

- The sender address of a session is always `session:<uuid>`; replies to it
  route back across hosts automatically.
- A message to a session survives a rebind of the same Hub session (generation
  bump) but not a new Hub session: a replacement session does not inherit the
  queue unless the operator re-addresses it (`hub msg readdress`).
- `parseAddress` (`src/bus.ts:111-127`) gains the `session:` and `human:`
  kinds; `@` addresses are resolved at send time only.

## 4. Message model and states

New columns on `messages` (all nullable for old rows):

- `to_session_id`, `from_session_id`: resolved Hub session ids.
- `recipient_seq`: per-recipient monotonic integer assigned in the send
  transaction (`recipient_cursors(actor, last_seq)`). Ordering and inbox
  cursors use `recipient_seq`; the ULID remains the id.
- `delivery`: `inbox` (default for agent→agent notes) or `tui` (inject into the
  terminal). Sends to a session from a human or a
  named agent default to `tui`.
- `deliver_by`: deadline for TUI injection, default 24 h, max 7 days.
- `state`: see below; `state_reason` text.
- `ref_json`: optional large-payload reference (§8).

States (forward only, each with a timestamp and a `message_events` row):

```
accepted ─► dispatched ─► delivered ─► read ─► acked ─► accepted_work ─► completed
   │            │             │
   │            │             └─► (reply) implies read
   │            ├─► uncertain ──► delivered | expired      (resolution, §6)
   │            └─► not_ready (transient, back to accepted with backoff)
   └─► expired | undeliverable(session_ended|recipient_gone)
```

| State | Meaning | Who sets it |
|-------|---------|-------------|
| accepted | Stored in Hub, idempotency checked, seq assigned | Hub |
| dispatched | The target host claimed the delivery command | Hub on claim |
| delivered | `inbox`: written to the native mailbox. `tui`: `message deliver` confirmed the envelope was typed and submitted at an idle prompt | Host ack |
| read | The recipient opened it (`hub msg show`, `hub inbox --mark-read`) or replied | Recipient credential |
| acked / accepted_work / completed | Existing recipient stages, unchanged | Recipient credential |
| uncertain | The host lost track between write and confirmation | Host ack / lease expiry |
| expired | `deliver_by` passed without `delivered` | Hub sweeper |
| undeliverable | Target session stopped, or `@` resolution target vanished | Hub |

`delivered_at` keeps its old meaning for non-session recipients (inbox
polled). For session recipients, inbox polling by the host never sets it.

## 5. Delivery into a headful TUI

### 5.1 Hub side

- Send to a `session:` recipient creates the message plus one session-control
  command `{kind: 'message', message_id, recipient_seq}` in the same
  transaction. Commands of kind `message` are exempt from the 3-attempt
  limit; they live until `deliver_by` or until the session ends.
- Offers per session are serialized: only the lowest undelivered
  `recipient_seq` is offered; later messages wait. This gives in-order delivery
  per recipient.
- `GET /api/session-commands/offers` returns message offers after stops and
  before ordinary sends; the binding is the same `{native_id, tag, workspace,
  generation, native_created_at_ms}`.

### 5.2 Host side (Linux and Windows, same code)

1. Claim the offer (lease 60 s).
2. Qualify the target (`qualify_target`, unchanged). Failure → ack `refused`
   with the reason; Hub marks the message `undeliverable` only for
   `session_ended`, otherwise retries.
3. `aplexer message send --workspace W --to TAG --kind hub-message --json`
   with the rendered body (§5.3). This is the inbox copy; its envelope id is
   recorded in the host receipt journal keyed by `message_id` so a retry never
   queues twice.
4. For `delivery=tui`: `aplexer message deliver <envelope>`. The aplexer
   readiness rule decides (hooks report `waiting|idle`). On `NotReady` the host
   acks `not_ready`: Hub returns the message to `accepted` with backoff
   (5 s, 15 s, 30 s, then every 60 s) without consuming an attempt.
5. On deliver success, ack `delivered` with the envelope id. Hub does **not**
   wait for a reply (the current `validateSendProof` rule stays for
   `hub sessions send` only).
6. On a crash or timeout after step 4 started, ack `uncertain`. Never re-type.
   Resolution: the next host pass runs `aplexer capture --screen` and searches
   for the message marker `⟦hub:<message_id-short>⟧`; found → `delivered`
   (evidence: capture hash), not found within `deliver_by` → `expired` with the
   inbox copy still present.
7. Delivery runs on a dedicated worker thread per host with a per-session
   in-flight limit of 1 and a global limit of 4, so a slow `deliver` no longer
   blocks capture/stop (fixes G7).

The host decides readiness only through aplexer (`message deliver`), not by
scraping the prompt glyph. The old bridge heuristic is retired.

### 5.3 Rendered TUI text

One prompt, plain text, at most 2 KiB rendered:

```
⟦hub:01JD3K7Q⟧ from agent:developer (thread 01JD3K…): <body, or first 1500 bytes>
[truncated: hub msg show 01JD3K7QXYZ…]  Reply: hub reply 01JD3K7QXYZ… --body -
```

The full body is always available through `hub msg show <id>` and the native
inbox copy. Control characters other than newline and tab are stripped before
rendering; the body is never interpreted as keystrokes (no escape sequences,
no Ctrl-C).

### 5.4 Windows equivalent

- Same aplexer CLI calls; on Windows they reach the session worker through its
  named pipe and write with ConPTY. No Windows-only send path in Hub.
- Requirement: the host process that runs `aplexer message deliver` must be
  able to open the session worker's pipe. Interactive sessions on Tabo are
  started inside the logged-on user's session, while the host Scheduled Task
  runs S4U (no interactive token). The Tabo child intake must prove one of:
  (a) the pipe DACL grants the same user SID and S4U can connect, or
  (b) the delivery worker runs as a second task with "Run only when user is
  logged on" (InteractiveToken) and talks to the main host over its local
  receipt journal. Prefer (a); (b) is the fallback.
- Hook readiness on Windows must be verified for codex and claude; until
  verified, `delivery=tui` to a Windows session falls back to `inbox` with
  `state_reason=tui_unverified_platform` (no silent typing into an unknown
  state).
- Workspace identity: if the canonical workspace no longer exists, the host
  qualifies by session UUID alone (aplexer accepts UUID selectors) instead of
  refusing.

## 6. Replies, retries and offline behaviour

- **Reply from a session**: `hub reply <message_id> --body -` (credential of the
  session) → new message `from session:<uuid>` to the original sender, same
  `thread_id`, `reply_to` set. If the original sender is a session on another
  host, it is delivered there by §5. If it is `agent:developer`
  or a human, it lands in that inbox (§9).
- **Reply via the native mailbox** (`a message reply`): the host relays any
  native reply whose `reply_to` is a `hub-message` envelope as `hub reply` with
  the session's credential. The relay is idempotent on the native envelope id.
- **Session to session**: `hub send --to session:<uuid>` or `--to tag@machine`.
  Default `delivery=inbox`; `--tui` asks for injection (allowed when the
  sender's project matches the recipient's, or the sender is a human or a named agent).
- **Host offline**: messages stay `accepted`; nothing is lost until
  `deliver_by`. The sender sees `accepted` with `state_reason=host_offline`
  (from the registry's heartbeat staleness).
- **Session busy**: `not_ready` backoff (§5.2 step 4).
- **Session stopped**: pending messages become `undeliverable:session_ended`
  and the sender gets a `status` message from `system:hub` (one per thread,
  not per message).
- **Idempotency**: unchanged (`UNIQUE(from_actor, idempotency_key)`); the CLI
  generates a key per invocation and prints it, so a retried CLI call with
  `--idempotency-key` is safe across devices.
- **Sender retries**: the CLI retries HTTP failures with the same key, 3 times
  with jitter; the server is the only place that changes state.

## 7. Hand-off

Child intakes (agent-hub):

| Child | Intake | Scope | Depends on |
|-------|--------|-------|-----------|
| M1 Hub: session/human addresses, seq, states, audit | b9cbf95e-81e2-4774-9d56-69bd997bd618 | §3, §4, §10, server endpoints | — |
| M2 Per-session messaging credential | 008529b5-5251-42bf-a8c8-f6c51fea520c | §8 auth, CLI identity | M1 |
| M3 Host delivery worker | ad9f9166-85a8-48c5-ba04-70fab15c721e | §5.1-5.3, §6 relays, retire `hub bridge` for Hub sessions | M1, M2 |
| M4 Windows/Tabo delivery and round-trip proofs | d96feddf-86ec-4af6-b980-02032ab933b6 | §5.4, proofs for a3227276 AC4 | M3, a3227276 AC1 |
| M5 CLI, MCP tools, human inbox, docs | 3bdf9715-d11d-48b7-8593-c30c4e05a3e6 | §11, hub-sessions usage docs | M1, M2 |
| M6 Named agents and Developer identity + round-trip proofs (Alexey t1840u) | a7c57b6e-a2fb-4c86-9d1f-543963985a28 | §9 | M1, M2, M5 (proof needs M3, M4) |

The Tabo implementation intake a3227276 owns M4 and the cross-host proofs.
M1–M3, M5 and M6 may be done by any implementer, in dependency order.

## 8. Auth

- **Session messaging credential.** When a Hub session binds (interactive or
  headless), the host requests `POST /api/sessions/:id/messaging-credential`
  with its host token. Hub checks the session is bound to that machine and
  mints an `agent` credential:
  - actor `session:<uuid>`, project = the session's project,
  - scopes `msg:send msg:read` only,
  - access token 1 h, refresh family bound to the session, revoked when the
    session is stopped or rebound to another machine.
- Storage: `<workspace>/.local/hub-session-auth/<native_id>.json`, written via
  `private_fs` (0600 on Linux, owner-only ACL on Windows). The CLI finds it
  through `APLEXER_SESSION_ID` + `APLEXER_WORKSPACE`, which aplexer already
  sets; no token goes into the engine environment.
- **Project scoping.** A session credential may send to sessions of its own
  project, to named agents (§9), to `human:*` actors that are Hub operators,
  and to existing bus
  addresses in its project. Cross-project sends are refused
  (`cross_project_forbidden`) unless the recipient project grants it in
  config (`messaging.cross_project_allow`). Human admins may send anywhere.
- **No sender spoofing on the new paths.** The new address kinds ignore
  `from_actor` and derive the sender from the credential, as conversations
  do. Admin `from_actor` override remains only for legacy addresses and is
  recorded in the audit event.
- Host tokens may only ack delivery commands for sessions bound to their
  machine (existing binding check) and may relay native replies only for
  those sessions.

## 9. Named agents and the Developer identity (Alexey t1840u)

A named agent is a stable address that outlives sessions and devices. Table
`named_agents(name, project, display, credential_family, delegates_json,
forward_session_id, created_by, created_at, disabled_at)`.

| Address | Who acts as it | Inbox read by | Forward |
|---------|----------------|---------------|---------|
| `agent:developer` | The Developer: the Grok Bot assistant via `ssh hetzner` + `hub` CLI, and via the hub-mcp connector | `hub inbox` / MCP `msg_inbox` | none (pull) |
| `agent:orchestrator-main` | The current orchestrator-main session | the session (TUI injection) | its current Hub session |
| `agent:oncall` | The current oncall-engineer session | the session | its current Hub session |

- **Own credential.** An operator creates the identity once:
  `hub agents create developer` mints an `agent` credential with actor
  `agent:developer`, scopes `msg:send msg:read`, stored on Hetzner as its own
  profile (`~/.config/agent-hub/developer.json`, used with
  `hub --profile developer` or `HUB_CONFIG_FILE`). It never shares the human
  `cli.json`, so Alexey's login and the Developer stay distinct identities.
- **MCP.** The hub-mcp connector keeps its human OAuth login. Messaging tools
  accept `as: "agent:developer"`; Hub allows it only when the caller's
  credential family is listed as a delegate of that named agent
  (`hub agents delegate developer --mcp-client <client>`), and records
  `on_behalf_of: human:<sub>` in the audit event. The sender is
  `agent:developer`, so replies thread back to the Developer inbox, not to
  Alexey's.
- **Reach.** A named agent may send to every session on every host, to other
  named agents and to humans. Project scoping (§8) does not apply to
  `agent:developer` (operator-created, cross-project by definition) and is
  configurable for the others.
- **Receiving.** Messages to `agent:developer` stay in its inbox (pull):
  `hub inbox --wait 60` or MCP `msg_inbox`. Replies use `hub reply <id>`, same
  `thread_id`, routed to the original sender's host by §5.
- **Forwarding** (orchestrator-main, oncall): when a named agent has
  `forward_session_id`, Hub enqueues TUI delivery to that session as in §5;
  the message stays addressed to the named agent, so a replacement session
  only needs `hub agents forward orchestrator-main --to session:<uuid>`
  (the session driver runs this on start). A session replies as the named
  agent when its messaging credential is a delegate of it.
- **Humans.** `human:<sub>` remains a valid recipient for Alexey. The web UI
  gets an Inbox page (unread count, threads, reply box, phone-friendly) for
  the human inbox and delegated named-agent inboxes.

Proofs required by M6: `agent:developer` ↔ a headful Tabo session and
`agent:developer` ↔ a headful Hetzner session. Each proof is a send, a TUI
delivery, a `hub reply` from the session and the reply read from the
Developer inbox through the CLI and through MCP, with `hub msg status` event
lists for both messages.

## 10. Size limits, ordering, audit

- **Sizes**: body ≤ 16 KiB UTF-8 bytes everywhere (the session-control limit is
  aligned from 16000 characters to 16384 bytes); TUI render ≤ 2 KiB (§5.3).
- **Large payloads by reference**: `ref` = `{kind: 'git'|'intake_artifact'|'url'|'path',
  value, sha256?, bytes?}`, max 2 KiB of JSON. `path` is only valid with a
  `machine` field and is shown, never fetched. No blob storage in Hub.
- **Ordering**: per recipient by `recipient_seq`; per thread by `recipient_seq`
  of each participant. No global order is promised.
- **Audit**: new append-only table `message_events(id, message_id, event, actor,
  machine_id, at, detail_json)` for every state change, auth decision
  (refusals included) and admin override. Event rows keep metadata only (no
  body) and are retained 180 days; message bodies keep the 30-day retention.
  `hub msg status <id>` prints the event list.

## 11. CLI and MCP

Existing commands are extended rather than duplicated:

```
hub send --to session:<uuid>|<tag>@<machine>|human:me|<legacy> [--tui|--inbox]
         [--kind note] [--body -|TEXT] [--thread ID] [--ref JSON]
         [--deliver-by 24h] [--idempotency-key K]
hub inbox [--unread] [--thread ID] [--wait 60] [--mark-read]
hub show <id>                       # sets read
hub reply <id> --body - [--tui]
hub ack|accept|complete <id>
hub msg status <id>                 # states + audit events
hub msg readdress <id> --to session:<uuid>   # operator only
hub agents create|show|delegate|forward NAME ...  # operator only
hub --profile developer inbox                     # Developer identity
```

- `--from` becomes optional and is refused when it differs from the credential
  for new address kinds.
- `hub inbox --wait` long-polls up to 60 s (single Durable Object request with
  a bounded wait), so sessions that prefer polling need no host loop.
- MCP: `msg_send`, `msg_inbox`, `msg_show`, `msg_reply`, `msg_ack`,
  `msg_status`, each with optional `as` (named agent, delegate-checked).
  The legacy `bus_*` tools stay for old addresses.
- Exit codes: 0 accepted, 2 refused (auth/validation), 3 recipient unknown or
  ambiguous, 4 transport error after retries.

## 12. Implementation notes

Code: `src/session-messaging.ts` (addresses, states, audit, delivery commands,
session credentials), `src/bus.ts` hooks, `src/session_control.ts`
(`message` offers/claims/acks), `host/agent_hub_host/session_messages.py`
(delivery, reply relay, credential provisioning), `host/agent_hub_host/msg_cli.py`
(`hub msg`). Tests: `test/session-messaging.test.ts`,
`host/tests/test_session_messages.py`, `host/tests/test_msg_cli.py`.

How it maps onto the design:

- Sessions and named agents are agent-registry identities (`session:<uuid>`,
  `agent:<name>`), not a new table: the existing bus, its threads, idempotency,
  recipient stages and host authority (agents bound to the host's machine)
  apply unchanged. `named_agents` is therefore the `agents` rows with project
  `agent`.
- Replies from a session: either `hub msg reply <id>` with the session's own
  credential, or the native `a message reply <envelope> "text"`, which the host
  relays as `session:<uuid>` (§6). The rendered TUI text names both.
- Session credentials live in the user's state directory
  (`~/.local/state/agent-hub/session-auth/<native-id>.json`,
  `%LOCALAPPDATA%\agent-hub\session-auth\` on Windows), not in the workspace,
  so no repository ever sees them. `hub msg` finds them via
  `APLEXER_SESSION_ID`; an explicit `HUB_CONFIG_FILE` wins.
- Developer identity: `hub msg agent-credential developer --output
  ~/.config/agent-hub/developer.json` (operator), then `hub msg --profile
  developer inbox|send|reply`. Through hub-mcp, the human login sends with
  `from_actor: agent:developer` and reads `bus_inbox actor=agent:developer`
  (existing admin authority); the `as`/delegate check of §9 is not built.
- CLI surface is `hub msg …` (new) instead of changing `hub send`, so existing
  `hub send --from` users keep their behavior.

Not built yet (follow-ups): MCP `as` delegation with `on_behalf_of` audit;
`human:<sub>` recipients and the web Inbox page; `hub msg readdress`; named
agent forwarding (`agent:orchestrator-main` → its current session) — today
those coordinators are reachable once they are Hub sessions (operator
adoption), as `<tag>@<machine>`; capture-marker resolution of `uncertain`
deliveries (they stay `uncertain` with the inbox copy authoritative); the
`tui_unverified_platform` fallback (Windows TUI delivery is proven on Tabo
instead).
