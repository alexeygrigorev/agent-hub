# Independent Review — Repaired M4 Conversation Links, M5 Conversation Pings, M7 Role Leases & Composed Current Main (`254091a9b1764638b43df58f0749a10778d62179` & `aec97138afd5bccd9cd053794d85ee6dcfef548f`)

- **Verdict**: **`SCOPED ACCEPT`**
- **Reviewed Commits**:
  - **R2 Retention Repair + Original Review Merge Pin**: `254091a9b1764638b43df58f0749a10778d62179` (`chore: record original links pings review before r2 report [skip ci]`, parents `3518a850c0fd377b6bc7894912a4fcb83b3cdbe9` and `4efa4a769755f59bb4569fdd044284d1e5451559`)
  - **Extended Final-Current-Main Composed Pin**: `aec97138afd5bccd9cd053794d85ee6dcfef548f` (`chore: compose retention repair with accepted intake main [skip ci]`, parents `254091a9b1764638b43df58f0749a10778d62179` and `bc796013d9f522d8a2e3ea050f7b264e2fabfc41`)
- **Ancestor & Component Commits Verified**:
  - `3518a850c0fd377b6bc7894912a4fcb83b3cdbe9` (`test(conversations): expand retention and retirement regressions [skip ci]`)
  - `3bbb41d42fe75a78dce59342cedbdec6ff32bc94` (`fix(conversations): align archived parent and retirement predicates [skip ci]`)
  - `be3b0184a2413adbb614dc7d83c542c0059f481b` (`chore: seed links pings r2 retention fix on current main [skip ci]`, parents `037bf039ec5ae01799a1cc863d31178a76107cdf` and `f6ec420cec3ab28fbea218dda478539b96fd4bbc`)
  - `4efa4a769755f59bb4569fdd044284d1e5451559` (`docs(review): add independent review of M4/M5/M7 candidate f6ec420 [skip ci]`, direct parent `f6ec420cec3ab28fbea218dda478539b96fd4bbc`)
  - `bc796013d9f522d8a2e3ea050f7b264e2fabfc41` (`docs(intake): record independent M12a intake review [skip ci]`, parent `b2b7a98d2431dfcb110e589db083cb025be52256` over `037bf039ec5ae01799a1cc863d31178a76107cdf`)
- **Implementer of Retention Repair (`3bbb41d` / `3518a85`)**: `platform-links-pings-r2/gpt-6` (Codex session `0e477d4f`, GPT-6 family, reasoning `high`, minor version `UNKNOWN`)
- **Reviewer**: `platform-links-pings-r2-review/gemini-4-argon-high` (Antigravity session `95d52bae-1116-48d5-8aee-1b459650a58c`, Argon high reasoning, different model family from GPT-6 implementer)
- **Date**: `2026-10-17`

Review: approved sha=aec97138afd5bccd9cd053794d85ee6dcfef548f reviewer=platform-links-pings-r2-review/gemini-4-argon-high
Review: approved sha=254091a9b1764638b43df58f0749a10778d62179 reviewer=platform-links-pings-r2-review/gemini-4-argon-high

---

## 1. Executive Summary & Findings Summary

Both `254091a9b1764638b43df58f0749a10778d62179` (the repaired M4 + M5 + M7 candidate with the immutable original review report merged) and `aec97138afd5bccd9cd053794d85ee6dcfef548f` (its composition with accepted `origin/main` `bc796013d9f522d8a2e3ea050f7b264e2fabfc41` including M12a private intake) are **accepted (`SCOPED ACCEPT`)** with **zero defects (`0` HIGH, `0` MEDIUM, `0` LOW)**.

