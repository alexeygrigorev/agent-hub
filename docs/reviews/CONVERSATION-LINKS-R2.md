# M4 Conversation Links R2 Repair Review (`docs/reviews/CONVERSATION-LINKS-R2.md`)

**Verdict**: **SCOPED ACCEPT** (scoped strictly to the exact M4 repaired composed source commit `7b6f7ffeac48cc8fc26a357e100c03da7360d017`; all four prior `REVISE` findings `F1`–`F4` from `docs/reviews/CONVERSATION-LINKS.md` on `1a15cb1513db147cd51f970e11edf5ad3df9abf3` are resolved and independently verified via real-Worker Miniflare SQLite HTTP and storage probes; **`0` High, `0` Medium, `0` Low** open findings on `7b6f7ffeac48cc8fc26a357e100c03da7360d017`).

- **Reviewed candidate commit**: `7b6f7ffeac48cc8fc26a357e100c03da7360d017` (`fix: initialize standalone M4 links and bound orphan retention [skip ci]`)
- **Intermediate repair commit**: `98aa5621fae24a55a38164e8ed912ce21c0a8553` (`fix: fence M4 replays and preserve canonical dispositions [skip ci]`)
- **Retained prior review commit**: `ab3b953dbbadcd10e6f0034ec4a970564735ba0d` (`docs/reviews/CONVERSATION-LINKS.md`, preserved byte-identical)
- **Original M4 composed candidate**: `1a15cb1513db147cd51f970e11edf5ad3df9abf3` (composed from `dcb27b74230da1b173a950ccbf264d4d1963ec76` on base `16d66d69708268ab84089447534f0439e0bfd893`)
- **Accepted product base commit (`main` lineage at composition)**: `16d66d69708268ab84089447534f0439e0bfd893` (subsequent `10c65ff` observed on `origin/main` before the review pin added only a post-main review document over `16d66d69708268ab84089447534f0439e0bfd893` with product code unchanged)
- **Repair author**: `platform-links-r2` (`gpt-6`, Codex session `eacd3c77-8eab-4b91-a795-8652b5436228`; original M4 author `platform-links/grok-4.6`, session `f04bc012-7703-489b-b504-281ca9beedda`)
- **Independent reviewer**: `platform-links-r2-review` (`gemini-4-argon-high`, Antigravity session `9673eaac-402c-4327-8f22-1b2ff1aa0c24`)
- **Governance & milestone boundaries**:
  - Source acceptance of `7b6f7ffeac48cc8fc26a357e100c03da7360d017` is strictly separate from `main` integration, live runtime state, and M11 physical cross-host adoption.
  - Task `0422` fork/WIP registration of `7b6f7ffeac48cc8fc26a357e100c03da7360d017` records provenance only, not acceptance or integration.
  - Routing compatibility request `01a147d0-8fdc-7d83-94aa-5281c5ab8dd6` to `routing` remains queued and unacknowledged; source acceptance is not a routing compatibility ACK, and no ACK is assumed or fabricated.

---

## 1. Source Integrity & File Digest Audit (`16d66d697082..7b6f7ffeac48`)

All `9` changed/added paths between `16d66d69708268ab84089447534f0439e0bfd893` and `7b6f7ffeac48cc8fc26a357e100c03da7360d017` are confined to the M4 conversation-links scope. The original `8`-test suite `test/conversation-links.test.ts` and the prior independent review report `docs/reviews/CONVERSATION-LINKS.md` are byte-for-byte unchanged (`0` weakened assertions, `0` digest re-pins). All shared routing, fleet, jobs, schema, registry, admission, and Wrangler configuration files are untouched.

