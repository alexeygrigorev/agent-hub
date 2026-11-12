"""Background delivery of the current approved hub-claude credential generation.

The host observes the one enrolled ``hub-claude`` bundle through the same
machine-scoped sync endpoint and exact acknowledgement that launch admission
uses. It never refreshes, leases, rotates or logs in, touches no other account
or profile, and runs on one bounded daemon worker so heartbeats, STOP and the
control path are never delayed. Diagnostics are sanitized codes only.
"""
import logging
import re
import threading
import time
from pathlib import Path

from . import provider_home, providers
from .client import ApiError

ACCOUNT = 'claude'
MODEL = 'opus'
INTERVAL_SECONDS = 120
BACKOFF_BASE_SECONDS = 15
BACKOFF_MAX_SECONDS = 900
NOT_ENROLLED_SECONDS = 900
REQUEST_TIMEOUT_SECONDS = 20
LIFETIME_SECONDS = 900
KEEP_GENERATIONS = 2
_LOCK = threading.Lock()
_CODE = re.compile(r'[a-z0-9_]{1,64}\Z')


class _Bounded:
    """Give every request of the worker its own short network timeout."""

    def __init__(self, client):
        self._client = client

    def request(self, method, path, body=None):
        return self._client.request(method, path, body, timeout=REQUEST_TIMEOUT_SECONDS)


def _state(bridge):
    with _LOCK:
        state = getattr(bridge, '_credential_delivery', None)
        if state is None:
            state = bridge._credential_delivery = {
                'thread': None, 'stop': threading.Event(), 'next_at': 0.0, 'failures': 0,
                'delivered': None, 'diagnostic': None}
        return state


def delivery_thread(bridge):
    """The in-flight worker, if any (tests join it; production never waits)."""
    return _state(bridge)['thread']


def diagnostic(bridge):
    """Last sanitized outcome code (None after a successful or idle pass)."""
    return _state(bridge)['diagnostic']


def delivered(bridge):
    """(generation, revision) last materialized and acknowledged, or None."""
    return _state(bridge)['delivered']


def stop(bridge):
    _state(bridge)['stop'].set()


def start(bridge):
    """Re-arm after a prior serve() stopped delivery (a new connection epoch)."""
    _state(bridge)['stop'].clear()


def request(bridge, now=None):
    """Start one background observation if due; never blocks. Returns True when started."""
    if 'claude' not in getattr(bridge, 'engines', ()) or not callable(getattr(getattr(bridge, 'client', None), 'request', None)):
        return False
    if getattr(getattr(bridge, 'journal', None), 'directory', None) is None:
        return False
    state = _state(bridge)
    now = time.monotonic() if now is None else now
    with _LOCK:
        running = state['thread']
        if state['stop'].is_set() or (running is not None and running.is_alive()) or now < state['next_at']:
            return False
        thread = state['thread'] = threading.Thread(
            target=_run, args=(bridge, state), name='hub2-credential-delivery', daemon=True)
        thread.start()
    return True


def _code(error):
    if isinstance(error, ApiError):
        return 'api_' + str(error.status) if isinstance(error.status, int) else 'api_unavailable'
    if isinstance(error, (TimeoutError, OSError)):
        return 'network_unavailable'
    message = str(error)
    if isinstance(error, RuntimeError) and _CODE.fullmatch(message):
        return message
    return 'delivery_failed'


def _run(bridge, state):
    log = logging.getLogger(__name__)
    delay = INTERVAL_SECONDS
    try:
        _deliver(bridge, state)
        with _LOCK:
            state['failures'], state['diagnostic'] = 0, None
    except Exception as error:
        code = _code(error)
        with _LOCK:
            state['failures'] += 1
            state['diagnostic'] = code
            delay = (NOT_ENROLLED_SECONDS if isinstance(error, ApiError) and error.status in {403, 404}
                     else min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** min(state['failures'] - 1, 6)))
        # The code only: exception text may be derived from untrusted payloads.
        log.warning('Claude credential delivery unavailable code=%s', code)
    finally:
        try:
            bridge.journal.close_thread()
        except Exception:
            pass
        with _LOCK:
            state['next_at'] = time.monotonic() + delay
            if state['thread'] is threading.current_thread():
                state['thread'] = None


def _deliver(bridge, state):
    client = _Bounded(bridge.client)
    bundle, _latest = providers._claude_sync_bundle(client)
    key = (bundle['credential_generation'], bundle['revision'])
    if state['delivered'] == key:
        return
    if state['stop'].is_set():
        raise RuntimeError('delivery_stopped')
    try:
        engine_binary = provider_home.install_managed_claude(bundle['version'])
    except (OSError, ValueError):
        raise RuntimeError('provider_sdk_install_refused') from None
    root = Path(bridge.journal.directory).resolve() / 'credential-delivery'
    try:
        manifest = provider_home.materialize(
            str(root), ACCOUNT, 'claude', bundle['credential_generation'], bundle['files'], MODEL,
            timeout_seconds=LIFETIME_SECONDS, engine_binary=engine_binary)
        verified = provider_home.verify(manifest)
    except (OSError, ValueError):
        raise RuntimeError('provider_materialization_refused') from None
    if verified.get('generation') != bundle['credential_generation']:
        raise RuntimeError('provider_materialization_refused')
    if state['stop'].is_set():
        raise RuntimeError('delivery_stopped')
    acknowledged = client.request('POST', '/api/engines/sync-report', {
        'state': 'synced',
        'items': [{'label': providers.CLAUDE_ENGINE_LABEL, 'revision': bundle['revision'], 'state': 'installed',
                   'credential_generation': bundle['credential_generation']}],
    })
    if not isinstance(acknowledged, dict) or acknowledged.get('status') != 'recorded':
        raise RuntimeError('provider_generation_ack_unverified')
    with _LOCK:
        state['delivered'] = key
    _prune(root, bundle['credential_generation'])


def _prune(root, current):
    """Drop only this worker's own superseded generation homes."""
    try:
        import shutil
        pattern = re.compile(re.escape(ACCOUNT) + r'-([0-9]+)-[0-9a-f]{12}\Z')
        homes = []
        for child in root.iterdir():
            match = pattern.fullmatch(child.name)
            if match and child.is_dir() and not child.is_symlink():
                homes.append((int(match.group(1)), child))
        for generation, child in sorted(homes, key=lambda item: item[0], reverse=True)[KEEP_GENERATIONS:]:
            if generation < current:
                shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass
