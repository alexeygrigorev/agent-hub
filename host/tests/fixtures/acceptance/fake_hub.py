#!/usr/bin/env python3
"""Synthetic modern CLI contracts. Never invokes Hub, native tools, or services."""
import base64
import copy
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
ST = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {'n': 0, 'sessions': {}, 'keys': {}, 'log': []}


def save():
    # Capture subprocesses can be killed at their deadline. Never truncate the
    # last complete state that later cleanup commands must still be able to read.
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


def choice(p):
    return {k: p[k] for k in ('policy_id', 'policy_version', 'machine_id', 'attempt', 'engine', 'model', 'effort')}


def row_for(s, show=False):
    row = {k: s[k] for k in ('id', 'native_id', 'engine', 'model', 'effort', 'workspace', 'policy_id', 'policy_version')}
    row.update(machine_id=s['host'], state='stopped' if s['stopped'] and not CFG.get('stop_not_confirmed') else 'running',
               operation_id=s['id'] + ':0', picker=copy.deepcopy(s['picker']))
    if show and CFG.get('fallback'):
        old = choice(row['picker'])
        pool = row['picker']['pool']
        index = next(i for i, x in enumerate(pool) if x['model'] == row['model'])
        chosen = pool[(index + 1) % len(pool)]
        row.update({k: chosen[k] for k in ('policy_id', 'model', 'engine', 'effort')})
        row['operation_id'] = s['id'] + ':1'
        row['picker'].update(choice(dict(row['picker'], **chosen, attempt=1)))
        row['picker']['history'] = [old, choice(row['picker'])]
        if CFG.get('cross_attempt_pool'):
            row['picker']['pool'] = [dict(x, share=1) for x in s['picker']['pool'] if x['policy_id'] == old['policy_id']]
    if show and CFG.get('show_pool_changed'):
        for option in row['picker']['pool']: option['weight'] *= 2
    if CFG.get('missing_picker') or show and CFG.get('show_missing_picker'):
        row.pop('picker')
    if CFG.get('picker_model_mismatch') and 'picker' in row:
        row['picker']['model'] = 'wrong-model'
    if CFG.get('missing_version') and 'picker' in row:
        row['picker'].pop('policy_version')
    return row


