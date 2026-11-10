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
transaction = (Path(sys.argv[1]).parent / "deploy-release-transaction.sh").read_text(encoding="utf-8")

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
require(workflow.count("if: inputs.verify_gate") == 2,
        "both external Resource Gate checks must be conditional")
require("true) gate_mode=full ;;" in workflow and "false) gate_mode=hub-only ;;" in workflow
        and 'apply "$DEPLOY_SHA" "$RELEASE_MODE" "$gate_mode"' in workflow,
        "verify_gate=false must select the fixed Hub-only transaction mode")
require('install-hub2-node.sh" --release-mode "$release_mode" --hub-only' in transaction,
        "the Hub-only transaction mode must select the fixed Hub-only installer flag")

print("Hub-only installer guard checks passed.")
PY
