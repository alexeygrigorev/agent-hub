"""Intake eff9a5ce: offered->bound latency — cycle profiling, non-fatal
telemetry, single-flight claims, benign already_claimed losers."""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import bus, fleet
from test_fleet import settings, offer


def _silent_cycle(cfg, request, runner, journal=None):
    """Run once() with the usual heavy seams stubbed; return (results, events)."""
    dispatcher = fleet.Dispatcher(cfg, request, runner,
                                  journal_dir=str(journal) if journal else None)
    buf = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch('agent_hub_host.provisioning.tick'))
        stack.enter_context(patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]))
        stack.enter_context(patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}}))
        stack.enter_context(patch('agent_hub_host.api.get_heartbeat_payload', return_value={}))
        stack.enter_context(patch('agent_hub_host.workclaim.sync_once', return_value={'ok': True}))
        stack.enter_context(patch('agent_hub_host.launcher_recovery.settle_retired_legacy', return_value=[]))
        stack.enter_context(patch.object(fleet, 'collect_subscriptions', return_value=[]))
        for method, value in [('report_catalog_bindings', []), ('reconcile_launches', []),
                              ('reconcile_imported_sessions', []), ('_derive_qualified_role_jobs', [])]:
            stack.enter_context(patch.object(dispatcher, method, return_value=value))
        with contextlib.redirect_stdout(buf):
            results = dispatcher.once()
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    return dispatcher, results, events


def _base_request_factory(calls, offers=(), fail_paths=()):
    def request(method, path, body=None):
        calls.append((method, path))
        if (method, path) in fail_paths:
            status = fail_paths[(method, path)]
            raise ValueError(f"HTTP request failed with status {status}: invalid_subscription:unmapped")
        if method == 'GET' and path == '/api/fleet/offers':
            return {'offers': list(offers)}
        return {}
    return request


def _base_runner(argv, **kw):
    if argv and argv[0] == 'maintained-gate':
        return {'launch_allowed': True, 'profile_supported': True,
                'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
    return []


def test_phases_published_with_expected_keys():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        calls = []
        dispatcher, results, events = _silent_cycle(
            cfg, _base_request_factory(calls), _base_runner, journal=Path(tmp))
        phase_events = [e for e in events if e.get('event') == 'fleet_cycle_phases']
        assert len(phase_events) == 1, events
        phases = phase_events[0]['phases']
        for key in ('provisioning', 'system', 'register', 'admission', 'heartbeat',
                    'probes', 'subscriptions', 'catalog', 'workclaim', 'capabilities',
                    'legacy', 'reconcile', 'watchdog', 'unconfigured_probes',
                    'offers', 'dispatch'):
            assert key in phases, (key, sorted(phases))
            assert isinstance(phases[key], (int, float)), (key, phases[key])
        assert phase_events[0]['offers'] == 0
        assert set(dispatcher.last_cycle_phases) == set(phases)


def test_subscriptions_400_is_nonfatal_and_offers_still_dispatched():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        calls = []
        item = offer()
        dispatched = []
        request = _base_request_factory(
            calls, offers=[item],
            fail_paths={('PUT', '/api/fleet/subscriptions'): 400})
        dispatcher = fleet.Dispatcher(cfg, request, _base_runner, journal_dir=str(Path(tmp)))
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('agent_hub_host.provisioning.tick'))
            stack.enter_context(patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]))
            stack.enter_context(patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}}))
            stack.enter_context(patch('agent_hub_host.api.get_heartbeat_payload', return_value={}))
            stack.enter_context(patch('agent_hub_host.workclaim.sync_once', return_value={'ok': True}))
            stack.enter_context(patch('agent_hub_host.launcher_recovery.settle_retired_legacy', return_value=[]))
            stack.enter_context(patch.object(fleet, 'collect_subscriptions', return_value=[]))
            for method, value in [('reconcile_launches', []), ('reconcile_imported_sessions', []),
                                  ('_derive_qualified_role_jobs', [])]:
                stack.enter_context(patch.object(dispatcher, method, return_value=value))
            stack.enter_context(patch.object(dispatcher, 'report_catalog_bindings', return_value=[]))
            stack.enter_context(patch.object(
                dispatcher, 'dispatch',
                side_effect=lambda current: dispatched.append(current['id']) or {'id': current['id'], 'state': 'held', 'reason': 'test'}))
            with contextlib.redirect_stdout(buf):
                results = dispatcher.once()
        events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        skipped = [e for e in events if e.get('event') == 'fleet_telemetry_skipped']
        assert any(s['phase'] == 'subscriptions' and 'invalid_subscription' in s['error'] for s in skipped), events
        assert [m for m, p in calls if p == '/api/fleet/offers'], calls
        assert dispatched == [item['id']], dispatched
        assert any(r.get('id') == item['id'] for r in results), results


