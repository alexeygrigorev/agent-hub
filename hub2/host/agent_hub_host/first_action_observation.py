"""Late first-action activity observation.

The native start receipt is returned as soon as the pane exists; the model's
first tool action (the whoami artifact) usually lands later. This module
observes that artifact off the control/heartbeat threads, binds it to the exact
launch evidence record and incarnation, and latches one proof per launch.

Nothing here gates, delays, stops or retries a launch. Every unavailable,
malformed, stale, foreign, replaced, future or symlinked input stays UNKNOWN.
The checks are same-UID best effort: they do not claim to defeat a hostile
process running as the same user.
"""
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path

from .native_launch import (UUID_RE, canonical, first_action_data_valid, retirement_private_json,
                            same_workspace)

ARTIFACT_LIMIT = 262144
SKEW_SECONDS = 5
MAX_PASS = 16
EVIDENCE_START_LIMIT = 65536
UNOBSERVED = object()
_RECORD_LOCK = threading.RLock()


def record_path(workspace, task_id):
    return Path(workspace) / '.local' / f'launch-{task_id}.json'


def artifact_path(workspace, task_id):
    return Path(workspace) / '.local' / f'first-action-{task_id}.json'


def read_artifact(path):
    """Bounded no-follow read: (parsed JSON, mtime seconds) or None.

    Every directory is opened without following links and must belong to the
    current user or root. The file must be one regular, single-link, owned file
    that nobody else can write. The agent writes it with its own umask, so
    unlike private evidence read permission is not restricted."""
    if os.name != 'posix':
        return None
    try:
        path = Path(os.path.abspath(path))
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
                if os.fstat(directory).st_uid not in (0, os.geteuid()):
                    return None
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, 'rb') as stream:
            meta = os.fstat(stream.fileno())
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_nlink != 1
                    or meta.st_mode & 0o022 or meta.st_size > ARTIFACT_LIMIT):
                return None
            raw = stream.read(ARTIFACT_LIMIT + 1)
        if len(raw) > ARTIFACT_LIMIT:
            return None
        return json.loads(raw), meta.st_mtime
    except (OSError, ValueError, TypeError):
        return None


def observe_proof(path, start_json, dispatched_at, now=None):
    """Genuine proof or None. Timestamps come from the artifact, never the clock.

    Returns the artifact file's write time and the native activity stamp it
    reports. A file older than the dispatch, or claiming a time in the future,
    is not proof."""
    now = time.time() if now is None else now
    if (not isinstance(start_json, dict) or isinstance(dispatched_at, bool)
            or not isinstance(dispatched_at, (int, float))):
        return None
    loaded = read_artifact(path)
    if loaded is None:
        return None
    data, mtime = loaded
    if not dispatched_at - SKEW_SECONDS <= mtime <= now + SKEW_SECONDS or not first_action_data_valid(data, start_json):
        return None
    stamp = data.get('updated_at_ms', data.get('last_activity_ms'))
    if stamp > (now + SKEW_SECONDS) * 1000:
        return None
    return {'validated_at': mtime, 'activity_ms': stamp}


def start_evidence(start_json):
    """Identity subset of the native start record kept for the later check.

    Oversized values are dropped, leaving the late observation UNKNOWN."""
    if not isinstance(start_json, dict):
        return None
    try:
        return start_json if len(canonical(start_json).encode()) <= EVIDENCE_START_LIMIT else None
    except (TypeError, ValueError):
        return None


def write_record(path, evidence):
    """Atomic private replacement; readers never observe a partial record."""
    from .client import private_file
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_file(path.parent)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    with _RECORD_LOCK:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as stream:
                stream.write(json.dumps(evidence))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        private_file(path)


def _record(path):
    """Owner-private launch evidence or None; unavailable input stays unknown."""
    try:
        return retirement_private_json(path, ARTIFACT_LIMIT)
    except (OSError, ValueError, TypeError):
        return None


def _number(value):
    return type(value) in (int, float) and value == value and value > 0


