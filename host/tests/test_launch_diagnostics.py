import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_hub_host.fleet import run_json
from agent_hub_host.launch_diagnostics import LocalCommandError, diagnostic, exception_diagnostic, check_auth_expired_text


class LaunchDiagnosticTests(unittest.TestCase):
    def test_current_memory_floor_traceback_survives_failed_process(self):
        tail = 'host MemAvailable below 2048MiB floor after reservation (launcher_memory_reserve)'
        script = 'raise ValueError(' + repr(tail) + ')'
        with self.assertRaises(LocalCommandError) as raised:
            run_json([sys.executable, '-c', script])
        info = exception_diagnostic(raised.exception, 'dispatch_headless')
        self.assertEqual(info['reason'], 'launcher_memory_reserve')
        self.assertEqual(info['error_type'], 'ValueError')
        self.assertEqual(info['exit_code'], 1)
        self.assertEqual(info['stage'], 'dispatch_headless')
        self.assertEqual(info['tail'], 'ValueError: ' + tail)
        self.assertIn(tail, info['message'])
        for unsafe in (tail + ' secret=private-value', tail.replace('2048', '/private/token'),
                       tail.replace('2048', '9' * 100)):
            result = diagnostic('ValueError: ' + unsafe)
            self.assertEqual(result['reason'], 'launcher_command_failed')
            self.assertNotIn('private', json.dumps(result))

    def test_real_failed_process_preserves_launcher_refusal_and_exit_status(self):
        script = "import sys; print('ValueError: host MemAvailable < 10GiB', file=sys.stderr); sys.exit(1)"
        with self.assertRaises(LocalCommandError) as raised:
            run_json([sys.executable, '-c', script])
        info = exception_diagnostic(raised.exception, 'dispatch_headless')
        self.assertEqual(info['reason'], 'launcher_memory_reserve')
        self.assertEqual(info['exit_code'], 1)
        self.assertEqual(info['error_type'], 'ValueError')
        self.assertIn('10 GiB', info['message'])
        self.assertEqual(info['stage'], 'dispatch_headless')

    def test_real_disk_refusal_preserves_class_message_and_exit_status(self):
        for filesystem in ('cwd', 'tmpdir'):
            tail = f'ValueError: {filesystem} filesystem free < 20GiB floor + required disk (39728447488 B)'
            script = "import sys; print(" + repr(tail) + ", file=sys.stderr); sys.exit(1)"
            with self.assertRaises(LocalCommandError) as raised:
                run_json([sys.executable, '-c', script])
            info = exception_diagnostic(raised.exception, 'dispatch_headless')
            self.assertEqual(info['reason'], 'launcher_disk_reserve')
            self.assertEqual(info['error_type'], 'ValueError')
            self.assertEqual(info['exit_code'], 1)
            self.assertEqual(info['tail'], tail)
            self.assertIn('disk headroom', info['message'])
            for unsafe in (tail + ' secret=private-value', tail.replace('39728447488', '/private/token')):
                result = diagnostic(unsafe)
                self.assertEqual(result['reason'], 'launcher_command_failed')
                self.assertNotIn('private', json.dumps(result))

    def test_output_never_copies_arbitrary_secrets_paths_goals_or_suffixes(self):
        for raw in ('secret=private-value /private/path', 'ValueError: host MemAvailable < 10GiB secret=private-value', '{"error":"private-value"}'):
            value = json.dumps(diagnostic(raw))
            self.assertNotIn('private-value', value)
            self.assertNotIn('/private/path', value)
        raw = 'secret=private-value\nValueError: host MemAvailable < 10GiB\n'
        self.assertEqual(diagnostic(raw)['reason'], 'launcher_memory_reserve')
        self.assertNotIn('private-value', str(diagnostic(raw)))

    def test_inprocess_refusals_use_same_safe_contract(self):
        info = exception_diagnostic(ValueError('Native Codex receipt mismatch'), 'dispatch_headless')
        self.assertEqual(info['reason'], 'launcher_receipt_mismatch')
        self.assertEqual(info['error_type'], 'ValueError')

    def test_detached_launcher_environment_explains_missing_wrapper(self):
        info = diagnostic('ValueError: Unsupported provider: codex (genuine protected wrapper scripts/launch-codex.sh not configured; raw routing denied)')
        self.assertEqual(info['reason'], 'launcher_codex_wrapper_unavailable')

    def test_opencode_api_key_expired_classification(self):
        raw = 'timestamp=2026-10-24T13:08:20.909Z level=ERROR run=8b645413 message="Failed to drain Session" cause="AI.Error: This API key has expired. Create a new API key in OpenCode Console and replace it in your client or secret store. Signing in again will not renew this key."'
        info = diagnostic(raw)
        self.assertEqual(info['reason'], 'engine_auth_expired')
        self.assertEqual(info['message'], 'The engine authentication failed or API key has expired.')
        self.assertNotIn('secret store', json.dumps(info))
        self.assertNotIn('OpenCode Console', json.dumps(info))

    def test_provider_auth_failure_401_classification(self):
        for raw in ('HTTP 401 Unauthorized', 'Error: 401 Client Error: Unauthorized for url', 'invalid_api_key: secret-12345'):
            info = diagnostic(raw)
            self.assertEqual(info['reason'], 'engine_auth_expired')
            self.assertEqual(info['message'], 'The engine authentication failed or API key has expired.')
            self.assertNotIn('secret-12345', json.dumps(info))

    def test_opencode_log_inspection_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / 'opencode' / 'log'
            log_dir.mkdir(parents=True)
            log_file = log_dir / 'opencode.log'
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
            log_file.write_text(f'timestamp={now_iso} level=ERROR cause="AI.Error: This API key has expired. Create a new API key in OpenCode Console"')
            with patch.dict(os.environ, {'XDG_DATA_HOME': tmpdir}):
                info = diagnostic('The launcher command failed; unrecognized output was withheld.', engine='opencode')
                self.assertEqual(info['reason'], 'engine_auth_expired')
                self.assertEqual(info['message'], 'The engine authentication failed or API key has expired.')

    def test_stale_log_does_not_override_current_launcher_refusal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / 'opencode' / 'log'
            log_dir.mkdir(parents=True)
            log_file = log_dir / 'opencode.log'
            # Old log line from hours ago
            log_file.write_text('timestamp=2026-10-20T10:00:00.000Z level=ERROR cause="AI.Error: This API key has expired."')
            with patch.dict(os.environ, {'XDG_DATA_HOME': tmpdir}):
                # Exact reproduction from reviewer
                info = diagnostic('Host MemAvailable after reservations must retain 10 GiB', engine='opencode', task_id='fresh-unrelated-task')
                self.assertEqual(info['reason'], 'launcher_memory_reserve')
                self.assertEqual(info['message'], 'Host memory headroom after launcher reservations is below 10 GiB.')

    def test_stale_log_older_than_cutoff_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / 'opencode' / 'log'
            log_dir.mkdir(parents=True)
            log_file = log_dir / 'opencode.log'
            # Old log entry
            log_file.write_text('timestamp=2026-10-20T10:00:00.000Z level=ERROR cause="AI.Error: This API key has expired."')
            # Set mtime back by 1000 seconds
            old_time = time.time() - 1000
            os.utime(log_file, (old_time, old_time))
            with patch.dict(os.environ, {'XDG_DATA_HOME': tmpdir}):
                info = diagnostic('The launcher command failed; unrecognized output was withheld.', engine='opencode', launched_at=time.time())
                self.assertEqual(info['reason'], 'launcher_command_failed')

    def test_echoed_prompt_does_not_trigger_auth_expired(self):
        prompt_echo = 'args=["run", "Implement Hub intake: fix API key has expired bug in OpenCode"]'
        self.assertFalse(check_auth_expired_text(prompt_echo))
        info = diagnostic(prompt_echo, exit_code=1)
        self.assertNotEqual(info['reason'], 'engine_auth_expired')

    def test_plain_task_mentioning_auth_keywords_does_not_trigger_auth_expired(self):
        task_text = 'Task: explain the error API key has expired'
        self.assertFalse(check_auth_expired_text(task_text))
        info = diagnostic(task_text, exit_code=1)
        self.assertNotEqual(info['reason'], 'engine_auth_expired')

    def test_check_engine_auth_expired_skips_unrelated_task_in_shared_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / 'opencode' / 'log'
            log_dir.mkdir(parents=True)
            log_file = log_dir / 'opencode.log'
            now_ts = time.time()
            iso_str = dt.datetime.fromtimestamp(now_ts, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            # Write a log entry for a DIFFERENT task
            log_file.write_text(
                f'timestamp={iso_str} level=INFO run=other123 message="cli starting" args="[\\"run\\", \\"--id\\", \\"hub-OTHER\\"]"\n'
                f'timestamp={iso_str} level=ERROR run=other123 message="Failed to drain Session" cause="AI.Error: This API key has expired."\n'
            )
            with patch.dict(os.environ, {'XDG_DATA_HOME': tmpdir}):
                # Checking for CURRENT task should not correlate with OTHER
                info = diagnostic('The launcher command failed; unrecognized output was withheld.', engine='opencode', task_id='hub-CURRENT', launched_at=now_ts)
                self.assertEqual(info['reason'], 'launcher_command_failed')
                # But checking for OTHER task should correlate
                info_other = diagnostic('The launcher command failed; unrecognized output was withheld.', engine='opencode', task_id='hub-OTHER', launched_at=now_ts)
                self.assertEqual(info_other['reason'], 'engine_auth_expired')

    def test_start_offsets_skips_prior_log_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / 'opencode' / 'log'
            log_dir.mkdir(parents=True)
            log_file = log_dir / 'opencode.log'
            log_file.write_text('timestamp=2026-10-24T14:00:55.000Z level=ERROR cause="AI.Error: This API key has expired."\n')
            offset = log_file.stat().st_size
            with patch.dict(os.environ, {'XDG_DATA_HOME': tmpdir}):
                # With start_offset after the error, it's skipped
                info = diagnostic('The launcher command failed; unrecognized output was withheld.', engine='opencode', start_offsets={str(log_file): offset}, launched_at=time.time())
                self.assertEqual(info['reason'], 'launcher_command_failed')

    def test_unknown_profile_and_launcher_cli_errors_are_surfaced_not_withheld(self):
        info = diagnostic('a: unknown profile hub-shared-codex\n', exit_code=1, stage='dispatch_interactive')
        self.assertEqual(info['reason'], 'launcher_unknown_profile')
        self.assertIn('a: unknown profile hub-shared-codex', info['message'])
        self.assertEqual(info.get('tail'), 'a: unknown profile hub-shared-codex')
        self.assertNotIn('withheld', info['message'])

        cli_err = diagnostic('a: session tag already in use: hub-1234 secret=my-secret /private/dir/file\n', exit_code=1)
        self.assertEqual(cli_err['reason'], 'launcher_command_failed')
        self.assertIn('a: session tag already in use: hub-1234', cli_err['message'])
        self.assertNotIn('my-secret', json.dumps(cli_err))
        self.assertNotIn('/private/dir/file', json.dumps(cli_err))
        self.assertNotIn('withheld', cli_err['message'])



class ReceiptWaitDiagnosticTests(unittest.TestCase):
    """Intake 643395c0: bounded receipt-wait expiry and exit codes are named,
    raw launcher bytes are still never forwarded."""

    def test_bounded_receipt_wait_is_named(self):
        info = diagnostic('Local command failed or exceeded resource limit')
        self.assertEqual(info['reason'], 'launcher_command_failed')
        self.assertEqual(info['error_type'], 'RuntimeError')
        self.assertIn('receipt wait expired', info['message'])
        self.assertNotIn('withheld', info['message'])
        self.assertNotIn('tail', info)

    def test_exit_code_is_surfaced_without_raw_bytes(self):
        raw = 'private-payload line\nValueError: internal detail /home/secret/path\n'
        info = diagnostic(raw, exit_code=1)
        self.assertEqual(info['reason'], 'launcher_command_failed')
        self.assertEqual(info['exit_code'], 1)
        self.assertIn('exit code 1', info['message'])
        self.assertNotIn('private-payload', json.dumps(info))
        self.assertNotIn('internal detail', json.dumps(info))
        self.assertNotIn('/home/secret/path', json.dumps(info))

    def test_unknown_output_without_exit_code_keeps_generic_message(self):
        info = diagnostic('totally unknown refusal text')
        self.assertEqual(info['reason'], 'launcher_command_failed')
        self.assertEqual(info['message'], 'The launcher command failed; unrecognized output was withheld.')