| Prior / Current Item | Severity | Status on `254091a` & `aec97138` | Verification Summary |
| :--- | :--- | :--- | :--- |
| **Prior F1** (`docs/reviews/CONVERSATION-LINKS-PINGS.md` §3.1): Archived-parent header deletion guard vs discovery/selection SQL mismatch (`A > M + 1` livelock, held-parent HOL blocking before `LIMIT`, missing frozen `intent_json` `$.conversationId` guard) | **HIGH** | **RESOLVED** | `src/conversations.ts:23-35` defines `ARCHIVED_PARENT_UNBLOCKED_SQL` and `ARCHIVED_PARENT_WORK_SQL` and shares them across `hasExpiredRows()` (`:1060`), Category 3 `hasWork` (`:1178`), `runArchivedStep()` candidate selection before `ORDER BY c.archived_at ASC, c.id ASC LIMIT ?` (`:1326`), and the final parent header deletion guard (`:1480`). Unblocked parents delete residual `conversation_automation_grants` inside `runArchivedStep()` before deleting the parent header; blocked parents drain `conversation_messages`, `conversation_members`, and `conversation_links`, then report `0` work and consume `0` candidate slots. Both `check_json` (`$.action.conversation_id`) and valid frozen `intent_json` (`$.conversationId`) on non-`done` slots pin the archived parent header. |
| **Prior F2** (`docs/reviews/CONVERSATION-LINKS-PINGS.md` §3.2): Category 0 (`slots`) retirement candidate SQL selected receipt-mismatched orphan slots before `ORDER BY s.name LIMIT ?`, stalling `slots` retirement and leaving `hasMore === true` | **MEDIUM** | **RESOLVED** | `src/conversations.ts:1120-1128` (`hasWork`) and `:1435-1437` (`runAutomationRetirementStep` Category 0) require `NOT EXISTS (SELECT 1 FROM conversation_automation_receipts r WHERE r.id = 'check:' || s.name || ':' || s.slot_key AND r.request_json IS NOT s.intent_json)` in SQL before `ORDER BY s.name LIMIT ?`. Category 1 (`receipts`, `:1130-1137`, `:1445-1449`) also requires `NOT EXISTS (... WHERE ... (s.state != 'done' OR r.request_json IS NOT s.intent_json))`, holding mismatched receipts even when `s.state = 'done'` so Category 1 cannot erase a mismatched receipt and allow Category 0 to unsafe-retire the slot on a subsequent pass. |
| **New Findings on `254091a` and `aec97138`** | — | **NONE (`0`)** | All `138` M4/M5/R2 Worker SQLite tests (`17` R2 regressions + `9` composed + `112` cohorts), `5` novel Argon adversarial probes, `36` M12a Worker SQLite tests (`test/intake.test.ts`), `7` M12a host CLI tests (`host/tests/test_intake.py`), `tsc --noEmit`, and `wrangler deploy --dry-run` pass with `0` failures. |

---

## 2. Mechanical Verification of Envelopes, Ancestry, and Changed Paths

Independent mechanical checks were executed and recorded in `<review-worktree>/.local/review/manifest-and-ancestry-verification.json` and `<review-worktree>/.local/review/combined-current-manifest-verification.json`:

1. **R2 Source Manifest (`254091a9b1764638b43df58f0749a10778d62179`)**:
   - All `221` tracked paths and SHA-256 digests in `git ls-tree -r 254091a9b1764638b43df58f0749a10778d62179` match `<coordination-workspace>/.local/platform/combined-r2-source-manifest.json` (`0` mismatches; compact JSON SHA-256 `47797a54595ca83ad459ffb13dae37516361edf24861356936a4ff85f7b92a72`).
   - Preserves all `209` baseline paths from `037bf039ec5ae01799a1cc863d31178a76107cdf` (including `docs/reviews/CLAIM-ADMISSION.md`, SHA-256 `9f432b315beaa2d37c3496b70108c45ba3d0194885a1c42371b0d55e103609e6`) plus `12` M4/M5/M7/R2 paths.
   - `be3b0184a2413adbb614dc7d83c542c0059f481b` is the exact byte union of `037bf039ec5ae01799a1cc863d31178a76107cdf` and `f6ec420cec3ab28fbea218dda478539b96fd4bbc` (`0` mismatches across `219` files).
   - `git diff --name-only be3b0184a2413adbb614dc7d83c542c0059f481b..254091a9b1764638b43df58f0749a10778d62179` contains **only** the 6 expected paths:
     - `docs/CONVERSATION-LINKS.md`
     - `docs/CONVERSATION-PINGS.md`
     - `docs/reviews/CONVERSATION-LINKS-PINGS.md`
     - `src/conversations.ts`
     - `test/conversation-links-pings-regressions.test.ts`
     - `test/vitest.conversation-links-pings-r2.config.ts`
   - `git diff --name-only 3518a850c0fd377b6bc7894912a4fcb83b3cdbe9..254091a9b1764638b43df58f0749a10778d62179` contains **only** `docs/reviews/CONVERSATION-LINKS-PINGS.md` (blob `ba01e67e32e84053ce44e15343bbf6a67e9aab2f`, SHA-256 `2746862d311a0ed68b32db894ece669db4d28447fe39593b466d52ef199e28f4`, byte-identical to `4efa4a769755f59bb4569fdd044284d1e5451559`). All runtime, test, and contract files in `254091a9b1764638b43df58f0749a10778d62179` are 100% byte-identical to `3518a850c0fd377b6bc7894912a4fcb83b3cdbe9`.
   - All `42` original `.test.ts` files and all `98` original frontend + host test paths (`test/**` and `host/tests/**` present in `be3b0184a2413adbb614dc7d83c542c0059f481b`) are 100% byte-identical between `be3b0184a2413adbb614dc7d83c542c0059f481b` and `254091a9b1764638b43df58f0749a10778d62179` (`0` weakened assertions or modified existing tests).
