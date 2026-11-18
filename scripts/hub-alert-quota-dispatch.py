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
import struct
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


def _read_verified_fd(fd, digest, *, allow_unlinked=False):
    """Read and verify an already-held regular file descriptor against digest."""
    if type(fd) is not int or fd < 0 or len(digest) != 64:
        raise RuntimeError('source_unqualified')
    info = os.fstat(fd)
    allowed_nlink = (0, 1) if allow_unlinked else (1,)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_nlink not in allowed_nlink or (info.st_mode & 0o222)
            or not 0 < info.st_size <= 262144):
        raise RuntimeError('source_unqualified')
    raw = os.pread(fd, info.st_size + 1, 0)
    if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != digest:
        raise RuntimeError('source_unqualified')
    return raw


def acquired(path_str, digest):
    """Open and verify a pinned regular non-writable source file via O_NOFOLLOW."""
    path = Path(path_str)
    if not path.is_absolute() or '..' in path.parts or len(digest) != 64:
        raise RuntimeError('source_unqualified')
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _read_verified_fd(fd, digest)
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


def recv_open(channel, end):
    """Receive open:<context> with optional single SCM_RIGHTS retained-source FD."""
    remaining = end - time.monotonic()
    if remaining <= 0 or not select.select([channel], [], [], remaining)[0]:
        raise TimeoutError()
    raw, anc, flags, _addr = channel.recvmsg(2049, socket.CMSG_SPACE(4), socket.MSG_DONTWAIT)
    if not raw or len(raw) > 2048 or (flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)):
        for _lvl, ctype, cdata in anc:
            if ctype == socket.SCM_RIGHTS:
                for i in range(0, len(cdata) - 3, 4):
                    try:
                        os.close(struct.unpack_from('i', cdata, i)[0])
                    except OSError:
                        pass
        raise RuntimeError('channel_unqualified')
    if not anc:
        return raw, None
    if len(anc) == 1 and anc[0][0] == socket.SOL_SOCKET and anc[0][1] == socket.SCM_RIGHTS and len(anc[0][2]) == 4:
        return raw, struct.unpack('i', anc[0][2])[0]
    for _lvl, ctype, cdata in anc:
        if ctype == socket.SCM_RIGHTS:
            for i in range(0, len(cdata) - 3, 4):
                try:
                    os.close(struct.unpack_from('i', cdata, i)[0])
                except OSError:
                    pass
    raise RuntimeError('channel_unqualified')


def send(channel, raw, end):
    remaining = end - time.monotonic()
    if remaining <= 0 or not select.select([], [channel], [], remaining)[1] or channel.send(raw) != len(raw):
        raise TimeoutError()


def run_once(selected, argv, observer):
    return selected.main(argv, _managed_observer=observer)


def main(argv, *, retained_fd=None):
    if len(argv) >= 2 and argv[0] == '--retained-fd':
        try:
            parsed_fd = int(argv[1])
        except ValueError as err:
            raise ValueError('invalid_retained_fd') from err
        if retained_fd is None:
            retained_fd = parsed_fd
        argv = argv[2:]
    elif retained_fd is None and 'HUB_ALERT_RETAINED_FD' in os.environ:
        try:
            retained_fd = int(os.environ['HUB_ALERT_RETAINED_FD'])
        except ValueError:
            retained_fd = None
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
    transferred_fd = None
    if end > time.monotonic():
        try:
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            channel.setblocking(False)
            try:
                channel.connect(str(Path(gate_dir) / 'source-gate.sock'))
            except BlockingIOError:
                pass
            send(channel, b'held-before-quota-source', end)
            custody_cmd, transferred_fd = recv_open(channel, end)
            if custody_cmd == b'custody':
                command, extra_fd = recv_open(channel, end)
                if extra_fd is not None:
                    try:
                        os.close(extra_fd)
                    except OSError:
                        pass
            else:
                command = custody_cmd
            if len(command) == 37 and command.startswith(b'open:'):
                candidate = command[5:].decode('ascii')
                if len(candidate) == 32 and all(c in '0123456789abcdef' for c in candidate):
                    context = candidate
        except Exception:
            context = None

    try:
        quota_raw = acquired(quota_path, quota_sha)
    except Exception:
        # Path swapped or unreadable after selection: run verified retained/transferred original once unqualified.
        fallback_fd = transferred_fd if transferred_fd is not None else retained_fd
        quota_raw = _read_verified_fd(fallback_fd, quota_sha, allow_unlinked=True)
        context = None
        qualified = False
    finally:
        if transferred_fd is not None:
            try:
                os.close(transferred_fd)
            except OSError:
                pass

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
        try:
            helper = types.ModuleType('selected_quota_client')
            helper.__file__ = client_path
            exec(compile(helper_raw, client_path, 'exec'), helper.__dict__)

            def exchange(_path, request, *, deadline=None):
                stop = min(time.monotonic() + 0.15, deadline) if deadline is not None else time.monotonic() + 0.15
                send(channel, helper.encode(request, helper.FRAME), stop)
                return helper.decode(recv(channel, stop), helper.REPLY)

            helper.exchange = exchange
            observer = ManagedObserver(helper.Client(None, context, None, 'quota_monitor'))
        except Exception:
            observer = None
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    pass
                channel = None

    original = None
    try:
        return run_once(selected, primary_args, observer)
    except BaseException as error:
        original = error
        raise
    finally:
        if channel is not None:
            try:
                channel.close()
            except BaseException as cleanup_error:
                if observer is not None:
                    try:
                        observer.fail()
                    except BaseException:
                        pass
                if original is None and not isinstance(cleanup_error, Exception):
                    raise


if __name__ == '__main__':
    if len(sys.argv) == 1:
        print(json.dumps({'status': 'INERT_PRELUDE', 'authority': 'NONE', 'complete': False}))
    else:
        raise SystemExit(main(sys.argv[1:]))
