# orchestrator-main and oncall-engineer: runbook

Two interactive Claude Code sessions (engine `sonnet`) on Hetzner replace Grok Bot's recurring routines. Contract: Hub intake `67847fde-a00a-4689-8460-fbad45599f99` (AC1-AC14).

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
It (1) fetches origin/main and creates/refreshes the run worktree (detached at origin/main), (2) syncs skills (below), (3) `a start --workspace <run dir> --cwd <run dir>/ops/<role> --tag <tag> --engine sonnet`, (4) accepts the bypass/trust prompts, (5) issues the loop command.

## /loop command
Claude Code syntax (checked from the `loop` skill and live in the pane): `/loop <interval> <prompt>`. Issued by the start script:
```
/loop 1h Run the orchestrator check: follow <run dir>/ops/orchestrator/hourly-check.md in this repo, starting with step 0 (skills sync, handoff, inbox).
```
(same with `oncall`). The exact line used last is in `<reports>/state/loop_command`.

## Skills (AC14)
Source: clone of `alexeygrigorev/bot-skills` at `~/git/bot-skills-ops` (note: `~/git/bot-skills` is not a git repo on Hetzner). `session-ctl.sh <role> skills-sync` runs `git pull --ff-only` and symlinks every `<skill>/SKILL.md` directory into `<run dir>/.claude/skills/` (project skills, auto-discovered by Claude Code; the dir is git-excluded in the run worktree). It runs on every start/restart (so restarts come back with the same skills) and as step 0 of each loop iteration. Both CLAUDE.md files list the skills to read and follow. If a pull fails the last checkout is used and logged.

## Scheduler and watchdog (AC4)
**`/loop` inside the interactive session is the scheduler.** There is no external cron that re-prompts or does work. The only external piece is a tiny restart-if-dead watchdog: `ops-watchdog.timer` (user, every 15 min) -> `ops-watchdog.service` -> `flock -n` + `timeout 900` + `nice -n 10` + `ionice -c3` -> `watchdog.sh` -> `session-ctl.sh <role> check`. It only tests that the session process is running and its worker alive. Dead/missing => write `handoff.md` (from `latest.md`, `NEEDS_ALEXEY.md`, inbox listing; a fresher hand-written handoff is kept), start a new session (skills synced, `/loop` re-issued). Restarts are rate limited (10 min). The unit sets `APLEXER_LAUNCH_SYSTEM_SCOPE=system` so sessions it starts do not die with the oneshot unit's cgroup. Manual run: `systemctl --user start ops-watchdog.service`; logs: `journalctl --user -u ops-watchdog.service`; `systemctl --user list-timers ops-watchdog.timer`.

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
`*-send` first appends `inbox/<UTC ts>-<pid>.md` (atomic, fsynced, never lost), then types a one-line `[INBOX id] …` into the pane via `a send --enter`, and prints `INBOX=saved <path>` plus `DELIVERY=delivered|not-delivered`. Exit 0 delivered, 2 saved only (the session drains the inbox each loop and after the restart), 1 not saved. The session moves handled files to `inbox/processed/`. Set `SENDER=alexey` to label the source.

## Mail access (alert triage)
This host cannot read `dev@dtcdev.click` or any alert mailbox: only `msmtp` (send-only) is installed; there is no IMAP client, Maildir, mbsync/fetchmail config, and no Gmail API reader. Missing: a read-only mailbox path (IMAP read credentials or Gmail API read scope for dev@dtcdev.click / the alert label) provisioned for a non-secret-printing fetcher by Alexey. Until then alerts arrive via `oncall-send` (Grok Bot forwards dev@ mail) and, later, the Hub webhook->session routing intake. Credential files were deliberately not read.

## Moving to another host
Pick a host with the Hub-validated resource floors, `a`, `claude`, `hub`, `gh` logged in and the repo clone `~/git/agent-hub`. Run `ops/install.sh`, copy `~/tmp/agent-reports/{orchestrator,oncall}` (handoff + inbox), stop the old sessions (`session-ctl.sh <role> status`, `a kill <id>`; ask Alexey first), run `start.sh` on the new host, point Grok Bot's `orch-send`/`oncall-send` at the new host (ssh wrapper). Disable the timer on the old host: `systemctl --user disable --now ops-watchdog.timer`.

## Playbook: Grok Bot limits running out (AC13)
Also a bot-skills skill: `grok-limits-playbook`.
1. **Signs**: Grok usage near the weekly budget (65% at mid-week on 2026-10-27), or Grok routines failing/refusing for quota.
2. **Cut order** (cheapest to lose first): hourly Hub report -> pending research follow-ups -> Codex quota check -> open-intake drain -> dev mailbox wake (last, until the Hub webhook exists). Grok keeps only: forwarding Alexey's messages and alerts, relaying `NEEDS_ALEXEY.md`.
3. **Where each goes**: drain, Codex quota, Hub report, research follow-ups -> orchestrator-main. Alert/outage triage (dev@ mail, Coolify, Contabo-deadman, CloudWatch, CI) -> oncall-engineer.
4. **Talk to them directly**: attach commands above; `orch-send` / `oncall-send` from a shell on Hetzner; Hub UI (hub.dtcdev.click) sessions list -> open the session.
5. **Webhook**: the Hub webhook->session routing (separate intake) replaces the Dapier->Grok mailbox wake: alerts land as messages in oncall-engineer without Grok.
6. **Restore** after limits reset: re-enable the Grok routines in reverse order, keep the forwarders, verify `latest.md` of both sessions is fresh and `NEEDS_ALEXEY.md` is being relayed; drop any duplicated duties from the Grok side.

## Rules
Standing rules live in each CLAUDE.md (AC6). Cost: both sessions run engine `sonnet` (`ORCH_ENGINE` overrides, don't).
