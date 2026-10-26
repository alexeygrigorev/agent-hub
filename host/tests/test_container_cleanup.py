import copy
import json
import subprocess
from agent_hub_host.container_cleanup import (
    eligible, eligible_ttl, has_test_label, container_age_hours, sweep,
)

SID = 'test-session'
CID = 'a' * 64
BASE = {'Config': {'Labels': {'agent.session': SID}},
        'State': {'Status': 'created', 'StartedAt': '0001-01-01T00:00:00Z'}}


def _legacy_run_factory(calls, state):
    def run(argv):
        calls.append(argv)
        if argv[1] == 'ps':
            # TTL path uses label-only filters; legacy test returns nothing there.
            if 'status=created' not in argv:
                return ''
            assert 'label=agent.session' in argv
            return CID
        if argv[1] == 'inspect':
            return json.dumps([BASE])
        if argv[1] == 'sessions':
            return json.dumps({'id': SID, 'state': state})
        assert argv == ['docker', 'rm', CID]
        return CID
    return run


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
            run = _legacy_run_factory(calls, state)
            sweep(apply, run=run)
            assert (['docker', 'rm', CID] in calls) == (apply and state == 'stopped')
    assert 'dry-run' in capsys.readouterr().out
    for failure in ('changed', 'lookup', 'wrong-id'):
        calls = []
        def run(argv):
            calls.append(argv)
            if argv[1] == 'ps':
                if 'status=created' not in argv:
                    return ''
                return CID
            if argv[1] == 'sessions':
                if failure == 'lookup': raise subprocess.TimeoutExpired(argv, 30)
                return json.dumps({'id': 'other' if failure == 'wrong-id' else SID, 'state': 'stopped'})
            obj = copy.deepcopy(BASE)
            if failure == 'changed' and sum(c[1] == 'inspect' for c in calls) > 1:
                obj['State']['Status'] = 'running'
            return json.dumps([obj])
        sweep(True, run=run)
        assert not any(c[1] == 'rm' for c in calls)


def test_test_label_and_coolify_guard():
    assert has_test_label({'agent.session': SID})
    assert has_test_label({'pocketshell.test': '1'})
    assert has_test_label({'pocketshell.test': 'true'})
    assert not has_test_label({})
    assert not has_test_label({'other': '1'})
    assert not has_test_label({'agent.session': SID, 'coolify.managed': 'x'})
    assert not has_test_label({'pocketshell.test': '1', 'com.coolify.app': 'y'})


def test_ttl_age_and_eligibility():
    old = {'Config': {'Labels': {'pocketshell.test': '1'}},
           'Created': '2026-10-24T00:00:00Z',
           'State': {'Status': 'exited', 'StartedAt': '2026-10-24T00:01:00Z'}}
    new = {'Config': {'Labels': {'pocketshell.test': '1'}},
           'Created': '2026-10-26T07:50:00Z',
           'State': {'Status': 'exited', 'StartedAt': '2026-10-26T07:51:00Z'}}
    now = 1793001600.0  # 2026-10-26T08:00:00Z
    assert container_age_hours(old, now) is not None and container_age_hours(old, now) > 6
    assert eligible_ttl(old, now, 6.0)
    assert not eligible_ttl(new, now, 6.0)
    running = copy.deepcopy(old)
    running['State']['Status'] = 'running'
    assert not eligible_ttl(running, now, 6.0)
    coolify = copy.deepcopy(old)
    coolify['Config']['Labels'] = {'pocketshell.test': '1', 'coolify.managed': 'x'}
    assert not eligible_ttl(coolify, now, 6.0)
    unlabeled = copy.deepcopy(old)
    unlabeled['Config']['Labels'] = {}
    assert not eligible_ttl(unlabeled, now, 6.0)


def test_ttl_sweep_dryrun_and_apply(capsys):
    old_cid = 'b' * 64
    old = {'Config': {'Labels': {'pocketshell.test': '1'}},
           'Created': '2026-10-24T00:00:00Z',
           'State': {'Status': 'exited', 'StartedAt': '2026-10-24T00:01:00Z'}}
    cool_cid = 'c' * 64
    cool = {'Config': {'Labels': {'pocketshell.test': '1', 'coolify.managed': 'x'}},
            'Created': '2026-10-24T00:00:00Z',
            'State': {'Status': 'exited', 'StartedAt': '2026-10-24T00:01:00Z'}}
    now = 1793001600.0
    for apply in (False, True):
        calls = []

        def run(argv):
            calls.append(argv)
            if argv[1] == 'ps':
                if 'status=created' in argv:
                    return ''
                return f'{old_cid}\n{cool_cid}\n{"d" * 64}'
            if argv[1] == 'inspect':
                # Third id is unlabeled.
                return json.dumps([old if old_cid in argv else cool if cool_cid in argv else
                                   {'Config': {'Labels': {}}, 'Created': '2026-10-24T00:00:00Z',
                                    'State': {'Status': 'exited'}}])
            if argv == ['docker', 'rm', old_cid]:
                return old_cid
            raise AssertionError(f'unexpected call {argv}')

        sweep(apply, run=run, max_age_hours=6.0, now=now)
        out = capsys.readouterr().out
        assert f'container={old_cid}' in out and 'action=remove-ttl' in out
        assert (['docker', 'rm', old_cid] in calls) == apply
        assert not any(c == ['docker', 'rm', cool_cid] for c in calls)
    # Live session retains even when old.
    calls = []

    def run_live(argv):
        calls.append(argv)
        if argv[1] == 'ps':
            if 'status=created' in argv:
                return ''
            return old_cid
        if argv[1] == 'inspect':
            return json.dumps([{'Config': {'Labels': {'agent.session': SID}},
                                'Created': '2026-10-24T00:00:00Z',
                                'State': {'Status': 'exited'}}])
        if argv[1] == 'sessions':
            return json.dumps({'id': SID, 'state': 'running'})
        raise AssertionError(f'unexpected call {argv}')

    sweep(True, run=run_live, max_age_hours=6.0, now=now)
    assert not any(c[1] == 'rm' for c in calls)
    assert 'retained' in capsys.readouterr().out


def test_agent_session_hub_unknown_retains():
    # agent.session with Hub lookup failure must retain (fail-closed).
    cid = 'e' * 64
    old = {'Config': {'Labels': {'agent.session': 'test-session'}},
           'Created': '2026-10-24T00:00:00Z',
           'State': {'Status': 'exited', 'StartedAt': '2026-10-24T00:01:00Z'}}
    calls = []
    def run(argv):
        calls.append(argv)
        if argv[1] == 'ps':
            if 'status=created' in argv:
                return ''
            return cid
        if argv[1] == 'inspect':
            return __import__('json').dumps([old])
        if argv[1] == 'sessions':
            raise Exception('hub down')
        raise AssertionError(f'unexpected {argv}')
    import json as _json
    # monkey-patch _hub_session_state to simulate unknown by making sessions show fail
    sweep(True, run=run, max_age_hours=6.0, now=1793001600.0)
    assert not any(c[1] == 'rm' for c in calls)
