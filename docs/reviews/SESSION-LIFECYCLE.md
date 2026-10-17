# Independent Argon Review — M15 Session Lifecycle Plan Extension (`2119268`) & Module-Only First Slice (`4e0f864`)

- **Reviewer**: `platform-lifecycle-review` / `gemini-4-argon-high` (native session `4ae01ad6-b234-46a4-ac99-560eef4da8b2`, engine `antigravity`, isolated worktree `/home/alexey/git/agent-hub-wt-platform-lifecycle-review`)
- **Task**: `actual0438` (`platform-life-review-0438`)
- **Reporting To**: `platform` (`e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363`, `/home/alexey/git/agent-coordination`)
- **Date**: `2026-10-17`

Review: approved sha=211926811508eff974775436c639a72bdd9b6e37 reviewer=platform-lifecycle-review/gemini-4-argon-high
Review: changes-requested sha=4e0f8640add93e828955ecd1b2bcfa35d55b5133 reviewer=platform-lifecycle-review/gemini-4-argon-high

---

## 1. Separate Verdicts & Mechanically Derived Pin Lineage

| Pin | Repository | Full Commit SHA (40-hex) | Short | Sole Parent | Commit Date (ISO) | Commit Subject | Agent Trailer | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Pin 1 (M15 Plan + `hub-zai` Custody)** | `/home/alexey/git/agent-coordination` | `211926811508eff974775436c639a72bdd9b6e37` | `2119268` | `24bc7cc` | `2026-10-17T13:59:24+02:00` | `docs: assign lifecycle registry and hub ops handoff [skip ci]` | `Agent: platform/e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363 (codex)` | **SCOPED ACCEPT** (`approved`) |
| **Pin 2 (M15a/b Module-Only Slice)** | `/home/alexey/git/agent-hub-wt-platform-lifecycle-review` | `4e0f8640add93e828955ecd1b2bcfa35d55b5133` | `4e0f864` | `6bc91a1` | `2026-10-17T13:56:54+02:00` | `M15a/b: session lifecycle projection and guarded cleanup-plan seams with synthetic tests [skip ci]` | `Agent: platform-lifecycle (opencode; native ee366bf6-6fca-4303-91c7-fee061bb8dbe)` | **REVISE** (`changes-requested`) |

1. **Verdict on Pin 1 (`211926811508eff974775436c639a72bdd9b6e37` — M15 Plan Extension & `hub-zai` Handoff Sequencing): SCOPED ACCEPT.**
   The coordinator diff (`docs/platform/PLAN.md`, `docs/platform/STATUS.md`, `docs/platform/ARGON-PLAN-REVIEW.md`; 3 files changed, 183 insertions, 23 deletions) faithfully translates Alexey's Addendum 5 (`hub-intake-brief.md`) and the 17 October session-triage requirements into `M15a`–`M15d`, preserves strict priority ordering (`M12e` real cross-host AI loop first, `M12b/c` intake assignment/backfill/handoff second, `M15` independent on spare capacity, unrelated `M9` accounting UI/DTO not a prerequisite for `M12`), and bounds `hub-zai` handoff recovery (`actual0437`) sequentially behind the primary `M12` source freeze without re-running the invalidated unfiltered pipeline script. Section 2 records the binding implementation gate IDs (`M15-IG1`–`M15-IG7`).
2. **Verdict on Pin 2 (`4e0f8640add93e828955ecd1b2bcfa35d55b5133` — M15a/b Module-Only First Slice): REVISE (`changes-requested`).**
   The 6-file additive slice (`docs/SESSION-LIFECYCLE.md` [6,723 B], `src/session_lifecycle.ts` [17,346 B], `host/agent_hub_host/session_lifecycle.py` [12,919 B], `test/session-lifecycle.test.ts` [6,277 B], `host/tests/test_session_lifecycle.py` [8,177 B], `test/vitest.session-lifecycle.config.ts` [221 B]; 936 insertions total) respects file-scope boundaries (no unapproved edits to `src/hub.ts`, `src/schema.ts`, `src/session_control.ts`, or `host/agent_hub_host/cli.py`), uses synthetic fixtures only, and passes its 9 unique author test cases (3 Vitest + 6 Python) and `tsc --noEmit`. However, independent code audit and guarded novel negative boundary probes uncovered **6 concrete security, correctness, and TS/Python drift counterexamples in the implemented pure functions right now** (`M15-F1`–`M15-F6` in Section 4), including cleanup-eligibility bypasses for unproven/stale `superseded` and resumed `terminal_evidence` sessions, empty/unknown ownership and target-binding bypasses, boolean/negative epoch bugs, timezone-naive timestamp drift between TS and Python, dead `unknown` fallback code, and boundary secret-truncation leakage.
