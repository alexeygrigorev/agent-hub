import contextlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_hub_host import repo_clone_windows as clone_fs


class WindowsCloneTests(unittest.TestCase):
    def test_host_enables_supported_cloning_without_windows_worktree_effects(self):
        from agent_hub_host import fleet
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = {'workspace_roots':[str(root)],'repos':{'checkout_root':str(root),'worktree_root':str(root/'trees')}}
            with patch.object(fleet,'repo_provisioning_supported',return_value=False), \
                    patch.object(clone_fs,'supported',return_value=True):
                layout = fleet.repo_layout(settings)
                self.assertTrue(layout['clone_enabled'])
                self.assertEqual(layout['checkout_root'],root)
                self.assertIsNone(layout['worktree_root'])
            with patch.object(fleet,'os',SimpleNamespace(name='nt')), \
                    patch.object(clone_fs,'supported',return_value=True), \
                    patch.object(fleet,'repo_layout',return_value=layout), \
                    patch.object(clone_fs,'clone',return_value=root/'demo') as adapter:
                self.assertEqual(fleet.guarded_repo_clone(settings,'demo','org/demo',str(root/'demo'),disk_floor_gb=15),root/'demo')
                self.assertEqual(adapter.call_args.kwargs['disk_floor_gb'],15)
                self.assertIs(adapter.call_args.kwargs['auth_ok'],fleet.gh_auth_ok)

    def test_native_lock_open_pins_the_handle_and_closes_it_on_conversion_error(self):
        import os
        import sys
        from unittest.mock import Mock
        from agent_hub_host import win32_fs
        native = Mock()
        native.open.return_value = 123
        crt = SimpleNamespace(open_osfhandle=Mock(return_value=7))
        environment = SimpleNamespace(name='nt',O_RDWR=os.O_RDWR,O_BINARY=32768,O_NOINHERIT=128,fdopen=Mock(),close=Mock())
        path = Path('/lock')
        with patch.object(clone_fs,'os',environment), patch.dict(sys.modules,{'msvcrt':crt}), \
                patch.object(win32_fs,'api',return_value=native):
            clone_fs._open_lock(path)
            native.open.assert_called_once_with(path,0xC0000000,disposition=4)
            self.assertTrue(crt.open_osfhandle.call_args.args[1] & environment.O_NOINHERIT)
            native.CloseHandle.assert_not_called()
            crt.open_osfhandle.side_effect = OSError('conversion')
            with self.assertRaises(OSError):
                clone_fs._open_lock(path)
            native.CloseHandle.assert_called_once_with(123)
            crt.open_osfhandle.side_effect = None
            environment.fdopen.side_effect = OSError('stream')
            with self.assertRaises(OSError):
                clone_fs._open_lock(path)
            environment.close.assert_called_once_with(7)

    def fixture(self, callback):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = {'workspace_roots': [str(root)], 'repos': {'checkout_root': str(root)}}
            usage = SimpleNamespace(total=1000*1024**3, used=100*1024**3, free=900*1024**3)
            with patch.object(clone_fs, 'checkout_lock', side_effect=lambda p: contextlib.nullcontext()) as lock, \
                    patch.object(clone_fs.shutil, 'disk_usage', return_value=usage):
                callback(root, settings, lock)

    def call(self, root, settings, **kwargs):
        return clone_fs.clone(settings, 'demo', 'org/demo', str(root/'demo'),
                              disk_floor_gb=15, auth_ok=lambda: True,
                              verify_origin=lambda *args: None, **kwargs)

    def test_clone_publishes_complete_checkout_and_uses_lock(self):
        def check(root, settings, lock):
            def run(argv, **kwargs):
                (Path(argv[4])/'.git').mkdir()
                return 0
            with patch.object(clone_fs, '_run', side_effect=run) as run:
                self.assertEqual(self.call(root, settings), root/'demo')
                self.assertEqual(self.call(root, settings), root/'demo')
                run.assert_called_once()
            self.assertEqual(lock.call_args.args[0], root/'.hub-clone-demo.lock')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['demo'])
        self.fixture(check)

    def test_failure_removes_only_new_staging_and_preserves_siblings(self):
        def check(root, settings, lock):
            sibling = root/'.hub-clone-demo-old'; sibling.mkdir()
            (sibling/'keep').write_text('keep')
            with patch.object(clone_fs, '_run', return_value=1):
                with self.assertRaisesRegex(ValueError, 'repo_clone_failed'):
                    self.call(root, settings)
            self.assertEqual(list(root.iterdir()), [sibling])
            self.assertEqual((sibling/'keep').read_text(), 'keep')
        self.fixture(check)

    def test_explicit_refusal_reasons(self):
        def check(root, settings, lock):
            with patch.object(clone_fs, '_run') as run:
                for changes, reason in [({'disk_state':'warn'}, 'host_disk_warn_no_clone'),
                                         ({'disk_floor_gb':999}, 'host_disk_warn_no_clone'),
                                         ({'origin':''}, 'repo_origin_missing'),
                                         ({'auth_ok':lambda: False}, 'host_gh_auth_unavailable'),
                                         ({'target_dir':str(root/'other')}, 'repo_checkout_missing')]:
                    args = dict(settings=settings, repo='demo', origin='org/demo', target_dir=str(root/'demo'),
                                disk_floor_gb=15, auth_ok=lambda: True, verify_origin=lambda *a:None)
                    args.update(changes)
                    with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                        clone_fs.clone(**args)
                run.assert_not_called()
        self.fixture(check)

    def test_existing_unrelated_directory_is_preserved(self):
        def check(root, settings, lock):
            (root/'demo').mkdir(); (root/'demo'/'keep').write_text('keep')
            with self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                self.call(root, settings)
            self.assertEqual((root/'demo'/'keep').read_text(), 'keep')
        self.fixture(check)

    def test_unknown_disk_and_unavailable_roots_fail_with_explicit_reasons(self):
        def check(root, settings, lock):
            with patch.object(clone_fs.shutil, 'disk_usage', side_effect=OSError('unavailable')):
                with self.assertRaisesRegex(ValueError, 'host_disk_warn_no_clone'):
                    self.call(root, settings)
            for total, free in [(float('nan'), 100), (100, float('inf')), (100, 101)]:
                with patch.object(clone_fs.shutil, 'disk_usage', return_value=SimpleNamespace(total=total, used=1, free=free)):
                    with self.assertRaisesRegex(ValueError, 'host_disk_warn_no_clone'):
                        self.call(root, settings)
            settings['workspace_roots'] = [str(root/'missing')]
            with self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                self.call(root, settings)
        self.fixture(check)

    def test_windows_lock_unlock_and_timeout(self):
        import sys
        from unittest.mock import Mock
        api = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=Mock())
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'msvcrt':api}):
            lock = Path(directory)/'lock'
            with clone_fs.checkout_lock(lock):
                self.assertEqual(lock.read_bytes(), b'\0')
            self.assertEqual([c.args[1] for c in api.locking.call_args_list], [1, 2])
            api.locking.side_effect = OSError('busy')
            with self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                with clone_fs.checkout_lock(lock, timeout=0):
                    self.fail('lock admitted two holders')

    def test_reparse_destination_is_rejected_before_clone(self):
        def check(root, settings, lock):
            (root/'demo').mkdir(); (root/'demo'/'.git').mkdir()
            original = clone_fs._plain
            with patch.object(clone_fs, '_plain', side_effect=lambda path: path != root/'demo' and original(path)), \
                    patch.object(clone_fs, '_run') as run:
                with self.assertRaisesRegex(ValueError, 'repo_checkout_missing'):
                    self.call(root, settings)
                run.assert_not_called()
        self.fixture(check)

    def test_origin_verification_failure_never_publishes_clone(self):
        def check(root, settings, lock):
            def run(argv, **kwargs):
                (Path(argv[4])/'.git').mkdir()
                return 0
            def reject(*args):
                raise ValueError('repo_origin_mismatch')
            with patch.object(clone_fs, '_run', side_effect=run):
                with self.assertRaisesRegex(ValueError, 'repo_origin_mismatch'):
                    clone_fs.clone(settings, 'demo', 'org/demo', str(root/'demo'), disk_floor_gb=15,
                                   auth_ok=lambda:True, verify_origin=reject)
            self.assertEqual(list(root.iterdir()), [])
        self.fixture(check)

    def test_timed_out_command_terminates_owned_windows_tree(self):
        import subprocess
        from unittest.mock import Mock
        process = Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired('gh', 1), 1]
        with patch.object(clone_fs.subprocess, 'Popen', return_value=process), \
                patch.object(clone_fs.subprocess, 'run') as taskkill:
            with self.assertRaises(subprocess.TimeoutExpired):
                clone_fs._run(['gh','repo','clone','org/demo'], timeout=1)
            self.assertEqual(taskkill.call_args.args[0], ['taskkill','/PID','123','/T','/F'])
            self.assertEqual(process.wait.call_count, 2)

    def test_readonly_git_object_cleanup_retries_and_preserves_origin_reason(self):
        import os
        import stat
        original_rmtree = clone_fs.shutil.rmtree

        def check(root, settings, lock):
            def run(argv, **kwargs):
                git = Path(argv[4])/'.git'
                git.mkdir()
                (git/'pack').write_bytes(b'object')
                return 0

            def windows_rmtree(path, *, onerror):
                pack = path/'.git'/'pack'
                pack.chmod(stat.S_IREAD)
                def unlink(filename):
                    self.assertTrue(Path(filename).stat().st_mode & stat.S_IWRITE)
                    os.unlink(filename)
                onerror(unlink, str(pack), (PermissionError, PermissionError('readonly'), None))
                original_rmtree(path)

            with patch.object(clone_fs, '_run', side_effect=run), \
                    patch.object(clone_fs.shutil, 'rmtree', side_effect=windows_rmtree):
                with self.assertRaisesRegex(ValueError, 'repo_origin_mismatch'):
                    clone_fs.clone(settings, 'demo', 'org/demo', str(root/'demo'), disk_floor_gb=15,
                                   auth_ok=lambda:True,
                                   verify_origin=lambda *a: (_ for _ in ()).throw(ValueError('repo_origin_mismatch')))
            self.assertEqual(list(root.iterdir()), [])
        self.fixture(check)

    def test_permanent_cleanup_failure_does_not_mask_clone_reason(self):
        original_rmtree = clone_fs.shutil.rmtree
        def check(root, settings, lock):
            with patch.object(clone_fs, '_run', return_value=1), \
                    patch.object(clone_fs.shutil, 'rmtree', side_effect=PermissionError('busy')):
                with self.assertRaisesRegex(ValueError, 'repo_clone_failed'):
                    self.call(root, settings)
            # Only this test's owned staging is removed after releasing the fake failure.
            for staging in root.iterdir():
                original_rmtree(staging)
        self.fixture(check)
