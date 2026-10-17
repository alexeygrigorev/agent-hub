# Post-Main Argon Deep Review (`GATE-CONV-REVIEW`): Integrated Conversation Platform & Portable Host Storage on `main` (`7929a1bc` → `16d66d69`)

- **Gate**: `GATE-CONV-REVIEW` (mandatory post-`main`-integration Argon deep review of integrated conversation direction, safety invariants, and roadmap readiness)
- **Reviewed published `main` commits**:
  - Initial post-land CFB integration pin: `7929a1bc07bfea3a2b387989800cf1b0ba140984` (`feat: add human-agent conversations, pairing and dakit UI [skip ci]`)
  - Current fast-forwarded published `main` pin (`C-POSTLAND-16`): `16d66d69708268ab84089447534f0439e0bfd893` (`docs(windows): publish corrected factual audit of 3888a439 [skip ci]`, integrating Windows/portable host source commit `3888a439f9e32d5a075a7e3d23b08ae9aae8201d`)
- **Pre-conversation `main` baseline**: `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`
- **Reviewer**: `platform-postland-review` / `gemini-4-argon-high` (session `4d1c2347-80d5-4c23-acfb-996868462c01`, engine `antigravity`, isolated worktree `/home/alexey/git/agent-hub-wt-platform-postland-review`)
- **Coordinator / Head**: `platform` (`e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363`, `/home/alexey/git/agent-coordination`)
- **Verdict**: **SCOPED ACCEPT** (`Review: approved sha=16d66d69708268ab84089447534f0439e0bfd893 reviewer=platform-postland-review/gemini-4-argon-high`, also explicitly approving initial post-land pin `7929a1bc07bfea3a2b387989800cf1b0ba140984`)

Review: approved sha=16d66d69708268ab84089447534f0439e0bfd893 reviewer=platform-postland-review/gemini-4-argon-high

---

## 1. Executive Summary & Scope Boundaries

1. **Verdict (`SCOPED ACCEPT`)**: Published Agent Hub `main` at `7929a1bc07bfea3a2b387989800cf1b0ba140984` and its fast-forward extension `16d66d69708268ab84089447534f0439e0bfd893` (`C-POSTLAND-16`) faithfully integrate the exact reviewed Conversation Final Bundle (`CFB` `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`, 199-file source manifest `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a`) and the exact reviewed Windows/POSIX private host storage delta (`3888a439f9e32d5a075a7e3d23b08ae9aae8201d`, 203-file source manifest `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804`).
2. **Core Safety Invariants Hold on Published `main`**:
   - Human (`human:<sub_id>`) and agent (`<project>:<tag>` or legacy projectless `<tag>`) namespaces are disjoint and injective; collapsed `admin`/`root`/`principal`/`TOKEN_ADMIN_EMAIL` identities, `host`/`job`/`routing:submit`-only tokens, and sender-override payload fields are deterministically rejected.
   - Multi-project `F7` registry uniqueness (`(project, tag)` plus partial unique index on projectless `tag`) prevents cross-project collision; bare `DELETE /api/agents/<tag>` refuses ambiguous multi-project tags with `409 agent_tag_ambiguous`; qualified `DELETE /api/agents/<project>%3A<tag>` immediately revokes only that project's agent credentials and denies both active bearer calls (`401`) and refresh attempts (`400`).
   - Public device pairing enforces explicit `machine_id` binding (`null` or registered machine), rejects concurrent registration conflicts (`409 agent_machine_conflict`) and bare-tag canonical identity shifts (`400 invalid_approval`), and immediately invalidates previously issued `machine_id: null` tokens when an agent registration transitions from `NULL` to a bound machine (`registerAgentPairing` + `revokeUnboundAgentCredentials`).
   - Conversation membership, owner-only mutations, last-owner protection (`409 cannot_remove_last_owner`), immediate `403 member_removed` revocation (including for `created_by`), non-blocking tombstone cap eviction (`F10`), join-at-current-watermark (`last_read_seq = currentLastSeq`), retained-row pagination (`F11`), parent-pruned reply idempotency (`F12`), and single-transaction fair retention (`ConversationHub.runRetentionCleanup`) all match their reviewed contracts.
   - The official 21-file `public/vendor/dakit` lock (`v0.2.0`, commit `0e283cd`) is intact with zero raw hex/rgb/hsl palette colors across all 8 fleet/auth/conversations UI files, and `public/conversations.js` isolates drafts/outboxes per actor via injective `conv-outbox:v2:["actor","id"]` tuples and monotonic `authGeneration`/`routeGeneration` guards.
   - The foreground native conversation bridge (`host/agent_hub_host/conversation_bridge.py`) is held by default (`run_cycles(..., enabled=False)` raises `BridgeCapabilityGap(HOLD_REASON)`), enforces 7-tuple journal identity `[origin, actor, project, workspace, machine, session_id, tag]`, re-verifies live membership (`member(cid)`) before every native send and hub reply/read, persists uncertainty reservations (`attempted: True`) before `native.send` so uncertain sends are reconciled from `aplexer message log` and never blindly repeated, and proves missing-range emptiness via ascending `after_seq` pages before recording local `P3` gap recovery (`server_read_advanced: False`).
   - On `16d66d69708268ab84089447534f0439e0bfd893`, `host/agent_hub_host/private_fs.py` and `host/agent_hub_host/win32_fs.py` replace Unix-only top-level `fcntl` imports and route host config, bridge receipts, native session reads, and conversation bridge journals through descriptor-pinned no-follow POSIX operations and Win32/NTFS handle-pinned DACL contracts without altering any of the 193 non-Windows CFB files.
3. **Strict Gate Separation (What This Verdict Does *Not* Claim)**:
   - Source acceptance, `main` integration, live Cloudflare/Cognito rollout, and physical cross-host adoption are separate gates.
   - This report does **not** claim full `M1–M11` completion, live deployment to `hub.dtcdev.click`, physical Windows (`Win35`) DLL/NTFS execution (`M3b` physical / `M11`), `M5` scheduled follow-up completion, or `M8a`/`M8b` live multi-account/cross-host routing completion.

---

## 2. Exact Git Pins, Digests, & Changed-Path Verification

### 2.1 Canonical Commit & Artifact Table

