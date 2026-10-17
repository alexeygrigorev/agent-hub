# Scheduled conversation follow-ups

M5 source candidate; independent review, head integration and physical acceptance
are separate gates. This change does not enable checks, enroll a host, deploy,
write a native inbox, wake a model, or close issue 102, M8b or M11.

## Authority and provenance

`system:conversation-checks` is an internal automation sender. It is never a human,
agent credential, member, root role or device identity. The API exposes
this literal sender address, `sender_kind:system` and message kind `note`. The
current UI uses the literal address as the sender label (no directory entry or
special automation badge); its unused sender-kind adapter falls back to human.
There is no human attribution or native binding for this reserved address. Ordinary member sends and bridge self-message suppression
keep their original meaning. A scheduled write leaves every member's read cursor
unchanged. The normal bridge can subsequently deliver the distinct-sender note
and acknowledge it through its existing rules.

A currently signed-in, enabled human **owner of that conversation** explicitly
authorizes automation through `PUT /api/conversations/<cid>/automation`, with the
normal session cookie and CSRF header. An outside admin, ordinary member, agent,
collapsed token-admin identity or CallerContext alone cannot issue this grant.
No secret is supplied to or stored in a check. The owner request is:

```json
{"check":"followup","target_actor":"example:worker","body":"Follow-up {check}: {detail}","on":"fail","expires_at":1792212000000}
```

`expires_at` is epoch milliseconds, strictly in the future and bounded by both
the issuing session's expiry and 24 hours. The example timestamp must be replaced
with a valid local expiry. The response returns `grant_id`, `sender_actor` and
`expires_at`. Reissuing the grant creates a new version. Revoke with CSRF-protected
`DELETE` on the same endpoint and JSON `{"check":"followup"}`. Each grant binds
one exact CID, check name, target, message template and action policy. It grants
no wildcard write permission. Revocation or replacement never revives an older
slot's grant version. Removing a target or issuing owner revokes the grant, even
if membership is later restored.

The target must be an exact current canonical member. A bare project alias is
rejected rather than promoted at execution time. A genuinely projectless actor
can be bound only to that exact registry/credential identity. Agent eligibility
binds one current credential at grant issuance, then rechecks revocation, expiry,
read scope, immutable registry actor, current registry and machine binding before
evaluation and every new effect. That credential is used only to validate the
recipient; automation never sends as it. Human targets and the issuing owner
must remain enabled in `users`; the issuer must remain a conversation owner.
Membership, archive and grant expiry are checked again in the write transaction.
Rotated/replaced credentials require an explicit new grant; there is no fallback
to another credential or delayed alias resolution.

## Check configuration

Only the existing admin/CSRF checks route configures schedules. Defaults remain
liveness-only. Example after obtaining a real owner grant:

```json
{"checks":[{"name":"followup","kind":"conversation_backlog","interval_s":300,
"params":{"actor":"example:worker","max_age_s":3600,"conversation_id":"CID"},
"action":{"type":"message","to":"example:worker","conversation_id":"CID",
"grant_id":"GRANT_ID","body":"Follow-up {check}: {detail}","on":"fail"}}]}
```

`conversation_backlog` counts retained rows strictly older than `max_age_s`,
with `seq > target.last_read_seq`, excluding the target's own sends. A bus ACK
is unrelated. Prefix/interior retention gaps neither acknowledge nor erase
remaining unread rows. Optional `params.conversation_id` restricts evaluation;
omitting it scans only explicit grants for that exact check and target, at most
50 conversations. Missing authority, invalid identities and larger scans produce
an error, never a healthy result. No conversation message action runs on an evaluation error.
An alert-only backlog check still requires owner grants for its evaluated CIDs.
A routine message uses the same explicit grant with `on:always`.

The action's `to` is the monitored recipient, never its author. For backlog
messages it must equal `params.actor`; a configured CID must match the action
CID. The message template and `on` must match the grant. Templates are bounded
at 4,000 characters, rendered bodies at 8,000 characters and the original
conversation byte caps. No scheduler, provider dispatch or PTY path is added.

