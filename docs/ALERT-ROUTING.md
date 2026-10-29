# Alert routing

Set `HUB_ALERT_TO` to comma-separated existing inbox actors with a reader, or
set `HUB_ALERT_WEBHOOK` to an HTTPS receiver. Both may be used. Developer supplies
the real destination before rollout. `HUB_ALERT_TO` is a checked-in Worker var:
set its actual actor list in `wrangler.jsonc` (or the deployment's explicit
configuration). A dashboard-only value is overwritten by subsequent deploys
because the deploy script does not use `--keep-vars`. Use the existing secret
configuration path for `HUB_ALERT_WEBHOOK`; the empty var does not override that
secret. Coordinate configuration and release before enabling delivery. The checked-in empty destination refuses delivery and opens
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

Delivery SLO failures count all overdue messages and recipients, list the top
20 recipients by backlog, and prefix `NO_READER` with affected names among those
20. Additional recipients are counted as `+K more`; reader lookups and the
notification body stay bounded. Registry-unbound
messages also fail this check. Neither the check nor reader tracking acknowledges
messages or discards backlog. A rejected bus GET (HTTP 401) for a known active credential opens a
`bus_auth` incident. Normal one-hour session/worker access expiry gets a two-minute
refresh grace instead: a successful authenticated read or refresh clears the
pending failure and resolves any auth incident for that reader. If no recovery
arrives, the next checks tick raises the incident (at most 20 due readers per
run). Failed refreshes on still-active known credentials alert immediately.
Anonymous/unknown requests and revoked predecessors do not allocate alerts.
Identity comes from stored credentials; caller-supplied labels and tokens never
enter alert text. Reader evidence expires after 15 minutes and delivery receipts
after 24 hours; checks prune expired rows using age indexes.

Notifications use stored receipts and stable idempotency keys. A continuous open
incident sends once per destination. Reopened `session` incidents in the `stalled` state with `no_native_progress`
are limited to one successful delivery per destination per rolling 24 hours.
Other reopened incidents and different states notify immediately, so genuine
repeat machine, check, credential, or ops outages remain visible. Failed
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

When changing destinations, existing open incidents are retried in batches of at
most 20 during each checks invocation (normally every five minutes). The initial
backlog can therefore create a one-time burst of up to 20 notifications per
invocation per configured destination, until all eligible open incidents have
receipts. This burst is additional to newly opened alerts; count the current open
incidents in the authorized rollout report before release. This worker's scoped
credential cannot read that production backlog, so no live total is claimed.
