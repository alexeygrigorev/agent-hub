"""Windows release companions only: no enrollment, credentials, profiles or ACK.

Uses the maintained Win32 private-filesystem implementation. The public
interpreter chain permits read-only ambient access, but no foreign writers.
Private manifests/archives/receipts require protected owner-only ACLs. Same-user
administrators are trusted; publication is cooperative CAS, not hostile-UID CAS.
"""
import ast
from contextlib import ExitStack, contextmanager
import ctypes as C
import hashlib
import io
import json
import math
import ntpath
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
import zipfile

from . import private_fs, win32_fs as fs

ABI = 'hub2-windows-credential-artifacts-v1'
HELPER = 'agent_hub_host/managed_claude_deliver.py'
WINDOWS_CONSUMER = 'agent_hub_host/windows_managed_claude_deliver.py'
WINDOWS_CONSUMER_ABI = 'hub2-windows-managed-claude-consumer-v1'
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
ROLES = {'launcher', 'venv_config', 'base_executable', 'python_dll'}
HEX = re.compile(r'[0-9a-f]{64}\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')


class Refused(RuntimeError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise Refused('duplicate_json')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs)


def identity(info):
    return {'volume': info.volume, 'index': [info.index_high, info.index_low],
            'size': (info.size_high << 32) | info.size_low,
            'created': list(info.times[:2]), 'written': list(info.times[4:6])}


def _info(a, handle, directory=False):
    info = a.info(handle, directory)
    # OFFLINE, recall-on-open/data-access, pinned/unpinned cloud placeholders.
    # A local drive alone is not evidence that these objects are local code.
    if info.attributes & (0x1000 | 0x40000 | 0x400000 | 0x80000 | 0x100000):
        raise Refused('offline_or_cloud_source')
    return info


def _code_acl(a, handle):
    """Program files may be readable by others, never writable by them."""
    owner, dacl, descriptor = fs.PTR(), fs.PTR(), fs.PTR()
    error = a.GetSecurityInfo(handle, 1, 5, C.byref(owner), None,
                              C.byref(dacl), None, C.byref(descriptor))
    if error:
        raise Refused('code_acl')
    try:
        owners = [a.user_sid, *a.trusted_sids[:2]]  # user, SYSTEM, Administrators
        if not owner or not dacl or not any(a.EqualSid(owner, sid) for sid in owners):
            raise Refused('code_owner')
        trusted = [*owners, a.trusted_sids[-1]]  # OWNER RIGHTS refers to verified owner
        acl = C.cast(dacl, C.POINTER(fs.ACL)).contents
        if not 1 <= acl.count <= 128:
            raise Refused('code_acl')
        for index in range(acl.count):
            ptr = fs.PTR()
            a.check(a.GetAce(dacl, index, C.byref(ptr)))
            ace = C.cast(ptr, C.POINTER(fs.ACE)).contents
            if ace.kind not in (0, 1) or ace.flags & ~(1 | 2 | 4 | 8 | 16) or ace.size < fs.ACE.sid.offset + 8:
                raise Refused('code_acl')
            sid = fs.PTR(ptr.value + fs.ACE.sid.offset)
            # Foreign grants are closed to read/execute/control/synchronize only;
            # unknown/generic-all/maximum-allowed cannot smuggle write authority.
            if ace.kind == 0 and ace.mask & ~0xA01200A9 and not any(a.EqualSid(sid, s) for s in trusted):
                raise Refused('code_writer')
    finally:
        if descriptor:
            a.LocalFree(descriptor)


@contextmanager
def _parents(path, private):
    root, parts = fs.local_path(path)
    a, handles = fs.api(), []
    try:
        current = root
        handles.append(a.open(current, 0, directory=True))
        _info(a, handles[-1], True)
        for part in parts[:-1]:
            current = ntpath.join(current, part)
            handles.append(a.open(current, 0, directory=True))
            _info(a, handles[-1], True)
        if private:
            a.private(handles[-1], directory=True)
        yield
    finally:
        errors = []
        while handles:
            handle = handles.pop()
            try:
                a.check(a.CloseHandle(handle))
            except BaseException as error:
                errors.append(error)
        if errors:
            raise Refused('parent_close_unknown')