## Durable slots and receipts

The existing scheduler retains its latest slot per check name in SQLite. It
re-reads each due row immediately before acquiring a reservation. A token and
30-second evaluation lease fence stale evaluators; a conditional token check
precedes dispatch and completion. While a slot is active, config replacement,
interval changes and restart reuse its original check, result, grant version,
rendered payload and idempotency key. New conversation keys include a random persisted generation, independent of
wall-clock time, so interval changes and backward clocks cannot reuse an earlier
key. Existing R2 slot keys and frozen payloads are preserved on initialization.
A forced rerun of a finished slot returns
its stored result until the slot's original next boundary. Ordinary configuration
replacement preserves `next_due_at`.

Conversation dispatch failures keep the slot unresolved and report `error`.
The next eligible run reconciles that frozen intent **before** reevaluating
backlog. A message and its Hub storage receipt commit in one synchronous
transaction. Response loss, detail drift, config drift and even pruning of the
message cannot recreate the effect: the same intent returns the original
message ID. A mismatching intent fails. A committed receipt may be recovered
after grant revocation because that performs no new write. Any new effect still
requires current authority. Disabling/removing a check stops automatic retries.
Until retention explicitly reconciles or cancels it, reenabling the same name
retains its unresolved intent rather than substituting new configuration.

A `conversation_message` action confirms Hub storage only. Native delivery,
recipient reading, task acceptance and completion remain separate facts.
Bridge delivery is unconfirmed until the existing host bridge records its own
receipt. No successful Hub write is described as native delivery.

Persistence keeps the original caps: 1,000 grant rows, 1,000 latest-slot rows,
4,096 receipts, and 1,000 run-history rows. Templates and rendered bodies retain their existing character limits. New
frozen definitions are capped at 128 KiB, results at 64 KiB, and intents at
64 KiB of serialized UTF-8. Oversize rendering is rejected before persistence;
new slots freeze the definition without duplicate SQL JSON/status fields.
Existing R2 frozen rows are preserved until reconciled or retired. These payload
caps complement the row caps; invalid large legacy configuration reports an
error for that check while other due checks continue. Original conversation storage limits still apply
before sequence allocation; follow-ups never evict retained unread history.

## Operational retirement

The latest slot is also durable write authority. A receipt may return its original
message ID without authorizing another write. Without a receipt, the Hub requires
an exact current non-done slot: check name, opaque effect key and serialized
frozen intent must all match. A done, deleted or superseded slot cannot authorize
a new effect. Missing receipts alone never permit a write. Old requests fail with
`automation_slot_retired` after retirement, including after message pruning,
restart, recipient/CID/grant replacement or a backward clock. This requires no
forever-growing tombstone table and does not promote old credentials or aliases.

The existing retention hook handles slots, receipts and grants as three fair
categories alongside archived conversations, removed members and messages. The
composed Hub shares the same outer transaction and deletion allowance with bus
messages and claims. `automationRowsDeleted` is included in `rowsDeleted` (and
`conversationRowsDeleted` at the Hub boundary); updates do not masquerade as
deletions. Every category fetch is limited by its allowance. Primary/expression
indexes serve effect-key and grant references; anti-join scans are additionally
bounded by the unchanged 4,096/1,000 table caps. No new timer, job loop or manual
SQL purge is required.

- A non-done slot pins its receipt, even after grant revocation, expiry or parent
  removal. Valid unresolved intent and its first payload survive cleanup and
  restart. Retry recovers the atomic Hub receipt before reevaluation.
- Cleanup reconciles invalid-grant slots and slots whose checks were removed.
  An exact receipt records the original accepted Hub effect; absence of a receipt
  closes write authority with an explicit error and no accepted effect. A receipt
  that disagrees with its frozen intent remains held for integrity reconciliation.
  Slot closure fences paused evaluators and delayed sends in the same transaction.
- Once a slot is done, its receipt may retire immediately: even before a newer
  slot exists, the old key no longer authorizes a write. Current done slots retain
  their original next boundary, preserving interval and backward-clock fences.
