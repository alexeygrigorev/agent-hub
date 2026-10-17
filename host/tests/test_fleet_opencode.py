import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet, fleet_launch


def settings(workspace, provider="opencode",
             model="opencode-go/muse-spark-1.3-contributor"):
    return {'version': 1, 'workspaces': {'demo': str(workspace)}, 'subscriptions': [
        {'service': 'go', 'account_label': 'shared-go', 'engine': 'opencode',
         'profile': None, 'admission_argv': ['maintained-gate'],
         'launcher_provider': provider, 'launcher_model': model}
    ], 'launcher': {'argv': ['python3', '-m', 'launcher'], 'root': str(workspace),
                    'config_dir': str(Path(str(workspace)) / 'config')}}


def offer(**kw):
    base = {'id': '12345678-1234-1234-1234-123456789abc', 'engine': 'opencode',
            'profile': None, 'account': 'shared-go', 'mode': 'headless',
            'workspace': 'demo', 'prompt': 'arbitrary brief $(no interpolation)'}
    base.update(kw)
    return base


class FleetOpencodeTests(unittest.TestCase):
    def test_headless_opencode_uses_aplexer_backend_not_task_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp))
            calls = []
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True,
                            'account_label': 'shared-go',
                            'observed_at': fleet.utcnow()}
                if 'submit' in argv:
                    self.assertIn('--json', argv)
                    return {}
                if 'plan' in argv:
                    # Genuine maintained-CLI plan shape (verified live):
                    # provider/model/task_fit plus ranking metadata, never
                    # Hub account_label/profile.
                    return {'eligible': [{'name': 'go',
                                          'provider': 'opencode',
                                          'model': 'opencode-go/muse-spark-1.3-contributor',
                                          'health': 1.0,
                                          'task_fit': 1.0,
                                          'promo_multiplier': 1.0}]}
                if 'run' in argv:
                    return {'backend': 'aplexer', 'task_id': 'hub-x',
                            'receipt': {'session_id': 'native-uuid-1',
                                        'state': 'running',
                                        'first_action_verified': True,
                                        'engine': 'opencode'}}
                return {}
            d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
            result = d.dispatch(offer())
            self.assertEqual(result['state'], 'running')
            self.assertEqual(result['native_id'], 'native-uuid-1')
            run_argv = calls[-1]
            self.assertIn('--backend', run_argv)
            self.assertIn('aplexer', run_argv)
            self.assertNotIn('task-units', run_argv)
            self.assertNotIn('--as-controller', run_argv)

    def test_headless_other_providers_keep_task_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp), provider='codex', model='gpt-6-sol')
            cfg['subscriptions'][0].update(engine='codex')
            calls = []
            def runner(argv, **kw):
                calls.append(argv)
                if argv[0] == 'maintained-gate':
                    return {'launch_allowed': True, 'profile_supported': True,
                            'account_label': 'shared-go',
                            'observed_at': fleet.utcnow()}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'codex', 'model': 'gpt-6-sol',
                                          'task_fit': 1.0,
                                          'account_label': 'shared-go',
                                          'profile': None}]}
                if 'run' in argv:
                    return {'receipt': {'unit_name': 'u1', 'exit_code': 0}}
                return {}
            d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
            result = d.dispatch(offer(engine='codex', prompt='Goal'))
            self.assertEqual(result['native_id'], 'u1')
            self.assertIn('task-units', calls[-1])
            self.assertIn('--as-controller', calls[-1])

    def test_unit_only_receipt_without_uuid_is_rejected_for_binding(self):
        # A task-units-style unit_name receipt without session_id must not
        # validate as a native opencode binding (can't bind/control M12).
        with self.assertRaises(ValueError):
            fleet_launch.validate_native_receipt(
                {'receipt': {'unit_name': 'agent-task-x.service',
                             'state': 'running',
                             'first_action_verified': True,
                             'engine': 'opencode'}})
        self.assertEqual(
            fleet_launch.validate_native_receipt(
                {'receipt': {'session_id': 'native-uuid-9',
                             'state': 'running',
                             'first_action_verified': True,
                             'engine': 'opencode'}}),
            'native-uuid-9')

    def test_helper_contract_exact_argv_and_payload(self):
        sub = fleet_launch.opencode_subscription(
            'shared-go', admission_argv=['maintained-gate'])
        self.assertEqual(sub['launcher_provider'], 'opencode')
        self.assertEqual(sub['launcher_model'],
                         'opencode-go/muse-spark-1.3-contributor')
        # No nested launcher key inside a subscription: load_settings
        # rejects it; the launcher CLI lives in TOP-LEVEL settings.
        self.assertNotIn('launcher', sub)
        launcher_cfg = fleet_launch.opencode_launcher_config(
            ['python3', '-m', 'launcher'], '/repo/ql', '/repo/ql/cfg')
        self.assertEqual(launcher_cfg['argv'], ['python3', '-m', 'launcher'])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fleet_launch.opencode_settings(
                'demo', tmp, 'shared-go',
                admission_argv=['maintained-gate'],
                launcher_argv=['python3', '-m', 'launcher'],
                launcher_root='/repo/ql',
                launcher_config_dir='/repo/ql/cfg')
            path = Path(tmp) / 'subscriptions.json'
            path.write_text(json.dumps(cfg))
            loaded = fleet.load_settings(path)
            self.assertEqual(loaded['subscriptions'][0]['account_label'],
                             'shared-go')
            self.assertEqual(loaded['launcher']['root'], '/repo/ql')
        payload = fleet_launch.launcher_payload('/ws', 'brief $x', 'shared-go')
        self.assertEqual(payload['model_requirements'],
                         {'providers': ['opencode'],
                          'models': ['opencode-go/muse-spark-1.3-contributor']})
        argv = fleet_launch.launcher_run_argv(['python3', '-m', 'launcher'],
                                              '/cfg', 'hub-id', '/ws', '/ws')
        self.assertIn('aplexer', argv)
        self.assertNotIn('task-units', argv)
        # Prompt stays a separate element downstream (launcher appends it;
        # never shell-interpolated here).
        self.assertNotIn('brief $x', argv)
        off = fleet_launch.opencode_offer('oid', 'demo', 'arbitrary $brief',
                                          account='shared-go')
        self.assertEqual(off['prompt'], 'arbitrary $brief')
        self.assertEqual(off['mode'], 'headless')


