"""Recover legacy no-start reservations using the maintained launcher fence."""


def reconcile_unstarted(dispatcher, *, dry_run=False, rows=None):
    from .fleet import resolve_workspace_path, resolve_offer_cwd
    from .launcher_recovery import settle_unstarted
    proposals = []
    if rows is None:
        try:
            rows = dispatcher.request('GET', '/api/fleet/reconciliation')['launches']
        except (ValueError, KeyError, TypeError):
            return proposals  # Old Worker during a rolling update.
    if not isinstance(rows, list) or len(rows) > 100:
        return proposals
    for row in rows:
        try:
            # Lost claim/finish responses may leave no dispatch diagnostic or
            # host journal. Recover from the maintained launcher's own ledger.
            from .wedged_launches import reconcile_claimed_launch
            recovered = reconcile_claimed_launch(dispatcher, row, dry_run=dry_run)
            if recovered:
                proposals.append(recovered)
                continue
            if row.get('native_id'):
                if reconcile_retired(dispatcher, row, dry_run=dry_run):
                    proposals.append({'id': row['id'], 'state': 'exited' if not dry_run else 'probe_required'})
                continue
            diagnostic = row.get('diagnostic', {})
            if diagnostic.get('stage') != 'dispatch_headless' or not diagnostic.get('reason', '').startswith('launcher_'):
                continue
            workspace = resolve_workspace_path(dispatcher.settings, row['workspace'])
            cwd = resolve_offer_cwd(workspace, row.get('cwd'), dispatcher.settings)
            task_id = 'hub-' + row['id']
            proposals.append({'id': row['id'], 'state': 'probe_required'})
            if dry_run:
                if reconcile_launcher_retired(dispatcher, row, cwd, dry_run=True):
                    proposals[-1]['state'] = 'retired_verified'
                continue
            if not settle_unstarted(dispatcher.settings.get('launcher', {}), task_id, workspace, cwd, diagnostic):
                if reconcile_launcher_retired(dispatcher, row, cwd):
                    proposals[-1]['state'] = 'failed'
                continue
            ack = dispatcher.request('POST', '/api/fleet/launches/' + row['id'] + '/result', {
                'state': 'failed', 'confirmed_exit': True, 'launcher_task_id': task_id,
                'reason': diagnostic['reason'], 'diagnostic': diagnostic})
            if ack.get('state') == 'failed':
                proposals[-1]['state'] = 'failed'
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return proposals


def reconcile_launcher_retired(dispatcher, row, cwd, *, dry_run=False):
    """A first-action failure can have spawned: recover its real native receipt."""
    from .native_sessions import NativeTarget
    from .retired_native import verify_native_retirement
    launcher = dispatcher.settings.get('launcher', {})
    try:
        task_id = 'hub-' + row['id']
        prefix = launcher['argv'] + ['--config-dir', launcher['config_dir']]
        status = dispatcher.runner(prefix + ['status', '--id', task_id], cwd=launcher['root'])
        binding = status.get('native_binding') or {}
        payload = status.get('payload') or {}
        if (status.get('state') != 'failed' or payload.get('owner') != 'hub-fleet'
                or binding.get('task_id') != task_id or binding.get('tag') != 'task-' + task_id
                or binding.get('workspace') != str(cwd) or binding.get('engine') != row.get('engine')):
            return False
        target = NativeTarget(session_id=binding['native_id'], tag=binding['tag'], workspace=binding['workspace'],
                              created_at_ms=binding['incarnation'], engine=binding['engine']).validated()
        retired, _, _ = verify_native_retirement(dispatcher.settings.get('aplexer_bin', 'aplexer'), target,
                                                state_dir=dispatcher.settings.get('aplexer_state_dir'))
        if not retired:
            return False
        if dry_run:
            return True
        released = dispatcher.runner(prefix + ['reconcile', '--id', task_id, '--json'], cwd=launcher['root'])
        if released.get('native_id') != target.session_id or released.get('resources_reserved') is not False or released.get('state') != 'failed':
            return False
        ack = dispatcher.request('POST', '/api/fleet/launches/' + row['id'] + '/result', {
            'state': 'failed', 'confirmed_exit': True, 'native_id': target.session_id, 'launcher_task_id': task_id})
        return ack.get('state') == 'failed'
    except Exception:
        return False


def reconcile_retired(dispatcher, row, *, dry_run=False):
    """Repair a lost launch journal from the exact server binding and native tombstone."""
    from .native_sessions import NativeTarget
    from .retired_native import verify_native_retirement
    binding = row.get('binding') or {}
    if binding.get('native_id') != row['native_id'] or not binding.get('native_created_at_ms'):
        return False
    try:
        target = NativeTarget(session_id=row['native_id'], tag=binding['tag'], workspace=binding['workspace'],
                              created_at_ms=binding['native_created_at_ms'], engine=row['engine']).validated()
        retired, _, _ = verify_native_retirement(dispatcher.settings.get('aplexer_bin', 'aplexer'), target,
                                                state_dir=dispatcher.settings.get('aplexer_state_dir'))
        if not retired:
            return False
        if dry_run:
            return True
        result = {'state': 'exited', 'confirmed_exit': True, 'native_id': row['native_id']}
        if row.get('launcher_task_id'):
            result['launcher_task_id'] = row['launcher_task_id']
        ack = dispatcher.request('POST', '/api/fleet/launches/' + row['id'] + '/result', result)
        return ack.get('state') == 'exited'
    except Exception:
        # Missing/invalid tombstones, unreachable catalog and reused tags all
        # retain capacity. Never turn an unknown native effect into exit proof.
        return False


def activity_ms(session):
    """Harness event time is progress evidence; catalog observation is not.

    Older native clients only expose workload-output time. Never use record
    updated_at or the host's current clock as an activity fallback.
    """
    event = session.get('reported_state_event_ms')
    if type(event) is int and event > 0:
        return event
    return session.get('last_activity_ms')


def confirmed_empty(session):
    """Only a native containment receipt plus absent recorded processes proves exit."""
    import os
    if session.get('containment_empty') is not True:
        return False
    pids = [session.get('workload_pid'), session.get('worker_pid')]
    if not any(type(pid) is int and pid > 0 for pid in pids):
        return False
    for pid in pids:
        if type(pid) is not int or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            continue
        except OSError:
            return False
    return True
