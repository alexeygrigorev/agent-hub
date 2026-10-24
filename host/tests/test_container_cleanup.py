import copy
import json
import subprocess
from agent_hub_host.container_cleanup import eligible, sweep

SID = 'test-session'
CID = 'a' * 64
BASE = {'Config': {'Labels': {'agent.session': SID}},
        'State': {'Status': 'created', 'StartedAt': '0001-01-01T00:00:00Z'}}


def test_selector():
    assert eligible(BASE, SID)
    assert not eligible(BASE, 'other')
    for labels in ({}, {'agent.session': SID, 'coolify.managed': 'false'},
                   {'agent.session': SID, 'com.coolify.app': 'app'}):
        obj = copy.deepcopy(BASE)
        obj['Config']['Labels'] = labels
        assert not eligible(obj, SID)
    for status in ('running', 'exited', 'paused', 'dead'):
        obj = copy.deepcopy(BASE)
        obj['State']['Status'] = status
        assert not eligible(obj, SID)
    obj = copy.deepcopy(BASE)
    obj['State']['StartedAt'] = '2026-10-24T00:00:00Z'
    assert not eligible(obj, SID)


def test_sweep_states_dryrun_and_revalidation(capsys):
    for state in ('running', 'failed', 'uncertain', 'stopped'):
        for apply in (False, True):
            calls = []
            def run(argv):
                calls.append(argv)
                if argv[1] == 'ps':
                    assert 'label=agent.session' in argv
                    assert 'status=created' in argv
                    return CID
                if argv[1] == 'inspect':
                    return json.dumps([BASE])
                if argv[1] == 'sessions':
                    return json.dumps({'id': SID, 'state': state})
                assert argv == ['docker', 'rm', CID]
                return CID
            sweep(apply, run=run)
            assert (['docker', 'rm', CID] in calls) == (apply and state == 'stopped')
    assert 'dry-run' in capsys.readouterr().out
    for failure in ('changed', 'lookup', 'wrong-id'):
        calls = []
        def run(argv):
            calls.append(argv)
            if argv[1] == 'ps': return CID
            if argv[1] == 'sessions':
                if failure == 'lookup': raise subprocess.TimeoutExpired(argv, 30)
                return json.dumps({'id': 'other' if failure == 'wrong-id' else SID, 'state': 'stopped'})
            obj = copy.deepcopy(BASE)
            if failure == 'changed' and sum(c[1] == 'inspect' for c in calls) > 1:
                obj['State']['Status'] = 'running'
            return json.dumps([obj])
        sweep(True, run=run)
        assert not any(c[1] == 'rm' for c in calls)
