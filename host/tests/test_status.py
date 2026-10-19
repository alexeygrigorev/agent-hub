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

    def test_default_uses_single_post_and_limit_falls_back_to_get(self):
        with patch.object(status, 'fetch_status_post', return_value=FIXTURE_DTO) as post, \
             patch.object(status, 'fetch_status_get', return_value=FIXTURE_DTO) as get:
            code, _, error = self.run_cli('status', '--since', '2h')
        self.assertEqual(code, 0, error)
        post.assert_called_once()
        get.assert_not_called()
        with patch.object(status, 'fetch_status_post', return_value=FIXTURE_DTO) as post, \
             patch.object(status, 'fetch_status_get', return_value=FIXTURE_DTO) as get:
            code, _, error = self.run_cli('status', '--limit', '5')
        self.assertEqual(code, 0, error)
        post.assert_not_called()
        get.assert_called_once()
        with patch.object(status, 'fetch_status_post', return_value=FIXTURE_DTO) as post, \
             patch.object(status, 'fetch_status_get', return_value=FIXTURE_DTO) as get:
            code, _, error = self.run_cli('status', '--after', 'task-1')
        self.assertEqual(code, 0, error)
        post.assert_not_called()
        get.assert_called_once()

    def test_summary_and_available_diff_rendered(self):
        dto = dict(FIXTURE_DTO)
        dto['summary'] = {'total': 27, 'complete': True, 'truncated': False,
                          'by_status': {'open': 27}}
        dto['categories'] = {'diff': {'state': 'available', 'added': ['t-new'],
                                      'removed': [], 'changed': [{'id': 't-1'}, {'id': 't-2'}],
                                      'baseline_advanced': True}}
        with patch.object(status, 'fetch_status', return_value=dto):
            code, out, error = self.run_cli('status')
        self.assertEqual(code, 0, error)
        self.assertIn('summary: 27 tasks complete', out)
        self.assertIn('open=27', out)
        self.assertIn('diff baseline: available added=1 removed=0 changed=2 advanced', out)

    def test_local_only_preserves_diagnostics_without_api_call(self):
        with patch.object(status, 'fetch_status') as transport, \
             patch('agent_hub_host.config.load_config', return_value={'access_token': 'x', 'base_url': 'https://hub.test'}):
            code, out, error = self.run_cli('status', '--local-only')
        self.assertEqual(code, 0, error)
        transport.assert_not_called()
        self.assertIn('Logged in', out)

    def test_render_human_includes_host_health(self):
        dto = dict(FIXTURE_DTO)
        dto['host_health'] = [{
            'machine_id': 'host-hetzner',
            'freshness': 'fresh',
            'load': {'load1': 0.75, 'load5': 1.10, 'load15': 1.25},
            'memory': {
                'total_bytes': 16 * 1024 * 1024 * 1024,
                'used_bytes': 8 * 1024 * 1024 * 1024,
                'available_bytes': 8 * 1024 * 1024 * 1024,
                'percent': 50.0,
                'status': 'ok'
            }
        }]
        rendered = status.render_human(dto)
        self.assertIn('host health:', rendered)
        self.assertIn('host-hetzner: load=0.75, 1.10, 1.25', rendered)
        self.assertIn('mem=8.0GB/16.0GB (50.0%, 8.0GB avail) [fresh]', rendered)

    def test_render_human_shows_canonical_reports_and_exact_probe_facts(self):
        dto = dict(FIXTURE_DTO)
        observed = '2026-10-19T01:38:08.858Z'
        sha = 'a' * 40
        dto['tasks'] = [{
            'reports': [{'reference': 'retained-reference'}],
            'commits': [{'reference': 'bbbbbbbbbbbb', 'ci_state': 'unknown',
                         'deploy_state': 'unknown'}],
            'hub_sessions': [{
                'hub_session_id': 'canonical-session',
                'host_observation': {
                    'state': 'agentworking', 'note': 'Running tests\x1b\u202e',
                    'observed_at': observed, 'provenance': {'freshness': 'fresh'},
                },
                'report_headline': {
                    'headline': 'Registry review\x1b\u202e', 'verdict': 'REVISE',
                    'observed_at': observed, 'provenance': {'freshness': 'fresh'},
                    'body': 'private report body must not be rendered',
                },
                'probe_evidence': {
                    'git': {'sha': sha, 'ancestry': 'merged_in_main',
                            'observed_at': observed, 'provenance': {'freshness': 'fresh'}},
                    'ci': {'state': 'passing', 'run': 'https://example.test/run/42',
                           'observed_at': observed, 'provenance': {'freshness': 'fresh'}},
                    'deploy': {'sha': sha, 'state': 'deployed',
                               'observed_at': observed, 'provenance': {'freshness': 'fresh'}},
                },
            }],
        }]
        rendered = status.render_human(dto)
        self.assertIn('host-observation: state=agentworking note=Running tests observed=%s freshness=fresh' % observed, rendered)
        self.assertIn('report: Registry review verdict=REVISE', rendered)
        self.assertIn('git sha=%s ancestry=merged_in_main' % sha, rendered)
        self.assertIn('ci state=passing run=https://example.test/run/42', rendered)
        self.assertIn('deploy sha=%s state=deployed' % sha, rendered)
        self.assertIn('observed=%s freshness=fresh' % observed, rendered)
        self.assertIn('report references: 1', rendered)
        self.assertIn('commit bbbbbbbbbbbb git=unknown ci=unknown deploy=unknown', rendered)
        self.assertNotIn('private report body', rendered)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)

    def test_render_human_preserves_unknown_and_stale_canonical_evidence(self):
        dto = dict(FIXTURE_DTO)
        dto['tasks'] = [{'hub_sessions': [{
            'host_observation': {'state': None, 'note': None, 'observed_at': None, 'provenance': {'freshness': 'stale'}},
            'report_headline': {'headline': 'PASS appears only in the heading', 'verdict': None},
            'probe_evidence': {'git': {'provenance': {'freshness': 'stale'}},
                               'ci': {}, 'deploy': None},
        }]}]
        rendered = status.render_human(dto)
        self.assertIn('host-observation: state=unknown observed=unknown freshness=stale', rendered)
        self.assertNotIn('note=', rendered)
        self.assertIn('report: PASS appears only in the heading verdict=unknown', rendered)
        self.assertIn('git sha=unknown ancestry=unknown observed=unknown freshness=stale', rendered)
        self.assertIn('ci state=unknown run=unknown observed=unknown freshness=unknown', rendered)
        self.assertNotIn('state=passing', rendered)
        self.assertNotIn('state=deployed', rendered)

    def test_render_human_shows_host_observation_without_note(self):
        dto = dict(FIXTURE_DTO)
        observed = '2026-10-19T01:38:08.858Z'
        dto['tasks'] = [{'hub_sessions': [{
            'host_observation': {
                'state': 'limit_hit',
                'observed_at': observed,
                'provenance': {'freshness': 'fresh'},
            },
        }]}]
        rendered = status.render_human(dto)
        self.assertIn('host-observation: state=limit_hit observed=%s freshness=fresh' % observed, rendered)
        self.assertNotIn('note=', rendered)

    def test_render_human_commits_probe_evidence_and_unknown(self):
        dto = dict(FIXTURE_DTO)
        dto['tasks'] = [{
            'id': 'task-commit-test',
            'status': 'in_progress',
            'title': 'Commit test',
            'updated_at': '2026-10-17T00:00:00.000Z',
            'commits': [
                {
                    'reference': '4dfd635a51df10be7fe2a133477333a02f983cba',
                    'git_ancestry': {'ancestry': 'on_main'},
                    'ci_state': 'failing',
                    'deploy_state': 'not_deployed',
                },
                {
                    'reference': 'abc123def456',
                    'ci_state': 'unknown',
                    'deploy_state': 'unknown',
                },
            ],
        }]
        rendered = status.render_human(dto)
        self.assertIn('commit 4dfd635a51df git=on_main ci=failing deploy=not_deployed', rendered)
        self.assertIn('commit abc123def456 git=unknown ci=unknown deploy=unknown', rendered)


if __name__ == '__main__':
    unittest.main()
