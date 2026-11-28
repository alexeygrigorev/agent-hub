"""Automatic disk hygiene for Hub hosts: finished-session artifact cleanup & engine retention.

Deletes only rebuildable untracked/ignored artifacts inside worktrees whose Hub
sessions are finished and idle > 24h, plus superseded managed-engine copies/caches
not referenced by any running session. Dry-run is the default; ``--apply`` is explicit.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

ARTIFACT_CATEGORIES: dict[str, str] = {
    '.venv': 'venv',
    'node_modules': 'node_modules',
    'target': 'rust_target',
    'dist': 'build_dist',
    'build': 'build_dist',
    '.next': 'build_dist',
    '__pycache__': 'python_cache',
    '.pytest_cache': 'python_cache',
    '.mypy_cache': 'python_cache',
    '.ruff_cache': 'python_cache',
}

FINISHED_SESSION_STATES = frozenset({
    'completed', 'complete', 'stopped', 'failed', 'archived',
    'retired-finished', 'expired',
})

ACTIVE_SESSION_STATES = frozenset({
    'running', 'queued', 'held', 'offered', 'starting', 'launching',
    'pending_admission', 'claimed', 'admitted', 'aborting', 'deadline_stopping',
})

PROTECTED_FILE_SUFFIXES = ('.db', '.sqlite', '.sqlite3', '-wal', '-shm', '.pem', '.key', '.p12', '.pfx', '.mp4', '.mkv', '.webm', '.wav')
PROTECTED_DIR_NAMES = frozenset({'.git', '.codex', '.claude', '.gemini', '.zcodex', '.opencode', 'recordings'})


def default_denylist(home: Path) -> tuple[Path, ...]:
    return (
        home / 'tmp' / 'agent-reports',
        home / 'git' / 'relay',
        home / 'git' / 'academy',
        home / 'recordings',
        home / '.config' / 'agent-hub' / 'providers',
        home / '.config' / 'agent-hub' / 'v2-journal.sqlite',
    )


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def is_denylisted(path: Path, denylist: Iterable[Path]) -> bool:
    resolved = path.resolve(strict=False)
    for denied in denylist:
        denied_resolved = denied.resolve(strict=False)
        if resolved == denied_resolved or _is_under(resolved, denied_resolved):
            return True
    name_lower = path.name.lower()
    if name_lower.startswith('credentials') or name_lower.startswith('.env'):
        return True
    if any(name_lower.endswith(sfx) for sfx in PROTECTED_FILE_SUFFIXES):
        return True
    return False


def dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        stack = [path]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink():
                                total += entry.stat(follow_symlinks=False).st_size
                            elif entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
    except OSError:
        return 0
    return total


def scan_open_paths(targets: Iterable[Path]) -> tuple[set[Path], bool]:
    """Return (opened_targets, scan_complete). Fail closed if /proc cannot be read."""
    resolved_targets = {t.resolve(strict=False): t for t in targets if t.exists()}
    if not resolved_targets:
        return set(), True
    if os.name != 'posix' or not Path('/proc').is_dir():
        return set(), True
    found: set[Path] = set()
    my_uid = os.getuid()
    is_root = os.geteuid() == 0

    def _check(raw: str) -> None:
        if not raw or not raw.startswith('/'):
            return
        cleaned = os.path.normpath(raw.removesuffix(' (deleted)'))
        cur = Path(cleaned)
        while True:
            if cur in resolved_targets:
                found.add(resolved_targets[cur])
            if cur.parent == cur:
                return
            cur = cur.parent

    try:
        procs = glob.glob('/proc/[0-9]*')
    except OSError:
        return set(), False

    for proc in procs:
        try:
            if not is_root and os.stat(proc).st_uid != my_uid:
                continue
        except OSError:
            continue
        for sub in ('cwd', 'fd/*'):
            for item in glob.glob(os.path.join(proc, sub)):
                try:
                    _check(os.readlink(item))
                except OSError:
                    continue
        try:
            with open(os.path.join(proc, 'maps'), encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    parts = line.rstrip().split(None, 5)
                    if len(parts) == 6:
                        _check(parts[5])
        except OSError:
            continue
    return found, True


def _parse_ts_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return value / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str) and value.strip():
        s = value.strip()
        if s.isdigit():
            v = int(s)
            return v / 1000.0 if v > 1e11 else float(v)
        try:
            dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
            return dt.timestamp()
        except ValueError:
            return None
    return None


def load_hub_sessions(sessions_json: Path | None = None) -> list[dict[str, Any]] | None:
    if sessions_json is not None:
        try:
            data = json.loads(sessions_json.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None
    else:
        try:
            proc = subprocess.run(
                ['hub', 'sessions', 'list', '--json', '--all'],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if proc.returncode != 0:
                return None
            data = json.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return None
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        if data.get('next_cursor') not in (None, ''):
            return None
        items = data.get('items') if 'items' in data else data.get('sessions')
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return None


def _session_workspace(session: dict[str, Any]) -> str | None:
    for key in ('workspace', 'cwd', 'requested_workspace'):
        val = session.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _session_state(session: dict[str, Any]) -> str:
    return str(session.get('state') or session.get('status') or session.get('phase') or '').strip().lower()


def _session_finished_idle_ok(
    worktree: Path,
    sessions: list[dict[str, Any]],
    *,
    now_s: float,
    min_idle_hours: float,
) -> tuple[bool, str]:
    wt_resolved = worktree.resolve(strict=False)
    matching: list[dict[str, Any]] = []
    for s in sessions:
        ws = _session_workspace(s)
        if not ws:
            continue
        ws_resolved = Path(ws).resolve(strict=False)
        if ws_resolved == wt_resolved or _is_under(ws_resolved, wt_resolved) or _is_under(wt_resolved, ws_resolved):
            matching.append(s)
    if not matching:
        return False, 'no_hub_session'
    latest_ts = 0.0
    for s in matching:
        state = _session_state(s)
        if state in ACTIVE_SESSION_STATES or state not in FINISHED_SESSION_STATES:
            return False, f'session_active_or_unknown:{state or "unknown"}'
        ts = _parse_ts_seconds(s.get('updated_at')) or _parse_ts_seconds(s.get('last_activity_at')) or _parse_ts_seconds(s.get('created_at'))
        if ts is None:
            return False, 'session_timestamp_missing'
        latest_ts = max(latest_ts, ts)
    idle_hours = (now_s - latest_ts) / 3600.0
    if idle_hours <= min_idle_hours:
        return False, f'idle_{idle_hours:.1f}h_le_{min_idle_hours:.0f}h'
    return True, f'finished_idle_{idle_hours:.1f}h'


def measure_mount_used_pct(mount: Path) -> float:
    if hasattr(os, 'statvfs'):
        st = os.statvfs(mount)
        frsize = st.f_frsize or st.f_bsize
        used = (st.f_blocks - st.f_bfree) * frsize
        avail = st.f_bavail * frsize
        denom = used + avail
        return (used * 100.0 / denom) if denom > 0 else 0.0
    usage = shutil.disk_usage(mount)
    return (usage.used * 100.0 / usage.total) if usage.total > 0 else 0.0


def _dir_newest_mtime(path: Path, *, max_entries: int = 5000) -> float:
    try:
        latest = path.lstat().st_mtime
    except OSError:
        return time.time()
    seen = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    seen += 1
                    if seen > max_entries:
                        return latest
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if st.st_mtime > latest:
                            latest = st.st_mtime
                        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return latest


def discover_project_dirs(home: Path) -> list[Path]:
    found: dict[Path, Path] = {}
    for base in (home / 'work', home / 'git'):
        if not base.is_dir():
            continue
        try:
            for entry in base.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    found[entry.resolve(strict=False)] = entry
        except OSError:
            continue
    return sorted(found.values())


def is_worktree_safe_to_prune(wt: Path) -> tuple[bool, str]:
    """Return (True, reason) only for clean, linked git worktrees with no unpushed/unmerged work."""
    try:
        inside = subprocess.run(
            ['git', '-C', str(wt), 'rev-parse', '--is-inside-work-tree'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if inside.returncode != 0 or inside.stdout.strip() != 'true':
            return False, 'not_git_worktree'
        git_dir = subprocess.run(
            ['git', '-C', str(wt), 'rev-parse', '--path-format=absolute', '--git-dir'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        common_dir = subprocess.run(
            ['git', '-C', str(wt), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if git_dir.returncode != 0 or common_dir.returncode != 0:
            return False, 'git_dir_unknown'
        if Path(git_dir.stdout.strip()).resolve(strict=False) == Path(common_dir.stdout.strip()).resolve(strict=False):
            return False, 'main_checkout_not_linked_worktree'

        status = subprocess.run(
            ['git', '-C', str(wt), 'status', '--porcelain'],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            return False, 'dirty_worktree'

        # Check if HEAD is merged into origin/main or origin/master
        merged = False
        for ref in ('refs/remotes/origin/main', 'refs/remotes/origin/master', 'refs/heads/main', 'refs/heads/master'):
            chk = subprocess.run(
                ['git', '-C', str(wt), 'merge-base', '--is-ancestor', 'HEAD', ref],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if chk.returncode == 0:
                merged = True
                break
        if not merged:
            # Also check if upstream exists and has 0 unpushed commits
            up = subprocess.run(
                ['git', '-C', str(wt), 'rev-list', '--count', '@{u}..HEAD'],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if up.returncode != 0 or (up.stdout.strip().isdigit() and int(up.stdout.strip()) > 0):
                return False, 'unpushed_or_unmerged_work'
            return False, 'not_merged_to_default_branch'

        return True, 'merged_clean_worktree'
    except (OSError, subprocess.SubprocessError):
        return False, 'git_check_failed'


def _prune_worktree(wt: Path) -> bool:
    try:
        common_res = subprocess.run(
            ['git', '-C', str(wt), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        common_parent = Path(common_res.stdout.strip()).parent if common_res.returncode == 0 else None
        if common_parent and common_parent.is_dir():
            rm = subprocess.run(
                ['git', '-C', str(common_parent), 'worktree', 'remove', '--force', str(wt)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if rm.returncode == 0 and not wt.exists():
                return True
        if _safe_remove_dir(wt):
            if common_parent and common_parent.is_dir():
                subprocess.run(
                    ['git', '-C', str(common_parent), 'worktree', 'prune'],
                    capture_output=True, text=True, timeout=15, check=False,
                )
            return True
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def _parse_docker_size(raw: str) -> int:
    import re
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)', raw, re.I)
    if not m:
        return 0
    unit = m.group(2).upper()
    base = 1024 if unit.endswith('IB') else 1000
    power = {'B': 0, 'KB': 1, 'KIB': 1, 'MB': 2, 'MIB': 2, 'GB': 3, 'GIB': 3, 'TB': 4, 'TIB': 4}[unit]
    return int(float(m.group(1)) * (base ** power))


def plan_and_clean_docker(*, apply: bool, min_age_hours: float = 24.0) -> list[dict[str, Any]]:
    if not shutil.which('docker'):
        return []
    try:
        info = subprocess.run(['docker', 'info'], capture_output=True, text=True, timeout=10, check=False)
        if info.returncode != 0:
            return []
        df = subprocess.run(
            ['docker', 'system', 'df', '--format', '{{.Type}}|{{.Reclaimable}}'],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if df.returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []

    reclaimable_images = 0
    reclaimable_build = 0
    for line in df.stdout.splitlines():
        parts = line.split('|', 1)
        if len(parts) != 2:
            continue
        kind, val = parts[0].strip(), parts[1].strip()
        if kind == 'Images':
            reclaimable_images = _parse_docker_size(val)
        elif kind == 'Build Cache':
            reclaimable_build = _parse_docker_size(val)

    results: list[dict[str, Any]] = []
    until = f'{int(max(1, min_age_hours))}h'
    if reclaimable_images > 0:
        status = 'WOULD_REMOVE'
        if apply:
            pr = subprocess.run(
                ['docker', 'image', 'prune', '-a', '-f', '--filter', f'until={until}'],
                capture_output=True, text=True, timeout=60, check=False,
            )
            status = 'REMOVED' if pr.returncode == 0 else 'REMOVE_FAILED'
        results.append({
            'path': 'docker:unused-images',
            'category': 'docker_images',
            'bytes': reclaimable_images,
            'status': status,
            'reason': f'unused_docker_images_until_{until}',
        })
    if reclaimable_build > 0:
        status = 'WOULD_REMOVE'
        if apply:
            pr = subprocess.run(
                ['docker', 'builder', 'prune', '-f', '--filter', f'until={until}'],
                capture_output=True, text=True, timeout=60, check=False,
            )
            status = 'REMOVED' if pr.returncode == 0 else 'REMOVE_FAILED'
        results.append({
            'path': 'docker:build-cache',
            'category': 'docker_images',
            'bytes': reclaimable_build,
            'status': status,
            'reason': f'docker_build_cache_until_{until}',
        })
    return results


def _dir_newest_mtime_idle_ok(path: Path, *, now_s: float, min_idle_hours: float) -> tuple[bool, str]:
    latest = _dir_newest_mtime(path)
    idle_hours = (now_s - latest) / 3600.0
    if idle_hours <= min_idle_hours:
        return False, f'mtime_idle_{idle_hours:.1f}h_le_{min_idle_hours:.0f}h'
    return True, f'mtime_idle_{idle_hours:.1f}h'


def discover_worktrees(home: Path, extra_roots: Iterable[Path] = ()) -> list[Path]:
    patterns = [
        str(home / 'git' / '*-wt-*'),
        str(home / 'git' / '*' / '.local' / 'hub-worktrees' / '*'),
        str(home / 'work' / '*' / '.local' / 'hub-worktrees' / '*'),
        str(home / 'git' / '*' / '.claude' / 'worktrees' / '*'),
    ]
    found: dict[Path, Path] = {}
    for pat in patterns:
        for raw in glob.glob(pat):
            p = Path(raw)
            if p.is_dir():
                found[p.resolve(strict=False)] = p
    for root in extra_roots:
        if root.is_dir():
            found[root.resolve(strict=False)] = root
    return sorted(found.values())


def _is_git_tracked(worktree: Path, relpath: Path) -> bool:
    """Return True (fail closed) if git tracks anything at relpath or if git fails."""
    try:
        proc = subprocess.run(
            ['git', '-C', str(worktree), 'ls-files', '--error-unmatch', str(relpath)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode == 0:
            return True
        proc2 = subprocess.run(
            ['git', '-C', str(worktree), 'ls-files', '--', str(relpath)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc2.returncode != 0 or proc2.stdout.strip():
            return True
        return False
    except (OSError, subprocess.SubprocessError):
        return True


def find_rebuildable_artifacts(worktree: Path, denylist: Iterable[Path]) -> list[tuple[Path, str]]:
    """Find allowlisted untracked/ignored artifact dirs inside worktree."""
    results: list[tuple[Path, str]] = []
    stack = [worktree]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            name = entry.name
            if name in PROTECTED_DIR_NAMES:
                continue
            p = Path(entry.path)
            if is_denylisted(p, denylist):
                continue
            if name in ARTIFACT_CATEGORIES:
                try:
                    rel = p.relative_to(worktree)
                except ValueError:
                    continue
                if not _is_git_tracked(worktree, rel):
                    results.append((p, ARTIFACT_CATEGORIES[name]))
                continue
            stack.append(p)
    return sorted(results, key=lambda x: str(x[0]))


def plan_managed_engines_retention(
    managed_root: Path,
    sessions: list[dict[str, Any]] | None,
    denylist: Iterable[Path],
) -> list[tuple[Path, str, str]]:
    """Return [(path, category, reason)] for superseded managed-engine entries/caches."""
    if not managed_root.is_dir():
        return []
    candidates: list[tuple[Path, str, str]] = []
    state_file = managed_root / 'state.json'
    active_dirs: set[str] = set()
    if state_file.is_file():
        try:
            entries = json.loads(state_file.read_text(encoding='utf-8'))
            if isinstance(entries, list):
                for item in entries:
                    if isinstance(item, dict) and item.get('label') and item.get('revision'):
                        active_dirs.add(f"{item['label']}-{item['revision']}")
        except (OSError, json.JSONDecodeError):
            # Fail closed on unreadable state.json: only consider cache dirs
            active_dirs = set()

    running_text = json.dumps(sessions or []) if sessions is not None else ''
    if active_dirs:
        for entry in sorted(managed_root.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            name = entry.name
            if name in ('engine-launch', 'npm-cache') or name.startswith('opencode-device-') or name in active_dirs:
                continue
            # Keep if any running session references this directory name
            if sessions is None or (name in running_text):
                continue
            # Only prune versioned/revisioned engine directories (<label>-<uuid/rev>)
            if '-' in name and not is_denylisted(entry, denylist):
                candidates.append((entry, 'managed_engines', 'superseded_managed_engine_unreferenced'))

    # Safe regenerable package caches inside managed-engines/*/home/.cache/{uv,pip} & .npm
    queued_parents = {p.resolve(strict=False) for p, _, _ in candidates}
    for entry in sorted(managed_root.iterdir()):
        if entry.resolve(strict=False) in queued_parents:
            continue
        home_dir = entry / 'home'
        if not home_dir.is_dir():
            continue
        for rel in (Path('.cache/uv'), Path('.cache/pip'), Path('.npm')):
            cache_dir = home_dir / rel
            if cache_dir.is_dir() and not cache_dir.is_symlink() and not is_denylisted(cache_dir, denylist):
                candidates.append((cache_dir, 'managed_engines', f'managed_engine_cache:{rel}'))
    return candidates


def _safe_remove_dir(path: Path) -> bool:
    def _onerror(func: Callable[..., Any], p: str, _exc: Any) -> None:
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    try:
        shutil.rmtree(path, onerror=_onerror)
        return not path.exists()
    except OSError:
        return False


def run_hygiene(
    *,
    apply: bool = False,
    home: Path | None = None,
    state_dir: Path | None = None,
    sessions_json: Path | None = None,
    extra_worktrees: Iterable[Path] = (),
    min_idle_hours: float = 24.0,
    min_used_pct: float | None = None,
    include_docker: bool = True,
    now_s: float | None = None,
    denylist: Iterable[Path] | None = None,
    managed_root: Path | None = None,
    mount_path: Path = Path('/'),
) -> dict[str, Any]:
    home = home or Path.home()
    state_dir = state_dir or (home / '.local' / 'state' / 'hub-disk-hygiene')
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        fallback_base = Path(runtime) if runtime and os.path.isdir(runtime) else Path('/dev/shm')
        state_dir = fallback_base / f'hub-disk-hygiene-{os.getuid()}'
        state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / 'hygiene.log'
    now_s = time.time() if now_s is None else now_s
    deny = tuple(denylist) if denylist is not None else default_denylist(home)
    managed_root = managed_root or (home / '.config' / 'agent-hub' / 'managed-engines')

    df_before = shutil.disk_usage(mount_path)
    used_pct = measure_mount_used_pct(mount_path)
    categories: dict[str, int] = {
        'venv': 0,
        'node_modules': 0,
        'rust_target': 0,
        'build_dist': 0,
        'python_cache': 0,
        'managed_engines': 0,
        'docker_images': 0,
        'stale_worktrees': 0,
    }
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    artifacts_allowed = min_used_pct is None or used_pct >= min_used_pct
    if not artifacts_allowed:
        skipped.append({'path': str(mount_path), 'reason': f'used_{used_pct:.1f}_lt_{min_used_pct:.0f}'})

    sessions = load_hub_sessions(sessions_json)
    worktrees = discover_worktrees(home, extra_worktrees)
    wt_resolved_set = {w.resolve(strict=False) for w in worktrees}
    project_dirs = [p for p in discover_project_dirs(home) if p.resolve(strict=False) not in wt_resolved_set] if artifacts_allowed else []

    engine_candidates = plan_managed_engines_retention(managed_root, sessions, deny) if artifacts_allowed else []
    scan_targets = list(worktrees) + list(project_dirs) + [p for p, _, _ in engine_candidates]
    opened, scan_ok = scan_open_paths(scan_targets)
    opened_resolved = {o.resolve(strict=False) for o in opened}

    if not scan_ok:
        skipped.append({'path': 'ALL', 'reason': 'proc_open_scan_incomplete'})
    else:
        for wt in worktrees:
            wt_resolved = wt.resolve(strict=False)
            if is_denylisted(wt, deny):
                skipped.append({'path': str(wt), 'reason': 'denylisted'})
                continue
            if wt in opened or wt_resolved in opened_resolved:
                skipped.append({'path': str(wt), 'reason': 'live_process_open'})
                continue

            # AC3: Prune closed/merged Hub worktrees with no unpushed work and no active session
            has_active = False
            if sessions is not None:
                for s in sessions:
                    ws = _session_workspace(s)
                    if not ws:
                        continue
                    ws_res = Path(ws).resolve(strict=False)
                    if ws_res == wt_resolved or _is_under(ws_res, wt_resolved) or _is_under(wt_resolved, ws_res):
                        if _session_state(s) in ACTIVE_SESSION_STATES:
                            has_active = True
                            break
            if not has_active:
                prune_ok, prune_reason = is_worktree_safe_to_prune(wt)
                if prune_ok:
                    size = dir_size_bytes(wt)
                    status = 'WOULD_REMOVE'
                    if apply:
                        status = 'REMOVED' if _prune_worktree(wt) else 'REMOVE_FAILED'
                    if status in ('WOULD_REMOVE', 'REMOVED'):
                        categories['stale_worktrees'] += size
                    actions.append({
                        'path': str(wt),
                        'worktree': str(wt),
                        'category': 'stale_worktrees',
                        'bytes': size,
                        'status': status,
                        'reason': prune_reason,
                    })
                    continue

            if not artifacts_allowed:
                continue

            if sessions is not None:
                ok, reason = _session_finished_idle_ok(wt, sessions, now_s=now_s, min_idle_hours=min_idle_hours)
            else:
                ok, reason = _dir_newest_mtime_idle_ok(wt, now_s=now_s, min_idle_hours=min_idle_hours)
            if not ok:
                skipped.append({'path': str(wt), 'reason': reason})
                continue

            for art_path, cat in find_rebuildable_artifacts(wt, deny):
                size = dir_size_bytes(art_path)
                status = 'WOULD_REMOVE'
                if apply:
                    status = 'REMOVED' if _safe_remove_dir(art_path) else 'REMOVE_FAILED'
                if status in ('WOULD_REMOVE', 'REMOVED'):
                    categories[cat] = categories.get(cat, 0) + size
                actions.append({
                    'path': str(art_path),
                    'worktree': str(wt),
                    'category': cat,
                    'bytes': size,
                    'status': status,
                    'reason': reason,
                })

        for proj in project_dirs:
            proj_resolved = proj.resolve(strict=False)
            if is_denylisted(proj, deny):
                skipped.append({'path': str(proj), 'reason': 'denylisted'})
                continue
            if proj in opened or proj_resolved in opened_resolved:
                skipped.append({'path': str(proj), 'reason': 'live_process_open'})
                continue
            for art_path, cat in find_rebuildable_artifacts(proj, deny):
                art_ok, art_reason = _dir_newest_mtime_idle_ok(art_path, now_s=now_s, min_idle_hours=min_idle_hours)
                if not art_ok:
                    skipped.append({'path': str(art_path), 'reason': art_reason})
                    continue
                size = dir_size_bytes(art_path)
                status = 'WOULD_REMOVE'
                if apply:
                    status = 'REMOVED' if _safe_remove_dir(art_path) else 'REMOVE_FAILED'
                if status in ('WOULD_REMOVE', 'REMOVED'):
                    categories[cat] = categories.get(cat, 0) + size
                actions.append({
                    'path': str(art_path),
                    'worktree': str(proj),
                    'category': cat,
                    'bytes': size,
                    'status': status,
                    'reason': art_reason,
                })

        for eng_path, cat, reason in engine_candidates:
            if eng_path in opened or eng_path.resolve(strict=False) in opened_resolved:
                skipped.append({'path': str(eng_path), 'reason': 'live_process_open'})
                continue
            size = dir_size_bytes(eng_path)
            if size == 0:
                continue
            status = 'WOULD_REMOVE'
            if apply:
                status = 'REMOVED' if _safe_remove_dir(eng_path) else 'REMOVE_FAILED'
            if status in ('WOULD_REMOVE', 'REMOVED'):
                categories[cat] = categories.get(cat, 0) + size
            actions.append({
                'path': str(eng_path),
                'category': cat,
                'bytes': size,
                'status': status,
                'reason': reason,
            })

        if include_docker and artifacts_allowed:
            for d_act in plan_and_clean_docker(apply=apply, min_age_hours=min_idle_hours):
                if d_act['status'] in ('WOULD_REMOVE', 'REMOVED'):
                    categories['docker_images'] += d_act['bytes']
                actions.append(d_act)

    total_bytes = sum(categories.values())
    df_after = shutil.disk_usage(mount_path)
    date_str = datetime.datetime.fromtimestamp(now_s, tz=datetime.timezone.utc).strftime('%Y-%m-%d')
    mode_str = 'apply' if apply else 'dry-run'
    cat_gb = {k: round(v / (1024 ** 3), 4) for k, v in categories.items()}
    daily_line = (
        f"DAILY_HYGIENE_SUMMARY date={date_str} mode={mode_str} "
        f"reclaimed_gb={total_bytes / (1024 ** 3):.4f} "
        f"categories={json.dumps(cat_gb, sort_keys=True)}"
    )

    ts = datetime.datetime.fromtimestamp(now_s, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    log_lines = [
        f"{ts} mode={mode_str} df_before_free_gb={df_before.free / (1024 ** 3):.2f} "
        f"df_after_free_gb={df_after.free / (1024 ** 3):.2f} actions={len(actions)} skipped={len(skipped)}",
        *[f"  {a['status']} {a['category']} {a['bytes']}B {a['path']} ({a['reason']})" for a in actions],
        daily_line,
    ]
    try:
        with open(log_path, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(log_lines) + '\n')
    except OSError:
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        fallback_base = Path(runtime) if runtime and os.path.isdir(runtime) else Path('/dev/shm')
        fallback_dir = fallback_base / f'hub-disk-hygiene-{os.getuid()}'
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
            log_path = fallback_dir / 'hygiene.log'
            with open(log_path, 'a', encoding='utf-8') as fh:
                fh.write('\n'.join(log_lines) + '\n')
        except OSError:
            pass

    return {
        'mode': mode_str,
        'total_bytes': total_bytes,
        'total_gb': round(total_bytes / (1024 ** 3), 4),
        'categories_bytes': categories,
        'categories_gb': cat_gb,
        'actions': actions,
        'skipped': skipped,
        'df_before_free_bytes': df_before.free,
        'df_after_free_bytes': df_after.free,
        'daily_line': daily_line,
        'log_path': str(log_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Hub host disk hygiene (dry-run by default)')
    parser.add_argument('--apply', action='store_true', help='Actually delete allowlisted artifacts')
    parser.add_argument('--dry-run', action='store_true', help='Report only (default)')
    parser.add_argument('--sessions-json', type=Path, default=None, help='Path to hub sessions JSON snapshot')
    parser.add_argument('--min-idle-hours', type=float, default=24.0)
    parser.add_argument('--min-used-pct', type=float, default=None, help='Only run cleanup when root usage >= pct')
    parser.add_argument('--no-docker', action='store_true', help='Skip docker image/build-cache pruning')
    parser.add_argument('--json', action='store_true', help='Print JSON result')
    args = parser.parse_args(argv)
    result = run_hygiene(
        apply=bool(args.apply and not args.dry_run),
        sessions_json=args.sessions_json,
        min_idle_hours=args.min_idle_hours,
        min_used_pct=args.min_used_pct,
        include_docker=not args.no_docker,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"mode={result['mode']} total_gb={result['total_gb']} log={result['log_path']}")
        for a in result['actions']:
            print(f"  {a['status']:12s} {a['category']:15s} {a['bytes']:12d}B  {a['path']}")
        print(result['daily_line'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
