"""Report and fast-forward the configured launcher, without inbound host access.

Only checkout installations can update. Immutable release directories report
identity but require a separately prepared release. Commands never use a shell
or expose git/pip output (which can include authenticated URLs).
"""
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from . import self_update as git_update

SMOKE = "import launcher, launcher.cli; print('smoke ok')"


def identity(root, run=subprocess.run):
    root = Path(root)
    sha = git_update.current_sha(root, run)
    if sha:
        code, top = git_update._git(run, root, 'rev-parse', '--show-toplevel', timeout=30)
        if code == 0 and Path(top).resolve() == root.resolve():
            return sha, True
    # Prepared immutable releases are named by the full source SHA.
    if git_update.SHA.fullmatch(root.name) and (root / 'launcher').is_dir():
        return root.name, False
    return None, False


def smoke(root, argv, run):
    # Honor the configured Python executable, including python.exe on Windows.
    # Other wrappers cannot safely be assumed to accept Python -c.
    if len(argv) < 3 or argv[-2:] != ['-m', 'launcher']:
        return 'unsupported_launcher_argv'
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    code, git_dir = git_update._git(run, root, 'rev-parse', '--absolute-git-dir')
    if code:
        return 'smoke_failed'
    # Isolate bytecode: a same-second merge/rollback must not leave a cached
    # failed module, nor let old cached modules make the new smoke pass.
    with tempfile.TemporaryDirectory(prefix='hub-launcher-smoke-', dir=git_dir) as cache:
        env['PYTHONPYCACHEPREFIX'] = cache
        code, out = git_update._run(run, argv[:-2] + ['-c', SMOKE], root, 120, env)
    return None if code == 0 and 'smoke ok' in out else 'smoke_failed'


def _apply(target, root, argv, *, run=subprocess.run):
    root = Path(root)
    before, checkout = identity(root, run)
    if not git_update.SHA.fullmatch(target or ''):
        return 'refused', 'invalid_sha', before
    if before == target:
        return 'current', None, before
    if not checkout:
        return 'unsupported', 'not_a_git_checkout', before
    if len(argv) < 3 or argv[-2:] != ['-m', 'launcher']:
        return 'unsupported', 'unsupported_launcher_argv', before
    # Refuse all local changes, including untracked files, before updating.
    # Git also refuses an untracked-file collision; no forced checkout/reset.
    checks = [
        (('branch', '--show-current'), lambda v: v == 'main', 'not_on_main'),
        (('status', '--porcelain=v1', '--untracked-files=all'), lambda v: not v, 'dirty_checkout'),
        (('fetch', '--quiet', 'origin', 'main'), lambda v: True, 'fetch_failed'),
        (('cat-file', '-e', target + '^{commit}'), lambda v: True, 'unknown_commit'),
        (('merge-base', '--is-ancestor', target, 'origin/main'), lambda v: True, 'not_on_origin_main'),
        (('merge-base', '--is-ancestor', before, target), lambda v: True, 'not_fast_forward'),
    ]
    for args, valid, reason in checks:
        code, out = git_update._git(run, root, *args)
        if code or not valid(out):
            return 'refused', reason, before
    # Once merge starts, even a timeout needs a rollback attempt.
    try:
        code, _ = git_update._git(run, root, 'merge', '--ff-only', '--quiet', target)
        error = 'merge_failed' if code else smoke(root, argv, run)
        if error is None:
            return 'updated', None, target
    except (OSError, subprocess.SubprocessError):
        error = 'update_command_failed'
    try:
        if git_update._git(run, root, 'reset', '--keep', '--quiet', before)[0] == 0:
            return 'failed', error, before
    except (OSError, subprocess.SubprocessError):
        pass
    return 'failed', error + '_rollback_failed', git_update.current_sha(root, run)


@contextmanager
def update_lock(path, *, platform=None):
    """A process-death-safe lock on both Windows and Unix; never unlink it."""
    platform = platform or sys.platform
    with open(path, 'a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if platform == 'win32':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if platform == 'win32':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def apply(target, root, argv, *, run=subprocess.run):
    root = Path(root)
    sha, checkout = identity(root, run)
    if not checkout:
        return _apply(target, root, argv, run=run)
    code, lock_path = git_update._git(run, root, 'rev-parse', '--git-path', 'hub-launcher-update.lock')
    if code or not lock_path:
        return 'refused', 'lock_unavailable', sha
    path = Path(lock_path)
    if not path.is_absolute():
        path = root / path
    with update_lock(path) as acquired:
        if not acquired:
            return 'refused', 'update_in_progress', sha
        return _apply(target, root, argv, run=run)


def step_once(request, settings, *, run=subprocess.run):
    launcher = settings.get('launcher') or {}
    root = launcher.get('root')
    target = (request('GET', '/api/fleet/routing/launcher-release') or {}).get('sha')
    sha, state, error = None, 'unsupported', 'launcher_not_configured'
    if root:
        try:
            sha, _ = identity(root, run)
            if target:
                state, error, sha = apply(target, root, launcher.get('argv') or [], run=run)
            else:
                state, error = ('current', None) if sha else ('unsupported', 'unknown_launcher_version')
        except (OSError, subprocess.SubprocessError):
            state, error = 'failed', 'update_command_failed'
    request('POST', '/api/fleet/routing/launcher-release/report',
            {'root': str(root) if root else None, 'current_sha': sha, 'state': state, 'error': error})
    # Only fixed error codes and SHAs, never command output or configuration.
    print(json.dumps({'event': 'launcher_release', 'sha': sha, 'state': state, 'error': error}), flush=True)
