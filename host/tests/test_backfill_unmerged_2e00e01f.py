import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'backfill-unmerged-2e00e01f.py'
SPEC = importlib.util.spec_from_file_location('backfill_unmerged_2e00e01f', SCRIPT)
BACKFILL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKFILL)


class BackfillTests(unittest.TestCase):
    def test_default_root_uses_checkout_parent_when_home_is_managed(self):
        script = '/home/alexey/git/agent-hub-wt-example/scripts/backfill-unmerged-2e00e01f.py'
        self.assertEqual(BACKFILL.default_root(script, '/managed/engine/home'), Path('/home/alexey/git'))

    def test_default_root_falls_back_to_home_for_other_checkout_layouts(self):
        script = '/workspace/agent-hub/scripts/backfill-unmerged-2e00e01f.py'
        self.assertEqual(BACKFILL.default_root(script, '/home/operator'), Path('/home/operator/git'))

    def test_git_timeout_is_reported_as_unknown_without_waiting_sixty_seconds(self):
        timeout = subprocess.TimeoutExpired(['git', 'status'], 10)
        with patch.object(BACKFILL.subprocess, 'run', side_effect=timeout) as run:
            self.assertIsNone(BACKFILL.git('/repo', 'status', '--porcelain'))
        self.assertEqual(run.call_args.kwargs['timeout'], BACKFILL.GIT_TIMEOUT)
        self.assertLess(BACKFILL.GIT_TIMEOUT, 60)

    def test_forbidden_intake_lookup_is_not_misreported_as_no_intake(self):
        with patch.object(BACKFILL, 'git', return_value=None), \
             patch.object(BACKFILL.intake, 'request', side_effect=BACKFILL.intake.IntakeHTTPError(403, 'worker_scope_forbidden')):
            task, inspected, error = BACKFILL.find_intake(Path('/repo/demo-wt-12345678'), {})
        self.assertIsNone(task)
        self.assertTrue(inspected)
        self.assertEqual(error, 'worker_scope_forbidden')


if __name__ == '__main__':
    unittest.main()
