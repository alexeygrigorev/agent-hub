"""Bounded private local files; no backend fallback on failed safety proof.

POSIX uses descriptor-relative no-follow operations. Windows uses pinned no-delete
share directory handles, protected owner DACLs, NTFS and Win32 no-follow opens.
Same-user processes/admins are trusted, as with the native session registry.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
import uuid


def _windows():
    return sys.platform == 'win32'


def _private(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
        raise PermissionError('Unsafe private file or directory')


@contextmanager
def parent(path, *, create=False, private=True):
    if _windows():
        from . import win32_fs
        with win32_fs.parent(path, create=create, private=private, native=not private) as anchor:
            yield anchor
        return
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.parent.parts[1:]
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        if private:
            _private(os.fstat(fd), directory=True)
        yield fd
    finally:
        os.close(fd)


def _open(anchor, path, flags, *, private=True):
    if _windows():
        from . import win32_fs
        return win32_fs.open_file(path, flags, private=private)
    fd = os.open(Path(path).name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=anchor)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError('Unsafe nonregular or multiply linked file')
        if private:
            _private(info)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_bytes(path, limit, *, private=True):
    with parent(path, private=private) as anchor:
        fd = _open(anchor, path, os.O_RDONLY, private=private)
        with os.fdopen(fd, 'rb') as stream:
            if os.fstat(stream.fileno()).st_size > limit:
                raise ValueError('Private file exceeds byte bound')
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('Private file grew beyond byte bound')
            return data


def _existing(anchor, path):
    try:
        fd = _open(anchor, path, os.O_RDONLY)
    except FileNotFoundError:
        return
    os.close(fd)


def atomic_write(path, raw, limit):
    if len(raw) > limit:
        raise ValueError('Private file exceeds byte bound')
    path = Path(path)
    with parent(path, create=True) as anchor:
        _existing(anchor, path)
        # Random, exclusive staging avoids clobbering crash residue or another writer.
        tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        fd = _open(anchor, tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _existing(anchor, path)
            if _windows():
                from . import win32_fs
                win32_fs.replace(tmp, path)
            else:
                os.replace(tmp.name, path.name, src_dir_fd=anchor, dst_dir_fd=anchor)
                os.fsync(anchor)
        finally:
            try:
                if _windows():
                    os.unlink(tmp)
                else:
                    os.unlink(tmp.name, dir_fd=anchor)
            except FileNotFoundError:
                pass


@contextmanager
def exclusive_lock(path):
    with parent(path, create=True) as anchor:
        fd = _open(anchor, path, os.O_CREAT | os.O_RDWR)
        try:
            if _windows():
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            # Kernel releases the lock on close and on process death. Never unlink it.
            os.close(fd)
