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
    from .fleet import resolve_workspace_path, resolve_offer_cwd
    launcher = dispatcher.settings.get('launcher', {})
    try:
        task_id = 'hub-' + row['id']
        prefix = launcher['argv'] + ['--config-dir', launcher['config_dir']]
        status = dispatcher.runner(prefix + ['status', '--id', task_id], cwd=launcher['root'])
        binding = status.get('native_binding') or {}
        payload = status.get('payload') or {}
        native_workspace = str(cwd)
        # The native mailbox stays at the workspace root; the engine's
        # working directory is independently carried in the launcher payload.
        if row.get('mode') == 'headless':
            workspace = resolve_workspace_path(dispatcher.settings, row.get('workspace', binding.get('workspace')))
            expected_cwd = resolve_offer_cwd(workspace, row.get('cwd'), dispatcher.settings)
            if str(cwd) != str(expected_cwd) or payload.get('cwd') != str(expected_cwd):
                return False
            native_workspace = str(workspace)
        if (status.get('state') != 'failed' or payload.get('owner') != 'hub-fleet'
                or binding.get('task_id') != task_id or binding.get('tag') != 'task-' + task_id
                or binding.get('workspace') not in (native_workspace, str(cwd)) or binding.get('engine') != row.get('engine')):
            return False
        target = NativeTarget(session_id=binding['native_id'], tag=binding['tag'], workspace=binding['workspace'],
                              created_at_ms=binding['incarnation'], engine=binding['engine']).validated()
        retired, _, _ = verify_native_retirement(dispatcher.settings.get('aplexer_bin', 'aplexer'), target,
                                                state_dir=dispatcher.settings.get('aplexer_state_dir'))
        if not retired and row.get('mode') == 'headless':
            from .terminal_reports import retained_terminal_exit
            retired, _, _ = retained_terminal_exit(dispatcher, target)
        if not retired:
            return False
        if dry_run:
            return True
        if row.get('mode') == 'headless':
            return reconcile_headless_retired(dispatcher, row, target)
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
        if not retired and row.get('mode') == 'headless':
            from .terminal_reports import retained_terminal_exit
            retired, _, _ = retained_terminal_exit(dispatcher, target)
        if not retired:
            return False
        if dry_run:
            return True
        if row.get('mode') == 'headless':
            return reconcile_headless_retired(dispatcher, row, target)
        result = {'state': 'exited', 'confirmed_exit': True, 'native_id': row['native_id']}
        if row.get('launcher_task_id'):
            result['launcher_task_id'] = row['launcher_task_id']
        ack = dispatcher.request('POST', '/api/fleet/launches/' + row['id'] + '/result', result)
        return ack.get('state') == 'exited'
    except Exception:
        # Missing/invalid tombstones, unreachable catalog and reused tags all
        # retain capacity. Never turn an unknown native effect into exit proof.
        return False


