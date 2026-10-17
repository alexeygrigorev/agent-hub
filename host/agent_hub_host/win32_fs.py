"""CPython 3.11+, Windows 10/11, fixed local NTFS. No shell/third-party ACL tool.

Every API failure is an error. No FAT/SMB/cloud/reparse/device namespace fallback.
This module imports on POSIX for deterministic API contract tests; only api()
loads Windows DLLs. Handles and security descriptors always have one owner.
"""
from contextlib import contextmanager, ExitStack
import ctypes as C
import ntpath
import os
from pathlib import Path

U32, U16, U8, PTR = C.c_uint32, C.c_uint16, C.c_ubyte, C.c_void_p
BOOL = C.c_int
FULL = 0x1f01ff
READ_CONTROL = 0x20000
REPARSE = 0x400
DIRECTORY = 0x10


class FileInfo(C.Structure):
    _fields_ = [('attributes', U32), ('times', U32 * 6), ('volume', U32),
                ('size_high', U32), ('size_low', U32), ('links', U32),
                ('index_high', U32), ('index_low', U32)]


class SecurityAttributes(C.Structure):
    _fields_ = [('length', U32), ('descriptor', PTR), ('inherit', BOOL)]


class ACL(C.Structure):
    _fields_ = [('revision', U8), ('reserved', U8), ('size', U16),
                ('count', U16), ('reserved2', U16)]


class ACE(C.Structure):
    _fields_ = [('kind', U8), ('flags', U8), ('size', U16), ('mask', U32), ('sid', U32)]


