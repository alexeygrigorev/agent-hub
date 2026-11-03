"""Archive paging uses the same authorized intake transport and bounded inputs."""
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub_host import cli, intake

class TestArchiveCli(unittest.TestCase):
    def test_archived_history_cursor_and_limit(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'history', 'fixture-task', '--archived', '--after', '7', '--limit', '2']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={'items': [], 'next_after': None}) as request:
            cli.main()
        request.assert_called_once_with('GET', '/api/intake/tasks/fixture-task/history?archived=1&limit=2&after=7')
        self.assertEqual(json.loads(output.getvalue())['items'], [])

    def test_show_points_to_archived_history_with_count_and_read_command(self):
        output = io.StringIO()
        result = {'task': {'id': 'fixture-task', 'history_archive': {'count': 23, 'latest_sequence': 99,
                  'href': '/api/intake/tasks/fixture-task/history?archived=1'}}}
        with patch('sys.argv', ['hub', 'intake', 'show', 'fixture-task']), patch('sys.stdout', output), patch.object(intake, 'request', return_value=result):
            cli.main()
        shown = json.loads(output.getvalue())['task']['history_archive']
        self.assertEqual(shown['count'], 23)
        self.assertEqual(shown['read_command'], 'hub intake history fixture-task --archived --after 98 --limit 1')

    def test_append_prints_server_archive_location(self):
        output = io.StringIO()
        response = {'appended_event': {'archived': True, 'sequence': 99}}
        with patch('sys.argv', ['hub', 'intake', 'append', 'fixture-task', '--version', '4', '--key', 'retry-1', '--event-file', 'event.json']), \
             patch('sys.stdout', output), patch.object(intake, 'json_file', return_value={'kind': 'note', 'text': 'fixture'}), \
             patch.object(intake, 'request', return_value=response):
            cli.main()
        self.assertEqual(json.loads(output.getvalue())['appended_event'], {'archived': True, 'sequence': 99})

    def test_failed_append_persist_error_exits_nonzero(self):
        output = io.StringIO()
        errors = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'append', 'fixture-task', '--version', '4', '--key', 'retry-1', '--event-file', '-']), \
             patch.object(intake, 'json_file', return_value={'kind': 'note', 'text': 'fixture'}), \
             patch('sys.stdout', output), patch('sys.stderr', errors), \
             patch.object(intake, 'request', side_effect=intake.IntakeHTTPError(500, 'internal_error')):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('Intake HTTP 500: internal_error', errors.getvalue())

    def test_invalid_archive_page_never_reaches_transport(self):
        for option, value in [('--limit', '0'), ('--limit', '101'), ('--after', '-1')]:
            with self.subTest(option=option, value=value), patch('sys.argv', ['hub', 'intake', 'history', 'fixture-task', '--archived', option, value]), patch('sys.stderr', io.StringIO()), patch.object(intake, 'request') as request:
                with self.assertRaises(SystemExit):
                    cli.main()
                request.assert_not_called()


class TestQueuePriorityCli(unittest.TestCase):
    def test_promote_posts_cas_payload(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'promote', 'fixture-task', '--version', '3', '--key', 'promo-key']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={'task': {'id': 'fixture-task'}}) as request:
            cli.main()
        request.assert_called_once_with('POST', '/api/intake/tasks/fixture-task/promote', {'key': 'promo-key', 'expected_version': 3})
        self.assertEqual(json.loads(output.getvalue())['task']['id'], 'fixture-task')

    def test_promote_fetches_version_and_generates_key_when_omitted(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'promote', 'fixture-task']), patch('sys.stdout', output), patch.object(intake, 'request', side_effect=[{'task': {'control': {'version': 7}}}, {'task': {'id': 'fixture-task'}}]) as request:
            cli.main()
        self.assertEqual(request.call_args_list[0].args, ('GET', '/api/intake/tasks/fixture-task'))
        post = request.call_args_list[1].args
        self.assertEqual((post[0], post[1]), ('POST', '/api/intake/tasks/fixture-task/promote'))
        self.assertEqual(post[2]['expected_version'], 7)
        self.assertTrue(post[2]['key'].startswith('promote-'))

    def test_create_and_update_carry_priority(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'create', '--project', 'demo', '--title', 'T', '--text', 'x', '--priority', 'high', '--key', 'k']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={}) as request:
            cli.main()
        method, path, body = request.call_args.args
        self.assertEqual((method, path), ('POST', '/api/intake/tasks'))
        self.assertEqual(body['priority'], 'high')
        with patch('sys.argv', ['hub', 'intake', 'update', 'fixture-task', '--version', '2', '--key', 'k2', '--priority', 'low']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={}) as request:
            cli.main()
        method, path, body = request.call_args.args
        self.assertEqual((method, path), ('POST', '/api/intake/tasks/fixture-task'))
        self.assertEqual(body['priority'], 'low')

    def test_queue_lists_the_auto_queue(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'queue', '--project', 'demo', '--limit', '10']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={'items': [{'id': 'q-1', 'queue_position': 1}]}) as request:
            cli.main()
        request.assert_called_once_with('GET', '/api/intake/queue?project=demo&limit=10')
        self.assertEqual(json.loads(output.getvalue())['items'][0]['id'], 'q-1')


