#!/usr/bin/env python3
"""Owned non-generative process input endpoint; no provider/tool/Host ACK fake.

All original process verbs delegate to the unchanged lifetime fixture. Only
send is added: a real live workload must read the exact body and produce its
own receipt before this native test endpoint returns success. One input only.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

PHASE_SHA = '0b01cbb95841ba638b2bc6028b2a318019bef54814ffc28f0e2fac5cb862aa15'
PHASE = Path(__file__).with_name('phase24_native_barrier.py')


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 32768:
            raise ValueError('owned fixture read')
        return json.loads(stream.read(32769))


def write_once(path, value):
    # Publish only a fully written record; readers never see partial JSON.
    temporary = path.with_name('.' + path.name + '.%d.new' % os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)  # Existing target refuses.
    finally:
        temporary.unlink()


def birth(pid):
    fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ValueError('owned process exited')
    return fields[19]


def state():
    directory = Path(os.environ['APLEXER_STATE_DIR']).resolve(strict=True)
    if read(directory / 'phase24-owned.json') != {'state': str(directory), 'uid': os.getuid()}:
        raise ValueError('not owned fixture')
    return directory


def workload():
    """An actual child reads one input; its output describes that input only."""
    directory = state()
    native = str(uuid.UUID(os.environ['APLEXER_SESSION_ID']))
    owner = {'native_id': native, 'pid': os.getpid(), 'birth': birth(os.getpid())}
    write_once(directory / ('session-loop-ready-' + native + '.json'), owner)
    until = time.monotonic() + 240
    received = False
    while time.monotonic() < until:
        path = directory / ('session-loop-input-' + native + '.json')
        if not received and path.exists():
            request = read(path)
            if set(request) != {'owner', 'body'} or request['owner'] != owner or not isinstance(request['body'], str):
                raise ValueError('input identity')
            digest = hashlib.sha256(request['body'].encode()).hexdigest()
            # This line is produced by the real child after reading input.
            print('SESSION-LOOP-READ:' + digest, flush=True)
            write_once(directory / ('session-loop-receipt-' + native + '.json'),
                       dict(owner, body_sha256=digest, count=1))
            received = True
        time.sleep(.02)
    raise SystemExit('owned fixture lifetime expired')


def send(args):
    if len(args) != 4 or args[0] != 'send' or args[2:] != ['--stdin', '--enter']:
        raise ValueError('fixed send shape')
    native = str(uuid.UUID(args[1]))
    if native != args[1]:
        raise ValueError('canonical identity')
    directory = state()
    saved = read(directory / ('phase24-native-' + native + '.json'))
    row = read(directory / 'stub-sessions' / (native + '.json'))
    if (saved['binding'] != {k: row[k] for k in saved['binding']}
            or row['id'] != native or row['phase'] != 'running'
            or saved['labels'].get('hub.managed') != 'true'):
        raise ValueError('native binding changed')
    for record in saved['processes'].values():
        if birth(record['pid']) != record['birth']:
            raise ValueError('process incarnation changed')
    target = saved['processes']['workload_pid']
    ready = read(directory / ('session-loop-ready-' + native + '.json'))
    if ready != {'native_id': native, 'pid': target['pid'], 'birth': target['birth']}:
        raise ValueError('workload not ready')
    body = sys.stdin.buffer.read(16385)
    if not body or len(body) > 16384:
        raise ValueError('bounded input')
    text = body.decode('utf-8', 'strict')
    write_once(directory / ('session-loop-input-' + native + '.json'), {'owner': ready, 'body': text})
    receipt = directory / ('session-loop-receipt-' + native + '.json')
    until = time.monotonic() + 5
    while time.monotonic() < until:
        if birth(target['pid']) != target['birth']:
            raise ValueError('workload disappeared')
        if receipt.exists():
            result = read(receipt)
            if result != dict(ready, body_sha256=hashlib.sha256(body).hexdigest(), count=1):
                raise ValueError('workload receipt mismatch')
            print(json.dumps({'native_id': native, 'workload_read': True}))
            return 0
        time.sleep(.02)
    raise ValueError('workload read UNKNOWN')


def main():
    if hashlib.sha256(PHASE.read_bytes()).hexdigest() != PHASE_SHA:
        raise ValueError('lifetime fixture changed')
    if sys.argv[1:2] == ['send']:
        return send(sys.argv[1:])
    os.execv(sys.executable, [sys.executable, str(PHASE), *sys.argv[1:]])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit('owned session input fixture refused')
