"""Intake 7db967f6: the register request must never kill the fleet-host cycle.

registry.ts answers POST /api/machines/register with 400 payload_too_large
once JSON.stringify(capabilities).length passes the server budget (4096
before 0917add9, 16384 since). The cycle's first Hub request died at that
400 ahead of the /api/host/poll heartbeat, so host check-ins went stale and
unclaimed stops expired as stop_host_unreachable (207 journal failures on
2026-10-27; live RMTHZ capabilities sample 4592 chars).
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet


def register_settings(roots, workspaces):
    return {'version': 1, 'workspace_roots': list(roots), 'workspaces': dict(workspaces),
            'subscriptions': [
                {'service': 'codex', 'account_label': 'shared-codex', 'plan': 'pro',
                 'engine': 'codex', 'profile': None, 'admission_argv': ['maintained-gate']}]}


def oversized_settings():
    """Hetzner-like layout: many long per-worktree workspaces, few roots."""
    roots = [f'/home/alexey/git/repo-{n}' for n in range(3)]
    workspaces = {f'wt-{n:03d}': f'/home/alexey/git/agent-hub-wt-sibling-{n:03d}-{"x" * 150}'
                  for n in range(93)}
    return register_settings(roots, workspaces), roots


def oversized_capabilities():
    cfg, roots = oversized_settings()
    caps = {
        'engines': ['codex'],
        'workspaces': fleet.advertised_workspaces(cfg),
        'cpu_count': 12,
        'fleet_modes': ['interactive', 'headless'],
        'launcher_resources': True,
        'fleet_goal_routes': [{'engine': 'codex', 'account': f'acct-{n}',
                               'profile': f'hub-acct-{n}'} for n in range(48)],
        'fleet_route_rejections': [{'engine': 'claude', 'account': 'native-claude',
                                    'profile': None, 'mode': 'headless',
                                    'reason': 'model_unsupported'}],
    }
    return caps, roots


class RegisterBudgetTests(unittest.TestCase):
    def test_oversized_capabilities_trim_under_budget_and_keep_roots(self):
        caps, roots = oversized_capabilities()
        self.assertGreater(fleet._capabilities_json_size(caps), fleet.REGISTER_CAPABILITIES_BUDGET)
        bounded, meta = fleet.register_capabilities_within_budget(caps, roots)
        self.assertIsNotNone(meta)
        self.assertEqual(meta['before'], fleet._capabilities_json_size(caps))
        self.assertLessEqual(meta['after'], fleet.REGISTER_CAPABILITIES_BUDGET)
        self.assertIn('fleet_route_rejections', meta['dropped'])
        for root in roots:
            self.assertIn(root, bounded['workspaces'])
        self.assertNotIn('fleet_route_rejections', bounded)
        # Compact serialization mirrors the server's JSON.stringify length.
        self.assertEqual(len(json.dumps(bounded, separators=(',', ':'))), meta['after'])

    def test_fitting_capabilities_pass_through_untouched(self):
        caps = {'engines': ['codex'], 'workspaces': ['/repo']}
        bounded, meta = fleet.register_capabilities_within_budget(caps, ['/repo'])
        self.assertIsNone(meta)
        self.assertEqual(bounded, caps)

    def test_cycle_trims_oversized_register_capabilities(self):
        cfg, roots = oversized_settings()
        calls = []
        printed = io.StringIO()

        def request(method, path, body=None):
            calls.append((method, path, body))
            return {'offers': []}

        def runner(argv, **kw):
            if argv[0] == 'maintained-gate':
                return {'launch_allowed': True, 'profile_supported': True,
                        'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
            if argv[0] == 'aplexer':
                return []
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            cfg['aplexer_state_dir'] = tmp
            with patch('agent_hub_host.system.get_system_info',
                       return_value={'capabilities': {}, 'hostname': 'test', 'os': 'test',
                                     'network': 'home', 'roles': []}), \
                 patch('agent_hub_host.api.get_heartbeat_payload',
                       return_value={'type': 'heartbeat'}):
                with contextlib.redirect_stdout(printed):
                    results = fleet.Dispatcher(cfg, request, runner).once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], 'workclaim-sync')
        register = [c for c in calls if c[1] == '/api/machines/register']
        self.assertEqual(len(register), 1)
        caps = register[0][2]['capabilities']
        self.assertLessEqual(fleet._capabilities_json_size(caps), fleet.REGISTER_CAPABILITIES_BUDGET)
        for root in roots:
            self.assertIn(root, caps['workspaces'])
        events = [json.loads(line) for line in printed.getvalue().splitlines()
                  if line.startswith('{')]
        bounded_events = [e for e in events if e.get('event') == 'register_capabilities_bounded']
        self.assertEqual(len(bounded_events), 1)
        self.assertLessEqual(bounded_events[0]['after'], fleet.REGISTER_CAPABILITIES_BUDGET)

    def test_register_400_payload_too_large_never_kills_the_cycle(self):
        cfg = register_settings(['/repo'], {'demo': '/repo'})
        calls = []
        printed = io.StringIO()

        def request(method, path, body=None):
            calls.append((method, path, body))
            if path == '/api/machines/register':
                raise ValueError('HTTP request failed with status 400: payload_too_large')
            return {'offers': []}

        def runner(argv, **kw):
            if argv[0] == 'maintained-gate':
                return {'launch_allowed': True, 'profile_supported': True,
                        'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
            if argv[0] == 'aplexer':
                return []
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            cfg['aplexer_state_dir'] = tmp
            with patch('agent_hub_host.system.get_system_info',
                       return_value={'capabilities': {}, 'hostname': 'test', 'os': 'test',
                                     'network': 'home', 'roles': []}), \
                 patch('agent_hub_host.api.get_heartbeat_payload',
                       return_value={'type': 'heartbeat'}):
                with contextlib.redirect_stdout(printed):
                    results = fleet.Dispatcher(cfg, request, runner).once()
        # The cycle completed: register was attempted, the heartbeat and the
        # rest of the slow cycle (subscriptions, census, offers) still ran.
        # (The route advertisement precedes each heartbeat, intake 6cbb862a.)
        self.assertEqual([c[1] for c in calls if c[1] != '/api/host/routes'][:2],
                         ['/api/machines/register', '/api/host/poll'])
        self.assertTrue(any(c[1] == '/api/fleet/offers' for c in calls))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], 'workclaim-sync')
        events = [json.loads(line) for line in printed.getvalue().splitlines()
                  if line.startswith('{')]
        skipped = [e for e in events if e.get('event') == 'register_skipped']
        self.assertEqual(len(skipped), 1)
        self.assertIn('payload_too_large', skipped[0]['error'])


if __name__ == '__main__':
    unittest.main()
