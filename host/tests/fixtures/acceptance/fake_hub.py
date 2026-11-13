#!/usr/bin/env python3
"""Synthetic modern CLI contracts for Hub intake, sessions, native `a`, `git`, and alerts.

Parses real maintained CLI arguments (`intake.py`, `session_control.py`).
Never invokes real Hub, native tools, or services.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads(Path(os.environ['FAKE_CFG']).read_text())
STATE_PATH = Path(os.environ['FAKE_CFG'] + '.state')
ST = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {
    'n': 0, 'sessions': {}, 'keys': {}, 'intakes': {}, 'intake_keys': {}, 'log': [],
}


def save():
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=STATE_PATH.parent,
                                         prefix=STATE_PATH.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(ST, stream)
        os.replace(temporary, STATE_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def out(obj):
    save()
    print(json.dumps(obj))
    sys.exit(0)


def fail(msg):
    save()
    print(msg, file=sys.stderr)
    sys.exit(1)


def value(argv, flag):
    return argv[argv.index(flag) + 1]


def canonical_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def choice(p):
    return {k: p[k] for k in ('policy_id', 'policy_version', 'machine_id', 'attempt', 'engine', 'model', 'effort')}


def _attempt_of(s, show=False):
    return 1 if (show and CFG.get('fallback')) else 0


def _attempt_op(s, attempt):
    return '%s:%d' % (s['base_op'], attempt)


def project_dispatch(s, state='running'):
    """Actual hub2 project-do.ts sagaView: operation_id = BASE, attempt_operation_id = <base>:<attempt>, int attempt."""
    attempt = 1 if CFG.get('fallback') else 0
    return {
        'id': s['id'], 'operation_id': s['base_op'], 'attempt_operation_id': _attempt_op(s, attempt),
        'intake_id': s['intake_id'], 'machine_id': s['host'], 'epoch': s['epoch'], 'state': state,
        'native_id': s['native_id'], 'attempt': attempt, 'next_action_at': None,
    }


def exit_evidence(s, intake_id):
    """Project-shaped native exit evidence nested in stopped Machine views and final_evidence.finish_after_stop."""
    attempt = 1 if CFG.get('fallback') else 0
    return {
        'operation_id': s['base_op'], 'attempt_operation_id': _attempt_op(s, attempt), 'session_id': s['id'],
        'intake_id': intake_id, 'machine_id': s['host'], 'epoch': s['epoch'], 'native_id': s['native_id'],
        'state': 'stopped', 'exit_code': 0, 'native_exit_confirmed': not CFG.get('final_no_native_exit'),
        'evidence': {'containment_empty': not CFG.get('final_containment_not_empty'), 'source': 'aplexer_retirement'},
        'evidence_digest': 'e' * 64,
    }


def final_stop_evidence(s, intake_id):
    """Normal operator STOP: intake failed / session_stop_confirmed with full final stop proof."""
    attempt = 1 if CFG.get('fallback') else 0
    fe = {
        'stopped': True, 'reaped': False, 'operation_id': s['base_op'], 'attempt_operation_id': _attempt_op(s, attempt),
        'session_id': s['id'], 'machine_id': s['host'], 'epoch': s['epoch'], 'reason': 'stop_confirmed',
        'native_id': s['native_id'], 'observed_at': 4000,
        'completion': {'controlled': True, 'session_id': s['id'], 'outcome': 'unfinished',
                       'reason': 'session_stop_confirmed', 'depth': 0},
        'finish_after_stop': exit_evidence(s, intake_id),
    }
    if CFG.get('final_attempt_contradiction'):
        fe['finish_after_stop']['attempt_operation_id'] = _attempt_op(s, attempt + 1)
    if CFG.get('final_reason_not_stop'):
        fe['completion']['reason'] = 'session_finished'
    return fe


def row_for(s, show=False):
    row = {k: s[k] for k in (
        'id', 'native_id', 'engine', 'model', 'effort', 'workspace', 'requested_workspace',
        'policy_id', 'policy_version', 'intake_id', 'project_id', 'epoch', 'mode', 'interactive',
    )}
    stopped = s['stopped']
    row.update(
        machine_id=s['host'],
        state='stopped' if stopped else 'running',
        # Actual hub2 machine-do.ts sessionView: operation_id IS the attempt id; no attempt_operation_id/attempt.
        operation_id=_attempt_op(s, _attempt_of(s, show)),
        picker=copy.deepcopy(s['picker']),
        evidence=exit_evidence(s, s['intake_id']) if stopped else None,
    )
    if CFG.get('show_redundant_alias'):
        row['attempt_operation_id'] = row['operation_id']
    if CFG.get('show_alias_contradiction'):
        row['attempt_operation_id'] = s['base_op']
    if CFG.get('show_attempt_contradiction'):
        row['attempt'] = 7
    if CFG.get('show_malformed_operation'):
        row['operation_id'] = 'opaque-' + s['id']
    if stopped and CFG.get('stop_evidence_contradiction'):
        row['evidence']['attempt_operation_id'] = s['base_op'] + ':5'
    if stopped and not CFG.get('stop_not_confirmed'):
        row['stop_confirmed'] = True
    if show and CFG.get('fallback'):
        old = choice(row['picker'])
        pool = row['picker']['pool']
        index = next(i for i, x in enumerate(pool) if x['model'] == row['model'])
        chosen = pool[(index + 1) % len(pool)]
        row.update({k: chosen[k] for k in ('policy_id', 'model', 'engine', 'effort')})
        row['picker'].update(choice(dict(row['picker'], **chosen, attempt=1)))
        row['picker']['history'] = [old, choice(row['picker'])]
        if CFG.get('cross_attempt_pool'):
            row['picker']['pool'] = [dict(x, share=1) for x in s['picker']['pool'] if x['policy_id'] == old['policy_id']]
    if show and CFG.get('show_pool_changed'):
        for option in row['picker']['pool']:
            option['weight'] *= 2
    if CFG.get('missing_picker') or show and CFG.get('show_missing_picker'):
        row.pop('picker', None)
    if CFG.get('picker_model_mismatch') and 'picker' in row:
        row['picker']['model'] = 'wrong-model'
    if CFG.get('missing_version') and 'picker' in row:
        row['picker'].pop('policy_version', None)
    return row


def _write_worker_artifact(s, intake_row):
    """Simulate a real worker executing the brief and writing canonical result.json."""
    if CFG.get('no_artifact'):
        return
    brief = intake_row['text']
    m_run = re.search(r'run ([0-9a-f-]{36}), sample (\d+)/\d+, parent ([0-9a-f-]{36})', brief)
    m_ch = re.search(r'challenge file at `([^`]+)`', brief)
    m_rel = re.search(r'only to `([^`]+)` inside', brief)
    if not (m_run and m_ch and m_rel):
        return
    run_id, sample, parent_intake_id = m_run.group(1), int(m_run.group(2)), m_run.group(3)
    challenge_path = Path(m_ch.group(1))
    if not challenge_path.exists():
        return
    challenge32 = challenge_path.read_bytes()
    ws = Path(s['workspace'])
    ws.mkdir(parents=True, exist_ok=True)
    result_path = ws / m_rel.group(1)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    m_helper = re.search(r'Root-pinned helper `([^`]+)` to `([^`]+)`', brief)
    if m_helper:
        # The synthetic worker copies the Root-pinned helper bytes (it never executes them).
        s['probe_command'] = 'python3 ' + m_helper.group(2)
        if not CFG.get('no_probe_file'):
            probe = ws / m_helper.group(2)
            probe.parent.mkdir(parents=True, exist_ok=True)
            data = Path(m_helper.group(1)).read_bytes()
            if CFG.get('probe_tampered'):
                data += b'# locally edited copy\n'
            probe.write_bytes(data)
    if CFG.get('result_preexisting'):
        return
    head = CFG.get('worker_head', CFG.get('git_head', 'b' * 40))
    status_raw = CFG.get('git_status_raw', '').encode('utf-8')
    status_sha = hashlib.sha256(status_raw).hexdigest()
    entries = 0 if not status_raw else len(status_raw[:-1].split(b'\x00'))
    work = {
        'schema': 'phase7_native_work_v2',
        'run_id': run_id,
        'sample': sample,
        'parent_intake_id': parent_intake_id,
        'child_intake_id': intake_row['id'],
        'session_id': s['id'],
        'native_id': s['native_id'],
        'project': 'alexeygrigorev/agent-hub',
        'machine_id': 'hetzner-aplexer',
        'workspace': s['workspace'],
        'head': head,
        'tracked_status_sha256': status_sha,
        'tracked_status_entries': entries,
        'calculation': CFG.get('worker_calc', 5050),
    }
    digest = hashlib.sha256(b'HUB_PHASE7_NATIVE_WORK_V2\x00' + challenge32 + b'\x00' + canonical_bytes(work)).hexdigest()
    if CFG.get('result_bad_digest'):
        digest = '0' * 64
    result = dict(work, proof_digest=digest)
    s['proof_digest'] = digest
    s['proof_head'] = head
    s['run_id'] = run_id
    s['sample'] = sample
    if CFG.get('result_symlink'):
        target = result_path.parent / 'real.json'
        target.write_bytes(canonical_bytes(result) + b'\n')
        result_path.symlink_to(target)
    elif CFG.get('result_hardlink'):
        target = result_path.parent / 'hard.json'
        target.write_bytes(canonical_bytes(result) + b'\n')
        os.link(target, result_path)
    elif CFG.get('result_duplicate_key'):
        result_path.write_text('{"schema":"phase7_native_work_v2","schema":"phase7_native_work_v2"}\n')
    elif CFG.get('result_non_canonical'):
        result_path.write_bytes(json.dumps(result, indent=2).encode('utf-8') + b'\n')
    else:
        result_path.write_bytes(canonical_bytes(result) + b'\n')


def _handle_intake(argv):
    from agent_hub_host import intake as intake_cli
    parser = argparse.ArgumentParser()
    intake_cli.add_commands(parser.add_subparsers(dest='cmd'))
    args = parser.parse_args(['intake'] + argv)
    sub = args.intake_cmd
    ST['log'].append('intake:' + sub)

    if sub == 'create':
        key = args.key
        if key in ST['intake_keys']:
            if ST.get('intake_replay_count', {}).get(key, 0) < CFG.get('create_replay_fail_count', 0):
                ST.setdefault('intake_replay_count', {})[key] = ST.get('intake_replay_count', {}).get(key, 0) + 1
                fail('upstream 502 on intake create replay')
            item = ST['intakes'][ST['intake_keys'][key]]
            out({'intake': item, 'task': {'id': item['id'], 'updated_at': item['updated_at'], 'control': {'version': item['updated_at']}}})
        idx = len(ST['intakes']) + 1
        cid = str(uuid.UUID(int=9000 + (1 if CFG.get('reuse_intake_id') else idx)))
        text = Path(args.text_file).read_text(encoding='utf-8') if args.text_file else args.text
        ac = Path(args.acceptance_criteria_file).read_text(encoding='utf-8') if args.acceptance_criteria_file else args.acceptance_criteria
        ac_list = [
            line for line in (
                re.sub(r'^\s*(?:[-*]|\d+[.)])\s+', '', raw).strip()
                for raw in (ac or '').splitlines()
            ) if line
        ] if ac is not None else []
        item = {
            'id': cid,
            'project': args.project,
            'title': args.title,
            'text': text,
            'acceptance_criteria': ac_list,
            'status': 'open',
            'assigned_machine_id': None,
            'session_id': None,
            # Actual hub2 viewIntake (project-do.ts): `dispatch` is omitted until a saga exists; create ignores
            # body.source and persists provenance={type:user,code_task}; original_ask mirrors title/text.
            'updated_at': 1000 + idx,
            'original_ask': {'title': args.title.strip(), 'text': text},
            'provenance': ({'type': 'forwarded', 'reference': args.source_reference}
                           if CFG.get('legacy_forwarded_provenance') else {'type': 'user', 'code_task': True}),
            'parent_intake_id': None,
            'events': [{'seq': 1, 'actor_id': 'operator', 'kind': 'created', 'at': 1000, 'body': {'title': args.title}}],
        }
        ST['intakes'][cid] = item
        ST['intake_keys'][key] = cid
        if CFG.get('create_fail_after_accept_at') == idx - 1:
            fail('upstream 502 after create')
        out({'intake': item, 'task': {'id': cid, 'updated_at': item['updated_at'], 'control': {'version': item['updated_at']}}})

    if sub == 'show':
        item = copy.deepcopy(ST['intakes'][args.id])
        if CFG.get('history_incomplete_archive'):
            item['history_archive'] = {'count': 3, 'latest_sequence': 3}
        if CFG.get('history_snapshot_changed'):
            ST['intakes'][args.id]['updated_at'] += 1
        out({'intake': item, 'task': {'id': item['id'], 'updated_at': item['updated_at'], 'control': {'version': item['updated_at']}}})

    if sub == 'append':
        iid = args.id
        if iid in ST['intakes']:
            item = ST['intakes'][iid]
            seq = item['events'][-1]['seq'] + 1
            item['events'].append({'seq': seq, 'actor_id': 'operator', 'kind': 'note', 'at': 2000 + seq, 'body': {'note': args.note}})
            item['updated_at'] += 1
        out({'ok': True, 'id': iid, 'key': args.key})

    if sub == 'history':
        item = ST['intakes'][args.id]
        after = args.after or 0
        limit = args.limit or 64
        evs = [e for e in item['events'] if e['seq'] > after]
        page = evs[:limit]
        nxt = page[-1]['seq'] if len(evs) > limit else None
        out({'events': page, 'next': nxt})

    if sub == 'update':
        item = ST['intakes'][args.id]
        if CFG.get('archive_cas_conflict') or (args.version is not None and args.version != item['updated_at']):
            fail('conflict: expected_updated_at mismatch')
        if CFG.get('archive_unproven'):
            out({'intake': dict(item, status='open')})
        item['status'] = args.status or item['status']
        item['updated_at'] += 1
        if CFG.get('archive_lost_ack'):
            fail('upstream 502 after archive applied')
        out({'intake': item})

    fail('unhandled intake command: ' + sub)


def _make_descendant_session(intake_row, desc_sid):
    return {
        'id': desc_sid,
        'native_id': str(uuid.UUID(int=5000 + len(ST['sessions']) + 1)),
        'engine': 'claude',
        'model': 'claude-sonnet-4-6',
        'effort': 'medium',
        'workspace': str(Path(CFG.get('child_workspace_root', '/tmp/fake-ws')) / desc_sid),
        'requested_workspace': '/home/user/work/acceptance',
        'host': 'hetzner-aplexer',
        'intake_id': intake_row['id'],
        'project_id': 'alexeygrigorev/agent-hub',
        'epoch': 1,
        'mode': 'interactive',
        'interactive': True,
        'stopped': False,
        'picker': {
            'policy_id': 'p-claude', 'policy_version': 7, 'machine_id': 'hetzner-aplexer',
            'attempt': 0, 'engine': 'claude', 'model': 'claude-sonnet-4-6', 'effort': 'medium',
            'pool': [], 'history': [],
        },
        'policy_id': 'p-claude',
        'policy_version': 7,
        'base_op': str(uuid.UUID(int=0xC0000000 + len(ST['sessions']) + 1)),
    }


def _handle_sessions(argv):
    from agent_hub_host import session_control
    parser = argparse.ArgumentParser()
    session_control.add_commands(parser.add_subparsers(dest='cmd'))
    args = parser.parse_args(['sessions'] + argv)
    sub = args.sessions_cmd
    ST['log'].append('sessions:' + sub)

    if sub == 'pick':
        fail('independent pick must never be used for session acceptance')

    if sub == 'start':
        for banned in ('--engine', '--model', '--effort', '--tier', '--override-reason', '--reason',
                       '--prompt', '--brief-file', '--cwd', '--headless', '--with-goal'):
            assert banned not in argv, banned
        assert '--interactive' in argv
        assert value(argv, '--machine') == 'hetzner-aplexer'
        req_ws = value(argv, '--workspace')
        assert req_ws.startswith('/')
        assert '--intake-task-id' in argv
        key = value(argv, '--key')
        child_id = value(argv, '--intake-task-id')
        ST.setdefault('start_requests', []).append(list(argv))
        if key in ST.get('refused_keys', []):
            fail('Session HTTP request failed with status 400 (invalid_request)')
        if key in ST['keys']:
            assert '--no-wait' in argv
            count = ST.setdefault('replays', {}).get(key, 0) + 1
            ST['replays'][key] = count
            if count <= CFG.get('replay_failure_count', 0):
                fail('upstream returned 502 Bad Gateway')
            row = row_for(ST['sessions'][ST['keys'][key]])
            if CFG.get('bad_recovered_id'):
                row['id'] = '../../invalid-session'
            out(row)
        i = ST['n']
        ST['n'] += 1
        if CFG.get('fail_start_at') == i:
            ST.setdefault('refused_keys', []).append(key)
            fail('Session HTTP request failed with status 400 (invalid_request)')
        models = CFG['models']
        model = models[i % len(models)]
        pool = copy.deepcopy(CFG.get('pool', json.loads((HERE / 'hub_sessions_pick.json').read_text())['pool']))
        chosen = next((x for x in pool if x['model'] == model), dict(policy_id='unknown', model=model, engine='claude', effort='medium'))
        sid = str(uuid.UUID(int=1 if CFG.get('reuse_session_id') else i + 1))
        picker = dict(chosen, policy_version=7, machine_id=CFG.get('host', 'hetzner-aplexer'), attempt=0, pool=pool)
        picker.pop('weight', None)
        picker.pop('share', None)
        picker['history'] = [choice(picker)]
        child_ws = CFG.get('workspace') or str(Path(CFG.get('child_workspace_root', req_ws)) / ('child-' + str(i + 1)))
        native_id = None if CFG.get('null_native_stop') else (
            CFG['native_id'] if 'native_id' in CFG else str(uuid.UUID(int=1001 if CFG.get('reuse_native_id') else i + 1001))
        )
        s = dict(
            id=sid,
            native_id=native_id,
            engine=chosen['engine'],
            model=model,
            effort=chosen['effort'],
            workspace=child_ws,
            requested_workspace=req_ws,
            host=picker['machine_id'],
            intake_id=child_id,
            project_id='alexeygrigorev/agent-hub',
            epoch=1,
            mode='headless' if CFG.get('force_headless_mode') else 'interactive',
            interactive=not CFG.get('force_headless_mode'),
            stopped=False,
            picker=picker,
            policy_id=chosen['policy_id'],
            policy_version=7,
            base_op=str(uuid.UUID(int=0xB0000000 + i + 1)),
        )
        ST['sessions'][sid] = s
        ST['keys'][key] = sid
        if child_id in ST['intakes']:
            ST['intakes'][child_id]['session_id'] = sid
            ST['intakes'][child_id]['assigned_machine_id'] = picker['machine_id']
            ST['intakes'][child_id]['dispatch'] = project_dispatch(s)
            ST['intakes'][child_id]['updated_at'] += 1
        if native_id and child_id in ST['intakes']:
            _write_worker_artifact(s, ST['intakes'][child_id])
        if CFG.get('leak_at') == i:
            fail('session_start_failed; session %s reached starting' % sid if CFG.get('leak_with_id', True) else 'Session wait timed out')
        if CFG.get('hang_start_at') == i:
            save()
            time.sleep(30)
        if CFG.get('accepted_nonjson') or CFG.get('accepted_nonjson_at') == i:
            save()
            print('upstream sent malformed success')
            sys.exit(0)
        if CFG.get('accepted_502'):
            fail('upstream returned 502 Bad Gateway')
        # Start response top level is the Project saga view (base + attempt alias); binding_wait is Machine-shaped.
        row = dict(row_for(s), operation_id=s['base_op'], attempt_operation_id=_attempt_op(s, 0), attempt=0)
        row.pop('evidence', None)
        if CFG.get('start_response_base_drift'):
            row['operation_id'] = str(uuid.UUID(int=0xDEAD0000 + i))
            row['attempt_operation_id'] = row['operation_id'] + ':0'
        if CFG.get('initial_missing_id'):
            row.pop('id', None)
        if CFG.get('initial_bad_id'):
            row['id'] = '../../invalid-session'
        if CFG.get('initial_offered'):
            row['state'] = 'offered'
        out(dict(row, client_key=key, binding_wait={'session': row_for(s)}))

    if sub == 'show':
        s = ST['sessions'][args.id]
        row = row_for(s, show=True)
        if CFG.get('wrong_show_id'):
            row['id'] = str(uuid.UUID(int=999999))
        if CFG.get('natural_exit_after_proof') and ST['log'].count('sessions:list') >= 1 and not s['stopped']:
            row['state'] = 'exited'
        out(row if CFG.get('flat_show') else {'session': row})

    if sub == 'list':
        rows = []
        for s in ST['sessions'].values():
            if s['stopped']:
                continue
            row = row_for(s, show=True)
            if CFG.get('list_no_host'):
                row['machine_id'] = None
                row.pop('host', None)
            if CFG.get('list_no_workspace'):
                row.pop('workspace', None)
            rows.append(row)
        out({'sessions': rows, 'complete': True})

    if sub == 'stop':
        s = ST['sessions'][args.id]
        s['stopped'] = True
        ST['log'].append('stop:' + s['id'])
        child_id = s.get('intake_id')
        if child_id in ST['intakes']:
            intake_row = ST['intakes'][child_id]
            if isinstance(intake_row.get('dispatch'), dict) and intake_row['dispatch'].get('id') == s['id']:
                # Normal operator STOP: saga complete -> view 'failed'; intake failed / session_stop_confirmed.
                intake_row['dispatch']['state'] = 'failed'
                intake_row['status'] = 'failed'
                if not CFG.get('final_missing'):
                    intake_row['final_evidence'] = final_stop_evidence(s, child_id)
                if CFG.get('dispatch_alias_base_mismatch'):
                    intake_row['dispatch']['attempt_operation_id'] = str(uuid.UUID(int=0xFEED)) + ':0'
                if CFG.get('dispatch_attempt_int_contradiction'):
                    intake_row['dispatch']['attempt'] = 1 - intake_row['dispatch']['attempt']
                if CFG.get('dispatch_machine_attempt_as_base'):
                    intake_row['dispatch']['operation_id'] = intake_row['dispatch']['attempt_operation_id']
                if CFG.get('dispatch_missing_alias'):
                    intake_row['dispatch'].pop('attempt_operation_id', None)
                intake_row['updated_at'] += 1
            if CFG.get('fresh_successor_after_first_scan') and intake_row.get('_injected_count', 0) < 2:
                intake_row['_injected_count'] = intake_row.get('_injected_count', 0) + 1
                dsid = str(uuid.UUID(int=7700 + len(ST['sessions'])))
                ST['sessions'][dsid] = _make_descendant_session(intake_row, dsid)
                seq = intake_row['events'][-1]['seq'] + 1
                intake_row['events'].append({
                    'seq': seq, 'actor_id': 'hub2-completion', 'kind': 'task_continuation_started',
                    'at': 3000 + seq, 'body': {'previous_session_id': s['id'], 'session_id': dsid, 'depth': intake_row['_injected_count'], 'reason': 'late'},
                })
                intake_row['session_id'] = dsid
                intake_row['dispatch'] = project_dispatch(ST['sessions'][dsid])
                intake_row['updated_at'] += 1
            elif not intake_row.get('_lineage_injected'):
                intake_row['_lineage_injected'] = True
                if CFG.get('continuation_descendant'):
                    dsid = str(uuid.UUID(int=7100 + len(ST['sessions'])))
                    ST['sessions'][dsid] = _make_descendant_session(intake_row, dsid)
                    seq = intake_row['events'][-1]['seq'] + 1
                    intake_row['events'].append({
                        'seq': seq, 'actor_id': 'hub2-completion', 'kind': 'task_continuation_started',
                        'at': 3000, 'body': {'previous_session_id': s['id'], 'session_id': dsid, 'depth': 1, 'reason': 'exit'},
                    })
                    intake_row['session_id'] = dsid
                    intake_row['dispatch'] = project_dispatch(ST['sessions'][dsid])
                    intake_row['updated_at'] += 1
                elif CFG.get('verification_descendant'):
                    dsid = str(uuid.UUID(int=7200 + len(ST['sessions'])))
                    ST['sessions'][dsid] = _make_descendant_session(intake_row, dsid)
                    seq = intake_row['events'][-1]['seq'] + 1
                    intake_row['events'].append({
                        'seq': seq, 'actor_id': 'hub2-verification', 'kind': 'task_verification_started',
                        'at': 3000, 'body': {'original_session_id': s['id'], 'previous_session_id': s['id'], 'session_id': dsid, 'attempt': 1},
                    })
                    intake_row['session_id'] = dsid
                    intake_row['dispatch'] = project_dispatch(ST['sessions'][dsid])
                    intake_row['updated_at'] += 1
                elif CFG.get('foreign_assignment'):
                    intake_row['session_id'] = str(uuid.UUID(int=888888))
                    intake_row['updated_at'] += 1
                elif CFG.get('foreign_assigned_machine'):
                    intake_row['assigned_machine_id'] = 'foreign-host'
                    intake_row['updated_at'] += 1
                elif CFG.get('cleanup_missing_dispatch'):
                    intake_row.pop('dispatch', None)
                    intake_row['updated_at'] += 1
                elif CFG.get('cleanup_null_machine'):
                    intake_row['assigned_machine_id'] = None
                    intake_row['updated_at'] += 1
                elif CFG.get('cleanup_dispatch_missing_op'):
                    intake_row['dispatch'].pop('operation_id', None)
                    intake_row['updated_at'] += 1
                elif CFG.get('cleanup_dispatch_null_machine'):
                    intake_row['dispatch']['machine_id'] = None
                    intake_row['updated_at'] += 1
                elif CFG.get('cleanup_epoch_drift'):
                    intake_row['dispatch']['epoch'] = 2
                    intake_row['updated_at'] += 1
                elif CFG.get('cleanup_native_drift'):
                    intake_row['dispatch']['native_id'] = str(uuid.UUID(int=424242))
                    intake_row['updated_at'] += 1
                elif CFG.get('cleanup_stop_view_op_drift'):
                    s['base_op'] = str(uuid.UUID(int=0xD0000000))
                elif CFG.get('pending_dispatch_state'):
                    intake_row['dispatch'] = {
                        'id': s['id'], 'state': 'offered', 'operation_id': 'unproven-other-operation',
                        'epoch': 99, 'machine_id': 'hetzner-aplexer',
                    }
                    intake_row['updated_at'] += 1
                elif CFG.get('history_seq_gap'):
                    seq = intake_row['events'][-1]['seq'] + 2
                    intake_row['events'].append({
                        'seq': seq, 'actor_id': 'operator', 'kind': 'note', 'at': 3000, 'body': {'note': 'gap'},
                    })
                    intake_row['updated_at'] += 1
                elif CFG.get('descendant_over_limit'):
                    prev = s['id']
                    for k in range(9):
                        dsid = str(uuid.UUID(int=7500 + k))
                        ST['sessions'][dsid] = _make_descendant_session(intake_row, dsid)
                        seq = intake_row['events'][-1]['seq'] + 1
                        intake_row['events'].append({
                            'seq': seq, 'actor_id': 'hub2-completion', 'kind': 'task_continuation_started',
                            'at': 3000 + k, 'body': {'previous_session_id': prev, 'session_id': dsid, 'depth': k + 1, 'reason': 'loop'},
                        })
                        prev = dsid
                    intake_row['session_id'] = prev
                    intake_row['dispatch'] = project_dispatch(ST['sessions'][prev])
                    intake_row['updated_at'] += 1
        out({'id': s['id'], 'state': 'stopped', 'stop_confirmed': not CFG.get('stop_not_confirmed')})

    fail('unhandled sessions command: ' + sub)


def hub(argv):
    if argv[0] == 'intake':
        _handle_intake(argv[1:])
    elif argv[0] == 'sessions':
        _handle_sessions(argv[1:])
    fail('unhandled hub root command: ' + argv[0])


def native(argv):
    if argv == ['list', '--json', '--running']:
        rows = []
        for s in ST['sessions'].values():
            if not s['native_id']:
                continue
            if CFG.get('natural_exit_after_proof') and ST['log'].count('sessions:list') >= 1 and not s['stopped']:
                continue
            if not s['stopped'] or CFG.get('native_lingers'):
                rows.append({'id': s['native_id'], 'state': 'running', 'workspace': s['workspace']})
            elif CFG.get('a_list_includes_exited'):
                rows.append({'id': s['native_id'], 'state': 'exited'})
        out(rows)
    if len(argv) == 3 and argv[0] == 'status' and argv[2] == '--json':
        s = _native_session(argv[1])
        ST['log'].append('native:status')
        calls = ST.setdefault('status_calls', 0) + 1
        ST['status_calls'] = calls
        row = {
            'id': s['native_id'], 'tag': 'task-hub2-' + s['id'], 'workspace': s['workspace'],
            'created_at_ms': 1794585409000 + (calls if CFG.get('h_birth_changes') and calls > 1 else 0),
            'engine': s['engine'], 'phase': 'running', 'state': 'running',
            'worker_alive': True, 'worker_reachable': not CFG.get('h_status_unreachable'),
            # Unrelated full-record fields the driver must never persist.
            'command': ['agy', '--synthetic'], 'env': {'SYNTHETIC_PRIVATE_VALUE': 'must-not-leak'},
        }
        if CFG.get('h_status_unreachable'):
            row['rpc_error'] = 'synthetic persisted-status fallback'
        out(row)
    if len(argv) == 4 and argv[0] == 'capture' and argv[2:] == ['--screen', '--svg']:
        s = _native_session(argv[1])
        ST['log'].append('native:capture-svg')
        save()
        if CFG.get('h_capture_huge'):
            sys.stdout.write('<svg>' + 'x' * 300000)
            sys.exit(0)
        if CFG.get('h_capture_hang'):
            time.sleep(30)
        sys.stdout.write(_hydrogen_svg(s))
        sys.exit(0)
    if len(argv) == 5 and argv[0] == 'send' and argv[2:] == ['--hex', '0f', '--json']:
        s = _native_session(argv[1])
        ST['log'].append('native:send-0f')
        s['h_keys'] = s.get('h_keys', 0) + 1
        if CFG.get('h_after_send', 'expanded') == 'expanded':
            s['h_view'] = 'expanded'
        if CFG.get('h_lost_ack'):
            fail('synthetic lost send response')
        out({'id': s['native_id'], 'bytes': 1, 'status': 'pty_written', 'enter_written': False, 'consumed': None})
    if len(argv) == 4 and argv[0] == 'capture' and argv[2:] == ['--screen', '--plain']:
        nid = argv[1]
        s = next((x for x in ST['sessions'].values() if x['native_id'] == nid), None)
        if not s:
            fail('native session not found')
        expected_line = 'AC10_WORK_V2 %s %s %s %s 5050' % (
            s.get('run_id', ''), s.get('sample', ''), s.get('proof_digest', ''), s.get('proof_head', ''),
        )
        if CFG.get('trust_menu_rendered'):
            save()
            print('Do you trust the contents of this project?\n> Yes, I trust this folder')
            sys.exit(0)
        if CFG.get('capture_prompt_only'):
            save()
            print('> Phase 7 interactive native-pane probe\n' + expected_line)
            sys.exit(0)
        if CFG.get('claude_waiting_only'):
            save()
            print('● Bash(python3 work.py)\n  ⎿  Waiting…\n' + expected_line)
            sys.exit(0)
        if CFG.get('claude_prose_only') or CFG.get('capture_assistant_only'):
            save()
            print('● Done! Here is the readiness line:\n' + expected_line + '\n❯ ')
            sys.exit(0)
        if CFG.get('capture_result_only'):
            save()
            print(json.dumps({'type': 'result', 'result': expected_line}))
            sys.exit(0)
        if CFG.get('hydrogen_prose_command_exited'):
            save()
            print('  Command exited with code 0.\n  ' + expected_line)
            sys.exit(0)
        if CFG.get('claude_unterminated_echo'):
            save()
            print('● Bash(python3 -c\n' + expected_line + '\n')
            sys.exit(0)
        if CFG.get('hydrogen_unrelated_action'):
            save()
            print('● Ran (git status) (ctrl+o to expand)\n\nAssistant final summary:\n' + expected_line + '\n')
            sys.exit(0)
        probe_cmd = s.get('probe_command', 'python3 .local/a10/missing.py')
        if CFG.get('hydrogen_unrelated_python'):
            save()
            print('● Ran (python3 -c unrelated.py) (ctrl+o to expand)\n\nAssistant final summary:\n' + expected_line + '\n> ')
            sys.exit(0)
        if CFG.get('hydrogen_unrelated_shell'):
            save()
            print('● Ran (bash -c true) (ctrl+o to expand)\n\n  ' + expected_line + '\n> ')
            sys.exit(0)
        if CFG.get('hydrogen_assistant_footer'):
            save()
            print('● Ran 2 commands (aplexer whoami --jso... · cd /tmp...)\n\nAssistant: I ran `' + probe_cmd
                  + '`.\n' + expected_line + '\n> ')
            sys.exit(0)
        if CFG.get('hydrogen_truncated_header'):
            save()
            print('● Ran (' + probe_cmd[:20] + '...) (ctrl+o to expand)\n> ')
            sys.exit(0)
        engine = CFG.get('capture_format_override', row_for(s, show=True)['engine'])
        save()
        if engine == 'claude':
            print('● Bash(python3 - <<\'PY\' ...)\n  ⎿  ' + expected_line + '\n\n● Done.\n❯ ')
        elif engine == 'antigravity':
            # Real Hydrogen render collapses stdout; only the exact completed probe header is shown.
            print('● Ran (' + probe_cmd + ') (ctrl+o to expand)\n> ')
        else:
            print(expected_line)
        sys.exit(0)
    fail('forbidden or unknown native command: ' + repr(argv))


def _native_session(nid):
    s = next((x for x in ST['sessions'].values() if x['native_id'] == nid), None)
    if not s:
        fail('native session not found')
    return s


SVG_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" width="748" height="624" viewBox="0 0 748 624" '
            'font-family="DejaVu Sans Mono,Menlo,Consolas,monospace"><title>%s</title>'
            '<rect width="100%%" height="100%%" fill="#0c0c0c"/><g font-size="15" xml:space="preserve">')
MODE = 'Auto mode: edits & safe commands auto-approved (shift+tab to cycle)'
CAPTION = 'Format observation complete; awaiting Root STOP.'


def _esc(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def render_svg(title, rows):
    """Synthetic rendering of the observed 80x24 Hydrogen screen grammar (never real provider output)."""
    parts = [SVG_HEAD % _esc(title)]
    for y in sorted(rows):
        for x, fill, weight, text in rows[y]:
            parts.append('<text x="%d" y="%d" fill="%s" textLength="%d"%s>%s</text>' % (
                x, y, fill, 9 * len(text), ' font-weight="bold"' if weight else '', _esc(text)))
    parts.append('<rect x="0" y="588" width="748" height="24" fill="#1b1b1b"/>'
                 '<rect x="32" y="516" width="9" height="24" fill="none" stroke="#d4d4d4" stroke-width="1"/>'
                 '<text x="14" y="605" fill="#c8cdd4">' + _esc('● RUNNING  ^b ?' + ' ' * 65) + '</text></g></svg>')
    return ''.join(parts)


def _hydrogen_svg(s):
    home = os.path.expanduser('~')
    title = 'aplexer ~/' + s['workspace'][len(home) + 1:] + ':task-hub2-' + s['id']
    cmd = s.get('probe_command', 'python3 .local/h7_000000000000_01.py')
    composer = '> draft text' if CFG.get('h_view') == 'draft' else None
    rows = {
        492: [(14, '#5c6370', None, '─' * 80)],
        516: ([(14, '#7cbff2', None, '>'), (23, '#d4d4d4', None, ' '), (32, '#5c6370', None, MODE)]
              if composer is None else [(14, '#7cbff2', None, '>'), (23, '#d4d4d4', None, ' '),
                                        (32, '#d4d4d4', None, 'draft text')]),
        540: [(14, '#5c6370', None, '─' * 80)],
        564: [(14, '#5c6370', None, '? for shortcuts'), (437, '#e5c07b', None, 'auto'),
              (473, '#d4d4d4', None, ' · Gemini 4 Hydrogen · medium')],
    }
    view = s.get('h_view') or CFG.get('h_view') or 'collapsed'
    if view == 'busy':
        rows[396] = [(14, '#98c379', None, '●'), (23, '#d4d4d4', None, ' '), (32, '#e5c07b', 'bold', 'Running'),
                     (95, '#abb2bf', None, '(' + cmd + ')')]
    elif view == 'expanded':
        rows.update({
            156: [(14, '#5c6370', None, '▾ Thought for 2s')],
            204: [(14, '#98c379', None, '●'), (23, '#d4d4d4', None, ' '), (32, '#e5c07b', 'bold', 'Bash'),
                  (68, '#abb2bf', None, '(' + cmd + ')')],
            228: [(14, '#abb2bf', None, '  ⎿  '), (59, '#d4d4d4', None, 'Created At: 2026-11-13T16:57:09+01:00')],
            252: [(59, '#d4d4d4', None, 'Completed At: 2026-11-13T16:57:10+01:00')],
            276: [(59, '#d4d4d4', None, 'Command exited with code %d.' % CFG.get('h_exit_code', 0))],
            324: [(59, '#d4d4d4', None, 'Command output:')],
            348: [(59, '#d4d4d4', None, 'AC10_WORK_V2 %s %s' % (s.get('run_id', ''), s.get('sample', '')))],
            372: [(59, '#d4d4d4', None, s.get('proof_digest', ''))],
            396: [(59, '#d4d4d4', None, s.get('proof_head', '') + ' 5050 '), (473, '#abb2bf', None, '(ctrl+o to collapse)')],
            444: [(14, '#d4d4d4', None, '  ' + (('I ran ' + cmd) if CFG.get('h_fake_footer') else CAPTION))],
        })
    else:
        count = CFG.get('h_aggregate', 3)
        rows.update({
            372: [(14, '#abb2bf', None, '○ Thought for 11.8s')],
            396: [(14, '#98c379', None, '●'), (23, '#d4d4d4', None, ' '), (32, '#e5c07b', 'bold', 'Ran'),
                  (59, '#d4d4d4', None, ' %d commands ' % count),
                  (167, '#abb2bf', None, '(+%d · %s...)' % (count - 1, cmd[:20])), (437, '#d4d4d4', None, ' '),
                  (446, '#abb2bf', None, '(ctrl+o to expand)')],
            444: [(14, '#d4d4d4', None, '  ' + CAPTION)],
        })
    return render_svg(title, rows)


def fake_git(argv):
    assert argv[0] == '-C'
    if argv[2:4] == ['rev-parse', '--verify']:
        print(CFG.get('git_head', 'b' * 40))
        sys.exit(0)
    if argv[2:4] == ['status', '--porcelain=v1']:
        sys.stdout.buffer.write(CFG.get('git_status_raw', '').encode('utf-8'))
        sys.exit(0)
    fail('unexpected git command: ' + repr(argv))


def alerts():
    if CFG.get('alerts_broken'):
        print('not json')
        sys.exit(0)
    if CFG.get('active_ids_only'):
        out({'alerts': [{'id': 'old'}]})
    after = bool(ST['sessions'])
    count = CFG.get('alert_count_before', 40)
    if after and any(CFG.get(k) for k in ('alert_during', 'alert_transient', 'alert_dedup')):
        count += 1
    if after and CFG.get('alert_reset'):
        count -= 1
    generation = 'synthetic-test-epoch'
    if after and CFG.get('alert_generation_change'):
        generation += '-new'
    observed = int(time.time() * 1000) - (120000 if CFG.get('alert_stale') or after and CFG.get('alert_post_stale') else 0)
    if after and CFG.get('alert_cached_post'):
        observed = ST['watermarks'][0]['observed_at_ms'] + 1
    watermark = dict(kind='dev_alert_occurrences_v1', recipient='dev@dtcdev.click', generation=generation,
                     count=count, observed_at_ms=observed)
    if CFG.get('alert_missing_metadata'):
        watermark.pop('generation')
    if CFG.get('alert_wrong_recipient'):
        watermark['recipient'] = 'someone-else@example.test'
    ST.setdefault('watermarks', []).append(watermark)
    out(watermark)


name = Path(sys.argv[0]).name
if name == 'hub':
    hub(sys.argv[1:])
elif name == 'a':
    native(sys.argv[1:])
elif name == 'git':
    fake_git(sys.argv[1:])
else:
    alerts()
