#!/usr/bin/env python3
"""Exclusively stage BOTH diagnostic boundaries; never configure or activate them.

No default action, network, credentials, service control or historical coverage.
Run as the Node service owner with an existing private parent. A consumed token
is never reused, even after failure. The caller MUST require exit0 and the exact
returned receipt before considering configuration; an existing file alone is
not success. Failed/uncertain claims and staging directories are retained.
"""
import argparse
from contextlib import ExitStack
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import uuid

ARTIFACTS = ('hub2/node/alert-evidence.ts', 'hub2/node/main.ts', 'hub2/src/worker.ts',
             'hub2/src/model-picker.ts', 'hub2/src/routing-pool.ts')
PRODUCERS = ('picker_excluded', 'picker_quota_stale')
FILE_LIMIT = 16 * 1024 * 1024
META_LIMIT = 1024 * 1024
GIT_BUDGET = 20


class Refused(Exception):
    """Fixed diagnostic, never raw Git/path/config contents."""


def require(ok):
    if not ok:
        raise Refused('staging_refused')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def path_value(path):
    value = os.fspath(path)
    require(isinstance(value, str) and len(value.encode('utf8')) <= 4096 and '\x00' not in value
            and os.path.isabs(value) and os.path.normpath(value) == value)
    return Path(value)


def inode(info):
    return info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode


def file_identity(info):
    return (*inode(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class Directory:
    """Keep the entire no-follow ancestry pinned and recheck path identities."""
    def __init__(self, path, stack, *, private=False):
        self.path = path_value(path)
        self.links = []
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        require(info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o022)
        for part in self.path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, child)
            info = os.fstat(child)
            require(info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o022)
            self.links.append((fd, part, child, inode(info)))
            fd = child
        self.fd = fd
        self.original = inode(os.fstat(fd))
        if private:
            info = os.fstat(fd)
            require(info.st_uid == os.geteuid() and info.st_gid == os.getegid() and stat.S_IMODE(info.st_mode) == 0o700)

    def verify(self):
        for parent, name, child, expected in self.links:
            actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISDIR(actual.st_mode) and inode(actual) == expected
                    and inode(os.fstat(child)) == expected)
        require(inode(os.fstat(self.fd)) == self.original)

    def absent(self, name):
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise Refused('staging_refused')

    def child(self, name, stack):
        os.mkdir(name, 0o700, dir_fd=self.fd)
        result = Directory(self.path / name, stack, private=True)
        return result

    def sync(self):
        self.verify()
        os.fsync(self.fd)
        self.verify()


class File:
    def __init__(self, directory, name, stack, *, data=None, limit=FILE_LIMIT):
        self.directory, self.name, self.limit = directory, name, limit
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_RDONLY if data is None else os.O_RDWR | os.O_CREAT | os.O_EXCL
        self.fd = os.open(name, flags, 0o600, dir_fd=directory.fd)
        stack.callback(os.close, self.fd)
        self.check_permissions(os.fstat(self.fd), created=data is not None)
        if data is not None:
            require(len(data) <= limit)
            os.fchmod(self.fd, 0o600)
            remaining = memoryview(data)
            while remaining:
                count = os.write(self.fd, remaining)
                require(count > 0)
                remaining = remaining[count:]
            os.fsync(self.fd)
        self.original = file_identity(os.fstat(self.fd))
        self.verify()
        self.bytes = self.read()
        if data is not None:
            require(self.bytes == data)

    @staticmethod
    def check_permissions(info, *, created=False):
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid in ((os.geteuid(),) if created else (0, os.geteuid()))
                and not info.st_mode & 0o022)
        if created:
            require(info.st_gid == os.getegid() and stat.S_IMODE(info.st_mode) == 0o600)

    def verify(self):
        self.directory.verify()
        actual = os.stat(self.name, dir_fd=self.directory.fd, follow_symlinks=False)
        require(file_identity(actual) == self.original
                and file_identity(os.fstat(self.fd)) == self.original)
        self.check_permissions(actual)

    def read(self):
        self.verify()
        require(self.original[6] <= self.limit)  # tuple: dev,ino,uid,gid,mode,nlink,size,...
        os.lseek(self.fd, 0, os.SEEK_SET)
        chunks, total = [], 0
        while total <= self.limit:
            value = os.read(self.fd, min(65536, self.limit + 1 - total))
            if not value:
                break
            chunks.append(value)
            total += len(value)
        self.verify()
        require(total == self.original[6] and total <= self.limit)
        return b''.join(chunks)

    def unchanged(self):
        require(self.read() == self.bytes)
        self.verify()


