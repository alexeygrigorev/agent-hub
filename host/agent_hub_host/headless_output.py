"""Preserve sanitized output while forwarding a managed headless run.

Exit/death authority stays with native containment and its durable receipt.
This local file supplies output only, never a substitute exit code.
"""
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

from . import private_fs
from .exit_evidence import sanitize

UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
MAX_SOURCE_BYTES = 65536


def identity(env, engine):
    native_id = env.get('APLEXER_SESSION_ID', '')
    tag = env.get('APLEXER_TAG', '')
    workspace = env.get('APLEXER_WORKSPACE', '')
    if not all(isinstance(value, str) for value in (native_id, tag, workspace, engine)):
        return None
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', engine):
        return None
    launch = re.fullmatch('task-hub-(' + UUID + ')', tag)
    if not launch or not re.fullmatch(UUID, native_id) or not Path(workspace).is_absolute():
        return None
    return {'native_id': native_id, 'launch_id': launch[1], 'tag': tag,
            'workspace': workspace, 'engine': engine}


def run(argv, env, engine):
    """Run only an already admitted headless command; preserve its outcome.

    Stdout and stderr are merged in their pipe write order. Snapshot the
    complete bounded prefix before forwarding any newly observed bytes.
    Filtering the prefix from byte zero retains multiline secret context;
    an over-budget source is explicitly withheld rather than arbitrarily
    seeking into a credential. Raw bytes are never written to a local file.
    """
    exact = identity(env, engine)
    if exact is None:
        raise ValueError('headless output requires exact native Hub identity')
    path = Path(exact['workspace']) / '.local' / 'hub-headless-output' / (exact['launch_id'] + '.json')
    prefix = bytearray()
    over_budget = False
    bytes_observed = 0
    forwarding = True
    child = None
    previous = {}
    pending_signal = None

    def persist(complete=False, incomplete=False):
        payload = {**exact, 'schema_version': 1,
                   'observed_at_ms': int(time.time() * 1000),
                   'bytes_observed': min(bytes_observed, 9007199254740991),
                   'capture_state': 'over_budget' if over_budget else 'children_open' if incomplete else 'complete' if complete else 'in_progress',
                   'log_tail': '' if over_budget else sanitize(prefix.decode('utf-8', 'replace'), 2048)}
        try:
            private_fs.atomic_write(path, json.dumps(payload).encode('utf-8'), 32768)
        except (OSError, ValueError):
            # Capture failure cannot change the admitted engine's outcome.
            pass

    def forward_signal(signum, _frame):
        nonlocal pending_signal
        pending_signal = signum
        if child is not None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, forward_signal)
        persist()
        child = subprocess.Popen(argv, env=env, stdin=None,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if pending_signal is not None:
            child.send_signal(pending_signal)
        drained = False
        exited_at = None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                if child.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at > 2:
                        # A background descendant may retain the pipe. Do not
                        # hide the engine exit behind an unbounded reader;
                        # native containment still owns those descendants.
                        break
                if not selector.select(.1):
                    continue
                data = os.read(child.stdout.fileno(), 4096)
                if not data:
                    drained = True
                    break
                bytes_observed += len(data)
                if not over_budget:
                    if len(prefix) + len(data) > MAX_SOURCE_BYTES:
                        over_budget = True
                        prefix.clear()
                    else:
                        prefix.extend(data)
                persist()
                if forwarding:
                    try:
                        offset = 0
                        while offset < len(data):
                            offset += os.write(sys.stdout.fileno(), data[offset:])
                    except OSError:
                        forwarding = False
        code = child.wait()
        persist(complete=drained, incomplete=not drained)
        return code
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child is not None and child.stdout is not None:
            child.stdout.close()


def exit_like_child(code):
    if code < 0 and os.name != 'nt':
        sig = -code
        if sig not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
    raise SystemExit(code)


def retained(record):
    """Read output only for the caller's already-qualified native identity.

    This never establishes death, changes runtime outcome or releases a lease.
    A killed wrapper can leave a useful in-progress snapshot because each
    snapshot is sanitized and committed before its bytes reach native stdout.
    """
    env = {'APLEXER_SESSION_ID': record.get('native_id'),
           'APLEXER_TAG': record.get('tag'),
           'APLEXER_WORKSPACE': record.get('workspace')}
    exact = identity(env, record.get('engine'))
    start = record.get('incarnation')
    if (exact is None or exact['launch_id'] != record.get('offer_id')
            or type(start) is not int or start <= 0
            or record.get('mode') not in (None, 'headless')):
        return None
    path = Path(exact['workspace']) / '.local' / 'hub-headless-output' / (exact['launch_id'] + '.json')
    try:
        value = json.loads(private_fs.read_bytes(path, 32768))
        if not isinstance(value, dict) or any(value.get(key) != item for key, item in exact.items()):
            return None
        if (type(value.get('schema_version')) is not int or value['schema_version'] != 1
                or value.get('capture_state') not in ('in_progress', 'complete', 'children_open', 'over_budget')
                or type(value.get('observed_at_ms')) is not int
                or not start <= value['observed_at_ms'] <= int(time.time() * 1000) + 60000
                or type(value.get('bytes_observed')) is not int
                or not 0 <= value['bytes_observed'] <= 9007199254740991
                or not isinstance(value.get('log_tail'), str)
                or len(value['log_tail']) > 4096):
            return None
        if ((value['capture_state'] == 'over_budget') != (value['bytes_observed'] > MAX_SOURCE_BYTES)
                or (value['bytes_observed'] == 0 and value['log_tail'])):
            return None
        return {'log_tail': '' if value['capture_state'] == 'over_budget' else sanitize(value['log_tail']),
                'capture_state': value['capture_state'], 'path': str(path)}
    except (OSError, ValueError, TypeError):
        return None
