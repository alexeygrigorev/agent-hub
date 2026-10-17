# Hub session control (M12e) — PROPOSED contract, pending head interface ACK

Status: **proposal only**. No cross-worker host assumptions are implemented
until the head returns an explicit interface ACK. The durable queue, auth and
idempotency below are independently implementable; the host-execution seam
names the sibling worker's surface without assuming its internals.

Goal: usable `hub sessions` API + public CLI for list / start
(engine/tier/host/cwd/brief) / send with confirmed delivery / capture / stop,
replacing `psc`. Workers prove with synthetic/local sources only; head handles
authorized physical qualification and independent review later.

## Non-goals and holds

- No second dispatcher, scheduler, account oracle, or duplicate fleet consumer.
  Start reuses the existing `FleetService.reserve` (+ routing where applicable).
- No raw native source edits; native adapters are read-only holds here.
- No routing/fleet/auth/schema/jobs/claim policy edits. New tables only.
- M6/M8 gates are reused, never bypassed: unqualified launches are held
  honestly (`held` / `no_eligible_route`), never forced.
- Fixture-to-runtime completion is never claimed. HTTP success, PTY bytes, or
  native-inbox persistence alone are NOT proof the target read/processed
  anything.

## State model

Session (hub view of one fleet launch + command cursor):

- `queued` — intent recorded, no fleet reservation yet.
- `offered` — fleet reservation exists, host has not claimed.
- `starting` — host claimed, native receipt not yet verified.
- `running` — verified native receipt bound (native id + tag + workspace).
- `uncertain` — an effect may have happened; reconciliation required before
  any retry or dependent command.
- `stopped` — confirmed native exit observed.
- `failed` / `refused` — terminal without a running session.

Command (one durable intent: `send`, `capture`, `stop`):

- `recorded` — durably stored, not yet fetched by the host.
- `transported` — fetched by the bound host (poll receipt logged).
- `acked` — correlated receiver confirmation verified (see below).
- `refused` — host or native layer refused with a reason; never retried blindly.
- `uncertain` — attempt outcome unknown; retained until reconciled.

Every read distinguishes these five. `recorded` never renders as delivered;
`transported` never renders as processed.

## Endpoints (proposed, all `Cache-Control: private, no-store`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/sessions` | admin, or agent with `fleet:launch` | Record start intent; delegates placement to existing fleet reserve |
| GET | `/api/sessions` | same as above (owner-scoped for agents) | List sessions, newest first, paginated |
| GET | `/api/sessions/:id` | owner or admin | Show session + command cursor + binding |
| POST | `/api/sessions/:id/send` | owner or admin | Enqueue one prompt-delivery command |
| POST | `/api/sessions/:id/capture` | owner or admin | Enqueue one bounded capture command |
| POST | `/api/sessions/:id/stop` | owner or admin | Enqueue one stop command |
| GET | `/api/sessions/:id/commands` | owner or admin | List commands with per-command state |
| GET | `/api/session-commands/offers` | host credential, machine-bound | Poll pending commands for this machine |
| POST | `/api/session-commands/:command_id/ack` | host credential, machine-bound | Report correlated outcome |

UUIDs are `crypto.randomUUID()`; ids in paths match
`^[a-f0-9-]{36}$`. AllPOST bodies are bounded JSON (32 KiB).

### POST /api/sessions (start)

Request:

```json
{
  "key": "client-stable-key-001",
  "engine": "codex",
  "tier": "quick",
  "machine": "hetzner",
  "workspace": "demo",
  "cwd": "demo/project",
  "brief": "Short non-secret goal, <= 8000 chars",
  "access": { "mode": "edit", "claim_id": "…", "project": "…", "scopes": ["demo/project"] }
}
```

- `key` (label, required): idempotency key per `(actor, key)`.
- `engine` / `machine` (labels, optional): placement hints, same semantics as
  fleet plan inputs.
- `tier` (optional, `quick|daily|deep`): informational; model selection stays
  with existing routing policy. Unknown tiers are rejected, never guessed.
