"""Bounded USER-side transport for a future privileged natural-quota donor.

This only transfers custody. It does not authorize a source, execute an image,
admit a producer, or acknowledge adoption. The Root FIFO receiver must perform
its existing checks before adoption. No privileged helper is invoked here.
"""
import array
import json
import os
import re
import select
import socket
import struct
import time


ROLES = ('interpreter', 'entry', 'selection', 'selection_directory', 'source',
         'source_directory', 'client', 'client_directory', 'fan_group',
         'gate_socket', 'gate_directory', 'gate_node', 'ack_write', 'manifest')
MAX_PACKET = 4096
MAX_HANDOFF_NS = 5_000_000_000


class TransferError(ValueError):
    """The optional observer transport did not establish custody."""


def _require(value):
    if not value:
        raise TransferError('natural_donation_transfer_rejected')


def _close_owned(fds):
    """Remove ownership before close; an uncertain close is never retried."""
    clean = True
    while fds:
        fd = fds.pop()
        try:
            os.close(fd)
        except OSError:
            clean = False
    return clean


class ReceivedDonation:
    """Only newly reserved slots belong to this object, never caller FDs."""
    def __init__(self, slots, deadline_ns):
        self._owned = list(slots)
        self.slots = tuple(slots)
        self.deadline_ns = deadline_ns
        self.cleanup_certain = True

    @property
    def manifest_fd(self):
        _require(bool(self._owned))
        return self.slots[-1]

    def close(self):
        self.cleanup_certain = _close_owned(self._owned) and self.cleanup_certain
        return self.cleanup_certain

    def take(self):
        """Transfer all custody to the validating caller exactly once."""
        _require(bool(self._owned))
        result = tuple(self._owned)
        self._owned.clear()
        return result


def _unique(items):
    result = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _wait(channel, deadline_ns, *, write=False):
    while True:
        remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        _require(remaining > 0)
        try:
            ready = select.select([] if write else [channel],
                                  [channel] if write else [], [], remaining)
        except InterruptedError:
            continue
        _require(bool(ready[1] if write else ready[0]))
        _require(time.monotonic_ns() < deadline_ns)
        return


def _rights(ancillary, owned):
    """Collect all received rights before rejecting any control metadata."""
    valid = True
    seen = set()
    for level, kind, data in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            valid = False
            continue
        values = array.array('i')
        aligned = len(data) - len(data) % values.itemsize
        values.frombytes(data[:aligned])
        valid = valid and aligned == len(data)
        for fd in values:
            if fd < 0 or fd in seen:
                valid = False
            elif fd >= 0:
                seen.add(fd)
                owned.append(fd)
    _require(valid and len(owned) == len(ROLES))


def receive_root_donation(channel, *, nonce, deadline_ns):
    """Receive one bounded seqpacket from a real UID0 peer into our slots.

    The caller owns the connected socket and must close it. All acquired FDs
    remain CLOEXEC: a later reviewed exec/adoption layer must deliberately
    select inheritance. This function sends no ACK and never unlinks a gate.
    """
    now = time.monotonic_ns()
    _require(os.getuid() == os.geteuid() > 0)
    _require(type(nonce) is str and re.fullmatch('[0-9a-f]{64}', nonce))
    _require(type(deadline_ns) is int and now < deadline_ns <= now + MAX_HANDOFF_NS)
    _require(channel.family == socket.AF_UNIX
             and channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET)
    pid, uid, _gid = struct.unpack('3i', channel.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    _require(pid > 0 and uid == 0)
    _require(hasattr(socket, 'MSG_CMSG_CLOEXEC'))
    slots = []
    received = []
    try:
        for _ in ROLES:
            fd = os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            slots.append(fd)
            _require(fd > 2)
        request = json.dumps({'v': 1, 'kind': 'natural_donation_request_v1',
                              'nonce': nonce, 'target_uid': os.getuid(),
                              'deadline_ns': deadline_ns, 'slots': slots,
                              'roles': ROLES}, separators=(',', ':')).encode()
        _require(len(request) <= MAX_PACKET)
        while True:
            _wait(channel, deadline_ns, write=True)
            try:
                count = channel.sendmsg([request], [], socket.MSG_DONTWAIT)
            except (BlockingIOError, InterruptedError):
                continue
            _require(count == len(request))
            break
        while True:
            _wait(channel, deadline_ns)
            try:
                packet, ancillary, flags, _address = channel.recvmsg(
                    MAX_PACKET, socket.CMSG_SPACE(len(ROLES) * array.array('i').itemsize),
                    socket.MSG_CMSG_CLOEXEC | socket.MSG_DONTWAIT)
            except (BlockingIOError, InterruptedError):
                continue
            break
        _rights(ancillary, received)
        _require(not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
        _require(bool(packet) and time.monotonic_ns() < deadline_ns)
        value = json.loads(packet, object_pairs_hook=_unique,
                           parse_constant=lambda _: _require(False))
        _require(type(value) is dict and set(value) == {
            'v', 'kind', 'nonce', 'target_uid', 'deadline_ns', 'roles'})
        _require(type(value['v']) is int and value['v'] == 1
                 and value['kind'] == 'natural_donation_rights_v1'
                 and value['nonce'] == nonce
                 and type(value['target_uid']) is int and value['target_uid'] == os.getuid()
                 and type(value['deadline_ns']) is int and value['deadline_ns'] == deadline_ns
                 and value['roles'] == list(ROLES))
        _require(not set(received).intersection(slots))
        for source, target in zip(received, slots):
            _require(time.monotonic_ns() < deadline_ns)
            os.dup2(source, target, inheritable=False)
            _require(time.monotonic_ns() < deadline_ns)
        _require(_close_owned(received))
        _require(time.monotonic_ns() < deadline_ns)
        result = ReceivedDonation(slots, deadline_ns)
        slots = []  # custody passes exactly once; no adoption ACK here
        return result
    except BaseException:
        _close_owned(received)
        _close_owned(slots)
        raise
