"""Maintained-launcher refusal contract regression (intake 43071c69)."""
import json
import sys
import unittest

from agent_hub_host.fleet import run_json
from agent_hub_host.launch_diagnostics import (
    LocalCommandError, REFUSALS, diagnostic, exception_diagnostic,
)


class LauncherMemoryDiagnosticsTests(unittest.TestCase):
    def test_numeric_floor_survives_failed_process_as_safe_capacity_refusal(self):
        for floor in (128, 2048, 10240, 1048576):
            with self.subTest(floor=floor):
                line = f'ValueError: host MemAvailable below {floor}MiB floor after reservation (launcher_memory_reserve)'
                script = 'import sys; print(' + repr(line) + ', file=sys.stderr); sys.exit(1)'
                with self.assertRaises(LocalCommandError) as raised:
                    run_json([sys.executable, '-c', script])
                value = exception_diagnostic(raised.exception, 'dispatch_headless')
                self.assertEqual(value['reason'], 'launcher_memory_reserve')
                self.assertEqual(value['error_type'], 'ValueError')
                self.assertEqual(value['exit_code'], 1)
                self.assertEqual(value['tail'], line)
                self.assertEqual(value['stage'], 'dispatch_headless')
                # Recovery accepts only messages in this fixed registry.
                self.assertIn((value['reason'], value['message']), set(REFUSALS.values()))

    def test_unknown_or_suffixed_output_remains_withheld(self):
        template = 'ValueError: host MemAvailable below {}MiB floor after reservation (launcher_memory_reserve)'
        for line in (template.format('/private/token'), template.format('-1'),
                     template.format('0'), template.format('1' * 100),
                     template.format('2048') + ' secret=private-value'):
            with self.subTest(line=line):
                value = diagnostic(line)
                self.assertEqual(value['reason'], 'launcher_command_failed')
                self.assertNotIn('tail', value)
                self.assertNotIn('private', json.dumps(value))

    def test_antigravity_headless_provider_refusal_survives_traceback_with_redacted_diagnostic(self):
        line = "ValueError: Provider 'antigravity' is not supported for managed headless execution"
        script = "raise ValueError(\"Provider 'antigravity' is not supported for managed headless execution\")"
        with self.assertRaises(LocalCommandError) as raised:
            run_json([sys.executable, '-c', script])
        value = exception_diagnostic(raised.exception, 'dispatch_headless')
        self.assertEqual(value['reason'], 'launcher_model_unsupported')
        self.assertEqual(value['error_type'], 'ValueError')
        self.assertEqual(value['exit_code'], 1)
        self.assertEqual(value['tail'], line)
        self.assertEqual(value['stage'], 'dispatch_headless')
        self.assertNotIn('unrecognized output was withheld', value['message'])
        self.assertIn((value['reason'], value['message']), set(REFUSALS.values()))
        unsafe = diagnostic(line + ' /private/token')
        self.assertEqual(unsafe['reason'], 'launcher_command_failed')
        self.assertNotIn('/private/token', json.dumps(unsafe))


if __name__ == '__main__':
    unittest.main()
