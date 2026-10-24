"""Bounded native progress sampling; never upload argv, filenames or process data.

Polling a catalog is not activity. CPU deltas exclude the aplexer worker;
file changes are bounded to the session cwd. Only exact owned headless
incarnations can have orphan children reaped through the native stop oracle.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time


def event_ms(row):
    values = [row.get(k) for k in ('last_activity_ms', 'reported_state_event_ms')]
    return max((v for v in values if type(v) is int and v > 0), default=None)


def process(pid, proc_root=Path('/proc')):
    """Identity + CPU of a non-zombie process; missing and unreadable differ."""
    if type(pid) is not int or pid <= 0 or not proc_root.is_dir():
        return None
    try:
        fields = (proc_root / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            return False
        return {'pid': pid, 'start': int(fields[19]), 'cpu': int(fields[11]) + int(fields[12])}
    except FileNotFoundError:
        return False
    except (OSError, ValueError, IndexError):
        return None


def tree_cpu(pid, proc_root=Path('/proc')):
    """At most 256 processes/50ms; PID identities prevent reuse as progress."""
    pending, seen, total = [pid], set(), {}
    deadline = time.monotonic() + .05
    while pending and len(seen) < 256 and time.monotonic() < deadline:
        current = pending.pop()
        if current in seen or type(current) is not int:
            continue
        seen.add(current)
        p = process(current, proc_root)
        if isinstance(p, dict):
            total[(current, p['start'])] = p['cpu']
            try:
                pending.extend(int(x) for x in (proc_root / str(current) / 'task' / str(current) / 'children').read_text().split())
            except (OSError, ValueError):
                pass
    return total


def leader_in_containment(row, leader, proc_root):
    """A reused PID outside the native containment is unknown, never activity."""
    expected = row.get('workload_cgroup') or row.get('containment_cgroup')
    if not isinstance(leader, dict) or not expected:
        return leader
    if not isinstance(expected, str):
        return None
    expected = expected.removeprefix('/sys/fs/cgroup').rstrip('/')
    if not expected.startswith('/') or expected == '/' or '..' in expected.split('/'):
        return None
    try:
        groups = (proc_root / str(leader['pid']) / 'cgroup').read_text().splitlines()
        paths = [line.split(':', 2)[2] for line in groups if line.count(':') >= 2]
        return leader if any(path == expected or path.startswith(expected + '/') for path in paths) else None
    except OSError:
        return None


def file_signature(cwd):
    """Metadata only, no symlinks; incomplete/unreadable scans stay unknown."""
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return None
    digest, count = hashlib.sha256(), 0
    deadline = time.monotonic() + .05
    try:
        if not Path(cwd).is_dir():
            return None
        for root, dirs, files in os.walk(cwd, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {'.git', '.local', 'node_modules', '.venv', '__pycache__', '.wrangler', 'dist'} and not Path(root, d).is_symlink())
            for name in sorted(files):
                if count >= 2048 or time.monotonic() > deadline:
                    return None
                p = Path(root, name)
                stat = p.lstat()
                if not p.is_symlink():
                    digest.update(str((str(p.relative_to(cwd)), stat.st_mtime_ns, stat.st_size)).encode())
                count += 1
        return digest.hexdigest()
    except OSError:
        return None


def owned_headless(row, records):
    """Journal ownership is required for cleanup, plus the exact native tuple."""
    return next((r for r in records.values() if r.get('mode') == 'headless'
                 and r.get('offer_id') and r.get('task_id') == 'hub-' + r['offer_id']
                 and r.get('tag') == 'task-hub-' + r['offer_id']
                 and r.get('native_id') == row.get('id') and r.get('tag') == row.get('tag')
                 and r.get('workspace') == row.get('workspace') and r.get('engine') == row.get('engine')
                 and r.get('incarnation') == row.get('created_at_ms') and not r.get('settled')), None)


class SessionProgress:
    def __init__(self, *, now_ms=lambda: int(time.time() * 1000), proc_root=Path('/proc'), files=file_signature, stop=None, state_path=None):
        self.now_ms, self.proc_root, self.files, self.stop = now_ms, proc_root, files, stop
        self.state_path = Path(state_path) if state_path else None
        self.samples = {}
        if self.state_path:
            try:
                from . import private_fs
                saved = json.loads(private_fs.read_bytes(self.state_path, 512 * 1024))
                if isinstance(saved, list) and len(saved) <= 500:
                    for r in saved:
                        if (isinstance(r, dict) and isinstance(r.get('id'), str) and len(r['id']) <= 160
                                and type(r.get('incarnation')) is int and type(r.get('last')) is int
                                and r.get('cleanup') in ('none', 'reaped', 'uncertain')
                                and all(type(r.get(k)) is bool for k in ('seen_alive', 'attempted', 'orphaned'))):
                            self.samples[(r['id'], r['incarnation'])] = {k: r[k] for k in ('last', 'seen_alive', 'attempted', 'cleanup', 'orphaned')}
            except (OSError, ValueError, TypeError):
                pass

    def persist(self):
        if self.state_path:
            from . import private_fs
            rows = [dict(id=sid, incarnation=incarnation, **{k: row[k] for k in ('last', 'seen_alive', 'attempted', 'cleanup', 'orphaned')})
                    for (sid, incarnation), row in self.samples.items()]
            private_fs.atomic_write(self.state_path, json.dumps(rows).encode(), 512 * 1024)

    def sample(self, rows, records=None):
        records = records or {}
        now, files, live = self.now_ms(), {}, set()
        budget = time.monotonic() + 4
        cleanup_attempted = False
        result = {}
        for row in rows[:500]:
            sid, incarnation = row.get('id'), row.get('created_at_ms')
            if not sid or type(incarnation) is not int or incarnation <= 0:
                continue
            key = (sid, incarnation)
            live.add(key)
            previous = self.samples.get(key, {})
            event = event_ms(row)
            last = max(previous.get('last', incarnation), event or incarnation)
            leader = leader_in_containment(row, process(row.get('workload_pid'), self.proc_root), self.proc_root)
            terminal = row.get('phase') in ('exited', 'dead', 'pruned', 'forgotten') and row.get('containment_empty') is True
            cpu = tree_cpu(row.get('workload_pid'), self.proc_root) if isinstance(leader, dict) and not terminal and time.monotonic() < budget else {}
            prior_cpu = previous.get('cpu', {})
            # Ignore worker housekeeping and process creation alone. A real
            # workload must consume >=100ms CPU since its preceding sample.
            ticks = max(1, int(os.sysconf('SC_CLK_TCK') / 10)) if hasattr(os, 'sysconf') else 10
            cpu_progress = sum(max(0, value - prior_cpu.get(pid, value)) for pid, value in cpu.items()) >= ticks
            if cpu_progress:
                last = now
            cwd = row.get('cwd') or row.get('workspace')
            if cwd not in files:
                files[cwd] = self.files(cwd) if not terminal and time.monotonic() < budget else None
            signature = files[cwd]
            if previous.get('files') and signature and previous['files'] != signature:
                last = now
            owned = owned_headless(row, records)
            # A direct exec/run launch is also reportable without journal
            # ownership, but it can never be automatically stopped here.
            command = row.get('command') or []
            headless = bool(owned or (row.get('engine') != 'shell' and isinstance(command, list)
                                     and any(x in ('exec', 'run', '-p', '--print') for x in command[:12])))
            alive = isinstance(leader, dict) if leader is not None else None
            orphan = headless and alive is False and row.get('containment_empty') is False
            cleanup = previous.get('cleanup', 'none')
            # Require a live leader observed by this monitor first. Existing
            # sessions discovered already orphaned are reported, never killed.
            if orphan and owned and previous.get('seen_alive') and not previous.get('attempted') and self.stop and not cleanup_attempted:
                cleanup_attempted = True
                previous['attempted'] = True
                # Persist the attempt before any effect; a restart must never
                # blindly repeat an uncertain kill.
                self.samples[key] = dict(previous, last=last, seen_alive=True, attempted=True,
                                         cleanup='uncertain', orphaned=True)
                self.persist()
                try:
                    cleanup = 'reaped' if self.stop(row) else 'uncertain'
                except Exception:
                    cleanup = 'uncertain'
            baseline_cpu = cpu if cpu_progress else {pid: prior_cpu.get(pid, value) for pid, value in cpu.items()}
            self.samples[key] = {'last': last, 'cpu': baseline_cpu, 'files': signature,
                                 'seen_alive': previous.get('seen_alive', False) or (headless and alive is True),
                                 'attempted': previous.get('attempted', False), 'cleanup': cleanup,
                                 'orphaned': orphan or previous.get('orphaned', False)}
            result[sid] = {'last_progress_at': dt.datetime.fromtimestamp(min(last, now) / 1000, dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
                           'headless': headless, 'agent_alive': alive if headless else None,
                           'orphaned': self.samples[key]['orphaned'], 'cleanup': cleanup}
        self.samples = {k: v for k, v in self.samples.items() if k in live}
        self.persist()
        return result


def stop_orphan(executable, row):
    """Use the existing exact native stop; bounded TERM grace + exit oracle."""
    from .native_sessions import NativeTarget, stop_session
    target = NativeTarget(session_id=row['id'], tag=row['tag'], workspace=row['workspace'],
                          created_at_ms=row['created_at_ms'], engine=row['engine'])
    return stop_session(executable, target, grace_ms=1000).state == 'stopped'
