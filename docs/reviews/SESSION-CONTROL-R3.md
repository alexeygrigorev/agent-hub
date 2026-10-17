# M12e Session Control R3 & Standalone Host-Enrollment Authority Review (`1f4e3007c1d5d02217861d5d1f1812549e7186c1`)

- **Verdict**: **`REVISE`** (`4 HIGH`, `2 MEDIUM`; real cross-host AI runtime loop and live native smoke remain **`OPEN / UNVERIFIED`**)
- **Reviewed Commit Pin**: `1f4e3007c1d5d02217861d5d1f1812549e7186c1`
  - **First Parent (`HEAD^1`, M12e R3 + Host Authority implementation)**: `fb5afbfb70c40e56c8863e42beaefb23c69b4533`
  - **Second Parent (`HEAD^2`, `main` integration parent)**: `91b8b6c189c4f54b9d9e2c44d7dfcb09e5208396`
  - **Second-Parent Merge Diff (`git diff fb5afbfb70c40e56c8863e42beaefb23c69b4533..1f4e3007c1d5d02217861d5d1f1812549e7186c1`)**: adds only `docs/reviews/QUOTA-STATUS-PLAN.md` (`0` source or test changes between `HEAD^1` and `HEAD`).
- **Connected Source Manifest Verification**:
  - **Manifest**: `/home/alexey/git/agent-coordination/.local/platform/session-r3-source-manifest.json` and `session-r3-source-manifest-proof.json`
  - **Verified Pair Count**: `253` `(path, sha256)` pairs (`0` mismatches pre-review in `.local/review/source-manifest-pre.json` and `0` mismatches post-review in `.local/review/source-manifest-post.json`)
  - **Verified Manifest Digest**: `47914191f5f8ac7495c9f7f6cf6627241b0634356541f39afcdf8d87faa5f979`
- **Reviewer Provenance**:
  - **Agent / Session**: `platform-session-r3-review` (native UUID `a935a199-6226-45d1-9271-0cdabc8b3165`)
  - **Engine / Model**: `antigravity` / `antigravity/gemini-4-argon-high` (independent model family from Muse Spark 1.3 author)
  - **Workspace / Agent Branches**: `/home/alexey/git/agent-hub-wt-platform-session-r3-review` (`actual0440` / `platform-sess-r3rev-0440`)
- **Implementer Provenance Under Review**:
  - **Agent / Session**: `platform-session-r3-recovery2` (native UUID `a9da74f2-c3df-47b5-8d39-8d538487b132`)
  - **Engine / Model**: `opencode` / `opencode-go/muse-spark-1.3-contributor` (`task0430`)
  - **Lineage & Historical Metadata Notes**:
    - Earlier recovery session `ea30c66c-174f-44a4-add3-7aa738c6e031` ended normally awaiting design ACK (not an unknown crash).
    - Earlier `ad643451` false 26-green Vitest claim was withdrawn after guard-killed full suites; original failed receipts, `.local/session-r3/109610.patch`, and native archive remain preserved in the author worktree.
    - Root seed work declaration was late (with no peer writer overlap) and recorded as a head private incident; the claim is now valid for audit trail purposes without retroactive compliance or implied source acceptance.
    - Inherited root `PROGRESS.md` in the repository tree is stale historical `ad643451` metadata (recording outdated short parent/commit claims) and is **not** accepted publication metadata; it was left untouched during this review for head archival/omission at final integration.

---

## 1. Executive Summary

Commit `1f4e3007c1d5d02217861d5d1f1812549e7186c1` substantially advances the M12e Session Control R3 surface and its shared host-enrollment authority prerequisite:
1. It introduces monotonic machine approval sequencing (`approval_seq`) in `src/host_enrollment.ts`, `src/registry.ts`, `src/auth.ts`, `src/device.ts`, and `src/hub.ts`, plus synchronous transaction boundaries (`prepareEnrollHostToken`, `prepareRotatedHostSecret`, `enrollHostTokenSync`, `rotateHostSecretSync`).
2. It adds late-proof convergence (`convergeLateDeliveredProof`) when a command row is `claimed` with `lease_expires_at <= t` before `sweepExpiredOffers` re-queues it, and preserves durable proofs across uncertain overwrites (`src/session_control.ts:750-858`).
3. It fixes the journal scan starvation bug by partitioning `unsent` vs `reconcile` candidates before slicing to `MAX_RECONCILE_BATCH = 20` (`host/agent_hub_host/session_receiver.py:404-439`) and adds `TERMINAL_409_CODES` classification (`host/agent_hub_host/session_executor.py:41-52, 680-752`).
4. It binds observed live `created_at_ms` from `a list --json` during first-delivery target identity pinning (`host/agent_hub_host/native_sessions.py:553-570`) and adds `start --cwd` verification (`host/agent_hub_host/native_sessions.py:725-777`).

However, independent source analysis and guarded novel counterexample probes against the real Worker/SQLite Durable Object (`vit-novel-r3-12-v3`, `vit-novel-r3-34-v2`, `vit-novel-r3-56`) and the Python host executor/receiver/native adapter (`py-probe-r3-counterexamples-v2`) uncovered **4 `HIGH` and 2 `MEDIUM` defects** that violate the R2 review requirements, `docs/HOST-ENROLLMENT-AUTHORITY.md`, and `docs/SESSION-CONTROL.md`:

| ID | Severity | Area | Summary | Guarded Mechanical Proof |
| :--- | :--- | :--- | :--- | :--- |
| **F1** | **HIGH** | Host Authority (`src/host_enrollment.ts`, `src/registry.ts`, `src/session_control.ts`) | `approvalAuthorityMatches` returns `true` for any legacy unwitnessed credential (`approval_seq IS NULL`) even when `machines.approval_seq IS NOT NULL`, allowing unbound witnessed machines to be reclaimed by legacy credentials; furthermore, `SessionControlService.hostOk` never checks `approval_seq` or `machines.credential_id`, allowing stale superseded credentials (`approval_seq = 1 < 2`) to bind/act on sessions on unbound machines. | `vit-novel-r3-12-v3` (`N1`, `N2`) |
| **F2** | **HIGH** | Host Auth & Txn Atomicity (`src/session_control.ts`) | `POST /api/session-commands/:id/ack` executes `session_control_proofs` lookup and `sameAck` early return **before** loading `session`, checking `session.machine_id === caller.machineId`, or calling `this.hostOk(session, caller)`, letting a **foreign machine credential** receive `200 OK` on ACK replay; `POST /api/sessions/:id/binding` reads `session` before `await body(request)` and passes the stale pre-`await` snapshot into `this.hostOk(session, caller)` inside `this.transaction(...)`. | `vit-novel-r3-34-v2` (`N3`, `N4`) |
| **F3** | **HIGH** | Native Engine Qualification (`host/agent_hub_host/session_executor.py`, `session_receiver.py`) | `handle_claimed_command` and `_reconcile_unknown_locked` drop `engine` when constructing `NativeTarget` (and `offer_to_command` omits `engine` for `stop` offers), allowing an offer for `engine="codex"` to execute against a live `engine="shell"` session and ACK `delivered`. | `py-probe-r3-counterexamples-v2` (`test_p1`) |
| **F4** | **HIGH** | Native Stop Containment Oracle (`host/agent_hub_host/native_sessions.py`) | Real installed `a` CLI emits `/sys/fs/cgroup/...` under `containment_cgroup` (while `workload_cgroup` is `/user.slice/...`), which `project_session` ignores; worse, `_confirm_workload_exit` returns `True` as soon as `worker_pid` and `workload_pid` exit **before** checking `if cgroup_gone is False: return False`, and `stop` passes `before[0]` (pre-kill running snapshot) to `_confirm_workload_exit` when `a kill` removes the catalog row. | `py-probe-r3-counterexamples-v2` (`test_p2`) + `.local/review/first-action-worker.json` |
| **F5** | **MEDIUM** | Tier / CWD / Fleet Qualification (`src/session_control.ts`, `src/fleet.ts`, `host/agent_hub_host/fleet.py`) | `POST /api/sessions` omits `tier` from idempotency `canonical` payload; `src/fleet.ts` coerces `tier: null` to `'daily'`; `applyBinding` uses unanchored `seen.endsWith('/' + want)` allowing cross-root suffix match (`/evil/other/sub` for `/srv/hub/demo` + `sub`) and rejects `cwd: '.'`; `Dispatcher.dispatch` skips `_verify_native_cwd` in `headless` mode and `load_subscriptions` raises `KeyError: 'service'` on `"engine"`-keyed dicts. | `vit-novel-r3-56` (`N5`) + `py-probe-r3-counterexamples-v2` (`test_p3`) |
| **F6** | **MEDIUM** | Journal 409 & Crash Convergence (`host/agent_hub_host/session_executor.py`, `session_receiver.py`) | First-attempt terminal `409` in `handle_claimed_command` records only `ack_error = "ValueError"` without classifying `TERMINAL_409_CODES` (causing a duplicate POST on next cycle); `_resubmit_ack_locked` overwrites `state` to `"uncertain"` on unclassified/transient `409`s; a crash between `_run_claimed_effect` and post-effect `_write_journal` strands `cmd-<id>.json` (`attempted: True, state: None`) permanently outside `_pending_reconcile_names`. | `py-probe-r3-counterexamples-v2` (`test_p4`) |

