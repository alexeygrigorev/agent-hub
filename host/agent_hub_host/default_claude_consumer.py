"""Opt-in, ONE-WAY central default-Claude consumer.

Hub renews the ``hub-claude`` login centrally and the Hub2 daemon passively
observes each generation. This module lets an operator opt the host's *native*
Claude login (``<store>/.credentials.json``) into following those generations.

Safety model, in short:

* Explicit operator enrollment only; absence of the enrollment file is a no-op.
  Enrollment pins the supported native store protocol, the exact effective
  store path (``CLAUDE_SECURESTORAGE_CONFIG_DIR`` before ordinary config
  resolution, resolved once here and never again from the runtime
  environment), the account/label/revision and a consumer identity, and it
  requires the local ``claudeAiOauth`` subdocument to equal the approved
  central payload (not expiry, not the whole document). It starts in OBSERVE
  mode, which never writes and never claims delivery.
* One-way. This code never uploads, refreshes, logs in or calls a provider; a
  changed local login is never reconciled back to Hub. Refresh versus account
  switch cannot be told apart from changed bytes or expiry, so any local OAuth
  content that is neither the incoming central content nor content this
  consumer already knew (last committed, pending source or pending target) is
  ``diverged``: no write, no network.
* Under the native refresh-before-storage locks (``native_claude_store``) the
  document is re-read and ONLY the ``claudeAiOauth`` entry is replaced; every
  unrelated entry (MCP credentials, ...) is preserved. A digest-only pending
  intent is made durable before the file replacement and a committed
  generation afterwards, so a crash in between reconciles forward from the
  exact pending identity and never rolls newer content back. Older and
  equal-but-different generations are refused.
* Honest limits: it coordinates only with writers that honour the native
  locks; it does not coordinate with direct file writers or the legacy
  ``claude-cred-sync`` copier. WRITE mode must only be enabled after a
  separately verified writer handoff (the legacy copier is stopped by its
  owner; this module never controls services). Running-client recovery is not
  claimed here.
* Native executable: enrollment also binds the resolved public ``claude``
  executable (real path, identity) and its self-reported version, which must be
  a supported one; both are revalidated before every delivery and a missing,
  replaced, untrusted or unsupported executable refuses THIS consumer only (never
  a Hub start). Re-enroll after an upgrade. A race between that check and the
  write is not claimed away.
* No automatic hard-crash lock recovery: a lock left by a killed run is reported
  as ``lock_busy`` / ``lock_stale_operator_recovery`` and removed by the operator.
* ``disable`` fsyncs the managed directory and fails honestly if it cannot.

Root handoff (bounded, nothing here activates anything): enroll (observe) ->
``check`` -> keep observe-only while the legacy copier is active -> only after
its owner separately verifies it stopped, ``enable-writes
--confirm-writer-handoff`` -> watch deliveries. ``disable`` returns to the
untouched native file; the legacy script and units are never modified.

Everything this module returns, prints or stores is digests and fixed reason
codes: never tokens, token lengths, paths of the credential or exception text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid

from . import native_claude_store as native, private_fs, provisioning

LABEL = 'hub-claude'
ACCOUNT = 'claude'
CREDENTIAL_PATH = '.claude/.credentials.json'
STATE_NAME = 'default-claude-consumer.json'
MAX_PAYLOAD = 16000
PROVISIONING_WAIT_S = 15
NATIVE_WAIT_S = 10.0
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
CONSUMER_ID = re.compile(r'[0-9a-f]{32}\Z')
REVISION = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
HELD = ('in_step', 'materialized')
MODES = ('observe', 'write')

# Crash-window hook for tests: called with a window name; may raise/exit.
FAULT = None


class Refused(Exception):
    """A fixed, non-sensitive refusal reason."""


def _fault(point):
    if FAULT is not None:
        FAULT(point)


def state_path():
    return provisioning.root() / STATE_NAME


# ----------------------------------------------------------------- state

def _generation(value):
    return type(value) is int and 1 <= value <= 2**53 - 1


def _valid_executable(pin):
    return (isinstance(pin, dict) and set(pin) == {'path', 'dev', 'ino', 'size', 'mtime_ns', 'version'}
            and isinstance(pin['path'], str) and os.path.isabs(pin['path'])
            and all(type(pin[k]) is int for k in ('dev', 'ino', 'size', 'mtime_ns'))
            and isinstance(pin['version'], str) and native.VERSION_RE.fullmatch(pin['version']))


def _valid(state):
    keys = {'version', 'protocol', 'label', 'account', 'consumer_id', 'store', 'store_dev', 'store_ino',
            'revision', 'mode', 'enrolled_generation', 'committed', 'pending', 'executable'}
    if not isinstance(state, dict) or set(state) != keys or state['version'] != 1:
        return False
    committed, pending = state['committed'], state['pending']
    return (_valid_executable(state['executable']) and isinstance(state['protocol'], str) and state['label'] == LABEL and state['account'] == ACCOUNT
            and isinstance(state['consumer_id'], str) and CONSUMER_ID.fullmatch(state['consumer_id'])
            and isinstance(state['store'], str) and os.path.isabs(state['store'])
            and type(state['store_dev']) is int and type(state['store_ino']) is int
            and isinstance(state['revision'], str) and REVISION.fullmatch(state['revision'])
            and state['mode'] in MODES and _generation(state['enrolled_generation'])
            and isinstance(committed, dict) and set(committed) == {'generation', 'oauth_digest'}
            and _generation(committed['generation']) and isinstance(committed['oauth_digest'], str)
            and DIGEST.fullmatch(committed['oauth_digest'])
            and (pending is None or (isinstance(pending, dict)
                                     and set(pending) == {'generation', 'from_digest', 'to_digest'}
                                     and _generation(pending['generation'])
                                     and all(isinstance(pending[k], str) and DIGEST.fullmatch(pending[k])
                                             for k in ('from_digest', 'to_digest')))))


def load_state():
    """The enrollment, or None when this host never opted in. A present but
    unreadable/foreign/malformed file is a refusal, never silently 'absent'."""
    try:
        raw = private_fs.read_bytes(state_path(), 65536)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise Refused('enrollment_unreadable') from None
    try:
        state = native.loads_strict(raw)
    except ValueError:
        raise Refused('enrollment_invalid') from None
    if not _valid(state):
        raise Refused('enrollment_invalid')
    return state


def save_state(state):
    if not _valid(state):
        raise Refused('enrollment_invalid')
    private_fs.atomic_write(state_path(), json.dumps(state, sort_keys=True).encode(), 65536)


def current_set(managed_present):
    """Consumer-set identity from the current enrollment (lock-free atomic read).
    An unreadable enrollment yields its own identity so an ACK cannot cover it."""
    try:
        return consumer_set(managed_present, load_state())
    except Refused:
        return 'enrollment_unreadable'


def consumer_set(managed_present, state):
    """Identity of the consumers an ACK must cover. Observe mode is not an
    enabled delivery consumer, so it neither blocks nor widens an ACK."""
    default = None
    if state is not None and state['mode'] == 'write':
        default = {'id': state['consumer_id'], 'store': state['store'], 'protocol': state['protocol']}
    blob = json.dumps({'managed': bool(managed_present), 'default': default}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


# -------------------------------------------------------------- payloads

def payload_oauth(request):
    """Validated incoming (generation, revision, oauth subdocument, digest)."""
    try:
        files = request['files']
        raw = files[CREDENTIAL_PATH]
        document = native.loads_strict(raw)
        oauth = document['claudeAiOauth']
    except (KeyError, TypeError, ValueError):
        raise Refused('invalid_request') from None
    if (not isinstance(oauth, dict) or not isinstance(oauth.get('accessToken'), str)
            or not oauth['accessToken'].strip() or len(raw.encode('utf8')) > MAX_PAYLOAD):
        raise Refused('invalid_request')
    return oauth, native.oauth_digest(oauth)


def _classify(state, generation, incoming, local):
    """Pure decision. ``local`` is the local oauth digest. Returns one of
    'hold' (already holds the incoming content), 'write' (known prior content),
    'diverged'."""
    if local == incoming:
        return 'hold'
    known = {state['committed']['oauth_digest']}
    if state['pending']:
        known.update((state['pending']['from_digest'], state['pending']['to_digest']))
    return 'write' if local in known else 'diverged'


def _generation_ok(state, generation, incoming):
    committed, pending = state['committed'], state['pending']
    if generation < committed['generation']:
        return False
    if generation == committed['generation'] and incoming != committed['oauth_digest']:
        return False
    if pending is not None:
        if generation < pending['generation']:
            return False
        if generation == pending['generation'] and incoming != pending['to_digest']:
            return False
    return True


def _committed(state, generation, digest):
    return dict(state, committed={'generation': generation, 'oauth_digest': digest}, pending=None)


# --------------------------------------------------------------- delivery

def deliver(request, *, check_only=False):
    """Deliver one approved generation. Returns a fixed-reason dict:
    {'status': 'absent'} (not enrolled), or {'status': <code>, 'enabled': bool,
    } where ``HELD`` codes mean the native store durably
    holds exactly this generation. Never raises with sensitive text."""
    try:
        with provisioning.lock(timeout=PROVISIONING_WAIT_S, bounded=True):
            state = load_state()
            if state is None:
                return {'status': 'absent'}
            status = _deliver(state, request, check_only)
    except Refused as refused:
        return {'status': str(refused), 'enabled': True}
    except TimeoutError:
        return {'status': 'lock_timeout', 'enabled': True}
    except (OSError, ValueError, TypeError, KeyError):
        return {'status': 'delivery_failed', 'enabled': True}
    return {'status': status, 'enabled': state['mode'] == 'write'}


def _deliver(state, request, check_only):
    if sys.platform != 'linux':
        return 'platform_unsupported'
    if state['protocol'] not in native.SUPPORTED_PROTOCOLS:
        return 'protocol_unsupported'
    try:
        # Blocks this consumer only, never a Hub start; before any store access.
        native.verify_executable(state['executable'])
    except native.StoreRefused as refused:
        return str(refused)
    generation = request['generation']
    if request['revision'] != state['revision']:
        return 'revision_changed'
    oauth, incoming = payload_oauth(request)
    if not _generation_ok(state, generation, incoming):
        return 'generation_refused'
    store = state['store']
    try:
        if native.store_identity(store) != (state['store_dev'], state['store_ino']):
            return 'store_changed'
        if state['mode'] != 'write' or check_only:
            return _observe(state, generation, incoming, store, check_only)
        return _write(state, generation, oauth, incoming, store)
    except native.StoreRefused as refused:
        return str(refused)


def _read_local(store):
    raw = native.read_raw(store)
    if raw is None:
        raise native.StoreRefused('store_missing')
    document, oauth = native.parse_document(raw)
    return raw, document, native.oauth_digest(oauth)


def _observe(state, generation, incoming, store, check_only):
    """Lock-free verdict. Writes nothing and holds nothing."""
    _, _, local = _read_local(store)
    verdict = _classify(state, generation, incoming, local)
    if verdict == 'diverged':
        return 'diverged'
    if state['mode'] != 'write':
        return 'observe_only'
    # Check of a write-mode consumer: held only when the commit is durable too.
    if verdict == 'hold' and state['committed'] == {'generation': generation, 'oauth_digest': incoming} \
            and state['pending'] is None:
        return 'in_step'
    return 'pending'


def _write(state, generation, oauth, incoming, store):
    with native.NativeLocks(store, wait_s=NATIVE_WAIT_S) as locks:
        # Re-read under the locks; nothing decided before this point is trusted.
        raw, document, local = _read_local(store)
        verdict = _classify(state, generation, incoming, local)
        if verdict == 'diverged':
            return 'diverged'
        if verdict == 'hold':
            _fault('held')
            # Success is claimed only while the native locks are still ours.
            if locks.compromised():
                return 'lock_compromised'
            if state['committed'] != {'generation': generation, 'oauth_digest': incoming} or state['pending']:
                save_state(_committed(state, generation, incoming))
            return 'in_step'
        # Durable, digest-only intent BEFORE the replacement.
        intent = dict(state, pending={'generation': generation, 'from_digest': local, 'to_digest': incoming})
        save_state(intent)
        _fault('after_intent')
        if locks.compromised():
            return 'lock_compromised'
        updated = dict(document)
        updated['claudeAiOauth'] = oauth
        native.write_document(store, native.render_document(updated, raw))
        _fault('after_file')
        _, _, written = _read_local(store)
        if written != incoming:
            return 'delivery_failed'
        # A lock lost during or after the replacement never becomes a success or a
        # commit; the pending intent stays and the next run reconciles forward.
        if locks.compromised():
            return 'lock_compromised'
        save_state(_committed(intent, generation, incoming))
        return 'materialized'


# --------------------------------------------------- operator commands

def _view(state):
    return {'enrolled': True, 'mode': state['mode'], 'protocol': state['protocol'],
            'protocol_supported': state['protocol'] in native.SUPPORTED_PROTOCOLS,
            'claude_version': state['executable']['version'], 'claude_executable': state['executable']['path'],
            'label': state['label'], 'account': state['account'], 'revision': state['revision'],
            'consumer_id': state['consumer_id'], 'store': state['store'],
            'committed_generation': state['committed']['generation'],
            'pending_generation': state['pending']['generation'] if state['pending'] else None}


def _executable_state(state):
    try:
        native.verify_executable(state['executable'])
    except native.StoreRefused as refused:
        return str(refused)
    return None


def status():
    """Local, read-only. No network, no credential content."""
    with provisioning.lock(timeout=PROVISIONING_WAIT_S, bounded=True):
        state = load_state()
    if state is None:
        return {'enrolled': False}
    view = _view(state)
    try:
        if sys.platform != 'linux':
            view['local'] = 'platform_unsupported'
        elif state['protocol'] in native.SUPPORTED_PROTOCOLS and (blocked := _executable_state(state)) is not None:
            view['local'] = blocked
        else:
            if native.store_identity(state['store']) != (state['store_dev'], state['store_ino']):
                view['local'] = 'store_changed'
            else:
                _, _, local = _read_local(state['store'])
                known = {state['committed']['oauth_digest']}
                if state['pending']:
                    known.update((state['pending']['from_digest'], state['pending']['to_digest']))
                view['local'] = 'known' if local in known else 'diverged'
    except native.StoreRefused as refused:
        view['local'] = str(refused)
    return view


def _validated_view(view):
    if (not isinstance(view, dict) or view.get('label') != LABEL or view.get('status') != 'current'
            or not _generation(view.get('generation'))
            or not isinstance(view.get('revision'), str) or not REVISION.fullmatch(view['revision'])
            or not isinstance(view.get('files'), dict) or set(view['files']) != {CREDENTIAL_PATH}
            or not isinstance(view['files'][CREDENTIAL_PATH], str)):
        raise Refused('central_payload_invalid')
    return {'label': LABEL, 'generation': view['generation'], 'revision': view['revision'],
            'files': {CREDENTIAL_PATH: view['files'][CREDENTIAL_PATH]}}


def check(fetch):
    """Operator dry run: the verdict a delivery would reach, with zero writes."""
    request = _validated_view(fetch())
    result = deliver(request, check_only=True)
    return dict(result, generation=request['generation'])


def enroll(fetch, *, confirm_same_account, environ=None):
    """Bind this host's native login as an OBSERVE-mode default consumer."""
    if not confirm_same_account:
        raise Refused('confirmation_required')
    if sys.platform != 'linux':
        raise Refused('platform_unsupported')
    request = _validated_view(fetch())
    _, central = payload_oauth(request)
    try:
        store = native.effective_store(environ)
        identity = native.store_identity(store)
        _, _, local = _read_local(store)
    except native.StoreRefused as refused:
        raise Refused(str(refused)) from None
    if local != central:
        raise Refused('local_differs_from_central')
    try:
        executable = native.bind_executable(environ)
    except native.StoreRefused as refused:
        raise Refused(str(refused)) from None
    with provisioning.lock(timeout=PROVISIONING_WAIT_S, bounded=True):
        existing = load_state()
        consumer, mode = uuid.uuid4().hex, 'observe'
        if existing is not None:
            # Re-enrollment (e.g. a new engine revision) only for the same
            # store and only from content this consumer already knows.
            if (existing['store'] != str(store) or identity != (existing['store_dev'], existing['store_ino'])
                    or existing['protocol'] != native.PROTOCOL):
                raise Refused('enrolled_elsewhere')
            if request['generation'] < existing['committed']['generation']:
                raise Refused('generation_refused')
            consumer, mode = existing['consumer_id'], existing['mode']
        state = {'version': 1, 'protocol': native.PROTOCOL, 'label': LABEL, 'account': ACCOUNT,
                 'consumer_id': consumer, 'store': str(store), 'store_dev': identity[0], 'store_ino': identity[1],
                 'revision': request['revision'], 'mode': mode, 'enrolled_generation': request['generation'], 'executable': executable,
                 'committed': {'generation': request['generation'], 'oauth_digest': central}, 'pending': None}
        save_state(state)
    return dict(_view(state), status='enrolled')


