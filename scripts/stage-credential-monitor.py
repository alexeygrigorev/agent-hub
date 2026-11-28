#!/usr/bin/env python3
"""Stage a NEW Node observer boundary; never claim activation or coverage.

Default credential_monitor staging retains its three-artifact contract and
refuses replay. Active-release preparation additionally reuses exact retained
boundaries only behind durable source-bound intent; it never resets counters.

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


SOURCE_FAILURES = frozenset(('source_directory_untrusted', 'source_directory_unavailable',
    'source_file_unavailable', 'source_file_not_regular', 'source_file_foreign_owner',
    'source_file_linked', 'source_file_unsafe_mode', 'source_file_oversized',
    'source_file_changed', 'source_git_unverified', 'source_hash_mismatch', 'source_unclassified'))


class SourceUnverified(Refused):
    def __init__(self, reason):
        self.reason = reason if reason in SOURCE_FAILURES else 'source_unclassified'
        super().__init__('selected source unverified')


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


def artifact(checkout, relative, *, diagnostic=False):
    path = checkout / relative
    def check(condition, reason):
        if not condition and diagnostic:
            raise SourceUnverified(reason)
        require(condition)
    try:
        parent = directory(path.parent)
    except Refused:
        if diagnostic: raise SourceUnverified('source_directory_untrusted') from None
        raise
    except OSError:
        if diagnostic: raise SourceUnverified('source_directory_unavailable') from None
        raise
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(fd)
        check(stat.S_ISREG(info.st_mode), 'source_file_not_regular')
        check(info.st_uid in (0, os.geteuid()), 'source_file_foreign_owner')
        check(info.st_nlink == 1, 'source_file_linked')
        check(not info.st_mode & 0o022, 'source_file_unsafe_mode')
        check(info.st_size <= LIMIT, 'source_file_oversized')
        data = b''
        while len(data) <= LIMIT:
            chunk = os.read(fd, min(65536, LIMIT + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        after = os.fstat(fd)
        check(len(data) == info.st_size and len(data) <= LIMIT and
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns), 'source_file_changed')
        return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}
    except OSError:
        if diagnostic: raise SourceUnverified('source_file_unavailable') from None
        raise
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


def stage(checkout, destination, expected_commit, expected_hashes, *, producer="credential_monitor"):
    require(producer in PRODUCERS)
    diagnostic = producer != "credential_monitor"
    selected_artifacts = DIAGNOSTIC_ARTIFACTS if diagnostic else ARTIFACTS
    require(sys.platform == 'linux' and re.fullmatch('[0-9a-f]{40}', expected_commit)
            and set(expected_hashes) == set(selected_artifacts)
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
        artifacts = [artifact(checkout, name) for name in selected_artifacts]
        require(all(item['sha256'] == expected_hashes[name] for name, item in zip(selected_artifacts, artifacts)))
        source_id = hashlib.sha256(canonical(artifacts)).hexdigest()
        enrollment_id = uuid.uuid4().hex  # Public generation identity, not a credential.
        enrollment = {'kind': 'hub_picker_diagnostic_enrollment_v1' if diagnostic else 'hub_credential_monitor_enrollment_v1', 'producer': producer,
                      'recipient': 'dev@dtcdev.click', 'enrollment_id': enrollment_id,
                      'source_id': source_id, 'release_sha': expected_commit, 'artifacts': artifacts}
        journal = {'kind': 'hub_picker_diagnostic_journal_v1' if diagnostic else 'hub_credential_monitor_journal_v1',
                   **({'producer': producer} if diagnostic else {}), 'source_id': source_id,
                   'enrollment_id': enrollment_id, 'occurrence_count': 0, 'started_total': 0, 'finished_total': 0}
        os.mkdir(staging, 0o700, dir_fd=parent)
        temporary_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.fchmod(temporary_fd, 0o700)
        os.fsync(parent)
        write_new(temporary_fd, 'enrollment.json', enrollment)
        write_new(temporary_fd, 'journal.json', journal)
        os.fsync(temporary_fd)
        # Recheck both authority and bytes immediately before publication.
        require(head(checkout) == expected_commit and artifacts == [artifact(checkout, name) for name in selected_artifacts])
        current = os.stat(destination.parent, follow_symlinks=False)
        pinned = os.fstat(parent)
        require(stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino))
        publish(parent, staging, destination.name)
        os.fsync(parent)
        return {'kind': 'picker_diagnostic_staged_boundary_v1' if diagnostic else 'credential_monitor_staged_boundary_v1',
                **({'producer': producer} if diagnostic else {}), 'destination': str(destination),
                'release_sha': expected_commit, 'source_id': source_id, 'enrollment_id': enrollment_id,
                'historical_coverage': False, 'activated': False}
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        os.close(parent)
        os.close(checkout_fd)


PRODUCERS = ('credential_monitor', 'picker_excluded', 'picker_quota_stale')
KEYS = dict(zip(PRODUCERS, ('HUB2_ALERT_EVIDENCE_DIR', 'HUB2_PICKER_EXCLUDED_EVIDENCE_DIR',
                          'HUB2_PICKER_QUOTA_STALE_EVIDENCE_DIR')))
DIAGNOSTIC_ARTIFACTS = ARTIFACTS + ('hub2/src/model-picker.ts', 'hub2/src/routing-pool.ts')


def read_private(fd, name):
    leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        st = os.fstat(leaf)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid()
                and stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1 and st.st_size <= 131072)
        raw = os.read(leaf, 131073)
        after = os.fstat(leaf)
        require(len(raw) == st.st_size and (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        value = json.loads(raw)
        require(isinstance(value, dict))
        require(raw == canonical(value) + b'\n')  # Also rejects duplicate keys and noncanonical JSON.
        os.fsync(leaf)
        return value
    finally:
        os.close(leaf)


def sync_chain(path):
    fd = directory(path, private=True)
    os.close(fd)
    current = absolute(path)
    while True:
        fd = directory(current)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if current == current.parent:
            break
        current = current.parent


def validate_enrollment(entry, producer):
    diagnostic = producer != 'credential_monitor'
    require(isinstance(entry, dict))
    require(set(entry) == {'kind', 'producer', 'recipient', 'enrollment_id', 'source_id', 'release_sha', 'artifacts'})
    require(entry['kind'] == ('hub_picker_diagnostic_enrollment_v1' if diagnostic else 'hub_credential_monitor_enrollment_v1')
        and entry['producer'] == producer and entry['recipient'] == 'dev@dtcdev.click'
        and re.fullmatch('[0-9a-f]{32}', entry['enrollment_id'])
        and re.fullmatch('[0-9a-f]{40}', entry['release_sha'])
        and hashlib.sha256(canonical(entry['artifacts'])).hexdigest() == entry['source_id'])
    names = DIAGNOSTIC_ARTIFACTS if diagnostic else ARTIFACTS
    require(isinstance(entry['artifacts'], list) and len(entry['artifacts']) == len(names))
    for item, relative in zip(entry['artifacts'], names):
        require(set(item) == {'path', 'sha256'} and isinstance(item['path'], str)
            and item['path'].endswith('/' + relative) and absolute(item['path'])
            and re.fullmatch('[0-9a-f]{64}', item['sha256']))


def boundary_identity(path, producer):
    fd = directory(path, private=True)
    try:
        entry = read_private(fd, 'enrollment.json')
        validate_enrollment(entry, producer)
        return entry
    finally:
        os.close(fd)
        sync_chain(path)


def validate_boundary(path, producer, expected=None, *, rolling_prefix=False):
    require(type(rolling_prefix) is bool)
    fd = directory(path, private=True)
    try:
        entry = read_private(fd, 'enrollment.json')
        journal = read_private(fd, 'journal.json')
        diagnostic = producer != 'credential_monitor'
        validate_enrollment(entry, producer)
        require(isinstance(journal, dict))
        require(all(type(journal.get(k)) is int for k in ('occurrence_count', 'started_total', 'finished_total')))
        require(journal == {'kind': 'hub_picker_diagnostic_journal_v1' if diagnostic else 'hub_credential_monitor_journal_v1',
                           **({'producer': producer} if diagnostic else {}), 'source_id': entry['source_id'],
                           'enrollment_id': entry['enrollment_id'], 'occurrence_count': 0, 'started_total': 0, 'finished_total': 0})
        # Never use a prior process snapshot as a healthy witness or restore its
        # counters into a new boot. Inspect retained records only for corruption;
        # no record/epoch is rewritten, discarded, or interpreted as continuity.
        rows = []
        with os.scandir(fd) as entries:
            for item in entries:
                require(len(rows) < 8192)
                rows.append(item.name)
        chains, checkpoints = {}, {}
        rolling_boots = {match[1] for name in rows if (match := re.fullmatch(
            r'checkpoint-([0-9a-f]{32})-([01])\.json', name)) is not None}
        for name in rows:
            if name in ('enrollment.json', 'journal.json'):
                continue
            match = re.fullmatch(r'snapshot-([0-9a-f]{32})-([1-9][0-9]{0,2})\.json', name)
            checkpoint = re.fullmatch(r'checkpoint-([0-9a-f]{32})-([01])\.json', name)
            require(match is not None or checkpoint is not None)
            boot = (match or checkpoint)[1]
            record = read_private(fd, name)
            require(isinstance(record, dict))
            common = {'kind', 'controller_id', 'sequence', 'previous_sha256', 'journal'}
            require(set(record) == (common | {'prefix_sha256'} if checkpoint else common)
                    and record['kind'] == (('hub_picker_diagnostic_checkpoint_v1' if diagnostic else 'hub_credential_monitor_checkpoint_v1')
                        if checkpoint else ('hub_picker_diagnostic_snapshot_v1' if diagnostic else 'hub_credential_monitor_snapshot_v1'))
                    and record['controller_id'] == boot and type(record['sequence']) is int
                    and isinstance(record['previous_sha256'], str) and re.fullmatch('[0-9a-f]{64}', record['previous_sha256']))
            sequence = record['sequence']
            if checkpoint:
                require(256 < sequence <= 9007199254740991 and sequence % 2 == int(checkpoint[2])
                        and isinstance(record['prefix_sha256'], str) and re.fullmatch('[0-9a-f]{64}', record['prefix_sha256']))
            else:
                require(sequence == int(match[2]) and sequence <= 256)
            snapshot = record['journal']
            require(isinstance(snapshot, dict) and set(snapshot) == set(journal)
                    and all(snapshot[k] == journal[k] for k in journal if k not in ('occurrence_count', 'started_total', 'finished_total'))
                    and all(type(snapshot[k]) is int and 0 <= snapshot[k] <= 9007199254740991 for k in ('occurrence_count', 'started_total', 'finished_total'))
                    and snapshot['occurrence_count'] <= snapshot['finished_total'] <= snapshot['started_total']
                    and snapshot['started_total'] - snapshot['finished_total'] <= 128
                    and (checkpoint is not None or rolling_prefix or boot in rolling_boots or snapshot['started_total'] <= 128))
            (checkpoints if checkpoint else chains).setdefault(boot, {})[sequence] = record
        reached = False
        keys = ('occurrence_count', 'started_total', 'finished_total')
        for boot in set(chains) | set(checkpoints):
            chain = chains.get(boot, {})
            previous = hashlib.sha256(canonical(journal)).hexdigest()
            require(sorted(chain) == list(range(1, len(chain) + 1)))
            counts = (0, 0, 0)
            for number in sorted(chain):
                record = chain[number]
                current_counts = tuple(record['journal'][k] for k in keys)
                require(all(current >= prior for current, prior in zip(current_counts, counts)))
                counts = current_counts
                require(record['previous_sha256'] == previous)
                previous = hashlib.sha256(canonical(record)).hexdigest()
            rolled = checkpoints.get(boot, {})
            if not rolled:
                # Disk ABI at exactly128 is ambiguous until the first rollup:
                # keep the legacy hold rather than silently reset old capacity.
                reached |= bool(chain) and chain[max(chain)]['journal']['started_total'] == 128
                continue
            require(len(chain) == 256 and 1 <= len(rolled) <= 2)
            numbers = sorted(rolled)
            if len(numbers) == 1:
                require(numbers == [257])
            else:
                require(numbers[1] == numbers[0] + 1)
            prefix = previous
            for index, number in enumerate(numbers):
                record = rolled[number]
                require(record['prefix_sha256'] == prefix)
                current_counts = tuple(record['journal'][k] for k in keys)
                require(all(current >= prior for current, prior in zip(current_counts, counts)))
                counts = current_counts
                # An older superseded predecessor is deliberately not retained;
                # require the complete256 prefix anchor and the adjacent pair.
                if index > 0 or number == 257:
                    require(record['previous_sha256'] == previous)
                previous = hashlib.sha256(canonical(record)).hexdigest()
        if expected is not None:
            require(all(entry[k] == expected[k] for k in ('producer', 'release_sha', 'source_id', 'artifacts')))
        return {'enrollment': entry, 'capacity_reached': reached}
    finally:
        os.close(fd)
        sync_chain(path)


def committed_artifact_hash(checkout, commit, relative):
    # Installed bytes must belong to the selected local release, not merely an
    # edited checkout whose HEAD still names a reviewed commit. No network/Git
    # credentials/hooks/replacement objects are involved.
    env = {'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1'}
    command = ['/usr/bin/git', '--no-optional-locks', '-C', str(checkout), 'cat-file']
    object_name = commit + ':' + relative
    size = subprocess.run(command + ['-s', object_name], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=5, check=False)
    require(size.returncode == 0 and len(size.stdout) <= 32 and size.stdout.strip().isdigit())
    count = int(size.stdout)
    require(count <= LIMIT)
    data = subprocess.run(command + ['blob', object_name], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=5, check=False)
    require(data.returncode == 0 and len(data.stdout) == count)
    return hashlib.sha256(data.stdout).hexdigest()


def prepare_active(checkout, root, expected_commit, current, *, blocked=()):
    """Prepare each producer independently; never reset held same-tuple state."""
    require(sys.platform == 'linux')
    import fcntl
    checkout, root = absolute(checkout), absolute(root)
    require(set(current) == set(PRODUCERS) and set(blocked) <= set(PRODUCERS)
            and re.fullmatch('[0-9a-f]{40}', expected_commit))
    fd = directory(root, private=True)
    lock = None
    try:
        lock = os.open('.prepare.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fsync(lock);sync_chain(root)
        require(head(checkout) == expected_commit)
        specs, held, pointers, intents, source_failures = {}, {}, {}, {}, {}
        for producer in PRODUCERS:
            if producer in blocked:
                held[producer] = 'service_override'
                continue
            try:
                names = DIAGNOSTIC_ARTIFACTS if producer != 'credential_monitor' else ARTIFACTS
                artifacts = [artifact(checkout, name, diagnostic=True) for name in names]
                try:
                    expected_hashes = [committed_artifact_hash(checkout, expected_commit, name) for name in names]
                except (OSError, Refused, subprocess.SubprocessError):
                    raise SourceUnverified('source_git_unverified') from None
                if not all(row['sha256'] == expected_hash for row, expected_hash in zip(artifacts, expected_hashes)):
                    raise SourceUnverified('source_hash_mismatch')
                specs[producer] = {'producer': producer, 'release_sha': expected_commit,
                    'source_id': hashlib.sha256(canonical(artifacts)).hexdigest(), 'artifacts': artifacts}
            except (OSError, ValueError, TypeError, KeyError, Refused, subprocess.SubprocessError) as error:
                held[producer] = 'selected_source_unverified'
                source_failures[producer] = error.reason if isinstance(error, SourceUnverified) else 'source_unclassified'
        # Existing V1 source-bound intent may qualify a lost publication; it
        # does not authorize adopting a target with no durable ownership record.
        legacy = {'kind':'hub_node_observers_prepare_intent_v1','release_sha':expected_commit,'specs':specs}
        legacy_name = 'intent-' + expected_commit + '-' + hashlib.sha256(canonical(specs)).hexdigest() + '.json'
        for producer, spec in specs.items():
            try:
                if current[producer]:
                    try:
                        prior = boundary_identity(absolute(current[producer]), producer)
                    except (OSError, ValueError, TypeError, KeyError, Refused):
                        held[producer] = 'current_identity_unverified'
                        continue
                    same = all(prior[key] == spec[key] for key in ('producer','release_sha','source_id','artifacts'))
                    if same:
                        try:
                            checked = validate_boundary(absolute(current[producer]), producer, spec, rolling_prefix=True)
                        except (OSError, ValueError, TypeError, KeyError, Refused):
                            held[producer] = 'same_tuple_unqualified'
                            continue
                        if checked['capacity_reached']:
                            held[producer] = 'same_tuple_capacity_reached'
                            continue
                        # Existing trusted configured boundary, including a valid
                        # legacy name, is reused. Never create a second zero.
                        pointers[KEYS[producer]] = current[producer]
                        continue
                    # A genuine new selected source/release tuple is a NEW
                    # interval. The old enrollment identity is known; its held
                    # journal/snapshots need not be healthy and remain untouched.
                target = root / (producer + '-' + expected_commit + '-' + spec['source_id'])
                intent = {'kind':'hub_node_observer_prepare_intent_v2','producer':producer,
                          'release_sha':expected_commit,'spec':spec}
                name = 'intent-' + producer + '-' + expected_commit + '-' + spec['source_id'] + '.json'
                try:
                    previous = read_private(fd, name)
                except FileNotFoundError:
                    previous = None
                if previous is not None:
                    require(previous == intent)
                exists = os.path.lexists(target)
                if exists and previous is None:
                    try:
                        old_intent = read_private(fd, legacy_name)
                    except FileNotFoundError:
                        old_intent = None
                    if len(specs) != len(PRODUCERS) or old_intent != legacy:
                        held[producer] = 'target_ownership_unverified'
                        continue
                if previous is None:
                    write_new(fd, name, intent)
                sync_chain(root)  # File+all ancestor barriers, also on replay.
                if not exists:
                    stage(checkout, target, expected_commit,
                          {relative: row['sha256'] for relative, row in zip(
                              DIAGNOSTIC_ARTIFACTS if producer != 'credential_monitor' else ARTIFACTS,
                              spec['artifacts'])}, producer=producer)
                checked = validate_boundary(target, producer, spec, rolling_prefix=True)
                if checked['capacity_reached']:
                    held[producer] = 'same_tuple_capacity_reached'
                    continue
                pointers[KEYS[producer]] = str(target)
                intents[producer] = str(root / name)
            except (OSError, ValueError, TypeError, KeyError, Refused, subprocess.SubprocessError):
                held[producer] = 'producer_state_unqualified'
        require(head(checkout) == expected_commit)
        for producer, spec in specs.items():
            if KEYS[producer] not in pointers:
                continue
            try:
                require(spec['artifacts'] == [artifact(checkout, name, diagnostic=True) for name in
                    (DIAGNOSTIC_ARTIFACTS if producer != 'credential_monitor' else ARTIFACTS)])
            except (OSError, ValueError, TypeError, KeyError, Refused) as error:
                pointers.pop(KEYS[producer], None)
                held[producer] = 'selected_source_unverified'
                source_failures[producer] = error.reason if isinstance(error, SourceUnverified) else 'source_file_changed'
        sync_chain(root)
        require(set(held) == set(PRODUCERS) - {producer for producer in PRODUCERS if KEYS[producer] in pointers})
        return {'kind':'hub_node_observers_prepared_v2','release_sha':expected_commit,
                'activated':False,'historical_coverage':False,'pointers':pointers,
                'held':held,'intents':intents,'source_failures':source_failures}
    finally:
        if lock is not None:os.close(lock)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True)
    parser.add_argument('--destination')
    parser.add_argument('--prepare-active-root')
    parser.add_argument('--blocked-producer', choices=PRODUCERS, action='append', default=[])
    parser.add_argument('--producer', choices=PRODUCERS, default='credential_monitor')
    for producer in PRODUCERS:
        parser.add_argument('--current-' + producer.replace('_', '-'), default='')
    parser.add_argument('--expected-commit', required=True)
    for label in ('observer', 'main', 'worker', 'picker', 'pool'):
        parser.add_argument('--' + label + '-sha256')
    args = parser.parse_args()
    try:
        if args.prepare_active_root:
            require(args.destination is None)
            value = prepare_active(args.checkout, args.prepare_active_root, args.expected_commit,
                {producer: getattr(args, 'current_' + producer) for producer in PRODUCERS}, blocked=args.blocked_producer)
        else:
            require(args.destination is not None)
            selected = DIAGNOSTIC_ARTIFACTS if args.producer != 'credential_monitor' else ARTIFACTS
            values = (args.observer_sha256, args.main_sha256, args.worker_sha256, args.picker_sha256, args.pool_sha256)
            require(all(values[:len(selected)]) and not any(values[len(selected):]))
            value = stage(args.checkout, args.destination, args.expected_commit,
                          dict(zip(selected, values)), producer=args.producer)
        print(canonical(value).decode())
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, Refused, subprocess.SubprocessError):
        print('activation staging refused; no activation authorized; retain incomplete files for owner inspection', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