2. **Extended Final-Current-Main Composed Manifest (`aec97138afd5bccd9cd053794d85ee6dcfef548f`)**:
   - All `228` tracked paths and SHA-256 digests in `git ls-tree -r aec97138afd5bccd9cd053794d85ee6dcfef548f` match `<coordination-workspace>/.local/platform/combined-current-source-manifest.json` (`0` mismatches; compact JSON SHA-256 `ee2e0f158e46067ed77cbf8ac9addcbed55d4a4988c6a2c8fece1786dbcf70a0`).
   - `git diff --name-only 254091a9b1764638b43df58f0749a10778d62179..aec97138afd5bccd9cd053794d85ee6dcfef548f` is **only** the 10 M12a paths from `bc796013d9f522d8a2e3ea050f7b264e2fabfc41` (`docs/INTAKE-TASKS.md`, `docs/reviews/INTAKE-TASKS.md`, `host/agent_hub_host/__main__.py`, `host/agent_hub_host/cli.py`, `host/agent_hub_host/intake.py`, `host/tests/test_intake.py`, `src/hub.ts`, `src/intake.ts`, `test/intake.test.ts`, `test/vitest.intake.config.ts`), with all 9 non-`hub.ts` files 100% byte-identical to `bc796013d9f522d8a2e3ea050f7b264e2fabfc41`.
   - `git diff --name-only bc796013d9f522d8a2e3ea050f7b264e2fabfc41..aec97138afd5bccd9cd053794d85ee6dcfef548f` is **only** the 14 M4/M5/M7/R2 paths from `254091a9b1764638b43df58f0749a10778d62179`, with all 13 non-`hub.ts` files (`src/conversations.ts`, `src/conversation-links.ts`, `src/conversation-automation.ts`, `src/conversation-hub.ts`, `src/jobs.ts`, `src/role_leases.ts`, `host/agent_hub_host/jobs.py`, `docs/CONVERSATION-LINKS.md`, `docs/CONVERSATION-PINGS.md`, `docs/reviews/CONVERSATION-LINKS-PINGS.md`, `test/conversation-links-pings.test.ts`, `test/conversation-links-pings-regressions.test.ts`, `test/vitest.conversation-links-pings-r2.config.ts`) 100% byte-identical to `254091a9b1764638b43df58f0749a10778d62179`.
   - Only `src/hub.ts` (`50f67c0ec73a5816bbe094b4e6c394aa4326feeb7500144ae64535560743829c`) required a three-way merge between `254091a9b1764638b43df58f0749a10778d62179` and `bc796013d9f522d8a2e3ea050f7b264e2fabfc41`.
3. **Mechanical Note on Historical Commit Abbreviation in Prior Report**:
   - In the immutable prior review report `docs/reviews/CONVERSATION-LINKS-PINGS.md:15`, the accepted host-binding ancestor commit was abbreviated as `85fd279`. Mechanically checking `git log` confirms the actual Git SHA in the repository ancestry is `85fdcec5e8c2fcfe0cb6d49134732b27f64e7896` (`fix: bind agent credentials and root identity to registered hosts [skip ci]`), which was reviewed in `80921a67da3ded48ce27c772619c850cfc0e34d3`.

---

## 3. Deep Verification of F1 & F2 Repairs and Shared `src/hub.ts` Composition

### 3.1 Closure of Prior Finding F1 (HIGH — Archived-Parent Predicate Alignment & Frozen-Intent Guard)

In `src/conversations.ts:23-35`:

```ts
const ARCHIVED_PARENT_UNBLOCKED_SQL = `NOT EXISTS (
  SELECT 1 FROM conversation_automation_receipts r WHERE r.conversation_id = c.id
) AND NOT EXISTS (
  SELECT 1 FROM hub_check_slots s
  WHERE s.state != 'done'
    AND ((CASE WHEN json_valid(s.check_json) THEN json_extract(s.check_json, '$.action.conversation_id') END) = c.id
      OR (CASE WHEN json_valid(s.intent_json) THEN json_extract(s.intent_json, '$.conversationId') END) = c.id)
)`;
const ARCHIVED_PARENT_WORK_SQL = `(
  EXISTS (SELECT 1 FROM conversation_messages m WHERE m.conversation_id = c.id)
  OR EXISTS (SELECT 1 FROM conversation_members cm WHERE cm.conversation_id = c.id)
  OR EXISTS (SELECT 1 FROM conversation_links l WHERE l.conversation_id = c.id)
  OR (${ARCHIVED_PARENT_UNBLOCKED_SQL})
)`;
```

- **Shared Predicate Alignment (`hasExpiredRows`, `hasWork`, `runArchivedStep`, header delete guard)**:
  - `hasExpiredRows()` (`src/conversations.ts:1060`) checks `WHERE c.archived_at IS NOT NULL AND c.archived_at <= ? AND ${ARCHIVED_PARENT_WORK_SQL}`.
  - Category 3 `hasWork` in `retentionCleanup()` (`src/conversations.ts:1178`) uses the identical predicate.
  - `runArchivedStep()` (`src/conversations.ts:1326`) filters candidate parents with `WHERE c.archived_at IS NOT NULL AND c.archived_at <= ? AND ${ARCHIVED_PARENT_WORK_SQL} ORDER BY c.archived_at ASC, c.id ASC LIMIT ?`.
  - When a selected parent has no remaining `conversation_messages`, `conversation_members`, or `conversation_links` and satisfies `ARCHIVED_PARENT_UNBLOCKED_SQL`, `runArchivedStep()` (`src/conversations.ts:1443-1486`) deletes residual `conversation_automation_grants` for `c.id` within the step budget and then deletes the `conversations` header guarded by `AND ${ARCHIVED_PARENT_UNBLOCKED_SQL}`.
- **Why this closes all three F1 failure modes**:
  1. **No `A > M + 1` livelock**: An unblocked archived parent with `M` child rows and `A > M + 1` automation grants drains its `M` child rows, then on subsequent `limit=1` passes remains selected by `ARCHIVED_PARENT_WORK_SQL` (since `ARCHIVED_PARENT_UNBLOCKED_SQL` is true), deletes its remaining grants 1-by-1 in `runArchivedStep()`, and finally deletes the `conversations` header (`rowsDeleted > 0` on every pass until `hasMore === false`).
  2. **No head-of-line (HOL) blocking by held parents**: An older archived parent held by an undrained receipt or non-`done` slot still satisfies `ARCHIVED_PARENT_WORK_SQL` while it has messages, members, or links, so its child rows still drain cleanly. Once its messages, members, and links are empty, `ARCHIVED_PARENT_WORK_SQL` evaluates to `false`, so it is excluded before `ORDER BY c.archived_at ASC, c.id ASC LIMIT ?` and neither starves later eligible archived parents nor triggers false `hasMore: true` / alarm reschedule loops.
  3. **Frozen `intent_json` (`$.conversationId`) coverage**: Even if `hub_check_slots.check_json` was reconfigured to point to another conversation or nulled (`{}`) after freezing an in-flight intent, `CASE WHEN json_valid(s.intent_json) THEN json_extract(s.intent_json, '$.conversationId') END = c.id` pins the archived parent header while `s.state != 'done'`. Malformed or `NULL` `intent_json` is guarded by `json_valid(s.intent_json)`.

### 3.2 Closure of Prior Finding F2 (MEDIUM — Pre-`LIMIT` Mismatch Filtering & `done`-Slot Mismatch Hold)

In `src/conversations.ts:1120-1137` and `1435-1449`:

- **Category 0 (`slots`)**: Both `hasWork` and the candidate `SELECT s.name, s.slot_key FROM hub_check_slots s ... ORDER BY s.name LIMIT ?` require:
  - `s.state = 'done'`
  - Non-empty conversation reference in `check_json` (`$.action.conversation_id`) or valid `intent_json` (`$.conversationId`)
  - Orphaned check/conversation/grant authority (`NOT EXISTS` in `hub_checks`, `conversations`, or active `conversation_automation_grants`)
  - `NOT EXISTS (SELECT 1 FROM conversation_automation_receipts r WHERE r.id = 'check:' || s.name || ':' || s.slot_key AND r.request_json IS NOT s.intent_json)`