3. **Explicit Open Scope (`M15c` / `M15d` / Runtime):**
   Neither Pin 1 nor Pin 2 closes full `M15`. `M15c` (durable SQLite ownership-transfer and limit-decision persistence, `/api/sessions?view=lifecycle`, `POST /api/sessions/cleanup`, `hub sessions` / `hub sessions cleanup` CLI wiring), `M15d` (host-verified `--apply` stop and worktree/branch deletion with fail-closed Git reachability, dirty/untracked/index, symlink/realpath, shared-checkout, active-cwd, lock, and TOCTOU guards plus durable uncertain-outcome reconciliation), and live enrolled-host acceptance remain **OPEN**. Pure module seams are not real Hub behavior and waive no future gate.

---

## 2. Review of Pin 1 (`2119268`) — M15 Plan Extension, Priorities & `hub-zai` Custody

### 2.1 Alignment With Briefs, Priorities & `hub-zai` Handoff (`actual0437`)

- **Priority Discipline (`PLAN.md` L86–142, L736–739, L816–838; `STATUS.md` L24–65):**
  - Keeps `M12e` (`actual0430` in `agent-hub-wt-platform-session-r3` under `a9da74f2`, followed by the real Grok Bot box $\to$ Hetzner start/send-confirm/capture/confirmed-stop loop) as Priority 1, and `M12b/c` assignment/backfill/fresh-agent `hub handoff` adoption as Priority 2.
  - Explicitly states that `M9` accounting/UI is a separate review path and not a prerequisite for `M12` session orchestration (`PLAN.md` L98–99, L118–119, L819–820).
  - Properly records withdrawal of predecessor `ad6431cf`'s guard-aborted unfiltered Vitest claims while preserving all source, patch, and failed receipts, and documents thenormal model turn end of `ea30db81`.
- **`hub-zai` Custody & Sequential Recovery (`PLAN.md` L130–137, L831–833; `STATUS.md` L52–63):**
  - Processes Alexey's `hub-zai` handoff once as `actual0437`, sequenced after `M12` source freeze under `a9da74f2` with verified read-only inventory.
  - Confirms `f5efc43` alert default already landed on `main`, forbids re-running the old `ship-alert-default.sh` unfiltered pipeline script, preserves the shared checkout and existing timers, and requires revalidated custody, domain/auth capability, enrollment, and replacement-check verification before any deployment or timer cutover.

### 2.2 Binding M15 Implementation Gates (`M15-IG1`–`M15-IG7`)

