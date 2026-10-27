"""Real command failure evidence survives refusal reporting without secrets."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host.command_diagnostics import output_tail, WINDOW_LIMIT
from agent_hub_host.dispatch_diagnostics import classify
from agent_hub_host.fleet import Dispatcher, run_json
from agent_hub_host.launch_diagnostics import LocalCommandError, exception_diagnostic


class LocalCommandDiagnostics(unittest.TestCase):
    def test_real_failure_captures_command_exit_and_separate_redacted_tails(self):
        script = ('import sys;print("probe context");'
                  'sys.stderr.write("token=hidden-device-secret\\nlast failure");sys.exit(23)')
        with self.assertRaises(LocalCommandError) as caught:
            run_json([sys.executable, '-c', script])
        diagnostic = classify(caught.exception)
        self.assertEqual(diagnostic['command'], Path(sys.executable).name)
        self.assertEqual(diagnostic['exit_code'], 23)
        self.assertEqual(diagnostic['stdout_tail'], 'probe context')
        self.assertIn('last failure', diagnostic['stderr_tail'])
        self.assertEqual(diagnostic['reason'], 'dispatch_local_command_failed')
        self.assertNotIn('hidden-device-secret', json.dumps(diagnostic))
        self.assertNotIn('probe context', str(caught.exception))
        self.assertNotIn(script, json.dumps(diagnostic))

    def test_native_client_failure_has_the_same_command_evidence(self):
        result = subprocess.CompletedProcess(['aplexer'], 17, 'native context', 'Bearer private-native-token')
        with patch('agent_hub_host.aplexer_client.client_call', return_value=result):
            with self.assertRaises(LocalCommandError) as caught:
                run_json(['aplexer', 'status', 'private-selector', '--json'])
        diagnostic = classify(caught.exception)
        self.assertEqual(diagnostic['command'], 'aplexer')
        self.assertEqual(diagnostic['exit_code'], 17)
        self.assertEqual(diagnostic['stdout_tail'], 'native context')
        self.assertNotIn('private-native-token', json.dumps(diagnostic))
        self.assertNotIn('private-selector', json.dumps(diagnostic))

    def test_evidence_reaches_reject_and_private_journal(self):
        error = LocalCommandError(command='/private/bin/quse', exit_code=9,
                                  stdout='checking prerequisites', stderr='password="fixture-password" invalid option')
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def request(method, path, body=None):
                calls.append((method, path, body))
                return {'state': 'failed', **body}
            dispatcher = Dispatcher({'version': 1, 'subscriptions': [], 'workspaces': {'demo': tmp}},
                                    request, journal_dir=tmp)
            offer = {'id': '12345678-1234-1234-1234-123456789abc', 'workspace': 'demo'}
            results = []
            with patch.object(dispatcher, 'dispatch', side_effect=error):
                dispatcher._dispatch_one(offer, results)
            diagnostic = calls[0][2]['diagnostic']
            self.assertTrue(calls[0][1].endswith('/reject'))
            self.assertEqual(diagnostic['command'], 'quse')
            self.assertEqual(diagnostic['exit_code'], 9)
            self.assertIn('invalid option', diagnostic['stderr_tail'])
            journal = json.loads((Path(tmp) / 'fleet-launches' / (offer['id'] + '.json')).read_text())
            self.assertEqual(journal['diagnostic'], diagnostic)
            self.assertEqual(results[0]['diagnostic'], diagnostic)
            self.assertNotIn('fixture-password', json.dumps([calls, journal, results]))
            self.assertNotIn('/private/bin', json.dumps([calls, journal, results]))

    def test_postclaim_exception_diagnostic_retains_the_sanitized_fields(self):
        exc = LocalCommandError(command='launcher', exit_code=2, stdout='out', stderr='err')
        diagnostic = exception_diagnostic(exc, 'dispatch_headless')
        self.assertEqual(diagnostic['command'], 'launcher')
        self.assertEqual(diagnostic['exit_code'], 2)
        self.assertEqual(diagnostic['stdout_tail'], 'out')
        self.assertEqual(diagnostic['stderr_tail'], 'err')

    def test_raw_classification_input_is_never_used_as_command_output(self):
        exc = LocalCommandError('private-value\nValueError: host MemAvailable < 10GiB', exit_code=1)
        diagnostic = exception_diagnostic(exc, 'dispatch_headless')
        self.assertEqual(diagnostic['stdout_tail'], '')
        self.assertEqual(diagnostic['stderr_tail'], '')
        self.assertNotIn('private-value', json.dumps(diagnostic))

    def test_traceback_source_and_paths_are_withheld(self):
        raw = ('Traceback (most recent call last):\n'
               '  File "/private/path", line 1, in main\n'
               '    print("arbitrary-private-value")\nValueError: disk reserve')
        self.assertEqual(output_tail(raw), '[traceback output withheld]')

    def test_full_window_is_redacted_before_tail_truncation(self):
        secret = 'multiline-secret-' * 100
        raw = 'benign context ' * 100 + 'token="' + secret + '\nsecond-secret-line"\nlast failure'
        value = output_tail(raw)
        self.assertLessEqual(len(value), 512)
        self.assertTrue(value.endswith('last failure'))
        self.assertNotIn('multiline-secret', value)
        self.assertNotIn('second-secret-line', value)
        self.assertNotIn('\n', value)
        self.assertEqual(output_tail('benign context ' * 100 + 'last failure')[-12:], 'last failure')

    def test_secret_shapes_and_terminal_controls_are_removed(self):
        secrets = ['sk-abcdefghijk', 'ghp_' + 'z' * 20, 'AKIA' + 'X' * 16,
                   'url-password', 'query-secret', 'private-key-material', 'cookie-secret']
        raw = ('\x1b[31mprobe\x1b[0m ' + secrets[0] + ' ' + secrets[1] + ' ' + secrets[2] + '\n'
               'https://user:url-password@example.test/path?token=query-secret\n'
               '-----BEGIN PRIVATE KEY-----\nprivate-key-material\n-----END PRIVATE KEY-----\n'
               'Set-Cookie: cookie-secret\nfinal failure\x00')
        value = output_tail(raw)
        for secret in secrets:
            self.assertNotIn(secret, value)
        self.assertTrue(value.endswith('final failure'))
        self.assertFalse(any(ord(c) < 32 or ord(c) == 127 for c in value))
        self.assertNotIn('incomplete-secret', output_tail('-----BEGIN PRIVATE KEY-----\nincomplete-secret'))

    def test_oversized_or_unsanitizable_windows_fail_closed(self):
        self.assertEqual(output_tail('x' * (WINDOW_LIMIT + 1) + 'private-secret'), '[output exceeds diagnostic window]')
        with patch('agent_hub_host.log_redaction.redact_log', side_effect=ValueError('private-secret')):
            self.assertEqual(output_tail('private-secret'), '[output unavailable]')

    def test_large_captured_output_preserves_the_redacted_final_tail(self):
        raw = 'probe context ' * 6000 + 'token="' + ('multiline-secret\n' * 200) + '"\nfinal failure'
        value = output_tail(raw)
        self.assertLessEqual(len(value), 512)
        self.assertTrue(value.endswith('final failure'))
        self.assertNotIn('multiline-secret', value)

    def test_command_identity_never_contains_path_args_or_secret_shapes(self):
        for command in ['/private/bin/quse --token=private', 'sk-abcdefghijk', 'x' * 100]:
            diagnostic = LocalCommandError(command=command).command_diagnostic
            self.assertEqual(diagnostic['command'], 'redacted')