def hub(argv):
    assert argv[0] == 'sessions', argv
    sub = argv[1]
    # Exercise real argument parsing as well as synthetic command responses.
    import argparse
    from agent_hub_host import session_control
    parser = argparse.ArgumentParser()
    session_control.add_commands(parser.add_subparsers(dest='cmd'))
    parser.parse_args(argv)
    ST['log'].append(sub)
    if sub == 'pick':
        fail('independent pick must never be used for session acceptance')
    if sub == 'start':
        for banned in ('--engine', '--model', '--effort', '--tier', '--override-reason', '--reason'):
            assert banned not in argv, banned
        assert value(argv, '--machine') == 'hetzner-aplexer'
        assert value(argv, '--cwd').startswith('/')
        assert '--wait' not in argv  # exercise real default wait, never an obsolete explicit gate
        key, prompt = value(argv, '--key'), value(argv, '--prompt')
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
            if CFG.get('bad_recovered_id'): row['id'] = '../../invalid-session'
            out(row)
        i = ST['n']; ST['n'] += 1
        if CFG.get('fail_start_at') == i:
            ST.setdefault('refused_keys', []).append(key)
            fail('Session HTTP request failed with status 400 (invalid_request)')
        model = CFG['models'][i % len(CFG['models'])]
        pool = copy.deepcopy(CFG.get('pool', json.loads((HERE / 'hub_sessions_pick.json').read_text())['pool']))
        chosen = next((x for x in pool if x['model'] == model), dict(policy_id='unknown', model=model, engine='codex', effort='medium'))
        sid = str(uuid.UUID(int=1 if CFG.get('reuse_session_id') else i + 1))
        picker = dict(chosen, policy_version=7, machine_id=CFG.get('host', 'hetzner-aplexer'), attempt=0, pool=pool)
        picker.pop('weight', None); picker.pop('share', None)
        picker['history'] = [choice(picker)]
        s = dict(id=sid, native_id=CFG['native_id'] if 'native_id' in CFG else str(uuid.UUID(int=1001 if CFG.get('reuse_native_id') else i + 1001)), engine=chosen['engine'], model=model, effort=chosen['effort'],
                 workspace=CFG.get('workspace', value(argv, '--cwd')), host=picker['machine_id'], prompt=prompt,
                 stopped=False, picker=picker, policy_id=chosen['policy_id'], policy_version=7)
        ST['sessions'][sid] = s; ST['keys'][key] = sid
        ST['log'].append('start:--intake-task-id' if '--intake-task-id' in argv else 'start:synthetic')
        if CFG.get('leak_at') == i:
            fail('session_start_failed; session %s reached starting' % sid if CFG.get('leak_with_id', True) else 'Session wait timed out')
        if CFG.get('hang_start_at') == i:
            save(); time.sleep(30)
        if CFG.get('accepted_nonjson') or CFG.get('accepted_nonjson_at') == i:
            save(); print('upstream sent malformed success'); sys.exit(0)
        if CFG.get('accepted_502'):
            fail('upstream returned 502 Bad Gateway')
        row = row_for(s)
        if CFG.get('initial_missing_id'): row.pop('id')
        if CFG.get('initial_bad_id'): row['id'] = '../../invalid-session'
        if CFG.get('initial_offered'):
            row['state'] = 'offered'
        out(dict(row, client_key=key, binding_wait={'session': row_for(s)}))
    if sub == 'show':
        row = row_for(ST['sessions'][argv[2]], show=True)
        if CFG.get('wrong_show_id'): row['id'] = str(uuid.UUID(int=999999))
        out(row if CFG.get('flat_show') else {'session': row})
    if sub == 'list':
        rows = []
        for s in ST['sessions'].values():
            if s['stopped']:
                continue
            row = row_for(s, show=True)
            if CFG.get('list_no_host'): row['machine_id'] = None
            if CFG.get('list_no_workspace'): row.pop('workspace', None)
            rows.append(row)
        out({'sessions': rows, 'complete': True})
    if sub == 'capture':
        assert '--wait' in argv and '--key' in argv
        s = ST['sessions'][argv[2]]
        encoded = re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', s['prompt'])[1]
        marker = base64.b64decode(encoded).decode()
        if CFG.get('capture_prompt_only'):
            event = {'type': 'item.started', 'item': {'type': 'command_execution', 'command': s['prompt'], 'aggregated_output': marker}}
        elif CFG.get('capture_assistant_only'):
            event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': marker}}
        elif CFG.get('capture_result_only'):
            event = {'type': 'result', 'result': marker}
        else:
            event = {'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': marker + '\n'}}
        out({'command_wait': {'state': 'pending' if CFG.get('capture_unacked') else 'acked', 'ack': {'capture': json.dumps(event)}}})
    if sub == 'stop':
        s = ST['sessions'][argv[2]]; s['stopped'] = True
        ST['log'].append('stop:' + s['id'])
        out({'id': s['id'], 'state': 'stopped'})
    fail('unhandled hub command')


def native(argv):
    assert argv == ['list', '--json', '--running'], 'native capture/mutation forbidden'
    rows = []
    for s in ST['sessions'].values():
        if not s['stopped'] or CFG.get('native_lingers'):
            rows.append({'id': s['native_id'], 'state': 'running', 'workspace': s['workspace']})
        elif CFG.get('a_list_includes_exited'):
            rows.append({'id': s['native_id'], 'state': 'exited'})
    out(rows)


def alerts():
    # Synthetic occurrence source; not a claim that the production active-alert
    # API supports this contract. Transient/resolved and deduplicated events each
    # increment the durable occurrence total even though active IDs do not change.
    if CFG.get('alerts_broken'):
        print('not json'); sys.exit(0)
    if CFG.get('active_ids_only'):
        out({'alerts': [{'id': 'old'}]})
    after = bool(ST['sessions'])
    count = CFG.get('alert_count_before', 40)
    if after and any(CFG.get(k) for k in ('alert_during', 'alert_transient', 'alert_dedup')):
        count += 1
    if after and CFG.get('alert_reset'): count -= 1
    generation = 'synthetic-test-epoch'
    if after and CFG.get('alert_generation_change'): generation += '-new'
    observed = int(time.time() * 1000) - (120000 if CFG.get('alert_stale') or after and CFG.get('alert_post_stale') else 0)
    if after and CFG.get('alert_cached_post'):
        observed = ST['watermarks'][0]['observed_at_ms'] + 1
    watermark = dict(kind='dev_alert_occurrences_v1', recipient='dev@dtcdev.click', generation=generation,
                     count=count, observed_at_ms=observed)
    if CFG.get('alert_missing_metadata'): watermark.pop('generation')
    if CFG.get('alert_wrong_recipient'): watermark['recipient'] = 'someone-else@example.test'
    ST.setdefault('watermarks', []).append(watermark)
    out(watermark)


name = Path(sys.argv[0]).name
if name == 'hub': hub(sys.argv[1:])
elif name == 'a': native(sys.argv[1:])
else: alerts()