- `workspace` (label, required): configured host workspace label.
- `cwd` (optional): slash-separated path *relative* to the workspace root,
  `..`-free, max 256 chars. The server checks syntax only; the host resolves
  strictly inside its configured root and rejects traversal/escaping symlinks.
- `brief` (optional): prompt text for headless starts, max 8000 chars.
  Interactive starts MUST omit `brief` (empty native prompt rule, as fleet).
- `access` (optional): claim-admission declaration, passthrough to
  `FleetService.reserve`. Edit mode without `approved_action` is rejected the
  same way fleet rejects it.

Behavior:

1. Same-transaction idempotency replay on `(actor, key)`: identical canonical
   intent returns the original session; conflicting payload returns 409.
2. Admission is evaluated exactly as `FleetService.reserve` evaluates it
   (same `evaluate` call, `enqueue` then `reserve` phases). A failed check
   creates neither session nor reservation.
3. On success the service calls the existing `reserve` with the mapped fleet
   input and persists `{ session_id == fleet_launch_id, mapping }` atomically
   with the reservation link. A crash between reserve and link is recovered by
   relinking the exact retained `request_key`, never by launching twice.
4. Placement failure returns `409 no_eligible_route` with the fleet `rejected`
   list and the admission `access` mode — an honest hold, not a start.

Response (`201`, or `200` replay):

```json
{
  "id": "…",
  "state": "offered",
  "machine": "hetzner", "engine": "codex", "mode": "headless",
  "access": "edit", "replay": false
}
```

Holds return `409` with `{ "error": "no_eligible_route", "rejected": […],
"access": "edit" }`; admission failures return the admission error verbatim.

### POST /api/sessions/:id/send

Request: `{ "key": "…", "body": "prompt text, 1..16000 chars" }`.
Rules: session must be `running` (bound); `starting`/`uncertain` commands are
rejected with `409 session_not_bound` — no blind sends into unverified
sessions. One command row per `(session_id, actor, key)`; same key + same body
replays, conflicting body is 409. Response `201 { "command_id": "…", "state":
"recorded" }`.

### POST /api/sessions/:id/capture

Request: `{ "key": "…", "max_bytes": 65536 }` (`max_bytes` 1..65536, default
65536). Same binding rule as send. Capture output is sanitized with the
existing routing `redact` before storage and before any response; raw history
archives are never uploaded. Response `201 { "command_id": "…", "state":
"recorded" }`.

### POST /api/sessions/:id/stop

Request: `{ "key": "…" }`. Allowed from `offered|starting|running|uncertain`.
Stop verifies the intended native exit: the host must report
`confirmed_exit: true` with the exact bound native id before the session
reaches `stopped`. An `uncertain` start cannot be blindly retried; stop on an
uncertain session reconciles first. Response `201 { "command_id": "…",
"state": "recorded" }`.

### GET /api/session-commands/offers (host poll)

Host credential with `host` scope; the server checks current credential,
registry incarnation and machine binding on every call. Returns at most 20
pending commands for launches assigned to that machine:

```json
{ "offers": [
  { "command_id": "…", "session_id": "…", "kind": "send|.capture|stop",
    "native_tag": "hub-<launch-id-short>", "workspace": "demo",
    "payload": { "body": "…" },
    "correlation": "cmd:<command_id>:gen:<generation>" }
]}
```

Fetching marks commands `transported` with poll-at timestamp in the same
transaction. `native_tag` is a TAG (native `message send --to` expects a tag,
never a UUID). `generation` is the hub-side binding counter incremented on
every (re)bind; the host echoes it back so stale tags cannot be confused after
tag reuse.

### POST /api/session-commands/:command_id/ack (host ack)

Request:

```json
{
  "correlation": "cmd:<command_id>:gen:<generation>",
  "outcome": "delivered|refused|uncertain",
  "native": { "native_id": "…", "tag": "…", "workspace": "…" },
  "reason": "optional refusal/uncertainty reason, <= 512 chars",
  "capture": "optional sanitized capture text, <= max_bytes"
}
```

- `delivered` requires the correlated receiver ACK (see below), not merely
  transport or inbox persistence. The server verifies generation match and
  native binding match before marking `acked`.
