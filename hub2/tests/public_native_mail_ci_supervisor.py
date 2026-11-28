"""Linux CI-only stdlib supervisor. Inert on import; no runtime proof by itself.

Own new-session timeout leader stays UNREAPED until the last group signal.
After that signal we reap only this subreaper's descendants to kernel ECHILD.
No numeric group signalling ever follows reap. Escaped/stubborn descendants,
identity/probe failure or parent loss are failures, never closure evidence.
"""
import base64
import ctypes
import errno
import json
import os
import selectors
import signal
import subprocess
import sys
import time

OUT_CAP = 65536
ERR_CAP = 16384


def cleanup_owned(ops, anchor, deadline):
    """Injectable cleanup state machine; all fake tests avoid RealOps entirely."""
    unknown = False
    signals = []
    try:
        if ops.now() >= deadline:
            raise RuntimeError('cleanup_deadline')
        if (type(anchor) is not tuple or len(anchor) != 4 or any(type(v) is not int or v < 1 for v in anchor)
                or anchor[0] != anchor[2] or anchor[0] != anchor[3]):
            raise RuntimeError('anchor_unknown')
        if ops.identity() != anchor:
            raise RuntimeError('identity_unknown')
        # Probe only while the original leader PID/birth/session is still held.
        if ops.group_exists():
            ops.send(signal.SIGINT); signals.append('INT')
            ops.drain_until(min(deadline, ops.now() + min(.2, max(0, (deadline - ops.now()) / 2))))
            if ops.now() >= deadline:
                raise RuntimeError('cleanup_deadline')
            if ops.identity() != anchor:
                raise RuntimeError('identity_changed')
            if ops.group_exists():
                ops.send(signal.SIGKILL); signals.append('KILL')
        # From this point no operation may signal a numeric PID or PGID.
    except Exception:
        unknown = True
    reaped = False
    residual = False
    leader_status = None
    try:
        while ops.now() < deadline:
            value = ops.reap()
            if value == 'ECHILD':
                reaped = True
                break
            if value is None:
                ops.drain_until(min(deadline, ops.now() + .01))
                continue
            pid, status = value
            if pid == anchor[0]:
                leader_status = status
            else:
                residual = True
    except Exception:
        unknown = True
    return {'owned_subtree_reaped': reaped, 'unknown': unknown or not reaped,
            'residual_children': residual, 'leader_status': leader_status,
            'signals': signals}


def drain_capture(ops, deadline):
    # ECHILD is established independently BEFORE this capture-completeness check.
    while ops.now() < deadline and ops.capture_pending():
        ops.drain_until(min(deadline, ops.now() + .01))
    return not ops.capture_pending()


