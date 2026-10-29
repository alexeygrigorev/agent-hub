# Orchestrator liveness and backlog idle bound

Intake 500fd60b. Alexey (29 Oct 2026, t2023u): "We must not have such a long idle period with this many tasks."

## Root cause of the 29 Oct 02:16Z–11:27Z "stall" (AC1)

Evidence: `journalctl --user -u ops-watchdog.service` on Hetzner, and the Claude transcript of aplexer session `367a1f94` (Claude session `c8b43303`, `~/.claude/projects/-home-alexey-git-agent-hub-wt-orchestrator-run-ops-orchestrator/`).

**The session was not frozen.** At 02:15:50Z the watchdog started `367a1f94` with the old prompt `/loop 1h Run the orchestrator check: follow …/hourly-check.md`. Over the next nine hours it:

- ran 8 more hourly ticks (cron job `e36434c1` fired 03:33Z through 10:33Z);
- handled 10 inbox and peer messages (review custody for 410caa75, deploy-red, canary);
- finished its last turn at 11:26Z.

**Why the watchdog said OK (and "recovery verified").** Before 266329ee, `check` only asked aplexer whether a process existed (`phase=running`, `worker_alive=true`). It never asked whether work happened. At 00:15Z and 02:15Z the previous workers had exited after their last turns (23:36Z, 02:11Z). Each time the watchdog started a new process and logged "recovery verified" as soon as that process existed. Nothing checked that it ever ticked.

**Why it fired at 11:27Z with 33091 s.** Commit 266329ee added the tick log and the stall check, and was installed at 11:27:19Z. The running session still had the pre-266329ee prompt, which never writes `orchestrator-ticks.jsonl`, and the file did not exist yet. The first new check therefore measured from `last_start` (02:15:50Z): 33091 s. It restarted a live session. The tick log only has entries from 11:37Z onward.

**Why the backlog sat idle anyway.** Throughput was lost in two ways:

- The cadence was 1 hour.
- 5 of the 8 hourly ticks started nothing (03:33Z, 05:33Z, 07:33Z, 08:33Z, 10:33Z). The other three only started one backup fixer (04:33Z), unblocked one session (06:33Z) and started one fix (09:33Z). Each held tick was reported as a normal outcome:
  - 03:33Z: launches queued behind a disk reservation held by stuck `offered` sessions;
  - 04:33Z: every Hub launch failing at the headless dispatch step since about 03:35Z;
  - 05:33Z–08:33Z: load holds at 45–127 on 12 cores, and the disk gate (about 54 GB required, 49–51 GB free).

**Why there was no alert.** The only alert path was `restart_failed` reaching 2. A live but unproductive session never fails a restart, so that path could not fire. Nothing independent of the watchdog checked progress or the backlog.

## Fix

### Host watchdog (AC2, `ops/lib/session-ctl.sh`)

- **Bounded idle.** No tick for 30 min (`MAX_TICK_AGE`) means a stall, even while the process is alive. The session writes a tick line at the start of every tick (brief step 0) and at the end (step 8).
- **Proven restarts.** After every restart the new session must append a tick within 10 min (`PROOF_WINDOW`). A transient `systemd-run --on-active` timer re-runs the watchdog right after the window. Until then the watchdog logs `recovery pending proof` and neither restarts again nor resets failures.
- **Second recovery path.** A failed proof starts path 2: a new session on `ORCH_FALLBACK_ENGINE` (default `sonnet`) with a fresh handoff, again with a 10-min proof. Paths then alternate, so recovery never stops.
- **Alerting (AC5).** A failed restart or a failed proof is one failed recovery. Only 2 in a row alert: `NEEDS_ALEXEY.md`, the oncall inbox, and the Hub alert below. A proven recovery resets the count.
- **Hub reports.** Every run reports `hub ops heartbeat --kind watchdog --state … --recovery-failures N --tick-at …`. A failed report never changes the local decision.

Tests: `host/tests/test_ops_watchdog.py`, which cover the 29 Oct reproduction, the 30-min bound, a proven restart, a failed proof leading to path 2 and then an alert after two failures, a session dying inside the window, and a Hub report failure.

### Hub-side liveness (AC3, AC5, `src/ops-liveness.ts`)

This runs on the Hub Durable Object's minute alarm, so it works without the orchestrator session, the host watchdog or the host itself.

- **Reports:** `POST /api/ops/heartbeat`, from an admin or an agent credential with `health:report`. Bodies are `{role, kind: tick|watchdog, state?, recovery_failures?, tick_at?, summary?}`. The CLI is `hub ops heartbeat`.
- **Status:** `GET /api/ops/liveness` (admin), CLI `hub ops status`. Roles show as `ok`, `tick_stale`, `watchdog_stale` or `tick_and_watchdog_stale`.
- **Alerts** (`ops_liveness/ops-orchestrator`) open only when automatic recovery failed twice: the watchdog reports `recovery_failures >= 2`, or the watchdog has been silent for two 30-min windows (60 min), so host-side recovery cannot run. They go through the normal Hub alert delivery (`HUB_ALERT_TO`) and resolve on recovery.

### Backlog idle kick (AC4)

- **Detection.** Every minute Hub counts open or in-progress intakes with non-empty AC, and intake-linked sessions in `starting`, `offered` or `running` (both counts bounded by indexes and `LIMIT`). Open intakes with AC and zero such sessions open an idle episode (`ops_backlog_idle`).
- **Why it was idle.** The episode records the orchestrator state, tick and watchdog ages, and the top queue hold reasons.
- **Starting work.** After 30 min Hub starts work itself through `IntakeWatchdog.idleKick`. This is the canonical admission path of the autonomous queue (410caa75 / 6b53e801). It tries up to 3 of the oldest ready intakes and stops at the first admission. All admission checks still apply: AC, workspace, retry backoff, capacity, observation budget and an operator's explicit `HUB_INTAKE_QUEUE_ENABLED=false`. The only check it skips is the per-project continuous-pickup scope (`HUB_INTAKE_QUEUE_PROJECTS`). It kicks at most once per 30 min, and each kick is recorded in the intake audit (`idle_kick`).
- **Alert.** If work is still idle 30 min after a kick, that kick failed. Two failed kicks in a row raise `ops_liveness/ops-backlog-idle`. Any running intake session resolves the episode and the alert.
- **Switches.** `HUB_OPS_LIVENESS=false` disables the whole check. `HUB_BACKLOG_IDLE_KICK=false` keeps detection and records but never starts work.

Tests: `test/ops-liveness.test.ts`.

## Operations

- After landing on main: `ops/install.sh` on Hetzner. The running orchestrator picks up the new brief on its next restart or handoff.
- To inspect: `hub ops status` and `journalctl --user -u ops-watchdog.service`.
- Live proof (AC6): freeze or kill `orchestrator-main` at a time agreed with it. Then watch for, in order: the `STALLED`/`DEAD` log line, `recovery path 1 started`, `recovery proven: fresh tick …`, and `hub ops status` showing `ok`.
