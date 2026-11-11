# Model policy recovery protocol

The VM deploy transaction applies the reviewed commit's canonical model policy
for active releases, using the same recovery protocol as the standalone helper.
Bootstrap releases do not apply policy. Deployment execution and credential
provisioning remain deployment-owner work.

## Server contract

`PUT /api/model-policy` retains ordinary nonempty candidate writes. Admin-only
`restore_unconfigured: true` permits an empty candidate list only with an explicit
nonnegative integer `expected_version`. It writes a tombstone into the existing
single policy row; it never deletes the row or resets its version. Both public
policy GET and internal picker GET return HTTP 404
`{"error":"model_policy_unconfigured","version":N}` for an initial or restored
unconfigured state. Initial absence is version zero. A restored state keeps its
new monotonically increasing version, so an old version-zero write cannot win.

All CAS checks precede digest idempotence. A stale writer receives 409 even when
its requested data already matches. Equal policy data at the correct version is
unchanged and does not increment the version. Existing read/write authorization
is preserved. No schema DDL or new endpoint is introduced.

## Standalone helper

Existing `show` and `apply [--expected-version N]` commands remain available.
Recovery operations (`prepare`, `apply`, `rollback`, and `commit`) require an absolute `--receipt` path and a full lowercase
`--source-sha`. The receipt's immediate directory must already exist, be owned
by the invoking user and have mode 0700. Receipt and lock files are 0600;
symlinks, nonregular files, hard links and nonprivate receipts are refused.
The helper serializes concurrent invocations with a file lock and atomically
replaces/fsyncs its bounded receipt before and after mutations.

Example shape, using deployment-owner-supplied reviewed values:

```
python3 scripts/apply-model-policy.py prepare --url https://hub.dtcdev.click \
  --file /protected/candidate-model-policy.json --source-sha <full-reviewed-SHA> \
  --receipt /protected/transaction/model-policy.json
python3 scripts/apply-model-policy.py apply --url https://hub.dtcdev.click \
  --file /protected/candidate-model-policy.json --source-sha <same-SHA> \
  --receipt /protected/transaction/model-policy.json
python3 scripts/apply-model-policy.py rollback --url https://hub.dtcdev.click \
  --source-sha <same-SHA> --receipt /protected/transaction/model-policy.json
```

The receipt binds the normalized server URL, source SHA and canonical candidate
content hash. Apply rechecks the supplied file against that protected content.
The helper binds these values; it does not attest that an arbitrary file came
from Git. The deployment owner must extract and verify the candidate file from
the exact reviewed source commit before preparing it. Rollback uses the protected
before snapshot and does not depend on a mutable checkout's policy file.
URLs reject credentials, paths, queries, fragments and redirects. Plain HTTP is
allowed only for loopback testing. HTTP exchanges have ten-second timeouts and
128,000-byte response bounds; receipt metadata is bounded to four times that
size to hold before/desired/applied data. Tokens come from `HUB2_TOKEN` or the
existing CLI token file; they never enter receipts or printed recovery errors.
Receipt files contain policy configuration and must remain protected.

`prepare` snapshots current validated policy data and version before any mutation.
Repeating prepare preserves that snapshot rather than silently adopting a newer
operator policy. Transactional apply and rollback always use the saved CAS
version. A verified success response establishes the applied version; a replay
checks that it remains current. Unchanged candidates establish no new write to
undo, so their rollback never changes an intervening operator policy.

## Ambiguous outcomes and recovery

A lost apply response does not prove which writer installed the observed data.
Even the expected next version with identical candidates might belong to a
competing operator. GET reconciliation can show that the old state remains safe
for a same-CAS retry, or mark the receipt uncertain, but cannot grant rollback
ownership from equality. An uncertain apply fails with
`manual_reconciliation_required: true`; rollback refuses it. This also covers a
crash after server success but before the success receipt was persisted.

For rollback, a lost response can be resolved when GET observes the exact expected
restored data/version: no new write ownership is acquired. If an operator changed
the policy in between, rollback preserves that change, fails explicitly, and
retains its receipt. Replaying rollback must never issue a second restore over
that operator change. Corrupt or mismatched metadata is not permission to force
an overwrite. Do not delete an unresolved receipt to manufacture ownership;
manual reconciliation requires reviewing live policy and protected transaction
evidence before any separately authorized action.

