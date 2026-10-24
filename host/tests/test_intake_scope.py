"""Scope DTOs and refusal diagnostics survive the public CLI boundary."""
import io
import json
import unittest
from unittest.mock import patch
from agent_hub_host import cli, intake


class ScopeCliTest(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['hub', *args]), patch('sys.stdout', out), patch('sys.stderr', err):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_show_preserves_scope_flag_and_operator_decision(self):
        task = {'id': 'fixture', 'scope_flag': 'needs_decision', 'scope': {
            'policy_version': 1, 'clear': False, 'block_code': 'scope_decision_required'}}
        with patch.object(intake, 'request', return_value={'task': task}):
            code, out, err = self.run_cli('intake', 'show', 'fixture')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['task'], task)

    def test_project_update_uses_ordinary_CAS_endpoint(self):
        with patch.object(intake, 'request', return_value={'task': {}}) as transport:
            code, _, err = self.run_cli('intake', 'update', 'fixture', '--project', 'pocketshell', '--version', '2', '--key', 'scope-project')
        self.assertEqual(code, 0, err)
        self.assertEqual(transport.call_args.args, ('POST', '/api/intake/tasks/fixture', {
            'key': 'scope-project', 'expected_version': 2, 'project': 'pocketshell'}))

    def test_server_scope_refusals_remain_actionable(self):
        for slug in ('scope_decision_required', 'scope_out_of_bounds'):
            with self.subTest(slug=slug), patch.object(intake, 'request', side_effect=intake.IntakeHTTPError(409, slug)):
                code, out, err = self.run_cli('intake', 'show', 'fixture')
            self.assertEqual(code, 1)
            self.assertEqual(out, '')
            self.assertIn(slug, err)

    def test_scope_decision_is_forwarded_as_data_never_local_approval(self):
        event = {'kind': 'scope_decision', 'verdict': 'in_scope', 'text': 'Operator decision'}
        with patch.object(intake, 'json_file', return_value=event), patch.object(intake, 'request', side_effect=intake.IntakeHTTPError(403, 'operator_required')) as transport:
            code, _, err = self.run_cli('intake', 'append', 'fixture', '--version', '1', '--key', 'decision', '--event-file', '-')
        self.assertEqual(code, 1)
        self.assertIn('operator_required', err)
        self.assertEqual(transport.call_args.args[2]['event'], event)
