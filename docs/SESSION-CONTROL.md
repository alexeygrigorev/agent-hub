# Hub session control (M12e) — implemented contract

Status: **implemented in this worktree** (`src/session_control.ts` + narrow
`src/hub.ts` seam + `host/agent_hub_host/session_control.py` + narrow
`host/agent_hub_host/cli.py` wiring + SQLite/HTTP tests). Interface provisional
until the sibling host worker confirms the receiver-proof half; the proof shape
below is the exact proposal sent to head.

Goal: usable `hub sessions` API + public CLI for list / start
(engine/machine/host/cwd/brief) / send with confirmed delivery / capture /
stop, replacing `psc`. Workers prove with synthetic/local sources only; head
handles authorized physical qualification and independent review later.

## Non-goals and holds

- No second dispatcher, scheduler, account oracle, or duplicate fleet consumer.
  Start reuses the existing `FleetService.reserve` (+ routing where applicable).
- No raw native source edits; native adapters are read-only holds here.
- No routing/fleet/auth/schema/jobs/claim policy edits. New tables only
  (`session_control_*`, additive migration in `SessionControlService.init`).
- M6/M8 gates are reused, never bypassed: unqualified launches are held
  honestly (`held` / `no_eligible_route`), never forced.
- Fixture-to-runtime completion is never claimed. HTTP success, PTY bytes, or
  native-inbox persistence alone are NOT proof the target read/processed
  anything.

## State model

Session (hub view of one fleet launch + command cursor):

- `queued` — intent recorded, no fleet reservation yet.
- `held` — placement refused or unavailable; honest hold with the fleet reason.
- `refused` — fleet rejected the intent as invalid (client error, 400).
- `offered` — fleet reservation exists, host has not bound a native session.
- `starting` — reserved for the fleet claim/result mirror (accepted pre-state).
- `running` — verified native binding bound (exact UUID + tag + workspace).
- `uncertain` — reserved; an effect may have happened, reconcile first.
- `stopped` — confirmed native exit observed.
- `failed` — reserved terminal.

Command (one durable intent: `send`, `capture`, `stop`):

- `recorded` — durably stored, not yet fetched by the host.
- `transported` — fetched by the bound host (poll receipt) or host-reported
  PTY-bytes-only. NEVER delivery/processing proof.
- `claimed` — atomic execution fence held by one machine + claim token.
- `acked` — correlated receiver confirmation verified (see proof below).
- `refused` — host or native layer refused with a reason; never retried blindly.
- `uncertain` — attempt outcome unknown (expired/lost claim); retained until
  reconciled, never re-offered, never blindly reissued.

Every read distinguishes these. `recorded` never renders as delivered;
`transported` never renders as processed.

## Endpoints (all `Cache-Control: private, no-store`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/sessions` | admin, or agent with `fleet:launch` | Record start intent; delegates placement to existing fleet reserve |
| GET | `/api/sessions?limit&cursor` | same as above (owner-scoped for agents) | List sessions, newest first, cursor-paginated, complete |
| GET | `/api/sessions/:id` | owner or admin | Show session + binding |
| POST | `/api/sessions/:id/send` | owner or admin | Enqueue one prompt-delivery command |
| POST | `/api/sessions/:id/capture` | owner or admin | Enqueue one bounded capture command |
| POST | `/api/sessions/:id/stop` | owner or admin | Enqueue one stop command |
| GET | `/api/sessions/:id/commands?limit&cursor` | owner or admin | List commands with per-command state, cursor-paginated |
| POST | `/api/sessions/:id/binding` | host credential, machine-bound | Report the exact observed native binding (Fleet result -> running) |
| GET | `/api/session-commands/offers` | host credential, machine-bound | Poll pending commands for this machine (fetch receipt only) |
| POST | `/api/session-commands/:command_id/claim` | host credential, machine-bound | Atomic execution fence BEFORE any native effect |
| POST | `/api/session-commands/:command_id/ack` | host credential, machine-bound | Report correlated outcome (claim holder only) |

UUIDs are `crypto.randomUUID()`; ids in paths match
`^[a-f0-9-]{36}$`. All POST bodies are bounded JSON (32 KiB).

### POST /api/sessions (start)

Request: `key` (required idempotency label per `(actor, key)`), `workspace`
(required label), `engine` / `machine` (optional labels, REAL fleet route
constraints), `brief` (optional headless prompt, 1..8000 chars), `mode`
(`headless`|`interactive`, interactive starts empty), `access` (optional
claim-admission declaration passed through to fleet).