def _remove_durably(path):
    """Unlink ``path`` and fsync its directory, so an acknowledged removal cannot
    reappear after a crash. The directory is synced even when the file was already
    absent (an earlier removal may not have reached disk). Returns whether a file
    was removed; a sync failure is a refusal, never a quiet success."""
    path = os.fspath(path)
    try:
        with private_fs.parent(path) as anchor:
            try:
                os.unlink(os.path.basename(path), dir_fd=anchor)
                removed = True
            except FileNotFoundError:
                removed = False
            os.fsync(anchor)
    except OSError:
        raise Refused('disable_not_durable') from None
    return removed


def disable():
    """Remove the enrollment durably. Leaves the credential file, locks and legacy copier alone.
    If the removal cannot be made durable the command fails (``disable_not_durable``) and may
    simply be repeated: until it succeeds a crash could restore the enrollment."""
    with provisioning.lock(timeout=PROVISIONING_WAIT_S, bounded=True):
        removed = _remove_durably(state_path())
    return {'enrolled': False, 'removed': removed}


def set_mode(mode, *, confirm_writer_handoff=False):
    if mode == 'write' and not confirm_writer_handoff:
        raise Refused('writer_handoff_confirmation_required')
    with provisioning.lock(timeout=PROVISIONING_WAIT_S, bounded=True):
        state = load_state()
        if state is None:
            raise Refused('not_enrolled')
        if mode == 'write' and state['protocol'] not in native.SUPPORTED_PROTOCOLS:
            raise Refused('protocol_unsupported')
        save_state(dict(state, mode=mode))
        return _view(dict(state, mode=mode))


