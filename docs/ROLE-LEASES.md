# Configured automation role leases

M7 uses the current Hub Durable Object and its existing `leases` table. No role
is configured or enabled by default. This source contract does not establish
deployment, physical cross-host adoption, backend death detection or unattended
recovery acceptance.

An operator uses an existing authenticated admin session and its CSRF header to
`PUT /api/roles/{project}/{name}` with `expected_generation` and `config`.
Configuration contains `enabled`, `profiles: ["writer-run"]`,
`actions: ["execute"]`, explicit enrolled machine IDs in `hosts`, and
`ttl_seconds` (60–1200). Names are lowercase bounded identifiers; reserved
identity namespaces including root and principal are refused. Every successful
configuration write advances both generation and lease epoch and fences old
jobs. It snapshots the machines' current host credentials; rotation requires
explicit operator reconfiguration. Role custody grants no conversation
membership, human identity, native session identity or filesystem edit scope.

The enrolled host requests an actual job offer with
`POST /api/roles/{project}/{name}/jobs`: `generation`, `expected_epoch`,
`idempotency_key` (`YYYY-MM-DD:unique-key`, using the current UTC day),
`profile: "writer-run"`, and `payload: {"date": "YYYY-MM-DD"}`. The project
is derived from configuration. Arbitrary commands and arguments are refused.
It then uses the existing authenticated job claim, start, renew and finish API.
The claim is the lease acquisition CAS. Competing offers cannot both acquire
the same epoch. Writer claims also consult current active leases for the existing
writer-date exclusion, including both legacy and role jobs. Existing root APIs, scheduling defaults and legacy job profiles
retain their behavior; no Win35 root is enabled or renewed.

Before any consumer effect, `POST /api/jobs/{id}/execute` with the job bearer,
epoch and generation checks current config, enrolled credential, fresh machine
gate, deadline and lease inside the same SQL transaction as recording execution
intent on the job. Only its first response permits execution. A duplicate or an
uncertain response requires HOLD and reconciliation, never a blind retry.
An unresolved intent for the same profile and canonical payload also blocks a
replacement job with a different request key, across epochs and roles. A lease
takeover permits different approved work; it cannot resolve an uncertain prior
physical effect. There is no endpoint that clears an uncertain intent merely to
retry it. That reconciliation remains an M8b runtime/owner gate.

The job lifecycle repeats the current binding checks at each role mutation.
Identical start and completion replies can be replayed while the credential is
current and the role epoch has not advanced. Changed completion content conflicts.
Completed role credentials retain their empty scope list until their bounded
expiry so the completion receipt can be read again; terminal job state forbids
new effects or renewal. Reconfiguration revokes them immediately.
SQL fencing cannot atomically fence provider or OS process starts. The current
real host backend supports root-run; real writer-run execution remains an M8b
gate. Unsupported consumers must HOLD before effect intent or execution. Source
tests use qualified fake consumers, without running providers or services.
Role metadata is included in both poll offers and job claim replies. The host
checks the binding again before committing intent, checks the response against
the exact role/profile/payload, and stops role renewal on any refusal or transport
uncertainty. A consumer exception or uncertain first-action/start state retains
the intent for reconciliation. The legacy root dispatcher is unchanged.

No role grant substitutes for M6 source claims, routing placement or quota
reservation. This contract invokes the existing job consumer; it provides no
new fleet launch, command adapter or source-edit admission path. A future M8b
backend must still use those authorities wherever its effects require them.
Backend death detection, native process identity, cross-host clocks and physical
process fencing remain M8a/M8b/runtime acceptance gates. A stale process already
started outside SQLite cannot be stopped by a SQL transaction.

At most 64 role configurations and 1024 role job receipts are admitted. Admission
retires up to 64 abandoned expired offers and prunes up to 64 completed records older than three days, with associated job
credentials, offers and health events. Unresolved execution intents are retained
and consume capacity even after fencing. Dated keys are rejected after their
UTC day, so pruning receipts cannot reactivate an old request. Capacity exhaustion
fails closed; it never drops uncertain work to permit another execution.

Independent review of the final source and head integration remain required.

Validation covers authenticated HTTP against Miniflare's real SQLite: operator
CSRF and namespace boundaries, competing acquisition and effect requests,
execution intent rollback, positive claim/start/renew/finish, replay conflicts,
expiry/takeover/reconnect/restart, credential rotation/expiry/revocation, config
generation, host eligibility, clock rollback, bounded storage and retention.
The host stdlib suite exercises explicit fake effects, unsupported backends and
lost authorization/renewal responses alongside the existing root lifecycle.