class RealOps:
    def __init__(self, end, expected_parent):
        self.end = end
        self.child = None
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                                   ctypes.c_ulong, ctypes.c_ulong]
        self.libc.prctl.restype = ctypes.c_int
        self.selector = selectors.DefaultSelector()
        self.output = {'out': bytearray(), 'err': bytearray()}
        self.input = b''
        self.fault = None
        self.cancelled = False
        self.anchor = None
        self.root_status = None
        self.parent = expected_parent
        self.old_handlers = {}
    def now(self):
        return time.monotonic()
    def configure(self):
        if not sys.platform.startswith('linux'):
            raise RuntimeError('linux_required')
        # Single-thread process, no global signal mask or inherited spawn hook.
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.old_handlers[sig] = signal.signal(sig, lambda *_: setattr(self, 'cancelled', True))
        # SIG_IGN would auto-reap our identity anchor; explicitly restore the
        # normal child-reaping contract before any spawn.
        self.old_handlers[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, [])
        if os.getppid() != self.parent:
            raise RuntimeError('parent_changed')
        if self.libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise RuntimeError('subreaper_unavailable')
        if self.libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise RuntimeError('parent_fence_unavailable')
        if os.getppid() != self.parent:
            raise RuntimeError('parent_changed')
    def read_input(self):
        os.set_blocking(0, False)
        self.selector.register(0, selectors.EVENT_READ, 'input')
        data = bytearray()
        try:
            while self.now() < self.end - .7 and not self.cancelled:
                for key, _ in self.selector.select(min(.01, max(0, self.end - self.now()))):
                    raw = os.read(key.fd, 8193)
                    if not raw:
                        return bytes(data)
                    data.extend(raw)
                    if len(data) > 8192:
                        raise RuntimeError('input_cap')
            raise RuntimeError('input_deadline')
        finally:
            self.selector.unregister(0)
    def spawn(self, command, data):
        parent = os.getpid()
        libc = self.libc
        def child_fence():
            signal.pthread_sigmask(signal.SIG_SETMASK, [])
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
                os._exit(125)
        self.child = subprocess.Popen(command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            close_fds=True, preexec_fn=child_fence)
        # Popen.poll/wait/communicate MUST NOT run before our last group signal.
        self.anchor = self.identity()
        if self.anchor[0] != self.anchor[2] or self.anchor[0] != self.anchor[3]:
            raise RuntimeError('new_session_required')
        self.input = data
        for stream, events, name in [(self.child.stdout, selectors.EVENT_READ, 'out'),
            (self.child.stderr, selectors.EVENT_READ, 'err'),
            (self.child.stdin, selectors.EVENT_WRITE, 'stdin')]:
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, events, name)
    def identity(self):
        pid = self.child.pid
        # Metadata only for our own unreaped direct child; never argv/env.
        with open('/proc/' + str(pid) + '/stat', 'rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise RuntimeError('stat_cap')
        fields = raw[raw.rfind(b')') + 2:].split()
        if len(fields) < 20 or int(fields[1]) != os.getpid():
            raise RuntimeError('parent_identity_unknown')
        return (pid, int(fields[19]), int(fields[2]), int(fields[3]))
    def group_exists(self):
        try:
            os.killpg(self.anchor[0], 0)
            return True
        except ProcessLookupError:
            return False
        # EPERM and every other error remain UNKNOWN, never empty.
    def send(self, sig):
        os.killpg(self.anchor[0], sig)
    def drain_until(self, until):
        while self.now() < until:
            for key, _ in self.selector.select(min(.01, max(0, until - self.now()))):
                if key.data == 'stdin':
                    try:
                        if self.input:
                            count = os.write(key.fd, self.input)
                            self.input = self.input[count:]
                        if not self.input:
                            self.selector.unregister(key.fileobj)
                            key.fileobj.close()
                    except BrokenPipeError:
                        self.selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    raw = os.read(key.fd, 8192)
                    if not raw:
                        self.selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    buf = self.output[key.data]
                    cap = OUT_CAP if key.data == 'out' else ERR_CAP
                    if len(buf) + len(raw) > cap:
                        self.fault = 'capture_cap'
                    else:
                        buf.extend(raw)
    def capture_pending(self):
        return any(key.data in ('out', 'err') for key in self.selector.get_map().values())
    def leader_done(self):
        return os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    def reap(self):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return 'ECHILD'
        if not pid:
            return None
        if pid == self.child.pid:
            self.root_status = status
            self.child.returncode = os.waitstatus_to_exitcode(status)
        return pid, status
    def close(self):
        # Attempt each independent owned close once, even if another fails.
        failures = False
        for key in list(self.selector.get_map().values()):
            try:
                self.selector.unregister(key.fileobj)
            except Exception:
                failures = True
            if key.data != 'input':
                try:
                    key.fileobj.close()
                except Exception:
                    failures = True
        try:
            self.selector.close()
        except Exception:
            failures = True
        for sig, old in self.old_handlers.items():
            signal.signal(sig, old)
        return failures


def supervise(command, milliseconds, expected_parent):
    start = time.monotonic()
    ops = RealOps(start + milliseconds / 1000, expected_parent)
    result = {'owned_subtree_reaped': False, 'unknown': True,
              'residual_children': False, 'leader_status': None, 'signals': []}
    failed = False
    capture_complete = False
    try:
        ops.configure()
        data = ops.read_input()
        ops.spawn(command, data)
        while ops.now() < ops.end - .7 and not ops.cancelled and not ops.fault:
            if ops.leader_done():
                break
            ops.drain_until(min(ops.end - .7, ops.now() + .01))
        else:
            failed = True
    except BaseException:
        failed = True
    finally:
        if ops.child is not None:
            try:
                # Recover only this still-unreaped own leader's identity. If an
                # earlier pin exists, never replace it to accommodate a mismatch.
                if ops.anchor is None:
                    ops.anchor = ops.identity()
                result = cleanup_owned(ops, ops.anchor, ops.end - .05)
            except BaseException:
                failed = True
        if result['owned_subtree_reaped']:
            try:
                capture_complete = drain_capture(ops, ops.end - .01)
            except BaseException:
                failed = True
        result['capture_complete'] = capture_complete
        if ops.close():
            failed = True
    status = result['leader_status']
    code = os.waitstatus_to_exitcode(status) if status is not None else None
    # Any adopted nonleader is a residual cleanup defect, even if it happened
    # to exit0. No successful scenario is credited after forced orphan cleanup.
    good = (capture_complete and not failed and not ops.cancelled and not ops.fault and not result['unknown']
            and not result['residual_children'] and code == 0)
    return {'schema': 1, 'supervision': {**result, 'leader_exit_code': code,
            'success': good}, 'stdout_b64': base64.b64encode(ops.output['out']).decode('ascii'),
            'stderr_b64': base64.b64encode(ops.output['err']).decode('ascii')}, good


def main():
    if (len(sys.argv) < 7 or sys.argv[1] != '--parent-pid'
            or sys.argv[3] != '--remaining-ms' or sys.argv[5] != '--'):
        raise ValueError('fixed_invocation_required')
    expected_parent = int(sys.argv[2])
    milliseconds = int(sys.argv[4])
    if (expected_parent < 2 or os.getppid() != expected_parent
            or not 1 <= milliseconds <= 49000 or not os.path.isabs(sys.argv[6])):
        raise ValueError('invalid_budget_or_command')
    publish_end = time.monotonic() + milliseconds / 1000
    result, good = supervise(sys.argv[6:], milliseconds, expected_parent)
    raw = json.dumps(result, separators=(',', ':')).encode() + b'\n'
    if len(raw) > 114688:
        raise ValueError('receipt_cap')
    # One bounded pipe write; reader independently caps total bytes. No logs,
    # original exception strings, argv, environment or control input emitted.
    os.set_blocking(1, False)
    selector = selectors.DefaultSelector()
    try:
        selector.register(1, selectors.EVENT_WRITE)
        while raw and time.monotonic() < publish_end:
            if not selector.select(min(.01, max(0, publish_end - time.monotonic()))):
                continue
            try:
                count = os.write(1, raw)
                if count <= 0:
                    return 2
                raw = raw[count:]
            except BlockingIOError:
                continue
        if raw:
            return 2
    finally:
        selector.close()
    return 0 if good else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        os.write(2, b'public_mail_supervisor_unknown\n')
        raise SystemExit(2)