| Path | Base SHA-256 (`16d66d697082`) | Prior Candidate SHA-256 (`1a15cb1513db`) | Repaired Candidate SHA-256 (`7b6f7ffeac48`) | Bytes (`7b6f7ffeac48`) |
| --- | --- | --- | --- | --- |
| `docs/CONVERSATION-LINKS.md` | `new` | `18016f5f762c08b8928b977bf9738ac227fd9dbaa388270ec9353c76f26eb86d` | `1e255f4dae488d8ce93dad6a9666738a6ae956ba5e9f8e3fbc85789c42326f06` | `9,217` |
| `docs/CONVERSATIONS.md` | `38fc8d3ecbded91f06ede50ecdcbc73f49a4b8d79a715542e7063206749f33cb` | `a2c436dd7070321d8de86133c28ad6dfb07b4485aee993aee1971995cf12a8b4` | `a3048bf523a4b52496769e5da2faedfe67fe23c5e55d4b1099fa822befb585fc` | `46,205` |
| `docs/reviews/CONVERSATION-LINKS.md` | `new` (in `ab3b953dbbad`) | `9e168c620dc3063d3a3a6686695b3634ec4ebc14f8ba2ce682136812c78d4dcf` | `9e168c620dc3063d3a3a6686695b3634ec4ebc14f8ba2ce682136812c78d4dcf` (unchanged) | `29,550` |
| `src/conversation-hub.ts` | `948457385592374ab455523a5727614fcff1c12661c954b707e011c8cf0e0b72` | `3c842cfbdea05c2e4c466ac4c9db41c4075b1186ad4339c90c687f7518d4f0ab` | `7a8e44ceb47dc717f088d46e3f0c620b3746ac949fc2ad8eba78fb0dd0255be8` | `6,409` |
| `src/conversation-links.ts` | `new` | `25c129baa86791a0356f7c2436dc62a2cba3b159f7eb0c3e9f371a062320d83f` | `3574d057647e1a602e1637f5e8b2d9fb5eeebbce70c7a46d05c161569312a331` | `20,496` |
| `src/conversations.ts` | `2448644aa7462e1a09c73890014989850242cbefd79e9cad841d4e866c6c3147` | `57197bd2adea3fb30c996256e45fbb49dbbaaec37f70fda39fab49dbc1fd45c3` | `799521abf77df9e8bc061d3f543bad6e5b32d87aeea11fc96eb5da78cc1763d2` | `126,770` |
| `test/conversation-links.test.ts` | `new` | `740887d6e7b2ede3aeedfdb0a43bfdca279f1f21a1ca3027af55e5ccf812410e` | `740887d6e7b2ede3aeedfdb0a43bfdca279f1f21a1ca3027af55e5ccf812410e` (unchanged) | `23,836` |
| `test/conversation-links-r2.test.ts` | `new` | `n/a` | `29bb67056dadd892f7d4d819e7e642912b56e90152cf6925f3947ffdb6657dd1` | `31,032` |
| `test/vitest.conversation-links.config.ts` | `new` | `f8e88e0de278e342690140aa58f57aa0e45247c4ec6ceae22ccc392ef97c5c9d` | `b8b0d33fe40d567c4329f37b0eab81291a0ad3cae71424d0a899ac345c562df1` | `920` |

**Verified unchanged shared files** (`16d66d69708268ab84089447534f0439e0bfd893` == `7b6f7ffeac48cc8fc26a357e100c03da7360d017`):
- `src/routing.ts`: `7614418591b63b2f673ef2cf0ef08f4bce09405898e79d40965a1bb6335e5847` (`25,302` B)
- `src/fleet.ts`: `95f12961ff4633439f35e39e441d89443fa325c562d0ed2e9939944bc68a32a9` (`34,804` B)
- `src/schema.ts`: `1dacf018852752ad82e4342276be036b9cbf8c7387eccbbf5281e47a13619ded` (`4,884` B)
- `src/jobs.ts`: `3c19f230e0f7101b799b36774edf68bc7e9199d033fbe5b3288a494071fd6b69` (`120,230` B)
- `src/env.ts`: `264e73739035c653b11761c13a33bfd12c811f66e42b223896a1f25573e09c09` (`819` B)
- `src/claim_admission.ts`: `5b6b728de08f32784f03c9a4123356b25fce2425a886a83f2be392b4e63b3dc5` (`13,696` B)
- `src/agent_registry.ts`: `5aa252d73029b51f4852a7850a02cf24d4b197b4a2e235a366862f99a49c0a3e` (`11,556` B)
- `wrangler.jsonc`: `ec4e4bfb05f15e00ca7d6fae724aa015e5ef2841d83c44af0b0e7aa1a5a5d921` (`1,087` B)

