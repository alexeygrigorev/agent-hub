"""Real backup/helper/broker compatibility with fake mail/remote commands only."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid
import subprocess

from agent_hub_host import alert_broker as b

r = b.ram

ROOT = Path(__file__).resolve().parents[3]
SOURCE_SCRIPTS = ROOT / 'scripts'
SCRIPT_NAMES = ('hub_alert_client.py', 'hub2-backup-pull.sh', 'hub-db-backup-staleness.sh')
EXPECTED_HELPER_SHA = hashlib.sha256((SOURCE_SCRIPTS / 'hub_alert_client.py').read_bytes()).hexdigest()



class FakeManager:
    def __init__(self, callback):
        self.callback = callback
        self.closed = False
        self.calls = []
        self.wire = self

    def start(self):
        self.calls.append('start')

    def receive(self, *_):
        return False

    def handle(self, *_):
        raise AssertionError('no fake messages')

    def barrier(self):
        self.calls.append('barrier')

    def close(self):
        self.closed = True


MTA_SCRIPT = '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['MAIL_LOG'], 'a') as f:
    f.write(json.dumps({'tool': os.path.basename(sys.argv[0]), 'argv': sys.argv[1:], 'body': sys.stdin.read()}) + '\\n')
'''


class RealBackupBrokerCompatTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='br-', dir=os.environ.get('RUNNER_TEMP') or None))
        self._owned_brokers = []
        self._owned_threads = []
        self.addCleanup(self._cleanup_fixture)
        self._broker_seq = 0
        self.scripts = self.root / 'scripts'
        self.scripts.mkdir(mode=0o700)
        for name in SCRIPT_NAMES:
            target = self.scripts / name
            target.write_bytes((SOURCE_SCRIPTS / name).read_bytes())
            # Only fixture files change mode; source checkout is read-only.
            target.chmod(0o644 if name.endswith('.py') else 0o700)
        self.bin = self.root / 'bin'
        self.bin.mkdir(mode=0o700)
        for name in ('sendmail', 'msmtp'):
            p = self.bin / name
            p.write_text(MTA_SCRIPT)
            p.chmod(0o700)
        for name in ('ssh', 'rsync', 'quota'):
            p = self.bin / name
            p.write_text(
                f'#!/bin/sh\nprintf "%s\\n" "{name}" >> "$BUSINESS_LOG"\n'
                f'exit "${{FAKE_{name.upper()}_RC:-0}}"\n'
            )
            p.chmod(0o700)

    def _cleanup_fixture(self):
        # A bounded close is not proof of thread death. Never remove a fixture
        # under a still-live owner; retain it and fail with the owned path.
        threads = list(self._owned_threads)
        threads.extend(broker.thread for broker in self._owned_brokers if broker.thread is not None)
        if any(thread.is_alive() for thread in threads):
            self.fail(f'fixture retained for unresolved owned thread: {self.root}')
        shutil.rmtree(self.root)

    def _start_broker(self):
        sock_dir = self.root / f's{self._broker_seq}'
        self._broker_seq += 1
        sock_dir.mkdir(mode=0o700)
        sock_path = sock_dir / 'observer.sock'
        self.assertLessEqual(len(os.fsencode(str(sock_path))), 107)
        pins = {role: r.SourcePin(1, 2, 'a' * 64) for role in r.ROLES}
        broker = b.Broker(sock_dir, pins, collector_factory=FakeManager)
        self._owned_brokers.append(broker)
        self.addCleanup(self._close_broker, broker)
        broker.start()
        deadline = time.monotonic() + 2.0
        while not (sock_path.exists() and broker.collector is not None):
            self.assertFalse(broker.closed, "fake-manager broker closed during startup")
            self.assertLess(time.monotonic(), deadline, 'broker failed to bind observer.sock')
            time.sleep(0.01)
        return broker, sock_path

    def _close_broker(self, broker):
        broker.close()
        if broker.thread is not None:
            broker.thread.join(timeout=1.0)
            self.assertFalse(broker.thread.is_alive(), 'owned broker thread remains; fixture retained')

    def _run_script(self, *, role='notifier', fresh=False, state=None, args=(),
                    socket_path=None, client_sha=EXPECTED_HELPER_SHA,
                    scripts_dir=None, extra_env=None):
        run = self.root / f'run-{len(list(self.root.iterdir()))}'
        run.mkdir(mode=0o700)
        home = run / 'home'
        home.mkdir(mode=0o700)
        tmpdir = run / 'tmp'
        tmpdir.mkdir(mode=0o700)
        for d in ('local', 'box', 'state'):
            (run / d).mkdir(mode=0o700)
        if fresh:
            (run / 'local' / 'hub2-db-fixture.tar.gz').write_bytes(b'fixture')
        if state is not None:
            (run / 'state' / 'staleness.state').write_text(state)
        for name in ('sendmail', 'msmtp', 'ssh', 'rsync', 'quota'):
            self.assertTrue(os.access(self.bin / name, os.X_OK))

        sdir = scripts_dir or self.scripts
        env = {
            'PATH': f'{self.bin}:/usr/bin:/bin',
            'HOME': str(home),
            'TMPDIR': str(tmpdir),
            'HUB_DB_BACKUP_ENV_FILE': '/dev/null',
            'BACKUP_DIR': str(run / 'local'),
            'STORAGEBOX_DIR': str(run / 'box'),
            'STATE_DIR': str(run / 'state'),
            'BACKUP_GLOB': 'hub2-db-*.tar.gz',
            'ALERT_TO': 'dev@dtcdev.click',
            'ALERT_OPS_TO': 'dev@dtcdev.click',
            'MAIL_LOG': str(run / 'mail.jsonl'),
            'BUSINESS_LOG': str(run / 'business.log'),
        }
        if socket_path is not None:
            env['HUB_BACKUP_ALERT_BROKER_SOCKET'] = str(socket_path)
        if client_sha is not None:
            env['HUB_BACKUP_ALERT_CLIENT_SHA256'] = client_sha
        if role == 'pull':
            pull_env = run / 'pull.env'
            pull_env.write_text('\n'.join([
                'VM_HOST=fixture.invalid',
                'SSH_KEY=/fixture-not-a-key',
                f'BACKUP_DIR={run / "local"}',
                f'STORAGEBOX_DIR={run / "box"}',
                f'STATE_DIR={run / "state"}',
                f'STALENESS_SCRIPT={sdir / "hub-db-backup-staleness.sh"}',
            ]) + '\n')
            env['HUB2_BACKUP_PULL_ENV_FILE'] = str(pull_env)
        if extra_env:
            env.update(extra_env)

        for name in ('sendmail', 'msmtp', 'ssh', 'rsync', 'quota'):
            self.assertEqual(shutil.which(name, path=env['PATH']), str(self.bin / name))
        self.assertEqual(env['HUB_DB_BACKUP_ENV_FILE'], '/dev/null')
        for name in ('BACKUP_DIR', 'STORAGEBOX_DIR', 'STATE_DIR'):
            self.assertTrue(Path(env[name]).is_relative_to(self.root))
        script_name = 'hub2-backup-pull.sh' if role == 'pull' else 'hub-db-backup-staleness.sh'
        t0 = time.monotonic()
        res = subprocess.run(
            ['/bin/bash', str(sdir / script_name), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        elapsed = time.monotonic() - t0
        mail_path = run / 'mail.jsonl'
        biz_path = run / 'business.log'
        return {
            'rc': res.returncode,
            'out': res.stdout,
            'err': res.stderr,
            'elapsed': elapsed,
            'mail': [json.loads(line) for line in mail_path.read_text().splitlines()] if mail_path.exists() else [],
            'business': biz_path.read_text().splitlines() if biz_path.exists() else [],
        }

    def _assert_zero_authority(self, broker):
        snap = broker.request({'v': 1, 'op': 'snapshot', 'nonce': 'a' * 32}, (os.getpid(), os.getuid(), os.getgid()))
        self.assertIs(snap['complete'], False)
        self.assertIs(snap['ram_consistent'], False)
        for role_counts in broker.ram.counts.values():
            self.assertEqual(role_counts['started'], 0)
            self.assertEqual(role_counts['finished'], 0)
            self.assertEqual(role_counts['occurrences'], 0)
        self.assertEqual(broker.ram.active, {})

    def test_01_fixture_is_exact_source_copy_with_safe_fixture_only_modes(self):
        for name in SCRIPT_NAMES:
            self.assertEqual((self.scripts / name).read_bytes(), (SOURCE_SCRIPTS / name).read_bytes())
            self.assertEqual((self.scripts / name).stat().st_mode & 0o777,
                             0o644 if name.endswith('.py') else 0o700)
        self.assertEqual(Path(b.__file__).resolve(),
                         ROOT / 'hub2/host/agent_hub_host/alert_broker.py')

    def test_02_staleness_real_helper_and_broker_cases(self):
        cases = [
            ({}, 'alert', 1, 2),
            ({'fresh': True}, 'observation', 0, 0),
            ({'args': ['--backup-failed', 'fixture']}, 'alert', 1, 0),
            ({'args': ['--backup-failed', 'fixture', '--no-mail']}, 'alert', 0, 0),
            ({'extra_env': {'HUB_DB_STALE_DRILL': '1'}}, 'observation', 0, 2),
        ]
        for opts, expected_decision, expected_mails, expected_rc in cases:
            with self.subTest(opts=opts):
                broker, sock = self._start_broker()
                res = self._run_script(role='notifier', socket_path=sock, **opts)
                self.assertEqual((res['rc'], len(res['mail'])), (expected_rc, expected_mails), res)
                self.assertEqual(len(broker.unqualified_frames), 3, broker.unqualified_frames)
                frames = [rec['frame'] for rec in broker.unqualified_frames]
                self.assertEqual([f['phase'] for f in frames], ['begin', 'decision', 'terminal'])
                self.assertEqual([f['seq'] for f in frames], [0, 1, 2])
                self.assertEqual(frames[1]['decision'], expected_decision)
                self.assertEqual([f['observer_ok'] for f in frames], [True, False, False])
                ids = {f['invocation_id'] for f in frames}
                self.assertEqual(len(ids), 1)
                cid = next(iter(ids))
                self.assertIsNotNone(re.fullmatch(r'[0-9a-f]{32}', cid))
                self.assertEqual(uuid.UUID(hex=cid).version, 4)
                self.assertTrue(all(f['parent_id'] is None and f['role'] == 'backup_notifier' for f in frames))
                peers = {(rec['peer_process']['pid'], rec['peer_process']['birth_ticks'], rec['peer_process']['boot_id'])
                         for rec in broker.unqualified_frames}
                self.assertEqual(len(peers), 1)
                self.assertTrue(all(rec['qualification'] == 'none' for rec in broker.unqualified_frames))
                self._assert_zero_authority(broker)

    def test_03_pull_nested_real_helper_and_broker_success(self):
        broker, sock = self._start_broker()
        res = self._run_script(role='pull', fresh=True, socket_path=sock)
        self.assertEqual((res['rc'], len(res['mail']), res['business']), (0, 0, ['ssh', 'rsync', 'rsync']), res)
        self.assertEqual(len(broker.unqualified_frames), 6, broker.unqualified_frames)

        pull_recs = [rec for rec in broker.unqualified_frames if rec['frame']['role'] == 'backup_pull']
        notif_recs = [rec for rec in broker.unqualified_frames if rec['frame']['role'] == 'backup_notifier']
        self.assertEqual(len(pull_recs), 3)
        self.assertEqual(len(notif_recs), 3)

        pull_ids = {rec['frame']['invocation_id'] for rec in pull_recs}
        notif_ids = {rec['frame']['invocation_id'] for rec in notif_recs}
        self.assertEqual(len(pull_ids), 1)
        self.assertEqual(len(notif_ids), 1)
        pull_id = next(iter(pull_ids))
        notif_id = next(iter(notif_ids))
        self.assertNotEqual(pull_id, notif_id)
        self.assertIsNotNone(re.fullmatch(r'[0-9a-f]{32}', pull_id))
        self.assertIsNotNone(re.fullmatch(r'[0-9a-f]{32}', notif_id))

        self.assertTrue(all(rec['frame']['parent_id'] is None for rec in pull_recs))
        self.assertTrue(all(rec['frame']['parent_id'] == pull_id for rec in notif_recs))

        pull_pids = {rec['peer_process']['pid'] for rec in pull_recs}
        notif_pids = {rec['peer_process']['pid'] for rec in notif_recs}
        self.assertEqual(len(pull_pids), 1)
        self.assertEqual(len(notif_pids), 1)
        self.assertNotEqual(pull_pids, notif_pids)

        self.assertEqual([rec['frame']['phase'] for rec in pull_recs], ['begin', 'decision', 'terminal'])
        self.assertEqual(pull_recs[1]['frame']['condition'], 'pull_terminal')
        self.assertTrue(pull_recs[2]['frame']['flags'].get('exit_success'))
        self.assertEqual([rec['frame']['phase'] for rec in notif_recs], ['begin', 'decision', 'terminal'])
        self.assertEqual(notif_recs[1]['frame']['decision'], 'observation')
        self._assert_zero_authority(broker)

    def test_04_pull_nested_real_helper_and_broker_ssh_failure(self):
        broker, sock = self._start_broker()
        res = self._run_script(role='pull', socket_path=sock, extra_env={'FAKE_SSH_RC': '23'})
        self.assertEqual((res['rc'], len(res['mail']), res['business']), (23, 1, ['ssh']), res)
        self.assertEqual(len(broker.unqualified_frames), 6, broker.unqualified_frames)

        pull_recs = [rec for rec in broker.unqualified_frames if rec['frame']['role'] == 'backup_pull']
        notif_recs = [rec for rec in broker.unqualified_frames if rec['frame']['role'] == 'backup_notifier']
        self.assertEqual(len(pull_recs), 3)
        self.assertEqual(len(notif_recs), 3)

        pull_id = pull_recs[0]['frame']['invocation_id']
        notif_id = notif_recs[0]['frame']['invocation_id']
        self.assertNotEqual(pull_id, notif_id)
        self.assertTrue(all(rec['frame']['parent_id'] == pull_id for rec in notif_recs))
        self.assertNotEqual(pull_recs[0]['peer_process']['pid'], notif_recs[0]['peer_process']['pid'])

        self.assertIs(pull_recs[2]['frame']['flags'].get('exit_success'), False)
        self.assertEqual(notif_recs[1]['frame']['decision'], 'alert')
        self.assertEqual(notif_recs[1]['frame']['condition'], 'backup_failed')
        self._assert_zero_authority(broker)

    def test_05_missing_socket_bad_sha_and_unsafe_leaf(self):
        # Missing socket
        res = self._run_script(role='notifier', args=['--backup-failed', 'missing-sock'],
                               socket_path=self.root / 'absent.sock')
        self.assertEqual((res['rc'], len(res['mail'])), (0, 1), res)

        # Bad SHA
        broker, sock = self._start_broker()
        res = self._run_script(role='notifier', args=['--backup-failed', 'bad-sha'],
                               socket_path=sock, client_sha='0' * 64)
        self.assertEqual((res['rc'], len(res['mail'])), (0, 1), res)
        self.assertEqual(len(broker.unqualified_frames), 0)

        # Missing SHA
        res = self._run_script(role='notifier', args=['--backup-failed', 'no-sha'],
                               socket_path=sock, client_sha=None)
        self.assertEqual((res['rc'], len(res['mail'])), (0, 1), res)
        self.assertEqual(len(broker.unqualified_frames), 0)

        # Unsafe group-writable leaf in isolated copy
        bad_scripts = self.root / 'bad-scripts'
        bad_scripts.mkdir(mode=0o700)
        for name in ('hub-db-backup-staleness.sh', 'hub2-backup-pull.sh'):
            p = bad_scripts / name
            p.write_bytes((SOURCE_SCRIPTS / name).read_bytes())
            p.chmod(0o700)
        c = bad_scripts / 'hub_alert_client.py'
        c.write_bytes((SOURCE_SCRIPTS / 'hub_alert_client.py').read_bytes())
        c.chmod(0o620)
        res = self._run_script(role='notifier', args=['--backup-failed', 'unsafe-leaf'],
                               socket_path=sock, scripts_dir=bad_scripts)
        self.assertEqual((res['rc'], len(res['mail'])), (0, 1), res)
        self.assertEqual(len(broker.unqualified_frames), 0)

    def _start_slow_listener(self):
        sock_dir = self.root / 'slow'
        sock_dir.mkdir(mode=0o700)
        sock_path = sock_dir / 'observer.sock'
        stop = threading.Event()
        thread = None
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)

        def close():
            stop.set()
            if thread is not None and thread.ident is not None:
                thread.join(timeout=1.0)
                self.assertFalse(thread.is_alive(), 'owned slow-listener thread remains; fixture retained')
            srv.close()

        self.addCleanup(close)
        srv.bind(str(sock_path))
        srv.listen(4)

        def serve():
            while not stop.is_set():
                try:
                    srv.settimeout(0.05)
                    conn, _ = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with conn:
                    time.sleep(0.25)

        thread = threading.Thread(target=serve, daemon=True)
        self._owned_threads.append(thread)
        thread.start()
        return sock_path

    def test_06_slow_timeout_socket_preserves_business_once(self):
        sock_path = self._start_slow_listener()
        res = self._run_script(role='notifier', args=['--backup-failed', 'slow-broker'],
                               socket_path=sock_path)
        self.assertEqual((res['rc'], len(res['mail'])), (0, 1), res)
        self.assertLess(res['elapsed'], 2.0)

    def test_07_broker_start_failure_closes_before_fixture_removal(self):
        case = type(self)('test_01_fixture_is_exact_source_copy_with_safe_fixture_only_modes')
        case.setUp()
        self.addCleanup(case.doCleanups)
        closed = []

        class FailedBroker:
            thread = None
            def __init__(self, *_args, **_kwargs):
                pass
            def start(self):
                raise RuntimeError('injected startup failure')
            def close(self):
                closed.append(case.root.exists())

        with mock.patch.object(b, 'Broker', FailedBroker):
            with self.assertRaisesRegex(RuntimeError, 'injected startup failure'):
                case._start_broker()
        self.assertTrue(case.doCleanups())
        self.assertEqual(closed, [True])
        self.assertFalse(case.root.exists())

    def test_08_socket_bind_failure_closes_before_fixture_removal(self):
        case = type(self)('test_01_fixture_is_exact_source_copy_with_safe_fixture_only_modes')
        case.setUp()
        self.addCleanup(case.doCleanups)
        closed = []

        class FailedSocket:
            def bind(self, _path):
                raise OSError('injected bind failure')
            def close(self):
                closed.append(case.root.exists())

        with mock.patch.object(socket, 'socket', return_value=FailedSocket()):
            with self.assertRaisesRegex(OSError, 'injected bind failure'):
                case._start_slow_listener()
        self.assertTrue(case.doCleanups())
        self.assertEqual(closed, [True])
        self.assertFalse(case.root.exists())

    def test_09_live_thread_retains_fixture_and_reports_failure(self):
        case = type(self)('test_01_fixture_is_exact_source_copy_with_safe_fixture_only_modes')
        case.setUp()
        self.addCleanup(case.doCleanups)
        thread = mock.Mock()
        thread.is_alive.return_value = True
        case._owned_threads.append(thread)
        try:
            with self.assertRaisesRegex(AssertionError, 'fixture retained'):
                case._cleanup_fixture()
            self.assertTrue(case.root.exists())
        finally:
            case._owned_threads.clear()


if __name__ == '__main__':
    unittest.main(verbosity=2)