| Gate ID | Scope | Binding Contract Required for Source & Runtime Acceptance |Pin 1 Plan (`2119268`) | Pin 2 Slice (`4e0f864`) |
| :--- | :--- | :--- | :--- | :--- |
| **`M15-IG1`** | Canonical Single Truth & Additive Schema | Reuse `fleet_launches`, `fleet_sessions`, `session_control_*`, and intake `link` events on `/api/sessions` (`?view=lifecycle`) and `hub sessions`. No second registry, `/api/lifecycle` surface, controller, quota store, or lease table. | **PASS** (`PLAN.md` L752–759) | **PARTIAL** (Pure seam only; duplicates logic across TS/Py — see `M15-F6`) |
| **`M15-IG2`** | Authenticated Custody & Epoch-Fenced Transfer | Immutable `starter` from authenticated launch creation; `owner` changes only via authenticated current-custodian + integer epoch-fenced (`>= 0`) durable transfer. Generic notes, client `owner` strings, `unknown`/`legacy`/empty owners, and admin override never grant custody. `human`/Alexey sessions permanently protected. | **PASS** (`PLAN.md` L754–759, L783–788) | **FAIL** (`M15-F3`: TS allows `owner: ""`, `owner_provenance: "unknown"` ignored, Py allows `bool` epoch & `None` target IDs) |
| **`M15-IG3`** | Fresh Provenance, Multi-Flag Retention & No-Banner-Alone | Grouping (`waiting_alexey`, `limit_blocked`, `finished_stale`, `active`, `unknown`) preserves all simultaneous `flags[]` and `obligations[]`. Both `finished` and `superseded` require authoritative no-pending proof, zero obligations, and **no activity newer than the completion/report/terminal timestamp**. Missing provenance classifies as `unknown`, not `active`. | **PASS** (`PLAN.md` L760–768) | **FAIL** (`M15-F1`, `M15-F2`, `M15-F5`: `superseded` skips proof & freshness; `terminalFresh` ignores newer activity; `unknown` branch unreachable) |
| **`M15-IG4`** | Limit Reset Honesty & Durable Decisions (`M15c`) | Timezone-naive reset strings or missing `limit_zone` stay `limit_blocked_unknown_reset` and never fabricate local or UTC zones. `resume_on_reset`, `reassign`, and `drop` persist durably with actor/time/reason and re-verify fresh quota, routing, reserves, and cooldown before execution. | **PASS** (`PLAN.md` L762–775) | **FAIL / OPEN** (`M15-F4`: naive reset marked known with 2h TS/Py drift; durable decisions **OPEN M15c**) |
| **`M15-IG5`** | Privacy & Text-as-Data Redaction | Default projection emits structured reason codes only; free-text notes are opt-in (`note_text_opt_in`), redacted **before** length truncation, marked `text_as_data: true`, and never treated as a sole privacy guarantee. | **PASS** (`PLAN.md` L739–740, L767–768) | **FAIL** (`M15-F6`: `sanitizeObservationText` slices at 512 before regex redaction, leaking secret prefixes) |
| **`M15-IG6`** | Guarded Dry-Run Default & Fail-Closed Cleanup (`M15b`/`M15d`) | Dry-run default performs zero stop/delete/fetch effects. `--apply` revalidates exact `(machine_id, native_id)` UUID/incarnation, custodian epoch, containment, and zero busy/draft/pending state; worktree/branch deletion requires owned realpath under root, no symlinks, no shared checkout, no active cwd/worktree/branch users or locks, clean working tree/index/untracked, and HEAD reachable from freshly verified `origin/main`. | **PASS** (`PLAN.md` L776–797) | **FAIL / OPEN** (`M15-F1`–`M15-F3` in `planCleanup`/`validateCleanupTarget`; `--apply` & Git guards **OPEN M15d**) |
| **`M15-IG7`** | Single Authority Split (Server Classifies, Host Validates Effects) | Server owns canonical SQLite joins, classification, projection, cleanup planning, and durable decisions; Python host module owns host observation probes, fail-closed target/process/worktree execution guards, and thin CLI/HTTP transport without maintaining a drifting second copy of server classification/planning rules. | **PASS** (`PLAN.md` L746–797) | **FAIL** (`M15-F6`: full TS/Py duplication with 3 concrete semantic divergences) |

---

## 3. Private Triage Fixture Coverage Audit (`session-cleanup-2026-10-17.md`)

Using the 17 October triage fixture strictly as private input data (with synthetic equivalents in tests and zero private session tags or screen text in Git), the table below maps each real operational scenario to Pin 2 (`4e0f864`):

