"""Background delivery of the current approved hub-claude credential generation.

One bounded daemon worker observes the enrolled ``hub-claude`` generation
through a passive host read (never a refresh, refresh lease, rotation, login or
global sync), hands the material to the maintained managed-engines consumer
through the verified installed release archive (the same trust boundary as
managed readiness), and only then sends the exact sync-report ACK for the
generation that consumer actually holds.

Ownership is per ``serve()`` connection epoch. An epoch object is created once
and only ever stopped; it is never re-armed. Every step that writes, ACKs or
publishes state re-checks, under the module lock, that its epoch is still the
current one, so a late result of an old connection cannot act after a
reconnect. Heartbeats only *request* a pass and never wait; STOP and the
control path never touch this worker. Diagnostics are fixed codes only.
"""
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time

from .client import ApiError

LABEL = 'hub-claude'
CREDENTIAL_PATH = '.claude/.credentials.json'
# Passive, approved-host-only observation: no refresh, lease or material
# change on the Hub side (Root-owned contract, see task-0514).
PASSIVE_PATH = '/api/engines/hub-claude/passive-credential'
GENERATION_PATH = '/api/engines/hub-claude/generation?generation=%d&revision=%s'
ACK_PATH = '/api/engines/sync-report'
INTERVAL_SECONDS = 120
BACKOFF_BASE_SECONDS = 15
BACKOFF_MAX_SECONDS = 900
NOT_ENROLLED_SECONDS = 900
REQUEST_TIMEOUT_SECONDS = 20
HELPER_TIMEOUT_SECONDS = 60
HELPER_OUTPUT_BYTES = 4096
MAX_CREDENTIAL_BYTES = 16000
REVISION = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
_CODE = re.compile(r'[a-z0-9_]{1,64}\Z')
_LOCK = threading.Lock()
# Runs only the verified bundled CLI module; never this daemon's package.
HELPER_BOOT = ('import sys; sys.path.insert(0, sys.argv[1]); '
               'from agent_hub_host.managed_claude_deliver import main; raise SystemExit(main(sys.argv[2:]))')
HELPER_ENV_DROP = ('HUB_CONFIG_FILE', 'HUB2_CONFIG', 'HUB_TOKEN', 'HUB2_TOKEN', 'HUB_JOB_TOKEN', 'HUB_URL',
                   'HUB_BASE_URL', 'APLEXER_TAG', 'APLEXER_WORKSPACE', 'APLEXER_SESSION_ID')


class Refused(RuntimeError):
    """A fixed, non-sensitive outcome code."""


class _Epoch:
    """One serve() connection's ownership. Stopped once, never revived."""
    __slots__ = ('stopped',)

    def __init__(self):
        self.stopped = threading.Event()


def _state(bridge):
    with _LOCK:
        state = getattr(bridge, '_credential_delivery', None)
        if state is None:
            state = bridge._credential_delivery = {
                'epoch': None, 'thread': None, 'child': None, 'next_at': 0.0, 'failures': 0,
                'delivered': None, 'diagnostic': None}
        return state


def delivery_thread(bridge):
    """The in-flight worker, if any (tests join it; production never waits)."""
    return _state(bridge)['thread']


def diagnostic(bridge):
    """Last sanitized outcome code (None after a successful pass)."""
    return _state(bridge)['diagnostic']


def delivered(bridge):
    """(generation, revision) last delivered and acknowledged, or None."""
    return _state(bridge)['delivered']


def start(bridge):
    """Begin a new connection epoch; any earlier epoch stays stopped for good."""
    state = _state(bridge)
    with _LOCK:
        if state['epoch'] is not None:
            state['epoch'].stopped.set()
        state['epoch'] = _Epoch()
        state['next_at'] = 0.0
        child = state['child']
    _terminate(child)


