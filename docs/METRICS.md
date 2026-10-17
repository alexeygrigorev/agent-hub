# Hub sanitized operational metrics (`M9`)

Status: source contract for an independent Hub receiver and host adapter.
The Agent Dashboard accounting engines stay host-side producers. This
milestone does not change their schemas or algorithms, does not copy raw
stores into the Hub, and does not claim producer-owner ACK or live
ingestion.

Routes after collision check against Hub `6cf0c18`: `POST /api/metrics`,
authenticated `GET /api/metrics`, authenticated page `GET /metrics`
(`/metrics.html`). No unauthenticated metrics port, new secret, cloud
service, timer, or profile launch.

## 1. Who may write and who may read

Write (`POST /api/metrics`) requires a current enrolled **host** bearer
(`credentials.kind = host`, scope `host`, not revoked, not expired). The
Durable Object binds provenance from that credential: `host_id` is
`credentials.machine_id`, which must equal the live `machines.id` whose
`credential_id` is this credential. JSON `host_id`, spoof headers, and
query `host_id` cannot select another machine. After the body and digest
are awaited, the same credential kind, scopes, expiry, revocation, and
machine binding are revalidated with a fresh clock inside the durable
write. A check that only ran before the body is not sufficient.

Host refresh (`POST /api/token/refresh`) atomically rotates
`machines.credential_id` and stored metrics bindings from the validated
predecessor to the new credential inside the same transaction, only when
the binding still equals the predecessor and the same live machine
incarnation. Stale, foreign, rebound, or deleted/re-enrolled families never
overwrite a new registration. Old tokens stay denied, replay revokes the
family, and a legitimate refresh preserves the prior snapshot instead of
purging it.

Refused (typically 401/403): foreign host, ordinary agent, job,
routing-client, human/admin session, revoked, expired, rebound host
credential, unregistered machine. A dashboard session never grants
machine write, even with a valid CSRF token.

Read (`GET /api/metrics`):

- Host bearer: own latest snapshot only.
- Admin human session: CSRF/origin checks; explicit `host_id` for a full
  snapshot, or bounded `limit`/`after` pagination of latest summaries.
- Agent/job/routing tokens: 403. Reads never return another project's
  private operational facts to an agent token.

`GET /metrics` is a private static page served only after the existing
session gate in `src/worker.ts`.

## 2. Storage

`MetricsStore` initializes SQL inside the existing Hub Durable Object.
No new Durable Object or database. Tables:

- `host_metrics_latest`: one current facts row per `host_id`.
- `host_metrics_idempotency`: bounded `(host_id, request_key)` retry
  index with digest, `as_of_ms`, and byte size.

Caps (enforced in the write transaction): body 32 KiB streaming;
8 idempotency rows per host; 64 global idempotency rows; 64 global
latest rows; 256 KiB stored bytes per host; 2 MiB global across all
retained latest payloads and retry receipt bytes. Oldest non-latest
retry rows are dropped first. Stale latest rows (`age >= 7200 s`) may
be retired to admit a new host. Protected (non-stale) latest rows do
not bypass the cap: if nothing evictable remains, the write is refused
(`507 capacity_exceeded`) and rolled back. Postcondition: every retained
payload and receipt byte fits the caps. This is not an unlimited
append-only log.

Idempotency: after current authorization, a retry with the same
`request_key` and payload digest returns the original snapshot and its
original `received_at` while it remains in the bounded retry window,
even when a newer latest snapshot exists. Same key with a different
digest is 409. A previously unseen snapshot whose `as_of` is older than
the stored latest cannot overwrite newer facts (409 `out_of_order_as_of`).
If that original receipt has already been evicted from the bounded
window, the retry is treated as a new old write and is refused rather
than reconstructing an unbounded archive.

Deletion and re-enrollment: a write or read that sees a missing machine,
an expired/revoked/wrong-kind/wrong-scope credential, a rebound
`credentials.machine_id`, or a live `machines.credential_id` that is not
the presented credential, does not revive the previous host-owned
snapshot. Retention deletes rows whose host is gone or whose stored
credential is no longer a current valid host binding.

R4 failure-cause deletion guard: a *denied* stale caller never purges a
valid successor's rows. On authorization failure only rows still bound to
the caller's own invalid/displaced credential are retired
(`host_id` + `credential_id` conditional); rows bound to the live
credential survive on read, write, receipt, migration, and rollback paths.
The kind/scope/expiry/revocation/family/incarnation checks are unchanged;
only the deletion scope is constrained. A denied stale refresh descendant
sees 403, a revoked predecessor 401, while the current incarnation keeps
returning 200 with its latest and receipts intact.

