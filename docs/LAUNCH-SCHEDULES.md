# Hub cron launch schedules

Intake a839dfb6: operator-configured cron schedules that launch a fresh session per tick through the normal admission path.

## Design

`src/launch_schedules.ts` owns two SQLite tables (created by its `init()`):

- `launch_schedules` — one row per schedule: unique lowercase `name` (2–40 chars), `project`, five-field `cron`, IANA `tz`, `prompt` (≤ 7000 bytes), optional `acceptance_criteria` (≤ 8192 bytes), optional `engine`/`tier`/`workspace`, `task_kind` (implement/review/ask/goal, default implement), `overlap` policy (skip/queue/allow, default skip), `enabled`, and a durable cursor (`last_tick_ms` + cached `next_due_ms`).
- `launch_schedule_runs` — one row per evaluated occurrence: `tick_at` (the cron occurrence), `fired_at`, `intake_id`, `session_id`, `outcome` (`started`/`held`/`queued`/`pending`/`skipped`/`refused`/`error`), bounded `reason`. The index `launch_schedule_runs_recent` serves the dashboard; history outlives a deleted schedule.

Each tick creates a Hub intake (`source.type: "schedule"`, with the schedule name and occurrence as provenance) and then starts the linked headless session through the canonical `SessionControlService` start — the same path every other Hub service uses. Placement, capacity gates, holds and the intake acceptance-criteria gate all apply unchanged; a tick never bypasses admission. Outcomes are recorded honestly: a held start is `held` with the placement hold reason (session-control answers holds with a 409 that carries the session row), an explicit refusal is `refused` with the gate error, an unknown outcome (5xx) is `error`.

Ticking runs in `Hub.runChecks()` on every cron fire (`*/5` and `*/30`; the `*/30` slot keeps the root scheduler). A manual single-check run (`runChecks('check')`) never fires schedules, and `POST /api/schedules/:id/tick` evaluates exactly one schedule (`source: "manual"`).

## Cron evaluation

Strict five-field expressions (numbers only; `*`, lists, ranges, `N/step`; day-of-week 7 normalizes to Sunday; vixie OR-rule when both day fields are restricted). Evaluation resolves wall-clock occurrences in the schedule's IANA timezone through `Intl.DateTimeFormat`, with physical verification (`utcFromLocal`): a wall time skipped by a spring-forward gap yields no tick that day, and a fall-back repeat fires once at its first instantiation. Missed occurrences (Hub down) collapse: one fire per pass, cursor jumps to the pass time. Pausing clears the cached next-due time; resuming resets the cursor to now, so a paused schedule never backfills. Day stepping walks local dates by UTC noon so a local date advances exactly once per step; the scan bound is 1490 local days (a four-year leap cycle). An impossible date (`0 0 30 2 *`) simply never fires.

## Overlap policy

Evaluated against the schedule's own newest run whose session is still live (queued/held/offered/starting/running/uncertain):

- **skip** (default): the due occurrence is recorded as `skipped` with reason `overlap_previous_live:<session id>`.
- **queue**: at most one `pending` run is parked (`overlap_queued_previous_live`); further due ticks are `skipped` with `overlap_queue_pending:<run id>`. When the previous run is no longer live, the parked tick dispatches before any fresh occurrence, updating the same row in place.
- **allow**: fires concurrently.

Per-fire session names are `<schedule>.<YYYYMMDDTHHMM>`, so holds never collide with the held-row hygiene (same actor + workspace + name).

## API, CLI and UI

Admin-only (`/api/schedules`, same operator/cookie authority as webhooks; CSRF applies for cookie callers):

- `POST /api/schedules` (create; 409 `name_taken`; create answers `acceptance_criteria_warning` when no AC — ticks then record the intake, but the start gate refuses with `acceptance_criteria_required` until the schedule carries criteria), `GET` (list), `GET/DELETE /api/schedules/:id|:name`, `POST .../:id/pause|resume|tick`, `GET .../:id/runs?limit=` (≤ 100).
- CLI: `hub schedule create|list|show|pause|resume|delete|runs|tick` (`host/agent_hub_host/schedules.py`; `--prompt-file`/`--acceptance-criteria-file`, `-` for stdin; delete needs `--yes`). All schedule commands are deliberate CLI exclusions in `src/mcp-tools.ts` (operator-only product scope).
- UI: `/schedules` (`public/schedules.html|js|css`) — create form, schedules table (pause/resume/delete/open), per-schedule run history and a manual tick button.

## Audit

`schedule.create|pause|resume|delete` land in the audit table. Delete keeps run history: deleted schedules stay auditable through their runs.

## Testing

`test/launch-schedules.test.ts` covers the cron core (UTC + Europe/Berlin DST gap/fall-back, OR-rule), create validation, the admin lifecycle (pause/resume cursor reset, delete keeping runs, audit), the tick engine with injected hooks and clock (fire-once cursors, missed-occurrence collapse, held/refused/error outcomes, unknown-outcome honesty, all three overlap policies with pending dispatch, invalid-cron pause), and end-to-end runs through the real intake/session services (schedule provenance on the intake history, held session linked via `intake_task_id`, overlap skip against a live held session, and the real `acceptance_criteria_required` refusal). CLI payloads are pinned in `host/tests/test_schedules_cli.py`.

## Operator notes

- On production the `*/5` cron drives ticks. The deployed dev worker runs with crons stripped (`deploy.yml` removes `triggers.crons` for `agent-hub-dev`), so dev ticks need a manual `POST /api/schedules/:id/tick` or the schedule simply stays quiet.
- A held run means the fleet had no placement; it is live until placed or cancelled and blocks the next tick under the default overlap policy.