def test_catalog_failure_is_nonfatal():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        calls = []
        dispatcher = fleet.Dispatcher(cfg, _base_request_factory(calls), _base_runner,
                                      journal_dir=str(Path(tmp)))
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('agent_hub_host.provisioning.tick'))
            stack.enter_context(patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]))
            stack.enter_context(patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}}))
            stack.enter_context(patch('agent_hub_host.api.get_heartbeat_payload', return_value={}))
            stack.enter_context(patch('agent_hub_host.workclaim.sync_once', return_value={'ok': True}))
            stack.enter_context(patch('agent_hub_host.launcher_recovery.settle_retired_legacy', return_value=[]))
            stack.enter_context(patch.object(fleet, 'collect_subscriptions', return_value=[]))
            for method, value in [('reconcile_launches', []), ('reconcile_imported_sessions', []),
                                  ('_derive_qualified_role_jobs', [])]:
                stack.enter_context(patch.object(dispatcher, method, return_value=value))
            stack.enter_context(patch.object(
                dispatcher, 'report_catalog_bindings',
                side_effect=ValueError('HTTP request failed with status 400: invalid_snapshot')))
            with contextlib.redirect_stdout(buf):
                results = dispatcher.once()
        events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        assert any(e.get('event') == 'fleet_telemetry_skipped' and e.get('phase') == 'catalog'
                   for e in events), events
        assert any(e.get('event') == 'fleet_cycle_phases' for e in events), events
        assert [m for m, p in calls if p == '/api/fleet/offers'], calls


def test_already_claimed_loser_holds_without_reject():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        calls = []
        item = offer()

        def request(method, path, body=None):
            calls.append((method, path))
            if method == 'GET' and path == '/api/fleet/offers':
                return {'offers': [item]}
            return {}

        def dispatch(current):
            raise ValueError('HTTP request failed with status 409: already_claimed')

        dispatcher = fleet.Dispatcher(cfg, request, _base_runner, journal_dir=str(Path(tmp)))
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('agent_hub_host.provisioning.tick'))
            stack.enter_context(patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]))
            stack.enter_context(patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}}))
            stack.enter_context(patch('agent_hub_host.api.get_heartbeat_payload', return_value={}))
            stack.enter_context(patch('agent_hub_host.workclaim.sync_once', return_value={'ok': True}))
            stack.enter_context(patch('agent_hub_host.launcher_recovery.settle_retired_legacy', return_value=[]))
            stack.enter_context(patch.object(fleet, 'collect_subscriptions', return_value=[]))
            for method, value in [('report_catalog_bindings', []), ('reconcile_launches', []),
                                  ('reconcile_imported_sessions', []), ('_derive_qualified_role_jobs', [])]:
                stack.enter_context(patch.object(dispatcher, method, return_value=value))
            stack.enter_context(patch.object(dispatcher, 'dispatch', side_effect=dispatch))
            with contextlib.redirect_stdout(buf):
                results = dispatcher.once()
        outcome = next(r for r in results if r.get('id') == item['id'])
        assert outcome == {'id': item['id'], 'state': 'held', 'reason': 'already_claimed'}, outcome
        assert not any(p.endswith('/reject') for _, p in calls), calls


