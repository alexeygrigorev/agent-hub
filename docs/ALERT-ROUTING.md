# Alert routing

`HUB_ALERT_TO` defaults to the empty actor list. `HUB_ALERT_WEBHOOK` defaults to
the existing Dapier `/hooks/webhook/dev` route, which enters the `dev-mailbox`
pipeline and wakes the Developer bot. `wrangler.jsonc` pins this URL so later
deploys preserve the route. The existing Dapier hook requires its bearer token
as the Worker secret `HUB_ALERT_WEBHOOK_TOKEN`; provision it through the normal
secret path. Never put the token in Git or reports. Without the secret,
delivery fails visibly as `alert_routing:destination` and is retried after 15
minutes. No new mailbox or unattended inbox is created. Additional
`HUB_ALERT_TO` actors must already exist and have an active credential/read
loop; an email address is not a bus actor.

A reader must have an unexpired, unrevoked credential and must successfully poll
its own inbox within 15 minutes. Empty polls count. Host bridge polls count only
for that host's registered agents. Operator inspection of someone else's inbox
and generic credential usage do not count. Poll at least every five minutes.
Reader evidence survives Worker restarts and expires when polling stops.

Delivery SLO failures count all overdue messages and recipients, list the top
20 recipients by backlog, and prefix `NO_READER` with affected names among those
20. Additional recipients are counted as `+K more`; reader lookups and the
notification body stay bounded. Registry-unbound messages also fail this check.
Neither the check nor reader tracking acknowledges messages or discards backlog.
A rejected bus GET (HTTP 401) for a known active credential opens a `bus_auth`
incident. Normal one-hour session/worker access expiry gets a two-minute refresh
grace instead: a successful authenticated read or refresh clears the pending
failure and resolves any auth incident for that reader. If no recovery arrives,
the next checks tick raises the incident (at most 20 due readers per run).
Failed refreshes on still-active known credentials alert immediately.
Anonymous/unknown requests and revoked predecessors do not allocate alerts.
Identity comes from stored credentials; caller-supplied labels and tokens never
enter alert text. Reader evidence expires after 15 minutes and delivery receipts
after 24 hours; checks prune expired rows using age indexes.

Notifications use stored receipts and stable idempotency keys. A continuous open
incident sends once per destination. Reopened `session` incidents in the
`stalled` state with `no_native_progress` are limited to one successful delivery
per destination per rolling 24 hours. Other reopened incidents and different
states notify immediately, so genuine repeat machine, check, credential, or ops
outages remain visible. Failed and rate-limited destinations retry no more
frequently than every 15 minutes; successful destinations are skipped.
Session-audit summary subjects contain distinct actionable batches: each
distinct payload has its own receipt, and an identical payload reuses its
successful receipt for 24 hours. This preserves successive batches without
mailing duplicate summaries. A destination change suppresses individual retry
of the pre-existing open alert backlog and emits one `cutover_backlog` summary.
Suppressed rows remain visible in Hub liveness and delivery-SLO results and are
never acknowledged or deleted. Current routing, bus-auth, and check failures
remain eligible for delivery. HTTPS receivers should honor the supplied
`Idempotency-Key` to cover a crash after receiver acceptance but before
recording its receipt. Webhook POSTs refuse redirects and time out after ten
seconds.

The observed three long-lived shell subjects produced about 863 repeats in two
days (~432/day). With subject receipts and the stalled-session 24-hour cooldown,
those three continuous open stalls produce at most three successful
notifications per destination per day, with no five-minute reminders. The
destination cutover adds one backlog summary. New subjects, distinct checks and
daily digest parts add their own notifications; this is not a global mailbox cap
or a prediction of new incidents.

`agent-coordination:platform` requires its own sanctioned agent pairing and a
reader, or an explicit operator retirement decision. Preserve all outstanding
messages while that decision is pending. A different actor's credential or an
operator ACK is not a replacement for its reader.

The alerting Worker credential cannot inspect the current production backlog.
The first production check after a destination cutover reports the number of
open rows it suppresses in the single summary; no live total is claimed here.

## Inbox-ack and onward-relay receipts (intake 1429bf88)

Delivery to a bot inbox is not the end of an alert, so the Hub now records
what happened next. Every successful inbox alert send links its bus message id
to the alert (`alert_inbox_links`), and the recipient's `read`/`ack` stages
stamp that link, giving a durable, operator-readable receipt per alert with no
message bodies. Onward relay to Alexey is reported, never inferred: the relay
owner (Grok Bot / Developer) records the forward receipt id and timestamp per
alert through the operator path, and an alert with no such receipt is reported
as explicitly `unconfirmed`.

- Operator view: `GET /api/alert-receipts?limit=N` (bounded, admin/cookie or
  operator token) lists per alert: `delivered_to`, inbox links with read/ack
  times, the relay receipt or `relay_state: "unconfirmed"`, plus an
  `unconfirmed` count. CLI: `hub alerts receipts [--limit N] [--json]`.
- Relay report: `POST /api/alert-receipts/:id/relay` with `{"relay_ref":
  "<forward receipt id>", "relayed_at": "<ISO>"}` (admin only; worker
  credentials are denied). CLI: `hub alerts relay <id> --ref <receipt-id>
  [--at ISO]`. A later report replaces the earlier one; writes are audited
  (`alert.relay_receipt`) and carry ids only.
- Retention: receipt rows share the liveness alert lifecycle and are pruned
  when their alert row ages out of retention; orphans never linger.

