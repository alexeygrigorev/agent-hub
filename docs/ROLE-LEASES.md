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
the same epoch. Existing root APIs, scheduling defaults and legacy job profiles
retain their behavior; no Win35 root is enabled or renewed.

Before any consumer effect, `POST /api/jobs/{id}/execute` with the job bearer,
epoch and generation checks current config, enrolled credential, fresh machine
gate, deadline and lease inside the same SQL transaction as recording execution
intent on the job. Only its first response permits execution. A duplicate or an
uncertain response requires HOLD and reconciliation, never a blind retry.

The job lifecycle repeats the current binding checks at each role mutation.
SQL fencing cannot atomically fence provider or OS process starts. The current
real host backend supports root-run; real writer-run execution remains an M8b
gate. Unsupported consumers must HOLD before effect intent or execution. Source
tests use qualified fake consumers, without running providers or services.

At most 64 role configurations and 1024 role job receipts are admitted. Admission
prunes up to 64 completed records older than three days, with associated job
credentials, offers and health events. Unresolved execution intents are retained
and consume capacity even after fencing. Dated keys are rejected after their
UTC day, so pruning receipts cannot reactivate an old request. Capacity exhaustion
fails closed; it never drops uncertain work to permit another execution.

Independent review of the final source and head integration remain required.
