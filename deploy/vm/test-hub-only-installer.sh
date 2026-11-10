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
require(is_inside_hub_only_guard('install -m 644 "$DEPLOY_DIR/hub.caddy" "$CADDY_SITE"'),
        "Hub-only install must skip Hub site installation")
require(is_inside_hub_only_guard('systemctl reload caddy 2>/dev/null || systemctl restart caddy'),
        "Hub-only install must skip Caddy reload/restart")
require('systemctl restart agent-hub-hub2.service' in installer,
        "Hub-only install must still restart Hub2")

require('HUB_ONLY: ${{ inputs.hub_only }}' in workflow,
        "deployment must receive the explicit hub_only choice")
require("true) caddy_mode=hub-only ;;" in workflow and "false|\"\") caddy_mode=full ;;" in workflow
        and 'apply "$DEPLOY_SHA" "$RELEASE_MODE" "$caddy_mode"' in workflow,
        "hub_only=true must select the fixed Hub-only transaction mode")
require('install-hub2-node.sh" --release-mode "$release_mode" --hub-only' in transaction,
        "the Hub-only transaction mode must select the fixed Hub-only installer flag")

print("Hub-only installer guard checks passed.")
PY
