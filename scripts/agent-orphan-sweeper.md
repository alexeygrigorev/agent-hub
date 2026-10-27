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
# Pin an already verified immutable host release. Do not copy the module alone.
# Set PYTHONPATH=<verified-release>/host in this root-owned environment file:
sudoedit /home/alexey/.config/agent-hub/orphan-sweeper.env
PYTHONPATH=<verified-release>/host python3 -m agent_hub_host.orphan_sweeper --hub-bin /home/alexey/.local/bin/hub --machine-id hetzner
# Inspect dry-run, then enable:
sudo install -m 0644 scripts/agent-orphan-sweeper.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-orphan-sweeper.timer
```

Logs contain only session ids/tags/actions/reasons, never transcripts.

The normal session host runs the same detection code in report-only mode every
60 seconds, independently of the slow maintenance loop. It posts observations
with the existing host credential. No extra timer installation is needed for
missing-worktree alerts or recreated-path reporting. The optional timer above
retains the legacy grace/interactive behavior and must use the package from a
verified release so relative imports resolve.

Before launching into a linked checkout, the host records its original Git
common directory path and device/inode under
`~/.local/state/agent-hub/worktree-identities/<launch-id>.json`. A recreated path
is accepted only when that original identity matches. Unknown legacy evidence
stays conservative; a valid unrelated repository never counts as recovery.
