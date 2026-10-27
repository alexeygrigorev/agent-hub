#!/usr/bin/env bash
# Install ops scripts + systemd user units. Safe to re-run after every change on main.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; dest="$HOME/.local/share/agent-hub-ops"
mkdir -p "$dest" "$HOME/bin" "$HOME/.config/systemd/user"
install -m755 "$here/lib/session-ctl.sh" "$here/lib/liveness.sh" "$dest/"
install -m755 "$here/lib/send.sh" "$HOME/bin/orch-send"
install -m755 "$here/lib/send.sh" "$HOME/bin/oncall-send"
install -m644 "$here"/systemd/ops-liveness.service "$here"/systemd/ops-liveness.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now ops-liveness.timer
echo "installed: $dest, ~/bin/orch-send, ~/bin/oncall-send, ops-liveness.timer"
