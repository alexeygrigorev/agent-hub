# M4 + M5 (+ M7) Composed Source — Independent Deep Review (`docs/reviews/CONVERSATION-LINKS-PINGS.md`)

- **Verdict**: **`REVISE`** (`1` High, `1` Medium, `0` Low product defects confirmed in `src/conversations.ts` on both `72af225b8758817250c2a0d37372b948fbb7dc42` and `f6ec420cec3ab28fbea218dda478539b96fd4bbc`; **`0` defects** in `src/conversation-links.ts`, `src/conversation-hub.ts`, `src/conversation-automation.ts`, or the M7 role-leases integration `src/role_leases.ts` / `src/jobs.ts` / `src/hub.ts` / `host/agent_hub_host/jobs.py`).
- **Review lines**:
  - `Review: changes-requested sha=f6ec420cec3ab28fbea218dda478539b96fd4bbc reviewer=platform-links-pings-review/gemini-4-argon-high`
  - `Review: changes-requested sha=72af225b8758817250c2a0d37372b948fbb7dc42 reviewer=platform-links-pings-review/gemini-4-argon-high`
- **Reviewed final combined commit**: `f6ec420cec3ab28fbea218dda478539b96fd4bbc` (`docs(reviews): normalize publication notes and relative evidence paths [skip ci]`)
  - **Direct parent of `f6ec420`**: `7bc56745b59b499c1ab79c23608d402737512284` (`Merge M7 role leases and review into composed M4/M5 review branch [skip ci]`, parents `72af225b8758817250c2a0d37372b948fbb7dc42` and `04e42aa2c5d81311c580d139f04e3bddf3a793bd`)
  - **M7 R2 review commit**: `04e42aa2c5d81311c580d139f04e3bddf3a793bd` (`docs(reviews): accept M7 role-leases R2 repair [skip ci]`, reviewer `platform-role-leases-r2-review/grok-4.6`)
  - **M7 R2 repaired source commit**: `275c84a39766df45c1028f04bc6f7ba8b65c3287` (`test: cover unresolved writer-date fences after expiry [skip ci]`, parent `6a2cbd11b16f1e97ca65326c3129f6fc39b95046`, author `platform-role-leases-r2/gpt-6`)
- **Reviewed initial M4+M5 composed commit**: `72af225b8758817250c2a0d37372b948fbb7dc42` (`test: cover M4 conversation links with M5 bounded retention [skip ci]`)
  - **Direct parent of `72af225`**: `6ca2fc6a11904ef31e65992986e4269a508511ae` (`Merge accepted M4 R2 review record into M4/M5 composition [skip ci]`, parents `82de920bdc795246346e82f6379d5a8cdad0aea5` and `fc5bd30dc7e0ee135158007bd2add1edc79e66ab`)
  - **M4+M5 source merge commit**: `82de920bdc795246346e82f6379d5a8cdad0aea5` (`Merge M4 conversation links (7b6f7ffe) with M5 main (6cf0c185) [skip ci]`, parents `6cf0c185495a33cf04ec2b986c3c901ed3de2cda` and `7b6f7ffeac48cc8fc26a357e100c03da7360d017`)
  - **M4 R2 accepted source & review**: `7b6f7ffeac48cc8fc26a357e100c03da7360d017` (`platform-links-r2/gpt-6`), independently accepted at `fc5bd30dc7e0ee135158007bd2add1edc79e66ab` (`platform-links-r2-review/gemini-4-argon-high`, over original R1 review `ab3b953dbbadcd10e6f0034ec4a970564735ba0d`)
  - **M5 integrated base at composition**: `6cf0c185495a33cf04ec2b986c3c901ed3de2cda` (integrating M5 source `212560b6331f6347519f862b93c2a5cd7975871b` and Grok R3 acceptance `34ee3d03568aa2770013c6371c229bc9f2b14975`; separate independent host-binding `85fd279` / `80921a0` later fast-forwarded `main` on host-only auth/config/API + runbook without touching conversation, jobs, or hub core code)
- **Composition author**: `platform-links-m5-compose/gpt-6` (Codex session `fa957e2f-e826-41b6-9b20-d96c339a135b`, `gpt-6` family high, minor unknown; early commit trailers on `82de920` and `6ca2fc6` contained an unverified minor version string retracted by the author on `72af225` and documented in `.local/compose/FINAL.md`)
- **Independent reviewer**: `platform-links-pings-review/gemini-4-argon-high` (Antigravity session `4e37667d-a7a6-4657-b03b-efd08c5b5fba`, distinct model family from `gpt-6`)
- **Governance & milestone boundaries**:
  - Source review of `72af225b8758817250c2a0d37372b948fbb7dc42` and `f6ec420cec3ab28fbea218dda478539b96fd4bbc` is strictly separate from `main` integration, live runtime state, routing compatibility ACK (`01a147d0-8fdc-7d83-94aa-5281c5ab8dd6`), M8a/M8b host `writer-run` backend qualification, and M11 physical cross-host adoption.
  - Task `0422` fork/WIP state is owned by `platform` head and is not source acceptance or `main` publication.

---

## 1. Mechanical Source Envelope, Ancestry & File Digest Audit

