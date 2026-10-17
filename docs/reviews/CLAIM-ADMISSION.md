# M6 Re-Review — Ownership-Aware Editable Admission (`CLAIM-ADMISSION`)

- **Verdict**: `ACCEPT`
- **Review Line**: `Review: approved sha=71349b42ec88f9a0a1e228d4411e1c716c2d8e0f reviewer=platform-claims-rereview/gemini-4-argon-high`
- **Reviewed Commit**: `71349b42ec88f9a0a1e228d4411e1c716c2d8e0f` (`Repair unstarted offer reconciliation and routed edit authority [skip ci]`)
- **Author**: `platform-claims-r2/codex` (`gpt-6.1-sol` / high; Agent Branches task `0400` / `platform-claims-r2-0400`)
- **Reviewer**: `platform-claims-rereview/gemini-4-argon-high` (independent model family)
- **Baseline R1 Review**: `63dd32ee9d918cc6930e4a9bc3405c49f5411388` (`REVISE` on `fe7a8c1c8d7d56d1381043bd78539c7fb2f935db`)
- **Lineage Verified from Git**:
  - Base `origin/main`: `213fcf61780b9a02645c747f1a13104a2e55b321`
  - Preserved Grok WIP commit: `0fda8bc1c06aea3b449c483842ffef5a7c5bbc0e`
  - Prior repair commit: `1cd08b75fc87e7e237bc6c2360b72e723987f0e7`
  - Prior R1 reviewed commit: `fe7a8c1c8d7d56d1381043bd78539c7fb2f935db`
  - Candidate HEAD: `71349b42ec88f9a0a1e228d4411e1c716c2d8e0f`
- **Reviewed Diff Scope (`213fcf61780b9a02645c747f1a13104a2e55b321..71349b42ec88f9a0a1e228d4411e1c716c2d8e0f`)**:
  - [`docs/CLAIM-ADMISSION.md`](../CLAIM-ADMISSION.md) (`+116`)
  - [`src/claim_admission.ts`](../../src/claim_admission.ts) (`+259`)
  - [`src/fleet.ts`](../../src/fleet.ts) (`+119 / -45`, net `+74`)
  - [`src/routing.ts`](../../src/routing.ts) (`+81 / -45`, net `+36`)
  - [`test/claim-admission.test.ts`](../../test/claim-admission.test.ts) (`+843`)

---

## Executive Summary & Standalone Landing Decision

Commit `71349b42ec88f9a0a1e228d4411e1c716c2d8e0f` cleanly resolves all four findings (**F1–F4**) from the baseline Argon R1 review (`63dd32ee9d918cc6930e4a9bc3405c49f5411388`) while preserving all M6 ownership-aware admission invariants, existing model-routing tier/quota/fairness policies, and host lifecycle safety guarantees.

**Can this 5-file M6 change land independently and safely on `main` now?**
- **Yes.** Against `origin/main` (`213fcf61780b9a02645c747f1a13104a2e55b321`), the 5-file M6 diff is self-contained, touches only `docs/CLAIM-ADMISSION.md`, `src/claim_admission.ts`, `src/fleet.ts`, `src/routing.ts`, and `test/claim-admission.test.ts` (after routing peer scope release and ACK), introduces zero regressions across existing Fleet, Routing, Bus, Liveness, Registry, and Python host/CLI suites, and is safe to integrate onto `main` without claiming M1 conversation retention completion, F7 registry migration completion, or M8a host-execution completion.

---

## Dispositions of Original R1 Findings (F1–F4)

### F1 (High -> RESOLVED): Atomic retirement of invalid unstarted `offered` rows with linked `routing_jobs` and bounded circular sweep

