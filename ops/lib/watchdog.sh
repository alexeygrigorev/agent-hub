#!/usr/bin/env bash
# Only check the two interactive sessions and restart one after a confirmed death/missing record.
# Claude Code's in-session /loop owns recurring oversight; this wrapper never schedules prompts.
set -uo pipefail
CTL="$(dirname "$(readlink -f "$0")")/session-ctl.sh"
rc=0
for role in orchestrator oncall; do
  "$CTL" "$role" check || rc=1
done
exit "$rc"
