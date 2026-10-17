#!/usr/bin/env python3
"""Tests for CI/CD workflow validator and rollback helper."""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "validate-workflow.py"
ROLLBACK_SCRIPT = REPO_ROOT / "scripts" / "ci" / "rollback.py"


class TestCICD(unittest.TestCase):
    def test_validate_workflows_passes(self):
        """Verify current repository workflows pass static validation."""
        res = subprocess.run([sys.executable, str(VALIDATE_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Validator failed: {res.stdout}\n{res.stderr}")
        self.assertIn("deploy.yml passed all checks", res.stdout)
        self.assertIn("rollback.yml passed all checks", res.stdout)
        self.assertIn("All CI/CD workflow validations PASSED", res.stdout)

    def test_rollback_help(self):
        """Verify rollback helper prints usage cleanly."""
        res = subprocess.run([sys.executable, str(ROLLBACK_SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Rollback help failed: {res.stderr}")
        self.assertIn("--version-id", res.stdout)
        self.assertIn("--list", res.stdout)
        self.assertIn("Agent Hub Worker Rollback Helper", res.stdout)


if __name__ == "__main__":
    unittest.main()
