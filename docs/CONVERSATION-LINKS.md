# Conversation task, review, and outcome links (M4)

Metadata-only join between an existing conversation and an existing routing job or fleet launch. The links service does not submit tasks, reserve launches, mutate job state, or create conversations or membership. Canonical status is always derived from the live `routing_jobs` / `fleet_launches` rows. There is no second dispatcher, tracker, or Durable Object.

Implementation: `src/conversation-links.ts`, composed from `src/conversation-hub.ts`. Routing policy, tiers, placement, `src/routing.ts`, `src/fleet.ts`, `src/schema.ts`, `src/jobs.ts`, and `src/env.ts` are unchanged.

## Mapping (no independent review/outcome table)

| `kind` | `ref` binds to | Canonical status source |
|---|---|---|
| `routing_job` | existing `routing_jobs.id` (UUID) | Live job `state`. Only active dispatch states (`queued`, `launched`, `running`) may use the existing fleet fallback: launch `exited` → `done`, `failed` → `failed`, `uncertain` → `stuck`, `running` → `running`, `offered`/`starting` → `queued`. A known non-terminal launch idle more than 30 minutes is `stuck`. Explicit job dispositions always win over launch state. |
| `fleet_launch` | existing `fleet_launches.id` (UUID) | Live launch `state`: `offered`, `starting`, `running`, `uncertain`, `exited`, `failed`. Only `exited` is complete; `failed` is terminal without completion. |
| `review` | existing `routing_jobs.id` | A thread association, with the same live canonical status. Only canonical `accepted` proves completion. `done` alone supplies no accepted-review proof and remains non-terminal for this link; `completed-awaiting-review` remains pending. Canonical `failed`/`cancelled` are terminal without completion and do not assert a reviewer verdict. |
| `outcome` | existing `routing_jobs.id` | Same status derivation as `routing_job`: `done`/`accepted` are terminal and complete; `failed`/`cancelled` are terminal without completion. |

For job and outcome links, `completed-awaiting-review`, `waiting-on-human`, and `stuck` remain non-terminal and incomplete even if the launch exited. `accepted` is read only from the canonical job row; an attachment label or user-supplied verdict never establishes it. There is no independent reviewer identity or verdict record in this schema. If no canonical accepted-review fact exists, `review.complete` is false. Unrecognized job/launch states return `canonical_status: null`, `terminal: false`, `complete: false`, while `available: true` indicates that the authorized canonical row exists. Missing or unauthorized rows instead return the unavailable view. Unknown states never fall back to worker exit.

Client fields such as `status`, `verdict`, `outcome`, `completed`, `url`, or `href` are rejected (`400 unknown_fields`). Attaching an `outcome` or `review` link to a still-queued job is allowed; the response reports the current derived status and never marks the work complete until canonical facts establish completion for that link kind.

History is the join row itself. Job/launch state updates and Durable Object restarts do not delete the association. Later-deleted targets stay linked until a member removes them, and they are returned as an unavailable tombstone rather than as a completed result.

## HTTP

All routes reuse conversation authentication (`authenticateConversationRequest` + live `verifyPrincipal`). Session CSRF is required on mutating methods. Host, job, and routing-submit-only credentials are refused as conversation principals (F7).

### Attach
`POST /api/conversations/:id/links`

```json
{ "kind": "routing_job", "ref": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "idempotency_key": "link-job-1" }
```

- Auth: current active member with `bus:send`, **and** current GET visibility on the target (see below).
- Non-member, including an admin session that is not a member: `403` (`not_a_member` / `member_removed`).
- Unknown conversation or expired-archived parent: `404 not_found`.
- Unknown or unauthorized target: `404 not_found` (opaque; no existence leak).
- Duplicate `(conversation_id, kind, ref)`: `200` with `"replay": true`.
- Same `(conversation_id, created_by, idempotency_key)` with the same payload: `200` replay. Different payload: `409 idempotency_conflict` only after current target visibility succeeds. Both key replay and duplicate-ref replay revalidate the live principal, membership, target existence and target ACL in the same transaction before any successful response; missing/unauthorized targets receive the same opaque `404`, including after deletion and recreation under a different actor.
- Over cap: `409 storage_limit_exceeded`.
- Does not insert a routing job or fleet launch.

