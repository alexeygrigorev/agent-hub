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
        with self.assertRaisesRegex(RuntimeError, 'exit is unproven'):
            with c.ControllerLease(self.directory):
                self.fail('must preserve crashed owner evidence')

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
                with patch.object(supervisor.sys, 'argv', argv), \
                     patch.object(supervisor, 'is_windows', return_value=True), \
                     patch.object(supervisor.signal, 'signal'), \
                     patch.object(supervisor.threading, 'Event', return_value=stop), \
                     patch.object(c.ControllerLease, 'stop_requested', return_value=True), \
                     patch.object(supervisor, 'native', side_effect=native):
                    supervisor.main()
                kills = [call for call in calls if call[0] == 'kill']
                self.assertEqual(len(kills), 0 if mode == 'changed_identity' else 1)
                self.assertEqual((control / 'stopped.json').exists(), mode == 'confirmed')
                self.assertEqual((control / 'owner.json').exists(), mode != 'confirmed')


if __name__ == '__main__':
    unittest.main()