| Triage Fixture Scenario Class | Required Product Behavior | Pin 2 (`4e0f864`) Seam Behavior | Status |
| :--- | :--- | :--- | :--- |
| **Protected human / foreign-coordinator / active worktree sessions** | Never eligible for cleanup; admin does not override foreign custody | `planCleanup` blocks `human_owned: true` and `owner !== callerOwner`; fails to block `owner_provenance: 'unknown'` or TS `owner: ""` | **FAIL (`M15-F3`)** |
| **Completed sessions with report + follow-up / human action still open** | Retain open obligations (`followup`, `blocker`, `pending_approval`); block cleanup | `pending_obligations` / `awaiting_alexey` retained in `obligations[]` / `flags[]` and blocked in `planCleanup` when passed | **PASS (Seam)** |
| **Superseded sessions where successor shipped the fix** | Eligible only when superseded **and** authoritative no-pending proof holds **and** no newer activity occurred | `classifySession` `superseded` branch (`L189`) skips `authoritative_no_pending_proof` and `reportFreshEnough` whenever `report_written && screen_done` is true | **FAIL (`M15-F1`)** |
| **Already-vanished sessions / missing worktree under session** | Missing worktree classified `missing_worktree_unknown` (`unknown` group); never treated as confirmed stopped or cleanup-eligible | `worktree_missing: true` returns `group: 'unknown', state: 'missing_worktree_unknown'` while preserving other flags | **PASS (Seam)** |
| **Dirty worktree (`uncommitted edit`) or unattributed pre-existing worktree** | Retain and list only; never delete dirty, unmerged, shared, or unowned worktrees | `M15d` worktree/git inspection not yet implemented; `validateCleanupTarget` accepts blank/None IDs and `'unknown'` owner | **FAIL (`M15-F3`) / OPEN (`M15d`)** |
| **Quota limit-blocked session with timezone-unqualified reset timestamp** | Classify `limit_blocked_unknown_reset`; record durable `resume_on_reset` / `reassign` / `drop` decision | Naive reset timestamp without zone is misclassified as `limit_blocked` (`reset=known`) with 2h TS/Py timezone drift; durable decisions open | **FAIL (`M15-F4`) / OPEN (`M15c`)** |
| **Shared-checkout branch with uncommitted changes** | Never delete shared checkout or branch in use; hand off or merge task | Shared-checkout and git-worktree guards remain **OPEN (`M15d`)** | **OPEN (`M15d`)** |

---

## 4. Concrete Security, Correctness & Drift Counterexamples in Pin 2 (`4e0f864`)

All 6 findings below were reproduced against the unmodified functions in `src/session_lifecycle.ts` and `host/agent_hub_host/session_lifecycle.py` under `run-guarded.py` (receipts `rev-py-novel-02` and `rev-vit-novel-02`).

