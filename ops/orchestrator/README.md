# orchestrator-main and oncall-engineer: runbook

Two persistent operations sessions on Hetzner handle Hub queue work and
cross-service incident triage. Developer (the Grok Bot overseer) forwards work
and checks progress; the sessions do investigation and queue work. The watchdog
selects from each role's configured engine allowlist when `hub quota` reports
fresh headroom; if Hub quota is unavailable, local fallback uses the same role
allowlist. For the purpose, current alert path, and intended end state, see
[On-call and orchestrator architecture](../../docs/ONCALL-ORCHESTRATOR-ARCHITECTURE.md).
Contract: Hub intake `67847fde-a00a-4689-8460-fbad45599f99` (AC1-AC14).

| | orchestrator-main | oncall-engineer |
|---|---|---|
| Brief | `ops/orchestrator/CLAUDE.md`, `hourly-check.md` | `ops/oncall/CLAUDE.md`, `hourly-check.md` |
| aplexer tag / run dir | `orchestrator-main` / `~/git/agent-hub-wt-orchestrator-run` | `oncall-engineer` / `~/git/agent-hub-wt-oncall-run` |
| Reports | `~/tmp/agent-reports/orchestrator/` | `~/tmp/agent-reports/oncall/` |
| Send | `orch-send` | `oncall-send` |
| Start | `ops/orchestrator/start.sh` | `ops/oncall/start.sh` |

Reports dir layout: `latest.md`, `NEEDS_ALEXEY.md`, `handoff.md`, `history/` (max 48), `inbox/` (+ `processed/`), `state/` (`last_start`, `loop_command`, `restart.log`).

## Install (once per host, re-run after changes on main)
```bash
git -C ~/git/agent-hub fetch origin && ops/install.sh   # from any checkout of origin/main
```
Copies `session-ctl.sh`, `watchdog.sh` to `~/.local/share/agent-hub-ops/`, `orch-send` / `oncall-send` to `~/bin/`, installs and enables the user timer `ops-watchdog.timer`.

## Start
```bash
ops/orchestrator/start.sh        # or: ops/oncall/start.sh
```
It (1) fetches origin/main and creates or refreshes the run worktree (detached
at origin/main), (2) syncs skills (below), (3) selects a configured
role-allowed engine with fresh quota headroom and starts it, (4) accepts the
bypass/trust prompts, and (5) sends one engine-neutral kickoff prompt. The
live sessions currently use Claude `/loop` for hourly work as a stopgap; Hub
scheduled prompts attached to the session tags are the engine-neutral target.
See the [architecture page](../../docs/ONCALL-ORCHESTRATOR-ARCHITECTURE.md) for
the current cadence and transition status.

## Startup prompt and schedule

`orchestrator-main` still prefers `antigravity`; its other configured non-Claude
routes remain OpenCode, Grok and ZCode. `oncall-engineer` is restricted to
OpenCode with the explicit model `opencode-go/muse-spark-1.3-contributor`.
Its start and every watchdog recovery pass that model to `a start --model`;
its allowlist does not include Antigravity, Gemini, Grok, Codex or ZCode. An
unavailable Hub quota or an unsafe engine override therefore cannot select
Argon, Astra, Opus or a high-effort route for oncall. If the pinned OpenCode
route is not configured or lacks fresh quota when Hub is available, recovery
holds rather than widening the allowlist.

For orchestrator only, the Hub quota pool is still named `gemini` in `provider`
metadata; that legacy provider key does not select the old Gemini CLI. Any
legacy `ENGINE=gemini` or `ORCH_ENGINE=gemini` preference is normalized to
Antigravity. If its preferred route is unavailable, selection tries configured
non-Claude routes in order: Antigravity, OpenCode, Grok, then ZCode.
The start script sends the role's hourly check (or orchestrator brief) as a normal prompt and records it in `<reports>/state/loop_command`. It does not issue Claude Code's `/loop` command. The current live sessions still use Claude `/loop` for hourly work as a stopgap. Hub scheduled prompts attached to `orchestrator-main` and `oncall-engineer` are the engine-neutral target; verify the live attachment and deliveries with `hub schedules list` before treating that transition as complete.

The `oncall-engineer` start and watchdog recovery paths are pinned to OpenCode
with `opencode-go/muse-spark-1.3-contributor`. They do not select Argon,
including medium, Astra, Opus, or a high-effort model. When Hub quota is
unavailable, local fallback stays on the pinned route; if that route is
unavailable, recovery holds rather than widening the allowlist. This is the
configured start policy and does not prove which model an already-running live
session uses; verify the live session before claiming it has switched.

## Skills (AC14)
Source: clone of `alexeygrigorev/bot-skills` at `~/git/bot-skills-ops` (note: `~/git/bot-skills` is not a git repo on Hetzner). `session-ctl.sh <role> skills-sync` runs `git pull --ff-only` and symlinks every `<skill>/SKILL.md` directory into `<run dir>/.claude/skills/` (the dir is git-excluded in the run worktree). It runs on every start/restart and as step 0 of each scheduled check. Both role instruction files list the skills to read and follow. If a pull fails the last checkout is used and logged.