`tier` is rejected with `400 unsupported_tier` BEFORE any effect: fleet
`plan()`/`reserve()` accept a fixed key set with no tier consumer
(`src/fleet.ts` plan keys), and inventing a tier-to-placement mapping would be
a new provider dispatcher, which is out of scope. Select `engine`/`machine`
explicitly instead.

`cwd` is rejected with `400 unsupported_cwd` BEFORE any reservation: fleet
plan strictly rejects unknown keys, and the host cannot retroactively change
a launched cwd, so carrying cwd in a side table would NOT make the existing
fleet start honor it. Launches happen at the workspace root until an actual
existing start adapter consumes the constraint. The `cwd` field stays in host
offers (always null today) for that future adapter; the hub never claims an
executed cwd from the binding.

Behavior:

1. Same-transaction idempotency replay on `(actor, key)`: identical canonical
   intent returns the original session; conflicting payload returns 409.
2. The service calls the existing fleet `reserve` with the exact fleet key set
   `{mode, machine, engine, workspace, prompt, idempotency_key, access}`.
   Fleet client errors (400) pass through as 400 with the session marked
   `refused`; placement/admission failures (409) become an honest `held`
   session carrying the fleet reason plus the requested engine/machine.
3. On success `{ session_id, fleet_launch_id }` are linked atomically; the
   fleet reserve stays idempotent on its derived key, so replays and crash
   recovery cannot double-launch. Without a reserve hook the intent is held
   with `no_placement_authority`.
4. `cwd` is NOT sent to fleet (rejected key there). Non-root values are
   rejected with `400 unsupported_cwd` before any reservation; the offer
   field is reserved for the future start adapter that consumes it.

### Claim / ack discipline (one-time attempt fence)

1. Host polls `offers` (marks `recorded` -> `transported` fetch receipt only).
   Offers are authorized PER SESSION before any payload disclosure or state
   mutation: the presenting host credential must be bound to the session
   machine, its live family must match the machine registry's current family
   (when enrolled) and the adopted session host family. A foreign or newly
   re-enrolled family sees nothing and transports nothing; a deleted machine
   offers nothing. The claim-expiry sweep honors the same gate.
   Offers carry `command_id`, `kind`, `generation`, `correlation`, `payload`,
   plus `binding` with `native_id` (target UUID), `tag`, `workspace`,
   `generation`, and `native_created_at_ms` (incarnation; null means unbound).
   The receiver maps kind/payload EXACTLY to the host function op/body/
   NativeTarget (`send {body}` -> op send + `await_ack`; `capture {max_bytes}`
   -> limit/mode; `stop {}` -> op stop) via `session_receiver.receive_once`
   (`hub sessions host-receive --once`); `start` is rejected (Fleet-owned).
2. Host claims ONE command: `POST .../claim {correlation, attempt_id}` where
   `attempt_id` is the host's own journaled execution-attempt nonce (one per
   command per local mutex, `recv-<command_id>.json` under flock BEFORE any
   claim). The grant is atomic compare-and-swap to
   `claimed` with holder machine, fresh unpredictable claim token, generation,
   expiry (default 5 min, returned as BOTH `expires_at` ISO canonical and
   `expires_at_ms` int for the host clock), and attempt counter. Reclaim with the SAME attempt id
   replays the token (lost-reply reconciliation by the same attester); any
   other attempt id on the same machine gets `409 already_claimed` and a
   rival machine gets `409 claim_conflict` — one claim authorizes exactly
   one effect, never another executor. Expired claims become `uncertain`
   and are never re-offered.
3. Host executes ONLY after the claim, re-verifying the exact binding
   (native UUID + tag + workspace + generation + native_created_at)
   immediately before the effect.
4. Host acks with `{correlation, outcome, native, claim_token, ...}` where
   `native` is ALWAYS the target native session triple
   (`native_id` = bound native session UUID, never an envelope id). Only
   the live claim holder may ack; an ack after lease expiry marks the command
   `uncertain` (the effect may have happened) and is rejected with
   `claim_expired` — never marked delivered, never reissued.
   A SEND whose recipient reply has not arrived yet is NOT acked at all on
   the first cycle: the host journals the durable attempt with the original
   native envelope id and reconciles read-only (same claim token, no second
   send) while the lease lasts — and, once the lease has lapsed, after it:
   a delayed delivered proof on the SAME attempt (token + correlation +
   current binding + fresh authority) still reconciles an uncertain send to
   `acked` with no new effect and without re-opening claiming. Proof-less
   outcomes stay terminal. Premature `uncertain` would lock the server
   command (`409 command_uncertain`) and strand the delayed reply.
