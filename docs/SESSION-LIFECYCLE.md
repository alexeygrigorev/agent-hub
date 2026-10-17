# M15 Session Lifecycle Registry (proposal — safe module only, integration pending head ACK)

Source: Alexey Addendum 5 (`hub-intake-brief.md`). Statuses here are design intent for the
safe module/projection; the full hub feature (routes/CLI wiring/host execution) is NOT shipped
until the integration seams below are ACKed, an independent different-provider review passes,
and real host acceptance happens later.

## What exists in this change

- `src/session_lifecycle.ts` — pure M15a projection + M15b plan/executor seam + shared DTO seam.
- `host/agent_hub_host/session_lifecycle.py` — stdlib-only mirror of the same pure logic.
- Tests (synthetic fixtures only): `test/session-lifecycle.test.ts`, `host/tests/test_session_lifecycle.py`.
- No edits to `src/hub.ts`, `src/schema.ts`, `src/session_control.ts` (does not exist on main yet),
  `host/agent_hub_host/cli.py`, or any `session_control.py` (R3 candidate stays readonly in
  `../agent-hub-wt-platform-session-r3`). No duplicate registry/controller/quota/lease store.

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
  Requested seam (head ACK needed): call the init beside existing fleet/intake init in `src/hub.ts`;
  no ALTERs to existing tables.

## Ownership (dedicated transfer op, not tag text)

- `starter`: immutable `fleet_launches.actor + requester_kind` at creation.
- `owner`: starts as starter; changes only via a dedicated authenticated, authorized
  durable transfer with current-owner + epoch fencing (`validateOwnershipTransfer` seam).
  Generic intake note/decision text is NEVER a transfer. `unknown`/`legacy` never applies.
- Arbitrary JSON `owner` tags are untrusted input. Admin privilege is NOT another
  coordinator's ownership. `human`/Alexey sessions are permanently protected (never
  disabled, never transferred except a legitimate authenticated transfer of a non-human record).

## Classification (fresh provenance required, all flags preserved)

Order for the primary group: `missing_worktree_unknown` → `waiting_alexey` (fresh ≤24h question/approval) →
`limit_blocked` (`window/reset/zone/unknown`; unknown stays needs-action, never resumes from a
timestamp alone) → `finished`/`superseded` (fresh explicit terminal completion/supersession evidence
OR report+done screen, AND independent authoritative no-pending/no-active/no-draft proof, AND no
newer activity) → `idle` (≥ configurable hours, default 24, still `active` group) → `active` →
`unknown`. A report, screen, or terminal record ALONE never qualifies. Grouping picks one primary
but `flags[]` + `obligations[]` preserve every simultaneous condition (missing worktree + waiting
human, limit + draft) into status/handoff. Observations default to allowlisted structured reason
codes + provenance; free-text snippets are opt-in only (`note_text_opt_in`), bounded ≤512, redacted
as a backstop (regex alone is not a privacy guarantee), and always data, never instructions. Projection is deterministic (group rank, session key),
bounded (page ≤50 + cursor), and scoped (owner/task/host).

## Decisions

- Limit-block decisions are durable: `resume_on_reset(engine)` / `reassign(engine/session)` /
  `drop(reason)`, each with actor/time/reason. A reset alone never authorizes a launch; fresh
  quota/routing/reserves/cooldown still apply at execution time.
- Same classified facts feed `hub status` / `hub handoff` via `toStatusDto`/`toHandoffDto`.

## Cleanup safety (dry-run default)

- `planCleanup` is pure: eligible ONLY when group is `finished_stale`, state is confirmed
  `finished`/`superseded`, caller owns the session, no pending obligations, and not human-owned.
  Idle/limit/waiting-Alexey alone are never eligible.
- `validateCleanupTarget` (executor seam) revalidates machine/native/owner, refuses on
  busy/draft/pending or failed containment, never does tag-only kill, never treats a missing
  record as stopped. Operations are idempotent; uncertain outcomes stay durable (no blind retry).
- Tree/branch removal (future apply path, NOT executed here): only the owned per-session tree,
  canonical realpath under the owned root, no symlink escape, no shared checkout, no other
  session cwd, no extra worktree user of the branch, no git locks; clean including untracked/index;
  no unmerged commits; HEAD entirely reachable from FRESH `origin/main` (network unavailable or
  stale ref fails closed to listed-only). Argv arrays only, no shell interpolation. Dry-run is
  readonly (pure inputs, no fetch mutation). In this task only disposable synthetic fixture
  repos/processes wholly inside the owned private test `TMPDIR` may be used; real worktrees,
  histories, and sources stay preserved.

## Proposed contracts (need head + M12 worker ACK; canonical sessions surface only)

- `GET /api/sessions?view=lifecycle` grouped projection (additive view param; never a second
  `/api/lifecycle` registry); `GET /api/sessions/:id` single; `POST /api/sessions/cleanup`
  (dry-run default; apply is OPEN M15c/d); decisions ride the existing POST decision seam
  (durable server decisions are OPEN M15c/d).
- CLI: `hub sessions` defaults to the grouped lifecycle listing + `hub sessions cleanup [--apply]`
  (dry-run default). Existing `list/start/send/capture/stop` are preserved; no substitute
  `lifecycle-*` spelling. No fake apply success is shipped; TS/Python authority state machines
  are not duplicated.

## Tests

Synthetic tags/UUIDs/paths only (e.g. `synth:alpha`, `synth-host`, `/synth/ws`). Classes mirror the
private 2026-10-17 triage (finished, superseded, idle, limit with/without reset, waiting-Alexey,
missing worktree, active-with-pending, human-protected, foreign-owner) without private names.
Run under the head-owned guarded runner; Vitest selections use explicit `-t` filters (≤2 cases).
