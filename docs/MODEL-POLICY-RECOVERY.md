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

The standing deployment credential is an independently revocable model-policy
service credential, provisioned by the deployment owner into
`/etc/agent-hub/deploy-credentials/model-policy-admin.json`, containing an
`access_token` string. The directory must be root-owned and private (0700), and
the regular, single-link JSON file root-owned and private (0600). Symlinks and
nonprivate files are refused. Keep this credential under the root-owned
`/etc/agent-hub` configuration tree, separate from service-owned database and
transaction storage. Do not change ownership of existing service data to install
a deploy credential. Provision it through the established private
credential channel; never put tokens in Git, workflow output, command arguments,
or this document. The service credential has only `model-policy:read` and `model-policy:write`;
it is not a human login, host enrollment, or general administrator. An absent or
untrusted standing credential requires the separately reviewed bootstrap flow
before policy application; the service API alone does not install that flow.
There is no assumption that this credential is already provisioned on the VM.

The transaction deliberately clears ambient `HUB2_TOKEN` for helper invocations
and passes this explicit `--token-file`. Neither `HUB2_BOOTSTRAP_TOKEN` nor
root's default CLI login is a substitute. Requests use the authenticated API on fixed
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

## Deployment service credential API

An authenticated, currently allowlisted human administrator can issue a dedicated
credential with `POST /api/deployment-credentials`. Both normal admin bearer
and browser-cookie authorization are supported; cookies retain their existing
same-origin and CSRF checks. The exact body is
`{"request_id":"<canonical lowercase UUIDv4>","label":"vm-policy-deploy"}`.
Labels contain 1–80 ASCII letters, digits, spaces, dots, underscores, colons or
hyphens and begin with a letter or digit. Caller-selected scopes, principals,
purpose, expiry and other fields are refused.

The 201 response has `Cache-Control: no-store`, `token_type: "Bearer"`, a single
64-character hexadecimal `access_token`, and `credential` metadata:
`id: "deployment:<request_id>"`, `request_id`, `kind: "service"`,
`purpose: "model-policy"`, the label, fixed scopes
`["model-policy:read","model-policy:write"]`, `expires_at: null`, `created_at`
and `revoked: false`. Tokens are random, independently hashed in the existing
credentials table, and have no refresh credential or shared human family.
Ordinary CLI refresh does not rotate them. They are non-expiring, explicitly
revocable credentials; removing the issuing principal's email from the admin
allowlist also invalidates them on the next request. Their owner must rotate or
revoke them when deployment authority is retired.

A service token can use only `GET`, `PUT` and recovery `POST` on
`/api/model-policy`, subject to all existing validation and CAS rules. It cannot
start sessions, enroll hosts, mint credentials, use MCP or refresh itself.
`GET /api/model-policy` is a read-only credential verification request: HTTP200
returns the configured policy; typed HTTP404 `model_policy_unconfigured` with
an integer version is a valid unconfigured policy. No private internal route
is required. Other API authority is never inferred from this capability.

Persist a token-free issuance intent before POST. A repeated request ID never
returns a secret again: it yields HTTP409
`deployment_credential_already_issued` with the deterministic credential ID.
A conflicting label or issuing principal yields
`deployment_credential_request_conflict`. After a lost creation response, an
administrator must revoke that deterministic ID before trying a new request ID;
never blindly mint another credential or claim the lost token was recovered.
`POST /api/deployment-credentials/deployment:<request_id>/revoke` with `{}`
returns `{id,revoked:true,replay:false}`; repeat revocation returns `replay:true`.
An unknown ID returns HTTP404. A retired request ID is never resurrected.

Admin `GET /api/deployment-credentials` returns at most 32 metadata items and a
`next` cursor, accepted as `?after=<deployment:id>`. Responses contain no tokens
or hashes. At most 32 credentials can be active. Revoked rows remain as durable
idempotency tombstones; retained storage grows with explicit operator issuance,
not session starts. No schema migration or direct database minting is needed.
Older code without service verification rejects these tokens; policy recovery
must finish before checking out such a release.
The required Control Node test runner exercises the real Worker/auth/Control
path and SQLite state, including misuse and lost-response cases.

## One-time bootstrap handoff

The standing service file is separate from the temporary operator bootstrap
file `/etc/agent-hub/deploy-credentials/model-policy-bootstrap.json`.
The latter is root-owned/private and carries the approved interface:
`schema: "hub2-model-policy-bootstrap-v1"`,
`intent: "exchange_model_policy_service_credential"`, a canonical UUID
`request_id`, full `target_sha`, `access_token`, `refreshed_at_ms`,
`expires_at_ms`, and `ttl_seconds` from the actual one-time refresh result.
It must be explicitly prepared by the authorized deployment owner; the VM must
not automatically refresh or recopy an ordinary shared human login.

The transaction streams the credential helper from the verified candidate Git
object for precheckout structural preflight. This step performs no HTTP request:
the old server may not understand service credentials. A valid standing service
needs no bootstrap. A historical access-token-only file is never treated as a
service and needs a fresh, explicitly bound bootstrap handoff to proceed.

After candidate health and schema compatibility checks, the transaction verifies
candidate HEAD and copies the reviewed helper into its protected transaction
directory. It then invokes `exchange --post-health --target-sha <candidate>`
before canonical-policy preparation or apply intent. Production uses the fixed
loopback URL and root-private paths; the disposable test mode alone invokes the
same helper core with an owned directory, loopback fixture and current test UID.
Ambient bearer/config environment variables are cleared. The VM never refreshes
the human bootstrap credential. An existing service is only verified, never
reissued or replaced through bootstrap fallback.

Before issuance, the helper fsyncs a token-free intent binding request ID, target
SHA, API URL, label, purpose and the old output digest. It writes any received
service candidate privately before validating its read authority and atomically
replacing the standing file. The old forensic file is preserved on uncertainty.
Creation receipts, bootstrap files and received candidates stay in the private
credential directory; neither service nor human tokens enter deploy metadata,
command arguments or output. Their retention/reconciliation is owner-managed.

The transaction also records exchange start before launching its privileged
helper. The privileged launcher records completion only after its actual child
exits. A killed shell/sudo/launcher cannot manufacture that receipt. Before any
rollback transition, the transaction checks these records and inspects private
issuance intent, received candidate and installed output under the helper lock.
A confirmed pre-issuance failure can roll back normally. An incomplete process,
unknown issuance, malformed evidence or busy helper keeps candidate code and
protected metadata in place, preserving the supported revoke API for explicit
operator recovery. This also covers SIGKILL after server issuance and before a
response reaches the helper; no JSON error response is required to detect it.
The transaction never automatically remints, revokes or force-clears this state.

Required release checks run the helper regressions and composed transaction
cases. These are source/offline guarantees; provisioning and live exchange still
require the deployment owner's reviewed handoff and release verification.

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
