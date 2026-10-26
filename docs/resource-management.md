# Resource management

Status: implemented source (2026-10-26). Release gates are the full local suite, independent exact-version review, and green CI/dev before production. Live acceptance evidence belongs in the intake DONE note and operator report. Intake: `4fcd3139-fb34-435c-86ec-5a24ddf6c3ee`.
Keep this doc updated as each part lands.

## Goal

Use Hetzner to the max (Alexey t1475u, 2026-10-26: «34 это очень много памяти,
целая половина. Мы реально не используем нашу машину, надо использовать по
максимум»). Memory should not be what limits us. Queued sessions keep getting
placed until real MemAvailable gets close to the floor. After that, the limit
should be CPU or provider capacity.

Baseline that started this (2026-10-26 03:40 Berlin, Hetzner, 12 cores,
62.7 GiB RAM, 32 GiB swap partition):

- 18 running launcher sessions. Real anon memory per session (cgroup
  `memory.stat anon`) had a median of about 200 MB, with a range of 13 to 427 MB.
- The launcher reserved 1500 MB per session (4096 MB for managed Codex), which
  came to `reservation_mem_mb` = 32192. The Hub saw `active_mem_mb` of about 23 GB.
- The floor was 10 GiB. MemAvailable was about 36 GiB, yet about 20 sessions
  stayed queued with `launcher_memory_reserve`.
- Load average was 23–28. The CPU was about 50–75% busy, and the rest was IO wait.

## Parts

Resource management covers four parts.

### 1. Token manager (TBM)

Covers token and quota pools, banked tranches, pacing and the kill switch. See intake
`95c1862c` and the `agent-hub-wt-tbm-e2e` branch (`src/tbm-*.ts`,
`tbm_session_tokens`). This doc does not change TBM. The resource ledger
below sits next to it as the matching per-session cost record (tokens on one
side, memory and CPU on the other), joined through the canonical session binding. TBM stores its token key as `machine_id:native_id`; the resource ledger stores both that native identity and canonical Hub `session_id`. Never join by tag.

### 2. Memory/CPU accounting (TBM extension)

Each session gets one durable row in the Hub resource ledger
(`session_resource_usage`, module `src/resource-management.ts`):

| field | source |
|---|---|
| session_id, fleet_launch_id, intake_id | Hub |
| project / workspace, engine, model, tier, task kind (implement/review/ask/goal) | Hub launch record |
| started_at, finished_at, runtime_s | Hub lifecycle |
| peak_rss_mb (sampled anon high-water mark), peak_pss_mb when available | host sampler: dedicated cgroup `memory.stat anon`, or `/proc/<pid>/smaps_rollup` Anonymous/Pss summed over the exact process tree; cache-inclusive `memory.peak` is not anonymous RSS |
| avg_cpu_cores, p95_cpu_cores, cpu_seconds | Dedicated cgroup CPU counter or exact process-tree user/system + reaped-child CPU seconds; average over runtime, time-weighted p95 of sampled interval cores |
| final state, reap reason (if any) | Hub / watchdog |

The host samples every running session's cgroup on each heartbeat and sends
the running peaks. The row is finalized when the session ends. Rows can be
joined with `tbm_session_tokens` on its `session_id = machine_id || ":" || native_id` key, so one session gives you its
tokens, memory and CPU together.

### 3. Placement

Placement is gated on live data, not on a fixed sum of per-session reservations.

- **Memory:** a new session is placed while
  `MemAvailable - ramp_reserve(inflight starts) - estimate(new) >= floor`.
  - Resident sessions are already counted inside MemAvailable, so they are not
    reserved again.
  - Only sessions still starting up (offered, starting, or the first few
    minutes of running) hold a ramp reserve.
  - `estimate(new)` is the p90 `peak_rss_mb` for the same
    project/engine/kind from the ledger. It falls back to per-engine p90, then
    to a default.
- **Floor:** 2 GiB MemAvailable (Alexey t1476u). OOM guard: when MemAvailable
  is below the floor, new sessions stay queued (`launcher_memory_reserve`).
  Running sessions are never killed or stopped because of memory. Swap (a
  32 GiB partition) is the cushion.
