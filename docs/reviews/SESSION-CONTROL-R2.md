# M12e Session Control Repair R2 Review (`2ca4700a80af316538319da462a4c17dac17867a`)

- **Verdict**: **SCOPED REVISE**
- **Reviewed Commit (`HEAD`)**: `2ca4700a80af316538319da462a4c17dac17867a` — `[skip ci] M12e repair: reconcile delayed replies, qualify senders, enforce live family, durable acks, canonical Fleet binding, bounded pagination`
- **Sole Direct Parent (`HEAD^1`)**: `bd3d64196e4bccde5b1146b5ea1a17cc22d669c3` — `docs(review): correct Git ancestry metadata and sampling cadence disclosure in SESSION-CONTROL.md [skip ci]`
- **Initial Review Commit (`HEAD~2`)**: `4d1462ca38b0cd47f153fc08b2d3fbc47cea87a9` — `docs(review): independent M12e connected session control review (SCOPED REVISE) [skip ci]`
- **Initial Composed Implementation Commit (`HEAD~3`)**: `f9417de39d7818f18cf6f4a3a8770c6a67518db0` — `[skip ci] M12e compose: real-Worker composed loop proves server agreement`
- **Accepted `main` Base Ancestor**: `6bc91a1278250b374ea1d0b11acd353fae51a8cb` — `docs: correct independent review Git metadata before publication [skip ci]` (first-parent ancestor via `d841c6456badd27c1026d74abb3cae7e26473cf5`)
- **Author Identity**: `platform-session-repair` (`1f455987-a036-4ff8-8789-e8e5d845da3e`), OpenCode Go / **Muse Spark 1.3** (`opencode-go/muse-spark-1.3-contributor`)
- **Reviewer Identity (`.local/review/first-action-worker.json`)**:
  - `id`: `e0fb234a-784a-4cd4-b9e9-5c8ef76184aa`
  - `tag`: `platform-session-r2-review`
  - `engine`: `antigravity` — **Gemini 4 Argon High** (`gemini-4-argon-high`, different model family from Muse Spark 1.3)
  - `workspace` / `cwd`: `/home/alexey/git/agent-hub-wt-platform-session-r2-review` (Agent Branches `task0430`)
  - `parent_session`: `e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363` (`platform` in `/home/alexey/git/agent-coordination`)
  - `worker_pid` / `workload_pid`: `1510597` / `1510620`
  - `worker_cgroup`: `/user.slice/user-1000.slice/user@1000.service/app.slice/pocketshell-gateway.service`
  - `workload_cgroup`: `/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-e0fb234a-784a-4cd4-b9e9-5c8ef76184aa.scope`
  - `containment_cgroup`: `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-e0fb234a-784a-4cd4-b9e9-5c8ef76184aa.scope`
  - **Configured Outer Limits (`first-action-worker.json`)**: `memory_bytes = 1,572,864,000` (`1500 MiB`), `pids = 256`, `cpu_quota_us = 100000`, `cpu_period_us = 100000`, `history_bytes = 4,194,304`, CPU affinity pinned to core `3` (`taskset -c 3` / `sched_setaffinity`), `umask 077`, private `TMPDIR` under `/home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/tmp`

---

## 1. Executive Summary & Verdict Rationale

Commit `2ca4700a80af316538319da462a4c17dac17867a` delivers substantial, well-structured repairs across `src/session_control.ts`, `src/hub.ts`, `host/agent_hub_host/{native_sessions,session_executor,session_receiver,session_control}.py`, and `docs/{SESSION-CONTROL,SESSION-HOST}.md`, and adds 28 new repair regression tests (`17` Python host tests in `host/tests/test_session_repair.py` and `11` Worker tests in `test/session-control-repair.test.ts`). All **169 unique source test cases** (`77` original + `28` new repair + `64` compatibility) and `./node_modules/.bin/tsc --noEmit` were independently executed and passed under `.local/review/run_monitored.py` (`peak_allocated_bytes = 11,898,880` B = `11.35 MiB`, strictly below the `14 MiB` early guard and `18 MiB` hard cap; `0` guard aborts).

However, passing the 169 source test cases is not runtime closure. Independent code inspection, **7 novel Worker + Python host adversarial probes (`W1`–`W4`, `H1`–`H3`)**, and **2 narrowly authorized genuine throwaway native shell probes (`platform-session-r2-probe-sender` `0ee0cbbc-6c27-49ab-b514-747aaf043444` and `platform-session-r2-probe-target` `c89852c8-a547-477d-88a7-e534b3b0420b`)** demonstrate that **SCOPED REVISE** is required on `2ca4700a80af316538319da462a4c17dac17867a` before acceptance:

1. **R2-F1 (HIGH — Inherited Core Authority Dependency on `src/registry.ts` & `src/hub.ts`, Reproduced in `W1` & `W2`)**:
   - Although `SessionControlService.hostOk` (`src/session_control.ts:231-247`) consults `machines.credential_id -> credentials.family_id` on every call, `2ca4700a` inherits the unpatched `POST /api/machines/register` (`src/registry.ts:171-204`) and `POST /api/device/token` (`src/hub.ts:659-681`) routes identified in the independent M9 R4 review (`docs/reviews/METRICS-R4.md`, commit `4e6b2964` on `0c9493fd`).
   - **Reproduced on `2ca4700a`**:
     - In **`W1`**, a superseded, unrevoked host credential family (`host-cred-1`, `host-family-1`) calls `POST /api/machines/register` after the machine was re-enrolled to `host-cred-new` (`host-family-new`). Because `src/registry.ts:196-200` unconditionally executes `UPDATE machines SET ..., credential_id = ? WHERE id = ?` with no CAS or family continuity fence, `host-cred-1` overwrites `machines.credential_id`, hijacks pre-bind `POST /api/sessions/:id/binding` (`200 OK`), adopts `host_family = 'host-family-1'`, and locks `host-cred-new` out of `/offers`, `/claim`, `/ack`, and `/binding`.
     - In **`W2`**, after newer host pairing `B` exchanges its device token, binds a session (`host_family = family_B`), and receives an enqueued command, a delayed exchange of an older approved host device code `A` via `POST /api/device/token` unconditionally overwrites `machines.credential_id` to `A` (`src/hub.ts:676`), locking `B` out of its own active bound session (`GET /api/session-commands/offers` returns `offers: []` because `m.machine_family !== family_B`).
   - *Cross-lane status*: A separate Muse worker prepared core host-authority repairs on `f20b35a60af2de17c91303dd317da70b44f56174` (under independent Codex M9 R5 review `task0433`, and combined in head draft `b969` with `269` verified source pairs). Per review rules, `f20b35a6` is not imported into `2ca4700a` and the inherited authority weakness is not waived.
2. **R2-F2 (HIGH — F3 Post-`await body(request)` Authority & `NULL` Family Stamping Gap in `src/session_control.ts`, Reproduced in `W3`)**:
   - `POST /api/sessions` (`src/session_control.ts:458-544`), `POST /api/sessions/:id/binding` (`lines 572-587`), and `POST /api/sessions/:id/send|capture|stop` (`lines 602-625` & `enqueue` `lines 857-884`) check `agentLive` / `hostOk` / `ownerOk` **before** `await body(request)` and do **not** re-verify live caller/machine authority inside `this.transaction(...)`.
   - **Reproduced in `W3`**: When a host credential is revoked and the machine is re-enrolled to a new family while `await body(request)` streams on `POST /api/sessions/:id/binding`, the transaction still commits (`200 OK`), transitions the session to `running`, and leaves `host_family = NULL` (because `adoptHostFamily` sees `presentingFamily(caller) === null` and silently no-ops). Similarly, when an agent credential is revoked while `await body(request)` streams on `POST /api/sessions/:id/send`, `enqueue` still commits (`201 Created`) and inserts `owner_family = NULL` into `session_control_commands`.
3. **R2-F3 (HIGH — F1 Server/Host Asymmetry When Late Reply ACK Arrives After `claim_expires_at` Before `GET /offers` Sweep, Reproduced in `W4` & `H1`)**:
   - In `src/session_control.ts:785-795`, `POST /api/session-commands/:id/ack` invokes `this.lateReplyAck(row, input, caller)` **only** when `row.state === 'uncertain'` (which happens only *after* `GET /api/session-commands/offers` has swept expired claims). When `row.state === 'claimed'` and `this.claimExpired(row)` is `true` (e.g. `reconcile_pending_command` runs after restart before `/offers`, or `claim_expires_at` elapses during `receive_once` between `GET /offers` at line 284 and `reconcile_pending_command` at line 345), line 793 sets `state = 'uncertain'` and returns **`409 claim_expired`** even when the host presents a valid `outcome: 'delivered'` send ACK with valid unused `proof` (`W4`).
   - **Host-side corruption (`H1`)**: When `reconcile_pending_command` (`_reconcile_unknown_locked`, `host/agent_hub_host/session_executor.py:596-603`) receives that `409 claim_expired`, it overwrites the local journal's `state` from `"acknowledged"` to `"uncertain"` (`ack_submitted: False`). Thereafter:
     1. `reconcile_pending_command` permanently skips the journal (`prior.get("state") != "unknown"`, line 672),
     2. `handle_claimed_command` with the same claim raises `SessionUncertain` at line 790 before reaching `_resubmit_ack_locked` at line 803, and
     3. even if `resubmit_pending_ack` runs on the next cycle and the server (now in `'uncertain'`) accepts `lateReplyAck` as `acked` (`200 OK`), `_resubmit_ack_locked` (`line 643`) writes `updated = {**prior, "ack_submitted": True}`, leaving the local host journal and `receive_once` reconciliation summary permanently stuck at `state = "uncertain"` instead of `"acknowledged"`.
