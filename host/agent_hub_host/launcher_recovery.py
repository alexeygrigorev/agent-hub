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


def settle_unstarted(launcher, task_id, workspace, target_cwd):
    """Return positive no-start proof, or False on contention/unknown effects.

    POSIX maintained-launcher storage only. Unsupported platforms fail closed.
    No native process is killed, and no row or original payload is deleted.
    """
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
                if row[0] != "queued" and not (row[0] == "failed" and row[3] == REASON):
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
                    changed = conn.execute("UPDATE tasks SET state = 'failed', reason = ?, reviewer = 'hub-fleet-recovery', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND state = 'queued'", (REASON, task_id)).rowcount
                    if changed != 1:
                        return False
                return True
    except (ImportError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False