- **Category 1 (`receipts`)**: Both `hasWork` and the candidate `DELETE FROM conversation_automation_receipts WHERE id IN (SELECT r.id ... ORDER BY r.id LIMIT ?)` require:
  - `NOT EXISTS (SELECT 1 FROM hub_check_slots s WHERE r.id = 'check:' || s.name || ':' || s.slot_key AND (s.state != 'done' OR r.request_json IS NOT s.intent_json))`
- **Why this closes F2 without creating a cross-category erasure hazard**:
  - Mismatched orphan `done` slots (`r.request_json IS NOT s.intent_json`) are filtered out in SQL **before** `ORDER BY s.name LIMIT ?`, so any number of earlier mismatched slots (`a-bad-0..N`) never starve later retireable slots (`z-good`) even at `limit=1`, and never cause Category 0 to report `hasWork = true` with `deleted = 0`.
  - Holding mismatched receipts in Category 1 even when `s.state = 'done'` ensures Category 1 cannot delete the mismatched receipt first and thereby turn the mismatched slot into an unblocked Category 0 candidate on the next pass. Both the slot (`intent_json`) and the receipt (`request_json`) remain preserved until reconciled.

### 3.3 Verification of Shared `src/hub.ts` Merge on `aec97138afd5bccd9cd053794d85ee6dcfef548f`

Comparing `src/hub.ts` on `aec97138afd5bccd9cd053794d85ee6dcfef548f` against both parents (`254091a9b1764638b43df58f0749a10778d62179` and `bc796013d9f522d8a2e3ea050f7b264e2fabfc41`) confirms:

- `Hub` constructor (`src/hub.ts:138-140`) initializes `this.intakeService = new IntakeService(this.sql, fn => this.ctx.storage.transactionSync(fn))` and calls `this.intakeService.init()` after `this.initDb()`.
- `Hub.fetch()` (`src/hub.ts:323-353`) routes `/api/intake` and `/api/intake/*` to `this.intakeService.handle(...)`, enforcing `mixed_authority` (`403`) when both `Authorization` and `Cookie` headers are present, constant-time operator-token fallback (`tokenAdminSubject`) when no registered credential matches, and CSRF/session verification on cookie requests.
- `Hub.fetch()` (`src/hub.ts:388`) preserves `role_binding` on `/api/host/poll` offers, and `handleJobsRoutes` (`src/hub.ts:1836-1953`) preserves `/api/roles/:project/:name`, `/api/roles/:project/:name/jobs`, `/api/jobs/:id/execute`, and `hostCredentialId` binding on `/api/jobs/:id/claim` and `/api/jobs/:id/decline`.
- In `src/conversation-hub.ts`, `Hub extends BaseHub` inherits `intakeService` and all `/api/intake` and `/api/roles/*` routes while adding `conversationsService`, `linksService`, and fair `busService.retentionCleanup` composition.
- Both SQLite trigger families on `agents` and `machines` (`agents_revoke_machine_binding` / `credentials` revocation in `src/agent_registry.ts` and `intake_end_registration` / `intake_end_binding` / `intake_end_machine` in `src/intake.ts:79-81`) coexist and fire atomically in the same transaction when an agent's `machine_id`, `project`, or `tag` changes (verified in Argon Probe 5).

---

## 4. Independent Verification Runs, Coverage Proof, and Resource Peaks

All commands were executed serially on CPU core `0` (`taskset -c 0`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, `umask 077`, Vitest `--configLoader runner`, private `TMPDIR` / `XDG_CACHE_HOME` under `<review-worktree>/.local/tmp`) via `<review-worktree>/.local/review/run.py`.

### 4.1 Summary of Independent Execution Runs

