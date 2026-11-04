# Hub 2.0 cost controls

Status: release gate in progress, 4 November 2026. This document records Alexey's accepted workload and billing decisions. The controller and alert delivery path have local/runtime evidence, but are not installed on the production Worker; the Cloudflare account backstop is unconfigured.

## Accepted limits and defaults

- The normal-day workload is approximately 33,000 operations. The former 20,000-row hard limit is withdrawn. The full canonical fixture remains unchanged, and the most recent source-stable run is recorded in [FEATURES.md](FEATURES.md).
- The primary alert fires once when Hub variable cost is **strictly greater than USD 0.03 in a UTC day**. Its persisted record must include the UTC day, accumulated/estimated USD, category breakdown, observation timestamp, metric coverage and provider-data lag. A threshold alert does not stop work.
- Initial enforced ceiling: **USD 0.10 per Hub UTC day**, with **USD 0.01 reserved** for status, alert delivery, stop and recovery. The local BudgetDO/Worker gate implements and tests this default and audited recovery. A production envelope has not been derived or installed, so this is not production-ready.
- Assume all monthly included allowances are exhausted. Use marginal paid rates for projections. The shared fixed USD 5 Workers subscription is excluded from the Hub variable-cost alert and is reported separately.
- The accepted 6,000 product-SLOC target is tracked independently from operation and money budgets. Alexey accepted the previously reported modest 362-line overage for rollout; do not minify or delete retained behavior to meet the target.

## Billable dimensions and rates

| Category | Marginal paid rate used | Evidence that must be collected |
| --- | ---: | --- |
| Worker standard requests | USD 0.30 / million | Worker invocations, including WebSocket upgrade requests; static-asset treatment must match the deployed mode |
| Worker CPU | USD 0.02 / million CPU milliseconds | Runtime CPU time, separated from wall time |
| Durable Object requests | USD 0.15 / million | DO HTTP/RPC sessions, incoming WebSocket messages at the documented 20:1 request ratio, and alarm invocations |
| Durable Object active duration | USD 12.50 / million GB-seconds | Billable active duration; account for the allocated 128 MB per DO instance |
| SQLite rows read | USD 0.001 / million | Actual cursor rows read, including index rows |
| SQLite rows written | USD 1.00 / million | Actual cursor rows written, indexes, deletes and `setAlarm()` writes |
| SQLite storage | USD 0.20 / GB-month | Persisted database storage by owner/shard |
| Legacy Workers KV reads | USD 0.50 / million | Per-key reads, including missing-key reads |
| Legacy Workers KV writes/deletes/lists | USD 5.00 / million | Per-key or request units as Cloudflare bills them |
| Legacy KV storage | USD 0.50 / GB-month | Stored bytes over the billing period |