`201` / `200` body (scalars only):

```json
{
  "id": "01KLINKS000000000000000000",
  "conversation_id": "01JABCDEF0123456789ABCDEFG",
  "kind": "routing_job",
  "ref": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  "created_at": "2026-10-17T12:00:00.000Z",
  "created_by": "human:user-alice",
  "replay": false,
  "target": {
    "available": true,
    "canonical_status": "queued",
    "terminal": false,
    "complete": false
  }
}
```

### List
`GET /api/conversations/:id/links?limit=50&cursor=`

Active member with `bus:read`. Each item includes live `target`. If the caller cannot currently GET that target, or the row is gone, `target.available` is `false` and `canonical_status` is `null`. Pagination `limit` is `1..100` (default `50`). Cursor is `<created_at>|<id>`.

### Get / remove
`GET /api/conversations/:id/links/:linkId` — member + live target tombstone as above.

`DELETE /api/conversations/:id/links/:linkId` — any active member may detach a link (so unavailable refs can be removed). Remove does not mutate the job or launch.

### Reverse
`GET /api/conversations/links?kind=routing_job&ref=<uuid>&limit=50&cursor=`

Requires current GET visibility on that target (`404 not_found` otherwise, including unknown ids). Returns only conversations where the caller is a **current** member. Current membership (including principal kind) and archived-parent expiry are applied in SQL before the bounded `limit + 1` keyset query. `next_cursor` is present exactly when another visible association exists at that transaction snapshot; its anchor is the last returned visible link. A later page rechecks current authority and membership. Expired or non-member associations neither truncate pages nor supply cursor anchors. Does not list foreign-project or non-member conversations.

## Target GET visibility (read-only inspect)

Copied from existing routing/fleet GET auth; not a new authority:

- **routing_job / review / outcome** (GET `/api/tasks/:id`): visible when the verified caller is an admin human session, or an agent with `routing:submit` or (`bus:send` and `fleet:launch`) whose actor equals `routing_jobs.actor`. Assigned hosts are not conversation principals, so they cannot attach or reverse through this API.
- **fleet_launch** (GET `/api/fleet`): the dashboard list is admin-session-only. Non-admin agents receive `404` for launch refs. Agents that need launch progress attach the related `routing_job` instead; job status already overlays launch state.

Conversation membership alone never grants access to a foreign or private job. Project-qualified agent identity is the live `agents` row (F7). Credentials are re-read for revocation and expiry inside the transaction.

## Bounds

| Bound | Value |
|---|---|
| Links per conversation | 32 |
| Total retained links | 4096 |
| `ref` | UUID, 36 chars |
| `idempotency_key` | `1..128` `[A-Za-z0-9._:-]` |
| `kind` | four tokens above |
| Page `limit` | `1..100`, default `50` |

## Retention

Link rows are children of `conversations`. Fair bus-versus-conversation cleanup is unchanged at the hub layer. Inside the conversation budget, archived-parent cleanup deletes link rows in indexed `id` chunks and counts each deleted row toward `rowsDeleted <= limit`. The conversation header is removed only after messages, members, and links are gone. Orphan links (parent already missing) are a fourth fair conversation-cleanup category, sharing that same affected-row budget with archived children, removed members, and expired messages. A zero remaining allowance deletes nothing. Link associations are not expired independently of their parent, so ordinary job state updates and restarts keep the reference.

## Out of scope

Physical host adoption (M11), ping/follow-up (M5), routing policy/tier/placement, new review engine, `review_gate.py`, deploys, and any change to routing schema.

Standalone `ConversationsService` constructs the same links handler used by the composed `Hub`. Its `init()` creates both schemas before routes or cleanup. Child operations join the current storage transaction without recursively initializing the parent or opening nested transactions. Startup orphan reconciliation invokes one fair retention pass of at most 100 affected rows when orphan links exist; remaining work is reported by `hasExpiredRows` and drained by ordinary bounded cleanup. Repeated initialization is safe, including on a current schema version. Link deletions, parent accounting and fairness cursors roll back together on failure.