def stop(bridge):
    """End the current epoch without waiting; an in-flight helper is terminated."""
    state = _state(bridge)
    with _LOCK:
        if state['epoch'] is not None:
            state['epoch'].stopped.set()
        state['epoch'] = None
        child = state['child']
    _terminate(child)


def _terminate(child):
    """Kill the helper's own process group (it is a session leader we started)."""
    if child is None:
        return
    try:
        if os.name != 'nt' and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
    except (OSError, ProcessLookupError):
        pass


def _owns(state, epoch):
    """Caller holds _LOCK."""
    return state['epoch'] is epoch and not epoch.stopped.is_set()


def _fence(state, epoch):
    with _LOCK:
        if not _owns(state, epoch):
            raise Refused('delivery_stopped')


def request(bridge, now=None):
    """Start one background pass if due; never blocks. Returns True when started."""
    if 'claude' not in getattr(bridge, 'engines', ()) or not callable(getattr(getattr(bridge, 'client', None), 'request', None)):
        return False
    if not callable(getattr(getattr(bridge, 'launcher', None), 'managed_claude_readiness', None)):
        return False
    state = _state(bridge)
    now = time.monotonic() if now is None else now
    with _LOCK:
        epoch, running = state['epoch'], state['thread']
        # Single in-flight across epochs: an old worker must finish (it is
        # fenced and its helper killed) before a new epoch starts another.
        if epoch is None or epoch.stopped.is_set() or (running is not None and running.is_alive()) or now < state['next_at']:
            return False
        thread = state['thread'] = threading.Thread(
            target=_run, args=(bridge, state, epoch), name='hub2-credential-delivery', daemon=True)
        thread.start()
    return True


def _code(error):
    if isinstance(error, ApiError):
        return 'api_' + str(error.status) if isinstance(error.status, int) else 'api_unavailable'
    if isinstance(error, subprocess.TimeoutExpired):
        return 'consumer_timeout'
    if isinstance(error, (TimeoutError, OSError)):
        return 'network_unavailable'
    message = str(error)
    if isinstance(error, RuntimeError) and _CODE.fullmatch(message):
        return message
    return 'delivery_failed'


def _run(bridge, state, epoch):
    log = logging.getLogger(__name__)
    delay = INTERVAL_SECONDS
    try:
        outcome = _deliver(bridge, state, epoch)
        with _LOCK:
            if _owns(state, epoch):
                state['failures'], state['diagnostic'] = 0, None
                if outcome is not None:
                    state['delivered'] = outcome
    except Exception as error:
        code = _code(error)
        with _LOCK:
            current = _owns(state, epoch)
            if current:
                state['failures'] += 1
                state['diagnostic'] = code
                delay = (NOT_ENROLLED_SECONDS if isinstance(error, ApiError) and error.status in {403, 404}
                         else min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** min(state['failures'] - 1, 6)))
        if current:
            # The code only: exception text may derive from untrusted payloads.
            log.warning('Claude credential delivery unavailable code=%s', code)
    finally:
        try:
            bridge.journal.close_thread()
        except Exception:
            pass
        with _LOCK:
            if _owns(state, epoch):
                state['next_at'] = time.monotonic() + delay
            if state['thread'] is threading.current_thread():
                state['thread'] = None


def _observe(client):
    """Passive current generation; refuses anything but exact, bounded material."""
    view = client.request('GET', PASSIVE_PATH, None, timeout=REQUEST_TIMEOUT_SECONDS)
    if (not isinstance(view, dict) or view.get('label') != LABEL or view.get('status') != 'current'
            or type(view.get('generation')) is not int or not 1 <= view['generation'] <= 2**53 - 1
            or not isinstance(view.get('revision'), str) or not REVISION.fullmatch(view['revision'])
            or not isinstance(view.get('files'), dict) or set(view['files']) != {CREDENTIAL_PATH}
            or not isinstance(view['files'][CREDENTIAL_PATH], str) or not view['files'][CREDENTIAL_PATH]
            or len(view['files'][CREDENTIAL_PATH].encode('utf8')) > MAX_CREDENTIAL_BYTES):
        raise Refused('passive_generation_invalid')
    return view['generation'], view['revision'], view['files']


