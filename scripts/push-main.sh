#!/usr/bin/env bash
# Hub worker/release-slot entrypoint; raw pushes use the same installed hook.
set -euo pipefail
if [ "$#" -ne 0 ]; then
  echo 'Usage: scripts/push-main.sh (after obtaining the orchestrator-main release slot)' >&2
  exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec python3 host/agent_hub_host/prepush.py push
