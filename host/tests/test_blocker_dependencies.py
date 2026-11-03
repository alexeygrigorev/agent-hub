"""Dependency commands preserve CAS and idempotency; transport is synthetic."""
import io
import json
import unittest
from unittest.mock import patch

from agent_hub_host import cli, intake, session_control
from agent_hub_host.session_rules import compose_prompt


class BlockerCommands(unittest.TestCase):
    def run_cli(self, *args):
        output = io.StringIO()
        with patch('sys.argv', ['hub', *args]), patch('sys.stdout', output):
            cli.main()
        return json.loads(output.getvalue())

    def test_depend_fetches_version_and_preserves_key_and_ids(self):
        with patch.object(intake, 'request', side_effect=[{'task': {'version': 7}}, {'linked': True}]) as request:
            self.assertEqual(self.run_cli('intake', 'depend', 'original-task', '--on', 'quota-task', '--key', 'stable'), {'linked': True})
        self.assertEqual(request.call_args_list[0].args, ('GET', '/api/intake/tasks/original-task'))
        self.assertEqual(request.call_args_list[1].args, ('POST', '/api/intake/tasks/original-task/depend',
            {'key': 'stable', 'expected_version': 7, 'on': 'quota-task', 'remove': False}))

    def test_blocker_and_decompose_send_explicit_versions_without_extra_reads(self):
        with patch.object(intake, 'request', return_value={'recorded': True}) as request:
            self.run_cli('intake', 'blocker', 'original-task', '--title', 'Build binary', '--text', 'Missing ARM binary',
                         '--acceptance-criteria', 'Binary launches', '--key', 'blocker-key', '--version', '3')
            request.assert_called_once_with('POST', '/api/intake/tasks/original-task/blocker',
                {'key': 'blocker-key', 'expected_version': 3, 'title': 'Build binary',
                 'text': 'Missing ARM binary', 'acceptance_criteria': 'Binary launches'})
        with patch.object(intake, 'request', return_value={'recorded': True}) as request:
            self.run_cli('intake', 'decompose', 'original-task', '--workspace', 'repo', '--version', '4', '--key', 'deep-key')
            request.assert_called_once_with('POST', '/api/intake/tasks/original-task/decompose',
                {'key': 'deep-key', 'expected_version': 4, 'workspace': 'repo'})

    def test_force_start_is_forwarded_and_standing_prompt_requires_blocker_intake(self):
        with patch.object(session_control, 'request', return_value={'id': 'fixture', 'state': 'queued'}) as request:
            self.run_cli('sessions', 'start', '--workspace', 'repo', '--intake-task', 'original-task', '--prompt', 'Inspect', '--force')
            self.assertTrue(request.call_args.args[2]['force'])
        prompt = compose_prompt('Complete work')
        self.assertIn('hub intake blocker', prompt)
        self.assertIn('acceptance-criteria', prompt)
        self.assertIn('Never close or delete intakes', prompt)
        self.assertIn('never print or copy tokens', prompt)
