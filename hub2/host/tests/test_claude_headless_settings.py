"""Claude headless settings-source isolation for Hub-managed launches."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hub2_agent_hub_host import native_launch as nl


class ClaudeHeadlessSettingsTest(unittest.TestCase):
    def launch(self, *, local=False, interactive=False):
        with tempfile.TemporaryDirectory() as workspace:
            claude_dir = Path(workspace) / '.claude'
            claude_dir.mkdir()
            (claude_dir / 'settings.json').write_text(json.dumps({
                'permissions': {'disableBypassPermissionsMode': 'disable'},
            }))
            captured = {}
            native_id = 'd6cf9872-8597-48a5-be64-990cdef4db31'

            def runner(argv, **options):
                child = argv[argv.index('--') + 1:]
                captured['child'] = child
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'id': native_id,
                    'tag': 'task-claude-settings-test',
                    'workspace': workspace,
                    'command': child,
                }), '')

            adapter = nl.NativeLauncher(
                {'private_provider_homes': not local,
                 'routes': {'claude': nl.LOCAL_ROUTES['claude']}},
                aplexer='synthetic-aplexer', runner=runner,
            )
            specification = {
                'workspace': workspace,
                'engine': 'claude',
                'model': 'sonnet',
                'prompt': 'run the assigned task',
                'interactive': interactive,
                'timeout_seconds': 90,
                'operation_id': 'claude-settings-operation',
                'epoch': 1,
            }
            with mock.patch.object(adapter, 'wait_first_action', return_value={
                    'session_id': native_id, 'first_action_verified': True}), \
                 mock.patch.object(adapter, 'record_evidence'):
                adapter.launch('claude-settings-test', specification)
            return captured['child']

    def test_direct_headless_uses_managed_source_when_project_disables_bypass(self):
        child = self.launch()
        self.assertEqual(child.count('--setting-sources'), 1)
        self.assertEqual(child[child.index('--setting-sources') + 1], 'user')
        self.assertIn('--dangerously-skip-permissions', child)
        self.assertIn('-p', child)

    def test_local_profile_headless_passes_user_only_setting_source(self):
        child = self.launch(local=True)
        engine_args = [arg.removeprefix('--engine-arg=') for arg in child if arg.startswith('--engine-arg=')]
        self.assertEqual(engine_args.count('--setting-sources'), 1)
        self.assertEqual(engine_args[engine_args.index('--setting-sources') + 1], 'user')

    def test_interactive_claude_keeps_workspace_settings(self):
        child = self.launch(interactive=True)
        self.assertNotIn('--setting-sources', child)


if __name__ == '__main__':
    unittest.main()
