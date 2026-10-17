# M4 Conversation Links Review (`docs/reviews/CONVERSATION-LINKS.md`)

**Verdict**: **REVISE** (scoped to the exact M4 composed source commit `1a15cb1513db147cd51f970e11edf5ad3df9abf3`; **3 High** and **1 Medium** product defects identified in `src/conversation-links.ts` and `src/conversations.ts` and reproduced via real-Worker Miniflare SQLite probes).

- **Reviewed composed commit**: `1a15cb1513db147cd51f970e11edf5ad3df9abf3`
- **Accepted base parent commit (`main` lineage)**: `16d66d69708268ab84089447534f0439e0bfd893`
- **Author final source commit**: `dcb27b74230da1b173a950ccbf264d4d1963ec76` (`platform-links/grok-4-6-high`, session `f04bc012-7703-489b-b504-281ca9beedda`, authored on `7929a1bc07bfea3a2b387989800cf1b0ba140984`; early author commit `c0f0eb589cd7a456d3b0fecf3447e90e2f715160`)
- **Reviewer**: `platform-links-review` (`gemini-4-argon-high`, `antigravity`, session `e3f6e2ff-c100-48cb-b908-e0ba3df92a55`)
- **Composed tree manifest SHA-256** (`[[path, sha256], ...]` compact JSON across all `207` tracked files): `96ea8ee548250fed85679407395064a8b60dc05fd45b3ae882fbd4686bec671a` (`7` changed M4 paths byte-identical to `dcb27b74230da1b173a950ccbf264d4d1963ec76`; all `200` other tracked files byte-identical to `16d66d69708268ab84089447534f0439e0bfd893`)
- **Governance & milestone separation**:
  - Source review/acceptance on `1a15cb1513db147cd51f970e11edf5ad3df9abf3`, `main` integration, live runtime state, and physical cross-host M11 adoption are strictly separate.
  - Routing compatibility ACK `01a147d0-8fdc-7d83-94aa-5281c5ab8dd6` from `routing` remains queued and unreceived; it is not assumed or fabricated.
  - M5 (`conversation-automation` / pings) is separate and unmerged and is not assumed by M4.

---

## 1. Composed Source Integrity & Changed-Path Digests

Head composed `1a15cb1513db147cd51f970e11edf5ad3df9abf3` by merging accepted base `16d66d69708268ab84089447534f0439e0bfd893` with author commit `dcb27b74230da1b173a950ccbf264d4d1963ec76`. Independent verification against owner-relative head receipts `.local/platform/m4-composition-evidence.json` and `.local/platform/m4-composed-source-manifest.json` confirmed `0` hash mismatches across all `207` tracked files (`96ea8ee548250fed85679407395064a8b60dc05fd45b3ae882fbd4686bec671a`).

The seventh path (`test/vitest.conversation-links.config.ts`) is a test-only Vitest/Miniflare configuration file outside the initial six-file author packet; head inspected its local bindings (`test/conversation-links.test.ts` include only, `fileParallelism: false`, fake local auth bindings) and explicitly extended test scope while keeping `wrangler.jsonc` and all production configs untouched.

