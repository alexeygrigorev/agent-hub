"""Native managed Codex command contract; no engine or host service is started."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import provisioning as p


class ManagedEffortTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX execve command capture")
    def test_native_command_effort(self):
        for label in ('shared-codex', 'codex-orin'):
            for model, explicit, session, expected in (
                ('gpt-6.1-sol', [], [], 'medium'),
                ('gpt-6-luna', [], [], 'xhigh'),
                # Older/forward-compatible routes may be accepted without a unique policy effort.
                ('custom-model', [], [], 'high'),
                ('gpt-6.1-sol', [], ['--hub-session-effort=max'], 'xhigh'),
                ('gpt-6.1-sol', ['-c', 'model_reasoning_effort="high"'], [], 'high'),
            ):
                with self.subTest(label=label, model=model, explicit=explicit, session=session):
                    with tempfile.TemporaryDirectory() as temporary:
                        managed = Path(temporary)
                        revision = '11111111-1111-4111-8111-111111111111'
                        b = dict(label=label, provider='codex', version='0.163.0',
                                 revision=revision, args=['-c', 'model_reasoning_effort="high"'],
                                 files={'.codex/config.toml': 'model_reasoning_effort = "xhigh"\n'}, env={})
                        dest = managed / (label + '-' + revision)
                        dest.mkdir(mode=0o700)
                        p.write_json(dest / 'bundle.json', b)
                        p.write_json(managed / 'state.json', [dict(label=label, revision=revision)])
                        with patch.object(p, 'root', return_value=managed), \
                             patch.object(p, 'provision_managed_home'), \
                             patch.object(p, 'refresh_launch_credential'), \
                             patch.object(p, 'install_managed_engine_permissions'), \
                             patch.object(p, 'engine_environment', return_value={}), \
                             patch.object(p, 'engine_argv', return_value=['codex']), \
                             patch.object(p, 'hub_launch_gate', return_value=(True, 'ok')), \
                             patch.object(p, 'managed_model_allowed', return_value=True), \
                             patch.object(p.sys, 'argv', ['execute', label, 'exec', '--model', model] + session + explicit + ['task']), \
                             patch.object(p.os, 'execve') as launch:
                            p.execute()
                        argv = launch.call_args.args[1]
                        values = [a for a in argv if a.startswith('model_reasoning_effort=')]
                        self.assertEqual(values, ['model_reasoning_effort="' + expected + '"'])
                        self.assertNotIn('--hub-session-effort=max', argv)

    def test_caller_effort_is_explicit_but_bundle_effort_is_a_default(self):
        for override in (
            ['-c', 'model_reasoning_effort="xhigh"'],
            ['--config', 'model_reasoning_effort = "high"'],
            ['--config=model_reasoning_effort="low"'],
            ['-cmodel_reasoning_effort="high"'],
            ['--effort', 'high'],
            ['--effort=high'],
        ):
            with self.subTest(override=override, source='caller'):
                b = dict(provider='codex', args=[])
                self.assertEqual(
                    p.managed_codex_effort_args(b, ['exec', '--model', 'gpt-6.1-sol'] + override),
                    [],
                )
            with self.subTest(override=override, source='profile'):
                b = dict(provider='codex', args=override)
                self.assertEqual(
                    p.managed_codex_effort_args(b, ['exec', '--model', 'gpt-6.1-sol']),
                    ['-c', 'model_reasoning_effort="medium"'],
                )
                cleaned, found = p.strip_codex_effort_args(override)
                self.assertEqual(cleaned, [])
                self.assertTrue(found)

    def test_no_model_required_and_diagnostics_unchanged(self):
        b = dict(provider='codex', args=['-c', 'model_reasoning_effort="high"'])
        self.assertEqual(p.managed_codex_effort_args(b, ['task']),
                         ['-c', 'model_reasoning_effort="medium"'])
        self.assertEqual(p.managed_codex_effort_args(b, ['exec', '--model', 'gpt-6-luna']),
                         ['-c', 'model_reasoning_effort="xhigh"'])
        self.assertEqual(p.managed_codex_effort_args(
            b, ['exec', '--model', 'gpt-6.1-sol', '--hub-session-effort=max']),
            ['-c', 'model_reasoning_effort="xhigh"'])
        for args in (['--version'], ['--help']):
            self.assertEqual(p.managed_codex_effort_args(b, args), [])
        self.assertEqual(p.managed_codex_effort_args(dict(provider='zai', args=[]), ['task']), [])
