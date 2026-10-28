"""Fence a maintained-launcher task that provably never crossed spawn.

The launcher's queued -> starting transition and Popen use launch.lock.
Taking that same lock and changing queued -> failed fences even a delayed
launcher still doing preflight. Missing native observations alone prove nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3

REASON = "hub-fleet: launcher refused before native start"


def settle_unstarted(launcher, task_id, workspace, target_cwd, diagnostic=None):
    """Return positive no-start proof, or False on contention/unknown effects.

    POSIX maintained-launcher storage only. Unsupported platforms fail closed.
    No native process is killed, and no row or original payload is deleted.
    """
    # Only locally constructed fixed diagnostics can enter launcher storage.
    reason = REASON
    if isinstance(diagnostic, dict):
        # Messages are already fixed summaries, so validate the complete DTO
        # against the same registry instead of accepting arbitrary caller text.
        from .launch_diagnostics import REFUSALS
        known = {(slug, message) for slug, message in REFUSALS.values()}
        if (diagnostic.get('reason'), diagnostic.get('message')) in known:
            reason += ': ' + diagnostic['reason'] + ': ' + diagnostic['message']
        else:
            reason += ': launcher_command_failed: Launcher command failed; diagnostic output withheld.'
    if not re.fullmatch(r"hub-[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
        return False
    try:
        import fcntl
        cfg = Path(launcher["config_dir"])
        db, lock = cfg / "state.db", cfg / "launch.lock"
        if cfg.is_symlink() or not db.is_file() or db.is_symlink() or lock.is_symlink():
            return False
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(str(db), timeout=1) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT state, payload, idempotency_key, reason FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if not row or row[2] != task_id:
                    return False
                payload = json.loads(row[1])
                if payload.get("owner") != "hub-fleet" or Path(payload["cwd"]).resolve(strict=True) != Path(target_cwd).resolve(strict=True):
                    return False
                if row[0] != "queued" and not (row[0] == "failed" and isinstance(row[3], str) and (row[3] == REASON or row[3].startswith(REASON + ": "))):
                    return False
                if conn.execute("SELECT 1 FROM task_native_bindings WHERE task_id = ?", (task_id,)).fetchone():
                    return False
                for root in {Path(workspace).resolve(strict=True), Path(target_cwd).resolve(strict=True)}:
                    local = root / ".local"
                    if local.is_symlink():
                        return False
                    for name in (f"launch-{task_id}-start.json", f"first-action-{task_id}.json"):
                        artifact = local / name
                        if artifact.exists() or artifact.is_symlink():
                            return False
                if row[0] == "queued":
                    changed = conn.execute("UPDATE tasks SET state = 'failed', reason = ?, reviewer = 'hub-fleet-recovery', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND state = 'queued'", (reason, task_id)).rowcount
                    if changed != 1:
                        return False
                return True
    except (ImportError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False


def reconcile_resources(launcher, runner, *, workspace_paths=None, workspace_aliases=None):
    """Periodic launcher reconciliation also covers stops/failures without journals.

    Launcher owns exact native death proof and resident credit. Unknown or live
    workers retain their leases; this command never starts or stops a process.
    """
    from .fleet import utcnow
    observed_at = utcnow()  # Do not hide launches that occur during the scan.
    try:
        result = runner(list(launcher["argv"]) + ["--config-dir", launcher["config_dir"],
                                               "resource-status"], cwd=launcher["root"])
        memory = result["active_mem_mb"]
        if type(memory) not in (int, float) or not 0 <= memory <= 10**9:
            return None
        projected = {"active_mem_mb": memory, "observed_at": observed_at}
        disk = result.get('active_disk_mb')
        if type(disk) in (int, float) and 0 <= disk <= 10**9:
            # Maintained launcher/resources.py contract: 20 GiB plus the
            # ledger's active disk promises and one bounded 512 MiB spike.
            # This mirrors the gate; it never lowers or configures it.
            projected.update(active_disk_mb=disk, disk_floor_mb=20480, next_disk_mb=512)
            if workspace_paths is not None:
                import shutil
                paths = []
                for raw in sorted(set(workspace_paths))[:64]:
                    try:
                        path = Path(raw).resolve(strict=True)
                        if path.is_dir():
                            paths.append({'path': str(path), 'free_gb': shutil.disk_usage(path).free / (1024**3)})
                            # Native launches put TMPDIR here. Preserve this
                            # logical association even when tmp is a symlink or
                            # separate mount. Before creation, use its nearest
                            # existing parent without creating any directories.
                            temp = path / '.local' / 'tmp'
                            existing = temp
                            while not existing.exists():
                                if existing.is_symlink():
                                    raise ValueError('dangling temp symlink')
                                existing = existing.parent
                            if existing.is_dir():
                                paths.append({'path': str(temp), 'free_gb': shutil.disk_usage(existing).free / (1024**3)})
                    except (OSError, ValueError, TypeError):
                        continue
                projected['disk_paths'] = paths
                aliases = {}
                if isinstance(workspace_aliases, dict):
                    for alias, raw in sorted(workspace_aliases.items())[:64]:
                        try:
                            target = str(Path(raw).resolve(strict=True))
                            if isinstance(alias, str) and 0 < len(alias) <= 512 and any(p['path'] == target for p in paths):
                                aliases[alias] = target
                        except (OSError, ValueError, TypeError):
                            continue
                projected['disk_aliases'] = aliases
            breakdown = result.get('breakdown')
            if isinstance(breakdown, list) and len(breakdown) <= 256 and all(
                    isinstance(r, dict) and isinstance(r.get('task_id'), str)
                    and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', r['task_id']) for r in breakdown):
                projected['disk_task_ids'] = [r['task_id'] for r in breakdown]
        return projected
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        return None
