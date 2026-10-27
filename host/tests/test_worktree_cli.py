"""Synthetic `hub worktree` CLI coverage (intake 4b161918); never loads operator configuration."""
import io
import json
import unittest
from unittest.mock import patch

from agent_hub_host import cli, intake, worktree_cli

TASK = '4b161918-0000-4000-8000-000000000001'
SESSION = '5c272a29-1111-4111-9111-111111111111'


def run_cli(*args):
    output, error = io.StringIO(), io.StringIO()
    with patch('sys.argv', ['hub', *args]), patch('sys.stdout', output), patch('sys.stderr', error):
        try:
            cli.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue(), error.getvalue()


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TestWorktreeCli(unittest.TestCase):
    def test_list_gets_intake_worktrees_with_prefix_passthrough(self):
        with patch.object(intake, 'request', return_value={'worktrees': []}) as transport:
            code, out, error = run_cli('worktree', 'list', '4b161918')
        self.assertEqual(code, 0, error)
        transport.assert_called_once_with('GET', '/api/intake/tasks/4b161918/worktrees')
        self.assertEqual(json.loads(out), {'worktrees': []})

    def test_refresh_posts_key_and_optional_worktree(self):
        with patch.object(intake, 'request', return_value={'commands': [{'id': 'c1'}]}) as transport:
            self.assertEqual(run_cli('worktree', 'refresh', '4b161918', '--key', 'wt-fixed')[0], 0)
            transport.assert_called_once_with('POST', '/api/intake/tasks/4b161918/worktrees/refresh', {'key': 'wt-fixed'})
            transport.reset_mock()
            code, _, error = run_cli('worktree', 'refresh', '4b161918', '--worktree', 'w1')
            self.assertEqual(code, 0, error)
            method, path, payload = transport.call_args.args
            self.assertEqual((method, path), ('POST', '/api/intake/tasks/4b161918/worktrees/refresh'))
            self.assertEqual(payload['worktree_id'], 'w1')
            self.assertRegex(payload['key'], r'^wt-[0-9a-f]{32}$')

    def test_remove_short_prefix_is_intake_without_lookup(self):
        with patch.object(intake, 'request', return_value={'commands': []}) as transport:
            code, _, error = run_cli('worktree', 'remove', '4b161918', '--worktree', 'w1', '--key', 'wt-a')
        self.assertEqual(code, 0, error)
        transport.assert_called_once_with('POST', '/api/intake/tasks/4b161918/worktrees/remove',
                                          {'key': 'wt-a', 'worktree_id': 'w1'})

    def test_remove_full_uuid_that_is_an_intake_uses_intake_endpoint(self):
        with patch.object(intake, 'request', side_effect=[{'task': {'id': TASK}}, {'commands': []}]) as transport:
            code, _, error = run_cli('worktree', 'remove', TASK, '--key', 'wt-b')
        self.assertEqual(code, 0, error)
        self.assertEqual([c.args for c in transport.call_args_list], [
            ('GET', f'/api/intake/tasks/{TASK}'),
            ('POST', f'/api/intake/tasks/{TASK}/worktrees/remove', {'key': 'wt-b'})])

    def test_remove_full_uuid_not_an_intake_is_a_session(self):
        with patch.object(intake, 'request', side_effect=[intake.IntakeHTTPError(404, 'not_found'), {'commands': []}]) as transport:
            code, _, error = run_cli('worktree', 'remove', SESSION, '--key', 'wt-c')
        self.assertEqual(code, 0, error)
        self.assertEqual([c.args for c in transport.call_args_list], [
            ('GET', f'/api/intake/tasks/{SESSION}'),
            ('POST', '/api/intake/worktrees/remove', {'key': 'wt-c', 'session_id': SESSION})])

    def test_remove_lookup_errors_other_than_404_are_not_guessed(self):
        with patch.object(intake, 'request', side_effect=[intake.IntakeHTTPError(403, 'forbidden')]) as transport:
            code, out, error = run_cli('worktree', 'remove', SESSION)
        self.assertEqual(code, 1)
        self.assertEqual(transport.call_count, 1)
        self.assertIn('forbidden', error)
        self.assertEqual(out, '')

    def test_register_payload_and_absolute_path(self):
        with patch.object(intake, 'request', return_value={'worktree': {'id': 'w1'}}) as transport:
            code, _, error = run_cli('worktree', 'register', '4b161918', '--machine', 'hetzner',
                                     '--path', '/home/u/git/repo-wt-x', '--branch', 'repo-wt-x',
                                     '--session', SESSION, '--key', 'wt-reg')
            self.assertEqual(code, 0, error)
            transport.assert_called_once_with('POST', '/api/intake/tasks/4b161918/worktrees', {
                'key': 'wt-reg', 'worktree': {'machine': 'hetzner', 'path': '/home/u/git/repo-wt-x',
                                              'branch': 'repo-wt-x', 'session_id': SESSION}})
            transport.reset_mock()
            self.assertEqual(run_cli('worktree', 'register', '4b161918', '--machine', 'm', '--path', 'rel/path')[0], 1)
            transport.assert_not_called()

    def test_wait_polls_until_terminal_and_prints_final_results(self):
        clock = FakeClock()
        replies = [{'commands': [{'id': 'c1', 'state': 'queued'}]},
                   {'command': {'id': 'c1', 'state': 'queued'}},
                   {'command': {'id': 'c1', 'state': 'claimed'}},
                   {'snapshot_version': 3, 'command': {'id': 'c1', 'state': 'done', 'result': {'outcome': 'ok', 'removed': True}}}]
        with patch.object(intake, 'request', side_effect=replies) as transport, \
             patch.object(worktree_cli, 'monotonic', clock.monotonic), \
             patch.object(worktree_cli, 'sleep', clock.sleep):
            code, out, error = run_cli('worktree', 'remove', '4b161918', '--wait', '--key', 'wt-w')
        self.assertEqual(code, 0, error)
        self.assertEqual([c.args[:2] for c in transport.call_args_list[1:]],
                         [('GET', '/api/intake/worktree-commands/c1')] * 3)
        self.assertEqual(clock.sleeps, [3.0, 3.0])
        result = json.loads(out)
        self.assertTrue(result['complete'])
        self.assertEqual(result['commands'][0]['result']['outcome'], 'ok')

    def test_wait_is_bounded_by_timeout_and_exits_nonzero(self):
        clock = FakeClock()

        def send(method, path, payload=None):
            return {'command_id': 'c9'} if method == 'POST' else {'id': 'c9', 'state': 'queued'}
        with patch.object(intake, 'request', side_effect=send) as transport, \
             patch.object(worktree_cli, 'monotonic', clock.monotonic), \
             patch.object(worktree_cli, 'sleep', clock.sleep):
            code, out, error = run_cli('worktree', 'refresh', '4b161918', '--wait', '--timeout', '10')
        self.assertEqual(code, 1)
        self.assertLessEqual(sum(clock.sleeps), 10)
        self.assertEqual(transport.call_count, 1 + 4)
        self.assertEqual(json.loads(out)['timed_out'], ['c9'])
        self.assertIn('still pending', error)

    def test_wait_failed_or_refused_command_exits_nonzero(self):
        for final in ({'state': 'failed', 'result': {'outcome': 'error'}},
                      {'state': 'done', 'result': {'outcome': 'refused', 'reason': 'dirty'}}):
            clock = FakeClock()
            with self.subTest(final=final), \
                 patch.object(intake, 'request', side_effect=[{'commands': [{'id': 'c2'}]}, dict(final, id='c2')]), \
                 patch.object(worktree_cli, 'monotonic', clock.monotonic), \
                 patch.object(worktree_cli, 'sleep', clock.sleep):
                code, out, error = run_cli('worktree', 'remove', '4b161918', '--wait')
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)['commands'][0]['result'], final['result'])
            self.assertIn('failed or were refused', error)

    def test_wait_with_hub_refusal_and_no_command_exits_nonzero(self):
        response = {'task_id': TASK, 'commands': [], 'worktrees': [],
                    'refused': [{'worktree_id': 'w1', 'result': 'refused', 'reason': 'live session bound: s1 (running)'}]}
        with patch.object(intake, 'request', return_value=response) as transport:
            code, out, error = run_cli('worktree', 'remove', '4b161918', '--wait')
        self.assertEqual(code, 1)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(json.loads(out)['refused'][0]['reason'], 'live session bound: s1 (running)')
        self.assertIn('Hub refused the remove', error)

    def test_wait_rejects_bad_timeout_before_any_request(self):
        with patch.object(intake, 'request') as transport:
            self.assertEqual(run_cli('worktree', 'remove', '4b161918', '--wait', '--timeout', '0')[0], 1)
            transport.assert_not_called()

    def test_transport_failure_reports_retry_key(self):
        with patch.object(intake, 'request', side_effect=intake.IntakeTransportError('Intake transport failed')):
            code, _, error = run_cli('worktree', 'refresh', '4b161918', '--key', 'wt-retry')
        self.assertEqual(code, 1)
        self.assertIn('wt-retry', error)


if __name__ == '__main__':
    unittest.main()
