#!/usr/bin/env python3
"""Stage a NEW credential_monitor activation boundary; never activate it.

Linux, stdlib only. No API, credentials, configuration, services or historical
coverage. A nonzero/lost result is NOT approval to activate an existing target.
Interrupted private .stage-* directories are deliberately retained for owner
inspection; they are never accepted as an enrollment or automatically reused.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

ARTIFACTS = ('hub2/node/alert-evidence.ts', 'hub2/node/main.ts', 'hub2/src/worker.ts')
LIMIT = 16 * 1024 * 1024


class Refused(Exception):
    pass


def require(ok):
    if not ok:
        raise Refused('activation staging refused')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def absolute(path):
    value = os.fspath(path)
    require(os.path.isabs(value) and os.path.normpath(value) == value and '\x00' not in value)
    return Path(value)


def directory(path, *, private=False):
    """Open each actual directory without following any symlink."""
    path = absolute(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
            info = os.fstat(fd)
            require(info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o002)
        info = os.fstat(fd)
        if private:
            require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def artifact(checkout, relative):
    path = checkout / relative
    parent = directory(path.parent)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.geteuid())
                and info.st_nlink == 1 and not info.st_mode & 0o022 and info.st_size <= LIMIT)
        data = b''
        while len(data) <= LIMIT:
            chunk = os.read(fd, min(65536, LIMIT + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        after = os.fstat(fd)
        require(len(data) == info.st_size and len(data) <= LIMIT and
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def head(checkout):
    # No ambient Git overrides, credential helpers, hooks, network or refresh.
    env = {'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
    result = subprocess.run(['/usr/bin/git', '--no-optional-locks', '-C', str(checkout), 'rev-parse', '--verify', 'HEAD'],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 64)
    value = result.stdout.decode('ascii').strip()
    require(re.fullmatch('[0-9a-f]{40}', value))
    return value


def write_new(fd, name, value):
    leaf = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        os.fchmod(leaf, 0o600)
        data = canonical(value) + b'\n'
        while data:
            written = os.write(leaf, data)
            require(written > 0)
            data = data[written:]
        os.fsync(leaf)
    finally:
        os.close(leaf)


def publish(parent, staging, target):
    # Atomic no-replace publication: POSIX rename alone would overwrite a raced
    # empty target directory. Unsupported kernels fail closed, no fallback.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    require(rename is not None)
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(staging), parent, os.fsencode(target), 1) != 0:
        raise OSError(ctypes.get_errno(), 'exclusive publication refused')


def stage(checkout, destination, expected_commit, expected_hashes):
    require(sys.platform == 'linux' and re.fullmatch('[0-9a-f]{40}', expected_commit)
            and set(expected_hashes) == set(ARTIFACTS)
            and all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) for v in expected_hashes.values()))
    checkout, destination = absolute(checkout), absolute(destination)
    checkout_fd = directory(checkout)
    try:
        parent = directory(destination.parent, private=True)
    except BaseException:
        os.close(checkout_fd)
        raise
    temporary_fd = None
    staging = '.stage-' + uuid.uuid4().hex
    try:
        try:
            os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Refused('activation staging refused')
        require(head(checkout) == expected_commit)
        artifacts = [artifact(checkout, name) for name in ARTIFACTS]
        require(all(item['sha256'] == expected_hashes[name] for name, item in zip(ARTIFACTS, artifacts)))
        source_id = hashlib.sha256(canonical(artifacts)).hexdigest()
        enrollment_id = uuid.uuid4().hex  # Public generation identity, not a credential.
        enrollment = {'kind': 'hub_credential_monitor_enrollment_v1', 'producer': 'credential_monitor',
                      'recipient': 'dev@dtcdev.click', 'enrollment_id': enrollment_id,
                      'source_id': source_id, 'release_sha': expected_commit, 'artifacts': artifacts}
        journal = {'kind': 'hub_credential_monitor_journal_v1', 'source_id': source_id,
                   'enrollment_id': enrollment_id, 'occurrence_count': 0, 'started_total': 0, 'finished_total': 0}
        os.mkdir(staging, 0o700, dir_fd=parent)
        temporary_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.fchmod(temporary_fd, 0o700)
        os.fsync(parent)
        write_new(temporary_fd, 'enrollment.json', enrollment)
        write_new(temporary_fd, 'journal.json', journal)
        os.fsync(temporary_fd)
        # Recheck both authority and bytes immediately before publication.
        require(head(checkout) == expected_commit and artifacts == [artifact(checkout, name) for name in ARTIFACTS])
        current = os.stat(destination.parent, follow_symlinks=False)
        pinned = os.fstat(parent)
        require(stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino))
        publish(parent, staging, destination.name)
        os.fsync(parent)
        return {'kind': 'credential_monitor_staged_boundary_v1', 'destination': str(destination),
                'release_sha': expected_commit, 'source_id': source_id, 'enrollment_id': enrollment_id,
                'historical_coverage': False, 'activated': False}
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        os.close(parent)
        os.close(checkout_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--expected-commit', required=True)
    for label in ('observer', 'main', 'worker'):
        parser.add_argument('--' + label + '-sha256', required=True)
    args = parser.parse_args()
    try:
        value = stage(args.checkout, args.destination, args.expected_commit,
                      dict(zip(ARTIFACTS, (args.observer_sha256, args.main_sha256, args.worker_sha256))))
        print(canonical(value).decode())
        return 0
    except (OSError, ValueError, UnicodeError, Refused, subprocess.SubprocessError):
        print('activation staging refused; no activation authorized; retain incomplete files for owner inspection', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