| Path | Base SHA-256 (`16d66d69708268ab84089447534f0439e0bfd893`) | Candidate & Author SHA-256 (`1a15cb1513db147cd51f970e11edf5ad3df9abf3` / `dcb27b74230da1b173a950ccbf264d4d1963ec76`) | Bytes |
| --- | --- | --- | --- |
| `docs/CONVERSATION-LINKS.md` | `new` | `18016f5f762c08b8928b977bf9738ac227fd9dbaa388270ec9353c76f26eb86d` | `6,602` |
| `docs/CONVERSATIONS.md` | `38fc8d3ecbded91f06ede50ecdcbc73f49a4b8d79a715542e7063206749f33cb` | `a2c436dd7070321d8de86133c28ad6dfb07b4485aee993aee1971995cf12a8b4` | `45,846` |
| `src/conversation-hub.ts` | `948457385592374ab455523a5727614fcff1c12661c954b707e011c8cf0e0b72` | `3c842cfbdea05c2e4c466ac4c9db41c4075b1186ad4339c90c687f7518d4f0ab` | `6,589` |
| `src/conversation-links.ts` | `new` | `25c129baa86791a0356f7c2436dc62a2cba3b159f7eb0c3e9f371a062320d83f` | `20,469` |
| `src/conversations.ts` | `2448644aa7462e1a09c73890014989850242cbefd79e9cad841d4e866c6c3147` | `57197bd2adea3fb30c996256e45fbb49dbbaaec37f70fda39fab49dbc1fd45c3` | `125,792` |
| `test/conversation-links.test.ts` | `new` | `740887d6e7b2ede3aeedfdb0a43bfdca279f1f21a1ca3027af55e5ccf812410e` | `23,836` |
| `test/vitest.conversation-links.config.ts` | `new` | `f8e88e0de278e342690140aa58f57aa0e45247c4ec6ceae22ccc392ef97c5c9d` | `826` |

All non-M4 files (`src/routing.ts`, `src/fleet.ts`, `src/jobs.ts`, `src/schema.ts`, `src/env.ts`, `src/claim_admission.ts`, `src/agent_registry.ts`, `wrangler.jsonc`, and all existing tests) are completely unchanged from `16d66d69708268ab84089447534f0439e0bfd893`.

---

## 2. Blocking Findings Requiring `REVISE`

### Finding F1 (High): `POST /api/conversations/:id/links` Replay & Duplicate-Ref Checks Precede `inspectTarget()`, Bypassing Target ACLs and Live Target Existence Checks

- **Location**: `src/conversation-links.ts:204-238` (`ConversationLinksService.attach`)
- **Root Cause**:
  Inside `ConversationLinksService.attach()`, the idempotency-key replay lookup (`priorKey`, lines `204-220`) and the conversation-scoped `(conversation_id, kind, ref)` duplicate lookup (`priorRef`, lines `222-233`) execute and return `this.formatLink(..., principal, true)` (`HTTP 200`) **before** `const visible = this.inspectTarget(kind, ref, principal); if (!visible.row) throw new ConversationDomainError('not_found', 404);` at lines `235-238`.
- **Security & Correctness Impact**:
  1. **Cross-member target ACL bypass on `POST /api/conversations/:id/links`**:
     Per `docs/CONVERSATION-LINKS.md` §3, attaching a link requires **both** the conversation gate (`assertActiveMember`) and the target gate (`canInspectJob` / `canInspectLaunch`, returning opaque `404 not_found` when the caller lacks target read access). Because `priorRef` (`WHERE conversation_id = ? AND kind = ? AND ref = ?`) is not scoped to `created_by` and runs before `inspectTarget()`, once an admin (`human:alice`) or job owner attaches a foreign `routing_job` or admin-only `fleet_launch` to a shared conversation, **any other member of that conversation** (including an agent that does not own the `routing_job`, an agent that is not an admin for `fleet_launch`, or an agent with only `bus:send` and zero routing/fleet scopes) calling `POST /api/conversations/:id/links` with that `(kind, ref)` (even with a brand-new `Idempotency-Key`) receives **`200 OK`** (`replay: true`, `created_by: "human:alice"`) instead of **`404 not_found`**.
  2. **Deleted target / recreated foreign target / scope-downgraded actor bypass on retry**:
     If `projA:worker-1` attaches its own `routing_job` `J` (`201 Created`), and `J` is subsequently deleted from `routing_jobs` or deleted and recreated with the same UUID `J` under a foreign actor (`routing-client:foreign` or `projB:worker-2`), or `projA:worker-1` has its `routing:submit` scope removed from `credentials`, retrying `POST /api/conversations/:id/links` (with the same `idempotency_key` via `priorKey` or without `idempotency_key` via `priorRef`) returns **`200 OK`** (`replay: true`) instead of **`404 not_found`**.
