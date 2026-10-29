"""Intake 6cbb862a: every heartbeat advertises the routes Dispatcher._route() accepts."""
from unittest.mock import patch


def test_every_heartbeat_advertises_every_dispatchable_route(tmp_path):
    """Hub offers only routes in the latest heartbeat (fleet_routes), matching Dispatcher._route()."""
    from agent_hub_host import fleet
    cfg = {'version': 1, 'workspace_roots': [], 'workspaces': {'demo': str(tmp_path)}, 'aplexer_state_dir': str(tmp_path),
           'subscriptions': [
               {'service': 'codex', 'account_label': 'codex-orin', 'plan': 'pro', 'engine': 'codex',
                'profile': 'hub-codex-orin', 'admission_argv': ['maintained-gate']},
               {'service': 'claude', 'account_label': 'local-claude', 'plan': 'max', 'engine': 'claude', 'profile': None}]}
    calls = []
    def request(method, path, body=None):
        calls.append((method, path, body))
        return {'offers': []}
    def runner(argv, **kw):
        if argv[0] == 'maintained-gate':
            return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'codex-orin', 'observed_at': fleet.utcnow()}
        return [] if argv[0] == 'aplexer' else {}
    expected = [{'engine': 'codex', 'account': 'codex-orin', 'profile': 'hub-codex-orin'},
                {'engine': 'claude', 'account': 'local-claude', 'profile': None}]
    with patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}, 'hostname': 't', 'os': 'test', 'network': 'home', 'roles': []}), \
         patch('agent_hub_host.api.get_heartbeat_payload', side_effect=lambda *a, **k: {'type': 'heartbeat'}), \
         patch('agent_hub_host.provisioning.tick', return_value={'status': 'synced'}), \
         patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(cfg, request, runner)
        d.once()
        d.checkin_tick()
    polls = [c[2] for c in calls if c[1] == '/api/host/poll']
    assert len(polls) >= 2
    assert all(hb['fleet_routes'] == expected for hb in polls)


def _subs(n):
    return [{'service': 'codex', 'account_label': f'acct-{i}', 'plan': 'pro', 'engine': 'codex', 'profile': f'hub-acct-{i}', 'admission_argv': ['maintained-gate']}
            for i in range(n)]


def test_advertisement_is_never_truncated_at_64_65_128_routes(tmp_path):
    """Review 89766b23: 65 dispatchable routes advertised only 64, so Hub held the 65th forever."""
    from agent_hub_host import fleet
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        _check_sizes(fleet, tmp_path)


def _check_sizes(fleet, tmp_path):
    for n in (64, 65, 128):
        d = fleet.Dispatcher({'version': 1, 'workspace_roots': [], 'workspaces': {'demo': str(tmp_path)}, 'subscriptions': _subs(n)},
                             lambda *a, **k: {}, lambda *a, **k: {})
        hb = d.attach_route_advertisement({'type': 'heartbeat'})
        assert 'fleet_routes_overflow' not in hb and len(hb['fleet_routes']) == n
        for i in (0, n - 1):
            assert {'engine': 'codex', 'account': f'acct-{i}', 'profile': f'hub-acct-{i}'} in hb['fleet_routes']
            assert d._route({'account': f'acct-{i}', 'engine': 'codex', 'profile': f'hub-acct-{i}', 'workspace': 'demo'}) is not None
    assert fleet.MAX_ADVERTISED_ROUTES == 128


def _dispatcher(fleet, tmp_path):
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        return fleet.Dispatcher({'version': 1, 'workspace_roots': [], 'workspaces': {'demo': str(tmp_path)}, 'subscriptions': _subs(1)},
                                lambda *a, **k: {}, lambda *a, **k: {})


def test_advertisement_past_the_count_cap_is_an_explicit_overflow(tmp_path):
    """Local + managed routes past the cap: an overflow count, never a truncated list."""
    from agent_hub_host import fleet
    d = _dispatcher(fleet, tmp_path)
    d.settings['subscriptions'] = _subs(129) + _subs(3)  # duplicates count once
    assert len(d.route_advertisement()) == 129
    hb = d.attach_route_advertisement({'type': 'heartbeat'})
    assert hb == {'type': 'heartbeat', 'fleet_routes_overflow': 129}
    d.settings['subscriptions'] = _subs(2)
    assert d.attach_route_advertisement(hb) == {'type': 'heartbeat', 'fleet_routes': d.route_advertisement()}


def test_heartbeat_with_routes_stays_under_the_hub_body_limit(tmp_path):
    """Review 91bf3c43: 128 long-label routes plus large launcher telemetry exceeded Hub's 32 KiB body limit."""
    import json
    from agent_hub_host import fleet
    d = _dispatcher(fleet, tmp_path)
    long = lambda p, i: (p + str(i)).ljust(64, 'x')
    d.settings['subscriptions'] = [{'service': 'codex', 'account_label': long('a', i), 'engine': 'codex', 'profile': long('p', i)} for i in range(128)]
    path = '/' + '/'.join(['d' * 199, 'e' * 199])
    telemetry = {'type': 'heartbeat', 'launcher_resources': {'workspaces': {f'ws-{i}': path for i in range(24)},
                                                             'disk_paths': [path, path]}}
    without = len(json.dumps(telemetry).encode())
    hb = d.attach_route_advertisement(dict(telemetry))
    assert hb['fleet_routes_overflow'] == 128 and 'fleet_routes' not in hb
    assert len(json.dumps(hb).encode()) < 32768 and len(json.dumps(hb).encode()) - without < 64
    # The same 128 long routes with ordinary telemetry fit, complete.
    small = d.attach_route_advertisement({'type': 'heartbeat'})
    assert len(small['fleet_routes']) == 128 and len(json.dumps(small).encode()) <= fleet.HEARTBEAT_ROUTE_BODY_LIMIT < 32768
