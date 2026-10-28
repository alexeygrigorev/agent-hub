# orchestrator-main hourly check

Alexey standing order t1756u (corrected t1760u): the orchestrator owns execution of ALL Hub work. Each tick:

- (a) Check all open intakes and live sessions: running/offered/held/failed/stalled, no activity >30 min, CI red on main, deploy failed, launcher_command_failed, capture_not_acked, no_eligible_route/held with rejected[].
- (b) When something breaks, start the fix in the same tick: Hub intake with AC plus a session, or resume/restart the stalled session. Dedupe only if a live session is actively working that same root cause.
- (c) Finished work gets a DONE note with proof; never close or delete intakes.
- (d) NEEDS_ALEXEY is only for prod/GitHub environment approvals, secrets, deletions, or Alexey's own 2FA. Never approve prod gates.

Cadence: every 15 min via Hub scheduled pings once Hub schedules exist; until then, `/loop 1h` is a stopgap. Keep `LOOP_INTERVAL` in `ops/lib/session-ctl.sh` unchanged.

Run each scheduled ping (or hourly `/loop` stopgap tick). Be brief; use `HUB_CONFIG_FILE=$HOME/.config/agent-hub/cli.json` (`host.json` does not exist on Hetzner). Human `hub auth login` on Hetzner was done 2026-10-27 22:17 (orin@datatalks.club), so `hub quota` / `hub engines` work with cli.json.

0. Setup: `~/.local/share/agent-hub-ops/session-ctl.sh orchestrator skills-sync` (pull bot-skills; relink). If `~/tmp/agent-reports/orchestrator/handoff.md` is newer than your session start, read it first. Drain `inbox/` (see CLAUDE.md duty 9). Check remaining context (<15% => handoff-restart per CLAUDE.md).
1. Load/memory: `uptime`, `free -g`. Respect live resource gates. If capacity blocks a launch, start permitted recovery in this tick and track the blocked fix and next action.
2. Hub: `hub sessions list`, `hub status`/`hub fleet` as available: held, no_eligible_route with rejected[], slots, host release SHA; CI red on main, deploy failed, launcher_command_failed and capture_not_acked. Run the hub-sessions hard gate before any fix.
3. Stuck sessions: check running/offered/held/failed/stalled sessions and any with no activity >30 min: `hub sessions show <id>`; resume/restart or start a Hub intake with AC plus a fixer session in the same tick; dedupe only against a live session actively working the same root cause.
4. Codex quota: `hub quota` (and `quse`). At shared-codex 0%: apply the earliest banked reset.
5. Drain: `hub intake list --status open`: DONE notes for verified intakes; restart forgotten ones; start queued ones with AC subject to live resource and quota gates.
6. `~/tmp/pending-research-followups.md`: close OPEN rows whose reports landed.
7. Alerts: anything in the inbox that is an alert: triage via alert-triage skill; real outage => `oncall-send` with a precise summary.
8. Write `latest.md` (include UTC timestamp, session/intake ids), `NEEDS_ALEXEY.md` (empty if nothing), copy to `history/`.
