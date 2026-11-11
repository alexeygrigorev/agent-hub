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
require('sleep 3' not in installer,
        "installer must not rely on a single fixed sleep before health check")
require('wait_for_hub2_readiness()' in installer,
        "installer must poll Hub2 readiness with a bounded loop")

import os
import subprocess
import tempfile

services_start = installer.index('echo "== services"')
services_block = installer[services_start:]
scratch = Path(sys.argv[1]).resolve().parents[1] / ".local/tmp"
scratch.mkdir(parents=True, exist_ok=True)

with tempfile.TemporaryDirectory(prefix="hub-installer-readiness-", dir=scratch) as tmp:
    work = Path(tmp)
    bindir = work / "bin"
    bindir.mkdir()

    (bindir / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"${SYSTEMCTL_LOG:?}\"\n"
        "if [[ \"$1\" == is-active ]]; then\n"
        "  count=0\n"
        "  [[ ! -s \"${IS_ACTIVE_COUNT_FILE:?}\" ]] || read -r count < \"$IS_ACTIVE_COUNT_FILE\"\n"
        "  count=$((count + 1))\n"
        "  printf '%s\\n' \"$count\" > \"$IS_ACTIVE_COUNT_FILE\"\n"
        "  if [[ \"${FAIL_ACTIVE_AT:-0}\" =~ ^[0-9]+$ ]] && ((FAIL_ACTIVE_AT > 0 && count >= FAIL_ACTIVE_AT)); then\n"
        "    exit 3\n"
        "  fi\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"$*\" == *'http://127.0.0.1:8787/health'* ]] || exit 96\n"
        "count=0\n"
        "[[ ! -s \"${CURL_COUNT_FILE:?}\" ]] || read -r count < \"$CURL_COUNT_FILE\"\n"
        "count=$((count + 1))\n"
        "printf '%s\\n' \"$count\" > \"$CURL_COUNT_FILE\"\n"
        "if [[ \"${FAIL_CURL_FIRST:-0}\" =~ ^[0-9]+$ ]] && ((count <= FAIL_CURL_FIRST)); then\n"
        "  printf 'curl: (7) Failed to connect to 127.0.0.1 port 8787 after 0 ms: Could not connect to server\\n' >&2\n"
        "  exit 7\n"
        "fi\n"
        "printf '{\"ok\":true,\"service\":\"hub2\"}\\n'\n",
        encoding="utf-8",
    )
    (bindir / "sleep").write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"$#\" == 1 && \"$1\" == 1 ]] || exit 100\n"
        "printf '%s\\n' \"$1\" >> \"${SLEEP_LOG:?}\"\n",
        encoding="utf-8",
    )
    for tool in ("systemctl", "curl", "sleep"):
        (bindir / tool).chmod(0o755)

    def run_services(hub_only: str = "true", fail_curl_first: int = 0, fail_active_at: int = 0):
        systemctl_log = work / "systemctl.log"
        sleep_log = work / "sleep.log"
        curl_count = work / "curl.count"
        active_count = work / "active.count"
        for f in (systemctl_log, sleep_log, curl_count, active_count):
            f.write_text("", encoding="utf-8")
        env = dict(
            os.environ,
            PATH=f"{bindir}:{os.environ['PATH']}",
            HUB_ONLY=hub_only,
            SYSTEMCTL_LOG=str(systemctl_log),
            SLEEP_LOG=str(sleep_log),
            CURL_COUNT_FILE=str(curl_count),
            IS_ACTIVE_COUNT_FILE=str(active_count),
            FAIL_CURL_FIRST=str(fail_curl_first),
            FAIL_ACTIVE_AT=str(fail_active_at),
        )
        proc = subprocess.run(["bash", "-euo", "pipefail", "-c", services_block], env=env, text=True, capture_output=True)
        return (
            proc,
            int(curl_count.read_text(encoding="utf-8").strip() or "0"),
            len(sleep_log.read_text(encoding="utf-8").splitlines()),
            systemctl_log.read_text(encoding="utf-8").splitlines(),
        )

    # 1. Transient ECONNREFUSED on first 3 attempts -> ready on attempt 4
    proc, curls, sleeps, sys_calls = run_services(hub_only="true", fail_curl_first=3)
    require(proc.returncode == 0, f"transient ECONNREFUSED should recover cleanly: {proc.stderr}")
    require(curls == 4, f"expected 4 curl attempts, got {curls}")
    require(sleeps == 3, f"expected 3 sleep intervals, got {sleeps}")
    require('{"ok":true,"service":"hub2"}' in proc.stdout, "missing health response on stdout")
    require(not any("caddy" in line for line in sys_calls), "Hub-only mode must not touch caddy")

    # 2. Permanent ECONNREFUSED across all 15 attempts -> loud diagnostic failure
    proc, curls, sleeps, _ = run_services(hub_only="true", fail_curl_first=15)
    require(proc.returncode != 0, "permanent ECONNREFUSED must fail closed")
    require(curls == 15, f"expected 15 bounded curl attempts, got {curls}")
    require(sleeps == 14, f"expected 14 sleeps across 15 attempts, got {sleeps}")
    require(
        "ERROR: Hub2 local health check http://127.0.0.1:8787/health did not become ready after 15 attempts (last curl exit=7)" in proc.stderr,
        f"missing bounded readiness diagnostic on stderr: {proc.stderr}",
    )

    # 3. Terminal service crash on attempt 2 -> immediate failure without exhausting 15 sleeps
    proc, curls, sleeps, _ = run_services(hub_only="true", fail_curl_first=15, fail_active_at=2)
    require(proc.returncode != 0, "inactive/failed service must fail closed immediately")
    require(curls == 1, f"expected 1 curl before service crash on attempt 2, got {curls}")
    require(sleeps == 1, f"expected 1 sleep before service crash on attempt 2, got {sleeps}")
    require(
        "ERROR: agent-hub-hub2.service entered an inactive/failed state on readiness attempt 2/15." in proc.stderr,
        f"missing terminal service diagnostic on stderr: {proc.stderr}",
    )

print("Hub-only installer guard checks passed.")
PY