def command(args):
    from . import config, default_claude_transport

    def fetch():
        base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or config.load_config().get('base_url')
        # Passive-only binding: one GET, no import/exec/re-pin authority, no POST.
        _, passive = default_claude_transport.bind_passive(base)
        return passive()

    try:
        action = args.default_claude_cmd
        if action == 'status':
            return status()
        if action == 'check':
            return check(fetch)
        if action == 'enroll':
            return enroll(fetch, confirm_same_account=bool(args.confirm_same_account))
        if action == 'disable':
            return disable()
        if action == 'enable-writes':
            return set_mode('write', confirm_writer_handoff=bool(args.confirm_writer_handoff))
        if action == 'observe':
            return set_mode('observe')
    except Refused as refused:
        raise ValueError('default-claude: ' + str(refused)) from None
    raise ValueError('default-claude: unknown command')


def add_commands(commands):
    parser = commands.add_parser(
        'default-claude',
        help='Opt-in ONE-WAY central default Claude login consumer (starts observe-only; never uploads)')
    sub = parser.add_subparsers(dest='default_claude_cmd', required=True)
    enroll_cmd = sub.add_parser(
        'enroll', help='Bind this host native Claude login (exact store path, protocol, account); observe mode only')
    enroll_cmd.add_argument('--confirm-same-account', action='store_true',
                            help='You confirm the local default login and hub-claude are the same account')
    sub.add_parser('status', help='Local read-only enrollment state; no network, no credential content')
    sub.add_parser('check', help='Dry-run verdict for the current central generation; writes nothing')
    sub.add_parser('disable', help='Remove the enrollment; leaves credentials, locks and the legacy copier alone')
    writes = sub.add_parser(
        'enable-writes',
        help='Allow writes. Only after the legacy claude-cred-sync copier is separately verified stopped')
    writes.add_argument('--confirm-writer-handoff', action='store_true',
                        help='You confirm no other writer (legacy copier) owns the native credential file')
    sub.add_parser('observe', help='Return to observe-only (never writes)')