- Removed-check slots retire after reconciliation. Cleanup revokes their bound
  grant before deleting the slot; later name reuse needs fresh owner authority.
  A newly issued replacement grant has a distinct ID and is unaffected by an
  old slot's revocation. Config replacement preserves live progress and retries.
- Expired, revoked, archived-parent and parentless grants retire once unresolved
  slots no longer reference them. Unused grants expire within their original
  session/24-hour bound. Standalone ConversationsService initializes the same
  slot schema and supports retirement without a scheduler configuration table.

A full table still makes the affected operation HOLD with an error until the
existing retention hook reclaims eligible rows. Unresolved valid intents are
preserved; they can retry through the existing run route, or be fenced and
reconciled by retention after explicit check removal, grant revocation/replacement,
expiry or parent archive. An accepted atomic receipt is recovered before audit
state is freed. Unknown effects are never labelled healthy or delivered. Retiring
an automation receipt does not acknowledge, delete or alter its conversation
message; ordinary retention controls message lifetime.

## Validation and remaining gates

The original R2 implementation/review lineage through `0419`'s head-held task,
`1229be7`, `c468635`, `d4affc7`, `117f240` and `6fb7b21` remains intact. The independent
[R2 report](reviews/CONVERSATION-PINGS-R2.md) retains its scoped acceptance and
operational-lifecycle FAIL verbatim. Retirement is a new source candidate descended
from `6fb7b21`, first checkpoint `48b3106`, with separate review and integration gates.

| Local retirement verification | Result |
| --- | --- |
| Follow-ups/checks | 59/59 (original 57 plus two byte-bound regressions) |
| Lifecycle regressions | 21/21 |
| Conversations/security | 26/26 |
| Retention/integration | 25/25, including additive accounting/rollback regression |
| Base Hub compatibility | 2/2 |
| TypeScript `tsc --noEmit` | PASS |

The lifecycle fixture seeds 4,095 historical requests with real serialized effect
bindings beside one actual completed receipt, then reclaims all 4,096. Another
32 actual intervals continue with zero settled receipts, one latest slot and one
grant; all member read watermarks remain unchanged. Tests cover exact old replay
after pruning, response loss at three phases, cross-interval restart, grant/member/
archive changes, orphan cap reclamation, send/cleanup races, different-CID/recipient
regrant, backward clocks, standalone R2 initialization, archived parent completion,
oversized rendered intent before storage, and transaction rollback with actual
row accounting.

Tests run serially on CPU 0 with single UV/V8/Go pools, an existing dependency
symlink, `--configLoader runner`, and private workspace TMPDIR/HOME/XDG paths.
Per-run logs and resource receipts, including failures, remain ignored. The
combined scratch cap is 25 MiB with an 18 MiB early guard. Largest sampled scratch
was 13,344,768 allocated / 13,255,397 apparent bytes, including root directory
inodes; largest process-group RSS was 638,775,296 bytes with 28 tasks. No guard
fired, installs occurred or evidence was manually deleted. One expanded lifecycle
run failed 15/19 assertions because a zero-day pruning fixture had mutated the
shared test service. Giving that pruner its own service restored fixture isolation;
all assertions were preserved and the 19-case rerun passed.

The affected compatibility subset was rerun because scheduler initialization,
write authorization and shared retention changed. R2's unrelated 206-case evidence
remains attributed to that source; those full groups were not needlessly repeated.
Sender/read logic and the Python/native bridge were unchanged: R2's memory bridge
4/4 is historical evidence, not a new physical-delivery claim. No source assertion
or digest pin was weakened. No native inbox, host journal or provider was used.

A different-model reviewer must accept the exact retirement SHA before head
integration. Physical M5 still needs an enrolled host, human/agent conversation,
reconnect ordering and a durable native inbox receipt. Source acceptance, main
integration, live runtime and cross-host adoption are distinct. M8b, issue 102
and M11 remain open; deployment and enrollment belong to their operations owners.
