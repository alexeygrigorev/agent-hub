"""Hub-controlled host self-update for checkout-installed hosts (intake a3227276).

Hosts without an inbound channel (Tabo: Windows, no ssh) still need host
releases. The operator pins an exact commit per machine in the Hub
(`hub host-release set --machine ID --sha SHA`); this step, run from the
session host loop, moves the host's own clean `main` checkout to that commit
and asks the controller to restart on the new code.

Safety: the Hub only supplies a SHA. The code comes from the checkout's own
`origin` and must be reachable from `origin/main` and a fast-forward of the
current HEAD. Dirty tracked files, another branch, or a missing checkout
refuse without touching anything. A failed dependency install or import
smoke test restores the previous commit (the tree was clean, so nothing is
lost) and keeps the running controller.
"""
import json
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SHA = re.compile(r'^[0-9a-f]{40}$')
POLL_S = 120
GIT_TIMEOUT_S = 300
PIP_TIMEOUT_S = 900
SMOKE = (
    "from agent_hub_host import model_contract as mc, native_admission as na, session_host, fleet;"
    "assert mc.policy_models() and na.routing_rules();"
    "print('smoke ok')"
)


def repo_root(run=subprocess.run):
    """The git checkout this package was installed from, or None.

    Release trees (Hetzner host-releases) are not checkouts. A package that
    merely sits inside some other repository is not one either: the
    checkout's own host/agent_hub_host must be this package directory.
    """
    package = Path(__file__).resolve().parent
    try:
        code, top = _run(run, ['git', '-C', str(package), 'rev-parse', '--show-toplevel'], package, 30)
    except (OSError, subprocess.SubprocessError):
        return None
    if code != 0 or not top or (Path(top) / 'host' / 'agent_hub_host').resolve() != package:
        return None
    return Path(top)


class UpdateRefused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _run(run, argv, cwd, timeout, env=None, umask=-1):
    result = run(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                 timeout=timeout, env=env, umask=umask)
    return result.returncode, (result.stdout or '').strip()


def _git(run, root, *args, timeout=GIT_TIMEOUT_S):
    return _run(run, ['git', '-C', str(root), *args], root, timeout)


def current_sha(root, run=subprocess.run):
    if not (Path(root) / '.git').exists():
        return None
    code, out = _git(run, root, 'rev-parse', 'HEAD', timeout=30)
    return out.lower() if code == 0 and SHA.match(out.lower()) else None


def release_sha(package=None):
    """Immutable releases carry build provenance rather than a .git directory."""
    root = Path(package or __file__).resolve().parent.parent.parent
    try:
        manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
        sha = manifest.get('release_rev')
        return sha.lower() if isinstance(sha, str) and SHA.fullmatch(sha.lower()) else None
    except (OSError, ValueError, TypeError):
        pass
    # Zipapp and wheel distributions carry the build stamp instead.
    try:
        from . import _build_info
        sha = _build_info.BUILD_COMMIT
        return sha.lower() if isinstance(sha, str) and SHA.fullmatch(sha.lower()) else None
    except (ImportError, AttributeError):
        return None


def ensure_windows_autostart(root, run=subprocess.run):
    """Upgrade the existing user supervisor task; never request elevation."""
    if sys.platform != 'win32':
        return None
    local = os.environ.get('LOCALAPPDATA')
    if not local or root is None:
        return 'wrapper_missing'
    runner = Path(local) / 'agent-hub' / 'run-session-host.ps1'
    script = Path(root) / 'scripts' / 'windows-host-autostart.ps1'
    if not runner.is_file() or not script.is_file():
        return 'wrapper_missing'
    marker = runner.parent / 'autostart-version.sha256'
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    try:
        if marker.read_text(encoding='ascii').strip() == digest:
            return 'configured'
    except (OSError, UnicodeError):
        pass
    try:
        code, _ = _run(run, ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script), '-Runner', str(runner)], root, 90)
        if code:
            return 'registration_failed'
        marker.write_text(digest + '\n', encoding='ascii')
        return 'configured'
    except (OSError, subprocess.SubprocessError):
        return 'registration_failed'


def _install_and_smoke(run, root, python):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    code, _ = _run(run, [python, '-m', 'pip', 'install', '--quiet', '-e', str(root)], root, PIP_TIMEOUT_S, env)
    if code != 0:
        return 'pip_install_failed'
    smoke_env = dict(env, PYTHONPATH=str(Path(root) / 'host'))
    code, out = _run(run, [python, '-c', SMOKE], root, 120, smoke_env)
    if code != 0 or 'smoke ok' not in out:
        return 'smoke_failed'
    return None


