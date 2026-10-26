# Orphan sweeper: stop headless sessions whose worktree is gone

`host/agent_hub_host/orphan_sweeper.py` detects sessions whose
workspace/worktree directory no longer exists (`/proc/<pid>/cwd` ends in
`(deleted)` or the path is missing), after a configurable grace period
(default 2h, `ORPHAN_GRACE_HOURS` / `--grace-hours`).

- Interactive sessions Alexey started manually are never stopped; they are
  reported only (`action=report-only`, `interactive=true`). Human ownership
  comes from Hub session info (`human_owned`/`protected`/`starter`), or,
  when Hub is unreachable, conservatively from the native tag (non
  `hub-*`/`task-hub-*` tags are treated as manual).
- Expired headless orphans are stopped via the normal aplexer stop path
  (`aplexer kill <id> --grace-ms 10000`: HUP, then SIGKILL escalation),
  the reason `worktree_missing` is recorded in the journal state file and
  reported via `hub sessions observe --worktree-missing` (records
  `worktree_missing` on the Hub session; linked-intake note is best-effort
  operator follow-up when Hub credentials permit).

Default is dry-run report (omit `--apply` to only print actions). State file
(`--state-path`, default `.local/orphan-sweeper.json`) tracks
`first_missing_at` per native id for the grace period.

```sh
sudo install -m 0755 host/agent_hub_host/orphan_sweeper.py /usr/local/lib/agent-hub-orphan-sweeper.py
python3 /usr/local/lib/agent-hub-orphan-sweeper.py --hub-bin /home/alexey/.local/bin/hub --machine-id hetzner
# Inspect dry-run, then enable:
sudo install -m 0644 scripts/agent-orphan-sweeper.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-orphan-sweeper.timer
```

Logs contain only session ids/tags/actions/reasons, never transcripts.
