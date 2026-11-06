# Hub 2.0 cost controls

Status: release gate in progress, 6 November 2026. The clean-start source passed the complete numeric canonical Workerd workload with a source-stable native meter and no budget hard stop under the approved USD 0.25 cap. The test-only profile is not installed on the production Worker. The USD 0.03/day alert and USD 0.01 reserve remain unchanged. Provider-rated usage and Cloudflare billing backstop access remain unverified, so Hub estimates are not invoice guarantees.

## Accepted limits and defaults

- The normal-day workload is approximately 33,000 operations. The former 20,000-row hard limit is withdrawn. The full canonical fixture remains unchanged, and the most recent source-stable run is recorded in [FEATURES.md](FEATURES.md).
- The primary alert fires once when Hub variable cost is **strictly greater than USD 0.03 in a UTC day**. Its persisted record must include the UTC day, accumulated/estimated USD, category breakdown, observation timestamp, metric coverage and provider-data lag. A threshold alert does not stop work.
- Approved enforced ceiling: **USD 0.25 per Hub UTC day**, with **USD 0.01 reserved** for status, alert delivery, stop and recovery. The primary alert remains **USD 0.03 per UTC day**. The local BudgetDO/Worker gate must use this default and audited recovery on the exact release source; production is not active until the complete gate passes.
- Assume all monthly included allowances are exhausted. Use marginal paid rates for projections. The shared fixed USD 5 Workers subscription is excluded from the Hub variable-cost alert and is reported separately.
- The 6,000 product-SLOC target is tracked independently from operation and money budgets. On 2026-11-06 Alexey explicitly approved the exact 8,962 product-SLOC tree, including Resource Manager, as the maximum for this release, superseding the prior 7,853 maximum and obsolete 6,362 maximum. CI rejects growth above 8,962; do not minify or delete retained behavior to meet the target.

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

The production Wrangler manifest carries `*/15 * * * *` (96 scheduled invocations per UTC day) from the clean-start deployment. `scheduled()` returns without touching any binding in maintenance and bootstrap modes; it performs the bounded alert dispatch only in active mode. Account quota refresh is on demand when its persisted snapshot is stale; Account discovery uses its own explicit 1–6 hour Durable Object deadline when enabled; Project schedules use their existing per-object alarms. The canonical cost profile includes all 96 scheduled Worker invocations, and a configured 15-minute cadence is not evidence of live delivery or provider billing visibility.

The ceiling limits Hub-controlled effects. Cloudflare does not expose a per-Worker daily hard-bill cap in this design, and inbound requests still cost something before the Worker can deny them. Therefore the ceiling must cover all fanout and background work, the external watcher must detect runaway inbound/request volume promptly, and the product-level Cloudflare backstop below remains necessary. Do not claim a full bill cap from SQLite row counters alone.

## Provider reconciliation and alert backstop

Cloudflare's restricted Account Usage v2 endpoint returns one account-level record per billable metric per day, including usage within included allowances; it is not automatically Hub-attributed. The API documents usage/cost querying but the account endpoint is not a live per-Worker daily control. Reconcile its rated categories to Hub counters by Worker/DO identity where supported; unallocated account-wide rows remain `unattributed`, not Hub zero. The [Billable Usage API](https://developers.cloudflare.com/api/resources/billing/subresources/usage/methods/get_account_usage_v2/) and dashboard data are provider source-of-truth after their processing lag. The exact lag for this account is not yet measured and must be displayed as unknown until reconciled.