def apply(target, root, *, run=subprocess.run, python=None):
    """Move ``root`` to ``target``. Returns (state, error, sha_now)."""
    python = python or sys.executable
    root = Path(root)
    before = current_sha(root, run)
    if before is None:
        return 'unsupported', 'not_a_git_checkout', None
    if before == target:
        return 'current', None, before
    code, branch = _git(run, root, 'branch', '--show-current', timeout=30)
    if code != 0 or branch != 'main':
        return 'refused', 'not_on_main', before
    code, dirty = _git(run, root, 'status', '--porcelain=v1', '--untracked-files=no', timeout=60)
    if code != 0 or dirty:
        return 'refused', 'dirty_checkout', before
    if _git(run, root, 'fetch', '--quiet', 'origin', 'main')[0] != 0:
        return 'refused', 'fetch_failed', before
    if _git(run, root, 'cat-file', '-e', target + '^{commit}', timeout=30)[0] != 0:
        return 'refused', 'unknown_commit', before
    if _git(run, root, 'merge-base', '--is-ancestor', target, 'origin/main', timeout=60)[0] != 0:
        return 'refused', 'not_on_origin_main', before
    if _git(run, root, 'merge-base', '--is-ancestor', before, target, timeout=60)[0] != 0:
        return 'refused', 'not_fast_forward', before
    if _git(run, root, 'merge', '--ff-only', '--quiet', target)[0] != 0:
        return 'refused', 'merge_failed', before
    error = _install_and_smoke(run, root, python)
    if error is None:
        return 'updated', None, target
    # Clean tree before the move, so --keep restores exactly the old commit.
    if _git(run, root, 'reset', '--keep', '--quiet', before)[0] == 0:
        _install_and_smoke(run, root, python)
        return 'failed', error, current_sha(root, run) or before
    return 'failed', error + '_rollback_failed', current_sha(root, run)


def step_once(request, *, root=None, run=subprocess.run, python=None, log=print):
    """One poll: fetch the target, apply it, report. True means restart now."""
    root = Path(root) if root else repo_root(run)
    target = (request('GET', '/api/fleet/routing/host-release') or {}).get('sha')
    target = target.lower() if isinstance(target, str) and SHA.match(target.lower()) else None
    if root is None:
        sha = release_sha()
        if sha and target and sha != target:
            request('POST', '/api/fleet/routing/host-release/report', {'current_sha': sha, 'state': 'updating', 'error': None})
            from .immutable_release import apply as apply_immutable
            state, error, sha = apply_immutable(target, sha, request,
                root=Path(__file__).resolve().parent.parent.parent, run=run, python=python)
        else:
            state, error = ('current', None) if sha else ('unsupported', 'release_provenance_missing')
    elif target is None:
        sha = current_sha(root, run)
        state, error = ('current', None) if sha else ('unsupported', 'not_a_git_checkout')
    else:
        state, error, sha = apply(target, root, run=run, python=python)
    report = {'current_sha': sha, 'state': state, 'error': error}
    autostart = ensure_windows_autostart(root, run)
    if autostart is not None:
        report['autostart_state'] = autostart
    request('POST', '/api/fleet/routing/host-release/report', report)
    log(json.dumps({'event': 'host_release', 'state': state, 'error': error, 'sha': sha}), flush=True)
    return state == 'updated'


def loop(request, stop, *, root=None, interval=POLL_S, clock=time.monotonic, launcher_settings=None):
    """Daemon body: poll until an update lands, then set ``stop`` so the
    controller drains and exits; the supervisor restarts it on the new code."""
    while not stop.is_set():
        started = clock()
        try:
            if step_once(request, root=root):
                stop.set()
                return
        except Exception as exc:
            print(json.dumps({'event': 'host_release_error', 'type': type(exc).__name__}), flush=True)
        try:
            from . import fleet, launcher_update
            launcher_update.step_once(request, launcher_settings() if launcher_settings else fleet.load_settings())
        except Exception as exc:
            print(json.dumps({'event': 'launcher_release_error', 'type': type(exc).__name__}), flush=True)
        stop.wait(max(1.0, interval - (clock() - started)))
