# Hub session control (M12e) — implemented contract

The public Hub CLI/API and Hetzner host are live. A real Antigravity session
passed start, guarded original-envelope delivery with a correlated model reply,
public capture, and confirmed stop on 17 October 2026. Transport alone remains
insufficient: the send command reaches `acked` with outcome `delivered` only
after the host verifies the recipient reply.

The accepted fixture was Hub `6e9771ef-ed17-4e40-bf4a-ba39e52075b3`,
send command `106c16ae-26f6-4124-a533-c4c8e6c7a1d6`. Its ordinary user message
requested a short reply; the native reply matched the original envelope and
observed correlation/body digest. No command approval or task input was needed.
Earlier failed attempts remain recorded; this acceptance does not establish
M8 role execution, cross-host reconnect, or retired-worktree cleanup.

## Qualified Antigravity startup

Keep the quota service/account separate from the actual native engine. The
quota service can be `gemini` while the engine is `antigravity`; all routes
sharing that subscription must use the same account pool. Launcher `1867b85`
records `antigravity` for new Antigravity native sessions. Existing bindings
retain their original engine and incarnation.

A trusted custom `startup_argv` replaces the native engine command. Preserve
the configured engine's unattended permission policy in that argv; otherwise
tool commands can wait on approval even though delivery succeeded. The qualified
per-session Antigravity command uses `--dangerously-skip-permissions`, without
changing global settings. Keep the host's existing resource/admission wrapper.

The interactive startup runs one genuine initialization turn through
`--prompt-interactive`, then waits. Native PreInvocation/Stop hooks report its
real lifecycle; neither capture nor a manual state report substitutes for them.
The initialization briefing also teaches the internal receipt protocol:

> Respond READY for initialization without tools. For later native Hub messages,
> read the received envelope by its exact native ID with `aplexer message show
> ID --json`. If `data.hub_session_control` exists, reply to that native ID using
> `--data` JSON containing the observed `correlation_id` and `body_digest`.
> Keep those fields exact. The reply body is the normal response; acknowledging
> receipt does not claim task completion. Do not read an inbox during initialization.

This protocol belongs in the trusted agent startup, so human task text does not
need to carry implementation fields. Use the actual `antigravity` engine;
recording an `agy` process as `gemini` applies a different native readiness policy
and can reject harmless Antigravity terminal redraws after its Stop hook.

```bash
hub sessions start --machine YOUR_MACHINE --workspace YOUR_ALLOWED_WORKSPACE \
  --engine antigravity --interactive --name developer-task \
  --key developer-start-001 --wait --timeout 180
hub sessions send developer-task --body-file ./task.txt \
  --key developer-send-001 --wait --timeout 120
hub sessions capture developer-task --screen --key developer-capture-001 --wait
hub sessions stop developer-task --key developer-stop-001 --wait --timeout 90
```

