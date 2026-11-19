"""Primary caller for immutable natural-quota archive payloads.

The root-owned selection file chooses original business bytes and fixed roles;
it does not admit a source to the broker. A producer cannot manufacture manager,
process, kernel-open or retirement evidence through this interface.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import time
import types


PAYLOADS = ('hub-quota-publish.py', 'hub_alert_client.py', 'natural-quota-prelude.py')


def _require(value):
    if not value:
        raise ValueError('natural_selection_unavailable')


def _digest(value):
    _require(type(value) is str and len(value) == 64
             and all(c in '0123456789abcdef' for c in value))
    return value


def _path(value):
    _require(type(value) is str and '\0' not in value and len(os.fsencode(value)) <= 4096)
    p = Path(value)
    _require(p.is_absolute() and '..' not in p.parts and str(p) == value)
    return p


def read_root_file(path, digest, *, limit=262144):
    """Read one exact root-owned file through root-owned nonsymlink parents."""
    p = _path(path)
    _digest(digest)
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    fd = None
    try:
        for part in p.parent.parts[1:]:
            before = os.fstat(directory)
            _require(stat.S_ISDIR(before.st_mode) and before.st_uid == 0
                     and not before.st_mode & 0o022)
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                          | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = nxt
        parent = os.fstat(directory)
        _require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0
                 and not parent.st_mode & 0o022)
        fd = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | os.O_CLOEXEC, dir_fd=directory)
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == 0
                 and before.st_nlink == 1 and not before.st_mode & 0o022
                 and 0 < before.st_size <= limit)
        raw = os.pread(fd, before.st_size + 1, 0)
        after = os.fstat(fd)
        named = os.stat(p.name, dir_fd=directory, follow_symlinks=False)
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                           s.st_ctime_ns, s.st_uid, s.st_mode, s.st_nlink)
        _require(stamp(before) == stamp(after) == stamp(named)
                 and len(raw) == before.st_size
                 and hashlib.sha256(raw).hexdigest() == digest)
        return raw
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _unique(items):
    out = {}
    for key, value in items:
        _require(key not in out)
        out[key] = value
    return out


def read_selection(path, digest):
    value = json.loads(read_root_file(path, digest, limit=65536),
                       object_pairs_hook=_unique,
                       parse_constant=lambda _: _require(False))
    fields = {'v', 'kind', 'quota_path', 'client_path', 'gate_directory',
              'quota_sha256', 'client_sha256', 'prelude_sha256', 'roles'}
    _require(type(value) is dict and set(value) == fields
             and type(value['v']) is int and value['v'] == 1
             and value['kind'] == 'natural_quota_roles_v1')
    for key in ('quota_path', 'client_path', 'gate_directory'):
        _path(value[key])
    _require(len(os.fsencode(value['gate_directory'])) <= 80
             and type(value['roles']) is dict
             and set(value['roles']) == {'quota_publish', 'quota_monitor'})
    for key in ('quota_sha256', 'client_sha256', 'prelude_sha256'):
        _digest(value[key])
    for role, spec in value['roles'].items():
        _require(type(spec) is dict and set(spec) == {'argv'}
                 and type(spec['argv']) is list
                 and all(type(s) is str for s in spec['argv']))
        argv = tuple(spec['argv'])
        if role == 'quota_publish':
            _require(argv == ())
        else:
            _require(len(argv) == 8 and argv[:3] ==
                     ('--check', '--health-check', '--health-config')
                     and argv[4:] == ('--health-timeout', '8', '--alert-timeout', '8'))
            _path(argv[3])
    return value


class DeadlineChannel:
    """Optional observer socket; every operation clips the original deadline."""
    def __init__(self, directory, end):
        from .quota_dispatch_entry import _send_pkt, _recv_pkt
        self._send, self._recv = _send_pkt, _recv_pkt
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.socket.setblocking(False)
            info = os.stat(directory, follow_symlinks=False)
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o700)
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('natural_observer_expired')
            result = self.socket.connect_ex(str(Path(directory) / 'source-gate.sock'))
            if result not in (0, errno.EINPROGRESS, errno.EAGAIN, errno.EALREADY):
                raise OSError(result, 'natural_observer_unavailable')
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('natural_observer_expired')
            if result and (not select.select([], [self.socket], [], remaining)[1]
                           or self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)):
                raise TimeoutError('natural_observer_unavailable')
            peer = struct.unpack('3i', self.socket.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            _require(peer[1] == os.getuid() and time.monotonic() < end)
        except BaseException:
            self.socket.close()
            raise

    def send(self, raw, end):
        self._send(self.socket, raw, end)

    def recv(self, end):
        return self._recv(self.socket, end)

    def close(self):
        self.socket.close()


def archive_payloads():
    loader = globals().get('__loader__')
    archive = getattr(loader, 'archive', None)
    _require(type(archive) is str and callable(getattr(loader, 'get_data', None)))
    # Required business is retained first. Optional companion faults cannot
    # prevent its verified, once-only unobserved execution.
    values = {PAYLOADS[0]: loader.get_data(archive + '/natural/' + PAYLOADS[0])}
    for name in PAYLOADS[1:]:
        try:
            values[name] = loader.get_data(archive + '/natural/' + name)
        except Exception:
            values[name] = None
    return values


def run_primary(path, digest, role, *, started_ns, payloads):
    """Actual archive caller. Primary source is NOT opened before admission.

    Original bytes come from the immutable archive. The fresh watched-file open
    happens only inside run_selected's post-context source_open callback.
    """
    plan = read_selection(path, digest)
    _require(role in plan['roles'] and type(payloads) is dict
             and set(payloads) <= set(PAYLOADS) and PAYLOADS[0] in payloads)
    quota = payloads['hub-quota-publish.py']
    _require(type(quota) is bytes and 0 < len(quota) <= 262144
             and hashlib.sha256(quota).hexdigest() == plan['quota_sha256'])
    argv = tuple(plan['roles'][role]['argv'])
    try:
        prelude = payloads.get('natural-quota-prelude.py')
        _require(type(prelude) is bytes and 0 < len(prelude) <= 262144
                 and hashlib.sha256(prelude).hexdigest() == plan['prelude_sha256'])
        module = types.ModuleType('immutable_natural_prelude')
        exec(compile(prelude, '<immutable-natural-prelude>', 'exec'), module.__dict__)
        selected = module.SelectedQuota(role, quota, plan['quota_sha256'],
            payloads.get('hub_alert_client.py'), plan['client_sha256'], argv,
            source_path=plan['quota_path'], client_path=plan['client_path'])
    except Exception:
        # Nothing has executed the business yet. An optional bootstrap failure
        # must not cause a second invocation or execute a mutable replacement.
        from .quota_dispatch_entry import _run_verified_original_once
        return _run_verified_original_once(quota, plan['quota_path'], list(argv))

    def source_open(_selected):
        _require(read_root_file(plan['quota_path'], plan['quota_sha256']) == quota)

    return module.run_selected(selected, source_open,
        lambda end: DeadlineChannel(plan['gate_directory'], end),
        started_ns=started_ns)
