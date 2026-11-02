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


WORKER_CREDENTIAL_CATEGORIES = frozenset({
    'hub_refused_duplicate', 'hub_refused', 'hub_unreachable',
    'invalid_response', 'expired', 'workspace_unwritable', 'unknown',
})


class WorkerCredentialError(RuntimeError):
    """A fixed, safe category for a failed scoped worker credential request."""

    def __init__(self, category):
        if category not in WORKER_CREDENTIAL_CATEGORIES:
            category = 'unknown'
        self.category = category
        super().__init__('Worker Hub credential unavailable (' + category + ')')


_DUPLICATE_HUB_ERRORS = frozenset({
    'already_claimed', 'claim_conflict', 'worker_credential_duplicate',
    'worker_launch_duplicate',
})


def _hub_failure_category(exc):
    """Classify only status and allowlisted error slugs; never inspect body text."""
    status = getattr(exc, 'http_status', None)
    code = next((getattr(exc, name, None) for name in
                 ('hub_error', 'hub_reason', 'error_code', 'error_reason')
                 if isinstance(getattr(exc, name, None), str)), None)
    if status == 409 and code in _DUPLICATE_HUB_ERRORS:
        return 'hub_refused_duplicate'
    if status == 401:
        return 'expired'
    if isinstance(status, int) and status in (408, 425, 429, 500, 502, 503, 504):
        return 'hub_unreachable'
    if isinstance(status, int) and 400 <= status <= 499:
        return 'hub_refused'
    if isinstance(exc, (OSError, TimeoutError)):
        return 'hub_unreachable'
    return 'invalid_response'


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
    try:
        profile = request('POST', '/api/fleet/launches/' + launch_id + '/worker-credential', {})
    except Exception as exc:
        raise WorkerCredentialError(_hub_failure_category(exc)) from None
    if (not isinstance(profile, dict) or profile.get('worker_launch_id') != launch_id
            or profile.get('client_kind') != 'agent'
            or not isinstance(profile.get('access_token'), str) or not profile['access_token'].startswith('hubt_')
            or not isinstance(profile.get('refresh_token'), str) or not profile['refresh_token'].startswith('hubr_')):
        raise WorkerCredentialError('invalid_response')
    expires = profile.get('worker_expires_at')
    if type(expires) is not int:
        raise WorkerCredentialError('invalid_response')
    if expires <= int(time.time() * 1000):
        raise WorkerCredentialError('expired')
    try:
        from .api import check_url
        check_url(profile.get('base_url'))
    except Exception:
        raise WorkerCredentialError('invalid_response') from None
    try:
        private_fs.atomic_write(path, json.dumps(profile).encode(), 32768)
    except OSError:
        raise WorkerCredentialError('workspace_unwritable') from None
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
