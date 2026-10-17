# Agent Hub Conversations API & Integration Contract (M1)

> **Status**: Candidate domain service (`src/conversations.ts`) and HTTP route contract for Milestone M1. Production route wiring in `src/hub.ts` and `src/worker.ts` is reserved for head integration after independent review.

## 1. Overview & Architecture

The Conversations service (`ConversationsService`) provides durable, multi-party human and agent conversations backed by Durable Object SQLite storage (`SqlStorage`).

Key design invariants:
1. **Dedicated Durable Store & O(1) Free-Plan Accounting (No Unproven Dual-Write)**: Conversations and their messages live in dedicated SQLite tables (`conversations`, `conversation_members`, `conversation_messages`, `conversation_storage_usage`) with per-conversation monotonic sequences (`seq`). Message `request_json` stores compact metadata (`kind`, `reply_to`, `reply_to_seq`, `bridge_ref`, `body_bytes`) without duplicating `body`. Messages accept an optional `bridge_ref` field to record native aplexer mailbox envelope IDs without dual-writing to the legacy bus table.
2. **Verified Server-Side Identity & Disjoint Namespaces (F1, F3, F4, F7)**:
   - **Human identity (`formatHumanActorFromSub` vs `parseCanonicalHumanActor`)**: Derived strictly from verified `SessionData.sub` (`getSession(env, request)`) via injective encoding `formatHumanActorFromSub(sub)` -> `human:<sub_id>`. Raw subject encoding never trims whitespace and never strips an existing `human:` prefix, so distinct verified subjects (`"alice"` -> `"human:alice"` vs `"human:alice"` -> `"human:human:alice"`) never collide, and whitespace/control characters fail closed (`403 invalid_session_subject`). Separate canonical address parsing (`parseCanonicalHumanActor`) requires an exact `human:` prefix. Token-admin sessions (`/auth/token`, `email === 'admin-token@hub.local'`) share their single deterministic operator subject (`human:admin-token:<hash16>`); separate human users are never fabricated from a shared token. The collapsed bus actor `'admin'` from `authenticateBusRequest` is **refused** (`403 collapsed_admin_identity_refused`).
   - **Agent identity & disjoint namespaces**: Derived strictly from a verified `credentials` row with `kind = 'agent'`, resolved against registered `agents`. Reserved namespace segments (`human`, `role`, `machine`, `routing-client`, `admin`, `root`, `principal`, `system`, `hub`, `schedule`) are rejected in both `project` and `tag` across every resolver, directory listing, principal verifier, and membership check (`isDisjointAgentActor`). Final agent and human actor namespaces are strictly disjoint, and `requireActiveMembership` enforces both `actor` and `kind` (`member.kind === principal.kind`). Whenever an agent registration has a `project`, its canonical conversation identity is **always** project-qualified (`<project>:<tag>`); bare `<tag>` is used only for projectless agent rows, any bare `<tag>` shared by multiple projects is **rejected as ambiguous** (`403` / `404`), and cross-project mismatches (`projB:tag` when `tag` is registered in `projA`) fail closed.
   - **Current shared `agents` schema limitation (F7)**: While `ConversationsService` enforces project-qualified `<project>:<tag>` identities and fails closed on cross-project ambiguity, the shared base table `agents` in `src/schema.ts` currently declares `tag TEXT PRIMARY KEY` and `POST /api/agents` in `src/hub.ts` returns `409 Conflict` when registering the same `tag` under a second project. Coordinated migration of `src/schema.ts` / `src/hub.ts` (`UNIQUE(project, tag)` with project-aware `/api/agents` lookup) is tracked for shared-scope handoff.
   - **Live post-body credential & session revalidation (F3)**: HTTP routes enforce initial authentication and CSRF disposition before reading the request body, and every service method re-invokes `verifyPrincipal` **inside** the synchronous SQLite transaction (`this.runInTxn(...)`) after all body `await` points complete—re-querying live `credentials` (`revoked_at IS NULL`, `expires_at > now`, live `kind`, `actor`, `scopes`) and `session.exp` / `users.is_admin` with zero await gap before SQLite effects.
   - **Refused credential kinds**: `host` credentials, `job` credentials, `routing:submit`-only client tokens (`routing-client:*`), `role:root`, and revoked/expired tokens are refused (`403 forbidden` / `401 unauthorized`) before any content access.
   - **No JSON or Header Impersonation**: Supplying `from`, `from_actor`, `sender`, `sender_actor`, or `author` in request JSON returns `400 sender_override_forbidden`. Unverified request headers (`X-Actor`, `X-User`, etc.) are ignored by the route handler.
3. **Strict Membership Isolation & Replay ACL (F2)**:
   - An authenticated human admin session is **not** blanket authorization to read or send in other humans' or agents' conversations. Every conversation read, message list, message send, read-cursor update, and **`createConversation` idempotency replay** requires the caller to be a current, non-removed member (`removed_at IS NULL`) of that conversation inside the transaction.
4. **CSRF Enforcement**:
   - The route handler accepts an already-verified principal and an explicit `csrfVerified: boolean` disposition.
   - Cookie/session-authenticated principals (`authType === 'session'`) **must** have `csrfVerified === true` (via `verifyCsrf(request, session)`) on all mutating HTTP methods (`POST`, `PUT`, `PATCH`, `DELETE`). Missing or invalid CSRF is rejected with `403 { "error": "csrf_invalid" }` before body parsing or storage access.
5. **Injected Identity Validator**:
   - Membership targets (`members` on creation or `POST /api/conversations/:id/members`) are validated through an injected `IdentityValidator` interface. The default SQLite validator (`createSqlIdentityValidator(sql)`) accepts only registered non-reserved agents in `agents` (excluding `root` role, reserved namespaces, and ambiguous bare tags), registered humans in `users` (`human:<sub_id>`), and the caller's own verified principal identity. Unknown or fabricated actors are refused (`404 unknown_participant`).

