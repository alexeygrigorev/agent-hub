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

    def test_render_human_shows_task_current_and_continuation_state(self):
        dto = dict(FIXTURE_DTO)
        dto['tasks'] = [{
            'id': 'task-context-1',
            'status': 'in_progress',
            'title': 'Handoff test',
            'updated_at': '2026-10-19T08:00:00.000Z',
            'current_state': 'Running full tests\x1b[2J\u202e',
            'continuation_state': 'active',
            'owner': 'demo:assistant',
            'next_step': 'Ship increment',
        }]
        rendered = status.render_human(dto)
        self.assertIn('owner=demo:assistant next=Ship increment', rendered)
        self.assertIn('current-state: Running full tests', rendered)
        self.assertIn('continuation: active', rendered)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)

    def test_render_human_shows_task_report_references_with_evidence(self):
        dto = dict(FIXTURE_DTO)
        observed = '2026-10-19T08:30:00.000Z'
        dto['tasks'] = [{
            'id': 'task-reports-1',
            'status': 'open',
            'title': 'Report test',
            'updated_at': '2026-10-19T08:00:00.000Z',
            'reports': [
                {
                    'reference': '~/tmp/agent-reports/hub-0800.md',
                    'headline': 'Agent Hub Report — 08:00 Increment\x1b\u202e',
                    'verdict': 'PASS',
                    'provenance': {'observed_at': observed, 'freshness': 'fresh'},
                },
                {
                    'reference': '~/tmp/agent-reports/stale-report.md',
                    'headline': None,
                    'verdict': None,
                    'provenance': {'freshness': 'stale'},
                },
            ],
        }]
        rendered = status.render_human(dto)
        self.assertIn('report references: 2', rendered)
        self.assertIn('report-ref: ~/tmp/agent-reports/hub-0800.md headline=Agent Hub Report — 08:00 Increment verdict=PASS observed=%s freshness=fresh' % observed, rendered)
        self.assertIn('report-ref: ~/tmp/agent-reports/stale-report.md headline=unknown verdict=unknown observed=unknown freshness=stale', rendered)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)

    def test_render_human_omits_absent_task_context(self):
        dto = dict(FIXTURE_DTO)
        dto['tasks'] = [{
            'id': 'task-empty-context',
            'status': 'open',
            'title': 'Empty context task',
            'updated_at': '2026-10-19T08:00:00.000Z',
            'current_state': None,
            'continuation_state': None,
            'reports': [],
        }]
        rendered = status.render_human(dto)
        self.assertNotIn('current-state:', rendered)
        self.assertNotIn('continuation:', rendered)
        self.assertNotIn('report references:', rendered)
        self.assertNotIn('report-ref:', rendered)

    def test_render_human_shows_session_agent_state_and_freshness(self):
        dto = dict(FIXTURE_DTO)
        observed = '2026-10-19T09:00:00.000Z'
        dto['tasks'] = [{
            'id': 'task-agent-state-1',
            'status': 'in_progress',
            'title': 'Agent state test',
            'updated_at': observed,
            'sessions': [
                {
                    'machine_id': 'hetzner',
                    'native_id': 'native-working',
                    'engine': 'opencode',
                    'phase': 'running',
                    'agent_state': 'working\x1b\u202e',
                    'linkage': 'verified_launch',
                    'observed_at': observed,
                    'provenance': {'freshness': 'fresh'},
                },
                {
                    'machine_id': 'hetzner',
                    'native_id': 'native-idle',
                    'engine': 'codex',
                    'phase': 'running',
                    'agent_state': 'idle',
                    'linkage': 'unlinked_observation',
                    'observed_at': observed,
                    'provenance': {'freshness': 'stale'},
                },
            ],
        }]
        rendered = status.render_human(dto)
        self.assertIn('session hetzner/native-working engine=opencode phase=running agent=working linkage=verified_launch observed=%s freshness=fresh' % observed, rendered)
        self.assertIn('session hetzner/native-idle engine=codex phase=running agent=idle linkage=unlinked_observation observed=%s freshness=stale' % observed, rendered)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)

    def test_render_human_shows_task_partial_session_coverage(self):
        dto = dict(FIXTURE_DTO)
        dto['tasks'] = [{
            'id': 'task-cov-1',
            'status': 'open',
            'title': 'Coverage test',
            'updated_at': '2026-10-19T09:00:00.000Z',
            'session_coverage': {
                'coverage': 'partial',
                'missing_hosts': ['win35\x1b', 'host-backup\u202e'],
                'reason': 'linked_host_missing_or_stale',
            },
        }]
        rendered = status.render_human(dto)
        self.assertIn('session-coverage: partial missing=win35, host-backup', rendered)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)

    def test_render_human_shows_hub_session_decision_reason(self):
        dto = dict(FIXTURE_DTO)
        dto['tasks'] = [{
            'id': 'task-decision-1',
            'status': 'in_progress',
            'title': 'Decision reason test',
            'updated_at': '2026-10-19T09:00:00.000Z',
            'hub_sessions': [{
                'hub_session_id': '44444444-4444-4444-8444-444444444444',
                'linkage': 'hub_session',
                'state': 'running',
                'machine': 'hetzner',
                'lifecycle': {
                    'group': 'active',
                    'state': 'running',
                    'decision': {
                        'kind': 'reap\x1b',
                        'reason': 'heartbeat_expired\u202e',
                    },
                },
            }],
        }]
        rendered = status.render_human(dto)
        self.assertIn('hub-session 44444444-4444-4444-8444-444444444444 state=running machine=hetzner lifecycle=active/running decision=reap reason=heartbeat_expired', rendered)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)

    def test_render_human_omits_absent_session_diagnostics(self):
        dto = dict(FIXTURE_DTO)
        dto['tasks'] = [{
            'id': 'task-clean-session',
            'status': 'open',
            'title': 'Clean session task',
            'updated_at': '2026-10-19T09:00:00.000Z',
            'sessions': [{
                'machine_id': 'hetzner',
                'native_id': 'native-bare',
                'engine': 'codex',
                'phase': 'running',
                'agent_state': None,
                'linkage': 'verified_launch',
                'observed_at': '2026-10-19T09:00:00.000Z',
                'provenance': {},
            }],
            'session_coverage': {
                'coverage': 'complete',
                'missing_hosts': [],
            },
            'hub_sessions': [{
                'hub_session_id': '55555555-5555-4555-8555-555555555555',
                'linkage': 'hub_session',
                'state': 'running',
                'machine': 'hetzner',
                'lifecycle': {
                    'group': 'active',
                    'state': 'running',
                    'decision': {},
                },
            }],
        }]
        rendered = status.render_human(dto)
        self.assertIn('session hetzner/native-bare engine=codex phase=running linkage=verified_launch observed=2026-10-19T09:00:00.000Z', rendered)
        self.assertNotIn('agent=', rendered)
        self.assertNotIn('freshness=', rendered)
        self.assertNotIn('session-coverage:', rendered)
        self.assertNotIn('decision=', rendered)
        self.assertNotIn('reason=', rendered)
        self.assertNotIn('tag=', rendered)
        self.assertNotIn('workspace=', rendered)
        self.assertNotIn('flags=', rendered)

    def test_render_human_shows_session_and_hub_session_worktree_tag_flags(self):
        dto = dict(FIXTURE_DTO)
        observed = '2026-10-19T10:00:00.000Z'
        dto['tasks'] = [{
            'id': 'task-worktree-1',
            'status': 'in_progress',
            'title': 'Worktree honesty test',
            'updated_at': observed,
            'sessions': [{
                'machine_id': 'hetzner',
                'native_id': 'native-1',
                'engine': 'codex',
                'phase': 'running',
                'tag': 'push-hub-1400',
                'workspace': '/home/alexey/git/worktrees/push-1400/agent-hub',
                'linkage': 'verified_launch',
                'observed_at': observed,
                'provenance': {'freshness': 'fresh'},
            }],
            'hub_sessions': [{
                'hub_session_id': '66666666-6666-4666-8666-666666666666',
                'linkage': 'hub_session',
                'state': 'running',
                'machine': 'hetzner',
                'binding': {
                    'tag': 'platform',
                    'workspace': '/home/alexey/git/agent-coordination',
                },
                'lifecycle': {
                    'group': 'active',
                    'state': 'running',
                    'flags': ['worktree_missing'],
                    'decision': {'kind': 'reap', 'reason': 'worktree_vanished'},
                },
            }],
        }]
        rendered = status.render_human(dto)
        self.assertIn('session hetzner/native-1 engine=codex phase=running tag=push-hub-1400 workspace=/home/alexey/git/worktrees/push-1400/agent-hub linkage=verified_launch', rendered)
        self.assertIn('hub-session 66666666-6666-4666-8666-666666666666 state=running machine=hetzner lifecycle=active/running flags=worktree_missing decision=reap reason=worktree_vanished tag=platform workspace=/home/alexey/git/agent-coordination', rendered)

    def test_render_human_resolves_disk_aliases_and_handles_missing_primary(self):
        dto = dict(FIXTURE_DTO)
        dto['disk'] = [
            {
                'machine_id': 'host-hetzner',
                'mount_point': '/',
                'device_id': '66307',
                'is_primary': True,
                'percent': 70.0,
                'status': 'ok',
            },
            {
                'machine_id': 'host-hetzner',
                'mount_point': '/home',
                'device_id': '66307',
                'is_primary': False,
                'percent': 70.0,
                'status': 'ok',
            },
            {
                'machine_id': 'host-hetzner',
                'mount_point': '/data',
                'device_id': '66308',
                'is_primary': True,
                'percent': 60.0,
                'status': 'ok',
            },
            {
                'machine_id': 'host-hetzner',
                'mount_point': '/tmp',
                'device_id': '66308',
                'is_primary': False,
                'percent': 60.0,
                'status': 'ok',
            },
            {
                'machine_id': 'host-hetzner',
                'mount_point': '/var/explicit',
                'device_id': '99999',
                'is_primary': False,
                'primary_mount': '/custom/base',
                'percent': 45.0,
                'status': 'ok',
            },
            {
                'machine_id': 'host-hetzner',
                'mount_point': '/mnt/orphan',
                'device_id': '88888',
                'is_primary': False,
                'percent': 20.0,
                'status': 'ok',
            },
        ]
        rendered = status.render_human(dto)
        self.assertIn('host-hetzner /: 70.0% (ok)', rendered)
        self.assertIn('host-hetzner /home: 70.0% (ok) (alias of /)', rendered)
        self.assertIn('host-hetzner /data: 60.0% (ok)', rendered)
        self.assertIn('host-hetzner /tmp: 60.0% (ok) (alias of /data)', rendered)
        self.assertIn('host-hetzner /var/explicit: 45.0% (ok) (alias of /custom/base)', rendered)
        self.assertIn('host-hetzner /mnt/orphan: 20.0% (ok) (alias)', rendered)
        self.assertNotIn('(alias of )', rendered)

    def test_render_human_escapes_workspace_tag_and_flags(self):
        dto = dict(FIXTURE_DTO)
        dto['tasks'] = [{
            'id': 'task-ctrl-chars',
            'status': 'open',
            'title': 'Control char test',
            'updated_at': '2026-10-19T10:00:00.000Z',
            'sessions': [{
                'machine_id': 'hetzner',
                'native_id': 'native-1',
                'engine': 'codex',
                'phase': 'running',
                'tag': 'tag\x1b\u202e',
                'workspace': '/path/to\x1b/worktree\u202e',
                'linkage': 'verified_launch',
            }],
            'hub_sessions': [{
                'hub_session_id': '77777777-7777-4777-8777-777777777777',
                'linkage': 'hub_session',
                'state': 'running',
                'machine': 'hetzner',
                'binding': {
                    'tag': 'hubtag\x1b',
                    'workspace': '/hub/ws\u202e',
                },
                'lifecycle': {
                    'group': 'active',
                    'state': 'running',
                    'flags': ['flag\x1b1', 'flag2\u202e'],
                },
            }],
        }]
        rendered = status.render_human(dto)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)
        self.assertIn('tag=tag workspace=/path/to/worktree', rendered)
        self.assertIn('flags=flag1,flag2', rendered)
        self.assertIn('tag=hubtag workspace=/hub/ws', rendered)


if __name__ == '__main__':
    unittest.main()