class API:
    def __init__(self):
        kernel = C.WinDLL('kernel32', use_last_error=True)
        adv = C.WinDLL('advapi32', use_last_error=True)
        def bind(dll, name, args, result=BOOL):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            setattr(self, name, fn)
        bind(kernel, 'CreateFileW', [C.c_wchar_p, U32, U32, PTR, U32, U32, PTR], PTR)
        bind(kernel, 'CreateDirectoryW', [C.c_wchar_p, PTR])
        bind(kernel, 'CloseHandle', [PTR])
        bind(kernel, 'GetCurrentProcess', [], PTR)
        bind(kernel, 'GetFileInformationByHandle', [PTR, C.POINTER(FileInfo)])
        bind(kernel, 'GetFinalPathNameByHandleW', [PTR, C.c_wchar_p, U32, U32], U32)
        bind(kernel, 'GetDriveTypeW', [C.c_wchar_p], U32)
        bind(kernel, 'GetVolumeInformationW', [C.c_wchar_p, C.c_wchar_p, U32, PTR, PTR, PTR, C.c_wchar_p, U32])
        bind(kernel, 'MoveFileExW', [C.c_wchar_p, C.c_wchar_p, U32])
        bind(kernel, 'LocalFree', [PTR], PTR)
        bind(adv, 'OpenProcessToken', [PTR, U32, C.POINTER(PTR)])
        bind(adv, 'GetTokenInformation', [PTR, U32, PTR, U32, C.POINTER(U32)])
        bind(adv, 'ConvertSidToStringSidW', [PTR, C.POINTER(PTR)])
        bind(adv, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', [C.c_wchar_p, U32, C.POINTER(PTR), PTR])
        bind(adv, 'GetSecurityInfo', [PTR, U32, U32, C.POINTER(PTR), PTR, C.POINTER(PTR), PTR, C.POINTER(PTR)], U32)
        bind(adv, 'GetSecurityDescriptorControl', [PTR, C.POINTER(U16), C.POINTER(U32)])
        bind(adv, 'GetAce', [PTR, U32, C.POINTER(PTR)])
        bind(adv, 'EqualSid', [PTR, PTR])
        self.user_buffer, self.user_sid, self.sid_text = self._user()

    def check(self, ok):
        if not ok:
            raise C.WinError(C.get_last_error())
        return ok

    def _user(self):
        token, size = PTR(), U32()
        self.check(self.OpenProcessToken(self.GetCurrentProcess(), 8, C.byref(token)))
        try:
            self.GetTokenInformation(token, 1, None, 0, C.byref(size))
            if not 0 < size.value <= 65536:
                raise PermissionError('Unknown token SID size')
            buffer = C.create_string_buffer(size.value)
            self.check(self.GetTokenInformation(token, 1, buffer, size.value, C.byref(size)))
            sid = C.cast(buffer, C.POINTER(PTR))[0]
            text = PTR()
            self.check(self.ConvertSidToStringSidW(sid, C.byref(text)))
            try:
                return buffer, sid, C.wstring_at(text)
            finally:
                self.LocalFree(text)
        finally:
            self.CloseHandle(token)

    @contextmanager
    def security(self, directory=False):
        sd = PTR()
        flags = 'OICI' if directory else ''
        self.check(self.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f'O:{self.sid_text}D:P(A;{flags};FA;;;{self.sid_text})', 1, C.byref(sd), None))
        try:
            sa = SecurityAttributes(C.sizeof(SecurityAttributes), sd, 0)
            yield sa
        finally:
            self.LocalFree(sd)

    def private(self, handle, *, protected=True, inherited=False):
        owner, dacl, sd = PTR(), PTR(), PTR()
        error = self.GetSecurityInfo(handle, 1, 5, C.byref(owner), None, C.byref(dacl), None, C.byref(sd))
        if error:
            raise OSError(error, 'Cannot verify private ACL')
        try:
            control, revision = U16(), U32()
            self.check(self.GetSecurityDescriptorControl(sd, C.byref(control), C.byref(revision)))
            if (not owner or not dacl or not self.EqualSid(owner, self.user_sid)
                    or (protected and not control.value & 0x1000)):
                raise PermissionError('Private owner/protected DACL required')
            acl = C.cast(dacl, C.POINTER(ACL)).contents
            if acl.count != 1:
                raise PermissionError('Exactly one owner-only full-control ACE required')
            ptr = PTR()
            self.check(self.GetAce(dacl, 0, C.byref(ptr)))
            ace = C.cast(ptr, C.POINTER(ACE)).contents
            sid = PTR(ptr.value + ACE.sid.offset)
            if (ace.kind != 0 or ace.flags & ~(1 | 2 | (16 if inherited else 0)) or ace.mask != FULL
                    or ace.size < ACE.sid.offset + 8 or not self.EqualSid(sid, self.user_sid)):
                raise PermissionError('Unexpected or inherited private ACE')
            return bool(control.value & 0x1000)
        finally:
            if sd:
                self.LocalFree(sd)

    def info(self, handle, directory=False):
        info = FileInfo()
        self.check(self.GetFileInformationByHandle(handle, C.byref(info)))
        if info.attributes & REPARSE or bool(info.attributes & DIRECTORY) != directory:
            raise PermissionError('Reparse point or wrong object type')
        if not directory and info.links != 1:
            raise PermissionError('Multiply linked private file')
        return info

    def open(self, path, access, disposition=3, directory=False, security=None):
        # Share read/write, never delete: pin ancestors/lock inode against rename.
        handle = self.CreateFileW(str(path), access | READ_CONTROL, 3,
                                  C.byref(security) if security is not None else None,
                                  disposition, 0x00200000 | (0x02000000 if directory else 0), None)
        if handle == PTR(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            self.info(handle, directory)
            return handle
        except BaseException:
            self.CloseHandle(handle)
            raise


_instance = None


def api():
    global _instance
    if _instance is None:
        _instance = API()
    return _instance


def local_path(path):
    text = str(path)
    drive, tail = ntpath.splitdrive(text)
    if (len(drive) != 2 or drive[1] != ':' or not drive[0].isascii() or not drive[0].isalpha()
            or not tail.startswith('\\') or '/' in text or ':' in tail or '\x00' in text):
        raise ValueError('Explicit plain absolute local drive path required')
    parts = tail.split('\\')[1:]
    for part in parts:
        if (not part or part in ('.', '..') or part.endswith((' ', '.'))
                or any(ch in '<>"|?*' or ord(ch) < 32 for ch in part)
                or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$',
                    *(f'COM{i}' for i in '¹²³'), *(f'LPT{i}' for i in '¹²³'),
                    *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
            raise ValueError('Unsafe Windows path component')
    root = drive + '\\'
    fs = C.create_unicode_buffer(32)
    a = api()
    if a.GetDriveTypeW(root) != 3:
        raise PermissionError('Fixed local drive required')
    a.check(a.GetVolumeInformationW(root, None, 0, None, None, None, fs, len(fs)))
    if fs.value != 'NTFS':
        raise PermissionError('Local NTFS proof required')
    return root, parts


@contextmanager
def parent(path, *, create=False, private=True, native=False):
    root, parts = local_path(path)
    a = api()
    with ExitStack() as stack:
        current = root
        handles = []
        handle = a.open(current, 0, directory=True)
        stack.callback(a.CloseHandle, handle)
        handles.append(handle)
        for component in parts[:-1]:
            current = ntpath.join(current, component)
            if create:
                with a.security(directory=True) as sa:
                    if not a.CreateDirectoryW(current, C.byref(sa)):
                        error = C.get_last_error()
                        if error != 183:  # ERROR_ALREADY_EXISTS; still verify opened object
                            raise C.WinError(error)
            handle = a.open(current, 0, directory=True)
            stack.callback(a.CloseHandle, handle)
            handles.append(handle)
        if native:
            # Native Rust files may inherit their sole owner ACE. Verify the
            # complete owner-only suffix back to a protected directory while
            # all ancestors stay pinned; never require rewriting native state.
            for ancestor in reversed(handles):
                if a.private(ancestor, protected=False, inherited=True):
                    break
            else:
                raise PermissionError('Native registry lacks a protected owner-only root')
        if private:
            a.private(handle)
        yield handle


def open_file(path, flags, *, private=True):
    import msvcrt
    a = api()
    disposition = 1 if flags & os.O_EXCL else (4 if flags & os.O_CREAT else 3)
    access = 0x80000000
    if flags & (os.O_WRONLY | os.O_RDWR):
        access |= 0x40000000
    with a.security() as sa:
        handle = a.open(path, access, disposition, security=sa)
    try:
        # Native registry reads also require private ACL proof on Windows.
        a.private(handle, protected=private, inherited=not private)
        fd = msvcrt.open_osfhandle(handle, (flags & (os.O_WRONLY | os.O_RDWR)) | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        a.CloseHandle(handle)
        raise
    return fd  # CRT fd now owns the handle


def replace(source, target):
    # Same directory, same volume. Bounded: no retry after sharing/ACL/IO uncertainty.
    api().check(api().MoveFileExW(str(source), str(target), 1 | 8))


def canonical_workspace(path):
    # Native Rust uses canonicalize + strip_verbatim_prefix. Preserve actual case,
    # never lower() or normcase() a journal identity or native workspace hash.
    a = api()
    with parent(ntpath.join(str(path), '__hub_path_probe__'), private=False) as handle:
        buf = C.create_unicode_buffer(32768)
        count = a.GetFinalPathNameByHandleW(handle, buf, len(buf), 0)
        if not 0 < count < len(buf):
            raise PermissionError('Cannot canonicalize native workspace')
        text = buf.value
        if text.startswith('\\\\?\\'):
            text = text[4:]
        local_path(ntpath.join(text, '__hub_path_probe__'))
        return text