Machine registration fencing (`POST /api/machines/register`): registration
confirms the caller's own live binding (same-credential re-registration,
which includes the binding just established by that credential's own
admin-approved device issuance) or claims an unbound (`NULL`) machine.
Takeover of a live foreign binding is refused with 403
`stale_machine_binding` and mutates nothing; the takeover itself is the
device-pairing authority's job (admin approve + token issuance sets
`machines.credential_id`). The update is compare-and-swap on the current
binding, so a lost race denies instead of overwriting. A superseded
original token (revoked by refresh) is denied 401; a refreshed stale
descendant 403. Unknown machine rows are 404, never silently created.
Legitimate first bootstrap, fresh explicit re-pairing/recovery, current
same-family refresh/re-registration, and unbound bootstrap registration
keep returning 200. No new pairing-proof fields are trusted from request
bodies.

Stable server-owned enrollment authority (R5): operator approval is the
sole origin of host authority. Each host approval mints a strictly
increasing per-machine integer (`device_pairings.approved_seq` and
`machines.approval_seq`, inherited by issued credentials and preserved
across refresh, `NULL` while pre-migration legacy) inside the same
approval transaction, independent of pairing creation order,
issuance/consumption order, client bodies, and clocks. Token issuance
revalidates the pairing's authority against the machine inside its
transaction after awaited signing: a delayed older approved code is denied
without consuming the code or rebinding the machine, so it never mints
newer consent; a legacy grant with no verifiable witness may bootstrap an
unbound machine only while no current witness exists there, and never
replaces an already bound one. Registration
enforces the same match for non-owners as defense in depth while the
currently bound owner keeps routine re-registration (approval witness and
bound owner are distinct; takeover happens only at genuine exchange).
Approval replay stays already-processed, issuance retry stays idempotent,
and genuine later approvals, ties, unbound bootstrap, and legacy owner
continuity keep working.

## 3. Snapshot DTO (`schema_version: "1.0"`)

```json
{
  "schema_version": "1.0",
  "as_of": "2026-10-17T12:00:00.000Z",
  "host_id": "mach_…",
  "request_key": "metrics:2026-10-17T12:00:00.000Z:ab12",
  "coverage": {
    "utilization": "observed",
    "usage": "partial",
    "accepted": "missing"
  },
  "gaps": [{ "code": "partial_coverage" }],
  "utilization": {
    "unknown": false,
    "projects": {
      "agent-branches": {
        "unknown": false,
        "unique_agents": 1,
        "total_agent_hours": 2.5,
        "coverage": 0.4,
        "unattributed_agent_hours": 0,
        "hourly_buckets": []
      }
    }
  },
  "usage": {
    "unknown": false,
    "projects": {
      "agent-branches": {
        "unknown": false,
        "total_input_tokens": 10,
        "total_output_tokens": 4,
        "total_cache_read_tokens": null,
        "total_cache_creation_tokens": null,
        "total_reasoning_tokens": null,
        "total_noncache_input_tokens": null,
        "has_unknown_tokens": false,
        "has_unknown_cache": true,
        "has_unknown_reasoning": true,
        "cache_coverage": null,
        "reasoning_coverage": null,
        "event_count": 1
      }
    }
  },
  "accepted": {
    "unknown": true,
    "accepted_outcomes": null,
    "task_count": null,
    "commit_count": null,
    "review_count": null,
    "projects": {}
  }
}
```

Rules:

- `as_of` is extended timezone-aware UTC (`YYYY-MM-DDTHH:MM:SS` with
  optional 1-9 digit fraction and `Z` or `+00:00` only), matching the
  receiver grammar on both sides with ASCII full-string validation:
  Unicode digits and a trailing newline are rejected by the host helper
  before config/credentials/HTTP, exactly as the receiver answers 400
  `invalid_as_of`. Compact `YYYYMMDDTHHMMSSZ` spellings,
  lowercase designators, nonzero offsets, and naive stamps are likewise
  rejected. Sub-millisecond precision is normalized to milliseconds on
  both sides (receiver `Date.parse` semantics), so `.123456789` `as_of`
  against `.123` bucket boundaries (and vice versa) validates.
  `as_of` may not be in the future relative to the server clock.
- Explicit `host_id: null` is rejected by the host helper before
  config/HTTP: on this host-bound receiver explicit null is universally
  different from omitted host identity (403 `host_id_mismatch`).
  Omission keeps working (credential binding).