4. **R2-F4 (MEDIUM — F1/F4 Host Journal Starvation via Unfiltered `[:20]` Slice & Infinite 409 Resubmit Loop, Reproduced in `H2` & `H3`)**:
   - **Starvation (`H2`)**: `receive_once` (`host/agent_hub_host/session_receiver.py:335-337`) slices `sorted(name for name in os.listdir(journal_dir) if name.startswith("cmd-") and name.endswith(".json"))[:20]` **before** filtering out already-completed journals (`ack_submitted == True`). Because completed `cmd-<uuid>.json` files are never deleted or skipped prior to slicing, once 20 completed commands have UUIDs sorting lexicographically before a pending command's UUID, `receive_once` never visits the pending command again for F1 late-reply reconciliation or F4 lost-ACK resubmission.
   - **Infinite 409 retry loop (`H3`)**: Although `_resubmit_ack_locked`'s docstring (`session_executor.py:623`) states *"a terminal server verdict (HTTP 409) closes it as uncertain"*, `resubmit_pending_ack` (`lines 699-705`) checks only `if prior.get("state") == "unknown": return None` and does **not** check `prior.get("state") == "uncertain"` or `prior.get("ack_error")`. Because `_resubmit_ack_locked` sets `state = "uncertain", ack_submitted = False` while keeping `ack_payload`, every subsequent `receive_once` cycle re-POSTs a rejected 409 ACK (`claim_conflict`, `stale_generation`, `invalid_late_proof`) forever.
5. **R2-F5 (MEDIUM — Live Native Protocol & Lifecycle Fidelity Gaps Verified by Own 2-Shell Probes)**:
   - **F2 Cross-Workspace Reply Routing vs Same-Workspace-Only Sender Qualification**: Live probe between `platform-session-r2-probe-sender` (`0ee0cbbc-6c27-49ab-b514-747aaf043444` in `.local/review/native-sender`) and `platform-session-r2-probe-target` (`c89852c8-a547-477d-88a7-e534b3b0420b` in `.local/review/native-target`) proved that installed `a message reply <MESSAGE_ID> <TEXT>` across workspaces **succeeds (`exit 0`)** when the sender is a bound session, and writes the reply envelope (`01a14981-aec0-7be0-b1f0-d18bad858a36`) into the **sender's** workspace mailbox (`.local/review/native-sender`), NOT the target's workspace mailbox (`cross_reply_in_target_workspace_log: null`). However, `qualify_sender_binding` (`native_sessions.py:394-398`) refuses whenever `sender_ws != target.workspace` under the inaccurate message `"recipients in the target workspace could not reply"`, and `verify_ack_via_reply` (`line 578`) reads only `target.workspace`. Consequently, a host receiver running in one service session workspace cannot send to target sessions in any other workspace on the machine.
   - **F6 Installed `envelope.from` Shape vs `verify_ack_via_reply` Incarnation Provenance**: Live probe proved that the installed `a` binary's message and reply envelopes carry `"from": {"engine", "session_id", "tag", "workspace"}` **without** `"created_at_ms"` (only the synthetic `REPAIR_FAKE_A` fixture in `host/tests/test_session_repair.py:111,149` fabricated `"created_at_ms"` inside `envelope.from`). Because `sender.get("created_at_ms")` is always `None` on the real installed `a` binary, `verify_ack_via_reply` (`native_sessions.py:574,637,648`) discards the `NativeSession` `row` returned by `qualify_target` and falls back to `target.created_at_ms` (the expected value) instead of recording `row.created_at_ms` (the observed live catalog value).
   - **F5 Engine Binding Check & Confirmed Containment Stop Gaps**: Neither `applyBinding` (`src/session_control.ts:338-348`) nor `qualify_target` / `qualify_sender_binding` (`host/agent_hub_host/native_sessions.py`) verifies `engine` (e.g., `reported.engine === launch.engine`), and `stop_session` (`native_sessions.py:723-779`) neither verifies cgroup/process `containment_empty` after `a kill` nor distinguishes a post-kill vanished catalog row from a concurrent `a forget`/`a prune`.

---

## 2. Mechanical Verification of Source Envelope & Git Ancestry

Pre-test (`20261017T102632Z-source-verify-pre-clean`) and post-test (`20261017T105718Z-source-verify-post`) runs of `.local/review/verify_source_envelope.py` mechanically verified all Git and filesystem facts (`.local/review/source-verification-pre.json` and `.local/review/source-verification-post.json`):

| Check | Expected / Manifest Value | Observed Value | Status |
| :--- | :--- | :--- | :---: |
| Worktree `HEAD` | `2ca4700a80af316538319da462a4c17dac17867a` | `2ca4700a80af316538319da462a4c17dac17867a` | **PASS** |
| Sole direct parent (`HEAD^1`) | `bd3d64196e4bccde5b1146b5ea1a17cc22d669c3` | `bd3d64196e4bccde5b1146b5ea1a17cc22d669c3` (1 parent) | **PASS** |
| Grandparent (`HEAD~2`) | `4d1462ca38b0cd47f153fc08b2d3fbc47cea87a9` | `4d1462ca38b0cd47f153fc08b2d3fbc47cea87a9` | **PASS** |
| Great-grandparent (`HEAD~3`) | `f9417de39d7818f18cf6f4a3a8770c6a67518db0` | `f9417de39d7818f18cf6f4a3a8770c6a67518db0` | **PASS** |
| Accepted `main` ancestor | `6bc91a1278250b374ea1d0b11acd353fae51a8cb` | `6bc91a1278250b374ea1d0b11acd353fae51a8cb` (verified via `git merge-base --is-ancestor`) | **PASS** |
| Manifest path | `/home/alexey/git/agent-coordination/.local/platform/session-r2-source-manifest.json` | `246` `(path, sha256)` entries (`243` original + `3` new repair test/config paths) | **PASS** |
| Compact sorted JSON SHA-256 | `6ec9613d0a6cc22a67bd1bc73d23e719bc004f5bd1881c6af284fbcb8a7cd15c` | `6ec9613d0a6cc22a67bd1bc73d23e719bc004f5bd1881c6af284fbcb8a7cd15c` (raw pretty file SHA-256 `aaa39b8fe440b89b354e83564f177b3f2d3ef2cced192c4d02aa27d66e95d42e`) | **PASS** |
| Proof file | `/home/alexey/git/agent-coordination/.local/platform/session-r2-source-proof.json` | Matches `head == 2ca4700a...`, `parent == bd3d6419...`, `tracked_count == 246`, `manifest_sha256 == 6ec9613d...` | **PASS** |
| Git blob & worktree SHA-256 match | All `246` tracked paths match manifest pre & post | `246 / 246` matched (`0` blob mismatches, `0` worktree mismatches) | **PASS** |
| Old reviews & proposals immutable | `git diff bd3d6419..2ca4700a -- docs/reviews docs/proposals` empty | `23` files under `docs/reviews/*` (including `SESSION-CONTROL.md` at `bd3d6419` and baseline `CLAIM-ADMISSION.md`) and `docs/proposals/*` have `0` diff bytes | **PASS** |
| Distinct from M9 246-file envelope | `metrics-r4-source-manifest.json` (`4877d953...`) | Both have `246` entries, but `18` session-control paths exist only in M12e, `18` metrics paths exist only in M9, and `5` shared paths differ (`docs/API.md`, `host/agent_hub_host/cli.py`, `src/hub.ts`, `tsconfig.json`, `vitest.config.ts`) | **PASS** |

### Exact Verbatim Git Ancestry Chain (`6bc91a127825..2ca4700a80af`)

| Commit SHA | Parent SHA(s) | Verbatim Commit Subject |
| :--- | :--- | :--- |
| `2ca4700a80af316538319da462a4c17dac17867a` | `bd3d64196e4bccde5b1146b5ea1a17cc22d669c3` | `[skip ci] M12e repair: reconcile delayed replies, qualify senders, enforce live family, durable acks, canonical Fleet binding, bounded pagination` |
| `bd3d64196e4bccde5b1146b5ea1a17cc22d669c3` | `4d1462ca38b0cd47f153fc08b2d3fbc47cea87a9` | `docs(review): correct Git ancestry metadata and sampling cadence disclosure in SESSION-CONTROL.md [skip ci]` |
| `4d1462ca38b0cd47f153fc08b2d3fbc47cea87a9` | `f9417de39d7818f18cf6f4a3a8770c6a67518db0` | `docs(review): independent M12e connected session control review (SCOPED REVISE) [skip ci]` |
| `f9417de39d7818f18cf6f4a3a8770c6a67518db0` | `0aabc8c05f43374b17aea3d7e7775fb8f22808a3` | `[skip ci] M12e compose: real-Worker composed loop proves server agreement` |
| `0aabc8c05f43374b17aea3d7e7775fb8f22808a3` | `15faace087551bd551621575dc77ea21dac1f737` | `[skip ci] M12e compose: connected loop tests + corrected contract docs` |
| `15faace087551bd551621575dc77ea21dac1f737` | `9bb9a7777772d778c011b4c860a0947a663b2d3e` | `[skip ci] M12e compose: corrected host proof + bounded host-receive receiver` |
| `9bb9a7777772d778c011b4c860a0947a663b2d3e` | `d84ca875898a3aaf89ef8dabdefcf93c7f4640b6` | `[skip ci] M12e compose: server claim/offers/binding contract for connected receiver` |
| `d84ca875898a3aaf89ef8dabdefcf93c7f4640b6` | `d841c6456badd27c1026d74abb3cae7e26473cf5 69ae9f1e9a8f4cf6e33fc205080f7ee1350ca15e` | `chore: compose native session host adapter [skip ci]` |
| `d841c6456badd27c1026d74abb3cae7e26473cf5` | `6bc91a1278250b374ea1d0b11acd353fae51a8cb cd0e9d099efea884ee9e52fcbe7c0901e03d4128` | `chore: compose hub session API with accepted main [skip ci]` |
| `6bc91a1278250b374ea1d0b11acd353fae51a8cb` | `4c7cf04bd689b81c867bd2622f2c116bede9d94a` | `docs: correct independent review Git metadata before publication [skip ci]` |