def cycle(workspace, rows, pending, request=None):
    """Run one finite once() cycle with a canned native catalog + pending."""
    from agent_hub_host import api, system  # noqa: F401 (patched below)
    calls = []

    def default_request(method, path, body=None):
        calls.append((method, path, body))
        if path == '/api/sessions/host-bindings':
            return {'sessions': pending}
        if path == '/api/fleet/offers':
            return {'offers': []}
        return {}

    def runner(argv, **kw):
        if argv[0] == 'maintained-gate':
            return {'launch_allowed': True, 'profile_supported': True,
                    'account_label': 'shared-go',
                    'observed_at': fleet.utcnow()}
        if argv[0] == 'aplexer':
            return rows
        return {}

    cfg = settings(Path(workspace))
    with patch('agent_hub_host.system.get_system_info',
               return_value={'capabilities': {}}), \
         patch('agent_hub_host.api.get_heartbeat_payload',
               return_value={'type': 'heartbeat'}):
        results = fleet.Dispatcher(cfg, request or default_request,
                                   runner).once()
    return results, calls


LID = '12345678-1234-1234-1234-123456789abc'
SID = '22345678-1234-1234-1234-123456789abc'
UUID_A = '32345678-1234-1234-1234-123456789abc'
UUID_B = '42345678-1234-1234-1234-123456789abc'


