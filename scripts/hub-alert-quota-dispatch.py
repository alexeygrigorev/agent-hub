"""Normal same-PID quota-monitor prelude; inert unless invoked with pinned source args.

Connects to source-gate.sock inside the single original reservation deadline
(also capped at 150ms), opens the pinned quota source and client modules,
awaits qualified-run, and executes the selected quota module's main() ONCE
with original primary arguments and exception identity preserved.
"""
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import stat
import sys
import time
import types


class ManagedObserver:
    """Best-effort source claims over inherited channel; never gates primary work."""

    def __init__(self, child):
        self.child = child
        self.dirty = False

    def fail(self):
        self.dirty = True
        if self.child is not None:
            self.child.dirty = True

    def emit(self, phase, **fields):
        try:
            self.child.dirty |= self.dirty
            if not self.child.emit(phase, **fields):
                self.fail()
                return False
            return True
        except BaseException as error:
            self.fail()
            if not isinstance(error, Exception):
                raise
            return False


def acquired(path_str, digest):
    """Open and verify a pinned regular non-writable source file via O_NOFOLLOW."""
    path = Path(path_str)
    if not path.is_absolute() or '..' in path.parts or len(digest) != 64:
        raise RuntimeError('source_unqualified')
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_nlink != 1 or (info.st_mode & 0o222)
                or not 0 < info.st_size <= 262144):
            raise RuntimeError('source_unqualified')
        raw = os.pread(fd, info.st_size + 1, 0)
        if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != digest:
            raise RuntimeError('source_unqualified')
        return raw
    finally:
        os.close(fd)


def recv(channel, end):
    remaining = end - time.monotonic()
    if remaining <= 0 or not select.select([channel], [], [], remaining)[0]:
        raise TimeoutError()
    raw, anc, flags, _addr = channel.recvmsg(2049, 0, socket.MSG_DONTWAIT)
    if not raw or len(raw) > 2048 or anc or (flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)):
        raise RuntimeError('channel_unqualified')
    return raw


def send(channel, raw, end):
    remaining = end - time.monotonic()
    if remaining <= 0 or not select.select([], [channel], [], remaining)[1] or channel.send(raw) != len(raw):
        raise TimeoutError()


def run_once(selected, argv, observer):
    return selected.main(argv, _managed_observer=observer)


def main(argv):
    if len(argv) < 7 or argv[6] != '--':
        raise ValueError('invalid_prelude_args')
    deadline_ns, quota_sha, client_sha, gate_dir, quota_path, client_path = argv[:6]
    primary_args = argv[7:]

    try:
        end = min(int(deadline_ns) / 1e9, time.monotonic() + 0.15)
    except Exception:
        end = time.monotonic()

    channel = None
    context = None
    qualified = False
    if end > time.monotonic():
        try:
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            channel.setblocking(False)
            try:
                channel.connect(str(Path(gate_dir) / 'source-gate.sock'))
            except BlockingIOError:
                pass
            send(channel, b'held-before-quota-source', end)
            command = recv(channel, end)
            if len(command) == 37 and command.startswith(b'open:'):
                candidate = command[5:].decode('ascii')
                if len(candidate) == 32 and all(c in '0123456789abcdef' for c in candidate):
                    context = candidate
        except Exception:
            context = None

    quota_raw = acquired(quota_path, quota_sha)
    helper_raw = None
    if context is not None:
        try:
            helper_raw = acquired(client_path, client_sha)
            send(channel, b'quota-source-opened', end)
            qualified = recv(channel, end) == b'qualified-run'
        except Exception:
            qualified = False

    selected = types.ModuleType('selected_quota_monitor')
    selected.__file__ = quota_path
    exec(compile(quota_raw, quota_path, 'exec'), selected.__dict__)

    observer = None
    if qualified and helper_raw is not None:
        helper = types.ModuleType('selected_quota_client')
        helper.__file__ = client_path
        exec(compile(helper_raw, client_path, 'exec'), helper.__dict__)

        def exchange(_path, request, *, deadline=None):
            stop = min(time.monotonic() + 0.15, deadline) if deadline is not None else time.monotonic() + 0.15
            send(channel, helper.encode(request, helper.FRAME), stop)
            return helper.decode(recv(channel, stop), helper.REPLY)

        helper.exchange = exchange
        observer = ManagedObserver(helper.Client(None, context, None, 'quota_monitor'))

    original = None
    try:
        return run_once(selected, primary_args, observer)
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            if channel is not None:
                channel.close()
        except BaseException as error:
            if observer is not None:
                observer.fail()
            if original is None and not isinstance(error, Exception):
                raise


if __name__ == '__main__':
    if len(sys.argv) == 1:
        print(json.dumps({'status': 'INERT_PRELUDE', 'authority': 'NONE', 'complete': False}))
    else:
        raise SystemExit(main(sys.argv[1:]))
