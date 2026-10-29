#!/usr/bin/env bash
# Host-wide heavy-suite admission (FIFO within normal/final priority classes).
# Usage: scripts/capped-test-run.sh [--slots N] [--wait S] [--priority normal|final] -- COMMAND...
# AGENT_HUB_HEAVY_TEST_PRIORITY=final selects final pre-push verification.
# All sessions must share AGENT_HUB_HEAVY_TEST_DIR and AGENT_HUB_HEAVY_TEST_SLOTS.
# See docs/HETZNER-OVERLOAD-0689CE9C.md. Requires Python 3 and Linux flock.
set -uo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/heavy-test-admission.py" "$@"
