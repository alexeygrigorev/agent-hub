# Scheduled conversation follow-ups

M5 source candidate; independent review, head integration and physical acceptance
are separate gates. This change does not enable checks, enroll a host, deploy,
write a native inbox, wake a model, or close issue 102, M8b or M11.

## Authority and provenance

`system:conversation-checks` is an internal automation sender. It is never a human,
agent credential, member, root role or device identity. The conversation UI shows
this literal sender address and the `note` message kind; it has no native binding
or human attribution. Ordinary member sends and bridge self-message suppression
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
an error, never a healthy result. No message action runs on an evaluation error.
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
rendered payload and idempotency key. A forced rerun of a finished slot returns
its stored result until the slot's original next boundary. Ordinary configuration
replacement preserves `next_due_at`.

Conversation dispatch failures keep the slot unresolved and report `error`.
The next eligible run reconciles that frozen intent **before** reevaluating
backlog. A message and its Hub storage receipt commit in one synchronous
transaction. Response loss, detail drift, config drift and even pruning of the
message cannot recreate the effect: the same intent returns the original
message ID. A mismatching intent fails. A committed receipt may be recovered
after grant revocation because that performs no new write. Any new effect still
requires current authority. Disabling/removing a check stops automatic retries;
reenabling the same name retains unresolved intent rather than silently
substituting a new configuration.

A `conversation_message` action confirms Hub storage only. Native delivery,
recipient reading, task acceptance and completion remain separate facts.
Bridge delivery is unconfirmed until the existing host bridge records its own
receipt. No successful Hub write is described as native delivery.

Persistence is bounded: 1,000 grant rows, 1,000 latest-slot rows, 4,096 immutable
conversation receipts, and the existing 1,000 run-history bound. Grants reuse
one row per CID/check; slots reuse one row per check name. Receipts are not
automatically deleted: at capacity new effects hold with an error. Operations
must arrange independently reviewed receipt lifecycle/retirement before that
capacity is exhausted; silently expiring receipt keys would permit duplicates.
Original conversation storage limits are enforced before sequence allocation,
and no follow-up evicts retained unread history to make room.

## Validation and remaining gates

Validation is local against the real Worker and SQLite, with fixture humans and
public device-flow agent issuance. It does not use live credentials, native
inboxes, host journals or provider dispatch. The initial checks suite passed
11/11; typecheck passed after correcting three SQL cursor calls. An initial
40-case new suite passed 39 cases and timed out in the 51-conversation fixture;
its still-running fixture caused an additional unhandled rejection. That run is
retained. The scan fixture now has a bounded 20-second timeout, and a separate
stale-evaluator fencing case was added. Final evidence is pending.

Independent verification should attack watermark preservation and native bridge
eligibility with real HTTP history; outside-admin and disabled-human cases;
credential/membership changes between evaluation and dispatch; response loss
with changed details/config/restart; and two overlapping due snapshots including
a later row. Review the exact final SHA and preserve ordinary F7/M6, auth,
retention, caps and negative assertions. Physical M5 remains unproven until the
runtime owner demonstrates an enrolled host plus human/agent conversation,
reconnect ordering and a durable native inbox receipt. Domain and enrollment
remain operations dependencies.