## Scheduler and watchdog (AC4; idle bound intake 500fd60b)
Hub scheduled prompts attached to each session tag are the scheduler. `ops-watchdog.timer` (user, every 10 min) -> `ops-watchdog.service` -> `flock -n` + `timeout 900` + `nice -n 10` + `ionice -c3` -> `watchdog.sh` -> `session-ctl.sh <role> check` checks health and progress; it does not schedule the work itself.

For the orchestrator a live process is not enough. `check`:
- restarts when the session is dead, or when it is alive but `~/tmp/agent-reports/orchestrator-ticks.jsonl` has not changed for 30 min (`MAX_TICK_AGE`). Every OK run arms a 1s-accurate transient deadline timer for the moment the newest tick turns 30 min old. That timer runs this role's check directly under a per-role lock, so detection waits neither for the 10-min timer nor for the oncall check;
- proves every restart: the new session must append a tick within 10 min of the recorded start (`PROOF_WINDOW`). A transient timer re-runs the watchdog right after the deadline. Inside the window it neither restarts again nor reports OK. Proof reads the tick history: only a tick inside the window counts, and a late first tick starts path 2;
- if the proof fails, it starts recovery path 2: a new session preferring `ORCH_FALLBACK_ENGINE` (default `antigravity`), excluding the failed route and requiring fresh quota headroom, with a fresh handoff and a 10-min proof. Paths then alternate;
- counts consecutive failed recoveries (failed restart or failed proof). Only at 2 does it alert: `NEEDS_ALEXEY.md`, the oncall inbox, and Hub (below). A proven recovery resets the count;
- reports each run to Hub: `hub ops heartbeat --kind watchdog --state <ok|recovering|proof_pending|proof_failed|...> --recovery-failures N --tick-at <tick log time>`. A failed report never changes the local decision.

Hub-side check (`src/ops-liveness.ts`, minute alarm; `hub ops status`): it works without this watchdog and without the session. It alerts only after automatic recovery failed twice: the watchdog reports 2 consecutive failures, or the watchdog has been silent for two 30-min windows. Its backlog check counts open intakes with AC against intake sessions starting or running. After 30 min with none, Hub records why it was idle and starts the oldest ready intake through canonical admission, at most once per 30 min. Two starts in a row that produce no working session raise an alert. `HUB_OPS_LIVENESS=false` disables it, and `HUB_BACKLOG_IDLE_KICK=false` disables only the start. Details: `docs/ORCHESTRATOR-LIVENESS.md`.

The oncall role checks dead processes, limit screens, and unprocessed inbox items older than 20 min. Its recovery uses only the pinned OpenCode Muse Spark route; fresh Hub quota is required when Hub is available, and an unavailable Hub falls back locally to that same route. The orchestrator also checks limit screens and stale ticks. Its recovery requires a configured non-Claude route with fresh Hub quota headroom; Claude and Codex model routes are excluded. Restarts outside recovery are rate limited (10 min). The unit sets `APLEXER_LAUNCH_SYSTEM_SCOPE=system` so sessions it starts do not die with the oneshot unit's cgroup. Manual run: `systemctl --user start ops-watchdog.service`; logs: `journalctl --user -u ops-watchdog.service`; `systemctl --user list-timers ops-watchdog.timer`.

## Restart / handoff (AC3)
- Context under 15%: the session writes `handoff.md` and runs `session-ctl.sh <role> handoff-restart` (detached; kills itself and starts a fresh one, which reads the handoff in step 0).
- Manual: write `<reports>/handoff.md`, then `ops/<role>/start.sh restart`.

## Attach and message (AC5, AC11, AC12)
```bash
a open orchestrator-main --workspace ~/git/agent-hub-wt-orchestrator-run     # attach (Ctrl-b d detaches); also: a attach <id> from `a list`
a open oncall-engineer  --workspace ~/git/agent-hub-wt-oncall-run
hub sessions list | grep -E 'orchestrator-main|oncall-engineer'               # Hub view
orch-send "text" ; orch-send --file msg.md ; echo text | orch-send -          # to orchestrator-main
oncall-send "text"                                                            # to oncall-engineer (same options)
```
`*-send` first appends `inbox/<UTC ts>-<pid>.md` (atomic, fsynced, never lost), then types a one-line `[INBOX id] …` into the pane via `a send --enter`, and prints `INBOX=saved <path>` plus `DELIVERY=delivered|not-delivered`. Exit 0 delivered, 2 saved only (the session drains the inbox on scheduled checks and after a restart), 1 not saved. The session moves handled files to `inbox/processed/`. Set `SENDER=alexey` to label the source.

## Mail access and alert delivery

