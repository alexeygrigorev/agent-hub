#!/usr/bin/env python3
"""Owned test adapter around the unchanged aplexer process fixture, never real a.

Only metadata actually received in --label is added to delegated status/list.
The startup barrier withholds this adapter's stdout after the original fixture
has started its real workload. No host journal, ACK or finish is manufactured.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

STUB_SHA = '78916de3ce7fc058e33db7b2c30b94d3f6c25125385cb8e16fef1da943081361'
STUB = Path(__file__).resolve().parents[3] / 'hub2/tests/e2e/bin/a'


def load(path):
    raw = path.read_bytes()
    if len(raw) > 262144:
        raise ValueError('fixture bound')
    return json.loads(raw)


def save(path, value):
    raw = json.dumps(value).encode()
    temporary = path.with_name(path.name + '.%d.new' % os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def binding(row):
    return {key: row[key] for key in ('id', 'tag', 'workspace', 'created_at_ms')}


def birth(pid):
    tail = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    if tail[0] == 'Z':
        raise ValueError('fixture process already exited')
    return tail[19]


def decorate(state, row, fault):
    sidecar = state / ('phase24-native-' + row['id'] + '.json')
    if not sidecar.is_file():
        return row
    saved = load(sidecar)
    if saved['binding'] != binding(row):
        raise ValueError('fixture identity changed')
    row = dict(row, labels=dict(saved['labels']))
    if fault == 'foreign_attempt':
        row['labels']['hub.attempt'] = 'foreign-attempt-not-the-received-label'
    if fault == 'no_exit':
        row.pop('exit', None)  # Withhold proof, never fabricate a successful exit.
    return row


def main():
    if hashlib.sha256(STUB.read_bytes()).hexdigest() != STUB_SHA:
        raise ValueError('fixture source changed')
    state = Path(os.environ['APLEXER_STATE_DIR']).resolve(strict=True)
    if load(state / 'phase24-owned.json') != {'state': str(state), 'uid': os.getuid()}:
        raise ValueError('not an owned lifetime fixture')
    control = load(state / 'phase24-control.json')
    if set(control) != {'fault', 'hold_start'} or control['fault'] not in ('none', 'foreign_attempt', 'no_exit'):
        raise ValueError('fixture control')
    if type(control['hold_start']) is not bool:
        raise ValueError('fixture control')
    args = sys.argv[1:]
    if not args:
        raise ValueError('fixture command')
    labels = {}
    if args[0] == 'start':
        header = args[:args.index('--')]
        for index, word in enumerate(header):
            if word == '--label':
                key, value = header[index + 1].split('=', 1)
                if key in labels:
                    raise ValueError('duplicate fixture label')
                labels[key] = value
        if set(labels) != {'hub.managed', 'hub.session', 'hub.attempt'}:
            raise ValueError('missing actual launch labels')
    delegated = list(args)
    if args[0] == 'kill' and control['fault'] == 'no_exit' and '--keep-record' not in delegated:
        delegated.append('--keep-record')  # Actual exit stays observable to cleanup.
    child = None
    failure = None
    try:
        child = subprocess.Popen([sys.executable, str(STUB), *delegated],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, close_fds=True)
        out, err = child.communicate(timeout=20)
    except BaseException as error:
        failure = error
        raise
    finally:
        errors = []
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
            except BaseException as error:
                errors.append(error)
            for name in ('stdout', 'stderr'):
                pipe = getattr(child, name)
                setattr(child, name, None)
                try:
                    if pipe is not None:
                        pipe.close()
                except BaseException as error:
                    errors.append(error)
        if errors and failure is None:
            raise errors[0]
    if len(out) > 262144 or len(err) > 262144:
        raise ValueError('fixture output bound')
    if child.returncode:
        return child.returncode  # No arbitrary stderr or partial response on failure.
    if args[0] == 'start':
        row = json.loads(out)
        recorded = load(state / 'stub-sessions' / (row['id'] + '.json'))
        if (binding(row) != binding(recorded) or row['phase'] != 'running'
                or recorded['phase'] != 'running' or not row.get('workload_pid')):
            raise ValueError('workload did not actually start')
        sidecar = state / ('phase24-native-' + row['id'] + '.json')
        if sidecar.exists():
            raise ValueError('duplicate fixture start')
        processes = {name: {'pid': row[name], 'birth': birth(row[name])}
                     for name in ('worker_pid', 'workload_pid')}
        save(sidecar, {'binding': binding(row), 'labels': labels, 'processes': processes})
        save(state / 'phase24-barrier.json', {'binding': binding(row),
             'processes': processes})
        deadline = time.monotonic() + 25
        while control['hold_start'] and not (state / 'phase24-release').exists():
            if time.monotonic() >= deadline:
                return 1  # Never publish a stale start result after the test barrier.
            time.sleep(0.02)
        out = json.dumps(decorate(state, row, control['fault'])).encode()
    elif args[0] in ('status', 'list'):
        value = json.loads(out)
        value = ([decorate(state, row, control['fault']) for row in value]
                 if isinstance(value, list) else decorate(state, value, control['fault']))
        out = json.dumps(value).encode()
        save(state / 'phase24-observed.json', {'verb': args[0], 'at': time.monotonic()})
    elif args[0] == 'kill':
        saved = load(state / ('phase24-native-' + args[1] + '.json'))
        if saved['binding']['id'] != args[1]:
            raise ValueError('fixture kill identity changed')
        save(state / 'phase24-kill.json', {'native_id': args[1],
             'binding': saved['binding'], 'labels': saved['labels']})
    sys.stdout.buffer.write(out)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit('owned lifetime fixture refused')