- **Empirical Reproduction** (`.local/review/adversarial.probe.test.ts` :: `Finding 1`):
  - Before `human:alice` attaches `jobForeign = f1000000-0000-4000-8000-000000000001` (owned by `routing-client:foreign`) and `launchAdmin = f1000000-0000-4000-8000-000000000003` to shared conversation `conv.id`, `POST /api/conversations/${conv.id}/links` by `projA:worker-1` returns `404`.
  - After `human:alice` attaches both (`201`), `POST /api/conversations/${conv.id}/links` by `projA:worker-1` (`idempotency_key: "w1-fresh-key"`) for `jobForeign` returns **`200 OK`** (`replay: true`, `created_by: "human:alice"`), and `POST` by `projB:worker-2` (scopes `["bus:send", "bus:read"]` only) for `launchAdmin` returns **`200 OK`**.
  - After `projA:worker-1` attaches its own `jobOwn = f1000000-0000-4000-8000-000000000002` (`idempotency_key: "w1-own-key"`) and `jobOwn` is deleted and recreated with `actor = "routing-client:foreign"`, retrying `POST` with `idempotency_key: "w1-own-key"` returns **`200 OK`** instead of **`404`**.
- **Required Fix**:
  In `ConversationLinksService.attach()` (`src/conversation-links.ts:204-238`), enforce live target existence and caller target visibility (`const visible = this.inspectTarget(kind, ref, principal); if (!visible.row) throw new ConversationDomainError('not_found', 404);`) **before** returning any `priorKey` or `priorRef` replay (while preserving `idempotency_conflict` `409` only when the caller's own `priorKey` has a different `(kind, ref)` and the caller can inspect the target, or checking `inspectTarget` immediately after input validation at line `203` so unauthorized/missing targets always fail closed with `404 not_found`).

---

### Finding F2 (High): Standalone `ConversationsService.init()` Neither Creates `conversation_links` Nor Wires a Default `ConversationLinksService`, Breaking Standalone Routes and Orphaning Child Links on `retentionCleanup()`

- **Location**: `src/conversations.ts:818, 869-991, 1017-1019, 1244-1263, 1404-1408, 3428, 3642, 3697` and `src/conversation-links.ts:168-175`
- **Root Cause**:
  1. `ConversationsService` declares `public linksHandler: ConversationLinksHandler | null = null;` (`src/conversations.ts:818`), and `ConversationsService.init()` (`src/conversations.ts:869-991`) neither executes `CONVERSATION_LINKS_SCHEMA_SQL` nor initializes a default `ConversationLinksService` (nor calls `this.linksHandler?.init()`).
  2. Startup orphan reconciliation in `ConversationsService.init()` (`src/conversations.ts:909-915`) deletes orphaned `conversation_messages` and `conversation_members` rows without a parent `conversations` header, but omits `conversation_links`.
  3. Only the composed `Hub` constructor (`src/conversation-hub.ts:43-51`) wires `this.conversationsService.linksHandler = this.linksService` and calls `this.linksService.init()`.
- **Security, Retention & Correctness Impact**:
  1. **Standalone `ConversationsService` initialization fails the brief invariant** (*"Direct standalone ConversationsService init and composedHub init must both create required tables before cleanup/routes"*): instantiating `const svc = new ConversationsService(sql, txSync); svc.init();` on fresh SQLite storage leaves `conversation_links` uncreated (`sqlite_master` has `0` rows for `conversation_links`) and `svc.linksHandler === null`, causing all `/api/conversations/:id/links` and `/api/conversations/links` routes on `svc.handleFetch()` to fail with `404 not_found` (`src/conversations.ts:3428, 3642`).
  2. **Standalone `ConversationsService.retentionCleanup()` creates permanent orphan rows in `conversation_links`**: when a standalone `ConversationsService` instance runs `retentionCleanup()` on storage containing `conversation_links` rows (as in `test/conversation-retention.test.ts:647, 1295`), `this.linksHandler` is `null`. Step `1c` (`if (this.linksHandler && stepRemaining > 0)` at `src/conversations.ts:1244`) is skipped, and step `1d` (`src/conversations.ts:1263`) deletes the expired archived `conversations` header directly (`conversationsDeleted: 1, linksDeleted: 0`), leaving child `conversation_links` rows permanently orphaned in SQLite without counting them against `rowsDeleted <= limit`.
  3. Even if a caller manually assigns `svc.linksHandler = new ConversationLinksService(sql, svc, txSync)` and calls `svc.init()`, `svc.init()` does not call `this.linksHandler.init()`, so subsequent route or cleanup calls fail with `SqliteError: no such table: conversation_links`.
- **Empirical Reproduction** (`.local/review/adversarial.probe.test.ts` :: `Finding 2`):
  - Calling `standalone = new ConversationsService(sql, txSync); standalone.init();` on storage without `conversation_links` leaves `standalone.linksHandler === null` and `SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'conversation_links'` empty (`0` rows).
  - After creating conversation `conv.id`, attaching a `routing_job` link (`201`), expiring `conv.id`'s `archived_at`, and running `standalone.retentionCleanup(Date.now(), 10)`, `conversationsDeleted` is `1`, `linksDeleted` is `0`, `SELECT COUNT(*) FROM conversations WHERE id = conv.id` is `0`, and `SELECT COUNT(*) FROM conversation_links WHERE conversation_id = conv.id` is **`1` (orphaned)**.
- **Required Fix**:
  1. Wire a default `ConversationLinksService` in `ConversationsService` constructor (or lazily in `init()` when `this.linksHandler === null`) so standalone `ConversationsService` has a live `linksHandler` out of the box.
  2. In `ConversationsService.init()`, always initialize the `conversation_links` schema (`this.linksHandler.init()` or `CONVERSATION_LINKS_SCHEMA_SQL`) inside the same transaction and include `DELETE FROM conversation_links WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_links.conversation_id)` in startup orphan reconciliation (`src/conversations.ts:909-915`).

---

### Finding F3 (High): `deriveJobStatus()` Clobbers `completed-awaiting-review`, `cancelled`, and `accepted` States When `launch_id` Exists, and `kind = 'review'` Reports `complete: true` Without an Accepted Review

- **Location**: `src/conversation-links.ts:43-45, 112-122, 625-673` (`JOB_TERMINAL`, `JOB_HELD`, `jobStatusView`, `deriveJobStatus`, `inspectTarget`)
- **Root Cause**:
  1. `src/conversation-links.ts:43-44` defines:
     ```ts
     const JOB_TERMINAL = new Set(['done', 'failed']);
     const JOB_HELD = new Set(['waiting-on-human', 'stuck']);
     ```
     In `deriveJobStatus(job)` (`src/conversation-links.ts:628-642`), whenever `job.launch_id` is non-null (which holds for every dispatched routing job in `routing_jobs`) and `status` is not in `JOB_TERMINAL` or `JOB_HELD`, `deriveJobStatus` **overwrites** `job.state` with a state derived from `fleet_launches.state` (`launch.state === 'exited' ? 'done' : ...`). Because `'completed-awaiting-review'`, `'cancelled'`, and `'accepted'` are absent from both `JOB_TERMINAL` and `JOB_HELD`, any job in `'completed-awaiting-review'`, `'cancelled'`, or `'accepted'` whose worker process has exited (`launch.state === 'exited'`) has its status overwritten to `'done'`!
     *(Note: the author's test at `test/conversation-links.test.ts:472` seeded `jobAwaitingReview` with `launchId: null`, masking this bug whenever a real worker launch is associated with the job.)*
  2. Even when `job.launch_id` is `null`, `jobStatusView(status)` (`src/conversation-links.ts:112-122`) sets `terminal = JOB_TERMINAL.has(status)` and `complete = status === 'done'`. Consequently:
     - `job.state === 'cancelled'` (with `launch_id: null`) is reported as `terminal: false, complete: false` (instead of `terminal: true, complete: false`).
     - `job.state === 'accepted'` (with `launch_id: null`) is reported as `terminal: false, complete: false` (instead of `terminal: true, complete: true`).
  3. In `inspectTarget(kind, ref, principal)` (`src/conversation-links.ts:668-673`), `kind === 'review'` calls the exact same `jobStatusView(this.deriveJobStatus(job))` as `routing_job` and `outcome`. Thus attaching `kind: 'review'` to any job with `state = 'done'` (or `state = 'completed-awaiting-review'` with an exited launch) returns `complete: true, terminal: true, canonical_status: 'done'`, fabricating review completion from worker exit without an accepted review verdict.
- **Empirical Reproduction** (`.local/review/adversarial.probe.test.ts` :: `Finding 3`):
  - `jobAwaitingWithLaunch` (`state: 'completed-awaiting-review'`, `launch_id` pointing to `launchExited` with `state: 'exited'`): attaching `kind: 'routing_job'` and `kind: 'review'` both return `{ available: true, canonical_status: 'done', terminal: true, complete: true }` instead of `{ available: true, canonical_status: 'completed-awaiting-review', terminal: false, complete: false }`.
  - `jobCancelledWithLaunch` (`state: 'cancelled'`, `launch_id: launchExited`): attaching `kind: 'routing_job'` returns `{ available: true, canonical_status: 'done', terminal: true, complete: true }`; `jobCancelledNoLaunch` (`state: 'cancelled'`, `launch_id: null`) returns `{ available: true, canonical_status: 'cancelled', terminal: false, complete: false }`.
  - `jobAcceptedNoLaunch` (`state: 'accepted'`, `launch_id: null`): attaching `kind: 'review'` returns `{ available: true, canonical_status: 'accepted', terminal: false, complete: false }`; `jobDoneUnreviewed` (`state: 'done'`, `launch_id: null`) with `kind: 'review'` returns `{ available: true, canonical_status: 'done', terminal: true, complete: true }`.
- **Required Fix**:
  1. Only overlay `fleet_launches.state` in `deriveJobStatus(job)` when `job.state` is in the active pre-completion dispatch states (`'queued'`, `'launched'`, `'running'`), preserving explicit canonical states `'completed-awaiting-review'`, `'waiting-on-human'`, `'stuck'`, `'done'`, `'failed'`, `'cancelled'`, and `'accepted'` even when `job.launch_id` points to an `'exited'` or `'failed'` launch.
  2. Treat `'done'`, `'accepted'`, `'failed'`, and `'cancelled'` as `terminal: true` (with `'cancelled'` and `'failed'` having `complete: false`, and `'completed-awaiting-review'` having `terminal: false, complete: false`).
  3. Differentiate `kind === 'review'` in `jobStatusView(status, kind)`: for `kind === 'review'`, set `complete: status === 'accepted'` (and `terminal: status === 'accepted' || status === 'failed' || status === 'cancelled'`) so a job that is merely `'done'` or `'completed-awaiting-review'` never reports `complete: true` for a `review` link, while `routing_job` and `outcome` report `complete: status === 'done' || status === 'accepted'`.

---

### Finding F4 (Medium): `reverse()` Filters Expired-Archived Conversations in JavaScript *After* SQL `LIMIT ?`, Causing Premature Pagination Truncation and Hiding Active Linked Conversations

- **Location**: `src/conversation-links.ts:410-454` (`ConversationLinksService.reverse`)
- **Root Cause**:
  In `ConversationLinksService.reverse()`, the SQL query (`lines 410-434`) joins `conversations c ON c.id = l.conversation_id` and applies `ORDER BY l.created_at DESC, l.id DESC LIMIT ?` (`limit + 1`) **without** filtering out expired-archived conversations in SQL. Afterward, lines `435-442` filter the limited result array in JavaScript (`.filter(row => { const conv = ...; return conv && !this.isExpiredArchived(conv); })`, also performing an redundant per-row `SELECT * FROM conversations WHERE id = ?` query) and then compute `const hasMore = rows.length > limit` (`line 443`).
- **Correctness Impact**:
  Whenever one or more links in the SQL `limit + 1` window belong to expired-archived conversations that have not yet been purged by retention cleanup:
  - Filtering them out after `LIMIT ?` reduces `rows.length` to `<= limit`, causing `hasMore` to evaluate to `false` and `next_cursor` to be `null` even when older active linked conversations exist beyond the window.
  - When `limit` (e.g. `limit = 1`) and the `limit + 1` newest links for `(kind, ref)` are on expired-archived conversations while an older link is on an active conversation, `GET /api/conversations/links?kind=...&ref=...&limit=1` returns `links: [], next_cursor: null`, hiding the active linked conversation completely.
- **Empirical Reproduction** (`.local/review/adversarial.probe.test.ts` :: `Finding 4`):
  - Three conversations (`cActiveOld` active at `2026-10-01T00:00:01.000Z`, `cArch1` and `cArch2` expired-archived at `2026-10-01T00:00:02.000Z` and `2026-10-01T00:00:03.000Z`) link to `jobRef = f4000000-0000-4000-8000-000000000001`.
  - Calling `GET /api/conversations/links?kind=routing_job&ref=f4000000-0000-4000-8000-000000000001&limit=1` as `human:alice` returns `200 OK` with `links: []` and `next_cursor: null`, completely hiding `cActiveOld`.
- **Required Fix**:
  Filter expired-archived conversations directly in the SQL `WHERE` clause of `reverse()` using `archivedCutoffIso = new Date(this.now() - this.conversations.limits.archivedConversationRetentionMs).toISOString()` (`AND (c.archived_at IS NULL OR c.archived_at > ?)`, matching `ConversationsService.listConversations` in `src/conversations.ts:2318`) before `ORDER BY l.created_at DESC, l.id DESC LIMIT ?`, and remove the post-`LIMIT` N+1 query/filter.

---

## 3. Criterion-by-Criterion Audit Summary

| Area | Status on `1a15cb1513db147cd51f970e11edf5ad3df9abf3` | Evidence & Notes |
| --- | --- | --- |
| **Dual-Gate Auth on First Attach (`POST /api/conversations/:id/links`)** | **PARTIAL (`REVISE` — F1)** | First-time attach enforces `verifyPrincipal` (`requireWriteScope: true, isMutatingHttp: true`), `assertActiveMember` (non-member admin receives `403 forbidden`), and `inspectTarget` (foreign/unknown target returns opaque `404 not_found`). However, replay (`priorKey`) and duplicate (`priorRef`) paths return `200 OK` before `inspectTarget()` (**Finding F1**). |
| **Reuse of Existing Routing/Fleet GET Access Semantics** | **PASS (modulo F1)** | `canInspectJob` (`src/conversation-links.ts:583-591`) matches `src/routing.ts:380-404` (`admin` session or `routing:submit` / `bus:send + fleet:launch` agent with `job.actor === principal.actor`). `canInspectLaunch` (`src/conversation-links.ts:593-599`) matches `src/fleet.ts:597-604` (admin human session only). |
| **Live Credential, F7 Binding, Revocation, Expiry & CSRF Checks in `txSync`** | **PASS** | `authenticateConversationRequest` reads body before `txSync`, and `verifyPrincipal` re-reads live `credentials`, `agents`, and `machines` rows inside `txSync` via F7 `credentialActor`. Revoked/expired tokens return `401`; missing CSRF on cookie `POST`/`DELETE` returns `403`; caller-supplied `actor`/`sub` headers or JSON fields cannot override server-verified identity. |
| **Reverse Lookup Privacy (`GET /api/conversations/links?kind&ref`)** | **PARTIAL (`REVISE` — F4)** | `reverse()` checks `inspectTarget` first (`404` if target missing or unauthorized) and joins `conversation_members m` (`m.actor = ? AND m.kind = ? AND m.removed_at IS NULL`), never leaking foreign `conversation_id`s, memberships, prompts, workspaces, or accounts. However, post-`LIMIT` JS filtering of expired-archived conversations truncates pagination (`next_cursor: null`) and hides active linked conversations (**Finding F4**). |
| **Canonical Target Existence, Scalar SQL Safety & Read-Only Attachment** | **PASS** | `kind` is restricted to `routing_job | fleet_launch | review | outcome`; `ref` is validated by lowercase UUIDv4/UUID regex `UUID_RE`; `idempotency_key` is bounded by `SAFE_KEY_RE` (`1..128` chars); all SQL queries use parameterized `?` bindings. Attaching/removing links never inserts or mutates `routing_jobs`, `fleet_launches`, `conversations`, or `conversation_members`. |
| **Live Target Status Derivation & `review`/`outcome` Mapping** | **FAIL (`REVISE` — F3)** | Both `review` and `outcome` map to `routing_jobs.id` (`docs/CONVERSATION-LINKS.md` §2). Deleted targets return tombstone `{ available: false, canonical_status: null, terminal: false, complete: false }`. However, `deriveJobStatus()` clobbers `'completed-awaiting-review'`, `'cancelled'`, and `'accepted'` to `'done'` when `launch_id` has `state = 'exited'`; `'cancelled'` and `'accepted'` lack proper `terminal`/`complete` flags; and `kind = 'review'` reports `complete: true` on unreviewed `'done'` jobs (**Finding F3**). |
| **Per-Conversation & Global Link Caps (`32` / `4,096`)** | **PASS** | `attach()` enforces `maxLinksPerConversation` (`32`, `409 conversation_link_limit_exceeded`) and `maxTotalLinks` (`4,096`, `409 total_link_limit_exceeded`) inside `txSync`. No payload bodies are duplicated in `conversation_links`. |
| **Fair Retention Cleanup (`rowsDeleted <= limit`, `limit = 1`) & Standalone vs. Composed Init** | **PARTIAL (`REVISE` — F2)** | Composed `Hub` (`src/conversation-hub.ts`) initializes both tables and deletes child `conversation_links` in indexed chunks (`deleteChunkForConversation`) within `rowsDeleted <= limit` (verified at `limit = 1`) before deleting expired archived `conversations` headers. However, standalone `ConversationsService.init()` leaves `linksHandler = null` and `conversation_links` uncreated, returning `404` on link routes and leaving orphaned `conversation_links` rows on standalone `retentionCleanup()` (**Finding F2**). |

---

## 4. Independent Verification Runs, Test Attribution & Resource Peaks

### Test Attribution (No Inflated Counts)

- **Author baseline suites verified on `1a15cb1513db147cd51f970e11edf5ad3df9abf3`**:
  1. `tsc --noEmit`: TypeScript typecheck (`0` errors).
  2. `test/conversation-links.test.ts` (`-c test/vitest.conversation-links.config.ts`): `8` real-Worker Miniflare SQLite HTTP tests authored in M4.
  3. `test/conversation-platform.test.ts` (`-c test/vitest.conversation-platform.config.ts`): `2` existing conversation platform integration tests.
  4. `test/conversation-retention.test.ts` (`-t "mixed 50 messages"`): `1` existing F14 mixed-budget fair retention test.
  5. `test/conversation-integration.test.ts` (`-t "preserves Hub export name"`): `1` existing Durable Object `Hub` export and composition test.
- **Independent reviewer adversarial suite (`.local/review/adversarial.probe.test.ts`, `-c .local/review/vitest.adversarial.config.ts`)**:
  - `5` real-Worker Miniflare SQLite HTTP/storage probe tests executed with `--configLoader runner` (preventing Vite from writing `.vite-temp` through the shared `node_modules` symlink) and `cacheDir: '.local/tmp/vite-cache'`, reproducing Findings `F1`–`F4` and independently verifying `limit = 1` retention fairness, mid-flight credential revocation, and reverse-lookup cursor privacy.

### Per-Run Resource & Cgroup Peaks

All runs were executed serially under `umask 077`, `TMPDIR=.local/tmp` (owner-relative under the review workspace), `taskset -c 0`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, and measured by `.local/review/measure_run.py` (logged in `.local/review/runs.jsonl`).

| Run Label | Command | Exit | Tests (Pass / Fail) | Duration (s) | Scratch Peak Alloc (B) | Scratch Peak Apparent (B) | Peak Inodes | Cgroup `memory.current` Peak (B) | Cgroup `memory.peak` (B) | Cgroup `pids` (cur / peak) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `typecheck` | `./node_modules/.bin/tsc --noEmit` | `0` | n/a | `1.214` | `483,328` | `476,880` | `4` | `418,680,832` | `418,680,832` | `31 / 31` |
| `conversation-links` | `./node_modules/.bin/vitest run -c test/vitest.conversation-links.config.ts` | `0` | `8 / 0` | `4.776` | `5,246,976` | `5,221,747` | `40` | `651,915,264` | `652,644,352` | `54 / 54` |
| `conversation-platform` | `./node_modules/.bin/vitest run -c test/vitest.conversation-platform.config.ts` | `0` | `2 / 0` | `4.073` | `1,101,824` | `1,083,073` | `19` | `633,724,928` | `652,644,352` | `54 / 56` |
| `f14-mixed-budget` | `./node_modules/.bin/vitest run test/conversation-retention.test.ts -t "mixed 50 messages"` | `0` | `1 / 0` | `3.918` | `1,044,480` | `1,026,037` | `19` | `647,020,544` | `652,644,352` | `54 / 56` |
| `hub-export` | `./node_modules/.bin/vitest run test/conversation-integration.test.ts -t "preserves Hub export name"` | `0` | `1 / 0` | `3.659` | `1,044,480` | `1,026,686` | `19` | `653,180,928` | `653,185,024` | `55 / 56` |
| `adversarial-probe` (initial import resolution check) | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/vitest.adversarial.config.ts` | `1` | `0 / 0` (import path fix needed) | `3.987` | `634,880` | `615,060` | `18` | `655,888,384` | `659,296,256` | `52 / 56` |
| `adversarial-probe-run2` (`root: process.cwd()` check) | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/vitest.adversarial.config.ts` | `1` | `0 / 0` (`src/auth/session` path fix) | `3.837` | `651,264` | `628,139` | `22` | `664,543,232` | `666,894,336` | `52 / 56` |
| `adversarial-probe-pass` (`.ts` suffix check) | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/vitest.adversarial.config.ts` | `1` | `0 / 0` (`src/auth/session` path fix) | `3.839` | `655,360` | `628,818` | `22` | `671,076,352` | `672,645,120` | `52 / 56` |
| `adversarial-probe-alias` (`@src` alias check) | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/vitest.adversarial.config.ts` | `1` | `0 / 0` (`src/auth/session` path fix) | `5.915` | `655,360` | `629,690` | `22` | `674,009,088` | `679,510,016` | `52 / 56` |
| `adversarial-probe-verified` (discovered `Hub` pre-init in `runInDurableObject` & `conversationsService` prop) | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/vitest.adversarial.config.ts` | `1` | `3 / 2` | `4.703` | `3,764,224` | `3,733,375` | `37` | `692,707,328` | `692,854,784` | `52 / 56` |
| `adversarial-probe-all5` (final retained 5-test probe) | `./node_modules/.bin/vitest run --configLoader runner -c .local/review/vitest.adversarial.config.ts` | `0` | `5 / 0` | `4.547` | `4,018,176` | `3,989,627` | `37` | `693,379,072` | `693,784,576` | `52 / 56` |

- **Overall scratch peak across all runs** (`.local/review` + `.local/tmp` including directory inodes): `5,246,976` bytes allocated (`5.00 MiB`), `5,221,747` bytes apparent (`4.98 MiB`), `40` inodes — well within the `18 MiB` early guard and `25 MiB` hard cap.

---

## 5. Retained Owner-Relative Evidence Locators

- **Review report (tracked)**: `docs/reviews/CONVERSATION-LINKS.md`
- **Retained adversarial Vitest probe & config (gitignored under review workspace)**:
  - `.local/review/adversarial.probe.test.ts`
  - `.local/review/vitest.adversarial.config.ts`
  - `.local/review/adversarial-evidence.json`
  - `.local/review/measure_run.py`
  - `.local/review/runs.jsonl`
- **Head composition receipts (gitignored under coordination workspace)**:
  - `.local/platform/m4-composition-evidence.json`
  - `.local/platform/m4-composed-source-manifest.json`
