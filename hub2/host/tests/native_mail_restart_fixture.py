"""Fake native/API/socket boundaries around the real receiver; no live transports.

Crash core extracted from the independently approved native_mail_cutover_driver.
The physical target is constant, never selected by current message identity.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
from functools import partial
import uuid

from agent_hub_host.agent import Bridge, Journal
from agent_hub_host.client import Client
from agent_hub_host import messages

SESSION = 'fixture-session'
NATIVE = '61ee2e26-87f1-4753-9a0e-83f7ead5e5c5'
TAG = 'task-fixture'
FIRST = 'mail-first'
LATER = 'mail-later'


def mail(identity):
    return {'id': identity, 'epoch': 1, 'recipient': 'session:' + SESSION,
            'session_id': SESSION, 'operation_id': 'delivery-' + identity,
            'body': 'Synthetic native mailbox fixture'}


def append(root, name, value):
    with (root / name).open('a') as stream:
        stream.write(json.dumps(value, sort_keys=True) + '\n')
        stream.flush(); os.fsync(stream.fileno())


def emit(kind, **value):
    print(json.dumps({'kind': kind, **value}), flush=True)


class StopServe(BaseException):
    pass


class NoWorkPool:
    def submit(self, *args):
        raise AssertionError('mail fixture must not launch or run native commands')


class Socket:
    def __init__(self, connection): self.connection, self.buffer = connection, b''
    def __enter__(self):
        emit('connected', connection=self.connection, pid=os.getpid(), thread=threading.get_ident())
        return self
    def __exit__(self, *_): emit('closed', connection=self.connection)
    def send(self, raw): emit('sent', connection=self.connection, frame=json.loads(raw))
    def recv(self, timeout=None):
        end = time.monotonic() + min(timeout or 3, 3)
        while b'\n' not in self.buffer:
            remaining = end - time.monotonic()
            if remaining <= 0 or not select.select([sys.stdin], [], [], remaining)[0]:
                raise TimeoutError()
            chunk = os.read(sys.stdin.fileno(), 16385)
            if not chunk or len(self.buffer) + len(chunk) > 16384: raise StopServe()
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        event = json.loads(line)
        if event == {'fixture': 'disconnect'}: raise ConnectionResetError('synthetic_disconnect')
        if event == {'fixture': 'stop'}: raise StopServe()
        if event.get('type') != 'mailbox.deliver': raise AssertionError('unexpected fixture input')
        return line.decode()



def run(root, mode):
    journal = Journal(root / 'journal.sqlite')
    client = Client('https://fixture.invalid', 'synthetic-not-a-credential', timeout=1)
    physical = {'id': NATIVE, 'tag': TAG, 'workspace': str(root),
                'phase': 'running', 'created_at_ms': 1000}
    def request(method, path, body=None, key=None):
        assert method == 'POST' and path.startswith('/api/messages/')
        identity = path.split('/')[3].split('?')[0]
        assert identity in (FIRST, LATER) and body['epoch'] == 1
        append(root, 'api.jsonl', {'id': identity, 'phase': body['phase']})
        if body['phase'] == 'claim': return {'claim_id': 'claim-' + identity, 'epoch': 1}
        assert body['phase'] == 'receipt' and identity == LATER, 'uncertain first must never be ACKed'
        emit('receipt_posted', id=identity, native_receipt=body['native_receipt'])
        return {}  # Only the later fake transport receipt; no first-row repair.
    client.request = request
    def fake_native(arguments, **options):
        if arguments[1:2] == ['status']:
            assert arguments[2] == NATIVE
            append(root, 'status.jsonl', physical)
            return subprocess.CompletedProcess(arguments, 0, json.dumps(physical), '')
        assert arguments[1:3] == ['message', 'send'] and '--data' in arguments
        assert arguments[arguments.index('--to') + 1] == TAG
        assert '--enter' not in arguments and '--strict' not in arguments
        identity = json.loads(arguments[arguments.index('--data') + 1])['message_id']
        row = journal.db.execute('SELECT state,receipt FROM native_mail WHERE id=?', (identity,)).fetchone()
        assert row['state'] == 'executing' and row['receipt'] is None
        receipt = None if mode == 'crash' else str(uuid.uuid4())
        append(root, 'effects.jsonl', {'id': identity, 'receipt': receipt})
        if mode == 'crash': os._exit(73)
        return subprocess.CompletedProcess(arguments, 0, receipt + '\n', '')
    bridge = Bridge(client, journal, [root], ['shell'], executable=sys.executable,
                    runner=fake_native, launcher_config={'private_provider_homes': False})
    original_deliver = messages.deliver
    messages.deliver = partial(original_deliver, runner=fake_native)
    try:
        if mode == 'crash':
            with journal.db:
                journal.db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state) VALUES(?,?,?,?,?,?,?,?)',
                    ('fixture-launch', SESSION, 1, 'synthetic', str(root), TAG, NATIVE, 'running'))
            bridge.dispatch({'type': 'mailbox.deliver', 'message': mail(FIRST)}, Socket(0), None)
            raise AssertionError('expected own child exit73 after executing/effect commit')
        assert mode == 'receive'
        row = journal.db.execute('SELECT * FROM launches WHERE session_id=?', (SESSION,)).fetchone()
        assert row['native_id'] == NATIVE and row['tag'] == TAG and row['state'] == 'running'
        # Unrelated host probes are not this fixture's scope; no real profiles,
        # inventory, launch pools, heartbeat threads or network are constructed.
        bridge.heartbeat = lambda *_: None
        bridge.reconcile = lambda: None
        bridge.publish_managed_readiness = lambda *_: None
        bridge._launch_pool = bridge._command_pool = NoWorkPool()
        bridge._worker_slots = threading.BoundedSemaphore(1)
        connection = 0
        def socket_factory(_):
            nonlocal connection
            connection += 1
            return Socket(connection)
        client.socket = socket_factory
        try: bridge._serve('fixture-machine', [])
        except StopServe: return 0
    finally:
        messages.deliver = original_deliver
        journal.close_thread()


if __name__ == '__main__':
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2]))