## VM deployment transaction

Before an active deploy, the deployment owner must provision an existing active
Hub operator credential with `operator:admin` into
`/etc/agent-hub/deploy-credentials/model-policy-admin.json`, containing an
`access_token` string. The directory must be root-owned and private (0700), and
the regular, single-link JSON file root-owned and private (0600). Symlinks and
nonprivate files are refused. Keep this credential under the root-owned
`/etc/agent-hub` configuration tree, separate from service-owned database and
transaction storage. Do not change ownership of existing service data to install
a deploy credential. Provision it through the established private
credential channel; never put tokens in Git, workflow output, command arguments,
or this document. The deploy transaction neither creates nor enrolls an admin.
An absent or untrusted credential refuses an active deploy before checkout.
There is no assumption that this credential is already provisioned on the VM.

The transaction deliberately clears ambient `HUB2_TOKEN` for helper invocations
and passes this explicit `--token-file`. Neither the bootstrap token nor root's
default CLI login is a substitute. Requests use the authenticated API on fixed
loopback `http://127.0.0.1:8787`; no new authorization bypass is introduced.

After candidate health and schema compatibility checks, while automatic recovery
is still armed, the transaction copies the candidate helper and
`config/model-policy.json` into its root-private recovery directory. It prepares
a receipt bound to the deployed SHA and URL, durably records apply intent, then
applies through `apply_recoverable`. The policy is applied before deployment is
marked applied, and its receipt remains protected throughout public smokes. Each deployment uses its
unique transaction token both in the protected `model-policy-<token>` directory
and in the receipt. Each operation captures that token and source SHA once;
a stale child cannot open a later deployment's receipt, even for the same SHA.
Standalone receipts without a transaction token remain compatible; a token-bound
receipt requires the identical `--transaction-token` on every helper operation.

An unchanged canonical policy keeps its version. Finalization calls helper
`commit`, which holds the receipt lock while reading the API to verify exact
policy data/version and durably recording the `committed` phase. It never
applies a prepared receipt or acquires new policy-write ownership. A delayed
rollback either finishes before that check (causing commit to refuse), or sees
the committed fence and refuses to restore. This holds even if the privileged
helper survives a killed transaction shell and no longer holds its deploy lock.
The fence is written before CLI publication commitment and metadata retirement.
A failure after sealing policy is forward-only: retain candidate code and
metadata, fix the finalization failure, and retry commit. Rollback cannot undo
a committed policy receipt. Repeated commit rechecks current data/version and
cannot overwrite intervening operator changes.

Rollback restores policy using the protected candidate helper **before checkout
of the previous release** and before CLI-pointer restoration. It never restores
a SQLite backup, so concurrent user writes remain intact. Failures before policy
apply intent can restore code normally. After intent, a missing/corrupt receipt,
unproved outcome, unavailable credential/API, or policy CAS conflict stops
recovery with candidate code and protected metadata retained. Review that state
through the existing manual reconciliation process; deleting evidence or forcing
an overwrite is not recovery. A lost successful apply response may require this
manual step even when GET currently shows canonical candidates. A lost rollback
response can be reconciled by the helper's exact restored-data/version check.

The transaction lock serializes live transaction shells. Per-receipt locks and
durable commit fences protect surviving privileged children. These protect helper
operations; policy CAS still protects against external operator changes. Tokens
are not copied into the transaction directory. The protected helper, canonical
configuration and receipt are removed only after successful final verification
or completed rollback.

## Compatibility boundary

An old unconfigured GET404 without a version cannot prepare recovery: the helper
refuses rather than guessing version zero. Legacy show/apply remain compatible,
but standalone protocol tests do not prove first-deploy recovery against every
old server. The later transaction may prepare after compatible candidate code
is healthy, but must persist the receipt before any policy mutation.

Initial-unconfigured rollback needs the compatible server's tombstone recovery
protocol and cannot use an ordinary empty policy PUT or row deletion. A legacy
invalid stored policy must be separately reconciled by its authorized owner;
automatic deployment never weakens current model-policy validation to restore it.

No production apply, rollback, deployment or recovery drill has been performed
as part of this source implementation.