The primary USD 0.03 alert is Hub-specific, UTC-day scoped, persisted and remains required. The independent Cloudflare backstop is planned as two additive **notification-only** Spend Policies at **USD 0.50 per current billing period per product**: one for account Workers spend and one for account Durable Objects spend. These are account product totals, not Hub-specific and not daily; their combined notice can therefore occur after up to USD 1.00 across those two product balances. Each policy must reuse an unambiguous recipient already present on an enabled billing notification policy, preferring an existing policy for the same product. The implementation never falls back to the account profile or creates a recipient. Ambiguous/missing recipient state fails closed. It never changes or deletes existing policies and verifies creation by API read-back. Cloudflare's API supports separate `workers` and `durable_objects` spend policies with `notification` action; `usage_block` is explicitly excluded. See [Spend Policies](https://developers.cloudflare.com/api/resources/billing/subresources/spend_policies/), [create](https://developers.cloudflare.com/api/resources/billing/subresources/spend_policies/methods/create/). Cloudflare's account-wide [Budget Alerts](https://developers.cloudflare.com/billing/manage/budget-alerts/) aggregate all usage-based products by billing period, so they are broader. Cloudflare also documents per-product usage notifications, but these alert on product-specific metrics (for example requests), not Hub-attributed dollar spend; the Spend Policy API is the narrower dollar-based backstop here.

The published Spend Policies API documents list/create/get/update/delete but no safe test-send operation. Do not cross a real billing threshold to test delivery. Verify a newly created policy with a read-back; if Cloudflare adds a supported test action for that product, use it and retain delivery evidence. Current Cloudflare policy state, recipient, API access and delivery are not yet verified. The account-level current-state balance is an asynchronous estimate and is not a synchronous bill or delivery proof.

## Current cost observations

### V4 numeric workload result — 2026-11-05 16:20 UTC

- Immutable-source report: `/home/alexey/tmp/agent-reports/hub2-auth-v4-canonical-diagnostic.json` (SHA-256 `e2a57eb40b2e4c7573d5ab707aa45772fda2e241072b1f174a8f6dec71e5caf3`); post-run source hash sidecar: `/home/alexey/tmp/agent-reports/hub2-auth-v4-source-manifest.json` (SHA-256 `474eef4b2842204ba5d2bc6ba28fd4c2583393858c72f9603ff2245abc5c0d21`). The report's raw pre-run manifest hash is null because that runner variable was unset; it records per-file before/after hashes and declares `source_stable=true`. The sidecar is explicitly post-run and is not a signed pre-run manifest.
- Numeric workload completed: 70 starts, 49 scheduled occurrences, 100 native message receipts and phone reads, 50 claim cycles, 48 stale-quota batches, four discovery batches, 690 root/operator role calls, 20 retries, 16 host transitions, retained archive search/retry history and a persisted cold restart. The source-stable measurement has no recorded overruns, but it used a test-only USD 1 ceiling override and has `financial_acceptance=false` and `release_acceptance=false`. At that run the product default was USD 0.10; Alexey later approved USD 0.25/day. This older run is not the current-cap exact-head gate.
- The measured worst UTC-day estimate is USD 0.183481108398 settled plus USD 0.006709123276 reserved at peak ordering, or USD 0.193957030760 with the USD 0.01 recovery reserve. Required final-window operations and 96 assumed scheduled alert invocations fell on the next UTC day. The conservative model co-locates those USD 0.008437427390 operations with the worst measured day; this is a model, not observed same-day spend. The minimum under that model rounded to USD 0.21; Alexey approved USD 0.25/day to provide headroom. This diagnostic predates the clean-start source and does not replace the exact-head USD 0.25 run.
- Provider CPU, billable DO duration, storage attribution and Cloudflare-rated account cost remain unknown or based on retained configured upper bounds. A separate runtime trace rejected the proposed 500 ms CPU refund bound. The 96 scheduled invocations/day assume a proposed `*/15 * * * *` cadence; that cadence is not yet installed or verified in production.
- This v4 run is complete for its numeric fixture only, not exact-head release acceptance or proof of a Cloudflare bill. Populated older-schema migration is obsolete under Alexey's clean-start decision; configured production prompt inventory remains separately uncovered. The USD 0.03 UTC-day alert and USD 0.01 reserve remain unchanged.

- The last sanitized prior-Hub measurement reported about USD 19/day on 2 November, with approximately 2.87 billion row reads/day and 16.44 million row writes/day. This is historical Hub1/old-driver evidence, not a Hub2 invoice.
- The production route was re-enabled on 4 November and serves the deliberate 503 Hub-off stub. Hub2 is not deployed. This does not prove the Cloudflare account's current bill is zero.
- The available Cloudflare credential returned HTTP 403 for billing usage/spend-policy access. Do not retry that probe. Account-level usage, current plan state, current billing-period spend and actual native-policy recipients remain unverified.
- The prior 70-start normal-day fixture measured 31,018 SQLite rows plus 1,045 proven alarm writes (32,063); its conservative count of all alarm operations is 33,946. Its source changed during the run, so it is not an accepted current-source result.
- The prior 20-start diagnostic measured 13,256 SQL cursor rows and 676 alarm operations (160 proven `setAlarm()` row writes), with zero SQL/alarm operations in the idle phase. The run was stable while it executed, but its BudgetDO, budget-edge and Worker hashes do not match the final reviewed recovery source, so it is not current-source evidence. Its test-only upper-bound envelope projected USD 0.069814409228 (USD 0.060693742571 settled plus USD 0.000092946007 reserved at the captured status read), all fourteen units `upper_bound`, provider `unknown`/health `degraded`. This is not observed Cloudflare spend or production sizing. It shows the blanket test envelope is unsuitable for production; no lower production envelope is approved from these data.
- Worker CPU, DO billed duration, storage and provider-rated account cost remain unknown or configured upper bounds. The clean-start 70-start Workerd run below is a source-stable local estimate, not a provider invoice or production-installed envelope.
- A bounded measurement-only Cloudflare trace canary (`42036801305`) used a synthetic Worker/DO profile configured for a 500 ms CPU limit. The completed trace items reported Worker `cpuTime=1612 ms, wallTime=1765 ms`, Durable Object `cpuTime=2467 ms, wallTime=2652 ms`, WebSocket `cpuTime=2414 ms, wallTime=2558 ms`, and alarm `cpuTime=2345 ms, wallTime=2583 ms`. The synthetic resources were deleted and independently verified absent. These observed CPU values exceed 500 ms; therefore the proposed 500 ms CPU/duration refund profile is rejected and must not be installed. This canary does not measure Hub production billing or establish a replacement limit.
- The earlier cold public-read mismatch was traced to the measurement interval: the public-read effect profile excluded cold constructor/prelude SQL and the fixed BudgetDO settlement allowance. The observed cold request receipt was 77 reads / 89 writes, plus 64 reads / 8 writes for controller settlement, for 141 reads / 97 writes total. The 10R/11W public-read effect maximum was not an end-to-end bound. The initial 13,046,402 pUSD estimate versus 104,705,402 pUSD settled amount is therefore retained as evidence of the old envelope's under-reservation, not as a current cost result.
- A later source-stable, native-meter-valid diagnostic used bundle SHA-256 `04b6190af37b3c669398df88202a96671948e425a42fefe125ad12e8e2d7714f` and candidate auth profile SHA-256 `822fd5124bd2415cf9bc3e62fd01241d1335986b642bbe3a083784df20ae3dd2`. It measured 50 of 70 starts, 49 scheduled occurrences, 48 stale batches, 50 claim cycles, 16 host transitions and 690 root/operator API calls; it did not complete the 100-message workload. The first message request exceeded its configured 250 ms wall bound at 302 ms, so the guarded effect remained uncertain. Subsequent failures included a 3-of-4 discovery count, an active intake blocking archive, and the expected archived state missing after restart. This is a failed partial diagnostic, not a canonical pass. Its test-only USD 1 ceiling override did not alter the product's then-default USD 0.10 ceiling. At the authenticated budget-status snapshot, estimated settled cost was USD 0.141268440726, reserved USD 0.000678053608 and projected USD 0.144764111625; after that status observation settled, the durable ledger was USD 0.141316425128 settled plus USD 0.000558761206 held. These are local paid-rate estimates, not Cloudflare-rated spend. The partial diagnostic exceeded the then-configured USD 0.10 ceiling, so that candidate is not an acceptable full-day profile. The current approved USD 0.25 ceiling does not turn this incomplete run into acceptance. No source-stable complete 70-start/49-schedule/100-message result was accepted by this diagnostic.
- The cost owner reported a 37-case native Workerd gate for the safety-prelude accounting fix, and a narrow independent review accepted the exact delta at `97353ad1`: cold pre-auth SQL, the reservation request and the fixed 64-read/8-write settlement allowance are included once. A separate saturated-auth-GC Workerd fixture passed for `/device` and token revoke, removing 75 expired rows while preserving the metered DB path. These are focused component proofs, not full-day cost acceptance.
- The latest completed v3 canonical diagnostic was failed and incomplete: it stopped at 50/70 starts after an 826 ms placement exceeded its 750 ms fixture bound and returned 503 after a durable queued start; 49 scheduled occurrences, 48 stale batches and four discovery batches were observed, but 0/100 messages completed. Its later durable ledger snapshot was USD 0.119331707028 settled plus USD 0.000388540804 held, above the then-default USD 0.10 ceiling. This is local paid-rate estimation under a test-only diagnostic override, not Cloudflare-rated spend or a passing whole-day profile. The USD 0.25 ceiling is now approved, but this incomplete run remains non-accepting. The later v4 run completed the workload under a USD 1 test override; exact-head acceptance at the approved USD 0.25 ceiling is still required.

### Clean-start canonical numeric workload — 2026-11-06

- Exact report: `.local/tmp/normal-day-nDlpDC/normal-day-report.json`; it records 24 per-file Hub source hashes, `source_stable=true`, a valid native meter and no raw pre-run manifest hash. The Workerd bundle hash is `f66ecab0d766175c93905c5d2065305164df1a79c5989e961332ac960b3f95fc`; the test-only candidate profile hash is `07952a8cea168f27a5ca1277ec8fc2abcf7bce329bed5b93b7a67de1c1f32c53`.
- The complete numeric gate passed: 70 starts, 49 scheduled occurrences, 100 native messages, 50 claims, 48 stale-quota batches, four discovery batches, 20 retries, 16 host transitions, 690 role calls, archive/retry history, persisted restart, idle-zero, 1,440 host-resource heartbeats and 96 scheduled alert checks. `passed=true`, `canonical_passed=true`, `missing=[]`, and no workload errors. The separately reported legacy prompt inventory is `not_imported_by_authorized_clean_start`; 49 actual durable scheduled occurrences and ProjectWork tests cover schedule behavior. No Hub1 prompt records are imported.
- The first measured heartbeat allowance (16 writes/96 reads) failed closed when a real cold heartbeat settled at 54 writes/97 reads. The candidate was raised to 72 writes/128 reads; the host report cadence is 240 seconds with a 300-second Resource Manager TTL and at least a 60-second timing margin before scheduled starts. The host still rechecks local physical resources immediately before spawn. The canonical fixture records 360 reports per host per day.
- The three simulated UTC-day ledgers settled USD `0.001898063492`, `0.210326602600` and `0.008530971998`; each has `hard_stopped=0`. All-day settled estimates sum to USD `0.220755638090`, with a maximum held reservation of USD `0.006709123276`. Conservatively co-locating that total and hold on one UTC day, then retaining the USD `0.01` recovery reserve, gives USD `0.237464761366`, under the approved USD `0.25` ceiling by USD `0.012535238634`. This is an upper-bound model at exhausted-allowance marginal rates, not observed same-day Cloudflare spend.
- The final budget view reports provider reconciliation as `unknown` and health as `degraded`. Worker CPU, DO duration and storage use configured upper bounds; the rejected 500 ms CPU-refund proposal is not installed. The test-only profile is not provisioned to production. The simulation's projected run-rate is not a provider forecast. The Dapier alert receiver/restart-dedupe proof is a separate live-channel transport test with a synthetic amount, not a real cost-threshold crossing.

## Acceptance tests before activation

- Threshold strictly greater than USD 0.03, one alert per UTC day, UTC rollover, durable restart dedupe and repeated send failure/recovery.
- Per-category accounting with all allowances treated as exhausted; no missing/non-finite source can pass as zero. Verify Worker requests/CPU, DO requests/duration, SQLite reads/writes, storage and any KV dimension independently.
- Reserve/settle replay, lost response after effect, denied-over-ceiling fanout/retry, bounded concurrency/backoff, open/close circuit breaker, and restart-persistent runaway fixture.
- Operator override requires a scoped, expiring, audited credential; emergency stop preserves all source data and still permits native completion/release and cost reporting.
- Compare local estimates to Cloudflare rated usage; state attribution and provider lag honestly. Test missing account API permission and unavailable provider state as monitoring-health failure.
- Configure and API-read-back the two Cloudflare product policies only after checking existing policies and recipient source. Record their exact IDs, USD thresholds, billing period and recipients. Never update or replace an existing policy. Do not claim delivery testing for Spend Policies until Cloudflare supports it.
- Test an alert while Hub service is intentionally unavailable using an existing approved external watch/alert route; no new Grok routine and no provider/model invocation.

Production activation remains gated on runtime integration, clean-start production configuration, the exact-head approved-cap full workload gate, and verified alert delivery. Billing API access is unavailable with the current credential; the Cloudflare monthly product backstop remains unconfigured and is tracked as a provider-access limitation, not treated as zero spend. Local and live-channel checkpoint evidence below does not certify the remaining production gates.

## Public integration checkpoint (4 November, local Workerd)

The current source includes a fail-closed public pre-routing reservation in
budget-edge.ts. Every normal public request obtains its durable reservation
before authentication/application fanout. The adapter guards direct namespace
call count and wall deadline, and retains reservations after ambiguous effects.
It conservatively settles the configured envelope with coverage labelled
upper_bound; it does not report these estimates as observed CPU or provider bills.

HUB2_BUDGET_ENVELOPES and HUB2_BUDGET_ENVELOPES_PART2 have no production
fallback. In production release modes, both bindings are required. Concatenate
their exact UTF-8 contents, in that order, to obtain one JSON document with
version:1 and normal/safety plans. Split the document at a UTF-8 character
boundary so each value is at most 5,000 bytes. A missing, truncated, or invalid
pair fails closed before effects. Local fixtures may still provide one complete
JSON value when no production release mode is set. Each plan requires all
fourteen BudgetDO metric units, max_do_calls
(1?64), max_wall_ms (100?60000), and a provenance description.
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
