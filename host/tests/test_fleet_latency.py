"""Binding follows each confirmed start, before another offer or poll interval."""
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from agent_hub_host import fleet
from test_fleet import settings, offer


@pytest.mark.parametrize('catalog_failure', [False, True])
def test_new_launch_binds_in_same_cycle_and_catalog_failure_retains_running(catalog_failure):
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp)); events = []; active = False
        item = offer()
        row = {'id': '11111111-1111-4111-8111-111111111111', 'tag': 'hub-' + item['id'], 'engine': 'codex', 'workspace': tmp, 'created_at_ms': 1790000000000, 'phase': 'running', 'profile': None}
        def request(method, path, body=None):
            events.append(path)
            if path == '/api/fleet/sessions' and active and catalog_failure:
                raise ValueError('temporary catalog transport failure')
            if path == '/api/sessions/host-bindings':
                return {'sessions': [{'id': 'canonical', 'fleet_launch_id': item['id'], 'engine': 'codex', 'native_id': row['id']}] if active else []}
            if path == '/api/fleet/offers': return {'offers': [item, dict(item, id='second')] if not active else []}
            return {}
        def runner(argv, **kw):
            if argv[0] == 'maintained-gate': return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
            return [row] if active else []
        def dispatch(current):
            nonlocal active
            if current['id'] == 'second':
                if not catalog_failure: assert '/api/sessions/canonical/binding' in events
                return {'id': 'second', 'state': 'held'}
            active = True
            return {'id': current['id'], 'state': 'running', 'native_id': row['id']}
        with patch('agent_hub_host.provisioning.tick'), patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]), patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}}), patch('agent_hub_host.api.get_heartbeat_payload', return_value={}), patch('agent_hub_host.workclaim.sync_once', return_value={'ok': True}):
            d = fleet.Dispatcher(cfg, request, runner)
            with patch.object(d, 'dispatch', side_effect=dispatch) as starts, patch.object(d, 'reconcile_launches', return_value=[]), patch.object(d, 'reconcile_imported_sessions', return_value=[]):
                result = d.once()
                assert result[1]['state'] == 'running'
                assert starts.call_count == 2
                if catalog_failure:
                    assert any(r.get('state') == 'binding_pending' for r in result)
                    catalog_failure = False
                    events.clear()
                    d.once()
                    assert '/api/sessions/canonical/binding' in events
                    assert starts.call_count == 2  # reconciliation never repeats native start
                else:
                    assert '/api/sessions/canonical/binding' in events