- Every receiver-required nested project key must be present: a missing
  counter (e.g. absent `total_reasoning_tokens`) is not explicit `null`
  and is rejected before any network call, as the receiver answers 400
  `invalid_usage`. Explicit `null`, partial, and true-zero projections
  keep working on all posting paths through the shared validator.
- Utilization `hourly_buckets` is exactly 24 half-open UTC hours tiling
  `[as_of - 24h, as_of)`. No extra hours. Adjacent buckets abut. A native
  unknown utilization project with all counters null and an empty bucket
  list validates (the receiver fills it); a known project with an empty
  list is still rejected, as is a missing key.
- Counts are finite numbers or `null` (unknown). Booleans, `NaN`,
  `Infinity`, and negatives are rejected. Proven `0` is kept. Partial
  coverage is unknown, not a fake zero.
- Canonical project ids are
  `agent-branches`, `agent-dashboard`, `quota-launcher`,
  `agent-coordination`, plus visible `unattributed` when the producer
  reported that gap. Unsupported ids are a gap, not silently attributed.
- `accepted_outcomes` is distinct from `task_count`, `commit_count`, and
  `review_count`. Distinct reviews require agreed review-event identity
  (`review_id`/`review_ids`); reviewer labels alone keep `review_count`
  null. Entry evidence is validated before counting: empty objects,
  reviewer-only objects, and bool/object (non-string) feature, task,
  commit, or review identifiers are malformed and keep totals unknown,
  never observed zero; identifiers are never coerced with `str()`.
  A valid entry without review-event proof keeps the review TOTAL null
  even when a sibling entry carries a valid event. The same agreed event
  across canonical projects counts once in the aggregate, not once per
  project. True zero requires a validated observed empty window without
  unresolved acceptance coverage; gaps or malformed inputs preserve
  partial/unknown and null counts. A malformed canonical project makes the
  aggregate review total null even when a sibling project proves an event
  (independently proven feature/task/commit counts survive); commit-only
  nonempty evidence keeps review unknown and never invents review 0, which
  is reserved for genuinely empty validated input.
- Utilization, usage, and accepted share the same `as_of`.
- Quota percentages, cost, and money are omitted from Hub uploads.
  The server rejects those fields if present. Unknown/private nested
  keys (paths, prompts, text, session/conversation ids, accounts,
  credentials, transcripts) are rejected. Errors do not echo rejected
  values or raw bodies.
- Coverage enums: `observed`, `unknown`, `missing`, `unsupported`,
  `partial`. Gap codes: `missing`, `unsupported_project`,
  `unattributed`, `partial_coverage`, `producer_unknown`, `empty_window`.

GET responses add server `received_at` and `stale` (`age >= 7200 s`).
`Cache-Control: private, no-store`.

## 4. Host adapter

Public CLI `hub metrics post` reads **explicit** Dashboard report files
(`--hourly`, `--usage`, `--features`, or `--from-json`). It never opens
the Dashboard default private paths. Tests use fixtures only.

The adapter:

1. Projects an allowlisted summary (discards `sources.path`, identities,
   feature text, quota/cost).
2. Validates the DTO before POST. `post_metrics_snapshot` applies the
   same strict allowlist; an arbitrary dictionary is refused without a
   network call.
3. Uses the existing configured Hub HTTP client and host token. Tokens
   are not accepted on argv and are not logged.
4. Sends a stable `--key`. A lost response is retried with the same key
   and payload (no recomputation that would drift counters).
5. Surfaces missing/unsupported producer projects as `unknown` / gap
   codes. It does not patch the readonly producer.

## 5. UI

`/metrics` uses the installed official dakit vendor (shell, tokens,
semantic color/border rules, `.dk-select`). No one-off palette. All
values are assigned with `textContent`. The selected host identity is
visible. The host list is an accessible paginated selector; refresh
keeps the current host when it still exists and shows an unavailable
state when it disappeared. When the listing comes back empty while a
host was selected, the selector keeps that preserved unavailable option
selected (no silent switch to another host or to `No hosts`); an initial
empty state with no prior selection still renders empty. Detail and
refresh commits are fenced by request generation and current selection
across all awaited and catch paths: a delayed superseded response (or its
error) is ignored once the selection moved on, and a selection made
during refresh survives it. Auth, loading, empty, stale, unknown, access
denied, expiry, and service failure are distinct. Known cache-read,
cache-creation, reasoning, and noncache-input counts are shown separately
from coverage fractions. Reasoning is never added to output. Quota is
never converted to tokens or money.

## 6. Out of scope

Producer agreement, runtime ingestion on enrolled hosts, 24h observed
production data, and M11 physical adoption remain separate. This source
does not claim them.
