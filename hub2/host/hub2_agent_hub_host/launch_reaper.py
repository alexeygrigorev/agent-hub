"""Expire undispatched admission waits and retire already-reported failures.

Native process death is still qualified by Bridge.reconcile; elapsed time never
stands in for containment evidence.
"""
import json
import logging
import subprocess
import threading
import time

ADMISSION_WINDOW_MS = 60_000
CENSUS_RETRY_SECONDS = 5
_OBSERVER_LOCK = threading.Lock()
_CENSUS_ERRORS = (RuntimeError, OSError, AttributeError, subprocess.SubprocessError)


def initialize(journal):
    with journal.db as db:
        db.execute('CREATE TABLE IF NOT EXISTS launch_windows(operation_id TEXT PRIMARY KEY, admission_expires_at INTEGER NOT NULL)')
        # Old journals have no creation timestamp. Give them one bounded upgrade
        # window, persisted once, rather than renewing their age at each restart.
        db.execute('INSERT OR IGNORE INTO launch_windows SELECT operation_id, ? FROM launches',
                   (int(time.time()*1000) + ADMISSION_WINDOW_MS,))


def remember(journal, operation, expires_at):
    now = int(time.time()*1000)
    until = min(expires_at, now + ADMISSION_WINDOW_MS) if type(expires_at) is int else now + ADMISSION_WINDOW_MS
    with journal.db as db:
        db.execute('INSERT OR IGNORE INTO launch_windows VALUES(?,?)', (operation, until))


def _observer(bridge):
    with _OBSERVER_LOCK:
        state = getattr(bridge, '_launch_reaper_observer', None)
        if state is None:
            state = bridge._launch_reaper_observer = {'thread': None, 'inventory': None, 'retry_at': 0.0}
        return state


def observation_thread(bridge):
    """The in-flight census worker, if any (tests join it; production never waits)."""
    return _observer(bridge)['thread']


def _observe(bridge, state):
    """Collect one registry census off the control loop, then settle with it.

    Runs on a daemon thread with no bridge lock held. The subprocess has its own
    bounded timeout; a failed observation leaves every launch unresolved.
    """
    log = logging.getLogger(__name__)
    try:
        try:
            inventory = bridge.launcher.inventory()
        except _CENSUS_ERRORS:
            log.warning('Expired launch census unavailable')
            with _OBSERVER_LOCK:
                state['retry_at'] = time.monotonic() + CENSUS_RETRY_SECONDS
            return
        with _OBSERVER_LOCK:
            state['inventory'] = inventory
        # Settle immediately: freshness is short and the next control sweep may be late.
        _sweep(bridge, None, inventory)
        settle_legacy = getattr(bridge, 'settle_legacy_census', None)
        if callable(settle_legacy):
            settle_legacy(inventory)
    except Exception:
        log.exception('Expired launch observation failed')
    finally:
        try:
            bridge.journal.close_thread()
        except Exception:
            pass
        with _OBSERVER_LOCK:
            if state['thread'] is threading.current_thread():
                state['thread'] = None


def request_census(bridge, refresh=False):
    """Return a fresh cached census, or start one single-inflight observation and return None. Never blocks."""
    state = _observer(bridge)
    with _OBSERVER_LOCK:
        inventory = state['inventory']
        try:
            if inventory is not None and not refresh and inventory.fresh():
                return inventory
        except _CENSUS_ERRORS:
            pass
        state['inventory'] = None
        running = state['thread']
        if (running is not None and running.is_alive()) or time.monotonic() < state['retry_at']:
            return None
        if not callable(getattr(getattr(bridge, 'launcher', None), 'inventory', None)):
            return None
        thread = state['thread'] = threading.Thread(target=_observe, args=(bridge, state), name='hub2-launch-reaper-census', daemon=True)
        thread.start()
    return None


def reap_expired_launches(bridge, now=None):
    """Settle overdue launches without waiting on native I/O; census rows settle from a background observation."""
    _sweep(bridge, now, None)


def _sweep(bridge, now, inventory):
    now = int(time.time()*1000) if now is None else now
    db = bridge.journal.db
    rows = db.execute("SELECT launches.*, launch_windows.admission_expires_at FROM launches JOIN launch_windows USING(operation_id) "
                      "WHERE state IN ('pending_admission','launch_failed_unconfirmed','aborting','deadline_stopping','launching','running') "
                      "AND COALESCE(runtime_deadline,admission_expires_at)<=? LIMIT 1000", (now,)).fetchall()
    requested = inventory is not None
    unusable_logged = False
    refreshed = False
    for row in rows:
        evidence = None
        needs_census = (row['state'] in {'aborting','deadline_stopping','launching','running'}
                        and type(row['runtime_deadline']) is int and row['runtime_deadline'] <= now)
        if needs_census:
            if not requested:
                requested, inventory = True, request_census(bridge)
            if inventory is None:
                continue
            try:
                evidence = inventory.absence_evidence(row['workspace'], row['tag'])
            except _CENSUS_ERRORS:
                if not unusable_logged:
                    unusable_logged = True
                    logging.getLogger(__name__).warning('Expired launch census unusable session=%s', row['session_id'])
                continue
            if evidence is None:
                continue
            # The receiver rejects evidence whose census began before the deadline
            # (permanently), so such a census must never be applied; keep the row
            # unresolved and obtain a census that starts at/after the deadline.
            started = evidence.get('started_at_ms')
            if type(started) is not int or started < row['runtime_deadline']:
                if not refreshed:
                    refreshed = True
                    request_census(bridge, refresh=True)
                continue
            evidence = {**evidence, 'dispatch_provenance': 'host_journal'}
        with bridge._inflight_lock:
            if row['session_id'] in bridge._inflight:
                continue
            if row['state'] == 'pending_admission':
                if row['native_id'] or row['native_dispatch_started'] == 1:
                    continue
                payload = {'operation_id': row['operation_id'], 'epoch': row['epoch'], 'native_id': None,
                           'launch_failed': True, 'no_dispatch': True, 'admission_expired': True}
                state, key = 'stopped_before_dispatch', f"no-dispatch:{row['operation_id']}:{row['epoch']}"
            elif needs_census:
                if not inventory.fresh():
                    continue
                payload = {'operation_id': row['operation_id'], 'epoch': row['epoch'], 'native_id': row['native_id'],
                           'launch_expired': True, 'native_outcome': 'unconfirmed',
                           'runtime_deadline': row['runtime_deadline'], 'evidence': evidence}
                state, key = 'expired_unconfirmed', f"launch-expired:{row['operation_id']}:{row['epoch']}"
            elif row['state'] in {'aborting','deadline_stopping','launching','running'}:
                continue
            else:
                if not row['legacy_failure_json']:
                    continue
                payload = json.loads(row['legacy_failure_json'])
                state, key = 'launch_failed_reported', f"legacy-unobserved:{row['operation_id']}:{row['epoch']}"
            with db:
                changed = db.execute('UPDATE launches SET state=? WHERE operation_id=? AND epoch=? AND state=?',
                                     (state, row['operation_id'], row['epoch'], row['state']))
                if changed.rowcount:
                    bridge.journal.enqueue(key, f"/api/sessions/{row['session_id']}/finish", payload)
