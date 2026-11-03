# orchestrator-main and oncall-engineer: runbook

Two interactive Claude Code sessions (engine `sonnet`) on Hetzner replace Grok Bot's recurring routines. Contract: Hub intake `67847fde-a00a-4689-8460-fbad45599f99` (AC1-AC15).

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
Checked in the installed Claude Code `/help` on 2026-11-03: `/loop` runs a prompt or slash command on a recurring interval (the command list shows `/loop 5m /foo…`). The start script issues it at the intended one-hour cadence. The current sessions still show their earlier `/loop 15m` and are paused by Claude's weekly limit; their first successful hourly iteration remains pending a quota-safe run:
```
/loop 1h Run the orchestrator check: follow <run dir>/ops/orchestrator/hourly-check.md in this repo, starting with step 0 (skills sync, handoff, inbox).
```
(same with `oncall`). The exact line used last is in `<reports>/state/loop_command`.

## Skills (AC14)
Source: the consolidated `alexeygrigorev/bots-management` repository at `~/git/bots-management-ops` (the former `bot-skills` repository was consolidated on 2026-10-30). `session-ctl.sh <role> skills-sync` runs `git pull --ff-only` and symlinks every `<skill>/SKILL.md` directory into `<run dir>/.claude/skills/` (project skills, auto-discovered by Claude Code; the dir is git-excluded in the run worktree). It runs on every start/restart (so restarts come back with the same skills) and as step 0 of each loop iteration. Both CLAUDE.md files list the skills to read and follow. If a pull fails the last checkout is used and logged.

## Scheduler and watchdog (AC4)
**Claude Code's in-session `/loop` is the scheduler.** There is no external cron that re-prompts, checks ticks, or decides whether useful work happened. The only external timer is `ops-watchdog.timer` (user, every 15 minutes). Its one-shot service checks the native aplexer worker and restarts a session only when that process is confirmed dead or missing. A live process is left alone even if a report or tick file is stale. A restart reads the handoff, syncs skills, starts a Sonnet session, and reissues `/loop`.

The service uses `flock -n`, `timeout 900`, `nice -n 10`, and `ionice -c3`. It does not create transient timers or send periodic prompts. Inspect the timer with `systemctl --user list-timers ops-watchdog.timer`; run one check with `systemctl --user start ops-watchdog.service`; inspect its result with `systemctl --user status ops-watchdog.service` and `journalctl --user -u ops-watchdog.service`.

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
This host cannot read `dev@dtcdev.click` or any alert mailbox: only `msmtp` (send-only) is installed; there is no IMAP client, Maildir, mbsync/fetchmail config, and no Gmail API reader. Missing: a read-only mailbox path (IMAP read credentials or Gmail API read scope for dev@dtcdev.click / the alert label) provisioned for a non-secret-printing fetcher by Alexey. The Hub `dev-mailbox` webhook is enabled for `oncall-engineer`; until delivery from the live mailbox source is verified alongside Grok's existing wake, alerts still arrive via `oncall-send` (Grok Bot forwards dev@ mail). Credential files were deliberately not read.

## Moving to another host
Pick a host with the Hub-validated resource floors, `a`, `claude`, `hub`, `gh` logged in and the repo clone `~/git/agent-hub`. Run `ops/install.sh`, copy `~/tmp/agent-reports/{orchestrator,oncall}` (handoff + inbox), stop the old sessions (`session-ctl.sh <role> status`, `a kill <id>`; ask Alexey first), run `start.sh` on the new host, point Grok Bot's `orch-send`/`oncall-send` at the new host (ssh wrapper). Disable the timer on the old host: `systemctl --user disable --now ops-watchdog.timer`.

## Playbook: Grok Bot limits running out (AC13)
Also a bot-skills skill: `grok-limits-playbook`.

1. **Signs**: Grok usage near its weekly budget (65% at mid-week on 2026-10-27), or Grok routines failing/refusing for quota.
2. **Cut order** (cheapest to lose first): hourly Hub report -> pending research follow-ups -> Codex quota check -> open-intake drain -> dev mailbox wake. Keep the mailbox wake until the live Hub webhook source and delivery to both sessions have passed a dual-delivery check. Grok keeps only forwarding and relaying `NEEDS_ALEXEY.md`.
3. **Where work goes**: `orchestrator-main` handles Hub health/placement, stuck sessions, load/memory, Codex quota, intake drain, research follow-ups, and receives every Alexey message and every alert through `orch-send`. `oncall-engineer` triages alerts (dev@ mail, Coolify, Contabo-deadman, CloudWatch, CI); forward dev@ infrastructure alerts to it with `oncall-send` as well. Developer forwards those items and does not investigate them.
4. **Talk to them directly**: attach with the commands above; send `orch-send` / `oncall-send` from a shell on Hetzner; or use the Hub UI (`hub.dtcdev.click`, Sessions).
5. **Webhook transition**: the authenticated Hub webhook route is in place and targets `oncall-engineer` (see [docs/WEBHOOKS.md](../docs/WEBHOOKS.md)); the live mailbox source and dual delivery to both sessions are not verified. Keep the Dapier-to-Grok wake and forward dev@ alerts to both sessions until that check passes. Afterward, the Hub webhook can replace the mailbox wake.
6. **Scheduler**: Claude Code `/loop 1h` is the current scheduler stopgap. Hub scheduled prompts (`hub schedules create --session <label> --cron '0 * * * *' --prompt-file <f>`, intake 19ec91c2 AC8) are the engine-agnostic target. Create and verify a schedule for each session, then set `LOOP_INTERVAL=off` and cancel/restart the in-session loops. The external `ops-watchdog.timer` remains a 15-minute dead-process check and never re-prompts live sessions.
7. **Restore** after limits reset: re-enable paused Grok routines in reverse cut order, keep the forwarders, verify both `latest.md` reports are fresh and `NEEDS_ALEXEY.md` is relayed, then remove only duties confirmed to be duplicated. A separate Hub driver migration is documented in [Hub driver migration](../../docs/DRIVER.md#orchestrator-migration); use its handoff criteria if that path is selected.

## Hub webhooks (dev@ route target)
The Hub now exposes authenticated webhook endpoints that deliver payloads into a target session (intake `19ec91c2`): routes, delivery status and audit live at hub.dtcdev.click `/webhooks.html`, CLI `hub webhooks create|list|delete|test`. The dev@ mailbox route targets **oncall-engineer** (not orchestrator-main); see [docs/WEBHOOKS.md](../docs/WEBHOOKS.md) for auth (token/HMAC), rate limits, size caps and the test command. Dapier's live webhook is only repointed once the old Grok Bot wake keeps working (dual-deliver); deleting the old webhook needs Alexey.

## Rules
Standing rules live in each CLAUDE.md (AC6). Cost: both sessions and recovery sessions always run engine `sonnet`; the launcher does not accept model overrides.
