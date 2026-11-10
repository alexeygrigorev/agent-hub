#!/usr/bin/env python3
"""Fake `hub` / `a` / alert-sample binary for hub_acceptance_10 tests (dispatch on argv[0] basename).

Response shapes are cloned from the real-CLI fixtures next to this file. Scenario knobs come from the JSON file
named by FAKE_CFG; state lives in FAKE_CFG + '.state'. Offline only."""
import copy
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.load(open(os.environ['FAKE_CFG']))
STATE_PATH = os.environ['FAKE_CFG'] + '.state'
try:
    ST = json.load(open(STATE_PATH))
except OSError:
    ST = {'n': 0, 'sessions': {}, 'keys': {}, 'log': []}


def fx(name):
    return json.load(open(HERE / name))


def save():
    json.dump(ST, open(STATE_PATH, 'w'))


def out(obj):
    print(json.dumps(obj))
    save()
    sys.exit(0)


def fail(msg, code=1):
    ST['log'].append('fail:' + msg)
    save()
    print(msg, file=sys.stderr)
    sys.exit(code)


def uid(i, kind):
    return str(uuid.UUID(int=(kind << 64) | (i + 1)))


def row_for(s):
    row = copy.deepcopy(fx('hub_sessions_show.json')['session'])
    row.update(id=s['id'], native_id=s['native_id'], model=s['model'], engine=s['engine'],
               state=s['state'], machine_id=s['host'])
    row.pop('stop_confirmed', None)
    return row


def new_session(i, key, prompt):
    models = CFG['models']
    s = {'id': uid(i, 1), 'native_id': uid(i, 2), 'model': models[i % len(models)],
         'engine': CFG.get('engine', 'claude'), 'state': 'running', 'host': CFG.get('host', 'hetzner-aplexer'),
         'workspace': CFG.get('workspace', '/home/user/work/ws-%d' % i), 'prompt': prompt, 'key': key, 'stopped': False}
    ST['sessions'][s['id']] = s
    ST['keys'][key] = s['id']
    return s


def hub(argv):
    assert argv[0] == 'sessions', argv
    sub = argv[1]
    if sub == 'pick':
        if CFG.get('pick_fail'):
            fail('pick failed')
        pool = CFG.get('pool') or fx('hub_sessions_pick.json')['pool']
        pick = fx('hub_sessions_pick.json')
        pick['pool'] = pool
        out(pick)
    if sub == 'start':
        key = argv[argv.index('--key') + 1]
        prompt = argv[argv.index('--prompt') + 1]
        for banned in ('--engine', '--model', '--tier', '--machine'):
            assert banned not in argv, banned
        if key in ST['keys']:  # idempotent replay
            s = ST['sessions'][ST['keys'][key]]
            out({'id': s['id'], 'client_key': key, 'engine': s['engine'], 'model': s['model']})
        i = ST['n']
        if CFG.get('fail_start_at') == i:
            ST['n'] += 1
            fail('start refused')
        ST['n'] += 1
        s = new_session(i, key, prompt)
        if CFG.get('leak_at') == i:  # session created, then --wait fails
            if CFG.get('leak_with_id', True):
                fail('session_start_failed; session %s reached starting; reconcile with --key %s via show/events' % (s['id'], key))
            fail('Session wait timed out')
        if CFG.get('hang_start_at') == i:
            save()
            time.sleep(30)
        ST['log'].append('start:' + ' '.join(a for a in argv if a.startswith('--intake')))
        resp = {'id': s['id'], 'route': 'claude/%s/medium' % s['model'], 'engine': s['engine'], 'model': s['model'],
                'effort': 'medium', 'client_key': key, 'binding_wait': {'session': row_for(s)}}
        if CFG.get('start_pool'):
            resp['pool'] = CFG['start_pool']
        out(resp)
    if sub == 'show':
        if CFG.get('flat_show'):
            s = ST['sessions'][argv[2]]
            out(row_for(s))
        s = ST['sessions'][argv[2]]
        if s['stopped'] and not CFG.get('stop_not_confirmed'):
            s['state'] = 'stopped'
        out({'session': row_for(s)})
    if sub == 'list':
        items = []
        for s in ST['sessions'].values():
            r = row_for(s)
            if CFG.get('list_no_host'):
                r['machine_id'] = None
            if not CFG.get('list_no_workspace'):
                r['workspace'] = s['workspace']
            items.append(r)
        out({'items': items, 'sessions': items, 'complete': True})
    if sub == 'stop':
        s = ST['sessions'][argv[2]]
        s['stopped'] = True
        ST['log'].append('stop:' + s['id'])
        out({'id': s['id'], 'state': 'stopping'})
    raise SystemExit('unhandled hub %s' % argv)


def native(argv):
    if argv[0] == 'list':
        rows = fx('a_list_running.json')
        res = []
        for s in ST['sessions'].values():
            if s['stopped'] and not CFG.get('native_lingers'):
                continue
            r = copy.deepcopy(rows[0])
            r.update(id=s['native_id'], workspace=s['workspace'], cwd=s['workspace'])
            res.append(r)
        if CFG.get('a_list_includes_exited'):  # historical rows must not count as live
            for s in ST['sessions'].values():
                if s['stopped']:
                    r = copy.deepcopy(rows[0])
                    r.update(id=s['native_id'], state='exited', phase='exited')
                    res.append(r)
        out(res)
    if argv[0] == 'capture':
        s = next(v for v in ST['sessions'].values() if v['native_id'] == argv[1])
        if CFG.get('capture_prompt_only'):
            out_text = '> ' + s['prompt'] + '\n'
        else:
            m = re.search(r'\$\(\((\d+)\*(\d+)\)\)', s['prompt'])
            out_text = '> %s\nran echo\n%d\n' % (s['prompt'], int(m[1]) * int(m[2]))
        print(out_text)
        save()
        sys.exit(0)
    raise SystemExit('unhandled a %s' % argv)


def alerts():
    ids = list(CFG.get('alerts_before', []))
    if CFG.get('alert_during') and ST['sessions']:
        ids.append('alert-during')
    if CFG.get('alerts_broken'):
        print('not json')
        sys.exit(0)
    out({'alerts': [{'id': i} for i in ids]})


name = os.path.basename(sys.argv[0])
if name == 'hub':
    hub(sys.argv[1:])
elif name == 'a':
    native(sys.argv[1:])
else:
    alerts()
