# oncall-engineer hourly check

Run once per `/loop` tick.
0. `~/.local/share/agent-hub-ops/session-ctl.sh oncall skills-sync`; read `~/tmp/agent-reports/oncall/handoff.md` if newer than your start; drain `inbox/`; check context (<15% => handoff-restart).
1. For each open alert (from inbox or earlier ticks): re-verify live state (health endpoints, `gh run list`, Hub). Classify real / noise / drill / test leak; start fixers for real ones per CLAUDE.md; forward nothing to people.
2. Write `latest.md` (UTC timestamp even when quiet: "no alerts") and `NEEDS_ALEXEY.md`; copy to `history/`.
