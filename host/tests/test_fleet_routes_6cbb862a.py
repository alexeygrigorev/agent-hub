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
