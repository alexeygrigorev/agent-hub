"""Driver CLI lifecycle requests preserve retry identity and honest waits."""
import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from agent_hub_host import cli, driver, session_control


def parse(*argv):
    parser = argparse.ArgumentParser()
    driver.add_parser(parser.add_subparsers(dest='cmd', required=True))
    return parser.parse_args(['driver', *argv])


class DriverCliTests(unittest.TestCase):
    def test_engine_host_and_force_reach_canonical_api(self):
        for engine in ('claude', 'codex', 'antigravity', 'zai', 'opencode'):
            with self.subTest(engine=engine), patch.object(session_control, 'request', return_value={'driver': {}}) as request:
                driver.execute(parse('start', '--engine', engine, '--host', 'host-one', '--force', '--key', 'retry-key'))
                request.assert_called_once_with('POST', '/api/driver/start', {
                    'key': 'retry-key', 'engine': engine, 'host': 'host-one', 'workspace': 'agent-hub', 'force': True})

    def test_status_is_read_only_and_stop_has_no_launch_options(self):
        with patch.object(session_control, 'request', return_value={'driver': None}) as request:
            self.assertEqual(driver.execute(parse('status')), {'driver': None})
            request.assert_called_once_with('GET', '/api/driver')
        with patch.object(session_control, 'request', return_value={'state': 'stopped'}) as request:
            driver.execute(parse('stop', '--key', 'stop-key'))
            request.assert_called_once_with('POST', '/api/driver/stop', {'key': 'stop-key'})

    def test_restart_retries_same_intent_until_server_confirms(self):
        responses = [{'pending_stop': True}, {'pending_stop': True}, {'state': 'starting', 'session': {'id': 'new'}}]
        with patch.object(session_control, 'request', side_effect=responses) as request, \
                patch.object(driver.time, 'sleep'), patch.object(driver.time, 'monotonic', return_value=10):
            result = driver.execute(parse('restart', '--key', 'restart-key'))
        self.assertEqual(result['state'], 'starting')
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call == request.call_args_list[0] for call in request.call_args_list))
        self.assertNotIn('workspace', request.call_args.args[2])

    def test_no_wait_returns_pending_and_generated_reconciliation_key(self):
        with patch.object(session_control, 'request', return_value={'state': 'pending_stop'}) as request:
            result = driver.execute(parse('restart', '--wait', '0'))
        self.assertEqual(result['state'], 'pending_stop')
        self.assertTrue(result['wait_timed_out'])
        self.assertEqual(result['key'], request.call_args.args[2]['key'])
        self.assertEqual(request.call_count, 1)

    def test_deadline_does_not_issue_another_request(self):
        with patch.object(session_control, 'request', return_value={'state': 'pending_stop'}) as request, \
                patch.object(driver.time, 'monotonic', side_effect=[0, 1, 2]), \
                patch.object(driver.time, 'sleep') as sleep:
            result = driver.execute(parse('restart', '--wait', '2', '--key', 'k'))
        self.assertTrue(result['wait_timed_out'])
        sleep.assert_called_once_with(1)
        self.assertEqual(request.call_count, 1)

    def test_transport_failure_is_never_replayed(self):
        with patch.object(session_control, 'request', side_effect=RuntimeError('private-token')) as request:
            with self.assertRaises(driver.DriverRequestError) as error:
                driver.execute(parse('start', '--key', 'recover-key'))
        self.assertEqual(request.call_count, 1)
        self.assertIn('--key recover-key', str(error.exception))
        self.assertNotIn('private-token', str(error.exception))
        self.assertNotIn('private-token', driver.diagnostic(RuntimeError('private-token')))

    def test_invalid_wait_or_engine_rejected_before_network(self):
        for args in [('start', '--engine', 'unknown'), ('restart', '--wait', 'nan'),
                     ('restart', '--wait', 'inf'), ('restart', '--wait', '-1'), ('restart', '--wait', '601')]:
            with self.subTest(args=args), redirect_stderr(io.StringIO()), patch.object(session_control, 'request') as request:
                with self.assertRaises(SystemExit):
                    parse(*args)
                request.assert_not_called()

    def test_invalid_key_rejected_before_network(self):
        with patch.object(session_control, 'request') as request:
            with self.assertRaises(ValueError):
                driver.execute(parse('start', '--key', '../bad'))
            request.assert_not_called()

    def test_real_cli_dispatch_prints_structured_status(self):
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['hub', 'driver', 'status']), \
                patch.object(session_control, 'request', return_value={'driver': None, 'session': None, 'schedule': None}), \
                redirect_stdout(stdout):
            cli.main()
        self.assertEqual(json.loads(stdout.getvalue()), {'driver': None, 'session': None, 'schedule': None})

    def test_cli_failure_does_not_echo_exception_secret(self):
        stderr = io.StringIO()
        with patch.object(sys, 'argv', ['hub', 'driver', 'status']), \
                patch.object(session_control, 'request', side_effect=RuntimeError('private-token')), \
                redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                cli.main()
        self.assertEqual(error.exception.code, 1)
        self.assertNotIn('private-token', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
