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


def test_heartbeat_with_routes_never_exceeds_the_hub_body_limit(tmp_path):
    """Review 91bf3c43 / 4224a469: neither the list nor the overflow fallback may push a heartbeat past 32 KiB."""
    import json
    from agent_hub_host import fleet
    d = _dispatcher(fleet, tmp_path)
    size = lambda hb: len(json.dumps(hb).encode('utf-8'))
    assert fleet.HEARTBEAT_BODY_LIMIT == 32768
    long = lambda p, i: (p + str(i)).ljust(64, 'x')
    long_routes = [{'service': 'codex', 'account_label': long('a', i), 'engine': 'codex', 'profile': long('p', i)} for i in range(128)]

    # Reviewer's exact case: 32,750-byte telemetry, one short route. Neither the
    # list nor the overflow marker fits, so the heartbeat is sent unchanged.
    base = _telemetry()
    assert size(base) == 32750
    d.settings['subscriptions'] = [{'service': 'codex', 'account_label': 'a', 'engine': 'codex', 'profile': None}]
    hb = d.attach_route_advertisement(json.loads(json.dumps(base)))
    assert hb == base and size(hb) <= 32768

    # Near-limit telemetry with 128 long routes: the list does not fit, the overflow marker does.
    base = _telemetry(aliases=60)
    assert 32768 - 64 > size(base) > 32768 - 29000
    d.settings['subscriptions'] = long_routes
    hb = d.attach_route_advertisement(json.loads(json.dumps(base)))
    assert hb == {**base, 'fleet_routes_overflow': 128} and size(hb) <= 32768

    # Already-oversized telemetry: never grows.
    base = _telemetry(aliases=64, path_len=600)
    assert size(base) > 32768
    hb = d.attach_route_advertisement(json.loads(json.dumps(base)))
    assert hb == base

    # Ordinary telemetry: the complete 128-route list fits.
    small = d.attach_route_advertisement({'type': 'heartbeat'})
    assert len(small['fleet_routes']) == 128 and size(small) <= 32768

    # Every fallback step keeps the body bounded for any supported telemetry size that fit before.
    for aliases in range(1, 65, 3):
        base = _telemetry(aliases=aliases)
        for subs in (long_routes, _subs(129), _subs(3)):
            d.settings['subscriptions'] = subs
            hb = d.attach_route_advertisement(json.loads(json.dumps(base)))
            assert size(hb) <= max(32768, size(base))
            assert ('fleet_routes' in hb) + ('fleet_routes_overflow' in hb) <= 1
            if 'fleet_routes' in hb:
                assert len(hb['fleet_routes']) == len({(x['engine'], x['account_label'], x['profile']) for x in subs})
