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

## Interactive vs headless sessions and follow-up choice

Sessions that expect follow-up sends must start interactive (`mode: interactive`, e.g. Codex with `/goal` via `--with-goal`).
In interactive mode:
- The session runs its initial brief/goal turn, fires `SessionStart` / `Stop` hooks reporting `working` then `idle`.
- When `idle`, aplexer delivers subsequent `sessions send` commands directly to the agent's input prompt, where they are executed and acknowledged within 60s.

Headless sessions (`mode: headless`, e.g. `codex exec`, `opencode run`, `-p`) run as one-shot commands without an active interactive input channel.
- Senders cannot inject input into a running headless execution; Hub fails fast with `send_unsupported_headless` rather than leaving sends stuck in `claimed`.
- Follow-ups to headless work must go through `codex exec resume <thread-id> <prompt>` to resume execution in a completed thread.

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
see below), `engine` / `engine_override_reason` / `machine` (optional labels, REAL fleet route
constraints; default start omits `engine` and goes through tier/policy with no
casual picker — an explicit `engine` is an override and REQUIRES non-empty
`engine_override_reason` explaining why, or the start is refused with `400
engine_reason_required`), `brief` (optional headless prompt, 1..8000 chars), `mode`
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
  bare native tag is never a friendly name (no tag-guessing or latest-wins).
  Adopted native sessions also support the exact qualified alias described
  below. The host binding route stays strict Hub UUID.

### Observed (non-Hub) native sessions: adopted and controllable

Hub may control any session, including manual PocketShell/psc sessions and
sessions started by Alexey or another coordinator. Session origin does not
prohibit sending input, stopping, or deleting a session; ordinary caller
authorization and exact native identity checks still apply.

The machine-authenticated host catalog (`PUT /api/fleet/sessions`) preserves
observations and resource/account accounting. Catalog labels alone do not
transfer custody or authorize locking a user's checkout. For a new external
native session, an authenticated admin submits its exact catalog tuple and
`machine_id` to `POST /api/sessions/host-adoption`. HTTP 202 means custody is
pending, not that adoption completed. The enrolled host polls that same path
with GET, verifies the actual native UUID, tag, workspace, engine and creation
time, saves immutable repository evidence, and locks the worktree with the
canonical session UUID. Only its exact receipt at
`POST /api/sessions/host-adoption/receipt` creates the canonical binding.
No duplicate Fleet launch is created. Unverified census-only adoption opens a
durable `needs_attention` item; existing canonical sessions are not relocked
by repeating an adoption request.
Running observed native sessions are reported as `provenance: observed_native`
and `controllability: controllable`; `active` and `starting` states are also
controllable. Other observed states report `historical_read_only`.

Authorized human admins can list/show, send, capture and stop these sessions
by native UUID or exact qualified alias (`<workspace_basename>:<tag>`).
Non-admin agent credentials retain their existing access restrictions. This
is the existing send/capture/stop contract, not a separate delete API.
Observed sessions remain protected from automated cleanup; permission to
stop/delete a session does not authorize deleting its worktree or repository.
See [NATIVE-SESSIONS.md](NATIVE-SESSIONS.md) for adoption and lifecycle details.

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
   `claim_expired` — never marked delivered, never reissued — EXCEPT:
   (a) a send carrying a valid delivered proof on the SAME live claim, which
   reconciles through `lateReplyAck` whether the row is already `uncertain`
   or still `claimed` with an elapsed lease: the verdict never depends on
   offers-sweep ordering (409s stay classified by code + command/attempt/
   proof: same-command lost-response idempotency converges, `proof_replayed`
   for another command is terminal and kept as a durable human-obligation
   fact); (b) a fenced terminal failure receipt on the SAME claim +
   correlation (`failed`/`recipient_not_ready` on positive cancellation
   proof, else `uncertain` with an inbox reason), which is accepted as
   terminal `failed`/`uncertain` instead of burning another lease. Accepted
   outcomes are `delivered` | `transported` | `refused` | `uncertain` |
   `failed` (`failed` maps to state `failed`; `send_unsupported_headless`
   refusals also map to `failed`).
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

### Send readiness, inbox waiting, and expired claims

- Pre-send readiness (AC4): `POST /api/sessions/:id/send` checks
  `fleet_sessions` for the bound target. A target counts as recently
  reported only with a non-empty `agent_state` AND a fresh `observed_at`
  (within 5 minutes, not in the future). Otherwise the send is queued with
  warning `target_no_reported_state`; with `require_ready: true`
  (`--require-ready`) it is refused with HTTP 409
  `target_not_ready`. Stale state never silently passes as ready.
