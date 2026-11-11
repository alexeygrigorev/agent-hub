#!/usr/bin/env python3
"""Finite Hub alert evidence. This module never sends mail or observes providers.

Producer integrations own the evidence that every invocation is witnessed by an
independent controller. A private enrollment pins both installed producer and
controller artifacts. The controller must durably witness dispatch BEFORE it
happens, count *all* invocations even when this ledger could not begin, and mark
coverage incomplete on any observer error. A fresh snapshot is a real barrier
over dispatched work, not a timer rewriting yesterday's counters. Its durability
must be independent of this observer's failure domain. Operator metadata is not a
substitute for that integration or its live verification.

An observation failure must be caught by the caller so alerts and starts remain
available. It must not be followed by a successful completion of that invocation.
The independent invocation counts expose even a failed begin followed by a crash.
No missing file is interpreted as zero, and aggregate never updates freshness.
"""
import argparse
import contextlib
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import urllib.parse
import sys
import time
import uuid

RECIPIENT = 'dev@dtcdev.click'
REQUIRED_PRODUCERS = frozenset(('picker_control', 'picker_excluded', 'picker_quota_stale',
                               'credential_monitor', 'start_canary', 'quota_stale', 'backup'))
OPTIONAL_PRODUCERS = frozenset(('hub_watchdog',))
LIMIT = 131072
MAX_RECEIPTS = 128
MAX_ACTIVE = 8
MAX_COUNT = 2**63 - 1
MAX_AGE_MS = 60000
DECISIONS = {'observation': False, 'alert': True, 'alert_dedup': True, 'recovery': True}
CANARY_DECISIONS = {'pass': 'observation', 'recovering': 'observation',
                    'failure_below_threshold': 'observation', 'recovery': 'recovery',
                    'critical': 'alert', 'critical_dedup': 'alert_dedup'}


class EvidenceUnavailable(Exception):
    """Fixed, secret-free error codes; callers must fail open for actual work."""


def require(condition, reason):
    if not condition:
        raise EvidenceUnavailable(reason)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}', value) is not None


def number(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'duplicate_state_key')
        value[key] = item
    return value


class PrivateDirectory:
    """Pin every directory by fd; never follow leaf or ancestor symlinks."""
    def __init__(self, path, *, create=False):
        require(sys.platform == 'linux' and fcntl is not None, 'platform_unavailable')
        path = Path(path)
        require(path.is_absolute() and '..' not in path.parts, 'invalid_directory')
        self.fds = []
        try:
            self.fds.append(os.open('/', os.O_RDONLY | os.O_DIRECTORY))
            for part in path.parts[1:]:
                try:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=self.fds[-1])
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o700, dir_fd=self.fds[-1])
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=self.fds[-1])
                self.fds.append(fd)
                info = os.fstat(fd)
                # Shared group-writable checkout ancestors are permissible:
                # every component is pinned and owner checked, and the final
                # state directory is owner-only. Another uid cannot replace it
                # with an inode owned by this uid. World-writable roots refuse.
                require(info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o002,
                        'unsafe_directory_ancestor')
            info = os.fstat(self.fds[-1])
            require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                    'state_directory_not_private')
            if create:
                self.sync()
        except BaseException:
            self.close()
            raise

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def sync(self):
        # Also repair entries left behind by a failed creation in an earlier run.
        for fd in reversed(self.fds):
            os.fsync(fd)

    def open(self, name, flags=os.O_RDONLY):
        require(identifier(name), 'invalid_state_name')
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fds[-1])
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and not info.st_mode & 0o077 and info.st_nlink == 1,
                    'unsafe_state_file')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, name):
        fd = self.open(name)
        try:
            require(os.fstat(fd).st_size <= LIMIT, 'state_too_large')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(LIMIT + 1)
            require(len(raw) <= LIMIT, 'state_too_large')
            value = json.loads(raw, object_pairs_hook=unique_object)
            require(isinstance(value, dict), 'invalid_state')
            return value
        finally:
            os.close(fd)

    def publish(self, name, value, *, new=False):
        require(identifier(name), 'invalid_state_name')
        raw = canonical(value)
        require(len(raw) <= LIMIT, 'state_too_large')
        temp = 'tmp-' + uuid.uuid4().hex
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fds[-1])
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
            if new:
                os.link(temp, name, src_dir_fd=self.fds[-1], dst_dir_fd=self.fds[-1],
                        follow_symlinks=False)
            else:
                # Validate the current inode rather than replace an unsafe leaf.
                prior = self.open(name)
                os.close(prior)
                os.replace(temp, name, src_dir_fd=self.fds[-1], dst_dir_fd=self.fds[-1])
            self.sync()
        finally:
            os.close(fd)
            try:
                os.unlink(temp, dir_fd=self.fds[-1])
                self.sync()
            except FileNotFoundError:
                pass

    @contextlib.contextmanager
    def lock(self, *, write=False, timeout=0.25):
        fd = self.open('ledger.lock', os.O_RDWR if write else os.O_RDONLY)
        deadline = time.monotonic() + max(0, min(timeout, 1))
        try:
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, 'evidence_lock_busy')
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            yield
        finally:
            os.close(fd)


