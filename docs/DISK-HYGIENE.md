# Hub Host Disk Hygiene, Early Alerting & ENOSPC-Safe Delivery

## Overview

To prevent Hub hosts (starting with RMTHZ) from silently filling `/` to 100% and losing alerts or failing intakes:

1. **Automatic Disk Hygiene (`hub2/host/agent_hub_host/disk_hygiene.py`, `hub-disk-hygiene.timer`)**
   - Runs every 6 hours via `systemctl --user` on Linux Hub hosts (and can be triggered on-demand by Hub admission).
   - Inspects worktrees whose Hub sessions are finished (`completed`, `complete`, `stopped`, `failed`, `archived`, `retired-finished`, `expired`) and idle `> 24h`.
   - Deletes **only** untracked/ignored rebuildable artifacts: `.venv`, `node_modules`, `target`, `dist`, `build`, `.next`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`.
   - **Never deletes**: the worktree itself, unpushed commits, dirty tracked files, git-tracked files, `~/tmp/agent-reports`, `~/git/relay`, recordings, DB files (`*.db`, `*.sqlite*`, `*-wal`, `*-shm`), credentials, or any worktree with a live process `cwd`/open file/mapped file in `/proc`.
   - Dry-run is the default (`--apply` enables deletion). Logs every action and emits a `DAILY_HYGIENE_SUMMARY` line with GB reclaimed by category (`venv`, `node_modules`, `rust_target`, `build_dist`, `python_cache`, `managed_engines`).

2. **Managed-Engines Retention Policy (`~/.config/agent-hub/managed-engines`)**
   - **Keep current**: any directory matching `<label>-<revision>` in `managed-engines/state.json` or referenced by any running Hub session.
   - **Prune superseded**: unreferenced superseded `<label>-<revision>` directories not used by any running session or open `/proc` handle.
   - **Cache hygiene**: safe regenerable package caches inside `managed-engines/*/home/{.cache/uv,.cache/pip,.npm}` when not open by a live process.

3. **Early Disk Alerting (`hub2/host/agent_hub_host/disk_alert.py`, `hub-disk-alert.timer`)**
   - Runs every 5 minutes, storing its sample/dedupe state in tmpfs (`$XDG_RUNTIME_DIR/hub-disk-alert` or `/dev/shm`).
   - Fires **WARN** at `>= 85%` used on `/`, **CRIT** at `>= 92%` used, and **CRIT** on projected fill within `24h`.
   - Delivers alerts via `oncall-send` (with optional `HUB_DISK_ALERT_HTTP_URL` fallback).

4. **ENOSPC-Safe `oncall-send` / `orch-send` (`ops/lib/send.sh`)**
   - Attempts durable write to `~/tmp/agent-reports/<role>/inbox/`.
   - On write failure (e.g. `ENOSPC`), falls back to tmpfs (`$XDG_RUNTIME_DIR/agent-reports/<role>/inbox` or `/dev/shm/agent-reports-$USER/<role>/inbox`), prints `STATUS=fallback_tmpfs`, and delivers the alert pointer to the live session pane.
   - Automatically drains tmpfs-queued `.md` files back into the primary inbox on subsequent invocations (or `--drain`) once disk space returns.

5. **Hub Admission Disk Backpressure (`resource_admission.py`, `machine-do.ts`, `project-do.ts`)**
   - Pre-dispatch admission checks `free_gb >= disk_floor_gb` (default `15.0` GiB, configurable via `disk_floor_gb` / `HUB2_DISK_FLOOR_GB`).
   - When below the floor, admission triggers `disk_hygiene.run_hygiene(apply=True)` once per cooldown window and refuses dispatch with `disk_below_floor`.
   - `ProjectDO` keeps the intake `queued` (instead of marking it `failed`/`blocked`/`launch_failed`) and re-queues `dispatch_sagas` to `pending_machine` with a 30s backoff so intakes resume automatically once disk space returns.
