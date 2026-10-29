"""Intake 6cbb862a: the host posts the routes Dispatcher._route() accepts before every heartbeat."""
import json
from unittest.mock import patch

EXPECTED = [{'engine': 'codex', 'account': 'codex-orin', 'profile': 'hub-codex-orin'},
            {'engine': 'claude', 'account': 'local-claude', 'profile': None}]


def _cfg(tmp_path, subs=None):
    return {'version': 1, 'workspace_roots': [], 'workspaces': {'demo': str(tmp_path)}, 'aplexer_state_dir': str(tmp_path),
            'subscriptions': subs if subs is not None else [
                {'service': 'codex', 'account_label': 'codex-orin', 'plan': 'pro', 'engine': 'codex',
                 'profile': 'hub-codex-orin', 'admission_argv': ['maintained-gate']},
                {'service': 'claude', 'account_label': 'local-claude', 'plan': 'max', 'engine': 'claude', 'profile': None}]}


def _runner(fleet):
    def runner(argv, **kw):
        if argv[0] == 'maintained-gate':
            return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'codex-orin', 'observed_at': fleet.utcnow()}
        return [] if argv[0] == 'aplexer' else {}
    return runner


class _Host:
    """System, heartbeat telemetry and provisioning stubs for a Dispatcher under test."""
    def __init__(self, heartbeat=None):
        hb = heartbeat or {'type': 'heartbeat'}
        self.ps = [patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}, 'hostname': 't', 'os': 'test', 'network': 'home', 'roles': []}),
                   patch('agent_hub_host.api.get_heartbeat_payload', side_effect=lambda *a, **k: json.loads(json.dumps(hb))),
                   patch('agent_hub_host.provisioning.tick', return_value={'status': 'synced'}),
                   patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[])]

    def __enter__(self):
        for p in self.ps:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.stop()


def test_routes_are_posted_before_every_heartbeat_and_heartbeats_carry_none(tmp_path):
    """Hub offers only routes in the latest /api/host/routes post, matching Dispatcher._route()."""
    from agent_hub_host import fleet
    calls = []

    def request(method, path, body=None):
        calls.append((path, body))
        return {'offers': []}
    with _Host():
        d = fleet.Dispatcher(_cfg(tmp_path), request, _runner(fleet))
        d.once()
        d.checkin_tick()
    paths = [p for p, _ in calls]
    polls = [i for i, p in enumerate(paths) if p == '/api/host/poll']
    routes = [i for i, p in enumerate(paths) if p == '/api/host/routes']
    assert len(polls) >= 2 and routes and routes[0] < polls[0]
    assert all(calls[i][1] == {'fleet_routes': EXPECTED} for i in routes)
    # Heartbeat bodies are exactly what they were without route advertisement.
    assert not any({'fleet_routes', 'fleet_routes_overflow', 'fleet_routes_at'} & set(calls[i][1]) for i in polls)


def test_a_changed_list_is_posted_before_the_next_heartbeat_and_an_unchanged_one_refreshed(tmp_path):
    from agent_hub_host import fleet
    calls = []
    clock = [1000.0]

    def request(method, path, body=None):
        calls.append((path, body))
        return {'offers': []}
    with _Host(), patch('agent_hub_host.fleet.time.monotonic', side_effect=lambda: clock[0]):
        d = fleet.Dispatcher(_cfg(tmp_path), request, _runner(fleet))
        d.checkin_tick()
        d.checkin_tick()
        assert [p for p, _ in calls].count('/api/host/routes') == 1  # unchanged within the refresh window
        clock[0] += fleet.ROUTE_ADVERTISEMENT_REFRESH_S
        d.checkin_tick()
        assert [p for p, _ in calls].count('/api/host/routes') == 2  # refreshed well inside Hub's 180 s TTL
        assert fleet.ROUTE_ADVERTISEMENT_REFRESH_S * 2 < 180
        del calls[:]
        d.settings['subscriptions'] = [s for s in d.settings['subscriptions'] if s['account_label'] != 'codex-orin']
        d.checkin_tick()
    assert [p for p, _ in calls][:2] == ['/api/host/routes', '/api/host/poll']
    assert calls[0][1] == {'fleet_routes': EXPECTED[1:]}


def test_a_failed_route_post_skips_the_heartbeat_and_its_offers(tmp_path):
    """Review d2b92d22: never a heartbeat (offers) without the current explicit route state."""
    import pytest
    from agent_hub_host import fleet
    calls = []

    def request(method, path, body=None):
        calls.append(path)
        if path == '/api/host/routes':
            raise OSError('hub unreachable')
        return {'offers': []}
    with _Host():
        d = fleet.Dispatcher(_cfg(tmp_path), request, _runner(fleet))
        with pytest.raises(OSError):
            d.checkin_tick()
    assert '/api/host/poll' not in calls