### `M15-F1` (High): `superseded` Classification Bypasses Both `authoritative_no_pending_proof` and `reportFreshEnough` (Stale Report / Newer Activity)
- **Location**: [src/session_lifecycle.ts](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/src/session_lifecycle.ts#L186-L192), [session_lifecycle.py](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/host/agent_hub_host/session_lifecycle.py#L119-L124).
- **Defect**:
  ```ts
  if (input.superseded_by && noObligations && (finishedSignals || (terminalFresh && input.authoritative_no_pending_proof))) {
    reasons.push(`superseded by ${input.superseded_by} with authoritative no-pending proof`);
    return { session_key: key, group: 'finished_stale', state: 'superseded', ... };
  }
  ```
  When `superseded_by` is non-empty and `finishedSignals` (`report_written && screen_done`) is `true`, the expression `(finishedSignals || ...)` short-circuits before checking `input.authoritative_no_pending_proof` and never checks `reportFreshEnough` (`report != null && (activity == null || activity <= report)`). Even when `authoritative_no_pending_proof: false` and `last_activity_at` (`11:30Z`) is 2.5 hours newer than `report_at` (`09:00Z`), `classifySession` returns `group: 'finished_stale', state: 'superseded'` with the false reason string `"superseded ... with authoritative no-pending proof"`, and `planCleanup` marks the active, unproven session **eligible for cleanup**.
- **Required Fix**: Require `Boolean(input.authoritative_no_pending_proof)` on **both** branches of `superseded` and require `(finishedSignals && reportFreshEnough) || terminalFresh` (with `terminalFresh` also bounded against newer activity per `M15-F2`).

### `M15-F2` (High): `terminalFresh` Ignores Newer `last_activity_at` (`activity > terminalAt`) and `reportFreshEnough` Fails Open on Corrupt `last_activity_at` / Future `report_at`
- **Location**: [src/session_lifecycle.ts](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/src/session_lifecycle.ts#L181-L197), [session_lifecycle.py](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/host/agent_hub_host/session_lifecycle.py#L113-L130).
- **Defect**:
  1. `terminalFresh` checks `now >= terminalAt && now - terminalAt <= 24 * 3600000` without checking `activity == null || activity <= terminalAt`. In `if (noObligations && input.authoritative_no_pending_proof && (finishedSignals || terminalFresh) && (reportFreshEnough || terminalFresh))`, `terminalFresh` short-circuits `reportFreshEnough`. A session with `terminal_evidence: 'explicit_completion'` at `09:00Z` that resumed working with `last_activity_at: '2026-10-17T11:30:00Z'` is falsely classified as `group: 'finished_stale', state: 'finished'` with reason `"terminal completion with authoritative no-pending proof and no newer activity"`.
  2. `reportFreshEnough` (`report != null && (activity == null || activity <= report)`) does not verify `report <= now` (accepting future `report_at`) and treats a malformed/unparseable string `last_activity_at: "corrupt"` (`ISO(...) === null`) as `activity == null`, failing open.
- **Required Fix**: Require `activity <= terminalAt` (and if `input.last_activity_at != null`, require `activity != null && activity <= now`) for `terminalFresh`, and require `report <= now && now - report <= 24 * 3600000` (or explicit freshness bound) with fail-closed parsing when `last_activity_at` is present.

### `M15-F3` (High): Ownership Provenance & Target-Binding Bypasses + TS/Python Behavioral Drift (`planCleanup`, `validateCleanupTarget`, `validateOwnershipTransfer`)
- **Location**: [src/session_lifecycle.ts](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/src/session_lifecycle.ts#L263-L339), [session_lifecycle.py](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/host/agent_hub_host/session_lifecycle.py#L173-L229).
- **Defects**:
  1. **TS vs Python Drift on Empty-String Owner (`C3a`)**: In `src/session_lifecycle.ts:281`, `if (c.owner == null || c.owner === 'unknown' || c.owner === 'legacy')` does not reject `c.owner === ''`. Calling `planCleanup([{ ...finished, owner: '' }], '')` returns **`eligible.length === 1` in TypeScript** vs **`0` in Python** (`(cand.get("owner") or None) in (None, "unknown", "legacy")`).
  2. **Ignored `owner_provenance` & Dropped Custody Fields**: `classifySession` strips `owner`, `owner_provenance`, `starter`, and `human_owned` from `ClassifiedSession`, and `planCleanup` never inspects `owner_provenance`. A session with `owner_provenance: 'unknown'` and an untrusted client `owner` string is marked eligible for cleanup.
  3. **Blank/Null/Unknown Target Binding Accepted (`C3b`)**: `validateCleanupTarget` / `validate_cleanup_target` checks `observed_* !== expected_*` without verifying that `expected_machine_id`, `expected_native_id`, and `expected_owner` are non-empty, non-null, and not `'unknown'`/`'legacy'`. In Python, `validate_cleanup_target({"contained": True})` (with `machine_id`, `native_id`, and `owner` all `None`) returns `{"ok": True}`; in both TS and Python, blank strings `""` or `expected_owner: "unknown", observed_owner: "unknown"` return `{ ok: true }`.
  4. **Python `bool`-as-`int` Epoch Bypass & Negative Epochs (`C3c`)**: In `session_lifecycle.py:220-222`, `isinstance(True, int)` is `True` in Python (`bool` subclasses `int` and `True == 1`), so `validate_ownership_transfer` with `current_epoch=True, expected_epoch=1` returns `{"ok": True}` in Python while returning `{ ok: false, error: 'epoch_fence_failed' }` in TypeScript. Both TS and Python also accept negative epochs (`current_epoch: -1, expected_epoch: -1`).
- **Required Fix**: Preserve `owner`, `owner_provenance`, `starter`, and `human_owned` on `ClassifiedSession`; reject `owner_provenance === 'unknown'` or empty/whitespace/`unknown`/`legacy` `owner` or `callerOwner` in `planCleanup`; require non-empty valid identifiers and authenticated non-`unknown`/`legacy` owner in `validateCleanupTarget`; and in `validateOwnershipTransfer` require `typeof x === 'number' && Number.isSafeInteger(x) && x >= 0` in TS and `isinstance(x, int) and not isinstance(x, bool) and x >= 0` in Python.

### `M15-F4` (Medium): Timezone-Naive Timestamp Drift Between TS and Python & Unqualified `limit_reset_at` Marked as Known Reset (`C4`)
- **Location**: [src/session_lifecycle.ts](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/src/session_lifecycle.ts#L97-L101), [session_lifecycle.py](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/host/agent_hub_host/session_lifecycle.py#L39-L48).
- **Defect**:
  1. Python `_parse_ms` (`L44-45`) converts timezone-naive timestamps (`parsed.tzinfo is None`) to `timezone.utc`, whereas TypeScript `Date.parse` parses naive ISO timestamps in host local time (`CEST = UTC+2`), creating a **2-hour (`7,200,000 ms`) drift** between TS and Python on identical inputs.
  2. When `limit_hit: true` and `limit_reset_at` is timezone-naive (e.g. `"2026-10-21 13:16"` with `limit_zone: null`, the exact Z.ai pattern from the 17 October triage fixture) or already in the past (`limit_reset_at <= now`), both TS and Python classify it as `state: 'limit_blocked'` with `flags: ['limit_known_reset']` and `reset=known` instead of `limit_blocked_unknown_reset`.
- **Required Fix**: Require explicit timezone offset (`Z` or `[+-]HH:MM`) in timestamp parser (consistent with `src/fleet.ts:40` `/T.*(?:Z|[+-]\d{2}:\d{2})$/i`), and require either an explicit offset in `limit_reset_at` or a valid IANA/UTC `limit_zone` (with `reset > now`) before classifying a limit reset as known.

### `M15-F5` (Medium): Dead/Unreachable `unknown` Fallback Branch & Future `last_activity_at` Handling (`C5`)
- **Location**: [src/session_lifecycle.ts](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/src/session_lifecycle.ts#L201-L213), [session_lifecycle.py](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/host/agent_hub_host/session_lifecycle.py#L134-L149).
- **Defect**: Immediately after `if (idle != null && idle >= idleHours) return ...`, line 207 checks `if (phase === 'running' || idle == null || idle < idleHours)`. Because `idle` is either `null` or `< idleHours` at line 207, `idle == null || idle < idleHours` is **always true**, making lines 212–213 (`return { group: 'unknown', state: 'unknown', reasons: ['insufficient fresh provenance'] }`) 100% unreachable dead code in both TS and Python. A session with `phase: null`, `last_activity_at: null`, `observed_at: null`, and `owner_provenance: 'unknown'` is classified into `group: 'active', state: 'active'`. Furthermore, a future `last_activity_at > now` produces `idle < 0 < idleHours` and is classified as `'fresh activity within idle threshold'`.
- **Required Fix**: Require fresh valid `observed_at` and either `phase === 'running'` or a valid non-future `last_activity_at` (`0 <= idle < idleHours`) for `active`; route missing/stale/future provenance or non-running phases without fresh activity to `group: 'unknown', state: 'unknown'`.

### `M15-F6` (Low/Medium): Truncation-Before-Redaction Leak in `sanitizeObservationText` (`C6`) & Duplicated TS/Python Authority State Machine
- **Location**: [src/session_lifecycle.ts](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/src/session_lifecycle.ts#L105-L112), [session_lifecycle.py](file:///home/alexey/git/agent-hub-wt-platform-lifecycle-review/host/agent_hub_host/session_lifecycle.py#L29-L36).
- **Defect**:
  1. `sanitizeObservationText(raw, max = 512)` slices `raw.slice(0, max)` **before** running `/[A-Za-z0-9_\-+/=]{20,}/g`. When a 26-character secret token starts at index 500 (`'note '.repeat(100) + 'KeyValue9876543210ABCDEFGH'`), `.slice(0, 512)` truncates the secret to 12 characters (`'KeyValue9876'`), which is `< 20` chars and escapes redaction in both TS and Python.
  2. Duplicating the entire classification/planning/ownership state machine across `src/session_lifecycle.ts` and `host/agent_hub_host/session_lifecycle.py` contradicts the head correction in author `PROGRESS.md` L132 ("no duplicated TS/Python authority machines; host validates, server classifies") and already caused 3 concrete TS/Python divergences (`M15-F3.1`, `M15-F3.4`, `M15-F4.1`).
- **Required Fix**: Apply regex redaction on a bounded window (e.g. `raw.slice(0, max + 128)`) **before** final `.slice(0, max)` truncation (avoiding slicing inside `[redacted]`), and keep canonical classification/planning authority on the server while scoping the Python host module to observation collection, fail-closed host target/worktree/process execution guards, and thin CLI/HTTP DTO handling.

---

## 5. Verification Receipts: Unique vs Repeat Tests, Guarded Runs & Resource Envelope

### 5.1 Verification of Author's 13 Guarded Receipts (`lifecycle-source-proof.json`)

Independent inspection of `/home/alexey/git/agent-coordination/.local/platform/lifecycle-source-proof.json` and `/home/alexey/git/agent-hub-wt-platform-lifecycle/.local/lifecycle/guarded/*/receipt.json` confirms:
- **Run counts & outcomes**: 13 guarded runs total (`runner_sha256` prefix `538762018ae2`): **11 exit `0`** and **2 exit `1`** (`py-lifecycle-01` failed with `ModuleNotFoundError: No module named 'agent_hub_host'` before `PYTHONPATH` was set; `tsc-lifecycle-01` failed with `TS2345` because `base()` inferred `owner_provenance: string` before `'starter' as const`). **0 guard aborts** (`abort_reason: null` on all 13).
- **Sampling & resource peaks**: Sampled (not continuous) at `0.1s` target interval; max sample gap across all 13 runs is **`0.479416s`** (`0.479s` on `vit-lifecycle-03`). Peak scratch disk across the author's worktree reached `8,725,438` B apparent (`tsc-lifecycle-03`) and `10,199,040` B allocated (`tsc-lifecycle-03`, `vit-lifecycle-07`); peak `child_rss_sum` `369,635,328` B (`tsc-lifecycle-03`); peak `observed_cgroup_memory_current` `970,158,080` B; peak `observed_cgroup_pids_current` `45`.
- **Unique vs repeat coverage**: The author's suite contains **9 unique test cases** (3 Vitest `it` blocks in `test/session-lifecycle.test.ts` + 6 Python `unittest` methods in `host/tests/test_session_lifecycle.py`) plus `tsc --noEmit`. Runs `vit-lifecycle-04..07` (4 runs), `py-lifecycle-03` (1 run), and `tsc-lifecycle-03` (1 run) are repeat runs of the same 9 cases and `tsc`, not additional test coverage.

### 5.2 Reviewer's 10 Independent Guarded Runs (`.local/review/guarded/*/receipt.json`)

All reviewer test executions ran via `python3 /home/alexey/git/agent-coordination/.local/platform/run-guarded.py --root /home/alexey/git/agent-hub-wt-platform-lifecycle-review --namespace review --label <label> --early-mib 8 --hard-mib 10 --timeout 120 --retire-child-tmp -- <cmd>` (one command per tool invocation, `<=2` Vitest cases per `-t` filter, Vitest cache isolated to `.local/review/vite-cache`). All 4 failing runs are preserved in `.local/review/guarded/`.

| Label | Command Summary | Exit | Abort | Selected | Samples / Max Gap (s) | Peak Scratch Apparent / Allocated (B) | Peak Child RSS / Threads | Observed Cgroup Mem / PIDs (`memory.peak` / `pids.peak`) | Retired Files |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `rev-tsc-01` | `npx tsc --noEmit` | `0` | `null` | `[]` | `19` / `0.439905` | `1,987,208` / `3,346,432` | `370,393,088` B / `14` | `512,823,296` B / `25` (`519,385,088` / `25`) | `574` |
| `rev-py-existing-01` | `unittest host.tests.test_session_lifecycle -v` (6 author cases) | `0` | `null` | `[]` | `1` / `0.000000` | `670,495` / `696,320` | `18,219,008` B / `1` | `217,681,920` B / `12` (`519,385,088` / `25`) | `0` |
| `rev-vit-existing-01` | `vitest -t "m15 synthetic lifecycle classification"` (2 author cases) | `0` | `null` | `[2]` | `9` / `0.424029` | `2,169,336` / `3,555,328` | `273,801,216` B / `26` | `338,132,992` B / `37` (`519,385,088` / `37`) | `574` |
| `rev-vit-existing-02` | `vitest -t "m15 synthetic cleanup plan and executor seam"` (1 author case) | `0` | `null` | `[1]` | `8` / `0.394850` | `2,352,442` / `3,747,840` | `268,976,128` B / `26` | `338,890,752` B / `37` (`519,385,088` / `37`) | `574` |
| `rev-py-novel-01` | `unittest .local/review/test_session_lifecycle_novel.py` (dotted-path loader error, retained) | `1` | `null` | `[]` | `1` / `0.000000` | `1,041,552` / `1,101,824` | `18,219,008` B / `1` | `222,392,320` B / `12` (`519,385,088` / `37`) | `0` |
| `rev-vit-novel-01` | `vitest -t "m15 reviewer novel boundary probes"` (1 pass, 1 fail w/ 8 soft assertions `C1a..C5`, retained) | `1` | `null` | `[2]` | `9` / `0.415772` | `2,548,806` / `3,964,928` | `283,385,856` B / `26` | `331,087,872` B / `37` (`519,385,088` / `37`) | `574` |
| `rev-py-novel-02` | `unittest test_session_lifecycle_novel -v` (1 pass, 1 fail w/ 7 `subTest` failures `C1..C6`, retained) | `1` | `null` | `[]` | `1` / `0.000000` | `1,242,756` / `1,323,008` | `18,087,936` B / `1` | `214,478,848` B / `12` (`519,385,088` / `37`) | `0` |
| `rev-vit-novel-02` | `vitest -t "m15 reviewer novel boundary probes"` (1 pass, 1 fail w/ 9 soft assertions `C1a..C6`, retained) | `1` | `null` | `[2]` | `9` / `0.554199` | `2,751,526` / `4,190,208` | `289,583,104` B / `26` | `342,446,080` B / `37` (`519,385,088` / `37`) | `574` |
| `rev-py-novel-pass-01` | `unittest ...test_01_novel_boundary_pass -v` (1 novel pass case) | `0` | `null` | `[]` | `1` / `0.000000` | `1,437,616` / `1,544,192` | `18,612,224` B / `1` | `222,564,352` B / `12` (`519,385,088` / `37`) | `0` |
| `rev-vit-novel-pass-01` | `vitest -t "novel-boundary-pass"` (1 novel pass case) | `0` | `null` | `[1]` | `8` / `0.583473` | `2,936,189` / `4,399,104` | `290,021,376` B / `26` | `363,487,232` B / `37` (`519,385,088` / `37`) | `574` |

- **Unique vs repeat summary (reviewer)**: 9 unique author cases verified + `tsc --noEmit` + **4 unique reviewer novel test blocks** (`novel-boundary-pass` and `novel-counterexample-fail` in `.local/review/session-lifecycle-novel.probe.test.ts`; `test_01_novel_boundary_pass` and `test_02_novel_counterexamples_contract_enforcement` [7 subTests] in `.local/review/test_session_lifecycle_novel.py`).
- **Containment & scratch summary**: All 10 runs executed inside `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-4ae01ad6-b234-46a4-ac99-560eef4da8b2.scope` (`memory.max = 1,572,864,000`, `pids.max = 256`, `cpu.max = 100000 100000`), with `0` surviving child PIDs and final retained `.local` disk usage of `1,622,156` B apparent / `1,744,896` B allocated (`< 8 MiB` early / `10 MiB` hard limits).