5. Lost ack responses are repaired by IDENTICAL re-submit, never by a new
   effect: the host persists the ack payload and its submission state
   atomically with the outcome and re-POSTs it verbatim under the same claim
   token (discovered via its journal, since claimed/acked rows are never
   re-offered). The server records the claim token in `ack_json` and replays
   success for a byte-identical re-submit (same token/correlation/outcome/
   native/proof/capture/exit); each native reply envelope still confirms at
   most one command. Anything else on a finished row keeps the terminal
   errors. A terminal server verdict (HTTP 409) on re-submit closes the
   journal as `uncertain` without a new effect.

### Binding reconciliation (Fleet-canonical)

`POST /api/sessions/:id/binding` reconciles the reported native session
against the canonical assigned Fleet launch (read-only; Fleet owns the
launch lifecycle): the launch row must exist on the session's machine in a
live post-start state (`starting`|`running` — `offered` is unstarted,
`failed`/`exited`/`uncertain` are not bindable), the tag must be the exact
Fleet consumer tag for that launch (`hub-<fleet_launch_id>`), an
already-reported launch native id must match, AND the existing Fleet sessions
report must already carry this exact native identity (same machine + UUID +
tag + workspace, non-terminal phase). Binding before the Fleet consumer
reports the native identity is refused (`fleet_native_unreported`); a
reported tag/workspace mismatch or a terminal native phase is refused as
well. The launch row alone never proves the tag/workspace mapping, so an
arbitrary workspace is never adopted on a launch id plus an unreported UUID.
The binding workspace (absolute native path) is deliberately NOT compared to
the Fleet workspace label — different domains (hub label vs. canonical native
path) — so machine + tag + native id + state + the sessions report carry the
reconciliation instead, and the host still re-qualifies
the exact target (UUID + tag + workspace + incarnation) before every effect.

### Correlated delivery proof (host-verified native reply provenance)

Deliberately NOT cryptographic target authentication: the hub cannot verify
native envelopes server-side. The host retrieves the native log/envelope
itself, verifies the reply references the delivered message, and attests the
provenance below. The hub checks shape, exact sender binding, reply linkage,
and global envelope uniqueness. A bare object echoing the public
correlation with a non-empty id is NOT proof; absent proof keeps the command
at recorded / transported / uncertain. SEND `delivered` requires `proof`
(else `409 proof_required`); `transported` is the honest outcome for
PTY-bytes-only delivery. The validator (`validateSendProof`, unit-tested)
enforces:

```json
proof: {
  "native_message_id": "<original native envelope id of the delivered message>",
  "reply_to_native_id": "<native envelope id of the target reply referencing it>",
  "reply_digest": "<sha256 hex of the reply envelope as observed by the host>",
  "sender_native_id": "<exact native sender UUID>",
  "sender_tag": "<exact native tag>",
  "sender_workspace": "<absolute canonical native workspace>",
  "sender_generation": 1,
  "sender_created_at_ms": 1792223000000
}
```

Rules: the reply envelope must differ from the message envelope; digest must
be 64-hex; the sender triple plus generation plus `native_created_at_ms`
must equal the session binding (binding without `native_created_at_ms` is
rejected, so incarnation evidence is mandatory); each reply envelope
confirms at most one command (`session_control_proofs` primary key — reuse
across commands is `409 proof_replayed`, while a duplicate proof for the
SAME command replays success for lost-ack repair). The sibling host worker supplies the receiver
mechanism (reply-to vs. log scan) and journals `attempt_id`; this shape is
the shared agreement point. The host forwards the reply's ACTUAL sender
creation value; the hub rejects a mismatch rather than accepting an
overwritten expectation.

Capture output is projected on the host BEFORE upload, bounded
(`max_bytes` 1..65536), server-redacted with the existing routing `redact`
(routing regexes cannot prove arbitrary terminal secrets removed — the doc
and CLI never claim redaction proves privacy), and responses flag
truncation. No raw archives, no foreign-session capture.

Stop verifies the intended native exit: `delivered` on a stop requires
`confirmed_exit: true` with the exact bound native triple before the session
reaches `stopped`.

### Ownership and credentials