class TestMergeCheckCli(unittest.TestCase):
    def test_merge_check_omits_only_explicitly_superseded_report(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        other_id = '33333333-3333-4333-8333-333333333333'
        old_sha, new_sha, other_sha = 'a1' * 20, 'b2' * 20, 'c3' * 20
        shown = {'task': {'id': 'fixture-task', 'payload_data': {'history': [
            {'id': old_id, 'kind': 'report', 'reference': old_sha},
            {'id': other_id, 'kind': 'report', 'reference': other_sha},
            {'id': new_id, 'kind': 'report', 'reference': new_sha, 'supersedes': [old_id]},
        ]}}}
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'merge-check', 'fixture-task', '--repo', '/repo', '--dry-run']), \
                patch('sys.stdout', output), \
                patch.object(intake, 'request', return_value=shown) as request, \
                patch('agent_hub_host.merge_evidence.fetch_origin'), \
                patch('agent_hub_host.merge_evidence.collect', return_value={'commits': []}) as collect:
            cli.main()
        request.assert_called_once_with('GET', '/api/intake/tasks/fixture-task')
        collect.assert_called_once_with(str(Path('/repo').resolve()), [other_sha, new_sha], fetch=False,
                                        include_head=True, preferred_deploy_sha=new_sha)
        self.assertTrue(json.loads(output.getvalue())['dry_run'])

    def test_newer_superseding_report_drops_stale_merge_state_shas(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        old_sha, new_sha = 'a1' * 20, 'b2' * 20
        stale_head, stale_commit = 'c3' * 20, 'd4' * 20
        shown = {'task': {
            'id': 'fixture-task',
            'payload_data': {'history': [
                {'id': old_id, 'kind': 'report', 'reference': old_sha, 'captured_at': 100},
                {'id': new_id, 'kind': 'report', 'reference': new_sha, 'captured_at': 301,
                 'supersedes': [old_id]},
            ]},
            'merge_state': {'head': stale_head, 'commits': [{'sha': stale_commit}],
                            'activity_at': 200, 'evidence_at': 250},
        }}
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'merge-check', 'fixture-task', '--repo', '/repo', '--dry-run']), \
                patch('sys.stdout', output), \
                patch.object(intake, 'request', return_value=shown), \
                patch('agent_hub_host.merge_evidence.fetch_origin'), \
                patch('agent_hub_host.merge_evidence.collect', return_value={'commits': []}) as collect:
            cli.main()
        collect.assert_called_once_with(str(Path('/repo').resolve()), [new_sha], fetch=False,
                                        include_head=True, preferred_deploy_sha=new_sha)

    def test_older_superseding_report_keeps_merge_state_shas_required(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        old_sha, new_sha = 'a1' * 20, 'b2' * 20
        recorded_head, recorded_commit = 'c3' * 20, 'd4' * 20
        shown = {'task': {
            'id': 'fixture-task',
            'payload_data': {'history': [
                {'id': old_id, 'kind': 'report', 'reference': old_sha, 'captured_at': 100},
                {'id': new_id, 'kind': 'report', 'reference': new_sha, 'captured_at': 201,
                 'supersedes': [old_id]},
            ]},
            'merge_state': {'head': recorded_head, 'commits': [{'sha': recorded_commit}],
                            'activity_at': 200, 'evidence_at': 250},
        }}
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'merge-check', 'fixture-task', '--repo', '/repo', '--dry-run']), \
                patch('sys.stdout', output), \
                patch.object(intake, 'request', return_value=shown), \
                patch('agent_hub_host.merge_evidence.fetch_origin'), \
                patch('agent_hub_host.merge_evidence.collect', return_value={'commits': []}) as collect:
            cli.main()
        collect.assert_called_once_with(str(Path('/repo').resolve()),
                                        [recorded_head, new_sha, recorded_commit], fetch=False,
                                        include_head=True, preferred_deploy_sha=recorded_head)

    def test_private_clone_checkpoint_replaces_stored_head_and_is_preferred_deploy(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        old_sha, new_sha = 'a1' * 20, 'b2' * 20
        shown = {'task': {
            'id': 'fixture-task',
            'payload_data': {'history': [
                {'id': old_id, 'kind': 'report', 'reference': old_sha, 'captured_at': 100},
                {'id': new_id, 'kind': 'report', 'reference': new_sha, 'captured_at': 301,
                 'supersedes': [old_id]},
            ]},
            'merge_state': {'head': 'c3' * 20, 'commits': [{'sha': 'd4' * 20}],
                            'activity_at': 500, 'evidence_at': 250},
        }}
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'merge-check', 'fixture-task', '--dry-run']), \
                patch('sys.stdout', output), \
                patch.object(intake, 'request', return_value=shown), \
                patch('agent_hub_host.merge_evidence.own_clone', return_value='/private/repo'), \
                patch('agent_hub_host.merge_evidence.fetch_origin'), \
                patch('agent_hub_host.merge_evidence.collect', return_value={'commits': []}) as collect:
            cli.main()

        collect.assert_called_once_with('/private/repo', [new_sha], fetch=False,
                                        include_head=False, preferred_deploy_sha=new_sha)
        evidence = json.loads(output.getvalue())['evidence']
        self.assertEqual(evidence['head'], new_sha)

    def test_private_clone_without_checkpoint_prefers_latest_active_report(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        old_sha, new_sha = 'a1' * 20, 'b2' * 20
        shown = {'task': {'id': 'fixture-task', 'payload_data': {'history': [
            {'id': old_id, 'kind': 'report', 'reference': old_sha},
            {'id': new_id, 'kind': 'report', 'reference': new_sha,
             'supersedes': [old_id]},
        ]}}}
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'merge-check', 'fixture-task', '--dry-run']), \
                patch('sys.stdout', output), \
                patch.object(intake, 'request', return_value=shown), \
                patch('agent_hub_host.merge_evidence.own_clone', return_value='/private/repo'), \
                patch('agent_hub_host.merge_evidence.fetch_origin'), \
                patch('agent_hub_host.merge_evidence.collect', return_value={'commits': []}) as collect:
            cli.main()

        collect.assert_called_once_with('/private/repo', [new_sha], fetch=False,
                                        include_head=False, preferred_deploy_sha=new_sha)
        self.assertNotIn('head', json.loads(output.getvalue())['evidence'])

    def test_private_clone_after_checkpoint_keeps_persisted_head(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        current_sha = 'b2' * 20
        shown = {'task': {
            'id': 'fixture-task',
            'payload_data': {'history': [
                {'id': old_id, 'kind': 'report', 'reference': 'a1' * 20, 'captured_at': 100},
                {'id': new_id, 'kind': 'report', 'reference': current_sha, 'captured_at': 301,
                 'supersedes': [old_id]},
            ]},
            'merge_state': {'head': current_sha, 'commits': [{'sha': current_sha}],
                            'activity_at': 500, 'evidence_at': 400},
        }}
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'merge-check', 'fixture-task', '--dry-run']), \
                patch('sys.stdout', output), \
                patch.object(intake, 'request', return_value=shown), \
                patch('agent_hub_host.merge_evidence.own_clone', return_value='/private/repo'), \
                patch('agent_hub_host.merge_evidence.fetch_origin'), \
                patch('agent_hub_host.merge_evidence.collect', return_value={'commits': []}) as collect:
            cli.main()

        collect.assert_called_once_with('/private/repo', [current_sha, current_sha], fetch=False,
                                        include_head=False, preferred_deploy_sha=current_sha)
        self.assertNotIn('head', json.loads(output.getvalue())['evidence'])
