from agent_hub_host import fleet, session_progress as p
from agent_hub_host.zcodex_health import ZcodexHealth


def row(sid='z1', **kw):
    return dict({'id': sid, 'created_at_ms': 1000, 'engine': 'zcodex', 'state': 'exiting',
                 'tag': 'hub-o', 'command': ['zcodex', '/goal task'], 'workload_pid': 2 ** 22 + 7,
                 'containment_empty': False}, **kw)


def test_zcodex_cap_is_engine_specific():
    assert fleet.worker_resource_argv('zcodex')[1] == '4096M'
    assert fleet.worker_resource_argv('codex')[1] == '1500M'
    assert fleet.worker_resource_argv()[1] == '1500M'


def test_dead_leader_with_children_is_terminal_error(tmp_path):
    s = p.SessionProgress(now_ms=lambda: 120000, output=lambda r: None, proc_root=tmp_path)
    out = s.sample([row()])
    assert out['z1']['terminal_error'] == 'zcodex_agent_exited'
    assert s.sample([row(containment_empty=True)])['z1'].get('terminal_error') is None
    assert 'terminal_error' not in s.sample([row(engine='codex')])['z1']


def test_hold_on_new_failure_and_release_only_by_probe_pass(tmp_path):
    calls, probes, clock = [], [False], [1000.0]
    h = ZcodexHealth(lambda m, path, body=None: calls.append(body), tmp_path / 's.json',
                     probe=lambda: probes[0], now=lambda: clock[0], threaded=False)
    bad = {'z1': {'terminal_error': 'zcodex_agent_exited'}}
    assert not h.observe([row()], bad)  # pre-existing corpse: baseline only
    assert calls == []
    assert h.observe([row(), row('z2')], {**bad, 'z2': {'terminal_error': 'zcodex_agent_exited'}})
    assert calls == [{'engine': 'zcodex', 'status': 'unavailable', 'reason': 'zcodex_session_failed'}]
    clock[0] += 400
    assert h.observe([row('z2')], bad)  # probe fails: hold stays
    assert len(calls) == 1
    clock[0] += 400
    probes[0] = True
    assert not h.observe([row('z2')], bad)
    assert calls[-1] == {'engine': 'zcodex', 'status': 'ok', 'reason': 'zcodex_probe_passed'}


def test_headless_reconnect_exhaustion_holds_until_real_probe_passes(tmp_path):
    calls, healthy, clock = [], [False], [1000.0]
    h = ZcodexHealth(lambda m, path, body=None: calls.append(body), tmp_path / 'headless.json',
                     probe=lambda: healthy[0], now=lambda: clock[0], threaded=False)
    assert not h.observe([], {})
    dead = row(command=['zcodex', 'exec', 'task'], phase='exited', containment_empty=True,
               exit={'code': 75, 'oom_killed': False})
    progress = {'z1': {'headless': True, 'agent_alive': False, 'cleanup': 'none'}}
    assert h.observe([dead], progress)
    assert calls == [{'engine': 'zcodex', 'status': 'unavailable', 'reason': 'zcodex_session_failed'}]
    clock[0] += 400
    assert h.observe([dead], progress)
    assert len(calls) == 1
    healthy[0] = True
    clock[0] += 400
    assert not h.observe([dead], progress)
    assert calls[-1] == {'engine': 'zcodex', 'status': 'ok', 'reason': 'zcodex_probe_passed'}
    assert not h.observe([dead], progress)  # same failure cannot re-hold after recovery


def test_headless_oom_is_held_but_existing_corpses_are_baselined(tmp_path):
    calls = []
    dead = row(command=['zcodex', 'exec', 'task'], phase='exiting',
               exit={'code': None, 'signal': 9, 'oom_killed': True})
    h = ZcodexHealth(lambda *args: calls.append(args), tmp_path / 'oom.json', threaded=False)
    assert not h.observe([dead], {})
    assert not calls
    assert h.observe([dead, dict(dead, id='new', created_at_ms=2000)], {})
    assert len(calls) == 1


def test_quiet_live_successful_stopped_and_unconfirmed_sessions_do_not_hold(tmp_path):
    calls = []
    h = ZcodexHealth(lambda *args: calls.append(args), tmp_path / 'negative.json', threaded=False)
    assert not h.observe([], {})
    rows = [
        row('quiet', phase='running', command=['zcodex', 'exec', 'task']),
        row('success', phase='exited', exit={'code': 0}),
        row('stopped', phase='exiting', exit={'code': None, 'signal': 15, 'oom_killed': False}),
        row('unknown', phase='running', exit={'code': 75}),
        row('untyped', phase='exited', exit={'code': '75'}),
        row('other', phase='exited', engine='codex', exit={'code': 75}),
    ]
    assert not h.observe(rows, {r['id']: {'headless': True, 'agent_alive': True} for r in rows})
    assert calls == []
