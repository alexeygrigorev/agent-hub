# RMTHZ container cleanup hook

The host timer checks local `status=created` containers carrying `agent.session`
every minute. It looks up each exact label with `hub sessions show`, retains
anything whose session is not confirmed `stopped`, then re-inspects before a
plain `docker rm`. Any Coolify label or nonzero start timestamp excludes it.
Lookup errors retain containers. Logs include IDs/actions, never arbitrary labels.
No force removal, volume removal, pruning or unlabeled-container cleanup occurs.

Install the hook on RMTHZ with the existing operator's Hub credentials:

```sh
sudo install -m 0755 host/agent_hub_host/container_cleanup.py /usr/local/lib/agent-hub-container-cleanup.py
python3 /usr/local/lib/agent-hub-container-cleanup.py --hub-bin /home/alexey/.local/bin/hub
# Inspect the dry-run log before enabling removal.
sudo install -m 0644 scripts/agent-container-cleanup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-container-cleanup.timer
```

Each removal also emits a flushed dry-run record before its second inspection.
Use `journalctl -u agent-container-cleanup.service` to inspect decisions. Labels
must identify a session registered in Hub. Unknown or unregistered labels are
retained for manual attribution. Agents remain responsible for cleanup of their
running/exited test containers and other test resources through trap/finally.
The timer is a backstop for never-started containers after confirmed session exit.
