"""Cooperative handover: only the supervisor can stop its own in-memory child.

The installer never kills a PID or selects a session by tag. A private nonce
addresses the live owner; the owner acknowledges only after native containment
is empty. Unknown/crashed owners retain their evidence and fail closed.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import re
import time
import uuid
from . import private_fs


def _read(path):
    return json.loads(private_fs.read_bytes(path, 16384))


def _write(path, value):
    private_fs.atomic_write(path, json.dumps(value).encode(), 16384)


class ControllerLease:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.generation = uuid.uuid4().hex
        self.stack = ExitStack()

    def __enter__(self):
        try:
            self.stack.enter_context(private_fs.exclusive_lock(self.directory / 'owner.lock'))
            if (self.directory / 'owner.json').exists():
                raise RuntimeError('Previous controller exit is unproven; evidence preserved')
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.close()

    def publish(self, receipt):
        _write(self.directory / 'owner.json', {'version': 1, 'generation': self.generation,
               'receipt': {k: receipt[k] for k in ('id', 'workspace', 'tag', 'engine', 'created_at_ms', 'command')}})

    def stop_requested(self):
        try:
            return _read(self.directory / 'stop.json') == {'generation': self.generation}
        except FileNotFoundError:
            return False

    def confirm_exit(self):
        # Called only by the live owner after validating its exact native
        # receipt and the post-kill Job Object empty witness.
        _write(self.directory / 'stopped.json', {'generation': self.generation, 'containment_empty': True})
        (self.directory / 'owner.json').unlink()


def handover(directory, *, timeout=90):
    directory = Path(directory)
    try:
        owner = _read(directory / 'owner.json')
    except FileNotFoundError:
        return
    generation = owner.get('generation')
    if (owner.get('version') != 1 or not isinstance(generation, str)
            or not re.fullmatch('[a-f0-9]{32}', generation)):
        raise ValueError('Invalid controller ownership record; preserved')
    _write(directory / 'stop.json', {'generation': generation})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            stopped = _read(directory / 'stopped.json')
        except FileNotFoundError:
            stopped = None
        if stopped == {'generation': generation, 'containment_empty': True}:
            # Also require the owner process to release its kernel lock.
            # The task-state gate then waits for PowerShell itself to exit.
            try:
                with private_fs.exclusive_lock(directory / 'owner.lock'):
                    if not (directory / 'owner.json').exists():
                        return
            except OSError:
                pass
        time.sleep(0.25)
    raise RuntimeError('Controller handover unconfirmed; existing controller and evidence preserved')