### Changed Paths in `bd3d64196e4bccde5b1146b5ea1a17cc22d669c3..2ca4700a80af316538319da462a4c17dac17867a`

15 modified existing paths (`M`) + 3 newly added repair test/config paths (`A`, bringing tracked files from `243` to `246`):
- **Modified (`15`)**: `docs/SESSION-CONTROL.md`, `docs/SESSION-HOST.md`, `host/agent_hub_host/native_sessions.py`, `host/agent_hub_host/session_control.py`, `host/agent_hub_host/session_executor.py`, `host/agent_hub_host/session_receiver.py`, `host/tests/test_native_bridge_contract.py`, `host/tests/test_session_control.py`, `host/tests/test_session_control_loop.py`, `host/tests/test_session_executor.py`, `host/tests/test_session_real_worker.py`, `src/hub.ts`, `src/session_control.ts`, `test/session-control-composed.test.ts`, `test/session-control.test.ts`.
- **Added (`3`)**: `host/tests/test_session_repair.py`, `test/session-control-repair.test.ts`, `vitest.session-repair.config.ts`.

---

## 3. Criterion-by-Criterion Evaluation of Initial Findings (F1–F6)

| Criterion | Summary of Author Repair in `2ca4700a` | Remaining Defects / Gaps | Status |
| :--- | :--- | :--- | :---: |
| **F1: Asynchronous `send` ACK & late-reply reconciliation** | Removed premature `POST /ack` (`outcome: 'uncertain'`) on 0ms initial check in `handle_claimed_command` (`session_executor.py:849-854`); added `reconcile_pending_command` (`lines 650-678`) + post-cycle reconciliation pass in `receive_once` (`session_receiver.py:333-358`); added server `lateReplyAck` (`src/session_control.ts:627-686`) fenced by `holder_attempt`, `claim_token`, `holder`, `generation`, `hostOk`, and unused `session_control_proofs` row; CLI `cmd_send` treats `transported` as non-terminal (`session_control.py:21,158`). | **1)** Server `POST /api/session-commands/:id/ack` (`src/session_control.ts:785-795`) calls `lateReplyAck` only when `row.state === 'uncertain'`. When `row.state === 'claimed'` and `claimExpired(row)` is true (before `GET /offers` sweeps it), server sets `state = 'uncertain'` and returns `409 claim_expired` even for a valid `delivered` proof (`W4`). **2)** On receiving that `409`, `_reconcile_unknown_locked` (`session_executor.py:596-603`) sets local journal `state = 'uncertain'`, after which `reconcile_pending_command` skips it, `handle_claimed_command` raises `SessionUncertain`, and `resubmit_pending_ack` leaves local journal `state = 'uncertain'` even after server ACKs (`H1`). **3)** `receive_once` slices `sorted(os.listdir(journal_dir))[:20]` before filtering completed journals (`H2`). | **FAIL (REVISE)** |
| **F2: Genuine bound sender qualification & `envelope.from` validation** | Added `qualify_sender_binding` (`native_sessions.py:345-409`) and `validate_native_send_envelope` (`lines 207-237`) before/after `message send`; removed `--from` fabrication; receiver pre-checks `qualify_sender_binding` (`session_receiver.py:290-295`) before claiming `send` offers so unbound callers leave offers unclaimed. | Live 2-shell probe proved that installed `a message reply <MESSAGE_ID> <TEXT>` across workspaces **succeeds (`exit 0`)** for a bound sender and writes the reply into the **sender's** workspace mailbox (`.local/review/native-sender`), whereas `qualify_sender_binding` (`native_sessions.py:394-398`) refuses whenever `sender_ws != target.workspace` and `verify_ack_via_reply` (`line 578`) reads only `target.workspace`. A host receiver in one service workspace cannot send to targets in any other workspace. | **PARTIAL / CAPABILITY GAP** |
| **F3: Live SQL credential, actor registry, machine & family continuity checks** | `presentingFamily` (`src/session_control.ts:171-190`), `agentLive` (`lines 196-204`), `ownerOk` (`lines 206-221`), and `hostOk` (`lines 231-247`) consult `credentials`, `agents`, and `machines` live in SQLite; removed the `owner_token_id` shortcut (`ownerOk` now requires `presentingFamily(caller)` and `agentLive(caller)` on every non-admin call); `src/hub.ts:251-257` requires live unrevoked `credentialActor` for agent bearer tokens. | **1)** Inherited `POST /api/machines/register` (`src/registry.ts:196-200`) and `POST /api/device/token` (`src/hub.ts:676`) overwrite `machines.credential_id` without CAS or family continuity, allowing a superseded host family (`W1`) or delayed older device grant (`W2`) to hijack `hostOk` and lock out the current bound host family. **2)** `POST /api/sessions/:id/binding`, `POST /api/sessions/:id/send|capture|stop`, and `POST /api/sessions` do not re-verify `hostOk` / `ownerOk` / `presentingFamily` inside `this.transaction(...)` after `await body(request)` (`W3`). | **FAIL (REVISE)** |
| **F4: Durable locally completed effect & lost-ACK recovery** | `handle_claimed_command` persists `ack_payload` and `ack_submitted` in `cmd-<id>.json` (`session_executor.py:883-897`); added `resubmit_pending_ack` (`lines 681-707`) + server idempotent replay on identical `claim_token` (`src/session_control.ts:764-784`). | **1)** `resubmit_pending_ack` (`session_executor.py:699-705`) checks `prior.get("state") == "unknown"` but fails to check `prior.get("state") == "uncertain"` or `ack_error`, causing terminal HTTP 409 ACKs (`claim_conflict`, `stale_generation`, etc.) to be retried on every `receive_once` cycle forever (`H3`). **2)** `receive_once` `[:20]` slice starves `resubmit_pending_ack` behind 20 completed journals (`H2`). | **FAIL (REVISE)** |
| **F5: Authoritative Fleet launch + `fleet_sessions` binding reconciliation** | `applyBinding` (`src/session_control.ts:320-348`) verifies `fleet_launches` (`machine_id`, `state IN ('starting','running')`, `tag === 'hub-' + launch.id`, `native_id`) AND `fleet_sessions` (`SELECT tag, workspace, phase FROM fleet_sessions WHERE machine_id = ? AND native_id = ?`, non-terminal phase, exact `tag` and `workspace` match). | **1)** `applyBinding` (`src/session_control.ts:338-348`) and `native_sessions.py` (`qualify_target`) do not check `fleet_sessions.engine` against `session_control_sessions.engine` (`launch.engine`), accepting a `shell` or mismatched-engine native session if tag/workspace match. **2)** `stop_session` (`native_sessions.py:723-779`) does not verify cgroup/process `containment_empty` after `a kill`. | **FAIL (REVISE)** |
| **F6: Cursor pagination & honest reply provenance** | Added `cursorClause` over `(created_at, id)` in `src/session_control.ts:268-277,551-558,593-600`; added bounded multi-page cursor traversal in `wait_for_command` (`session_control.py:34-79`) and `cmd_list` (`lines 125-146`) with seen-cursor loop detection; `verify_ack_via_reply` (`native_sessions.py:596-621`) checks `sender.get("created_at_ms")` and requires both `correlation_id` and `body_digest` in body fallback; docs updated to state host-verified provenance. | Live 2-shell probe showed that real `a message` envelopes do **not** include `created_at_ms` inside `envelope.from` (`from` has only `engine`, `session_id`, `tag`, `workspace`). In `verify_ack_via_reply` (`native_sessions.py:574,637,648`), the `NativeSession` `row` returned by `qualify_target` is discarded and `sender_created_at_ms` falls back to `target.created_at_ms` (expected) instead of `row.created_at_ms` (observed live catalog value). | **MINOR / REVISE** |

---

## 4. Detailed Findings, Reproductions & Required Fixes

### Finding R2-F1 (HIGH): Inherited Core Host-Authority Weaknesses in `src/registry.ts` and `src/hub.ts` Break `SessionControlService.hostOk` (`W1`, `W2`)

- **Locations**:
  - `src/registry.ts:171-204` (`POST /api/machines/register`)
  - `src/hub.ts:659-681` (`POST /api/device/token`)
  - `src/session_control.ts:231-247` (`SessionControlService.hostOk`)