- Inbox waiting (AC5): a send whose recipient reply has not arrived is
  journaled as pending with NO terminal ack. Under the live claim fence the
  host POSTs `POST /api/session-commands/:id/status` with
  `status: "waiting in inbox for recipient"` + host reason. `GET
  /api/sessions/:id/commands` projects that `host_status` instead of
  `claimed`, and `hub sessions commands` / `send --wait` print
  `waiting in inbox for recipient: <reason>`. The command stays claimed
  under its lease while bounded read-only reconciliation watches for the
  delayed reply; no second send is ever issued.
- Expired claims (AC3): if the lease burns while the envelope is still
  undelivered in the aplexer inbox (host posted the pending status), the
  server marks the command `failed` with `recipient_not_ready` and never
  re-queues it — no blind re-queue, no duplicates. Unclaimed commands, or
  commands with no stored inbox effect, remain eligible for bounded custody
  retry (same fence, max 3 leases). Host-side, the controller `message ack`
  does NOT prove cancellation (it advances a consumer cursor; no
  original-sender cancel exists in the published aplexer CLI). The host
  verifies removal via the message log and reports `failed` /
  `recipient_not_ready` only on positive proof (retract ok + log fetch ok +
  envelope absent); otherwise it reports `uncertain` with an explicit
  `recipient_not_ready_uncertain` reason and never re-sends. The server
  accepts that fenced terminal receipt on the same claim + correlation.

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
truncation. Captures require authorization and an exact native binding,
including for adopted sessions. No raw archives are uploaded.

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
hub sessions start --workspace demo|/absolute/repo/path [--engine opencode --reason 'Why this engine is needed'] [--machine hetzner]
    [--cwd demo/project] [--brief-file goal.txt | --prompt "..." | --interactive] [--name friendly] [--key k1]
    [--intake-task-id TASK_ID] [--wait] [--timeout 300] [--interval 5]
hub sessions send <session-id|name> --body-file prompt.txt [--key k2] [--wait ...]
hub sessions capture <session-id|name> [--max-bytes 65536] [--screen] [--key k3] [--wait ...]
hub sessions stop <session-id|name> [--key k4] [--wait ...]
hub sessions show <session-id|name>
hub sessions commands <session-id|name>
hub sessions wake <session-id|name> --at <ISO8601|now> (--prompt TEXT | --body-file F) [--key K]
hub sessions host-receive --once --machine-id hetzner [--actor machine:hetzner]
    [--credential-family host:hetzner] [--journal-dir ABS] [--aplexer-bin aplexer] [--limit 20]