- **Source Repair Verified**:
  - [`src/claim_admission.ts`](../../src/claim_admission.ts#L69-L77) (`retireInvalidOffer(row)`): Strictly guards `if (row.state !== 'offered') return null;`. When `this.heldReason(row)` is non-null, transitions the unstarted `fleet_launches` row (`WHERE id = ? AND state = 'offered'`) to `state = 'failed'`, `verification_status = 'not_started'`, `exit_reason = reason`, `finished_at = at`, `updated_at = at`, and updates any linked `routing_jobs` row (`WHERE launch_id = row.id`) to `state = 'failed'`, `report = 'Not started: ' + reason`, `updated_at = at` in the caller's synchronous SQLite transaction.
  - [`src/claim_admission.ts`](../../src/claim_admission.ts#L80-L88) (`reconcileOffers()`): Executes a bounded `LIMIT 100` circular keyset sweep over `fleet_launches WHERE state = 'offered'` (`id > offerCursor`, wrapping around with `id <= offerCursor` when fewer than 100 rows match), supported by partial index `fleet_offered ON fleet_launches(id) WHERE state = 'offered'` ([`src/fleet.ts`](../../src/fleet.ts#L74)). `this.offerCursor` is updated only after `this.transaction(...)` commits.
  - [`src/fleet.ts`](../../src/fleet.ts#L97), [`src/fleet.ts`](../../src/fleet.ts#L174), [`src/fleet.ts`](../../src/fleet.ts#L254), [`src/fleet.ts`](../../src/fleet.ts#L274-L277), [`src/routing.ts`](../../src/routing.ts#L63), and [`src/routing.ts`](../../src/routing.ts#L182): Runs `reconcileOffers()` before account capacity evaluation (`accountStatus`), launch preview/reservation (`reserve`), account registry replacement (`PUT /api/fleet/accounts`), host offer polling (`GET /api/fleet/offers`, plus per-machine `retireInvalidOffer`), routing `dispatch()`, and routed task `enqueue`.
  - [`src/fleet.ts`](../../src/fleet.ts#L337-L349) (`POST /api/fleet/launches/:id/claim`): Checks `retireInvalidOffer(row)` before host gate/account checks, and after `this.accountStatus(account)` (which sweeps up to 100 offers and during which synchronous clock state could advance), re-reads `SELECT * FROM fleet_launches WHERE id = ?` and re-runs `retireInvalidOffer(current)` before `UPDATE fleet_launches SET state = 'starting' ... WHERE id = ? AND state = 'offered'`, preventing resurrection of a retired offer.
- **Adversarial Verification**:
  - Verified across candidate HTTP tests (`test/claim-admission.test.ts:654-706, 779-843`) and independent Argon **Probe 1** (`.local/tmp/m6-rereview-probes.test.ts`):
    1. A high-ID (`ffffffff-ffff-4fff-8fff-ffffffffffff`) invalid `offered` routed edit launch placed behind 105 low-ID valid `offered` read launches is deterministically reached and retired on the second bounded `reconcileOffers()` sweep (no starvation).
    2. Injecting a SQL `ABORT` trigger on `routing_jobs` update during `reconcileOffers()` and `/claim` rolls back both the `fleet_launches` state change and `offerCursor` advancement.
    3. Once retired, `PUT /api/fleet/accounts` succeeds (`200`), account/machine capacity and scope locks are freed immediately, and `LivenessService.evaluate()` after `launchStuckS` (600s) opens zero `stuck` alerts for retired offers.
    4. `starting`, `running`, and `uncertain` launches are never retired by `retireInvalidOffer()` or `reconcileOffers()` and continue holding capacity and blocking overlapping claims until `confirmed_exit: true`.
    5. Inspection GETs (`GET /api/fleet/claim-policy`, `GET /api/fleet/routing/machines`, `GET /api/fleet/routing/policy`, `GET /api/fleet/tasks`, `GET /api/fleet/tasks/:id`) perform no mutating SQL writes; only admin `GET /api/fleet` (via `accountStatus`) and host `GET /api/fleet/offers` trigger `reconcileOffers()` within their existing authorized roles.

### F2 (High -> RESOLVED): Enqueue & plan conflict enforcement and truthful `admission_hold` on unlaunched queued jobs

- **Source Repair Verified**:
  - [`src/claim_admission.ts`](../../src/claim_admission.ts#L222-L224): Removed the `phase !== 'enqueue'` exemption; `this.conflictingLaunches(authority.claim, opts.launchId)` now runs across all phases (`enqueue`, `reserve`, `start`).
  - [`src/routing.ts`](../../src/routing.ts#L56-L59) (`RoutingService.view()`): Computes `admissionHold` for unlaunched queued jobs (`row.state === 'queued'` when `launch` is null) via `this.fleet.admission.heldReason({ request_json: row.request_json, workspace: input.workspace })`, and suppresses the generic capacity message (`queue_reason: null`) whenever `admissionHold` is non-null.
  - [`src/routing.ts`](../../src/routing.ts#L93): `RoutingService.dispatch()` calls `evaluate(..., 'reserve', { authority })` rather than `'enqueue'`.
- **Adversarial Verification**:
  - Verified in candidate HTTP tests (`test/claim-admission.test.ts:708-737`) and independent Argon **Probe 2**:
    1. When an active launch (`offered`, `starting`, `running`, or `uncertain`) retains an edit claim whose bus lease subsequently expires and a second agent acquires a replacement claim for an overlapping scope, `POST /api/fleet/plan`, `POST /api/fleet/tasks`, and `POST /api/fleet/launches` all reject with `409 claim_conflict` and create zero rows.
    2. Concurrent distinct requests with the same or overlapping claims admit at most one launch (`[201, 409]`), while exact retries with the same `idempotency_key`, canonical payload, and `credential_id` replay without duplicate effects.
    3. An earlier queued task (`launch_id: null`) waiting on a capability truthfully transitions from `admission_hold: null, queue_reason: "No fresh eligible..."` to `admission_hold: "claim_conflict" | "claim_inactive" | ..., queue_reason: null` as soon as a conflicting launch starts or its authority becomes invalid.

### F3 (Medium -> RESOLVED): Current `require_claim` workspace policy enforced across all access modes at `/offers` and `/claim`

- **Source Repair Verified**:
  - [`src/claim_admission.ts`](../../src/claim_admission.ts#L229-L239) (`heldReason(row)`): Evaluates `this.evaluate(..., row.workspace, declared.access, 'start', { authority, launchId: row.id })` for all access modes (`read`, `legacy_unspecified`, and `edit`), rather than returning `null` early for non-`edit` modes.
  - [`src/fleet.ts`](../../src/fleet.ts#L277) & [`src/fleet.ts`](../../src/fleet.ts#L338-L347): Both `GET /api/fleet/offers` and `POST /api/fleet/launches/:id/claim` call `this.admission.retireInvalidOffer(row)` (which delegates to `heldReason(row)`), so an already-`offered` `legacy_unspecified` launch (direct or routed) is immediately retired (`state = 'failed'`, `verification_status = 'not_started'`, `exit_reason = 'claim_required'`, HTTP `403` on `/claim`) if `require_claim: true` is enabled on its workspace before host start, while explicit `read` offers remain valid (`200` on `/claim`).
- **Adversarial Verification**:
  - Verified across all 4 `(legacy_unspecified | read) x (launches | tasks)` combinations in `test/claim-admission.test.ts:739-765` and independent Argon **Probe 3**.

### F4 (Medium -> RESOLVED): End-to-end HTTP-paired agent routed `edit` authority with least privilege

- **Source Repair Verified**:
  - [`src/routing.ts`](../../src/routing.ts#L132-L135) & [`src/routing.ts`](../../src/routing.ts#L175-L188): Implements the reviewer-offered alternative from R1 without expanding `/api/device/code` scopes or granting bus authority to routing-only clients:
    - Defines `routingClient = caller.kind === 'agent' && auth.scopes?.includes('routing:submit')` and `fleetEditor = caller.kind === 'agent' && auth.scopes?.includes('bus:send') && auth.scopes?.includes('fleet:launch')`.
    - Line 175 (`if (!admin && !routingClient && parsed.access.mode !== 'edit') return reply({ error: 'forbidden' }, 403);`) restricts `fleetEditor` callers on `POST /api/fleet/tasks` strictly to `access.mode === 'edit'`—they cannot submit routed `read` or `legacy_unspecified` tasks without `routing:submit`.
    - Line 183 passes `{ permission: routingClient ? 'routing:submit' : 'fleet:launch' }` into `ClaimAdmission.evaluate()`, which verifies from the live `credentials` row in SQLite that the agent credential is unrevoked, unexpired, and carries both `'bus:send'` and `permission` (`'fleet:launch'` or `'routing:submit'`), binding `permission` into `edit_authority` for all subsequent `reserve` and `start` revalidations ([`src/claim_admission.ts`](../../src/claim_admission.ts#L197-L208)).
    - Line 188 enforces `sameEditCaller(inserted.prior.request_json, caller)` on idempotent retries in `POST /api/fleet/tasks` (matching `FleetService.reserve` at [`src/fleet.ts`](../../src/fleet.ts#L189)).
- **Adversarial Verification**:
  - Verified in `test/claim-admission.test.ts:541-652, 767-777` and independent Argon **Probe 4** using full HTTP provisioning (`/auth/token` -> `/api/device/code` -> `/api/device/approve/:code` -> `/api/device/token` -> `/api/agents` -> `/api/bus/claims` -> `/api/fleet/tasks` -> `/api/fleet/offers` -> `/api/fleet/launches/:id/claim`) with zero SQL credential injection:
    1. A paired agent with `['bus:send', 'fleet:launch']` submits routed `edit` work, offers, claims, runs, and completes via HTTP.
    2. The same `fleetEditor` token is denied (`403 forbidden`) when attempting to submit routed `read` or `legacy_unspecified` tasks.
    3. Routing-only tokens minted via `POST /api/fleet/routing/clients/:name` (`['routing:submit']` only) retain `read`/`legacy_unspecified` submission (`201`) but fail closed (`403`) on `edit` tasks and `/api/bus/claims`.
    4. Admin cookies/tokens, host tokens, job tokens, other-agent/other-project tokens, revoked tokens, and expired tokens all fail closed (`403`).
    5. `POST /api/token/refresh` revokes the bound `credential_id`, causing any unstarted `offered` launch from the pre-refresh token to fail `/claim` with `403 claim_authority_unsupported` (`state = 'failed'`, `verification_status = 'not_started'`) and rejecting same-`idempotency_key` retries under the post-refresh token with `409 idempotency_conflict`.

---

## New Findings on Standalone M6 (`71349b42ec88f9a0a1e228d4411e1c716c2d8e0f`)

- **None.** No High, Medium, or Low defects remain in the 5-file M6 diff against `origin/main` (`213fcf61780b9a02645c747f1a13104a2e55b321`).

---

## Forward Integration Note: Intersection with Unmerged F7 Registry Proposal (`0399` / `7d8cd282`)

Per the review brief, we inspected the read-only intersection between candidate `71349b42ec88f9a0a1e228d4411e1c716c2d8e0f` and the separate F7 project-qualified agent registry proposal (`docs/proposals/PROJECT-AGENT-REGISTRY.patch` at `7d8cd282d292a45b0ec297ec1a8e86ff327fc332`, reviewed separately):

1. **Where M6 and F7 already align**:
   - When an agent pairs with a project-qualified actor (`project:tag`, as in `pairedHttp()` at `test/claim-admission.test.ts:591`), or when F7's `POST /api/device/approve/:code` rewrites `device_pairings.actor` to `project:tag` before `POST /api/device/token` inserts the `credentials` row, `credentials.actor` and `credentials.registry_actor` are both `project:tag`, matching `caller.actor` from `authenticateBusRequest`.
2. **Integration point to reconcile when F7 is applied**:
   - In [`src/claim_admission.ts`](../../src/claim_admission.ts#L197-L204), `ClaimAdmission.evaluate()` currently queries:
     ```ts
     const credential: any = this.sql.exec('SELECT kind, actor, subject, scopes, revoked_at, expires_at FROM credentials WHERE id = ?', principal.tokenId).toArray()[0];
     ...
     if (!credential || credential.kind !== 'agent' || (credential.actor || credential.subject) !== principal.actor || ...)
     ```
   - Under F7, `bindAgentCredentials` adds `credentials.registry_actor` and `authenticateBusRequest` sets `caller.actor = credentialActor(this.sql, cred)` (which resolves `cred.registry_actor ?? cred.actor ?? cred.subject` and returns `null` if a bare projectless credential later collides with a project-qualified agent).
   - As demonstrated in Argon **Probe 5** (`.local/tmp/m6-rereview-probes.test.ts`), if a pre-existing legacy credential row has bare `credentials.actor = 'editor'` while F7's `bindAgentCredentials` populates `credentials.registry_actor = 'tasks:editor'`, `principal.actor` from `authenticateBusRequest` is `'tasks:editor'` whereas `(credential.actor || credential.subject)` is `'editor'`, causing `(credential.actor || credential.subject) !== principal.actor` to reject with `403 claim_authority_unsupported`. Conversely, during `heldReason()` / `dispatch()`, M6 reconstructs `{ kind: 'agent', actor: authority.actor, tokenId: authority.credential_id }` directly without calling F7's `credentialActor(this.sql, credential)`.
   - **Action for the later F7+M6 integration step**: When F7 (`src/agent_registry.ts`) is applied onto `main` after M6 lands, include `registry_actor` in `src/claim_admission.ts:197` (`SELECT kind, actor, subject, registry_actor, scopes, revoked_at, expires_at FROM credentials WHERE id = ?`) and compare `credentialActor(this.sql, credential) !== principal.actor`. This is strictly an F7 merge-time adapter item, not a defect in standalone M6 on `origin/main`.

---

## Criteria Verification Matrix

| Requirement / Adversarial Dimension | Status | Evidence & Notes |
| :--- | :--- | :--- |
| Uses existing `/api/bus/claims` authority (`claims` table); no second lock registry | **PASS** | [`src/claim_admission.ts`](../../src/claim_admission.ts#L137-L143) & [`src/claim_admission.ts`](../../src/claim_admission.ts#L211-L220) read `claims` directly and share `BusService.validateClaimProject` / `isAuthorizedForActor`. |
| Single synchronous DO SQLite transaction at enqueue, offer/reservation, routing link, and host `/claim` | **PASS** | [`src/fleet.ts`](../../src/fleet.ts#L173-L202), [`src/fleet.ts`](../../src/fleet.ts#L333-L358), [`src/routing.ts`](../../src/routing.ts#L72-L120), [`src/routing.ts`](../../src/routing.ts#L179-L187). Verified rollback on injected SQLite triggers. |
| Identity/caller provenance from live Hub auth adapter; live `credentials` row re-read by `credential_id` | **PASS** | [`src/hub.ts`](../../src/hub.ts#L1313-L1389) & [`src/claim_admission.ts`](../../src/claim_admission.ts#L187-L208). Rejects body/header caller overrides and raw SQL token spoofing. |
| F1: Invalid unstarted `offered` rows retire atomically with `routing_jobs`, freeing capacity/scopes/registry/liveness; bounded `<= 100` circular sweep; `/claim` re-read prevents resurrection; `starting`/`running`/`uncertain` never freed before `confirmed_exit: true` | **PASS** | [`src/claim_admission.ts`](../../src/claim_admission.ts#L69-L88), [`src/fleet.ts`](../../src/fleet.ts#L337-L349). Verified in candidate F1 suite and Argon Probe 1. |
| F2: `enqueue`, `reserve`, and `start` all enforce `conflictingLaunches` (including replacement claims after expiry and concurrent same-holder requests); unlaunched queued `view()` reports `admission_hold` and suppresses generic `queue_reason` | **PASS** | [`src/claim_admission.ts`](../../src/claim_admission.ts#L222-L224), [`src/routing.ts`](../../src/routing.ts#L56-L59). Verified in candidate F2 suite and Argon Probe 2. |
| F3: Current `require_claim` policy enforced for all access modes at `/offers` and `/claim`; offered `legacy_unspecified` cannot bypass policy transition; explicit `read` unaffected | **PASS** | [`src/claim_admission.ts`](../../src/claim_admission.ts#L166-L173) & [`src/claim_admission.ts`](../../src/claim_admission.ts#L229-L239). Verified in candidate F3 suite and Argon Probe 3. |
| F4: Real HTTP-paired agent (`bus:send` + `fleet:launch`) can submit routed `edit` only; routing-only tokens (`routing:submit`) cannot borrow claims or submit edits; token refresh invalidates unstarted offers and blocks idempotent key rewrite | **PASS** | [`src/routing.ts`](../../src/routing.ts#L132-L135), [`src/routing.ts`](../../src/routing.ts#L175-L188), [`src/claim_admission.ts`](../../src/claim_admission.ts#L253-L259). Verified in candidate F4 suite and Argon Probe 4. |
| Existing read/legacy routing, tier/model/quota capacity, fairness, account pinning, verified exit, and uncertain non-replay preserved | **PASS** | All existing Fleet, Routing, Bus, HTTP, Liveness, Registry, and Python host/CLI tests pass without modification. |

---

## Independent Verification Commands, Test Counts & Resource Bounds

All verification commands were executed in `<separate-review-checkout>` at `71349b42ec88f9a0a1e228d4411e1c716c2d8e0f` using a temporary symlink to `<existing-installed-dependencies>/node_modules` (removed after testing), `TMPDIR="$PWD/.local/tmp"`, and single-core / single-pool bounds (`taskset -c 0 env UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1`):

1. **TypeScript Typecheck**:
   ```bash
   taskset -c 0 env UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1 TMPDIR="$PWD/.local/tmp" npm run typecheck
   ```
   - **Result**: `tsc --noEmit` exited `0` (clean).

2. **Worker Vitest Suites (Candidate M6 + Fleet/Routing/Bus/HTTP/Liveness/Assets/Registry)**:
   ```bash
   taskset -c 0 env UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1 TMPDIR="$PWD/.local/tmp" npm test -- test/claim-admission.test.ts test/fleet.test.ts test/routing.test.ts test/m3-bus.test.ts test/m3-http.test.ts test/fleet-assets.test.ts test/liveness.test.ts test/agent-registry.test.ts
   ```
   - **Result**: **8 test files passed, 163 passed (163 total), 0 failed** (`31.43s`):
     - `test/claim-admission.test.ts`: **61 passed** (including **19 real HTTP end-to-end regressions** across `pairedHttp()` and Hub fetch routes)
     - `test/routing.test.ts`: **24 passed**
     - `test/fleet.test.ts`: **18 passed**
     - `test/m3-bus.test.ts`: **31 passed**
     - `test/m3-http.test.ts`: **10 passed**
     - `test/fleet-assets.test.ts`: **4 passed**
     - `test/liveness.test.ts`: **13 passed**
     - `test/agent-registry.test.ts`: **2 passed**

3. **Independent Argon Adversarial Probe Suite (`.local/tmp/m6-rereview-probes.test.ts`)**:
   ```bash
   taskset -c 0 env UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1 TMPDIR="$PWD/.local/tmp" npm test -- --config .local/tmp/vitest.probes.config.ts .local/tmp/m6-rereview-probes.test.ts
   ```
   - **Result**: **1 test file passed, 5 passed (5 total), 0 failed** (`3.26s`) — independently exercising F1 circular sweep wrap-around (>100 low-ID valid offers + high-ID invalid routed offer) and SQL trigger rollback, F2 replacement-claim conflict across `/plan`, `/tasks`, `/launches` and queued `admission_hold`, F3 `require_claim` transition on offered legacy vs. explicit read work, F4 `fleetEditor` least-privilege and refresh-token invalidation, and Probe 5 F7 `registry_actor` intersection.

4. **Python Host & CLI Unit Tests**:
   ```bash
   taskset -c 0 env UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1 PYTHONPATH=host TMPDIR="$PWD/.local/tmp" python3 -m unittest host.tests.test_fleet host.tests.test_routing host.tests.test_bus host.tests.test_cli -v
   ```
   - **Result**: **42 passed, 0 failed** (`4.792s`):
     - `host.tests.test_fleet`: 18 passed
     - `host.tests.test_routing`: 5 passed
     - `host.tests.test_bus`: 10 passed
     - `host.tests.test_cli`: 9 passed

5. **Resource & Scratch Verification**:
   - Host `MemAvailable`: ~37.6 GiB (>= 10 GiB gate)
   - Root filesystem free: ~74 GiB (>= 50 GiB gate)
   - `.local/tmp` scratch usage: < 1 MiB (<= 35 MiB cap)

---

## Honest Source vs. Runtime & Milestone Boundaries

- **Pre-land M6 code review only**: This `ACCEPT` verdict covers the 5-file M6 ownership-aware editable admission diff (`213fcf61780b9a02645c747f1a13104a2e55b321..71349b42ec88f9a0a1e228d4411e1c716c2d8e0f`). It is not the post-first-milestone Argon direction review, and it does not accept whole-platform completion.
- **Separate milestones remain separate**: M1 conversation API/retention repairs, F7 project-qualified agent registry migration (`0399`), and M8a host execution holds are reviewed and tracked separately. When F7 is later integrated onto `main`, `src/claim_admission.ts:197-204` should use `credentialActor(this.sql, credential)` so legacy bare credentials migrated via `registry_actor` match `caller.actor`.
- **Control-plane admission vs. OS sandboxing**: `access.mode: "read"` and `access.mode: "edit"` enforce Hub control-plane admission and scope exclusivity prior to host `/claim`; they do not provide OS-level filesystem sandboxing once a host process starts.
- **Local synthetic fixtures vs. physical execution**: All Worker Durable Object and Python host tests executed against local Miniflare/Vitest SQLite and unit mocks, not live Cloudflare deployment or physical multi-host launcher runs.