- **Analysis**:
  - `SessionControlService.hostOk` (`src/session_control.ts:237-241`) checks:
    ```ts
    const m: any = this.sql.exec(
      'SELECT m.id, m.credential_id, c.family_id AS machine_family FROM machines m LEFT JOIN credentials c ON c.id = m.credential_id WHERE m.id = ?',
      session.machine_id).toArray()[0];
    if (!m) return { ok: false, error: 'not_found' };
    if (m.machine_family && m.machine_family !== family) return { ok: false, error: 'host_credential_changed' };
    ```
  - Because `hostOk` trusts `machines.credential_id` as the authoritative current machine enrollment, any route in the same Durable Object that allows a stale/superseded credential or older device pairing to overwrite `machines.credential_id` directly compromises `hostOk`:
    1. **`POST /api/machines/register` (`src/registry.ts:173-200`, reproduced in `W1`)**: `verifyBearerScope` checks only that the bearer token is an unrevoked, non-expired `host` credential with `cred.machine_id === 'hetzner'`. Line 196 then unconditionally runs:
       ```sql
       UPDATE machines SET hostname = ?, os = ?, network = ?, roles = ?, capabilities = ?, credential_id = ? WHERE id = ?
       ```
       If a machine was re-enrolled to `host-cred-new` (`host-family-new`) without revoking an older credential `host-cred-1` (`host-family-1`), `host-cred-1` is initially rejected by `hostOk` (`409 host_credential_changed`). But once `host-cred-1` calls `POST /api/machines/register`, `machines.credential_id` is overwritten back to `host-cred-1`. Immediately afterward, `host-cred-1` succeeds in binding pre-bind sessions (`POST /api/sessions/:id/binding` -> `200 OK`, adopting `host_family = 'host-family-1'`) and locks `host-cred-new` out of `/offers`, `/claim`, `/ack`, and `/binding`.
    2. **`POST /api/device/token` (`src/hub.ts:659-681`, reproduced in `W2`)**: When two host device pairings (`OLD-CODE` and `NEW-CODE`) for `hetzner` are approved and `NEW-CODE` exchanges its token first (`credBRow`, `family_B`), binds a session (`host_family = family_B`), and has a command enqueued, a delayed token exchange of `OLD-CODE` via `POST /api/device/token` unconditionally executes `UPDATE machines SET credential_id = ?, last_checkin_at = ? WHERE id = ?` (`src/hub.ts:676`). Thereafter, `m.machine_family` is `family_A`, while `session.host_family` is `family_B`, so **neither** `A` nor `B` can pass `hostOk` — permanently bricking the bound session.
- **Reproduction**:
  - Executed in `.local/review/novel-worker-probes.test.ts` (`run_id: 20261017T104505Z-vitest-novel-worker-4-ok`, tests `W1` and `W2`, `exit_code: 0`).
- **Required Fix**:
  - Couple acceptance with the core host-authority repair (`task0433` / `f20b35a6` once independently reviewed and accepted): `POST /api/machines/register` must re-verify credential liveness inside a synchronous transaction after `await safeJson(request)` and enforce that `machines.credential_id` either is `NULL` or belongs to the **same `family_id`** as the presenting credential, and `POST /api/device/token` must reject stale device-code exchanges when a newer enrollment is already bound to `machines.credential_id`.

---

### Finding R2-F2 (HIGH): F3 Post-`await body(request)` Authority & `NULL` Family Stamping Gap in `src/session_control.ts` (`W3`)

- **Locations**:
  - `src/session_control.ts:458-544` (`POST /api/sessions`)
  - `src/session_control.ts:565-587` (`POST /api/sessions/:id/binding`)
  - `src/session_control.ts:602-625` & `857-884` (`POST /api/sessions/:id/send|capture|stop` and `enqueue`)
- **Analysis**:
  - In `src/session_control.ts`, request bodies are read asynchronously via `const input = await body(request)`. While `await body(request)` is pending, concurrent requests or WebSocket events in the Durable Object can revoke the caller's credential, delete the agent row, or re-enroll/delete the host machine:
    1. In `POST /api/sessions/:id/binding` (`lines 572-587`), `this.hostOk(session, caller)` is checked at line 572 **before** `const input = await body(request)` at line 574. Inside `this.transaction(() => { ... })` (`lines 578-587`), `this.hostOk(session, caller)` is **not** re-checked, and `this.adoptHostFamily(session.id, caller)` (`lines 249-254`) silently returns early if `this.presentingFamily(caller)` is `null`. Thus, if `caller`'s credential is revoked or the machine is re-enrolled during `await body(request)`, `applyBinding` still commits (`200 OK`), transitions the session to `running`, and leaves `host_family = NULL`.
    2. In `POST /api/sessions/:id/send|capture|stop` (`lines 602-625`), `this.ownerOk(session, caller)` is checked at line 603 **before** `const input = await body(request)` at line 605. Inside `this.transaction(() => this.enqueue(...))` (`lines 857-884`), when `!prior`, `this.ownerOk(session, caller)` is **not** re-checked, and line 882 inserts `this.presentingFamily(caller)` directly into `owner_family`. If `caller`'s credential was revoked during `await body(request)`, `enqueue` commits (`201 Created`) and stamps `owner_family = NULL`.
    3. In `POST /api/sessions` (`lines 458-544`), `this.agentLive(caller)` is checked at line 460 **before** `const input = await body(request)` at line 461, and when `!prior` inside `this.transaction(...)` (`lines 483-505`), `this.agentLive(caller)` / `this.presentingFamily(caller)` are not re-verified before inserting the session row.
- **Reproduction**:
  - Executed in `.local/review/novel-worker-probes.test.ts` (`run_id: 20261017T104505Z-vitest-novel-worker-4-ok`, test `W3`, `exit_code: 0`): a chunked `ReadableStream` body revokes `host-cred-1` and re-enrolls `hetzner` to `host-cred-2` inside `pull()` after initial `hostOk` passed; `POST /api/sessions/:id/binding` still returns `200 OK` with `state === 'running'` and `host_family === null`. A second chunked `ReadableStream` revokes `cred-A` during `POST /api/sessions/:id/send`; it still returns `201 Created` with `owner_family === null`.
- **Required Fix**:
  - Re-verify `this.hostOk(session, caller)` (and non-null `presentingFamily(caller)` for non-admin callers) inside `this.transaction(...)` in `POST /api/sessions/:id/binding`; re-verify `this.ownerOk(session, caller)` inside `this.enqueue(...)` before inserting a new command; and re-verify `this.agentLive(caller)` (and non-null `presentingFamily(caller)` for non-admin callers) inside `this.transaction(...)` in `POST /api/sessions` before inserting a new session.

---

### Finding R2-F3 (HIGH): F1 Server/Host Asymmetry When `POST /ack` Arrives After `claim_expires_at` Before `GET /offers` Sweep (`W4`, `H1`)

- **Locations**:
  - `src/session_control.ts:785-799` (`POST /api/session-commands/:id/ack`)
  - `host/agent_hub_host/session_executor.py:592-608` (`_reconcile_unknown_locked`)
  - `host/agent_hub_host/session_executor.py:631-647` (`_resubmit_ack_locked`)
  - `host/agent_hub_host/session_executor.py:789-812` (`handle_claimed_command`)
- **Analysis**:
  - On the server (`src/session_control.ts:785-795`):
    ```ts
    if (row.state === 'uncertain') {
      return this.lateReplyAck(row, input, caller);
    }
    if (row.state !== 'claimed') return response({ error: 'claim_required', state: row.state }, 409);
    if (!row.claim_token || row.claim_token !== input.claim_token || row.holder !== caller.machineId) {
      return response({ error: 'claim_conflict' }, 409);
    }
    if (this.claimExpired(row)) {
      this.sql.exec("UPDATE session_control_commands SET state = 'uncertain', updated_at = ? WHERE id = ?", this.at(), row.id);
      return response({ error: 'claim_expired', state: 'uncertain' }, 409);
    }
    ```
  - Notice the ordering dependency: a claimed command whose lease (`claim_expires_at`) has elapsed transitions from `'claimed'` to `'uncertain'` **only** when `GET /api/session-commands/offers` sweeps expired claims (`lines 695-698`) **or** when a failed `POST /ack` hits lines 792-794.
  - Therefore, if `POST /api/session-commands/:id/ack` arrives with a valid `outcome: 'delivered'` send proof *after* `claim_expires_at` has passed but *before* `GET /api/session-commands/offers` has swept the row to `'uncertain'` (which happens whenever `reconcile_pending_command` is called directly after a restart or lease expiry, or when `claim_expires_at` elapses during `receive_once` between `GET /offers` at `session_receiver.py:284` and `reconcile_pending_command` at `line 345`), the server **rejects the valid delivered proof with `409 claim_expired`** (`W4`).
  - Worse, on the host (`H1`), when `_reconcile_unknown_locked` (`session_executor.py:596-603`) receives that `409 claim_expired`, it overwrites the local journal's `state` from `"acknowledged"` to `"uncertain"` (`ack_submitted: False`). From that point on:
    1. `reconcile_pending_command` (`line 672`) permanently skips the journal because `prior.get("state") != "unknown"`.
    2. `handle_claimed_command` (`line 790`) raises `SessionUncertain` before reaching `_resubmit_ack_locked` at line 803.
    3. Even when `resubmit_pending_ack` runs on the next cycle and the server (now in `'uncertain'` as a side effect of the previous 409) accepts `lateReplyAck` (`200 OK`, server `state: 'acked'`), `_resubmit_ack_locked` (`line 643`) writes `updated = {**prior, "ack_submitted": True}`, leaving the local host journal and `receive_once` reconciliation summary corrupted at `state = "uncertain"` instead of `"acknowledged"`.
