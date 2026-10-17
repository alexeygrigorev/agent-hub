# M15 Session Lifecycle Registry (M15d LIVE: real cleanup --apply)

Source: Alexey Addendum 5 (`hub-intake-brief.md`). Statuses here are live:
the durable seams below are wired, tested (HTTP/SQLite + CLI + synthetic
host repos), and merged to main. Dry-run stays the default and is readonly;
`--apply` now enqueues real M12 stop intents and removes owned worktrees
under the trusted allowlist (head runs the live throwaway acceptance).

## What exists (M15c shipped)

- `src/session_lifecycle.ts` — CANONICAL pure M15a projection + M15b plan/executor seam + shared DTO seam (unchanged authority).
- `src/session_lifecycle_registry.ts` (NEW) — ONE durable/server authority:
  canonical joins (`session_control_*` + `fleet_launches`/`fleet_sessions` +
  intake canonical links), additive observation/decision storage, grouped
  view builder, dry-run planner, apply refusal. No second classifier.
- `src/session_control.ts` (narrow lifecycle wiring only; command
  auth/effect code preserved) — `init()` creates the additive
  observations/decisions tables; `handle()` serves:
  `GET /api/sessions?view=lifecycle`, `POST /api/sessions/observations`,
  `POST /api/sessions/cleanup` (dry-run default; apply refused).
- `host/agent_hub_host/session_lifecycle.py` — host-only stdlib guards +
  thin transport (`sanitize_observation_text`, strict `_parse_ms` with
  explicit UTC-offset range check, `validate_cleanup_target`,
  NON-AUTHORITATIVE `validate_ownership_transfer` pre-flight,
  allowlisted `build_observation_payload`). Host DDL copy retired: table
  DDL authority is the server schema string only.
- CLI (`hub sessions`, session subcommands only in
  `host/agent_hub_host/session_control.py`): bare `hub sessions` returns
  the grouped lifecycle projection; `hub sessions cleanup` plans dry-run
  by default; `hub sessions observe` posts machine-scoped observations.
  Existing `list/start/send/capture/stop` preserved, no substitute
  `lifecycle-*` spelling.
- Tests (synthetic fixtures only): `test/session-lifecycle-m15c.test.ts`
  (7 HTTP/SQLite cases) + `test/vitest.session-lifecycle-m15c.config.ts`,
  `host/tests/test_session_lifecycle_m15c.py` (10 transport/CLI cases).
  Pre-existing suites still green: session-control 26/26, lifecycle pure
  10/10, host lifecycle/control 25/25. `host/tests/test_session_control_loop.py`
  cannot load in this env (pre-existing: requires pytest, not installed).
- No edits to `src/hub.ts`, `src/schema.ts`, `src/intake.ts`,
  `host/agent_hub_host/cli.py`, or any `session_control.py` outside the
  `sessions` subcommands. No duplicate registry/controller/quota/lease store.

## Canonical reuse (no second tracker)

- Host-observed truth: `fleet_sessions(machine_id, native_id, ...)` via `PUT /api/fleet/sessions`.
- Durable intent/binding: `fleet_launches(actor, requester_kind, machine_id, engine, native_id, state, ...)`.
- Task linkage: intake `link` events (`native_id, engine, tier, host, hub, provenance, routing_job_id`;
  canonical links require operator + known routing job).
- Command custody (M12e, R3 candidate readonly): `session_control_*` recorded→transported→claimed→
  acked/refused/uncertain with `validateSendProof`. Lifecycle never reimplements placement or delivery.
  The server classifies the canonical record; the host validates effects.
- Additive migration only: `SESSION_LIFECYCLE_SCHEMA` creates
  `session_lifecycle_observations` + `session_lifecycle_decisions` with `IF NOT EXISTS`
  ONLY where canonical existing event data cannot represent the fact; session
  identity/provenance/control state are never duplicated.
  Landed M15c: `SessionControlService.init` creates the additive tables
  beside the existing session-control schema (no ALTERs to existing
  tables, no global-schema migration);
  no ALTERs to existing tables.