class PinnedFile:
    """Own CRT FD and all no-delete parent handles through every borrower."""
    def __init__(self, path, limit, private):
        self.path, self.limit, self.private = str(path), limit, private
        self.stack = ExitStack()
        self.fd = None
        try:
            self.stack.enter_context(_parents(path, private))
            a = fs.api()
            # FILE_SHARE_READ only: pin bytes and pathname, not merely deletion.
            handle = a.CreateFileW(self.path, 0x80000000 | fs.READ_CONTROL, 1,
                                   None, 3, 0x00200000, None)
            if handle == fs.PTR(-1).value:
                raise Refused('file_open')
            try:
                _info(a, handle)
                if private:
                    a.private(handle)
                else:
                    _code_acl(a, handle)
                import msvcrt
                self.fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
            except BaseException:
                a.CloseHandle(handle)
                raise
            self.initial = identity(_info(a, self.handle()))
            if not 0 <= self.initial['size'] <= limit:
                raise Refused('file_bound')
            self.raw = self.read()
            self.record = {'path': self.path, 'identity': self.initial,
                           'size': len(self.raw), 'sha256': digest(self.raw)}
            self.check()
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass  # preserve original refusal/control after all close attempts
            raise

    def handle(self):
        import msvcrt
        if self.fd is None:
            raise Refused('file_closed')
        return msvcrt.get_osfhandle(self.fd)

    def read(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        chunks, total = [], 0
        while True:
            part = os.read(self.fd, min(65536, self.limit + 1 - total))
            if not part:
                break
            total += len(part)
            if total > self.limit:
                raise Refused('file_bound')
            chunks.append(part)
        return b''.join(chunks)

    def check(self):
        a = fs.api()
        held = self.handle()
        if self.private:
            a.private(held)
        else:
            _code_acl(a, held)
        if identity(_info(a, held)) != self.initial or digest(self.read()) != self.record['sha256']:
            raise Refused('file_changed')
        named = a.open(self.path, 0)
        try:
            if identity(_info(a, named)) != self.initial:
                raise Refused('file_replaced')
        finally:
            a.check(a.CloseHandle(named))

    def close(self):
        fd, self.fd = self.fd, None
        try:
            if fd is not None:
                os.close(fd)
        finally:
            self.stack.close()


def _chain_paths():
    """Resolve the interpreter already executing us, never PATH or a receipt dict."""
    if sys.platform != 'win32':
        raise Refused('windows_only')
    launcher = str(Path(sys.executable).absolute())
    venv = ntpath.dirname(ntpath.dirname(launcher))
    if ntpath.basename(launcher).lower() != 'python.exe' or ntpath.basename(ntpath.dirname(launcher)).lower() != 'scripts' or ntpath.basename(venv).lower() != 'venv':
        raise Refused('selected_interpreter')
    kernel = C.WinDLL('kernel32', use_last_error=True)
    call = kernel.GetModuleFileNameW
    call.argtypes, call.restype = [fs.PTR, C.c_wchar_p, fs.U32], fs.U32
    buf = C.create_unicode_buffer(32768)
    module = getattr(sys, 'dllhandle', None)  # CPython's actual loaded DLL handle
    if type(module) is not int or module <= 0:
        raise Refused('python_dll')
    count = call(module, buf, len(buf))
    if not 0 < count < len(buf):
        raise Refused('python_dll')
    paths = {'launcher': launcher, 'venv_config': ntpath.join(venv, 'pyvenv.cfg'),
             'base_executable': str(Path(sys._base_executable).absolute()), 'python_dll': buf.value}
    for path in paths.values():
        fs.local_path(path)
    return ntpath.dirname(venv), paths


def _helper_pin():
    # This tiny build binding is stamped from the same source snapshot as hub.pyz.
    from ._credential_artifact_binding import HELPER_SOURCE_SHA256, HELPER_ABI
    if not HEX.fullmatch(HELPER_SOURCE_SHA256) or HELPER_ABI != ABI:
        raise Refused('helper_binding')
    return HELPER_SOURCE_SHA256


def _runtime_abi():
    if sys.implementation.name != 'cpython' or sys.version_info < (3, 11):
        raise Refused('interpreter_abi')
    return {'implementation': 'cpython', 'version': list(sys.version_info[:3]),
            'cache_tag': sys.implementation.cache_tag}


def _consumer_pin():
    from ._credential_artifact_binding import WINDOWS_CONSUMER_SOURCE_SHA256, WINDOWS_CONSUMER_ABI as abi
    if not HEX.fullmatch(WINDOWS_CONSUMER_SOURCE_SHA256) or abi != WINDOWS_CONSUMER_ABI:
        raise Refused('consumer_binding')
    return WINDOWS_CONSUMER_SOURCE_SHA256


def validate_artifacts(manifest_raw, archive_raw, version, protocol):
    manifest = decode(manifest_raw)
    if (not isinstance(version, str) or not COMMIT.fullmatch(version)
            or type(protocol) is not int or not isinstance(manifest, dict)
            or manifest.get('host_version') != version
            or type(manifest.get('protocol_version')) is not int
            or manifest['protocol_version'] != protocol or protocol != 1):
        raise Refused('release_binding')
    record = manifest.get('artifacts', {}).get('hub.pyz')
    if (not isinstance(record, dict) or set(record) != {'sha256', 'size'}
            or not isinstance(record['sha256'], str) or not HEX.fullmatch(record['sha256'])
            or type(record['size']) is not int or not 0 < record['size'] <= MAX_ARCHIVE
            or len(archive_raw) != record['size'] or digest(archive_raw) != record['sha256']):
        raise Refused('archive_binding')
    with zipfile.ZipFile(io.BytesIO(archive_raw)) as archive:
        names = archive.namelist()
        if len(names) > 4096 or len(names) != len(set(names)):
            raise Refused('archive_members')
        def read(name, limit):
            info = archive.getinfo(name)
            if info.is_dir() or not 0 < info.file_size <= limit:
                raise Refused('archive_member')
            return archive.read(info)
        marker = ast.parse(read('agent_hub_host/_build_info.py', 4096))
        values = {}
        for node in marker.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in {'BUILD_COMMIT', 'BUILD_DATE'}
                    and node.targets[0].id not in values):
                raise Refused('archive_marker')
            values[node.targets[0].id] = ast.literal_eval(node.value)
        if values.get('BUILD_COMMIT') != version or digest(read(HELPER, 131072)) != _helper_pin():
            raise Refused('helper_source')
        if digest(read(WINDOWS_CONSUMER, 131072)) != _consumer_pin():
            raise Refused('consumer_source')
    return manifest


