import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host.fleet import run_json
from agent_hub_host.launch_diagnostics import LocalCommandError, diagnostic, exception_diagnostic


class LaunchDiagnosticTests(unittest.TestCase):
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
