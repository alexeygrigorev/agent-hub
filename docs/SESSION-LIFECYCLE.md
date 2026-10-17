# M15 Session Lifecycle Registry (proposal — safe module only, integration pending head ACK)

Source: Alexey Addendum 5 (`hub-intake-brief.md`). Statuses here are design intent for the
safe module/projection; the full hub feature (routes/CLI wiring/host execution) is NOT shipped
until the integration seams below are ACKed, an independent different-provider review passes,
and real host acceptance happens later.

## What exists in this change

- `src/session_lifecycle.ts` — CANONICAL pure M15a projection + M15b plan/executor seam + shared DTO seam.
- `host/agent_hub_host/session_lifecycle.py` — host-only stdlib guards + thin transport (`sanitize_observation_text`, strict `_parse_ms`, `validate_cleanup_target`, `validate_ownership_transfer`). No `classify_session` / `project_sessions` / `plan_cleanup` / DTO duplicates (M15-F6 single authority).
- Tests (synthetic fixtures only): `test/session-lifecycle.test.ts` + NEW `test/session-lifecycle-r2.test.ts`, `host/tests/test_session_lifecycle.py` (host guards) + NEW `host/tests/test_session_lifecycle_r2.py`. Vitest pool: legacy `test/vitest.session-lifecycle.config.ts` unchanged; new `test/vitest.session-lifecycle-r2.config.ts` runs old + r2 together with isolated `.local/lifecycle-r2/vite-cache` (Argon used private existing/novel configs; Node-only Vitest is legitimate here because all covered functions are pure with no HTTP/runtime effects — real HTTP/SQLite stays OPEN M15c).
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

- `starter`: immutable `fleet_launches.actor + requester_kind` at creation, retained in projection (`ClassifiedSession.starter`); `owner` / `owner_provenance` / `human_owned` also retained (M15-F3).
- `owner`: starts as starter; changes only via a dedicated authenticated, authorized
  durable transfer with current-owner + epoch fencing (`validateOwnershipTransfer` seam).
  Epochs must be non-bool safe integers `>= 0` (TS `Number.isSafeInteger`, Python
  `isinstance(int) and not bool`); booleans/negatives/non-safe fail. Generic intake note/decision text is NEVER a transfer. `unknown`/`legacy`/blank/whitespace never applies, for `new_owner`, `current_owner`, `expected_owner`, `owner`, `callerOwner`, and `owner_provenance` (`authenticated_handoff`/`starter` only).
  `validateCleanupTarget` additionally requires exact non-empty valid `expected_machine_id` / `expected_native_id` / `expected_owner` binding (`unbound_target` / `unknown_owner_never_applies` fail closed); blank/None IDs never pass even on exact match.
  Eligibility inputs are TRUSTED canonical-server joins/context only; payload bools/strings never authenticate (real HTTP/SQLite auth is M15c OPEN).
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
  `lifecycle-*` spelling. No fake apply success is shipped; the TS server is the single
  classification/planning authority and the Python host owns only observation/execution
  guards + thin transport.

## Next integration seams (proposal; no imports or shared edits in this slice)

- Canonical M12 records stay source of truth; reference READONLY primary M12 freeze
  `../agent-hub-wt-platform-session-r3` source `1f4e3007c1d5d02217861d5d1f1812549e7186c1`
  (not imported): server joins from `fleet_sessions` / `fleet_launches` / intake `link` /
  `session_control_*` supply the TRUSTED owner/epoch/provenance/activity inputs for the gates above.
  Durable SQLite decisions, `--apply` Git/worktree/process guards, and runtime acceptance are M15c/d OPEN
  pending accepted M12 canonical integration. No second DB/controller; no fake product completion.

## Tests

Synthetic tags/UUIDs/paths only (e.g. `synth:alpha`, `synth-host`, `/synth/ws`). Classes mirror the
private 2026-10-17 triage (finished, superseded, idle, limit with/without reset, waiting-Alexey,
missing worktree, active-with-pending, human-protected, foreign-owner) without private names.
Run under the head-owned guarded runner; Vitest selections use explicit `-t` filters (≤2 cases).