def _still_current(client, generation, revision):
    observed = client.request('GET', GENERATION_PATH % (generation, revision), None, timeout=REQUEST_TIMEOUT_SECONDS)
    if observed != {'label': LABEL, 'generation': generation, 'revision': revision, 'status': 'current'}:
        raise Refused('generation_changed')


def _environment():
    env = {key: value for key, value in os.environ.items()
           if key not in HELPER_ENV_DROP and not key.startswith(('CLAUDE_', 'ANTHROPIC_', 'AWS_'))}
    return env


def _consume(bridge, state, epoch, archive, generation, revision, files):
    """Materialize into the maintained managed-engines home via the verified
    release helper; returns its fixed status. Killed on epoch stop."""
    python = os.path.join(os.path.dirname(archive), 'bin', 'python')
    payload = json.dumps({'label': LABEL, 'generation': generation, 'revision': revision, 'files': files}).encode()
    with _LOCK:
        if not _owns(state, epoch):
            raise Refused('delivery_stopped')
        child = state['child'] = (getattr(bridge, 'delivery_popen', None) or subprocess.Popen)(
            [python, '-I', '-S', '-c', HELPER_BOOT, archive, 'materialize'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_environment(), close_fds=True, start_new_session=True)
    try:
        try:
            out, _ = child.communicate(payload, timeout=HELPER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate(child)
            child.communicate()
            raise
    finally:
        with _LOCK:
            if state['child'] is child:
                state['child'] = None
    _fence(state, epoch)
    if out is None or len(out) > HELPER_OUTPUT_BYTES:
        raise Refused('consumer_output_invalid')
    try:
        result = json.loads(out)
    except ValueError:
        raise Refused('consumer_output_invalid') from None
    if not isinstance(result, dict):
        raise Refused('consumer_output_invalid')
    if child.returncode != 0:
        reason = result.get('reason')
        raise Refused('consumer_' + reason if isinstance(reason, str) and _CODE.fullmatch(reason) and len(reason) <= 55
                      else 'consumer_refused')
    status = result.get('status')
    if status == 'absent' and set(result) == {'status'}:
        return status
    if (status in ('in_step', 'materialized') and result.get('generation') == generation
            and result.get('revision') == revision and set(result) == {'status', 'generation', 'revision'}):
        return status
    raise Refused('consumer_output_invalid')


def _deliver(bridge, state, epoch):
    _fence(state, epoch)
    readiness = bridge.launcher.managed_claude_readiness()
    if not isinstance(readiness, dict) or not isinstance(readiness.get('archive'), str):
        raise Refused('managed_claude_unavailable')
    client = bridge.client
    generation, revision, files = _observe(client)
    key = (generation, revision)
    with _LOCK:
        if state['delivered'] == key:
            return None
    _fence(state, epoch)
    status = _consume(bridge, state, epoch, readiness['archive'], generation, revision, files)
    if status == 'absent':
        # No enrolled managed-engines consumer on this host: never ACK a copy
        # nothing uses. Truthful diagnostic; retry on the normal interval.
        raise Refused('managed_claude_consumer_absent')
    # The consumer now holds exactly (generation, revision). Re-check Hub, then
    # ACK only while this connection epoch still owns delivery.
    _still_current(client, generation, revision)
    _fence(state, epoch)
    acknowledged = client.request('POST', ACK_PATH, {
        'state': 'synced',
        'items': [{'label': LABEL, 'revision': revision, 'state': 'installed', 'credential_generation': generation}],
    }, timeout=REQUEST_TIMEOUT_SECONDS)
    if not isinstance(acknowledged, dict) or acknowledged.get('status') != 'recorded':
        raise Refused('generation_ack_unverified')
    return key
