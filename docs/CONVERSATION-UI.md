# Conversations UI

The private conversations page (`/conversations.html`) for the agent hub:
list–detail layout on the vendored dakit design system, message thread with
history pagination, composer with preserved drafts and idempotent retry,
read cursors, reconnect handling and a "new conversation" dialog restricted
to verified hub participants.

## Scope and status

- Owned by the platform UI workstream (M2). This file documents the UI's
  transport contract mapping, not the API itself.
- The transport follows the M1 conversations contract exactly; see the
  contract document `M1-UI-REPAIR.md` in the platform coordination private
  store and `docs/SPEC.md` for milestone context.
- The static UI is verified against strict protocol fixtures
  (`test/conversations-ui.browser.mjs`) that implement the real contract
  shapes and reject wrong paths and payload keys. That is a design proof,
  not end-to-end: genuine Worker + CLI + browser integration is a later
  milestone, owned by head integration after independent review.

## Transport contract (what the page actually sends)

| Action | Request | Notes |
|---|---|---|
| Verified identities | `GET /api/conversations/participants` | `{ self: { actor, kind, display_name }, participants: [...] }`. The verified self identity is `self.actor`; the page never infers identity from email or an admin flag. Actors are opaque strings (`human:<sub>`, `<project>:<tag>`). |
| Conversation list | `GET /api/conversations` | Summaries (`id`, `kind`, `title`, `member_count`, `last_seq`, `last_message_preview`, `updated_at`, `my_read_seq`, `unread_count`). The page does not depend on `members` in summaries. |
| Conversation detail | `GET /api/conversations/:id` | Top-level metadata plus the `conversation` wrapper and active `members` (`actor`, `kind`, `role`); used for the detail header. |
| Message history | `GET /api/conversations/:id/messages` | Initial load without cursor (latest page, `seq ASC`). Older pages use `?before_seq=`; incremental polling uses `?after_seq=`. The page consumes `has_more_before` / `prev_before_seq` / `has_more_after` / `next_after_seq` and `self_actor`. |
| Send message | `POST /api/conversations/:id/messages` | `{ body, idempotency_key }`. Retries reuse the exact stored key and payload so the server replays the same message instead of appending a duplicate. Sender identity is always server-verified; the page never sends `from` / `sender_actor` / author-style fields. |
| Read cursor | `POST /api/conversations/:id/read` | `{ seq: <last rendered seq> }`, answered `200` with JSON (`read_seq`, `last_seq`, `unread_count`). The page never sends `last_read_message_id` and never expects `204`. |
| Create conversation | `POST /api/conversations` | `{ kind, title, members: [<canonical actors>], idempotency_key }`. `title` is required for every kind — direct titles are derived from the chosen participant's display name; groups ask for one. Members are canonical actor strings picked only from the verified participants list; the page never sends `member_ids`. |

Auth: session cookie plus `X-CSRF-Token` (from `/auth/me` `csrf_token`) on
every mutating request. `401` redirects to the login page once. `403`
(csrf_invalid excepted, which refreshes the token and retries exactly once)
stops all retry loops for that resource: the error surfaces in place and
drafts are preserved.

## Behaviour notes

- URL state is `?conversation=<id>`; the dakit split component owns pane
  switching, the back button in the narrow view and `aria-current` on the
  selected row.
- All message content renders through `textContent` / `createElement`;
  hostile markup stays inert literal text (browser-checked).
- Failed sends keep the draft with the original idempotency key across
  reloads; restored entries that claim to be in flight are normalized to
  failed, because nothing can be in flight after a page reload.
- Disk and memory outboxes use `conv-outbox:v2:` plus a JSON tuple of
  `[participants.self.actor, conversationId]`. Entries also carry both
  values; opaque actor strings are never split or inferred from email.
  Legacy and foreign entries are purged. Unknown identity hides all
  outboxes and blocks sending; a transient verification failure keeps
  attributed disk entries sealed until that actor is verified again.
- Session refreshes and send/create preflights verify the participant
  identity. Account changes and sign-out clear prior pending rows, typed
  text, cached conversations and create intent before new account chrome
  renders. Auth and route generations guard asynchronous completions;
  abort controllers cancel superseded fetches. A CSRF refresh cannot
  replay an earlier actor's mutation under a new account.
- Create retries retain the exact key and canonical payload after uncertain
  failures. The fingerprint uses kind, trimmed title and sorted member
  actors; changing that payload or opening a new dialog starts a new intent.
  Named groups may include self plus one other participant.
- Membership denial disables the textarea, Send and message Retry, keeps
  already visible own text, and stops thread polling and read/send requests.
  The composer validates the API's 16,384 UTF-8 byte limit before sending.
- The composer is disabled while offline with drafts preserved; reconnect
  re-enables sending and resyncs.
- All styling comes from the vendored dakit snapshot
  (`public/vendor/dakit`, pinned by `dakit.lock.json`); missing kit needs
  are filed as dakit needs, not one-off visual systems.
- dakit's split owns pane display at its mobile breakpoint; account triggers
  use the official `dk-account-trigger__chev` class and its 16px size.
  dakit currently lacks a message-thread component. The need for message
  rows, delivery/retry states, composer and history divider has been handed
  to the design-system coordinator through the head. Until a shared
  component is agreed, the existing app layout uses semantic kit tokens.
- Static guards live in `test/conversations-ui.checks.mjs`: vendor lock,
  dakit border/color/scale checkers, DOM-safety and CSP hygiene, plus a
  protocol guard that fails the build if guessed pre-contract transport
  keys reappear in the page code.

## Local verification

Run `node test/conversations-ui.checks.mjs` and
`node test/conversations-ui.browser.mjs` (the latter reuses the available
Playwright/Chromium installation). Browser fixtures enforce the M1 request
shapes and cover account isolation, reload and in-flight races, bounded
CSRF refresh, uncertain create replay, membership loss, UTF-8 limits,
mobile back and official component geometry. Each render scenario clears
storage: desktop/mobile, light/dark, list/thread, empty/error/retry/keyboard.
These checks remain a protocol design proof; final M2 acceptance requires
the combined real Worker/browser flow after M1 security review.

## Future integration (owned by head, one line)

The Worker keeps importing the base `Hub` until the security-reviewed M1
`ConversationHub` module lands on main. The exact future change in
`src/worker.ts` is:

```ts
import { Hub } from './conversation-hub';
```

After the combined API hook is integrated, the public `/` entry point
becomes the conversations page; fleet dashboard navigation stays at
`/fleet.html`. Conversations assets are already on the authenticated
private-asset allowlist in `src/worker.ts` together with the shipped
vendored dakit files and fonts.
