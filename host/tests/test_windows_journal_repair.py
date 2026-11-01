"""Windows journal DACL repair (intake 61bba72f): the Tabo host journal dir
predated CPython's os.mkdir(mode=0o700) protected-DACL behavior, inherited
ambient %LOCALAPPDATA% ACEs (CodexSandboxUsers RX), and then failed every
private_fs journal operation forever - the host-receive cycle died with
PermissionError on each pending offer and session commands were never
claimed (stop_not_claimed, 0 attempts).
"""
import contextlib
import ctypes as C
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from agent_hub_host import session_host, win32_fs as win


class RepairFakeAPI:
    """Simulated Win32 ACL boundary for the bounded tree repair."""
    def __init__(self):
        self.calls, self.closed = [], []
        self.fail_private = set()
        self.owner_sid = 77
        self.set_error = 0
        self.security_dirs = []
        self.user_sid = 77

    def GetDriveTypeW(self, root):
        return 3

    def GetVolumeInformationW(self, root, *args):
        args[-2].value = 'NTFS'
        return 1

    def check(self, value):
        if not value:
            raise OSError('simulated API failure')

    def open(self, path, access, disposition=3, directory=False, security=None):
        self.calls.append((str(path), access, disposition, directory))
        return 0x200000000 + len(self.calls)

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def private(self, handle, **kwargs):
        if handle in self.fail_private:
            raise PermissionError('simulated unprotected DACL')
        return True

    @contextlib.contextmanager
    def security(self, directory=False):
        self.security_dirs.append(bool(directory))
        sa = win.SecurityAttributes()
        sa.descriptor = 0xABCD
        yield sa

    def GetSecurityInfo(self, handle, kind, fields, owner, group, dacl, sacl, sd):
        C.cast(owner, C.POINTER(win.PTR))[0] = self.owner_sid
        return 0

    def GetSecurityDescriptorDacl(self, sd, present, dacl, defaulted):
        C.cast(present, C.POINTER(win.BOOL))[0] = 1
        C.cast(dacl, C.POINTER(win.PTR))[0] = 0xACDC
        C.cast(defaulted, C.POINTER(win.BOOL))[0] = 0
        return 1

    def SetSecurityInfo(self, handle, kind, fields, owner, group, dacl, sacl):
        self.calls.append(('set', handle, fields, dacl is not None))
        if self.set_error:
            return self.set_error
        self.fail_private.discard(handle)  # the applied DACL protects the entry
        return 0

    def EqualSid(self, left, right):
        left = getattr(left, 'value', left)
        right = getattr(right, 'value', right)
        return left == right


def fake_entries(spec):
    """Replace os.scandir for one walk with a fixed directory shape."""
    def scandir(path):
        text = str(path)
        entries = []
        for name, kind in spec.get(text, []):
            entries.append(types.SimpleNamespace(
                name=name,
                is_dir=lambda follow_symlinks=False, k=kind: k == 'dir',
                is_file=lambda follow_symlinks=False, k=kind: k == 'file'))
        return contextlib.nullcontext(entries)
    return scandir


class SimulatedRepairTests(unittest.TestCase):
    ROOT = r'C:\Work\Private\journal'

    def setUp(self):
        self.a = RepairFakeAPI()
        patcher = patch.object(win, 'api', return_value=self.a)
        patcher.start()
        self.addCleanup(patcher.stop)

    def repaired(self, spec, **kwargs):
        with patch.object(win.os, 'scandir', fake_entries(spec)):
            return win.repair_private_tree(self.ROOT, **kwargs)

    def test_protects_only_failing_entries_and_reports_the_count(self):
        spec = {self.ROOT: [('state.json', 'file'), ('sub', 'dir')],
                self.ROOT + r'\sub': [('inner.json', 'file')]}
        # Every entry starts unprotected and gets a DACL of its own kind.
        self.a.fail_private = set(range(0x200000001, 0x200000010))
        count = self.repaired(spec)
        sets = [c for c in self.a.calls if c[0] == 'set']
        self.assertEqual(count, 4)
        self.assertEqual(len(sets), 4)
        self.assertEqual(self.a.security_dirs, [True, False, True, False])

    def test_already_private_entries_are_left_untouched(self):
        spec = {self.ROOT: []}
        self.assertEqual(self.repaired(spec), 0)
        self.assertEqual([c for c in self.a.calls if c[0] == 'set'], [])

    def test_non_owner_entry_refuses_without_writing_a_dacl(self):
        self.a.fail_private = {0x200000001}
        self.a.owner_sid = 88
        with self.assertRaises(PermissionError):
            self.repaired({})
        self.assertEqual([c for c in self.a.calls if c[0] == 'set'], [])

    def test_failed_dacl_application_is_fatal_and_closes_handles(self):
        self.a.fail_private = {0x200000001}
        self.a.set_error = 5
        with self.assertRaises(OSError):
            self.repaired({})
        opens = [c for c in self.a.calls if c[0] != 'set']
        self.assertEqual(len(self.a.closed), len(opens))

    def test_failed_final_proof_raises(self):
        self.a.fail_private = set(range(0x200000001, 0x200000010))

        def never_protects(self_api, handle, kind, fields, owner, group, dacl, sacl):
            self_api.calls.append(('set', handle, fields, dacl is not None))
            return 0  # claims success but private() keeps failing

        with patch.object(RepairFakeAPI, 'SetSecurityInfo', never_protects):
            with self.assertRaises(PermissionError):
                self.repaired({})

    def test_entry_and_depth_bounds_fail_closed(self):
        spec = {self.ROOT: [('a', 'file'), ('b', 'file'), ('c', 'file')]}
        with self.assertRaises(PermissionError):
            self.repaired(spec, max_entries=2)
        deep = {self.ROOT: [('l0', 'dir')],
                self.ROOT + r'\l0': [('l1', 'dir')],
                self.ROOT + r'\l0\l1': []}
        with self.assertRaises(PermissionError):
            self.repaired(deep, max_depth=1)

    def test_non_regular_entry_is_refused(self):
        spec = {self.ROOT: [('link', 'other')]}
        with self.assertRaises(PermissionError):
            self.repaired(spec)