| Run Label | Target Pin | Command / Config | Result | Duration (`s`) | Peak Alloc (`B`) | Peak Apparent (`B`) | Peak RSS (`B`) | Peak Tasks |
| :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| `typecheck` | `254091a` | `node node_modules/typescript/bin/tsc --noEmit` | `exit 0` | `2.723` | `524,288` | `510,219` | `240,242,688` | `6` |
| `regressions-r2` | `254091a` | `vitest run --config test/vitest.conversation-links-pings-r2.config.ts --configLoader runner` | `17/17 PASS` | `10.457` | `11,337,728` | `11,282,654` | `686,235,648` | `28` |
| `typecheck-aec` | `aec97138` | `node node_modules/typescript/bin/tsc --noEmit` | `exit 0` | `4.483` | `638,976` | `583,880` | `252,674,048` | `6` |
| `adversarial-r2-probe-v6` | `aec97138` | `vitest run --config .local/review/vitest.adversarial.config.ts --configLoader runner` | `5/5 PASS` | `13.088` | `5,459,968` | `5,335,132` | `662,245,376` | `28` |
| `regressions-r2-aec` | `aec97138` | `vitest run --config test/vitest.conversation-links-pings-r2.config.ts --configLoader runner` | `17/17 PASS` | `14.707` | `12,484,608` | `12,317,893` | `684,400,640` | `28` |
| `host-intake-unittest-v2` | `aec97138` | `python3 -m unittest discover -s host/tests -t host -p test_intake.py` | `7/7 PASS` | `2.241` | `765,952` | `632,204` | `58,318,848` | `3` |
| `composed-1` | `aec97138` | `test/conversation-links-pings.test.ts` (batch 1/2) | `5/5 PASS` | `13.819` | `12,959,744` | `12,794,936` | `609,660,928` | `28` |
| `composed-2` | `aec97138` | `test/conversation-links-pings.test.ts` (batch 2/2) | `4/4 PASS` | `9.575` | `4,775,936` | `4,598,038` | `592,949,248` | `28` |
| `conversation-integration-1` | `aec97138` | `test/conversation-integration.test.ts` | `5/5 PASS` | `9.287` | `4,235,264` | `4,050,082` | `594,223,104` | `28` |
| `conversation-links-r2-1` | `aec97138` | `test/conversation-links-r2.test.ts` (4 retention/lifecycle cases) | `4/4 PASS` | `9.121` | `4,165,632` | `3,967,372` | `582,340,608` | `28` |
| `conversation-links-1` | `aec97138` | `test/conversation-links.test.ts` (3 retention/lifecycle cases) | `3/3 PASS` | `8.980` | `3,121,152` | `2,904,865` | `580,960,256` | `28` |
| `conversation-pings-retirement-1..3` | `aec97138` | `test/conversation-pings-retirement.test.ts` (3 batches: `8+8+5`) | `21/21 PASS` | `33.381` | `7,249,920` | `7,021,035` | `603,901,952` | `28` |
| `conversation-retention-1..3` | `aec97138` | `test/conversation-retention.test.ts` (3 batches: `8+8+4`) | `20/20 PASS` | `26.875` | `6,352,896` | `6,084,516` | `615,874,560` | `28` |
| `checks-1..2` | `aec97138` | `test/checks.test.ts` (2 batches: `8+6`) | `14/14 PASS` | `17.270` | `3,715,072` | `3,425,405` | `629,710,848` | `28` |
| `conversation-pings-1..6` | `aec97138` | `test/conversation-pings.test.ts` (6 batches: `8+8+8+8+8+5`) | `45/45 PASS` | `69.927` | `6,569,984` | `6,192,675` | `655,183,872` | `28` |
| `intake-full` | `aec97138` | `test/intake.test.ts` (all 36 cases across 12 `it` blocks) | `36/36 PASS` | `20.281` | `9,240,576` | `8,782,286` | `671,031,296` | `28` |
| `bundle-dryrun-aec` | `aec97138` | `wrangler deploy --dry-run --outdir .local/tmp/bundle-dryrun` | `exit 0` | `4.028` | `3,268,608` | `2,796,379` | `405,733,376` | `14` |

*(Note: 4 intermediate probe-fixture development iterations `adversarial-r2-probe`, `adversarial-r2-probe-v2..v5`, plus `host-intake-unittest` before adding `-s host/tests -t host`, and `intake-2` before running `intake-full` are retained in `<review-worktree>/.local/review/runs.jsonl` and `<review-worktree>/.local/review/logs/` as required by the review protocol. None were product failures.)*

### 4.2 Novel Argon Adversarial Probes (`<review-worktree>/.local/review/m4-m5-r2-adversarial.probe.test.ts`)