def read_private(path):
    path = Path(path)
    with PrivateDirectory(path.parent) as directory:
        return directory.read(path.name)


def verify_source(entry):
    artifacts = entry.get('artifacts')
    require(isinstance(artifacts, list) and 1 <= len(artifacts) <= 8, 'source_proof_missing')
    normalized = []
    for item in artifacts:
        require(isinstance(item, dict) and isinstance(item.get('path'), str)
                and re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256'))), 'invalid_source_proof')
        path = Path(item['path'])
        require(path.is_absolute(), 'invalid_source_path')
        # These are installed code artifacts, not credential/config contents.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.geteuid())
                    and not info.st_mode & 0o022 and info.st_nlink == 1
                    and info.st_size <= 16 * 1024 * 1024, 'unsafe_source_artifact')
            h = hashlib.sha256()
            total = 0
            while True:
                data = os.read(fd, 65536)
                if not data:
                    break
                total += len(data)
                require(total <= 16 * 1024 * 1024, 'source_artifact_too_large')
                h.update(data)
            require(h.hexdigest() == item['sha256'], 'installed_source_changed')
        finally:
            os.close(fd)
        normalized.append({'path': str(path), 'sha256': item['sha256']})
    require(entry.get('source_id') == digest(normalized), 'source_identity_mismatch')


