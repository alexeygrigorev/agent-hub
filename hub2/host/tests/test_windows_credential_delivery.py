"""AUTHORED UNRUN. Normal imports; synthetic receipt/home, no provider/runtime.

The fake process executes the real helper's deliver() in-process solely against
this test's private temporary receipt/home. Windows APIs and physical processes
are separate fakes; this does not claim real Windows qualification.
"""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from agent_hub_host import credential_delivery as delivery, windows_release as release
from host.agent_hub_host import windows_managed_claude_deliver as helper
from host.agent_hub_host import managed_claude_deliver as managed, provisioning, credentials

REV = '11111111-2222-3333-4444-555555555555'
PATH = delivery.CREDENTIAL_PATH
OLD, NEW = 'synthetic-old-not-auth', 'synthetic-new-not-auth'


class JoinedWindowsDelivery(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root.chmod(0o700)
        self.managed = self.root / 'managed'
        self.managed.mkdir(mode=0o700)
        self.profile = self.root / 'profile.toml'
        self.archive = self.root / 'hub.pyz'
        with zipfile.ZipFile(self.archive, 'w'):
            pass
        self.stack.enter_context(patch.object(provisioning, 'root', return_value=self.managed))
        self.stack.enter_context(patch.object(provisioning, 'profile_path', return_value=self.profile))
        self.dest = self.managed / ('hub-claude-' + REV)
        self.dest.mkdir(mode=0o700)
        home = self.dest / 'home' / '.claude'
        home.mkdir(parents=True, mode=0o700)
        home.parent.chmod(0o700)
        (home / '.credentials.json').write_text(OLD)
        (home / '.credentials.json').chmod(0o600)
        self.bundle = {'label': 'hub-claude', 'provider': 'claude', 'revision': REV,
            'version': '1.2.3', 'files': {PATH: OLD}, 'env': {}, 'args': [],
            'credential_paths': [PATH], 'credential_generation': 1}
        provisioning.write_json(self.dest / 'bundle.json', self.bundle)
        provisioning.write_json(self.managed / 'state.json',
            [{'label': 'hub-claude', 'provider': 'claude', 'revision': REV}])
        provisioning.write_json(self.dest / credentials.STATE_FILE,
            {'generation': 1, 'digests': {PATH: credentials._digest(OLD.encode())}})
        self.write_profile()
        self.profile_before = self.profile.read_bytes()
        # Sentinels prove this selected consumer never writes other providers.
        self.other = self.managed / 'other-providers.json'
        self.other.write_text('{"AGY":"unchanged","go":"unchanged","Codex":"unchanged"}')
        self.other_before = self.other.read_bytes()
        self.lease = object.__new__(release.WindowsRelease)
        self.lease.files, self.lease.closed, self.lease.borrowers = [], False, 0
        self.lease.owner_thread = release.threading.get_ident()
        self.lease.python, self.lease.archive = 'TRUSTED-SELECTED-PYTHON', str(self.archive)
        self.epoch = delivery._Epoch()
        self.state = {'epoch': self.epoch, 'child': None, 'delivered': None,
                      'delivered_set': None, 'delivered_managed': False}
        self.calls, self.acks = [], []
        self.after_materialize = None
        self.generation = 2
        self.client = SimpleNamespace(request=self.request)
        self.bridge = SimpleNamespace(client=self.client, delivery_popen=self.popen,
            launcher=SimpleNamespace(windows_credential_artifacts=lambda: self.lease))
        # This tranche must never call provider/default-native or enrollment.
        self.stack.enter_context(patch.object(managed.default_claude_consumer, 'deliver',
            side_effect=AssertionError('native default action forbidden')))
        self.stack.enter_context(patch.object(provisioning, 'sync',
            side_effect=AssertionError('global sync forbidden')))
        self.stack.enter_context(patch.object(provisioning, 'sync_target',
            side_effect=AssertionError('enrollment forbidden')))

    def write_profile(self):
        previous = provisioning.sys.argv[0]
        try:
            provisioning.sys.argv[0] = str(self.archive)
            command = provisioning.module_command('execute', 'hub-claude')
        finally:
            provisioning.sys.argv[0] = previous
        self.profile.write_text('[profiles.operator-agy]\nengine = "antigravity"\ncommand = ["synthetic"]\n'
            + provisioning.BEGIN + '\n[profiles.hub-hub-claude]\nengine = "claude"\ncommand = '
            + json.dumps(command) + '\n' + provisioning.END + '\n')
        self.profile.chmod(0o600)

    def request(self, method, path, body=None, timeout=None):
        self.calls.append((method, path))
        if method == 'GET' and path == delivery.PASSIVE_PATH:
            return {'label': 'hub-claude', 'status': 'current', 'generation': 2,
                    'revision': REV, 'files': {PATH: NEW}}
        if method == 'GET' and path.startswith('/api/engines/hub-claude/generation?'):
            return {'label': 'hub-claude', 'status': 'current',
                    'generation': self.generation, 'revision': REV}
        if method == 'POST' and path == delivery.ACK_PATH:
            self.acks.append(body)
            return {'status': 'recorded'}
        raise AssertionError('Unexpected API or provider request')

    def popen(self, argv, **kwargs):
        self.assertEqual(argv, [self.lease.python, '-I', '-S', '-c',
            delivery.WINDOWS_HELPER_BOOT, str(self.archive), argv[-1]])
        self.assertIn(argv[-1], ('materialize', 'check'))
        self.assertGreater(self.lease.borrowers, 0)
        outer = self
        class Child:
            returncode = None
            def poll(self): return self.returncode
            def communicate(self, payload=None, timeout=None):
                outer.assertEqual(timeout, delivery.HELPER_TIMEOUT_SECONDS)
                request = managed._read_request(io.BytesIO(payload))
                try:
                    result = helper.deliver(str(outer.archive), request, argv[-1] == 'check')
                    self.returncode = 0
                except (managed.Refused, ValueError, OSError):
                    result, self.returncode = {'status': 'refused', 'reason': 'delivery_failed'}, 3
                if argv[-1] == 'materialize' and outer.after_materialize:
                    outer.after_materialize()
                return json.dumps(result).encode(), b''
            def kill(self): self.returncode = -9
        return Child()

    def run_delivery(self):
        # Patch only delivery's platform view, not global sys or the POSIX fake IO backend.
        with patch.object(delivery, 'sys', SimpleNamespace(platform='win32')):
            return delivery._deliver(self.bridge, self.state, self.epoch)

    def test_actual_leased_materialize_recheck_and_authenticated_ack(self):
        result = self.run_delivery()
        self.assertEqual(result[0], (2, REV))
        self.assertRegex(result[1], r'^[0-9a-f]{32}$')
        self.assertTrue(result[2])
        self.assertEqual(len(self.acks), 1)
        self.assertEqual(self.acks[0]['items'][0]['credential_generation'], 2)
        self.assertEqual((self.dest / 'home' / PATH).read_text(), NEW)
        self.assertEqual(provisioning.load_bundle(self.dest)['credential_generation'], 2)
        self.assertEqual(self.other.read_bytes(), self.other_before)
        self.assertEqual(self.profile.read_bytes(), self.profile_before)
        self.assertTrue(self.lease.closed)
        self.assertEqual(self.lease.borrowers, 0)

    def test_absent_or_unconfigured_consumer_never_enrolls_or_acks(self):
        for absent in ('state', 'profile'):
            with self.subTest(absent=absent):
                self.lease.closed = False
                if absent == 'state':
                    provisioning.write_json(self.managed / 'state.json', [])
                else:
                    provisioning.write_json(self.managed / 'state.json',
                        [{'label': 'hub-claude', 'provider': 'claude', 'revision': REV}])
                    self.profile.unlink()
                with self.assertRaises(delivery.Refused): self.run_delivery()
                self.assertEqual(self.acks, [])
                self.assertEqual((self.dest / 'home' / PATH).read_text(), OLD)

    def test_rotation_after_materialize_suppresses_ack(self):
        self.after_materialize = lambda: setattr(self, 'generation', 3)
        with self.assertRaises(delivery.Refused): self.run_delivery()
        self.assertEqual(self.acks, [])
        self.assertEqual((self.dest / 'home' / PATH).read_text(), NEW)

    def test_profile_disappearance_after_materialize_suppresses_ack(self):
        self.after_materialize = self.profile.unlink
        with self.assertRaises(delivery.Refused): self.run_delivery()
        self.assertEqual(self.acks, [])

    def test_epoch_stop_after_materialize_suppresses_ack_and_closes_reaped_lease(self):
        self.after_materialize = self.epoch.stopped.set
        with self.assertRaises(delivery.Refused): self.run_delivery()
        self.assertEqual(self.acks, [])
        self.assertTrue(self.lease.closed)

    def test_rollback_and_equal_generation_changed_bytes_refuse(self):
        for generation in (1, 2):
            with self.subTest(generation=generation):
                request = {'label': 'hub-claude', 'generation': generation,
                           'revision': REV, 'files': {PATH: NEW}}
                if generation == 1:
                    # Equal generation, changed bytes: exact receipt guard.
                    pass
                else:
                    provisioning.write_json(self.dest / credentials.STATE_FILE,
                        {'generation': 3, 'digests': {PATH: credentials._digest(OLD.encode())}})
                with self.assertRaises(managed.Refused): helper.deliver(str(self.archive), request)
                self.assertEqual((self.dest / 'home' / PATH).read_text(), OLD)
                self.assertEqual(self.acks, [])

    def test_unknown_spawn_keeps_lease_borrowed_and_original_control(self):
        original = KeyboardInterrupt('synthetic private control')
        def fail(*args, **kwargs): raise original
        self.bridge.delivery_popen = fail
        with self.assertRaises(KeyboardInterrupt) as caught: self.run_delivery()
        self.assertIs(caught.exception, original)
        self.assertIs(self.state['windows_pending_artifact_lease'], self.lease)
        self.assertFalse(self.lease.closed)
        self.assertEqual(self.lease.borrowers, 1)
        self.assertEqual(self.acks, [])

    def test_unreaped_timeout_does_not_free_code_or_ack(self):
        class Child:
            returncode = None
            pid = 999
            def poll(self): return None
            def communicate(self, *args, **kwargs): raise subprocess.TimeoutExpired('fixed', 60)
            def kill(self): pass
        self.bridge.delivery_popen = lambda *a, **k: Child()
        with patch.object(delivery, '_terminate'):
            with self.assertRaises(subprocess.TimeoutExpired): self.run_delivery()
        self.assertIs(self.state['windows_pending_artifact_lease'], self.lease)
        self.assertIsNotNone(self.state['child'])
        self.assertFalse(self.lease.closed)
        self.assertEqual(self.acks, [])

    def test_foreign_dict_never_selects_python_or_acks(self):
        self.bridge.launcher.windows_credential_artifacts = lambda: {'python': 'PATH'}
        with self.assertRaises(delivery.Refused): self.run_delivery()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.acks, [])

    def test_consumer_change_inside_helper_gap_is_not_a_held_verdict(self):
        real = managed._managed
        def change(*args, **kwargs):
            result = real(*args, **kwargs)
            self.profile.unlink()
            return result
        request = {'label': 'hub-claude', 'generation': 2,
                   'revision': REV, 'files': {PATH: NEW}}
        with patch.object(managed, '_managed', side_effect=change):
            with self.assertRaises(managed.Refused):
                helper.deliver(str(self.archive), request)
        self.assertEqual(self.acks, [])

    def test_helper_cli_rejects_relative_unc_ads_and_non_windows(self):
        for path in ('relative.pyz', r'\\server\share\hub.pyz', r'C:\x\..\hub.pyz', r'C:\x\hub.pyz:stream'):
            with self.subTest(path=path):
                self.assertFalse(helper._archive_argument(path))
        self.assertTrue(helper._archive_argument(r'C:\owned\hub.pyz'))
        with patch.object(helper, 'sys', SimpleNamespace(platform='linux')):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(helper.main([r'C:\owned\hub.pyz', 'materialize']), 3)
            self.assertNotIn(NEW, output.getvalue())

    def test_actual_windows_lock_uses_bounded_msvcrt_not_flock(self):
        from types import ModuleType
        fake = ModuleType('msvcrt')
        fake.LK_NBLCK = 2
        calls = []
        fake.locking = lambda fd, mode, count: calls.append((fd, mode, count))
        with patch.dict('sys.modules', {'msvcrt': fake}), \
             patch.object(provisioning.os, 'fstat', return_value=SimpleNamespace(st_size=1)), \
             patch.object(provisioning.os, 'lseek'):
            provisioning._windows_wait_lock(123, timeout=0.1)
        self.assertEqual(calls, [(123, 2, 1)])
