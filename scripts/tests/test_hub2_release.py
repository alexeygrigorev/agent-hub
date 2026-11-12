"""Targeted release safety checks; no network, live service or live venv writes."""
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import unittest
import zipfile
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), REPO / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


updater = module('hub2-self-update')
builder = module('build-hub2-release')
enrollment = module('hub2-enroll')
SHA = 'a' * 40
OLD_SHA = 'b' * 40
MACHINE = 'release-test-machine'
STATUS_PATH = '/api/fleet/routing/self'
HOST = f'agent_hub_host_v2-2.0.0+g{SHA}-py3-none-any.whl'


class ReleaseTests(unittest.TestCase):
    def test_native_self_update_bypasses_config_and_propagates_exit(self):
        code = '''
from types import SimpleNamespace
from unittest.mock import patch
from agent_hub_host.cli import main
with patch('subprocess.run', return_value=SimpleNamespace(returncode=7)) as run:
    assert main(['--config','/missing/config','self-update','--root','/isolated/root','--no-restart']) == 7
    args = run.call_args.args[0]
    assert args[1] == '/isolated/root/updater.py' and '--no-restart' in args
'''
        subprocess.run([sys.executable, '-c', code], check=True,
                       env=dict(os.environ, PYTHONPATH=str(REPO / 'hub2/host')),
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def setUp(self):
        (REPO / '.local/tmp').mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPO / '.local/tmp', prefix='test-release-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'host'
        self.files = {name: ('artifact:' + name).encode() for name in
                      (HOST, 'websockets-16.0-py3-none-any.whl', 'hub.pyz',
                       'hub2-self-update.py', 'hub2-enroll.py', 'host-install.sh', *updater.UNITS)}
        self.files['manifest.json'] = json.dumps({'built_at_ms': 1000}).encode()
        self.manifest = {'schema_version': 1, 'protocol_version': 1, 'host_version': SHA,
                         'python_requires': '>=3.11',
                         'artifacts': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                                       for name, data in self.files.items()}}
        self.calls = []
        self.downloads = []
        self.config = Path(self.temporary.name) / '.config/agent-hub/v2.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'machine_id': MACHINE, 'token': 'synthetic-enrollment-token'}))
        self.status = {'machine_id': MACHINE, 'connected': True, 'host_version': SHA,
                       'protocol_version': 1, 'version_mismatch': False}
        self.status_rc = 0
        self.status_output = None
        self.status_timeout = False

    def fetch(self, url, limit):
        name = url.rsplit('/', 1)[1]
        self.downloads.append(name)
        return json.dumps(self.manifest).encode() if name == 'host-release.json' else self.files[name]

    def command(self, args):
        self.calls.append([str(value) for value in args])

    def status_command(self, args, *, stdout, stderr, timeout):
        self.calls.append(args)
        self.assertEqual(args, [str(self.root / 'releases' / SHA / 'bin/hub2'), '--config', str(self.config),
                                'request', 'GET', STATUS_PATH])
        self.assertEqual(stdout, subprocess.PIPE)
        self.assertEqual(stderr, subprocess.DEVNULL)
        self.assertEqual(timeout, 15)
        if self.status_timeout:
            raise subprocess.TimeoutExpired(args, timeout, output=b'synthetic-sensitive-output')
        raw = json.dumps(self.status).encode() if self.status_output is None else self.status_output
        return subprocess.CompletedProcess(args, self.status_rc, stdout=raw)

    def old_release(self):
        old = self.root / 'releases' / OLD_SHA
        old.mkdir(parents=True)
        (self.root / 'current').symlink_to(old)
        return old

    def install(self, **kwargs):
        with patch.object(updater, 'fetch', self.fetch), patch.object(updater, 'command', self.command), \
             patch.object(updater.time, 'sleep'), patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.subprocess, 'run', self.status_command):
            updater.install('https://example.invalid', self.root, **kwargs)

    def policy(self, last=1, pending=None):
        self.root.mkdir(exist_ok=True)
        updater.write_json(self.root / updater.ACTIVATION_RECEIPT,
                           {'last_activation': last, 'pending_since': pending})

    def receipt(self):
        return json.loads((self.root / updater.ACTIVATION_RECEIPT).read_text())

    def test_scheduled_batch_defers_without_downloading_or_restarting(self):
        old = self.old_release()
        self.policy(last=1000)
        with patch.object(updater.time, 'time', return_value=1001):
            self.install(scheduled=True)
        self.assertEqual(self.downloads, ['host-release.json'])
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse(any('restart' in args for args in self.calls))
        self.assertEqual(self.receipt()['reason'], 'release_batch_interval')
        self.assertEqual(self.receipt()['next_check_at'], 1000 + updater.ACTIVATION_INTERVAL)
        self.assertEqual((self.root / updater.ACTIVATION_RECEIPT).stat().st_mode & 0o777, 0o600)

    def test_busy_deferral_preserves_deadline_across_newer_releases(self):
        old = self.old_release()
        self.policy()
        start = updater.ACTIVATION_INTERVAL + 1
        with patch.object(updater, 'managed_session_count', return_value=3), \
             patch.object(updater.time, 'time', return_value=start):
            self.install(scheduled=True)
        self.assertEqual(self.receipt()['reason'], 'managed_sessions_running')
        self.assertEqual(self.receipt()['managed_sessions'], 3)
        with patch.object(updater, 'managed_session_count', return_value=2), \
             patch.object(updater.time, 'time', return_value=start + 100):
            self.assertTrue(updater.defer_activation(self.root, self.root / 'current', 'c' * 40))
        self.assertEqual(self.receipt()['pending_since'], start)
        self.assertEqual(self.receipt()['version'], 'c' * 40)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(self.downloads, ['host-release.json'])

    def test_busy_wait_expires_and_success_starts_new_batch_interval(self):
        self.old_release()
        self.policy(pending=updater.ACTIVATION_INTERVAL + 1)
        now = updater.ACTIVATION_INTERVAL + 1 + updater.MAX_BUSY_WAIT
        reasons = []
        original = updater.write_json
        def capture(path, value):
            if path.name == updater.ACTIVATION_RECEIPT:
                reasons.append(value['reason'])
            return original(path, value)
        with patch.object(updater, 'managed_session_count', return_value=4), \
             patch.object(updater.time, 'time', return_value=now), \
             patch.object(updater, 'write_json', side_effect=capture):
            self.install(scheduled=True)
        self.assertIn('bounded_wait_expired', reasons)
        self.assertEqual(self.receipt()['last_activation'], now)
        self.assertIsNone(self.receipt()['pending_since'])
        self.assertEqual(sum('restart' in args for args in self.calls), 1)
        with patch.object(updater.time, 'time', return_value=now + 1):
            self.assertTrue(updater.defer_activation(self.root, self.root / 'current', 'd' * 40))

    def test_idle_host_activates_without_wait_after_batch_interval(self):
        self.old_release()
        self.policy()
        with patch.object(updater, 'managed_session_count', return_value=0), \
             patch.object(updater.time, 'time', return_value=updater.ACTIVATION_INTERVAL + 1):
            self.install(scheduled=True)
        self.assertEqual(self.receipt()['decision'], 'activated')
        self.assertEqual(sum('restart' in args for args in self.calls), 1)

    def test_session_starting_during_preparation_defers_before_switch(self):
        old = self.old_release()
        self.policy()
        with patch.object(updater, 'managed_session_count', side_effect=[0, 1]), \
             patch.object(updater.time, 'time', return_value=updater.ACTIVATION_INTERVAL + 1):
            self.install(scheduled=True)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertFalse(any('restart' in args for args in self.calls))
        self.assertEqual(self.receipt()['reason'], 'managed_sessions_running')

    def test_unknown_sessions_defer_but_do_not_block_forever(self):
        self.old_release()
        self.policy()
        start = updater.ACTIVATION_INTERVAL + 1
        with patch.object(updater, 'managed_session_count', return_value=None), \
             patch.object(updater.time, 'time', return_value=start):
            self.assertTrue(updater.defer_activation(self.root, self.root / 'current', SHA))
        self.assertEqual(self.receipt()['reason'], 'managed_sessions_unknown')
        with patch.object(updater, 'managed_session_count', return_value=None), \
             patch.object(updater.time, 'time', return_value=start + updater.MAX_BUSY_WAIT):
            self.assertFalse(updater.defer_activation(self.root, self.root / 'current', SHA))
        self.assertEqual(self.receipt()['reason'], 'bounded_wait_expired')

    def test_first_policy_uses_current_link_activation_time(self):
        self.old_release()
        activated = (self.root / 'current').lstat().st_mtime
        with patch.object(updater.time, 'time', return_value=activated + 1):
            self.assertTrue(updater.defer_activation(self.root, self.root / 'current', SHA))
        self.assertEqual(self.receipt()['last_activation'], activated)

    def test_managed_session_journal_is_read_only_and_conservative(self):
        import sqlite3
        journal = self.config.parent / 'v2-journal.sqlite'
        with patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            self.assertIsNone(updater.managed_session_count())
            self.assertFalse(journal.exists())
            with sqlite3.connect(journal) as db:
                db.execute('CREATE TABLE launches(state TEXT)')
                states = ['running', 'launching', 'pending_admission', 'aborting',
                          'deadline_stopping', 'launch_failed_unconfirmed', 'exited', 'stopped_before_dispatch',
                          'launch_failed_reported']
                db.executemany('INSERT INTO launches VALUES (?)', [(state,) for state in states])
            self.assertEqual(updater.managed_session_count(), 6)
            with sqlite3.connect(journal) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM launches').fetchone()[0], 9)

    def test_invalid_policy_fails_before_activation(self):
        self.old_release()
        self.policy(last=float('nan'))
        with self.assertRaisesRegex(ValueError, 'policy timestamp'):
            self.install(scheduled=True)
        self.assertEqual(self.downloads, ['host-release.json'])

    def test_valid_update_is_offline_and_keeps_previous(self):
        old = self.old_release()
        self.install()
        self.assertEqual((self.root / 'previous').resolve(), old)
        self.assertEqual((self.root / 'current').resolve().name, SHA)
        self.assertTrue(old.exists())
        pip = next(args for args in self.calls if 'pip' in args)
        self.assertIn('--no-index', pip)
        self.assertIn('--no-deps', pip)
        self.assertFalse(any('systemctl' in args and 'stop' in args for args in self.calls))
        self.assertEqual(os.readlink(self.root / 'current/bin/hub'), '../hub.pyz')

    def test_permissive_umask_install_and_same_version_reuse_keep_private_receipt(self):
        previous = os.umask(0o002)
        try:
            self.install(install_service=True, restart=False)
            target = (self.root / 'current').resolve()
            receipt = target / 'release.json'
            snapshot = target / 'units-before.json'
            original, inode = receipt.read_bytes(), receipt.stat().st_ino
            self.assertEqual(json.loads(original), self.manifest)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.calls.clear()
            self.install(restart=False)
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_ino), (original, inode))
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.calls, [])
        finally:
            os.umask(previous)

    def test_same_version_does_not_implicitly_mutate_legacy_receipt(self):
        self.install(restart=False)
        receipt = (self.root / 'current').resolve() / 'release.json'
        receipt.chmod(0o664)  # Owned synthetic legacy receipt; no live release.
        original, inode = receipt.read_bytes(), receipt.stat().st_ino
        self.calls.clear()
        self.install(restart=False)
        self.assertEqual((receipt.read_bytes(), receipt.stat().st_ino), (original, inode))
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o664)
        self.assertEqual(self.calls, [])

    def test_private_json_write_does_not_follow_old_fixed_temporary_links(self):
        previous = os.umask(0o002)
        try:
            self.root.mkdir()
            for kind in ('symlink', 'hardlink'):
                with self.subTest(kind=kind):
                    foreign = self.root / (kind + '-foreign')
                    foreign.write_bytes(b'synthetic unrelated bytes')
                    receipt = self.root / (kind + '.json')
                    legacy_temp = receipt.with_name(receipt.name + '.tmp')
                    if kind == 'symlink':
                        legacy_temp.symlink_to(foreign)
                    else:
                        os.link(foreign, legacy_temp)
                    original = foreign.read_bytes()
                    updater.write_json(receipt, {'synthetic': True})
                    self.assertEqual(json.loads(receipt.read_bytes()), {'synthetic': True})
                    self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(receipt.stat().st_nlink, 1)
                    self.assertEqual(foreign.read_bytes(), original)
                    self.assertTrue(legacy_temp.exists())
        finally:
            os.umask(previous)

    def test_retained_rebuild_and_cleanup_resume_preserve_private_receipt(self):
        previous = os.umask(0o002)
        try:
            _, retained, _ = self.prepare_retained_switch()
            receipt = retained / 'release.json'
            original = receipt.read_bytes()
            receipt.chmod(0o664)  # Rebuild verifies and replaces this owned fixture.
            with patch.object(updater, 'finish_retained_switch', side_effect=OSError('cleanup interrupted')):
                with self.assertRaisesRegex(OSError, 'cleanup interrupted'):
                    self.install()
            inode = receipt.stat().st_ino
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_mode & 0o777), (original, 0o600))
            self.calls.clear()
            self.install()
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_ino), (original, inode))
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertFalse((self.root / 'retained-switch-committed.json').exists())
            self.assertEqual(self.calls, [])
        finally:
            os.umask(previous)

    def test_same_version_repairs_wheel_operator_shim_without_restart(self):
        self.install()
        target = (self.root / 'current').resolve()
        entry = target / 'bin/hub'
        entry.unlink()
        entry.write_text('old wheel entry point')
        host_entry = target / 'bin/hub2'
        host_entry.write_text('host entry point')
        self.calls.clear()
        self.install()
        self.assertEqual(os.readlink(entry), '../hub.pyz')
        self.assertEqual(host_entry.read_text(), 'host entry point')
        self.assertFalse(any('systemctl' in args or 'pip' in args for args in self.calls))
        self.calls.clear()
        self.install()
        self.assertEqual(self.calls, [])

    def test_same_version_refuses_corrupt_zipapp_before_replacing_shim(self):
        self.install()
        target = (self.root / 'current').resolve()
        entry = target / 'bin/hub'
        entry.unlink()
        entry.write_text('old wheel entry point')
        (target / 'hub.pyz').write_bytes(b'corrupt')
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'installed release artifact mismatch: hub.pyz'):
            self.install()
        self.assertEqual(entry.read_text(), 'old wheel entry point')
        self.assertEqual(self.calls, [])

    def test_activated_shell_uses_operator_zipapp_with_host_package_present(self):
        target = self.root / 'releases' / SHA
        target.parent.mkdir(parents=True)
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(target)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Build the real operator CLI, then reproduce the host wheel's entry
        # points. PYTHONPATH supplies the actual host package to this venv.
        with patch('sys.stdout', new=io.StringIO()):
            module('build-hub-cli').build(target, '0.2.0')
        for name in ('hub', 'hub2'):
            entry = target / 'bin' / name
            entry.write_text(f'#!{target}/bin/python\nfrom agent_hub_host.cli import main\nraise SystemExit(main())\n')
            entry.chmod(0o755)
        data = (target / 'hub.pyz').read_bytes()
        manifest = {'artifacts': {'hub.pyz': {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}
        updater.install_operator_cli(target, manifest)
        (self.root / 'current').symlink_to(target)
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                payload = {'sessions': [], 'has_more': False} if self.path.startswith('/api/sessions') else {}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = Path(self.temporary.name) / 'operator.json'
            profile.write_text('{}')
            profile.chmod(0o600)
            env = dict(os.environ, HOME=self.temporary.name, HUB_CONFIG_FILE=str(profile),
                       HUB_URL=f'http://127.0.0.1:{server.server_port}', HUB_TOKEN='synthetic-operator-token',
                       PYTHONPATH=str(REPO / 'hub2/host'))
            result = subprocess.run(['/bin/sh', '-ec',
                                     '. "$1/bin/activate"; command -v hub; hub --version; hub sessions list; hub2 --help',
                                     'activated-shell', str(self.root / 'current')],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(target / 'bin/hub'), result.stdout)
            self.assertIn('hub 0.2.0', result.stdout)
            self.assertIn('usage: hub2', result.stdout)
            self.assertTrue(any(path.startswith('/api/sessions') and token == 'Bearer synthetic-operator-token'
                                for path, token in requests), requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def prepare_retained_switch(self):
        current = self.old_release()
        self.install(install_service=True)
        retained = (self.root / 'current').resolve()
        startup = retained / 'lib/python3.11/site-packages/retained.pth'
        startup.parent.mkdir(parents=True)
        startup.write_text('import unverified_retained_code')
        updater.link(current, self.root / 'current')
        updater.link(retained, self.root / 'previous')
        units = Path(self.temporary.name) / '.config/systemd/user'
        (units / updater.UNITS[0]).write_bytes(b'active service unit')
        (self.root / 'updater.py').write_bytes(b'current updater')
        self.calls.clear()
        self.downloads.clear()
        return current, retained, units

    def test_retained_preparation_rechecks_sessions_without_restart_on_deferral(self):
        current, retained, units = self.prepare_retained_switch()
        before = (retained / 'release.json').read_bytes()
        self.policy()
        with patch.object(updater, 'managed_session_count', side_effect=[0, 1]), \
             patch.object(updater.time, 'time', return_value=updater.ACTIVATION_INTERVAL + 1):
            self.install(scheduled=True)
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((retained / 'release.json').read_bytes(), before)
        self.assertTrue((retained / 'lib/python3.11/site-packages/retained.pth').is_file())
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')
        self.assertFalse(any('restart' in args or 'stop' in args for args in self.calls))
        self.assertFalse((self.root / 'retained-switch.json').exists())
        self.assertFalse(retained.with_name('.retained-' + retained.name).exists())
        self.assertFalse((self.root / 'failed-releases.json').exists())
        self.assertEqual(self.receipt()['reason'], 'managed_sessions_running')

    def test_rebuilds_retained_release_without_executing_it_or_replacing_updater(self):
        current, retained, units = self.prepare_retained_switch()
        receipt = (retained / 'release.json').read_bytes()
        startup = retained / 'lib/python3.11/site-packages/retained.pth'
        previous_command = self.command

        def assert_clean_command(args):
            if Path(args[0]).is_relative_to(retained):
                self.assertFalse(startup.exists())
            previous_command(args)

        self.command = assert_clean_command
        self.install()
        self.assertFalse(startup.exists())
        self.assertEqual((self.root / 'current').resolve(), retained)
        self.assertEqual((self.root / 'previous').resolve(), current)
        self.assertEqual((retained / 'release.json').read_bytes(), receipt)
        self.assertEqual((self.root / 'updater.py').read_bytes(), b'current updater')
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), self.files[updater.UNITS[0]])
        self.assertEqual(self.downloads, ['host-release.json', updater.UNITS[0], HOST,
                                          'websockets-16.0-py3-none-any.whl', 'hub.pyz', 'hub2-enroll.py',
                                          'host-release.json'])
        self.assertEqual(sum('restart' in args for args in self.calls), 1)
        self.assertTrue(any('venv' in args for args in self.calls))
        self.assertFalse((self.root / 'retained-switch.json').exists())
        self.assertFalse((self.root / 'retained-switch-committed.json').exists())
        self.assertFalse(retained.with_name('.retained-' + retained.name).exists())

    def test_retained_release_readiness_failure_restores_units_and_both_links(self):
        current, retained, units = self.prepare_retained_switch()
        self.status_rc = 1
        startup = retained / 'lib/python3.11/site-packages/retained.pth'
        previous_command = self.command

        def assert_quiesced_before_restore(args):
            if args[2:3] == ['stop']:
                self.assertFalse(startup.exists())
            previous_command(args)

        self.command = assert_quiesced_before_restore
        with self.assertRaisesRegex(RuntimeError, 'host daemon connection not verified'):
            self.install()
        self.assertIn(['systemctl', '--user', 'stop', updater.UNITS[0]], self.calls)
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')
        self.assertTrue((retained / 'lib/python3.11/site-packages/retained.pth').is_file())
        self.assertEqual(sum('restart' in args for args in self.calls), 2)
        self.assertFalse((self.root / 'retained-switch.json').exists())
        self.assertIn(SHA, json.loads((self.root / 'failed-releases.json').read_text()))

    def test_interrupted_retained_switch_recovers_even_when_manifest_is_unavailable(self):
        current, retained, units = self.prepare_retained_switch()
        journal = self.root / 'retained-switch.json'
        updater.write_json(journal, {'current': current.name, 'target': retained.name, 'previous': retained.name,
                                     'units': {name: (units / name).read_bytes().hex() for name in updater.UNITS},
                                     'switching': True})
        retained.rename(retained.with_name('.retained-' + retained.name))
        retained.mkdir()
        # The pointer was already restored, but the candidate may still run.
        updater.link(current, self.root / 'previous')
        (units / updater.UNITS[0]).write_bytes(b'interrupted service unit')

        def offline(*args):
            raise OSError('offline')

        self.fetch = offline
        with self.assertRaisesRegex(OSError, 'offline'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')
        self.assertFalse(journal.exists())
        self.assertIn(['systemctl', '--user', 'stop', updater.UNITS[0]], self.calls)
        self.assertTrue((retained / 'lib/python3.11/site-packages/retained.pth').is_file())

    def test_retained_build_failure_restores_original_environment(self):
        current, retained, units = self.prepare_retained_switch()
        with patch.object(updater, 'prepare_environment', side_effect=RuntimeError('venv build failed')):
            with self.assertRaisesRegex(RuntimeError, 'venv build failed'):
                self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')
        self.assertTrue((retained / 'lib/python3.11/site-packages/retained.pth').is_file())

    def test_committed_retained_switch_resumes_backup_cleanup_without_restarting(self):
        current, retained, _ = self.prepare_retained_switch()
        cleanup = updater.finish_retained_switch
        with patch.object(updater, 'finish_retained_switch', side_effect=OSError('cleanup interrupted')):
            with self.assertRaisesRegex(OSError, 'cleanup interrupted'):
                self.install()
        journal = self.root / 'retained-switch-committed.json'
        backup = retained.with_name('.retained-' + retained.name)
        self.assertTrue(journal.is_file())
        self.assertTrue(backup.is_dir())
        self.calls.clear()
        with patch.object(updater, 'finish_retained_switch', wraps=cleanup) as resumed:
            self.install()
        resumed.assert_called_once()
        self.assertEqual((self.root / 'current').resolve(), retained)
        self.assertEqual((self.root / 'previous').resolve(), current)
        self.assertFalse(backup.exists())
        self.assertFalse(journal.exists())
        self.assertEqual(self.calls, [])

    def test_resumed_cleanup_syncs_committed_decision_before_discarding_backup(self):
        _, retained, _ = self.prepare_retained_switch()
        journal = self.root / 'retained-switch-committed.json'
        backup = retained.with_name('.retained-' + retained.name)
        sync = updater.sync_directory

        def interrupt_sync(directory):
            if directory == self.root and journal.exists():
                self.assertTrue(backup.is_dir())
                raise OSError('interrupted commit sync')
            sync(directory)

        with patch.object(updater, 'sync_directory', side_effect=interrupt_sync):
            with self.assertRaisesRegex(OSError, 'interrupted commit sync'):
                self.install()
        self.assertTrue(journal.exists())
        self.assertTrue(backup.is_dir())
        observed = []

        def track_sync(directory):
            observed.append((directory, backup.is_dir()))
            sync(directory)

        with patch.object(updater, 'sync_directory', side_effect=track_sync):
            self.install()
        self.assertEqual(observed[0], (self.root, True))
        self.assertIn((retained.parent, False), observed)
        self.assertFalse(journal.exists())

    def test_committed_cleanup_resyncs_releases_if_backup_is_already_removed(self):
        _, retained, _ = self.prepare_retained_switch()
        sync = updater.sync_directory
        journal = self.root / 'retained-switch-committed.json'
        backup = retained.with_name('.retained-' + retained.name)

        def interrupt_sync(directory):
            if directory == retained.parent and journal.exists() and not backup.exists():
                raise OSError('interrupted directory sync')
            sync(directory)

        with patch.object(updater, 'sync_directory', side_effect=interrupt_sync):
            with self.assertRaisesRegex(OSError, 'interrupted directory sync'):
                self.install()
        self.assertTrue(journal.exists())
        self.assertFalse(backup.exists())
        with patch.object(updater, 'sync_directory', wraps=sync) as synced:
            self.install()
        synced.assert_any_call(retained.parent)
        self.assertFalse(journal.exists())

    def test_killed_updater_leaves_lock_held_until_installer_exits(self):
        self.root.mkdir()
        started = self.root / 'child-started'
        release = self.root / 'child-may-exit'
        child = """
from pathlib import Path
import sys
import time

Path(sys.argv[1]).touch()
deadline = time.monotonic() + 10
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(.02)
"""
        parent = """
from pathlib import Path
import runpy
import sys

api = runpy.run_path(sys.argv[1])
with api['update_lock'](Path(sys.argv[2])):
    api['command']([sys.executable, '-c', sys.argv[3], *sys.argv[4:]])
"""
        with subprocess.Popen([sys.executable, '-c', parent, str(REPO / 'scripts/hub2-self-update.py'),
                               str(self.root), child, str(started), str(release)]) as process:
            try:
                deadline = time.monotonic() + 5
                while not started.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(started.exists())
                process.kill()
                process.wait(timeout=5)
                with self.assertRaises(BlockingIOError), updater.update_lock(self.root):
                    self.fail('orphan installer did not hold the update lock')
            finally:
                release.touch()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                with updater.update_lock(self.root):
                    break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.02)

    def test_retained_release_refuses_different_receipt_or_tampered_download(self):
        current, retained, _ = self.prepare_retained_switch()
        with patch.dict(self.manifest, {'protocol_version': 2}):
            with self.assertRaisesRegex(ValueError, 'does not match the published manifest'):
                self.install()
        self.files['hub.pyz'] = b'tampered'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch: hub.pyz'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual(self.calls, [])

    def test_retained_activation_rechecks_published_manifest_after_socket_readiness(self):
        current, retained, units = self.prepare_retained_switch()
        fetch = self.fetch

        def changed(url, limit):
            if url.endswith('/host-release.json') and 'host-release.json' in self.downloads:
                return b'{}'
            return fetch(url, limit)

        self.fetch = changed
        with self.assertRaisesRegex(ValueError, 'published host release changed'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')

    def test_existing_uv_creates_venv_without_system_ensurepip(self):
        with patch.object(updater.shutil, 'which', return_value='/mock/uv'):
            self.install()
        self.assertTrue(any(args[:3] == ['/mock/uv', 'venv', '--allow-existing'] for args in self.calls))
        self.assertTrue(any(args[:3] == ['/mock/uv', 'pip', 'install'] and '--no-index' in args for args in self.calls))

    def test_stdlib_fallback_when_uv_unavailable(self):
        with patch.object(updater.shutil, 'which', return_value=None):
            self.install()
        self.assertTrue(any(args[1:3] == ['-m', 'venv'] for args in self.calls))

    def test_corrupt_artifact_never_touches_current_or_installs(self):
        old = self.old_release()
        self.files[HOST] = b'tampered'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_restart_failure_restores_previous_and_removes_only_new_release(self):
        old = self.old_release()
        original = self.command

        def fail_restart(args):
            original(args)
            if [str(value) for value in args] == ['systemctl', '--user', 'is-active', '--quiet', 'agent-hub2-host.service']:
                raise RuntimeError('failed restart')

        self.command = fail_restart
        with self.assertRaisesRegex(RuntimeError, 'failed restart'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertTrue(old.exists())
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertEqual(sum('restart' in args for args in self.calls), 2)

    def test_delayed_service_failure_rolls_back_and_next_tick_backs_off(self):
        old = self.old_release()
        checks = 0
        def delayed(args):
            nonlocal checks
            self.calls.append([str(value) for value in args])
            if 'is-active' in args:
                checks += 1
                if checks == 5:
                    raise RuntimeError('post-start crash')
        self.command = delayed
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(checks, 5)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.calls.clear()
        self.install()
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads((self.root / 'failed-releases.json').read_text())[SHA]['attempts'], 1)

    def test_interrupted_marked_install_and_pending_link_are_recovered(self):
        old = self.old_release()
        partial = self.root / 'releases' / SHA
        partial.mkdir()
        (partial / '.installing').write_text(SHA)
        (self.root / 'current.next').symlink_to(partial)
        self.install()
        self.assertFalse(partial.exists())
        self.assertFalse((self.root / 'current.next').is_symlink())
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertTrue((self.root / 'failed-releases.json').exists())

    def test_installed_units_refresh_on_timer_update(self):
        self.old_release()
        units = Path(self.temporary.name) / '.config/systemd/user'
        units.mkdir(parents=True)
        (units / updater.UNITS[0]).write_text('old unit')
        self.install()
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), self.files[updater.UNITS[0]])
        self.assertTrue(any('daemon-reload' in args for args in self.calls))

    def test_failed_activation_restores_unit_contents(self):
        self.old_release()
        units = Path(self.temporary.name) / '.config/systemd/user'
        units.mkdir(parents=True)
        (units / updater.UNITS[0]).write_text('original unit')
        def failure(args):
            if 'is-active' in args:
                raise RuntimeError('failed activation')
        self.command = failure
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual((units / updater.UNITS[0]).read_text(), 'original unit')
        self.assertFalse((units / updater.UNITS[1]).exists())

    def test_interrupted_activation_reverts_current_before_retry(self):
        old = self.old_release()
        partial = self.root / 'releases' / SHA
        partial.mkdir()
        (partial / '.installing').write_text(SHA)
        (self.root / 'previous').symlink_to(old)
        (self.root / 'current').unlink()
        (self.root / 'current').symlink_to(partial)
        self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertTrue(any('restart' in args for args in self.calls))
        self.assertFalse(partial.exists())

    def test_new_protocol_is_installed_when_manifest_and_wheel_match(self):
        self.manifest['protocol_version'] = 2
        self.status['protocol_version'] = 2
        self.install()
        check = next(args for args in self.calls if '-c' in args)
        self.assertIn('PROTOCOL_VERSION == 2', check[-1])

    def test_prune_keeps_three_owned_releases_and_unmanaged_directory(self):
        self.old_release()
        for letter in ('c', 'd', 'e', 'f'):
            path = self.root / 'releases' / (letter * 40)
            path.mkdir()
            (path / 'release.json').write_text(json.dumps({'host_version': path.name}))
        unmanaged = self.root / 'releases/owner-work'
        unmanaged.mkdir()
        self.install()
        managed = [path for path in (self.root / 'releases').iterdir() if path.name != 'owner-work']
        self.assertEqual(len(managed), 3)
        self.assertTrue(unmanaged.exists())

    def test_first_install_failure_removes_current_and_new_environment(self):
        def failure(args):
            if 'restart' in args:
                raise RuntimeError('cannot start')
        self.command = failure
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertFalse((self.root / 'current').is_symlink())
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def provider_bin(self, directory, *commands):
        directory = Path(self.temporary.name) / directory
        directory.mkdir(parents=True)
        for name in commands:
            executable = directory / name
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o700)
        return directory

    def test_service_install_preserves_discovered_provider_and_runtime_directories(self):
        versioned = self.provider_bin('nvm/versions/node/v99.12.3/bin', 'codex', 'claude', 'opencode', 'node', 'npm')
        wrappers = self.provider_bin('provider wrappers', 'zcodex', 'agy', 'a', 'aplexer')
        irrelevant = self.provider_bin('unrelated', 'unrelated-command')
        path = os.pathsep.join(map(str, (wrappers, irrelevant, versioned, wrappers)))
        env = Path(self.temporary.name) / '.config/agent-hub/host.env'
        original_command = self.command

        def check_before_restart(args):
            if 'restart' in args:
                self.assertTrue(env.is_file())
            original_command(args)

        self.command = check_before_restart
        with patch.dict(os.environ, PATH=path, SYNTHETIC_PRIVATE_TOKEN='do-not-copy'):
            self.install(install_service=True)
        expected = [str(wrappers), str(versioned), str(Path(self.temporary.name) / '.local/bin'),
                    '/usr/local/bin', '/usr/bin', '/bin']
        text = env.read_text()
        self.assertIn('PATH="' + os.pathsep.join(expected) + '"\n', text)
        self.assertNotIn(str(irrelevant), text)
        self.assertNotIn('SYNTHETIC_PRIVATE_TOKEN', text)
        self.assertNotIn('do-not-copy', text)
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        for name in updater.PROVIDER_PATH_COMMANDS:
            self.assertIsNotNone(shutil.which(name, path=os.pathsep.join(expected)))

    def test_service_install_discovers_isolated_default_native_a_executable(self):
        native = self.provider_bin('native-only/bin', 'a')
        with patch.dict(os.environ, PATH=str(native)):
            self.install(install_service=True, restart=False)
        text = (self.config.parent / 'host.env').read_text()
        self.assertIn('PATH="' + str(native) + ':', text)
        self.assertIsNotNone(shutil.which('a', path=str(native)))
        self.assertIsNone(shutil.which('aplexer', path=str(native)))

    def test_current_service_restarts_when_environment_is_first_published(self):
        self.install()
        self.calls.clear()
        self.install(install_service=True)
        restart = ['systemctl', '--user', 'restart', updater.UNITS[0]]
        self.assertIn(restart, self.calls)
        self.assertNotIn(['systemctl', '--user', 'start', updater.UNITS[0]], self.calls)
        observation = next(i for i, args in enumerate(self.calls) if args[-1] == STATUS_PATH)
        self.assertLess(self.calls.index(restart), observation)

    def test_current_service_restarts_without_changing_existing_environment(self):
        self.install()
        env = self.config.parent / 'host.env'
        env.write_bytes(b'PATH=/operator/bin:/usr/bin\n')
        self.calls.clear()
        self.install(install_service=True)
        self.assertIn(['systemctl', '--user', 'restart', updater.UNITS[0]], self.calls)
        self.assertNotIn(['systemctl', '--user', 'start', updater.UNITS[0]], self.calls)
        self.assertEqual(env.read_bytes(), b'PATH=/operator/bin:/usr/bin\n')

    def test_current_service_environment_can_be_staged_without_restart(self):
        self.install()
        self.calls.clear()
        self.install(install_service=True, restart=False)
        self.assertTrue((self.config.parent / 'host.env').is_file())
        self.assertFalse(any(args[0] == 'systemctl' and ('start' in args or 'restart' in args or 'enable' in args)
                             for args in self.calls))
        self.assertFalse(any(args[-1] == STATUS_PATH for args in self.calls))

    def test_staged_current_service_environment_is_loaded_on_later_explicit_install(self):
        self.install()
        self.install(install_service=True, restart=False)
        env = self.config.parent / 'host.env'
        staged = env.read_bytes()
        self.calls.clear()
        self.install(install_service=True)
        restart = ['systemctl', '--user', 'restart', updater.UNITS[0]]
        self.assertIn(restart, self.calls)
        observation = next(i for i, args in enumerate(self.calls) if args[-1] == STATUS_PATH)
        self.assertLess(self.calls.index(restart), observation)
        self.assertEqual(env.read_bytes(), staged)

    def test_current_service_retries_restart_after_environment_publication_then_repair_failure(self):
        self.install()
        self.calls.clear()
        with patch.object(updater, 'repair_service_files', side_effect=RuntimeError('synthetic repair failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic repair failure'):
                self.install(install_service=True)
        env = self.config.parent / 'host.env'
        published = env.read_bytes()
        self.assertFalse(any(args[0] == 'systemctl' for args in self.calls))
        self.calls.clear()
        self.install(install_service=True)
        restart = ['systemctl', '--user', 'restart', updater.UNITS[0]]
        self.assertIn(restart, self.calls)
        observation = next(i for i, args in enumerate(self.calls) if args[-1] == STATUS_PATH)
        self.assertLess(self.calls.index(restart), observation)
        self.assertEqual(env.read_bytes(), published)

    def test_provider_symlink_keeps_bin_directory_and_quotes_environment_file(self):
        versioned = self.provider_bin('node versions/quoted"bin\\dir', 'node')
        target = self.provider_bin('package/libexec', 'codex') / 'codex'
        (versioned / 'codex').symlink_to(target)
        with patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.dict(os.environ, PATH=str(versioned)):
            updater.preserve_provider_path()
        text = (self.config.parent / 'host.env').read_text()
        escaped = str(versioned).replace('\\', '\\\\').replace('"', '\\"')
        self.assertIn(escaped + ':', text)
        self.assertNotIn(str(target.parent), text)

    def test_explicit_environment_override_is_untouched_during_service_upgrade_and_failure(self):
        self.old_release()
        env = self.config.parent / 'host.env'
        original = b'PATH=/operator/explicit/bin:/usr/bin\nEXPLICIT_SETTING=keep\n'
        env.write_bytes(original)
        env.chmod(0o640)
        self.status_rc = 1
        with self.assertRaisesRegex(RuntimeError, 'connection not verified'):
            self.install(install_service=True)
        self.assertEqual(env.read_bytes(), original)
        self.assertEqual(env.stat().st_mode & 0o777, 0o640)
        (self.root / 'failed-releases.json').unlink()
        self.status_rc = 0
        self.install(install_service=True)
        self.assertEqual(env.read_bytes(), original)

    def test_existing_empty_or_symlink_environment_is_not_replaced(self):
        env = self.config.parent / 'host.env'
        target = self.config.parent / 'operator.env'
        target.write_bytes(b'PATH=/operator/bin\n')
        with patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            env.touch()
            updater.preserve_provider_path()
            self.assertEqual(env.read_bytes(), b'')
            env.unlink()
            for destination in (target, self.config.parent / 'missing-operator.env'):
                env.symlink_to(destination)
                updater.preserve_provider_path()
                self.assertTrue(env.is_symlink())
                self.assertEqual(env.readlink(), destination)
                env.unlink()
        self.assertEqual(target.read_bytes(), b'PATH=/operator/bin\n')

    def test_provider_environment_publication_does_not_clobber_concurrent_override(self):
        env = self.config.parent / 'host.env'
        real_link = os.link

        def operator_wins(source, destination):
            env.write_bytes(b'PATH=/concurrent/operator/bin\n')
            real_link(source, destination)

        with patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.os, 'link', side_effect=operator_wins):
            updater.preserve_provider_path()
        self.assertEqual(env.read_bytes(), b'PATH=/concurrent/operator/bin\n')
        self.assertEqual(list(self.config.parent.glob('host.env.*')), [])

    def test_generated_provider_environment_survives_normal_update_and_repair(self):
        versioned = self.provider_bin('versioned/bin', 'codex', 'node')
        with patch.dict(os.environ, PATH=str(versioned)):
            self.install(install_service=True, restart=False)
        env = self.config.parent / 'host.env'
        original = env.read_bytes()
        with patch.dict(os.environ, PATH='/usr/bin:/bin'):
            self.install(install_service=True, restart=False)
            # Exercise normal new-release activation with the generated config.
            old = self.root / 'releases' / OLD_SHA
            old.mkdir()
            updater.link(old, self.root / 'current')
            shutil.rmtree(self.root / 'releases' / SHA)
            self.install()
        self.assertEqual(env.read_bytes(), original)
        self.assertIn(str(versioned), env.read_text())

    def test_failed_install_leaves_complete_provider_config_for_recovery(self):
        self.status_rc = 1
        with self.assertRaisesRegex(RuntimeError, 'connection not verified'):
            self.install(install_service=True)
        env = self.config.parent / 'host.env'
        original = env.read_bytes()
        self.assertFalse((self.root / 'current').is_symlink())
        self.assertFalse(any('enable' in args for args in self.calls))
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(RuntimeError, 'activation backoff'):
            self.install(install_service=True)
        self.assertEqual(env.read_bytes(), original)

    def test_provider_environment_write_failure_does_not_activate_or_enable_service(self):
        old = self.old_release()
        with patch.object(updater.os, 'link', side_effect=OSError('synthetic publication failure')):
            with self.assertRaisesRegex(OSError, 'synthetic publication failure'):
                self.install(install_service=True)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertFalse((self.config.parent / 'host.env').exists())
        self.assertEqual(list(self.config.parent.glob('host.env.*')), [])
        self.assertFalse(any(args[0] == 'systemctl' for args in self.calls))

    def test_normal_update_does_not_import_ambient_provider_environment(self):
        self.install()
        self.assertFalse((self.config.parent / 'host.env').exists())

    def test_initial_staging_writes_units_without_enabling_or_starting_them(self):
        self.install(install_service=True, restart=False)
        units = Path(self.temporary.name) / '.config/systemd/user'
        self.assertEqual([path.name for path in sorted(units.iterdir())], sorted(updater.UNITS))
        self.assertEqual([args for args in self.calls if args[0] == 'systemctl'],
                         [['systemctl', '--user', 'daemon-reload']])

    def test_direct_install_restarts_for_new_release_before_enabling_timer(self):
        old = self.old_release()
        self.install(install_service=True)
        self.assertEqual((self.root / 'previous').resolve(), old)
        self.assertEqual((self.root / 'current').resolve().name, SHA)
        self.assertTrue(any(args[2:] == ['restart', updater.UNITS[0]] for args in self.calls))
        observation = next(i for i, args in enumerate(self.calls) if args[-1] == STATUS_PATH)
        daemon_enabled = self.calls.index(['systemctl', '--user', 'enable', updater.UNITS[0]])
        timer_enabled = self.calls.index(['systemctl', '--user', 'enable', '--now', updater.UNITS[2]])
        self.assertLess(observation, daemon_enabled)
        self.assertLess(daemon_enabled, timer_enabled)
        active = ['systemctl', '--user', 'is-active', '--quiet', updater.UNITS[0]]
        self.assertEqual(self.calls[observation - 6:observation], [active] * 6)
        self.assertEqual(self.calls[observation + 1], active)

    def test_direct_install_old_server_rolls_back_without_enabling_timer(self):
        old = self.old_release()
        (self.root / 'updater.py').write_bytes(b'old updater')
        self.status_rc = 1
        self.status_output = b'synthetic-sensitive-output'
        with self.assertRaisesRegex(RuntimeError, '^host daemon connection not verified$'):
            self.install(install_service=True)
        self.assertFalse(any('enable' in args for args in self.calls))
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual((self.root / 'updater.py').read_bytes(), b'old updater')
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_readiness_rejects_wrong_socket_status_and_sanitizes_failed_transport(self):
        self.install(install_service=True, restart=False)
        correct = dict(self.status)
        cases = [
            ('wrong machine', {'machine_id': 'other-machine'}, None, False),
            ('disconnected', {'connected': False}, None, False),
            ('invalid connected flag', {'connected': 1}, None, False),
            ('reported stale release', {'host_version': OLD_SHA}, None, False),
            ('wrong protocol', {'protocol_version': 2}, None, False),
            ('invalid protocol type', {'protocol_version': True}, None, False),
            ('server mismatch', {'version_mismatch': True}, None, False),
            ('missing mismatch', {}, json.dumps({k: v for k, v in correct.items() if k != 'version_mismatch'}).encode(), False),
            ('oversized', {}, json.dumps(correct).encode() + b' ' * 4096, False),
            ('malformed', {}, b'synthetic-sensitive-output', False),
            ('timed out', {}, None, True),
        ]
        with patch.object(updater, 'command', self.command), patch.object(updater.time, 'sleep'), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.subprocess, 'run', self.status_command):
            for name, changes, raw, timeout in cases:
                with self.subTest(name=name):
                    self.status = {**correct, **changes}
                    self.status_output = raw
                    self.status_timeout = timeout
                    self.calls.clear()
                    with self.assertRaisesRegex(RuntimeError, '^host daemon connection not verified$') as failure:
                        updater.check_service_ready(self.root / 'current')
                    self.assertIsNone(failure.exception.__cause__)
                    self.assertEqual(sum('is-active' in args for args in self.calls), 6)
                    self.assertEqual(self.calls[-1][-1], STATUS_PATH)

    def test_explicit_install_fails_during_backoff_but_normal_update_defers(self):
        old = self.old_release()
        (self.root / 'failed-releases.json').write_text(json.dumps({SHA: {'retry_at': 4102444800}}))
        for restart in (True, False):
            with self.subTest(restart=restart), self.assertRaisesRegex(RuntimeError, 'activation backoff'):
                self.install(install_service=True, restart=restart)
        self.install()
        self.assertEqual(self.calls, [])
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_updater_replacement_failure_rolls_back_without_enabling_timer(self):
        old = self.old_release()
        installed_updater = self.root / 'updater.py'
        installed_updater.write_bytes(b'old updater')
        replace = Path.replace

        def fail_replace(path, target):
            if target == installed_updater:
                raise OSError('updater publication failed')
            return replace(path, target)

        with patch.object(Path, 'replace', fail_replace), self.assertRaisesRegex(OSError, 'publication failed'):
            self.install(install_service=True)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(installed_updater.read_bytes(), b'old updater')
        self.assertFalse(any('enable' in args for args in self.calls))

    def test_timer_enable_failure_leaves_committed_release_for_explicit_install_retry(self):
        old = self.old_release()
        timer_enable = ['systemctl', '--user', 'enable', '--now', updater.UNITS[2]]
        record_command = self.command

        def fail_enable(args):
            record_command(args)
            if args == timer_enable:
                raise RuntimeError('timer enable failed')

        self.command = fail_enable
        with self.assertRaisesRegex(RuntimeError, 'timer enable failed'):
            self.install(install_service=True)
        target = (self.root / 'current').resolve()
        self.assertEqual(target.name, SHA)
        self.assertEqual((self.root / 'previous').resolve(), old)
        self.assertFalse((target / '.installing').exists())
        self.assertFalse((self.root / 'failed-releases.json').exists())
        self.assertEqual((self.root / 'updater.py').read_bytes(), self.files['hub2-self-update.py'])
        self.command = record_command
        self.calls.clear()
        self.install(install_service=True)
        self.assertIn(['systemctl', '--user', 'restart', updater.UNITS[0]], self.calls)
        self.assertIn(timer_enable, self.calls)
        self.assertEqual((self.root / 'current').resolve(), target)

    def test_same_version_rebuild_repairs_only_shared_artifacts_without_restart(self):
        self.install(install_service=True, restart=False)
        target = (self.root / 'current').resolve()
        old_receipt = (target / 'release.json').read_bytes()
        old_cli = (target / 'hub.pyz').read_bytes()
        old_enroller = (target / 'hub2-enroll.py').read_bytes()
        rebuilt = {'hub.pyz': b'rebuilt cli', HOST: b'rebuilt host wheel',
                   'manifest.json': json.dumps({'built_at_ms': 123456}).encode()}
        for name, data in rebuilt.items():
            self.files[name] = data
            self.manifest['artifacts'][name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        units = Path(self.temporary.name) / '.config/systemd/user'
        (units / updater.UNITS[1]).write_text('outdated')
        (units / updater.UNITS[2]).unlink()
        (self.root / 'updater.py').unlink()
        self.calls.clear()
        self.downloads.clear()
        self.install(install_service=True, restart=False)
        self.assertCountEqual(self.downloads, ['host-release.json', 'hub2-self-update.py', *updater.UNITS[1:]])
        self.assertEqual((target / 'release.json').read_bytes(), old_receipt)
        self.assertEqual((target / 'hub.pyz').read_bytes(), old_cli)
        self.assertEqual((target / 'hub2-enroll.py').read_bytes(), old_enroller)
        for name in updater.UNITS:
            self.assertEqual((units / name).read_bytes(), self.files[name])
        self.assertEqual((self.root / 'updater.py').read_bytes(), self.files['hub2-self-update.py'])
        self.assertEqual((self.root / 'current').resolve(), target)
        self.assertFalse((target / '.installing').exists())
        self.assertFalse(any('venv' in args for args in self.calls))
        self.assertEqual([args for args in self.calls if args[0] == 'systemctl'],
                         [['systemctl', '--user', 'daemon-reload']])
        self.calls.clear()
        self.install()
        self.assertEqual(self.calls, [])

    def test_same_version_stage_rejects_changed_protocol_or_service_records(self):
        self.install(install_service=True, restart=False)
        units = Path(self.temporary.name) / '.config/systemd/user'
        timer = units / updater.UNITS[2]
        timer.unlink()
        artifacts = self.manifest['artifacts']
        cases = [
            ('protocol', {'protocol_version': 2}, 'release identity'),
            ('timer checksum', {'artifacts': {**artifacts, updater.UNITS[2]: {**artifacts[updater.UNITS[2]], 'sha256': '0' * 64}}},
             'service artifact mismatch'),
            ('updater size', {'artifacts': {**artifacts, 'hub2-self-update.py': {**artifacts['hub2-self-update.py'], 'size': 1}}},
             'service artifact mismatch'),
        ]
        for case, fields, error in cases:
            with self.subTest(case=case), patch.dict(self.manifest, fields):
                self.calls.clear()
                self.downloads.clear()
                with self.assertRaisesRegex(ValueError, error):
                    self.install(install_service=True, restart=False)
                self.assertEqual(self.downloads, ['host-release.json'])
                self.assertEqual(self.calls, [])
                self.assertFalse(timer.exists())

    def test_same_version_stage_requires_a_valid_local_receipt_and_enroller(self):
        self.install(install_service=True, restart=False)
        target = (self.root / 'current').resolve()
        receipt = target / 'release.json'
        original = receipt.read_bytes()
        receipt.write_text('[]')
        with self.assertRaisesRegex(ValueError, 'unsupported release protocol'):
            self.install(install_service=True, restart=False)
        receipt.write_bytes(original)
        (target / 'hub2-enroll.py').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'installed release artifact mismatch: hub2-enroll.py'):
            self.install(install_service=True, restart=False)

    def test_same_version_stage_refuses_corrupt_repair_before_writing_units(self):
        self.install(install_service=True, restart=False)
        units = Path(self.temporary.name) / '.config/systemd/user'
        (units / updater.UNITS[1]).unlink()
        (units / updater.UNITS[2]).unlink()
        self.files[updater.UNITS[2]] = b'corrupt'
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install(install_service=True, restart=False)
        self.assertFalse((units / updater.UNITS[1]).exists())
        self.assertFalse((units / updater.UNITS[2]).exists())
        self.assertFalse(any(args[0] == 'systemctl' for args in self.calls))

    def test_shell_starts_only_daemon_until_enrollment_and_readiness_succeed(self):
        for scenario in ('fresh', 'enroll', 'start', 'is-active', 'routing', 'enrolled', 'backoff'):
            enrolled = scenario in ('enrolled', 'backoff')
            failure = '' if scenario in ('fresh', 'enrolled') else scenario
            with self.subTest(scenario=scenario):
                home = Path(self.temporary.name) / ('shell-' + scenario)
                root = home / '.local/share/agent-hub2'
                current = root / 'current'
                target = root / 'releases' / SHA
                current_bin = target / 'bin'
                current_bin.mkdir(parents=True)
                (target / 'release.json').write_text(json.dumps(self.manifest))
                current.symlink_to(target)
                fake_bin = home / 'fake-bin'
                fake_bin.mkdir()
                log = home / 'commands.jsonl'
                config = home / '.config/agent-hub/v2.json'
                enrollment = json.dumps({'machine_id': MACHINE, 'token': 'synthetic-enrollment-token'})
                if enrolled:
                    config.parent.mkdir(parents=True)
                    config.write_text(enrollment)
                shim = home / 'downloaded.py'
                shim.write_text(f"""
import json, os, runpy, sys, time
if '--no-restart' in sys.argv:
    assert '--install-service' in sys.argv
    with open(os.environ['INSTALL_TEST_LOG'], 'a') as stream:
        stream.write(json.dumps(['stage']) + '\\n')
elif '--install-service' in sys.argv:
    with open(os.environ['INSTALL_TEST_LOG'], 'a') as stream:
        stream.write(json.dumps(['restart-install']) + '\\n')
    if os.environ['INSTALL_TEST_FAIL'] == 'backoff':
        sys.exit(1)
else:
    time.sleep = lambda _: None
    runpy.run_path({str(REPO / 'scripts/hub2-self-update.py')!r}, run_name='__main__')
""")
                stub = fake_bin / 'stub'
                stub.write_text(f"""#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
config = Path(os.environ['INSTALL_TEST_CONFIG'])
if name == 'curl':
    shutil.copyfile(os.environ['INSTALL_TEST_UPDATER'], args[args.index('-o')+1])
    sys.exit(0)
if name == 'hub2-enroll.py':
    event = 'enroll'
elif name == 'systemctl':
    event = args[1]
    assert args[0] == '--user'
elif name == 'hub2':
    assert args == ['--config', str(config), 'request', 'GET', {STATUS_PATH!r}]
    assert os.environ['HUB2_TOKEN'] == 'synthetic-override-token'
    event = 'routing'
else:
    assert name == 'loginctl'
    event = name
with open(os.environ['INSTALL_TEST_LOG'], 'a') as stream:
    stream.write(json.dumps([event, *args]) + '\\n')
if event == os.environ['INSTALL_TEST_FAIL']:
    sys.exit(1)
if event == 'enroll':
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text({enrollment!r})
elif event in ('start', 'routing', 'enable'):
    assert config.is_file()
    if event == 'routing':
        print({json.dumps(self.status)!r})
""")
                stub.chmod(0o700)
                for name in ('curl', 'loginctl', 'systemctl'):
                    (fake_bin / name).symlink_to(stub)
                (fake_bin / 'python3').symlink_to(sys.executable)
                (current_bin / 'python').symlink_to(sys.executable)
                (current_bin / 'hub2').symlink_to(stub)
                (current / 'hub2-enroll.py').symlink_to(stub)
                result = subprocess.run(['/bin/sh', str(REPO / 'deploy/host-install.sh')],
                                        env={**os.environ, 'HOME': str(home), 'PATH': str(fake_bin)+':/usr/bin:/bin',
                                             'HUB_BASE_URL': 'https://example.invalid', 'HUB_WORKSPACE_ROOT': str(home),
                                             'HUB2_TOKEN': 'synthetic-override-token', 'INSTALL_TEST_LOG': str(log), 'INSTALL_TEST_CONFIG': str(config),
                                             'INSTALL_TEST_UPDATER': str(shim), 'INSTALL_TEST_FAIL': failure},
                                        capture_output=True, text=True, timeout=10)
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                events = [args[0] for args in commands]
                if enrolled:
                    self.assertEqual(events, ['loginctl', 'restart-install'])
                    self.assertEqual(result.returncode, 1 if failure else 0, result.stderr)
                    self.assertEqual('installed and enrolled' in result.stdout, not failure)
                elif failure:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(events[-1], failure)
                    self.assertNotIn('enable', events)
                    self.assertFalse(any(updater.UNITS[2] in args for args in commands))
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(events, ['stage', 'enroll', 'loginctl', 'start', *(['is-active'] * 6),
                                              'routing', 'is-active', 'enable', 'enable'])
                    self.assertEqual(commands[3], ['start', '--user', 'start', updater.UNITS[0]])
                    self.assertEqual(commands[-1], ['enable', '--user', 'enable', '--now', updater.UNITS[2]])

    def test_reject_unsafe_manifest_and_protocol_before_install(self):
        for key, value in [('protocol_version', 0), ('host_version', '../invalid'), ('python_requires', '>=99.0')]:
            with self.subTest(key=key):
                invalid = dict(self.manifest, **{key: value})
                with self.assertRaises(ValueError):
                    updater.validate_manifest(invalid)
        self.manifest['artifacts']['../escape'] = self.manifest['artifacts'][HOST]
        with self.assertRaisesRegex(ValueError, 'unsafe artifact'):
            self.install()
        self.assertEqual(self.calls, [])

    def test_existing_unmanaged_environment_is_preserved(self):
        target = self.root / 'releases' / SHA
        target.mkdir(parents=True)
        marker = target / 'owner-data'
        marker.write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'explicit recovery'):
            self.install()
        self.assertEqual(marker.read_text(), 'preserve')

    def test_export_excludes_tracked_build_output(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as archive:
            for name in ['hub2/host/agent_hub_host/client.py', 'hub2/host/build/lib/agent_hub_host/client.py']:
                entry = tarfile.TarInfo(name)
                entry.size = 5
                archive.addfile(entry, io.BytesIO(b'code\n'))
        with patch.object(builder, 'run', side_effect=[(SHA + '\n').encode(), data.getvalue()]):
            builder.export_source(REPO, 'HEAD', Path(self.temporary.name))
        self.assertTrue((Path(self.temporary.name) / 'hub2/host/agent_hub_host/client.py').is_file())
        self.assertFalse((Path(self.temporary.name) / 'hub2/host/build').exists())

    def test_enrollment_defaults_to_central_credentials_and_never_replaces_config(self):
        class Client:
            base_url = 'https://example.invalid'

            def request(self, method, path, body):
                self.request_body = body
                return {'machine_id': 'test-machine', 'token': 'test-secret'}

        client = Client()
        saved = []
        path = Path(self.temporary.name) / 'config.json'
        result = enrollment.enroll(client, lambda path, data: saved.append(data), path,
                                   [self.temporary.name], ['codex'], 'test-code')
        self.assertEqual(result, 'test-machine')
        self.assertEqual(saved[0]['launcher'], {'private_provider_homes': True})
        self.assertEqual(saved[0]['token'], 'test-secret')
        self.assertNotIn('code', saved[0])
        path.write_text('existing')
        with self.assertRaisesRegex(ValueError, 'refusing to replace'):
            enrollment.enroll(client, lambda *_: self.fail('must not save'), path,
                              [self.temporary.name], ['codex'], 'new-code')


def publication_bundle(directory, commit, label='fixture'):
    directory.mkdir()
    wheel = f'agent_hub_host_v2-2.0.0+g{commit}-py3-none-any.whl'
    with zipfile.ZipFile(directory / wheel, 'w') as archive:
        archive.writestr('agent_hub_host/_release.py', f'HOST_VERSION = {commit!r}\nPROTOCOL_VERSION = 1\n')
    with zipfile.ZipFile(directory / 'hub.pyz', 'w') as archive:
        archive.writestr('agent_hub_host/_build_info.py', f'BUILD_COMMIT = "{commit}"\n')
        archive.writestr('__main__.py', 'raise RuntimeError("staged code must never run")\n')
    cli_digest = hashlib.sha256((directory / 'hub.pyz').read_bytes()).hexdigest()
    (directory / 'hub.pyz.sha256').write_text(f'{cli_digest}  hub.pyz\n')
    (directory / 'manifest.json').write_text(json.dumps(
        {'built_from_commit': commit, 'sha256': cli_digest, 'build_label': label}))
    for name in ('websockets-16.0-py3-none-any.whl', 'host-install.sh', 'hub2-enroll.py',
                 *builder.CLI_INSTALLERS, *builder.UNITS):
        (directory / name).write_text('artifact:' + name)
    (directory / 'hub2-self-update.py').write_text('raise RuntimeError("staged updater must never run")\n')
    records = {}
    for path in directory.iterdir():
        data = path.read_bytes()
        records[path.name] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    manifest = {'host_version': commit, 'protocol_version': 1, 'schema_version': 1,
                'python_requires': '>=3.11', 'artifacts': records}
    raw = json.dumps(manifest, sort_keys=True).encode()
    (directory / 'host-release.json').write_bytes(raw)
    return directory, hashlib.sha256(raw).hexdigest()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        scratch = REPO / '.local/tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch, prefix='test-publication-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'published'
        self.current = self.root / 'current'
        self.receipt = self.directory / 'publication.json'

    def publish(self, action, commit=None, digest=None, bundle=None, *, receipt=None, token=None, error=None):
        args = ['--root', str(self.root), action]
        if commit is not None:
            args += ['--commit', commit, '--manifest-sha256', digest]
        if action == 'stage':
            args += ['--bundle', str(bundle)]
        else:
            args += ['--receipt', str(receipt or self.receipt)]
        if token is not None:
            args += ['--transaction-token', token]
        result = subprocess.run([sys.executable, str(REPO / 'scripts/publish-hub2-release.py'), *args],
                                capture_output=True, text=True, timeout=10)
        if error is None:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(error, result.stderr)

    def stage(self, commit, label):
        bundle, digest = publication_bundle(self.directory / label, commit, label)
        self.publish('stage', commit, digest, bundle)
        return bundle, digest

    def test_stage_is_immutable_and_reuses_only_exact_bundle(self):
        bundle, digest = self.stage(SHA, 'original')
        release = self.root / 'releases' / SHA
        inode = release.stat().st_ino
        self.assertEqual(release.stat().st_mode & 0o777, 0o555)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o444 for path in release.iterdir()))
        self.assertFalse(self.current.is_symlink())
        self.publish('stage', SHA, digest, bundle)
        self.assertEqual(release.stat().st_ino, inode)
        rebuilt, rebuilt_digest = publication_bundle(self.directory / 'rebuilt', SHA, 'rebuilt')
        self.publish('stage', SHA, rebuilt_digest, rebuilt, error='manifest differs')
        self.assertEqual(release.stat().st_ino, inode)
        self.assertEqual((release / 'host-release.json').read_bytes(), (bundle / 'host-release.json').read_bytes())

    def test_stage_refuses_non_regular_files_from_the_upload_directory(self):
        bundle, digest = publication_bundle(self.directory / 'untrusted', SHA)
        installer = bundle / 'install.sh'
        installer.unlink()
        os.mkfifo(installer)
        self.publish('stage', SHA, digest, bundle, error='not a bounded regular file')
        self.assertFalse((self.root / 'releases' / SHA).exists())
        installer.unlink()
        installer.symlink_to(bundle / 'install.ps1')
        self.publish('stage', SHA, digest, bundle, error='publication failed')
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_activate_and_rollback_initial_absence_are_idempotent(self):
        _, digest = self.stage(SHA, 'first')
        self.publish('activate', SHA, digest)
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')
        original_receipt = self.receipt.read_bytes()
        self.assertIsNone(json.loads(original_receipt)['previous'])
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        self.publish('activate', SHA, digest)
        self.assertEqual(self.receipt.read_bytes(), original_receipt)
        self.publish('rollback')
        self.assertFalse(os.path.lexists(self.current))
        self.assertEqual(json.loads(self.receipt.read_bytes())['phase'], 'rolled_back')
        self.publish('rollback')
        self.publish('activate', SHA, digest, error='receipt cannot activate')

    def test_delayed_root_activation_is_fenced_even_before_a_receipt_exists(self):
        _, digest = self.stage(SHA, 'first')
        old_token, new_token = '1' * 32, '2' * 32
        token_file = self.receipt.parent / 'transaction-token'
        token_file.write_text(old_token + '\n')
        self.publish('rollback', token=old_token)
        self.assertFalse(self.receipt.exists())
        self.publish('activate', SHA, digest, token=old_token, error='cancelled publication')
        self.assertFalse(self.current.is_symlink())

        (token_file.parent / 'cli-publication-cancelled').unlink()
        token_file.write_text(new_token + '\n')
        self.publish('activate', SHA, digest, token=old_token, error='another deploy transaction')
        self.assertFalse(self.current.is_symlink())
        self.publish('activate', SHA, digest, token=new_token)
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')
        self.publish('commit', token=old_token, error='another deploy transaction')
        self.current.unlink()
        self.publish('commit', token=new_token, error='matched CLI assets are not active')
        self.current.symlink_to(f'releases/{SHA}')
        self.publish('commit', token=new_token)
        self.publish('commit', token=new_token)
        self.publish('rollback', token=new_token, error='already committed')
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')

    def test_receipt_precedes_switch_and_crash_retries_preserve_previous(self):
        _, old_digest = self.stage(OLD_SHA, 'old')
        self.publish('activate', OLD_SHA, old_digest, receipt=self.directory / 'old.json')
        _, digest = self.stage(SHA, 'next')
        publisher = module('publish-hub2-release')
        switch_pointer = publisher.switch_pointer

        def interrupted(root, pointer):
            pending = json.loads(self.receipt.read_bytes())
            self.assertEqual(pending['previous'], f'releases/{OLD_SHA}')
            self.assertEqual(pending['previous_manifest_sha256'], old_digest)
            self.assertEqual(pending['phase'], 'prepared')
            self.assertEqual(os.readlink(self.current), pending['previous'])
            switch_pointer(root, pointer)
            raise OSError('interrupted after atomic switch')

        with patch.object(publisher, 'switch_pointer', interrupted), self.assertRaisesRegex(OSError, 'interrupted'):
            publisher.activate(self.root, SHA, digest, self.receipt)
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')
        self.publish('activate', SHA, digest)
        self.assertEqual(json.loads(self.receipt.read_bytes())['phase'], 'active')
        self.publish('rollback')
        self.assertEqual(os.readlink(self.current), f'releases/{OLD_SHA}')
        self.publish('rollback')

    def test_corrupt_candidate_never_activates_and_does_not_prevent_rollback(self):
        _, digest = self.stage(SHA, 'candidate')
        artifact = self.root / 'releases' / SHA / 'hub2-enroll.py'
        original = artifact.read_bytes()
        artifact.chmod(0o644)
        artifact.write_bytes(b'x' * len(original))
        self.publish('activate', SHA, digest, error='unsafe ownership or permissions')
        artifact.chmod(0o444)
        self.publish('activate', SHA, digest, error='checksum mismatch')
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.current.is_symlink())
        artifact.chmod(0o644)
        artifact.write_bytes(original)
        artifact.chmod(0o444)
        self.publish('activate', SHA, digest)
        artifact.chmod(0o644)
        artifact.write_bytes(b'x' * len(original))
        self.publish('rollback')
        self.assertFalse(os.path.lexists(self.current))

    def test_rollback_refuses_unrelated_and_unmanaged_current(self):
        _, digest = self.stage(SHA, 'candidate')
        _, other_digest = self.stage(OLD_SHA, 'other')
        self.publish('activate', SHA, digest)
        self.publish('activate', OLD_SHA, other_digest, receipt=self.directory / 'other.json')
        before = self.receipt.read_bytes()
        self.publish('rollback', error='current does not belong')
        self.assertEqual(os.readlink(self.current), f'releases/{OLD_SHA}')
        self.current.unlink()
        self.current.write_text('unmanaged')
        self.publish('rollback', error='not a managed release pointer')
        self.assertEqual(self.current.read_text(), 'unmanaged')
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_rollback_refuses_previous_same_sha_bundle_substitution(self):
        _, old_digest = self.stage(OLD_SHA, 'old')
        self.publish('activate', OLD_SHA, old_digest, receipt=self.directory / 'old.json')
        _, digest = self.stage(SHA, 'candidate')
        self.publish('activate', SHA, digest)
        substituted, _ = publication_bundle(self.directory / 'substituted', OLD_SHA, 'substituted')
        old = self.root / 'releases' / OLD_SHA
        old.chmod(0o755)
        old.rename(self.directory / 'retained-original')
        substituted.rename(old)
        for artifact in old.iterdir():
            artifact.chmod(0o444)
        old.chmod(0o555)
        self.publish('rollback', error='manifest differs')
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')

    def test_vm_transaction_publishes_applied_release_and_rolls_back_before_checkout(self):
        vm = self.directory / 'vm'
        vm.mkdir()

        def git(*args):
            return subprocess.check_output(['git', '-C', str(vm), *args], text=True).strip()

        git('init', '--quiet')
        git('config', 'user.name', 'Release Fixture')
        git('config', 'user.email', 'release@example.invalid')
        git('config', 'core.hooksPath', '/dev/null')
        checker = module('schema-rollback-preflight')
        schema_contract = REPO / 'deploy/schema-compatibility.json'
        schemas = json.loads(schema_contract.read_text())['schemas']
        db_dir = vm / '.local/hub2-db'
        db_dir.mkdir(parents=True)
        for binding, path in checker.SOURCES.items():
            source = vm / path
            source.parent.mkdir(parents=True, exist_ok=True)
            version = schemas[binding]['bridge_write']
            source.write_text(f'initializeSchema(ctx, this.db, {version}, SCHEMA);\n')
            with sqlite3.connect(db_dir / f'{binding}-fixture.sqlite') as db:
                db.execute('CREATE TABLE hub2_schema(singleton INTEGER PRIMARY KEY, version INTEGER)')
                db.execute('INSERT INTO hub2_schema VALUES(1, ?)', (version,))
        installer = vm / 'deploy/vm/install-hub2-node.sh'
        installer.parent.mkdir(parents=True)
        installer.write_text('set -eu\n[ "$1" = --hub-only ]\n[ ! -L .local/cli-releases/current ]\n'
                             'printf rebuilt > hub2/public/cli/manifest.json\n')
        (vm / '.gitignore').write_text('/.local/\n/.dev.vars\n/hub2/public/cli/\n')
        git('add', 'deploy', '.gitignore', 'hub2')
        git('commit', '--quiet', '-m', 'old release without publisher')
        previous = git('rev-parse', 'HEAD')
        git('branch', '-M', 'main')
        installer.write_text('set -eu\n[ "$*" = "--release-mode active --hub-only --schema-migrations defer --keep-cli-assets" ]\n'
                             '[ "$(cat hub2/public/cli/manifest.json)" = legacy ]\n')
        (vm / 'scripts').mkdir()
        for script in ('publish-hub2-release.py', 'build-hub2-release.py', 'hub2-self-update.py',
                       'schema-rollback-preflight.py', 'apply-model-policy.py', 'prepare-model-policy-service-credential.py'):
            shutil.copyfile(REPO / 'scripts' / script, vm / 'scripts' / script)
        shutil.copyfile(schema_contract, vm / 'deploy' / schema_contract.name)
        for path in checker.SOURCES.values():
            shutil.copyfile(REPO / path, vm / path)
        (vm / 'config').mkdir()
        shutil.copyfile(REPO / 'config/model-policy.json', vm / 'config/model-policy.json')
        git('add', 'scripts', 'deploy', 'hub2', 'config')
        git('commit', '--quiet', '-m', 'candidate release')
        candidate = git('rev-parse', 'HEAD')
        git('remote', 'add', 'origin', str(vm))
        git('checkout', '--quiet', '--detach', previous)
        (vm / '.dev.vars').write_text('HUB2_RELEASE_MODE=active\n')
        legacy_manifest = vm / 'hub2/public/cli/manifest.json'
        legacy_manifest.parent.mkdir(parents=True)
        legacy_manifest.write_text('legacy')
        upload_parent = vm / '.local/tmp'
        upload_parent.mkdir(parents=True)
        _, digest = publication_bundle(upload_parent / f'ci-cli-release-{candidate}', candidate)

        state = self.directory / 'deploy-state'
        # Reuse the offline protocol peer; execute the real protected policy
        # helper while retaining this fixture's CLI publication/rollback checks.
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('release_policy_transaction_fixture', REPO / 'test/test_schema_release_transaction.py')
        policy_fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy_fixture)
        health_log = self.directory / 'health.log'
        policy_request_pointers = []

        def observe_commit(repo, *args):
            current = vm / '.local/cli-releases/current'
            policy_request_pointers.append(os.readlink(current) if current.is_symlink() else None)
            return git(*args)

        policy = policy_fixture.PolicyAPI(SimpleNamespace(repo=vm, state=state, health_log=health_log, candidate=candidate,
                                                         git=observe_commit))
        self.addCleanup(policy.close)
        prior = json.loads(json.dumps(policy.current['candidates']))
        prior[0]['weight'] = 37
        policy.current = {'version': 5, 'candidates': prior}
        credentials = self.directory / 'deploy-credentials'
        credentials.mkdir(mode=0o700)
        credential = credentials / 'model-policy-admin.json'
        credential.write_text(json.dumps(policy_fixture.service_document(target_sha=candidate)))
        credential.chmod(0o600)
        fake_bin = self.directory / 'fake-bin'
        fake_bin.mkdir()
        commands = {'sudo': 'exec "$@"', 'systemctl': 'exit 0', 'npm': 'exit 0',
                    'install': 'if [ "$1:$2" = -d:-o ]; then shift 5; set -- -d "$@"; '
                               'elif [ "$1" = -o ]; then shift 4; fi; exec "$MOCK_INSTALL" "$@"',
                    'python3': 'if [ "$1:$2" = "-:$MOCK_STATE" ]; then cat > "$MOCK_RETIRE"; '
                               'exec "$MOCK_PYTHON" "$@" < "$MOCK_RETIRE"; fi; exec "$MOCK_PYTHON" "$@"',
                    'curl': 'printf healthy >> "$MOCK_HEALTH_LOG"; printf "%s\\n" "$MOCK_HEALTH"'}
        for name, body in commands.items():
            path = fake_bin / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o755)
        env = {**os.environ, 'PATH': f'{fake_bin}:{os.environ["PATH"]}', 'AGENT_HUB_DEPLOY_TEST_MODE': '1',
               'AGENT_HUB_TEST_REPO': str(vm), 'AGENT_HUB_TEST_TRANSACTION_DIR': str(state),
               'AGENT_HUB_TEST_CADDY_CONFIG': str(self.directory / 'unused-Caddyfile'),
               'AGENT_HUB_TEST_POLICY_URL': policy.url, 'AGENT_HUB_TEST_POLICY_CREDENTIAL': str(credential),
               'MOCK_HEALTH_LOG': str(health_log), 'HUB2_TOKEN': 'ambient-token-must-not-be-used',
               'MOCK_INSTALL': shutil.which('install'),
               'MOCK_STATE': str(state), 'MOCK_RETIRE': str(self.directory / 'retire.py'), 'MOCK_PYTHON': sys.executable,
               'MOCK_HEALTH': '{"ok":true,"service":"hub2"}'}

        def transaction(*args, expected_error=None, overrides=None):
            result = subprocess.run(['bash', str(REPO / 'deploy/vm/deploy-release-transaction.sh'), *args],
                                    env={**env, **(overrides or {})}, capture_output=True, text=True, timeout=10)
            if expected_error is None:
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

        pointer = vm / '.local/cli-releases/current'
        transaction('apply', candidate, 'active', 'hub-only', 'matched-cli')
        self.assertEqual(policy.current['version'], 6)
        self.assertEqual(json.loads((policy_fixture.policy_directory(state) / 'receipt.json').read_text())['phase'], 'applied')
        self.assertEqual(legacy_manifest.read_text(), 'legacy')
        self.assertEqual((state / 'schema-mode').read_text().strip(), 'defer')
        self.assertEqual((state / 'cli-legacy.previous/manifest.json').read_text(), 'legacy')
        with (upload_parent / '.deploy-transaction.lock').open('ab') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            transaction('rollback', expected_error='another Hub2 deploy transaction')
        transaction('publish', candidate, digest, expected_error='active Hub2 API',
                    overrides={'MOCK_HEALTH': '{"status":"bootstrap","dispatch_enabled":false}'})
        self.assertFalse((state / 'cli-assets.json').exists())
        transaction('publish', candidate, 'f' * 64, expected_error='manifest differs')
        transaction('commit', expected_error='matched CLI assets were not published')
        self.assertEqual(json.loads((policy_fixture.policy_directory(state) / 'receipt.json').read_text())['phase'], 'applied',
                         'a premature finalize must not seal policy before CLI publication')
        self.assertFalse(pointer.is_symlink())
        transaction('publish', candidate, digest)
        transaction('publish', candidate, digest)
        self.assertEqual(os.readlink(pointer), f'releases/{candidate}')
        self.assertEqual(json.loads((state / 'cli-assets.json').read_text())['phase'], 'active')
        old_token = (state / 'transaction-token').read_text().strip()
        transaction('rollback')
        self.assertFalse(pointer.is_symlink())
        self.assertFalse(state.exists())
        self.assertEqual(git('rev-parse', 'HEAD'), previous)
        self.assertEqual(legacy_manifest.read_text(), 'legacy')
        self.assertEqual(policy.current, {'version': 7, 'candidates': prior})
        self.assertEqual(policy.writes()[-1][0], 'PUT')
        self.assertEqual(policy.writes()[-1][2], candidate, 'policy restoration precedes checkout')
        restore_index = next(i for i, call in enumerate(policy.calls) if call[0] == 'PUT')
        self.assertEqual(policy_request_pointers[restore_index], f'releases/{candidate}',
                         'policy restoration precedes published CLI pointer rollback')
        transaction('apply', candidate, 'active', 'hub-only', 'matched-cli')
        new_token = (state / 'transaction-token').read_text().strip()
        stale = subprocess.run([sys.executable, env['MOCK_RETIRE'], str(state), old_token],
                               capture_output=True, text=True, timeout=10)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn('different deploy transaction', stale.stderr)
        self.assertEqual((state / 'transaction-token').read_text().strip(), new_token)
        transaction('publish', candidate, digest)
        transaction('commit')
        self.assertFalse(state.exists())
        self.assertEqual(os.readlink(pointer), f'releases/{candidate}')
        self.assertEqual(policy.current['version'], 8)
        self.assertEqual([call[0] for call in policy.writes()], ['POST', 'PUT', 'POST'])


if __name__ == '__main__':
    unittest.main()
