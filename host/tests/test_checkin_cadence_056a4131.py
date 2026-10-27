"""Intake 056a4131: host liveness check-in (heartbeat + capabilities PUT) on
its own <=60 s cadence, independent of slow cycle phases, and consistent with
the Hub's 180 s online window."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet, session_host
try:
    from host.tests.test_fleet import settings
except ImportError:  # PYTHONPATH=host/tests
    from test_fleet import settings

REPO = Path(__file__).resolve().parents[2]


def _gate_runner(argv, **kw):
    if argv and argv[0] == 'maintained-gate':
        return {'launch_allowed': True, 'profile_supported': True,
                'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
    return {'active_mem_mb': 512}


def test_checkin_interval_bounded_under_60s():
    assert fleet.CHECKIN_REFRESH_S <= 60, fleet.CHECKIN_REFRESH_S
    # The Hub online window covers both halves of the check-in.
    assert fleet.CHECKIN_REFRESH_S * 3 <= 180, fleet.CHECKIN_REFRESH_S
    # The session host loop must schedule the check-in tick from the same
    # constant (source check keeps the loop honest about its cadence).
    src = (REPO / 'host' / 'agent_hub_host' / 'session_host.py').read_text()
    assert 'fleet.CHECKIN_REFRESH_S' in src
    assert 'checkin_step_once' in src


def test_checkin_tick_posts_poll_and_capabilities_while_slow_cycle_holds_lock():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        cfg['capabilities'] = ['cap:demo']
        cfg['max_agents'] = 2
        cfg['subscriptions'][0]['launcher_model'] = 'gpt-6.1-sol'
        cfg['subscriptions'][0]['launcher_effort'] = 'high'
        posts = []

        def request(method, path, body=None):
            posts.append((method, path, body))
            return {}
        dispatcher = fleet.Dispatcher(cfg, request, _gate_runner, journal_dir=tmp)
        held = dispatcher._acquire_cycle_lock()  # slow cycle in progress
        assert held not in (False, None)
        try:
            with patch('agent_hub_host.api.get_heartbeat_payload',
                       return_value={'type': 'heartbeat', 'mem_avail_mb': 1}):
                hb = dispatcher.checkin_tick()
        finally:
            held.close()
        polls = [b for m, p, b in posts if p == '/api/host/poll']
        caps = [b for m, p, b in posts if p == '/api/fleet/routing/capabilities' and m == 'PUT']
        assert len(polls) == 1 and len(caps) == 1, posts
        # No admission gates or launcher reconciliation on the liveness path.
        assert 'launcher_resources' not in hb
        # The advertisement matches the slow cycle's payload shape.
        assert caps[0] == {'needs': ['cap:demo'], 'max_agents': 2, 'runners': [
            {'engine': 'codex', 'profile': None, 'account': 'shared-codex',
             'model': 'gpt-6.1-sol', 'effort': 'high'}]}
        # The check-in never takes over telemetry projection ownership.
        assert dispatcher._last_hb is None


def test_checkin_tick_carries_latest_projections_but_not_stale_snapshot_only():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        cfg['capabilities'] = ['cap:demo']
        cfg['max_agents'] = 2
        posts = []

        def request(method, path, body=None):
            posts.append((method, path, body))
            return {}
        dispatcher = fleet.Dispatcher(cfg, request, _gate_runner, journal_dir=tmp)
        dispatcher._last_hb = {'gate': {'launch_allowed': True, 'at': 'latest'},
                               'launcher_resources': {'active_mem_mb': 256},
                               'cpu_usage': {'observed_at': 'latest'}}
        with patch('agent_hub_host.api.get_heartbeat_payload',
                   return_value={'type': 'heartbeat', 'mem_avail_mb': 9}):
            hb = dispatcher.checkin_tick()
        poll = [b for m, p, b in posts if p == '/api/host/poll'][0]
        assert poll['mem_avail_mb'] == 9  # fresh base sample, not an old snapshot
        assert poll['gate'] == {'launch_allowed': True, 'at': 'latest'}
        assert poll['launcher_resources'] == {'active_mem_mb': 256}
        assert hb is not dispatcher._last_hb  # _last_hb ownership unchanged


def test_checkin_tick_skips_capabilities_without_full_settings():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))  # no capabilities/max_agents advertised
        posts = []

        def request(method, path, body=None):
            posts.append((method, path, body))
            return {}
        dispatcher = fleet.Dispatcher(cfg, request, _gate_runner, journal_dir=tmp)
        assert dispatcher.capabilities_put_payload() is None
        with patch('agent_hub_host.api.get_heartbeat_payload', return_value={'type': 'heartbeat'}):
            dispatcher.checkin_tick()
        assert [p for m, p, b in posts if m == 'PUT'] == [], posts
        assert [p for m, p, b in posts if p == '/api/host/poll']


def test_slow_cycle_capabilities_put_uses_shared_payload_helper():
    # The slow cycle and the fast check-in must advertise the same payload.
    import inspect
    src = inspect.getsource(fleet.Dispatcher._once_locked)
    assert 'self.capabilities_put_payload()' in src
    assert "'runners':" not in src, 'slow cycle must not carry a second payload derivation'


def test_session_host_schedules_checkin_step_on_own_thread():
    import inspect
    src = inspect.getsource(session_host.run)
    assert "checkin_step_once, (dispatcher,), 'fleet-checkin'" in src
    assert 'fleet.CHECKIN_REFRESH_S' in src
    step = inspect.getsource(session_host.checkin_step_once)
    assert 'checkin_tick' in step and 'checkin_error' in step
