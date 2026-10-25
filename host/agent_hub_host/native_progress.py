"""Local transcript metadata only; paths and contents never leave the host.

Bind to a native sidecar, a workload's open rollout, or one unambiguous
Codex session_meta matching cwd and launch time. Never pick the newest log
in a shared workspace. A missing/ambiguous log is not fresh progress.
"""
from __future__ import annotations

import datetime as dt
from contextlib import closing
import itertools
import json
import os
from pathlib import Path
import sqlite3
import time


def codex_roots(row):
    env = row.get('env') or {}
    roots = []
    if isinstance(env, dict) and env.get('CODEX_HOME'):
        roots.append(Path(env['CODEX_HOME']) / 'sessions')
    profile = row.get('profile')
    if isinstance(profile, str) and profile.startswith('hub-'):
        from .provisioning import root, read_json
        label = profile.removeprefix('hub-')
        for entry in read_json(root() / 'state.json', [])[:100]:
            if entry.get('label') == label and entry.get('provider') in ('codex', 'zai'):
                base = root() / (label + '-' + entry['revision']) / 'home'
                roots.extend(base / name / 'sessions' for name in ('.codex', '.zcodex'))
    elif not profile:
        roots.extend(Path.home() / name / 'sessions' for name in ('.codex', '.zcodex'))
    return roots


def matches_launch(path, row):
    try:
        with path.open('rb') as stream:
            meta = json.loads(stream.readline(65537))
        payload = meta.get('payload', {})
        at = dt.datetime.fromisoformat(payload['timestamp'].replace('Z', '+00:00')).timestamp() * 1000
        return (meta.get('type') == 'session_meta' and payload.get('cwd') == (row.get('cwd') or row.get('workspace'))
                and abs(at - row['created_at_ms']) <= 10000)
    except (OSError, ValueError, KeyError, TypeError):
        return False


class NativeTranscriptProgress:
    def __init__(self, roots=codex_roots, proc_root=Path('/proc'), opencode_data=None):
        self.roots, self.proc_root = roots, proc_root
        self.opencode_data = opencode_data or os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share'
        self.paths = {}

    def retain(self, keys):
        self.paths = {key: path for key, path in self.paths.items() if key in keys}

    def __call__(self, row):
        key = (row['id'], row['created_at_ms'])
        path = self.paths.get(key)
        try:
            if row.get('engine') == 'opencode':
                return self.opencode_progress(row)
            if path is None:
                path = self.locate(row)
            if path is not None:
                stat = path.stat()
                if stat.st_size > 0:
                    self.paths[key] = path
                    return int(stat.st_mtime * 1000)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.paths.pop(key, None)
        return None

    def opencode_progress(self, row):
        """Native message events only, never database/global session mtime."""
        env = row.get('env') or {}
        db = Path(env.get('OPENCODE_DB') or Path(self.opencode_data) / 'opencode/opencode.db')
        if not db.is_absolute() or not db.is_file():
            return None
        try:
            with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=.05)) as conn:
                deadline = time.monotonic() + .05
                conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                sessions = conn.execute('SELECT id FROM session_v2 WHERE directory = ? AND parent_id IS NULL AND time_created BETWEEN ? AND ? LIMIT 2',
                    (row.get('cwd') or row.get('workspace'), row['created_at_ms'] - 10000, row['created_at_ms'] + 10000)).fetchall()
                if len(sessions) != 1:
                    return None
                value = conn.execute('SELECT MAX(time_updated) FROM session_message WHERE session_id = ?', (sessions[0][0],)).fetchone()[0]
                return value if type(value) is int and value > 0 else None
        except sqlite3.Error:
            return None

    def locate(self, row):
        # Native sidecars already bind the exact conversation and survive
        # process exit/resume. Reading one does not mutate another session.
        history = row.get('history_path')
        if isinstance(history, str) and Path(history).is_absolute():
            parent = Path(history).parent
            if parent.name == row['id']:
                try:
                    with (parent / 'transcript.json').open('rb') as stream:
                        bound = json.loads(stream.read(65537))
                    path = Path(bound['path'])
                    if path.is_absolute() and path.is_file():
                        return path
                except (OSError, ValueError, KeyError, TypeError):
                    pass
        if row.get('engine') not in ('codex', 'zcodex'):
            return None
        roots = self.roots(row)
        deadline = time.monotonic() + .05
        candidates = set()
        pid = row.get('workload_pid')
        if type(pid) is int and pid > 0:
            try:
                for fd in itertools.islice((self.proc_root / str(pid) / 'fd').iterdir(), 128):
                    if time.monotonic() > deadline:
                        return None
                    try:
                        path = fd.resolve(strict=True)
                        if path.name.startswith('rollout-') and path.suffix == '.jsonl' and matches_launch(path, row):
                            candidates.add(path)
                    except OSError:
                        continue
            except OSError:
                pass
        if candidates:
            return next(iter(candidates)) if len(candidates) == 1 else None
        # Native launch and provider session_meta clocks can straddle UTC
        # midnight. Inspect at most 512 files across those three days.
        count = 0
        launch = dt.datetime.fromtimestamp(row['created_at_ms'] / 1000, dt.timezone.utc)
        for root in roots:
            for offset in (-1, 0, 1):
                day = (launch + dt.timedelta(days=offset)).strftime('%Y/%m/%d')
                for path in (root / day).glob('*.jsonl'):
                    count += 1
                    if count > 512 or time.monotonic() > deadline:
                        return None
                    if matches_launch(path, row):
                        candidates.add(path)
        return next(iter(candidates)) if len(candidates) == 1 else None
