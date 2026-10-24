"""Offered launches must not silently survive a completed controller cycle."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_hub_host import fleet, session_host


class OfferedRefusals(unittest.TestCase):
    def cycle(self, root, offers, reject_error=None, dispatch_error=None):
        settings = {'version': 1, 'workspaces': {'demo': str(root)},
                    'subscriptions': [{'service': 'zai', 'engine': 'zcodex',
                                       'account_label': 'shared-zai', 'profile': None}]}
        calls = []
        def request(method, path, body=None):
            calls.append((method, path, body))
            if path == '/api/fleet/offers':
                return {'offers': offers}
            if path.endswith('/reject'):
                if reject_error:
                    raise reject_error
                return {'id': path.split('/')[-2], 'state': 'failed', **body}
            return {}
        runner = Mock(return_value={})
        dispatcher = fleet.Dispatcher(settings, request, runner, journal_dir=root)
        with contextlib.ExitStack() as stack:
            for name, value in [('provisioning.tick', None), ('provisioning.managed_subscriptions', []),
                                ('system.get_system_info', {'capabilities': {}}),
                                ('api.get_heartbeat_payload', {}), ('workclaim.sync_once', {'ok': True}),
                                ('launcher_recovery.settle_retired_legacy', [])]:
                stack.enter_context(patch('agent_hub_host.' + name, return_value=value))
            stack.enter_context(patch.object(fleet, 'collect_subscriptions', return_value=[]))
            for method, value in [('report_catalog_bindings', []), ('reconcile_launches', []),
                                  ('reconcile_imported_sessions', []), ('_derive_qualified_role_jobs', [])]:
                stack.enter_context(patch.object(dispatcher, method, return_value=value))
            if dispatch_error:
                stack.enter_context(patch.object(dispatcher, 'dispatch', side_effect=dispatch_error))
            results = dispatcher.once()
        runner.assert_not_called()
        return results, calls

    def offer(self, mode, **extra):
        return {'id': '12345678-1234-1234-1234-123456789abc', 'mode': mode,
                'engine': 'zcodex', 'account': 'shared-zai', 'profile': None,
                'workspace': 'demo', 'prompt': 'private task',
                'selection': {'id': 'glm-flash', 'model': 'glm-5.3-flash', 'effort': 'high'},
                **extra}

    def test_missing_adapter_is_refused_in_one_cycle_for_both_incident_modes(self):
        for mode in ('headless', 'interactive'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                offer = self.offer(mode, **({'with_goal': True} if mode == 'interactive' else {}))
                results, calls = self.cycle(Path(tmp), [offer])
                self.assertEqual(results[-1], {'id': offer['id'], 'state': 'failed',
                                               'reason': 'admission_unconfigured'})
                self.assertEqual([p for _, p, _ in calls if p.endswith('/claim')], [])
                self.assertEqual([b for _, p, b in calls if p.endswith('/reject')],
                                 [{'reason': 'admission_unconfigured'}])

    def test_unknown_failure_is_redacted_and_does_not_starve_later_offers(self):
        with tempfile.TemporaryDirectory() as tmp:
            offers = [self.offer('headless'), self.offer('interactive', id='22345678-1234-1234-1234-123456789abc')]
            results, calls = self.cycle(Path(tmp), offers, dispatch_error=RuntimeError('Bearer PRIVATE goal /path'))
            self.assertEqual([r['reason'] for r in results[1:]], ['dispatch_refused'] * 2)
            self.assertNotIn('PRIVATE', json.dumps([results, calls]))

    def test_lost_rejection_or_already_claimed_response_is_not_exit_proof(self):
        for error in (TimeoutError('transport'), ValueError('409 already_claimed')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                results, _ = self.cycle(Path(tmp), [self.offer('headless')], reject_error=error)
                self.assertEqual(results[-1]['state'], 'held')
                self.assertEqual(results[-1]['reason'], 'admission_unconfigured')

    def test_routed_jobs_keep_their_separate_report_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, calls = self.cycle(Path(tmp), [self.offer('headless', task_id='task')])
            self.assertFalse(any(p.endswith('/reject') for _, p, _ in calls))

    def test_controller_logs_refusal_slug_without_raw_payload(self):
        dispatcher = Mock()
        dispatcher.once.return_value = [{'id': 'private-id', 'state': 'failed',
                                         'reason': 'admission_unconfigured', 'error': 'PRIVATE'}]
        collector = Mock(last_collect_monotonic=0, last_hold=None, last_error=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            session_host.maintenance_step_once(dispatcher, collector)
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertIn({'event': 'fleet_dispatch_refused', 'state': 'failed',
                       'reason': 'admission_unconfigured'}, events)
        self.assertNotIn('private', out.getvalue().lower())