This host has no alert-mail reader. The current route is infrastructure mail to
`dev@dtcdev.click`, then Dapier and the Grok Bot's “Dapier/dev mailbox wake”
routine; Developer forwards the alert with `oncall-send` and usually
`orch-send` as well. The send command saves the message before pane delivery.
If no oncall session is live, `oncall-send` exits 2 after saving it to the
inbox. A direct Dapier-to-oncall route is being implemented separately and is
not the current path; keep using the Developer-forwarded route until direct
delivery has an end-to-end proof. See the [architecture page](../../docs/ONCALL-ORCHESTRATOR-ARCHITECTURE.md).

## Moving to another host
Pick a host with the Hub-validated resource floors, `a`, `hub`, `gh` logged in and the repo clone `~/git/agent-hub`. At least one supported non-Claude engine must be configured and have fresh Hub quota headroom. Run `ops/install.sh`, copy `~/tmp/agent-reports/{orchestrator,oncall}` (handoff + inbox), stop the old sessions (`session-ctl.sh <role> status`, `a kill <id>`; ask Alexey first), run `start.sh` on the new host, point Grok Bot's `orch-send`/`oncall-send` at the new host (ssh wrapper). Disable the timer on the old host: `systemctl --user disable --now ops-watchdog.timer`.

## Playbook: Grok Bot limits running out (AC13)
Also a bot-skills skill: `grok-limits-playbook`.

> **Scheduler = Hub schedules (intake 19ec91c2 AC8-AC10; see [docs/PROMPT-SCHEDULES.md](../../docs/PROMPT-SCHEDULES.md)).** Library prompts `orchestrator-hourly` / `oncall-triage` are delivered to the existing session tags. Check schedule state and deliveries with `hub schedules list` and `hub schedules deliveries`; `session-ctl.sh` sends only an engine-neutral kickoff prompt and does not issue `/loop`.

> **Hub driver (intake 1035ad06):** the replacement orchestrator is one managed interactive session, started with `hub driver start --engine codex --workspace agent-hub`; inspect it with `hub driver status`. The start command also queues the standing order and creates the 15-minute session prompt schedule through `/api/session-schedules`. This is distinct from `hub schedule`, which starts a fresh session on each tick. Keep `orchestrator-main` and its watchdog until its owner verifies two consecutive useful driver cycles, acknowledges the handoff, and retires the old in-session cron. See [Hub driver migration](../../docs/DRIVER.md#orchestrator-migration).

> **Engine-neutral operation (AC15).** The start script sends a plain one-time kickoff. Hub scheduled prompts deliver each later check to the session tag, so the schedule works across supported engines. Watchdog recovery chooses only configured non-Claude routes with fresh Hub quota headroom; `ORCH_FALLBACK_ENGINE` defaults to `antigravity`.
1. **Signs**: Grok usage near the weekly budget (65% at mid-week on 2026-10-27), or Grok routines failing/refusing for quota.
2. **Cut order** (cheapest to lose first): hourly Hub report -> pending research follow-ups -> Codex quota check -> open-intake drain -> dev mailbox wake (last, until the Hub webhook exists). Grok keeps only: forwarding Alexey's messages and alerts, relaying `NEEDS_ALEXEY.md`.
3. **Scheduler note**: Hub schedules (`hub schedules list`, engine-agnostic, durable queue) drive both sessions; watchdog recovery does not depend on `/loop`.
3. **Where each goes**: drain, Codex quota, Hub report, research follow-ups -> orchestrator-main. Alert/outage triage (dev@ mail, Coolify, Contabo-deadman, CloudWatch, CI) -> oncall-engineer.
4. **Talk to them directly**: attach commands above; `orch-send` / `oncall-send` from a shell on Hetzner; Hub UI (hub.dtcdev.click) sessions list -> open the session.
5. **Webhook**: the Hub webhook->session routing (separate intake) replaces the Dapier->Grok mailbox wake: alerts land as messages in oncall-engineer without Grok.
6. **Restore** after limits reset: re-enable the Grok routines in reverse order, keep the forwarders, verify `latest.md` of both sessions is fresh and `NEEDS_ALEXEY.md` is being relayed; drop any duplicated duties from the Grok side.

## Hub webhooks (dev@ route target)
The Hub now exposes authenticated webhook endpoints that deliver payloads into a target session (intake `19ec91c2`): routes, delivery status and audit live at hub.dtcdev.click `/webhooks.html`, CLI `hub webhooks create|list|delete|test`. The dev@ mailbox route targets **oncall-engineer** (not orchestrator-main); see [docs/WEBHOOKS.md](../docs/WEBHOOKS.md) for auth (token/HMAC), rate limits, size caps and the test command. Dapier's live webhook is only repointed once the old Grok Bot wake keeps working (dual-deliver); deleting the old webhook needs Alexey.

## Rules
Standing rules live in each role instruction file (AC6). Watchdog-selected engines must be non-Claude and have fresh Hub quota headroom. Oncall is pinned to the OpenCode Muse Spark model above; never use Argon, Astra, Opus or high effort for `oncall-engineer`.
