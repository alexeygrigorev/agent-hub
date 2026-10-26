# RMTHZ container cleanup hook (TTL reaper)

The host timer reaps docker containers carrying a test label
(`agent.session=<id>` or `pocketshell.test=1`) older than N hours
(configurable, default 6h). It never touches unlabeled, running/restarting,
or Coolify (`coolify*`, `com.coolify*`) containers, and it logs each removal.

Two paths share one safety core:
- legacy: `status=created` + zero StartedAt + `agent.session` label, only
  after Hub confirms the linked session is `stopped` (re-inspected before
  `docker rm`).
- TTL: any non-running labeled test container older than `--max-age-hours`.
  When `agent.session` names a Hub session, only a confirmed `stopped`
  (or unknown/missing) session authorizes removal; live sessions retain.
  Pure `pocketshell.test` containers need no Hub lookup.

Ship it with dry-run mode (default): running without `--apply` only prints
`dry-run ... action=remove-ttl/remove-created`. Lookup errors retain.
Logs include IDs/actions/ages only, never arbitrary labels.
No force removal, volume removal, pruning or unlabeled-container cleanup.

Install the hook on RMTHZ with the existing operator's Hub credentials:

```sh
sudo install -m 0755 host/agent_hub_host/container_cleanup.py /usr/local/lib/agent-hub-container-cleanup.py
python3 /usr/local/lib/agent-hub-container-cleanup.py --hub-bin /home/alexey/.local/bin/hub --max-age-hours 6
# Inspect the dry-run log before enabling removal.
python3 /usr/local/lib/agent-hub-container-cleanup.py --apply --max-age-hours 6 --hub-bin /home/alexey/.local/bin/hub
sudo install -m 0644 scripts/agent-container-cleanup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-container-cleanup.timer
```

Each removal also emits a flushed dry-run record before its second inspection.
Use `journalctl -u agent-container-cleanup.service` to inspect decisions. Labels
must identify a session registered in Hub for the `agent.session` path; unknown
or unregistered labels are retained for manual attribution in the legacy path
and removed by TTL only when old and non-running. Agents remain responsible for
cleanup of their running/exited test containers and other test resources through
trap/finally. The timer is a backstop for orphaned test containers.