Rates follow the [Workers](https://developers.cloudflare.com/workers/platform/pricing/), [Durable Objects](https://developers.cloudflare.com/durable-objects/platform/pricing/) and [KV pricing](https://developers.cloudflare.com/kv/platform/pricing/) pages (checked 4 November 2026). Workers subrequests are not separately billed as Worker requests; an incoming Worker request and the downstream DO request are separate dimensions. Connected non-hibernating DO WebSockets accrue shared active duration, and each `setAlarm()` is one SQLite row write. These are marginal estimates, not a substitute for invoice-rated provider data. SQLite cursor rows do not measure Worker requests/CPU, DO requests/duration, storage or legacy KV. Missing or stale dimensions are `unknown` and make monitoring unhealthy; they never become zero.

## Enforcement and recovery behavior

1. Before queue fanout, retries, provider refresh, scheduled start, cross-shard delivery or other expensive effect, obtain a durable idempotent reservation against the UTC-day ceiling. The admission decision uses upper-bound marginal paid cost, not free-tier headroom.
2. Persist the reservation and request digest before the effect. Settle with observed category counters afterward. A lost effect/settlement response remains reserved/uncertain until reconciliation; lease timeout alone cannot release budget.
3. Use a persisted per-category daily ledger and circuit-breaker state. Bound concurrency, attempts, backoff and batch size. At the ceiling, deny new expensive effects and stop optional fanout while preserving authorization/revocation, durable data, native exit receipts, capacity reconciliation, alerting and operator recovery.
4. A temporary operator override is authenticated, scoped to one UTC day and an explicit ceiling, and records actor, reason, amount, start/expiry and digest. It never disables metering or correctness. Ordinary stop preserves the ledger and all work records.
5. Alert dedupe is durable on `(hub, UTC day, threshold)`. Check and persist the claim before sending through the existing operational alert channel; delivery retries reuse the same alert ID. Alert a projected run-rate crossing before accumulated cost reaches USD 0.03, with the projection window and coverage shown.
6. The budget view reports each cost category, the 3-cent alert state, enforced ceiling, reserved vs settled amounts, forecast, provider reconciliation timestamp/lag and missing dimensions. An unavailable provider source raises a health error.

The ceiling limits Hub-controlled effects. Cloudflare does not expose a per-Worker daily hard-bill cap in this design, and inbound requests still cost something before the Worker can deny them. Therefore the ceiling must cover all fanout and background work, the external watcher must detect runaway inbound/request volume promptly, and the product-level Cloudflare backstop below remains necessary. Do not claim a full bill cap from SQLite row counters alone.

## Provider reconciliation and alert backstop

Cloudflare's restricted Account Usage v2 endpoint returns one account-level record per billable metric per day, including usage within included allowances; it is not automatically Hub-attributed. The API documents usage/cost querying but the account endpoint is not a live per-Worker daily control. Reconcile its rated categories to Hub counters by Worker/DO identity where supported; unallocated account-wide rows remain `unattributed`, not Hub zero. The [Billable Usage API](https://developers.cloudflare.com/api/resources/billing/subresources/usage/methods/get_account_usage_v2/) and dashboard data are provider source-of-truth after their processing lag. The exact lag for this account is not yet measured and must be displayed as unknown until reconciled.

The primary USD 0.03 alert is Hub-specific, UTC-day scoped, persisted and remains required. The independent Cloudflare backstop is planned as two additive **notification-only** Spend Policies at **USD 0.50 per current billing period per product**: one for account Workers spend and one for account Durable Objects spend. These are account product totals, not Hub-specific and not daily; their combined notice can therefore occur after up to USD 1.00 across those two product balances. Each policy must reuse an unambiguous recipient already present on an enabled billing notification policy, preferring an existing policy for the same product. The implementation never falls back to the account profile or creates a recipient. Ambiguous/missing recipient state fails closed. It never changes or deletes existing policies and verifies creation by API read-back. Cloudflare's API supports separate `workers` and `durable_objects` spend policies with `notification` action; `usage_block` is explicitly excluded. See [Spend Policies](https://developers.cloudflare.com/api/resources/billing/subresources/spend_policies/), [create](https://developers.cloudflare.com/api/resources/billing/subresources/spend_policies/methods/create/). Cloudflare's account-wide [Budget Alerts](https://developers.cloudflare.com/billing/manage/budget-alerts/) aggregate all usage-based products by billing period, so they are broader. Cloudflare also documents per-product usage notifications, but these alert on product-specific metrics (for example requests), not Hub-attributed dollar spend; the Spend Policy API is the narrower dollar-based backstop here.

The published Spend Policies API documents list/create/get/update/delete but no safe test-send operation. Do not cross a real billing threshold to test delivery. Verify a newly created policy with a read-back; if Cloudflare adds a supported test action for that product, use it and retain delivery evidence. Current Cloudflare policy state, recipient, API access and delivery are not yet verified. The account-level current-state balance is an asynchronous estimate and is not a synchronous bill or delivery proof.

## Current cost observations

- The last sanitized prior-Hub measurement reported about USD 19/day on 2 November, with approximately 2.87 billion row reads/day and 16.44 million row writes/day. This is historical Hub1/old-driver evidence, not a Hub2 invoice.
- The production route was re-enabled on 4 November and serves the deliberate 503 Hub-off stub. Hub2 is not deployed. This does not prove the Cloudflare account's current bill is zero.
- The available Cloudflare credential returned HTTP 403 for billing usage/spend-policy access. Do not retry that probe. Account-level usage, current plan state, current billing-period spend and actual native-policy recipients remain unverified.
- The prior 70-start normal-day fixture measured 31,018 SQLite rows plus 1,045 proven alarm writes (32,063); its conservative count of all alarm operations is 33,946. Its source changed during the run, so it is not an accepted current-source result.
- The prior 20-start diagnostic measured 13,256 SQL cursor rows and 676 alarm operations (160 proven `setAlarm()` row writes), with zero SQL/alarm operations in the idle phase. The run was stable while it executed, but its BudgetDO, budget-edge and Worker hashes do not match the final reviewed recovery source, so it is not current-source evidence. Its test-only upper-bound envelope projected USD 0.069814409228 (USD 0.060693742571 settled plus USD 0.000092946007 reserved at the captured status read), all fourteen units `upper_bound`, provider `unknown`/health `degraded`. This is not observed Cloudflare spend or production sizing. It shows the blanket test envelope is unsuitable for production; no lower production envelope is approved from these data.
- Hub2 has not completed the accepted approximately 33,000-operation normal-day workload on a frozen source with a production-derived envelope. Worker CPU, DO billed duration, storage and provider-rated account cost remain unknown.

## Acceptance tests before activation

- Threshold strictly greater than USD 0.03, one alert per UTC day, UTC rollover, durable restart dedupe and repeated send failure/recovery.
- Per-category accounting with all allowances treated as exhausted; no missing/non-finite source can pass as zero. Verify Worker requests/CPU, DO requests/duration, SQLite reads/writes, storage and any KV dimension independently.
- Reserve/settle replay, lost response after effect, denied-over-ceiling fanout/retry, bounded concurrency/backoff, open/close circuit breaker, and restart-persistent runaway fixture.
- Operator override requires a scoped, expiring, audited credential; emergency stop preserves all source data and still permits native completion/release and cost reporting.
- Compare local estimates to Cloudflare rated usage; state attribution and provider lag honestly. Test missing account API permission and unavailable provider state as monitoring-health failure.
- Configure and API-read-back the two Cloudflare product policies only after checking existing policies and recipient source. Record their exact IDs, USD thresholds, billing period and recipients. Never update or replace an existing policy. Do not claim delivery testing for Spend Policies until Cloudflare supports it.
- Test an alert while Hub service is intentionally unavailable using an existing approved external watch/alert route; no new Grok routine and no provider/model invocation.

Production activation remains gated on the runtime integration, production configuration and provider backstop. Local and live-channel checkpoint evidence is recorded below; it does not certify the remaining production gates.

## Public integration checkpoint (4 November, local Workerd)

The current source includes a fail-closed public pre-routing reservation in
budget-edge.ts. Every normal public request obtains its durable reservation
before authentication/application fanout. The adapter guards direct namespace
call count and wall deadline, and retains reservations after ambiguous effects.
It conservatively settles the configured envelope with coverage labelled
upper_bound; it does not report these estimates as observed CPU or provider bills.

HUB2_BUDGET_ENVELOPES has no production fallback. It is JSON with version:1 and
normal/safety plans. Each plan requires all fourteen BudgetDO metric units,
max_do_calls (1?64), max_wall_ms (100?60000), and a provenance description.
CPU, DO duration, SQLite reads/writes and persistent storage cannot silently be
zero/missing. Production sizing/provenance remains an operator rollout gate;
the checked-in test envelope is explicitly synthetic and is never loaded by
production source. Abort timers are cleared after namespace calls.

The authenticated recovery allowance covers budget status/override, token
revocation, stop-only machine connections/stop acknowledgements, operator stop
commands, and shaped machine native-exit reports. Existing downstream session,
identity, containment, epoch and grant checks remain authoritative; the budget
classification grants no application permissions. Spoofed safety headers and
ordinary send/capture requests do not obtain this allowance.

Account collection and central refresh reserve before provider HTTP/refresh
intent; a denied refresh never creates an issued token candidate. Quota retry
and discovery wake are persisted at retry_at. Mail native producer/retry
reservations preserve epoch, body, claims and uncertainty. Actual native
receipt acknowledgement and fenced cancellation use the recovery allowance.
Project and Machine owners mount the same budgetedBackgroundRequest helper
before their scheduled/WS/retry effects.

The scheduled alert dispatcher claims each persisted alert, uses its stable
Idempotency-Key and retries through the existing bounded ledger. Configuration
requires HUB_ALERT_WEBHOOK and secret HUB_ALERT_WEBHOOK_TOKEN. The receiver must
honor idempotency to suppress an accepted delivery whose response was lost.
The prior Hub's documented approved destination is the existing Dapier
/hooks/webhook/dev pipeline; provisioning and a live delivery-log read-back
are still required. No new operational recipient or Grok routine is created.

Final local gates: BudgetDO ledger/recovery 14/14, public Worker/BudgetDO 16/16,
and core lifecycle 14/14 on a source-stable Workerd snapshot. An independent
review pins the exact BudgetDO, edge and Worker hashes and the final public/core
report hashes. The gates cover pre-fanout denial, unknown metrics, funded
hard-stop recovery, restart persistence, stop, native started/finish and alert
retry/dedupe. Account/mail tests cover pre-provider denial and durable retry
pause. These use synthetic financial bounds and do not certify production
sizing, provider reconciliation or a Cloudflare account-wide hard bill cap.

## Verified existing-channel transport receipt

On 4 November the actual dispatcher posted one clearly labelled transport test
with a synthetic amount to the existing Dapier dev hook. Its first accepted
response was deliberately discarded. After runtime restart the same message ID
was retried: Dapier answered 202 with duplicate=true and the same event ID. The
authenticated delivery read-back reported Ran dev-mailbox and a completed
workflow run. No real billing threshold was crossed to create this test.

The existing hook's supported dedupe_path was changed from empty to message_id;
its endpoint, enabled state, other settings and existing bearer token were
preserved. This covers receiver acceptance followed by sender interruption.
The production Worker still needs the existing `HUB_ALERT_WEBHOOK` and
`HUB_ALERT_WEBHOOK_TOKEN` bindings; they have not been installed there. The
transport receipt proves the existing route and receiver dedupe, not a real
USD 0.03 threshold crossing or alert operation while Hub is down. No secret or
private run evidence is committed. Provider-rated billing access returned
HTTP 403 with the available credential; do not repeat that probe. The native
Cloudflare monthly backstop remains unconfigured pending billing-capable
dashboard/API access.

A denied request flood allocates no reservation row per refusal and does not
rewrite the UTC-day record. The persisted committed/reserved ledger still
controls subsequent decisions after restart. This avoids an expensive SQLite
write loop while paused; inbound Worker/DO requests themselves remain billable.

The scheduled dispatcher invocation itself reserves on alert_delivery before
reading the queue, including an empty queue. Its ledger units record at least
one background invocation; the recurring path performs one bounded queue read
and does not send or claim already delivered alerts. Missing configuration
remains a visible scheduling failure. This is distinct from each alert delivery
attempt, which also obtains its own bounded reservation.

An overrun breaker is not cleared merely by raising the ceiling. An authenticated
operator can explicitly request clear_hard_stop:true with the UTC-day override
and a reason. Recovery is audited and refuses while any metric coverage is
unknown or an unknown-metric health incident is open. It does not clear unknown
health or release uncertain reservations. Ordinary ceiling overrides retain the
existing breaker unless this explicit recovery action is requested.

The public override remains reachable after an actual overrun beyond the old
ceiling. After fresh authentication and CSRF validation, the BudgetDO applies
the audited proposal and reserves the server-configured recovery envelope in
one transaction under the proposed ceiling. An unfundable recovery rolls back
both the override and audit; replay is bound to actor, proposal and units.
Settlement debits the full recovery envelope. This route cannot change unknown
coverage, release uncertain holds or silently close the breaker.

A successful same-day override changes admission for new work immediately.
Already deferred Project, Machine, Account and Mailbox operations keep their
durably persisted `retry_at` (normally the next UTC-day boundary). This release
does not force a cross-object wake or move those pending deadlines earlier;
same-day override does not yet accelerate already deferred retries.
Provider reconciliation remains visibly unknown/degraded after operator
recovery; the action only certifies known event coverage and changes the
application ledger breaker.
