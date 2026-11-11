#!/usr/bin/env python3
"""Stage verified CI release files and transactionally switch the serving pointer."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import tempfile
import uuid
import zipfile

VERIFIER = runpy.run_path(str(Path(__file__).resolve().with_name('build-hub2-release.py')))
VERIFY_BUNDLE = VERIFIER['verify_bundle']
VALIDATE_MANIFEST = runpy.run_path(str(Path(__file__).resolve().with_name('hub2-self-update.py')))['validate_manifest']
MAX_MANIFEST = VERIFIER['MAX_MANIFEST']
MAX_RECEIPT = 8192
COMMIT = re.compile(r'[0-9a-f]{40}')
DIGEST = re.compile(r'[0-9a-f]{64}')
TOKEN = re.compile(r'[0-9a-f]{32}')
POINTER = re.compile(r'releases/([0-9a-f]{40})')


def public_directory(path, *, parents=False):
    # Release files are served by the unprivileged Node service.
    umask = os.umask(0o022)
    try:
        path.mkdir(parents=parents, exist_ok=True, mode=0o755)
    finally:
        os.umask(umask)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def protected_directory(path, *, immutable=False):
    info = path.lstat()
    write_bits = 0o222 if immutable else 0o022
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & write_bits:
        raise ValueError('publication directory has unsafe ownership or permissions')


def verify_release(path, commit, digest):
    protected_directory(path, immutable=True)
    for artifact in path.iterdir():
        info = artifact.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o222 or info.st_nlink != 1):
            raise ValueError('release artifact has unsafe ownership or permissions')
    return VERIFY_BUNDLE(path, commit, digest)


@contextlib.contextmanager
def publication_lock(root):
    fd = os.open(root / '.publish.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('publication lock is not a regular file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def bounded_file(path, limit, *, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('publication input is not a bounded regular file')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('publication input exceeds size limit')
    return data


def stage(root, bundle, commit, digest):
    releases = root / 'releases'
    target = releases / commit
    if target.exists() or target.is_symlink():
        verify_release(target, commit, digest)
        return

    # The upload directory is writable by the deploy account. Open it once,
    # then copy only bounded regular files before verifying protected bytes.
    upload = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        raw = bounded_file('host-release.json', MAX_MANIFEST, dir_fd=upload)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('release manifest differs from the CI bundle')
        manifest = json.loads(raw)
        VALIDATE_MANIFEST(manifest)
        if manifest['host_version'] != commit:
            raise ValueError('release does not match the deployment commit')
        files = manifest['artifacts']
        if set(os.listdir(upload)) != files.keys() | {'host-release.json'}:
            raise ValueError('upload directory has missing or unexpected files')
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=releases) as temporary:
            copied = Path(temporary)
            for name in (*files, 'host-release.json'):
                data = raw if name == 'host-release.json' else bounded_file(name, files[name]['size'], dir_fd=upload)
                with (copied / name).open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o444)
                    os.fsync(stream.fileno())
            VERIFY_BUNDLE(copied, commit, digest)
            copied.chmod(0o555)
            fsync_dir(copied)
            copied.rename(target)
            fsync_dir(releases)
    finally:
        os.close(upload)


def current_pointer(root):
    current = root / 'current'
    if not current.is_symlink():
        if current.exists():
            raise ValueError('current is not a managed release pointer')
        return None
    pointer = os.readlink(current)
    if not POINTER.fullmatch(pointer):
        raise ValueError('current is not a managed release pointer')
    return pointer


def verify_previous(root, pointer, digest=None):
    if pointer is None:
        return None
    target = root / pointer
    if target.is_symlink() or not target.is_dir():
        raise ValueError('previous release directory is unavailable')
    if digest is None:
        digest = hashlib.sha256(bounded_file(target / 'host-release.json', MAX_MANIFEST)).hexdigest()
    verify_release(target, POINTER.fullmatch(pointer)[1], digest)
    return digest


def switch_pointer(root, pointer):
    current = root / 'current'
    if pointer is None:
        current.unlink()
        fsync_dir(root)
        return
    staged = root / ('.pointer-' + uuid.uuid4().hex)
    staged.symlink_to(pointer)
    try:
        os.replace(staged, current)
        fsync_dir(root)
    finally:
        staged.unlink(missing_ok=True)


def read_receipt(path, root):
    receipt = json.loads(bounded_file(path, MAX_RECEIPT))
    fields = {'format', 'root', 'candidate', 'manifest_sha256', 'previous', 'previous_manifest_sha256', 'phase'}
    if (not isinstance(receipt, dict) or receipt.keys() != fields or type(receipt['format']) is not int
            or receipt['format'] != 1 or receipt['root'] != str(root)
            or not isinstance(receipt['candidate'], str) or not POINTER.fullmatch(receipt['candidate'])
            or not isinstance(receipt['manifest_sha256'], str) or not DIGEST.fullmatch(receipt['manifest_sha256'])
            or receipt['previous'] is not None and (not isinstance(receipt['previous'], str) or not POINTER.fullmatch(receipt['previous']))
            or receipt['phase'] not in ('prepared', 'active', 'rolling_back', 'rolled_back')):
        raise ValueError('publication receipt is invalid for this root')
    previous_digest = receipt['previous_manifest_sha256']
    if ((receipt['previous'] is None) != (previous_digest is None)
            or previous_digest is not None and (not isinstance(previous_digest, str) or not DIGEST.fullmatch(previous_digest))):
        raise ValueError('publication receipt has invalid previous identity')
    return receipt


def write_receipt(path, receipt, phase, *, create=False):
    data = (json.dumps({**receipt, 'phase': phase}, sort_keys=True) + '\n').encode()
    if len(data) > MAX_RECEIPT:
        raise ValueError('publication receipt exceeds size limit')
    fd, temporary = tempfile.mkstemp(prefix='.publication-receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def activate(root, commit, digest, receipt_path):
    candidate = f'releases/{commit}'
    verify_release(root / candidate, commit, digest)
    current = current_pointer(root)
    if receipt_path.exists() or receipt_path.is_symlink():
        receipt = read_receipt(receipt_path, root)
        if receipt['candidate'] != candidate or receipt['manifest_sha256'] != digest:
            raise ValueError('publication receipt belongs to another candidate')
        if receipt['phase'] == 'active' and current == candidate:
            return
        if receipt['phase'] != 'prepared' or current not in (receipt['previous'], candidate):
            raise ValueError('publication receipt cannot activate the current pointer')
        verify_previous(root, receipt['previous'], receipt['previous_manifest_sha256'])
    else:
        previous_digest = verify_previous(root, current)
        receipt = {'format': 1, 'root': str(root), 'candidate': candidate, 'manifest_sha256': digest,
                   'previous': current, 'previous_manifest_sha256': previous_digest}
        write_receipt(receipt_path, receipt, 'prepared', create=True)
    if current != candidate:
        switch_pointer(root, candidate)
    write_receipt(receipt_path, receipt, 'active')


def rollback(root, receipt_path):
    receipt = read_receipt(receipt_path, root)
    current = current_pointer(root)
    previous = receipt['previous']
    if receipt['phase'] == 'rolled_back':
        if current != previous:
            raise ValueError('current changed after this publication was rolled back')
        return
    if current not in (receipt['candidate'], previous):
        raise ValueError('current does not belong to this publication')
    verify_previous(root, previous, receipt['previous_manifest_sha256'])
    if receipt['phase'] != 'rolling_back':
        write_receipt(receipt_path, receipt, 'rolling_back')
    if current != previous:
        switch_pointer(root, previous)
    write_receipt(receipt_path, receipt, 'rolled_back')


def record_marker(path):
    try:
        with path.open('xb') as stream:
            stream.write(b'yes\n')
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        pass
    fsync_dir(path.parent)


def check_transaction(receipt, token, action):
    # The deploy shell can disappear while a sudo child is still alive. This
    # fence and the receipt check run under the same lock as pointer changes.
    parent = receipt.parent
    if bounded_file(parent / 'transaction-token', 33) != (token + '\n').encode():
        raise ValueError('publication belongs to another deploy transaction')
    cancelled = parent / 'cli-publication-cancelled'
    committed = parent / 'cli-publication-committed'
    if action == 'rollback':
        if os.path.lexists(committed):
            raise ValueError('publication was already committed')
        record_marker(cancelled)
    elif os.path.lexists(cancelled):
        raise ValueError('the deploy transaction cancelled publication')
    elif action == 'activate' and os.path.lexists(committed):
        raise ValueError('publication was already committed')


def commit_publication(root, receipt_path):
    receipt = read_receipt(receipt_path, root)
    candidate = receipt['candidate']
    if receipt['phase'] != 'active' or current_pointer(root) != candidate:
        raise ValueError('the matched CLI assets are not active')
    verify_release(root / candidate, POINTER.fullmatch(candidate)[1], receipt['manifest_sha256'])
    record_marker(receipt_path.parent / 'cli-publication-committed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('stage', 'activate'):
        command = commands.add_parser(action)
        command.add_argument('--commit', required=True)
        command.add_argument('--manifest-sha256', required=True)
        if action == 'stage':
            command.add_argument('--bundle', type=Path, required=True)
        else:
            command.add_argument('--receipt', type=Path, required=True)
            command.add_argument('--transaction-token')
    rollback_command = commands.add_parser('rollback')
    rollback_command.add_argument('--receipt', type=Path, required=True)
    rollback_command.add_argument('--transaction-token')
    commit_command = commands.add_parser('commit')
    commit_command.add_argument('--receipt', type=Path, required=True)
    commit_command.add_argument('--transaction-token', required=True)
    args = parser.parse_args(argv)
    if args.action in ('stage', 'activate') and (not COMMIT.fullmatch(args.commit) or not DIGEST.fullmatch(args.manifest_sha256)):
        parser.error('a full commit and manifest SHA-256 are required')
    token = getattr(args, 'transaction_token', None)
    if token is not None and not TOKEN.fullmatch(token):
        parser.error('invalid deploy transaction token')
    try:
        create_store = args.action == 'stage' or (args.action == 'rollback' and token is not None)
        if args.root.is_symlink():
            raise ValueError('publication root cannot be a symlink')
        if create_store:
            public_directory(args.root, parents=True)
        root = args.root.resolve(strict=True)
        protected_directory(root)
        with publication_lock(root):
            releases = root / 'releases'
            if releases.is_symlink():
                raise ValueError('release store cannot be a symlink')
            if create_store:
                public_directory(releases)
            protected_directory(releases)
            if args.action == 'stage':
                fsync_dir(root)
                stage(root, args.bundle, args.commit, args.manifest_sha256)
            else:
                receipt = args.receipt.parent.resolve(strict=True) / args.receipt.name
                if receipt in (root / 'current', root / '.publish.lock') or receipt.is_relative_to(releases):
                    raise ValueError('receipt cannot replace publication state')
                if token is not None:
                    check_transaction(receipt, token, args.action)
                if args.action == 'activate':
                    activate(root, args.commit, args.manifest_sha256, receipt)
                elif args.action == 'commit':
                    commit_publication(root, receipt)
                elif token is None or os.path.lexists(receipt):
                    rollback(root, receipt)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        parser.exit(1, f'Hub2 release publication failed: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