Owner actor + credential token id + credential family are stored on every
session/command row. Every read/write/replay/claim/ack re-authorizes against
LIVE registry state inside the current transaction (after any awaited body):
admins bypass; owners must present the owning actor with a live credential in
the owning family — a matching token id alone never authorizes, and agent
callers must additionally resolve a live registry incarnation
(`credentialActor`). Revoked/expired presenting credentials fail closed;
same-family refresh keeps working and adopts the rotated token id;
delete-and-reregister (new family, same actor) renders as `not_found`, never
an upgrade. Host endpoints additionally require the session machine to equal
the credential machine, the machine row to still exist in `machines`, and —
when the enrolled credential resolves to a known family — the presenting
family to equal that CURRENT family (a superseded pre-bind family cannot
hijack the first binding and lock out the enrolled machine), plus
host-family continuity once adopted. The recorded ack carries the authorizing
claim token so identical lost-ack re-submits are recognizable.
Cross-origin/cookie/CSRF/mixed-auth handling is identical to the intake
mount (same helper shape, narrow operator-token route scope, no global
`BusAuth` changes).

### CLI (public `hub` only, same credentials)

```sh
hub sessions list [--state running] [--limit 50]
hub sessions start --workspace demo [--engine codex] [--machine hetzner]
    [--cwd demo/project] [--brief-file goal.txt | --interactive] [--key k1]
    [--wait] [--timeout 300] [--interval 5]
hub sessions send <session-id> --body-file prompt.txt [--key k2] [--wait ...]
hub sessions capture <session-id> [--max-bytes 65536] [--key k3] [--wait ...]
hub sessions stop <session-id> [--key k4] [--wait ...]
hub sessions show <session-id>
hub sessions commands <session-id>
hub sessions host-receive --once --machine-id hetzner [--actor machine:hetzner]
    [--credential-family host:hetzner] [--journal-dir ABS] [--aplexer-bin aplexer] [--limit 20]
```

`--wait` polls boundedly to a terminal state (session: running/stopped/...;
command: acked/refused/uncertain — `transported` alone never completes the
wait), following server `next_cursor` pages (bounded, id-deduped) so commands
past the first page and state-filtered listings are complete. Every mutation prints its `client_key`; after a lost response, retry
with the SAME `--key` (replay vs. 409 conflict), never an unkeyed retry. No
silent drops; HTTP 201 (queued) is never rendered as delivery. Errors never
echo prompt text, tokens, or server-controlled bodies. `--tier` is accepted
by the parser and passed through so the server's explicit `unsupported_tier`
surfaces (no silent hint).

## Tables (new, additive — no existing schema edits)

- `session_control_sessions(id, actor, request_key, request_json, fleet_launch_id,
  machine_id, engine, mode, state, binding_json, owner_token_id, owner_family,
  host_family, created_at, updated_at, UNIQUE(actor, request_key))`
- `session_control_commands(id, session_id, kind, actor, request_key,
  payload_json, state, generation, correlation, poll_at, ack_json,
  owner_token_id, owner_family, holder_machine, claim_token,
  claim_expires_at, holder_attempt, attempts, created_at, updated_at,
  UNIQUE(session_id, actor, request_key))`
- `session_control_proofs(reply_envelope_id PRIMARY KEY, command_id,
  session_id, created_at)` — each native reply confirms at most one command.

Caps: 2000 pending commands globally, 200 per session, 20 offers per poll,
100 rows per page with `next_cursor`; refusals are `429 queue_full` /
`session_queue_full` without evicting pending work. Retention and privacy
follow intake conventions (`private, no-store`, redacted captures, bounded
rows, complete pagination).

## Acceptance (source only; physical gates stay with head)

- Real SQLite/HTTP tests (`test/session-control.test.ts`, 26 cases):
  auth/unscoped, input validation, tier-before-placement, cwd-before-
  reservation (`unsupported_cwd`), honest hold, idempotent start/conflict,
  constraint passthrough (fleet-exact keys), fleet-400 passthrough, held
  replay, unbound refusal, send idempotency, owner isolation,
  refresh-family continuity vs. rebound rejection, HTTP binding route
  (mandatory created_at, conflict/foreign/UUID-shape), null cwd in offers,
  poll-once, one-time claim fence (rival/correlation/token/already_claimed),
  expiry-to-uncertain with no re-offer, native-envelope proof (required +
  foreign/stale/self-reply/digest + cross-command replay rejection),
  stop-confirmed-exit, refused/uncertain retention, capture redaction,
  binding-conflict on tag reuse, cursor pagination completeness, queue caps.
- Host CLI tests (`host/tests/test_session_control.py`, 11 cases): paths,
  headless/interactive, payloads, error hygiene, bounds, wait validation,
  start/send wait semantics (transport never completes), timeout + keyed
  reconciliation, list filtering.
- `tsc --noEmit` clean. No test starts, reads, or stops a live agent.
