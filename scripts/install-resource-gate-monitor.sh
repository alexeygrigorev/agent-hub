#!/usr/bin/env bash
# Install the external Resource Gate/desktop/Hub monitor on RMTHZ. Its gate
# credential is fetched from DataOps directly into the protected state dir.
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root: sudo bash scripts/install-resource-gate-monitor.sh" >&2
  exit 2
fi
id alexey >/dev/null 2>&1 || { echo "required service user alexey is missing" >&2; exit 2; }
command -v runuser >/dev/null 2>&1 || { echo "required command is missing: runuser" >&2; exit 2; }
[[ -x /home/alexey/.local/bin/dataops ]] || { echo "DataOps CLI is unavailable for alexey" >&2; exit 2; }
[[ -r /home/alexey/.agent-browser/remotes.json ]] || { echo "Agent Browser remote profile is missing" >&2; exit 2; }
NVM_DIR=/home/alexey/.nvm
NODE_BIN="$(NVM_DIR="$NVM_DIR" /bin/bash -c '. "$NVM_DIR/nvm.sh" >/dev/null 2>&1 && nvm which default' 2>/dev/null)" \
  || { echo "Node.js default cannot be resolved from alexey's NVM installation" >&2; exit 2; }
[[ -x "$NODE_BIN" ]] || { echo "Node.js is unavailable at the configured NVM default" >&2; exit 2; }
NODE_DIR="$(dirname "$NODE_BIN")"
RUNUSER_PATH="$NODE_DIR:/home/alexey/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR=/var/lib/resource-gate-monitor
GATE_TOKEN_FILE="$STATE_DIR/gate-token"
install -D -o root -g root -m 755 "$REPO_ROOT/scripts/resource-gate-monitor.sh" \
  /usr/local/sbin/resource-gate-monitor.sh
install -o root -g root -m 644 "$REPO_ROOT/deploy/resource-gate-monitor.service" \
  /etc/systemd/system/resource-gate-monitor.service
install -o root -g root -m 644 "$REPO_ROOT/deploy/resource-gate-monitor.timer" \
  /etc/systemd/system/resource-gate-monitor.timer
install -d -o alexey -g alexey -m 750 "$STATE_DIR"

# `--out` writes the DataOps value directly to a mode-0600 file and never to
# stdout. The monitor reads this file; no token is stored in the repository or
# passed through a unit environment variable.
runuser -u alexey -- env PATH="$RUNUSER_PATH" DATAOPS_CONFIG_DIR=/home/alexey/.config/dataops \
  /home/alexey/.local/bin/dataops secrets get resource-gate.agent-token \
  --field value --out "$GATE_TOKEN_FILE" >/dev/null
chown alexey:alexey "$GATE_TOKEN_FILE"
chmod 600 "$GATE_TOKEN_FILE"
[[ -s "$GATE_TOKEN_FILE" ]] || { echo "DataOps returned an empty Resource Gate token" >&2; exit 2; }

# Validate the configured authenticated desktop read without requiring an
# active time-limited grant. `remote health` returns exit 1 when no grant is
# active; the JSON must still prove that the gateway and desktop are online and
# that an authenticated grant state was returned.
HEALTH_JSON="$STATE_DIR/.desktop-health.$$.json"
trap '[[ ! -e "$HEALTH_JSON" ]] || unlink "$HEALTH_JSON"' EXIT
HEALTH_RC=0
runuser -u alexey -- env AGENT_BROWSER_REMOTE_CONFIG=/home/alexey/.agent-browser/remotes.json \
  NVM_DIR="$NVM_DIR" "$NODE_BIN" /home/alexey/opt/agent-browser/bin/cli.js remote health desktop \
  > "$HEALTH_JSON" 2>/dev/null || HEALTH_RC=$?
python3 - "$HEALTH_JSON" <<'PY'
import json, sys
try:
    result = json.load(open(sys.argv[1], encoding="utf-8"))
    service = result.get("service")
    grant = result.get("grant")
    valid = (result.get("device") == "desktop" and result.get("desktop") == "online"
             and isinstance(service, dict) and service.get("ok") is True
             and isinstance(grant, dict) and type(grant.get("active")) is bool
             and not result.get("error"))
except Exception:
    valid = False
if not valid:
    raise SystemExit("authenticated desktop health preflight failed")
print("authenticated desktop health preflight passed")
PY
if (( HEALTH_RC > 1 )); then
  echo "Agent Browser health command failed with exit $HEALTH_RC" >&2
  exit 2
fi

systemctl daemon-reload
systemctl enable --now resource-gate-monitor.timer
systemctl start resource-gate-monitor.service
systemctl is-active --quiet resource-gate-monitor.timer
echo "Resource Gate, desktop grant path, and Hub monitor installed; timer is active."