## Ownership (dedicated transfer op, not tag text)

- `starter`: immutable `fleet_launches.actor + requester_kind` at creation, retained in projection (`ClassifiedSession.starter`); `owner` / `owner_provenance` / `human_owned` also retained (M15-F3).
- `owner`: starts as starter; changes only via a dedicated authenticated, authorized
  durable transfer with current-owner + epoch fencing (`validateOwnershipTransfer` seam).
  Epochs must be non-bool safe integers `>= 0` (TS `Number.isSafeInteger`, Python
  `isinstance(int) and not bool`); booleans/negatives/non-safe fail. Generic intake note/decision text is NEVER a transfer. `unknown`/`legacy`/blank/whitespace never applies, for `new_owner`, `current_owner`, `expected_owner`, `owner`, `callerOwner`, and `owner_provenance` (`authenticated_handoff`/`starter` only).
  `validateCleanupTarget` additionally requires exact non-empty valid `expected_machine_id` / `expected_native_id` / `expected_owner` binding (`unbound_target` / `unknown_owner_never_applies` fail closed); blank/None IDs never pass even on exact match.
  Eligibility inputs are TRUSTED canonical-server joins/context only; payload bools/strings never authenticate (real HTTP/SQLite auth landed in M15c).
- Arbitrary JSON `owner` tags are untrusted input. Admin privilege is NOT another
  coordinator's ownership. `human`/Alexey sessions are permanently protected (never
  disabled, never transferred except a legitimate authenticated transfer of a non-human record).

## Classification (fresh provenance required, all flags preserved)

Strict timestamps (M15-F2/F4, both languages): parsers require explicit `Z` or `±HH:MM`
(same rule as `src/fleet.ts:40`); naive/missing/unparseable → null with no local/UTC guess,
so UTC vs Europe/Helsinki runs agree. Present-malformed activity, future activity/report/terminal,
and stale (>24h) evidence all fail closed. `limit_zone` names never rescue naive timestamps here
(no audited DST resolution in this slice); `Z`/`±HH:MM` + `reset > now` qualifies a known reset
even when `limit_zone` is absent. Missing/unparseable/expired reset → `limit_blocked_unknown_reset`;
a reset never promotes capacity or authorizes a launch.

Order for the primary group: `missing_worktree_unknown` → `waiting_alexey` (fresh ≤24h question/approval) →
`limit_blocked` (`window/reset/zone/unknown`; unknown stays needs-action, never resumes from a
timestamp alone) → `finished`/`superseded` (same gate: zero obligations AND `authoritative_no_pending_proof === true`
AND `((report_written && screen_done && reportFresh) || terminalFresh)` with both freshness terms bounded
against newer activity as above; report/screen/terminal alone never qualifies) → `idle` (≥ configurable hours, default 24, still `active` group) → `active` (requires fresh valid `observed_at` ≤24h AND (`phase === 'running'` OR fresh non-future activity); else `unknown`) →
`unknown` (reachable: empty/stale/missing/future provenance, non-running phase without fresh activity).
Grouping picks one primary
but `flags[]` + `obligations[]` preserve every simultaneous condition (missing worktree + waiting
human, limit + draft) into status/handoff. Observations default to allowlisted structured reason
codes + provenance; free-text snippets are opt-in only (`note_text_opt_in`), bounded ≤512 with oversize
dropped (not clipped), redacted on a `max+128` window BEFORE final truncation with trailing partial-marker
stripping (regex alone is not a privacy guarantee), and always data (`text_as_data: true` on handoff), never instructions. Projection is deterministic (group rank, session key),
bounded (page ≤50 + cursor), and scoped (owner/task/host).

## Test migration (M15-F6)

Prior Python `classify_session` / `project_sessions` / `plan_cleanup` / DTO mirrors duplicated the TS
authority and caused three concrete TS/Python divergences (empty-owner, bool-epoch, naive-TZ). They are
removed; unshipped-function compatibility is not a reason to keep duplicate authority. Legitimate old
Python author classification cases (including the `stale_banner` negative) migrated into
`test/session-lifecycle-r2.test.ts` with retained semantic negative coverage; no failures were deleted
to hide control examples. Host keeps its own strict execution-guard validation (necessary independent of
server eligibility), not a second registry or fake apply.