class Git:
    """Bound local Git object reads; no filters, hooks, replace refs or network."""
    def __init__(self, checkout, parent, stack):
        self.root = Directory(checkout, stack)
        self.metadata = Directory(self.root.path / '.git', stack)
        self.objects = Directory(self.metadata.path / 'objects', stack)
        self.metadata.absent('commondir')  # Shared/redirected object stores are not this deployment contract.
        info = Directory(self.objects.path / 'info', stack)
        info.absent('alternates')
        self.head_file = File(self.metadata, 'HEAD', stack, limit=4096)
        self.deadline = time.monotonic() + GIT_BUDGET
        self.cache = {}
        self.info = info
        # An isolated bare metadata view prevents Git reading checkout config,
        # includes, hooks, replacement refs or credential configuration. Only
        # the authenticated object store is exposed to the cat-file process.
        self.view_name = '.gitview-' + uuid.uuid4().hex
        self.view = parent.child(self.view_name, stack)
        self.view_refs = self.view.child('refs', stack)
        self.view_files = [File(self.view, 'HEAD', stack, data=b'ref: refs/heads/unused\n'),
                           File(self.view, 'config', stack, data=b'[core]\nrepositoryformatversion = 0\nbare = true\n')]
        def cleanup():
            # Exact owned temporary entries only, never evidence/claim/history.
            try:
                self.view.verify()
                for record in self.view_files:
                    record.verify(); os.unlink(record.name, dir_fd=self.view.fd)
                self.view_refs.verify(); os.rmdir('refs', dir_fd=self.view.fd)
                os.rmdir(self.view_name, dir_fd=parent.fd)
            except (OSError, Refused):
                pass  # Unexpected replacement is retained, never broadly removed.
        stack.callback(cleanup)

    def verify(self):
        self.root.verify(); self.metadata.verify(); self.objects.verify()
        self.info.verify(); self.info.absent('alternates'); self.head_file.unchanged()
        self.view.verify(); self.view_refs.verify()
        require(set(os.listdir(self.view.fd)) == {'HEAD', 'config', 'refs'} and not os.listdir(self.view_refs.fd))
        for record in self.view_files:
            record.unchanged()

    def run(self, args, limit):
        self.verify()
        env = {'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null', 'XDG_CONFIG_HOME': '/nonexistent', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0',
               'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1', 'GIT_DIR': '/proc/self/fd/' + str(self.view.fd),
               'GIT_OBJECT_DIRECTORY': '/proc/self/fd/' + str(self.objects.fd)}
        # The installer may kill this helper on timeout. Its sole Git child
        # must not outlive that ownership boundary (Linux-only helper).
        owner_pid = os.getpid()
        libc = ctypes.CDLL(None, use_errno=True)
        def child_lifetime():
            if libc.prctl(1, 9, 0, 0, 0) != 0 or os.getppid() != owner_pid:
                os._exit(70)
        child = subprocess.Popen(['/usr/bin/git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null', '-c', 'safe.directory='+str(self.view.path), '-c', 'core.fsmonitor=false', *args],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            pass_fds=(self.root.fd, self.view.fd, self.objects.fd), cwd='/', preexec_fn=child_lifetime)
        output = bytearray()
        try:
            os.set_blocking(child.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    remaining = self.deadline - time.monotonic()
                    require(remaining > 0)
                    require(selector.select(remaining))
                    value = os.read(child.stdout.fileno(), min(65536, limit + 1 - len(output)))
                    if not value:
                        break
                    output.extend(value)
                    require(len(output) <= limit)
            require(child.wait(timeout=max(.001, self.deadline - time.monotonic())) == 0)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            child.stdout.close()
        self.verify()
        return bytes(output)

    def head(self):
        # Production deploys are detached. A normal local branch is supported
        # without asking Git to read any checkout configuration.
        self.head_file.unchanged()
        value = self.head_file.bytes.removesuffix(b'\n')
        require(b'\n' not in value and b'\r' not in value)
        if value.startswith(b'ref: '):
            ref = value[5:]
            require(re.fullmatch(b'refs/heads/[A-Za-z0-9_./-]{1,200}', ref)
                    and b'..' not in ref and all(part and not part.startswith(b'.')
                        and not part.endswith((b'.', b'.lock')) for part in ref.split(b'/')))
            relative = ref.decode('ascii')
            with ExitStack() as stack:
                try:
                    file = File(Directory(self.metadata.path / str(Path(relative).parent), stack), Path(relative).name, stack, limit=64)
                    value = file.bytes.removesuffix(b'\n'); file.unchanged()
                except FileNotFoundError:
                    packed = File(self.metadata, 'packed-refs', stack, limit=META_LIMIT)
                    matches = [line.split(b' ',1)[0] for line in packed.bytes.splitlines()
                               if re.fullmatch(b'[0-9a-f]{40} ' + re.escape(ref), line)]
                    require(len(matches) == 1); value = matches[0]; packed.unchanged()
        require(re.fullmatch(b'[0-9a-f]{40}', value))
        return value.decode('ascii')

    def object(self, kind, oid):
        require(re.fullmatch('[0-9a-f]{40}', oid))
        key = kind, oid
        if key not in self.cache:
            limit = FILE_LIMIT if kind == 'blob' else META_LIMIT
            size = self.run(['cat-file', '-s', oid], 32).strip()
            require(re.fullmatch(b'[0-9]+', size) and int(size) <= limit)
            data = self.run(['cat-file', kind, oid], limit)
            require(len(data) == int(size)
                    and hashlib.sha1(kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data).hexdigest() == oid)
            self.cache[key] = data
        return self.cache[key]

    def artifacts(self, commit):
        raw = self.object('commit', commit)
        match = re.match(b'tree ([0-9a-f]{40})\n', raw)
        require(match is not None)
        root = match[1].decode()
        found = {}
        for path in ARTIFACTS:
            tree = root
            components = path.split('/')
            for index, component in enumerate(components):
                data, offset, entries = self.object('tree', tree), 0, {}
                while offset < len(data):
                    end = data.find(b'\0', offset)
                    require(end > offset and end + 21 <= len(data))
                    header, oid = data[offset:end], data[end+1:end+21].hex()
                    mode, name = header.split(b' ', 1)
                    require(name and name not in entries and b'/' not in name and name not in (b'.', b'..'))
                    entries[name] = mode, oid
                    offset = end + 21
                require(component.encode() in entries)
                mode, tree = entries[component.encode()]
                require(mode in ((b'100644', b'100755') if index == len(components)-1 else (b'40000',)))
            found[path] = tree, self.object('blob', tree)
        return found


def publish(parent, temporary, final):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    require(rename is not None)
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent.fd, os.fsencode(temporary), parent.fd, os.fsencode(final), 1):
        raise Refused('staging_refused')