Because of Findings **F1–F6**, the verdict is **`REVISE`**.

---

## 2. Author Receipt & Coverage Audit (`../agent-hub-wt-platform-session-r3`)

An independent audit of `../agent-hub-wt-platform-session-r3/.local/session-r3/PROGRESS.md`, all `30` guarded receipts in `../agent-hub-wt-platform-session-r3/.local/session-r3/guarded/*/receipt.json`, and all `20` legacy receipts in `../agent-hub-wt-platform-session-r3/.local/session-r3/receipts/*.json` was recorded in `.local/review/author-receipts-audit.json`.

### 2.1 Discrepancy in Author's `PROGRESS.md` Heading vs Retained Receipts

In `../agent-hub-wt-platform-session-r3/.local/session-r3/PROGRESS.md`, the section heading states:
> `### Execution Receipts (`.local/session-r3/guarded/`)`
> `Total: 21 guarded test/check runs — **all exit 0**, 0 guard kills, 0 preflight rejections.`

That heading omits **9 non-zero / rejected guarded directories** present in the exact same `../agent-hub-wt-platform-session-r3/.local/session-r3/guarded/` directory (`30` total subdirectories: `21` exit `0`, `8` non-zero test failures during iterative development, and `1` preflight rejection when scratch reached `14.13 MiB`):

| Author Guarded Label | Exit | Preflight Rejected | Notes |
| :--- | :---: | :---: | :--- |
| `vitest-host-r6-pair1` | `1` | `false` | `1 failed \| 1 passed` during `fb5afbfb` development |
| `vitest-host-r6-pair2` | `1` | `false` | `2 failed` during `fb5afbfb` development |
| `vitest-host-r6-pair3` | `1` | `false` | `2 failed` during `fb5afbfb` development |
| `vitest-host-r6-pair4` | `1` | `false` | `1 failed \| 1 passed` during `fb5afbfb` development |
| `vitest-host-r6-pair5` | `1` | `false` | `2 failed` during `fb5afbfb` development |
| `vitest-host-r6-pair6` | `1` | `false` | `1 failed` during `fb5afbfb` development |
| `vitest-r3-pair1` | `1` | `false` | `2 failed` during `fb5afbfb` development |
| `vitest-r3-pair2` | `1` | `false` | `1 failed` during `fb5afbfb` development |
| `vitest-r3-pair2-pass` | `2` | `true` | `preflight_scratch_above_early_ceiling:2251074:14811136:early=14680064` |

Additionally, `../agent-hub-wt-platform-session-r3/.local/session-r3/receipts/*.json` contains `20` earlier unguarded/early-runner receipts:
- **6 guard-killed full-suite Vitest runs (`exit = -9`)**: `vitest-host-authority-r6` (killed at `16.43 MiB`), `vitest-session-control-r3` (`18.17 MiB`), `vitest-session-control-repair` (`19.25 MiB`), `vitest-session-control-composed` (`19.50 MiB`), `vitest-session-control` (`19.20 MiB`), and `vitest-machine-pairing` (`19.37 MiB`).
- **6 exit `1` runs**: `py-session-r3`, `vitest-r3-1`, `vitest-r3-2`, `vitest-r3-3`, `vitest-r6-1`, `vitest-r6-2`.
- **8 exit `0` runs**: `tsc`, `py-session-repair`, `py-session-executor`, `py-session-control`, `py-session-loop`, `py-machine-binding`, `py-fleet-enrollment`, `py-fleet`.

### 2.2 Author's Actual Verified Subset vs Unrun Product Coverage

Across the `21` passing guarded runs in `../agent-hub-wt-platform-session-r3/.local/session-r3/guarded/`, the author actually executed:
- **TypeScript**: `2` `tsc --noEmit` runs (`tsc-check`, `tsc-final`).
- **Python**: `56` unique unittest cases across `7` runs (`py-r3` [4], `py-repair` [9], `py-executor` [28], `py-Enrollment` [5], `py-binding` [8], `py-loop-sub` [1], `py-control-sub` [1]) plus `1` repeat (`py-r3-final` [4]). The author left `131` relevant Python cases in `test_session_control.py` (19 unrun), `test_session_control_loop.py` (11 unrun), `test_session_control_real_worker.py` (5 unrun), `test_native_bridge_contract.py` (15 unrun), `test_intake.py` (14 unrun), `test_fleet.py` (21 unrun), `test_auth.py` (18 unrun), `test_cli.py` (14 unrun), and `test_http_cli.py` (14 unrun) **unrun (`UNVERIFIED` by author)**.
- **Vitest**: `16` unique selected Vitest cases across `11` runs (`test/session-control-r3.test.ts` [3], `test/host-authority-r6.test.ts` [11], and `2` selected cases from `test/session-control-repair.test.ts`). The author left all `26` cases in `test/session-control.test.ts`, `9` cases in `test/session-control-repair.test.ts`, `4` cases in `test/session-control-composed.test.ts`, and all compatibility cases in `test/refresh.test.ts`, `test/revoke.test.ts`, `test/device.test.ts`, and `test/machine-pairing.test.ts` **unrun (`UNVERIFIED` by author)**.

In this review, we independently executed the **entire** relevant matrix (`187` Python cases + `69` existing Vitest cases + `6` novel Worker/SQLite counterexample cases + `4` novel Python counterexample cases + `tsc --noEmit`) under the head's `run-guarded.py` runner (Section 7).

---

## 3. R2 Finding-by-Finding Resolution Matrix

