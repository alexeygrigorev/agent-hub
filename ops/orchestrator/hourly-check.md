# orchestrator-main hourly check

Run once per `/loop` tick. Be brief; use `HUB_CONFIG_FILE=$HOME/.config/agent-hub/host.json`.

0. Setup: `~/.local/share/agent-hub-ops/session-ctl.sh orchestrator skills-sync` (pull bot-skills; relink). If `~/tmp/agent-reports/orchestrator/handoff.md` is newer than your session start, read it first. Drain `inbox/` (see CLAUDE.md duty 9). Check remaining context (<15% => handoff-restart per CLAUDE.md).
1. Load/memory: `uptime`, `free -g`. If load/MemAvailable are bad, skip starting new sessions this tick and say so.
2. Hub: `hub sessions list`, `hub status`/`hub fleet` as available: held, no_eligible_route, slots, host release SHA. Run the hub-sessions hard gate before any fix.
3. Stuck sessions: for sessions with open intakes that are stalled/idle/failed: `hub sessions show <id>`; replace per CLAUDE.md duty 2.
4. Codex quota: `hub quota` (and `quse`). At shared-codex 0%: apply the earliest banked reset.
5. Drain: `hub intake list --status open`: DONE notes for verified intakes; restart forgotten ones; start queued ones with AC only if capacity allows (max a few per hour).
6. `~/tmp/pending-research-followups.md`: close OPEN rows whose reports landed.
7. Alerts: anything in the inbox that is an alert: triage via alert-triage skill; real outage => `oncall-send` with a precise summary.
8. Write `latest.md` (include UTC timestamp, session/intake ids), `NEEDS_ALEXEY.md` (empty if nothing), copy to `history/`.
