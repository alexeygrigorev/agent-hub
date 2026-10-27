#!/usr/bin/env bash
# Hourly liveness for orchestrator-main and oncall-engineer. ONLY alive/not-hung check + restart with handoff.
set -uo pipefail
CTL="$(dirname "$(readlink -f "$0")")/session-ctl.sh"
rc=0
for role in orchestrator oncall; do "$CTL" "$role" check || rc=1; done
exit $rc
