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
# Agent rm guard (incident 504d662b): refuse recursive rm of $HOME and critical dirs.
mkdir -p "$dest/guard-bin" "$HOME/.config/environment.d"
install -m755 "$here/guard-bin/rm" "$dest/guard-bin/rm"
install -m644 "$here/guard-bin/env.sh" "$dest/guard-bin/env.sh"
printf 'PATH=%s:${PATH}\nBASH_ENV=%s\n' "$dest/guard-bin" "$dest/guard-bin/env.sh" > "$HOME/.config/environment.d/50-agent-rm-guard.conf"
hook='[ -f "$HOME/.local/share/agent-hub-ops/guard-bin/env.sh" ] && . "$HOME/.local/share/agent-hub-ops/guard-bin/env.sh"  # agent rm guard (504d662b)'
for rc in "$HOME/.bashrc" "$HOME/.profile"; do
  touch "$rc"; grep -qF 'agent rm guard (504d662b)' "$rc" || printf '\n%s\n' "$hook" >> "$rc"
done
systemctl --user set-environment PATH="$dest/guard-bin:$(systemctl --user show-environment | sed -n 's/^PATH=//p')" BASH_ENV="$dest/guard-bin/env.sh" || true
systemctl --user daemon-reload
systemctl --user enable --now ops-watchdog.timer
echo "installed: $dest, ~/bin/orch-send, ~/bin/oncall-send, ops-watchdog.timer"
