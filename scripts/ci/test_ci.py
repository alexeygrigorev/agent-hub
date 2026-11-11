#!/usr/bin/env python3
"""CI/CD contract tests: canonical release, host distribution, rollback guard."""

import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "validate-workflow.py"
ROLLBACK_SCRIPT = REPO_ROOT / "scripts" / "ci" / "rollback.py"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"




class TestCICD(unittest.TestCase):
    def test_canonical_release_contract(self):
        result = subprocess.run(["node", "--test", "scripts/tests/release-contract.test.mjs"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_host_distribution_and_dependency_contract(self):
        import tomllib
        package = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        self.assertEqual(package["project"]["version"], "2.0.0")
        self.assertEqual(package["project"]["dependencies"], ["websockets==16.0"])
        self.assertEqual(package["tool"]["setuptools"]["package-dir"], {"": "hub2/host"})
        self.assertEqual(set(package["project"]["scripts"]), {"hub", "hub2"})


class TestRollbackContract(unittest.TestCase):
    def test_rollback_cannot_restore_legacy_version(self):
        rollback = (REPO_ROOT / ".github/workflows/rollback.yml").read_text()
        self.assertNotIn("CLOUDFLARE_API_TOKEN", rollback)
        self.assertNotIn("versions deploy", rollback)
        self.assertIn("source_ref", rollback)


if __name__ == "__main__":
    unittest.main()
