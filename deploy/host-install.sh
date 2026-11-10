#!/bin/sh
# Linux user-service installer. Pairing code is read privately by hub2 enroll.
# curl -fsSL https://hub.dtcdev.click/cli/host-install.sh | sh
set -eu
umask 077
BASE="${HUB_BASE_URL:-https://hub.dtcdev.click}"
ROOT="$HOME/.local/share/agent-hub2"
mkdir -p "$ROOT/tmp" "$HOME/.local/bin"
WORK="$(mktemp -d "$ROOT/tmp/install.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
# The first updater is authenticated by HTTPS. It verifies all subsequent
# artifacts against the release manifest before installing anything.
curl --proto '=https' --proto-redir '=https' -fsSL "${BASE%/}/cli/hub2-self-update.py" -o "$WORK/updater.py"
CONFIG="$HOME/.config/agent-hub/v2.json"
if [ -f "$CONFIG" ] && [ -L "$ROOT/current" ] && [ -f "$ROOT/current/release.json" ]; then
    RESTART_INSTALL=true
else
    RESTART_INSTALL=false
    python3 "$WORK/updater.py" --base-url "$BASE" --no-restart --install-service
    if [ ! -f "$CONFIG" ]; then
        "$ROOT/current/bin/python" "$ROOT/current/hub2-enroll.py" --server "$BASE" --root "${HUB_WORKSPACE_ROOT:-$HOME}" \
            --engine codex --engine claude --engine go --engine zai --engine antigravity
    fi
fi
# An existing legacy CLI remains untouched. Use hub2 for host administration;
# the matched full CLI is available as current/hub.pyz.
if [ ! -e "$HOME/.local/bin/hub2" ] && [ ! -L "$HOME/.local/bin/hub2" ]; then
    ln -s "$ROOT/current/bin/hub2" "$HOME/.local/bin/hub2"
fi
if [ ! -e "$HOME/.local/bin/hub" ] && [ ! -L "$HOME/.local/bin/hub" ]; then
    ln -s "$ROOT/current/hub.pyz" "$HOME/.local/bin/hub"
fi
loginctl enable-linger "$(id -un)"
if [ "$RESTART_INSTALL" = true ]; then
    # A newer version must restart the existing daemon, even when it is active.
    python3 "$WORK/updater.py" --base-url "$BASE" --install-service
else
    systemctl --user start agent-hub2-host.service
    python3 "$WORK/updater.py" --root "$ROOT" --check-service-ready
    systemctl --user enable agent-hub2-host.service
    systemctl --user enable --now agent-hub2-host-update.timer
fi
printf '%s\n' 'Hub2 host installed and enrolled.'
