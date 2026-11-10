#!/usr/bin/env bash
# Hub live start canary. Usage: hub-start-canary.sh [--mode shell|full] | --deploy-gate
# Exit 0 = pass; 1 = failed stage (JSON verdict on stdout and stderr).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$PATH:${HOME:-/nonexistent}/.local/bin"
exec python3 "$HERE/hub_start_canary.py" "$@"