---

## 2. Independent Verification of Prior `REVISE` Findings (`F1`–`F4`) & Effect-Time Stale Authority

### Finding F1 (High — Resolved): Current Target Authority Precedes Cached Idempotency Replay, Conflict, and Duplicate-Ref Paths
- **Repair Location**: `src/conversation-links.ts:186-243` (`ConversationLinksService.attach`)
- **Verification**:
  - Inside `this.txSync(() => { ... })`, after live `this.conversations.verifyPrincipal(auth, { requireWriteScope: true, isMutatingHttp: true })` and `this.conversations.assertActiveMember(conversationId, principal)`, `attach()` now executes:
    ```ts
    const visible = this.inspectTarget(kind, ref, principal);
    if (!visible.row) {
      throw new ConversationDomainError('not_found', 404);
    }
    ```
    at lines `208-211` **before** `priorKey` (`lines 213-229`) and `priorRef` (`lines 231-242`).
  - **Cross-member & conflict-probe isolation**: When `human:alice` attaches a foreign `routing_job` or admin-only `fleet_launch` to a shared conversation, non-owning or non-admin members (`projA:worker-1`, `projB:worker-2`) calling `POST /api/conversations/:id/links` with no `idempotency_key`, a fresh key, or an already-used key (`k-own`) receive opaque `404 { "error": "not_found" }` rather than `200 OK` replay or `409 idempotency_conflict`.
  - **Deleted/recreated target & scope downgrade on retry**: Deleting a previously attached `routing_job`, recreating its UUID under `actor = 'foreign:other'`, or stripping `routing:submit` / `fleet:launch` from `credentials.scopes` causes both keyed and unkeyed replay attempts to fail closed with `404 { "error": "not_found" }`.
  - **Untested effect-time stale authority challenged (`.local/review/adversarial.probe.test.ts`)**: Using zero-highWaterMark `ReadableStream` bodies on both `POST /api/conversations/:id/links` and `DELETE /api/conversations/:id/links/:linkId` that mutate SQLite state *after* pre-body `authenticateConversationRequest` + initial `verifyPrincipal` succeed, we independently verified that effect-time `txSync` revalidation rejects:
    1. Mid-stream membership removal (`conversation_members.removed_at` set -> `403 member_removed`).
    2. Mid-stream conversation archive expiry (`conversations.archived_at` expired -> `404 not_found`).
    3. Mid-stream write-scope removal (`bus:send` stripped from `credentials.scopes` -> `403 scope_missing`).
    4. Mid-stream human admin revocation (`users.is_admin = 0` -> `403 forbidden`).
    5. Mid-stream target ownership loss on a `priorKey` conflict candidate (`routing_jobs.actor` changed to `'foreign:other'` -> `404 not_found` before `409 idempotency_conflict`).
    6. Mid-stream credential revocation, `agents.project` change, `agents.machine_id` change, and `credentials.machine_id` mismatch (`401` / `403`), with zero unintended row mutations or link deletions.

