"""`hub limits` operator CLI for fleet concurrency caps (intake cbcadaa1)."""
import argparse
import contextlib
import io
import unittest
from unittest import mock

from agent_hub_host import limits_cli


def parse(*argv):
    parser = argparse.ArgumentParser()
    limits_cli.add_parser(parser.add_subparsers(dest='cmd'))
    return parser.parse_args(['limits', *argv])


class LimitsCliTest(unittest.TestCase):
    def test_routes_and_payloads(self):
        with mock.patch('agent_hub_host.session_control.request', return_value={}) as req:
            limits_cli.execute(parse('show'))
            req.assert_called_with('GET', '/api/fleet/limits')
            limits_cli.execute(parse('set', 'claude', '13'))
            req.assert_called_with('PATCH', '/api/fleet/limits', {'target': 'claude', 'max_concurrency': 13})
            limits_cli.execute(parse('set', 'hub-claude', '10', '--allow-lower'))
            req.assert_called_with('PATCH', '/api/fleet/limits', {'target': 'hub-claude', 'max_concurrency': 10, 'allow_lower': True})
        with self.assertRaises(ValueError):
            limits_cli.execute(parse('set', 'claude', '0'))

    def test_render(self):
        shown = {'engines': [{'engine': 'claude', 'accounts': ['hub-claude', 'shared-claude'], 'max_concurrency': 113, 'active': 4}],
                 'accounts': [{'account': 'hub-claude', 'provider': 'claude', 'max_concurrency': 13, 'active': 4, 'reserved': 1,
                               'observed_sessions': 3, 'native_live': 4, 'sessions': 4, 'available_slots': 9, 'enabled': True, 'status': 'ok'}]}
        text = limits_cli.render(parse('show'), shown)
        self.assertIn('claude  113  4       hub-claude,shared-claude', text)
        self.assertIn('hub-claude  claude  13   4       1         3         4     4         9     ok', text)
        result = {'target': 'claude', 'max_concurrency': 13, 'kind': 'engine', 'allow_lower': False,
                  'changes': [{'account': 'hub-claude', 'from': 8, 'to': 13, 'result': 'raised'},
                              {'account': 'shared-claude', 'from': 100, 'to': 100, 'result': 'kept_higher'}]}
        text = limits_cli.render(parse('set', 'claude', '13'), result)
        self.assertTrue(text.startswith('Set claude=13 (engine):'))
        self.assertIn('shared-claude  100   100  kept_higher', text)

    def run_with(self, exc, *argv):
        err = io.StringIO()
        with mock.patch('agent_hub_host.session_control.request', side_effect=exc), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as done:
            limits_cli.run(parse(*argv))
        return done.exception.code, err.getvalue()

    def test_run_maps_http_and_transport_errors(self):
        from agent_hub_host import session_control
        cases = [(403, 'forbidden', 'admin login required'), (404, 'unknown_limit_target', 'unknown engine or account'),
                 (409, 'account_lowering_requires_allow_lower', 'needs --allow-lower')]
        for status, code, hint in cases:
            exit_code, text = self.run_with(session_control.SessionHTTPError(status, code), 'set', 'hub-claude', '5')
            self.assertEqual(exit_code, 1)
            self.assertIn('HTTP %d %s' % (status, code), text)
            self.assertIn(hint, text)
        exit_code, text = self.run_with(session_control.SessionTransportError('Session transport failed: DNS lookup failed'), 'show')
        self.assertEqual((exit_code, text.strip()), (1, 'Session transport failed: DNS lookup failed'))
        exit_code, text = self.run_with(AssertionError('unused'), 'set', 'claude', '0')
        self.assertEqual(exit_code, 2)
        self.assertIn('between 1 and 100', text)


if __name__ == '__main__':
    unittest.main()
