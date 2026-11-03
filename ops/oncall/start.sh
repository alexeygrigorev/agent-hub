#!/usr/bin/env bash
# One command: start oncall-engineer on a quota-backed engine and send an engine-neutral kickoff.
exec "$(dirname "$(readlink -f "$0")")/../lib/session-ctl.sh" oncall "${1:-start}"