Keep each request key/payload when reconciling uncertain responses. Headless
intake assignments run their brief through the maintained launcher; the example
above starts an interactive session for subsequent send/capture/stop.

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
(required: a configured label, or an explicitly allowed absolute POSIX path —
see below), `engine` / `machine` (optional labels, REAL fleet route
constraints), `brief` (optional headless prompt, 1..8000 chars), `mode`
(`headless`|`interactive`, interactive starts empty), `name` (optional
friendly name, `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; stored in `request_json`,
shown in list/show, never identity), `access` (optional
claim-admission declaration passed through to fleet), `intake_task_id`
(optional linked intake task id; when present the start is refused with `400
acceptance_criteria_required` if that task's acceptance criteria are missing
or whitespace-only, or `404 unknown_intake_task` for an unknown id — checked
server-side before any Fleet placement or session row; part of start
idempotency. Starts without it are unlinked and unaffected).

`tier` resolves through the EXISTING model-routing table inside fleet plan
(configured tier membership filters eligible engine/profile routes; the
existing score order still decides — no new ranking or dispatcher). Unknown
tiers are rejected with `400 invalid_tier`; a tier with no eligible route
becomes an honest held session, never a silent upgrade. An explicit engine
outside the tier simply matches nothing (`no_eligible_route`).

`cwd` is consumed end to end: validated here as a workspace-relative
confined path, carried through the fleet reserve into the launch offer,
resolved by the host against allowlisted canonical roots with symlink
confinement (`resolve_offer_cwd`), passed to the installed
`a start --cwd`, and verified against the actual native catalog at binding
(`fleet_cwd_mismatch` on drift). The hub never claims an executed cwd it
cannot see; unreported catalog cwd holds honestly. Omitted tier/cwd arrive
at fleet plan as null (absent), never as malformed values.

Behavior:

1. Same-transaction idempotency replay on `(actor, key)`: identical canonical
   intent returns the original session; conflicting payload returns 409.
2. The service calls the existing fleet `reserve` with the exact fleet key set
   `{mode, machine, engine, tier, workspace, cwd, prompt, idempotency_key,
   access}` (tier/cwd are REAL placement constraints now, filtered/mapped by
   existing policy, never silently discarded).
   Fleet client errors (400) pass through as 400 with the session marked
   `refused`; placement/admission failures (409) become an honest `held`
   session carrying the fleet reason plus the requested engine/machine.
3. On success `{ session_id, fleet_launch_id }` are linked atomically; the
   fleet reserve stays idempotent on its derived key, so replays and crash
   recovery cannot double-launch. Without a reserve hook the intent is held
   with `no_placement_authority`.
4. Post-body authority inside the mutation txn: after `await body(request)`,
   creation re-verifies `agentLive` + non-null `presentingFamily` (non-admin),
   binding re-verifies `hostOk`, and every send/capture/stop enqueue
   re-reads the live session row AND `ownerOk` before inserting. A
   credential revoked, expired, rebound, or re-enrolled while the body
   streamed fails closed with no row, no NULL family stamp, and no
   state-machine violation (`test/session-control-r3.test.ts` R3-1..R3-3:
   streamed-body revoke races). Crypto token-hash prep stays async BEFORE
   the txn; all live credential/family/machine checks are synchronous
   INSIDE it — no awaited final-verifier gap.

### Absolute workspace identifiers (alongside labels)

A remote caller may address a workspace as an absolute POSIX path
(`hub sessions start --workspace /absolute/repo/path --engine opencode
--prompt "..."`). Labels keep their exact legacy meaning; paths are
additional identifiers, never replacements.

- Bounded canonical form on BOTH ends (server `src/fleet.ts` +
  `src/session_control.ts`, host `fleet.py`): starts with `/`, at most 512
  chars, no NUL/control chars, no backslashes, no `//`, no trailing slash,
  no `.`/`..` segments, segments confined to `[A-Za-z0-9._-]{1,64}`.
  Anything else is refused with 400 before any claim or native effect.
- No arbitrary path acceptance: the path must be EXPLICITLY configured in
  the trusted host settings (`workspaces` map, operator-owned private file)
  AND advertised in that host's capabilities `workspaces`. The server routes
  only exact capability matches (`capability_unavailable` otherwise, held
  honestly); the host's `_route` refuses unmapped offers before the fleet
  claim; host realpath/CWD confinement stays authoritative downstream.
  No auto-clone, no disk writes, no unknown-repository fallback.
- The claim-policy map accepts the same absolute entries (same validator),
  so existing role fences keep working: unconfigured paths fail closed.
- Host session reports already carry absolute native paths through the
  existing fleet-sessions metadata (no new registry or dispatcher).

### Friendly names and the Hub-name → native-tag → Hub-UUID mapping

- `--name` is display only. It rides in `request_json` (no schema change),
  is covered by start idempotency (same key + same name replays, same key +
  different name conflicts with 409), and is projected as `name` in list and
  show. Names are never identity or authority.
- Exact mapping, no guessing: a Hub session started through fleet binds the
  canonical native tag `hub-<fleet_launch_id>` (interactive dispatcher argv)
  or `task-hub-<fleet_launch_id>` (maintained launcher aplexer backend) to
  the exact native UUID + `native_created_at_ms` incarnation. List/show
  expose all three (`id`, `binding.tag`, `binding.native_id`).
- Convenience alias only: `show`/`commands` (GET) and `send`/`capture`/`stop`
  (POST) accept an exact friendly name in place of the Hub UUID. Resolution
  is actor-scoped (admins resolve within their own actor too), exact-match,
  single-hit only: unknown names are `not_found` (404, never an oracle),
  ambiguous names are `ambiguous_name` (409, refused — use the UUID), and a
  native tag is NEVER a name (no tag-guessing, no foreign-session control,
  never latest-wins). The host binding route stays strict Hub UUID.

### Observed (non-Hub) native sessions: read-only inventory, no adoption

"Can it list/send/capture sessions that psc started?" Honestly: the fleet
inventory (`fleet_sessions` reports) shows ACTUAL native sessions, including
ones other coordinators or Alexey started — but there is NO verified Hub
binding for them (no fleet launch, no UUID/tag/incarnation reconciliation),
so they are read-only observations. The hub never fakes a binding, never
claims ownership of another coordinator's or Alexey's sessions, and control
routes refuse them. Adoption would require a genuine Hub start (or an
explicit, reviewed adoption flow that does not exist); it is not claimed.

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
   (`native_id` = bound native session UUID, never an envelope id).    Only
   the live claim holder may ack; an ack after lease expiry marks the command
   `uncertain` (the effect may have happened) and is rejected with
   `claim_expired` — never marked delivered, never reissued — EXCEPT a send
   carrying a valid delivered proof on the SAME live claim, which reconciles
   through `lateReplyAck` whether the row is already `uncertain` or still
   `claimed` with an elapsed lease: the verdict never depends on offers-sweep
   ordering (409s stay classified by code + command/attempt/proof: same-
   command lost-response idempotency converges, `proof_replayed` for another
   command is terminal and kept as a durable human-obligation fact).
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
tag + workspace + engine + cwd, non-terminal phase). Binding before the
Fleet consumer reports the native identity is refused
(`fleet_native_unreported`); a reported tag/workspace mismatch or a terminal
native phase is refused as well. A known engine mismatch hard-rejects with
no mutation (`fleet_engine_mismatch` — a real SHELL row never binds a
requested model session); the requested cwd must match the reported catalog
cwd exactly or as trailing segments (`fleet_cwd_mismatch` otherwise;
unreported catalog cwd holds honestly). The launch row alone never proves
the tag/workspace mapping, so an arbitrary workspace is never adopted on a
launch id plus an unreported UUID.
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
hub sessions start --workspace demo|/absolute/repo/path [--engine opencode] [--machine hetzner]
    [--cwd demo/project] [--brief-file goal.txt | --prompt "..." | --interactive] [--name friendly] [--key k1]
    [--intake-task-id TASK_ID] [--wait] [--timeout 300] [--interval 5]