### Finding F2 (High — Resolved): Standalone `ConversationsService` Lifecycle, Bounded `100`-Row Startup Reconciliation, `limit = 1` Fairness & Atomic Rollback
- **Repair Location**: `src/conversations.ts:819, 833-838, 871, 886, 1002-1004, 1258-1275, 1377-1384, 1420-1423` and `src/conversation-hub.ts:43-45, 157-160`
- **Verification**:
  - `ConversationsService` constructor wraps `txSync` with a reentrant depth guard (`let depth = 0; ...`) so nested `linksHandler` calls join the active outer transaction without issuing nested `BEGIN`, and constructs `this.linksHandler = new ConversationLinksService(sql, this, this.txSync)`. Composed `Hub` (`src/conversation-hub.ts:43-45`) shares `this.linksService = this.conversationsService.linksHandler`.
  - `ConversationsService.init()` creates both `CONVERSATIONS_SCHEMA_SQL` and `this.linksHandler.init()` (`CONVERSATION_LINKS_SCHEMA_SQL`) inside the same transaction, then reconciles pre-existing orphan links with one bounded fair pass:
    ```ts
    if (this.linksHandler.hasExpiredRows(this.now())) {
      this.retentionCleanup(this.now(), DEFAULT_RETENTION_BATCH_LIMIT);
    }
    ```
    Confirmed with `105` (`test/conversation-links-r2.test.ts:386`) and `112` (`.local/review/adversarial.probe.test.ts`) pre-existing orphan links: `init()` deletes at most `100` rows, leaves `hasExpiredRows() === true`, and subsequent bounded `retentionCleanup` calls drain the remainder without unbounded startup scans.
  - Orphan links are registered as the fourth fair category in `runFairRetentionPass(this.sql, 'conv', ...)` (`src/conversations.ts:1377-1384, 1420-1423`) with an explicit zero-budget guard (`const budget = Math.min(remaining, stepBudget); if (budget <= 0) return 0;`), preventing the second leftover pass of `runFairRetentionPass` from normalizing `0` to `1` and over-deleting by 1 row.
  - Injecting a failure after `DELETE FROM conversation_links` inside standalone `retentionCleanup` rolls back `conversation_links`, `conversations`, `conversation_members`, `conversation_storage_usage`, and `retention_cleanup_cursor` atomically.

### Finding F3 (High — Resolved): Canonical Job Dispositions Preserved Across Worker Exit/Failure and `review.complete` True Only on `accepted`
- **Repair Location**: `src/conversation-links.ts:43-48, 115-126, 614-642, 660`
- **Verification**:
  - `JOB_TERMINAL = new Set(['done', 'accepted', 'failed', 'cancelled'])`, `JOB_ACTIVE = new Set(['queued', 'launched', 'running'])`, `JOB_KNOWN = new Set([...JOB_TERMINAL, ...JOB_ACTIVE, 'completed-awaiting-review', 'waiting-on-human', 'stuck'])`.
  - `deriveJobStatus(job)` (`src/conversation-links.ts:614-642`) overlays `fleet_launches.state` (and the 30-minute `JOB_STUCK_AFTER_MS` inactivity transition) **only** when `JOB_ACTIVE.has(status) && launch && LAUNCH_KNOWN.has(String(launch.state))`. Explicit canonical dispositions (`completed-awaiting-review`, `cancelled`, `accepted`, `done`, `failed`, `waiting-on-human`, `stuck`) and unrecognized states (`canonical_status: null, terminal: false, complete: false`) are never overwritten by an `exited` or `failed` worker launch.
  - `jobStatusView(status, kind)` (`src/conversation-links.ts:115-126`) sets:
    - `terminal = JOB_TERMINAL.has(status) && (kind !== 'review' || status !== 'done')`
    - `complete = status === 'accepted' || (kind !== 'review' && status === 'done')`
    For `kind === 'review'`, `complete` is `true` **only** when `status === 'accepted'`, and `status === 'done'` remains `terminal: false, complete: false`.