Both reviewed pins (`72af225b8758817250c2a0d37372b948fbb7dc42` and `f6ec420cec3ab28fbea218dda478539b96fd4bbc`) were verified directly from Git objects in this worktree (`.local/review/manifest-verification.json` and `.local/review/combined-manifest-verification.json`):

| Envelope / Check | Pin `72af225b8758817250c2a0d37372b948fbb7dc42` | Pin `f6ec420cec3ab28fbea218dda478539b96fd4bbc` |
| --- | --- | --- |
| **Head reviewed-source manifest** | `214` sorted `[path, sha256]` pairs; compact JSON SHA-256 `2c220e47327e8b788a89635d3b01b00a2af8d501d16d9180cbe146c50c0e162e` (`0` mismatches) | `217` sorted `[path, sha256]` pairs; compact JSON SHA-256 `253cb50a199ad4d3a62faf3568d652aa56c4306d7482d650bb177a6dc72c7915` (`0` mismatches) |
| **Author all-tracked manifest** (includes 14 proposal/review metadata files excluded from head's source envelope) | `228` sorted `[path, sha256]` pairs; compact JSON SHA-256 `0f1503361642d700af30be1d4a8ce38a6c352be1a53f220d29a7e1da49366fae` (`0` mismatches) | `231` tracked paths (`228` + `docs/ROLE-LEASES.md`, `docs/reviews/ROLE-LEASES.md`, `docs/reviews/ROLE-LEASES-R2.md`, `src/role_leases.ts`, `test/role-leases.test.ts` = `233` total tracked files) |
| **M4/M5 product & test paths vs `72af225`** | Baseline under review | **100% byte-identical** (`0` diff bytes across `src/conversations.ts`, `src/conversation-hub.ts`, `src/conversation-links.ts`, `src/conversation-automation.ts`, `src/bus.ts`, `src/checks.ts`, `test/conversation-links*.test.ts`, `test/vitest.conversation-links*.config.ts`, `docs/CONVERSATION-LINKS.md`, `docs/CONVERSATIONS.md`) |
| **Non-shared M4 incoming paths vs `7b6f7ff` / `fc5bd30`** | **100% byte-identical** (`docs/CONVERSATION-LINKS.md`, `docs/CONVERSATIONS.md`, `docs/reviews/CONVERSATION-LINKS.md`, `docs/reviews/CONVERSATION-LINKS-R2.md`, `src/conversation-links.ts`, `test/conversation-links.test.ts`, `test/conversation-links-r2.test.ts`, `test/vitest.conversation-links.config.ts`) | All M4 source/test paths 100% byte-identical; `docs/reviews/CONVERSATION-LINKS-R2.md` carries only head's 7-line publication note and historical table cell normalization (`m4-r2-report-pin-audit.json`) while preserving original `fc5bd30` in Git history |
| **EOF / whitespace check (`git diff --check`)** | `6ca2fc6..72af225`: exit `0` (`6cf0c18..72af225` reports 2 inherited trailing blank lines at `docs/reviews/CONVERSATION-LINKS-R2.md:154` and `test/conversation-links.test.ts:746` because the author restored original `7b6f7ff`/`fc5bd30` bytes after intermediate normalization in `.local/compose/eof-normalization.json`) | `72af225..f6ec420`: exit `0`; `7bc5674..f6ec420`: exit `0` |

### Key File Digests Across Base, Incoming Branches, and Composed Pins

| Path | `6cf0c185495a` (M5 Base) | `7b6f7ffeac48` (M4 R2) / `275c84a39766` (M7 R2) | `72af225b8758` (M4+M5 Pin) | `f6ec420cec3a` (M4+M5+M7 Pin) | Bytes (`f6ec420`) |
| --- | --- | --- | --- | --- | --- |
| `src/conversations.ts` | `84fa4d987443741ca96b9d1527d82f9da7e3e384b60643ebdfbb8c8003497466` | `799521abf77df9e8bc061d3f543bad6e5b32d87aeea11fc96eb5da78cc1763d2` (M4) | `e8a05d198a359d0a04008941e3ec697e8e3d97ca3671856623d504493987b843` | `e8a05d198a359d0a04008941e3ec697e8e3d97ca3671856623d504493987b843` | `143,346` |
| `src/conversation-hub.ts` | `135b8d07fa614d2e0d4cb79bf89119263452538f052464615df038ab2447ecbc` | `7a8e44ceb47dc717f088d46e3f0c620b3746ac949fc2ad8eba78fb0dd0255be8` (M4) | `bf25a824e763409de85999f738677ba5a16552e5c1f35e78c85cbeb2b9b23295` | `bf25a824e763409de85999f738677ba5a16552e5c1f35e78c85cbeb2b9b23295` | `6,976` |
| `src/conversation-links.ts` | `absent` | `3574d057647e1a602e1637f5e8b2d9fb5eeebbce70c7a46d05c161569312a331` (M4) | `3574d057647e1a602e1637f5e8b2d9fb5eeebbce70c7a46d05c161569312a331` | `3574d057647e1a602e1637f5e8b2d9fb5eeebbce70c7a46d05c161569312a331` | `20,496` |
| `src/conversation-automation.ts` | `bc7ca76efae86c08fd6eb5ba879184574df83900b6c6bfcf767a265f80157fa5` | `absent` in M4 | `bc7ca76efae86c08fd6eb5ba879184574df83900b6c6bfcf767a265f80157fa5` | `bc7ca76efae86c08fd6eb5ba879184574df83900b6c6bfcf767a265f80157fa5` | `8,595` |
| `test/conversation-links.test.ts` | `absent` | `740887d6e7b2ede3aeedfdb0a43bfdca279f1f21a1ca3027af55e5ccf812410e` (M4) | `740887d6e7b2ede3aeedfdb0a43bfdca279f1f21a1ca3027af55e5ccf812410e` | `740887d6e7b2ede3aeedfdb0a43bfdca279f1f21a1ca3027af55e5ccf812410e` | `23,836` |
| `test/conversation-links-r2.test.ts` | `absent` | `29bb67056dadd892f7d4d819e7e642912b56e90152cf6925f3947ffdb6657dd1` (M4) | `29bb67056dadd892f7d4d819e7e642912b56e90152cf6925f3947ffdb6657dd1` | `29bb67056dadd892f7d4d819e7e642912b56e90152cf6925f3947ffdb6657dd1` | `31,032` |
| `test/conversation-links-pings.test.ts` | `absent` | `absent` | `8f6a64d3b0b81034789c62ce51cf0451c209f3460cf0be8bd18c448ba3c6fc03` | `8f6a64d3b0b81034789c62ce51cf0451c209f3460cf0be8bd18c448ba3c6fc03` | `19,941` |
| `test/vitest.conversation-links-pings.config.ts` | `absent` | `absent` | `f0ae92539fb0854a0265c294cbbaabfa8812d0a5d071055b7aa8671667e02951` | `f0ae92539fb0854a0265c294cbbaabfa8812d0a5d071055b7aa8671667e02951` | `828` |
| `src/hub.ts` | `8b7f480e0a3cb158268bf131257ef7d9debd90c8df889b2d3076ef24fb8a90c1` | `8c426087166ca60c0ae607cb16a24423a29004e5e68b96a6953083380d9ce09f` (M7) | `8b7f480e0a3cb158268bf131257ef7d9debd90c8df889b2d3076ef24fb8a90c1` | `e2ec3716f769d81bf2d4a5a055433b963d4f6a1bd4db2f737f25a82867fa2c9f` | `124,065` |
| `src/jobs.ts` | `3c19f230e0f7101b799b36774edf68bc7e9199d033fbe5b3288a494071fd6b69` | `1990f4570c8de7a9aa711e3be8631a5be8b215d39335e18ec4cf8659283dd8a4` (M7) | `3c19f230e0f7101b799b36774edf68bc7e9199d033fbe5b3288a494071fd6b69` | `1990f4570c8de7a9aa711e3be8b215d39335e18ec4cf8659283dd8a4` (`131,752` B, identical to `275c84a`) | `131,752` |
| `src/role_leases.ts` | `absent` | `258b0a1244bc009bf6df0f00e0d4131c4972ab7442979540df26dc530cd728b5` (M7) | `absent` | `258b0a1244bc009bf6df0f00e0d4131c4972ab7442979540df26dc530cd728b5` | `8,966` |
| `host/agent_hub_host/jobs.py` | `4c99b18666a9de9fa6f508dc752d275914ab49661eb26ba7305fd0bb89fd81bd` | `0dc785f0a214b1067a8462fa8ef8cb828342480cf16c9f0a7cb697111a0841a1` (M7) | `4c99b18666a9de9fa6f508dc752d275914ab49661eb26ba7305fd0bb89fd81bd` | `0dc785f0a214b1067a8462fa8ef8cb828342480cf16c9f0a7cb697111a0841a1` | `35,610` |
| `test/role-leases.test.ts` | `absent` | `3474d5622ee345e8ee4c5791348b05d191bc3ab0db11103eb5548ba0b42db066` (M7) | `absent` | `3474d5622ee345e8ee4c5791348b05d191bc3ab0db11103eb5548ba0b42db066` | `28,486` |

---

## 2. Confirmed Product Findings Requiring Repair (`F1` High, `F2` Medium)

### Finding F1 (High — M4/M5 Composition Regression): Archived-Parent Automation Guard Desynchronizes Category 3 Eligibility (`hasWork` / `hasExpiredRows` / `expiredConvs`), Causing Permanent `limit = 1` Livelock and Cross-Conversation Head-of-Line Blocking

- **Severity**: **High** (livelocks `ConversationsService.retentionCleanup(now, 1)` and `Hub.busService.retentionCleanup(now, 1)` permanently under normal `limit = 1` incremental cleanup whenever an expired archived conversation has more automation rows than child rows, and head-of-line blocks later eligible expired archived conversations when an older expired archived conversation's child rows are drained while its header awaits automation retirement or an unresolved check slot).
- **Locations**:
  - `src/conversations.ts:1038–1050` (`ConversationsService.hasExpiredRows`)
  - `src/conversations.ts:1156–1166` (`runArchivedStep` -> `expiredConvs` SQL query)
  - `src/conversations.ts:1308–1314` (archived-parent automation guard added in composition merge `82de920bdc795246346e82f6379d5a8cdad0aea5`)
  - `src/conversations.ts:1463–1470` (Category 3 `hasWork` in `ConversationsService.retentionCleanup`)
  - Coupled effect on `src/conversation-hub.ts:103–142` (`Hub.busService.retentionCleanup` -> `takeConv`)
- **Root Cause**:
  In the M4/M5 composition commit `82de920bdc795246346e82f6379d5a8cdad0aea5`, `runArchivedStep()` (`src/conversations.ts:1308–1314`) added a parent guard so `DELETE FROM conversations WHERE id = ?` is skipped (`continue;`) while automation child state still references `conv.id`:
  ```ts
  if (this.sql.exec(`SELECT 1 FROM conversation_automation_grants WHERE conversation_id = ? LIMIT 1`, conv.id).toArray().length ||
      this.sql.exec(`SELECT 1 FROM conversation_automation_receipts WHERE conversation_id = ? LIMIT 1`, conv.id).toArray().length ||
      this.sql.exec(`SELECT 1 FROM hub_check_slots WHERE state != 'done' AND json_valid(check_json)
        AND json_extract(check_json, '$.action.conversation_id') = ? LIMIT 1`, conv.id).toArray().length) {
    continue;
  }
  ```
  However, three coupled SQL queries were not updated to match this guard:
  1. **Category 3 `hasWork()` (`src/conversations.ts:1463–1470`) and `hasExpiredRows()` (`src/conversations.ts:1041–1050`)**: Both still check only `SELECT 1 FROM conversations WHERE archived_at IS NOT NULL AND archived_at <= ? LIMIT 1`. Once an expired archived conversation's `conversation_messages`, `conversation_members`, and `conversation_links` are all deleted (`0` child rows left), Category 3 `hasWork()` still returns `true` even when `conv.id` is blocked by the parent guard (`grants`, `receipts`, or non-`done` `hub_check_slots`) and `runArchivedStep(budget)` is guaranteed to return `0`.
  2. **`expiredConvs` candidate selection (`src/conversations.ts:1156–1166`)**:
     ```sql
     SELECT id FROM conversations
     WHERE archived_at IS NOT NULL AND archived_at <= ?
     ORDER BY archived_at ASC, id ASC
     LIMIT ?
     ```
     applies `LIMIT ?` (`stepRemaining`) *before* checking whether each expired archived conversation has deletable child rows (`messages`, `members`, `links`) or is unblocked by the parent guard.
  3. **Incomplete slot reference check at `src/conversations.ts:1311`**: Line `1311` checks only `json_valid(check_json) AND json_extract(check_json, '$.action.conversation_id') = ?` and omits `(json_valid(intent_json) AND json_extract(intent_json, '$.conversationId') = ?)`, whereas `automationRetirementCandidates()` (`src/conversations.ts:1100–1101`) matches slots by either `check_json` or `intent_json` (since `PUT /api/checks` can replace `check_json` while `intent_json` remains frozen on an unresolved `retry` slot).
- **Deterministic Reproduction (`.local/review/m4-m5-adversarial.probe.test.ts` test 3, verified on both `72af225b8758817250c2a0d37372b948fbb7dc42` and `f6ec420cec3ab28fbea218dda478539b96fd4bbc`)**:
  1. **Permanent `limit = 1` livelock (`F1a`)**:
     - Create a conversation `convA` via `POST /api/conversations` (`2` members, `0` messages, `0` links), attach `4` automation grants (`g-0..g-3`) via `PUT /api/conversations/:id/automation`, and expire `convA.archived_at = '2000-01-01'` (more generally, any expired archived conversation with `M` child rows and `A > M + 1` expired/retireable automation rows in Category 1 or Category 2).
     - Call `retentionCleanup(t, 1)` repeatedly:
       - Passes 1–5 delete `[1, 1, 1, 1, 1]` rows (`2` members in Category 3 and `3` grants in Category 2).
       - On pass 5, Category 2 (`grants`, index `2`) deletes its 3rd grant while `1` grant still remains on `convA`, advancing `retention_cleanup_cursor('conv').next_index` to `(2 + 1) % 5 = 3`.
       - On pass 6 (`limit = 1`), `runFairRetentionPass` starts at index `3`, computes `eligible = [Category 3 (archived), Category 2 (grants)]` (`eligible.length = 2`), and because `totalBudget (1) < eligible.length (2)`, invokes **only** `eligible[0]` (`runArchivedStep(1)`).
       - `runArchivedStep(1)` finds `0` messages, `0` members, and `0` links on `convA`, hits the parent guard at line `1308` (`1` grant still exists), `continue`s, and returns `0`.
       - Because `deleted === 0`, `lastDeletedIndex` is `null`, so `retention_cleanup_cursor('conv').next_index` stays at `3`. Every subsequent `retentionCleanup(t, 1)` call returns `{ rowsDeleted: 0, hasMore: true }` forever (`[1, 1, 1, 1, 1, 0, 0, 0, 0, 0]`), leaving both the 4th grant and `convA` undeleted.
       - When `Hub.busService.retentionCleanup(t, 1)` runs, as soon as `retention_cleanup_cursor('hub').next_index` is `1` (`takeConv`), `takeConv(1)` returns `0`, freezing the `'hub'` cursor at `1` and starving expired 1:1 bus `messages`/`claims` (`takeBus`) and orphan `conversation_links`.
  2. **Cross-conversation head-of-line blocking (`F1b`)**:
     - Expired archived conversation `c1` (`archived_at = '2000-01-01'`) has drained its members/messages/links (`0` child rows) and has an unresolved `hub_check_slots` row in `state = 'retry'` (or remaining automation rows), so its header is legitimately held by the parent guard.
     - Newer expired archived conversation `c2` (`archived_at = '2000-01-02'`) has `2` members ready to drain and `0` automation rows.
     - Calling `retentionCleanup(t, 1)` runs `expiredConvs` with `ORDER BY archived_at ASC, id ASC LIMIT 1`, selects only `c1`, hits `continue;`, and returns `0` on every call, starving `c2` (`2` members and header remain untouched).
- **Required Fix**:
  Define a shared actionable-archived-conversation SQL predicate that requires `archived_at IS NOT NULL AND archived_at <= ?` **and** either:
  - `EXISTS (SELECT 1 FROM conversation_messages m WHERE m.conversation_id = conversations.id)`, or
  - `EXISTS (SELECT 1 FROM conversation_members cm WHERE cm.conversation_id = conversations.id)`, or
  - `EXISTS (SELECT 1 FROM conversation_links l WHERE l.conversation_id = conversations.id)`, or
  - (`NOT EXISTS (SELECT 1 FROM conversation_automation_grants g WHERE g.conversation_id = conversations.id)` AND `NOT EXISTS (SELECT 1 FROM conversation_automation_receipts r WHERE r.conversation_id = conversations.id)` AND `NOT EXISTS (SELECT 1 FROM hub_check_slots s WHERE s.state != 'done' AND ((json_valid(s.check_json) AND json_extract(s.check_json, '$.action.conversation_id') = conversations.id) OR (json_valid(s.intent_json) AND json_extract(s.intent_json, '$.conversationId') = conversations.id)))`).
  Use this exact predicate in `hasExpiredRows()` (`src/conversations.ts:1041–1050`), Category 3 `hasWork()` (`src/conversations.ts:1463–1470`), and `runArchivedStep()`'s `expiredConvs` `WHERE` clause before `ORDER BY archived_at ASC, id ASC LIMIT ?` (`src/conversations.ts:1156–1166`), and include `intent_json` in the parent guard at `src/conversations.ts:1311`.

---

### Finding F2 (Medium): Unfiltered Inconsistent Receipt in `automationRetirementCandidates()` (Category 0) Blocks Valid Slot Retirement at `limit = 1` and Leaves `hasExpiredRows()` Permanently `true`

- **Severity**: **Medium** (stalls Category 0 slot retirement at small limits when a held mismatched-receipt slot precedes retireable slots in `ORDER BY s.name`, and causes `hasExpiredRows()` / `hasMore` to remain `true` indefinitely with `rowsDeleted: 0`).
- **Locations**:
  - `src/conversations.ts:1036` (`hasExpiredRows` -> `this.automationRetirementCandidates(nowMs, 1)[0].length > 0`)
  - `src/conversations.ts:1098–1106` (`automationRetirementCandidates` Category 0 `hub_check_slots` query)
  - `src/conversations.ts:1420–1423` (`retireAutomation(0, budget)` receipt consistency guard)
  - `src/conversations.ts:1458–1461` (Category 0 `hasWork` in `retentionCleanup`)
- **Root Cause**:
  In `retireAutomation(0, budget)` (`src/conversations.ts:1420–1423`), before deleting a retireable `hub_check_slots` row and its settled receipt, the code checks:
  ```ts
  const receipt = this.sql.exec(`SELECT * FROM conversation_automation_receipts WHERE effect_key = ?`, 'check-' + row.slot_key).toArray()[0] as any;
  if (receipt && receipt.request_json !== row.intent_json) continue;
  ```
  Holding a slot whose receipt `request_json` does not match `row.intent_json` is intentional fail-closed behavior. However, `automationRetirementCandidates(nowMs, limit)` (`src/conversations.ts:1098–1106`) does not exclude such mismatched-receipt slots in SQL before `ORDER BY s.name LIMIT ?`.
- **Deterministic Reproduction (`.local/review/m4-m5-adversarial.probe.test.ts` test 4, verified on both `72af225b8758817250c2a0d37372b948fbb7dc42` and `f6ec420cec3ab28fbea218dda478539b96fd4bbc`)**:
  - Seed slot `'a-held'` (`slot_key = 'a-held-key'`, `state = 'retry'`, missing conversation `'c-held'`, `intent_json = '{"idempotencyKey":"check-a-held-key","body":"expected"}'`) and receipt `'check-a-held-key'` with `request_json = '{"idempotencyKey":"check-a-held-key","body":"TAMPERED"}'`.
  - Seed valid retireable slot `'b-orphan'` (`state = 'done'`, unconfigured check).
  - At `limit = 1`, `automationRetirementCandidates(t, 1)[0]` (`ORDER BY s.name LIMIT 1`) returns only `['a-held']`, which `retireAutomation(0, 1)` skips with `continue;`, returning `{ rowsDeleted: 0, hasMore: true }` on every `limit = 1` call and starving `'b-orphan'`.
  - After `'b-orphan'` is retired at `limit = 2` (`rowsDeleted: 1`), `hasExpiredRows(t)` and `retentionCleanup(t, 100).hasMore` remain `true` forever (`rowsDeleted: 0`) because `'a-held'` is still returned by `automationRetirementCandidates(t, 1)[0]`.
- **Required Fix**:
  In `automationRetirementCandidates()` (`src/conversations.ts:1104–1106`), add:
  ```sql
  AND NOT EXISTS (
    SELECT 1 FROM conversation_automation_receipts r
    WHERE r.effect_key = ('check-' || s.slot_key)
      AND r.request_json IS NOT s.intent_json
  )
  ```
  before `ORDER BY s.name LIMIT ?` so held inconsistent-receipt slots are preserved in SQLite without falsely reporting actionable retirement work or consuming `LIMIT` slots ahead of valid retireable slots.

---

## 3. Verified M4 + M5 + M7 Composition & Security Invariants (`0` Defects Outside F1/F2)

Outside Findings F1 and F2 in `src/conversations.ts`, deep code inspection and real-SQLite/Miniflare HTTP probes confirmed that all other M4, M5, and M7 invariants hold cleanly across both `72af225b8758817250c2a0d37372b948fbb7dc42` and `f6ec420cec3ab28fbea218dda478539b96fd4bbc`:

1. **Shared Durable Object `txSync` Reentrancy & Atomic Rollback (`src/conversation-hub.ts:27–45`, `src/conversations.ts:865–881`)**:
   - Both `Hub` and standalone `ConversationsService` wrap `storage.transactionSync` with a depth counter (`txnDepth > 0` joins the active transaction without nested `BEGIN`).
   - Late SQLite trigger aborts (`RAISE(ABORT, ...)` on `conversation_links`, `conversation_automation_grants`, or `retention_cleanup_cursor`) roll back the entire outer transaction atomically—preserving `retention_cleanup_cursor`, `conversation_storage_usage`, `conversations`, `conversation_messages`, `conversation_members`, `conversation_links`, `hub_check_slots`, `conversation_automation_receipts`, `conversation_automation_grants`, `messages`, and `claims`—and succeed cleanly on one retry once the trigger is dropped (`test/conversation-links-pings.test.ts` test 6).
2. **Standalone `ConversationsService.init()` Idempotency & Bounded `100`-Row Startup Cleanup (`src/conversations.ts:991–1025`)**:
   - Standalone `ConversationsService.init()` initializes `CONVERSATIONS_SCHEMA_SQL`, `AUTOMATION_SCHEMA`, `AUTOMATION_REVOKE_TRIGGER`, and `this.linksHandler.init()` (`CONVERSATION_LINKS_SCHEMA_SQL`) idempotently inside `txSync`, and runs at most one bounded `DEFAULT_RETENTION_BATCH_LIMIT = 100` cleanup pass across all categories (`test/conversation-links-pings.test.ts` test 8).
3. **Streamed-Body Mid-Request Stale Authority Fencing (`.local/review/m4-m5-adversarial.probe.test.ts` test 1)**:
   - Using zero-`highWaterMark` `ReadableStream` request bodies that mutate SQLite *after* `authenticateConversationRequest` completes, effect-time `txSync` revalidation rejects:
     - Missing/invalid CSRF on session-authenticated link attach and automation `PUT` (`403`).
     - Mid-stream agent `machine_id` rebind (`agents_revoke_machine_binding` trigger revokes credential -> `401`).
     - Mid-stream target ownership loss on a `priorKey` conflict candidate (`404 not_found` precedes `409 idempotency_conflict`).
     - Mid-stream member removal during `DELETE /api/conversations/:id/links/:linkId` (`403`, link preserved).
     - Mid-stream owner `users.is_admin = 0` demotion during `PUT /api/conversations/:id/automation` (`403`, `0` grants written).
4. **Unresolved Frozen Intent Retention, Current-Slot Replay Fence & Non-Conversation Backlog Coexistence (`.local/review/m4-m5-adversarial.probe.test.ts` test 2, `test/conversation-links-pings.test.ts` test 7)**:
   - Unresolved `hub_check_slots` (`state = 'retry'`) are retained across link cleanup and DO restarts and fail closed (`automation_grant_revoked`) without promoting or writing a message.
   - Backward-clock execution on a `done` slot replays idempotently without writing a duplicate message; once the settled receipt and `done` slot are retired (`automationRowsDeleted: 2`), replaying the old `FollowUpIntent` throws `automation_slot_retired`.
   - Non-conversation `inbox_backlog` checks evaluate and report `fail` normally alongside conversation `routine` checks.
   - `linksService.deleteChunkForConversation(convId, 0)` and `deleteChunkForConversation(convId, -5)` return `0` without normalizing non-positive budgets to `1`.
5. **Combined M4 + M5 + M7 `Hub` Integration on `f6ec420cec3ab28fbea218dda478539b96fd4bbc` (`src/hub.ts`, `src/jobs.ts`, `src/role_leases.ts`, `.local/review/m4-m5-adversarial.probe.test.ts` test 5)**:
   - `src/hub.ts` cleanly merges M5 (`checkDeps()` / `extraCheckDeps()`, `runChecks` / `scheduledTick`) and M7 (`/api/roles/:project/:name`, `/api/roles/:project/:name/jobs`, `/api/jobs/:id/execute`, `hostCredentialId` binding on claim/decline/offers).
   - Zero-scope role-leased job tokens (`scopes = []`, `actor = 'machine:<host>'`) are refused (`403`) on `/api/conversations`, `/api/conversations/:id/links`, `/api/conversations/:id/automation`, and `/api/roles/:project/:name`.
   - `JobsService.hasUnresolvedWriterIntent` fences same-UTC-date unresolved `writer-run` intents across distinct projects (`demo/writer` -> `other/writer`), aliases, reconfigurations, restarts, and legacy `writer-run:YYYY-MM-DD` keys at both `claimJob` (`409`) and `commitRoleExecution` (`HOLD` / `previous_execution_unresolved`), while allowing a different UTC date (`2099-05-07`) to claim (`200`) and execute (`execute: true`) without losing the retained `2099-05-06` intent.

---

## 4. Audit of Author Evidence & Independent Reviewer Run / Resource Table

### Author Evidence Audit (`.local/compose/`)
Read-only audit of `/home/alexey/git/agent-hub-wt-platform-links-m5-compose/.local/compose/` confirmed:
- The author retained the intermediate `exit 143` early scratch guard receipt (`19,521,536` B allocated in `.local/compose/runs-summary.json` and `.local/compose/config-merge-proof.json`), verified that `mergeConfig(baseConfig, ...)` in `test/vitest.conversation-links-pings.config.ts` had inherited `test/**/*.test.ts` from `vitest.config.ts`, and fixed `test/vitest.conversation-links-pings.config.ts` to use an explicit standalone config (`include: ['test/conversation-links-pings.test.ts']`).
- The author's final runs (`9/9` composed tests, `19/19` M4 links tests, `80/80` M5 pings/retention tests, `53/53` compatibility tests, `2/2` platform tests, `tsc --noEmit` exit `0`) passed within resource bounds (`18,178,048` B max allocated scratch). Those suites did not catch F1 or F2 because the author's `limit = 1, 2` test (`test/conversation-links-pings.test.ts:121–156`) seeded an expired archived conversation with `0` automation rows attached to that archived conversation (`seedRetiredAutomation` attached grants/receipts to `'missing-parent'` rather than `archived.id`).

### Independent Reviewer Serial Runs & Resource Peaks (`.local/review/runs.jsonl`)
All reviewer runs executed serially via `.local/review/run.py` under `taskset -c 0`, `umask 077`, `TMPDIR=/home/alexey/git/agent-hub-wt-platform-links-pings-review/.local/tmp`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, and `--configLoader runner`. All intermediate diagnostic/probe runs (`probe-72af-r3`, `adversarial-72af-r1..r5`, `composed-f6ec-r1`, `adversarial-f6ec-r1..r2`) are preserved in `.local/review/runs.jsonl` and `.local/review/logs/`.

| Run Label | Pin | Command / Scope | Exit | Result | Duration (s) | Alloc Peak (B) | Apparent Peak (B) | Inodes | Cgroup `memory.current` Peak (B) | Cgroup `memory.peak` (B) | Tasks / Pids Peak |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `typecheck-72af-r1` | `72af225` | `tsc --noEmit` | `0` | `0` errors | `3.581` | `491,520` | `478,963` | `7` | `451,018,752` | `457,056,256` | `7 / 17` |
| `composed-72af-r1` | `72af225` | `vitest run -c test/vitest.conversation-links-pings.config.ts` | `0` | `9 / 9` PASS | `13.621` | `16,199,680` | `16,129,890` | `56` | `720,801,792` | `721,965,056` | `28 / 42` |
| `probe-72af-r1` | `72af225` | Initial F1/F2 exploratory probe | `0` | `2 / 2` PASS | `7.400` | `1,163,264` | `1,110,357` | `37` | `678,764,544` | `721,965,056` | `28 / 42` |
| `links-72af-r1` | `72af225` | `vitest run -c test/vitest.conversation-links.config.ts` | `0` | `19 / 19` PASS (`8`+`11`) | `17.768` | `8,744,960` | `8,663,379` | `70` | `726,351,872` | `727,486,464` | `28 / 43` |
| `probe-72af-r2` | `72af225` | Exploratory probe (`--silent=false`) | `0` | `2 / 2` PASS | `11.801` | `1,409,024` | `1,334,320` | `46` | `717,877,248` | `727,486,464` | `28 / 43` |
| `probe-72af-r3` | `72af225` | Intentional diagnostic assertion dump (`diagA`/`diagB` for F1) | `1` | Diagnostic trace captured | `15.110` | `2,043,904` | `1,962,843` | `49` | `728,629,248` | `731,115,520` | `28 / 43` |
| `adversarial-72af-r1..r5` | `72af225` | Iterative fixture schema/token/clock alignment on `.local/review/m4-m5-adversarial.probe.test.ts` (5 runs preserved) | `1` | Tests 3–4 (F1/F2) PASS; fixture adjustments in Tests 1–2 | `9.91–22.03` | `3,358,720` (max) | `3,239,149` (max) | `69` | `724,492,288` | `731,115,520` | `28 / 43` |
| `adversarial-72af-r6` | `72af225` | `.local/review/m4-m5-adversarial.probe.test.ts` (Tests 1–4) | `0` | `4 / 4` PASS (F1 & F2 reproduced) | `22.400` | `3,444,736` | `3,304,728` | `75` | `744,574,976` | `745,132,032` | `28 / 43` |
| `typecheck-f6ec-r1` | `f6ec420` | `tsc --noEmit` | `0` | `0` errors | `14.024` | `712,704` | `582,842` | `55` | `508,743,680` | `745,132,032` | `6 / 43` |
| `py-jobs-f6ec-r1` | `f6ec420` | `python3 -m unittest host/tests/test_jobs.py` | `0` | `56 / 56` PASS | `11.449` | `1,040,384` | `790,963` | `135` | `307,339,264` | `745,132,032` | `7 / 43` |
| `composed-f6ec-r1` | `f6ec420` | `test/conversation-links-pings.test.ts` (before config `include` expansion) | `1` | Config `include` filter miss | `7.177` | `729,088` | `587,245` | `59` | `602,021,888` | `745,132,032` | `14 / 43` |
| `adversarial-f6ec-r1..r2` | `f6ec420` | Test 5 role-offer idempotency date prefix alignment (2 runs preserved) | `1` | Tests 1–4 PASS; Test 5 offer key aligned | `12.36–15.92` | `4,378,624` (max) | `4,196,839` (max) | `91` | `755,822,592` | `760,250,368` | `28 / 44` |
| `adversarial-f6ec-r3` | `f6ec420` | `.local/review/m4-m5-adversarial.probe.test.ts` (Tests 1–5: M4/M5 + F1/F2 + M7 combined Hub) | `0` | `5 / 5` PASS | `10.065` | `4,497,408` | `4,295,741` | `94` | `761,741,312` | `762,060,800` | `28 / 44` |
| `composed-f6ec-r2` | `f6ec420` | `test/conversation-links-pings.test.ts` | `0` | `9 / 9` PASS | `17.360` | `16,551,936` | `16,306,033` | `109` | `790,216,704` | `791,486,464` | `28 / 44` |
| `role-leases-f6ec-r1` | `f6ec420` | `test/role-leases.test.ts -t "unresolved\|acquires\|naked\|snapshot\|rolls back"` | `0` | `9 / 9` PASS (`21` skipped) | `12.654` | `6,971,392` | `6,748,003` | `112` | `771,641,344` | `791,486,464` | `28 / 44` |
| `role-leases-f6ec-r2` | `f6ec420` | `test/role-leases.test.ts` remaining `21` tests | `0` | `21 / 21` PASS (`9` skipped; `30 / 30` total) | `17.558` | `15,155,200` | `14,889,705` | `154` | `815,656,960` | `816,640,000` | `28 / 44` |

- **Overall Reviewer Scratch Peaks** (`.local/review` + `.local/tmp` including directory inodes):
  - **Peak allocated**: `16,551,936` B (`15.79 MiB`) — below the `20 MiB` (`20,971,520` B) early guard and `25 MiB` (`26,214,400` B) hard cap (`guard_triggered: false` on all `21` runs).
  - **Peak apparent**: `16,306,033` B (`15.55 MiB`), `154` peak inodes.
- **Overall Containment Cgroup Peaks**:
  - `memory.current` peak: `815,656,960` B (`777.87 MiB`); `memory.peak`: `816,640,000` B (`778.81 MiB` vs `1,500 MiB` limit).
  - `pids.peak`: `44` (vs `256` limit); process-group task peak: `28`.

---

## 5. Retained Evidence Locators

- **Tracked review report**: `docs/reviews/CONVERSATION-LINKS-PINGS.md`
- **Retained reviewer private evidence (gitignored under `.local/review/`)**:
  - `.local/review/m4-m5-adversarial.probe.test.ts` (`5` tests: streamed-body authority fencing, current-slot replay fence & non-conversation backlog, Finding F1 reproduction, Finding F2 reproduction, and combined M4+M5+M7 `Hub` verification)
  - `.local/review/links-pings-probe.test.ts` (initial diagnostic probe capturing `diagA` and `diagB` in `probe-72af-r3`)
  - `.local/review/vitest.adversarial.config.ts` and `.local/review/vitest.probe.config.ts`
  - `.local/review/manifest-verification.json` (`72af225` 214-pair and 228-pair manifest verification)
  - `.local/review/combined-manifest-verification.json` (`f6ec420` 217-pair manifest and M4/M5 byte-identity verification)
  - `.local/review/runs.jsonl` and `.local/review/logs/*.json` / `*.log` (`21` measured runs)
