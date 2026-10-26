#!/usr/bin/env bash
# Install the authorized daily repo refresh on this host; no cloud operations.
set -euo pipefail
if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo 'Usage: install-repos-refresh.sh MACHINE_ID' >&2
  exit 2
fi
HUB_REPO_MACHINE="$1"
HUB_REPO_BIN="$(command -v hub)"
HUB_REPO_CONFIG="${HUB_CONFIG_FILE:-${HOME}/.config/agent-hub/host.json}"
HUB_UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
mkdir -p "$HUB_UNIT_DIR"
# Refuse newline/specifier injection into unit values.
for value in "$HUB_REPO_BIN" "$HUB_REPO_CONFIG" "$HOME"; do
  if [[ "$value" == *$'\n'* || "$value" == *'%'* || "$value" == *'"'* || "$value" == *'\\'* ]]; then exit 2; fi
done
cat > "$HUB_UNIT_DIR/agent-hub-repos-refresh.service" <<UNIT
[Unit]
Description=Refresh private Hub repository registry

[Service]
Type=oneshot
Environment="HUB_CONFIG_FILE=$HUB_REPO_CONFIG"
ExecStart="$HUB_REPO_BIN" repos refresh --machine "$HUB_REPO_MACHINE" --root "$HOME/git"
TimeoutStartSec=1800
Nice=10
UMask=0077
UNIT
cat > "$HUB_UNIT_DIR/agent-hub-repos-refresh.timer" <<'UNIT'
[Unit]
Description=Daily private Hub repository registry refresh

[Timer]
OnCalendar=daily
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now agent-hub-repos-refresh.timer
systemctl --user start agent-hub-repos-refresh.service
systemctl --user is-enabled agent-hub-repos-refresh.timer
systemctl --user is-active agent-hub-repos-refresh.timer
