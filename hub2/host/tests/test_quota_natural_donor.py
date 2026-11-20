"""Simulated unit tests for the inert fixed-profile Root natural-quota donor.

All Root/UID0/systemctl/fanotify/process-identity facts in this file are
explicitly simulated; this module exercises protocol, FD custody, and fail-closed
guards without root privileges, sudo, systemctl, or live Host services.
"""
import array
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]


def _load_donor():
    spec = importlib.util.spec_from_file_location(
        'hub_alert_natural_donor', REPO / 'scripts/hub-alert-natural-donor.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


donor = _load_donor()
COMMIT = 'a' * 40


class SimulatedRootDonorTests(unittest.TestCase):
    def setUp(self):
        scratch = REPO / '.local/tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch, prefix='test-nat-donor-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.entry_bytes = b'PK\x03\x04simulated-host-pyz'
        self.entry_sha = hashlib.sha256(self.entry_bytes).hexdigest()
        self.entry_path = f'{donor.RELEASE_ROOT}/{COMMIT}/host.pyz'
        self.profile_path = self.root / 'natural-donor.json'
        self.ledger_path = self.root / 'custody.json'
        self.gate_dir = self.root / 'gate'
        self.gate_dir.mkdir(mode=0o700)
        self.unlinked = []
        self.profile = {
            'v': 1,
            'target_user': 'alexey',
            'unit': donor.TARGET_UNIT,
            'python_bin': '/usr/bin/python3.12',
            'release_commit': COMMIT,
            'entry_archive': self.entry_path,
            'entry_sha256': self.entry_sha,
            'source_directory': str(self.root / 'src'),
            'selection_path': str(self.root / 'sel/selection.json'),
            'broker_directory': str(self.root / 'broker'),
            'gate_directory': str(self.gate_dir),
            'startup_socket_path': str(self.root / 'startup.sock'),
            'ledger_path': str(self.ledger_path),
        }
        self.profile_path.write_text(json.dumps(self.profile), encoding='utf-8')
        self.clock_ns = 1_000_000_000

    def _run_donation(self, *, peer_uid=1000, peer_pid=4242, send_ack=b'ACK\n',
                      request_mutator=None, pin_fail=False, send_fail=False,
                      prior_ledger=None, nonce='b' * 64):
        donor_sock, user_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(donor_sock.close)
        self.addCleanup(user_sock.close)
        slot_fds = [os.open('/dev/null', os.O_RDONLY) for _ in donor.ROLES]
        self.addCleanup(lambda: [os.close(fd) for fd in slot_fds])
        received_bundle = {}
        ledger_store = {'current': prior_ledger}
        gate_holder = []

        def fake_user_peer():
            req = {
                'v': 1,
                'kind': 'natural_donation_request_v1',
                'nonce': nonce,
                'target_uid': 1000,
                'deadline_ns': self.clock_ns + 4_000_000_000,
                'slots': list(slot_fds),
                'roles': list(donor.ROLES),
            }
            if request_mutator is not None:
                request_mutator(req)
            received_bundle['req_slots'] = list(req['slots'])
            user_sock.send(json.dumps(req).encode('utf-8'))
            try:
                msg, anc, flags, _ = user_sock.recvmsg(
                    donor.MAX_PACKET,
                    socket.CMSG_SPACE(len(donor.ROLES) * array.array('i').itemsize),
                )
            except OSError:
                return
            if not msg:
                return
            received_bundle['reply'] = json.loads(msg.decode('utf-8'))
            rec_fds = []
            for level, kind, data in anc:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    arr = array.array('i')
                    arr.frombytes(data[:len(data) - len(data) % arr.itemsize])
                    rec_fds.extend(arr)
            received_bundle['fds'] = rec_fds
            received_bundle['flags'] = flags
            if len(rec_fds) == len(donor.ROLES):
                manifest_fd = rec_fds[-1]
                ack_fd = rec_fds[-2]
                received_bundle['manifest'] = json.loads(os.read(manifest_fd, 4096).decode('utf-8'))
                if send_ack is not None:
                    os.write(ack_fd, send_ack)
            for fd in rec_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

        thread = threading.Thread(target=fake_user_peer, daemon=True)
        thread.start()

        def fake_pin_interp(py_bin):
            fd = os.open('/dev/null', os.O_RDONLY)
            return fd, {'path': py_bin, 'dev': 1, 'ino': 10, 'size': 100, 'mtime_ns': 1, 'ctime_ns': 1, 'sha256': '1' * 64}

        def fake_pin_entry(path, sha):
            fd = os.open('/dev/null', os.O_RDONLY)
            return fd, {'path': path, 'dev': 1, 'ino': 11, 'size': len(self.entry_bytes), 'mtime_ns': 1, 'ctime_ns': 1, 'sha256': sha}

        def fake_pin_src(dir_path, name):
            if pin_fail and name == 'hub_alert_client.py':
                raise donor.DonorError('simulated_source_pin_failure')
            fd = os.open('/dev/null', os.O_RDONLY)
            dfd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
            return fd, dfd, {'name': name, 'dev': 1, 'ino': 12, 'mtime_ns': 1, 'ctime_ns': 1, 'sha256': '2' * 64}

        def fake_fan(_dfd, _name):
            return os.open('/dev/null', os.O_RDONLY)

        def fake_gate(_dir, _uid):
            gs = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            gdfd = os.open(str(self.gate_dir), os.O_RDONLY | os.O_DIRECTORY)
            gnfd = os.open('/dev/null', os.O_RDONLY)
            ident = (99, 999)
            gate_holder.append(ident)
            return gs, gdfd, gnfd, ident

        def fake_runner(argv, **_kwargs):
            self.assertEqual(argv[:4], [donor.SYSTEMCTL_BIN, '--user', '--machine=alexey@.host', 'show'])
            return types.SimpleNamespace(
                returncode=0,
                stdout='MainPID=4242\nActiveState=active\nSubState=running\nInvocationID=inv-1\nExecMainStartTimestampMonotonic=555\n',
            )

        def fake_ledger_read(_path, *, optional=False):
            return ledger_store['current']

        def fake_ledger_write(_path, data):
            ledger_store['current'] = dict(data)

        class _SendFailSock:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def sendmsg(self, *_args, **_kwargs):
                raise OSError('simulated_send_failure')

        connector_sock = _SendFailSock(donor_sock) if send_fail else donor_sock
        pw = pwd.struct_passwd(('alexey', 'x', 1000, 1000, '', '/home/alexey', '/bin/bash'))

        with patch.multiple(
            donor,
            load_fixed_profile=lambda _p=None: dict(self.profile),
            pin_interpreter=fake_pin_interp,
            pin_entry_archive=fake_pin_entry,
            pin_root_source=fake_pin_src,
        ):
            try:
                res = donor.donate_from_fixed_profile(
                    profile_path=str(self.profile_path),
                    uid_getter=lambda: 0,
                    euid_getter=lambda: 0,
                    pw_lookup=lambda _u: pw,
                    runner=fake_runner,
                    proc_reader=lambda _pid: ('4242 (hub2) S ' + '0 ' * 18 + '12345 0', 'Uid:\t1000\t1000\t1000\t1000\n'),
                    pidfd_opener=lambda _pid: -1,
                    monotonic_ns=lambda: self.clock_ns,
                    endpoint_connector=lambda _p, _u: connector_sock,
                    peer_cred_getter=lambda _s: (peer_pid, peer_uid, 1000),
                    fanotify_opener=fake_fan,
                    gate_binder=fake_gate,
                    ledger_reader=fake_ledger_read,
                    ledger_writer=fake_ledger_write,
                )
                return res, received_bundle, ledger_store['current']
            finally:
                thread.join(timeout=2)

    def test_inert_default_and_root_required(self):
        self.assertEqual(donor.main([]), 0)
        with self.assertRaisesRegex(donor.DonorError, 'root_entry_required'):
            donor.donate_from_fixed_profile(
                profile_path=str(self.profile_path),
                uid_getter=lambda: 1000,
                euid_getter=lambda: 1000,
            )

    def test_rejects_arbitrary_cli_flags(self):
        with self.assertRaises(SystemExit) as caught:
            donor.main(['--donate', '--profile', '/tmp/evil.json'])
        self.assertEqual(caught.exception.code, 2)

    def test_simulated_donation_transfers_14_rights_target_slots_and_receives_ack(self):
        res, bundle, ledger = self._run_donation()
        self.assertEqual(res['status'], 'DONATED')
        self.assertEqual(res['main_pid'], 4242)
        self.assertEqual(bundle['reply']['kind'], 'natural_donation_rights_v1')
        self.assertEqual(bundle['reply']['roles'], list(donor.ROLES))
        self.assertEqual(len(bundle['fds']), 14)
        manifest = bundle['manifest']
        self.assertEqual(manifest['v'], 2)
        self.assertEqual(manifest['kind'], 'natural_quota_root_donation_v1')
        self.assertEqual(manifest['ack_fd'], bundle['req_slots'][12])
        self.assertEqual(manifest['interpreter']['fd'], bundle['req_slots'][0])
        self.assertEqual(manifest['entry']['fd'], bundle['req_slots'][1])
        self.assertEqual(manifest['fan_group'], bundle['req_slots'][8])
        self.assertEqual(ledger['state'], 'adopted')

    def test_rejects_foreign_peer_replayed_nonce_bad_roles_and_bad_slots(self):
        with self.assertRaisesRegex(donor.DonorError, 'peer_identity_mismatch'):
            self._run_donation(peer_uid=1001)
        with self.assertRaisesRegex(donor.DonorError, 'peer_identity_mismatch'):
            self._run_donation(peer_pid=9999)
        with self.assertRaisesRegex(donor.DonorError, 'prior_custody_unknown'):
            self._run_donation(prior_ledger={'state': 'rights_may_have_transferred', 'nonce': 'c' * 64})
        with self.assertRaisesRegex(donor.DonorError, 'replayed_nonce'):
            self._run_donation(prior_ledger={'state': 'adopted', 'nonce': 'b' * 64})
        with self.assertRaisesRegex(donor.DonorError, 'invalid_request_roles'):
            self._run_donation(request_mutator=lambda r: r['roles'].reverse())
        with self.assertRaisesRegex(donor.DonorError, 'invalid_request_slots'):
            self._run_donation(request_mutator=lambda r: r.__setitem__('slots', [10] * 14))
        with self.assertRaisesRegex(donor.DonorError, 'invalid_request_deadline'):
            self._run_donation(request_mutator=lambda r: r.__setitem__('deadline_ns', self.clock_ns + 99_000_000_000))

    def test_missing_ack_and_partial_send_preserve_unknown_custody(self):
        with self.assertRaisesRegex(donor.DonorError, 'ack_eof_before_confirmation'):
            self._run_donation(send_ack=b'')
        with self.assertRaisesRegex(donor.DonorError, 'invalid_ack_payload'):
            self._run_donation(send_ack=b'NOPE')
        with self.assertRaisesRegex(OSError, 'simulated_send_failure'):
            self._run_donation(send_fail=True)

    def test_existing_foreign_gate_is_preserved_not_unlinked(self):
        (self.gate_dir / 'source-gate.sock').write_text('foreign')
        with self.assertRaisesRegex(donor.DonorError, 'existing_gate_node_preserved'):
            donor.bind_source_gate(str(self.gate_dir), os.getuid())
        self.assertTrue((self.gate_dir / 'source-gate.sock').exists())


if __name__ == '__main__':
    unittest.main()
