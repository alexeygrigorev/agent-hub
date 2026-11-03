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
spec = importlib.util.spec_from_file_location("cloudflare_preflight_under_test", REPO_ROOT / "scripts/ci/cloudflare_preflight.py")
PREFLIGHT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PREFLIGHT)


class TestCloudflarePreflight(unittest.TestCase):
    ENV = {"CLOUDFLARE_API_TOKEN": "test-secret-never-log", "CLOUDFLARE_ACCOUNT_ID": "a" * 32}

    def test_account_owned_token_never_needs_user_permissions(self):
        calls = []

        def request(path, token):
            calls.append(path)
            return 200, {"success": True, "result": {"status": "active"}}

        self.assertEqual(PREFLIGHT.verify(self.ENV, request), "account")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(path.startswith("/accounts/") for path in calls))
        self.assertTrue(all("services/" in path for path in calls[1:]))

    def test_user_owned_token_fallback(self):
        calls = []

        def request(path, token):
            calls.append(path)
            if path.startswith("/accounts/") and path.endswith("verify"):
                return 403, {"success": False, "errors": [{"code": 9109}]}
            return 200, {"success": True, "result": {"status": "active"}}

        self.assertEqual(PREFLIGHT.verify(self.ENV, request), "user")
        self.assertIn("/user/tokens/verify", calls)

    def test_invalid_token_stops_before_service_access_and_redacts_api_message(self):
        calls = []

        def request(path, token):
            calls.append(path)
            return 403, {"success": False, "errors": [{"code": 9109, "message": token}]}

        with self.assertRaises(RuntimeError) as error:
            PREFLIGHT.verify(self.ENV, request)
        self.assertEqual(len(calls), 2)
        self.assertIn("9109", str(error.exception))
        self.assertNotIn(self.ENV["CLOUDFLARE_API_TOKEN"], str(error.exception))
        self.assertNotIn(self.ENV["CLOUDFLARE_ACCOUNT_ID"], str(error.exception))

    def test_inactive_token_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "not active"):
            PREFLIGHT.verify(self.ENV, lambda *args: (200, {"success": True, "result": {"status": "disabled"}}))

    def test_missing_account_or_token_fails_before_network(self):
        def request(*args):
            self.fail("credentials must be validated before network access")

        for env in ({}, {**self.ENV, "CLOUDFLARE_ACCOUNT_ID": "bad"}):
            with self.assertRaises(RuntimeError):
                PREFLIGHT.verify(env, request)

    def test_active_token_without_service_permission_fails_closed(self):
        def request(path, token):
            if path.endswith("verify"):
                return 200, {"success": True, "result": {"status": "active"}}
            return 403, {"success": False, "errors": [{"code": 10000}]}

        with self.assertRaisesRegex(RuntimeError, "Workers Scripts permissions"):
            PREFLIGHT.verify(self.ENV, request)


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
            "ERROR Invalid access token [code: 9109]\nfetch failed\n",
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

    def test_push_ci_cannot_enter_production_deploy(self):
        content = DEPLOY_WORKFLOW.read_text()
        match = re.search(r"(?ms)^  deploy:\n(?P<job>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", content)
        self.assertIsNotNone(match, "deploy job")
        guard = re.search(r"(?m)^    if: \|\n(?P<condition>(?:^      [^\n]*\n)+)", match.group("job"))
        self.assertIsNotNone(guard, "deploy condition")
        condition = guard.group("condition")
        self.assertIn("github.event_name == 'workflow_dispatch'", {line.strip() for line in condition.splitlines()})
        self.assertNotRegex(condition, r"\bpush\b")

    def test_both_deploy_steps_use_retry_wrapper(self):
        content = DEPLOY_WORKFLOW.read_text()
        self.assertTrue(RETRY_SCRIPT.is_file())
        self.assertGreaterEqual(
            content.count("python3 scripts/ci/wrangler_retry.py npx wrangler deploy"), 2)
        for line in content.splitlines():
            if "npx wrangler deploy" in line and "--dry-run" not in line:
                self.assertIn("wrangler_retry.py", line, line)
        self.assertIn("wrangler_retry.py npx wrangler versions list", content)

    def test_read_only_credential_mode_cannot_deploy_or_claim_test_success(self):
        content = DEPLOY_WORKFLOW.read_text()
        preflight = content.split("  credential-preflight:\n", 1)[1].split("\n  test:", 1)[0]
        self.assertIn("inputs.verify_credentials_only == true", preflight)
        self.assertIn("cloudflare_preflight.py", preflight)
        self.assertNotIn("wrangler", preflight)
        test_guard = content.split("\n  test:\n", 1)[1].split("\n", 1)[0]
        self.assertIn("inputs.verify_credentials_only == true", test_guard)
        deploy = content.split("\n  deploy:\n", 1)[1]
        self.assertIn("inputs.verify_credentials_only != true", deploy)
        self.assertLess(deploy.index("cloudflare_preflight.py"), deploy.index("wrangler deploy --config"))

    def test_smoke_checks_unchanged(self):
        content = DEPLOY_WORKFLOW.read_text()
        self.assertIn("built_from_commit", content)
        self.assertIn("'/livez'", content)

    def test_workflow_dispatch_has_distinct_concurrency_group_from_push(self):
        content = DEPLOY_WORKFLOW.read_text()
        self.assertIn(
            "group: ${{ github.workflow }}-${{ github.event_name }}-${{ github.ref }}",
            content,
        )


if __name__ == "__main__":
    unittest.main()