- `refused` carries a machine-readable reason (`busy_composer`,
  `nonempty_composer`, `unknown_composer`, `session_gone`, `admission_changed`,
  …). The command is terminal; the caller must issue a new keyed command.
- `uncertain` retains the command for reconciliation; no automatic retry.
- Only the machine that polled the command may ack it (machine binding).

## Correlated delivery and receiver ACK (contract with sibling worker)

The hub transport (this worker) guarantees at-most-once durable intents with
correlation ids. The sibling host worker (`native_sessions.py` /
`session_executor.py`) owns native execution and defines the exact native
proof shape. Minimum agreement proposed here for ACK:

1. The host executes the command only after re-verifying exact binding:
   native UUID + workspace + tag + generation, resolved immediately before
   the effect (no cached catalog rows across the effect boundary).
2. Delivery uses the supported native guarded path with a correlated receiver
   confirmation (native reply/log id referencing the correlation). Unrelated
   PTY activity or inbox persistence is never counted as the ACK.
3. Busy, non-empty, menu, draft, or unknown composers are refused
   (`busy_composer` etc.), never typed into. No stale-idle bypass.
4. Stop confirms the intended native exit (exact id gone / exit receipt)
   before `confirmed_exit: true`.
5. Any crash/timeout between effect and confirmation reports `uncertain` with
   the correlation preserved; reconciliation reads the native log for the
   correlation before any new attempt.

Open for sibling/head confirmation: exact native proof field names, receiver
correlation mechanism (reply-to vs. log scan), capture truncation markers,
and generation source of truth (hub counter vs. native record).

## CLI (proposed, public `hub` only, same credentials)

```sh
hub sessions list [--json] [--state running] [--limit 50]
hub sessions start --engine codex --workspace demo [--machine hetzner] [--tier quick]
    [--cwd demo/project] [--brief-file goal.txt | --interactive] [--key k1]
hub sessions send <session-id> --body-file prompt.txt [--key k2]
hub sessions capture <session-id> [--max-bytes 65536] [--key k3]
hub sessions stop <session-id> [--key k4]
hub sessions show <session-id> [--json]
hub sessions commands <session-id> [--json]
```

No new credential format, dependency, or global install. Errors never echo
prompt text, tokens, or server-controlled bodies.

## Tables (new, additive — no existing schema edits)

- `session_control_sessions(id, actor, request_key, request_json, fleet_launch_id,
  state, binding_json, created_at, updated_at, UNIQUE(actor, request_key))`
- `session_control_commands(id, session_id, kind, actor, request_key,
  payload_json, state, generation, correlation, poll_at, ack_json, created_at,
  updated_at, UNIQUE(session_id, actor, request_key))`

Retention and privacy follow intake conventions (`private, no-store`,
redacted captures, bounded rows).

## Compatibility notes

- `src/hub.ts` needs a narrow seam only: construct + route `/api/sessions*`
  and `/api/session-commands/*` to the new `SessionControlService`, mirroring
  how `fleetService.handle` is mounted. Owned by hub-zai — ACK required.
- `host/agent_hub_host/cli.py` needs a narrow `sessions` subparser + dispatch
  to the new `session_control.py`, mirroring `intake.execute`. Owned by
  platform task scope — ACK required.
- Auth reuses `authenticateBusRequest` + operator-token fallback exactly as
  the intake mount does; agent scopes unchanged (`fleet:launch` for starts,
  owner-match for commands, `host` for poll/ack).
- Full assignment/backfill/MCP/quota stay outside this scope but compatible:
  session ids are fleet launch ids, so routing/task links keep working.

## Acceptance (source only; physical gates stay with head)

- Real SQLite/HTTP tests: auth (admin/agent/host/revoked), idempotent
  start/send with same key, conflicting key 409, unknown session 404,
  unbound-session send 409, busy/refused + uncertain paths, correlated ack
  with generation mismatch rejected, stop requires confirmed exit, capture
  redaction + bounds, unavailable-machine hold without launch.
- Host CLI tests with a fake hub HTTP adapter: list/start/send/capture/stop
  wiring, retry-same-key no-op, error hygiene (no secret/prompt echo).
- No test claims a live agent started, read, or stopped anything.
