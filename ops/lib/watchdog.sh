#!/usr/bin/env bash
# Tiny restart-if-dead watchdog. NOT a scheduler: Claude Code's /loop inside each session does the periodic work.
set -uo pipefail
CTL="$(dirname "$(readlink -f "$0")")/session-ctl.sh"
rc=0
for role in orchestrator oncall; do "$CTL" "$role" check || rc=1; done
exit $rc