- **CPU admission:** measured busy cores plus expected new demand and startup
  remainder must fit the configurable utilization target; CPU PSI retains a
  separate safety cap. Load average is context only (see below).
- Tune if it crashes: if the OOM killer fires or MemAvailable stays below the
  floor, raise the floor or the estimate multiplier one step and record that here.
- The cgroup `MemoryMax` per worker (1500M, or 4096M for managed Codex) is a
  per-session safety cap. It is separate from the placement estimate.

### 4. Watchdog (memory reaper)

Runs every 5–10 minutes (Alexey t1474u: «Надо ещё watchdog чтобы сессии не
держали долго память»).

- Releases reservations and reaps process trees of sessions that are finished,
  failed, stopped, or whose PID is dead. Sessions that are done but still alive
  are stopped cleanly once their final report has been saved, without waiting
  for the live-session idle timeout. Terminal cleanup requires at least five
  minutes without activity, so a terminal label cannot bypass recent-output
  protection. Every stop rechecks exact identity and
  unchanged output/activity after saving evidence; a resumed dead workload refuses cleanup.
- Sessions with no output or activity for more than `idle_minutes` (default 60)
  are flagged. The watchdog captures their output into the intake and then
  stops them.
- It never kills a session that is producing output. It never touches
  Alexey's own interactive sessions or sessions marked `keep`.
- It records each session's RSS high-water mark. Sessions above `rss_flag_gb`
  (default 4) are flagged in the overview.
- Every reap is logged with who, when, why and the MB freed, and is visible in
  Hub. The affected intake gets a note so the forgotten-intake watchdog
  (ea87fcb3 AC8) restarts it and it is not lost.

## Config knobs

These are Hub config values (`config/resource-management.json` in agent-hub,
loaded as deployment defaults, with validated runtime overrides in Hub SQL and mirrored to the host and launcher in watchdog settings and launch offers). They are not
hard-coded constants.

| knob | default | used by |
|---|---|---|
| `memory_floor_mb` | 2048 | Hub `plan()` gate, launcher `check_resources` |
| `ramp_reserve_default_mb` | measured p90, fallback 512 | placement estimate |
| `estimate_percentile` | 90 | placement estimate |
| `estimate_multiplier` | 1.25 | measured estimate growth allowance |
| `ramp_seconds` | 180 | additional reserve window after starting |
| `cpu_load_factor` | 1.5 | Deprecated compatibility field; never gates CPU |
| `cpu_target_utilization` | 0.9 | Real busy CPU plus startup and new-task commitments |
| `cpu_default_cores` | 1.0 | Conservative no-history task estimate |
| `cpu_estimate_multiplier` | 1.25 | Growth allowance over measured CPU p95 |
| `cpu_psi_hard_cap_pct` | 50 | Independent PSI CPU some avg10 safety cap |
| `watchdog_interval_s` | 300–600 | watchdog |
| `idle_minutes` | 60 | watchdog |
| `rss_flag_gb` | 4 | watchdog / overview |
| `keep` marker | per session | watchdog exclusion |

Implemented Hub source (2026-10-26): `src/resource-management.ts`
owns validated defaults, runtime settings, `session_resource_usage`,
`resource_evidence`, and `resource_reaps`. Hub plan subtracts only ramp headroom
and a measured next-session estimate. Resident session cgroup caps are not
subtracted again. Offered/starting/uncertain launches retain a conservative
commitment until their status resolves; recently running sessions receive credit
for fresh measured current anonymous RSS (a previous peak cannot masquerade as current usage). CPU admission uses the independently configured utilization target and PSI cap.

Read-only MCP tools `resources`, `resource_sessions`, and `resource_reaps` expose the same authenticated projections. Changes to the default config file trigger CI/deployment.

The authenticated API is under `/api/fleet/resources`: operator GET summary,
`/sessions?project=...&engine=...&kind=...`, `/reaps`, and PUT `/config`.
Own-host GET `/watchdog` supplies exact native bindings plus protected/managed
flags; POST `/report` accepts bounded samples and confirmed reaps. POST
`/evidence` first saves a redacted bounded output tail to the affected intake via
`IntakeService.serviceRecordEvent`; a saved evidence receipt is required before
reporting a stop. Receipt and note writes are atomic and retries are idempotent.
A successful reap updates the canonical session and fleet outcome, preserves the
open intake, and records a restart note for the forgotten-intake watchdog. Unproven freed memory is `null`, never a fabricated zero.