---

## 2. Canonical Actor Grammar

| Participant Kind | Canonical Format | Examples | Resolution Rules |
|---|---|---|---|
| Human | `human:<sub_id>` | `human:user-abc-123`, `human:admin-token:0123456789abcdef` | Encoded injectively from verified `SessionData.sub` via `formatHumanActorFromSub(sub)` (`^human:[A-Za-z0-9._:-]{1,120}$`, no trimming, no prefix stripping). Canonical target strings are parsed via `parseCanonicalHumanActor`. Bare `'admin'`, `'human:admin'`, `'root'`, or `'principal'` is rejected. |
| Agent | `<project>:<tag>` (when `project` is present) or `<tag>` (when `project` is null) | `projA:worker-1`, `standalone-bot` | Resolved against `agents` table. Both `project` and `tag` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` and must **not** match any reserved namespace (`human`, `role`, `machine`, `routing-client`, `admin`, `root`, `principal`, `system`, `hub`, `schedule`). Whenever `project` is non-empty, canonical identity is **always** `<project>:<tag>`. An unambiguous bare `<tag>` resolves to `<project>:<tag>`, whereas a bare `<tag>` matching more than one project is **rejected as ambiguous**. |

---

## 3. Input, Free-Plan Storage & Retention Bounds (F6, F10–F16)

| Bound | Default Value | Behavior / Error Code on Violation | Status |
|---|---|---|---|
| Max HTTP JSON request body | `32,768` bytes (32 KiB) | `body_too_large` | `413` |
| Max message `body` UTF-8 size | `16,384` bytes (16 KiB), non-empty | `body_too_large` / `invalid_body` | `413` / `400` |
| Conversation `title` (`name`) | `1..120` chars, no control chars | `invalid_title` | `400` |
| Conversation `kind` | `'direct'` or `'group'` | `invalid_conversation_kind` | `400` |
| Direct conversation active members | Exactly `2` distinct participants (creator + 1) | `invalid_direct_members` / `direct_conversation_member_limit` | `400` / `409` |
| Group conversation active members | `1..64` distinct participants | `member_limit_exceeded` / `invalid_members` | `409` / `400` |
| `idempotency_key` | `1..128` ASCII chars (`^[A-Za-z0-9._:-]{1,128}$`) | `invalid_idempotency_key` | `400` |
| `bridge_ref` | Optional, `1..128` ASCII chars (`^[A-Za-z0-9._:-]{1,128}$`) | `invalid_bridge_ref` | `400` |
| Pagination `limit` | Integer `1..100` (default `50`) | `invalid_limit` | `400` |
| Max retained conversations (`maxTotalConversations`) | `1,000` (active + archived until pruned) | `storage_limit_exceeded` (`conversation_limit_exceeded`) | `409` |
| Max retained messages per conversation (`maxMessagesPerConversation`) | `5,000` | `storage_limit_exceeded` (`conversation_message_limit_exceeded`) | `409` |
| Max total retained messages (`maxTotalMessages`) | `50,000` | `storage_limit_exceeded` (`total_message_limit_exceeded`) | `409` |
| Max retained message bytes per conversation (`maxBytesPerConversation`) | `8,388,608` bytes (8 MiB) | `storage_limit_exceeded` (`conversation_byte_limit_exceeded`) | `409` |
| Max total retained message bytes (`maxTotalMessageBytes`) | `67,108,864` bytes (64 MiB) | `storage_limit_exceeded` (`total_byte_limit_exceeded`) | `409` |
| Max retained removed-member tombstones per conversation (`maxRemovedMembersPerConversation`) | `128` | Evicts oldest removed-member tombstone in transaction so authorized revocation never fails (`F10`) | `200` |
| Max total retained removed-member tombstones (`maxTotalRemovedMembers`) | `2,048` | Evicts oldest removed-member tombstone in transaction so authorized revocation never fails (`F10`) | `200` |
| Retention window (`messageRetentionMs`, `removedMemberRetentionMs`, `archivedConversationRetentionMs`) | `30` days (`2,592,000,000` ms), global budget `100` rows per tick/alarm (`F14`) | Pruned in bounded chunks via inherited `scheduledTick` / `alarm` | — |

- **O(1) Transactional Accounting & Versioned Backfill (`F6`, `F16`)**: Global counts (`conversation_count`, `message_count`, `message_bytes`, `removed_member_count`, `schema_version`) are maintained in `conversation_storage_usage` (`singleton = 1`) alongside per-conversation counters (`retained_message_count`, `retained_message_bytes`, `removed_member_count`) on `conversations` and `body_bytes` on `conversation_messages`, avoiding full-table scans on message admission. `schema_version` must be a finite supported integer in `0..CONVERSATIONS_SCHEMA_VERSION` (`2`). Missing usage rows and versions `0`/`1` atomically backfill `conversation_messages.body_bytes`, per-conversation counters/previews, and global `conversation_storage_usage` inside `runInTxn` before any mutations or cleanup execute. Malformed values (`NaN`, non-integers, non-numeric text) and unsupported versions (`> 2`) fail closed from `init()` and never trust stored counters.
- **Tombstone Caps Never Block Revocation (`F10`)**: Reaching `maxRemovedMembersPerConversation` (`128`) or `maxTotalRemovedMembers` (`2,048`) never blocks an owner from revoking an active member. Inside the synchronous removal transaction, `evictRemovedMemberTombstonesForRevocation` evicts the oldest removed-member tombstone(s) (preferring non-creator tombstones first) to keep tombstone storage within the cap while immediately revoking the target member. In addition, `requireActiveMembership` returns `403 { "error": "forbidden", "reason": "member_removed" }` whenever `!member && conversation.created_by === actor`, preserving the F2 creator-replay fence even if the creator's tombstone was evicted or cleaned up.
- **Failed Admission Rollback & Retained Child Reply Replay (`F6`, `F12`)**: Admission checks run after idempotency replay lookup and before `last_seq` advancement inside `this.runInTxn(...)`. A rejected message never advances `last_seq`. Idempotent retries are matched against retained `(conversation_id, sender_actor, idempotency_key)` *before* resolving `reply_to` / `reply_to_seq` in `conversation_messages`, so retrying an already-stored child reply returns `200 OK` (`replay: true`) even after its parent message has expired and been pruned.
- **Global Affected-Row Cleanup Budget & Preview Hygiene (`F14`, `F15`)**: `BusService.retentionCleanup` consumes one shared remaining-row budget across expired bus `messages` (`expires_at <= now`) and expired bus `claims` (`expires_at < now`). `ConversationHub` then shares that same caller `limit` across bus rows and conversation rows (`conversation_messages`, `conversation_members`, archived `conversations` headers) so `messagesDeleted + claimsDeleted + conversationRowsDeleted <= limit`. A one-row-per-kind SQLite fairness cursor (`retention_cleanup_cursor`) rotates independently at each nested level (bus messages vs claims, bus vs conversation rows, and archived children vs removed-member tombstones vs ordinary expired messages) and survives Durable Object / service reconstruction. `ConversationHub` runs that composed pass, nested child deletions, and every selection cursor inside one `ctx.storage.transactionSync`; child services join the open transaction rather than committing separately, so a later cursor or row abort rolls back all successful work. When the remaining budget is smaller than the number of eligible categories, each of the first `budget` eligible categories in rotated order receives 1 row, so `limit = 1` still makes bounded progress while other categories are replenished. When the budget covers every eligible category, equal slices run in that rotated order and leftover budget may be `0` (no forced deletion after the allowance is exhausted). `hasMore` is true when any unfinished category still has expired rows, including when the current call's allowance is zero. Inherited `Hub.prototype.scheduledTick === BaseHub.prototype.scheduledTick` and `Hub.prototype.alarm === BaseHub.prototype.alarm` are unchanged. Within `conversationsService.retentionCleanup`, `limit` bounds the **total number of SQLite rows deleted** across child messages, child members, archived conversation headers, expired removed members, and expired messages (`rowsDeleted <= limit`). Expired archived conversations (`archived_at <= cutoff`) are immediately hidden from `getConversation` (`404 not_found`), `listConversations`, `listMessages`, and mutations while their child rows are purged in indexed chunks, and each `conversations` header row is deleted only after all of its child rows are gone. Whenever expired messages are pruned from a conversation, `last_message_preview` is recomputed from the newest remaining retained message (or cleared to `null` when no retained messages remain), ensuring expired message content never lingers in conversation details or listings.

---

## 4. HTTP Endpoints (`/api/conversations`)

### 4.1 List Registered Participants / Identities
`GET /api/conversations/participants` (alias: `GET /api/conversations/identities`)

Returns the caller's verified identity (`self`) and all registered human and agent identities eligible for conversation membership. Used by the UI and CLI to pick valid targets without fabricating identities.

- **Auth**: Verified human session or agent credential with `bus:read` scope.
- **Response `200 OK`**:
```json
{
  "self": {
    "actor": "human:user-123",
    "kind": "human",
    "display_name": "admin@fake.local"
  },
  "participants": [
    {
      "actor": "human:user-123",
      "kind": "human",
      "display_name": "admin@fake.local"
    },
    {
      "actor": "projA:worker-1",
      "kind": "agent",
      "display_name": "projA:worker-1",
      "project": "projA",
      "role": "worker",
      "machine_id": "machine-A"
    }
  ]
}
```

---

### 4.2 Create Conversation
`POST /api/conversations`

Creates a named `direct` or `group` conversation. The authenticated caller is automatically added as an active member with `role: "owner"`. All listed `members` (or `participants`) are validated through the identity validator and added with `role: "member"` (unless the entry is the creator, who remains `"owner"`).

- **Auth**: Verified human session (with valid `X-CSRF-Token`) or agent credential with `bus:send` scope.
- **Request Body**:
```json
{
  "kind": "group",
  "title": "Platform M1 Coordination",
  "members": ["projA:worker-1", "human:user-456"],
  "idempotency_key": "create-conv-001"
}
```
  - `kind`: `"direct" | "group"` (required).
  - `title` (or alias `name`): non-empty string `1..120` chars (required).
  - `members` (or alias `participants`): string array of participant actor IDs. For `"direct"`, total distinct members including the creator must be `2`. For `"group"`, total distinct members including the creator must be `1..64`.
  - `idempotency_key` (optional): `1..128` chars. Repeating the same `(creator_actor, idempotency_key)` first revalidates that the caller is still an **active member** (`removed_at IS NULL`) of the existing conversation inside the transaction (`403 member_removed` if removed), then verifies identical canonical parameters (`200 OK` with `"replay": true`, or `409 Conflict` on mismatch).
- **Response `201 Created`** (or `200 OK` on idempotent replay):
```json
{
  "id": "01JABCDEF0123456789ABCDEFG",
  "kind": "group",
  "title": "Platform M1 Coordination",
  "name": "Platform M1 Coordination",
  "created_by": "human:user-123",
  "created_at": "2026-10-17T00:25:00.000Z",
  "updated_at": "2026-10-17T00:25:00.000Z",
  "archived_at": null,
  "archived_by": null,
  "last_seq": 0,
  "last_message_at": null,
  "last_message_preview": null,
  "my_role": "owner",
  "my_read_seq": 0,
  "unread_count": 0,
  "member_count": 3,
  "members": [
    {
      "actor": "human:user-123",
      "kind": "human",
      "role": "owner",
      "added_at": "2026-10-17T00:25:00.000Z",
      "added_by": "human:user-123",
      "removed_at": null,
      "removed_by": null,
      "last_read_seq": 0,
      "last_read_at": null
    },
    {
      "actor": "human:user-456",
      "kind": "human",
      "role": "member",
      "added_at": "2026-10-17T00:25:00.000Z",
      "added_by": "human:user-123",
      "removed_at": null,
      "removed_by": null,
      "last_read_seq": 0,
      "last_read_at": null
    },
    {
      "actor": "worker-1",
      "kind": "agent",
      "role": "member",
      "added_at": "2026-10-17T00:25:00.000Z",
      "added_by": "human:user-123",
      "removed_at": null,
      "removed_by": null,
      "last_read_seq": 0,
      "last_read_at": null
    }
  ],
  "replay": false,
  "conversation": { "...same fields as top-level..." }
}
```

---

### 4.3 List Own Conversations
`GET /api/conversations`

Lists conversations where the authenticated caller is currently an **active** (`removed_at IS NULL`) member, ordered by `updated_at DESC, id DESC`. Non-members and removed members never see the conversation.

- **Auth**: Verified human session or agent credential with `bus:read` scope.
- **Query Parameters**:
  - `state` (optional): `"active"` (default), `"archived"`, or `"all"`.
  - `include_archived` (optional): `"true"` | `"false"`. When `"true"`, equivalent to `state=all`.
  - `limit` (optional): `1..100` (default `50`).
  - `cursor` (optional): pagination cursor (`<updated_at>|<id>`) from previous page's `next_cursor`.
- **Response `200 OK`**:
```json
{
  "conversations": [
    {
      "id": "01JABCDEF0123456789ABCDEFG",
      "kind": "group",
      "title": "Platform M1 Coordination",
      "name": "Platform M1 Coordination",
      "created_by": "human:user-123",
      "created_at": "2026-10-17T00:25:00.000Z",
      "updated_at": "2026-10-17T00:26:00.000Z",
      "archived_at": null,
      "archived_by": null,
      "last_seq": 2,
      "last_message_at": "2026-10-17T00:26:00.000Z",
      "last_message_preview": "Ready for integration",
      "my_role": "owner",
      "my_read_seq": 1,
      "unread_count": 1,
      "member_count": 3,
      "members": [ "...active member objects..." ]
    }
  ],
  "next_cursor": null
}
```

---

### 4.4 Get Conversation Details
`GET /api/conversations/:id`

Returns metadata, caller's read cursor/unread count, and active members for conversation `:id`.

- **Auth**: Active member of `:id` (human session or agent with `bus:read`).
- **Errors**:
  - `404 { "error": "not_found" }` if conversation `:id` does not exist.
  - `403 { "error": "forbidden", "reason": "member_removed" }` if caller was removed.
  - `403 { "error": "forbidden", "reason": "not_a_member" }` if caller is not a member.
- **Response `200 OK`**: Same shape as 4.2 (top-level conversation fields + `"conversation"` wrapper).

---

### 4.5 List Conversation Members
`GET /api/conversations/:id/members`

- **Auth**: Active member of `:id` (human session or agent with `bus:read`).
- **Query Parameters**:
  - `include_removed` (optional): `"true"` | `"false"` (default `"false"`). When `"true"`, includes removed members (`removed_at !== null`) for audit/history visibility.
- **Response `200 OK`**:
```json
{
  "conversation_id": "01JABCDEF0123456789ABCDEFG",
  "members": [
    {
      "actor": "human:user-123",
      "kind": "human",
      "role": "owner",
      "added_at": "2026-10-17T00:25:00.000Z",
      "added_by": "human:user-123",
      "removed_at": null,
      "removed_by": null,
      "last_read_seq": 2,
      "last_read_at": "2026-10-17T00:26:10.000Z"
    }
  ]
}
```

---

### 4.6 Add Member (Owner Authority Required)
`POST /api/conversations/:id/members`

Adds a registered identity, updates an active member's role, or restores a previously removed member in a `group` conversation:
- **Active-member upsert (`F5`)**: If the target is already an active member (`removed_at === null`), omitting `role` preserves their existing role (`owner` stays `owner`, `member` stays `member`). Any transition from `role: "owner"` to `role: "member"` enforces the **last-active-owner invariant** inside the transaction: if the target is the sole active owner of `:id`, the demotion is rejected with `409 { "error": "cannot_remove_last_owner" }`.
- **Consistent Join-at-Current-Watermark Policy (`F8`, `F13`)**: Both newly added members (`!existing`, including members re-added after their removed-member tombstone was pruned or evicted) and restored removed-member tombstones (`existing` with `removed_at !== null`) set `last_read_seq = Math.max(prev_read_seq, conversation.last_seq)` on join/re-join. Consequently, messages sent prior to a member's join or during their absence never appear in `unread_count` (`unread_count === 0` immediately on join/re-join), and tombstone cleanup or eviction never resets a re-added member's read cursor to `0`. History read ACL remains separate from `unread_count`: active members can still paginate retained message history via `GET /api/conversations/:id/messages`.

- **Auth**: Active member of `:id` with `role === "owner"` (human session with CSRF, or agent owner with `bus:send`).
- **Request Body**:
```json
{
  "actor": "projB:worker-2",
  "role": "member"
}
```
  - `actor` (or alias `member`): target participant identifier (validated via `IdentityValidator`).
  - `role` (optional): `"member"` or `"owner"`. Defaults to `"member"` for new/re-added members; preserves existing role when omitted on active members.
- **Errors**:
  - `403 { "error": "forbidden", "reason": "owner_required" }` if caller is not an active owner of `:id`.
  - `404 { "error": "unknown_participant" }` if target is not a registered/valid identity.
  - `409 { "error": "cannot_remove_last_owner" }` if demoting the last remaining active owner of `:id`.
  - `409 { "error": "conversation_archived" }` if conversation is archived.
  - `409 { "error": "direct_conversation_member_limit" }` if conversation `kind === "direct"`.
  - `409 { "error": "member_limit_exceeded" }` if active members would exceed `64`.
- **Response `200 OK` / `201 Created`**:
```json
{
  "conversation_id": "01JABCDEF0123456789ABCDEFG",
  "member": {
    "actor": "projB:worker-2",
    "kind": "agent",
    "role": "member",
    "added_at": "2026-10-17T00:27:00.000Z",
    "added_by": "human:user-123",
    "removed_at": null,
    "removed_by": null,
    "last_read_seq": 2,
    "last_read_at": "2026-10-17T00:27:00.000Z"
  }
}
```

---

### 4.7 Remove Member (Owner Authority Required)
`DELETE /api/conversations/:id/members/:actor`  
(Alternative POST route: `POST /api/conversations/:id/members/remove` with body `{ "actor": "<actor>" }`)

Marks a member as removed (`removed_at`, `removed_by`). Removed members immediately lose read and write access to the conversation.
- **Guaranteed Immediate Revocation Under Tombstone Caps (`F10`)**: If the conversation (`128`) or global (`2,048`) removed-member tombstone cap is full when an owner removes a member, the service evicts the oldest removed-member tombstone(s) (preferring non-creator tombstones first) inside the same synchronous transaction and records the new removal tombstone. Authorized member revocation never fails with `409 storage_limit_exceeded`.

- **Auth**: Active member of `:id` with `role === "owner"`.
- **Errors**:
  - `403 { "error": "forbidden", "reason": "owner_required" }` if caller is not an active owner.
  - `404 { "error": "member_not_found" }` if target is not an active member.
  - `409 { "error": "cannot_remove_last_owner" }` if target is the last remaining active owner.
  - `409 { "error": "conversation_archived" }` if conversation is archived.
- **Response `200 OK`**:
```json
{
  "conversation_id": "01JABCDEF0123456789ABCDEFG",
  "actor": "projB:worker-2",
  "status": "removed",
  "removed_at": "2026-10-17T00:28:00.000Z",
  "removed_by": "human:user-123"
}
```

---

### 4.8 Send Message (Idempotent & Atomic Per-Conversation Sequence)
`POST /api/conversations/:id/messages`

Appends a message to conversation `:id` with monotonically increasing per-conversation `seq` (`1, 2, 3, ...`). Automatically advances the sender's own `last_read_seq` to at least the new message's `seq`.
- **Retained Child Reply Replay After Parent Pruning (`F12`)**: Inside the synchronous transaction, after revalidating caller authentication, active membership, and non-archived conversation status, `sendMessage` checks for an existing retained message matching `(conversation_id, sender_actor, idempotency_key)` *before* resolving `reply_to` / `reply_to_seq` against live `conversation_messages`. If a retained message exists and the request's canonical payload (`body`, `kind`, `bridge_ref`, and `reply_to` / `reply_to_seq` supplied by ID, seq, or both) matches `existing`, the stored message is replayed (`200 OK`, `"replay": true`) even if the parent message has since expired and been pruned. Conflicting retries with the same `idempotency_key` return `409 { "error": "conflict", "reason": "idempotency_conflict" }`, and genuinely new messages referencing a pruned or non-existent parent return `400 { "error": "invalid_reply_to" }`.

- **Auth**: Active member of `:id` (human session with CSRF, or agent member with `bus:send`).
- **Request Body**:
```json
{
  "body": "Milestone M1 contract is ready for CLI and UI alignment.",
  "idempotency_key": "msg-key-001",
  "kind": "message",
  "reply_to": "01JABCDEF99999999999999999",
  "reply_to_seq": 1,
  "bridge_ref": "native-env-001"
}
```
  - `body`: non-empty string, max `16,384` UTF-8 bytes.
  - `idempotency_key`: required string `1..128` chars (`^[A-Za-z0-9._:-]{1,128}$`). Scoped per `(conversation_id, sender_actor, idempotency_key)`.
  - `kind` (optional): `"message"` (default), `"note"`, `"reply"`, `"status"`, `"handoff"`, or `"action"`.
  - `reply_to` (optional): message `id` within the **same** conversation.
  - `reply_to_seq` (optional): integer `seq` within the **same** conversation. If both `reply_to` and `reply_to_seq` are provided, they must refer to the same parent message in `:id`.
  - `bridge_ref` (optional): string `1..128` chars recording the originating native aplexer message ID when relayed by the opt-in host bridge.
  - **Forbidden fields**: `from`, `from_actor`, `sender`, `sender_actor`, `author` -> rejected with `400 { "error": "sender_override_forbidden" }`. Any other unrecognized field -> rejected with `400 { "error": "unknown_fields" }`.
- **Errors**:
  - `400 { "error": "invalid_reply_to" }` if `reply_to` / `reply_to_seq` does not exist in conversation `:id` (including if it belongs to a different conversation or was pruned before a new message was sent).
  - `403 { "error": "forbidden", "reason": "not_a_member" | "member_removed" | "scope_missing" }`.
  - `409 { "error": "conflict", "reason": "idempotency_conflict" }` if `(conversation_id, sender_actor, idempotency_key)` was already used with a different `body`, `kind`, `reply_to`, or `bridge_ref`.
  - `409 { "error": "storage_limit_exceeded" }` if retained per-conversation or global message/byte bounds are exceeded (rolls back without advancing `last_seq`).
  - `409 { "error": "conversation_archived" }` if conversation is archived.
  - `413 { "error": "body_too_large" }` if `body` exceeds 16,384 UTF-8 bytes.
- **Response `201 Created`** (or `200 OK` with `"replay": true` on idempotent retry):
```json
{
  "id": "01JABCDEF11111111111111111",
  "conversation_id": "01JABCDEF0123456789ABCDEFG",
  "seq": 2,
  "sender_actor": "human:user-123",
  "sender_kind": "human",
  "kind": "message",
  "body": "Milestone M1 contract is ready for CLI and UI alignment.",
  "idempotency_key": "msg-key-001",
  "reply_to": "01JABCDEF99999999999999999",
  "reply_to_seq": 1,
  "bridge_ref": "native-env-001",
  "created_at": "2026-10-17T00:26:00.000Z",
  "replay": false,
  "message": { "...same fields..." }
}
```

---

### 4.9 List / Poll Paginated Messages (History, Catch-Up & Gap Detection)
`GET /api/conversations/:id/messages`

Returns messages in conversation `:id` ordered deterministically by `seq ASC`. Reading messages does **not** implicitly mutate the caller's read cursor (`my_read_seq`). Active members may read full retained message history even when the conversation is archived (until the archived conversation itself expires).
- **Retained-Row Catch-Up & Gap Policy (`F11`)**:
  - `oldest_retained_seq` and `newest_retained_seq` report the minimum and maximum `seq` currently retained in `conversation_messages` (`null` when 0 messages are retained).
  - `pruned_through_seq` is the contiguous-prefix watermark (`oldest_retained_seq - 1` when the oldest retained sequence is `> 1`, `last_seq` when no messages remain, or `0` when sequence 1 is still retained). It is **not** the highest arbitrary removed sequence: same-millisecond ID ordering can delete an interior `seq` while keeping a lower one, so this watermark never advances past a retained message.
  - `has_more_after` is computed strictly from retained rows (`newest_seq !== null && newest_retained_seq !== null && newest_seq < newest_retained_seq`) and is **never `true` on an empty page**.
  - `next_after_seq` always advances past pruned gaps (`newest_seq ?? (after_seq !== undefined ? Math.max(after_seq, last_seq) : pruned_through_seq)`), so polling clients never stall on an unretained sequence cursor.
  - `gap_detected: true` when any sequence in the requested window is missing from retained rows (prefix, interior, or suffix holes). `gap_from_seq` / `gap_to_seq` describe the first missing contiguous range in that window. `resume_after_seq` stays the prefix watermark so catch-up never skips a retained sequence.
  - `unread_count` is the count of retained messages with `seq > my_read_seq` (`0` when none remain). Missing/pruned sequences are never counted as unread. List, detail, message, and read-receipt responses share this formula.

- **Auth**: Active member of `:id` (human session or agent with `bus:read`).
- **Query Parameters**:
  - `after_seq` (alias `since_seq`): integer `>= 0`. Returns up to `limit` messages with `seq > after_seq` in `seq ASC` order. Used for incremental polling, reconnect catch-up, and CLI bridge polling.
  - `before_seq`: integer `>= 1`. Returns the latest `limit` messages with `seq < before_seq`, ordered `seq ASC`. Used by the UI for "load older messages".
  - `limit`: integer `1..100` (default `50`).
  - *(Note: `after_seq` and `before_seq` are mutually exclusive; supplying both returns `400 invalid_pagination`.)*
  - When neither `after_seq` nor `before_seq` is supplied, returns the most recent `limit` messages in chronological `seq ASC` order.
- **Response `200 OK`**:
```json
{
  "conversation_id": "01JABCDEF0123456789ABCDEFG",
  "self_actor": "projA:worker-1",
  "messages": [
    {
      "id": "01JABCDEF99999999999999999",
      "conversation_id": "01JABCDEF0123456789ABCDEFG",
      "seq": 1,
      "sender_actor": "projA:worker-1",
      "sender_kind": "agent",
      "kind": "message",
      "body": "Initial status report.",
      "idempotency_key": "init-001",
      "reply_to": null,
      "reply_to_seq": null,
      "bridge_ref": null,
      "created_at": "2026-10-17T00:25:30.000Z"
    }
  ],
  "last_seq": 2,
  "my_read_seq": 1,
  "read_seq": 1,
  "unread_count": 1,
  "oldest_seq": 1,
  "newest_seq": 1,
  "oldest_retained_seq": 1,
  "newest_retained_seq": 2,
  "pruned_through_seq": 0,
  "resume_after_seq": 0,
  "gap_detected": false,
  "gap_from_seq": null,
  "gap_to_seq": null,
  "has_more_before": false,
  "has_more_after": true,
  "next_after_seq": 1,
  "prev_before_seq": 1
}
```

---

### 4.10 Update Per-Member Monotonic Read Cursor
`POST /api/conversations/:id/read`

Advances the caller's persisted per-member read cursor (`last_read_seq`). Monotonic: if the requested sequence is less than or equal to the caller's existing `last_read_seq`, the existing cursor is preserved without error.

- **Auth**: Active member of `:id` (human session with CSRF, or agent member with `bus:read` or `bus:send`).
- **Request Body** (optional JSON object):
```json
{
  "seq": 2
}
```
  - Accepts `seq` (or alias `read_seq`): integer `0 <= seq <= conversation.last_seq`.
  - Or `message_id`: string ID of a message within conversation `:id` (resolves to that message's `seq`).
  - If body is empty or `{}` with no `seq`/`message_id`, marks up to `conversation.last_seq`.
- **Errors**:
  - `400 { "error": "invalid_read_seq" }` if `seq` is negative, non-integer, or `> conversation.last_seq`, or if `message_id` does not belong to conversation `:id`.
- **Response `200 OK`**:
```json
{
  "conversation_id": "01JABCDEF0123456789ABCDEFG",
  "actor": "human:user-123",
  "read_seq": 2,
  "last_seq": 2,
  "unread_count": 0,
  "last_read_at": "2026-10-17T00:29:00.000Z"
}
```

---

### 4.11 Archive / Unarchive Conversation (Owner Authority Required)
`POST /api/conversations/:id/archive`  
`POST /api/conversations/:id/unarchive`

Explicit archive and history semantics:
- Archiving sets `archived_at` and `archived_by`.
- While archived:
  - Active members **can** read conversation metadata (`GET /api/conversations/:id`), list archived conversations (`GET /api/conversations?include_archived=true` or `state=archived`), paginate message history (`GET /api/conversations/:id/messages`), and advance read cursors (`POST /api/conversations/:id/read`).
  - Sending messages (`POST /api/conversations/:id/messages`) and changing membership (`POST/DELETE /api/conversations/:id/members`) are refused with `409 { "error": "conversation_archived" }`.
- Unarchiving clears `archived_at` and `archived_by`, restoring full active state with all prior messages, sequences, and read cursors intact.

- **Auth**: Active member of `:id` with `role === "owner"`.
- **Response `200 OK`**: Returns the updated conversation object (same shape as `GET /api/conversations/:id`).

---

## 5. Error & Status Code Summary

| HTTP Status | `error` Code | When Returned |
|---|---|---|
| `400` | `malformed_json` / `invalid_json_object` | Request body is not valid JSON object |
| `400` | `unknown_fields` | Request body contains unrecognized JSON properties |
| `400` | `unknown_query_param` | Request URL contains unrecognized query parameters |
| `400` | `sender_override_forbidden` | Request body attempts to specify `from`, `from_actor`, `sender`, `sender_actor`, or `author` |
| `400` | `invalid_conversation_kind` | `kind` is not `"direct"` or `"group"` |
| `400` | `invalid_title` | `title` / `name` is missing, empty, > 120 chars, or contains control characters |
| `400` | `invalid_members` / `invalid_direct_members` | `members` is not an array, or a `"direct"` conversation does not have exactly 2 distinct participants |
| `400` | `invalid_body` | Message `body` is missing, not a string, or empty |
| `400` | `invalid_idempotency_key` | `idempotency_key` is missing or outside `^[A-Za-z0-9._:-]{1,128}$` |
| `400` | `invalid_kind` | Message `kind` is not in the allowed set |
| `400` | `invalid_reply_to` | `reply_to` / `reply_to_seq` does not exist in the same conversation |
| `400` | `invalid_read_seq` | Read cursor `seq` is negative, non-integer, or `> conversation.last_seq` |
| `400` | `invalid_limit` / `invalid_pagination` / `invalid_cursor` | Invalid `limit`, conflicting `after_seq` + `before_seq`, or malformed `cursor` |
| `401` | `unauthorized` | Missing, expired, or revoked session cookie / bearer token |
| `403` | `csrf_invalid` | Cookie-authenticated mutating request is missing a valid `X-CSRF-Token` / matching `Origin` |
| `403` | `forbidden` (`reason: "collapsed_admin_identity_refused"`) | Caller identity is collapsed `'admin'` without a verified `SessionData.sub` |
| `403` | `forbidden` (`reason: "invalid_principal_kind"`) | Caller credential is `host`, `job`, `routing-client:*`, or `role:root` |
| `403` | `forbidden` (`reason: "scope_missing"`) | Agent credential lacks `bus:read` (for reads) or `bus:send` (for writes) |
| `403` | `forbidden` (`reason: "not_a_member"`) | Caller is not a member of the target conversation |
| `403` | `forbidden` (`reason: "member_removed"`) | Caller was removed from the target conversation |
| `403` | `forbidden` (`reason: "owner_required"`) | Non-owner member attempted to add/remove members or archive/unarchive |
| `404` | `not_found` | Conversation ID or route does not exist |
| `404` | `unknown_participant` | Target member actor is not a registered agent or registered/authenticated human |
| `404` | `member_not_found` | Target member to remove is not an active member of the conversation |
| `405` | `method_not_allowed` | Unsupported HTTP verb for route |
| `409` | `conflict` (`reason: "idempotency_conflict"`) | Same `idempotency_key` reused by the same caller with different payload |
| `409` | `conversation_archived` | Attempted to send a message or change membership in an archived conversation |
| `409` | `direct_conversation_member_limit` | Attempted to add/remove members on a `direct` conversation |
| `409` | `member_limit_exceeded` | Conversation active membership would exceed 64 |
| `409` | `cannot_remove_last_owner` | Attempted to remove or demote the last active owner of a conversation |
| `409` | `storage_limit_exceeded` | Conversation count, message count, message byte, or removed-member churn cap exceeded |
| `413` | `body_too_large` | Request JSON exceeds 32 KiB or message `body` exceeds 16 KiB |

---

## 6. Participant Registration Metadata & M3b Native Mailbox Bridge Requirements

### What M1 Provides Now (`GET /api/conversations/participants`, `GET /api/conversations/:id`, `GET /api/conversations/:id/messages`)
1. **Current Authenticated Conversation Member ID (`self_actor`)**:
   - Returned on `GET /api/conversations` (`self_actor`), `GET /api/conversations/:id` (`self_actor`), `GET /api/conversations/:id/messages` (`self_actor`), and `GET /api/conversations/participants` (`self.actor`).
   - Always uses the canonical project-qualified agent identity (`<project>:<tag>` when `project` is present) or namespaced human identity (`human:<sub_id>`).
2. **Agent Registration Metadata (Not Authoritative Live Session Binding)**:
   - Each agent entry in `GET /api/conversations/participants` and in `conversation.members` includes read-only `native_binding` registration metadata from the `agents` table (`tag`, `project`, `machine_id`, `workspace`, `session_id`).
   - **Important**: The `agents` table holds static/imported registration metadata, **not** an authoritative current native aplexer session UUID or live workspace attestation. It does **not** suffice for delegated bridge authorization. User-supplied JSON can never set or override `native_binding`.
3. **History Sequence & Client Reference (`bridge_ref`)**:
   - Every message in `GET /api/conversations/:id/messages` includes monotonic integer `seq`, `sender_actor` (canonical member ID), `idempotency_key`, and optional `bridge_ref`.
   - `bridge_ref` is strictly an opaque reference string on a message authored by the authenticated caller itself; **`bridge_ref` never grants author impersonation or delegated sender authority**.

### Full M3b Milestone Requirements (Native Mailbox Bridge)
M3b remains a full required milestone (not dropped). While M3a CLI ships direct human/agent conversation operations against M1, full M3b native mailbox bridging requires:
1. **Authoritative Live Native Session Binding**: Verified mapping between a canonical conversation member (`<project>:<tag>`) and a live, host-attested native aplexer session (`native_id` UUID, `tag`, canonical `workspace` from live `fleet_sessions` / local authoritative aplexer session records, never from unverified strings or stale `agents` import rows).
2. **Explicit Delegated Reply Policy**: Any bridge relaying a native aplexer reply into a conversation must prove that the validated native sender session (`from.session_id` + `from.tag` in the canonical workspace) is authorized to author as that conversation member ID; neither `bridge_ref` nor a shared host token can impersonate an arbitrary member without server-verified session binding.
3. **Idempotent Cursor & Envelope Mapping**: Durable mapping between conversation `(conversation_id, seq)` and native aplexer message UUIDs so reconnects neither re-deliver historic messages nor loop self-authored replies.

---

## 7. Route Integration Contract (`src/conversation-hub.ts` & Worker Alias)

To compose `ConversationsService` with the base `Hub` Durable Object without editing `src/hub.ts` while it is reserved:

### 7.1 `src/conversation-hub.ts` Subclass Composition

`src/conversation-hub.ts` defines a thin subclass `Hub extends BaseHub` (also exported as `ConversationHub`) that:
- Inherits `this.env`, `this.sql`, `this.ctx.storage.transactionSync`, and all base `Hub` services (`busService`, `jobsService`, `fleetService`, `livenessService`, `checksService`, `quotaStore`).
- Initializes `this.conversationsService = new ConversationsService(this.sql, txSync)` and runs `this.conversationsService.init()` (and re-runs it in `override initDb()`).
- Chains `this.conversationsService.retentionCleanup` into `this.busService.retentionCleanup` on the instance using one shared remaining-row budget across bus messages, bus claims, and conversation rows, while keeping `Hub.prototype.scheduledTick === BaseHub.prototype.scheduledTick` and `Hub.prototype.alarm === BaseHub.prototype.alarm` unchanged (so valid-slot ticks and final-5-minute late-slot refusals behave identically to `BaseHub`).
- Overrides `fetch(request)` **only** for the exact `/api/conversations` prefix (`path === '/api/conversations' || path.startsWith('/api/conversations/')`), delegating all other HTTP routes, WebSocket upgrades, RPC methods (`scheduledTick`, `runChecks`), and `alarm()` to `super`.
- Preserves the public Durable Object class export name `Hub`, binding namespace `HUB`, SQLite storage authority, and Wrangler migrations unchanged.

```ts
// In src/worker.ts (owned by UI/worker integrator):
import { Hub } from './conversation-hub';