- **Reproduction**:
  - Server half reproduced in `.local/review/novel-worker-probes.test.ts` (`W4`, `run_id: 20261017T104505Z-vitest-novel-worker-4-ok`).
  - Host half reproduced in `.local/review/test_novel_host_probes.py` (`test_H1_late_reply_reconcile_409_corrupts_local_journal_state_to_uncertain_even_after_server_acks`, `run_id: 20261017T105330Z-py-novel-host-3-all-pass`).
- **Required Fix**:
  - In `src/session_control.ts:792-795`, when `row.state === 'claimed'` and `this.claimExpired(row)` is `true`, if `row.kind === 'send' && input.outcome === 'delivered'`, delegate to `this.lateReplyAck(row, input, caller)` (which already verifies the holder, claim token, attempt, generation, `hostOk`, and unused `proof`, and transitions the row to `'uncertain'` only on failure) instead of unconditionally failing with `409 claim_expired`.
  - In `host/agent_hub_host/session_executor.py:643` (`_resubmit_ack_locked`), when a resubmitted ACK succeeds (`200 OK`) and `payload.get("outcome") == "delivered"`, restore the completed local journal state (`"acknowledged"` for `send`, `"captured"` for `capture`, `"stopped"` for `stop`) if `prior.get("state") == "uncertain"`.

---

### Finding R2-F4 (MEDIUM): Host Receiver `[:20]` Journal Starvation and Infinite 409 Resubmit Loop (`H2`, `H3`)

- **Locations**:
  - `host/agent_hub_host/session_receiver.py:335-358` (`receive_once`)
  - `host/agent_hub_host/session_executor.py:611-648,681-707` (`_resubmit_ack_locked`, `resubmit_pending_ack`)