def _identity_matches(record, candidate):
    """The record belongs to exactly this task, session, native, workspace, start and fence."""
    if (record.get('task_id') != 'hub2-' + candidate['session_id']
            or record.get('session_id') != candidate['native_id']
            or record.get('tag') != candidate['tag']
            or not same_workspace(record.get('workspace'), candidate['workspace'])
            or type(record.get('created_at_ms')) is not int
            or record['created_at_ms'] != candidate['created_at_ms']):
        return False
    for field in ('operation_id', 'epoch'):
        if field in record and (type(record[field]) is not type(candidate[field]) or record[field] != candidate[field]):
            return False
    return True


class LateFirstAction:
    """One bounded single-inflight worker plus an in-memory latch per launch.

    The caller (heartbeat) only snapshots rows and starts a pass; it never waits
    and never touches the filesystem. A pass performs at most MAX_PASS bounded
    metadata reads and exits, so a stuck read cannot pile up workers."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._proofs = {}
        self._thread = None

    @staticmethod
    def key(candidate):
        return (candidate['session_id'], candidate['operation_id'], candidate['epoch'], candidate['native_id'])

    def activity(self, candidate):
        """UNOBSERVED, None (latched without a usable stamp) or genuine activity ms.

        Always bound to the caller's current exact fence and native incarnation."""
        with self._lock:
            entry = self._proofs.get(self.key(candidate))
        if entry is None or entry['created_at_ms'] != candidate['created_at_ms']:
            return UNOBSERVED
        return entry['activity_ms']

    def request(self, candidates, live_keys, stop=None):
        """Prune retired launches and start one pass for unproven candidates."""
        with self._lock:
            for key in [key for key in self._proofs if key not in live_keys]:
                del self._proofs[key]
            pending = [candidate for candidate in candidates if self.key(candidate) not in self._proofs
                       or self._proofs[self.key(candidate)]['created_at_ms'] != candidate['created_at_ms']][:MAX_PASS]
            if not pending or (stop is not None and stop.is_set()):
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self._pass, args=(pending, stop),
                                            name='hub2-first-action', daemon=True)
            self._thread.start()
            return True

    def _pass(self, candidates, stop):
        for candidate in candidates:
            if stop is not None and stop.is_set():
                return
            try:
                self.observe(candidate, stop)
            except Exception:
                logging.getLogger(__name__).warning('Late first-action observation unavailable')

    def observe(self, candidate, stop=None):
        workspace, task_id = candidate['workspace'], 'hub2-' + candidate['session_id']
        if (not isinstance(candidate.get('native_id'), str) or not UUID_RE.fullmatch(candidate['native_id'])
                or candidate.get('tag') != 'task-' + task_id or type(candidate.get('created_at_ms')) is not int):
            return
        path = record_path(workspace, task_id)
        record = _record(path)
        if record is None or not _identity_matches(record, candidate):
            return
        now = self.clock()
        if record.get('first_action_validated_at') is not None:
            # Already latched on disk: restore the memory latch, never re-prove.
            stamp = record.get('first_action_activity_ms')
            usable = type(stamp) is int and 0 < stamp <= (now + SKEW_SECONDS) * 1000 and _number(record['first_action_validated_at'])
            self._latch(candidate, stamp if usable else None)
            return
        start = record.get('native_start')
        dispatched = record.get('native_dispatch_at')
        if (not isinstance(start, dict) or start.get('id') != candidate['native_id']
                or start.get('created_at_ms') != candidate['created_at_ms'] or not _number(dispatched)):
            return
        proof = observe_proof(artifact_path(workspace, task_id), start, dispatched, now)
        if proof is None or (stop is not None and stop.is_set()):
            return
        with _RECORD_LOCK:
            # Concurrent evidence writers (retry, replacement) win: re-read and
            # require the same identity and an unlatched record at write time.
            current = _record(path)
            if (current is None or not _identity_matches(current, candidate) or current.get('first_action_validated_at') is not None
                    or current.get('native_start') != start or current.get('native_dispatch_at') != dispatched
                    or (stop is not None and stop.is_set())):
                return
            current['first_action_validated_at'] = proof['validated_at']
            current['first_action_activity_ms'] = proof['activity_ms']
            write_record(path, current)
        self._latch(candidate, proof['activity_ms'])

    def _latch(self, candidate, activity_ms):
        with self._lock:
            self._proofs[self.key(candidate)] = {'created_at_ms': candidate['created_at_ms'],
                                                 'activity_ms': activity_ms}
