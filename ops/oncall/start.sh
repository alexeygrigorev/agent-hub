#!/usr/bin/env bash
# One command: start oncall-engineer (sonnet), accept trust, issue /loop.
exec "$(dirname "$(readlink -f "$0")")/../lib/session-ctl.sh" oncall "${1:-start}"
