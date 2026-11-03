# Disk floor janitor

Intake fcc31e56. The Hub host agent keeps free space on a host's root filesystem
at or above a floor (Hetzner: 100 GB) and works toward a target (200 GB). It does
this by deleting finished workspaces and reaping stale caches and test scratch.
It runs as part of the host agent, not as a separate routine, and it alerts only
when free space is still below the floor after a completed cleanup pass.

Source: `host/agent_hub_host/disk_janitor.py`. Hub side: `floorStatus` in
`src/monitoring.ts`.

## How it runs

`DiskMonitor` (the session host's disk thread) starts one background janitor pass
when one is due. A pass is due every `interval_s` (default 900 s), or every
`pressure_interval_s` (default 120 s) while free space is below the floor. Only
one pass runs at a time, and each pass stops at `time_budget_s` or after
`max_removals_per_run` removals. The disk observation for the floor-managed
mount carries a `floor` summary:

```json
{"floor_bytes": 107374182400, "target_bytes": 214748364800,
 "cleanup_at": "2026-10-30T01:00:00Z", "free_after_cleanup_bytes": 123456789,
 "freed_bytes": 0, "removed": 0, "dry_run": false}
```

The configuration lives in `disk-janitor.json` next to `disk-monitor.json` in the
host journal. If the file is missing, the janitor is disabled. If the file is
invalid, the janitor does not run (fails closed) and the disk monitoring keeps
working. Every pass writes `disk-janitor-state.json` and appends one JSON line to
`disk-janitor.log` (rotated at 2 MiB) in the journal. For a single manual pass:

```sh
HUB_CONFIG_FILE=<host.json> python3 -m agent_hub_host.disk_janitor --config <journal>/disk-janitor.json [--dry-run]
```

## What a pass removes

1. **Finished workspaces** (every pass). These are linked git worktrees directly
   under `workspace_roots` (globs allowed). Main checkouts and plain directories
   are never touched. A tree is removed only if all of these hold:
   - it is clean (no modified, staged or untracked files);
   - it has no unpushed commits;
   - it has no commits missing from all configured remote refs. Commits on
     `origin/main` qualify, and a clean feature branch qualifies once it is
     pushed to a remote even before it is merged;
   - every linked intake is DONE (Hub `linked_intake_open` is false). A tree
     whose name carries an intake id is kept when the Hub has no record of it,
     even if a terminal aplexer session refers to it: its DONE state is
     unknown.

   **Hard rule (operator, fcc31e56): uncommitted changes, unpushed commits,
   and no-DONE trees are never removed, at any pressure.** A clean terminal
   tree with unpushed commits gets a verified git bundle on the network disk
   after its grace period, and the source worktree stays in place.

   Which trees qualify, and when:
   - **Terminal:** all its sessions are terminal (`done`, `stopped`, `exited`,
     `failed`, `dead`, `pruned`, or `forgotten`). Removed after `grace_hours`
     (default 2 h) once commits are pushed. The command is `git worktree remove` (never `--force`:
     if git refuses, the tree is kept), followed by `git branch -d`, which only
     deletes a merged branch.
   - **Orphan:** no aplexer or Hub session refers to it. Removed only after
     `orphan_idle_hours` (default 24 h) and while free space is below the
     target.
   - **Held:** a human-protected session, or a Hub lifecycle lock whose
     session is terminal and whose intake is DONE. Removed only after
     `held_idle_hours` (default 24 h) and while free space is below the
     target. The lock is released just before removal and restored if the
     removal fails.
   - **Archived first:** a clean, pushed tree that also holds non-regenerable
     ignored data (for example `.local` evidence). That data is tarred to
     `archive_dir` on the network disk, under pressure and after
     `archive_idle_hours`. The archive is `files.tar.gz` plus `meta.json`. The
     tar is re-read after writing and every expected entry is checked; it is
     kept there, and only then is the tree removed. Unpushed commits instead get
     a deterministic `commits.bundle` plus `meta.json`; the source worktree is
     retained, and retries reuse the verified bundle. The archive must be on a mounted
     filesystem other than the cleaned mount, and at most `archive_max_mb`;
     otherwise the tree is kept. If that data contains a backup, dump or
     export name, the tree is kept.

   Right before removal, after any archive step, the janitor checks again that
   the tree is clean and pushed, that HEAD has not moved, that no aplexer/Hub
   session became live, and that no process is inside. For private ignored data,
   the tar contents and source files are hashed and compared again before the
   tree is removed. Any change or unreadable fact keeps the tree.

   Every removal (worktrees, caches, scratch) is receipt-first. An fsynced
   `removing` line goes to `disk-janitor-receipts.jsonl` in the journal before
   the effect; if it cannot be written, nothing is removed. A `removed` (or
   `kept_remove_refused`) line follows the effect. The file is append-only and
   never rotated. Each receipt records the path, HEAD, `origin/main` commit,
   reason, bytes and archive path.
2. **Engine caches** (`cache_rules`, every pass, age-bounded). Entries at
   `depth` below each cache directory (globs allowed, such as managed-engine
   homes) that have not been modified for `min_age_hours`. The Hetzner template
   walks one level into managed-engine `.cache` directories so quiet nested
   shards are eligible for reaping even when the top-level cache is recently
   touched.
3. **Test scratch** (`scratch_rules`, every pass, age-bounded). Entries
   matching each glob (for example `/tmp/hubtest-*`) that have not been
   modified for `min_age_hours`.

## Safety rules

Every rule fails closed: if a fact is unknown, the data is kept.

- Protected paths are never deleted. The built-in ones are `~/prod`,
  `~/backups`, `~/storagebox`, the home directory and `/`. Configured
  `protected_paths` and the archive directory are protected too. So is any
  ancestor of a protected path, and any path with a component that matches
  `prod`, `production`, `backup`, `dump` or `export`. A scratch or cache entry
  that *contains* such a name is also kept, and so is a workspace whose
  private data contains one. Such data is only ever moved by an operator,
  never deleted.
- A tree is kept if any of these hold:
  - an aplexer session is live in it (by workspace or cwd);
  - its Hub session is not terminal;
  - a linked intake is open;
  - it is git-locked (Hub lifecycle lock);
  - any process has its cwd or an open file inside it. This is checked again
    right before the removal, together with the git state.
- If `aplexer snapshot`, the Hub `worktree-guards` listing or `/proc` cannot be
  read, the workspace pass is skipped entirely.
- The janitor's own `git status` uses `--no-optional-locks`, so it never
  refreshes the index and never resets the idle clock it measures.

## Alerting

For a filesystem that has a `floor` summary, the Hub ignores the percent
thresholds: being at 85% or 90% no longer raises an informational alert. The
episode becomes `critical` (alert plus intake task) only in one of these cases:

- free space is below the floor and the last completed, non-dry-run cleanup
  pass also ended below the floor.

The alert resolves on its own once a later observation is back above the
floor. Filesystems without floor data keep the legacy 85%/95% behaviour.

## Hetzner configuration

The Hetzner configuration template is
[`config/disk-janitor-hetzner.example.json`](../config/disk-janitor-hetzner.example.json).
It uses the `/home/alexey/git` workspace root and the archive at
`/home/alexey/storagebox/agent-archives/hetzner-workspaces`. Its cache rules
cover managed-engine `.cache`, npm, Go and Cargo caches plus the equivalent
home caches. Its scratch rules cover `/tmp` test homes: `hubtest-*`,
`pytest-of-*`, `dataops-*`, `playwright*` and `miniflare-*`. The template must
be installed beside the host's `disk-monitor.json` as `disk-janitor.json` by
the authorized host-release/configuration process before the periodic job is
active; a missing file deliberately disables cleanup. In this worktree, only
the template is prepared; the host journal has not been modified and the host
agent has not been restarted.
