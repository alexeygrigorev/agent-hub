# Intake retention and capacity

Intakes, events, lifecycle audit and exact idempotency receipts are retained.
Terminal tasks are marked archived, preserving their full payload. Receipt age
and count do not delete data or refuse intake creation.

The hot admission aggregate includes UTF-8 task JSON and primary-key bytes for
non-archived, non-terminal tasks, grants and rules. Retained task archives and
replay receipts stay in separate SQLite tables/accounting outside that budget.
The hot budget is 128 MiB: room for roughly 1,000 maximum-size task records
(128 KiB each), compared with a few hundred current open tasks. Individual
request, task, event and record bounds still protect Worker memory.

The [Cloudflare limits](https://developers.cloudflare.com/durable-objects/platform/limits/)
include a 5 GB Free account storage budget.

The retained planning budget is 256 MiB, leaving substantial space below the
Workers Free SQLite Durable Object limit of 1 GB for indexes, page overhead,
and other Hub tables. This is an advisory budget, not an intake refusal.
At 80% of either budget, operator Hub status needs_attention and intake lists
surface intake_storage_high. Expand retention storage before exhaustion;
never resolve a capacity alert by closing or deleting intakes.

Operators can GET /api/intake/storage for one read-only transaction containing
UTF-8 byte totals, row counts, budget and warning. Events and original request
bytes are nested subsets of task bytes, not additional storage. Original request
bytes count the full intake events, including text and provenance. Audit bytes
are a JSON projection of the lifecycle rows. These logical byte totals exclude
SQLite page and index overhead. Legacy aggregate bytes reproduce the former
32 MiB check (hot tasks, grants, rules and all replay receipts).

Receipts preserve the complete historical response and canonical fingerprint.
No migration rewrites payloads. A large receipt archive is measured using SQL
aggregates rather than materialized into Worker memory during admission.