| R2 Group (`docs/reviews/SESSION-CONTROL-R2.md`) | R3 Status | Evaluation & Remaining Gaps |
| :--- | :---: | :--- |
| **1. Host-Enrollment & Credential Authority Prerequisite** (`prepareEnrollHostToken` / `prepareRotatedHostSecret` before `transactionSync`; synchronous live recheck inside mutation txn; `approval_seq` ordering; revocation/family fencing) | **PARTIALLY FIXED (`REVISE` — F1, F2)** | `src/hub.ts` now prepares crypto hashes before `storage.transactionSync(...)` and re-verifies `verifyAccessTokenSync` inside `transactionSync` for `/api/machines/register`, `/api/device/token`, `/api/auth/refresh`, and `/api/sessions*`. **However**: (a) `approvalAuthorityMatches` (`src/host_enrollment.ts:22`) returns `true` whenever `credentialSeq === null`, letting legacy `NULL` credentials reclaim witnessed unbound machines (`F1`); (b) `SessionControlService.hostOk` (`src/session_control.ts:232-248`) checks neither `approval_seq` nor `machines.credential_id`, letting superseded credentials bind/act on unbound machines (`F1`); (c) `ackCommand` (`src/session_control.ts:824-850`) returns `200 OK` on proof/ack replay *before* `hostOk` or `machine_id` checks (`F2`); and (d) `bindSession` (`src/session_control.ts:603, 624`) passes a pre-`await` `session` snapshot into `hostOk` inside `this.transaction` (`F2`). |
| **2. Late-Proof Convergence & Durable Proof Fencing** (expired claim converges before offer sweep without repeating native side effect; `acknowledged -> uncertain` overwrite restores on late verification; nonce/command/UUID/incarnation CAS) | **PARTIALLY FIXED (`REVISE` — F2)** | `convergeLateDeliveredProof` (`src/session_control.ts:750-782`) and `upsertProof` (`lines 702-748`) verify `(command_id, session_id, command_type, correlation_id, native_session_id, observed_created_at_ms)` and restore `acked` state when an expired claim (`row.state === 'claimed'`) receives a valid proof before `sweepExpiredOffers` re-queues it. **However**, `ackCommand` (`lines 824-832`) checks `session_control_proofs` and returns `200 OK` *before* authenticating `session.machine_id === caller.machineId` or `this.hostOk(session, caller)`, and without verifying that the caller's request payload matches the stored proof tuple (`F2`). |
| **3. Host Receiver Journal Fairness, Terminal 409s & Idempotency** (fair scan beyond 20 completed files; precise terminal 409 classification with durable human obligation; lost-HTTP-response ACK convergence) | **PARTIALLY FIXED (`REVISE` — F6)** | `_pending_reconcile_names` (`host/agent_hub_host/session_receiver.py:404-439`) now separates `unsent` (`ack_pending`) from `reconcile` (`unknown`) so completed/unknown files past 20 do not starve new work. **However**: (a) first-attempt `409` in `handle_claimed_command` (`session_executor.py:1011-1022`) does not classify `TERMINAL_409_CODES`, causing an extra POST on the next poll cycle (`F6`); (b) `_resubmit_ack_locked` (`lines 745-752`) treats *every* `409` (even unclassified codes outside `TERMINAL_409_CODES`) as `server_terminal_...` and overwrites `state` to `"uncertain"` (`F6`); and (c) a crash between `_run_claimed_effect` (`line 950`) and `_write_journal` (`line 978`) leaves `attempted: True, state: None`, which `_pending_reconcile_names` skips forever (`F6`). |
| **4. Actual Native Identity, Provenance, CWD & Stop Containment** (live catalog `created_at_ms` binding; sender vs target mailbox; engine/profile qualification; `a start --cwd`; stop cgroup/process containment oracle) | **PARTIALLY FIXED (`REVISE` — F3, F4, F5)** | `native_sessions.py:553-570` binds observed `created_at_ms` from `a list --json` on first delivery, and reply verification (`session_executor.py:534-580`) reads the sender's mailbox and checks live catalog incarnation. **However**: (a) `handle_claimed_command` and `_reconcile_unknown_locked` drop `engine` when building `NativeTarget`, allowing `engine="codex"` offers to execute against `engine="shell"` sessions (`F3`); (b) `project_session` ignores `containment_cgroup` on real installed `a` CLI, `_confirm_workload_exit` returns `True` when `worker_pid`/`workload_pid` exit even when `cgroup_gone is False`, and `stop` passes pre-kill `before[0]` to `_confirm_workload_exit` when `a kill` removes the catalog row (`F4`); and (c) `applyBinding` allows cross-root `cwd` suffix matches and `Dispatcher.dispatch` skips `_verify_native_cwd` in `headless` mode (`F5`). |
| **5. Resource Discipline & Honest Evidence Reporting** (`run-guarded.py`, `<= 2` selected Vitest cases per invocation, `14 MiB` early / `18 MiB` hard scratch, no false green claims over non-zero receipts) | **PARTIALLY FIXED (`REVISE` — §2.1)** | Author used `run-guarded.py` and kept Vitest runs to `<= 2` selected cases, but `../agent-hub-wt-platform-session-r3/.local/session-r3/PROGRESS.md` claimed `"Total: 21 guarded test/check runs — all exit 0, 0 guard kills, 0 preflight rejections"` while `9` additional guarded directories in the same folder had non-zero exits (`8` exit `1`, `1` preflight rejection exit `2`) and `131` relevant Python + `53` relevant Vitest cases remained unrun by the author. |

---

## 4. Detailed Findings

### Finding 1 [HIGH]: Host-Enrollment Authority Bypass on Unbound Witnessed Machines in `approvalAuthorityMatches` and Missing `approval_seq` / Bound-Machine Checks in `SessionControlService.hostOk`