def remote_spec(name, entry):
    remote = entry.get('remote')
    expected_kind = {'credential_monitor': 'credential_monitor_post_v1',
                     'picker_excluded': 'picker_diagnostic_v1', 'picker_quota_stale': 'picker_diagnostic_v1'}.get(name)
    require(expected_kind is not None and entry.get('mode') == 'external'
            and 'witness_path' not in entry and isinstance(remote, dict)
            and set(remote) == {'kind', 'base_url', 'credential_path', 'source_id', 'release_sha'}
            and remote.get('kind') == expected_kind, 'invalid_remote_authority')
    value = remote.get('base_url')
    require(isinstance(value, str) and len(value) <= 2048 and not any(ord(c) <= 32 for c in value), 'invalid_remote_origin')
    url = urllib.parse.urlsplit(value)
    require(url.scheme == 'https' or (url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1', 'localhost')), 'invalid_remote_origin')
    require(url.hostname and not url.username and not url.password and not url.query and not url.fragment
            and url.path in ('', '/'), 'invalid_remote_origin')
    require(url.port is None or 1 <= url.port <= 65535, 'invalid_remote_origin')
    require(isinstance(remote.get('credential_path'), str) and Path(remote['credential_path']).is_absolute()
            and re.fullmatch(r'[0-9a-f]{64}', str(remote.get('source_id')))
            and re.fullmatch(r'[0-9a-f]{40}', str(remote.get('release_sha'))), 'invalid_remote_pin')
    return remote


# Network work is in a bounded, isolated child. Socket timeouts alone permit a
# slow-drip response to run forever. No bearer enters argv, logs, disk or output.
REMOTE_FETCH = r"""
import json, sys, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError()
        value[key] = item
    return value
try:
    args = json.loads(sys.stdin.buffer.read(131073))
    request = urllib.request.Request(args['url'], headers={'Authorization': 'Bearer ' + args['token']}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=1.5) as response:
        if response.status != 200: raise ValueError()
        raw = response.read(131073)
    if len(raw) > 131072: raise ValueError()
    json.loads(raw, object_pairs_hook=unique)
    sys.stdout.buffer.write(raw)
except Exception:
    raise SystemExit(2)
"""


def remote_witness(name, entry):
    remote = remote_spec(name, entry)
    credential = read_private(remote['credential_path'])
    # Never forward a Hub credential to an unrelated configured HTTPS origin.
    require(isinstance(credential.get('base_url'), str)
            and credential['base_url'].rstrip('/') == remote['base_url'].rstrip('/'), 'credential_origin_mismatch')
    token = credential.get('access_token')
    require(isinstance(token, str) and 32 <= len(token) <= 8192
            and all(33 <= ord(c) < 127 for c in token), 'invalid_remote_credential')
    nonce = uuid.uuid4().hex
    endpoint = ('/api/model-picker/credential-monitor-evidence?nonce=' if name == 'credential_monitor' else
                '/api/model-picker/diagnostic-evidence?producer=' + name + '&nonce=')
    url = remote['base_url'].rstrip('/') + endpoint + nonce
    try:
        result = subprocess.run([sys.executable, '-I', '-S', '-c', REMOTE_FETCH],
            input=canonical({'url': url, 'token': token}), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=3, check=False,
            env={'PATH': os.defpath, 'LANG': 'C.UTF-8'})
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceUnavailable('remote_authority_unavailable') from None
    require(result.returncode == 0 and len(result.stdout) <= LIMIT, 'remote_authority_unavailable')
    value = json.loads(result.stdout, object_pairs_hook=unique_object)
    allowed = {'kind', 'producer', 'recipient', 'source_id', 'epoch', 'controller_id', 'limitations', 'complete',
               'started_total', 'finished_total', 'baseline_count', 'baseline_invocations', 'occurrence_count',
               'observed_at_ms', 'nonce', 'release_sha', 'artifact_proof'}
    require(isinstance(value, dict) and set(value) == allowed and value.get('nonce') == nonce
            and value.get('kind') == 'hub_controller_coverage_v1'
            and value.get('producer') == name and value.get('recipient') == RECIPIENT
            and value.get('artifact_proof') == 'loaded_and_current_v1'
            and value.get('source_id') == remote['source_id']
            and value.get('release_sha') == remote['release_sha'], 'remote_authority_identity_mismatch')
    # Local installed producer/fetcher pins and the remote VM pins are separate.
    # Enrollment/generation binds both; no local file claims to cover the VM.
    return dict(value, source_id=entry['source_id'])


def enrollment(value):
    require(value.get('kind') == 'hub_alert_enrollment_v1' and value.get('recipient') == RECIPIENT,
            'invalid_enrollment')
    entries = value.get('producers')
    require(isinstance(entries, dict) and REQUIRED_PRODUCERS <= set(entries)
            and set(entries) <= REQUIRED_PRODUCERS | OPTIONAL_PRODUCERS, 'producer_set_incomplete')
    for name, entry in entries.items():
        require(isinstance(entry, dict) and entry.get('mode') in ('ledger', 'external')
                and ('remote' in entry or isinstance(entry.get('witness_path'), str)), 'producer_not_enrolled')
        if 'remote' in entry:
            remote_spec(name, entry)
        verify_source(entry)
    return entries


def witness(name, entry, *, now_ms, remote_value=None):
    value = (remote_value if remote_value is not None else remote_witness(name, entry)) if 'remote' in entry else read_private(entry['witness_path'])
    require(value.get('kind') == 'hub_controller_coverage_v1' and value.get('producer') == name
            and value.get('source_id') == entry['source_id'], 'coverage_source_mismatch')
    require(identifier(value.get('epoch')) and identifier(value.get('controller_id')),
            'coverage_epoch_missing')
    require(value.get('limitations') == [] and value.get('complete') is True,
            'coverage_not_complete')
    for key in ('started_total', 'finished_total', 'observed_at_ms', 'baseline_count', 'baseline_invocations'):
        require(number(value.get(key)), 'invalid_coverage_counter')
    require(value['started_total'] == value['finished_total']
            and value['baseline_invocations'] <= value['started_total'], 'controller_invocation_incomplete')
    require(-5000 <= now_ms - value['observed_at_ms'] <= MAX_AGE_MS, 'coverage_stale')
    if entry['mode'] == 'external':
        require(number(value.get('occurrence_count'))
                and value['occurrence_count'] >= value['baseline_count'],
                'external_occurrence_count_missing_or_reset')
    return value


def install_enrollment(directory, value):
    """Explicit integration API, never called by aggregate or ordinary producers.

    Owners must first verify the instrumented runtime and independent controller
    coverage. This writes no zero counters and does not generate any witness.
    """
    enrollment(value)
    with PrivateDirectory(directory, create=True) as state:
        fd = os.open('ledger.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=state.fds[-1])
        os.fsync(fd)
        os.close(fd)
        state.sync()
        with state.lock(write=True):
            state.publish('enrollment.json', value, new=True)


def initialize_producer(directory, name, *, now_ms):
    """Explicit baseline from a real independent witness, not an absent-file zero."""
    require(name in REQUIRED_PRODUCERS | OPTIONAL_PRODUCERS, 'unknown_producer')
    with PrivateDirectory(directory) as state, state.lock(write=True):
        entry = enrollment(state.read('enrollment.json'))[name]
        require(entry['mode'] == 'ledger', 'producer_not_ledger')
        proof = witness(name, entry, now_ms=now_ms)
        require(proof['started_total'] == proof['baseline_invocations'], 'baseline_not_quiescent')
        state.publish(name + '.json', {
            'source_id': entry['source_id'], 'epoch': proof['epoch'], 'controller_id': proof['controller_id'],
            'baseline_count': proof['baseline_count'], 'baseline_invocations': proof['baseline_invocations'],
            'count': proof['baseline_count'], 'begun_total': proof['baseline_invocations'],
            'completed_total': proof['baseline_invocations'], 'active': {}, 'completed': [], 'decisions': {},
        }, new=True)


def ledger(value, entry):
    require(value.get('source_id') == entry['source_id'] and identifier(value.get('epoch'))
            and identifier(value.get('controller_id')), 'ledger_identity_mismatch')
    require(all(number(value.get(k)) for k in ('count', 'begun_total', 'completed_total')),
            'invalid_ledger_counter')
    require(number(value.get('baseline_count')) and number(value.get('baseline_invocations')),
            'invalid_ledger_baseline')
    require(isinstance(value.get('active'), dict) and len(value['active']) <= MAX_ACTIVE
            and isinstance(value.get('completed'), list) and len(value['completed']) <= MAX_RECEIPTS
            and isinstance(value.get('decisions'), dict) and len(value['decisions']) <= MAX_RECEIPTS,
            'invalid_ledger_receipts')
    active, completed, decisions = value['active'], value['completed'], value['decisions']
    require(all(identifier(k) and v is True for k, v in active.items())
            and all(identifier(k) for k in completed) and len(set(completed)) == len(completed)
            and not set(active).intersection(completed), 'invalid_invocation_receipts')
    require(value['completed_total'] == value['baseline_invocations'] + len(completed)
            and value['begun_total'] == value['completed_total'] + len(active), 'ledger_invocation_reset')
    for key, receipt in decisions.items():
        require(identifier(key) and isinstance(receipt, list) and len(receipt) == 2
                and receipt[0] in set(active) | set(completed) and receipt[1] in DECISIONS,
                'invalid_decision_receipt')
    require(value['count'] == value['baseline_count'] + sum(int(DECISIONS[v[1]]) for v in decisions.values()),
            'ledger_occurrence_reset')
    return value


class Producer:
    """One process epoch. Invocation IDs come from the independent controller."""
    def __init__(self, directory, name, epoch):
        require(name in REQUIRED_PRODUCERS | OPTIONAL_PRODUCERS and identifier(epoch), 'invalid_producer')
        self.directory, self.name, self.epoch = directory, name, epoch

    def mutate(self, callback):
        with PrivateDirectory(self.directory) as state, state.lock(write=True):
            entry = enrollment(state.read('enrollment.json'))[self.name]
            value = ledger(state.read(self.name + '.json'), entry)
            require(value['epoch'] == self.epoch, 'producer_epoch_changed')
            result = callback(value)
            state.publish(self.name + '.json', value)
            return result

    def begin(self, invocation_id):
        require(identifier(invocation_id), 'invalid_invocation_id')
        def begin(value):
            require(invocation_id not in value['completed'], 'invocation_already_completed')
            require(invocation_id not in value['active'], 'invocation_already_active')
            require(len(value['active']) < MAX_ACTIVE and len(value['completed']) < MAX_RECEIPTS,
                    'invocation_receipt_capacity')
            require(value['begun_total'] < MAX_COUNT, 'counter_exhausted')
            value['active'][invocation_id] = True
            value['begun_total'] += 1
        self.mutate(begin)
        return Invocation(self, invocation_id)


class Invocation:
    def __init__(self, producer, identity):
        self.producer, self.identity, self.dirty = producer, identity, False

    def record(self, decision_id, decision):
        """Call once per logical decision BEFORE suppression/delivery, not per incident."""
        def record(value):
            require(identifier(decision_id) and isinstance(decision, str) and decision in DECISIONS,
                    'invalid_alert_decision')
            require(self.identity in value['active'], 'invocation_not_active')
            prior = value['decisions'].get(decision_id)
            expected = [self.identity, decision]
            if prior is not None:
                require(prior == expected, 'decision_identity_conflict')
                return False
            require(len(value['decisions']) < MAX_RECEIPTS, 'decision_receipt_capacity')
            require(value['count'] < MAX_COUNT, 'counter_exhausted')
            value['decisions'][decision_id] = expected
            value['count'] += int(DECISIONS[decision])
            return True
        try:
            return self.producer.mutate(record)
        except Exception:
            self.dirty = True
            raise

    def complete(self):
        require(not self.dirty, 'invocation_observation_failed')
        def complete(value):
            require(self.identity in value['active'], 'invocation_not_active')
            value['active'].pop(self.identity)
            value['completed'].append(self.identity)
            value['completed_total'] += 1
        try:
            self.producer.mutate(complete)
        except Exception:
            self.dirty = True
            raise


def aggregate(directory, *, now_ms=None):
    """Read-only accepted contract, or explicit unavailable; never a fabricated zero."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        # Do network barriers outside the shared ledger lock: a slow remote
        # must not starve local producers' bounded exclusive writes.
        with PrivateDirectory(directory) as state, state.lock():
            original = state.read('enrollment.json')
            entries = enrollment(original)
        remote_values = {name: remote_witness(name, entry) for name, entry in entries.items() if 'remote' in entry}
        with PrivateDirectory(directory) as state, state.lock():
            current = state.read('enrollment.json')
            require(current == original, 'enrollment_changed_during_barrier')
            entries = enrollment(current)
            total, times, generations = 0, [], []
            for name, entry in sorted(entries.items()):
                proof = witness(name, entry, now_ms=now_ms, remote_value=remote_values.get(name))
                if entry['mode'] == 'external':
                    count = proof['occurrence_count']
                else:
                    value = ledger(state.read(name + '.json'), entry)
                    require(not value['active'], 'producer_invocation_active')
                    require(value['epoch'] == proof['epoch'] and value['controller_id'] == proof['controller_id'],
                            'coverage_epoch_changed')
                    require(value['baseline_count'] == proof['baseline_count']
                            and value['baseline_invocations'] == proof['baseline_invocations'],
                            'coverage_baseline_changed')
                    require(value['begun_total'] == value['completed_total'] == proof['started_total'],
                            'coverage_invocation_gap')
                    count = value['count']
                total += count
                require(total <= MAX_COUNT, 'aggregate_counter_exhausted')
                times.append(proof['observed_at_ms'])
                # Rebaselining is an explicit coverage boundary even when an
                # owner reuses the controller epoch. Bind delivery mode and
                # witness/artifact configuration too; switching evidence must
                # never make an earlier occurrence disappear within one run.
                generations.append([name, digest(entry), proof['controller_id'], proof['epoch'],
                                    proof['baseline_count'], proof['baseline_invocations']])
            return {'kind': 'dev_alert_occurrences_v1', 'recipient': RECIPIENT,
                    'generation': digest(generations), 'count': total, 'observed_at_ms': min(times)}
    except (OSError, ValueError, KeyError, TypeError, EvidenceUnavailable):
        return {'kind': 'hub_alert_evidence_unavailable_v1', 'available': False,
                'reason': 'coverage_unknown'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    result = aggregate(args.state_dir)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get('kind') == 'dev_alert_occurrences_v1' else 2


if __name__ == '__main__':
    raise SystemExit(main())
