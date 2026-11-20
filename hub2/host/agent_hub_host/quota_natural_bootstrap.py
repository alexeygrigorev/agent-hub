"""USER-side image validation before a future same-PID natural Host exec.

The original Root manifest pipe is never read or rewritten here. Root-source
adoption remains exclusively the post-exec receiver's job. No helper, exec,
service, gate, permission or privilege operation is performed by this module.
"""
import fcntl
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import time
import zipfile

from . import quota_natural_startup as startup
from . import quota_natural_transfer as transfer


RELEASE_ROOT = '/usr/local/lib/agent-hub/natural/releases'
INTERPRETERS = frozenset(('/usr/bin/python3.11', '/usr/bin/python3.12'))
MAX_IMAGE_BYTES = 16 * 1024 * 1024


def _deadline(deadline_ns):
    transfer._require(type(deadline_ns) is int and time.monotonic_ns() < deadline_ns)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _section(fd, path, deadline_ns, *, executable):
    """Facts come from a held FD, not a peer's hash or serialized dictionary."""
    _deadline(deadline_ns)
    transfer._require(type(fd) is int and fd > 2
                      and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
    before = os.fstat(fd)
    transfer._require(stat.S_ISREG(before.st_mode) and before.st_uid == 0
                      and before.st_nlink == 1 and not before.st_mode & 0o022
                      and 0 < before.st_size <= MAX_IMAGE_BYTES
                      and (not executable or before.st_mode & 0o111))
    chunks = []
    offset = 0
    while offset < before.st_size:
        _deadline(deadline_ns)
        chunk = os.pread(fd, min(65536, before.st_size - offset), offset)
        transfer._require(bool(chunk))
        chunks.append(chunk)
        offset += len(chunk)
    raw = b''.join(chunks)
    _deadline(deadline_ns)
    transfer._require(_stamp(before) == _stamp(os.fstat(fd))
                      and len(raw) == before.st_size
                      and (not executable or raw.startswith(b'\x7fELF')))
    return {'fd': fd, 'path': path, 'dev': before.st_dev, 'ino': before.st_ino,
            'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
            'ctime_ns': before.st_ctime_ns, 'sha256': hashlib.sha256(raw).hexdigest()}, raw


def _release_marker(raw, commit):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        transfer._require(len(entries) <= 1024)
        markers = [entry for entry in entries if entry.filename == 'agent_hub_host/_release.py']
        transfer._require(len(markers) == 1 and 0 < markers[0].file_size <= 4096)
        transfer._require(archive.read(markers[0]) == (
            f'HOST_VERSION = {commit!r}\nPROTOCOL_VERSION = 1\n').encode())


class BootstrapImages:
    """Borrow donation FDs; own only directory references opened by validation."""
    def __init__(self, sections, nodes, deadline_ns):
        self.sections = sections
        self.nodes = nodes
        self.deadline_ns = deadline_ns
        self.cleanup_certain = True

    def recheck(self):
        transfer._require(bool(self.nodes))
        for index, section in enumerate(self.sections):
            _deadline(self.deadline_ns)
            # Reuse the existing full Root/name/FD/hash verifier without
            # constructing a source selection or consuming the Root pipe.
            startup._image(section, self.nodes, executable=index == 0)
        _deadline(self.deadline_ns)

    def close(self):
        owned = list(self.nodes.values())
        self.nodes.clear()  # failed closes are uncertain, never retried
        self.cleanup_certain = transfer._close_owned(owned) and self.cleanup_certain
        return self.cleanup_certain


def validate_donated_images(donation, expected_commit):
    """Validate fixed Root release names, full held bytes and untouched FIFO.

    A successful return is image evidence only. It neither changes descriptor
    inheritance nor supplies Root-source authority or an adoption ACK.
    """
    transfer._require(type(donation) is transfer.ReceivedDonation
                      and type(expected_commit) is str
                      and re.fullmatch('[0-9a-f]{40}', expected_commit)
                      and os.getuid() == os.geteuid() > 0
                      and tuple(donation._owned) == donation.slots
                      and len(donation.slots) == len(transfer.ROLES)
                      and len(set(donation.slots)) == len(donation.slots))
    _deadline(donation.deadline_ns)
    # Validate existence before opening any new FD, preventing a missing
    # donated number from being reused as one of our directory references.
    for fd in donation.slots:
        transfer._require(type(fd) is int and fd > 2)
        os.fstat(fd)
        transfer._require(not os.get_inheritable(fd))
    manifest = os.fstat(donation.manifest_fd)
    transfer._require(stat.S_ISFIFO(manifest.st_mode) and manifest.st_uid == 0
                      and fcntl.fcntl(donation.manifest_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
    interpreter = os.readlink(f'/proc/self/fd/{donation.slots[0]}')
    archive = f'{RELEASE_ROOT}/{expected_commit}/host.pyz'
    transfer._require(interpreter in INTERPRETERS)
    nodes = {}
    try:
        nodes, _edges = startup._root_graph([Path(interpreter).parent, Path(archive).parent])
        first, _ = _section(donation.slots[0], interpreter, donation.deadline_ns, executable=True)
        second, raw = _section(donation.slots[1], archive, donation.deadline_ns, executable=False)
        _release_marker(raw, expected_commit)
        result = BootstrapImages((first, second), nodes, donation.deadline_ns)
        result.recheck()
        nodes = {}  # successful result owns these references
        return result
    finally:
        transfer._close_owned(list(nodes.values()))
