"""Windows Claude headless diagnostics and clean local-command exits (intake ad8aa931).

Claude headless launches there failed as a bare dispatch_internal_error
(35cf62ff, 8151fb8f) and as LocalCommandError on an exit-0 admission gate
whose complete JSON was captured (abe6bf8d).
"""
import json
import io
import sys
import unittest
from unittest.mock import patch

from agent_hub_host import fleet
from agent_hub_host.dispatch_diagnostics import classify
from agent_hub_host.launch_diagnostics import LocalCommandError

# Prints gate JSON and exits 0 while a child briefly retains inherited stdout.
LINGER = ("import json, subprocess, sys\n"
          "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%s)'])\n"
          "print(json.dumps({'launch_allowed': True}))\n")


class TestNamedPreStartRefusals(unittest.TestCase):
    def reason(self, message):
        return classify(ValueError(message))['reason']

    def test_fixed_messages_map_to_existing_slugs(self):
        self.assertEqual(self.reason('Selected model/effort is not configured locally'), 'model_unsupported')
        self.assertEqual(self.reason('Maintained launcher route is not configured'), 'dispatch_route_unconfigured')
        self.assertEqual(self.reason('Invalid provider profile'), 'dispatch_route_unconfigured')
        self.assertEqual(self.reason('Headless provider profile is not qualified'), 'dispatch_route_unconfigured')
        self.assertEqual(self.reason('Unsupported launch mode'), 'dispatch_offer_invalid')
        self.assertEqual(self.reason('workspace path exists and is not a git checkout'), 'workspace_unavailable')

    def test_variable_tails_map_by_prefix_and_carry_only_redacted_tails(self):
        cases = {"Launcher provider 'x' is not supported for headless execution": 'dispatch_route_unconfigured',
                 'repo clone failed: fatal: could not read Username for https://github.com': 'workspace_unavailable',
                 'repo_clone_auth_failed: owner/name: fatal: could not read Username for https://github.com': 'repo_clone_auth_failed',
                 'repo_not_found: owner/name: remote: Repository not found.': 'repo_not_found',
                 'repo_clone_failed: owner/name: fatal: unable to access': 'repo_clone_failed',
                 'git fetch failed: exit 128': 'workspace_unavailable',
                 'git checkout failed: error: pathspec': 'workspace_unavailable'}
        for message, slug in cases.items():
            diag = classify(ValueError(message))
            self.assertEqual(diag['reason'], slug, message)
            dumped = json.dumps(diag)
            # No credential-shaped material ever crosses the boundary, even
            # though the bounded git stderr tail now does (intake hubfail-t2596u).
            for shape in ('token=', 'ghp_', 'sk-', '-----BEGIN', 'Traceback (most recent call last)'):
                self.assertNotIn(shape, dumped, message)
            tail = diag.get('tail', '')
            self.assertTrue(len(tail) <= 512, message)
            self.assertNotIn('\n', tail, message)
        # The renamed proxy refusal keeps the dead proxy address for routing.
        proxy = classify(ValueError('repo_proxy_unreachable: DataTalksClub/website: proxy 127.0.0.1:8899 unreachable (ConnectionRefusedError)'))
        self.assertEqual(proxy['reason'], 'repo_proxy_unreachable')
        self.assertIn('127.0.0.1:8899', proxy.get('tail', ''))

    def test_claude_headless_value_errors_have_named_reasons(self):
        cases = {
            'Native profile receipt mismatch': 'native_profile_mismatch',
            'Native account receipt mismatch': 'native_account_mismatch',
            'Native lifecycle receipt is incomplete': 'native_lifecycle_incomplete',
            'No confirmed native execution receipt': 'native_execution_unconfirmed',
            'Maintained launcher route is not configured': 'dispatch_route_unconfigured',
            'Selected model/effort requires verified admission': 'model_unverified',
            'Native receipt mismatch': 'native_receipt_mismatch',
            'Native cwd mismatch': 'native_cwd_mismatch',
            'Noncanonical native UUID': 'native_session_identity_invalid',
            'Worker Hub credential could not be prepared': 'dispatch_credential_unavailable',
            'Result path escapes workspace root': 'dispatch_result_path_invalid',
            'Refuse symlinked ancestor directory: /private/worktree': 'dispatch_result_path_invalid',
            'Invalid final report': 'dispatch_result_invalid',
        }
        for message, slug in cases.items():
            with self.subTest(message=message):
                diagnostic = classify(ValueError(message))
                self.assertEqual(diagnostic['reason'], slug)
                self.assertTrue(diagnostic['message'])

    def test_unknown_messages_stay_internal(self):
        self.assertEqual(self.reason('Unmapped Claude native check failed'), 'dispatch_internal_error')
        self.assertEqual(self.reason('repo clone failedX'), 'dispatch_internal_error')

    def test_claude_headless_unknown_value_error_uses_fixed_fallback(self):
        diagnostic = classify(
            ValueError('Unmapped Claude native check failed'),
            value_error_fallback='dispatch_value_error')
        self.assertEqual(diagnostic['reason'], 'dispatch_value_error')
        self.assertEqual(diagnostic['message'], 'A Claude headless dispatch validation check refused before native start.')
        self.assertEqual(
            classify(ValueError('Unmapped Claude native check failed'),
                     value_error_fallback='arbitrary_detail')['reason'],
            'dispatch_internal_error')


