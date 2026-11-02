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
from .log_redaction import redact_log

UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def utf16_clip(text, limit, *, tail=False):
    """Use the Hub's JavaScript string budget without splitting a code point."""
    text = ''.join('\ufffd' if 0xd800 <= ord(c) <= 0xdfff else c for c in text)
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
    if isinstance(out.get('diagnostic'), dict):
        out['diagnostic'] = dict(out['diagnostic'])
        for key, limit in (('message', 300), ('tail', 1024)):
            if isinstance(out['diagnostic'].get(key), str):
                out['diagnostic'][key] = utf16_clip(sanitize(out['diagnostic'][key], 65536), limit)
    return out


def sanitize(text, limit=2048):
    # Redact the entire bounded window before truncating; never clip into a
    # credential. Drop control bytes and bare Bearer values as a backstop.
    if not isinstance(text, str):
        return ''
    text = redact_log(text)
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    return utf16_clip(text, limit, tail=True)


def same_native(record, row):
    # The catalog row carries the engine cwd for `--cwd` launches.
    from .retired_native import launch_workspace_targets
    return (row.get('workspace') in launch_workspace_targets(record)
            and all(row.get(a) == record.get(b) for a, b in (
                ('id', 'native_id'), ('created_at_ms', 'incarnation'), ('tag', 'tag'),
                ('engine', 'engine'))))


def _tail(path):
    """Redact complete bounded output before clipping away credential context."""
    with private_fs.parent(path, private=False) as anchor:
        fd = private_fs._open(anchor, path, os.O_RDONLY, private=False)
        with os.fdopen(fd, 'rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > 65536:
                raise ValueError('terminal history exceeds full-context privacy budget')
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('terminal output grew beyond privacy budget')
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
        for path, source in ((Path(history), 'native_history'),
                             (Path(history).with_name('screen.txt'), 'native_screen')):
            try:
                result['log_tail'] = sanitize(_tail(path), 2048)
                result['log_source'] = source
                break
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


def terminal_evidence(record, tombstone, *, git=subprocess.run, state_dir=None):
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
    # Managed headless output is captured before native can delete history.
    # Read this even after a prior census: it can contain the final output
    # produced between the last sample and a sub-second natural retirement.
    from .headless_output import retained
    capture = retained(record)
    if capture and capture['log_tail']:
        saved = dict(saved, log_tail=capture['log_tail'], log_source='managed_headless_output')
        record['exit_evidence'] = saved
    if not saved.get('log_tail') and isinstance(record.get('runtime_tail'), str):
        saved = dict(saved, log_tail=sanitize(record['runtime_tail']))
    # Fast launches may retire between catalog samples. These fixed, no-follow
    # native paths supply output only; the caller still proves exact retirement.
    if (not saved.get('log_tail') and UUID.fullmatch(str(record.get('native_id', '')))
            and UUID.fullmatch(str(record.get('offer_id', '')))
            and record.get('tag') == 'task-hub-' + record['offer_id']
            and type(record.get('incarnation')) is int and record['incarnation'] > 0
            and isinstance(record.get('workspace'), str) and Path(record['workspace']).is_absolute()
            and isinstance(record.get('engine'), str)):
        try:
            from .retired_native import resolve_aplexer_state_dir
            base = Path(resolve_aplexer_state_dir(state_dir))
            for sub in ('retired-sessions', 'sessions'):
                row = dict(id=record['native_id'], created_at_ms=record['incarnation'],
                           tag=record['tag'], workspace=record['workspace'], engine=record['engine'],
                           exit=native_exit, history_path=str(base / sub / record['native_id'] / 'history.bin'))
                recovered = snapshot_native(record, row)
                if recovered and recovered.get('log_tail'):
                    saved = dict(saved, **recovered)
                    record['exit_evidence'] = saved
                    break
        except (OSError, ValueError):
            pass
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
    workspace = record.get('workspace') or cwd
    if (isinstance(cwd, str) and Path(cwd).is_absolute() and type(start) is int
            and isinstance(workspace, str) and Path(workspace).is_absolute()
            and (Path(workspace) / '.git').exists()
            and Path(cwd).resolve().is_relative_to(Path(workspace).resolve())):
        try:
            since = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).isoformat()
            proc = git(['git', 'log', '-8', '--format=%H', '--since=' + since], cwd=cwd, capture_output=True, text=True, timeout=3, check=True)
            result['observed_commits'] = [s for s in proc.stdout.splitlines() if re.fullmatch('[0-9a-f]{40}', s)][:8]
            proc = git(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=cwd, capture_output=True, text=True, timeout=3, check=True)
            result['dirty_paths'] = [sanitize(s, 256) for s in proc.stdout.splitlines()[:16]]
        except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
            pass
    return result