## Decisions

- Limit-block decisions are durable: `resume_on_reset(engine)` / `reassign(engine/session)` /
  `drop(reason)`, each with actor/time/reason. A reset alone never authorizes a launch; fresh
  quota/routing/reserves/cooldown still apply at execution time.
- Same classified facts feed `hub status` / `hub handoff` via `toStatusDto`/`toHandoffDto`.

## Cleanup safety (dry-run default, real --apply)

- `planCleanup` is pure: eligible ONLY when group is `finished_stale`, state is confirmed
  `finished`/`superseded`, caller owns the session, no pending obligations, and not human-owned.
  Idle/limit/waiting-Alexey alone are never eligible.
- `validateCleanupTarget` (executor seam) revalidates machine/native/owner, refuses on
  busy/draft/pending or failed containment, never does tag-only kill, never treats a missing
  record as stopped. Operations are idempotent; uncertain outcomes stay durable (no blind retry).
- M15d REAL `--apply` (`POST /api/sessions/cleanup` `{apply:true, key?}`):
  owner-only (admin apply is never an oracle: 404), exact authenticated
  current-owner credential/family/incarnation re-verified per session INSIDE
  the mutation transaction (after the awaited body), admin never means
  another coordinator owns it, human/Alexey records permanently protected.
  Fresh eligibility is re-evaluated per session before any enqueue; each
  eligible target becomes ONE genuine M12 stop intent (existing
  recorded→transported→claimed→acked custody, idempotent per operation key;
  an already-pending stop replays instead of duplicating). Fleet-observed-only
  rows (no session-control intent), drifted bindings, and non-stoppable
  states are kept with actual reasons. The host executes each stop through
  the existing claim/effect/ack path with containment-confirmed exit.
- Tree/branch removal (host, after CONFIRMED stop only):
  `host/agent_hub_host/session_cleanup.py` removes ONLY the explicitly
  addressed dedicated registered worktree + branch under the trusted host
  `cleanup_roots` allowlist (explicit `cleanup_roots` in the host settings;
  `fleet.py` validates the allowlist shape, `session_host.py` surfaces its
  size, neither auto-deletes). Refuses shared main/bare/root workspaces,
  symlink/realpath escape, another live session cwd/worktree use, any
  dirty/index/untracked content, locks, branch checked out elsewhere, and
  unmerged commits. HEAD must be entirely reachable from FRESH `origin/main`
  (`ls-remote`, no local mutation; network/unknown fails closed to
  listed-only). Ordinary `git worktree remove` then `git branch -d` only
  after ancestry proof; no `--force`, no reset. Per-target durable journals
  (`cleanup-<machine>-<nativehex>.json` beside the receiver journals, never
  under a cleanup root) make restart/lost-response replay terminal
  `tree_removed` outcomes and re-evaluate kept ones — never repeating an
  unsafe deletion. CLI: `hub sessions cleanup --apply [--key KEY]` enqueues
  stops; adding `--tree --machine-id/--native-id/--owner/--worktree/--branch/
  --journal-dir/--cleanup-root` additionally removes the addressed tree once
  its stop reads back `stopped`, else reports `stop_pending` with no git
  effect. Partial results and uncertainty stay durable; every kept target
  reports its actual reason — no pretend success.
  Dry-run stays readonly (pure inputs, zero writes). Tests use only
  disposable synthetic fixture repos/processes wholly inside the owned
  private test `TMPDIR`; real worktrees, histories, and sources stay
  preserved.

## Live contracts (M15c shipped, tested)

- `GET /api/sessions?view=lifecycle` grouped projection (additive view
  param; never a second `/api/lifecycle` registry). Writer auth; non-admin
  callers are scoped to their own actor, admins may filter
  `?owner=&host=&task=`. Each entry carries the pure DTO (`group/state/
  reasons/flags/obligations`), `refs` (session/fleet/task/brief/report
  references where recorded, nulls where unknown), and the latest durable
  `decision` (limit-blocked sessions append it to reasons).
