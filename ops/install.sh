#!/usr/bin/env bash
# Install ops scripts + systemd user units. Safe to re-run after every change on main.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; dest="$HOME/.local/share/agent-hub-ops"
mkdir -p "$dest" "$HOME/bin" "$HOME/.config/systemd/user"
install -m755 "$here/lib/session-ctl.sh" "$here/lib/watchdog.sh" "$dest/"
install -m755 "$here/lib/send.sh" "$HOME/bin/orch-send"
install -m755 "$here/lib/send.sh" "$HOME/bin/oncall-send"
install -m644 "$here"/systemd/ops-watchdog.service "$here"/systemd/ops-watchdog.timer "$HOME/.config/systemd/user/"
mkdir -p "$HOME/tmp/briefs" && install -m644 "$here/orchestrator/brief.md" "$HOME/tmp/briefs/orchestrator-main.md"
systemctl --user daemon-reload
systemctl --user enable --now ops-watchdog.timer
echo "installed: $dest, ~/bin/orch-send, ~/bin/oncall-send, ops-watchdog.timer"