class TestCleanExitReaderGrace(unittest.TestCase):
    def test_clean_exit_returns_json(self):
        self.assertEqual(fleet.run_json([sys.executable, '-c', "print('{\"launch_allowed\": true}')"]), {'launch_allowed': True})

    def test_parseable_exit_zero_receipt_survives_an_inherited_pipe_handle(self):
        with patch.object(fleet, 'READER_GRACE_S', 0.05):
            self.assertEqual(fleet.run_json([sys.executable, '-c', LINGER % 0.2]), {'launch_allowed': True})

    def test_incomplete_output_with_lingering_reader_is_named_reader_timeout(self):
        code = ("import subprocess, sys\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.2)'])\n"
                "sys.stdout.write('{\\\"launch_allowed\\\":')\n"
                "sys.stdout.flush()\n")
        with patch.object(fleet, 'READER_GRACE_S', 0.02), self.assertRaises(LocalCommandError) as ctx:
            fleet.run_json([sys.executable, '-c', code])
        self.assertEqual(ctx.exception.command_diagnostic['failure_kind'], 'reader_timeout')
        self.assertEqual(classify(ctx.exception)['failure_kind'], 'reader_timeout')

    def test_output_limit_is_named_output_overflow(self):
        with self.assertRaises(LocalCommandError) as ctx:
            fleet.run_json([sys.executable, '-c', "import sys; sys.stdout.write('x' * 140000)"])
        self.assertEqual(ctx.exception.command_diagnostic['failure_kind'], 'output_overflow')
        self.assertEqual(classify(ctx.exception)['failure_kind'], 'output_overflow')

    def test_stdin_exit_race_is_named_but_parseable_success_is_accepted(self):
        class BrokenStdin:
            def __init__(self):
                self.closed = False
            def write(self, _data):
                raise BrokenPipeError('child exited before stdin write')
            def close(self):
                self.closed = True

        class CompletedProcess:
            def __init__(self, stdout):
                self.stdin = BrokenStdin()
                self.stdout = io.BytesIO(stdout)
                self.stderr = io.BytesIO(b'')
                self.returncode = 0
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode
            def kill(self):
                self.returncode = -9

        command = [sys.executable, '-c', 'pass']
        completed = CompletedProcess(b'{"launch_allowed": true}')
        with patch.object(fleet.subprocess, 'Popen', return_value=completed):
            self.assertEqual(fleet.run_json(command, payload={'goal': 'test'}), {'launch_allowed': True})
        self.assertTrue(completed.stdin.closed)

        completed = CompletedProcess(b'not json')
        with patch.object(fleet.subprocess, 'Popen', return_value=completed):
            with self.assertRaises(LocalCommandError) as ctx:
                fleet.run_json(command, payload={'goal': 'test'})
        self.assertTrue(completed.stdin.closed)
        self.assertEqual(ctx.exception.command_diagnostic['failure_kind'], 'stdin_write_failed')
        self.assertEqual(classify(ctx.exception)['failure_kind'], 'stdin_write_failed')

    def test_nonzero_exit_gets_no_grace(self):
        with self.assertRaises(LocalCommandError) as ctx:
            fleet.run_json([sys.executable, '-c', 'import sys; print("{}"); sys.exit(3)'])
        self.assertEqual(ctx.exception.diagnostic.get('exit_code'), 3)


if __name__ == '__main__':
    unittest.main()
