#!/usr/bin/env bash
# Install the Resource Gate monitor on RMTHZ. It sends mail through that host's
# existing msmtp `alerts` account and does not install anything on the GCP VM.
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root: sudo bash scripts/install-resource-gate-monitor.sh" >&2
  exit 2
fi
id alexey >/dev/null 2>&1 || { echo "required service user alexey is missing" >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -D -o root -g root -m 755 "$REPO_ROOT/scripts/resource-gate-monitor.sh" \
  /usr/local/sbin/resource-gate-monitor.sh
install -o root -g root -m 644 "$REPO_ROOT/deploy/resource-gate-monitor.service" \
  /etc/systemd/system/resource-gate-monitor.service
install -o root -g root -m 644 "$REPO_ROOT/deploy/resource-gate-monitor.timer" \
  /etc/systemd/system/resource-gate-monitor.timer
install -d -o alexey -g alexey -m 750 /var/lib/resource-gate-monitor

systemctl daemon-reload
systemctl enable --now resource-gate-monitor.timer
systemctl start resource-gate-monitor.service
systemctl is-active --quiet resource-gate-monitor.timer
echo "Resource Gate monitor installed; timer is active."