def validate_receipt(value, version, protocol):
    expected = {'schema', 'host_version', 'protocol_version', 'helper_abi', 'python_abi', 'manifest_sha256',
                'archive', 'interpreter', 'capsule'}
    if (not isinstance(value, dict) or set(value) != expected
            or type(value['schema']) is not int or value['schema'] != 1
            or value['host_version'] != version or type(value['protocol_version']) is not int
            or value['protocol_version'] != protocol or value['helper_abi'] != ABI
            or not isinstance(value['manifest_sha256'], str) or not HEX.fullmatch(value['manifest_sha256'])
            or not isinstance(value['capsule'], str) or not re.fullmatch('capsule-[0-9a-f]{32}', value['capsule'])
            or not isinstance(value['interpreter'], dict) or set(value['interpreter']) != ROLES):
        raise Refused('receipt_binding')
    abi = value['python_abi']
    if (not isinstance(abi, dict) or set(abi) != {'implementation', 'version', 'cache_tag'}
            or abi['implementation'] != 'cpython' or not isinstance(abi['version'], list)
            or len(abi['version']) != 3 or any(type(x) is not int or not 0 <= x <= 999 for x in abi['version'])
            or not isinstance(abi['cache_tag'], str) or not 1 <= len(abi['cache_tag']) <= 64
            or abi != _runtime_abi()):
        raise Refused('interpreter_abi')
    for record in [value['archive'], *value['interpreter'].values()]:
        if (not isinstance(record, dict) or set(record) != {'path', 'identity', 'size', 'sha256'}
                or not isinstance(record['path'], str) or not 0 < len(record['path']) <= 32768
                or type(record['size']) is not int or not 0 <= record['size'] <= 64 * 1024 * 1024
                or not isinstance(record['sha256'], str) or not HEX.fullmatch(record['sha256'])):
            raise Refused('record_binding')
        info = record['identity']
        if (not isinstance(info, dict) or set(info) != {'volume', 'index', 'size', 'created', 'written'}
                or type(info['volume']) is not int or not 0 <= info['volume'] <= 0xffffffff
                or type(info['size']) is not int or info['size'] != record['size']):
            raise Refused('record_identity')
        for field in ('index', 'created', 'written'):
            if (not isinstance(info[field], list) or len(info[field]) != 2
                    or any(type(x) is not int or not 0 <= x <= 0xffffffff for x in info[field])):
                raise Refused('record_identity')


