#!/usr/bin/env bash
# One command: prepare the run worktree, select a quota-backed engine, and send an engine-neutral kickoff.
exec "$(dirname "$(readlink -f "$0")")/../lib/session-ctl.sh" orchestrator "${1:-start}"
