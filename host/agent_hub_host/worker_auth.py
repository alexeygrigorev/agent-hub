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
import math
import urllib.parse
from . import private_fs

# Both native launch identities use the same private, per-launch profile.
# Interactive engines publish hub-<id>; maintained headless uses task-hub-<id>.
LAUNCH = re.compile(r'(?:task-)?hub-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z')


class WorkerContextError(ValueError):
    """A fixed diagnostic; never include paths, tokens or untrusted input."""
    def __init__(self):
        super().__init__('Worker context unavailable or unsafe; host re-issuance required.')


V2_LAUNCH = re.compile(r'task-hub2-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z')


def is_v2_worker():
    # A broken selector must not fall through to a privileged human/host profile.
    selector = os.environ.get('HUB2_CONFIG', '')
    return (os.environ.get('APLEXER_TAG', '').startswith('task-hub2-')
            or 'hub2-contexts' in Path(selector).parts)


def refuse_v2_profile_io():
    if is_v2_worker():
        raise WorkerContextError()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerContextError()
        result[key] = value
    return result


def _origin(value):
    from .config import canonical_origin
    origin = canonical_origin(value)
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment or '\\' in value):
        raise WorkerContextError()
    return origin


def _v2_context():
    match = V2_LAUNCH.fullmatch(os.environ.get('APLEXER_TAG', ''))
    workspace = os.environ.get('APLEXER_WORKSPACE', '')
    selector = os.environ.get('HUB2_CONFIG', '')
    if (not match or not workspace or not selector or os.environ.get('HUB_CONFIG_FILE')
            or os.environ.get('HUB_WORKER_CONFIG_FILE')):
        raise WorkerContextError()
    root, path = Path(workspace), Path(selector)
    if (not root.is_absolute() or not path.is_absolute()
            or workspace != str(root) or selector != str(path)
            or workspace != os.path.normpath(workspace) or selector != os.path.normpath(selector)
            or len(workspace.encode()) > 4096 or len(selector.encode()) > 4096):
        raise WorkerContextError()
    # No resolving symlinks into an apparently trusted location. The actual read
    # pins the entire no-follow ancestry, private leaf directory and regular file.
    if path.parent != root / '.local' / 'hub2-contexts':
        raise WorkerContextError()
    name = re.fullmatch(re.escape(match[1]) + r'-([1-9][0-9]{0,15})\.json', path.name)
    if not name:
        raise WorkerContextError()
    data = json.loads(private_fs.read_bytes(path, 16384).decode('utf-8'), object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or set(data) != {'server', 'token', 'expires_at', 'worker'}:
        raise WorkerContextError()
    worker = data['worker']
    fields = {'session_id', 'operation_id', 'epoch', 'intake_id', 'project_id', 'machine_id', 'actor'}
    if not isinstance(worker, dict) or set(worker) != fields:
        raise WorkerContextError()
    if (worker['session_id'] != match[1] or worker['actor'] != 'session:' + match[1]
            or type(worker['epoch']) is not int or worker['epoch'] != int(name[1])
            or worker['epoch'] > 9007199254740991):
        raise WorkerContextError()
    for key in ('session_id', 'intake_id'):
        if not NATIVE_ID.fullmatch(str(worker[key])):
            raise WorkerContextError()
    # These are the actual host/server identity bounds, not fabricated UUIDs
    # for project names or operation ids (which may carry a durable suffix).
    for key, pattern in [('operation_id', r'[A-Za-z0-9_.:-]{1,120}'),
                         ('machine_id', r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}'),
                         ('project_id', r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}')]:
        if not isinstance(worker[key], str) or not re.fullmatch(pattern, worker[key]):
            raise WorkerContextError()
    token, expiry = data['token'], data['expires_at']
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{32,256}', token)
            or type(expiry) not in (int, float) or not math.isfinite(expiry)
            or not time.time() < expiry <= time.time() + 86405):
        raise WorkerContextError()
    origin = _origin(data['server'])
    for key in ('HUB_URL', 'HUB_BASE_URL'):
        if os.environ.get(key) and _origin(os.environ[key]) != origin:
            raise WorkerContextError()
    for key in ('HUB_TOKEN', 'HUB_JOB_TOKEN'):
        if os.environ.get(key) and os.environ[key] != token:
            raise WorkerContextError()
    return {'base_url': data['server'].rstrip('/'), 'access_token': token,
            'client_kind': 'agent', '_hub2_worker': worker, '_hub2_expires_at': expiry}


def load_v2_context():
    if not is_v2_worker():
        return None
    try:
        return _v2_context()
    except (OSError, ValueError, TypeError, OverflowError, KeyError, UnicodeError, RecursionError):
        raise WorkerContextError() from None


def validate_v2_request(profile):
    if '_hub2_worker' in profile and load_v2_context() != profile:
        raise WorkerContextError()


def config_path():
    if is_v2_worker():
        load_v2_context()
        return Path(os.environ['HUB2_CONFIG'])
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
        return native_session_path()
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


NATIVE_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')


def native_session_path():
    """Intake-scoped profile of an observed native session (no Hub launch), e.g.
    an aplexer drain brief. The host writes it once an operator-verified intake
    link exists (session_messages.provision_intake_credentials); selected by the
    aplexer session identity, never by env secrets. Returns None when absent so
    unlinked sessions keep the normal profile resolution."""
    native = os.environ.get('APLEXER_SESSION_ID', '').strip().lower()
    workspace = os.environ.get('APLEXER_WORKSPACE')
    if not NATIVE_ID.fullmatch(native) or not workspace or not Path(workspace).is_absolute():
        return None
    path = Path(workspace) / '.local' / 'hub-worker-auth' / (native + '.json')
    return path if path.is_file() else None


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
    if is_v2_worker():
        raise WorkerContextError()
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
