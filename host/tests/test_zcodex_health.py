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
    assert not h.observe([row(exit={'code': 75})], bad)  # pre-existing corpse: baseline only
    assert calls == []
    assert h.observe([row(exit={'code': 75}), row('z2', exit={'code': 75})], {**bad, 'z2': {'terminal_error': 'zcodex_agent_exited'}})
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


def test_actual_sampler_markers_do_not_hold_normal_stopped_or_unknown_exits(tmp_path):
    calls = []
    h = ZcodexHealth(lambda *args: calls.append(args), tmp_path / 'producer.json', threaded=False)
    assert not h.observe([], {})
    rows = [row('normal', exit={'code': 0}),
            row('stopped', exit={'code': None, 'signal': 15, 'oom_killed': False}),
            row('unknown')]
    sampler = p.SessionProgress(now_ms=lambda: 120000, output=lambda r: None, proc_root=tmp_path)
    progress = sampler.sample(rows)
    assert all(progress[r['id']]['terminal_error'] == 'zcodex_agent_exited' for r in rows)
    assert not h.observe(rows, progress)
    assert not calls


def test_native_failed_phase_requires_explicit_failure_evidence(tmp_path):
    calls = []
    h = ZcodexHealth(lambda *args: calls.append(args), tmp_path / 'failed.json', threaded=False)
    assert not h.observe([], {})
    unknown = row('unknown', phase='failed', command=['zcodex', 'exec', 'task'],
                  containment_empty=True, exit={'code': None, 'signal': 9, 'oom_killed': False})
    assert not h.observe([unknown], {})
    assert h.observe([dict(unknown, id='oom', exit={'code': None, 'signal': 9, 'oom_killed': True})], {})
    assert len(calls) == 1


def test_vanished_native_qualified_retirement_holds_once_and_recovers_only_by_probe(tmp_path):
    import json
    from types import SimpleNamespace
    from agent_hub_host.native_sessions import NativeTarget
    from agent_hub_host.retired_native import verify_native_retirement
    from agent_hub_host.zcodex_health import observe_verified_terminal

    sid = 'ab7ac540-f991-4543-8580-5cce7136da1b'
    work = tmp_path / 'work'
    work.mkdir()
    target = NativeTarget(sid, 'task-hub-proof', str(work), 1000, 'zcodex').validated()
    record = {'native_id': sid, 'tag': target.tag, 'workspace': str(work),
              'incarnation': 1000, 'engine': 'zcodex'}
    calls, healthy, clock = [], [False], [1000.0]
    h = ZcodexHealth(lambda m, path, body=None: calls.append(body), tmp_path / 'retired-health.json',
                     probe=lambda: healthy[0], now=lambda: clock[0], threaded=False)
    owner = SimpleNamespace(_zcodex_health=h)
    assert not h.observe([row(sid, workspace=str(work), tag=target.tag, phase='running')], {})
    assert not h.observe([], {})  # disappearance alone proves nothing
    ok, evidence, _ = verify_native_retirement('unused', target, state_dir=str(tmp_path), sessions=[])
    assert not ok and evidence is None
    tomb = tmp_path / 'retired-sessions' / sid / 'tombstone.json'
    tomb.parent.mkdir(parents=True)
    tomb.write_text(json.dumps({'id': sid, 'tag': target.tag, 'workspace': str(work),
        'cause': 'finished', 'finished_at_ms': 5000,
        'exit': {'code': 75, 'signal': None, 'oom_killed': False, 'exited_at_ms': 5000}}))
    ok, evidence, _ = verify_native_retirement('unused', target, state_dir=str(tmp_path), sessions=[])
    assert ok
    assert not observe_verified_terminal(owner, dict(record, incarnation=2000), target, evidence)
    assert not calls
    assert observe_verified_terminal(owner, record, target, evidence)
    assert [c['status'] for c in calls] == ['unavailable']
    clock[0] += 400
    assert h.observe([], {})  # failing real probe cannot release
    healthy[0] = True
    clock[0] += 400
    assert not h.observe([], {})
    assert [c['status'] for c in calls] == ['unavailable', 'ok']
    assert not observe_verified_terminal(owner, record, target, evidence)
    assert [c['status'] for c in calls] == ['unavailable', 'ok']
    again = ZcodexHealth(lambda *args: calls.append(args), tmp_path / 'retired-health.json', threaded=False)
    assert not observe_verified_terminal(SimpleNamespace(_zcodex_health=again), record, target, evidence)
    assert len(calls) == 2


def test_verified_failure_hold_retries_after_rpc_failure_without_receipt_replay(tmp_path):
    from types import SimpleNamespace
    from agent_hub_host.native_sessions import NativeTarget
    from agent_hub_host.zcodex_health import observe_verified_terminal

    sid = 'ab7ac540-f991-4543-8580-5cce7136da1b'
    target = NativeTarget(sid, 'task-hub-proof', str(tmp_path), 1000, 'zcodex').validated()
    record = {'native_id': sid, 'tag': target.tag, 'workspace': str(tmp_path),
              'incarnation': 1000, 'engine': 'zcodex'}
    evidence = {'finished_at_ms': 5000, 'exit': {'code': 75}}
    def unavailable(*args):
        raise OSError('synthetic transport failure')
    state = tmp_path / 'pending.json'
    h = ZcodexHealth(unavailable, state, threaded=False)
    assert not h.observe([], {})
    assert observe_verified_terminal(SimpleNamespace(_zcodex_health=h), record, target, evidence)
    assert h.pending
    calls = []
    again = ZcodexHealth(lambda m, path, body=None: calls.append(body), state,
                         probe=lambda: False, threaded=False)
    assert again.observe([], {})  # no native row or receipt has to reappear
    assert not again.pending
    assert calls == [{'engine': 'zcodex', 'status': 'unavailable', 'reason': 'zcodex_session_failed'}]


def test_probe_started_before_new_failure_cannot_clear_the_new_hold(tmp_path):
    import threading
    calls, clock = [], [1000.0]
    started, release = threading.Event(), threading.Event()
    def probe():
        started.set()
        release.wait(timeout=10)
        return True
    h = ZcodexHealth(lambda m, path, body=None: calls.append(body), tmp_path / 'generation.json',
                     now=lambda: clock[0], probe=probe, threaded=True)
    try:
        assert not h.observe([], {})
        assert h.observe([row('old', exit={'code': 75})], {})
        clock[0] += 400
        assert h.observe([], {})
        assert started.wait(timeout=5)
        assert h.observe([row('new', exit={'code': 75})], {})
        release.set()
        h._thread.join(timeout=5)
        assert h.observe([], {})
        assert all(c['status'] == 'unavailable' for c in calls)
    finally:
        release.set()
        if h._thread is not None:
            h._thread.join(timeout=12)