1. **`Probe 1`**: Re-runs the exact original `F1a` (`4` grants, `2` members, `limit=1`), `F1b` (older receipt-held empty archived parent ahead of younger eligible archived parent, `limit=1`), and `F2` (orphan `done` slot `a-mismatch` with `r.request_json != s.intent_json` ahead of `b-valid`, `limit=1`) fixtures from the first review (`<prior-review-worktree>/.local/review/m4-m5-adversarial.probe.test.ts`), asserting the **repaired** behavior:
   - `F1a` converges in `7` steps (`[1, 1, 1, 1, 1, 1, 1]`) with `hasExpiredRows === false` and `0` remaining conversations/grants.
   - `F1b` skips the drained receipt-held parent on pass 1 and deletes the younger eligible parent + grant on passes 1–2 (`[1, 1]`), then returns `hasMore: false` and `hasExpiredRows: false` while preserving the held parent header and receipt.
   - `F2` skips `a-mismatch`, retires `b-valid` on pass 1 (`rowsDeleted: 1, hasMore: false`), and reports `hasExpiredRows: false` while preserving `a-mismatch` and its mismatched receipt.
2. **`Probe 2`**: Tests all 4 archived-parent blocking variants (`conversation_automation_receipts`, non-`done` slot with `check_json` `$.action.conversation_id`, non-`done` slot with `check_json = '{}'` and frozen `intent_json` `$.conversationId`, and `done` slot with mismatched receipt `r.request_json != s.intent_json`) alongside an unblocked archived parent:
   - Verifies that all 4 blocked parents drain their `conversation_messages`, `conversation_members`, and `conversation_links` to `0`, the unblocked parent is completely removed, and once children are drained the 4 held parents report `hasMore === false` and `hasExpiredRows === false` (zero alarm spam).
   - Verifies that resolving each hold one-by-one makes that exact parent eligible on the next `retentionCleanup(t, 2)` pass until all 4 headers are deleted.
3. **`Probe 3`**: Verifies that:
   - Non-orphan `done` slots (where `hub_checks`, `conversations`, and an active `conversation_automation_grants` row still exist) are never retired by Category 0.
   - A prefix of `5` mismatched orphan `done` slots (`a-bad-0..4`) never starves `3` later valid orphan `done` slots (`z-good-0..2`) at `limit=1`, while all `5` mismatched slots and their `5` receipts remain intact.
   - Positive current follow-ups on an active conversation continue to post unread `system:conversation-checks` notes without advancing recipient `last_read_seq`, and replaying an old `FollowUpIntent` after slot retirement fails closed with `automation_slot_retired`.
4. **`Probe 4`**: Verifies on the composed pin that M7 `RoleLeases` and `JobsService` preserve `0`-scope role-leased job credentials (`403` on `/api/conversations` and `/api/roles/*`), return fail-closed `HOLD` (`execution_intent_already_recorded`) on duplicate `/api/jobs/:id/execute`, block cross-project `writer-run` claims for the same UTC date (`2099-08-01`) with `409` when an earlier project's execution intent is unresolved after lease expiry, allow a different UTC date (`2099-08-02`) to claim and record execution intent cleanly, and keep unqualified `writer-run` execution fail-closed (`HOLD` / `unqualified_profile_effect`).
5. **`Probe 5`**: Verifies on `aec97138afd5bccd9cd053794d85ee6dcfef548f` that `IntakeService` (`/api/intake/rules`, `/api/intake/tasks`, `/api/intake/grants`, `/api/intake/handoff`, `/api/intake/tasks/:id/continue`), `ConversationsService` (`/api/conversations`), and `busService.retentionCleanup` operate together in a single `Hub` Durable Object instance, and that rebinding `agents.machine_id` atomically fires both `agents_revoke_machine_binding` (revoking `credentials.revoked_at` and returning `401` on `/api/intake/handoff`) and `intake_end_binding` (recording `revocation_reason: "registry_binding_changed"` in `intake_grants`).

### 4.3 Coverage Matrix & Resource Peaks (`<review-worktree>/.local/review/coverage-proof.json`, `<review-worktree>/.local/review/final-summary.json`)

- **M4 + M5 + R2 Target Matrix (`138` distinct Worker SQLite tests)**:
  - `17/17` R2 regression tests (`test/conversation-links-pings-regressions.test.ts`) — PASS on both `254091a` and `aec97138`
  - `9/9` composed M4+M5 tests (`test/conversation-links-pings.test.ts`) — PASS (`9` unique)
  - `112/112` causally selected original M4/M5/retention/automation/checks tests — PASS (`112` selected, `112` executed, `112` unique, `0` missing, `0` extra, `0` duplicates)