- **Locations**:
  - [src/host_enrollment.ts](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/host_enrollment.ts#L18-L25)
  - [src/registry.ts](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/registry.ts#L254-L273)
  - [src/session_control.ts](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/session_control.ts#L232-L248)
  - Specification: [docs/HOST-ENROLLMENT-AUTHORITY.md](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/docs/HOST-ENROLLMENT-AUTHORITY.md#L15-L29)

- **Root Cause**:
  1. **Unwitnessed legacy `NULL` bypass in `approvalAuthorityMatches`**:
     `docs/HOST-ENROLLMENT-AUTHORITY.md:15-18` specifies:
     > *"Unbound legacy rows (`machines.credential_id IS NULL`) may only be bound if `machines.approval_seq IS NULL` (truly pre-migration unwitnessed row) or if the caller's credential `approval_seq` matches the machine's current `approval_seq`. An unbound legacy row where `machines.approval_seq IS NOT NULL` cannot be claimed by a credential with `approval_seq IS NULL`."*
     However, `src/host_enrollment.ts:18-25` implements:
     ```ts
     export function approvalAuthorityMatches(
       credentialSeq: number | null | undefined,
       machineSeq: number | null | undefined,
     ): boolean {
       if (machineSeq === null || machineSeq === undefined) return true;
       if (credentialSeq === null || credentialSeq === undefined) return true;
       return credentialSeq >= machineSeq;
     }
     ```
     Because line 22 returns `true` whenever `credentialSeq === null || credentialSeq === undefined`, any legacy unwitnessed host credential (`approval_seq IS NULL`) bypasses the check even when `machineSeq !== null` (`machines.approval_seq = 1`). When `machines.credential_id IS NULL` (e.g. an approved enrollment is pending redemption or a machine row was pre-seeded with `approval_seq >= 1` and `credential_id = NULL`), `src/registry.ts:264` calls `approvalAuthorityMatches(caller.approvalSeq, existing?.approval_seq)` and allows the `approval_seq IS NULL` credential to bind itself as `machines.credential_id`!
  2. **Missing `approval_seq` and bound-machine checks in `SessionControlService.hostOk`**:
     In `src/session_control.ts:232-248`, `hostOk(session, caller)` checks:
     ```ts
     const m = this.one<{
       revoked: number;
       credential_id: string | null;
       machine_family: string | null;
     }>(
       `SELECT m.revoked, m.credential_id, c.family_id AS machine_family
        FROM machines m
        LEFT JOIN credentials c ON c.id = m.credential_id
        WHERE m.id = ?`,
       session.machine_id,
     );
     if (!m || m.revoked === 1) return false;
     if (m.machine_family && m.machine_family !== family) return false;
     ```
     It never selects `m.approval_seq` or `cred.approval_seq`, never calls `approvalAuthorityMatches(cred.approval_seq, m.approval_seq)`, and when `m.credential_id IS NULL` (`m.machine_family === null`) it allows **any** active credential for `session.machine_id`—even an older superseded credential `A` (`approval_seq = 1`) after a newer enrollment `B` (`approval_seq = 2`) has already advanced `machines.approval_seq = 2`—to bind a session (`POST /api/sessions/:id/binding`), stamp `session.host_family = A.family_id`, claim offers, and ACK commands!

- **Mechanical Proof (`vit-novel-r3-12-v3`, `N1` & `N2` in `.local/review/novel-r3.test.ts`)**:
  - **`N1` (direct single-HTTP-request exploit on witnessed unbound machine)**: Machine `m-legacy-witnessed` has `approval_seq = 1, credential_id = NULL` (witnessed by device code `dc_w1`). Credential `cred_legacy_null` (`approval_seq = NULL`) sends `POST /api/machines/register`. Expected: `403`. Actual: `200 OK`, and `machines.credential_id` is overwritten with `'cred_legacy_null'`.
  - **`N2` (direct single-HTTP-request exploit on session control with superseded credential)**: Machine `m-session-stale-seq` has `approval_seq = 2, credential_id = NULL`. Stale credential `A` (`approval_seq = 1`) is properly rejected by `POST /api/machines/register` with `403`, **but** when `A` calls `POST /api/sessions/:id/binding` on a `starting` session on `m-session-stale-seq`, it succeeds with `200 OK`, transitions the session to `running`, and locks `session_control_sessions.host_family` to `A`'s superseded family.

- **Required Fix**:
  1. In `src/host_enrollment.ts:22`, return `false` (not `true`) when `machineSeq !== null && machineSeq !== undefined` and `credentialSeq === null || credentialSeq === undefined`.
  2. In `src/session_control.ts:232-248` (`hostOk`), query `cred.approval_seq` and `m.approval_seq`, require `approvalAuthorityMatches(cred.approval_seq, m.approval_seq)`, and require the machine to be actively bound to the caller's credential/family (`m.credential_id !== null && m.machine_family === family`).

---

### Finding 2 [HIGH]: Pre-`hostOk` Proof Lookup and `sameAck` Authorization Bypass in `POST /api/session-commands/:id/ack` + Stale Pre-`await` `session` Snapshot in `POST /api/sessions/:id/binding`

- **Locations**:
  - [src/session_control.ts](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/session_control.ts#L603-L639) (`bindSession`)
  - [src/session_control.ts](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/session_control.ts#L814-L858) (`ackCommand`)

- **Root Cause**:
  1. **Unauthenticated/foreign-machine early returns in `ackCommand` (`src/session_control.ts:824-850`)**:
     Inside `ackCommand`:
     ```ts
     const row = this.commandRow(id);
     if (!row) return err(404, 'not_found', 'Session command not found');
     const existingProof = this.one<{
       outcome: string;
       result_json: string | null;
     }>(
       `SELECT outcome, result_json FROM session_control_proofs WHERE command_id = ?`,
       id,
     );
     if (row.state === 'acked' && existingProof) {
       return json(200, {
         command_id: id,
         state: 'acked',
         idempotent: true,
         outcome: existingProof.outcome,
         result: parseJson(existingProof.result_json),
       });
     }
     if (row.state !== 'claimed') {
       if (this.sameAck(row, input)) {
         return json(200, { command_id: id, state: row.state, idempotent: true });
       }
       ...
     }
     const session = this.sessionRow(row.session_id);
     if (!session || session.machine_id !== caller.machineId) {
       return err(403, 'forbidden', 'Host token machine does not own this session');
     }
     if (!this.hostOk(session, caller)) {
       return err(403, 'forbidden', 'Host credential is not active for this session machine');
     }
     ```
     Both early returns (`row.state === 'acked' && existingProof` at lines `824-832` and `this.sameAck(row, input)` at lines `838-840`) execute **before** loading `session` (`line 847`), checking `session.machine_id !== caller.machineId` (`line 848`), or calling `this.hostOk(session, caller)` (`line 849`). Worse, the `row.state === 'acked' && existingProof` branch does not even call `this.sameAck(row, input)` or `this.proofMatches(id, row, input)`—it returns `200 OK` (and leaks `existingProof.result_json`, which for `capture` contains captured transcript output!) to **any valid host token from any foreign machine** or any revoked-family token, even when the request supplies forged `claim_token`, `correlation_id`, and `native` parameters.
  2. **Stale pre-`await` `session` snapshot in `bindSession` (`src/session_control.ts:603, 624`)**:
     In `bindSession`:
     - Line 603 reads `const session = this.sessionRow(id);` **before** `const parsed = await body(request);` (line 611).
     - Inside `this.transaction(() => { ... })` (line 623), line 624 calls `if (!this.hostOk(session, caller))` using the **stale pre-`await` `session` variable** rather than `current` (which is only read at line 627: `const current = this.sessionRow(id);`).
     - If `session.host_family` was `null` when line 603 ran, and while `await body(request)` was pending the session was bound to rotated credential `B` (`current.host_family = B.family_id`) and `machines.credential_id` was cleared/unbound, `this.hostOk(session, caller)` at line 624 still sees `session.host_family === null` and fails to check `session.host_family !== family` against `current.host_family`!

- **Mechanical Proof (`vit-novel-r3-34-v2`, `N3` & `N4` in `.local/review/novel-r3.test.ts`; plus `.local/review/register-atomic.test.ts`)**:
  - **`N3` (direct external HTTP exploit)**: After host `m-ack-owner` ACKs command `cmdId`, a **foreign machine's host token** (`m-ack-foreign`) sends `POST /api/session-commands/${cmdId}/ack` with completely bogus `claim_token: 'wrong-claim-token'`, `correlation_id: 'wrong-corr'`, and `native.native_session_id: '00000000-0000-4000-8000-000000000999'`. It receives `200 OK {"command_id": ..., "state": "acked", "idempotent": true, ...}` instead of `403 forbidden`! And when the command row is in `'uncertain'` state, the foreign machine credential sending matching `(outcome, correlation_id, native)` hits `sameAck` before `hostOk` and also receives `200 OK`.
  - **`N4` (atomic-contract counterexample across `await body(request)`)**: Using a slow streamed request body on `POST /api/sessions/:id/binding` with old credential `A` (`family_A`), while `await body(request)` is suspended, credential `B` (`family_B`) binds the session (`host_family = family_B`) and `machines.credential_id` becomes `NULL`. When `A`'s body finishes streaming, `this.hostOk(session, caller)` evaluates the pre-`await` `session` (`host_family = null`), bypasses the `session.host_family !== family` check, and returns `409 session_binding_conflict` instead of `403 forbidden`.
  - **Distinction between atomic-contract counterexamples and external two-HTTP exploits**: `N3` (like `N1` and `N2`) requires only standard sequential HTTP requests and is directly exploitable over the network. `N4` (and the preserved `register-atomic.test.ts` custom-fetch barrier probe) demonstrates a violation of the R2 synchronous in-transaction recheck contract across an `await` boundary.

- **Required Fix**:
  1. In `src/session_control.ts:814-858` (`ackCommand`), load `const session = this.sessionRow(row.session_id)` and enforce `if (!session || session.machine_id !== caller.machineId)` and `if (!this.hostOk(session, caller))` **first**, before inspecting `existingProof` or calling `this.sameAck(row, input)`. Furthermore, in the `row.state === 'acked' && existingProof` branch, require `this.proofMatches(id, row, input)` (or `this.sameAck(row, input)`) before returning `200 OK`.
  2. In `src/session_control.ts:623-630` (`bindSession`), read `const current = this.sessionRow(id)` at the very top of `this.transaction(() => { ... })` and pass `current` (not the pre-`await` `session`) to `this.hostOk(current, caller)`.

---

### Finding 3 [HIGH]: Host Executor Drops `engine` When Constructing `NativeTarget` in `handle_claimed_command` and `_reconcile_unknown_locked`, Allowing `shell` Masquerade

- **Locations**:
  - [host/agent_hub_host/session_executor.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/session_executor.py#L644-L647) (`_reconcile_unknown_locked`)
  - [host/agent_hub_host/session_executor.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/session_executor.py#L889-L942) (`handle_claimed_command`)
  - [host/agent_hub_host/session_receiver.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/session_receiver.py#L140-L188) (`offer_to_command`)

- **Root Cause**:
  - `NativeSessionAdapter.resolve_target` (`host/agent_hub_host/native_sessions.py:524-527`) only checks the live session's engine when `target.engine` is populated:
    ```python
    if target.engine and row.engine != target.engine:
        return None, TargetFailure(
            "native_target_mismatch",
            f"native session {row.session_id} engine is {row.engine!r}, expected {target.engine!r}",
        )
    ```
  - While `offer_to_command` (`host/agent_hub_host/session_receiver.py:152-155`) places `offer["engine"]` into `command["target"]["engine"]` for `send` and `capture` offers (and omits `engine` altogether for `stop` offers at lines `179-188`), `SessionCommandExecutor.handle_claimed_command` (`host/agent_hub_host/session_executor.py:889-892`) constructs `NativeTarget` **without `engine`**:
    ```python
    target = NativeTarget(
        native_session_id=str(raw_target.get("native_session_id") or ""),
        tag=str(raw_target.get("tag") or ""),
        workspace=str(raw_target.get("workspace") or ""),
        created_at_ms=raw_target.get("created_at_ms"),
    )
    ```
  - Likewise, `_reconcile_unknown_locked` (`lines 644-647`) and the journaled `attempt["target"]` dictionary (`lines 939-941`) also omit `engine`.
  - As a result, `target.engine` is **always `None`** during real host command execution and reconciliation! Even though unit tests that call `NativeSessionAdapter` directly with `NativeTarget(..., engine="codex")` pass, in the actual `receive_once -> offer_to_command -> handle_claimed_command` pipeline, `target.engine` is silently discarded.

- **Mechanical Proof (`py-probe-r3-counterexamples-v2`, `test_p1` in `.local/review/probe_r3_counterexamples.py`)**:
  - `receive_once` claims a `send` offer specifying `engine="codex"` for native session `11111111-1111-4111-8111-111111111111`, while the live `a list --json` catalog row has `engine="shell"`.
  - Because `handle_claimed_command` drops `raw_target.get("engine")`, `resolve_target` sees `target.engine=None`, skips the engine check, delivers the message to the `shell` session, and ACKs `outcome="delivered"` to the Hub!

- **Required Fix**:
  1. In `host/agent_hub_host/session_receiver.py:179-188` (`offer_to_command` for `stop`), include `"engine": offer.get("engine")` inside `"target"`.
  2. In `host/agent_hub_host/session_executor.py:889-892` (`handle_claimed_command`), `lines 644-647` (`_reconcile_unknown_locked`), and `lines 939-941` (journaled `attempt["target"]`), pass `engine=str(raw_target.get("engine") or "") or None` (and reject commands with missing/unsupported engine when required).

---

### Finding 4 [HIGH]: Real Native Stop Containment Oracle Ignores `containment_cgroup` on Installed `a` CLI and Confirms Exit When Cgroup Descendants Survive

- **Locations**:
  - [host/agent_hub_host/native_sessions.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/native_sessions.py#L312-L337) (`project_session`)
  - [host/agent_hub_host/native_sessions.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/native_sessions.py#L825-L868) (`_workload_containment_gone`, `_confirm_workload_exit`)
  - [host/agent_hub_host/native_sessions.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/native_sessions.py#L920-L936) (`stop`)

- **Root Cause**:
  1. **`project_session` reads `workload_cgroup` instead of `containment_cgroup`**:
     On the real installed `a` CLI (verified directly from our own `.local/review/first-action-worker.json` output from `a whoami --json`), a session record contains:
     ```json
     "workload_cgroup": "/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-a935a199-6226-45d1-9271-0cdabc8b3165.scope",
     "containment_cgroup": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-a935a199-6226-45d1-9271-0cdabc8b3165.scope"
     ```
     `project_session` (`native_sessions.py:323`) extracts only:
     ```python
     workload_cgroup = str(raw.get("workload_cgroup") or raw.get("cgroup_path") or "") or None
     ```
     and never reads `containment_cgroup`. Then `_workload_containment_gone` (`line 835`) requires:
     ```python
     if cgroup_path and cgroup_path.startswith("/sys/fs/cgroup/"):
     ```
     Because `workload_cgroup` on real `a` sessions starts with `"/user.slice/..."` (without `"/sys/fs/cgroup"`), `cgroup_path.startswith("/sys/fs/cgroup/")` is **always `False`** on real `a` sessions! Consequently, `_workload_containment_gone` never inspects `/sys/fs/cgroup/.../cgroup.procs` on real native sessions.
  2. **`_confirm_workload_exit` returns `True` when `worker_pid` and `workload_pid` exit, BEFORE checking `if cgroup_gone is False: return False`**:
     Look at lines `853-866` of `host/agent_hub_host/native_sessions.py`:
     ```python
     states = [_pid_is_gone(pid) for pid in pids]
     cgroup_gone = self._workload_containment_gone(before)
     if states and any(s is False for s in states):
         return False, "observed workload PID still alive after kill"
     if states and all(s is True for s in states):
         return True, "observed workload PIDs dead after kill"
     if cgroup_gone is True:
         return True, "observed workload containment empty after kill"
     if cgroup_gone is False:
         return False, "observed workload containment still active after kill"
     ```
     Whenever `before` has `worker_pid` and/or `workload_pid` (which every running session has) and those lead PIDs exit after `a kill`, `if states and all(s is True for s in states): return True` executes at **line 858** and short-circuits **before** line 865 (`if cgroup_gone is False: return False`)! Thus, even if child/descendant processes remain alive in the workload's containment cgroup (`cgroup_gone is False` or `containment_empty is False`), `_confirm_workload_exit` falsely reports `(True, "observed workload PIDs dead after kill")` and ACKs `outcome="stopped"`!
  3. **`stop()` passes `before[0]` (the pre-kill running snapshot) to `_confirm_workload_exit` when `a kill` removes the catalog row**:
     In `stop()` (`lines 924-933`), when `not remaining` (the session row disappeared from `a list --json` after `a kill`), line 929 calls `confirmed, detail = self._confirm_workload_exit(before[0])`, where `before[0]` is the pre-kill snapshot taken while the session was running (`before[0].containment_empty` was `False` before kill).

- **Mechanical Proof (`py-probe-r3-counterexamples-v2`, `test_p2` in `.local/review/probe_r3_counterexamples.py`)**:
  - Proven both ways:
    1. Projecting the real `a whoami --json` schema (`workload_cgroup="/user.slice/..."`, `containment_cgroup="/sys/fs/cgroup/user.slice/..."`) yields `projected.workload_cgroup == "/user.slice/..."` and `_workload_containment_gone(projected) is None` even when the `/sys/fs/cgroup/...` directory exists and contains live PIDs.
    2. Even when `workload_cgroup` starts with `"/sys/fs/cgroup/..."` and `cgroup.procs` is non-empty (`_workload_containment_gone(row) is False`) and `containment_empty is False`, as soon as `worker_pid` and `workload_pid` are gone, `_confirm_workload_exit(row)` returns `(True, "observed workload PIDs dead after kill")`!

- **Required Fix**:
  1. In `project_session` (`native_sessions.py:323`), prefer `containment_cgroup` (or normalize `/user.slice/...` under `/sys/fs/cgroup` when `containment_cgroup` is absent) so `workload_cgroup` holds the actual `/sys/fs/cgroup/...` path.
  2. In `_confirm_workload_exit` (`native_sessions.py:853-866`), check `if cgroup_gone is False: return False, "observed workload containment still active after kill"` **before** returning `True` from `all(s is True for s in states)`, and require positive cgroup containment emptiness (not merely lead-PID death) when a containment cgroup is recorded.
  3. Do not rely on pre-kill `before[0].containment_empty` as post-kill state; verify live `/sys/fs/cgroup/.../cgroup.procs` emptiness after `a kill`.

---

### Finding 5 [MEDIUM]: `tier` Omitted from Session Idempotency `canonical` JSON, `tier: null` Coercion, Unanchored Cross-Root `cwd` Suffix Match in `applyBinding`, and Skipped `_verify_native_cwd` in Headless Dispatch

- **Locations**:
  - [src/session_control.ts](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/session_control.ts#L57) (`cwdOk`), [L361-L368](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/session_control.ts#L361-L368) (`applyBinding`), [L515-L526](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/session_control.ts#L515-L526) (`createSession`)
  - [src/fleet.ts](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/src/fleet.ts#L155-L163) (`parseTriggerBody`)
  - [host/agent_hub_host/fleet.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/fleet.py#L279) (`load_subscriptions`), [L379-L395](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/fleet.py#L379-L395) (`Dispatcher.dispatch`)

- **Root Cause & Mechanical Proof (`vit-novel-r3-56`, `N5` in `.local/review/novel-r3.test.ts`; `py-probe-r3-counterexamples-v2`, `test_p3` in `.local/review/probe_r3_counterexamples.py`)**:
  1. **`tier` omitted from `POST /api/sessions` idempotency `canonical` JSON (`src/session_control.ts:515-526`)**:
     `canonical` serializes `{ machine_id, engine, agent_type, workspace, cwd, tag, title, mode, prompt }` and omits `tier`. Proven in `N5`: creating a session with `idempotency_key: 'idem-tier-collision'` and `tier: 'quick'` returns `201` (`tier: 'quick'`), and retrying with the exact same `idempotency_key` but `tier: 'deep'` returns `200 OK` (`replay: true, tier: 'quick'`) instead of `409 idempotency_conflict`!
  2. **`tier: null` coerced to `'daily'` in `src/fleet.ts:157`**:
     `parseTriggerBody` checks `if (input.tier !== undefined)` instead of `if (input.tier !== undefined && input.tier !== null)`, so JSON `{"tier": null}` sets `rawTier = ""` and defaults to `'daily'` (`201 Created`) instead of `400 invalid_tier`.
  3. **Unanchored cross-root `cwd` suffix match in `applyBinding` (`src/session_control.ts:361-368`) and rejection of `cwd: '.'` (`line 57`)**:
     In `applyBinding`:
     ```ts
     if (session.cwd && session.cwd !== '.') {
       const want = session.cwd.replace(/\/+$/, '');
       const seen = binding.cwd.replace(/\/+$/, '');
       if (!seen || !(seen === want || seen.endsWith('/' + want))) {
         return err(409, 'session_binding_mismatch', ...);
       }
     }
     ```
     Because `seen` is never anchored to `binding.workspace` (i.e. `seen !== `${ws}/${want}``), when a session has `workspace = '/srv/hub/demo'` and relative `cwd = 'sub'`, a host binding reporting `workspace = '/srv/hub/demo'` and `cwd = '/evil/other/sub'` satisfies `seen.endsWith('/sub')` and is accepted with `200 OK`! Meanwhile, `cwdOk('.')` in `src/session_control.ts:57` rejects `'.'` (`p === '.'` returns `false`), even though line 361 checks `session.cwd !== '.'` and `src/fleet.ts:30` / `host/agent_hub_host/fleet.py:233` explicitly allow `'.'`.
  4. **Skipped `_verify_native_cwd` in headless dispatch (`host/agent_hub_host/fleet.py:379-395`) and `KeyError: 'service'` in `load_subscriptions` (`line 279`)**:
     In `Dispatcher.dispatch`, `self._verify_native_cwd(session_tag, resolved_cwd)` is only called inside `if mode == "interactive":` (`line 380`); when `mode == "headless"`, `dispatch()` skips `_verify_native_cwd` completely. And in `load_subscriptions` (`line 279`), `s.get("engine", s["service"])` eagerly evaluates `s["service"]` in Python before calling `s.get(...)`, raising `KeyError: 'service'` if a subscription entry uses `"engine"` without `"service"`.

- **Required Fix**:
  1. Include `tier` in `canonical` in `src/session_control.ts:515-526`, and reject `input.tier === null` in `src/fleet.ts:157`.
  2. In `src/session_control.ts:57`, allow `cwd === '.'` (or normalize it consistently with `fleet.ts`), and in `applyBinding` (`lines 361-368`), require `seen === (want.startsWith('/') ? want : `${ws}/${want}`)` where `ws = binding.workspace.replace(/\/+$/, '')`.
  3. In `host/agent_hub_host/fleet.py:379-395`, verify native CWD/containment for headless dispatches whenever a native session row exists, and fix line 279 to `s.get("engine") or s["service"]`.

---

### Finding 6 [MEDIUM]: Journal First-Attempt Terminal-409 Double-POST, Unclassified 409 Overwrite in `_resubmit_ack_locked`, and Post-Effect Crash Hole

- **Locations**:
  - [host/agent_hub_host/session_executor.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/session_executor.py#L690-L752) (`_ack_is_terminal_conflict`, `_resubmit_ack_locked`)
  - [host/agent_hub_host/session_executor.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/session_executor.py#L904-L908), [L935-L1023](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/session_executor.py#L935-L1023) (`handle_claimed_command`)
  - [host/agent_hub_host/session_receiver.py](file:///home/alexey/git/agent-hub-wt-platform-session-r3-review/host/agent_hub_host/session_receiver.py#L404-L439) (`_pending_reconcile_names`)

- **Root Cause & Mechanical Proof (`py-probe-r3-counterexamples-v2`, `test_p4` in `.local/review/probe_r3_counterexamples.py`)**:
  1. **First-attempt terminal `409` is not classified in `handle_claimed_command` (`session_executor.py:1011-1022`)**:
     When `self._ack(ack_payload)` raises a terminal `409` (e.g. `session_command_Restored_from_proof`) during `handle_claimed_command`, the `except Exception as exc:` block at line 1011 only writes `ack_error = type(exc).__name__` (`"ValueError"`) and leaves `ack_pending = True`, without checking `_ack_is_terminal_conflict(exc)`. This forces an unnecessary second HTTP POST on the next poll cycle before `_resubmit_ack_locked` finally classifies the 409.
  2. **Unclassified `409` in `_resubmit_ack_locked` overwrites valid local state to `"uncertain"` (`session_executor.py:745-752`)**:
     In `_resubmit_ack_locked`:
     ```python
     terminal, reason = self._ack_is_terminal_conflict(exc)
     if terminal or "409" in str(exc):
         journal["ack_pending"] = False
         journal["ack_terminal_conflict"] = True
         journal["ack_error"] = f"server_terminal_{reason}"
         journal["state"] = "uncertain"
         self._write_journal(path, journal)
         return True
     ```
     Because of `or "409" in str(exc)`, **any** HTTP 409—even one whose error code is not in `TERMINAL_409_CODES` (`terminal == False, reason == "unknown"`)—sets `ack_terminal_conflict = True`, `ack_error = "server_terminal_unknown"`, and overwrites `journal["state"]` (e.g. `"captured"` or `"acknowledged"`) to `"uncertain"`.
  3. **Crash window between `_run_claimed_effect` (`line 950`) and post-effect `_write_journal` (`line 978` / `1022`)**:
     Before running `_run_claimed_effect`, line 948 writes `journal` with `attempted = True` and no `"state"` key (`state = None`). If the host process crashes after `_run_claimed_effect` executes the native effect (`a send` / `a kill`) but before line 978 writes the post-effect journal, the journal file remains on disk with `attempted: True, state: None, ack_pending: False`. On restart:
     - `_pending_reconcile_names` (`session_receiver.py:408-413`) only queues files where `ack_pending is True` or `state == "unknown"`, so it **never** selects `attempted: True, state: None`.
     - If the Hub re-offers the command after lease expiry, `handle_claimed_command` (`session_executor.py:906-908`) sees `existing.get("attempted") is True` with `state is None` and immediately returns `{"command_id": command_id, "state": None, "replayed": True}` without reconciling or ACKing!

- **Required Fix**:
  1. Classify terminal `409`s via `_ack_is_terminal_conflict(exc)` in `handle_claimed_command` (`lines 1011-1022`) as well as `_resubmit_ack_locked`, and in `_resubmit_ack_locked` remove the blanket `or "409" in str(exc)` fallback so only explicit `TERMINAL_409_CODES` terminate ACK retries; keep the local execution outcome in a dedicated field instead of clobbering `journal["state"]`.
  2. Initialize pre-effect journals (`line 948`) with `"state": "unknown"` (or include `attempted is True and state is None` in `_pending_reconcile_names` and `handle_claimed_command`'s `_reconcile_unknown_locked` branch) so a crash immediately after `_run_claimed_effect` converges on restart.

---

## 5. Full `M12e` Product Qualification Audit

Per the intake brief, `M12e` requires a single coherent control plane where Hub CLI/API `start(engine, tier, host, cwd, brief)`, `send` (with confirmed delivery), `capture`/read, and `stop` (with confirmed containment) reuse the existing routing, ownership, host lease, grants, fresh quota, and profile qualification without introducing a second controller, and ultimately support a real `Grok (botbox) -> Hub -> Hetzner` AI session loop.

| Capability / Requirement | Current Implementation Status | Review Assessment |
| :--- | :--- | :--- |
| **`start(engine, tier, host, cwd, brief)`** (`src/fleet.ts`, `src/session_control.ts`, `host/agent_hub_host/fleet.py`, `native_sessions.py`, `cli.py`) | `hub session start` / `POST /api/sessions` and `POST /api/fleet/trigger` accept `engine`, `tier`, `machine_id`, `cwd`, `prompt`/`brief`. Host `native_sessions.py:start` passes `--cwd <resolved_cwd>` to `a start` and verifies observed `cwd` from `a list --json`. | **Needs Revision (`F5`)**: `POST /api/sessions` omits `tier` from idempotency `canonical` check; `src/fleet.ts` coerces `tier: null` to `'daily'`; `applyBinding` allows cross-root `cwd` suffix match; `Dispatcher.dispatch` skips `_verify_native_cwd` in `headless` mode. |
| **Reuse of existing routing, host lease, grants, fresh quota & profile qualification** (`src/fleet.ts`, `host/agent_hub_host/fleet.py`) | `FleetService` and `Dispatcher` validate subscription profile/tier mapping (`ENGINE_TIER_PROFILES`) and quota availability before launch. | **Needs Revision (`F1`, `F5`)**: `SessionControlService.hostOk` does not enforce `approval_seq` or active `machines.credential_id` binding (`F1`), and `load_subscriptions` raises `KeyError: 'service'` on `"engine"`-keyed dicts (`F5`). |
| **`send` with confirmed delivery & sender/target provenance** (`src/session_control.ts`, `host/agent_hub_host/session_executor.py`, `native_sessions.py`) | Replies are read from the **sender's** workspace mailbox (`session_executor.py:534-580`), and first delivery pins `observed_created_at_ms` from live `a list --json` (`native_sessions.py:553-570`). | **Needs Revision (`F2`, `F3`)**: `handle_claimed_command` and `_reconcile_unknown_locked` drop `engine` when building `NativeTarget`, allowing `engine="codex"` offers to deliver to `engine="shell"` sessions (`F3`); `ackCommand` returns `200 OK` to foreign machines on proof/ack replay (`F2`). |
| **`capture` / read** (`src/session_control.ts`, `host/agent_hub_host/native_sessions.py`) | Bound to `(native_session_id, tag, workspace, created_at_ms)` with bounded UTF-8 truncation (`MAX_CAPTURE_BYTES`). | **Needs Revision (`F2`, `F3`)**: `ackCommand` leaks `existingProof.result_json` (including captured transcript text) to any foreign machine credential when `row.state === 'acked'` (`F2`), and `handle_claimed_command` drops `engine` on `capture` (`F3`). |
| **`stop` with confirmed cgroup/process containment** (`host/agent_hub_host/native_sessions.py`) | Calls `a kill` and `_confirm_workload_exit`. | **Needs Revision (`F3`, `F4`)**: `offer_to_command` omits `engine` for `stop` (`F3`); `project_session` ignores `containment_cgroup` on real installed `a` CLI, and `_confirm_workload_exit` returns `True` when lead PIDs die even if `cgroup_gone is False` (`F4`). |
| **Real cross-host AI loop (`Grok botbox -> Hub -> Hetzner`)** | Tested only with local SQLite/Miniflare and synthetic `shell` / mock CLI fixtures (`test_session_control_real_worker.py`). | **`OPEN / UNVERIFIED`**: Synthetic/shell fixtures are not real cross-host AI acceptance. Remains `OPEN` pending fixes for `F1–F6` and head-authorized runtime verification. |

---

## 6. Native Boundary & Smoke-Probe Disposition

- **Native Sessions Launched During This Review**: **`0`** (no `a start`, `a send` to throwaway probes, or `a kill` executed).
- **Rationale**:
  1. Per the review brief, if native smoke is held or unnecessary to establish the source/protocol verdict, the reviewer must continue source/HTTP/host probes and scope final acceptance explicitly.
  2. Read-only inspection of our own real `a whoami --json` record (`.local/review/first-action-worker.json`) combined with guarded unit/integration probe `py-probe-r3-counterexamples-v2` (`test_p2`) already mechanically proved **Finding 4 (`HIGH`)**: the real installed `a` CLI populates `/sys/fs/cgroup/...` in `containment_cgroup` (while `workload_cgroup` is `/user.slice/...`), so `project_session` (`native_sessions.py:323`) drops the `/sys/fs/cgroup/...` path on every real native session, and `_confirm_workload_exit` (`lines 857-858`) short-circuits to `True` on lead-PID death before checking `cgroup_gone is False`.
  3. Launching throwaway native `shell` sessions was therefore not needed to prove the defect and would consume host/cgroup budget without changing the `REVISE` verdict. Live native smoke and real cross-host AI loop verification remain explicitly **`OPEN / UNVERIFIED`**.

---

## 7. Reviewer Guarded Execution & Resource Telemetry Inventory

All test and probe executions in this review were run sequentially (one guarded command per tool call, Vitest `<= 2` selected cases per invocation via `-t`, `--maxWorkers=1 --no-file-parallelism`) using `/home/alexey/git/agent-coordination/.local/platform/run-guarded.py` (`runner_sha256: 538762018ae23d06b1d38735b3af0a2d21f8cf8532872c124c92b2030df373ff`, `--early-mib 14`, `--hard-mib 18`, `--timeout 120`, `--retire-child-tmp`). Full machine-readable telemetry is archived in `.local/review/our-guarded-audit.json`.

### 7.1 Aggregate Resource & Telemetry Summary Across All 68 Guarded Runs

- **Total Guarded Runs**: `68` (`57` exit `0`, `11` non-zero preserved in `.local/review/guarded/*`; `0` guard kills, `0` preflight rejections, `0` OOM events).
- **Sampling Cadence**: discrete user-space polling (`target_interval_seconds = 0.1s`, `sampled_not_continuous = true`); **maximum observed sample gap across all 68 runs**: **`0.8707s`** (`vit-sc-p03`).
- **Scratch Disk Usage (`.local/`, `.wrangler/`, `dist/`)**:
  - **Early / Hard Ceilings**: `14.00 MiB` (`14,680,064` bytes) / `18.00 MiB` (`18,874,368` bytes)
  - **Peak Observed Scratch Allocated**: **`5,869,568` bytes (`5.60 MiB`)** (`tsc-noemit`)
  - **Peak Observed Scratch Apparent**: **`5,113,103` bytes (`4.88 MiB`)** (`tsc-noemit`)
  - **Final Scratch Disk After Batch 3 (`py-probe-r3-counterexamples-v2`)**: **`2,908,160` bytes (`2.77 MiB`) allocated**, **`1,528,743` bytes (`1.46 MiB`) apparent**
- **Child Process & Cgroup Counters** (`/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-a935a199-6226-45d1-9271-0cdabc8b3165.scope`):
  - **Peak Sampled Child RSS Sum**: **`631,304,192` bytes (`602.06 MiB`)** (`vit-repair-0506`; note: sum across child processes may double-count shared pages)
  - **Peak Sampled Child Threads**: **`30`**
  - **Peak Observed Cgroup `memory.current`**: **`1,060,818,944` bytes (`1,011.68 MiB`)** (limit `memory.max = 1,572,864,000` bytes / `1,500 MiB`)
  - **Cumulative Cgroup `memory.peak`**: **`1,061,597,184` bytes (`1,012.42 MiB`)** (`memory.events`: `low 0, high 0, max 0, oom 0, oom_kill 0, oom_group_kill 0`)
  - **Peak Observed Cgroup `pids.current` / Cumulative `pids.peak`**: **`42` / `45`** (limit `pids.max = 256`, `pids.events: max 0`)
  - **Surviving Owned Child PIDs After Every Run**: **`[]` (`0`)**

### 7.2 Private Novel Counterexample Probe Files & SHA-256 Digests

| Private File (`.local/review/`) | SHA-256 Digest | Provenance / Adaptation Notes |
| :--- | :--- | :--- |
| `.local/review/helpers.ts` | `2a17328549f83a90071044db03781f3d1f000bcd8ca56206af176ee1152088b5` | Adapted from `../agent-hub-wt-platform-metrics-r5-review/.local/review/helpers.ts` (`createTestEnv`, `issueHostToken` with optional `approvalSeq`, `apiRequest`) |
| `.local/review/register-atomic.test.ts` | `c0bf633bb942fc511be1c453006eff57e4135b78f59ed9e6eb8e3d533fe2d599` | Adapted from `../agent-hub-wt-platform-metrics-r5-review/.local/review/register-atomic.test.ts` (`POST /api/machines/register` mid-flight revocation + custom-fetch barrier probes) |
| `.local/review/vitest.novel.config.ts` | `884b07a0980ab9c583b9b9c93844b0d86622aeba33927d1be9e4bcaf145fa19e` | Dedicated Miniflare/Vitest config including `.local/review/*.test.ts` |
| `.local/review/novel-r3.test.ts` | `7e778ea91ec0f9eb46ccc9831a5151d9bc65764b17d466478046e818c2788656` | Novel Worker/SQLite probes `N1`–`N6` proving Findings **F1**, **F2**, and **F5** |
| `.local/review/probe_r3_counterexamples.py` | `3eb492262efae2d5e52b77d25483712086f3def4d8f96513198e7838b9347c25` | Novel Python unittest probes `test_p1`–`test_p4` proving Findings **F3**, **F4**, **F5**, and **F6** |

### 7.3 Preserved Non-Zero Guarded Runs (11 Runs)

All `11` non-zero guarded runs were preserved intact in `.local/review/guarded/*`:

| Label | Exit | Category | Reason for Non-Zero Exit |
| :--- | :---: | :--- | :--- |
| `py-session-r3` | `1` | CLI invocation typo | Invoked `python3 -m unittest host/tests/test_session_control_r3.py` (file path without `-s host/tests`); re-ran cleanly with `-s host/tests` as `py-session-r3-ok` (`4` passed). |
| `py-session-control` | `1` | Subprocess timeout in `test_cli_session_start_binds_and_sends_initial_prompt` | `subprocess.Popen` in test timed out at `8s` waiting for background CLI thread while contending on disk; re-ran cleanly as `py-session-control-ok` (`20` passed in `1.88s`). |
| `vit-probe-register-atomic` | `1` | Probe config discovery | Default `vitest.config.ts` only includes `test/**/*.test.ts`; created `.local/review/vitest.novel.config.ts`. |
| `vit-probe-register-atomic-ok` | `1` | Atomic barrier probe (`1 failed \| 1 passed`) | Test 1 (streamed-body revocation on `/api/machines/register`) **passed** (`401`); Test 2 (custom-fetch barrier inside `fetch`) failed because `prepareEnrollHostToken` runs before `transactionSync`. |
| `vit-novel-r3-12` | `1` | Probe discovery | Ran before adding `.local/review/vitest.novel.config.ts`. |
| `vit-novel-r3-12-ok` | `1` | Probe setup schema (`2 failed`) | Initial `N1`/`N2` probe inserted into `device_codes` without `interval_seconds`; fixed SQL setup in `vit-novel-r3-12-v3`. |
| `vit-novel-r3-12-v2` | `1` | Probe setup (`1 failed \| 1 passed`) | `N1` passed; `N2` initially used `createSession` via API before machine registration; updated `N2` in `vit-novel-r3-12-v3` (`2 passed`). |
| `vit-novel-r3-34` | `1` | Probe discovery | Ran before adding `.local/review/vitest.novel.config.ts`. |
| `vit-novel-r3-34-ok` | `1` | Probe assertion refinement (`1 failed \| 1 passed`) | `N3` passed; `N4` refined to assert exact `409` vs `403` stale-`session` behavior in `vit-novel-r3-34-v2` (`2 passed`). |
| `py-probe-r3-counterexamples` | `1` | Probe discovery | `unittest` needed `-s .local/review -p 'probe_*.py'`. |
| `py-probe-r3-counterexamples-ok` | `1` | Probe setup refinement (`3 passed, 1 error`) | `test_p1`, `test_p2`, `test_p3` passed; refined `test_p4` `NativeSessionAdapter` constructor args in `py-probe-r3-counterexamples-v2` (`4 passed`). |

### 7.4 Passing Guarded Runs & Unique Case Inventory (57 Passing Runs)

- **TypeScript Compiler (`1` run)**:
  - `tsc-noemit`: `./node_modules/.bin/tsc --noEmit` — exit `0` (`5.05s`, max gap `0.1066s`, peak scratch `5.60 MiB`).
- **Python Product Test Suites (`14` passing runs, `187` unique cases passed, `0` repeats among passing runs)**:
  - `py-session-r3-ok` (`test_session_control_r3.py`): `4` passed (`0.09s`)
  - `py-session-repair` (`test_session_control_repair.py`): `9` passed (`0.14s`)
  - `py-session-executor` (`test_session_executor.py`): `28` passed (`0.13s`)
  - `py-session-control-ok` (`test_session_control.py`): `20` passed (`1.88s`)
  - `py-session-control-loop` (`test_session_control_loop.py`): `12` passed (`0.22s`)
  - `py-session-real-worker` (`test_session_control_real_worker.py`): `5` passed (`10.37s`)
  - `py-native-bridge-contract` (`test_native_bridge_contract.py`): `15` passed (`0.12s`)
  - `py-intake` (`test_intake.py`): `14` passed (`0.11s`)
  - `py-fleet` (`test_fleet.py`): `21` passed (`0.54s`)
  - `py-fleet-enrollment` (`test_fleet_enrollment.py`): `5` passed (`0.15s`)
  - `py-machine-binding` (`test_machine_binding.py`): `8` passed (`0.12s`)
  - `py-auth` (`test_auth.py`): `18` passed (`0.21s`)
  - `py-cli` (`test_cli.py`): `14` passed (`0.10s`)
  - `py-http-cli` (`test_http_cli.py`): `14` passed (`0.16s`)
- **Existing Vitest Product Suites (`38` passing runs, `69` unique selected cases passed)**:
  - `test/session-control-r3.test.ts` (`3` unique cases): `vit-r3-12` (`2` passed), `vit-r3-3` (`1` passed)
  - `test/host-authority-r6.test.ts` (`11` unique cases): `vit-h-0102` (`2`), `vit-h-0304` (`2`), `vit-h-0506` (`2`), `vit-h-0708` (`2`), `vit-h-0910` (`2`), `vit-h-11` (`1`)
  - `test/session-control-composed.test.ts` (`4` unique cases): `vit-composed-0102` (`2`), `vit-composed-0304` (`2`)
  - `test/session-control-repair.test.ts` (`11` unique cases): `vit-repair-0102` (`2`), `vit-repair-0304` (`2`), `vit-repair-0506` (`2`), `vit-repair-0708` (`2`), `vit-repair-0910` (`2`), `vit-repair-11` (`1`)
  - `test/session-control.test.ts` (`26` unique cases): `vit-sc-p01` through `vit-sc-p13` (`13` runs × `2` cases = `26` passed)
  - Compatibility suites (`14` unique cases across `7` runs): `vit-refresh-12` (`2`), `vit-refresh-34` (`2`), `vit-revoke-12` (`2`), `vit-device-12` (`2`), `vit-device-34` (`2`), `vit-device-56` (`2`), `vit-pairing-0102` (`2`)
- **Novel Counterexample Probe Suites (`4` final passing runs, `10` unique counterexample probes passed)**:
  - `vit-novel-r3-12-v3` (`.local/review/novel-r3.test.ts`): `2` passed (`N1`, `N2`)
  - `vit-novel-r3-34-v2` (`.local/review/novel-r3.test.ts`): `2` passed (`N3`, `N4`)
  - `vit-novel-r3-56` (`.local/review/novel-r3.test.ts`): `2` passed (`N5`, `N6`)
  - `py-probe-r3-counterexamples-v2` (`.local/review/probe_r3_counterexamples.py`): `4` passed (`test_p1`, `test_p2`, `test_p3`, `test_p4`)
