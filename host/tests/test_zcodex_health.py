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
