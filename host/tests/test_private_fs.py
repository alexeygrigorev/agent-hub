"""Portable executable smoke: stdlib unittest; fake Win32 cases are labelled simulations."""
from contextlib import contextmanager
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host import private_fs as fs, win32_fs as win


class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Windows temp parent is not guaranteed owner-only. Let the adapter create
        # the private leaf with its initial protected descriptor, before any data.
        self.path = self.root / 'private' / 'state.json'

    def test_positive_roundtrip_replacement_and_bounds(self):
        fs.atomic_write(self.path, b'{"version":1}', 1024)
        fs.atomic_write(self.path, b'{"version":2,"extension":true}', 1024)
        self.assertEqual(json.loads(fs.read_bytes(self.path, 1024))['version'], 2)
        with self.assertRaises(ValueError):
            fs.read_bytes(self.path, 2)
        with self.assertRaises(ValueError):
            fs.atomic_write(self.path, b'x' * 1025, 1024)
        self.assertEqual(json.loads(fs.read_bytes(self.path, 1024))['extension'], True)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_real_subprocess_contention_and_process_death_release(self):
        lock = self.path.with_suffix('.lock')
        program = '''import sys,time
from agent_hub_host.private_fs import exclusive_lock
with exclusive_lock(sys.argv[1]):
 print('locked', flush=True)
 time.sleep(30)
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                   PYTHONDONTWRITEBYTECODE='1')
        child = subprocess.Popen([sys.executable, '-u', '-c', program, str(lock)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaises(OSError):
                with fs.exclusive_lock(lock):
                    self.fail('second process obtained lock')
        finally:
            child.kill()
            child.communicate(timeout=5)
        with fs.exclusive_lock(lock):
            self.assertTrue(lock.is_file())
        # Closing/reopening must not remove a persistent lock inode.
        with fs.exclusive_lock(lock):
            pass

    def test_hardlinks_rejected_for_reads_writes_and_locks(self):
        fs.atomic_write(self.path, b'original', 1024)
        alias = self.path.with_name('alias')
        os.link(self.path, alias)
        for operation in (lambda: fs.read_bytes(self.path, 1024),
                          lambda: fs.atomic_write(self.path, b'changed', 1024)):
            with self.assertRaises(OSError):
                operation()
        with self.assertRaises(OSError):
            with fs.exclusive_lock(self.path):
                self.fail('hardlink lock accepted')
        self.assertEqual(alias.read_bytes(), b'original')

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows reparse evidence only')
    def test_real_windows_reparse_refused_if_symlink_setup_supported(self):
        fs.atomic_write(self.path, b'original', 1024)
        link = self.root / 'junction-like-link'
        try:
            link.symlink_to(self.path.parent, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314:
                self.skipTest('OS privilege/developer mode does not allow symlink fixture')
            raise
        with self.assertRaises(OSError):
            fs.read_bytes(link / self.path.name, 1024)
        self.assertEqual(fs.read_bytes(self.path, 1024), b'original')

    @unittest.skipIf(os.name == 'nt', 'POSIX mode and nofollow evidence')
    def test_unsafe_modes_dangling_symlink_fifo_and_readonly(self):
        fs.atomic_write(self.path, b'original', 1024)
        os.chmod(self.path, 0o644)
        with self.assertRaises(PermissionError):
            fs.read_bytes(self.path, 1024)
        os.chmod(self.path, 0o600)
        link = self.path.with_name('link')
        link.symlink_to('missing')
        with self.assertRaises(OSError):
            fs.atomic_write(link, b'x', 1024)
        fifo = self.path.with_name('fifo')
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(OSError):
            fs.read_bytes(fifo, 1024)
        os.chmod(self.path.parent, 0o500)
        try:
            with self.assertRaises(PermissionError):
                fs.atomic_write(self.path, b'changed', 1024)
        finally:
            os.chmod(self.path.parent, 0o700)
        self.assertEqual(self.path.read_bytes(), b'original')

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor-relative race evidence')
    def test_ancestor_symlink_and_swap_after_open_do_not_redirect(self):
        fs.atomic_write(self.path, b'original', 1024)
        foreign = self.root/'foreign'
        foreign.mkdir(mode=0o700)
        alias = self.root/'alias'
        alias.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            fs.read_bytes(alias/self.path.name, 1024)
        real_replace = os.replace
        def swap(source, target, **kwargs):
            self.path.parent.rename(self.root/'moved')
            self.path.parent.symlink_to(foreign, target_is_directory=True)
            return real_replace(source, target, **kwargs)
        with patch.object(fs.os, 'replace', side_effect=swap):
            fs.atomic_write(self.path, b'anchored', 1024)
        self.assertEqual(list(foreign.iterdir()), [])
        self.assertEqual((self.root/'moved'/self.path.name).read_bytes(), b'anchored')

    def test_replace_failure_preserves_original_without_temp_residue(self):
        fs.atomic_write(self.path, b'original', 1024)
        target = 'agent_hub_host.win32_fs.replace' if os.name == 'nt' else 'os.replace'
        with patch(target, side_effect=OSError('injected replacement failure')):
            with self.assertRaises(OSError):
                fs.atomic_write(self.path, b'changed', 1024)
        self.assertEqual(fs.read_bytes(self.path, 1024), b'original')
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


class FakeAPI:
    """Simulated stdlib/Win32 boundary; never physical Windows evidence."""
    def __init__(self):
        self.calls, self.closed = [], []
        self.fail_private = False
        self.fs = 'NTFS'
        self.drive = 3
    def GetDriveTypeW(self, root):
        return self.drive
    def GetVolumeInformationW(self, root, *args):
        args[-2].value = self.fs
        return 1
    def check(self, value):
        if not value:
            raise OSError('simulated API failure')
    def open(self, path, access, disposition=3, directory=False, security=None):
        self.calls.append((str(path), access, disposition, directory, security is not None))
        return 0x100000000 + len(self.calls)
    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1
    def private(self, handle, **kwargs):
        if self.fail_private:
            raise PermissionError('simulated unknown ACL')
        return True
    @contextmanager
    def security(self, directory=False):
        yield win.SecurityAttributes()
    def CreateDirectoryW(self, path, security):
        return 1
    def MoveFileExW(self, source, target, flags):
        self.calls.append((source, target, flags))
        return 1
    def GetFinalPathNameByHandleW(self, handle, buf, size, flags):
        buf.value = '\\\\?\\C:\\Work\\Private'
        return len(buf.value)


class SimulatedWindowsTests(unittest.TestCase):
    def setUp(self):
        self.a = FakeAPI()
        self.patcher = patch.object(win, 'api', return_value=self.a)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_positive_ntfs_pinned_parent_and_atomic_write_through(self):
        with win.parent(r'C:\Work\Private\journal', create=True):
            self.assertEqual(len(self.a.calls), 3)
            self.assertEqual(self.a.closed, [])
        self.assertEqual(self.a.closed, [0x100000003, 0x100000002, 0x100000001])
        win.replace(r'C:\Work\Private\tmp', r'C:\Work\Private\journal')
        self.assertEqual(self.a.calls[-1][-1], 9)

    def test_unknown_filesystem_drive_acl_and_api_fail_closed(self):
        for fs, drive in [('FAT32', 3), ('NTFS', 4), ('', 3)]:
            self.a.fs, self.a.drive = fs, drive
            with self.assertRaises(PermissionError):
                with win.parent(r'C:\Work\journal'):
                    self.fail('unsupported filesystem accepted')
        self.a.fs, self.a.drive, self.a.fail_private = 'NTFS', 3, True
        with self.assertRaises(PermissionError):
            with win.parent(r'C:\Work\journal'):
                self.fail('unknown ACL accepted')
        self.assertEqual(len(self.a.closed), len(self.a.calls))
        with patch.object(self.a, 'GetVolumeInformationW', return_value=0):
            with self.assertRaises(OSError):
                win.local_path(r'C:\Work\journal')

    def test_windows_path_spelling_is_strict_and_canonical_case_preserved(self):
        for path in [r'C:relative', r'\relative', r'\\server\share\file', r'\\?\C:\file',
                     r'C:\a\..\b', r'C:\a\file:stream', r'C:\a\NUL.txt',
                     'C:\\a\\trailing. ', 'C:/a/file', 'C:\\a\\\\file']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                win.local_path(path)
        self.assertEqual(win.canonical_workspace(r'C:\Work\Private'), r'C:\Work\Private')

    def test_open_transfers_handle_once_and_acl_failure_closes(self):
        fake = types.SimpleNamespace(open_osfhandle=lambda h, flags: h + 99)
        with patch.dict(sys.modules, msvcrt=fake), patch.object(os, 'O_BINARY', 0x8000, create=True), patch.object(os, 'O_NOINHERIT', 0x80, create=True):
            fd = win.open_file(r'C:\Work\file', os.O_CREAT | os.O_EXCL | os.O_RDWR)
            self.assertEqual(fd, 0x100000001 + 99)
            self.assertEqual(self.a.calls[-1][2], 1)  # CREATE_NEW, not truncate
            self.assertTrue(self.a.calls[-1][-1])  # descriptor supplied at creation
            self.assertEqual(self.a.closed, [])
            self.a.fail_private = True
            with self.assertRaises(PermissionError):
                win.open_file(r'C:\Work\file', os.O_RDONLY)
        self.assertEqual(self.a.closed, [0x100000002])

    def test_actual_info_validation_reparse_hardlink_wrong_kind(self):
        raw = win.API.__new__(win.API)
        raw.check = self.a.check
        for attrs, links, directory in [(win.REPARSE, 1, False), (0, 2, False), (win.DIRECTORY, 1, False), (0, 1, True)]:
            def info(handle, ptr):
                item = C.cast(ptr, C.POINTER(win.FileInfo)).contents
                item.attributes, item.links = attrs, links
                return 1
            raw.GetFileInformationByHandle = info
            with self.assertRaises(PermissionError):
                raw.info(0x100000000, directory=directory)
        attrs, links = 0, 1
        self.assertEqual(raw.info(0x100000000).links, 1)

    def test_actual_acl_validation_positive_and_unknown_owner_ace_holds(self):
        raw = win.API.__new__(win.API)
        raw.check = self.a.check
        raw.user_sid = 77
        acl = win.ACL()
        acl.count = 1
        ace = win.ACE()
        ace.size, ace.mask = 24, win.FULL
        protected, same_owner, null_acl = True, True, False
        frees = []
        def security(handle, kind, fields, owner, group, dacl, sacl, sd):
            C.cast(owner, C.POINTER(win.PTR))[0] = 77 if same_owner else 88
            C.cast(dacl, C.POINTER(win.PTR))[0] = C.addressof(acl) if not null_acl else None
            C.cast(sd, C.POINTER(win.PTR))[0] = 99
            return 0
        def control(sd, value, revision):
            C.cast(value, C.POINTER(win.U16))[0] = 0x1000 if protected else 0
            return 1
        def getace(dacl, index, result):
            C.cast(result, C.POINTER(win.PTR))[0] = C.addressof(ace)
            return 1
        raw.GetSecurityInfo, raw.GetSecurityDescriptorControl, raw.GetAce = security, control, getace
        # Fake SID equality at OS boundary; production calls EqualSid on real SIDs.
        raw.EqualSid = lambda left, right: bool(left and (getattr(left, 'value', left) in (77, C.addressof(ace) + win.ACE.sid.offset)))
        raw.LocalFree = lambda ptr: frees.append(ptr.value)
        raw.private(123)
        for key, value in [('protected', False), ('same_owner', False), ('null_acl', True),
                           ('count', 2), ('flags', 0x10), ('mask', 0x120089), ('kind', 5)]:
            protected, same_owner, null_acl = True, True, False
            acl.count, ace.flags, ace.mask, ace.kind = 1, 0, win.FULL, 0
            if key == 'protected': protected = value
            elif key == 'same_owner': same_owner = value
            elif key == 'null_acl': null_acl = value
            elif key == 'count': acl.count = value
            else: setattr(ace, key, value)
            with self.subTest(key=key), self.assertRaises(PermissionError):
                raw.private(123)
        self.assertEqual(frees, [99] * 8)
        protected, same_owner, null_acl = False, True, False
        acl.count, ace.flags, ace.mask, ace.kind = 1, 16, win.FULL, 0
        self.assertFalse(raw.private(123, protected=False, inherited=True))
        with self.assertRaises(PermissionError):
            raw.private(123)


    def test_native_inherited_acl_requires_verified_owner_suffix_to_protected_root(self):
        checks = []
        def inherited(handle, **kwargs):
            checks.append(handle)
            return handle == 0x100000002  # protected Work root, inherited Private leaf
        with patch.object(self.a, 'private', side_effect=inherited):
            with win.parent(r'C:\Work\Private\session.json', private=False, native=True):
                pass
        self.assertEqual(checks, [0x100000003, 0x100000002])
        with patch.object(self.a, 'private', return_value=False):
            with self.assertRaises(PermissionError):
                with win.parent(r'C:\Work\Private\session.json', private=False, native=True):
                    self.fail('unprotected registry accepted')
        with patch.object(self.a, 'private', side_effect=PermissionError('foreign ancestor ACL')):
            with self.assertRaises(PermissionError):
                with win.parent(r'C:\Work\Private\session.json', private=False, native=True):
                    self.fail('foreign registry ancestor accepted')

    def test_actual_open_flags_pin_handle_and_cleanup_failed_type_proof(self):
        raw = win.API.__new__(win.API)
        calls, closed = [], []
        def create(*args):
            calls.append(args)
            return 0x123456789
        raw.CreateFileW, raw.CloseHandle = create, lambda h: closed.append(h)
        raw.info = lambda handle, directory: None
        self.assertEqual(raw.open('C:\\folder', 0, directory=True), 0x123456789)
        self.assertEqual(calls[-1][2], 3)  # share read/write without delete
        self.assertEqual(calls[-1][5], 0x00200000 | 0x02000000)
        with patch.object(raw, 'info', side_effect=PermissionError('reparse')):
            with self.assertRaises(PermissionError):
                raw.open('C:\\folder', 0)
        self.assertEqual(closed, [0x123456789])

    def test_msvcrt_nonblocking_contention_and_close_on_failure(self):
        fake = types.SimpleNamespace(LK_NBLCK=2, locking=lambda *args: None)
        lock = Path(tempfile.gettempdir())/'simulated-lock'
        # Only the selection/CRT contract is mocked, with real scratch file descriptors.
        with tempfile.TemporaryFile() as stream:
            duplicate = os.dup(stream.fileno())
            @contextmanager
            def parent(*args, **kwargs):
                yield 42
            with patch.object(fs, '_windows', return_value=True), patch.object(fs, 'parent', parent), \
                 patch.object(fs, '_open', return_value=duplicate), patch.dict(sys.modules, msvcrt=fake):
                def contended(fd, mode, count):
                    self.assertEqual((mode, count), (2, 1))
                    self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
                    raise BlockingIOError('simulated contention')
                fake.locking = contended
                with self.assertRaises(BlockingIOError):
                    with fs.exclusive_lock(lock):
                        self.fail('contended Windows lock accepted')
            with self.assertRaises(OSError):
                os.fstat(duplicate)


if __name__ == '__main__':
    unittest.main()
