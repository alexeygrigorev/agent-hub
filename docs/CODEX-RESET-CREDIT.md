# Earned Codex reset credit adapter (3290b981)

The host module uses the maintained Codex app-server stdio contract. It inherits
the current managed account; it never opens another profile, copies credentials,
buys credits, or changes Hub admission. The default operation reads only:

```sh
PYTHONPATH=host python3 -m agent_hub_host.codex_reset_credit
```

After the exact-source review, full suite, CI and normal deployment gates pass,
the authorized operator can redeem the specific expiring credit returned by that
read. Confirm the account ID and exact credit ID from a fresh read:

```sh
PYTHONPATH=host python3 -m agent_hub_host.codex_reset_credit \
  --consume --account-id '<accountId>' --credit-id '<creditId>'
```

The adapter requires an available `codexRateLimits` credit expiring within 24
hours and a genuine quota window at least 85% used with a future reset. This
avoids spending a credit on healthy quota. The backend can still return
`nothingToReset` or `noCredit`; these remain distinct receipts, not success.
Expired credits and unknown types or identities are refused before mutation.

The private durable journal lives at
`$CODEX_HOME/hub-earned-reset/attempts.sqlite3` (default `~/.codex/...`). Preserve
it across host releases and retries. All adapter processes for the managed
identity must share that journal; do not copy an identity to bypass its lock or
create a second journal. Each account/credit has one logical attempt and UUID
idempotency key, committed before sending. A process lock spans commits and RPC
calls. An uncertain outcome blocks other credits; at most one retry sends the
same key. Do not delete the journal or switch keys to recover an uncertain
attempt. Escalate after the retry bound instead.

`reset` and `alreadyRedeemed` require a fresh provider read. If refresh fails,
the success receipt remains durable and a subsequent invocation only reads;
it does not consume again. Provider errors are redacted. `eventId` in command
output identifies the local audit receipt; it is **not a Hub event ID**.

For Q2, retain command output with the exact credit, expiry, before/after count,
provider outcome, timestamps, and idempotency key. `creditDecrementVerified`
requires both a decreased available count and the selected credit disappearing
or becoming redeemed. Missing details cannot prove success. No token balance is
inferred from the coupon. A weekly quota drop alone proves no coupon debit.

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

# Within five minutes, after verifying the returned identity/state and gates:
PYTHONPATH=host python3 -m agent_hub_host.reset_credit_workflow consume \
  --event-id '<earned-reset:UUID>'

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
ambiguous match or neutral banked factor prevents preparation. This creates an
explicit operator-mediated pacing decision; it does not claim a scheduled
redemption actuator exists. Preparation never changes quota or grants.

The server allocates one stable provider idempotency key per account/credit;
the native helper now accepts this key and rejects a conflicting journal key.
This is the necessary replacement of frozen component `48eedf0a`: native RPC
semantics remain the same, while multiple hosts cannot mint different keys for
the same Hub decision. Existing helper-only attempts with another key fail
closed and require operator reconciliation, not journal deletion.

The provider receipt remains an **operator attestation**, with an exact native
journal SHA-256, separate server receipt time/actor and matching decision/key.
The server does not call Codex itself. Provider no-op outcomes retain null
post-read fields, rather than invented quota. The workflow never falls back to
a local-only event when Hub transport fails. If preparation fails, no provider
call occurs. If the receipt upload fails after redemption, preserve the native
journal and retry uploading that exact receipt with `record --event-id ...
--receipt-file ...`; that operation never invokes Codex. A stale decision stops
new `consume` attempts. Use the retained journal to investigate unknown outcomes.

`link-work` reads existing SQL routing/launch/account, completed status, useful
reward event and fresh post-receipt ingested pool observation. It records those
facts without modifying the job, quota, admission, or reward. It refuses work
predating the reset, a different account, a forced model/runner, and work
completed after credit expiry. Other grant/work observations support the proof
owner's separate no-starvation audit; they never automatically establish it.

`GET /api/quota/reset-credits?event_id=...` returns durable decision, receipt and
work event IDs. `ac2_accepted` and `no_starvation_verified` stay false: the proof
owner must independently check the actual provider receipt, refilled quota
drawdown, useful output and other grants. Server evidence linkage alone is not
acceptance. No live call in this runbook has been executed by unit tests.