class WindowsRelease:
    """Internal owner, not a serializable producer proof or enrollment input."""
    def __init__(self):
        raise Refused('use_verified_open')

    @classmethod
    def open(cls, version, protocol):
        if not isinstance(version, str) or not COMMIT.fullmatch(version) or type(protocol) is not int or protocol != 1:
            raise Refused('release_binding')
        root, chain = _chain_paths()
        pointer = ntpath.join(root, 'releases', version, 'credential-artifacts.json')
        self = object.__new__(cls)
        self.files, self.closed, self.borrowers = [], False, 0
        self.owner_thread = threading.get_ident()
        try:
            def pin(path, limit, private=True):
                file = PinnedFile(path, limit, private)
                self.files.append(file)
                return file
            receipt = pin(pointer, 32768)
            value = decode(receipt.raw)
            validate_receipt(value, version, protocol)
            capsule = ntpath.join(root, 'releases', version, value['capsule'])
            manifest = pin(ntpath.join(capsule, 'host-release.json'), MAX_MANIFEST)
            archive = pin(ntpath.join(capsule, 'hub.pyz'), MAX_ARCHIVE)
            validate_artifacts(manifest.raw, archive.raw, version, protocol)
            if digest(manifest.raw) != value['manifest_sha256'] or archive.record != value['archive']:
                raise Refused('receipt_archive')
            if not isinstance(value['interpreter'], dict) or set(value['interpreter']) != ROLES:
                raise Refused('interpreter_binding')
            for role in sorted(ROLES):
                executable = pin(chain[role], 64 * 1024 * 1024, private=False)
                if executable.record != value['interpreter'][role]:
                    raise Refused('interpreter_changed')
            self.archive, self.python = archive.path, chain['launcher']
            self.version, self.protocol = version, protocol
            self.manifest_sha256 = value['manifest_sha256']
            self.check()
            return self
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise

    def check(self):
        if threading.get_ident() != self.owner_thread:
            raise Refused('release_owner')
        if self.closed:
            raise Refused('release_closed')
        for file in self.files:
            file.check()

    @contextmanager
    def borrow(self):
        self.check()
        self.borrowers += 1
        try:
            yield self
            self.check()
        finally:
            self.borrowers -= 1

    def close(self):
        if threading.get_ident() != self.owner_thread:
            raise Refused('release_owner')
        if self.borrowers:
            raise Refused('release_borrowed')
        self.closed = True
        errors = []
        while self.files:
            file = self.files.pop()
            try:
                file.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise Refused('release_close_unknown')


@contextmanager
def _new_capsule(path):
    """Create once; existing complete or partial capsules are never overwritten."""
    handle, failure = None, None
    with _parents(path, True):
        a = fs.api()
        try:
            with a.security(directory=True) as security:
                if not a.CreateDirectoryW(path, C.byref(security)):
                    raise Refused('capsule_create_refused')
            handle = a.open(path, 0, directory=True)
            _info(a, handle, True)
            a.private(handle, directory=True)
            yield
        except BaseException as error:
            failure = error
            raise
        finally:
            if handle is not None:
                try:
                    a.check(a.CloseHandle(handle))
                except BaseException:
                    if failure is None:
                        raise Refused('capsule_close_unknown') from None


def publish(manifest_path, archive_path, expected_manifest_sha256):
    """Normal installer-only companion publication; no consumer activation."""
    started = last = time.monotonic()
    def bound():
        nonlocal last
        now = time.monotonic()
        if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (started, last, now))
                or not started <= last <= now <= started + 20):
            raise Refused('publication_deadline')
        last = now
    bound()
    result = _publish(manifest_path, archive_path, expected_manifest_sha256, bound)
    bound()  # after every owned file, directory and publication-lock close
    return result


