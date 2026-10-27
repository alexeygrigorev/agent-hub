#!/usr/bin/env bash
# One command: (re)create run worktree, sync skills, start orchestrator-main (sonnet), accept trust, issue /loop.
exec "$(dirname "$(readlink -f "$0")")/../lib/session-ctl.sh" orchestrator "${1:-start}"
