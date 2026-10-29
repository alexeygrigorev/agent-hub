#!/usr/bin/env python3
"""Tests for CI/CD workflow validator, rollback helper and wrangler deploy retry."""

import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "validate-workflow.py"
ROLLBACK_SCRIPT = REPO_ROOT / "scripts" / "ci" / "rollback.py"
RETRY_SCRIPT = REPO_ROOT / "scripts" / "ci" / "wrangler_retry.py"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


def load_retry_module():
    spec = importlib.util.spec_from_file_location("wrangler_retry_under_test", RETRY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RETRY = load_retry_module()


class TestCICD(unittest.TestCase):
    def test_reset_credit_safety_suites_are_discovered(self):
        """Money-adjacent reset-credit guards must run in the deploy job."""
        workflow = (REPO_ROOT / '.github/workflows/deploy.yml').read_text()
        # Python money-adjacent suites are listed explicitly in the deploy pytest step.
        for test in ('test/test_no_reset_apply.py', 'host/tests/test_codex_reset_credit.py',
                     'host/tests/test_reset_credit_workflow.py'):
            self.assertEqual(workflow.count(test), 1, test)
        # Vitest discovery is unfiltered: the guard file must exist and no vitest
        # invocation may narrow discovery with an explicit .test.ts file list.
        self.assertTrue((REPO_ROOT / 'test/reset-credit-evidence.test.ts').is_file(),
                        'test/reset-credit-evidence.test.ts')
        vitest_lines = [ln for ln in workflow.splitlines() if 'vitest run' in ln]
        self.assertTrue(vitest_lines, 'deploy.yml runs vitest discovery')
        for ln in vitest_lines:
            self.assertIsNone(re.search(r'\btest/[\w./-]+\.test\.ts', ln), ln)

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


class TestWranglerRetry(unittest.TestCase):
    """Classifier and bounded-retry behavior for transient Cloudflare API errors."""

    # The exact failure from run 40494634371 attempt #3 (2026-10-27).
    INCIDENT_10013_OUTPUT = (
        "Deploying agent-hub-dev\n"
        "ERROR A request to the Cloudflare API "
        "(/accounts/***/workers/scripts/agent-hub-dev/assets-upload-session) failed.\n"
        "An unknown error has occurred ... [code: 10013]\n"
    )

    def test_transient_cloudflare_10013(self):
        kind, reason = RETRY.classify_failure(self.INCIDENT_10013_OUTPUT)
        self.assertEqual(kind, "transient")
        self.assertIn("10013", reason)
        self.assertEqual(RETRY.error_code(self.INCIDENT_10013_OUTPUT), "10013")

    def test_transient_network_and_5xx_samples(self):
        for output in (
            "ERROR A request to the Cloudflare API (...) failed.\nfetch failed\n",
            "ERROR network connection lost while uploading assets\n",
            "ERROR A request to the Cloudflare API (...) failed.\ninternal server error\n",
            "ERROR request failed with status code: 503\n",
        ):
            kind, _ = RETRY.classify_failure(output)
            self.assertEqual(kind, "transient", output)

    def test_fatal_auth_config_build_samples(self):
        for output in (
            "ERROR A request to the Cloudflare API (...) failed.\nAuthentication error [code: 10000]\n",
            "ERROR Invalid API token provided; deploy rejected\n",
            "ERROR could not read config file .local/dev-wrangler.json\n",
            "ERROR [ERROR] Could not resolve \"./src/missing.ts\"\n",
        ):
            kind, _ = RETRY.classify_failure(output)
            self.assertEqual(kind, "fatal", output)

    def test_fatal_takes_precedence_over_transient(self):
        mixed = "fetch failed\nERROR Authentication error [code: 10000]\n"
        kind, _ = RETRY.classify_failure(mixed)
        self.assertEqual(kind, "fatal")

    def test_unrecognized_failure_is_not_transient(self):
        kind, _ = RETRY.classify_failure("ERROR something exploded unexpectedly\n")
        self.assertIsNone(kind)

    def test_retry_until_success_within_max_attempts(self):
        outputs = [(1, "blip [code: 10013]\n"), (1, "blip [code: 10013]\n"), (0, "Uploaded\n")]
        calls, sleeps = [], []

        def runner(argv):
            calls.append(argv)
            return outputs[len(calls) - 1]

        code, attempts, _ = RETRY.run_with_retries(
            ["npx", "wrangler", "deploy"], runner=runner, clock=sleeps.append)
        self.assertEqual(code, 0)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, list(RETRY.RETRY_SLEEP_SECONDS))

    def test_fatal_failure_never_retries(self):
        calls, sleeps = [], []
        code, attempts, kind = RETRY.run_with_retries(
            ["npx", "wrangler", "deploy"],
            runner=lambda argv: (calls.append(argv), (7, "Authentication error [code: 10000]\n"))[1],
            clock=sleeps.append)
        self.assertEqual(code, 7)
        self.assertEqual(attempts, 1)
        self.assertEqual(kind, "fatal")
        self.assertEqual(sleeps, [])

    def test_persistent_transient_stops_at_max_attempts(self):
        calls, sleeps = [], []

        def runner(argv):
            calls.append(argv)
            return (1, "blip [code: 10013]\n")

        code, attempts, kind = RETRY.run_with_retries(
            ["npx", "wrangler", "deploy"], runner=runner, clock=sleeps.append)
        self.assertEqual(code, 1)
        self.assertEqual(attempts, RETRY.MAX_ATTEMPTS)
        self.assertEqual(kind, "transient")
        self.assertEqual(sleeps, list(RETRY.RETRY_SLEEP_SECONDS))


class TestDeployWorkflowRetryWiring(unittest.TestCase):
    """Both production deploy paths must route through the shared retry wrapper."""

    def test_both_deploy_steps_use_retry_wrapper(self):
        content = DEPLOY_WORKFLOW.read_text()
        self.assertTrue(RETRY_SCRIPT.is_file())
        self.assertGreaterEqual(
            content.count("python3 scripts/ci/wrangler_retry.py npx wrangler deploy"), 2)
        for line in content.splitlines():
            if "npx wrangler deploy" in line and "--dry-run" not in line:
                self.assertIn("wrangler_retry.py", line, line)
        self.assertIn("wrangler_retry.py npx wrangler versions list", content)

    def test_smoke_checks_unchanged(self):
        content = DEPLOY_WORKFLOW.read_text()
        self.assertIn("built_from_commit", content)
        self.assertIn("'/livez'", content)


if __name__ == "__main__":
    unittest.main()
