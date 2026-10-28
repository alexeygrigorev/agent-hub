"""A headless launch must supply a usable, confined TMPDIR to its engine."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host import fleet


class LaunchWorkspaceTests(unittest.TestCase):
    def dispatch(self, root):
        calls = []
        settings = {
            'workspaces': {'repo': str(root)},
            'subscriptions': [{'service': 'go', 'engine': 'opencode',
                'account_label': 'go-test', 'launcher_provider': 'opencode',
                'launcher_model': 'test-model', 'admission_argv': ['gate']}],
            'launcher': {'argv': ['launcher'], 'root': str(root),
                         'config_dir': str(root / 'config')},
        }
        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'gate':
                return {'launch_allowed': True, 'profile_supported': True,
                        'account_label': 'go-test', 'observed_at': fleet.utcnow()}
            if 'plan' in argv:
                return {'eligible': [{'provider': 'opencode', 'model': 'test-model', 'task_fit': 1.0}]}
            if 'run' in argv:
                temp = argv[argv.index('--tmpdir') + 1]
                # Like ZCode's prompt writer: use TMPDIR directly, with no
                # fallback to /tmp that would hide a missing directory.
                result = subprocess.run([sys.executable, '-c',
                    'import os,tempfile; f=tempfile.TemporaryFile(dir=os.environ["TMPDIR"]); f.write(b"prompt")'],
                    env=dict(os.environ, TMPDIR=temp), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return {'receipt': {'session_id': '11111111-1111-4111-8111-111111111111',
                    'state': 'running', 'first_action_verified': True,
                    'tag': 'task-hub-test-launch', 'workspace': str(root),
                    'engine': 'opencode', 'phase': 'running'}}
            return {}
        dispatcher = fleet.Dispatcher(settings, lambda *args: {}, runner)
        # Scratch lives under this agent's checkout. Do not let Git's upward
        # discovery mistake the synthetic workspace for the real worktree.
        with patch.dict(os.environ, GIT_CEILING_DIRECTORIES=str(root.parent)):
            result = dispatcher.dispatch({'id': 'test-launch', 'engine': 'opencode',
                'account': 'go-test', 'mode': 'headless', 'workspace': 'repo', 'cwd': '.',
                'prompt': 'Read-only launch check'})
        return result, calls

    def test_explicit_workspace_without_tmp_can_write_engine_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse((root / '.local' / 'tmp').exists())
            result, calls = self.dispatch(root)
            self.assertEqual(result['state'], 'running')
            self.assertTrue(any('run' in argv for argv in calls))

    def test_reused_workspace_preserves_existing_tmp_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp = root / '.local' / 'tmp'
            temp.mkdir(parents=True)
            marker = temp / 'previous-session-evidence'
            marker.write_text('preserve')
            result, _ = self.dispatch(root)
            self.assertEqual(result['state'], 'running')
            self.assertEqual(marker.read_text(), 'preserve')

    def test_linked_tmp_is_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            (root / '.local').mkdir()
            foreign = Path(tmp) / 'foreign'
            foreign.mkdir()
            (root / '.local' / 'tmp').symlink_to(foreign, target_is_directory=True)
            result, calls = self.dispatch(root)
            self.assertEqual(result['diagnostic']['reason'], 'launcher_tmpdir_unavailable')
            self.assertFalse(any('run' in argv for argv in calls))
            self.assertEqual(list(foreign.iterdir()), [])
