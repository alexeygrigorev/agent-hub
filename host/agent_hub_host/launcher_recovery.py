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


def settle_retired_legacy(launcher, *, state_dir=None):
    """Release old unbound reservations only with exact native finished proof.

    Pre-journal tasks can outlive removed workspaces. A native tombstone with
    the exact task tag, original cwd and creation ordering is positive death
    evidence. Missing workspaces/units or an absent native catalog are not.
    Never invent a native incarnation/binding or task-result success.
    """
    import datetime as dt
    import uuid
    from .private_fs import read_bytes
    settled = []
    try:
        import fcntl
        cfg = Path(launcher['config_dir'])
        db, lock = cfg / 'state.db', cfg / 'launch.lock'
        native = Path(state_dir or os.environ.get('APLEXER_STATE_DIR') or Path.home() / '.local/state/aplexer')
        if cfg.is_symlink() or not db.is_file() or db.is_symlink() or lock.is_symlink():
            return settled
        # Bound directory enumeration too; overflow means incomplete evidence.
        from itertools import islice
        paths = list(islice((native / 'retired-sessions').iterdir(), 4097))
        if len(paths) > 4096:
            return settled
        live_paths = list(islice((native / 'sessions').iterdir(), 501))
        if len(live_paths) > 500:
            return settled
        live_tags = set()
        for directory in live_paths:
            # An unreadable live record makes absence unprovable. Never
            # release a reservation based on a partial native snapshot.
            row = json.loads(read_bytes(directory / 'session.json', 1048576, private=False))
            if not isinstance(row, dict) or not isinstance(row.get('tag'), str):
                return settled
            live_tags.add(row['tag'])
        proofs = {}
        for directory in paths:
            try:
                sid = str(uuid.UUID(directory.name))
                if sid != directory.name or ((native / 'sessions' / sid).exists() or (native / 'sessions' / sid).is_symlink()):
                    continue
                tomb = json.loads(read_bytes(directory / 'tombstone.json', 65536))
                if tomb.get('id') != sid or tomb.get('cause') != 'finished' or type(tomb.get('finished_at_ms')) is not int:
                    continue
                tag = tomb.get('tag')
                if not isinstance(tag, str) or not re.fullmatch(r'task-hub-[A-Za-z0-9][A-Za-z0-9_-]{0,63}', tag):
                    continue
                proofs.setdefault(tag, []).append(tomb)
            except (OSError, ValueError, TypeError):
                continue
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(str(db), timeout=1) as conn:
                conn.execute('BEGIN IMMEDIATE')
                rows = conn.execute("SELECT id, payload, created_at, idempotency_key FROM tasks WHERE state IN ('running','starting','launch-uncertain','stalled') AND id NOT IN (SELECT task_id FROM task_native_bindings) LIMIT 501").fetchall()
                if len(rows) > 500:
                    return settled
                for task_id, raw, created, key in rows:
                    try:
                        if key != task_id:
                            continue
                        payload = json.loads(raw)
                        if payload.get('owner') != 'hub-fleet' or not isinstance(payload.get('cwd'), str):
                            continue
                        matches = proofs.get('task-' + task_id, [])
                        if len(matches) != 1 or 'task-' + task_id in live_tags:
                            continue
                        tomb = matches[0]
                        # SQLite task timestamps are UTC. Preserve the original
                        # path spelling: resolve() could hide tag/path reuse.
                        at = dt.datetime.fromisoformat(created)
                        if at.tzinfo is None:
                            at = at.replace(tzinfo=dt.timezone.utc)
                        if (not Path(payload['cwd']).is_absolute() or tomb.get('workspace') != payload['cwd']
                                or not int(at.timestamp() * 1000) <= tomb['finished_at_ms'] <= int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)):
                            continue
                        sid = tomb['id']
                        if ((native / 'sessions' / sid).exists() or (native / 'sessions' / sid).is_symlink()):
                            continue
                        conn.execute("UPDATE tasks SET state='failed', reason=?, reviewer='hub-fleet-recovery', updated_at=CURRENT_TIMESTAMP WHERE id=?", ('hub-fleet: legacy native finished ' + sid, task_id))
                        settled.append({'task_id': task_id, 'native_id': sid, 'state': 'failed'})
                    except (ValueError, TypeError, KeyError):
                        continue
    except (ImportError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return []
    return settled