```

### Scheduled wakes (waiting on an external event = register a Hub wake)

Waiting on an external event means registering a Hub wake; a passive timer
without a wake is not allowed for Hub sessions. There is no interim
"aplexer wake not installed" path: use `hub sessions wake` instead of a
transient `systemd-run`/sleep loop that nobody reads.

```sh
hub sessions wake my-session --at 2026-10-27T12:05:00+02:00 --prompt "Bank window opened, resume" --key wake-1
hub sessions wake my-session --at now --body-file ./result-summary.md --key watcher-1
```

- Stored durably in DO SQLite (`session_control_wakes`), linked to the
  session's intake task, surviving Worker/host restarts.
- While pending, the session reports `state: waiting_until` with
  `waiting_until` (ISO) in list/show/status/dashboard; it is not `stalled`
  and idle/nudge logic does not fire before the wake time.
- At due time the Hub delivers the prompt through the verified send path
  (a durable `send` command) and requires an ack. If it is not acked within
  10 minutes, or the session is gone, the Hub records a `missed_wake`
  `session_control_wake_alerts` row (surfaced in `wakes` listing), appends an
  intake note, and clears `waiting_until` so the idle-with-open-AC
  replacement logic resumes. A terminal owner's wake also enters the existing
  intake restart queue. Replacement still requires no live owner and fresh,
  exact native exit evidence; a missing or unknown census stays attention-only.
  A late verified acknowledgement or fresh output progress resolves the alert
  without deleting its history.
- Watchers wake their owner with `--at now --body-file <summary>` so
  evidence capture always ends by waking the owning session.
- REST: `POST /api/sessions/:id/wake {key, at, prompt|body}`,
  `GET /api/sessions/:id/wakes`. MCP: `sessions_wake`.

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
- `session_control_wakes(id, session_id, actor, request_key, wake_at_ms,
  wake_at, prompt, state, command_id, intake_task_id, owner_token_id,
  owner_family, created_at, updated_at, delivered_at, acked_at,
  UNIQUE(session_id, actor, request_key))` + `session_control_wake_alerts`
  (missed-wake needs_attention items + intake notes).

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

Host-attested native aplexer sessions (interactive sessions, background work, task sessions) remain visible through `session_metadata` -> `PUT /api/fleet/sessions`. New canonical adoption requires the explicit operator intent and host lock receipt described above; existing adopted sessions retain their identity and control contract:

1. **Exact incarnation**: Preserves native UUID (`id`), tag, workspace, integer `created_at_ms`, and engine. Machine-bound host authority ensures that only the authorized enrolled host machine can report observations or claims.
2. **Provenance & Controllability**:
   - `provenance`: `observed_native` (vs `canonical_hub` for Hub-started sessions).
   - `controllability`: `controllable` for `running`, `active` and `starting` observed sessions; `historical_read_only` for other observed states.
3. **Qualified Native Aliases (`<workspace_basename>:<tag>`)**:
   - Mapped from the canonical workspace path basename and native tag (e.g. `/home/alexey/git/dataops` + `batching` -> `dataops:batching`; `/home/alexey/git` + `crm-redesign` -> `git:crm-redesign`).
   - Exact match resolution only. Collisions or reused tags return `409 ambiguous_name` honestly without "latest wins"; callers fall back to the exact canonical native UUID.
   - Separate resolver preserving the friendly name validation (`--name` on start continues to exclude colons).
4. **Security & Authorization**:
   - Legacy adopted sessions retain their prior `actor = 'unknown'` and null owner tokens. New explicit adoptions retain the authenticated custody actor and credential; census-supplied owner labels are ignored.
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

Claimed headless launches missing both a Hub native receipt and host journal
are probed by their owning host once their launcher task is five minutes old.
The timeout only permits a probe. The maintained launcher lock must fence a
still-queued task before Hub records `failed` / `launcher_not_started`; a
winning or unknown native start keeps its reservation. `sessions stop` can
then cancel the proven no-start intent and replay its receipt, preserving the
failed Fleet attempt. An exact launcher binding plus confirmed native death
also recovers a lost finish receipt and forwards the bounded `hub-results`
report. A result file alone cannot prove exit. Recovery does not launch or
stop a native session, delete data, or clean a worktree.

Hub itself abandons a claimed headless launch that bound no native within
15 minutes (`worker_silent_start`, verification `unverified`); its unbound
session leaves `offered`/`starting` as `failed`. `sessions stop` applies the
same bound immediately, so a wedged claim no longer returns
`session_not_cancellable` once it is older than 15 minutes; the receipt is
`effect: no_native_start` with `launch: abandoned_unverified`, because the
abandonment is not exit proof. For 24 hours the abandoned launch stays a
reconciliation candidate for its owning host only. The host's later exact
receipt supersedes the abandonment: a fenced no-start becomes
`not_started`; a recovered completion (also when the running receipt landed
but the binding and finish were lost) is recorded as the finish with its
`hub-results` report and the session becomes `stopped`; a late native start
is tracked as `running` again, and if the operator already cancelled the
session a `launch`/`late_native_start` alert opens instead of stranding it.

MCP exposes the same guarded operation as `sessions_cancel` with arguments
`{"id":"SESSION","payload":{"key":"STABLE_KEY"}}`. It re-enters ordinary
Hub authentication and returns the API cancellation receipt.

### Protecting sessions and cleaning up human-created workers

`hub sessions protect ID_OR_NAME --key KEY` permanently protects an owned session from automated cleanup. The same key replays its receipt; another key conflicts. There is no unprotect operation. MCP `sessions_protect` uses the same API. Protection never sends a prompt or stops a session. A host observation cannot clear it.

A human-created headless worker may be eligible for cleanup when its canonical Hub intent, headless Fleet launch, exact native binding and original human credential provenance agree. Human ownership remains visible separately from workload protection. Interactive, observed, explicitly protected, contradictory and unknown human workloads stay protected. Completion, no pending obligations, containment, retention and clean/merged Git checks still apply.

Cleanup requires a current credential from the creator's exact family, including after ordinary refresh. A different family with the same human actor has no cleanup authority. Older rows may recover family identity only from their immutable creator credential; missing provenance fails closed. Browser-only admin authority cannot establish individual family custody. The host claim repeats credential-family and protection checks. If authority changes after a claim and a host reports removal, the durable outcome records uncertainty rather than an authorized cleanup.

### Worktree guard: never remove your own worktree (incident 95c1862c)

The 95c1862c session removed its own git worktree (`git worktree remove` +
branch delete) while its intake was still open, because it treated a shipped
increment as the final push. Nothing guarded the tree, and the stale
` (deleted)` kernel label then hid the recreated checkout. Hub now locks
every session worktree on start/adopt
(`git worktree lock --reason "hub-session <id> intake <intake-id|none>"`),
so plain `git worktree remove` and `git worktree prune` refuse while the
session is live or its intake is open.

Rules for every session brief (also appended to Hub-generated briefs):

- NEVER remove your git worktree yourself — not with `git worktree remove`,
  `git worktree prune`, `rm -rf`, or branch deletes. The lock refuses; do not
  work around it.
- Remove the worktree only after the linked intake has a DONE note with live
  proof of every AC; while blocked or waiting, keep it.
- Every automated cleanup path (owned STOP-bundled removal, orphan sweeper,
  resource/idle watchdog reaps, disk-cleanup scripts) skips worktrees that
  are locked, belong to a live session, or belong to a session linked to an
  open intake.
- The lock releases only when the session is terminal AND (no linked intake,
  or the linked intake has a DONE note / is archived).
- If a live session's worktree path disappears, Hub opens a needs_attention
  item and appends a note to the linked intake within 10 minutes.
- A cwd that keeps the stale ` (deleted)` label while the same path holds a
  valid worktree of the original repository is reported as
  `cwd_stale_path_recreated`. The host compares the original Git common-dir
  path and device/inode captured before start/adopt; another repository at
  the same path is not recovery. Legacy sessions without original identity
  evidence remain unverified until that evidence is available.
- The installed session host checks worktrees in a separate report-only
  observer every 60 seconds, using host-authenticated observations. Slow
  Fleet maintenance and an uninstalled optional orphan timer do not delay it.

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

### Long-running CLI warnings

`hub sessions list` and `hub sessions show <id>` (also `status`) expose
`long_running`, `runtime` and `idle_for` in seconds, `last_activity_at`, and
`activity_state`. A running session is long-running strictly after 3 hours;
no output for more than 1 hour is idle. Runtime starts at native creation when
available. Activity uses the native output checkpoint reported by the host
(including output from commits), falling back to creation if no output exists;
reading status or capturing output does not refresh activity.

Use `--threshold 4h` on list/show/warnings/ack, or set
`HUB_SESSION_WARNING_THRESHOLD=4h`, to override the 3h default. Durations accept
positive integer seconds (`s`), minutes (`m`), hours (`h`) or days (`d`).

`hub sessions warnings` lists unsnoozed warnings with the session ID, name/tag,
intake ID when linked, engine, timing and activity state, five output lines,
and an exact snooze command. Each output capture has a bounded wait; an
unreachable host is reported as unavailable without hiding its warning.
`--json` returns the same information as structured data.

`hub sessions ack <id> [--snooze 3h]` records the authenticated actor, time and
expiry in a durable `warning_ack` session event. The default snooze duration
is the selected threshold. Inspect acknowledgements with
`hub sessions events <id>`. Snoozing hides the warning from `warnings` until
expiry; list/show still expose the runtime flag and snooze expiry. These
operations never stop sessions or delete anything. Warnings are displayed
only in the CLI; no email, webhook or dashboard badge is generated.


### Headless tool permission failures

Admission and a native spawn do not prove that an engine can perform work.
The host checks bounded first-turn output for an exact owned launch, using its
native UUID, workspace, tag, engine and incarnation. A permission-client refusal
or unsupported write-tool error produces `engine_tool_permission_refused`, holds
that engine on that machine for headless placement, and stops only that newly
launched execution through the existing confirmed-exit adapter. Automatic daily
work can retry once on an eligible healthy route; explicit engine overrides
retain their original choice. Interactive placement remains available.

A refused session displays `failed`. If native cleanup is unconfirmed,
`lifecycle_state` remains `uncertain`, `cleanup_pending` is true, and its capacity
reservation stays held. A running process, missing catalog entry or a tool error
alone never proves exit. The persisted stop oracle allows read-only reconciliation
without repeating a kill. Original output remains local; Hub receives fixed
sanitized diagnostics.

A fresh quota/configuration observation does not clear this hold. Before an
operator explicitly re-enables the engine with `hub fleet probe ENGINE --status ok`,
run a real headless execution through the maintained launcher and verify file
write, `git status`, and network `git ls-remote`, in addition to genuine first-action
and native lifecycle evidence. Until that proof passes, leave the hold in place.
Native ZCodex currently needs this hold: its launcher sandbox/approval contract
has not passed that execution probe (launcher issue 69). Managed-home permission
settings do not repair argv used by an unprofiled native launcher route.
