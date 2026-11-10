#!/usr/bin/env bash
# Phase 7 acceptance: ten consecutive model-less Hub starts. See hub_acceptance_10.py.
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/hub_acceptance_10.py" "$@"