### Finding F4 (Medium — Resolved): `reverse()` Enforces Membership, Principal Kind & Archive Expiry in SQL Before `LIMIT`
- **Repair Location**: `src/conversation-links.ts:414-453` (`ConversationLinksService.reverse`)
- **Verification**:
  - `reverse()` verifies `inspectTarget(kind, ref, principal)` first (`404 not_found` when missing or unauthorized), then executes a single keyset-paginated SQL query joining `conversation_members m` (`m.actor = ? AND m.kind = ? AND m.removed_at IS NULL`) and `conversations c` (`(c.archived_at IS NULL OR c.archived_at > ?)` using `now() - archivedConversationRetentionMs`) **before** `ORDER BY l.created_at DESC, l.id DESC LIMIT ?` (`limit + 1`).
  - Removed-member, wrong-principal-kind, non-member, and expired-archived conversations never consume `LIMIT` slots, truncate `next_cursor`, or leak foreign `conversation_id`s or cursor anchors, while non-expired archived conversations remain visible to active members.

---

## 3. Full M4 Acceptance Criteria Matrix

| Criterion | Verdict | Evidence |
| --- | --- | --- |
| Dual-gate auth on attach (`POST /api/conversations/:id/links`) including replay & conflict paths | **PASS** | `src/conversation-links.ts:187-242`; `test/conversation-links-r2.test.ts:197-256`; `.local/review/adversarial.probe.test.ts` |
| Reuse of existing routing/fleet GET visibility (`canInspectJob`, `canInspectLaunch`) | **PASS** | `src/conversation-links.ts:553-588`; non-admin agents receive opaque `404` on foreign jobs and all `fleet_launch` refs |
| Live F7 identity, bound machine, revocation, expiry, session admin & CSRF inside `txSync` | **PASS** | `src/conversations.ts:1465-1783`; verified across pre-body and streamed-body effect-time mutations on `POST` and `DELETE` |
| Canonical status derivation (`done`, `failed`, `cancelled`, `completed-awaiting-review`, `accepted`, `stuck`) & `review.complete` only on `accepted` | **PASS** | `src/conversation-links.ts:43-48, 115-126, 614-642`; `test/conversation-links-r2.test.ts:259-300`; `.local/review/adversarial.probe.test.ts` |
| Reverse lookup (`GET /api/conversations/links`) SQL pre-`LIMIT` filtering & cursor privacy | **PASS** | `src/conversation-links.ts:414-453`; `test/conversation-links-r2.test.ts:302-330`; `.local/review/adversarial.probe.test.ts` |
| Per-conversation (`32`) and global (`4,096`) link caps, scalar-only rows, tombstone views on deleted targets | **PASS** | `src/conversation-links.ts:244-270, 663-681`; `test/conversation-links.test.ts:535-607, 674-717`; `test/conversation-links-r2.test.ts:497-512` |
| Standalone & composed `init()`, `100`-row startup orphan reconciliation, `limit = 1` fair cleanup & atomic rollback | **PASS** | `src/conversations.ts:878-1006, 1098-1449`; `test/conversation-links-r2.test.ts:348-441`; `.local/review/adversarial.probe.test.ts` |
| No task submission/mutation on link attach/detach; routing submit without `conversation_id` unchanged | **PASS** | `test/conversation-links.test.ts:184-268, 719-743`; `test/conversation-links-r2.test.ts:332-345` |

---

## 4. Independent Verification Runs, Author Evidence Audit & Resource Peaks

### Author Evidence Audit (`.local/links-r2/`)
Read-only inspection of the author's `.local/links-r2/final.json`, `progress.md`, and `runs.jsonl` confirmed:
- All `5` intermediate failed runs (`links-r2`, `links-complete`, `typecheck-complete`, `effect-diagnostic`, `links-effect-fixed`) and their `.log` files were honestly retained and explained, including the real product bug caught and fixed by the author during `links-r2` (the second invocation in `runFairRetentionPass` passing `0` remaining budget to `runOrphanLinksStep`, fixed by `if (budget <= 0) return 0;` in `src/conversations.ts:1379`).
- Author final runs passed `27` selected tests (`8` original links + `11` new r2 links + `2` platform + `5` retention + `1` export) and `tsc --noEmit` (`0` errors) within resource bounds (`7,819,264` B allocated scratch peak).

