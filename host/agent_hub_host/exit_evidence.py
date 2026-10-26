"""Bounded exit evidence for an exact Hub-owned native incarnation.

Evidence never proves retirement. The caller must independently verify native
death before forwarding it. Raw terminal output stays local; only a redacted
snapshot is retained below the launch workspace.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import subprocess
import time

from . import private_fs
from .workclaim import redact_for_bus

UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def utf16_clip(text, limit, *, tail=False):
    """Use the Hub's JavaScript string budget without splitting a code point."""
    chars = []
    used = 0
    for char in reversed(text) if tail else text:
        width = 2 if ord(char) > 0xffff else 1
        if used + width > limit:
            break
        chars.append(char)
        used += width
    return ''.join(reversed(chars) if tail else chars)


def bound_finish_fields(payload):
    """Apply the wire budget after assembling either worker or host reports."""
    out = dict(payload)
    if isinstance(out.get('report'), str):
        out['report'] = utf16_clip(sanitize(out['report'], 65536), 4096)
    if isinstance(out.get('artifacts'), list):
        artifacts = []
        for original in out['artifacts'][:8]:
            if not isinstance(original, dict):
                continue
            item = dict(original)
            for key, limit in (('label', 120), ('name', 120), ('text', 1024)):
                if isinstance(item.get(key), str):
                    item[key] = utf16_clip(sanitize(item[key], 65536), limit)
            artifacts.append(item)
        out['artifacts'] = artifacts
    return out


def sanitize(text, limit=2048):
    # Redact the entire bounded window before truncating; never clip into a
    # credential. Drop control bytes and bare Bearer values as a backstop.
    if not isinstance(text, str):
        return ''
    text = ANSI.sub('', text)
    text = re.sub(r'(?i)\bBearer\s+[^\s\"\x27,;]+', 'Bearer [REDACTED]', text)
    text = redact_for_bus(text)
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    return utf16_clip(text, limit, tail=True)


def same_native(record, row):
    return all(row.get(a) == record.get(b) for a, b in (
        ('id', 'native_id'), ('created_at_ms', 'incarnation'), ('tag', 'tag'),
        ('workspace', 'workspace'), ('engine', 'engine')))


def _tail(path):
    """No-follow, bounded terminal history; discard a partial first line."""
    with private_fs.parent(path, private=False) as anchor:
        fd = private_fs._open(anchor, path, os.O_RDONLY, private=False)
        with os.fdopen(fd, 'rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            offset = max(0, size - 32768)
            stream.seek(offset)
            raw = stream.read(32768)
    if offset:
        raw = raw.partition(b'\n')[2]
    return sanitize(raw.decode('utf-8', 'replace'), 16384)


def snapshot_native(record, row, *, opencode_data=None):
    """Capture before the native catalog retires its history/exit row."""
    if not same_native(record, row) or not UUID.fullmatch(str(record.get('offer_id', ''))):
        return None
    saved = record.get('exit_evidence')
    result = dict(saved) if isinstance(saved, dict) else {}
    info = row.get('exit')
    if isinstance(info, dict):
        for source, target in (('code', 'exit_code'), ('signal', 'exit_signal'), ('exited_at_ms', 'exited_at_ms')):
            if type(info.get(source)) is int:
                result[target] = info[source]
        result['oom_killed'] = info.get('oom_killed') is True
    history = row.get('history_path')
    if isinstance(history, str) and Path(history).is_absolute() and Path(history).parent.name == row['id']:
        try:
            result['log_tail'] = sanitize(_tail(history), 2048)
            result['log_source'] = 'native_history'
        except (OSError, ValueError):
            pass
    if row.get('engine') == 'opencode' and opencode_data:
        outcome = _opencode_outcome(row, opencode_data)
        if outcome:
            result['engine_outcome'] = outcome
    # Retain one bounded sanitized per-session log, rather than a shared
    # opencode.log whose writes cannot be attributed to this launch.
    workspace = Path(record['workspace'])
    if not workspace.is_absolute():
        return None
    path = workspace / '.local' / 'hub-session-logs' / (record['offer_id'] + '.json')
    try:
        private_fs.atomic_write(path, json.dumps(result).encode(), 32768)
    except (OSError, ValueError):
        pass
    return result


def _opencode_outcome(row, data):
    db = Path(data) / 'opencode' / 'opencode.db'
    if not db.is_absolute() or not db.is_file() or db.is_symlink():
        return None
    try:
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=.05)) as conn:
            deadline = time.monotonic() + .05
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            matches = conn.execute('SELECT id, idle_outcome FROM session_v2 WHERE directory = ? AND parent_id IS NULL AND time_created BETWEEN ? AND ? LIMIT 2',
                (row.get('cwd') or row['workspace'], row['created_at_ms'] - 10000, row['created_at_ms'] + 10000)).fetchall()
            if len(matches) == 1 and matches[0][1] in ('succeeded', 'failed', 'cancelled'):
                return matches[0][1]
    except sqlite3.Error:
        pass
    return None


def terminal_evidence(record, tombstone, *, git=subprocess.run):
    """Unknown measurements remain null; partial work is observed, not owned."""
    saved = record.get('exit_evidence') or {}
    native_exit = record.get('runtime_exit') or {}
    # A journal can predate this schema or be malformed. Never let one field
    # crash reconciliation or create a permanently rejected Fleet receipt.
    if not isinstance(saved, dict):
        saved = {}
    if not isinstance(native_exit, dict):
        native_exit = {}

    def integer(value, nonnegative=False):
        return value if (type(value) is int and abs(value) <= 9007199254740991
                         and (not nonnegative or value >= 0)) else None

    # The retirement reader has already qualified this tombstone against the
    # exact UUID/tag/workspace/incarnation. New native versions retain ExitInfo
    # durably, so a short-lived catalog exit row is no longer required.
    durable = tombstone.get('exit')
    if isinstance(durable, dict):
        native_exit = {**native_exit, **durable}
        record['runtime_exit'] = native_exit
    end = saved.get('exited_at_ms') or native_exit.get('exited_at_ms') or tombstone.get('finished_at_ms')
    start = record.get('incarnation')
    code = integer(native_exit.get('code'))
    if code is None:
        code = integer(saved.get('exit_code'))
    signal = integer(native_exit.get('signal'), True)
    if signal is None:
        signal = integer(saved.get('exit_signal'), True)
    outcome = saved.get('engine_outcome')
    result = {'exit_code': code if code is not None else integer(native_exit.get('code')),
              'exit_signal': signal if signal is not None else integer(native_exit.get('signal'), True),
              'wall_time_ms': integer(end - start, True) if type(end) is int and type(start) is int else None,
              'log_tail': sanitize(saved.get('log_tail', '')),
              'engine_outcome': outcome if outcome in ('succeeded', 'failed', 'cancelled') else None,
              'observed_commits': [], 'dirty_paths': []}
    cwd = record.get('target_cwd') or record.get('workspace')
    if isinstance(cwd, str) and Path(cwd).is_absolute() and type(start) is int:
        try:
            since = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).isoformat()
            proc = git(['git', 'log', '-8', '--format=%H', '--since=' + since], cwd=cwd, capture_output=True, text=True, timeout=3, check=True)
            result['observed_commits'] = [s for s in proc.stdout.splitlines() if re.fullmatch('[0-9a-f]{40}', s)][:8]
            proc = git(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=cwd, capture_output=True, text=True, timeout=3, check=True)
            result['dirty_paths'] = [sanitize(s, 256) for s in proc.stdout.splitlines()[:16]]
        except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
            pass
    return result
