"""Real USER-owned SCM_RIGHTS transport; UID0 peer is simulated, never run."""
import array
import json
import os
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from agent_hub_host import quota_natural_transfer as transfer


class Peer:
    family = socket.AF_UNIX

    def __init__(self, channel, *, root=True):
        self.channel = channel
        self.root = root
        self.received = []

    def fileno(self):
        return self.channel.fileno()

    def getsockopt(self, level, kind, *args):
        if kind == socket.SO_PEERCRED and self.root:
            return struct.pack('3i', os.getpid(), 0, 0)
        return self.channel.getsockopt(level, kind, *args)

    def sendmsg(self, *args):
        return self.channel.sendmsg(*args)

    def recvmsg(self, *args):
        result = self.channel.recvmsg(*args)
        for level, kind, data in result[1]:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array('i')
                values.frombytes(data)
                self.received.extend(values)
        return result


@unittest.skipUnless(hasattr(socket, 'SCM_RIGHTS') and hasattr(socket, 'SO_PEERCRED'), 'Linux transport')
class NaturalTransferTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)
        self.peer = Peer(self.left)
        self.originals = [os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC) for _ in range(15)]
        for fd in self.originals:
            self.addCleanup(os.close, fd)
        self.nonce = 'ab' * 32
        self.deadline = time.monotonic_ns() + 2_000_000_000
        self.requests = []
        self.errors = []

    def donor(self, mutate=lambda value: value, *, count=14, raw=None):
        def run():
            try:
                packet = self.right.recv(4096)
                request = json.loads(packet)
                self.requests.append(request)
                value = {k: v for k, v in request.items() if k != 'slots'}
                value['kind'] = 'natural_donation_rights_v1'
                value = mutate(value)
                payload = raw if raw is not None else json.dumps(value).encode()
                control = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                            array.array('i', self.originals[:count]))] if count else []
                self.right.sendmsg([payload], control)
            except BaseException as error:
                self.errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(self.join, thread)

    def join(self, thread):
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.errors)

    def receive(self, **kwargs):
        return transfer.receive_root_donation(
            self.peer, nonce=kwargs.get('nonce', self.nonce),
            deadline_ns=kwargs.get('deadline_ns', self.deadline))

    def assert_closed(self, fds):
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def rejected(self, mutate=lambda value: value, *, count=14, raw=None):
        self.donor(mutate, count=count, raw=raw)
        with self.assertRaises((transfer.TransferError, ValueError, UnicodeError)):
            self.receive()
        self.assert_closed(self.requests[0]['slots'])
        self.assert_closed(self.peer.received)
        # Sender originals and the caller's socket remain owned by their caller.
        for fd in self.originals:
            os.fstat(fd)
        os.fstat(self.left.fileno())

    def test_real_rights_land_only_in_reserved_slots_and_are_cloexec(self):
        self.donor()
        result = self.receive()
        self.addCleanup(result.close)
        self.assertEqual(result.slots, tuple(self.requests[0]['slots']))
        self.assertEqual(result.manifest_fd, result.slots[-1])
        self.assertTrue(set(result.slots).isdisjoint(self.originals))
        for received, original in zip(result.slots, self.originals):
            a, b = os.fstat(received), os.fstat(original)
            self.assertEqual((a.st_dev, a.st_ino), (b.st_dev, b.st_ino))
            self.assertFalse(os.get_inheritable(received))
        self.assert_closed(self.peer.received)
        self.assertTrue(result.close())
        self.assertTrue(result.close())
        self.assert_closed(result.slots)
        with self.assertRaises(transfer.TransferError):
            _ = result.manifest_fd

    def test_take_transfers_custody_once_without_closing(self):
        self.donor()
        result = self.receive()
        slots = result.take()
        for fd in slots:
            self.addCleanup(os.close, fd)
        self.assertTrue(result.close())
        for fd in slots:
            os.fstat(fd)
        with self.assertRaises(transfer.TransferError):
            result.take()

    def test_actual_unprivileged_peer_refused_before_reservations(self):
        self.peer.root = False
        with patch.object(transfer.os, 'open', side_effect=AssertionError('unexpected reservation')):
            with self.assertRaises(transfer.TransferError):
                self.receive()

    def test_nonce_mismatch_closes_all_received_and_reserved_fds(self):
        self.rejected(lambda v: dict(v, nonce='cd' * 32))

    def test_deadline_cannot_be_renewed_by_donor(self):
        self.rejected(lambda v: dict(v, deadline_ns=v['deadline_ns'] + 1))

    def test_wrong_target_uid_refused(self):
        self.rejected(lambda v: dict(v, target_uid=0))

    def test_bool_version_refused(self):
        self.rejected(lambda v: dict(v, v=True))

    def test_role_reorder_refused(self):
        self.rejected(lambda v: dict(v, roles=list(reversed(v['roles']))))

    def test_unknown_header_field_refused(self):
        self.rejected(lambda v: dict(v, trusted=True))

    def test_missing_rights_refused(self):
        self.rejected(count=0)

    def test_short_rights_refused(self):
        self.rejected(count=13)

    def test_excess_rights_control_truncation_refused(self):
        self.rejected(count=15)

    def test_truncated_data_refused_and_rights_closed(self):
        self.rejected(raw=b'x' * (transfer.MAX_PACKET + 1))

    def test_duplicate_json_key_refused(self):
        self.rejected(raw=b'{"v":1,"v":1}')

    def test_eof_does_not_reserve_forever(self):
        self.right.shutdown(socket.SHUT_WR)
        with self.assertRaises(transfer.TransferError):
            self.receive()

    def test_original_deadline_timeout_closes_reservations(self):
        opened = []
        original = os.open
        def record(*args, **kwargs):
            fd = original(*args, **kwargs)
            opened.append(fd)
            return fd
        with patch.object(transfer.os, 'open', side_effect=record):
            with self.assertRaises(transfer.TransferError):
                self.receive(deadline_ns=time.monotonic_ns() + 20_000_000)
        self.assertEqual(len(opened), 14)
        self.assert_closed(opened)

    def test_invalid_inputs_never_send(self):
        with patch.object(self.peer, 'sendmsg', side_effect=AssertionError('sent')):
            for nonce, deadline in [('x', self.deadline), (self.nonce, True),
                                    (self.nonce, time.monotonic_ns() - 1),
                                    (self.nonce, time.monotonic_ns() + 10_000_000_000)]:
                with self.assertRaises(transfer.TransferError):
                    self.receive(nonce=nonce, deadline_ns=deadline)

    def test_expiry_during_slot_mapping_cannot_return_custody(self):
        self.donor()
        duplicate = os.dup2
        expired = False
        def map_fd(*args, **kwargs):
            nonlocal expired
            result = duplicate(*args, **kwargs)
            expired = True
            return result
        def clock():
            return self.deadline + 1 if expired else self.deadline - 1_000_000_000
        with patch.object(transfer.os, 'dup2', side_effect=map_fd), \
                patch.object(transfer.time, 'monotonic_ns', side_effect=clock):
            with self.assertRaises(transfer.TransferError):
                self.receive()
        self.assert_closed(self.requests[0]['slots'])
        self.assert_closed(self.peer.received)

    def test_partial_slot_mapping_failure_closes_all_own_handles(self):
        self.donor()
        duplicate = os.dup2
        calls = 0
        def map_fd(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('failed second mapping')
            return duplicate(*args, **kwargs)
        with patch.object(transfer.os, 'dup2', side_effect=map_fd):
            with self.assertRaises(OSError):
                self.receive()
        self.assert_closed(self.requests[0]['slots'])
        self.assert_closed(self.peer.received)
        for fd in self.originals:
            os.fstat(fd)

    def test_partial_reservation_failure_closes_only_new_slots(self):
        original = os.open
        opened = []
        def reserve(*args, **kwargs):
            if len(opened) == 3:
                raise OSError('reservation unavailable')
            fd = original(*args, **kwargs)
            opened.append(fd)
            return fd
        with patch.object(transfer.os, 'open', side_effect=reserve):
            with self.assertRaises(OSError):
                self.receive()
        self.assert_closed(opened)
        os.fstat(self.left.fileno())

    def test_cleanup_failure_is_sticky_and_descriptor_is_not_retried(self):
        result = transfer.ReceivedDonation([1000, 1001], self.deadline)
        with patch.object(transfer.os, 'close', side_effect=OSError('uncertain')) as close:
            self.assertFalse(result.close())
            self.assertFalse(result.close())
        self.assertEqual(close.call_args_list, [unittest.mock.call(1001), unittest.mock.call(1000)])


if __name__ == '__main__':
    unittest.main()