@unittest.skipUnless(os.name == 'nt', 'Real Windows DACL repair evidence')
class RealWindowsRepairTests(unittest.TestCase):
    def test_inherited_ambient_tree_becomes_fully_private(self):
        base = tempfile.mkdtemp(prefix='hub-journal-repair-')
        try:
            (pathlib.Path(base) / 'sub').mkdir()
            (pathlib.Path(base) / 'state.json').write_text('{}', encoding='utf-8')
            (pathlib.Path(base) / 'sub' / 'inner.json').write_text('{}', encoding='utf-8')
            a = win.api()

            def probe(path, directory):
                handle = a.open(path, 0, directory=directory)
                try:
                    a.private(handle, directory=directory)
                    return True
                except PermissionError:
                    return False
                finally:
                    a.CloseHandle(handle)

            win.repair_private_tree(base)
            self.assertTrue(probe(base, True))
            self.assertTrue(probe(os.path.join(base, 'state.json'), False))
            self.assertTrue(probe(os.path.join(base, 'sub'), True))
            self.assertTrue(probe(os.path.join(base, 'sub', 'inner.json'), False))
            # Idempotent: a second pass re-protects nothing.
            self.assertEqual(win.repair_private_tree(base), 0)
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class SessionHostWiringTests(unittest.TestCase):
    def setUp(self):
        self._thread_patches = [
            patch.object(session_host, 'maybe_start_status_thread', return_value=None),
            patch.object(session_host, 'maybe_start_metrics_thread', return_value=None),
            patch.object(session_host, 'maybe_start_disk_thread', return_value=None),
            patch.object(session_host, 'maybe_start_host_health_thread', return_value=None),
            patch.object(session_host, 'maybe_start_role_consumer_thread', return_value=None),
        ]
        for p in self._thread_patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self._thread_patches)])

    def base_patches(self, directory):
        return [
            patch.object(session_host.config, 'load_config',
                         return_value={'machine_id': 'test-machine', 'access_token': 'synthetic'}),
            patch.object(session_host.fleet, 'load_settings', return_value={}),
            patch.object(session_host, 'qualify_sender_binding',
                         return_value=types.SimpleNamespace(session_id='test-native',
                                                            workspace=str(pathlib.Path.cwd()))),
            patch.object(session_host.fleet, 'Dispatcher'),
            patch.object(session_host.session_control, 'execute'),
        ]

    def test_run_invokes_repair_on_the_resolved_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            patches = self.base_patches(directory)
            with patch.object(session_host, 'ensure_private_journal') as repair:
                for p in patches:
                    p.start()
                try:
                    session_host.run('synthetic-settings', directory, cycles=1)
                finally:
                    for p in reversed(patches):
                        p.stop()
            repair.assert_called_once_with(pathlib.Path(directory).resolve())

    @unittest.skipUnless(os.name == 'nt', 'Real Windows journal repair through run()')
    def test_run_repairs_plain_created_journal_before_use(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = pathlib.Path(directory) / 'legacy-state.json'
            legacy.write_text('{}', encoding='utf-8')  # inherits ambient DACLs
            patches = self.base_patches(directory)
            for p in patches:
                p.start()
            try:
                session_host.run('synthetic-settings', directory, cycles=1)
            finally:
                for p in reversed(patches):
                    p.stop()
            from agent_hub_host import private_fs
            data = private_fs.read_bytes(str(legacy), 1024)
            self.assertEqual(data, b'{}')


if __name__ == '__main__':
    unittest.main()
