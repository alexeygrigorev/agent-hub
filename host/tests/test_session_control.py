"""Synthetic public CLI/HTTP coverage for hub sessions; never executes prompts."""
import argparse
import io
import json
import os
import unittest
from unittest.mock import patch
from agent_hub_host import session_control


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root


class TestSessionControlCli(unittest.TestCase):
    def parse(self, *args):
        return parser().parse_args(['sessions', *args])

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

    def test_cancel_held_intent_uses_a_stable_key_without_native_stop(self):
        with patch.object(session_control, 'request', return_value={'state': 'cancelled'}) as transport:
            result = session_control.execute(self.parse('cancel', 'held-fixture', '--key', 'cancel-once'))
        self.assertEqual(result['state'], 'cancelled')
        transport.assert_called_once_with('POST', '/api/sessions/held-fixture/cancel', {'key': 'cancel-once'})

        with patch.object(session_control, 'request') as transport:
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('cancel', '../other', '--key', 'cancel-once'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('cancel', 'held-fixture', '--key', 'invalid/key'))
        transport.assert_not_called()

    def test_stop_wait_accepts_atomic_no_start_cancellation_without_host_command(self):
        receipt = {'state': 'cancelled', 'cancellation': {'effect': 'no_native_start'}}
        with patch.object(session_control, 'request', return_value=receipt) as transport:
            result = session_control.execute(self.parse('stop', 'held-fixture', '--key', 'stop-held', '--wait', '--timeout', '5'))
        self.assertEqual(result['stop_wait']['effect'], 'no_native_start')
        transport.assert_called_once_with('POST', '/api/sessions/held-fixture/stop', {'key': 'stop-held'})

    def test_stop_wait_does_not_accept_running(self):
        with patch.object(session_control, 'request', side_effect=[{'state': 'running'}, {'state': 'stopped'}]) as fetch, patch.object(session_control.time, 'sleep'):
            result = session_control.wait_for_session('s', 10, 1, stopping=True)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(fetch.call_count, 2)

    def test_list_show_commands_paths(self):
        with patch.object(session_control, 'request', return_value={'sessions': []}) as transport:
            result = session_control.execute(self.parse('list', '--limit', '10'))
        self.assertEqual(result, {'sessions': []})
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions?limit=10'))
        with patch.object(session_control, 'request', return_value={'id': 's'}) as transport:
            session_control.execute(self.parse('show', 's'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s'))
        with patch.object(session_control, 'request', return_value={'commands': []}) as transport:
            session_control.execute(self.parse('commands', 's'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s/commands'))

    def test_start_headless_brief_and_interactive_empty(self):
        with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--engine', 'codex', '--reason', 'codex required for test', '--brief-file', 'goal.txt', '--key', 'k'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['brief'], 'synthetic goal')
        self.assertEqual(payload['mode'], 'headless')
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--interactive', '--key', 'k'))
        self.assertEqual(transport.call_args.args[2]['mode'], 'interactive')
        self.assertNotIn('brief', transport.call_args.args[2])
        conflicting = self.parse('start', '--workspace', 'demo', '--interactive', '--key', 'k')
        conflicting.brief_file = 'goal.txt'
        with patch.object(session_control, 'read_file', return_value='x'):
            with self.assertRaises(ValueError):
                session_control.execute(conflicting)

    def test_start_links_intake_task_for_acceptance_gate(self):
        with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--brief-file', 'goal.txt',
                                               '--key', 'k', '--intake-task-id', 'task-1'))
        self.assertEqual(transport.call_args.args[2]['intake_task_id'], 'task-1')
        with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--brief-file', 'goal.txt', '--key', 'k'))
        self.assertNotIn('intake_task_id', transport.call_args.args[2])

    def test_start_retry_and_goal_keep_intake_link_and_key(self):
        for mode in ([], ['--with-goal']):
            with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                    patch.object(session_control, 'request', return_value={'id': 'session-fixture'}) as transport:
                args = self.parse('start', '--workspace', 'demo', '--brief-file', 'goal.txt',
                                  '--key', 'same-key', '--intake-task-id', 'task-fixture', *mode)
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

    def test_route_rejections_are_bounded_codes_without_provider_details(self):
        import os
        import urllib.error
        payload = {'error': 'no_eligible_route', 'rejected': [
            {'reason': 'capability_unavailable'}, {'reason': 'account_capacity'},
            {'reason': 'private error with secret'}, {'reason': 'account_capacity'}]}
        failure = urllib.error.HTTPError('https://hub.test/api/fleet/launches', 409, 'held', {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                session_control.request('POST', '/api/fleet/launches', {})
        self.assertIn('account_capacity, capability_unavailable', str(raised.exception))
        self.assertNotIn('private', str(raised.exception))

    def test_disabled_provider_hold_has_actionable_local_guidance(self):
        import os
        import urllib.error
        payload = {'id': 'held-opus', 'state': 'held', 'hold': {'error': 'provider_disabled', 'detail': 'private server body'}}
        failure = urllib.error.HTTPError('https://hub.test/api/sessions', 409, 'held', {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                session_control.request('POST', '/api/sessions', {})
        self.assertIn('provider_disabled', str(raised.exception))
        self.assertIn('choose an enabled engine/model', str(raised.exception))
        self.assertIn('fresh host route with verified quota', str(raised.exception))
        self.assertNotIn('private server body', str(raised.exception))

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

    def test_hold_payload_surfaces_code_and_held_session(self):
        import os
        import urllib.error
        body = json.dumps({'id': 'held-session-1', 'state': 'held',
                           'hold': {'error': 'no_eligible_route'}}).encode('utf-8')
        failure = urllib.error.HTTPError('https://hub.test/api/sessions', 409, 'held', {}, io.BytesIO(body))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                session_control.request('POST', '/api/sessions', {'key': 'k'})
        message = str(raised.exception)
        self.assertIn('status 409', message)
        self.assertIn('no_eligible_route', message)
        self.assertIn('held-session-1', message)

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
        views = [{'id': 's1', 'state': 'offered'}, {'id': 's1', 'state': 'running'}]
        with patch.object(session_control, 'request', side_effect=[{'id': 's1', 'state': 'offered'}, *views]) as transport, \
                patch.object(session_control.time, 'sleep', return_value=None):
            result = session_control.execute(args)
        self.assertEqual(result['client_key'], 'k1')
        self.assertEqual(result['binding_wait']['state'], 'running')
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s1'))

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
            with self.assertRaisesRegex(ValueError, 'stop_host_unreachable; command c9'):
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

    def test_start_prompt_inline_with_brief_boundaries(self):
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', 'do the thing', '--key', 'k'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['brief'], 'do the thing')
        self.assertEqual(payload['mode'], 'headless')
        # Mutually exclusive with --brief-file and --interactive.
        with self.assertRaises(SystemExit):
            self.parse('start', '--workspace', 'demo', '--prompt', 'x', '--brief-file', 'goal.txt')
        with self.assertRaises(SystemExit):
            self.parse('start', '--workspace', 'demo', '--prompt', 'x', '--interactive')
        # Same 8 KiB UTF-8 boundaries as --brief-file, never executed locally.
        with patch.object(session_control, 'request', return_value={}):
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', '   ', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', 'é' * 5000, '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', 'x' * 8001, '--key', 'k'))

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
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', 'goal', '--name', 'friendly', '--key', 'k'))
        self.assertEqual(transport.call_args.args[2]['name'], 'friendly')
        with patch.object(session_control, 'request', return_value={}):
            session_control.execute(self.parse('start', '--workspace', 'demo', '--interactive', '--name', 'empty-ok', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--name', '../escape', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--name', '', '--key', 'k'))

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
            session_control.execute(self.parse('start', '--workspace', 'demo', '--parent', 'parent-123', '--headless', '--key', 'k-lineage'))
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

    def test_start_engine_override_reason_required(self):
        args = self.parse('start', '--workspace', 'demo', '--engine', 'codex', '--key', 'k')
        with self.assertRaises(ValueError) as ctx:
            session_control.execute(args)
        self.assertIn('Engine override requires an explanation', str(ctx.exception))

    def test_dedicated_override_requires_engine_and_reason(self):
        with self.assertRaises(SystemExit):
            self.parse('override', '--workspace', 'demo', '--reason', 'some reason')

        with self.assertRaises(SystemExit):
            self.parse('override', '--workspace', 'demo', '--engine', 'codex')

        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('override', '--workspace', 'demo', '--engine', 'codex', '--reason', 'need codex', '--key', 'k'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['engine'], 'codex')
        self.assertEqual(payload['engine_override_reason'], 'need codex')

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
        args = self.parse('start', '--workspace', 'demo', '--brief-file', '/nonexistent/brief.txt', '--key', 'k')
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
            args = self.parse('start', '--workspace', 'demo', '--brief-file', tf_path, '--key', 'k')
            with self.assertRaises(session_control.SessionInputError) as ctx:
                session_control.execute(args)
            self.assertIn('character limit', str(ctx.exception))
            self.assertIn('8000', str(ctx.exception))
        finally:
            import os
            os.unlink(tf_path)

    def test_prompt_character_limit(self):
        args = self.parse('start', '--workspace', 'demo', '--prompt', 'do task', '--key', 'k')
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(args)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['brief'], 'do task')

        long_prompt = 'y' * (session_control.MAX_BRIEF_CHARS + 5)
        bad_args = self.parse('start', '--workspace', 'demo', '--prompt', long_prompt, '--key', 'k')
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
        args = self.parse('start', '--workspace', 'demo', '--intake-task-id', 'https://hub.test/#/intake/task-url-1234', '--key', 'k')
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(args)
        payload = transport.call_args.args[2]
        self.assertEqual(payload['intake_task_id'], 'task-url-1234')

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

    def test_send_require_ready_passes_flag(self):
        args = self.parse('send', 's1', '--prompt', 'hello', '--require-ready', '--key', 'k-ready')
        with patch.object(session_control, 'request', return_value={'command_id': 'c1', 'state': 'recorded'}) as transport:
            session_control.execute(args)
            payload = transport.call_args.args[2]
            self.assertTrue(payload.get('require_ready'))


if __name__ == '__main__':
    unittest.main()