hub sessions send <session-id|name> --body-file prompt.txt [--key k2] [--wait ...]
hub sessions capture <session-id|name> [--max-bytes 65536] [--screen] [--key k3] [--wait ...]
hub sessions stop <session-id|name> [--key k4] [--wait ...]
hub sessions show <session-id|name>
hub sessions commands <session-id|name>
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

- Real SQLite/HTTP tests (`test/session-control.test.ts`, 30 cases):
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
  binding-conflict on tag reuse, cursor pagination completeness, queue caps,
  absolute workspace acceptance/traversal refusal, name store/projection/
  idempotent replay, actor-scoped exact alias (ambiguous 409, foreign 404,
  never tag-match).
- Fleet placement (`test/fleet.test.ts`, 23 cases, incl. absolute-path
  routing when advertised, malformed-path refusal before claim/effects,
  unadvertised-path `capability_unavailable` with no launch row, absolute
  claim-policy entries, full label regression).
- Host CLI tests (`host/tests/test_session_control.py`, 15 cases): paths,
  headless/interactive, payloads, error hygiene, bounds, wait validation,
  start/send wait semantics (transport never completes), timeout + keyed
  reconciliation, list filtering, `--prompt` inline goals (mutual exclusion,
  8 KiB UTF-8/char bounds, blank refusal) and `--name` validation/passthrough.
- Host fleet (`host/tests/test_fleet.py`, 20 cases, incl. absolute workspace
  keys accepted/malformed refused and explicit-mapping-only routing).
- `tsc --noEmit` clean. No test starts, reads, or stops a live agent.

## Native session adoption & qualified aliases

Host-attested native aplexer sessions (interactive sessions, background work, task sessions) are observed via the maintained host catalog pipeline (`session_metadata` -> `PUT /api/fleet/sessions` and `POST /api/sessions/host-adoption`) and adopted into the canonical session control view:

1. **Exact incarnation**: Preserves native UUID (`id`), tag, workspace, integer `created_at_ms`, and engine. Machine-bound host authority ensures that only the authorized enrolled host machine can report observations or claims.
2. **Provenance & Controllability**:
   - `provenance`: `observed_native` (vs `canonical_hub` for Hub-started sessions).
   - `controllability`: `controllable` for active sessions (`running`, `starting`), `historical_read_only` for stopped sessions.
3. **Qualified Native Aliases (`<workspace_basename>:<tag>`)**:
   - Mapped from the canonical workspace path basename and native tag (e.g. `/home/alexey/git/dataops` + `batching` -> `dataops:batching`; `/home/alexey/git` + `crm-redesign` -> `git:crm-redesign`).
   - Exact match resolution only. Collisions or reused tags return `409 ambiguous_name` honestly without "latest wins"; callers fall back to the exact canonical native UUID.
   - Separate resolver preserving the friendly name validation (`--name` on start continues to exclude colons).
