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

## Write cuts (intake e8a22f4c, PR #2)

Green product (DO rows written ~$1/M) ran 16.4M/day on Nov 2 (~$16/day).
Top write scopes from the live meter: PUT /api/fleet/sessions 8.9M,
POST /api/fleet/resources/report 3.1M, checks:run_due 2.1M,
POST /api/fleet/launches/:id/reject 1.0M (3.25M calls, ~3.1M already
write-free 409s; UNIQUE(actor, request_key) rules out double-mints, so the
remainder is per-intent volume and needs admission-side quarantine).

- `checks:run_due`: identical verdicts skip the `hub_checks` UPDATE and the
  `hub_check_runs` INSERT; the 1000-row trim runs only when history grew.
- Census PUT: gating the two fleet-wide terminal reconciles on changed
  censuses was reverted: `account-capacity.test.ts` and `fleet.test.ts`
  require per-census settlement of verified no-start phantoms and terminal
  incarnations, and the reconciles write only on positive terminal evidence.
  (PR#2 keeps the checks identical-skip and the token persist cooldown.)
- `recordSessionTokens`: active-phase token upserts persist at most once per
  60s per session (`TOKEN_PERSIST_COOLDOWN_MS`); terminal phases always
  persist so the final count is never lost.

## Periodic job budget (design targets, intake e8a22f4c)

Design principle (AC1/AC3): steady-state polls read zero rows. Repeat
identical reads hit memoized zero-read cursors (WriteWatch) or are skipped
entirely by a write-stamp gate; only genuine fleet events (writes) cause
re-reads, bounded per run by the test pins below. Rows/day therefore scales
with fleet events, not with clocks. The DO alarm is a one-shot for due work
(`deleteAlarm()` when idle), not a polling tick.

Worst-case mechanical bounds (test pins x maximum runs/day) exceed the
guideline under pathological continuous-due-work; the expected column is the
quiet-fleet steady state the tests pin (repeat poll == 0 rows). Schema
existence probes (`sqlite_master` rewrites to `pragma_table_list`) are
memoized on the schema generation: every DDL bumps it, so a cached probe
stays exact until the schema changes and repeat polls read zero rows for
probes too.

| Job | Trigger | Worst case / run (test) | Expected / day, quiet fleet |
| --- | --- | --- | --- |
| `alarm:idle_work` | due wakes only, internally at most every 5 min | `SESSIONS*25` (first census) | 0 without due sessions/wakes |
| `alarm:bus_retention` | retention backlog only | 40 rows | 0 (skipped when no backlog) |
| `alarm:expire_messages` | due expiries only | 20 rows | 0 (indexed due scan, empty) |
| `alarm:jobs_alarms` | due jobs only | 100 rows | ~0 (no due alarms) |
| `alarm:webhooks` | pending deliveries only | 50 rows | 0 (empty pump) |
| `meter:flush` + hourly budget check | every 5 min flush; check once per hour | one ring upsert per active scope plus the total row; indexed two-hour summary | Conservative envelope ~4.4k rows/day at 12 active scopes: 13 × 288 ring rows plus at most 26 rows × 24 hourly checks |
| `alarm:host_history` | due sample only | O(1) history revision and bounded upsert | 0 when no host fact changed |
| `alarm:tbm` | quota/session/outcome writes | one write-stamp check; bounded 4,096-row replay after a real change | 0 without a relevant write |
| `tick:session_audit` | 30-min audit cadence | `SESSIONS*40` on the 150-session fixture; pending/outcome branches are indexed | 0 with no active sessions or outstanding actions; audit evidence follows session events |
| `tick:intake_watchdog` | 5-min cron, due queue and digest events | 2,077 rows on the 300-intake fixture; indexed due/queue branches | 0 with no open intakes or due work |
| `tick:intake_verifier` | 5-min cron, DONE claims only | 603 rows on the 300-intake fixture; indexed open-task selection | 0 with no claims awaiting verification |
| `tick:intake_triage` | hourly | 3,069 rows on the 300-intake fixture; indexed open-task selection | 0 with no forgotten intake candidates |
| `GET /api/fleet/offers` (per host, 10 s) | host poll | 400 idle / 60 repeat | 0 repeat (stamp cache); re-read on launch/session writes only |
| `PUT /api/fleet/sessions` (per host, 60 s) | host census | bounded primary-key lookups on change; unchanged census returns before SQL | 0 repeat; writes only on semantic change |
| `GET /api/session-messaging/credentials` (per host, 600 s) | host poll | sweep <= ~300 first run, then 0; list indexed + memoized | 0 repeat (stamp gate skips sweep, memo serves list) |
| `GET /api/session-intake/credentials` (per host, 600 s) | host poll | repeat 0; first run indexed (credentials_family) | 0 repeat; re-sweep only after session/credential writes |
| `GET /api/fleet/resources/watchdog`, `POST .../report` (report every 120 s) | host poll | revision-cached catalog; unchanged report returns before SQL | 0 repeat (test: repeat poll reads zero) |
| `GET /api/sessions` CPU estimates | dashboard/admin poll | <=500 recent rows per rebuild; shared cross-request cache | 0 repeat; rebuild on resource/CPU/RSS/config writes only |

The durable catalog markers record first-seen and completeness changes only.
While the DO is live, an authenticated report refreshes an in-memory per-host
freshness entry; a cold start uses the durable marker until the host reports
again. The machine heartbeat is not treated as proof that its native-session
census is current. Unchanged census and resource reports return before SQL.

The first scheduled-tick pass in the production-shaped budget fixture read
about 8.3k rows across scopes (150 sessions, 300 intakes, 3,000 retained audit
decisions and 3,000 queued messages). CI caps each step and each query (the
open-task join is bounded at two rows per eligible intake) and caps the total
stress-fixture pass at 12k. The quiet-fleet daily estimate assumes no active
sessions, open intakes, outstanding actions, or due work; it is a design
estimate, not a post-deploy measurement.

With 12 active meter scopes, the conservative meter envelope is about 4.4k
rows/day (counting one read for each ring upsert plus the two-hour indexed
summary). Other quiet-fleet scheduled jobs add close to zero without active
sessions, open intakes, outstanding actions, or due work. The 20,000/day
alert (`GET /api/metrics/rows-read`, `do_rows_read_budget`
liveness alert, deduped to one notification per open alert + hourly refresh)
stays silent at these levels. Noisy-fleet cost is proportional to real
placements, launches, credential mints and session transitions, not to poll
or tick rates.

Live baseline 2026-11-03 (intake note, pre-deploy of the census/estimate
fixes): meter 24h ~5.58B reads/day + ~17.0M writes/day; GraphQL Nov 2: 2.88B
reads, 16.4M writes. The remaining gap after this branch deploys is measured
with `GET /api/metrics/rows-read?hours=24&statements=30` (>=1h post-deploy
window) plus the Cloudflare GraphQL rowsRead/rowsWritten series; until the
live total is under the alert line the budget alert keeps firing as an
over_budget liveness alert (single notification, hourly refresh, no paging
storm) rather than a per-hour page.
