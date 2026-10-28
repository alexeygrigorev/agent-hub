# Banked reset expiry alerts

Intake `77f492ed-3bbe-4f0c-a89c-d7b01d341142`, rescoped by Alexey's
28 October instructions t1734u/t1741u: expiry alerts are required and automatic
redemption is forbidden. Alerts ask Alexey to approve whether to use a reset.
Only Alexey may approve and apply it manually; Hub and agents never redeem
resets or open purchase/credit options. There is no approval-to-apply endpoint.

The existing five-minute checks cron reads every registered Codex and zai account,
including accounts currently disabled for agent placement.
It uses fresh account-level server/subscription quota snapshots and unambiguous
registered host quota routes. Each `details.banked_resets` grant supplies its type,
availability and exact expiry. An unavailable/stale read does not erase a known
deadline. A fresh successful empty list confirms no grants remain.

An early notice is created at 48 hours before expiry (giving a 12-hour margin
before the required 36-hour warning); a second notice is created strictly below
24 hours. If a grant is first discovered later, Hub warns immediately and says
that discovery was late. It cannot retroactively send a 36-hour warning for an
unknown expiry. Every notice names the account, reset type, exact deadline in
`Europe/Berlin` (including the correct CET/CEST offset), and the manual action.

Notices use the existing liveness alert path, `HUB_ALERT_TO` inbox destinations
(default `grok-bot`, the Developer relay) and optional `HUB_ALERT_WEBHOOK`.
Existing per-target delivery receipts and retry handling apply. A durable
`banked_reset_notices` row deduplicates each threshold per grant independently
of process restarts and alert-history retention. Several same-type grants with
the same deadline remain distinct via a stable ordinal. Reordering the list
does not generate new notifications. Disappeared/expired grants resolve their
open alerts.

## Manual fallback

Open `/banked-resets.html` in the Hub and sign in as an administrator. The page
shows every registered Codex/zai account, known grants and the observation source.
For zai without a usable quota API, visit
`https://z.ai/manage-apikey/coding-plan/personal/usage`, sign in using email and
password and complete any Aliyun slider/captcha yourself. Hub never scrapes
pages behind a captcha and never stores provider passwords or browser cookies.

Choose the account, record **each available grant** as
`Weekly reset | 2026-12-22T01:00:00+01:00`, and save the complete list. Include
an explicit UTC offset or `Z`; timezone-free dates are rejected. Use an empty
list to confirm none. After manually using a grant, remove its line and save.
Newer successful automatic evidence replaces the manual list. Unknown accounts
raise one persistent Hub alert asking the operator to record the list or
confirm none; repeated checks do not create another alert.

The same operator surface is available as authenticated `GET` and `POST`
`/api/quota/resets`. POST takes
`{"account":"shared-zai","grants":[{"label":"Weekly reset","expires_at":"2026-12-22T00:00:00Z"}]}`.
Only human administrators can record values; browser writes require the normal
Hub CSRF token. Reads include accounts, active grants, observation metadata and
the most recent 100 threshold receipts. No provider credential is accepted.

## Alexey's decision

Only Alexey decides whether to use a reset and performs the manual action.
For Codex, on the correct account he can use `/usage` → **Redeem reset** → select the
earliest-expiring Full reset → confirm **Yes**. Verify `/status` weekly usage
is restored and credits did not change. For zai he uses the personal-plan usage page
above and select one earliest-expiring weekly grant. Never apply two resets
back to back, and never choose a paid/credit option.