def exact_bundle(root, directories):
    root.verify()
    require(set(os.listdir(root.fd)) == {*PRODUCERS, 'publication.json'})
    for directory in directories:
        directory.verify()
        require(set(os.listdir(directory.fd)) == {'enrollment.json', 'journal.json'})


def stage(checkout, parent, commit, transaction_token):
    require(sys.platform == 'linux' and isinstance(commit, str) and re.fullmatch('[0-9a-f]{40}', commit)
            and isinstance(transaction_token, str) and re.fullmatch('[0-9a-f]{32,64}', transaction_token))
    with ExitStack() as stack:
        destination = Directory(parent, stack, private=True)
        repo = Git(checkout, destination, stack)
        destination.absent(transaction_token)
        claim_name = transaction_token + '.claim'
        destination.absent(claim_name)
        require(repo.head() == commit)
        expected = repo.artifacts(commit)
        sources, artifacts = [], []
        for relative in ARTIFACTS:
            path = repo.root.path / relative
            source = File(Directory(path.parent, stack), path.name, stack)
            require(source.bytes == expected[relative][1])
            sources.append(source)
            artifacts.append({'path': str(path), 'sha256': sha(source.bytes)})
        source_id = sha(canonical(artifacts))
        final_path = destination.path / transaction_token
        # Durable reservation is never removed: interruption consumes this token.
        claim = File(destination, claim_name, stack, limit=META_LIMIT, data=canonical({
            'kind': 'picker_diagnostic_stage_claim_v1', 'transaction_token': transaction_token,
            'release_sha': commit, 'destination': str(final_path), 'source_id': source_id}) + b'\n')
        destination.sync()
        pending_name = '.pending-' + transaction_token + '-' + uuid.uuid4().hex
        pending = destination.child(pending_name, stack)
        entries, generated, directories = {}, [claim], []
        for producer in PRODUCERS:
            directory = pending.child(producer, stack)
            directories.append(directory)
            enrollment_id = uuid.uuid4().hex  # Nonsecret identity, never a credential.
            enrollment = {'kind': 'hub_picker_diagnostic_enrollment_v1', 'producer': producer,
                'recipient': 'dev@dtcdev.click', 'enrollment_id': enrollment_id, 'source_id': source_id,
                'release_sha': commit, 'artifacts': artifacts}
            journal = {'kind': 'hub_picker_diagnostic_journal_v1', 'producer': producer,
                'enrollment_id': enrollment_id, 'source_id': source_id,
                'occurrence_count': 0, 'started_total': 0, 'finished_total': 0}
            enrollment_bytes, journal_bytes = canonical(enrollment) + b'\n', canonical(journal) + b'\n'
            generated.append(File(directory, 'enrollment.json', stack, data=enrollment_bytes))
            generated.append(File(directory, 'journal.json', stack, data=journal_bytes))
            directory.sync()
            entries[producer] = {'directory': str(final_path / producer), 'source_id': source_id,
                                 'enrollment_id': enrollment_id, 'enrollment_sha256': sha(enrollment_bytes),
                                 'journal_sha256': sha(journal_bytes)}
        receipt = {'kind': 'picker_diagnostic_stage_receipt_v1', 'transaction_token': transaction_token,
            'release_sha': commit, 'directory': str(final_path), 'producers': entries, 'artifacts': artifacts,
            'git_blobs': {path: expected[path][0] for path in ARTIFACTS},
            'staged': True, 'activated': False, 'historical_coverage': False, 'caller_must_require_exit0': True}
        receipt_bytes = canonical(receipt) + b'\n'
        generated.append(File(pending, 'publication.json', stack, data=receipt_bytes))
        pending.sync()
        for source in sources:
            source.unchanged()
        require(repo.head() == commit)
        for record in generated:
            record.unchanged()
        destination.verify(); exact_bundle(pending, directories)
        publish(destination, pending_name, transaction_token)
        # Rename changes the one known ancestry link, not any child inode.
        pending.path = final_path
        parent_fd, _old, child, identity = pending.links[-1]
        pending.links[-1] = (parent_fd, transaction_token, child, identity)
        for directory in directories:
            directory.path = final_path / directory.path.name
            directory.links = pending.links + [directory.links[-1]]
        destination.sync()
        for source in sources:
            source.unchanged()
        require(repo.head() == commit)
        for record in generated:
            record.unchanged()
        exact_bundle(pending, directories)
        pending.sync(); destination.sync()
        # Final inode/mode/link fences follow durability work. No generated
        # evidence is rewritten to heal a replacement; runtime must still
        # independently verify its own loaded/current source on activation.
        for record in [*sources, *generated]:
            record.verify()
        repo.verify(); exact_bundle(pending, directories); destination.verify()
        return {**receipt, 'publication_receipt_sha256': sha(receipt_bytes), 'status': 'staged_durable_not_activated'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    command = commands.add_parser('stage')
    for flag in ('checkout', 'parent', 'commit', 'transaction-token'):
        command.add_argument('--' + flag, required=True)
    args = parser.parse_args()
    try:
        result = stage(args.checkout, args.parent, args.commit, args.transaction_token)
        print(canonical(result).decode('utf8'))
        return 0
    except (Refused, OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        print('staging_refused; no activation authorized; retain token claim and partial evidence', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
