# Durable Object rows-read budget (intake e8a22f4c, parent 8081a05a)

Target: low thousands of rows read per day (about 5,000 is a guideline), alert at 20,000/day.
Live meter: `GET /api/metrics/rows-read?hours=24` (scopes `route:*`, `alarm:*`, `tick:*`, `init:*`).

## Rules

1. Host-polled and periodic work is O(changed rows), never O(history). A query on a retained table
   (`jobs`, `fleet_launches`, `fleet_sessions`, `session_control_*`, `intake_tasks`) is either an
   indexed lookup bounded by its result, or runs only after a write marked it dirty.
2. No timer-driven scan: a periodic job reads one due-time or dirty marker, or nothing.
3. An index on a column that `rowsWritten`-checked UPDATEs modify changes `rowsWritten` (index
   maintenance is counted). `session_control_commands.state` must not be indexed: claim relies on `rowsWritten === 1`.
4. Every hot route has a rows-read bound test (`test/rows-read-*.test.ts`) that seeds production-shaped history.

## Phase 2 changes (request path)

| Route | Before (live, 18:45-18:52 Berlin) | Change |
| --- | --- | --- |
| `PUT /api/fleet/sessions` | 23.0M rows / 6.4 min, up to 243k rows/call | Reported-native PK lookups; live-phase gone detection via `fleet_session_machine_phase`; adopt, exit settlement and late-link pass only for changed natives (link pass for all natives only when the intake linkage revision moved); `hubManaged` via the `session_control_native` expression index instead of a scan per reported native |
| `GET /api/fleet/offers` | 23.7M rows / 6.4 min, up to 56k rows/call | Reconciliation candidates as per-state indexed branches (`fleet_launch_failed_recent` partial index); dispatch/reconcile/drain at most every 20 s unless a fleet write marked maintenance dirty |
| `POST /api/host/poll` | full `jobs` scan | partial index `jobs_offered` (offered rows only); also serves the alarm's offer-expiry lookups |

## Remaining periodic jobs (estimates from code reading, not measurements)

Ranked by likely rows/day; each must become change-driven (dirty flag or stored next-due) to reach the target.

| Job | Cadence | Main cost | Change-driven plan |
| --- | --- | --- | --- |
| TBM tick (`runTbmTick`) | <= every 4 min | terminal-session reward sweep; pool rollups | queue of unrewarded sessions; skip unless quota/outcome rows changed |
| Intake watchdog | every alarm, 60 s gate | 64 open tasks x snapshot | dirty on task/session/wake changes; next-due = earliest queue `next_at` |
| Host history sample | every alarm | per-account `reconcileCapacity`; `DISTINCT machine_id` scans of `fleet_launches` | sample on host writes; counters instead of scans |
| Ops liveness backlog | every alarm | up to 1,001 projection rows per minute | persisted counts updated on intake/session changes |
| Verifier / triage | 30 min cron | full `intake_tasks` scan | projection index; dirty on DONE claim |
| Jobs alarms | every alarm | `jobs` scans (now partial-indexed) | stored next-due |
| Idle work, session audit | 5 / 30 min | per running session reads | next-due from stall thresholds |
| Webhook pump, bus retention, message expiry, due wakes | every alarm | 2-10 index probes each | next-due = `MIN(...)` stored on write |

Phase 2 live numbers (before/after) are appended to the intake as notes once measured on prod.
