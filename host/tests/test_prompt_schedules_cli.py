"""hub prompts / hub schedules / --prompt-id CLI wiring (intake 19ec91c2 AC8-AC10)."""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


class PromptScheduleCli(unittest.TestCase):
    def _run(self, argv, response=None):
        from agent_hub_host import cli, session_control
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return response if response is not None else {}

        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['hub'] + argv), \
                patch.object(session_control, 'request', side_effect=request), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit:
                pass
        return calls, out.getvalue(), err.getvalue()

    def test_schedule_create_inline_prompt_and_vars(self):
        calls, _, _ = self._run(['schedules', 'create', '--name', 'hourly', '--session', 'orchestrator-main',
                                 '--cron', '0 * * * *', '--prompt', 'sweep {{var.x}}', '--var', 'x=1'])
        self.assertEqual(calls[0][0:2], ('POST', '/api/session-schedules'))
        self.assertEqual(calls[0][2]['prompt'], 'sweep {{var.x}}')
        self.assertEqual(calls[0][2]['prompt_vars'], {'x': '1'})
        self.assertEqual(calls[0][2]['target_label'], 'orchestrator-main')

    def test_schedule_create_prompt_id_and_requires_a_prompt(self):
        calls, _, _ = self._run(['schedules', 'create', '--name', 'hourly', '--session', 's',
                                 '--cron', '0 * * * *', '--prompt-id', 'orchestrator-hourly'])
        self.assertEqual(calls[0][2]['prompt_id'], 'orchestrator-hourly')
        calls, _, err = self._run(['schedules', 'create', '--name', 'hourly', '--session', 's', '--cron', '0 * * * *'])
        self.assertEqual(calls, [])
        self.assertIn('required', err)

    def test_schedule_delete_requires_yes_and_pause_resume_paths(self):
        calls, _, err = self._run(['schedules', 'delete', 'hourly'])
        self.assertEqual(calls, [])
        calls, _, _ = self._run(['schedules', 'pause', 'hourly'])
        self.assertEqual(calls[0][0:2], ('POST', '/api/session-schedules/hourly/pause'))
        calls, _, _ = self._run(['schedules', 'delete', 'hourly', '--yes'])
        self.assertEqual(calls[0][0:2], ('DELETE', '/api/session-schedules/hourly'))

    def test_prompts_crud_paths(self):
        calls, _, _ = self._run(['prompts', 'create', '--slug', 'oncall-triage', '--body', 'hello'])
        self.assertEqual(calls[0], ('POST', '/api/prompts', {'slug': 'oncall-triage', 'body': 'hello'}))
        calls, _, _ = self._run(['prompts', 'update', 'oncall-triage', '--body', 'v2', '--note', 'n'])
        self.assertEqual(calls[0], ('PUT', '/api/prompts/oncall-triage', {'body': 'v2', 'note': 'n'}))
        calls, out, _ = self._run(['prompts', 'show', 'oncall-triage@1'], {'body': 'text-body', 'version': 1})
        self.assertEqual(calls[0][0:2], ('GET', '/api/prompts/oncall-triage@1'))
        self.assertEqual(out.strip(), 'text-body')

    def test_sessions_send_prompt_id_renders_then_sends(self):
        from agent_hub_host import cli, session_control
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if path.endswith('/render'):
                return {'text': 'rendered hello'}
            return {'id': 'cmd', 'state': 'queued'}

        with patch.object(sys, 'argv', ['hub', 'sessions', 'send', 'sess-1', '--prompt-id', 'oncall-triage',
                                        '--var', 'a=b', '--key', 'k1']), \
                patch.object(session_control, 'request', side_effect=request), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                cli.main()
            except SystemExit:
                pass
        self.assertEqual(calls[0], ('POST', '/api/prompts/oncall-triage/render', {'vars': {'a': 'b'}}))
        self.assertEqual(calls[1][1], '/api/sessions/sess-1/send')
        self.assertEqual(calls[1][2]['body'], 'rendered hello')

    def test_webhook_create_prompt_id(self):
        calls, _, _ = self._run(['webhooks', 'create', '--name', 'dev-alerts', '--session', 'oncall-engineer',
                                 '--prompt-id', 'alert-template', '--var', 'team=dev'], {'id': 'r', 'url_path': '/webhooks/r'})
        self.assertEqual(calls[0][2]['prompt_id'], 'alert-template')
        self.assertEqual(calls[0][2]['prompt_vars'], {'team': 'dev'})


if __name__ == '__main__':
    unittest.main()
