# Earned Codex reset credit adapter (3290b981)

The host module uses the maintained Codex app-server stdio contract. It inherits
the current managed account; it never opens another profile, copies credentials,
buys credits, or changes Hub admission. The default operation reads only:

```sh
PYTHONPATH=host python3 -m agent_hub_host.codex_reset_credit
```

After the exact-source review, full suite, CI and normal deployment gates pass,
the Hub asks Alexey for approval, and only Alexey redeems the specific expiring
credit — in the provider's own console or app. This adapter and workflow never
send a provider redemption (no `account/rateLimitResetCredit/consume`); they
read, prepare Hub decision receipts, and attest what Alexey performed.

The eligibility read requires an available `codexRateLimits` credit expiring
within 24 hours and a genuine quota window at least 85% used with a future
reset. This avoids asking to spend a credit on healthy quota. Expired credits
and unknown types or identities are refused before any Hub preparation.

The private durable journal lives at
`$CODEX_HOME/hub-earned-reset/attempts.sqlite3` (default `~/.codex/...`). Preserve
it across host releases. It retains receipts of past attempts; this toolchain
no longer creates new attempts. Do not delete the journal to recover an
uncertain past attempt; escalate instead.

An acknowledged reset requires a fresh provider read as part of its evidence.
Provider errors are redacted. `eventId` in command output identifies the local
audit receipt; it is **not a Hub event ID**.

For Q2, retain command output with the exact credit, expiry, before/after count,
provider outcome, timestamps, and idempotency key. Proof that the coupon was
consumed requires both a decreased available count and the selected credit
disappearing or becoming redeemed. Missing details cannot prove success. No
token balance is inferred from the coupon. A weekly quota drop alone proves no
coupon debit.

The live-proof owner must then refresh genuine quota through normal host
collection, retain its Hub ingestion and pacing event IDs, and show useful
Hub-placed work drawing down the refilled quota while other grants continue to
receive work. This module does not clear holds, lower the 15% reserve, grant
admission, synthesize quota, or implement autonomous redemption. Provider
mutation is never part of unit tests. Intake stays open until all live evidence
is verified.

Contract verified against installed Codex 0.162.1 generated schemas and
[official app-server documentation](https://learn.chatgpt.com/docs/app-server#8-earned-rate-limit-resets-chatgpt).

## Hub-correlated operator workflow

The standalone helper above is a component, not Q2 acceptance. Use the workflow
below for a server-recorded decision and evidence chain. All native mutations
still require the exact-source review/full/CI/deployment gates first.

```sh
# Read-only provider observation plus durable Hub decision; no redemption.
PYTHONPATH=host python3 -m agent_hub_host.reset_credit_workflow prepare \
  --account shared-codex --credit-id '<exact native credit ID>'

# Alexey approves and performs the reset in the provider's own console.
# The Hub and agents never redeem it.

# After normal collection refreshes genuine quota and normally routed useful
# work completes (no manually forced model/runner):
PYTHONPATH=host python3 -m agent_hub_host.reset_credit_workflow link-work \
  --event-id '<earned-reset:UUID>' --routing-job-id '<actual Hub routing job ID>'
```

`POST /api/quota/reset-credits` is behind the existing TBM admin authentication
and cookie CSRF checks. `prepare` stores server time/actor, the actual latest
controller tick, fresh stored pool observations, unique EDF tranche matched by
account/expiry, current routing factors, other active grants, and the
operator-attested native credit identity. A global kill switch, stale data,
ambiguous match, disabled account, explicit pool kill or global engine health hold prevents preparation. A quota-reserve halt can require a reset; its actual routing factor is retained unchanged. The EDF reset-needed decision is separate from runnable-work eligibility. This creates an
explicit operator-mediated pacing decision; it does not claim a scheduled
redemption actuator exists. Preparation never changes quota or grants.

The server allocates one stable provider idempotency key per provider account ID/credit, independent of Hub alias;
retained receipts carry this key and `record` rejects a receipt whose key
does not match the decision. This is the necessary replacement of frozen
component `48eedf0a`: receipt identity semantics remain the same, so
evidence from multiple hosts cannot be confused for the same Hub decision.
Attestations carrying another key fail closed and require operator
reconciliation, not journal deletion.

The provider receipt remains an **operator attestation**, with an exact native
canonical receipt JSON SHA-256 (not the SQLite database hash), separate server receipt time/actor and matching decision/key.
The server does not call Codex itself. Provider no-op outcomes retain null
post-read fields, rather than invented quota. The workflow never falls back to
a local-only event when Hub transport fails. If preparation fails, no provider
call occurs. If the receipt upload fails after a reset Alexey performed,
preserve the native journal and retry uploading that exact receipt with
`record --event-id ... --receipt-file ...`; that operation never invokes Codex.
GET revalidates current controller freshness, EDF tranche, account, kill and
health policy for the evidence view. Use the supported read-only recovery
command even after five minutes:

```sh
PYTHONPATH=host python3 -m agent_hub_host.reset_credit_workflow reconcile \
  --event-id '<earned-reset:UUID>'
```

This reads the exact retained account/credit/key and fresh provider state; it
never sends consumption or creates a journal. Pending/unknown outcomes remain
unknown even if the coupon disappeared. Only an acknowledged reset can recover
a missing refresh; recovery keeps the original receipt hash and does not
rewrite the journal. An existing refill baseline is preserved. A prepared event
with an acknowledged reset but no baseline returns `recovered_receipt` and
`requires_record: true` with exit 2. Retain that exact JSON receipt privately,
inspect its recovery hash and refresh, then upload it with `record`. This
prevents later reads silently replacing the chosen baseline. A prepared event
without any retained attempt cannot be reconciled into success or given a new
key. Do not delete evidence or mint another attempt to bypass expiry.

`link-work` reads existing SQL routing/launch/account, completed status, useful
reward event, retained native post-refill baseline and fresh post-work ingested pool observation. It requires a positive percentage-point drawdown in the exact same reset window and duration, with normal ingestion after job completion. It records those
facts without modifying the job, quota, admission, or reward. It refuses work
predating the reset, a different account, a forced model/runner, and work
completed after credit expiry. Other grant/work observations support the proof
owner's separate no-starvation audit; they never automatically establish it.

`GET /api/quota/reset-credits?event_id=...` returns durable decision, receipt and
work event IDs. `ac2_accepted` and `no_starvation_verified` stay false: the proof
owner must independently check the actual provider receipt, refilled quota
drawdown, useful output and other grants. Server evidence linkage alone is not
acceptance. No live call in this runbook has been executed by unit tests.