4. **Security & Authorization**:
   - Adopted sessions carry `actor = 'unknown'` with null owner tokens.
   - Admin human device credentials pass authorization and can view, send, capture, and stop adopted sessions.
   - Foreign agent credentials fail closed (`404 not_found` / `403 forbidden`) and cannot view or control observed native sessions.
   - Normal Hub-started sessions with `fleet_launch_id` remain completely unchanged.
5. **Absence & Freshness**:
   - Missing native records from a host snapshot alone are NOT exit proof. Records are never deleted or prematurely marked stopped on absent polls.
6. **Automated Cleanup Protection**:
   - Observed native sessions are permanently protected from automated cleanup scripts or scheduled stop apply passes.
7. **Lineage Tracking & Unified Tree (Addendum 7)**:
   - `hub sessions list` provides a compact table with columns `TYPE`, `NAME`, `ENGINE`, `STATE`, `WORKSPACE`, `UPDATED` (and `--json` for machine output).
   - `session_type`: distinguishes `aplexer` interactive sessions from `headless` agent runs.
   - `hub sessions start` supports `--parent <session_id>` to record verified parentage. Server enforces existence, access control, and anti-cycling checks (returns HTTP 400 `parent_cycle_detected`).
   - Lineage hierarchy is rendered via `hub sessions tree` (or `hub sessions list --tree`) and available at `/sessions` dashboard (`/sessions.html`) with interactive filtering.



## Captures retained for owned cleanup

An explicit authenticated capture also stores its last successful bounded payload
in the private host journal, outside the session worktree. The archive is keyed
by host and exact native UUID/incarnation/tag/workspace/engine, with a command ID,
original capture time, byte bound and SHA-256. It is not uploaded in status.

Retired cleanup may use this existing capture only after native retirement is
independently verified and all current custody, obligation and Git guards pass.
Its capture time, native finish time and later evidence-read time stay distinct.
Absent or corrupt evidence still retains the worktree. This does not recover
history for sessions that retired before any capture was archived.


## Cancel an unused held intent

`hub sessions cancel SESSION --key STABLE_KEY` cancels your own held start
intent only when it has no Fleet reservation, native binding or commands. A
durable `cancelled` state and `no_native_start` receipt preserve its history.
The same key replays the receipt; a different key conflicts. Original start
replays cannot revive it, and retry refuses it. Intake can then assign the
unfinished task again. The owning agent or human bearer must still be authorized at commit
time; admin access cannot cancel someone else's intent. Browser bus identities
that collapse to `admin` cannot prove individual ownership and are refused;
use your human device-login CLI profile.

Use `sessions stop` for a session that reached Fleet or has a native binding.
Cancellation never stops a workload, removes a worktree or proves native exit.

MCP exposes the same guarded operation as `sessions_cancel` with arguments
`{"id":"SESSION","payload":{"key":"STABLE_KEY"}}`. It re-enters ordinary
Hub authentication and returns the API cancellation receipt.

### Protecting sessions and cleaning up human-created workers

`hub sessions protect ID_OR_NAME --key KEY` permanently protects an owned session from automated cleanup. The same key replays its receipt; another key conflicts. There is no unprotect operation. MCP `sessions_protect` uses the same API. Protection never sends a prompt or stops a session. A host observation cannot clear it.

A human-created headless worker may be eligible for cleanup when its canonical Hub intent, headless Fleet launch, exact native binding and original human credential provenance agree. Human ownership remains visible separately from workload protection. Interactive, observed, explicitly protected, contradictory and unknown human workloads stay protected. Completion, no pending obligations, containment, retention and clean/merged Git checks still apply.

Cleanup requires a current credential from the creator's exact family, including after ordinary refresh. A different family with the same human actor has no cleanup authority. Older rows may recover family identity only from their immutable creator credential; missing provenance fails closed. Browser-only admin authority cannot establish individual family custody. The host claim repeats credential-family and protection checks. If authority changes after a claim and a host reports removal, the durable outcome records uncertainty rather than an authorized cleanup.

### Start interactive goal work

`hub sessions start --workspace agent-hub --with-goal --prompt 'Fix the repeated binding failure' --key binding-goal-1`

`--with-goal` implies interactive mode and requires `--prompt` or `--brief-file`.
It refuses `--headless`. Automatic placement selects a Codex-family route;
`--engine` remains an operator override requiring `--reason`. Fleet accounts
and engine routes advertise `supports_goal`; Codex, Codex/Orin and ZCodex
support goals, while other engines report false. Existing disabled-provider,
quota, resource and profile checks still apply.

The host starts the trusted local engine command with one literal `/goal …`
argument. It never runs goal text in a shell. Managed accounts keep their
own profile and protected launch wrapper. Unmanaged routes need a trusted
`startup_argv`. Ordinary interactive starts still open an empty prompt and
may use automatic placement without a machine pin.