- **Additional Reviewer Verification on `aec97138`**:
  - `5/5` novel Argon adversarial probes (`m4-m5-r2-adversarial.probe.test.ts`) — PASS
  - `36/36` M12a intake Worker SQLite assertions/cases (`test/intake.test.ts`) — PASS
  - `7/7` M12a host intake unit tests (`host/tests/test_intake.py`) — PASS
  - **Total distinct passing tests executed on `aec97138afd5bccd9cd053794d85ee6dcfef548f`**: **`186`**
- **Resource & Scratch Peaks Across All `35` Logged Commands**:
  - `peak_allocated_bytes`: `12,959,744` B (`12.36 MiB`, below `16 MiB` early guard and `20 MiB` hard cap)
  - `peak_apparent_bytes`: `12,794,936` B (`12.20 MiB`, below `16 MiB` early guard and `20 MiB` hard cap)
  - `peak_group_rss_bytes`: `686,235,648` B (`654.45 MiB`, below `1,500 MiB` worker memory limit)
  - `peak_tasks`: `28` (below `256` tasks limit)
  - `cgroup_memory_current_peak_bytes`: `802,222,080` B (`765.06 MiB`)
  - `cgroup_pids_current_peak`: `44`
  - `guards_triggered`: `[]` (`0`)

---

## 5. Portable Evidence Locators & Scope Boundaries

### 5.1 Portable Evidence Locators

- **Reviewer Private Evidence (`<review-worktree>/.local/review/`)**:
  - `<review-worktree>/.local/review/manifest-and-ancestry-verification.json` (`254091a` 221-pair manifest + ancestry + 6-path diff + 42/98 test preservation proof)
  - `<review-worktree>/.local/review/combined-current-manifest-verification.json` (`aec97138` 228-pair manifest + parent diff + `src/hub.ts` merge proof)
  - `<review-worktree>/.local/review/m4-m5-r2-adversarial.probe.test.ts` (5 novel Argon adversarial probes)
  - `<review-worktree>/.local/review/coverage-proof.json` (`138/138` M4/M5/R2 matrix + `5/5` Argon probes + `36/36` M12a Worker + `7/7` M12a host = `186` distinct passes; `0` missing, `0` extra, `0` duplicates)
  - `<review-worktree>/.local/review/final-summary.json` and `<review-worktree>/.local/review/runs.jsonl` (all `35` command receipts and resource peaks)
  - `<review-worktree>/.local/review/logs/*.results.json` (per-batch Vitest JSON reports)
- **Prior Reviewer Readonly Evidence (`<prior-review-worktree>/.local/review/`)**:
  - `<prior-review-worktree>/.local/review/m4-m5-adversarial.probe.test.ts` and `<prior-review-worktree>/.local/review/logs/72af-r4-adversarial.json` (preserved readonly; originally demonstrated `F1a`/`F1b`/`F2` defects on `72af460` / `f6ec420`)
- **Implementer Readonly Evidence (`<implementer-worktree>/.local/links-pings-r2/`)**:
  - `<implementer-worktree>/.local/links-pings-r2/{FINAL.md,final-summary.json,coverage-proof.json,runs.jsonl,scope-and-preservation.json,report-merge-source-equivalence.json}`
- **Coordination Source Proofs (`<coordination-workspace>/.local/platform/`)**:
  - `<coordination-workspace>/.local/platform/{combined-r2-source-proof.json,combined-r2-source-manifest.json,combined-current-source-proof.json,combined-current-source-manifest.json}`

### 5.2 Scope & Operational Boundaries

1. **Source Acceptance Scope**: Covers exact commits `254091a9b1764638b43df58f0749a10778d62179` and `aec97138afd5bccd9cd053794d85ee6dcfef548f` for normal `main` integration.
2. **Unqualified `writer-run` Effect Backend**: Remains intentionally fail-closed (`HOLD`, `reason: "unqualified_profile_effect"`) in `host/agent_hub_host/jobs.py` until the downstream writer artifact consumer is project-qualified in its dedicated milestone.
3. **Separate Gates**: Live Cloudflare Worker deployment/runtime verification (M8/M11), physical cross-host assignment/recovery/backfill, and MCP/adoption gates remain separate from this source acceptance.
