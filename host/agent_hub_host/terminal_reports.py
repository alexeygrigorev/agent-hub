"""Independent terminal delivery with per-launch, cross-process journal fencing.

A result/capture/tombstone is only a scheduling hint. The ordinary reconciler
still proves native retirement and launcher resource release before delivery.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import re
from pathlib import Path
import threading
import uuid
import weakref

from . import private_fs

POLL_SECONDS = 5
_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()
_IDENTITY = ('offer_id', 'task_id', 'mode', 'workspace', 'native_id', 'tag',
             'engine', 'incarnation', 'target_cwd')


class StaleLaunchRecord(RuntimeError):
    """A concurrent reconciler advanced the durable delivery journal."""


@contextmanager
def launch_guard(dispatcher, offer_id):
    """Nonblocking per-offer lock, reentrant only in the owning thread.

    The lock file is never removed. Independent controller/timer processes use
    the same kernel lock; unrelated launches and the command loop never wait.
    """
    if not isinstance(offer_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', offer_id):
        raise ValueError('invalid launch id')
    jdir = dispatcher._journal_launches_dir()
    if jdir is None:
        yield
        return
    key = str(jdir.resolve() / (offer_id + '.lock'))
    held = getattr(_HELD, 'paths', set())
    if key in held:
        yield
        return
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise BlockingIOError('launch reconciliation busy')
    try:
        with private_fs.exclusive_lock(key):
            _HELD.paths = held | {key}
            try:
                yield
            finally:
                _HELD.paths = held
    finally:
        lock.release()


def read_record(dispatcher, offer_id):
    jdir = dispatcher._journal_launches_dir()
    if jdir is None:
        return None
    try:
        data = json.loads(private_fs.read_bytes(jdir / (offer_id + '.json'), 131072))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get('offer_id') != offer_id:
        raise ValueError('invalid launch journal')
    return data


def same_identity(left, right):
    # A positively qualified launcher recovery may fill a pre-start record's
    # previously absent native tuple. Established fields must never change.
    return all(left.get(k) in (None, '') or right.get(k) in (None, '')
               or left[k] == right[k] for k in _IDENTITY)


def persist(dispatcher, record):
    offer_id = record['offer_id']
    with launch_guard(dispatcher, offer_id):
        current = read_record(dispatcher, offer_id)
        if current is not None:
            if not same_identity(current, record):
                raise ValueError('launch journal identity conflict')
            if current.get('_journal_revision', 0) != record.get('_journal_revision', 0):
                # Never undo an outbox, acknowledged receipt, late delivery or
                # cleanup marker with a stale catalog/dispatch snapshot. A
                # stale delivery also retries from a fresh durable record.
                if (current.get('terminal_payload') or current.get('settled') is True
                        or record.get('terminal_payload') or record.get('settled') is True):
                    raise StaleLaunchRecord('launch delivery advanced')
                merged = dict(current)
                merged.update(record)
                record.update(merged)
        revision = (current or {}).get('_journal_revision', 0)
        if type(revision) is not int or revision < 0:
            raise ValueError('invalid launch journal revision')
        record['_journal_revision'] = revision + 1
        raw = json.dumps(record, sort_keys=True, ensure_ascii=False).encode('utf-8')
        private_fs.atomic_write(dispatcher._journal_launches_dir() / (offer_id + '.json'), raw, 131072)


def persist_observation(dispatcher, record):
    """Catalog snapshots are expendable; durable terminal delivery is not."""
    try:
        dispatcher._persist_launch_record(record)
    except (BlockingIOError, StaleLaunchRecord):
        pass


def guarded_records(dispatcher, offer_ids):
    """Reload under the same lock held through proof, outbox, ACK and cleanup."""
    for offer_id, snapshot in list(dispatcher._active_launches.items()):
        if offer_ids is not None and offer_id not in offer_ids:
            continue
        try:
            with launch_guard(dispatcher, offer_id):
                record = read_record(dispatcher, offer_id)
                if record is None:
                    record = copy.deepcopy(snapshot)
                elif not same_identity(snapshot, record):
                    continue
                dispatcher._active_launches[offer_id] = record
                yield offer_id, record
                dispatcher._active_launches[offer_id] = record
        except (OSError, ValueError, StaleLaunchRecord):
            continue


def ready_hint(record, state_dir):
    """A refused local hint must not starve every other launch's delivery."""
    try:
        return _ready_hint(record, state_dir)
    except (OSError, ValueError, TypeError):
        return False


def _ready_hint(record, state_dir):
    """Cheap local scheduling only; no native command, auth check or exit claim."""
    from .late_reports import pending, needs_cleanup
    if record.get('settled') is True:
        return pending(record) or needs_cleanup(record)
    if record.get('terminal_payload'):
        return True
    workspace, offer_id = record.get('workspace'), record.get('offer_id')
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        return False
    root = Path(workspace) / '.local'
    if (root / 'hub-results' / (offer_id + '.json')).is_file():
        return True
    # Retained capture completion also covers natural exits without result JSON.
    from .headless_output import retained
    capture = retained(record)
    if capture and capture.get('capture_state') in ('complete', 'children_open', 'over_budget'):
        return True
    try:
        native_id = str(uuid.UUID(record.get('native_id')))
    except (ValueError, TypeError, AttributeError):
        return False
    from .retired_native import resolve_aplexer_state_dir
    return (Path(resolve_aplexer_state_dir(state_dir)) / 'retired-sessions' /
            native_id / 'tombstone.json').is_file()


def poll(source):
    """Fresh journal observer, independent of maintenance/admission/auth work.

    At most four reconciliations run at once. A rotating bounded batch prevents
    one busy launch from monopolizing every poll. The dedicated single-inflight
    host thread waits for this batch; no detached worker backlog is accumulated.
    """
    if not isinstance(source.journal_dir, (str, Path)) or not source.journal_dir:
        return []
    from .fleet import Dispatcher
    observer = Dispatcher(dict(source.settings), request=source.request,
                          runner=source.runner, journal_dir=source.journal_dir,
                          role_config=source.role_config, role_config_path=source.role_config_path)
    candidates = sorted(offer_id for offer_id, record in observer._active_launches.items()
                        if ready_hint(record, observer.settings.get('aplexer_state_dir')))
    if not candidates:
        return []
    previous = getattr(source, '_terminal_cursor', '')
    candidates = [key for key in candidates if key > previous] + [key for key in candidates if key <= previous]
    batch = candidates[:8]
    source._terminal_cursor = batch[-1]
    def reconcile(offer_id):
        return observer.reconcile_launches(offer_ids={offer_id}, terminal_only=True)
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='terminal-report') as pool:
        return [row for rows in pool.map(reconcile, batch) for row in rows]
