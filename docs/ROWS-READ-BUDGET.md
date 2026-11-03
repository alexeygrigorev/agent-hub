# Durable Object rows-read budget (intake 8081a05a)

Target: low thousands of rows read per day (5,000/day); alert at 20,000/day.
Writes (intake e8a22f4c, green Cloudflare product ~$1/M): target low tens of
thousands/day, alert at 100,000/day (`HUB_ROWS_WRITTEN_ALERT_PER_DAY`).
Live meter: `GET /api/metrics/rows-read?hours=24&statements=30` reports per-route,
per-alarm-step and SQL-statement rows read AND written (per-statement writes
tracked since the written-budget alert shipped).

## Pre-change evidence

The last complete hour available before this change was 2026-11-02 23:00 UTC:
296,669,310 rows/hour, or 7,120,063,440 projected rows/day. A later partial
window on 2026-11-03, beginning at 00:22:25 UTC and lasting about 31 minutes,
recorded 829,802,499 rows total. The rates below normalize that partial window
to an hour; they are diagnostic estimates, not a full-hour measurement.

| Scope or statement | Rows in partial window | Approx. rows/hour |
| --- | ---: | ---: |
| `GET /api/sessions` | 626,565,682 | 1,212,700,000 |
| `alarm:intake_watchdog` | 67,753,656 | 131,100,000 |
| `GET /api/session-intake/credentials` | 43,345,783 | 83,900,000 |
| `POST /api/fleet/resources/report` | 26,856,918 | 52,000,000 |
| `GET /api/bus/inbox` | 15,144,769 | 29,300,000 |
| `PUT /api/fleet/sessions` | 11,883,046 | 23,000,000 |
| `GET /api/fleet/offers` | 10,623,762 | 20,600,000 |

Largest observed SQL contributors in that partial window:

| Hot statement | Rows | Calls | Rows/call (max) | Why it multiplied |
| --- | ---: | ---: | ---: | --- |
| Resource CPU estimate join over `session_resource_usage` and `session_cpu_usage` | 310,368,384 | 65,896 | 4,736 | Repeated history join while projecting sessions on `GET /api/sessions` |
| Watchdog session/protection join with JSON native-id extraction | 33,426,520 | 5,327 | 6,872 | Rebuilt authority lookup on each watchdog step |
| Session-control state scan | 26,069,500 | 5,748 | 8,994 | Repeated session-list reconciliation |
| Worker-access/credential existence join | 24,333,840 | 3,485 | 7,265 | Missing `(session_id,family_id)` lookup index |
| Intake task prefix fallback | 18,471,302 | 15,262 | 1,212 | `LIKE ? || '%'` did not use the case-insensitive id index |

These rows explain how thousands of rows per cursor became hundreds of millions
per hot route. The first fully complete post-change hour and an extrapolation
from at least one live hour are still required; this branch has not been
deployed and makes no post-change production claim.

## Changes in this branch (pre-deploy)

`RowsMeter` reads each cursor's `rowsRead` and `rowsWritten`, attributes it to
the current route/alarm scope, logs statements above the slow-read threshold,
and exposes totals and top statements through the live meter. The alert opens
or refreshes a liveness alert when a full-hour projection exceeds 20,000/day.

`WriteWatch` tracks per-table generations from SQL writes and trigger bodies.
Stable read-only statements can be memoized by exact SQL, bindings and source
generation; memo storage is bounded (512 entries, 2,000 rows and 256 KiB per
entry, 2 MiB total). Random/time-dependent statements, comma joins and unsafe
table functions remain on the SQL path. A cache hit still creates a metered
cursor with zero `rowsRead`.

| Hot path | Change |
| --- | --- |
| `GET /api/sessions` CPU estimate | Cache CPU history and cohort estimates on exact resource/CPU/RSS/config write stamps; skip unchanged resource upserts. A rebuild reads at most 500 recent rows. |
| Fleet session catalog, watchdog, liveness and idle audit | Write-through `fleet_sessions` rows on census/reap/phase changes; cold hydration uses indexed per-machine reads. The 30-minute idle audit shares this catalog for outcome verification and feature collection instead of issuing two retained-table scans per run. Canonical/session authority and resource evidence use per-machine stamps. |
| `GET /api/fleet/offers` | Cache only the empty response, keyed by all fleet/session inputs and a 20-second ceiling; active offers still use bounded indexed reads. |
| Admission/account status | Share a per-account in-memory result keyed by fleet/quota/resource writes with a 30-second ceiling. |
| `GET /api/liveness` | Read only indexed open/recent alert branches and bounded top-N history; live observed sessions use the write-through catalog. |
| Session/task and worker lookup | Add expression/index support for native binding, session/family credentials, due wakes and case-insensitive task prefixes. |
| Alarm work | Move idle work, watchdog/liveness checks, expiry, host/capacity history and bus retention to the existing five-minute cron. Alarms now service due jobs/wakes/webhooks and a durable retention backlog; 1-second retention re-arms cap at 30 before 60-second backoff. |
| `usage_quota_history` | Add `(account,provider,observed_at_ms)` lookup index; keep time-range reads on the time index and bound the 15-minute TBM replay to 4,096 rows. Schema initialization is once per SQL object. No new deletion or retention policy is added. |

The target is enforced by focused tests for repeat-poll zero reads, cache
freshness after writes, cold/warm observed-catalog reads, alarm backlog limits,
and query plans for the new indexes. The required pre-push runner and CI must
pass before this change is accepted. Live production readings remain necessary
to establish that cold starts and residual routes stay below 5,000/day.

## Quiet census (intake e8a22f4c)

PUT /api/fleet/sessions ran ~11 statements / ~560 rows per poll even when the
catalog was byte-identical (9.8M statements, 490M reads, 8.9M writes/day).
The handler now keys the canonical catalog (same fields, nulls and volatile
progress timestamps as the row compare) per machine in memory: on a key match
only the two capped freshness upserts run (3 statements, near-zero reads,
zero writes unless the minute cap elapsed). Changed censuses take the full
path unchanged (probes, adoption, reconciles, drain). The intake linkage
revision is deliberately not part of the key: it moves on every fleet-wide
intake write while adoption is idempotent and reruns on the next changed
census (credential/liveness paths read links live).
