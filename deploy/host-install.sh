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
python3 "$WORK/updater.py" --base-url "$BASE" --no-restart --install-service
if [ ! -f "$HOME/.config/agent-hub/v2.json" ]; then
    "$ROOT/current/bin/python" "$ROOT/current/hub2-enroll.py" --server "$BASE" --root "${HUB_WORKSPACE_ROOT:-$HOME}" \
        --engine codex --engine claude --engine go --engine zai --engine antigravity
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
systemctl --user start agent-hub2-host.service agent-hub2-host-update.timer
systemctl --user is-active --quiet agent-hub2-host.service
printf '%s\n' 'Hub2 host installed and enrolled.'
