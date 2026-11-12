"""Expire undispatched admission waits and retire already-reported failures.

Native process death is still qualified by Bridge.reconcile; elapsed time never
stands in for containment evidence.
"""
import json
import logging
import subprocess
import time

ADMISSION_WINDOW_MS = 60_000


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


def reap_expired_launches(bridge, now=None):
    now = int(time.time()*1000) if now is None else now
    db = bridge.journal.db
    rows = db.execute("SELECT launches.*, launch_windows.admission_expires_at FROM launches JOIN launch_windows USING(operation_id) "
                      "WHERE state IN ('pending_admission','launch_failed_unconfirmed','aborting','deadline_stopping','launching','running') "
                      "AND COALESCE(runtime_deadline,admission_expires_at)<=? LIMIT 1000", (now,)).fetchall()
    inventory = None
    for row in rows:
        with bridge._inflight_lock:
            if row['session_id'] in bridge._inflight:
                continue
            if row['state'] == 'pending_admission':
                if row['native_id'] or row['native_dispatch_started'] == 1:
                    continue
                payload = {'operation_id': row['operation_id'], 'epoch': row['epoch'], 'native_id': None,
                           'launch_failed': True, 'no_dispatch': True, 'admission_expired': True}
                state, key = 'stopped_before_dispatch', f"no-dispatch:{row['operation_id']}:{row['epoch']}"
            elif row['state'] in {'aborting','deadline_stopping','launching','running'}:
                if type(row['runtime_deadline']) is not int or row['runtime_deadline'] > now:
                    continue
                try:
                    if inventory is None or not inventory.fresh():
                        inventory = bridge.launcher.inventory()
                    evidence = inventory.absence_evidence(row['workspace'], row['tag'])
                    if evidence is None:
                        continue
                except (RuntimeError, OSError, AttributeError, subprocess.SubprocessError):
                    logging.getLogger(__name__).warning('Expired launch census unavailable session=%s', row['session_id'])
                    continue
                evidence = {**evidence, 'dispatch_provenance': 'host_journal'}
                payload = {'operation_id': row['operation_id'], 'epoch': row['epoch'], 'native_id': row['native_id'],
                           'launch_expired': True, 'native_outcome': 'unconfirmed',
                           'runtime_deadline': row['runtime_deadline'], 'evidence': evidence}
                state, key = 'expired_unconfirmed', f"launch-expired:{row['operation_id']}:{row['epoch']}"
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