export { Hub };
```

### 7.2 Direct `src/hub.ts` Wiring (Alternative / Post-Merge Consolidation)

```ts
import {
  ConversationsService,
  authenticateConversationRequest,
} from './conversations';

// 1. In Hub constructor:
this.conversationsService = new ConversationsService(
  this.sql,
  fn => this.ctx.storage.transactionSync(fn),
);
this.conversationsService.init();

// 2. In Hub.fetch(request):
if (path === '/api/conversations' || path.startsWith('/api/conversations/')) {
  const auth = await authenticateConversationRequest(this.env, this.sql, request);
  return this.conversationsService.handle(request, auth);
}
```

### 7.3 How `authenticateConversationRequest(env, sql, request)` Works:
1. Checks `getSession(env, request)`. If a valid admin browser session exists:
   - Computes `csrfVerified = verifyCsrf(request, session)`.
   - Maps `session.sub` to `actor = "human:" + session.sub` (never bare `'admin'`).
   - Ensures the verified human session (`session.sub`, `session.email`) is recorded/recognized by the identity validator.
   - Passes `{ caller: { actor: 'human:' + session.sub, kind: 'human', isAdmin: true }, scopes: ['bus:read', 'bus:send'], authType: 'session', csrfVerified, session }` to `conversationsService.handle(request, auth)`.
2. Otherwise checks `Authorization: Bearer ...` against `credentials` (`revoked_at IS NULL`, unexpired):
   - Rejects `host`, `job`, and `routing-client:*` credentials.
   - For `kind === 'agent'`, sets `caller = { actor: cred.actor || cred.subject, kind: 'agent', machineId: cred.machine_id, tokenId: cred.id, tokenHash: cred.token_hash }`, `authType = 'bearer'`, `csrfVerified = true`.
3. `conversationsService.handle(request, auth)` enforces authentication and CSRF disposition **before** reading the request body or accessing conversation rows.


