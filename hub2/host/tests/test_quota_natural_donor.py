"""Simulated unit tests for the inert fixed-profile Root natural-quota donor.

All Root/UID0/systemctl/fanotify/process-identity facts in this file are
explicitly simulated; this module exercises protocol, FD custody, deadline,
incarnation, and fail-closed guards without root privileges, sudo, systemctl,
or live Host services.
"""
import array
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import socket
import tempfile
import threading
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
                      prior_ledger=None, nonce='b' * 64, pidfd_factory=None,
                      runner_fn=None, proc_fn=None, boot_fn=None, clock_fn=None,
                      eagain_send=False, close_fail_once=False):
        donor_sock, user_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(donor_sock.close)
        self.addCleanup(user_sock.close)
        user_sock.settimeout(2.0)
        slot_fds = [os.open('/dev/null', os.O_RDONLY) for _ in donor.ROLES]
        self.addCleanup(lambda: [os.close(fd) for fd in slot_fds])
        received_bundle = {}
        ledger_store = {'current': prior_ledger}

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
            try:
                user_sock.send(json.dumps(req).encode('utf-8'))
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

        thread = threading.Thread(target=fake_user_peer)
        thread.start()

        def fake_pin_interp(py_bin, **_kwargs):
            fd = os.open('/dev/null', os.O_RDONLY)
            return fd, {'path': py_bin, 'dev': 1, 'ino': 10, 'size': 100, 'mtime_ns': 1, 'ctime_ns': 1, 'sha256': '1' * 64}

        def fake_pin_entry(path, sha, **_kwargs):
            fd = os.open('/dev/null', os.O_RDONLY)
            return fd, {'path': path, 'dev': 1, 'ino': 11, 'size': len(self.entry_bytes), 'mtime_ns': 1, 'ctime_ns': 1, 'sha256': sha}

        def fake_pin_src(dir_path, name, **_kwargs):
            if pin_fail and name == 'hub_alert_client.py':
                raise donor.DonorError('simulated_source_pin_failure')
            fd = os.open('/dev/null', os.O_RDONLY)
            dfd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
            return fd, dfd, {'name': name, 'dev': 1, 'ino': 12, 'mtime_ns': 1, 'ctime_ns': 1, 'sha256': '2' * 64}

        def fake_fan(_dfd, _name):
            return os.open('/dev/null', os.O_RDONLY)

        def fake_gate(_dir, _uid, **_kwargs):
            gs = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            gdfd = os.open(str(self.gate_dir), os.O_RDONLY | os.O_DIRECTORY)
            gnfd = os.open('/dev/null', os.O_RDONLY)
            return gs, gdfd, gnfd, (99, 999)

        def default_runner(argv, **_kwargs):
            self.assertEqual(argv[:4], [donor.SYSTEMCTL_BIN, '--user', '--machine=alexey@.host', 'show'])
            return types.SimpleNamespace(
                returncode=0,
                stdout='MainPID=4242\nActiveState=active\nSubState=running\nInvocationID=inv-1\nExecMainStartTimestampMonotonic=555\n',
            )

        def default_proc(_pid):
            return (
                '4242 (hub2) S ' + '0 ' * 18 + '12345 0',
                'Uid:\t1000\t1000\t1000\t1000\n',
                '0::/user.slice/user-1000.slice/user@1000.service/app.slice/agent-hub2-host.service\n',
            )

        def default_pidfd(_pid):
            return os.pidfd_open(os.getpid(), 0)

        def fake_ledger_read(_path, *, optional=False, **_kwargs):
            return ledger_store['current']

        def fake_ledger_write(_path, data, **_kwargs):
            ledger_store['current'] = dict(data)

        class _SockProxy:
            def __init__(self, inner):
                self._inner = inner
                self.send_attempts = 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def sendmsg(self, *args, **kwargs):
                if send_fail:
                    raise OSError('simulated_send_failure')
                if eagain_send and self.send_attempts == 0:
                    self.send_attempts += 1
                    raise BlockingIOError(errno.EAGAIN, 'simulated_eagain')
                return self._inner.sendmsg(*args, **kwargs)

        connector_sock = _SockProxy(donor_sock) if (send_fail or eagain_send) else donor_sock
        pw = pwd.struct_passwd(('alexey', 'x', 1000, 1000, '', '/home/alexey', '/bin/bash'))
        real_close = os.close
        failed_once = {'done': False}

        def maybe_fail_close(fd):
            if close_fail_once and not failed_once['done']:
                failed_once['done'] = True
                real_close(fd)
                raise OSError(errno.EIO, 'simulated_close_eio')
            real_close(fd)

        with patch.multiple(
            donor,
            load_fixed_profile=lambda _p=None, **_k: dict(self.profile),
            pin_interpreter=fake_pin_interp,
            pin_entry_archive=fake_pin_entry,
            pin_root_source=fake_pin_src,
        ), patch.object(os, 'close', side_effect=maybe_fail_close):
            try:
                res = donor.donate_from_fixed_profile(
                    profile_path=str(self.profile_path),
                    uid_getter=lambda: 0,
                    euid_getter=lambda: 0,
                    pw_lookup=lambda _u: pw,
                    runner=runner_fn or default_runner,
                    proc_reader=proc_fn or default_proc,
                    pidfd_opener=pidfd_factory or default_pidfd,
                    boot_id_reader=boot_fn or (lambda: 'boot-1'),
                    monotonic_ns=clock_fn or (lambda: self.clock_ns),
                    endpoint_connector=lambda _p, _u, _d, _m: connector_sock,
                    peer_cred_getter=lambda _s: (peer_pid, peer_uid, 1000),
                    fanotify_opener=fake_fan,
                    gate_binder=fake_gate,
                    ledger_reader=fake_ledger_read,
                    ledger_writer=fake_ledger_write,
                )
                return res, received_bundle, ledger_store['current']
            finally:
                try:
                    donor_sock.close()
                except OSError:
                    pass
                try:
                    user_sock.close()
                except OSError:
                    pass
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive(), 'fixture peer thread lingered')

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
        res, bundle, ledger = self._run_donation(eagain_send=True)
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

    def test_incarnation_guards_reject_missing_pidfd_dead_pid_and_reused_pid(self):
        with self.assertRaisesRegex(donor.DonorError, 'invalid_pidfd'):
            self._run_donation(pidfd_factory=lambda _pid: -1)
        # Readable pidfd (pipe with data) simulates an exited MainPID.
        r_dead, w_dead = os.pipe()
        os.write(w_dead, b'x')
        os.close(w_dead)
        with self.assertRaisesRegex(donor.DonorError, 'main_pid_exited'):
            self._run_donation(pidfd_factory=lambda _pid: r_dead)
        # Wrong unit cgroup membership rejected.
        with self.assertRaisesRegex(donor.DonorError, 'proc_unit_cgroup_mismatch'):
            self._run_donation(proc_fn=lambda _p: ('4242 (x) S ' + '0 ' * 18 + '123 0', 'Uid:\t1000\t1000\t1000\t1000\n', '0::/user.slice/other.service\n'))
        # InvocationID changing between initial query and pre-send recheck is rejected.
        calls = {'n': 0}
        def changing_runner(*_a, **_k):
            calls['n'] += 1
            inv = 'inv-1' if calls['n'] == 1 else 'inv-2'
            return types.SimpleNamespace(
                returncode=0,
                stdout=f'MainPID=4242\nActiveState=active\nSubState=running\nInvocationID={inv}\nExecMainStartTimestampMonotonic=555\n',
            )
        with self.assertRaisesRegex(donor.DonorError, 'unit_incarnation_changed'):
            self._run_donation(runner_fn=changing_runner)
        # Boot ID changing post-ACK is rejected.
        boots = {'n': 0}
        def changing_boot():
            boots['n'] += 1
            return 'boot-1' if boots['n'] <= 2 else 'boot-2'
        with self.assertRaisesRegex(donor.DonorError, 'proc_incarnation_changed'):
            self._run_donation(boot_fn=changing_boot)

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

    def test_deadline_enforcement_on_connect_backlog_and_post_ack(self):
        # Non-blocking connect to full backlog (listen(0) with queued clients) hits deadline.
        dir_fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.addCleanup(lambda: os.close(dir_fd))
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(srv.close)
        sock_path = self.root / 'b.sock'
        srv.bind(f'/proc/self/fd/{dir_fd}/b.sock')
        srv.listen(0)
        for _ in range(4):
            cx = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.addCleanup(cx.close)
            cx.setblocking(False)
            try:
                cx.connect(f'/proc/self/fd/{dir_fd}/b.sock')
            except OSError:
                pass
        tick = {'v': 1_000_000_000}
        def advancing_clock():
            tick['v'] += 100_000_000
            return tick['v']
        with patch.object(donor, '_walk_dir', return_value=os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)):
            with self.assertRaisesRegex(donor.DonorError, 'handoff_deadline_expired|handoff_wait_timeout|connect_failed'):
                donor._connect_user_endpoint(str(sock_path), os.getuid(), 1_050_000_000, advancing_clock)
        # Expiry during pin/ledger/post-ACK rejects rather than returning DONATED.
        steps = {'n': 0}
        def expire_late():
            steps['n'] += 1
            return self.clock_ns if steps['n'] < 15 else self.clock_ns + 10_000_000_000
        with self.assertRaisesRegex(donor.DonorError, 'handoff_deadline_expired'):
            self._run_donation(clock_fn=expire_late)

    def test_missing_ack_partial_send_and_uncertain_close_never_claim_clean(self):
        with self.assertRaisesRegex(donor.DonorError, 'ack_eof_before_confirmation'):
            self._run_donation(send_ack=b'')
        with self.assertRaisesRegex(donor.DonorError, 'invalid_ack_payload'):
            self._run_donation(send_ack=b'NOPE')
        with self.assertRaisesRegex(OSError, 'simulated_send_failure'):
            self._run_donation(send_fail=True)
        with self.assertRaisesRegex(donor.DonorError, 'cleanup_uncertain'):
            self._run_donation(close_fail_once=True)

    def test_walk_dir_drops_old_fd_before_close_and_never_retries_on_error(self):
        closed_fds = []
        real_close = os.close
        first = {'done': False}
        def fail_first_close(fd):
            closed_fds.append(fd)
            if not first['done']:
                first['done'] = True
                real_close(fd)
                raise OSError(errno.EIO, 'consumed_then_raised')
            real_close(fd)
        sub = self.root / 'a/b'
        sub.mkdir(parents=True)
        with patch.object(os, 'close', side_effect=fail_first_close):
            with self.assertRaisesRegex(OSError, 'consumed_then_raised'):
                donor._walk_dir(str(sub), os.getuid())
        self.assertEqual(len(closed_fds), len(set(closed_fds)), 'consumed FD must never be retried')

    def test_existing_foreign_gate_is_preserved_not_unlinked(self):
        (self.gate_dir / 'source-gate.sock').write_text('foreign')
        with self.assertRaisesRegex(donor.DonorError, 'existing_gate_node_preserved'):
            donor.bind_source_gate(str(self.gate_dir), os.getuid())
        self.assertTrue((self.gate_dir / 'source-gate.sock').exists())


if __name__ == '__main__':
    unittest.main()
