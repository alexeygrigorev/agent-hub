"""Recover lost claimed-launch receipts without spawning, stopping or deleting.

Timeout makes a launch eligible for a probe, never proof of process death.
The launcher lock fences a queued task, or its exact native lifecycle receipt
proves a completed run. Unknown effects retain their reservation.
"""
import datetime as dt
import json
from pathlib import Path
import sqlite3

CLAIM_TIMEOUT_SECONDS = 300


def reconcile_claimed_launch(dispatcher, row, *, dry_run=False):
    from .fleet import resolve_workspace_path, resolve_offer_cwd, _terminal_fleet_state
    from .launcher_recovery import settle_unstarted
    from .native_sessions import NativeTarget, SessionControlError

    if not isinstance(row, dict) or row.get('mode') != 'headless' or row.get('native_id'):
        return None
    launcher = dispatcher.settings.get('launcher', {})
    if not all(launcher.get(k) for k in ('argv', 'root', 'config_dir')):
        return None
    try:
        offer_id = row['id']
        task_id = 'hub-' + offer_id
        if row.get('launcher_task_id') not in (None, task_id):
            return None
        workspace = resolve_workspace_path(dispatcher.settings, row['workspace'])
        cwd = resolve_offer_cwd(workspace, row.get('cwd'), dispatcher.settings)
        prefix = list(launcher['argv']) + ['--config-dir', launcher['config_dir']]
        status = dispatcher.runner(prefix + ['status', '--id', task_id], cwd=launcher['root'])
        if not isinstance(status, dict):
            return None
        payload = status.get('payload') or {}
        if not isinstance(payload, dict):
            return None
        if payload.get('owner') != 'hub-fleet' or payload.get('cwd') != str(cwd):
            return None
        # The maintained status CLI deliberately returns only payload/state/
        # native_binding. Read identity and age from its existing local ledger,
        # as settle_unstarted does, without inventing CLI response fields.
        cfg = Path(launcher['config_dir'])
        db = cfg / 'state.db'
        if cfg.is_symlink() or db.is_symlink() or not db.is_file():
            return None
        with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=1) as conn:
            record = conn.execute('SELECT idempotency_key, created_at, payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if (not record or record[0] != task_id or not isinstance(record[2], str)
                or len(record[2].encode('utf-8')) > 131072 or json.loads(record[2]) != payload):
            return None
        # SQLite's launcher timestamps are UTC, including legacy naive strings.
        created = dt.datetime.fromisoformat(record[1])
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
        if not CLAIM_TIMEOUT_SECONDS <= age:
            return None
        if dry_run:
            return {'id': offer_id, 'state': 'probe_required'}
        if settle_unstarted(launcher, task_id, workspace, cwd):
            result = {'state': 'failed', 'confirmed_exit': True,
                      'launcher_task_id': task_id, 'reason': 'launcher_not_started'}
        else:
            binding = status.get('native_binding') or {}
            if not isinstance(binding, dict):
                return None
            if (binding.get('task_id') != task_id or binding.get('tag') != 'task-' + task_id
                    or binding.get('workspace') != str(cwd) or binding.get('engine') != row.get('engine')):
                return None
            target = NativeTarget(session_id=binding['native_id'], tag=binding['tag'],
                                  workspace=binding['workspace'], engine=binding['engine'],
                                  created_at_ms=binding['incarnation']).validated()
            # Launcher owns the incarnation-aware native probe and local lease
            # release. A structured report file by itself is never exit proof.
            receipt = dispatcher.runner(prefix + ['reconcile', '--id', task_id, '--json'], cwd=launcher['root'])
            if (not isinstance(receipt, dict) or receipt.get('task_id') != task_id or receipt.get('native_id') != target.session_id
                    or receipt.get('workspace') != target.workspace or receipt.get('tag') != target.tag
                    or receipt.get('resources_reserved') is not False
                    or receipt.get('native_status') not in ('dead', 'retired-finished')
                    or receipt.get('state') not in ('completed-awaiting-review', 'accepted', 'failed')):
                return None
            report = dispatcher._read_hub_finish_report({
                'offer_id': offer_id, 'workspace': str(cwd),
                'result_path': str(cwd / '.local' / 'hub-results' / (offer_id + '.json'))})
            result = {'state': _terminal_fleet_state(receipt['state'], bool(report)),
                      'native_id': target.session_id, 'launcher_task_id': task_id, 'confirmed_exit': True}
            if report:
                result.update(report)
                if result['state'] == 'failed':
                    result['report_status'] = 'failed'
            else:
                result.update(report_status='failed', report='Agent exited without a valid structured report; recovered exact launcher exit receipt.')
        ack = dispatcher.request('POST', '/api/fleet/launches/' + offer_id + '/result', result)
        if isinstance(ack, dict) and ack.get('id') == offer_id and ack.get('state') == result['state']:
            return {'id': offer_id, 'state': result['state'], 'reconciled': True}
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, sqlite3.Error, SessionControlError):
        # Includes lost acknowledgements. The next own-host cycle revalidates
        # the fence/identity and replays the same bounded terminal receipt.
        pass
    return None