def test_concurrent_cycle_skips_with_receipt_and_releases():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        calls = []
        first, _, _ = _silent_cycle(cfg, _base_request_factory(calls), _base_runner,
                                    journal=Path(tmp))
        marked = len(calls)
        # Hold the lock manually to simulate the sibling loop owning offers.
        held = first._acquire_cycle_lock()
        assert held not in (False, None)
        try:
            second = fleet.Dispatcher(cfg, _base_request_factory(calls), _base_runner,
                                      journal_dir=str(Path(tmp)))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                results = second.once()
            assert results == [{'id': 'cycle', 'state': 'skipped',
                                'reason': 'concurrent_cycle'}], results
            events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            assert any(e.get('event') == 'fleet_cycle_skipped' for e in events), events
            assert not any(m == 'GET' and p == '/api/fleet/offers'
                           for m, p in calls[marked:]), calls[marked:]
        finally:
            held.close()
        before = len(calls)
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('agent_hub_host.provisioning.tick'))
            stack.enter_context(patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]))
            stack.enter_context(patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}}))
            stack.enter_context(patch('agent_hub_host.api.get_heartbeat_payload', return_value={}))
            stack.enter_context(patch('agent_hub_host.workclaim.sync_once', return_value={'ok': True}))
            stack.enter_context(patch('agent_hub_host.launcher_recovery.settle_retired_legacy', return_value=[]))
            stack.enter_context(patch.object(fleet, 'collect_subscriptions', return_value=[]))
            for method, value in [('report_catalog_bindings', []), ('reconcile_launches', []),
                                  ('reconcile_imported_sessions', []), ('_derive_qualified_role_jobs', [])]:
                stack.enter_context(patch.object(second, method, return_value=value))
            with contextlib.redirect_stdout(buf):
                results = second.once()
        assert any(m == 'GET' and p == '/api/fleet/offers' for m, p in calls[before:]), calls[before:]
        assert not any(r.get('reason') == 'concurrent_cycle' for r in results), results


def test_journal_less_dispatchers_keep_legacy_behavior():
    cfg = settings(Path('/repo'))
    dispatcher = fleet.Dispatcher(cfg, lambda *a, **k: {}, lambda *a, **k: {})
    assert dispatcher._cycle_lock_path() is None
    assert dispatcher._acquire_cycle_lock() is None


def test_role_jobs_cache_hit_and_file_revision_invalidation():
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        roles_path = Path(tmp) / 'roles.json'
        roles_path.write_text(_json.dumps({"version": 1, "enabled": True, "profiles": {}}))
        cfg = settings(Path(tmp))
        dispatcher = fleet.Dispatcher(cfg, lambda *a, **k: {}, lambda *a, **k: {},
                                      role_config_path=str(roles_path))
        with patch.object(fleet.Dispatcher, '_derive_qualified_role_jobs_uncached',
                          return_value=['principal-wake']) as uncached:
            assert dispatcher._derive_qualified_role_jobs() == ['principal-wake']
            assert dispatcher._derive_qualified_role_jobs() == ['principal-wake']
            assert uncached.call_count == 1
            # Any file revision (here: different size) invalidates the cache.
            roles_path.write_text(_json.dumps({"version": 1, "enabled": True,
                                               "profiles": {}, "note": "rev2"}))
            assert dispatcher._derive_qualified_role_jobs() == ['principal-wake']
            assert uncached.call_count == 2


def test_bus_body_detail_extracts_error_slugs_only():
    assert bus._sanitized_body_detail(b'{"error":"invalid_subscription","reason":"unmapped"}') == \
        ': invalid_subscription:unmapped'
    assert bus._sanitized_body_detail(b'{"error":"already_claimed"}') == ': already_claimed'
    assert bus._sanitized_body_detail(b'not json') == ''
    assert bus._sanitized_body_detail(b'{"error":"ok","token":"SECRET-shape with spaces"}') == ': ok'
    assert bus._sanitized_body_detail(b'{"error":"a' + b'x' * 200 + b'"}') == ''
