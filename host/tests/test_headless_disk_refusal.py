"""The 26 Oct burst failed at disk admission, before any native start."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_hub_host.fleet import run_json
from agent_hub_host.launch_diagnostics import LocalCommandError, diagnostic, exception_diagnostic
from agent_hub_host.launcher_recovery import reconcile_resources


class HeadlessDiskRefusalTests(unittest.TestCase):
    def test_real_process_retains_exact_disk_valueerror_without_traceback_or_secrets(self):
        for engine in ('codex', 'opencode'):
            with self.subTest(engine=engine):
                script = "import sys; print('secret=private-value /private/path', file=sys.stderr); raise ValueError('cwd filesystem free < 20GiB floor + required disk (41875931136 B)')"
                with self.assertRaises(LocalCommandError) as raised:
                    run_json([sys.executable, '-c', script])
                value = exception_diagnostic(raised.exception, 'dispatch_headless', engine=engine)
                self.assertEqual(value['reason'], 'launcher_disk_reserve')
                self.assertEqual(value['error_type'], 'ValueError')
                self.assertEqual(value['exit_code'], 1)
                self.assertEqual(value['stage'], 'dispatch_headless')
                self.assertIn('disk', value['message'])
                self.assertNotIn('private', json.dumps(value))
                self.assertNotIn('Traceback', json.dumps(value))

    def test_only_complete_known_disk_grammar_is_classified(self):
        raw = 'ValueError: tmpdir filesystem free < 20GiB floor + required disk (41875931136 B)'
        self.assertEqual(diagnostic(raw)['reason'], 'launcher_disk_reserve')
        for bad in (raw + ' secret=private-value', 'Task: ' + raw, raw.replace('41875931136', 'private-value')):
            self.assertEqual(diagnostic(bad)['reason'], 'launcher_command_failed')
            self.assertNotIn('private-value', json.dumps(diagnostic(bad)))

    def test_existing_resource_scan_projects_only_bounded_disk_evidence(self):
        runner = Mock(return_value={'active_mem_mb': 0, 'active_disk_mb': 19456,
                                   'breakdown': [{'task_id': 'hub-existing', 'private': 'withheld'}]})
        config = {'argv': ['launcher'], 'root': '/launcher', 'config_dir': '/state'}
        with tempfile.TemporaryDirectory() as root:
            result = reconcile_resources(config, runner, workspace_paths=[root], workspace_aliases={"demo": root})
        self.assertEqual(result['active_disk_mb'], 19456)
        self.assertEqual(result['disk_floor_mb'], 20480)
        self.assertEqual(result['next_disk_mb'], 512)
        self.assertEqual(result['disk_task_ids'], ['hub-existing'])
        self.assertEqual(result['disk_aliases'], {'demo': root})
        self.assertEqual(len(result['disk_paths']), 2)
        self.assertGreater(result['disk_paths'][0]['free_gb'], 0)
        self.assertNotIn('withheld', json.dumps(result))
        runner.assert_called_once()
        for bad in (-1, float('nan'), float('inf'), True, '19456'):
            runner.return_value = {'active_mem_mb': 0, 'active_disk_mb': bad}
            self.assertNotIn('active_disk_mb', reconcile_resources(config, runner))

    def test_temp_mount_and_nearest_existing_parent_are_sampled(self):
        runner = Mock(return_value={'active_mem_mb': 0, 'active_disk_mb': 0})
        config = {'argv': ['launcher'], 'root': '/launcher', 'config_dir': '/state'}
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            local = base / '.local'
            local.mkdir()
            temp = local / 'tmp'
            with patch('shutil.disk_usage', side_effect=lambda p: Mock(free=(19 if Path(p) == local else 80) * 1024**3)):
                result = reconcile_resources(config, runner, workspace_paths=[root])
            self.assertIn({'path': str(temp), 'free_gb': 19}, result['disk_paths'])
            self.assertFalse(temp.exists())
            temp.mkdir()
            with patch('shutil.disk_usage', side_effect=lambda p: Mock(free=(18 if Path(p) == temp else 80) * 1024**3)):
                result = reconcile_resources(config, runner, workspace_paths=[root])
            self.assertIn({'path': str(temp), 'free_gb': 18}, result['disk_paths'])

    def test_invalid_or_oversized_ledger_never_sends_arbitrary_task_data(self):
        runner = Mock(return_value={'active_mem_mb': 0, 'active_disk_mb': 512,
                                   'breakdown': [{'task_id': 'secret=private-value'}]})
        config = {'argv': ['launcher'], 'root': '/launcher', 'config_dir': '/state'}
        self.assertNotIn('disk_task_ids', reconcile_resources(config, runner))
        runner.return_value['breakdown'] = [{'task_id': 'task'}] * 257
        self.assertNotIn('disk_task_ids', reconcile_resources(config, runner))
