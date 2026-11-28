"""Authored UNRUN: real source with fake Windows/file/process boundaries only."""
from contextlib import ExitStack, contextmanager, nullcontext
import ctypes as C
import io
import json
import ntpath
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from agent_hub_host import credential_delivery as delivery
from agent_hub_host import windows_release as release

VERSION = 'a' * 40
HELPER = b'def main():\n    pass\n'
ROOT = r'C:\fixture\agent-hub2'
CHAIN = {'launcher': ROOT + r'\venv\Scripts\python.exe',
         'venv_config': ROOT + r'\venv\pyvenv.cfg',
         'base_executable': r'C:\fixture\Python\python.exe',
         'python_dll': r'C:\fixture\Python\python313.dll'}


def archive_bytes(version=VERSION, helper=HELPER, consumer=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('agent_hub_host/_build_info.py', f'BUILD_COMMIT = "{version}"\n')
        archive.writestr(release.HELPER, helper)
        archive.writestr(release.WINDOWS_CONSUMER, helper if consumer is None else consumer)
    return buffer.getvalue()


class Store:
    def __init__(self):
        self.entries, self.counter, self.writes, self.closes = {}, 0, [], []

    def put(self, path, raw, private=True):
        self.counter += 1
        self.entries[str(path)] = (raw, self.counter, private)

    def pin(self, path, limit, private):
        store = self
        path = str(path)
        if path not in self.entries:
            raise FileNotFoundError()
        raw, identity, is_private = self.entries[path]
        if len(raw) > limit or private and not is_private:
            raise release.Refused('synthetic file refusal')
        class File:
            def __init__(self):
                self.path, self.raw = path, raw
                self.closed = False
                self.record = {'path': path, 'identity': {'volume': 1, 'index': [0, identity],
                               'size': len(raw), 'created': [0, identity], 'written': [0, identity]},
                               'size': len(raw), 'sha256': release.digest(raw)}
            def check(self):
                if store.entries.get(path) != (raw, identity, is_private):
                    raise release.Refused('synthetic source changed')
            def close(self):
                if not self.closed:
                    store.closes.append(path)
                    self.closed = True
        return File()

    def read(self, path, limit):
        if str(path) not in self.entries:
            raise FileNotFoundError()
        raw = self.entries[str(path)][0]
        if len(raw) > limit:
            raise ValueError()
        return raw

    def write(self, path, raw, limit):
        if len(raw) > limit:
            raise ValueError()
        self.writes.append(str(path))
        self.put(path, raw)


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.archive = archive_bytes()
        self.manifest = json.dumps({'host_version': VERSION, 'protocol_version': 1,
            'artifacts': {'hub.pyz': {'sha256': release.digest(self.archive), 'size': len(self.archive)}}}).encode()
        self.manifest_path, self.archive_path = ROOT + r'\input.json', ROOT + r'\input.pyz'
        self.store.put(self.manifest_path, self.manifest, False)
        self.store.put(self.archive_path, self.archive, False)
        for name, path in CHAIN.items():
            self.store.put(path, ('synthetic-' + name).encode(), False)
        self.pointer = ntpath.join(ROOT, 'releases', VERSION, 'credential-artifacts.json')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        module = ModuleType('agent_hub_host._release')
        module.HOST_VERSION, module.PROTOCOL_VERSION = VERSION, 1
        self.stack.enter_context(patch.dict(sys.modules, {'agent_hub_host._release': module}))
        self.stack.enter_context(patch.object(release, '_chain_paths', return_value=(ROOT, CHAIN)))
        self.stack.enter_context(patch.object(release, '_helper_pin', return_value=release.digest(HELPER)))
        self.stack.enter_context(patch.object(release, '_consumer_pin', return_value=release.digest(HELPER)))
        self.stack.enter_context(patch.object(release, 'PinnedFile', self.store.pin))
        self.stack.enter_context(patch.object(release, '_new_capsule', side_effect=lambda path: nullcontext()))
        self.stack.enter_context(patch.object(release.private_fs, 'exclusive_lock', side_effect=lambda path: nullcontext()))
        self.stack.enter_context(patch.object(release.private_fs, 'read_bytes', self.store.read))
        self.stack.enter_context(patch.object(release.private_fs, 'atomic_write', self.store.write))

    def publish(self):
        return release.publish(self.manifest_path, self.archive_path, release.digest(self.manifest))

    def test_same_release_publish_open_and_idempotent_repair(self):
        self.assertEqual(self.publish(), {'status': 'published'})
        before = list(self.store.writes)
        self.assertEqual(self.publish(), {'status': 'current'})
        self.assertEqual(self.store.writes, before)
        lease = release.WindowsRelease.open(VERSION, 1)
        self.assertEqual(lease.python, CHAIN['launcher'])
        with lease.borrow():
            with self.assertRaises(release.Refused):
                lease.close()
        lease.close()
        self.assertTrue(lease.closed)

    def test_windows_consumer_source_is_independently_pinned(self):
        changed = archive_bytes(consumer=b'foreign managed helper, never executed')
        manifest = json.loads(self.manifest)
        manifest['artifacts']['hub.pyz'] = {'sha256': release.digest(changed), 'size': len(changed)}
        self.manifest = json.dumps(manifest).encode()
        self.store.put(self.manifest_path, self.manifest, False)
        self.store.put(self.archive_path, changed, False)
        with self.assertRaisesRegex(release.Refused, 'consumer_source'):
            self.publish()
        self.assertNotIn(self.pointer, self.store.entries)

    def test_missing_receipt_refuses(self):
        with self.assertRaises(FileNotFoundError):
            release.WindowsRelease.open(VERSION, 1)

    def test_bad_archive_hash_size_version_protocol_and_helper_refuse(self):
        for kind in ('hash', 'size', 'version', 'protocol', 'helper'):
            with self.subTest(kind=kind):
                manifest = json.loads(self.manifest)
                raw = self.archive
                if kind == 'hash': manifest['artifacts']['hub.pyz']['sha256'] = '0' * 64
                if kind == 'size': manifest['artifacts']['hub.pyz']['size'] += 1
                if kind == 'version': manifest['host_version'] = 'b' * 40
                if kind == 'protocol': manifest['protocol_version'] = True
                if kind == 'helper':
                    raw = archive_bytes(helper=b'changed helper')
                    manifest['artifacts']['hub.pyz'] = {'sha256': release.digest(raw), 'size': len(raw)}
                with self.assertRaises(release.Refused):
                    release.validate_artifacts(json.dumps(manifest).encode(), raw, VERSION, 1)
        self.assertNotIn(self.pointer, self.store.entries)

    def test_same_tuple_missing_or_tampered_capsule_repair_keeps_previous(self):
        self.publish()
        old_raw = self.store.read(self.pointer, 32768)
        old = json.loads(old_raw)
        self.store.put(old['archive']['path'], b'tampered')
        self.assertEqual(self.publish(), {'status': 'published'})
        new = json.loads(self.store.read(self.pointer, 32768))
        self.assertNotEqual(new['capsule'], old['capsule'])
        backup = ntpath.join(ROOT, 'releases', VERSION, new['capsule'], 'previous-receipt.json')
        self.assertEqual(self.store.read(backup, 32768), old_raw)
        self.assertEqual(self.store.read(old['archive']['path'], 32768), b'tampered')

    def test_foreign_or_changed_interpreter_tuple_never_repaired(self):
        self.publish()
        pointer = self.store.read(self.pointer, 32768)
        writes = len(self.store.writes)
        self.store.put(CHAIN['base_executable'], b'replaced interpreter', False)
        with self.assertRaises(release.Refused):
            self.publish()
        self.assertEqual(self.store.read(self.pointer, 32768), pointer)
        self.assertEqual(len(self.store.writes), writes)

    def test_compare_before_publication_preserves_later_writer(self):
        reads = [0]
        actual_read = self.store.read
        def read(path, limit):
            if path == self.pointer:
                reads[0] += 1
                if reads[0] == 1:
                    self.store.put(path, b'later writer')
            return actual_read(path, limit)
        with patch.object(release.private_fs, 'read_bytes', read):
            with self.assertRaises(release.Refused):
                self.publish()
        self.assertEqual(self.store.read(self.pointer, 32768), b'later writer')

    def test_pointer_write_failure_retains_old_receipt(self):
        self.publish()
        old = self.store.read(self.pointer, 32768)
        value = json.loads(old)
        self.store.put(value['archive']['path'], b'bad')
        actual_write = self.store.write
        def write(path, raw, limit):
            if path == self.pointer:
                raise OSError('synthetic durable-write failure')
            return actual_write(path, raw, limit)
        with patch.object(release.private_fs, 'atomic_write', write):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(self.store.read(self.pointer, 32768), old)

    def test_held_source_drift_and_invalid_receipt_never_qualify(self):
        self.publish()
        lease = release.WindowsRelease.open(VERSION, 1)
        self.store.put(CHAIN['python_dll'], b'changed DLL', False)
        with self.assertRaises(release.Refused):
            lease.check()
        lease.close()
        raw = self.store.read(self.pointer, 32768)
        value = json.loads(raw)
        value['schema'] = True
        self.store.put(self.pointer, json.dumps(value).encode())
        with self.assertRaises(release.Refused):
            release.WindowsRelease.open(VERSION, 1)
        with self.assertRaises(release.Refused):
            release.decode(b'{"schema":1,"schema":1}')

    def test_capsule_collision_never_overwrites_prior_receipt(self):
        self.publish()
        old = self.store.read(self.pointer, 32768)
        value = json.loads(old)
        self.store.put(value['archive']['path'], b'bad')
        writes = len(self.store.writes)
        with patch.object(release, '_new_capsule', side_effect=release.Refused('collision')):
            with self.assertRaises(release.Refused): self.publish()
        self.assertEqual(self.store.read(self.pointer, 32768), old)
        self.assertEqual(len(self.store.writes), writes)

    def test_lease_close_errors_attempt_every_file_once(self):
        lease = object.__new__(release.WindowsRelease)
        lease.closed, lease.borrowers = False, 0
        lease.owner_thread = release.threading.get_ident()
        seen = []
        class File:
            def __init__(self, number): self.number = number
            def close(self):
                seen.append(self.number)
                if self.number == 2: raise OSError('synthetic close failure')
        lease.files = [File(1), File(2), File(3)]
        with self.assertRaises(release.Refused): lease.close()
        lease.close()
        self.assertEqual(seen, [3, 2, 1])

    def test_publication_late_close_never_reports_success(self):
        clock = [100]
        actual_pin = self.store.pin
        def pin(*args):
            file = actual_pin(*args)
            close = file.close
            def late_close():
                close()
                clock[0] = 121
            file.close = late_close
            return file
        with patch.object(release, 'PinnedFile', pin), \
                patch.object(release.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(release.Refused): self.publish()
        # Fully durable data may exist; timeout never authorizes deleting it.
        self.assertIn(self.pointer, self.store.entries)


class WindowsCheckOnly(unittest.TestCase):
    def make(self, child=None, spawn_error=None):
        lease = object.__new__(release.WindowsRelease)
        lease.files, lease.closed, lease.borrowers = [], False, 0
        lease.owner_thread = release.threading.get_ident()
        lease.python, lease.archive = 'VERIFIED-PYTHON', 'VERIFIED-ARCHIVE'
        child = child or SimpleNamespace(returncode=0, poll=lambda: 0,
            communicate=lambda *a, **k: (b'{"status":"absent","consumer_set":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', b''))
        calls = []
        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            if spawn_error is not None: raise spawn_error
            return child
        bridge = SimpleNamespace(delivery_popen=popen)
        epoch = delivery._Epoch()
        state = {'epoch': epoch, 'child': None}
        return lease, bridge, state, epoch, calls

    def run_check(self, bundle):
        lease, bridge, state, epoch, _ = bundle
        return delivery._consume_windows_check(bridge, state, epoch, lease, 2,
            '12345678-1234-1234-1234-123456789abc', {delivery.CREDENTIAL_PATH: 'synthetic'})

    def test_actual_command_uses_lease_interpreter_and_check_only(self):
        bundle = self.make()
        self.assertEqual(self.run_check(bundle), {'status': 'absent', 'consumer_set': 'a' * 32})
        lease, _, state, _, calls = bundle
        self.assertEqual(calls[0][0][0], 'VERIFIED-PYTHON')
        self.assertEqual(calls[0][0][-2:], ['VERIFIED-ARCHIVE', 'check'])
        self.assertIsNone(state['child'])
        self.assertEqual(lease.borrowers, 0)
        lease.close()

    def test_dictionary_is_not_a_trusted_interpreter(self):
        bundle = self.make()
        _, bridge, state, epoch, calls = bundle
        with self.assertRaises(delivery.Refused):
            delivery._consume_windows_check(bridge, state, epoch, {'python': 'PATH'}, 1, '', {})
        self.assertEqual(calls, [])

    def test_stopped_epoch_never_starts_child(self):
        bundle = self.make()
        bundle[3].stopped.set()
        with self.assertRaises(delivery.Refused): self.run_check(bundle)
        self.assertEqual(bundle[4], [])
        self.assertEqual(bundle[0].borrowers, 0)

    def test_ambiguous_spawn_retains_code_and_original_control(self):
        original = KeyboardInterrupt()
        bundle = self.make(spawn_error=original)
        with self.assertRaises(KeyboardInterrupt) as caught: self.run_check(bundle)
        self.assertIs(caught.exception, original)
        self.assertIs(bundle[2]['windows_pending_artifact_lease'], bundle[0])
        with self.assertRaises(release.Refused): bundle[0].close()

    def test_unreaped_child_retains_source_owner_and_no_success(self):
        child = SimpleNamespace(returncode=None, poll=lambda: None, kill=lambda: None,
            pid=999, communicate=lambda *a, **k: (b'{"status":"absent","consumer_set":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', b''))
        bundle = self.make(child)
        with patch.object(delivery, '_terminate'):
            with self.assertRaises(delivery.Refused): self.run_check(bundle)
        self.assertIs(bundle[2]['child'], child)
        self.assertIs(bundle[2]['windows_pending_artifact_lease'], bundle[0])
        with self.assertRaises(release.Refused): bundle[0].close()

    def test_late_source_change_after_reap_refuses(self):
        bundle = self.make()
        calls = [0]
        class Source:
            def check(self):
                calls[0] += 1
                if calls[0] == 2: raise release.Refused('changed after child')
            def close(self): pass
        bundle[0].files = [Source()]
        with self.assertRaises(release.Refused): self.run_check(bundle)
        self.assertEqual(bundle[0].borrowers, 0)
        bundle[0].close()


class AclAndHandleContracts(unittest.TestCase):
    def api(self, *, ace_sid=100, owner=100, mask=0x1f01ff, flags=0, protected=True):
        fs = release.fs
        a = object.__new__(fs.API)  # no DLL loaded; exercise actual API algorithms
        a.user_sid, a.trusted_sids = 100, [200, 300, 500]
        a.acl = fs.ACL(count=1)
        a.ace = fs.ACE(kind=0, flags=flags, size=fs.ACE.sid.offset + 8, mask=mask)
        sid_address = C.addressof(a.ace) + fs.ACE.sid.offset
        def sid(value):
            raw = value.value if hasattr(value, 'value') else value
            return ace_sid if raw == sid_address else raw
        a.EqualSid = lambda left, right: sid(left) == sid(right)
        a.check = lambda ok: None if ok else self.fail('unexpected Win32 failure')
        def security(handle, kind, bits, owner_out, group, dacl_out, sacl, descriptor):
            C.cast(owner_out, C.POINTER(fs.PTR))[0] = fs.PTR(owner)
            C.cast(dacl_out, C.POINTER(fs.PTR))[0] = fs.PTR(C.addressof(a.acl))
            C.cast(descriptor, C.POINTER(fs.PTR))[0] = fs.PTR(12345)
            return 0
        def get_ace(dacl, index, output):
            C.cast(output, C.POINTER(fs.PTR))[0] = fs.PTR(C.addressof(a.ace))
            return True
        def control(descriptor, output, revision):
            C.cast(output, C.POINTER(fs.U16))[0] = fs.U16(0x1000 if protected else 0)
            return True
        a.GetSecurityInfo, a.GetAce, a.GetSecurityDescriptorControl = security, get_ace, control
        def file_info(handle, output):
            value = C.cast(output, C.POINTER(fs.FileInfo)).contents
            value.attributes, value.links = 0x10, 1
            return True
        a.GetFileInformationByHandle = file_info
        a.frees = []
        a.LocalFree = lambda pointer: a.frees.append(pointer.value)
        return a

    def test_actual_private_acl_requires_owner_protection_and_no_inherited_ace(self):
        a = self.api()
        self.assertTrue(a.private(1))
        self.assertEqual(a.frees, [12345])
        for kwargs in ({'owner': 400}, {'protected': False}, {'flags': 16}):
            with self.subTest(kwargs=kwargs):
                a = self.api(**kwargs)
                with self.assertRaises(PermissionError): a.private(1)
                self.assertEqual(a.frees, [12345])

    def test_actual_program_acl_allows_foreign_read_not_foreign_write(self):
        release._code_acl(self.api(ace_sid=400, mask=0x120089, flags=16), 1)
        release._code_acl(self.api(ace_sid=200), 1)
        with self.assertRaises(release.Refused):
            release._code_acl(self.api(ace_sid=400), 1)
        with self.assertRaises(release.Refused):
            release._code_acl(self.api(owner=400), 1)

    def test_actual_file_info_rejects_reparse_directory_and_multiple_links(self):
        fs = release.fs
        for attributes, links in ((0x400, 1), (0x10, 1), (0, 2)):
            a = self.api()
            def info(handle, output):
                value = C.cast(output, C.POINTER(fs.FileInfo)).contents
                value.attributes, value.links = attributes, links
                return True
            a.GetFileInformationByHandle = info
            with self.assertRaises(PermissionError): a.info(1)

    def test_actual_path_parser_rejects_remote_ads_and_non_ntfs(self):
        for path in (r'\\server\share\file', r'C:\safe\file:stream', r'\\?\C:\file'):
            with self.assertRaises(ValueError): release.fs.local_path(path)
        a = self.api()
        a.GetDriveTypeW = lambda root: 4
        with patch.object(release.fs, 'api', return_value=a):
            with self.assertRaises(PermissionError): release.fs.local_path(r'C:\safe\file')
        a.GetDriveTypeW = lambda root: 3
        def volume(root, label, label_size, serial, maximum, flags, output, count):
            output.value = 'FAT32'
            return True
        a.GetVolumeInformationW = volume
        with patch.object(release.fs, 'api', return_value=a):
            with self.assertRaises(PermissionError): release.fs.local_path(r'C:\safe\file')

    def test_parent_close_failure_still_consumes_every_handle_once(self):
        a = self.api()
        opened, closed = [], []
        def open_(path, access, directory=False):
            opened.append(path)
            return len(opened)
        def close(handle):
            closed.append(handle)
            if handle == 2: raise OSError('synthetic close')
            return True
        a.open, a.CloseHandle = open_, close
        with patch.object(release.fs, 'api', return_value=a), \
                patch.object(release.fs, 'local_path', return_value=('C:\\', ['one', 'two', 'file'])):
            with self.assertRaises(release.Refused):
                with release._parents(r'C:\one\two\file', False): pass
        self.assertEqual(closed, [3, 2, 1])

    def test_actual_pinned_file_denies_write_delete_and_closes_crt_once(self):
        opened, handles, fds = [], [], []
        position = [0]
        def info(handle, directory=False):
            return SimpleNamespace(volume=1, index_high=0, index_low=9,
                size_high=0, size_low=3, times=[0, 1, 0, 2, 0, 3], attributes=0)
        a = SimpleNamespace(CreateFileW=lambda *args: opened.append(args) or 100,
            info=info, private=lambda handle: True, open=lambda *a, **k: 200,
            CloseHandle=lambda handle: handles.append(handle) or True,
            check=lambda ok: None)
        crt = SimpleNamespace(open_osfhandle=lambda *a: 7, get_osfhandle=lambda fd: 100)
        def read(fd, count):
            raw = b'abc'[position[0]:position[0] + count]
            position[0] += len(raw)
            return raw
        with patch.object(release, '_parents', side_effect=lambda *a: nullcontext()), \
                patch.object(release.fs, 'api', return_value=a), \
                patch.dict(sys.modules, {'msvcrt': crt}), \
                patch.object(release.os, 'O_BINARY', 0, create=True), \
                patch.object(release.os, 'O_NOINHERIT', 0, create=True), \
                patch.object(release.os, 'read', read), \
                patch.object(release.os, 'lseek', side_effect=lambda *a: position.__setitem__(0, 0)), \
                patch.object(release.os, 'close', side_effect=lambda fd: fds.append(fd)):
            file = release.PinnedFile(r'C:\owned\file', 10, True)
            self.assertEqual(file.raw, b'abc')
            self.assertEqual(opened[0][2], 1)  # FILE_SHARE_READ, neither write nor delete
            self.assertEqual(opened[0][5], 0x00200000)  # OPEN_REPARSE_POINT
            file.close(); file.close()
        self.assertEqual(handles, [200])
        self.assertEqual(fds, [7])

    def test_actual_file_constructor_control_closes_parent_and_preserves_identity(self):
        original, closed = KeyboardInterrupt(), []
        @contextmanager
        def parents(*args):
            try: yield
            finally: closed.append('parents')
        def private(handle): raise original
        a = SimpleNamespace(CreateFileW=lambda *args: 100,
                            info=lambda handle, directory=False: SimpleNamespace(attributes=0),
                            private=private, CloseHandle=lambda handle: closed.append(handle))
        with patch.object(release, '_parents', parents), patch.object(release.fs, 'api', return_value=a):
            with self.assertRaises(KeyboardInterrupt) as caught:
                release.PinnedFile(r'C:\owned\file', 10, True)
        self.assertIs(caught.exception, original)
        self.assertEqual(closed, [100, 'parents'])

    def test_adapter_rejects_offline_and_cloud_attributes(self):
        for attributes in (0x1000, 0x40000, 0x400000, 0x80000, 0x100000):
            a = SimpleNamespace(info=lambda handle, directory=False: SimpleNamespace(attributes=attributes))
            with self.assertRaises(release.Refused): release._info(a, 1)


if __name__ == '__main__':
    unittest.main()
