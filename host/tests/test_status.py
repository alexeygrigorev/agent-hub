"""Synthetic public CLI coverage for `hub status`; never loads operator configuration."""
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub_host import cli, status

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DTO = {
    'snapshot_at': '2026-10-17T00:00:00.000Z',
    'revision': 3,
    'tasks': [{
        'id': 'task-1', 'project': 'demo', 'status': 'open', 'title': 'Synthetic',
        'updated_at': '2026-10-17T00:00:00.000Z',
        'sessions': [{'machine_id': 'host-a', 'native_id': 'native-1', 'engine': 'opencode',
                       'phase': 'running', 'linkage': 'verified_launch',
                       'observed_at': '2026-10-17T00:00:00.000Z'}],
        'reports': [], 'commits': [],
        'pending_actions': [{'type': 'blocker', 'actor': 'demo:assistant',
                             'text_snippet': 'Synthetic blocker'}],
    }],
    'pagination': {'next': None, 'total': 1},
    'coverage': {'sessions': 'partial', 'missing_hosts': ['host-old']},
    'categories': {'diff': {'state': 'not_available'}},
}


class TestStatusCli(unittest.TestCase):
    def run_cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['hub', *args]), patch('sys.stdout', output), patch('sys.stderr', error):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), error.getvalue()

    def test_help_lists_status_flags(self):
        fixture = dict(os.environ, PYTHONPATH=str(ROOT / 'host'), PYTHONDONTWRITEBYTECODE='1',
                       HUB_CONFIG_FILE=str(ROOT / '.local/status/absent-fixture.json'))
        result = subprocess.run([sys.executable, '-m', 'agent_hub_host', 'status', '--help'],
                                env=fixture, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ('--since', '--json', '--task', '--project', '--host', '--local-only'):
            self.assertIn(flag, result.stdout)

    def test_json_and_human_agree_with_single_api_call(self):
        calls = []

        def fake_fetch(args):
            calls.append(dict(args.__dict__))
            return FIXTURE_DTO

        with patch.object(status, 'fetch_status', side_effect=fake_fetch):
            code, out, error = self.run_cli('status', '--since', '2h', '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(out)['revision'], 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get('since'), '2h')
        with patch.object(status, 'fetch_status', return_value=FIXTURE_DTO):
            code, out, error = self.run_cli('status', '--since', '2h')
        self.assertEqual(code, 0, error)
        self.assertIn('task-1', out)
        self.assertIn('missing hosts: host-old', out)
        self.assertIn('diff baseline: not_available', out)

    def test_since_task_project_host_limit_forwarded(self):
        seen = {}

        def fake_fetch(args):
            seen['query'] = status.build_query(args)
            return FIXTURE_DTO

        with patch.object(status, 'fetch_status', side_effect=fake_fetch):
            code, _, error = self.run_cli('status', '--task', 'task-1', '--project', 'demo',
                                          '--host', 'host-a', '--limit', '5')
        self.assertEqual(code, 0, error)
        for fragment in ('task=task-1', 'project=demo', 'host=host-a', 'limit=5'):
            self.assertIn(fragment, seen['query'])

    def test_invalid_limit_rejected_without_api_call(self):
        with patch.object(status, 'fetch_status') as transport:
            code, _, _ = self.run_cli('status', '--limit', '99')
        self.assertEqual(code, 1)
        transport.assert_not_called()

    def test_control_characters_escaped_in_human_output(self):
        dirty = dict(FIXTURE_DTO)
        dirty['tasks'] = [dict(FIXTURE_DTO['tasks'][0], title='Synthetic\x1b[2J\u202e')]
        with patch.object(status, 'fetch_status', return_value=dirty):
            code, out, error = self.run_cli('status')
        self.assertEqual(code, 0, error)
        for control in ('\x1b', '\u202e'):
            self.assertNotIn(control, out)
        self.assertIn('Synthetic', out)

    def test_hub_sessions_and_expected_offline_rendered(self):
        dto = dict(FIXTURE_DTO)
        dto['coverage'] = {'sessions': 'complete', 'missing_hosts': [],
                           'expected_offline': ['win35']}
        dto['tasks'] = [dict(FIXTURE_DTO['tasks'][0], **{
            'hub_sessions': [{
                'hub_session_id': '33333333-3333-4333-8333-333333333333',
                'linkage': 'hub_session', 'state': 'running', 'machine': 'host-a',
                'lifecycle': {'group': 'active', 'state': 'running',
                              'decision': {'kind': 'resume_on_reset'}},
            }],
            'lifecycle_summary': {'by_group': {'active': 1}, 'decisions': []},
        })]
        with patch.object(status, 'fetch_status', return_value=dto):
            code, out, error = self.run_cli('status')
        self.assertEqual(code, 0, error)
        self.assertIn('33333333-3333-4333-8333-333333333333', out)
        self.assertIn('lifecycle=active/running', out)
        self.assertIn('decision=resume_on_reset', out)
        self.assertIn('expected offline: win35', out)

    def test_local_only_preserves_diagnostics_without_api_call(self):
        with patch.object(status, 'fetch_status') as transport, \
             patch('agent_hub_host.config.load_config', return_value={'access_token': 'x', 'base_url': 'https://hub.test'}):
            code, out, error = self.run_cli('status', '--local-only')
        self.assertEqual(code, 0, error)
        transport.assert_not_called()
        self.assertIn('Logged in', out)


if __name__ == '__main__':
    unittest.main()