def reconcile_headless_retired(dispatcher, row, target):
    """Reuse the complete terminal contract instead of racing it with a bare exit.

    Capacity recovery also sees ordinary owned headless launches. The native
    tombstone alone must not bypass launcher release, worker report forwarding,
    runtime evidence or the durable acknowledgement/late-file journal.
    """
    import re
    from pathlib import Path
    from .fleet import resolve_workspace_path, resolve_offer_cwd
    try:
        workspace = resolve_workspace_path(dispatcher.settings, row.get('workspace', target.workspace))
        target_cwd = resolve_offer_cwd(workspace, row.get('cwd'), dispatcher.settings)
    except (ValueError, TypeError, OSError):
        return False
    # The launcher binds the native session to the engine cwd for `--cwd` launches.
    if target.workspace not in (str(workspace), str(target_cwd)):
        return False
    offer_id = row.get('id')
    if not isinstance(offer_id, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', offer_id):
        return False
    task_id = 'hub-' + offer_id
    if row.get('launcher_task_id') not in (None, task_id) or target.tag != 'task-' + task_id:
        return False
    records = getattr(dispatcher, '_active_launches', None)
    if not isinstance(records, dict):
        return False
    exact = dict(offer_id=offer_id, task_id=task_id, native_id=target.session_id,
                 tag=target.tag, workspace=str(workspace), incarnation=target.created_at_ms,
                 engine=target.engine, mode='headless')
    result_path = str(Path(workspace) / '.local/hub-results' / (offer_id + '.json'))
    record = records.get(offer_id)
    if record is None:
        record = {**exact, 'state': 'running', 'settled': False,
                  'target_cwd': str(target_cwd), 'result_path': result_path}
        records[offer_id] = record
    elif not isinstance(record, dict):
        return False
    elif record.get('state') == 'not_started':
        # A failed first-action handshake can have spawned. The real dispatch
        # journal deliberately has no native tuple until independent launcher
        # binding + retirement verification above supplies one. Do not erase
        # another launch, an established conflicting tuple, or a settled fence.
        native_fields = {'native_id', 'tag', 'incarnation', 'engine'}
        if (record.get('settled') is not False
                or not isinstance(record.get('reason'), str)
                or not record['reason'].startswith('launcher_')
                or any(record.get(k) != v for k, v in exact.items() if k not in native_fields)
                or any(record.get(k) not in (None, '', exact[k]) for k in native_fields)
                or record.get('target_cwd', str(target_cwd)) != str(target_cwd)):
            return False
        if isinstance(record.get('diagnostic'), dict):
            record['dispatch_diagnostic'] = dict(record['diagnostic'])
        record.update(exact, state='running', settled=False)
    elif any(record.get(k) != v for k, v in exact.items()):
        return False
    record['result_path'] = result_path
    dispatcher._persist_launch_record(record)
    results = dispatcher.reconcile_launches(offer_ids={offer_id}, terminal_only=True)
    return any(item.get('id') == offer_id and item.get('reconciled') is True for item in results)


def activity_ms(session):
    """Harness event time is progress evidence; catalog observation is not.

    Older native clients only expose workload-output time. Never use record
    updated_at or the host's current clock as an activity fallback.
    """
    event = session.get('reported_state_event_ms')
    if type(event) is int and event > 0:
        return event
    return session.get('last_activity_ms')


CGROUP_ROOT = '/sys/fs/cgroup/'
BOOT_ID_PATH = '/proc/sys/kernel/random/boot_id'


def kernel_containment_empty(session, *, cgroup_root=CGROUP_ROOT, boot_id_path=BOOT_ID_PATH):
    """Kernel receipt for a row whose native worker died (intake c206f556).

    A dead worker can never refresh ``containment_empty``; its last value
    stays False forever and Hub keeps the row running. Only the exact
    same-boot workload scope reporting ``populated 0`` (or removed by systemd
    on this boot) is accepted. A live worker's own report stays authoritative.
    """
    import os
    if session.get('worker_alive') is not False:
        return False
    path, identity = session.get('containment_cgroup'), session.get('containment_cgroup_identity')
    if (not isinstance(path, str) or not path.startswith(cgroup_root) or '/../' in path or path.endswith('/..')
            or not os.path.basename(path).startswith('aplexer-workload-') or not isinstance(identity, dict)):
        return False
    try:
        with open(boot_id_path, encoding='ascii') as handle:
            boot = handle.read().strip()
    except OSError:
        return False
    if not boot or identity.get('boot_id') != boot:
        return False
    try:
        with open(os.path.join(path, 'cgroup.events'), encoding='ascii') as handle:
            return 'populated 0' in handle.read().splitlines()
    except FileNotFoundError:
        # systemd garbage-collects an empty transient scope on this boot.
        return not os.path.exists(path)
    except OSError:
        return False


def confirmed_empty(session):
    """Only a native containment receipt plus absent recorded processes proves exit."""
    import os
    if session.get('containment_empty') is not True and not kernel_containment_empty(session):
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