Interactive sessions (including interactive goals) can be sampled but are never managed reap targets: their launch metadata cannot prove they are not Alexey's own interactive session. Only positively Hub-managed headless sessions qualify.
A protected session is excluded even when its launch was Hub-managed. Missing
intake linkage or inability to save evidence prevents a stop.

## How the stats are used for planning

- Placement estimate: p90 `peak_rss_mb` by (project, engine, kind), falling back
  to the coarser groups.
- Capacity view: the expected number of more sessions that fit is
  `(MemAvailable - floor) / p90` and `(1.5 × cores - load5)`, whichever is
  smaller.
- Cost per session: tokens (TBM) plus GB-hours and CPU-hours from the ledger.

## CLI and overview

- Session starts optionally accept `--task-kind implement|review|ask|goal`; the intake supplies the project for grouping and placement estimates.
- `hub resources`: a utilization line (memory used/total, load/cores, running,
  queued), the placement verdict and its reason, the floor and knobs, and
  sessions flagged by the watchdog.
- `hub resources sessions [--project P --engine E --kind K]`: rows from the
  per-session ledger with p50/p90.
- `hub resources reaps`: the watchdog log (who, when, why, MB freed, intake).
- Hub overview: a "Resource management" section with the same utilization
  line, per-group p90 stats, flagged sessions and recent reaps.

## Predictive CPU admission

CPU placement (intake `6aadd6c9`) uses real CPU usage, never load average.
The host samples `/proc/stat` over a bounded 0.2-second interval, subtracts
idle and iowait, and avoids counting guest time twice. It also reads CPU PSI
`some avg10`. Heartbeats carry `cpu_usage` with busy percent/cores, logical
cores, interval, PSI and observation time. Missing, reset or stale (>180 s)
telemetry queues with `cpu_usage_unknown`; PSI above the configured cap
queues with `cpu_pressure`. Load average remains visible as context.

For each new session, the estimator uses the 95th percentile of completed
sessions' measured p95 cores: same repo/project and task kind, then repo,
then global history, then the conservative default. The history is bounded to
500 recent completed measured sessions. Unknown measurements are excluded;
a measured zero remains valid. The multiplier applies to measured history.
`hub sessions show` exposes `expected_cpu_cores` and `expected_cpu_source`
(`repo_kind`, `repo`, `global`, `default`). The estimate is frozen at placement,
preserved across configuration/history changes, and forwarded to the maintained
launcher as part of its normal admission policy.

Admission requires:

```
current busy cores + startup remaining cores + expected new cores
    <= logical cores * cpu_target_utilization
```

Startup remaining is the sum of `max(0, frozen estimate - measured current)`
for offered/starting/uncertain sessions and running sessions in the configured
ramp window. Credit requires a fresh measurement aligned with the host busy
interval; otherwise the full startup estimate stays reserved. Stable running
sessions are already in busy CPU, and queued sessions reserve no CPU. The
claim path rechecks this formula excluding the offer's own commitment.
`cpu_headroom` includes current, expected, ramping and limit numbers in queued
reasons, session views and status. The independent 2048 MiB memory floor still
applies. Status labels name actual placement reasons instead of assuming memory.

Session CPU seconds and average cores use the existing resource ledger and
canonical native binding. The host journals a bounded lifetime histogram of
interval cores weighted by duration; p95 uses 0.05-core upper buckets (at most
0.05-core rounding up), with measured overflow above 1024 cores. First samples,
counter-source changes, resets and unreadable counters do not fabricate current
CPU usage. The histogram survives host restarts and finalization. Dedicated
cgroups are preferred; process-tree fallback includes waited-child CPU time,
but processes that both start and disappear entirely between polls without a
readable parent's waited-child counter cannot be reconstructed. Unknown remains
unknown. Reports retain workspace/project, engine/model, tier and kind metadata;
resource overview and `hub resources sessions` show average and p95 CPU cores.