def test_near_limit_telemetry_still_gets_explicit_route_state(tmp_path):
    """Review d2b92d22: the 32,750-byte supported heartbeat is sent unchanged and the routes still reach Hub."""
    from agent_hub_host import fleet
    base = _telemetry()
    assert len(json.dumps(base).encode('utf-8')) == 32750
    calls = []

    def request(method, path, body=None):
        calls.append((path, body))
        return {'offers': []}
    with _Host(heartbeat=base):
        d = fleet.Dispatcher(_cfg(tmp_path), request, _runner(fleet))
        d.checkin_tick()
    assert calls[0] == ('/api/host/routes', {'fleet_routes': EXPECTED})
    poll = next(b for p, b in calls if p == '/api/host/poll')
    assert not {'fleet_routes', 'fleet_routes_overflow'} & set(poll)
    assert poll['launcher_resources'] == base['launcher_resources']


def _subs(n):
    return [{'service': 'codex', 'account_label': f'acct-{i}', 'plan': 'pro', 'engine': 'codex', 'profile': f'hub-acct-{i}', 'admission_argv': ['maintained-gate']}
            for i in range(n)]


def _dispatcher(fleet, tmp_path, calls):
    def request(method, path, body=None):
        calls.append((path, body))
        return {}
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        return fleet.Dispatcher(_cfg(tmp_path, _subs(1)), request, lambda *a, **k: {})


def test_route_post_is_complete_at_64_65_128_routes(tmp_path):
    """Review 89766b23: 65 dispatchable routes advertised only 64, so Hub held the 65th forever."""
    from agent_hub_host import fleet
    assert fleet.MAX_ADVERTISED_ROUTES == 128
    for n in (64, 65, 128):
        calls = []
        d = _dispatcher(fleet, tmp_path, calls)
        d.settings['subscriptions'] = _subs(n)
        d.post_route_advertisement()
        assert calls[0][0] == '/api/host/routes' and len(calls[0][1]['fleet_routes']) == n
        for i in (0, n - 1):
            assert {'engine': 'codex', 'account': f'acct-{i}', 'profile': f'hub-acct-{i}'} in calls[0][1]['fleet_routes']
            assert d._route({'account': f'acct-{i}', 'engine': 'codex', 'profile': f'hub-acct-{i}', 'workspace': 'demo'}) is not None


def test_route_post_past_the_count_cap_is_an_explicit_overflow(tmp_path):
    """Local + managed routes past the cap: an overflow count, never a truncated list."""
    from agent_hub_host import fleet
    calls = []
    d = _dispatcher(fleet, tmp_path, calls)
    d.settings['subscriptions'] = _subs(129) + _subs(3)  # duplicates count once
    d.post_route_advertisement()
    assert calls == [('/api/host/routes', {'fleet_routes_overflow': 129})]


def test_largest_route_post_fits_the_hub_body_limit(tmp_path):
    """128 routes with 64-character engine, account and profile labels stay under 32 KiB."""
    from agent_hub_host import fleet
    calls = []
    d = _dispatcher(fleet, tmp_path, calls)
    label = lambda p, i: (p + str(i)).ljust(64, 'x')
    d.settings['subscriptions'] = [{'service': 'codex', 'account_label': label('a', i), 'engine': label('e', i), 'profile': label('p', i)}
                                   for i in range(128)]
    d.post_route_advertisement()
    body = calls[0][1]
    assert len(body['fleet_routes']) == 128
    assert len(json.dumps(body).encode('utf-8')) < 32768


def _telemetry(path_len=476, aliases=64):
    """Review 4224a469 fixture: supported launcher telemetry near Hub's 32 KiB body limit."""
    parts = [159, 159, path_len - 3 - 318]
    path = '/' + '/'.join(c * n for c, n in zip('def', parts))
    names = ['ws-%02d' % i for i in range(aliases)]
    names[0] += 'x' * 32
    at = '2026-10-29T20:00:00Z'
    return {'type': 'heartbeat', 'mem_avail_mb': 32000, 'disk_free_gb': 80, 'load': [1.0, 1.0, 1.0],
            'gate': {'launch_allowed': True, 'at': at}, 'running_jobs': [], 'sessions_summary': {},
            'launcher_resources': {'active_mem_mb': 0, 'observed_at': at, 'active_disk_mb': 0, 'disk_floor_mb': 20480,
                                   'next_disk_mb': 512, 'disk_paths': [{'path': path, 'free_gb': 80}, {'path': path + '/.local/tmp', 'free_gb': 80}],
                                   'disk_aliases': {n: path for n in names}, 'disk_task_ids': []}}