def _publish(manifest_path, archive_path, expected_manifest_sha256, bound):
    from ._release import HOST_VERSION, PROTOCOL_VERSION
    if not isinstance(expected_manifest_sha256, str) or not HEX.fullmatch(expected_manifest_sha256):
        raise Refused('manifest_pin')
    root, paths = _chain_paths()
    release = ntpath.join(root, 'releases', HOST_VERSION)
    pointer = ntpath.join(release, 'credential-artifacts.json')
    # One version's cooperative publication lock; never a global provider lock.
    with private_fs.exclusive_lock(ntpath.join(release, 'publication.lock')):
        with ExitStack() as stack:
            sources = []
            def pin(path, limit, private):
                bound()
                source = PinnedFile(path, limit, private)
                sources.append(source)
                stack.callback(source.close)
                bound()
                return source
            manifest = pin(manifest_path, MAX_MANIFEST, False)
            archive = pin(archive_path, MAX_ARCHIVE, False)
            if digest(manifest.raw) != expected_manifest_sha256:
                raise Refused('manifest_changed')
            trusted_manifest = validate_artifacts(manifest.raw, archive.raw, HOST_VERSION, PROTOCOL_VERSION)
            chain = {role: pin(path, 64 * 1024 * 1024, False).record for role, path in paths.items()}
            try:
                previous_file = pin(pointer, 32768, True)
                previous_raw = previous_file.raw
            except FileNotFoundError:
                previous_file = None
                previous_raw = None
            if previous_raw is not None:
                previous = decode(previous_raw)
                validate_receipt(previous, HOST_VERSION, PROTOCOL_VERSION)
                if (previous.get('host_version') != HOST_VERSION or previous.get('protocol_version') != PROTOCOL_VERSION
                        or previous.get('helper_abi') != ABI or previous.get('manifest_sha256') != expected_manifest_sha256
                        or previous.get('interpreter') != chain
                        or previous['archive']['path'] != ntpath.join(release, previous['capsule'], 'hub.pyz')
                        or previous['archive']['sha256'] != trusted_manifest['artifacts']['hub.pyz']['sha256']
                        or previous['archive']['size'] != trusted_manifest['artifacts']['hub.pyz']['size']):
                    raise Refused('repair_foreign_tuple')
                try:
                    current = WindowsRelease.open(HOST_VERSION, PROTOCOL_VERSION)
                except (OSError, ValueError, KeyError, Refused):
                    pass  # Same trusted tuple only; never overwrite old capsule.
                else:
                    current.close()
                    return {'status': 'current'}
            capsule_name = 'capsule-' + uuid.uuid4().hex
            capsule = ntpath.join(release, capsule_name)
            bound()
            stack.enter_context(_new_capsule(capsule))
            manifest_dest, archive_dest = ntpath.join(capsule, 'host-release.json'), ntpath.join(capsule, 'hub.pyz')
            private_fs.atomic_write(manifest_dest, manifest.raw, MAX_MANIFEST)
            bound()
            private_fs.atomic_write(archive_dest, archive.raw, MAX_ARCHIVE)
            bound()
            stored_manifest = pin(manifest_dest, MAX_MANIFEST, True)
            stored_archive = pin(archive_dest, MAX_ARCHIVE, True)
            if stored_manifest.raw != manifest.raw or stored_archive.raw != archive.raw:
                raise Refused('publication_readback')
            receipt = {'schema': 1, 'host_version': HOST_VERSION, 'protocol_version': PROTOCOL_VERSION,
                       'helper_abi': ABI, 'python_abi': _runtime_abi(), 'manifest_sha256': expected_manifest_sha256,
                       'archive': stored_archive.record, 'interpreter': chain, 'capsule': capsule_name}
            if previous_raw is not None:
                bound()
                private_fs.atomic_write(ntpath.join(capsule, 'previous-receipt.json'), previous_raw, 32768)
            for source in sources:
                source.check()
            try:
                actual_previous = private_fs.read_bytes(pointer, 32768)
            except FileNotFoundError:
                actual_previous = None
            if actual_previous != previous_raw:
                raise Refused('publication_conflict')
            if previous_file is not None:
                previous_file.check()  # held identity and bytes, not bytes-only CAS
                previous_file.close()  # consume own deny-delete handle before replace
            # Private atomic_write fsyncs data then uses MoveFileEx WRITE_THROUGH.
            # Readers deny deletion while borrowing; busy replace fails, retaining
            # the prior receipt. No automatic rollback over a later writer.
            bound()
            private_fs.atomic_write(pointer, (json.dumps(receipt, sort_keys=True) + '\n').encode(), 32768)
        bound()
        with _opened(HOST_VERSION, PROTOCOL_VERSION) as current:
            if current.manifest_sha256 != expected_manifest_sha256:
                raise Refused('publication_replaced')
    return {'status': 'published'}


@contextmanager
def _opened(version, protocol):
    lease = WindowsRelease.open(version, protocol)
    try:
        yield lease
    except BaseException:
        try:
            lease.close()
        except BaseException:
            pass
        raise
    else:
        lease.close()


def main():
    # Called only by the normal verified-wheel installer, not a daemon command.
    if len(sys.argv) != 4:
        raise SystemExit(2)
    try:
        result = publish(*sys.argv[1:])
    except Exception:
        print('{"status":"unavailable"}')
        raise SystemExit(1) from None
    print(json.dumps(result))
