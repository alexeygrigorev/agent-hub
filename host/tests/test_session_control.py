"""Synthetic public CLI/HTTP coverage for hub sessions; never executes prompts."""
import argparse
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
from agent_hub_host import cli, session_control


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root


class TestSessionControlCli(unittest.TestCase):
    def parse(self, *args):
        return parser().parse_args(['sessions', *args])

    def test_manual_intake_link_uses_canonical_route(self):
        with patch.object(session_control, 'request', return_value={'intake_id': 'task-fixture'}) as transport:
            result = session_control.execute(self.parse('link', 'owned-worker', '--intake-task-id', 'task-fixture', '--key', 'link-once'))
        self.assertEqual(result['intake_id'], 'task-fixture')
        transport.assert_called_once_with('POST', '/api/sessions/owned-worker/link', {'key': 'link-once', 'intake_task_id': 'task-fixture'})

    def test_wait_stops_at_failed_send_instead_of_waiting_until_timeout(self):
        failed = {'id': 'cmd', 'state': 'failed',
                  'ack': {'reason': 'send_unsupported_headless'}}
        with patch.object(session_control, '_fetch_command_entry', return_value=failed) as fetch, \
                patch.object(session_control.time, 'sleep') as sleep:
            self.assertEqual(session_control.wait_for_command('owned', 'cmd', 60, 1), failed)
        fetch.assert_called_once_with('owned', 'cmd')
        sleep.assert_not_called()

    def test_audit_export_returns_full_dataset_without_json_flag(self):
        dataset = {'exported_at': '2026-10-28T12:00:00Z', 'runs': [],
                   'decisions': [{'rule': 'stuck_uncommitted', 'features': {'idle_s': 2400},
                                  'screen_tail': 'continue, commit, report'}]}
        with patch.object(session_control, 'request', return_value=dataset) as transport:
            out = session_control.execute(self.parse('audit', '--export', '--limit', '100'))
        self.assertEqual(transport.call_args.args[:2],
                         ('GET', '/api/sessions/audit/export?limit=100'))
        self.assertIs(out, dataset)
        self.assertEqual(out['decisions'][0]['features']['idle_s'], 2400)
        rendered = session_control.render_output(
            self.parse('audit', '--export', '--limit', '100'), dataset)
        self.assertIs(rendered, dataset)  # --export never discards features/screen tails
        # The action-log view still renders human-readable text.
        view = {'audit': {'state': 'not_run'}, 'runs': [{'id': 1, 'at': '2026-10-28T12:00:00Z',
                'source': 'scheduled', 'scanned': 1, 'counts': {'observed': 1}, 'alert': None}],
                'decisions': [{'id': 1, 'run_id': 1, 'at': '2026-10-28T12:00:00Z', 'session_id': 's',
                               'machine_id': 'm', 'native_id': 'n', 'intake': None, 'engine': 'codex',
                               'mode': 'headless', 'idle_s': 60, 'rule': 'recent_progress',
                               'action': 'observe', 'detail': 'd', 'dispatched': True,
                               'outcome': None, 'outcome_at': None}]}
        rendered = session_control.render_output(self.parse('audit', '--last', '1'), view)
        self.assertIsInstance(rendered, str)

    def test_protect_uses_the_owner_route_and_never_a_native_stop(self):
        with patch.object(session_control, 'request', return_value={'cleanup_protection': {'permanent': True}}) as transport:
            result = session_control.execute(self.parse('protect', 'owned-worker', '--key', 'protect-once'))
        self.assertTrue(result['cleanup_protection']['permanent'])
        transport.assert_called_once_with('POST', '/api/sessions/owned-worker/protect', {'key': 'protect-once'})
        with patch.object(session_control, 'request') as transport:
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('protect', '../foreign', '--key', 'protect-once'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('protect', 'owned-worker', '--key', 'bad/key'))
        transport.assert_not_called()

    def test_cancel_queued_intent_uses_a_stable_key_without_native_stop(self):
        with patch.object(session_control, 'request', return_value={'state': 'cancelled'}) as transport:
            result = session_control.execute(self.parse('cancel', 'queued-fixture', '--key', 'cancel-once'))
        self.assertEqual(result['state'], 'cancelled')
        transport.assert_called_once_with('POST', '/api/sessions/queued-fixture/cancel', {'key': 'cancel-once'})

        with patch.object(session_control, 'request') as transport:
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('cancel', '../other', '--key', 'cancel-once'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('cancel', 'queued-fixture', '--key', 'invalid/key'))
        transport.assert_not_called()

    def test_stop_wait_accepts_atomic_no_start_cancellation_without_host_command(self):
        receipt = {'state': 'cancelled', 'cancellation': {'effect': 'no_native_start'}}
        with patch.object(session_control, 'request', return_value=receipt) as transport:
            result = session_control.execute(self.parse('stop', 'queued-fixture', '--key', 'stop-queued', '--wait', '--timeout', '5'))
        self.assertEqual(result['stop_wait']['effect'], 'no_native_start')
        transport.assert_called_once_with('POST', '/api/sessions/queued-fixture/stop', {'key': 'stop-queued'})

    def test_stop_wait_does_not_accept_running(self):
        with patch.object(session_control, 'request', side_effect=[{'state': 'running'}, {'state': 'stopped'}]) as fetch, patch.object(session_control.time, 'sleep'):
            result = session_control.wait_for_session('s', 10, 1, stopping=True)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(fetch.call_count, 2)

    def test_list_show_commands_paths(self):
        with patch.object(session_control, 'request', return_value={'sessions': [], 'next_cursor': None}) as transport:
            result = session_control.execute(self.parse('list', '--limit', '10'))
        self.assertEqual(result, {'sessions': [], 'next_cursor': None})
        # 3.2: live=1 is the default list view; a terminal --state walks history.
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions?live=1&limit=10'))
        with patch.object(session_control, 'request', return_value={'sessions': [], 'next_cursor': None}) as transport:
            session_control.execute(self.parse('list', '--limit', '10', '--state', 'failed'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions?limit=10'))
        with patch.object(session_control, 'request', return_value={'id': 's'}) as transport:
            session_control.execute(self.parse('show', 's'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s'))
        with patch.object(session_control, 'request', return_value={'commands': []}) as transport:
            session_control.execute(self.parse('commands', 's'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s/commands'))

    def test_hub2_terminal_state_queries_include_history(self):
        for state in ('expired', 'completed-awaiting-review'):
            with self.subTest(state=state):
                row = {'id': 'historic', 'state': state}

                def history_only(_method, path):
                    return {'sessions': [] if 'live=1' in path else [row], 'next_cursor': None}

                with patch.object(session_control, 'request', side_effect=history_only):
                    result = session_control.execute(self.parse('list', '--limit', '10', '--state', state))
                self.assertEqual(result['sessions'], [row])

    def test_sessions_show_cli_displays_routed_model_and_effort(self):
        row = {'id': 'session-fixture', 'state': 'running', 'engine': 'claude',
               'model_id': 'sonnet', 'model': 'sonnet', 'effort': 'medium'}
        output = io.StringIO()
        with patch.object(session_control, 'request', return_value=row), \
                patch.object(sys, 'argv', ['hub', 'sessions', 'show', row['id']]), \
                patch('sys.stdout', output):
            cli.main()
        rendered = json.loads(output.getvalue())
        self.assertEqual((rendered['model_id'], rendered['model'], rendered['effort']),
                         ('sonnet', 'sonnet', 'medium'))

    def test_start_headless_brief_and_interactive_empty(self):
        with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--engine', 'codex', '--reason', 'codex required for test', '--brief-file', 'goal.txt', '--key', 'k'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['brief'], 'synthetic goal')
        self.assertEqual(payload['mode'], 'headless')
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--interactive', '--key', 'k'))
        self.assertEqual(transport.call_args.args[2]['mode'], 'interactive')
        self.assertNotIn('brief', transport.call_args.args[2])
        conflicting = self.parse('start', '--no-wait', '--workspace', 'demo', '--interactive', '--key', 'k')
        conflicting.brief_file = 'goal.txt'
        with patch.object(session_control, 'read_file', return_value='x'):
            with self.assertRaises(ValueError):
                session_control.execute(conflicting)

    def test_start_repo_only_sends_repo_without_client_host_path(self):
        args = self.parse('start', '--no-wait', '--repo', 'DataTalksClub/relay', '--prompt', 'do relay work', '--key', 'repo-key', '--no-wait')
        with patch.object(session_control, 'request', return_value={'id': 'session-repo', 'state': 'offered'}) as transport:
            session_control.execute(args)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['repo'], 'DataTalksClub/relay')
        self.assertNotIn('workspace', payload)

    def test_start_workspace_label_is_forwarded_for_target_host_resolution(self):
        args = self.parse('start', '--no-wait', '--workspace', 'relay', '--prompt', 'do relay work', '--key', 'label-key', '--no-wait')
        with patch.object(session_control, '_repo_from_workspace', return_value=None), \
                patch.object(session_control, 'request', return_value={'id': 'session-label', 'state': 'offered'}) as transport:
            session_control.execute(args)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['workspace'], 'relay')
        self.assertNotIn('repo', payload)

    def test_start_links_intake_task_for_acceptance_gate(self):
        with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--brief-file', 'goal.txt',
                                               '--key', 'k', '--intake-task-id', 'task-1'))
        self.assertEqual(transport.call_args.args[2]['intake_task_id'], 'task-1')
        with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--brief-file', 'goal.txt', '--key', 'k'))
        self.assertNotIn('intake_task_id', transport.call_args.args[2])

    def test_start_retry_and_goal_keep_intake_link_and_key(self):
        for mode in ([], ['--with-goal']):
            with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                    patch.object(session_control, 'request', return_value={'id': 'session-fixture'}) as transport:
                args = self.parse('start', '--no-wait', '--workspace', 'demo', '--brief-file', 'goal.txt',
                                  '--key', 'same-key', '--intake-task-id', 'task-fixture', '--no-wait', *mode)
                for _ in range(2):
                    session_control.execute(args)
                self.assertEqual(transport.call_count, 2)
                for call in transport.call_args_list:
                    self.assertEqual(call.args[:2], ('POST', '/api/sessions'))
                    self.assertEqual(call.args[2]['key'], 'same-key')
                    self.assertEqual(call.args[2]['intake_task_id'], 'task-fixture')
                    self.assertEqual(call.args[2].get('with_goal', False), bool(mode))

    def test_send_capture_stop_payloads(self):
        with patch.object(session_control, 'read_file', return_value='prompt'), \
                patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('send', 'sid', '--body-file', 'p.txt', '--key', 'k'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/sid/send'))
        self.assertEqual(transport.call_args.args[2], {'key': 'k', 'body': 'prompt'})
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('capture', 'sid', '--no-wait', '--max-bytes', '1024', '--key', 'k'))
        self.assertEqual(transport.call_args.args[2], {'key': 'k', 'max_bytes': 1024})
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('stop', 'sid', '--no-wait', '--key', 'k'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/sid/stop'))

    def test_capture_waits_for_correlated_output_by_default(self):
        with patch.object(session_control, 'request', return_value={'command_id': 'c1'}) as transport, \
                patch.object(session_control, 'wait_for_command', return_value={'state': 'acked', 'ack': {'capture': 'screen'}}) as wait:
            result = session_control.execute(self.parse('capture', 'sid', '--screen', '--key', 'k'))
        wait.assert_called_once_with('sid', 'c1', 30.0, 1.0)
        self.assertEqual(result['command_wait']['ack']['capture'], 'screen')

    def test_disabled_provider_error_has_actionable_policy_guidance(self):
        import os
        import urllib.error
        payload = {'error': 'provider_disabled', 'detail': 'private server body'}
        failure = urllib.error.HTTPError('https://hub.test/api/sessions', 400, 'disabled', {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                session_control.request('POST', '/api/sessions', {})
        self.assertIn('provider_disabled', str(raised.exception))
        self.assertIn('choose an allowed model', str(raised.exception))
        self.assertIn('change the model pool', str(raised.exception))
        # 2.4: human-readable server detail is printed (bounded to 300 chars,
        # whitespace-collapsed) instead of being swallowed by the slug filter.
        self.assertIn('detail: private server body', str(raised.exception))

    def test_request_errors_hide_server_bodies(self):
        import os
        import urllib.error
        failure = urllib.error.HTTPError('https://hub.test/api/sessions', 500, 'boom', {}, io.BytesIO(b'secret'))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                session_control.request('GET', '/api/sessions')
        self.assertNotIn('secret', str(raised.exception))
        self.assertNotIn('boom', str(raised.exception))

    def test_reaper_dry_run_calls_the_hub_and_renders_live_counts(self):
        args = self.parse('reap', '--dry-run')
        result = {'dry_run': True, 'reaped': 0, 'stop_requested': 0,
                  'active_count_before': 12, 'active_count_after': 12}
        with patch.object(session_control, 'request', return_value=result) as transport:
            actual = session_control.execute(args)
        transport.assert_called_once_with('POST', '/api/sessions/reap', {'dry_run': True})
        self.assertIs(actual, result)
        self.assertIn('live active count 12 -> 12', session_control.render_output(args, result))

    def test_pick_is_a_dry_run_and_start_reports_the_chosen_route(self):
        args = self.parse('pick', '--tier', 'deep')
        result = {'route': 'claude/opus/medium', 'policy_id': 'claude-opus-medium', 'machine_id': 'h1',
                  'pool': [{'policy_id': 'claude-opus-medium', 'share': 0.25}],
                  'excluded': [{'policy_id': 'x', 'reason': 'credential_expired'}]}
        with patch.object(session_control, 'request', return_value=result) as transport:
            actual = session_control.execute(args)
        transport.assert_called_once_with('POST', '/api/model-picker/pick', {'tier': 'deep'})
        text = session_control.render_output(args, actual)
        self.assertIn('Picked route: claude/opus/medium', text)
        self.assertIn('EXCLUDED x (credential_expired)', text)
        started = {'route': 'codex/gpt-6-luna/max', 'engine': 'codex', 'model': 'gpt-6-luna', 'effort': 'max'}
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            self.assertIs(session_control.render_output(argparse.Namespace(sessions_cmd='start', json=False), started), started)
        self.assertIn('Picked route: codex/gpt-6-luna/max', err.getvalue())

    def test_inputs_are_bounded(self):
        with self.assertRaises(ValueError):
            session_control.read_file(__file__, limit=10)

    def test_wait_flag_validation(self):
        args = self.parse('start', '--workspace', 'demo', '--wait', '--timeout', '10', '--interval', '2')
        self.assertEqual(session_control.check_wait_args(args), (10.0, 2.0))
        bad = self.parse('start', '--workspace', 'demo', '--wait', '--timeout', '0')
        with self.assertRaises(ValueError):
            session_control.check_wait_args(bad)
        bad_interval = self.parse('send', 'sid', '--body-file', 'p.txt', '--wait', '--interval', '0.1')
        with self.assertRaises(ValueError):
            session_control.check_wait_args(bad_interval)

    def test_start_wait_polls_binding_to_running(self):
        args = self.parse('start', '--workspace', 'demo', '--key', 'k1', '--wait', '--timeout', '30', '--interval', '1')
        views = [{'session': {'id': 's1', 'state': 'offered'}}, {'session': {'id': 's1', 'state': 'running', 'native_id': 'native-1'}}]
        with patch.object(session_control, 'request', side_effect=[{'id': 's1', 'state': 'offered'}, *views]) as transport, \
                patch.object(session_control.time, 'sleep', return_value=None):
            result = session_control.execute(args)
        self.assertEqual(result['client_key'], 'k1')
        self.assertEqual(result['binding_wait']['session']['state'], 'running')
        self.assertEqual(result['state'], 'running')
        self.assertEqual(result['native_id'], 'native-1')
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s1'))

    def test_start_wait_reports_prelaunch_refusal_instead_of_returning_accepted(self):
        args = self.parse('start', '--workspace', 'relay', '--key', 'k-refused', '--wait', '--timeout', '30', '--interval', '1')
        refused = {'session': {'id': 's-refused', 'state': 'failed', 'native_id': None,
                               'attention': {'reason': 'workspace_label_unavailable'}}}
        with patch.object(session_control, 'request', side_effect=[{'id': 's-refused', 'state': 'offered'}, refused]), \
                patch.object(session_control.time, 'sleep', return_value=None):
            with self.assertRaisesRegex(ValueError, 'workspace_label_unavailable'):
                session_control.execute(args)

    def test_send_wait_ignores_transport_until_correlated_ack(self):
        args = self.parse('send', 's1', '--body-file', 'p.txt', '--key', 'k2', '--wait', '--timeout', '30', '--interval', '1')
        listing = lambda state: {'commands': [{'id': 'c1', 'state': state}]}
        with patch.object(session_control, 'read_file', return_value='prompt'), \
                patch.object(session_control, 'request', side_effect=[
                    {'command_id': 'c1', 'state': 'recorded'},
                    listing('recorded'), listing('transported'), listing('acked'),
                ]), \
                patch.object(session_control.time, 'sleep', return_value=None):
            result = session_control.execute(args)
        # Transported alone never completes the wait; only the correlated ack does.
        self.assertEqual(result['command_wait']['state'], 'acked')

    def test_wait_timeout_names_key_for_reconciliation(self):
        args = self.parse('stop', 's1', '--key', 'k3', '--timeout', '2')
        with patch.object(session_control, 'request', return_value={'command_id':'c9','state':'recorded'}), \
                patch.object(session_control, 'wait_for_command', side_effect=ValueError('Command wait timed out')):
            with self.assertRaisesRegex(ValueError, 'stop_pending; command c9'):
                session_control.execute(args)

    def test_lost_response_keeps_key_for_safe_retry(self):
        args = self.parse('send', 's1', '--body-file', 'p.txt', '--key', 'k4')
        with patch.object(session_control, 'read_file', return_value='prompt'), \
                patch.object(session_control, 'request', side_effect=ValueError('Session HTTP request failed with status 502')):
            with self.assertRaises(ValueError) as raised:
                session_control.execute(args)
        self.assertIn('k4', str(raised.exception))
        self.assertNotIn('prompt', str(raised.exception))

    def test_list_state_filters_without_echo(self):
        args = self.parse('list', '--state', 'running')
        payload = {'sessions': [{'id': 'a', 'state': 'running'}, {'id': 'b', 'state': 'offered'}], 'next_cursor': None}
        with patch.object(session_control, 'request', return_value=payload):
            result = session_control.execute(args)
        self.assertEqual([s['id'] for s in result['sessions']], ['a'])

    def test_default_list_and_json_follow_all_api_pages(self):
        args = self.parse('list', '--json')
        first = {'sessions': [{'id': str(i), 'state': 'running'} for i in range(100)],
                 'next_cursor': 'cursor-1', 'active_count': 125, 'active_count_capped': False}
        second = {'sessions': [{'id': str(i), 'state': 'running'} for i in range(100, 125)],
                  'next_cursor': None, 'active_count': 125, 'active_count_capped': False}
        with patch.object(session_control, 'request', side_effect=[first, second]) as transport:
            result = session_control.execute(args)
        self.assertEqual(len(result['sessions']), 125)
        self.assertEqual(result['sessions'][-1]['id'], '124')
        self.assertEqual(result['active_count'], 125)
        self.assertIsNone(result['next_cursor'])
        self.assertEqual(transport.call_args_list[0].args[:2], ('GET', '/api/sessions?live=1&limit=100'))
        self.assertEqual(transport.call_args_list[1].args[:2], ('GET', '/api/sessions?live=1&limit=100&cursor=cursor-1'))

    def test_state_filter_walks_pages_until_all_matches_are_found(self):
        args = self.parse('list', '--state', 'running')
        first = {'sessions': [{'id': 'a', 'state': 'offered'}], 'next_cursor': 'page-two'}
        second = {'sessions': [{'id': 'b', 'state': 'running'}], 'next_cursor': None}
        with patch.object(session_control, 'request', side_effect=[first, second]) as transport:
            result = session_control.execute(args)
        self.assertEqual([s['id'] for s in result['sessions']], ['b'])
        self.assertEqual(transport.call_count, 2)

    def test_list_reads_500_old_hub2_rows_and_normalizes_completion(self):
        pages = []
        for start in range(0, 500, 100):
            cursor = 'page/%d+next' % (start + 100) if start < 400 else None
            pages.append({'items': [{'id': str(i), 'state': 'running'} for i in range(start, start + 100)],
                          'next': cursor, 'complete': cursor is None})
        with patch.object(session_control, 'request', side_effect=pages) as transport:
            result = session_control.execute(self.parse('list', '--json'))
        self.assertEqual(len(result['sessions']), 500)
        self.assertEqual(result['sessions'], result['items'])
        self.assertEqual(result['sessions'][-1]['id'], '499')
        self.assertTrue(result['complete'])
        self.assertIsNone(result['next'])
        self.assertIsNone(result['next_cursor'])
        self.assertEqual(transport.call_count, 5)
        self.assertIn('cursor=page%2F100%2Bnext', transport.call_args_list[1].args[1])

    def test_hub2_next_is_followed_when_legacy_alias_is_null(self):
        pages = [
            {'sessions': [{'id': 'a'}], 'next_cursor': None, 'next': 'second', 'complete': False},
            {'sessions': [{'id': 'a'}, {'id': 'b'}], 'next_cursor': None, 'next': None, 'complete': True},
        ]
        with patch.object(session_control, 'request', side_effect=pages):
            result = session_control.execute(self.parse('list'))
        self.assertEqual([row['id'] for row in result['sessions']], ['a', 'b'])
        self.assertTrue(result['complete'])

    def test_limited_filtered_list_keeps_honest_continuation(self):
        pages = [
            {'items': [{'id': 'a', 'state': 'offered'}, {'id': 'b', 'state': 'running'},
                       {'id': 'c', 'state': 'running'}], 'next': 'second', 'complete': False},
            {'items': [{'id': 'd', 'state': 'running'}], 'next': 'third', 'complete': False},
        ]
        with patch.object(session_control, 'request', side_effect=pages) as transport:
            result = session_control.execute(self.parse('list', '--state', 'running', '--limit', '3'))
        self.assertEqual([row['id'] for row in result['sessions']], ['b', 'c', 'd'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['next'], 'third')
        self.assertEqual(result['next_cursor'], 'third')
        self.assertIn('limit=1&cursor=second', transport.call_args_list[1].args[1])

    def test_malformed_or_unqualified_session_pages_fail_loudly(self):
        for page in [
            {},
            {'items': [], 'complete': 'false'},
            {'sessions': [], 'complete': False},
            {'sessions': [], 'complete': True, 'next': 'more'},
            {'sessions': [], 'next_cursor': 'one', 'next': 'two'},
            {'sessions': [], 'next': False},
            {'sessions': [{'state': 'running'}]},
            {'sessions': 'not-a-list'},
        ]:
            with self.subTest(page=page), patch.object(session_control, 'request', return_value=page):
                with self.assertRaises(ValueError):
                    session_control.execute(self.parse('list'))

    def test_repeated_old_hub2_cursor_does_not_loop_or_claim_complete(self):
        page = {'items': [{'id': 'a'}], 'next': 'repeat', 'complete': False}
        with patch.object(session_control, 'request', side_effect=[page, page]) as transport:
            with self.assertRaisesRegex(ValueError, 'repeated a cursor'):
                session_control.execute(self.parse('list'))
        self.assertEqual(transport.call_count, 2)

    def test_repeated_cursor_is_rejected_even_when_limit_is_filled(self):
        pages = [
            {'items': [{'id': 'a'}], 'next': 'repeat', 'complete': False},
            {'items': [{'id': 'b'}], 'next': 'repeat', 'complete': False},
        ]
        with patch.object(session_control, 'request', side_effect=pages):
            with self.assertRaisesRegex(ValueError, 'repeated a cursor'):
                session_control.execute(self.parse('list', '--limit', '2'))

    def test_start_prompt_inline_with_brief_boundaries(self):
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'do the thing', '--key', 'k'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['brief'], 'do the thing')
        self.assertEqual(payload['mode'], 'headless')
        # Mutually exclusive with --brief-file and --interactive.
        with self.assertRaises(SystemExit):
            self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'x', '--brief-file', 'goal.txt')
        with self.assertRaises(SystemExit):
            self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'x', '--interactive')
        # Same 8 KiB UTF-8 boundaries as --brief-file, never executed locally.
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}):
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', '   ', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'é' * 5000, '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'x' * 8001, '--key', 'k'))

    def test_send_prompt_inline_with_body_boundaries(self):
        with patch.object(session_control, 'request', return_value={'command_id': 'c1'}) as transport:
            result = session_control.execute(self.parse('send', 'sid', '--prompt', 'do inline work', '--key', 'k'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/sid/send'))
        self.assertEqual(transport.call_args.args[2], {'key': 'k', 'body': 'do inline work'})
        self.assertEqual(result['command_id'], 'c1')
        self.assertEqual(result['client_key'], 'k')

        # Mutually exclusive with --body-file; one required.
        with self.assertRaises(SystemExit):
            self.parse('send', 'sid', '--prompt', 'x', '--body-file', 'p.txt')
        with self.assertRaises(SystemExit):
            self.parse('send', 'sid')

        # Same 16 KiB UTF-8 boundaries as --body-file, non-empty, never executed locally.
        with patch.object(session_control, 'request', return_value={}):
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('send', 'sid', '--prompt', '   ', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('send', 'sid', '--prompt', '', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('send', 'sid', '--prompt', 'é' * 9000, '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('send', 'sid', '--prompt', 'x' * (session_control.MAX_BODY + 1), '--key', 'k'))

        # Exact boundary: max 16 KiB UTF-8 is accepted.
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('send', 'sid', '--prompt', 'x' * session_control.MAX_BODY, '--key', 'k'))
        self.assertEqual(transport.call_args.args[2]['body'], 'x' * session_control.MAX_BODY)

        # Direct execution check: mutual exclusion and required body source.
        conflicting = self.parse('send', 'sid', '--body-file', 'p.txt', '--key', 'k')
        conflicting.prompt = 'conflict'
        with self.assertRaises(ValueError):
            session_control.execute(conflicting)
        missing = self.parse('send', 'sid', '--body-file', 'p.txt', '--key', 'k')
        missing.body_file = None
        missing.prompt = None
        with self.assertRaises(ValueError):
            session_control.execute(missing)

    def test_start_name_stored_and_validated(self):
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'goal', '--name', 'friendly', '--key', 'k'))
        self.assertEqual(transport.call_args.args[2]['name'], 'friendly')
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}):
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--interactive', '--name', 'empty-ok', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--name', '../escape', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--name', '', '--key', 'k'))

    def test_host_receive_requires_once_and_machine(self):
        args = self.parse('host-receive', '--once', '--machine-id', 'hetzner1')
        self.assertTrue(args.once)
        self.assertEqual(args.machine_id, 'hetzner1')
        self.assertEqual(args.limit, 20)
        with patch('agent_hub_host.session_receiver.receive_once', return_value={'offers_seen': 0}) as recv:
            result = session_control.execute(args)
        self.assertEqual(result, {'offers_seen': 0})
        self.assertTrue(recv.called)
        # Bounded limit enforced.
    def test_qualified_alias_routing_for_show_send_capture_stop(self):
        with patch.object(session_control, 'request', return_value={'id': 'native-1', 'qualified_alias': 'dataops:batching'}) as transport:
            session_control.execute(self.parse('show', 'dataops:batching'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/dataops:batching'))

        with patch.object(session_control, 'request', return_value={'id': 'native-2', 'qualified_alias': 'git:crm-redesign'}) as transport:
            session_control.execute(self.parse('show', 'git:crm-redesign'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/git:crm-redesign'))

        with patch.object(session_control, 'read_file', return_value='payload'), \
                patch.object(session_control, 'request', return_value={'command_id': 'c1', 'state': 'recorded'}) as transport:
            session_control.execute(self.parse('send', 'dataops:batching', '--body-file', 'p.txt', '--key', 'k1'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/dataops:batching/send'))

        with patch.object(session_control, 'request', return_value={'command_id': 'c2', 'state': 'recorded'}) as transport:
            session_control.execute(self.parse('capture', 'git:crm-redesign', '--no-wait', '--key', 'k2'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/git:crm-redesign/capture'))

        with patch.object(session_control, 'request', return_value={'command_id': 'c3', 'state': 'recorded'}) as transport:
            session_control.execute(self.parse('stop', 'dataops:batching', '--no-wait', '--key', 'k3'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/dataops:batching/stop'))

        with patch.object(session_control, 'request', return_value={'commands': []}) as transport:
            session_control.execute(self.parse('commands', 'dataops:batching'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/dataops:batching/commands'))

        # Friendly name check strictly excludes colon (separate qualified alias resolver preserving friendly name validation)
        with self.assertRaises(ValueError):
            session_control.check_session_name('dataops:batching')

    def test_start_lineage_parent_and_headless_flags(self):
        with patch.object(session_control, 'request', return_value={'id': 's-child'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--parent', 'parent-123', '--headless', '--key', 'k-lineage', '--no-wait'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['parent_id'], 'parent-123')
        self.assertEqual(payload['mode'], 'headless')

    def test_sessions_tree_routing(self):
        with patch.object(session_control, 'request', return_value={'tree': []}) as transport:
            session_control.execute(self.parse('tree'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/tree'))

        with patch.object(session_control, 'request', return_value={'tree': []}) as transport:
            session_control.execute(self.parse('list', '--tree'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/tree'))

    def test_render_table_and_tree_formatters(self):
        sample_sessions = [
            {
                'id': 'uuid-1',
                'session_type': 'aplexer',
                'qualified_alias': 'dataops:batching',
                'engine': 'gemini',
                'state': 'running',
                'workspace': '/home/alexey/git/dataops',
                'updated_at': '2026-10-17T19:40:00.000Z'
            },
            {
                'id': 'uuid-2',
                'session_type': 'headless',
                'qualified_alias': None,
                'name': 'worker-1',
                'engine': 'codex',
                'state': 'running',
                'workspace': '/home/alexey/git/agent-hub',
                'updated_at': '2026-10-17T19:42:00.000Z'
            }
        ]
        table = session_control.render_table(sample_sessions)
        self.assertIn('TYPE', table)
        self.assertIn('NAME', table)
        self.assertIn('dataops:batching', table)
        self.assertIn('worker-1', table)
        self.assertIn('aplexer', table)
        self.assertIn('headless', table)

        sample_tree = {
            'tree': [
                {
                    'id': 'uuid-1',
                    'session_type': 'aplexer',
                    'qualified_alias': 'dataops:batching',
                    'state': 'running',
                    'workspace': '/home/alexey/git/dataops',
                    'children': [
                        {
                            'id': 'uuid-2',
                            'session_type': 'headless',
                            'name': 'worker-1',
                            'state': 'running',
                            'workspace': '/home/alexey/git/agent-hub',
                            'children': []
                        }
                    ]
                }
            ]
        }
        tree = session_control.render_tree(sample_tree)
        self.assertIn('Sessions Lineage Tree:', tree)
        self.assertIn('[aplexer] dataops:batching (running)', tree)
        self.assertIn('[headless] worker-1 (running)', tree)
        self.assertIn('└── ', tree)

        # render_output with --json flag returns raw data
        args_json = self.parse('list', '--json')
        out_json = session_control.render_output(args_json, {'sessions': sample_sessions})
        self.assertIsInstance(out_json, dict)

        # render_output without --json returns formatted string
        args_plain = self.parse('list')
        out_plain = session_control.render_output(args_plain, {'sessions': sample_sessions})
        self.assertIsInstance(out_plain, str)
        self.assertIn('dataops:batching', out_plain)

    def test_start_overrides_do_not_require_a_reason(self):
        for options in [('--engine','codex'),('--tier','deep'),('--model','gpt-6-astra')]:
            with self.subTest(options=options), patch.object(session_control,'request',return_value={'id':'fixture-session'}) as transport:
                session_control.execute(self.parse('start', '--no-wait','--workspace','demo','--no-wait',*options,'--key','k'))
                self.assertNotIn('engine_override_reason',transport.call_args.args[2])

    def test_dedicated_override_requires_engine_but_reason_is_optional(self):
        with self.assertRaises(SystemExit):
            self.parse('override','--workspace','demo')
        with patch.object(session_control,'request',return_value={'id':'fixture-session'}) as transport:
            session_control.execute(self.parse('override','--workspace','demo','--engine','codex','--reason','need codex','--key','k'))
        self.assertEqual(transport.call_args.args[2]['engine_override_reason'],'need codex')

    def test_start_rejects_every_idless_receipt_even_with_no_wait(self):
        for receipt in [{},{'ok':True},{'session':{}},{'id':42},{'id':' '},{'id':'bad/path'}]:
            with self.subTest(receipt=receipt), patch.object(session_control,'request',return_value=receipt), self.assertRaisesRegex(ValueError,'no session id'):
                session_control.execute(self.parse('start','--workspace','demo','--no-wait','--key','k'))

    def test_list_state_filtering_waiting_and_idle(self):
        raw_sessions = [
            {'id': 's1', 'state': 'running', 'agent_state': 'running', 'waiting_confirmation': False},
            {'id': 's2', 'state': 'running', 'agent_state': 'waiting', 'waiting_confirmation': True},
            {'id': 's3', 'state': 'running', 'agent_state': 'idle', 'waiting_confirmation': False},
            {'id': 's4', 'state': 'stopped', 'agent_state': None, 'waiting_confirmation': False},
        ]
        with patch.object(session_control, 'request', return_value={'sessions': raw_sessions}):
            w_res = session_control.execute(self.parse('list', '--state', 'waiting'))
            self.assertEqual(len(w_res['sessions']), 1)
            self.assertEqual(w_res['sessions'][0]['id'], 's2')

            wc_res = session_control.execute(self.parse('list', '--state', 'waiting-confirmation'))
            self.assertEqual(len(wc_res['sessions']), 1)
            self.assertEqual(wc_res['sessions'][0]['id'], 's2')

            idle_res = session_control.execute(self.parse('list', '--state', 'idle'))
            self.assertEqual(len(idle_res['sessions']), 2)
            self.assertEqual({s['id'] for s in idle_res['sessions']}, {'s2', 's3'})

    def test_missing_brief_file_raises_session_input_error(self):
        args = self.parse('start', '--no-wait', '--workspace', 'demo', '--brief-file', '/nonexistent/brief.txt', '--key', 'k')
        with self.assertRaises(session_control.SessionInputError) as ctx:
            session_control.execute(args)
        self.assertIn('Brief file: file not found', str(ctx.exception))
        diag = session_control.diagnostic(ctx.exception)
        self.assertEqual(diag, 'Brief file: file not found')

    def test_brief_file_exceeding_character_limit_raises_input_error(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as tf:
            tf.write('x' * (session_control.MAX_BRIEF_CHARS + 10))
            tf_path = tf.name
        try:
            args = self.parse('start', '--no-wait', '--workspace', 'demo', '--brief-file', tf_path, '--key', 'k')
            with self.assertRaises(session_control.SessionInputError) as ctx:
                session_control.execute(args)
            self.assertIn('character limit', str(ctx.exception))
            self.assertIn('8000', str(ctx.exception))
        finally:
            import os
            os.unlink(tf_path)

    def test_prompt_character_limit(self):
        args = self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'do task', '--key', 'k')
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(args)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['brief'], 'do task')

        long_prompt = 'y' * (session_control.MAX_BRIEF_CHARS + 5)
        bad_args = self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', long_prompt, '--key', 'k')
        with self.assertRaises(session_control.SessionInputError) as ctx:
            session_control.execute(bad_args)
        self.assertIn('character limit', str(ctx.exception))

    def test_server_http_error_surfaces_status_error_field_and_detail(self):
        import urllib.error
        payload = {
            'error': 'invalid_session_request',
            'field': 'brief',
            'detail': 'invalid_brief',
        }
        failure = urllib.error.HTTPError('https://hub.test/api/sessions', 400, 'Bad Request', {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(session_control.SessionHTTPError) as ctx:
                session_control.request('POST', '/api/sessions', {'key': 'k-err'})
        msg = str(ctx.exception)
        self.assertIn('status 400', msg)
        self.assertIn('invalid_session_request', msg)
        self.assertIn('field: brief', msg)
        self.assertIn('detail: invalid_brief', msg)

    def test_server_tbm_admission_denied_surfaces_reason_code(self):
        import urllib.error
        payload = {
            'error': 'tbm_admission_denied',
            'reason': 'kill_switch_active',
        }
        failure = urllib.error.HTTPError('https://hub.test/api/sessions', 409, 'Conflict', {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(session_control.SessionHTTPError) as ctx:
                session_control.request('POST', '/api/sessions', {'key': 'k-tbm'})
        msg = str(ctx.exception)
        self.assertIn('status 409', msg)
        self.assertIn('tbm_admission_denied', msg)
        self.assertIn('reason: kill_switch_active', msg)

    def test_transport_error_diagnostics(self):
        import socket
        import urllib.error
        dns_err = urllib.error.URLError(socket.gaierror(-2, 'Name or service not known'))
        self.assertIn('DNS lookup failed', session_control.diagnostic(dns_err))

        timeout_err = TimeoutError('timed out')
        self.assertIn('timed out', session_control.diagnostic(timeout_err))

        json_err = RuntimeError('Expected JSON response')
        self.assertIn('Expected JSON response', session_control.diagnostic(json_err))

    def test_start_extracts_id_from_intake_url(self):
        args = self.parse('start', '--no-wait', '--workspace', 'demo', '--intake-task-id', 'https://hub.test/#/intake/task-url-1234', '--key', 'k')
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(args)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['intake_task_id'], 'task-url-1234')

    def test_start_intake_worktree_flags(self):
        # afab8367: a successor start replaces a live predecessor; Hub plans the
        # fresh sibling worktree, so a named cwd is refused client-side.
        args = self.parse('start', '--no-wait', '--workspace', 'demo', '--intake-task-id', 'task-1',
                          '--intake-worktree', 'successor', '--key', 'k-succ')
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(args)
        self.assertEqual(transport.call_args.args[2]['intake_worktree'], 'successor')
        self.assertNotIn('cwd', transport.call_args.args[2])
        args = self.parse('start', '--no-wait', '--workspace', 'demo', '--intake-task-id', 'task-1',
                          '--intake-worktree', 'detached', '--key', 'k-det')
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(args)
        self.assertEqual(transport.call_args.args[2]['intake_worktree'], 'detached')
        args = self.parse('start', '--no-wait', '--workspace', 'demo', '--intake-task-id', 'task-1',
                          '--intake-worktree', 'successor', '--cwd', 'sub/dir', '--key', 'k-bad')
        with self.assertRaisesRegex(ValueError, 'drop --cwd'):
            session_control.execute(args)

    def test_commands_rendering_shows_waiting_in_inbox_and_reason(self):
        raw = {
            'commands': [
                {
                    'id': 'cmd-12345678-0000',
                    'kind': 'send',
                    'state': 'waiting in inbox for recipient',
                    'reason': 'aplexer inbox recorded; recipient has not acknowledged',
                    'created_at': '2026-10-24T06:00:00Z',
                }
            ]
        }
        rendered = session_control.render_commands(raw)
        self.assertIn('waiting in inbox for recipient', rendered)
        self.assertIn('aplexer inbox recorded; recipient has not acknowledged', rendered)

    def test_send_warns_on_target_no_reported_state(self):
        import io
        args = self.parse('send', 's1', '--prompt', 'hello', '--key', 'k-warn')
        with patch.object(session_control, 'request', return_value={'command_id': 'c1', 'state': 'recorded', 'warning': 'target_no_reported_state'}), \
                patch('sys.stderr', new_callable=io.StringIO) as fake_err:
            session_control.execute(args)
            self.assertIn('Warning: target session has no recent reported state', fake_err.getvalue())

    def test_send_notes_headless_next_turn_delivery(self):
        import io
        args = self.parse('send', 's1', '--prompt', 'cancel the booking', '--key', 'k-next')
        with patch.object(session_control, 'request', return_value={'command_id': 'c1', 'state': 'recorded', 'delivery': 'next_turn'}), \
                patch('sys.stderr', new_callable=io.StringIO) as fake_err:
            session_control.execute(args)
            self.assertIn('queued in its inbox for its next turn', fake_err.getvalue())

    def test_send_rejected_at_send_time_for_headless_exec_fails_with_hint(self):
        args = self.parse('send', 's1', '--prompt', 'cancel the booking', '--key', 'k-exec')
        refused = session_control.SessionHTTPError(
            409, code='send_unsupported_headless',
            message='send_unsupported_headless: headless codex sessions accept no input')
        with patch.object(session_control, 'request', side_effect=refused):
            with self.assertRaises(ValueError) as caught:
                session_control.execute(args)
        self.assertIn('send_unsupported_headless', str(caught.exception))
        self.assertIn('accept no input', session_control.diagnostic(caught.exception))

    def test_send_require_ready_passes_flag(self):
        args = self.parse('send', 's1', '--prompt', 'hello', '--require-ready', '--key', 'k-ready')
        with patch.object(session_control, 'request', return_value={'command_id': 'c1', 'state': 'recorded'}) as transport:
            session_control.execute(args)
            payload = transport.call_args.args[2]
            self.assertTrue(payload.get('require_ready'))

    def test_start_explicit_fable_model_requires_claude_and_forwards_exact_request(self):
        for model in ('fable', 'claude-fable-5-2'):
            with patch.object(session_control, 'request', return_value={'id': 'fable-session'}) as transport:
                session_control.execute(self.parse(
                    'start', '--workspace', 'demo', '--engine', 'claude', '--model', model,
                    '--reason', 'The task explicitly requests Fable 5.2', '--prompt', 'analyze this',
                    '--key', 'fable-key', '--no-wait'))
            payload = transport.call_args.args[2]
            self.assertEqual(payload['engine'], 'claude')
            self.assertEqual(payload['model'], model)
            self.assertEqual(payload['engine_override_reason'], 'The task explicitly requests Fable 5.2')

        with patch.object(session_control, 'request') as transport:
            with self.assertRaisesRegex(ValueError, '--model fable requires --engine claude'):
                session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--engine', 'codex',
                                                    '--model', 'fable', '--reason', 'needs fable', '--prompt', 'work'))
        transport.assert_not_called()

    def test_start_purpose_defaults_to_implement_and_is_forwarded(self):
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'fix it', '--key', 'k-purpose'))
        self.assertEqual(transport.call_args.args[2]['purpose'], 'implement')
        with patch.object(session_control, 'request', return_value={'id':'fixture-session','state':'running'}) as transport:
            session_control.execute(self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'review it',
                                                '--purpose', 'review', '--key', 'k-purpose-review'))
        self.assertEqual(transport.call_args.args[2]['purpose'], 'review')
        with self.assertRaises(SystemExit):
            self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'x', '--purpose', 'deploy')

    def test_start_waits_by_default_with_bounded_150s_and_no_wait_skips(self):
        args = self.parse('start', '--workspace', 'demo', '--prompt', 'wait for me', '--key', 'k-wait')
        self.assertEqual(session_control.check_wait_args(args), (session_control.START_WAIT_TIMEOUT, 5.0))
        self.assertEqual(session_control.START_WAIT_TIMEOUT, 150.0)
        with patch.object(session_control, 'request', return_value={'session': {'id': 's-wait', 'state': 'offered'}, 'picker': {'pool': []}}) as transport, \
                patch.object(session_control, 'wait_for_session', return_value={'session': {'id': 's-wait', 'state': 'running'}}) as wait:
            result = session_control.execute(args)
        self.assertEqual(result['state'], 'running')
        self.assertEqual(result['session']['state'], 'running')
        self.assertEqual(result['picker'], {'pool': []})
        self.assertEqual(result['client_key'], 'k-wait')
        wait.assert_called_once_with('s-wait', 150.0, 5.0)
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions'))
        no_wait = self.parse('start', '--workspace', 'demo', '--prompt', 'receipt only', '--key', 'k-nowrap', '--no-wait')
        self.assertIsNone(session_control.check_wait_args(no_wait)[0])
        with patch.object(session_control, 'request', return_value={'id': 's-nowrap'}) as transport, \
                patch.object(session_control, 'wait_for_session') as wait:
            session_control.execute(no_wait)
        wait.assert_not_called()

    def test_start_wait_failure_reports_stage(self):
        args = self.parse('start', '--workspace', 'demo', '--key', 'k-stage', '--timeout', '5', '--interval', '1')
        failed = {'session': {'id': 's-stage', 'state': 'failed', 'stage': 'admission',
                              'attention': {'reason': 'no_machine_for_constraints'}}}
        with patch.object(session_control, 'request', side_effect=[{'id': 's-stage'}, failed]), \
                patch.object(session_control.time, 'sleep', return_value=None):
            with self.assertRaisesRegex(ValueError, 'stage=admission'):
                session_control.execute(args)

    def test_list_table_maps_hub2_session_view_fields(self):
        hub2_rows = [
            {'id': 'hub2-1', 'state': 'running', 'engine': 'claude', 'model': 'fable',
             'machine_id': 'hetzner-01', 'account_id': 'acct-fable', 'interactive': True,
             'native_id': 'd4762b81-9a0a-46fb-b6d0-79c4f382a0f7',
             'updated_at': 1773000000000},
            {'id': 'hub1-1', 'state': 'running', 'engine': 'codex', 'model': 'gpt-6',
             'machine': 'host-a', 'account': 'shared-codex', 'interactive': False,
             'native_tag': 'hub-offer-42', 'updated_at': '2026-11-10T12:34:56Z'},
            {'id': 'legacy-1', 'state': 'offered', 'updated_at': None},
        ]
        table = session_control.render_table(hub2_rows)
        self.assertIn('HOST', table)
        self.assertIn('ACCOUNT', table)
        self.assertIn('NATIVE', table)
        self.assertIn('hetzner-01', table)
        self.assertIn('acct-fable', table)
        self.assertIn('aplexer', table)
        self.assertIn('host-a', table)
        self.assertIn('shared-codex', table)
        self.assertIn('headless', table)
        self.assertIn('2026-11-10 12:34:56', table)
        self.assertIn('2026-', table)  # numeric epoch rendered as UTC timestamp
        self.assertIn('d4762b81-9a0a-46fb-b6d0-79c4f382a0f7', table)  # Hub2 native_id
        self.assertIn('hub-offer-42', table)  # Hub1 native_tag
        self.assertNotIn('by no host', table)

    def test_start_output_prints_native_pane_after_wait(self):
        args = self.parse('start', '--workspace', 'demo', '--prompt', 'bind me', '--key', 'k-native')
        result = {'id': 's-native', 'state': 'running', 'engine': 'claude', 'model': 'fable',
                  'binding_wait': {'session': {'id': 's-native', 'state': 'running',
                                               'native_id': 'd4762b81-9a0a-46fb-b6d0-79c4f382a0f7'}}}
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            session_control.render_output(args, result)
        self.assertIn('Native pane: d4762b81-9a0a-46fb-b6d0-79c4f382a0f7', err.getvalue())
        # Hub1 binding shape: flat view with native_tag.
        result_hub1 = {'id': 's-hub1', 'binding_wait': {'native_tag': 'hub-offer-42'}}
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            session_control.render_output(args, result_hub1)
        self.assertIn('Native pane: hub-offer-42', err.getvalue())
        # No binding yet (--no-wait or unbound): no pane line.
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            session_control.render_output(args, {'id': 's-unbound', 'state': 'offered'})
        self.assertNotIn('Native pane:', err.getvalue())

    def test_start_output_surfaces_picker_pool_shares(self):
        args = self.parse('start', '--no-wait', '--workspace', 'demo', '--prompt', 'route me', '--key', 'k-picker', '--no-wait')
        result = {'id': 's-picker', 'state': 'offered', 'engine': 'claude', 'model': 'fable',
                  'effort': 'medium', 'policy_id': 'claude-fable-medium', 'machine_id': 'hetzner-01',
                  'picker': {'pool': [{'policy_id': 'claude-fable-medium', 'share': 0.25},
                                      {'policy_id': 'codex-gpt6-medium', 'share': 0.5}]}}
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            session_control.render_output(args, result)
        self.assertIn('claude/fable/medium', err.getvalue())
        self.assertIn('claude-fable-medium', err.getvalue())
        self.assertIn('hetzner-01', err.getvalue())
        self.assertIn('25.0%', err.getvalue())

    def test_http_error_surfaces_stage_slug(self):
        import urllib.error
        payload = {'error': 'start_refused', 'detail': 'no machine matches needs', 'stage': 'placement'}
        failure = urllib.error.HTTPError('https://hub.test/api/sessions', 409, 'Conflict', {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(session_control.SessionHTTPError) as ctx:
                session_control.request('POST', '/api/sessions', {'key': 'k-stage-err'})
        msg = str(ctx.exception)
        self.assertIn('start_refused', msg)
        self.assertIn('stage: placement', msg)
        self.assertIn('no machine matches needs', msg)

    def test_hub2_unsupported_routes_fail_typed_and_legacy_escape_hatch(self):
        # conftest defaults tests to the legacy escape hatch; blank it here so
        # the production refusal path runs for real.
        refusals = [
            ('POST', '/api/bus/send'),
            ('GET', '/api/bus/claims'),
            ('GET', '/api/webhooks'),
            ('POST', '/api/prompts'),
            ('GET', '/api/conversations'),
            ('GET', '/api/fleet/tasks'),
            ('POST', '/api/fleet/plan'),
            ('GET', '/api/fleet/launches'),
            ('GET', '/api/fleet/accounts'),
            ('GET', '/api/fleet/resources'),
            ('POST', '/api/fleet/routing/host-goal'),
            ('GET', '/api/fleet/routing/policy/grok'),
            ('GET', '/api/fleet/machines/machine-1'),
            ('GET', '/api/ops/status'),
            ('GET', '/api/device/pair'),
            ('GET', '/api/session-schedules'),
            ('GET', '/api/driver/sessions'),
            ('GET', '/api/fleet/machines/machine-1?verbose=true'),
            ('GET', '/api/fleet/tasks/'),
        ]
        for method, path in refusals:
            with self.subTest(path=path), patch.dict(os.environ, {'HUB_ALLOW_LEGACY_ROUTES': ''}):
                with self.assertRaises(session_control.SessionInputError) as ctx:
                    session_control.ensure_hub2_supported(method, path)
                self.assertIn('not_supported_on_hub2', str(ctx.exception))
        allowed = [
            ('GET', '/api/sessions'),
            ('POST', '/api/sessions'),
            ('GET', '/api/sessions/s1'),
            ('GET', '/api/intake/tasks?project=demo'),
            ('GET', '/api/fleet'),
            ('GET', '/api/fleet/machines'),
            ('GET', '/api/fleet/machines?verbose=true'),
            ('GET', '/api/fleet/routing/machines'),
            ('GET', '/api/fleet/routing/policy'),
            ('GET', '/api/status'),
            ('POST', '/api/model-picker/pick'),
            ('GET', '/api/claims'),
            ('GET', '/api/quota'),
            ('GET', '/api/heartbeat'),
        ]
        for method, path in allowed:
            with self.subTest(path=path), patch.dict(os.environ, {'HUB_ALLOW_LEGACY_ROUTES': ''}):
                session_control.ensure_hub2_supported(method, path)
        with patch.dict(os.environ, {'HUB_ALLOW_LEGACY_ROUTES': '1'}):
            session_control.ensure_hub2_supported('POST', '/api/bus/send')

    def test_bus_request_gate_refuses_hub1_routes(self):
        from agent_hub_host import bus
        with patch.object(bus.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_URL': 'https://hub.test', 'HUB_TOKEN': 'fixture-env-token',
                                        'HUB_ALLOW_LEGACY_ROUTES': ''}):
            with self.assertRaises(ValueError) as ctx:
                bus._do_request('POST', '/api/bus/send', {'topic': 't'})
        self.assertIn('not_supported_on_hub2', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()

