"""Retry a late worker file after an authenticated terminal acknowledgement.

This path only enriches an existing terminal receipt. It never proves native
exit, changes terminal state, starts work, or releases a reservation.
"""
import time
import datetime as dt

WINDOW_MS = 120000


def pending(record, now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    deadline = record.get('late_report_deadline_ms')
    return (record.get('settled') is True and record.get('state') in ('exited', 'failed')
            and not record.get('routing_task_id') and record.get('worker_report_received') is False
            and type(deadline) is int and now_ms < deadline <= now_ms + WINDOW_MS)


def arm(record, received, runtime, now_ms=None):
    record['worker_report_received'] = bool(received)
    record['cleanup_pending'] = True
    if not received and not record.get('routing_task_id'):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        record['late_report_deadline_ms'] = now_ms + WINDOW_MS
        record['terminal_runtime'] = runtime


def deliver(dispatcher, record):
    if not pending(record):
        return False
    report = dispatcher._read_hub_finish_report(record)
    if not report:
        return False
    payload = dict(state=record['state'], confirmed_exit=True,
                   native_id=record['native_id'], launcher_task_id=record['task_id'], **report)
    runtime = record.get('terminal_runtime')
    if isinstance(runtime, dict):
        payload['runtime'] = dict(runtime, outcome='reported')
    try:
        ack = dispatcher.request('POST','/api/fleet/launches/'+record['offer_id']+'/result',payload)
    except Exception:
        return False
    if not isinstance(ack, dict) or ack.get('id') != record['offer_id'] or ack.get('state') != record['state']:
        return False
    record['worker_report_received'] = True
    record['late_report_deadline_ms'] = 0
    dispatcher._persist_launch_record(record)
    return True


def needs_cleanup(record):
    return (record.get('settled') is True and record.get('state') in ('exited', 'failed')
            and record.get('cleanup_pending') is True and not record.get('routing_task_id'))


def cleanup(dispatcher, record):
    if not needs_cleanup(record) or pending(record):
        return
    from pathlib import Path
    dispatcher._maybe_cleanup_worktree(Path(record['workspace']), record.get('native_id'))
    record['cleanup_pending'] = False
    record['late_report_deadline_ms'] = 0
    dispatcher._persist_launch_record(record)


def acknowledge(dispatcher, record, payload, received):
    """Persist an owned terminal delivery before posting; no native exit inference.

    The caller must already hold the maintained launcher's confirmed terminal
    receipt or independently qualified retirement. Unknown acknowledgements keep
    the exact intended state for a positively requalified retry after restart.
    """
    from .exit_evidence import bound_finish_fields
    if (payload.get('state') not in ('exited', 'failed') or payload.get('confirmed_exit') is not True
            or payload.get('native_id') != record.get('native_id')
            or payload.get('launcher_task_id') != record.get('task_id')):
        return False
    payload = bound_finish_fields(payload)
    if record.get('terminal_enrichment') is True and payload['state'] == 'exited':
        # The original diagnostic remains in the private journal. The existing
        # same-state API replay accepts runtime/report evidence without one.
        payload.pop('diagnostic', None)
    record['terminal_payload'] = payload
    dispatcher._active_launches[record['offer_id']] = record
    try:
        dispatcher._persist_launch_record(record)
        path = '/api/fleet/launches/'+record['offer_id']+'/result'
        try:
            ack = dispatcher.request('POST', path, payload)
        except ValueError as error:
            # A prior positive native catalog/stop receipt can already have
            # closed Fleet as exited. Missing JSON still means failed work,
            # but cannot rewrite that terminal lifecycle. Only this exact
            # authenticated conflict permits the existing same-native/task
            # exited replay; transport ambiguity never changes the intent.
            runtime = payload.get('runtime')
            if (str(error) != 'HTTP request failed with status 409: invalid_transition'
                    or record.get('mode') != 'headless' or record.get('routing_task_id')
                    or payload['state'] != 'failed' or payload.get('report_status') != 'failed'
                    or not isinstance(runtime, dict)
                    or runtime.get('outcome') not in ('failed-with-partial-work', 'failed-without-report')):
                raise
            payload = dict(payload, state='exited')
            payload.pop('diagnostic', None)
            record.update(terminal_enrichment=True, terminal_payload=payload)
            dispatcher._persist_launch_record(record)
            ack = dispatcher.request('POST', path, payload)
        if not isinstance(ack, dict) or ack.get('id') != record['offer_id'] or ack.get('state') != payload['state']:
            return False
        record.pop('terminal_payload', None)
        record.update(settled=True, state=payload['state'], settled_at=dt.datetime.now(dt.timezone.utc).isoformat())
        arm(record, received, payload.get('runtime'))
        dispatcher._persist_launch_record(record)
        cleanup(dispatcher, record)
        return True
    except Exception:
        # The server may have accepted the result. Preserve the delivery journal
        # and never report acknowledgement or clean the worktree on uncertainty.
        return False
