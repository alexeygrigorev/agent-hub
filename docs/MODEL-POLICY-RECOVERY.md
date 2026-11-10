# Model policy recovery protocol

This feature provides standalone policy recovery. It does not install a policy,
change deployment workflows, or integrate automatic application into the deploy
transaction. Those actions remain separate deployment-owner work.

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
Recovery operations require an absolute `--receipt` path and a full lowercase
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

## Future deployment integration boundary

An old unconfigured GET404 without a version cannot prepare recovery: the helper
refuses rather than guessing version zero. Legacy show/apply remain compatible,
but standalone protocol tests do not prove first-deploy recovery against every
old server. The later transaction may prepare after compatible candidate code
is healthy, but must persist the receipt before any policy mutation.

The later serial deploy transaction must protect the policy receipt before apply,
apply the exact reviewed configuration after compatible candidate code is healthy,
and retain recovery metadata through public smoke verification. Restore the policy
**before checking out code that may lack the tombstone recovery protocol**. If
restoration or CAS fails, retain metadata and report incomplete recovery. Never
restore an entire SQLite backup to roll back policy: that would discard concurrent
user writes. Initial-unconfigured rollback needs this compatible server protocol
and cannot be implemented by an ordinary empty policy PUT or row deletion.

No production apply, rollback, deployment or recovery drill has been performed
as part of this source implementation.
