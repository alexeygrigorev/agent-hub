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
    def test_canonical_release_contract(self):
        result = subprocess.run(["node", "--test", "test/root-promotion.test.mjs"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_host_distribution_and_dependency_contract(self):
        import tomllib
        package = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        self.assertEqual(package["project"]["version"], "2.0.0")
        self.assertEqual(package["project"]["dependencies"], ["websockets==16.0"])
        self.assertEqual(package["tool"]["setuptools"]["package-dir"], {"": "hub2/host"})
        self.assertEqual(set(package["project"]["scripts"]), {"hub", "hub2"})


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
    def test_production_retry_and_health_are_retained(self):
        content = DEPLOY_WORKFLOW.read_text()
        self.assertIn("wrangler_retry.py npx wrangler deploy --config wrangler.jsonc", content)
        self.assertIn("wrangler_retry.py npx wrangler versions list --config wrangler.jsonc", content)
        self.assertIn("https://hub.dtcdev.click/health", content)
        self.assertIn("payload.get('service') == 'hub2'", content)

    def test_rollback_cannot_restore_legacy_version(self):
        rollback = (REPO_ROOT / ".github/workflows/rollback.yml").read_text()
        self.assertNotIn("CLOUDFLARE_API_TOKEN", rollback)
        self.assertNotIn("versions deploy", rollback)
        self.assertIn("source_ref", rollback)


if __name__ == "__main__":
    unittest.main()
