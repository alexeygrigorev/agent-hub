"""Isolated ownership/release contracts; native Windows E2E remains required."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from agent_hub_host import bootstrap as b, bootstrap_control as c, private_fs


class WindowsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / 'AppData/Local/Programs/agent-hub'
        self.archive = self.home / 'hub.pyz'
        self.archive.write_bytes(b'verified candidate archive')
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        for p in (patch.object(b.sys, 'platform', 'win32'),
                  patch.object(private_fs, '_windows', return_value=False),
                  patch.object(b, 'architecture', return_value='arm64'),
                  patch.object(b.Path, 'home', return_value=self.home),
                  patch.dict(os.environ, {'LOCALAPPDATA': str(self.home / 'AppData/Local'),
                      'APPDATA': str(self.home / 'AppData/Roaming'), 'SystemRoot': 'C:/Windows'})):
            self.stack.enter_context(p)

    def complete(self, release):
        names = ['venv/Scripts/python.exe', 'bin/aplexer.exe', 'bin/a.exe', 'bin/agy.exe',
                 'package/host/agent_hub_host/host_supervisor.py',
                 'runtimes/node-v' + b.NODE_VERSION + '-win-arm64/node.exe',
                 'runtimes/node-v' + b.NODE_VERSION + '-win-arm64/node_modules/npm/bin/npm-cli.js',
                 'runtimes/git-' + b.GIT_VERSION + '-arm64/cmd/git.exe',
                 'runtimes/git-' + b.GIT_VERSION + '-arm64/usr/bin/bash.exe']
        for name in names:
            p = release / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'in-use executable fixture')
        for package in b.ENGINES.values():
            name, version = package.rsplit('@', 1)
            p = release / 'engines/node_modules' / name / 'package.json'
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({'version': version}))
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        private_fs.atomic_write(release / 'runtime-ready.json', json.dumps({'archive_sha256': digest}).encode(), 256)

    def test_same_archive_is_reused_without_overwriting_locked_executables(self):
        release, ready = b.windows_release(self.archive, self.base)
        self.assertFalse(ready)
        self.complete(release)
        snapshots = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in release.rglob('*') if p.is_file()}
        with patch.object(b, 'run'):
            again, ready = b.windows_release(self.archive, self.base)
        self.assertEqual(again, release)
        self.assertTrue(ready)
        for p, expected in snapshots.items():
            self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), expected)

    def test_partial_broken_and_new_releases_never_modify_the_previous_tree(self):
        partial, _ = b.windows_release(self.archive, self.base)
        sentinel = partial / 'keep'
        sentinel.write_text('preserved partial evidence')
        fresh, ready = b.windows_release(self.archive, self.base)
        self.assertFalse(ready)
        self.assertNotEqual(fresh, partial)
        self.complete(fresh)
        with patch.object(b, 'run', side_effect=RuntimeError('runtime broken')):
            repair, ready = b.windows_release(self.archive, self.base)
        self.assertFalse(ready)
        self.assertNotEqual(repair, fresh)
        self.archive.write_bytes(b'new verified candidate')
        upgrade, ready = b.windows_release(self.archive, self.base)
        self.assertNotEqual(upgrade, fresh)
        self.assertFalse(ready)
        self.assertEqual(sentinel.read_text(), 'preserved partial evidence')
        self.assertEqual((fresh / 'venv/Scripts/python.exe').read_bytes(), b'in-use executable fixture')

    def test_release_selection_is_persistent_and_state_is_shared(self):
        release, _ = b.windows_release(self.archive, self.base)
        os.environ['HUB_INSTALL_RELEASE'] = str(release)
        self.assertEqual(b.root(), release)
        self.assertEqual(b.state_root(release), self.base)
        self.assertEqual(b.python_install_root(release), self.base / 'python')
        os.environ['HUB_INSTALL_RELEASE'] = str(self.home / 'temp')
        with self.assertRaises(ValueError):
            b.root()

    def test_ready_install_skips_every_runtime_and_venv_mutation(self):
        release, _ = b.windows_release(self.archive, self.base)
        self.complete(release)
        helpers = ('ensure_node', 'ensure_aplexer', 'install_package', 'ensure_git',
                   'install_engines', 'ensure_antigravity')
        with __import__('contextlib').ExitStack() as stack:
            for name in helpers:
                stack.enter_context(patch.object(b, name, side_effect=AssertionError(name + ' must remain read-only')))
            run = stack.enter_context(patch.object(b, 'run'))
            b.main(['--archive', str(self.archive), '--uv', str(self.base / 'bin/uv.exe')])
        self.assertEqual(Path(run.call_args.args[0][0]), release / 'venv/Scripts/python.exe')
        self.assertIn('--configure', run.call_args.args[0])

    def test_unconfirmed_handover_cannot_replace_the_task_or_its_runner(self):
        release, _ = b.windows_release(self.archive, self.base)
        runner = release / 'controller.ps1'
        runner.write_text('previous working runner')
        with patch.object(c, 'handover', side_effect=RuntimeError('unconfirmed')) as handover, \
             patch.object(b, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                b.install_service(release, ['owned-python', '-m', 'supervisor'])
        handover.assert_called_once_with(self.base / 'controller')
        run.assert_not_called()
        self.assertEqual(runner.read_text(), 'previous working runner')


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'control'
        self.receipt = dict(id='owned', workspace='/release/package', tag='owned-controller',
                            engine='shell', created_at_ms=1, command=['owned-python'])

    def test_exact_generation_handover_requires_exit_ack_and_released_lock(self):
        errors = []
        with c.ControllerLease(self.directory) as lease:
            lease.publish(self.receipt)
            def request():
                try:
                    c.handover(self.directory, timeout=2)
                except BaseException as error:
                    errors.append(error)
            thread = threading.Thread(target=request)
            thread.start()
            try:
                deadline = time.monotonic() + 1
                while not lease.stop_requested() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(lease.stop_requested())
                lease.confirm_exit()
                self.assertTrue(thread.is_alive(), 'ack alone cannot release a live owner')
            finally:
                # Owner context releases the kernel lock before joining below.
                pass
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_wrong_generation_and_missing_exit_proof_preserve_owner(self):
        with c.ControllerLease(self.directory) as lease:
            lease.publish(self.receipt)
            c._write(self.directory / 'stop.json', {'generation': '0' * 32})
            self.assertFalse(lease.stop_requested())
            c._write(self.directory / 'stopped.json', {'generation': '0' * 32, 'containment_empty': True})
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                c.handover(self.directory, timeout=0)
            self.assertTrue((self.directory / 'owner.json').exists())
        with self.assertRaisesRegex(c.NeedsAttention, 'exit is unproven'):
            with c.ControllerLease(self.directory, observe=lambda receipt: None):
                self.fail('must preserve crashed owner evidence')
        self.assertEqual(c._read(self.directory / 'needs-attention.json')['status'], 'needs_attention')

    def crash(self, boot='boot-before'):
        with c.ControllerLease(self.directory, boot_probe=lambda: boot) as lease:
            lease.publish(self.receipt)
        return c._read(self.directory / 'owner.json')

    def test_reboot_archives_old_receipt_and_allows_next_controller(self):
        previous = self.crash()
        with c.ControllerLease(self.directory, boot_probe=lambda: 'boot-after',
                               observe=lambda receipt: self.fail('reboot proof needs no native lookup')) as lease:
            self.assertFalse((self.directory / 'owner.json').exists())
            archived = [c._read(p) for p in (self.directory / 'archive').iterdir()]
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0]['owner'], previous)
            self.assertEqual(archived[0]['proof'], 'boot_changed')
            lease.publish(dict(self.receipt, id='new-controller'))
            lease.confirm_exit()

    def test_same_boot_crashed_owner_recovers_only_exact_empty_containment(self):
        previous = self.crash()
        calls = []
        def observe(receipt):
            calls.append(receipt)
            return dict(receipt, containment_empty=True)
        with c.ControllerLease(self.directory, boot_probe=lambda: 'boot-before', observe=observe):
            self.assertFalse((self.directory / 'owner.json').exists())
        self.assertEqual(calls, [previous['receipt']])
        archive = next((self.directory / 'archive').iterdir())
        self.assertEqual(c._read(archive)['proof'], 'exact_receipt_containment_empty')

    def test_unknown_missing_mismatched_or_nonempty_receipt_never_recovers(self):
        self.crash()
        previous = (self.directory / 'owner.json').read_bytes()
        rows = [None, {}, dict(self.receipt, containment_empty=False),
                dict(self.receipt, containment_empty=True, id='another-controller'),
                dict(self.receipt, containment_empty=True, created_at_ms=2)]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(c.NeedsAttention):
                with c.ControllerLease(self.directory, boot_probe=lambda: None, observe=lambda receipt: row):
                    self.fail('unknown proof cannot start a replacement')
            self.assertEqual((self.directory / 'owner.json').read_bytes(), previous)
            self.assertFalse((self.directory / 'archive').exists())
        with self.assertRaises(c.NeedsAttention):
            c.handover(self.directory, timeout=0, boot_probe=lambda: 'boot-before', observe=lambda receipt: None)
        self.assertFalse((self.directory / 'stop.json').exists())

    def test_rerun_recovers_needs_attention_when_receipt_absent_from_catalog(self):
        self.crash()
        c._write_attention(self.directory)
        with c.ControllerLease(self.directory, boot_probe=lambda: None) as lease, \
                patch.object(c, 'observe_receipt', return_value=None), \
                patch.object(c, 'receipt_absent', return_value=True):
            pass
        self.assertFalse((self.directory / 'needs-attention.json').exists())
        self.assertFalse((self.directory / 'owner.json').exists())

    def test_receipt_absent_requires_successful_complete_listing(self):
        ok = Mock(returncode=0, stdout=json.dumps([{'id': 'other'}]))
        with patch.object(c.subprocess, 'run', return_value=ok):
            self.assertTrue(c.receipt_absent({'id': 'mine'}))
            self.assertFalse(c.receipt_absent({'id': 'other'}))
        with patch.object(c.subprocess, 'run', return_value=Mock(returncode=1, stdout='[]')):
            self.assertFalse(c.receipt_absent({'id': 'mine'}))
        with patch.object(c.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertFalse(c.receipt_absent({'id': 'mine'}))

    def test_installer_recovers_stale_owner_without_waiting_for_dead_supervisor(self):
        self.crash()
        c.handover(self.directory, timeout=0, boot_probe=lambda: 'boot-after',
                   observe=lambda receipt: self.fail('must use boot proof'))
        self.assertFalse((self.directory / 'owner.json').exists())
        self.assertFalse((self.directory / 'stop.json').exists())

    def test_exit_ack_published_before_interrupted_owner_removal_is_recoverable(self):
        previous = self.crash()
        c._write(self.directory / 'stopped.json', {'generation': previous['generation'], 'containment_empty': True})
        with c.ControllerLease(self.directory, boot_probe=lambda: None,
                               observe=lambda receipt: self.fail('ack already proves the owned exit')):
            self.assertFalse((self.directory / 'owner.json').exists())
        archive = next((self.directory / 'archive').iterdir())
        self.assertEqual(c._read(archive)['proof'], 'owned_exit_acknowledged')

    def test_recovery_archive_failure_preserves_the_original_owner(self):
        self.crash()
        previous = (self.directory / 'owner.json').read_bytes()
        original = c._write
        def fail_archive(path, value):
            if path.parent.name == 'archive':
                raise OSError('disk full')
            return original(path, value)
        with patch.object(c, '_write', side_effect=fail_archive):
            with self.assertRaisesRegex(OSError, 'disk full'):
                with c.ControllerLease(self.directory, boot_probe=lambda: 'boot-after'):
                    self.fail('must retain evidence when archiving fails')
        self.assertEqual((self.directory / 'owner.json').read_bytes(), previous)

    def test_unknown_startup_logs_needs_attention_and_retries_without_a_new_child(self):
        self.crash()
        script = Path(__file__).resolve().parents[2] / 'scripts/session-host-supervisor.py'
        spec = importlib.util.spec_from_file_location('bootstrap_unknown_startup', script)
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        config = Path(self.temp.name) / 'config.json'
        config.write_text('{}')
        argv = [str(script), '--workspace', self.temp.name, '--config', str(config),
                '--credential-config', str(config), '--journal-dir', str(self.directory / 'journal'),
                '--control-dir', str(self.directory)]
        import contextlib, io
        output = io.StringIO()
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        stop.wait.return_value = True
        with patch.object(supervisor.sys, 'argv', argv), \
             patch.object(supervisor, 'is_windows', return_value=True), \
             patch.object(supervisor.signal, 'signal'), patch.dict(os.environ), \
             patch.object(c, 'boot_identity', return_value=None), \
             patch.object(c, 'observe_receipt', return_value=None), \
             patch.object(c, 'receipt_absent', return_value=False), \
             patch.object(supervisor.threading, 'Event', return_value=stop), \
             patch.object(supervisor, 'native') as native, contextlib.redirect_stdout(output):
            # Intake 88ff6766: Task Scheduler never restarts a finished task, so
            # the supervisor stays up and retries recovery instead of exiting.
            self.assertIsNone(supervisor.main())
        native.assert_not_called()
        event = json.loads(output.getvalue())
        self.assertEqual((event['event'], event['type']), ('controller_retry', 'NeedsAttention'))
        self.assertEqual(event['retry_in_s'], supervisor.RETRY_DELAYS_S[0])
        stop.wait.assert_called_once_with(1)
        self.assertTrue((self.directory / 'owner.json').exists())

    def test_live_owner_lock_prevents_boot_based_recovery(self):
        with c.ControllerLease(self.directory, boot_probe=lambda: 'boot-before') as lease:
            lease.publish(self.receipt)
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                c.handover(self.directory, timeout=0, boot_probe=lambda: 'boot-after')
            self.assertTrue((self.directory / 'owner.json').exists())
            self.assertFalse((self.directory / 'archive').exists())
            self.assertFalse((self.directory / 'stop.json').exists())

    def test_timed_out_cancelled_and_expired_requests_cannot_stop_a_recovered_owner(self):
        with c.ControllerLease(self.directory) as lease:
            lease.publish(self.receipt)
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                c.handover(self.directory, timeout=0)
            self.assertFalse((self.directory / 'stop.json').exists())
            self.assertFalse(lease.stop_requested())
            # Hard-kill residue has an independent monotonic expiry.
            now = time.monotonic()
            request = {'generation': lease.generation, 'request_id': 'abandoned',
                       'created_monotonic': now - 91, 'expires_monotonic': now - 1}
            c._write(self.directory / 'stop.json', request)
            self.assertFalse(lease.stop_requested())
            request.update(created_monotonic=now, expires_monotonic=now + 90)
            c._write(self.directory / 'stop.json', request)
            self.assertTrue(lease.stop_requested())
            with patch.object(c.time, 'sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    c.handover(self.directory, timeout=2)
            self.assertFalse((self.directory / 'stop.json').exists())
            self.assertFalse(lease.stop_requested())

    def test_boot_probe_is_read_only_and_failed_or_malformed_output_is_unknown(self):
        from types import SimpleNamespace
        with patch.object(c.os, 'name', 'nt'), patch.object(c.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout='2026-10-29T12:00:00.1234567Z\n')
            self.assertEqual(c.boot_identity(), 'windows-boot:2026-10-29T12:00:00.1234567Z')
            self.assertEqual(run.call_args.kwargs['timeout'], 10)
            self.assertIn('Get-CimInstance', run.call_args.args[0][-1])
            for result in (SimpleNamespace(returncode=1, stdout=''),
                           SimpleNamespace(returncode=0, stdout='unknown')):
                run.return_value = result
                self.assertIsNone(c.boot_identity())

    def test_supervisor_only_acknowledges_its_exact_child_after_native_exit_proof(self):
        script = Path(__file__).resolve().parents[2] / 'scripts/session-host-supervisor.py'
        for mode in ('confirmed', 'unproven', 'changed_identity'):
            with self.subTest(mode=mode):
                spec = importlib.util.spec_from_file_location('bootstrap_supervisor_contract', script)
                supervisor = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(supervisor)
                home = Path(self.temp.name) / mode
                home.mkdir()
                config = home / 'config.json'
                config.write_text('{}')
                control = home / 'control'
                receipt, calls = {}, []
                def native(*args):
                    calls.append(args)
                    if args[0] == 'start':
                        receipt.update(id='only-child', tag=args[args.index('--tag') + 1],
                            workspace=str(home.resolve()), engine='shell', created_at_ms=1,
                            command=list(args[args.index('--') + 1:]))
                    if args[0] == 'kill':
                        return {'id': receipt['id'], 'record_removed': mode == 'confirmed'}
                    row = dict(receipt)
                    if args[0] == 'status' and mode == 'changed_identity':
                        row['id'] = 'other-child'
                    return row
                argv = [str(script), '--workspace', str(home), '--config', str(config),
                        '--credential-config', str(config), '--journal-dir', str(home / 'journal'),
                        '--control-dir', str(control)]
                stop = Mock()
                stop.wait.return_value = False
                stop.is_set.return_value = False
                with patch.object(supervisor.sys, 'argv', argv), \
                     patch.object(supervisor, 'is_windows', return_value=True), \
                     patch.object(supervisor.signal, 'signal'), \
                     patch.dict(os.environ), \
                     patch.object(supervisor.threading, 'Event', return_value=stop), \
                     patch.object(c.ControllerLease, 'stop_requested', return_value=True), \
                     patch('time.sleep'), \
                     patch.object(supervisor, 'native', side_effect=native):
                    with self.assertRaises(SystemExit) as exit:
                        supervisor.main()
                self.assertEqual(exit.exception.code, 76, 'accepted handover ends the controller.ps1 loop')
                kills = [call for call in calls if call[0] == 'kill']
                self.assertEqual(len(kills), 0 if mode == 'changed_identity' else 1)
                self.assertEqual((control / 'stopped.json').exists(), mode == 'confirmed')
                self.assertEqual((control / 'owner.json').exists(), mode != 'confirmed')
                self.assertEqual((control / 'needs-attention.json').exists(), mode != 'confirmed')


if __name__ == '__main__':
    unittest.main()


class ObservedWorkspaceDriftTest(unittest.TestCase):
    def test_recovery_accepts_exact_receipt_with_drifted_workspace(self):
        from agent_hub_host import bootstrap_control as bc
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            receipt = dict(id='owned', workspace='/release/package', tag='t', engine='shell',
                           created_at_ms=1, command=['python'])
            bc._write(d / 'owner.json', {'version': 2, 'generation': 'a' * 32, 'boot_identity': 'b', 'receipt': receipt})
            row = dict(receipt, workspace='C:/Temp', containment_empty=True)
            bc.recover_stale_owner(d, 'b', lambda r: row)
            self.assertFalse((d / 'owner.json').exists())