- **Analysis**:
  1. **Unfiltered `[:20]` journal slice (`H2`)**: In `session_receiver.py:335-337`:
     ```python
     pending = sorted(
         name for name in os.listdir(journal_dir)
         if name.startswith("cmd-") and name.endswith(".json"))[:20]
     ```
     Because `cmd-<uuid>.json` files are never deleted after `ack_submitted = True` and `receive_once` slices `[:20]` **before** inspecting whether each journal file is still pending (`state == "unknown"` or `not ack_submitted`), as soon as 20 completed command UUIDs sort lexicographically before a pending command's UUID (e.g., `cmd-00000000-...` before `cmd-ffffffff-...`), the pending command is never visited again by `receive_once` for F1 late-reply reconciliation or F4 lost-ACK resubmission.
  2. **Terminal 409 never closes journal in `resubmit_pending_ack` (`H3`)**: In `_resubmit_ack_locked` (`session_executor.py:635-641`), on HTTP 409 the code writes:
     ```python
     updated = {**prior, "state": "uncertain", "ack_submitted": False,
                "ack_error": "server_terminal_%s" % type(exc).__name__}
     _write_journal(journal_file, updated)
     ```
     However, `resubmit_pending_ack` (`lines 699-705`) checks only:
     ```python
     if prior.get("ack_submitted") or not isinstance(prior.get("ack_payload"), dict):
         return None
     if prior.get("state") == "unknown":
         return None
     return _resubmit_ack_locked(...)
     ```
     Because `resubmit_pending_ack` does not return `None` when `prior.get("state") == "uncertain"` (or when `prior.get("ack_error", "").startswith("server_terminal_")`), every subsequent `receive_once` cycle calls `_resubmit_ack_locked` again and re-POSTs the rejected 409 ACK forever. (The author's test `test_f4_terminal_server_verdict_closes_journal_without_new_effect` in `host/tests/test_session_repair.py:542-568` missed this because it stopped after the second `receive_once` cycle and never ran a third cycle.)
- **Reproduction**:
  - Executed in `.local/review/test_novel_host_probes.py` (`test_H2_receive_once_journal_slice_20_starves_pending_reconcile_and_resubmit_behind_completed_journals` and `test_H3_terminal_409_never_closes_journal_in_resubmit_pending_ack_causing_infinite_409_retry_loop`, `run_id: 20261017T105330Z-py-novel-host-3-all-pass`).
- **Required Fix**:
  - In `session_receiver.py:335-358`, filter journal files to those actually pending (`state == "unknown"` or `(not ack_submitted and state != "uncertain")`) before applying the bounded per-cycle cap of 20.
  - In `session_executor.py:701` (`resubmit_pending_ack`), return `None` when `prior.get("state") in ("unknown", "uncertain")` or `str(prior.get("ack_error") or "").startswith("server_terminal_")`.

---

### Finding R2-F5 (MEDIUM): Live Native Protocol & Lifecycle Fidelity Gaps (`native_sessions.py` & `src/session_control.ts`)

- **Locations**:
  - `host/agent_hub_host/native_sessions.py:345-409` (`qualify_sender_binding`)
  - `host/agent_hub_host/native_sessions.py:547-654` (`verify_ack_via_reply`)
  - `host/agent_hub_host/native_sessions.py:723-779` (`stop_session`)
  - `src/session_control.ts:338-348` (`applyBinding`)
- **Live Native Shell Probe Setup & Literal Installed Commands (`.local/review/native-shell-probe-evidence.json`, `run_id: 20261017T105636Z-native-shell-probes`)**:
  - After inspecting `a --help`, `a start --help`, `a status --help`, `a kill --help`, `a message {send,reply,log} --help`, and fresh `quse` (`20261017T105355Z-native-cli-inspect-help-quota`), we launched **two** throwaway `shell` sessions using the installed `a` binary:
    ```bash
    a start --workspace /home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-target \
      --tag platform-session-r2-probe-target --engine shell \
      --memory 128M --pids 32 --cpu-quota-us 100000 --cpu-period-us 100000 --history-bytes 16384 --json -- python3 target_worker.py ...
    a start --workspace /home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-sender \
      --tag platform-session-r2-probe-sender --engine shell \
      --memory 128M --pids 32 --cpu-quota-us 100000 --cpu-period-us 100000 --history-bytes 16384 --json -- python3 sender_worker.py ...
    ```
  - Automatically assigned native session identities (verified via `a whoami --json` inside each child session):
    - `platform-session-r2-probe-target`: `id = "c89852c8-a547-477d-88a7-e534b3b0420b"`, `engine = "shell"`, `created_at_ms = 1792234596879`, `workspace = "/home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-target"`.
    - `platform-session-r2-probe-sender`: `id = "0ee0cbbc-6c27-49ab-b514-747aaf043444"`, `engine = "shell"`, `created_at_ms = 1792234597281`, `workspace = "/home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-sender"`.
- **1. Cross-Workspace Reply Storage vs Same-Workspace Restriction (`F2`)**:
  - Inside `platform-session-r2-probe-sender`, calling `native_sessions.qualify_sender_binding("a", target_ws)` raised:
    ```text
    SessionControlError: native_sender_unbound: own native session lives in '/home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-sender', not the target workspace '/home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-target'; recipients in the target workspace could not reply
    ```
  - However, when `platform-session-r2-probe-sender` executed the literal installed command:
    ```bash
    a message send --workspace /home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-target \
      --to platform-session-r2-probe-target --kind note \
      --data '{"hub_session_control":{"correlation_id":"cross-ws-r2-1792234596744","body_digest":"7a305708336a66edb5aa8c4a1fbb90c3626412c245f414785b08715d7bbf1902"}}' \
      --json -- "cross-ws body r2-1792234596744"
    ```
    and `platform-session-r2-probe-target` read `a message inbox --json` and executed:
    ```bash
    a message reply 01a14981-acc3-7ff0-a8fd-7d6c8cd07dfe \
      --data '{"body_digest":"7a305708336a66edb5aa8c4a1fbb90c3626412c245f414785b08715d7bbf1902","correlation_id":"cross-ws-r2-1792234596744"}' \
      --json -- "cross-ack cross-ws-r2-1792234596744"
    ```
    **the installed `a message reply` succeeded (`returncode: 0`) across workspaces** and emitted this verbatim reply envelope:
    ```json
    {
      "schema_version": 1,
      "id": "01a14981-aec0-7be0-b1f0-d18bad858a36",
      "workspace": "/home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-sender",
      "created_at": 1792234598,
      "from": {
        "session_id": "c89852c8-a547-477d-88a7-e534b3b0420b",
        "workspace": "/home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-target",
        "tag": "platform-session-r2-probe-target",
        "engine": "shell"
      },
      "to": {
        "tag": "platform-session-r2-probe-sender",
        "session_id": "0ee0cbbc-6c27-49ab-b514-747aaf043444"
      },
      "kind": "reply",
      "data": {
        "body_digest": "7a305708336a66edb5aa8c4a1fbb90c3626412c245f414785b08715d7bbf1902",
        "correlation_id": "cross-ws-r2-1792234596744"
      },
      "body": "cross-ack cross-ws-r2-1792234596744",
      "reply_to": "01a14981-acc3-7ff0-a8fd-7d6c8cd07dfe",
      "delivery": "inbox"
    }
    ```
  - Notice `"workspace": "/home/alexey/git/agent-hub-wt-platform-session-r2-review/.local/review/native-sender"`: the installed `a` binary writes cross-workspace replies into the **sender's** workspace mailbox (`a message log --workspace .../native-sender --json` contains `01a14981-aec0-7be0-b1f0-d18bad858a36`, while `a message log --workspace .../native-target --json` has `cross_reply_in_target_workspace_log: null`). Because `verify_ack_via_reply` (`native_sessions.py:578`) reads only `fetch_message_log(executable, target.workspace)` and `qualify_sender_binding` (`lines 394-398`) rejects `sender_ws != target.workspace`, a host receiver running in one service workspace cannot send to targets in any other workspace on the host.
- **2. Installed `envelope.from` Shape Omits `created_at_ms` (`F6`)**:
  - As shown in the verbatim envelope above (and in the same-workspace reply envelope `01a14981-ad87-7ea1-ae93-f0fe0f567207`), the installed `a` binary's `from` object has keys `{"session_id", "workspace", "tag", "engine"}` and **never** includes `"created_at_ms"` or `"external"` (which were fabricated only in `REPAIR_FAKE_A` at `host/tests/test_session_repair.py:111,149`).
  - Consequently, in `verify_ack_via_reply` (`native_sessions.py:574,637,648`), `sender.get("created_at_ms")` is always `None` on the real installed `a` binary, and because line 574 discards the `NativeSession` `row` returned by `qualify_target(executable, target, live_only=False)`, line 648 falls back to `target.created_at_ms` (expected) instead of `row.created_at_ms` (observed live catalog incarnation).
- **3. Missing Engine Binding Check & Post-Stop Containment Verification (`F5`)**:
  - Neither `applyBinding` (`src/session_control.ts:338-348`) nor `qualify_target` (`native_sessions.py:310-343`) checks that `fleet_sessions.engine` (or `NativeSession.engine`) matches the session's requested AI engine (`row.engine` / `launch.engine`).
  - After stopping both throwaway probe sessions via `native_sessions.stop_session("a", s_target)` and `native_sessions.stop_session("a", t_target)` (`a kill --workspace ... --tag ... --signal HUP --grace-ms 2000 --json`), `a status --workspace ... --tag ... --json` returned `returncode: 1` (`a: no matching session`) and our probe verified directly against `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-<id>.scope` that both cgroups were removed (`cgroup_exists: false, containment_empty: true, pids_current: 0, procs: []`). However, `stop_session` (`native_sessions.py:723-779`) only checks `list_sessions` and never verifies cgroup/process `containment_empty`.
- **Required Fix**:
  - Support bound cross-workspace service senders by reading the sender workspace log in `verify_ack_via_reply` when `sender_binding.workspace != target.workspace` (matching the installed `a message reply` routing contract).
  - Capture `row = qualify_target(executable, target, live_only=False)` in `verify_ack_via_reply` (`native_sessions.py:574`), use `row.created_at_ms` when `sender.get("created_at_ms")` is absent, and remove the fabricated `from.created_at_ms` from `REPAIR_FAKE_A` so tests exercise the real `a` envelope shape.
  - Check `reported.engine === launch.engine` in `src/session_control.ts:338-348` (`applyBinding`), and verify `containment_empty` in `stop_session` (`native_sessions.py:723-779`).

---

## 5. Independent Execution of All 169 Unique Source Test Cases + Novel Probes

All **169 unique source test cases** claimed by the author (`77` original + `28` new repair + `64` compatibility) plus `./node_modules/.bin/tsc --noEmit`, **4 novel Worker adversarial probes (`W1`–`W4`)**, **3 novel Python host adversarial probes (`H1`–`H3`)**, and **2 narrowly authorized live native shell probes** were independently executed under `.local/review/run_monitored.py` (`taskset -c 3`, `umask 077`, private per-run `TMPDIR`, Vitest `<= 4` tests per process with `--configLoader runner --maxWorkers=1 --no-file-parallelism`). Mechanical verification of the 169 unique source cases is recorded in `.local/review/source-169-summary.json`:

### 5.1 Author's 169 Unique Source Test Cases + `tsc --noEmit`

| Category | Suite / File | Monitored Run ID(s) | Unique Cases | Result |
| :--- | :--- | :--- | :---: | :---: |
| Typecheck | `./node_modules/.bin/tsc --noEmit` | `20261017T102727Z-tsc-noemit` | N/A | **PASS (`exit 0`)** |
| **Original (77)** | `host/tests/test_session_control.py` | `20261017T102843Z-py-session-control-12` | `12` | **12 passed** |
| **Original (77)** | `host/tests/test_session_control_loop.py` | `20261017T102849Z-py-session-loop-6` | `6` | **6 passed** |
| **Original (77)** | `host/tests/test_session_executor.py` | `20261017T102856Z-py-session-executor-28` | `28` | **28 passed** |
| **Original (77)** | `host/tests/test_session_real_worker.py` (real `wrangler dev` + public CLI + receiver) | `20261017T102940Z-py-session-real-worker-1` | `1` | **1 passed** |
| **Original (77)** | `test/session-control-composed.test.ts` (1 batch of 4) | `20261017T103058Z-vitest-composed-4` | `4` | **4 passed** |
| **Original (77)** | `test/session-control.test.ts` (7 disjoint batches: `4+4+4+4+4+4+2`) | `20261017T103147Z-vitest-sc-b1-4` .. `20261017T103227Z-vitest-sc-b7-2` | `26` | **26 passed** |
| **New Repair (28)** | `host/tests/test_session_repair.py` | `20261017T102910Z-py-session-repair-17` | `17` | **17 passed** |
| **New Repair (28)** | `test/session-control-repair.test.ts` (3 disjoint batches: `4+4+3`) | `20261017T103704Z-vitest-repair-ok-b1-4` .. `20261017T103722Z-vitest-repair-ok-b3-3` | `11` | **11 passed** |
| **Compat (64)** | `host/tests/test_native_bridge_contract.py` (`13` pytest items = `11` author-counted native bridge cases + `2` helper checks) | `20261017T102953Z-py-native-bridge-11` | `11` (`13` items) | **13 passed** |
| **Compat (64)** | `host/tests/test_intake.py` (`15` pytest methods, `7` subtests = `17` author-counted cases) | `20261017T103000Z-py-intake-17` | `17` (`15` methods) | **15 passed (7 subtests)** |
| **Compat (64)** | `test/intake.test.ts` (9 disjoint batches: `4×9`) | `20261017T103302Z-vitest-intake-b1-4` .. `20261017T103409Z-vitest-intake-b9-4` | `36` | **36 passed** |
| **Total Unique Source Cases** | **All 10 Suites (`77 + 28 + 64`)** | **28 Counted Suite/Batch Runs** | **169** | **169 / 169 PASS** |

### 5.2 Reviewer's Novel Adversarial & Live Native Shell Probes

| Probe Suite | Monitored Run ID | Cases | What Was Proved |
| :--- | :--- | :---: | :--- |
| `.local/review/novel-worker-probes.test.ts` (`W1`–`W4`) | `20261017T104505Z-vitest-novel-worker-4-ok` | `4` | **`W1`**: Superseded host family re-registers via `POST /api/machines/register` to hijack session `/binding` and lock out new family. **`W2`**: Delayed older approved host device code exchanged via `POST /api/device/token` overwrites `machines.credential_id` and locks active bound session host out of `/offers`. **`W3`**: Streamed `POST /api/sessions/:id/binding` and `POST /api/sessions/:id/send` commit after credential revocation / machine re-enrollment during `await body(request)` with `host_family = NULL` / `owner_family = NULL`. **`W4`**: `POST /api/session-commands/:id/ack` after `claim_expires_at` while `row.state === 'claimed'` (before `GET /offers` sweep) fails with `409 claim_expired` even for valid `delivered` proof. |
| `.local/review/test_novel_host_probes.py` (`H1`–`H3`) | `20261017T105330Z-py-novel-host-3-all-pass` | `3` | **`H1`**: `reconcile_pending_command` receiving `409 claim_expired` overwrites local journal `state` to `'uncertain'`, causing `reconcile_pending_command` to skip it, `handle_claimed_command` to raise `SessionUncertain`, and `resubmit_pending_ack` to leave local `state = 'uncertain'` even after server ACKs. **`H2`**: `receive_once` `sorted(os.listdir(journal_dir))[:20]` starves pending `cmd-ffffffff-...` behind 20 completed `cmd-00000000-...` journals. **`H3`**: `resubmit_pending_ack` fails to check `state == 'uncertain'`, retrying terminal 409 ACKs on every cycle forever. |
| `.local/review/run_native_shell_probes.py` (`platform-session-r2-probe-sender` & `platform-session-r2-probe-target`) | `20261017T105636Z-native-shell-probes` | `2` live shell sessions | Verified real `a start`, `a whoami`, `a status`, `a message send/reply/inbox/log`, `a capture`, and `a kill` on throwaway `shell` sessions (`0ee0cbbc-6c27-49ab-b514-747aaf043444` and `c89852c8-a547-477d-88a7-e534b3b0420b`), proving cross-workspace replies land in the sender's mailbox, real `envelope.from` omits `created_at_ms`, adapter accepts `engine: "shell"`, and both cgroups were verified `containment_empty: true` after stop. |

### 5.3 Inspection of Author's Fixture Changes & Retained Seed-Red / Early-Guard Evidence

- **Legitimate fixture updates in `2ca4700a`**: We inspected the diffs in `test/session-control.test.ts`, `test/session-control-composed.test.ts`, and `host/tests/test_session_*.py`. The fixture updates legitimately align pre-existing synthetic tests with the stricter F2/F3/F5 guards (using canonical `hub-<fleet_launch_id>` tags, inserting `fleet_sessions` rows before `/binding`, ordering `machines` + `agents` + `credentials` rows with `family_id`, and seeding a bound sender session in `FAKE_A` so `qualify_sender_binding` succeeds). None of the fixture updates weakened product guards.
- **Author's retained `.local/session-repair/` evidence**: Read-only inspection of the author's evidence confirmed the author's seed-red runs (`13/16` host + `9/11` server failing before repair, then passing after repair) and the author's `49` monitored runs including `3` genuine early-guard aborts at the `14 MiB` threshold (`0` hard `18 MiB` breaches by the M12e author, whereas the separate M9 historical `46.29 MiB` / `27.06 MiB` breaches remain historical incidents on that lane).
- **Full disclosure of reviewer's non-counted / setup runs**: Out of `52` total monitored commands in `.local/review/runs-index.jsonl`, `39` exited `0` and `13` exited non-zero (`4` initial iterations of `verify_source_envelope.py` while aligning manifest JSON keys; `1` `py-session-control` run missing `PYTHONPATH=host`; `2` `verify-169-counts` script iterations; `3` `vitest-repair-b1..b3` runs that exited `0` with `0` matched tests because `-t` used conceptual labels before `vitest-repair-ok-b1..b3` ran with exact `it(...)` names; `1` initial `vitest-novel-worker-4` run before adding `hubt_` prefix / `pull()` stream callback; and `5` initial `py-novel-host-3*` runs while switching from `unittest` CLI to `pytest` and fixing `rc.offer_to_command`). None of those setup trials are counted as test passes or unique coverage, and all `52` runs are retained in `.local/review/runs-index.jsonl` and `.local/review/logs/`.

---

## 6. Configured Outer Limits vs Measured Inner In-Run Resource & Cadence Telemetry

We explicitly distinguish between **configured outer containment limits** and **measured inner in-run telemetry** across all **52 monitored runs** (`.local/review/all-runs-summary.json` and `.local/review/retirement-manifest.json`):

1. **Configured Outer Limits (`.local/review/first-action-worker.json` & `.local/review/run_monitored.py`)**:
   - **Cgroup Memory Limit (`memory.max`)**: `1,572,864,000` B (`1500 MiB`), with early monitor guard at `1,520,435,200` B (`1450 MiB`).
   - **Cgroup Tasks Limit (`pids.max`)**: `256`, with early monitor guard at `230`.
   - **CPU Quota & Affinity**: `cpu_quota_us = 100000` per `cpu_period_us = 100000` (`1.0` CPU), pinned to CPU core `3` (`taskset -c 3` + `os.sched_setaffinity(0, {3})`).
   - **Scratch Disk Limits across `.local`, `.wrangler`, `dist`**: `14 MiB` (`14,680,064` B) early guard (terminating only the owned `child_pgid` via `os.killpg(child_pgid, SIGKILL)`) and `18 MiB` (`18,874,368` B) hard cap.
   - **Target Sampling Interval**: `target_interval_ms = 100.0` (monotonic clock step target; actual inter-sample gaps are measured, never assumed to be a continuous `100.0 ms`).
2. **Measured Inner In-Run Telemetry (`.local/review/runs-index.jsonl` & `.local/review/all-runs-summary.json`)**:
   - **Max Measured Scratch Allocated Bytes**: **`11,898,880` B (`11.35 MiB`)** during `20261017T103409Z-vitest-intake-b9-4` (`2,781,184` B below the `14 MiB` early guard; `6,975,488` B below the `18 MiB` hard cap).
   - **Max Measured Scratch Apparent Bytes**: **`11,619,784` B (`11.08 MiB`)** during `20261017T103409Z-vitest-intake-b9-4`.
   - **Final Post-Retirement Scratch Allocated / Apparent Bytes**: **`2,265,088` B (`2.16 MiB`)** allocated / **`1,889,660` B (`1.80 MiB`)** apparent.
   - **Early Guard Aborts**: **`0`** (`early_guard_triggered: false` on all 52 runs).
   - **Retirement Accounting**: `66` disposable per-run temp/cache entries retired **only after** verifying child PGID death (`os.kill(child_pgid, 0)` -> `ProcessLookupError`), totaling `2,912,256` allocated bytes (`2,239,931` apparent bytes), each logged with SHA-256 digests in `.local/review/retirement-manifest.json`.
   - **Max Measured Child PGID RSS**: **`652,124,160` B (`621.91 MiB`)** during `20261017T102940Z-py-session-real-worker-1` (`wrangler dev` + Miniflare `workerd`).
   - **Max Measured Cgroup `memory.current` / `memory.peak`**: **`772,124,672` B (`736.36 MiB`)** / **`780,599,296` B (`744.44 MiB`)**.
   - **Max Measured Child Tasks / Cgroup `pids.current` / `pids.peak`**: **`28` child tasks** / **`44` cgroup `pids.current`** / **`44` cgroup `pids.peak`**.
   - **Measured Inter-Sample Gaps (`min_gap_ms`, `mean_gap_ms`, `max_gap_ms`)**:
     - Across the 33 multi-second runs (`duration_ms >= 2000`), measured per-run `mean_gap_ms` ranged from **`91.950 ms` to `98.614 ms`** (short `< 350 ms` helper commands had `mean_gap_ms` between `35.172 ms` and `62.932 ms` due to the final exit sample).
     - Across all 52 runs, the **maximum single inter-sample gap was `132.969 ms`** (`20261017T103711Z-vitest-repair-ok-b2-4`).

### Per-Run Measured Resource & Cadence Table (Counted Verification, Suite, Novel & Native Probe Runs)

| Run ID | Label | Exit | Duration (ms) | Samples | Min Gap (ms) | Mean Gap (ms) | Max Gap (ms) | Peak Alloc (B) | Post-Retire Alloc (B) | Peak Child RSS (B) | Peak Tasks |
| :--- | :--- | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `20261017T102632Z-source-verify-pre-clean` | `source-verify-pre-clean` | `0` | `307.8` | `6` | `0.705` | `61.299` | `104.132` | `602,112` | `598,016` | `25,747,456` | `2` |
| `20261017T102727Z-tsc-noemit` | `tsc-noemit` | `0` | `2208.4` | `25` | `0.733` | `91.950` | `101.784` | `606,208` | `602,112` | `267,907,072` | `6` |
| `20261017T102843Z-py-session-control-12` | `py-session-control-12` | `0` | `5415.9` | `57` | `1.235` | `96.608` | `103.791` | `806,912` | `643,072` | `246,235,136` | `1` |
| `20261017T102849Z-py-session-loop-6` | `py-session-loop-6` | `0` | `7510.9` | `78` | `2.421` | `97.498` | `104.292` | `819,200` | `671,744` | `261,963,776` | `2` |
| `20261017T102856Z-py-session-executor-28` | `py-session-executor-28` | `0` | `13617.2` | `139` | `1.751` | `98.614` | `104.222` | `1,114,112` | `708,608` | `266,174,464` | `3` |
| `20261017T102910Z-py-session-repair-17` | `py-session-repair-17` | `0` | `10114.9` | `104` | `1.529` | `98.139` | `105.172` | `1,028,096` | `761,856` | `498,040,832` | `2` |
| `20261017T102940Z-py-session-real-worker-1` | `py-session-real-worker-1` | `0` | `12508.5` | `128` | `0.787` | `98.475` | `108.847` | `3,846,144` | `811,008` | `652,124,160` | `21` |
| `20261017T102953Z-py-native-bridge-11` | `py-native-bridge-11` | `0` | `7035.8` | `73` | `3.581` | `97.372` | `104.541` | `2,023,424` | `868,352` | `264,089,600` | `2` |
| `20261017T103000Z-py-intake-17` | `py-intake-17` | `0` | `6912.7` | `72` | `1.342` | `97.288` | `103.728` | `1,105,920` | `905,216` | `272,748,544` | `3` |
| `20261017T103058Z-vitest-composed-4` | `vitest-composed-4` | `0` | `6809.1` | `71` | `1.133` | `97.242` | `103.661` | `3,829,760` | `942,080` | `588,857,344` | `28` |
| `20261017T103147Z-vitest-sc-b1-4` | `vitest-sc-b1-4` | `0` | `6409.0` | `67` | `0.962` | `97.071` | `106.569` | `3,657,728` | `1,085,440` | `580,780,032` | `28` |
| `20261017T103153Z-vitest-sc-b2-4` | `vitest-sc-b2-4` | `0` | `7410.2` | `77` | `0.799` | `97.476` | `105.819` | `3,735,552` | `1,122,304` | `583,651,328` | `28` |
| `20261017T103201Z-vitest-sc-b3-4` | `vitest-sc-b3-4` | `0` | `6610.5` | `69` | `1.231` | `97.164` | `106.039` | `3,825,664` | `1,159,168` | `584,982,528` | `28` |
| `20261017T103207Z-vitest-sc-b4-4` | `vitest-sc-b4-4` | `0` | `6409.9` | `67` | `0.986` | `97.082` | `106.613` | `3,952,640` | `1,191,936` | `585,687,040` | `28` |
| `20261017T103214Z-vitest-sc-b5-4` | `vitest-sc-b5-4` | `0` | `6510.2` | `68` | `1.055` | `97.129` | `106.868` | `4,075,520` | `1,228,800` | `592,334,848` | `28` |
| `20261017T103221Z-vitest-sc-b6-4` | `vitest-sc-b6-4` | `0` | `6510.0` | `68` | `1.078` | `97.122` | `104.958` | `4,042,752` | `1,261,568` | `586,096,640` | `28` |
| `20261017T103227Z-vitest-sc-b7-2` | `vitest-sc-b7-2` | `0` | `5810.8` | `61` | `1.011` | `96.790` | `105.151` | `1,417,216` | `1,294,336` | `567,050,240` | `28` |
| `20261017T103302Z-vitest-intake-b1-4` | `vitest-intake-b1-4` | `0` | `6309.8` | `66` | `1.068` | `97.034` | `105.054` | `2,617,344` | `1,331,200` | `599,142,400` | `28` |
| `20261017T103309Z-vitest-intake-b2-4` | `vitest-intake-b2-4` | `0` | `7111.7` | `74` | `0.920` | `97.383` | `107.864` | `3,158,016` | `1,363,968` | `604,983,296` | `28` |
| `20261017T103316Z-vitest-intake-b3-4` | `vitest-intake-b3-4` | `0` | `6409.2` | `67` | `1.148` | `97.073` | `106.182` | `2,531,328` | `1,400,832` | `594,284,544` | `28` |
| `20261017T103323Z-vitest-intake-b4-4` | `vitest-intake-b4-4` | `0` | `6908.2` | `72` | `1.161` | `97.264` | `106.128` | `2,777,088` | `1,433,600` | `591,835,136` | `28` |
| `20261017T103330Z-vitest-intake-b5-4` | `vitest-intake-b5-4` | `0` | `6612.2` | `69` | `1.651` | `97.178` | `105.343` | `2,621,440` | `1,478,656` | `591,081,472` | `28` |
| `20261017T103349Z-vitest-intake-b6-4` | `vitest-intake-b6-4` | `0` | `6309.8` | `66` | `1.131` | `97.024` | `106.146` | `2,994,176` | `1,511,424` | `600,203,264` | `28` |
| `20261017T103356Z-vitest-intake-b7-4` | `vitest-intake-b7-4` | `0` | `6909.9` | `72` | `1.149` | `97.283` | `104.554` | `2,785,280` | `1,544,192` | `595,820,544` | `28` |
| `20261017T103403Z-vitest-intake-b8-4` | `vitest-intake-b8-4` | `0` | `6111.3` | `64` | `2.337` | `96.949` | `108.789` | `3,088,384` | `1,585,152` | `590,041,088` | `28` |
| `20261017T103409Z-vitest-intake-b9-4` | `vitest-intake-b9-4` | `0` | `6412.1` | `67` | `2.600` | `97.110` | `106.449` | `11,898,880` | `1,617,920` | `617,762,816` | `28` |
| `20261017T103704Z-vitest-repair-ok-b1-4` | `vitest-repair-ok-b1-4` | `0` | `7409.9` | `77` | `1.162` | `97.446` | `106.981` | `3,846,144` | `1,691,648` | `590,532,608` | `28` |
| `20261017T103711Z-vitest-repair-ok-b2-4` | `vitest-repair-ok-b2-4` | `0` | `10714.8` | `110` | `1.329` | `98.272` | `132.969` | `4,575,232` | `1,728,512` | `586,424,320` | `28` |
| `20261017T103722Z-vitest-repair-ok-b3-3` | `vitest-repair-ok-b3-3` | `0` | `9912.8` | `102` | `2.456` | `98.110` | `111.670` | `4,034,560` | `1,781,760` | `586,051,584` | `28` |
| `20261017T104505Z-vitest-novel-worker-4-ok` | `vitest-novel-worker-4-ok` | `0` | `8813.3` | `91` | `1.693` | `97.882` | `122.196` | `4,775,936` | `1,925,120` | `589,533,184` | `28` |
| `20261017T105330Z-py-novel-host-3-all-pass` | `py-novel-host-3-all-pass` | `0` | `7812.7` | `81` | `1.763` | `97.608` | `107.251` | `2,170,880` | `2,117,632` | `263,974,912` | `2` |
| `20261017T105355Z-native-cli-inspect-help-quota` | `native-cli-inspect-help-quota` | `0` | `3117.8` | `34` | `1.697` | `94.208` | `107.686` | `2,633,728` | `2,162,688` | `309,870,592` | `18` |
| `20261017T105636Z-native-shell-probes` | `native-shell-probes` | `0` | `3115.1` | `34` | `2.271` | `94.213` | `101.934` | `2,240,512` | `2,232,320` | `36,872,192` | `2` |
| `20261017T105718Z-source-verify-post` | `source-verify-post` | `0` | `321.7` | `6` | `1.662` | `62.932` | `102.409` | `2,269,184` | `2,265,088` | `29,085,696` | `2` |

---

## 7. R3 Repair Acceptance Checklist & Remaining Platform Vision / Runtime Gaps

### 7.1 Required for M12e Scoped Repair Acceptance (R3)

1. **Core Host-Authority Coupling (`R2-F1`, `W1`, `W2`)**: Integrate with the reviewed core host-authority repair (`f20b35a6` / `task0433`) so `POST /api/machines/register` (`src/registry.ts`) and `POST /api/device/token` (`src/hub.ts`) cannot overwrite `machines.credential_id` across credential families or via delayed older device pairings.
2. **Post-`await body(request)` Transaction Authority (`R2-F2`, `W3`)**: Re-verify `hostOk` / `ownerOk` / `agentLive` and non-null `presentingFamily(caller)` inside `this.transaction(...)` in `POST /api/sessions`, `POST /api/sessions/:id/binding`, and `POST /api/sessions/:id/send|capture|stop` (`src/session_control.ts`).
3. **Late Reply ACK Before `/offers` Sweep & Local Journal State Restoration (`R2-F3`, `W4`, `H1`)**: In `src/session_control.ts:792-795`, allow `this.lateReplyAck(row, input, caller)` when `row.state === 'claimed'` and `this.claimExpired(row)` is `true` for a `send` command with `input.outcome === 'delivered'`; in `host/agent_hub_host/session_executor.py`, restore the local journal's completed state when a resubmitted ACK succeeds on the server.
4. **Pending-First Journal Filtering & Terminal 409 Closure (`R2-F4`, `H2`, `H3`)**: Filter `cmd-*.json` to pending entries before applying the per-cycle limit of 20 in `receive_once` (`host/agent_hub_host/session_receiver.py:335-358`), and return `None` in `resubmit_pending_ack` (`session_executor.py:701`) when `prior.get("state") == "uncertain"` or `ack_error` indicates a terminal 409 verdict.
5. **Cross-Workspace Reply Log Resolution, Real `envelope.from` Provenance, Engine Binding & Confirmed Containment Stop (`R2-F5`)**:
   - Support cross-workspace service senders by checking the sender's workspace log in `verify_ack_via_reply` when `sender_binding.workspace != target.workspace` (where installed `a message reply` writes cross-workspace replies).
   - Capture `row = qualify_target(...)` in `verify_ack_via_reply` and use `row.created_at_ms` when `sender.get("created_at_ms")` is absent; align `REPAIR_FAKE_A` with the real `a` envelope shape (`from: {session_id, workspace, tag, engine}`).
   - Enforce `reported.engine === launch.engine` in `applyBinding` (`src/session_control.ts:338-348`) and `qualify_target` (`native_sessions.py`).
   - Verify confirmed cgroup/process `containment_empty` in `stop_session` (`native_sessions.py:723-779`), never treating an absent catalog record alone as exit proof.

### 7.2 Remaining Full Platform Vision & Runtime Gaps (Not Closed by Fixture Loops)

M12e must not be shrunk to a synthetic fixture loop. Even after the M12e scoped repair items above pass R3, full platform acceptance requires closing:

1. **Full `psc`-Replacement Session Lifecycle Surface (`list`, `start(engine, tier, host, cwd, brief)`, `send`, `capture`, confirmed `stop`)**:
   - `tier` selection (`409 tier_held`) and non-root `cwd` (`409 non_root_cwd_held`) are currently held incremental limits, not delivered capabilities; full `engine`/`tier`/`host`/`cwd`/`brief` start, confirmed delivery, capture, and confirmed containment stop remain required to replace `psc`.
2. **M12 End-to-End Assignment, Backfill & Fresh-Agent Hub Handoff**:
   - Automatic Fleet machine/account assignment, backfill when a host drains or fails, and fresh-agent hub handoff remain open.
3. **M13 Quota Enforcement & M14 One-Call Status**:
   - Live provider quota gating in the hub/host dispatch loop (M13) and the unified one-call status surface (M14) remain open (`task0432`).
4. **M8 Profile/Account Routing & Native AI Engine Qualification**:
   - End-to-end profile/account selection and native AI engine qualification (verifying real AI agent state vs throwaway `shell` processes) remain open.
5. **M11 Physical Cross-Host Real AI Loop (`grok-botbox` -> `hetzner`) & Laptop / `win35` Enrollment**:
   - Real cross-host AI session launch, prompt delivery, reply confirmation, capture, and confirmed stop between `grok-botbox` and `hetzner` (and enrollment/validation of `laptop` and `win35`) remain open and mandatory before full runtime acceptance.
