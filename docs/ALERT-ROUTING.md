# Alert routing

Set `HUB_ALERT_TO` to comma-separated existing inbox actors with a reader, or
set `HUB_ALERT_WEBHOOK` to an HTTPS receiver. Both may be used. Developer supplies
the real destination. The checked-in empty destination refuses delivery and opens
an `alert_routing:destination` failure, visible in operator `hub status` and the
alerts API. There is no fallback to an unattended inbox. Webhook addresses are
not included in receipts or delivery errors; configure sensitive receivers as
Worker secrets. Plain email addresses are not bus actors: use an HTTPS receiver
that delivers email if email is the desired destination.

A reader must have an unexpired, unrevoked credential and must successfully poll
its own inbox within 15 minutes. Empty polls count. Host bridge polls count only
for that host's registered agents. Operator inspection of someone else's inbox
and generic credential usage do not count. Poll at least every five minutes.
Reader evidence survives Worker restarts and expires when polling stops.

Delivery SLO failures list overdue counts per recipient and prefix `NO_READER`
with the affected actors when no usable reader is observed. Registry-unbound
messages also fail this check. Neither the check nor reader tracking acknowledges
messages or discards backlog. A rejected bus GET (HTTP 401) and a rejected refresh
open a `bus_auth` incident. Known identities come from stored credentials; unknown
requests share one subject. Tokens and caller-supplied recipient labels are not
included. Authentication incidents remain open until operator remediation; fixing
credentials does not erase failure evidence.

Notifications use stored receipts and stable idempotency keys. A continuous open
incident sends once per destination. Reopened incidents for the same subject are
limited to one successful delivery per destination per rolling 24 hours. Failed
and rate-limited destinations retry no more frequently than every 15 minutes;
successful destinations are skipped. Session-audit summary subjects contain distinct
actionable batches: each distinct payload has its own receipt, and an identical
payload reuses its successful receipt for 24 hours. This preserves successive
batches without mailing duplicate summaries. Changing the destination configuration
retries existing open incidents against the new targets, preserving prior receipts. HTTPS receivers should honor the supplied
`Idempotency-Key` to cover a crash after receiver acceptance but before recording
its receipt. Webhook POSTs refuse redirects and time out after ten seconds.

The observed three long-lived shell subjects therefore produce at most three
successful notifications per destination per day, and no daily reminders for a
continuous incident after its initial notification. New subjects, distinct checks
and daily digest parts add their own notifications; this is not a global mailbox
cap or a prediction of new incidents.

`agent-coordination:platform` requires its own sanctioned agent pairing and a
reader, or an explicit operator retirement decision. Preserve all outstanding
messages while that decision is pending. A different actor's credential or an
operator ACK is not a replacement for its reader.
