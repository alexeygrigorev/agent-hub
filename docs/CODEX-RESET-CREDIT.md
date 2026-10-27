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
