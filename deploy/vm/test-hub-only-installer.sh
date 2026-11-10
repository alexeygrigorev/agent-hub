#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="$ROOT/deploy/vm/install-hub2-node.sh"
WORKFLOW="$ROOT/.github/workflows/deploy.yml"

python3 - "$INSTALLER" "$WORKFLOW" <<'PY'
from pathlib import Path
import sys

installer = Path(sys.argv[1]).read_text(encoding="utf-8")
workflow = Path(sys.argv[2]).read_text(encoding="utf-8")

def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")

def is_inside_hub_only_guard(needle: str) -> bool:
    pos = installer.find(needle)
    if pos < 0:
        return False
    guard = installer.rfind('if [[ "$HUB_ONLY" != true ]]; then', 0, pos)
    end = installer.find("\nfi", pos)
    return guard >= 0 and end > pos

require('HUB_ONLY=false' in installer, "installer must default to the full edge setup")
require('--hub-only)' in installer and 'HUB_ONLY=true' in installer,
        "installer must recognize the fixed --hub-only flag")
require(is_inside_hub_only_guard('install -m 644 "$DEPLOY_DIR/Caddyfile" /etc/caddy/Caddyfile'),
        "Hub-only install must skip Caddyfile installation")
require(is_inside_hub_only_guard('systemctl reload caddy 2>/dev/null || systemctl restart caddy'),
        "Hub-only install must skip Caddy reload/restart")
require('systemctl restart agent-hub-hub2.service' in installer,
        "Hub-only install must still restart Hub2")

require('default: true' in workflow, "Resource Gate verification must remain enabled by default")
require('VERIFY_GATE: ${{ inputs.verify_gate }}' in workflow,
        "deployment must receive the explicit verify_gate choice")
require("if: inputs.verify_gate\n        run:" in workflow,
        "only the external Resource Gate check is conditional")
branch_start = workflow.find('if [ \'"$VERIFY_GATE"\' = true ]; then')
branch_end = workflow.find("\n            fi", branch_start)
hub_only_branch = workflow[branch_start:branch_end] if branch_start >= 0 and branch_end > branch_start else ""
require("else\n" in hub_only_branch and "install-hub2-node.sh --release-mode" in hub_only_branch
        and "--hub-only" in hub_only_branch,
        "verify_gate=false must select the fixed Hub-only installer flag")

print("Hub-only installer guard checks passed.")
PY