- `POST /api/sessions/observations` — host-only machine-scoped writes
  (host credential must match `machine_id`; live family + machine registry
  re-verified in-txn) or admin operator records. Owner/starter/human
  identity keys are dropped server-side; unknown fields fail closed.
- `POST /api/sessions/decisions` — owner-only limit-block decisions
  (`resume_on_reset`/`reassign`/`drop` with actor/time/reason; engine
  required for resume/reassign, target required for reassign). The session
  must currently classify `limit_blocked`, must not be human-owned, and the
  caller must be the live owner (agent credential + family + incarnation).
  Admins are never another coordinator's owner (404, never oracle).
  Decisions persist in SQLite across restarts and feed the projection.
- `POST /api/sessions/cleanup` — dry-run default (`{dry_run:true}` or
  empty body): pure plan over the caller's scope, ZERO writes. `{apply:true,
  key?}` enqueues real M12 stop intents for freshly eligible OWN sessions
  (owner custody inside the txn; admin apply 404s; per-session fresh
  re-evaluation; pending stops replay; kept targets carry actual reasons).
  No fake apply success exists anywhere in this slice.
- CLI: `hub sessions` defaults to the grouped lifecycle listing +
  `hub sessions cleanup [--apply [--key KEY] [--tree ...]]` (dry-run
  default; `--apply` enqueues owned stops, `--tree` additionally removes the
  explicitly addressed owned worktree/branch after confirmed stop) +
  `hub sessions observe` (host observation transport). Existing
  `list/start/send/capture/stop` are preserved; no substitute `lifecycle-*`
  spelling. The TS server is the single classification/planning authority
  and the Python host owns only observation/execution guards + thin transport.

## Integration seams (landed M15c; M15d shipped, live acceptance next)

- Canonical M12 records stay source of truth: server joins from
  `fleet_sessions` / `fleet_launches` / intake `link` /
  `session_control_*` supply the TRUSTED owner/epoch/provenance/activity
  inputs for the gates. Only bound/observed native identities are keyed;
  unbound queued/held intents stay in the existing flat `list` view.
  No second DB/controller; no fake product completion.
- M15d (SHIPPED, head live-throwaway acceptance next): the `--apply`
  executor — server-side machine/native/owner revalidation + M12 stop
  enqueue, host-side containment-confirmed stop via claim/ack, owned
  per-session worktree/branch clean-and-merged removal under the trusted
  allowlist, durable per-target journals. Coordination contract for the
  parallel status worker: narrow PUBLIC
  `SessionControlService.hostObservationAllowed(machineId, caller)` returning
  `caller.kind==='host' && hostOk({machine_id}, caller).ok` (existing fresh
  host family/approval authority, no duplicate checker, no new privilege);
  the status worker owns `src/hub.ts` injection and still validates host
  scope + canonical task/session machine itself. Cleanup logic never trusts
  JSON ownership from host observations.

## Tests

Synthetic tags/UUIDs/paths only (e.g. `synth:alpha`, `synth-host`, `/synth/ws`). Classes mirror the
private 2026-10-17 triage (finished, superseded, idle, limit with/without reset, waiting-Alexey,
missing worktree, active-with-pending, human-protected, foreign-owner) without private names.
M15d adds `test/session-cleanup-m15d.test.ts` (4 HTTP/SQLite apply cases:
real enqueue + idempotent replay, admin refusal, stranger/idle/limit/human
kept, narrow host gate) and `host/tests/test_session_cleanup_m15d.py`
(10 synthetic-repo cases: confinement, clean+merged removal + replay,
dirty/unmerged/broken-remote/live-use/shared-checkout/checked-out-elsewhere
kept, allowlist plumbing, CLI transport). Host git fixtures live wholly under
the owned `.local/tmp`. Run under the head-owned guarded runner; Vitest
selections use explicit `-t` filters (≤2 cases).