### Independent Reviewer Serial Runs & Resource Peaks (`.local/review/runs.jsonl`)
All reviewer runs executed serially on CPU core `0` (`taskset -c 0`), `umask 077`, `TMPDIR=/home/alexey/git/agent-hub-wt-platform-links-r2-review/.local/tmp`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, and `--configLoader runner` with `cacheDir: '.local/tmp/vite-cache'`.

| Run Label | Command | Exit | Tests (Pass / Fail) | Duration (s) | Scratch Peak Alloc (B) | Scratch Peak Apparent (B) | Peak Inodes | Cgroup `memory.current` Peak (B) | Cgroup `memory.peak` (B) | Cgroup `pids` (cur / peak) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `typecheck` | `./node_modules/.bin/tsc --noEmit` | `0` | n/a (`0` errors) | `1.477` | `520,192` | `501,149` | `9` | `414,265,344` | `418,197,504` | `19 / 19` |
| `conversation-links-r2` | `./node_modules/.bin/vitest run --configLoader runner -c test/vitest.conversation-links.config.ts` | `0` | `19 / 0` (`8` orig + `11` r2) | `8.551` | `8,245,248` | `8,191,806` | `58` | `667,140,096` | `667,389,952` | `39 / 39` |
| `conversation-platform` | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/platform.config.ts` | `0` | `2 / 0` | `4.566` | `1,134,592` | `1,092,414` | `30` | `640,057,344` | `667,389,952` | `40 / 40` |
| `retention-subset` | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/compat.config.ts test/conversation-retention.test.ts -t "mixed 50 messages\|odd small limits\|injected cleanup rollback\|hub cursor INSERT abort\|rejects NaN"` | `0` | `5 / 0` | `5.043` | `3,334,144` | `3,285,309` | `43` | `647,634,944` | `667,389,952` | `39 / 40` |
| `hub-export` | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/compat.config.ts test/conversation-integration.test.ts -t "preserves Hub export name"` | `0` | `1 / 0` | `4.491` | `1,114,112` | `1,066,794` | `32` | `641,986,560` | `667,389,952` | `40 / 40` |
| `adversarial-probe-r2` | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/vitest.adversarial.config.ts` | `0` | `3 / 0` (independent Argon probe) | `4.800` | `2,772,992` | `2,720,149` | `39` | `651,755,520` | `667,389,952` | `40 / 40` |

- **Overall reviewer scratch peak** (`.local/review` + `.local/tmp` including directory inodes): `8,245,248` B allocated (`7.86 MiB`), `8,191,806` B apparent (`7.81 MiB`), `58` inodes — well below the `14 MiB` (`14,680,064` B) early guard and `20 MiB` (`20,971,520` B) hard cap (`guard_triggered: false`).
- **Containment cgroup peaks** (`aplexer-workload-9673eaac-402c-4327-8f22-1b2ff1aa0c24.scope`): `memory.peak = 667,389,952` B (`636.47 MiB` vs `1500 MiB` max), `pids.peak = 40` (vs `256` max).

---

## 5. Retained Evidence Locators

- **Tracked review report**: `docs/reviews/CONVERSATION-LINKS-R2.md`
- **Retained prior review report**: `docs/reviews/CONVERSATION-LINKS.md`
- **Retained reviewer private evidence (gitignored under `/home/alexey/git/agent-hub-wt-platform-links-r2-review/.local/review/`)**:
  - `.local/review/adversarial.probe.test.ts`
  - `.local/review/vitest.adversarial.config.ts`
  - `.local/review/platform.config.ts`
  - `.local/review/compat.config.ts`
  - `.local/review/measure_run.py`
  - `.local/review/runs.jsonl`
  - `.local/review/final.json`
  - `.local/review/*.log`
