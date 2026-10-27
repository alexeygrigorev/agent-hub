"""Per-launch CLI profile discovery; no secret is placed in the engine env.

APLEXER_TAG is the published native launch identity. The normal Hub CLI finds
one private profile below this repository, independently of managed HOME/XDG
scrubbing. Refresh saves back to this same profile through config.py.
"""
import contextlib
import errno
import json
import os
from pathlib import Path
import re
import time
from . import private_fs

# Both native launch identities use the same private, per-launch profile.
# Interactive engines publish hub-<id>; maintained headless uses task-hub-<id>.
LAUNCH = re.compile(r'(?:task-)?hub-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z')


def config_path():
    # The task-unit backend has no native aplexer identity. Its supported
    # EnvironmentFile transport carries only this non-secret profile selector.
    explicit = os.environ.get('HUB_WORKER_CONFIG_FILE')
    if explicit:
        path = Path(explicit)
        if (not path.is_absolute() or path.parent.name != 'hub-worker-auth'
                or not LAUNCH.fullmatch('hub-' + path.stem) or path.suffix != '.json'):
            raise ValueError('Invalid worker profile selector')
        return path
    match = LAUNCH.fullmatch(os.environ.get('APLEXER_TAG', ''))
    if not match:
        return None
    workspace = os.environ.get('APLEXER_WORKSPACE')
    if workspace:
        root = Path(workspace)
        if not root.is_absolute():
            raise ValueError('Worker workspace must be absolute')
        return root / '.local' / 'hub-worker-auth' / (match[1] + '.json')
    cwd = Path.cwd()
    for root in (cwd, *cwd.parents):
        if (root / '.git').exists():
            return root / '.local' / 'hub-worker-auth' / (match[1] + '.json')
    return None


def provision(workspace, launch_id, request):
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', launch_id):
        raise ValueError('Invalid worker launch identity')
    path = Path(workspace) / '.local' / 'hub-worker-auth' / (launch_id + '.json')
    if not path.is_absolute():
        raise ValueError('Worker workspace must be absolute')
    try:
        saved = json.loads(private_fs.read_bytes(path, 32768))
        if (saved.get('worker_launch_id') == launch_id and saved.get('client_kind') == 'agent'
                and saved.get('worker_expires_at', 0) > int(time.time() * 1000) and saved.get('access_token') and saved.get('refresh_token') and saved.get('base_url')):
            return path
    except (OSError, ValueError, TypeError):
        pass
    profile = request('POST', '/api/fleet/launches/' + launch_id + '/worker-credential', {})
    if (not isinstance(profile, dict) or profile.get('worker_launch_id') != launch_id
            or profile.get('client_kind') != 'agent'
            or not isinstance(profile.get('access_token'), str) or not profile['access_token'].startswith('hubt_')
            or not isinstance(profile.get('refresh_token'), str) or not profile['refresh_token'].startswith('hubr_')):
        raise ValueError('Invalid scoped worker credential response')
    expires = profile.get('worker_expires_at')
    if type(expires) is not int or expires <= int(time.time() * 1000):
        raise ValueError('Invalid scoped worker credential expiry')
    from .api import check_url
    check_url(profile.get('base_url'))
    private_fs.atomic_write(path, json.dumps(profile).encode(), 32768)
    return path


@contextlib.contextmanager
def refresh_guard():
    """Serialize CLI processes sharing one rotating worker refresh token."""
    path = None if os.environ.get('HUB_CONFIG_FILE') else config_path()
    if path is None:
        yield
        return
    manager = None
    deadline = time.monotonic() + 30
    while manager is None:
        candidate = private_fs.exclusive_lock(path.with_suffix('.refresh.lock'))
        try:
            candidate.__enter__()
            manager = candidate
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                raise RuntimeError('Worker credential refresh lock unavailable') from None
            time.sleep(.05)
    try:
        yield
    finally:
        manager.__exit__(None, None, None)