def native_row(uuid, tag, workspace):
    return {'id': uuid, 'tag': tag, 'workspace': str(workspace),
            'engine': 'opencode', 'phase': 'running',
            'created_at_ms': 1792250000000}


class FleetBindingTests(unittest.TestCase):
    def test_once_binds_actual_launcher_task_hub_tag(self):
        # The maintained launcher starts task "hub-<offer>" through
        # run_tag_for, so the native tag is "task-hub-<offer>" (not hub-).
        with tempfile.TemporaryDirectory() as tmp:
            row = native_row(UUID_A, 'task-hub-' + LID, tmp)
            pending = [{'id': SID, 'fleet_launch_id': LID,
                        'engine': 'opencode'}]
            results, calls = cycle(tmp, [row], pending)
            self.assertEqual(results, [])
            bindings = [c for c in calls if c[1].endswith('/binding')]
            self.assertEqual(len(bindings), 1)
            method, path, body = bindings[0]
            self.assertEqual(method, 'POST')
            self.assertEqual(path, '/api/sessions/' + SID + '/binding')
            self.assertEqual(body['native']['native_id'], UUID_A)
            self.assertEqual(body['native']['tag'], 'task-hub-' + LID)
            self.assertEqual(body['native']['workspace'], str(tmp))
            self.assertEqual(body['native']['native_created_at_ms'],
                             1792250000000)

    def test_once_still_binds_hub_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = native_row(UUID_A, 'hub-' + LID, tmp)
            pending = [{'id': SID, 'fleet_launch_id': LID,
                        'engine': 'opencode'}]
            _, calls = cycle(tmp, [row], pending)
            bindings = [c for c in calls if c[1].endswith('/binding')]
            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0][2]['native']['tag'], 'hub-' + LID)

    def test_once_prefers_canonical_native_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [native_row(UUID_A, 'task-hub-' + LID, tmp),
                    native_row(UUID_B, 'task-hub-' + LID, tmp)]
            # Ambiguous tag-only match binds nothing (never tag-only).
            _, calls = cycle(tmp, rows, [{'id': SID,
                                          'fleet_launch_id': LID,
                                          'engine': 'opencode'}])
            self.assertFalse([c for c in calls if c[1].endswith('/binding')])
            # Canonical server launch native_id selects the exact row.
            _, calls = cycle(tmp, rows, [{'id': SID,
                                          'fleet_launch_id': LID,
                                          'engine': 'opencode',
                                          'native_id': UUID_B}])
            bindings = [c for c in calls if c[1].endswith('/binding')]
            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0][2]['native']['native_id'], UUID_B)

    def test_once_skips_unproven_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            proven = native_row(UUID_A, 'hub-' + LID, tmp)
            for bad in [dict(proven, workspace=''),
                        dict(proven, created_at_ms='yesterday'),
                        dict(proven, created_at_ms=-5)]:
                _, calls = cycle(tmp, [bad], [{'id': SID,
                                               'fleet_launch_id': LID,
                                               'engine': 'opencode'}])
                self.assertFalse(
                    [c for c in calls if c[1].endswith('/binding')])

    def test_binding_refusal_does_not_kill_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = native_row(UUID_A, 'task-hub-' + LID, tmp)
            pending = [{'id': SID, 'fleet_launch_id': LID,
                        'engine': 'opencode'}]
            calls = []

            def refusing(method, path, body=None):
                calls.append((method, path, body))
                if path.endswith('/binding'):
                    raise ValueError('HTTP request failed with status 409')
                if path == '/api/sessions/host-bindings':
                    return {'sessions': pending}
                if path == '/api/fleet/offers':
                    return {'offers': []}
                return {}

            results, _ = cycle(tmp, [row], pending, request=refusing)
            self.assertEqual(results, [])
            self.assertTrue(any(c[1] == '/api/fleet/offers' for c in calls))


if __name__ == '__main__':
    unittest.main()