| Artifact / Commit Role | Full Commit SHA / SHA-256 Digest | Verified Status |
| :--- | :--- | :--- |
| Pre-conversation `main` baseline | `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` | `131` tracked non-artifact files; shared `/home/alexey/git/agent-hub` checkout remains pinned here untouched |
| Initial post-land CFB `main` commit | `7929a1bc07bfea3a2b387989800cf1b0ba140984` | `202` tracked files (`199` CFB source files + `3` conversation proposal/review docs) |
| Current fast-forwarded `main` commit (`C-POSTLAND-16`) | `16d66d69708268ab84089447534f0439e0bfd893` | `209` tracked files (`203` composed source files + `6` conversation/Windows proposal/review docs) |
| Windows source integration commit | `3888a439f9e32d5a075a7e3d23b08ae9aae8201d` | Direct parent `a42ea0fd6daeb04a1bfc4de73478f557dcf02171` on `7929a1bc07bfea3a2b387989800cf1b0ba140984`; applies exact Windows patch `bf2f3df7...` |
| CFB 199-file source manifest SHA-256 | `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a` | Verified 199/199 exact match on `7929a1bc07bfea3a2b387989800cf1b0ba140984` (`.local/review/manifest-verification.json`) |
| Composed 203-file source manifest SHA-256 | `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804` | Verified 203/203 exact match on `16d66d69708268ab84089447534f0439e0bfd893` (`.local/review/manifest-verification-16d66d69.json`) |
| `docs/proposals/CONVERSATION-INTEGRATION.patch` | `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10` | `1,366,991` bytes; `88` changed/added source paths on `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` |
| `docs/proposals/CONVERSATION-INTEGRATION.md` | `85a865edcec924d3b3038ad9240a3e5ed3b8a4dc154773a814af47d6f2fc3d32` | `36,794` bytes; frozen in `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` (direct Git parent `f9f6fb258e5abdf4d97e9292043828b4cc80ac82`) |
| Pre-main CFB Argon review commit | `5a4b9381d4863d10a1d46b249c0be83e17cee18f` | Original `docs/reviews/CONVERSATION-INTEGRATION.md` SHA-256 `e7627dfed6d2263a47a0740d50d5baacffa7601dbd66c5d57acdae559d09ada6`; published copy `d844c8581eab7592607c455f1dd42cafae757b0304fed0ccd2359fec0af7ae3b` (`34,875` bytes) adds explicit metadata note correcting `cfb4dc2` direct Git parent `f9f6fb258e5abdf4d97e9292043828b4cc80ac82` vs source base `5dcdf71` and author runtime `GPT-6` (original review commit immutable; report's raw KiB/binary units are valid) |
| Component inputs in CFB | `8dc9235ce246ff8316048fa837b34b53ae7a0cdd` (API R3), `ddaf80d82022736da82ff11d521c6a9f92ff593e` (F7 R2), `eee4d2f88305a6e64545c8e7b68f329a162811d1` (Pairing R2), `d80dfebd806ae320af699ab149b6c183e376ed07` (UI/M2b), `0896ebe157cc732342b17fb40a6cf6e096ef93a2` (P3/CLI) | Composed into `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` on `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` (which already contained M6 source) |
| `docs/proposals/WINDOWS-INTEGRATION.patch` | `bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500` | `51,378` bytes; identical to standalone Windows proposal `2de0a6c1846b110227cd1d15e99a7af6999158f5` (scoped accept `3d0dfadbed35812aeaacbb98f304acfecf711e91`) |
| `docs/proposals/WINDOWS-INTEGRATION.md` | `1b685be09932a00bea329e916cefbbbe30bf1e5e14ded6a90f9b33ee3ef25be4` | `17,215` bytes (`a42ea0fd6daeb04a1bfc4de73478f557dcf02171`) |
| Corrective Windows factual/source audit | `e5133fc9cf5e71a29fbfb900aaa3b0455984f8e5` | Original `docs/reviews/WINDOWS-INTEGRATION.md` SHA-256 `b0f26919dbe6dcb9897c561ab5e2f7c7e2979af797d0ed471e58bccb55df5c03` by `platform-win-factual-audit/grok-4.6`; published copy `86d50cceb165acd5a980f05f90bf05a14b42bd5e761e28865808a47f0a4788ac` (`26,486` bytes) adds head annotations normalizing owner-relative paths, `0da55c0` 134-file count vs `d79fab0` 133-file count, and final auditor scratch peak; supersedes defective narrative in `0da55c0789c3bdfa389a7253c250e0676bfd7da8` (`1ca7c9a2d490153c6c79f9371b95a95db3f352a6bf98fb6e7729dfd59441f0d7`) |

### 2.2 Changed-Path Breakdown Across `5dcdf71` → `7929a1b` → `16d66d6`

1. **`5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` → `7929a1bc07bfea3a2b387989800cf1b0ba140984` (`88` source paths + `3` doc artifacts = `91` git paths)**:
   - **Worker & Durable Object (`10` paths)**: `src/agent_registry.ts`, `src/bus.ts`, `src/claim_admission.ts`, `src/conversation-hub.ts`, `src/conversations.ts`, `src/hub.ts`, `src/jobs.ts`, `src/routing.ts`, `src/ui-pages.ts`, `src/worker.ts`.
   - **Public UI & Official Dakit 21-File Lock (`35` paths)**: `public/auth.css`, `public/auth.js`, `public/conversations.css`, `public/conversations.html`, `public/conversations.js`, `public/dashboard.js`, `public/fleet.css`, `public/fleet.html`, `public/fleet.js`, `public/index.html`, `public/styles.css`, `public/tokens.css`, `public/ui-kit.css`, plus `public/vendor/dakit/dakit.lock.json` and the `21` locked `public/vendor/dakit/**` files.
   - **Host Python CLI & Native Bridge (`7` paths)**: `host/agent_hub_host/cli.py`, `host/agent_hub_host/conversation_bridge.py`, `host/agent_hub_host/conversations.py`, `host/tests/test_cli.py`, `host/tests/test_conversation_bridge.py`, `host/tests/test_conversation_bridge_gap.py`, `host/tests/test_conversations.py`.
   - **Worker/UI/Pairing Tests & Docs (`36` source paths + `3` proposal/review docs)**: `docs/CONVERSATIONS.md`, `docs/SPEC.md`, `package.json`, `tsconfig.json`, `vitest.config.ts`, `wrangler.toml`, `test/**` (`30` files), plus `docs/proposals/CONVERSATION-INTEGRATION.md`, `docs/proposals/CONVERSATION-INTEGRATION.patch`, and `docs/reviews/CONVERSATION-INTEGRATION.md`.
2. **`7929a1bc07bfea3a2b387989800cf1b0ba140984` → `16d66d69708268ab84089447534f0439e0bfd893` (`10` host source/test paths + `3` Windows proposal/review docs = `13` git paths; all `193` other CFB source paths byte-identical)**:
   - `host/agent_hub_host/bridge.py` (`2b5e775d...` → `033db995...`)
   - `host/agent_hub_host/config.py` (`2ee1abe6...` → `6cce08e8...`)
   - `host/agent_hub_host/conversation_bridge.py` (`7e7ebaec...` → `747e3cf7...`)
   - `host/agent_hub_host/native_bridge.py` (`b8f1f4db...` → `528dab70...`)
   - `host/agent_hub_host/private_fs.py` (new, `ef4b6436bb74c08b2e4e367f82bf5a02bd1db9e3d04f416b480b46f8c8843a18`)
   - `host/agent_hub_host/win32_fs.py` (new, `34589f69dc0f11cabb79fd08afaf0c4a48b3a7e6ee2c2bf549b7066ade99c302`)
   - `host/tests/test_bridge.py` (`783d38ef...` → `dcb6dd44...`)
   - `host/tests/test_config.py` (`7d6fe1be...` → `761b3507...`)
   - `host/tests/test_private_fs.py` (new, `bd56b0cf54df9a92a39430754e6bc15e6a1f7fc1e5cac580bd94dad3529cc1bd`)
   - `host/tests/test_windows_imports.py` (new, `8a7d5bb02994c0558c13843f62823eb9d89c0e3eac5431e138c3e9291b06c52b`)
   - `docs/proposals/WINDOWS-INTEGRATION.md`, `docs/proposals/WINDOWS-INTEGRATION.patch`, `docs/reviews/WINDOWS-INTEGRATION.md`.
3. **Integration Custody & Shared Checkout Preservation**:
   - Verified from sanitized receipts `/home/alexey/git/agent-coordination/.local/platform/{hub-recovery-evidence,hub-canonical-sync-evidence,hub-shared-checkout-preservation-after,isolated-integration-scope-context}.json` and direct read-only inspection of `/home/alexey/git/agent-hub`:
     - `/home/alexey/git/agent-hub` remains at `HEAD` `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` with all `131` tracked files unchanged (`context_edit_overlaps: 0`).
     - Peer `hub-zai` (`7a2f9ac0`) in `/home/alexey/git/agent-hub` retains exclusive custody of its live environment/deploy/domain/enrollment scope; integrating user-authorized disjoint worktree commits to `origin/main` did not touch `/home/alexey/git/agent-hub` or infer/require a peer ACK across disjoint physical checkouts.
     - Agent Branches `task-0418` (`platform-conv-final-0418`) and `task-0420` (`platform-win-merge-0420`) remain under head custody; optional Agent Branches `tasks create` `500` during `7929a1b` integration was not retried and no new task ID was fabricated; canonical Agent Branches and private recovery clone both verified `git fsck --full` (`0` errors), `9` blob checks, `199`/`203` source SHA checks, and CLI `--help` checks.

---

## 3. Deep Source & Security Audit of Integrated Conversation Platform

### 3.1 Human & Agent Principal Disjointness and Live Credential Revalidation

- **Injective Human Encoding (`src/conversations.ts:308-337`, `1394-1512`)**:
  - `formatHumanActorFromSub` requires `HUMAN_SUB_REGEX` (`/^[A-Za-z0-9._:-]{1,120}$/`), never trims whitespace or strips an existing `human:` prefix, and refuses bare or prefixed `admin`, `root`, and `principal` (`403 collapsed_admin_identity_refused`).
  - `verifyPrincipal` requires human principals to originate from a verified admin session (`session.isAdmin === true`, `session.exp > now`), re-checks `users.is_admin` in SQLite when a user row exists, rejects any bearer token claiming a `human:` actor or `human`/`session` kind (`403 invalid_principal_kind`), and enforces `X-CSRF-Token` verification (`403 csrf_invalid`) on all mutating HTTP requests before reading the request body.
- **Disjoint Agent Identity & F7 Multi-Project Registry (`src/agent_registry.ts:28-210`, `369-440`, `src/conversations.ts:354-565`, `1514-1676`)**:
  - `migrateAgentRegistry` enforces `UNIQUE(project, tag)` plus partial index `idx_agents_projectless_tag ON agents(tag) WHERE project IS NULL` and `idx_credentials_registry_actor ON credentials(registry_actor)`.
  - `registryAddress` rejects reserved prefixes/namespaces (`human`, `role`, `machine`, `routing-client`, `admin`, `root`, `principal`, `system`, `hub`, `schedule`).
  - `createSqlIdentityValidator` canonicalizes project-qualified rows strictly as `${project}:${tag}` and projectless rows as `${tag}`, excluding `role = 'root'`.
  - `verifyPrincipal` re-queries `credentials` by `tokenId`/`tokenHash` inside the SQLite transaction, rejecting revoked or expired tokens (`401 unauthorized`) and resolving the live `agents` row via `credentialActor(this.sql, liveCred)` so deleting an agent via `DELETE /api/agents/:actor` immediately revokes its credentials (`401`) and denies refresh (`400 invalid_grant`).

### 3.2 Public Machine Pairing, Approval Races, & Token Refresh (`eee4d2f`)

- **Approval Race & Canonical Shift Defenses (`src/agent_registry.ts:318-365`, `src/hub.ts:694-750`, `src/ui-pages.ts:195-295`, `public/auth.js:1-95`)**:
  - Device review (`/device?code=...`) displays the canonical project/tag (`Project: <project> / Tag: <tag>` or `Project: (projectless) / Tag: <tag>`) and submits `expected_actor` plus the explicit selected `machine_id` (`string` or `null`).
  - If a bare-tag challenge's canonical identity changes before approval (e.g., `bare-ui` becomes `pair-local:bare-ui` via concurrent registration), approval fails closed with `400 invalid_approval`.
  - If an agent row already has a non-null `machine_id` (or acquires one concurrently while the review page is open) and the approval submits `machine_id: null` or a different machine, `registerAgentPairing` fails closed with `409 agent_machine_conflict`.
  - When an agent row's `machine_id` transitions from `NULL` to a bound `machine_id` during approval, `registerAgentPairing` updates `agents.machine_id`, sets `imported_from = 'pairing'`, and calls `revokeUnboundAgentCredentials` so any previously issued `machine_id: null` access/refresh tokens for that actor are immediately invalidated (`401` on API use, `400 invalid_grant` on `/api/token/refresh`).

### 3.3 Membership Lifecycle, Tombstone Cap Eviction (`F10`), Watermarks, & Fair Retention (`F11`/`F12`)

- **Membership & Tombstone Eviction (`src/conversations.ts:1819-1850`, `2314-2673`)**:
  - `addMember`, `removeMember`, and `setArchived` require active `owner` membership (`403 owner_required`); demoting or removing the last active owner is refused (`409 cannot_remove_last_owner`).
  - Removed members receive `403 member_removed` on all conversation reads/writes, including when `conversation.created_by === actor` even after tombstone eviction.
  - When per-conversation (`128`) or global (`2,048`) removed-member tombstone caps are reached, `removeMember` (`F10`, lines `2568-2646`) evicts the oldest removed-member tombstone(s) (preferring non-creator tombstones first: `ORDER BY CASE WHEN actor = created_by THEN 1 ELSE 0 END ASC, removed_at ASC`) so member revocation never fails due to storage pressure.
  - Both new member joins and re-added members initialize `last_read_seq = Math.max(prevReadSeq, currentLastSeq)` (lines `2424-2479`), preventing absent-period messages from appearing as unread while keeping retained history readable.
- **Pruned History, Interior Gaps, & Composed Retention (`src/conversations.ts:1052-1342`, `1688-1800`, `2784-2849`, `3119-3185`, `src/conversation-hub.ts:255-277`)**:
  - `listMessages` derives `has_more_before` and `has_more_after` strictly from retained rows (`F11`) and computes `pruned_through_seq` (contiguous prefix watermark) plus `gap_detected` / `gap_from_seq` / `gap_to_seq` via `getFirstMissingSeqRange` (covering prefix, interior, and suffix holes).
  - `sendMessage` checks retained idempotency (`UNIQUE(conversation_id, sender_actor, idempotency_key)`) *before* resolving `reply_to` (`F12`), allowing idempotent replay of a retained reply even after its parent message has expired.
  - `ConversationHub.runRetentionCleanup` wraps `super.runRetentionCleanup` and `conversations.runRetentionCleanup` inside a single `ctx.storage.transactionSync` transaction (`8dc9235`) and maintains exact `conversation_storage_usage` counters.

### 3.4 UI Account-Switch Races & Official Dakit 21-File Lock (`d80dfeb`)

- **Official `dakit` Lock (`public/vendor/dakit/dakit.lock.json`)**:
  - All `21` files in `dakit.lock.json` (`version: 0.2.0`, `commit: 0e283cd`) match their exact SHA-256 digests (`U1_dakit_21_file_lock_and_zero_raw_palette_css`).
  - Official `checkColors`, `checkBorders`, and `checkScale` pass with `0` violations across `public/{fleet.css,fleet.html,auth.css,auth.js,fleet.js,conversations.css,conversations.html,conversations.js}`.
- **Account-Switch & Outbox Isolation (`public/conversations.js:150-245`, `557-601`, `1024-1085`)**:
  - Outbox storage keys use `conv-outbox:v2:` + `JSON.stringify([actor, id])`, which is injective across arbitrary colons/quotes in actor or conversation IDs.
  - `resetAccountState` increments `state.authGeneration` and `state.routeGeneration`, clears in-memory threads/details/participants, and calls `purgeOutboxes(nextActor, keepScopedDisk)` so legacy unscoped keys and foreign-actor outboxes are purged on account switch or `401`/sign-out, and stale in-flight `sendOutbox` completions from an older `authGeneration` never re-persist drafts.

### 3.5 Host CLI, Foreground Bridge (`P3`), & Portable Private Storage (`0896ebe`, `3888a439` / `16d66d69`)

- **Foreground Bridge & `P3` Gap Recovery (`host/agent_hub_host/conversation_bridge.py:61-474`)**:
  - `run_cycles` defaults to `enabled=False` and raises `BridgeCapabilityGap(HOLD_REASON)`.
  - `qualify()` verifies live `aplexer --json whoami` and `aplexer --json list` against `qualify_native_service_binding` and `/api/conversations/participants` (`actor == project + ':' + binding.tag`, `row.machine_id == self.machine`, `metadata.machine_id == self.machine`, and non-contradictory `workspace`/`session_id` hints) without creating or locking the journal file.
  - `deliver()` records durable intent (`attempted: False, native_id: None`), then sets `attempted: True` and fsyncs the journal *before* invoking `aplexer message send --workspace <ws> --to <tag> --kind hub-conversation`, and reconciles delivery from `aplexer --json message log` so uncertain native sends are never blindly repeated.
  - `resume(cid, target, acknowledge_gap=True)` requires an existing journal (`create=False`), zero unresolved delivery/reply intents, and two ascending `empty_range_page(cid, cursor, target)` checks bracketed by `member(cid)` revalidations before atomically appending a `gaps` audit record and advancing `state['cursors'][cid] = target` with `server_read_advanced: False`.
- **Portable POSIX/Win32 Private Storage (`host/agent_hub_host/private_fs.py`, `host/agent_hub_host/win32_fs.py`)**:
  - POSIX walks from `path.anchor` with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, verifies `_private` (`st_uid == os.getuid()`, `st_mode & 0o077 == 0`, `st_nlink == 1` for files), stages `atomic_write` via exclusive `*.<32hex>.tmp` + `fsync` + `os.replace(..., src_dir_fd=anchor, dst_dir_fd=anchor)` + directory `fsync`, and locks via `fcntl.flock(LOCK_EX | LOCK_NB)` without unlinking the lock inode.
  - Windows (`win32_fs.py`) imports cleanly on POSIX without loading DLLs until `api()` is called; validates local fixed NTFS drive paths (`GetDriveTypeW == 3`, `GetVolumeInformationW == 'NTFS'`, rejecting UNC, `\\?\`, ADS `:`, `..`, reserved device names, and forward slashes); pins ancestor directories with `CreateFileW` share mode `3` (`FILE_SHARE_READ | FILE_SHARE_WRITE`, no delete) and `FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS`; applies protected owner-only SDDL `O:{sid}D:P(A;{OICI};FA;;;{sid})` at creation; verifies `GetSecurityInfo` + `SE_DACL_PROTECTED` (`0x1000`) + `acl.count == 1` + `ace.mask == 0x1f01ff` + `EqualSid`; replaces via `MoveFileExW(..., 1 | 8)`; and locks via `msvcrt.locking(fd, LK_NBLCK, 1)`.

---

## 4. Findings, Severity, Locations, Reproductions, & Fixes

No blocking (`P0`/`P1`/`P2`) source defect exists on published `main` (`7929a1bc07bfea3a2b387989800cf1b0ba140984` or `16d66d69708268ab84089447534f0439e0bfd893`). Two non-blocking (`Low / P3`) host/operational items were identified and verified during adversarial probing:

### Finding `F-POST-1` (Severity: Low / P3, Non-Blocking Host CLI Config Lifecycle Edge Case)

- **Summary**: `host/agent_hub_host/api.py` and `host/agent_hub_host/config.py` only record and synchronize `machine_id` when `/api/device/token` returns a non-null `machine_id`, leaving `host.json` `machine_id` stale across (a) a subsequent `machine_id: null` login over an existing `host.json` and (b) `/api/token/refresh` after a hub-side `NULL -> machine_id` binding update.
- **Exact Locations**:
  - `host/agent_hub_host/api.py:74-80` (`safe_http_json` checks `getattr(req, "full_url", "").rstrip("/").endswith("/api/device/token")` and `if "machine_id" in data and data["machine_id"] is not None:`)
  - `host/agent_hub_host/config.py:59-69` (`record_authenticated_machine_binding` returns early on `if machine_id is None: return` without clearing or recording an explicit `None` binding) and `host/agent_hub_host/config.py:119-152` (`update_config` preserves prior `cfg["machine_id"]` when `machine_id` is absent from `updates` and `_pending_machine_binding`)
  - `host/agent_hub_host/auth.py:84-91, 100-118` (`device_login` and `refresh_token` call `config.update_config(...)` without passing `machine_id`)
- **Impact & Why Non-Blocking for `GATE-CONV-REVIEW`**:
  - Does **not** bypass bridge security: `ConversationBridge.qualify()` (`host/agent_hub_host/conversation_bridge.py:127-130`) and `resume_from_args()` (`line 474`) cross-check the local `machine_id` against `/api/conversations/participants` (`row.get('machine_id') == self.machine`) and fail closed (`BridgeCapabilityGap`) on any mismatch.
  - However, if an operator re-runs `hub login` for an unbound (`machine_id: null`) agent on a host whose `host.json` already had `machine_id: "old-machine"`, `hub login` exits `0` and leaves `"old-machine"` in `host.json`; and if an initially `null`-paired agent is later bound to a machine on the hub, `auth.refresh_token()` receives the new `machine_id` from `/api/token/refresh` but does not persist it to `host.json` until a fresh `hub login` is run.
- **Reproduction**:
  ```bash
  umask 077 && TMPDIR=/home/alexey/git/agent-hub-wt-platform-postland-review/.local/tmp \
    UV_THREADPOOL_SIZE=1 NODE_OPTIONS="--v8-pool-size=1" GOMAXPROCS=1 \
    taskset -c 0 python3 .local/review/postland-host-probe.py
  # See PASS H3_cli_config_machine_id_null_relogin_and_refresh_sync_gap_verified
  ```
- **Recommended Fix (Follow-Up Host CLI Polish)**:
  - In `host/agent_hub_host/api.py:74`, match both `/api/device/token` and `/api/token/refresh` when `"machine_id" in data`, and pass `data["machine_id"]` (including `None`) to `config.record_authenticated_machine_binding`.
  - In `host/agent_hub_host/config.py`, record explicit `None` bindings (`_pending_machine_binding["has_binding"] = True`, `_pending_machine_binding["machine_id"] = None`) so `update_config` either rejects or clears a conflicting preexisting `cfg["machine_id"]` on `machine_id: null` agent login and persists newly bound `machine_id` values on `/api/token/refresh`.

### Finding `F-POST-2` (Severity: Low / P3, Non-Blocking Admin Rebind Ergonomics Note)

- **Summary**: `POST /api/agents` (`src/hub.ts:539`, `src/agent_registry.ts:369-393`) calls `registerRegisteredAgent(..., allowExisting = false)` (`409 agent_already_registered`), while `DELETE /api/agents/:actor` (`src/agent_registry.ts:413-436`) deletes the agent row and revokes all credentials for that actor.
- **Impact**: Binding an existing `machine_id: null` agent to a machine in place works via `/api/device/approve/:code` (`src/agent_registry.ts:350-358`), which also revokes prior unbound credentials (`revokeUnboundAgentCredentials`). Moving an already machine-bound agent to a *different* machine (or updating its `workspace`/`session_id` registration hints) intentionally requires `DELETE /api/agents/:actor` + `POST /api/agents` + re-pairing. Documenting this lifecycle in the operator runbook (or adding an explicit admin rebind endpoint that rotates credentials) will prevent operator confusion during `M8b`/`M11` host migration.

---

## 5. Executed Commands, Retained Pre-Main Provenance, & Resource Measurements

### 5.1 Commands Executed by This Reviewer (`platform-postland-review` / `4d1c2347`)

All commands ran serially on CPU 0 (`taskset -c 0`) with `umask 077`, `TMPDIR=/home/alexey/git/agent-hub-wt-platform-postland-review/.local/tmp`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS="--v8-pool-size=1"`, `GOMAXPROCS=1`, and zero dependency installs or copies. No `--enable-native` bridge cycles, bridge journals, or native inbox envelopes were created.

| Pin | Command / Probe | Result / Counts | Retained Evidence Path |
| :--- | :--- | :--- | :--- |
| `7929a1bc` | `199`-file CFB manifest + `88`-path diff + shared `/home/alexey/git/agent-hub` `131`-file preservation check | `PASS` (`199/199` match `17aab235...`; `131/131` shared checkout unchanged) | `.local/review/manifest-verification.json` |
| `7929a1bc` | `tsc -p .local/review/ws/tsconfig.json --noEmit` | `PASS` (`exit=0`) | `.local/review/logs/typecheck.log` |
| `7929a1bc` | `node test/existing-ui.checks.mjs` | `PASS` (`21` locked dakit files + fleet/auth color/border/scale checks) | `.local/review/logs/existing-ui.checks.log` |
| `7929a1bc` | `node test/conversations-ui.checks.mjs` | `PASS` (vendor lock, borders, colors, scale, DOM safety, CSP, M1 transport) | `.local/review/logs/conversations-ui.checks.log` |
| `7929a1bc` | `node .local/review/postland-worker-probe.mjs` (Miniflare + SQLite `Hub` + `worker.ts`) | `4/4 PASS` (`P1` namespaces/CSRF/sender-override, `P2` F7 multi-project & credential death, `P3` pairing null/race/bare-shift/refresh, `P4` membership/owner/watermark/idempotency) | `.local/review/logs/postland-worker-probe-7929.log` (prior diagnostic harness iterations retained in `postland-worker-probe-attempt1..5.log`) |
| `7929a1bc` | `python3 .local/review/postland-host-probe.py` (includes live read-only `ConversationBridge.qualify()` against own session `4d1c2347`) | `3/3 PASS` (`H1` genuine read-only native `qualify()` with `0` journal/lock writes, `H2` default HOLD/machine mismatch/revoked member/`empty_range_page` proof, `H3` `F-POST-1` CLI `machine_id` reproduction) | `.local/review/logs/postland-host-probe-7929.log` (prior harness iterations retained in `postland-host-probe-attempt1..2.log`) |
| `7929a1bc` | `node .local/review/postland-ui-probe.mjs` | `2/2 PASS` (`U1` 21-file dakit lock + 8-file UI color/border/scale check, `U2` `conv-outbox:v2:` tuple injectivity & account-switch purge) | `.local/review/logs/postland-ui-probe-7929.log` (prior harness iterations retained in `postland-ui-probe-attempt1..2.log`) |
| `7929a1bc` | `python3 -m unittest host/tests/test_conversations.py host/tests/test_conversation_bridge.py` | `50/50 PASS` in `2.611s` (`exit=0`) | `.local/review/logs/host-unit-subset-7929.log` (initial wrong-module-name invocation retained in `host-unit-subset-attempt1.log`) |
| `16d66d69` | `203`-file composed manifest + `10`-path Windows delta + doc artifact SHA-256 verification | `PASS` (`203/203` match `5fb8eeac...`; `209` tracked files; patch matches `bf2f3df7...`) | `.local/review/manifest-verification-16d66d69.json` |
| `16d66d69` | `python3 .local/review/postland-host-probe.py` | `3/3 PASS` (`H1`, `H2`, `H3` on `private_fs.py`-backed host package; `0` journal/lock writes) | `.local/review/logs/postland-host-probe-16d66d69.log` |
| `16d66d69` | `python3 -m unittest host/tests/test_private_fs.py host/tests/test_windows_imports.py host/tests/test_config.py host/tests/test_bridge.py` | `90` run: `89 PASS + 1 SKIP` (`test_real_windows_reparse_refused_if_symlink_setup_supported`, skipped on Linux) in `17.296s` | `.local/review/logs/host-windows-delta-16d66d69.log` |
| `16d66d69` | `node .local/review/postland-worker-probe.mjs` & `node .local/review/postland-ui-probe.mjs` (standalone `nodePaths` verification) | `4/4 PASS` & `2/2 PASS` | `.local/review/logs/postland-worker-probe-16d66d69.log`, `.local/review/logs/postland-ui-probe-16d66d69.log` |

### 5.2 Retained Pre-Main & Parallel Evidence (Attributed by Provenance; Not Re-Counted as This Reviewer's Runs)

| Provenance Owner & Pin | Evidence Layer & Exact Counts | Reconciled Native / Artifact Receipts |
| :--- | :--- | :--- |
| `platform-conv-review` (`5a4b9381d4863d10a1d46b249c0be83e17cee18f`, `gemini-4-argon-high`) on `cfb4dc26` (`17aab235...`) | `453/453` Worker Vitest cases (`18` files), `635/635` host Python cases, `30/30` mock-browser Playwright cases + `89` real Chromium PNGs across `11` routes (`4` viewport/theme combos), `9/9` public machine-pairing groups, `10/10` own-session native POSIX/P3 groups (8 preflight groups are repeats and excluded from unique counts), `wrangler deploy --dry-run` `552.38 KiB` / `105.98 KiB` gzip | `/home/alexey/git/agent-coordination/.local/platform/conv-review-native-reconciliation.json`: journal `conversation-bridge-4881d1cc7733a351e2c4297d.json` (`14952050b13d6a2ee34d8292645965e4fc1fba0682241038e927f41f460dd8ca`), mode `0600`, cursors `[3, 4]`, delivery `01a147ea-9d32-7791-a874-a598242e01f5`, reply `01a147ea-9f7b-7051-8a7e-229e947d2ae1`, `1` gap audit, `0` pending |
| `platform-conv-final-r3` (`cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`, `GPT-6`) | Frozen CFB author verification on `17aab235...` | `/home/alexey/git/agent-coordination/.local/platform/conv-final-r3-native-reconciliation.json`: journal `conversation-bridge-addd1e3bd53bfaaab1a0c02f.json` (`dcb91992689125bbb24b758bc94fe9dc41552b53fa2a97b82d4d22be5e01cd82`), mode `0600`, cursors `[3, 4]`, delivery `01a147d2-3e7a-7d21-a20f-b6b80f370ecd`, reply `01a147d2-40f6-7321-97cd-0ed0305b44ed`, `1` gap audit, `0` pending |
| `platform-win-merge` (`4a0b74cb`, `GPT-6`), `platform-win-merge-review` (`47b53dcb`, `gemini-4-argon-high`), & `platform-win-factual-audit` (`8d537279`, `grok-4.6`, `e5133fc9`) on `3888a439` (`5fb8eeac...`) | `654` host union cases (`653 PASS + 1 actual-Windows SKIP`, `749` subtests), `92/92` composed Worker Vitest, `6/6` reviewer storage probes, `58/58` `py_compile`, `24/24` `test_conversation_bridge_gap.py` | Author journal `db6ff994ad845fa28fef62ac3cf604e4cdf8bc7af3e49faf3b7a7cfd34abc128`; reviewer `47b53dcb` journal `8360ff91bce42ec3732efc35b9701ae6e6fcf9cd9eebf99b92d80a3b64582c87` (envelope `01a14800-8d7a-7fc0-849a-b1bd1d488509`, reply `01a14800-901f-7a01-868b-9f358abff1d0`, cursors `4` and `3`, `1` gap, `0` pending) |

### 5.3 Resource & Scratch Measurements (`platform-postland-review`)

- **Combined `.local/review` + `.local/tmp` scratch peak**: **`7,774,208` allocated bytes (`7.41 MiB`) / `6,822,746` apparent bytes (`6.51 MiB`)**, strictly within the `20 MiB` (`20,971,520` bytes) combined budget (`.local/review/resources.json`).
- **Session cgroup peaks**: `memory.peak = 432,181,248` bytes (`412.16 MiB` vs `1,572,864,000` bytes / `1500 MiB` limit); `pids.peak = 43` tasks (vs `256` limit).
- **Host floors at completion**: `MemAvailable = 39,320,911,872` bytes (`36.62 GiB` vs `10 GiB` floor); disk available `77,874,393,088` bytes (`72.53 GiB` vs `50 GiB` floor).

---

## 6. Roadmap State Consistency, Parallel Task Audit, & Next-Milestone Order

### 6.1 Roadmap State Consistency (`docs/platform/PLAN.md` & `docs/platform/STATUS.md`)

1. **Completed & Integrated on `main` (`16d66d69708268ab84089447534f0439e0bfd893`)**:
   - `M1` (Conversation Domain & Storage), `M2` (Human Web UI), `M2b` (provisional official `dakit` 21-file adoption across fleet/auth/conversations), `M3a` (Linux/POSIX CLI + foreground native conversation bridge + `P3` gap recovery), `M6` (Task Ownership & Placement Admission in `src/claim_admission.ts`, integrated on `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` and extended for `F7` project/agent scoping in `7929a1bc07bfea3a2b387989800cf1b0ba140984`), `F7` (multi-project agent registry & immediate credential revocation), `Pairing` (public machine-bound & `NULL`-machine device pairing), and `M3b` **source** (`3888a439f9e32d5a075a7e3d23b08ae9aae8201d` portable `private_fs.py` + `win32_fs.py`, audited by `e5133fc9cf5e71a29fbfb900aaa3b0455984f8e5`).
   - With this report (`GATE-CONV-REVIEW`), the post-main Argon gate for the integrated conversation + Windows host source bundle on `16d66d69708268ab84089447534f0439e0bfd893` is satisfied.
2. **Parallel Tasks & Unproven Physical/Runtime Gates (Strictly Separated)**:
   - **`M4` (Authorized Conversation Links to Canonical Tasks & Outcomes)**: Active in isolated worktree `/home/alexey/git/agent-hub-wt-platform-links` (`platform-links`, `f04bc012`, Grok). Physical worktree isolation means no cross-worktree source lease is held or blocked.
   - **`M5` (Conversation-Native Pings & Safe Follow-Up)**: Original Grok proposal `1229be75432dc55f9939135d1a14a7e6e6e6a04b` was rightly rejected (`REVISE`) by independent Codex review `1fe94af0a605f50af9b8a2eec4da11ea92265379` for five real defects:
     1. *Destructive target-authored follow-up*: inserting the follow-up message as `sender_actor = target` triggered `sendMessage`'s sender read-cursor advance (`last_read_seq = nextSeq`), erasing the target's unread state and causing `ConversationBridge.cycle()` (`if msg['sender_actor'] != self.actor:`) to skip native delivery to the target agent.
     2. *Arbitrary actor impersonation*: `/api/checks` accepted any `followup_actor` string and posted messages attributed to that actor without verifying current membership or delegation authority.
     3. *Response-loss idempotency gap*: `last_followup_at` / interval state was updated only after caller receipt rather than atomically with message creation.
     4. *Concurrent due-slot duplicate execution*: overlapping `/api/checks/followup/run` and scheduled alarm invocations could fire duplicate follow-ups in the same interval.
     5. *TypeScript compile failure*: `test/conversation-pings.test.ts` failed `tsc --noEmit`.
     No `M5` code is on `main`. Repaired candidate `0a4a2fb95467fa96518068f68f15c0c93e5301e6` (`platform-pings-r2`, Codex) is in isolated worktree `/home/alexey/git/agent-hub-wt-platform-pings-r2` and under independent Grok review (`platform-pings-r2-review`, `cfcf0df0` in `/home/alexey/git/agent-hub-wt-platform-pings-r2-review`).
   - **`M7` (Generalized Epoch-Fenced Role Leases)**: Active in isolated worktree `/home/alexey/git/agent-hub-wt-platform-role-leases` (`platform-role-leases`, `7566b10d`, Codex), generalizing `root-run` lease fencing to configured automation roles (`src/role_leases.ts`, `src/jobs.ts`, `host/agent_hub_host/jobs.py`).
   - **`M8a` / `M8b` (Qualified Host Execution with Launcher Receipts & Scheduled Role Job Profiles)**: Launcher revision `6ee79d6ae13265135b89ca87d37d503de0099b76` received scoped protocol acceptance in fresh Grok 4.6 review `f194f4fdc227592829b1ceb87f95423977b638f4` (`560` unit tests + `4` probes; older Argon report `f5aed5c87cfccd42caed78fa870c1acdd5651c44` remains disputed, and dirty shared `/home/alexey/git/agent-quota-launcher` stays untouched). Live backend-death reservation cleanup, live multi-account quota oracle (using the actually configured accounts without inventing a hard-coded 3-account requirement), genuine native first-action proof (`M8a`), and scheduled role job profiles (`M8b`: `writer-run`, `principal-wake`, `head-recovery`; issue 102 stays open) remain open.
   - **Physical Windows (`Win35`) & Live Deployment Gates**: `16d66d69708268ab84089447534f0439e0bfd893` proves the Windows host source contract on Linux (`89 PASS + 1 Windows SKIP` in `host-windows-delta-16d66d69.log`), but physical `kernel32.dll`/`advapi32.dll` load, protected NTFS owner DACLs, reparse/junction/hardlink refusal on real NTFS volumes, and `Win35` enrollment remain unproven until executed on `Win35`.

### 6.2 Ordered Next Milestones, Essential Ship Steps, Falsifiers, & Owners

| Order / Milestone | Current State on `16d66d69` | Concrete Essential Next Ship Step | Falsifier (What Fails the Gate) | Owner |
| :--- | :--- | :--- | :--- | :--- |
| **1. `M4` (Task/Work & Outcome Links)** | Candidate `3e134565` in `/home/alexey/git/agent-hub-wt-platform-links` | Complete different-model review of `src/conversation-links.ts` + HTTP/CLI link queries with strict active-membership checks (`requireActiveMembership`), re-base/verify on `16d66d69708268ab84089447534f0439e0bfd893`, and FF-integrate to `main`. | Non-member or revoked member can attach, list, or resolve task/branch/issue links for a conversation, or link rows leak across projects. | `platform-links` (`f04bc012`) → different-model reviewer → `platform` |
| **2. `M5` (Conversation-Native Backlog Pings)** | `1229be7` rejected by `1fe94af`; `0a4a2fb9` under review by `platform-pings-r2-review` (`cfcf0df0`) | Complete independent Grok review of `0a4a2fb95467fa96518068f68f15c0c93e5301e6` verifying explicit delegation grants, target unread preservation (`last_read_seq` untouched and `ConversationBridge` delivers), atomic idempotency + durable scheduler state, and `tsc --noEmit` before FF integration. | Follow-up post advances target `last_read_seq`, fails to deliver through `ConversationBridge`, allows unauthorized actor spoofing, or duplicates under concurrent alarm/manual run. | `platform-pings-r2` (`0a4a2fb9`) → `platform-pings-r2-review` (`cfcf0df0`) → `platform` |
| **3. `M7` (Generalized Epoch-Fenced Role Leases)** | Active in `/home/alexey/git/agent-hub-wt-platform-role-leases` (`7566b10d`) | Finish generalized epoch-fenced role leases (`src/role_leases.ts`, `src/jobs.ts`, `host/agent_hub_host/jobs.py`), pass Worker + host test suites and different-model review, and FF-integrate to `main`. | Stale lease holder or superseded epoch mutates job/conversation/claim state after lease loss or role handoff. | `platform-role-leases` (`7566b10d`) → different-model reviewer → `platform` |
| **4. Live Hub Rollout & POSIX `.local` `0700` Prep** | Source ready on `main` (`16d66d69`); shared `/home/alexey/git/agent-hub` owned by `hub-zai` | After Alexey's explicit deploy approval, fast-forward `/home/alexey/git/agent-hub` to `main`, verify owner `chmod 0700 <workspace>/.local` on participating POSIX workspaces, deploy Worker, and pair/enroll Hetzner host & agents via public `/device`. | Unapproved deploy, `.local` group/world bits (`0775`/`0755`) causing `private_fs._private` rejection, or stale `host.json` `machine_id` (`F-POST-1`). | `hub-zai` (`7a2f9ac0`) + `platform` (with Alexey deploy gate) |
| **5. `M3b` Physical Windows (`Win35`) Proof** | Source integrated (`16d66d69`); physical NTFS/DLL unproven | On physical Windows 10/11 NTFS host (`Win35`), run `python -m unittest discover -s host/tests -v` (confirming `test_real_windows_reparse_refused_if_symlink_setup_supported` runs and passes), verify real SDDL `O:{sid}D:P(A;;FA;;;{sid})` with OS ACL inspection, and run one head-ACKed public pairing + native bridge round-trip. | `test_real_windows_reparse_refused...` skips/fails, `win32_fs.api()` fails ABI/handle checks, DACL allows non-owner ACEs, or `MoveFileExW`/`msvcrt.locking` fails under contention. | Windows host owner (`Win35`) + `platform` |
| **6. `M8a` Live & `M8b` (Qualified Host Execution & Scheduled Role Job Profiles)** | `6ee79d6a` protocol scoped-accepted by `f194f4fd`; `M8b` awaits `M6`+`M7`+`M8a` | Prove live launcher backend-death reservation release, multi-account quota oracle across actual configured accounts, and hub-to-host launch with account/profile/provider/model/effort + native first-action receipt (`M8a`); then wire `writer-run`, `principal-wake`, and `head-recovery` job profiles with fenced role custody (`M8b`, issue 102). | Orphaned quota reservation after process kill, missing launcher/first-action receipt, or unfenced role job execution. | Launcher owner + `platform` |
| **7. `M9` & `M10` (Bounded Dashboard Accounting & Request-to-Outcome Obligations)** | Open (follow `M4`–`M8b`) | Implement bounded `Dashboard` accounting producer ingestion + `dakit` views within existing `public/vendor/dakit/**` lock (`M9`), and conversation request-to-outcome / ask-ledger / turn-end obligation follow-through (`M10`). | Unbounded accounting retention, mutation of locked `public/vendor/dakit/**`, raw palette literals, or silently dropped turn-end obligations. | Implementer → different-model reviewer → `platform` |
| **8. `M11` (Physical Two-Host Adoption & Legacy Retirement)** | Open final milestone | Complete physical two-host (Hetzner + `Win35`/second machine) human-to-agent and agent-to-agent conversation + routed task execution proof, then execute acknowledged §12 handoffs retiring legacy `:8788` adapter, `principal-ping`, and standalone `agent-dashboard`. | Retiring any legacy service before physical two-host proof and explicit operator/head handoff ACK. | `platform` + `root` + Alexey |
