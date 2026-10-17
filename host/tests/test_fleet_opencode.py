import json
import tempfile
import unittest
from pathlib import Path

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
                    return {}
                if 'plan' in argv:
                    return {'eligible': [{'provider': 'opencode',
                                          'model': 'opencode-go/muse-spark-1.3-contributor',
                                          'account_label': 'shared-go',
                                          'profile': None}]}
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
            'shared-go', admission_argv=['maintained-gate'],
            launcher_argv=['python3', '-m', 'launcher'],
            launcher_root='/repo/ql', launcher_config_dir='/repo/ql/cfg')
        self.assertEqual(sub['launcher_provider'], 'opencode')
        self.assertEqual(sub['launcher_model'],
                         'opencode-go/muse-spark-1.3-contributor')
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


if __name__ == '__main__':
    unittest.main()
