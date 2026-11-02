"""hub schedule CLI: payload building and file handling (intake a839dfb6).

The CLI only manages schedule definitions and reads run history over the
public admin API; every start decision stays server-side. These tests pin the
exact payloads sent to /api/schedules*, prompt/AC file reading with the
server's byte caps, and the guardrails (--yes for delete, prompt required).
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch


class ScheduleCliPayloads(unittest.TestCase):
    def _run_with_status(self, argv, request):
        from agent_hub_host import cli, session_control
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = 0
        with patch.object(sys, 'argv', ['hub'] + argv), \
                patch.object(session_control, 'request', side_effect=request), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                cli.main()
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
        return stdout.getvalue(), stderr.getvalue(), exit_code

    def _run(self, argv, request):
        stdout, stderr, _ = self._run_with_status(argv, request)
        return stdout, stderr

    def test_create_minimal_payload_uses_defaults(self):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {'id': 'sched-1', 'name': 'daily-writer', 'enabled': True}

        out, err = self._run(['schedule', 'create', '--name', 'daily-writer', '--project', 'agent-hub',
                              '--cron', '30 9 * * *', '--prompt', 'Write the daily note'], request)
        self.assertEqual(calls[0], ('POST', '/api/schedules', {
            'name': 'daily-writer', 'project': 'agent-hub', 'cron': '30 9 * * *', 'tz': 'UTC',
            'prompt': 'Write the daily note'}))
        parsed = json.loads(out)
        self.assertEqual(parsed['name'], 'daily-writer')
        self.assertEqual(err, '')

    def test_create_full_payload_with_files_and_options(self):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {'id': 'sched-2', 'name': 'nightly'}

        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = os.path.join(tmp, 'prompt.txt')
            ac_path = os.path.join(tmp, 'ac.txt')
            with open(prompt_path, 'w', encoding='utf-8') as handle:
                handle.write('Run the nightly check')
            with open(ac_path, 'w', encoding='utf-8') as handle:
                handle.write('Report exists with evidence')
            out, err = self._run(['schedule', 'create', '--name', 'nightly', '--project', 'agent-hub',
                                  '--cron', '0 3 * * *', '--tz', 'Europe/Berlin', '--engine', 'codex',
                                  '--tier', 'daily', '--task-kind', 'review', '--workspace', 'hub-nightly',
                                  '--overlap', 'queue', '--prompt-file', prompt_path,
                                  '--acceptance-criteria-file', ac_path, '--disabled'], request)
        self.assertEqual(calls[0], ('POST', '/api/schedules', {
            'name': 'nightly', 'project': 'agent-hub', 'cron': '0 3 * * *', 'tz': 'Europe/Berlin',
            'engine': 'codex', 'tier': 'daily', 'task_kind': 'review', 'workspace': 'hub-nightly',
            'overlap': 'queue', 'prompt': 'Run the nightly check',
            'acceptance_criteria': 'Report exists with evidence', 'enabled': False}))

    def test_create_rejects_oversized_prompt_before_any_request(self):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = os.path.join(tmp, 'big.txt')
            with open(prompt_path, 'w', encoding='utf-8') as handle:
                handle.write('x' * 7001)
            out, err = self._run(['schedule', 'create', '--name', 'big', '--project', 'p',
                                  '--cron', '* * * * *', '--prompt-file', prompt_path], request)
        self.assertEqual(calls, [])
        self.assertIn('Prompt', err)

    def test_create_requires_a_prompt_source(self):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {}

        out, err = self._run(['schedule', 'create', '--name', 'no-prompt', '--project', 'p',
                              '--cron', '* * * * *'], request)
        self.assertEqual(calls, [])
        self.assertNotEqual(err, '')

    def test_delete_requires_yes_and_runs_reads_history(self):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == 'GET':
                return {'runs': [{'tick_at': '2026-10-31T10:00:00Z', 'outcome': 'held',
                                  'intake_id': 't1', 'session_id': 's1', 'reason': 'placement_hold'}]}
            return {'deleted': True}

        out, err = self._run(['schedule', 'delete', 'daily-writer'], request)
        self.assertIn('--yes', err)
        out, err = self._run(['schedule', 'delete', 'daily-writer', '--yes'], request)
        self.assertEqual(calls[-1], ('DELETE', '/api/schedules/daily-writer', None))
        out, err = self._run(['schedule', 'runs', 'daily-writer', '--limit', '10'], request)
        self.assertEqual(calls[-1], ('GET', '/api/schedules/daily-writer/runs?limit=10', None))
        self.assertIn('placement_hold', out)

    def test_lifecycle_commands_hit_their_endpoints(self):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {'id': 'sched-9', 'enabled': method != 'POST' or not path.endswith('/pause')}

        for argv, expected in [
            (['schedule', 'pause', 'daily-writer'], ('POST', '/api/schedules/daily-writer/pause', {})),
            (['schedule', 'resume', 'daily-writer'], ('POST', '/api/schedules/daily-writer/resume', {})),
            (['schedule', 'tick', 'daily-writer'], ('POST', '/api/schedules/daily-writer/tick', {})),
            (['schedule', 'show', 'daily-writer'], ('GET', '/api/schedules/daily-writer', None)),
            (['schedule', 'list'], ('GET', '/api/schedules', None)),
        ]:
            self._run(argv + ['--json'], request)
            self.assertEqual(calls[-1], expected, argv)

    def test_list_renders_table_with_paused_marker(self):
        def request(method, path, payload=None):
            return {'schedules': [
                {'name': 'live', 'id': 'i1', 'project': 'p', 'cron': '* * * * *', 'tz': 'UTC',
                 'overlap': 'skip', 'next_due_at': '2026-10-31T11:00:00Z', 'enabled': True},
                {'name': 'stopped', 'id': 'i2', 'project': 'p', 'cron': '* * * * *', 'tz': 'UTC',
                 'overlap': 'skip', 'next_due_at': None, 'enabled': False},
            ]}

        out, err = self._run(['schedule', 'list'], request)
        self.assertIn('2026-10-31T11:00:00Z', out)
        self.assertIn('paused', out)

    def test_http_errors_render_bounded_cli_diagnostics_without_tracebacks(self):
        from agent_hub_host import session_control

        for status, code in ((403, 'worker_scope_forbidden'), (404, 'not_found'), (409, 'schedule_conflict')):
            with self.subTest(status=status):
                secret_body = 'private response body with credential=hidden'

                def request(*_args, **_kwargs):
                    raise session_control.SessionHTTPError(status, code=code, message=secret_body)

                out, err, exit_code = self._run_with_status(['schedule', 'show', 'daily-writer'], request)
                self.assertEqual(out, '')
                self.assertEqual(exit_code, 1)
                self.assertIn(f'HTTP {status} ({code})', err)
                self.assertNotIn(secret_body, err)
                self.assertNotIn('Traceback', err)

    def test_transport_error_does_not_echo_exception_details(self):
        from agent_hub_host import session_control

        def request(*_args, **_kwargs):
            raise session_control.SessionTransportError('transport failed; credential=hidden')

        out, err, exit_code = self._run_with_status(['schedule', 'show', 'daily-writer'], request)
        self.assertEqual(out, '')
        self.assertEqual(exit_code, 1)
        self.assertEqual(err.strip(), 'schedule operation failed: Hub transport error')
        self.assertNotIn('credential', err)
        self.assertNotIn('Traceback', err)

    def test_input_error_uses_cli_error_path_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, 'missing-prompt.txt')
            out, err, exit_code = self._run_with_status(
                ['schedule', 'create', '--name', 'writer', '--project', 'agent-hub',
                 '--cron', '0 9 * * *', '--prompt-file', missing_path],
                lambda *_args, **_kwargs: self.fail('input validation must fail before making an HTTP request'))

        self.assertEqual(out, '')
        self.assertEqual(exit_code, 1)
        self.assertEqual(err.strip(), 'schedule operation failed: Prompt: file not found')
        self.assertNotIn('Traceback', err)


if __name__ == '__main__':
    unittest.main()
