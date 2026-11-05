#!/usr/bin/env python3
"""Tests for CI/CD workflow validator, rollback helper and wrangler deploy retry."""

import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "validate-workflow.py"
ROLLBACK_SCRIPT = REPO_ROOT / "scripts" / "ci" / "rollback.py"
RETRY_SCRIPT = REPO_ROOT / "scripts" / "ci" / "wrangler_retry.py"
SLOT_SCRIPT = REPO_ROOT / "scripts" / "ci" / "release_slot_verify.py"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
ROLLBACK_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rollback.yml"


def load_retry_module():
    spec = importlib.util.spec_from_file_location("wrangler_retry_under_test", RETRY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_slot_module():
    spec = importlib.util.spec_from_file_location("release_slot_verify_under_test", SLOT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RETRY = load_retry_module()
SLOT = load_slot_module()
SLOT_A = "00000000-0000-4000-8000-000000000001"
SLOT_B = "00000000-0000-4000-8000-000000000002"
SHA_A = "a" * 40


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


class TestReleaseSlotGate(unittest.TestCase):
    """Orchestrator release gate (intake 2c87c231): machine-checked wiring.

    AC1/AC2: main pushes check a slot before tests without consuming it; a
    workflow_dispatch deploy consumes the same SHA-bound slot before deploy.
    Rollback dispatch consumes a version-bound slot the same way.
    """

    def test_deploy_job_gates_before_any_deploy_step(self):
        content = DEPLOY_WORKFLOW.read_text()
        self.assertIn("release_slot_verify.py", content, "deploy.yml must run the release slot gate")
        deploy_job = content.split("\n  deploy:", 1)[1]
        gate_at = deploy_job.index("release_slot_verify.py")
        first_deploy_at = deploy_job.index("npx wrangler deploy")
        self.assertLess(gate_at, first_deploy_at,
                        "the release slot gate must precede every deploy action")
        gate_step = deploy_job[deploy_job.index("- name: Release slot gate"):]
        gate_step = gate_step[:gate_step.index("- name:", 1)]
        self.assertNotIn("if:", gate_step,
                         "the gate step itself must be unconditional")

    def test_main_push_gate_checks_without_consuming_and_dispatch_consumes(self):
        content = DEPLOY_WORKFLOW.read_text()
        push_gate = content.split("\n  release_slot:", 1)[1].split("\n  test:", 1)[0]
        self.assertIn("github.event_name == 'push'", push_gate)
        self.assertIn("github.ref == 'refs/heads/main'", push_gate)
        self.assertIn("release_slot_verify.py --check-only", push_gate)
        test_job = content.split("\n  test:", 1)[1].split("\n  deploy:", 1)[0]
        self.assertIn("needs: release_slot", test_job)
        deploy_job = content.split("\n  deploy:", 1)[1]
        self.assertIn("github.event_name == 'workflow_dispatch'", deploy_job)
        self.assertIn("python3 scripts/ci/release_slot_verify.py", deploy_job)
        self.assertIn("--check-only", deploy_job, "dry-run dispatch must not consume a slot")

    def test_rollback_job_gates_before_deploy(self):
        content = ROLLBACK_WORKFLOW.read_text()
        self.assertIn("release_slot:", content, "rollback.yml must require a release_slot input")
        self.assertIn("release_slot_verify.py", content, "rollback.yml must run the release slot gate")
        job = content.split("\n  rollback:", 1)[1]
        self.assertLess(job.index("release_slot_verify.py"), job.index("wrangler versions deploy"),
                        "the release slot gate must precede the rollback deploy")
        self.assertIn('--target "$INPUT_VERSION_ID" --slot "$INPUT_RELEASE_SLOT"', content)

    def test_trailer_parser_requires_exactly_one_uuid_and_refuses_missing(self):
        message = "feat: x\n\nsome body\nRelease-Slot: " + SLOT_A + "\n"
        sha = SHA_A
        with mock.patch.object(SLOT, "commit_message", return_value=message):
            slot, reason = SLOT.trailer_slot(sha)
        self.assertEqual(slot, SLOT_A)
        self.assertIsNone(reason)
        with mock.patch.object(SLOT, "commit_message", return_value="feat: no trailer\n\nbody only\n"):
            slot, reason = SLOT.trailer_slot(sha)
        self.assertIsNone(slot)
        self.assertIn("no Release-Slot trailer", reason)
        with mock.patch.object(SLOT, "commit_message", return_value=message + "Release-Slot: " + SLOT_B + "\n"):
            slot, reason = SLOT.trailer_slot(sha)
        self.assertIsNone(slot)
        self.assertIn("multiple Release-Slot trailers", reason)

    def test_verify_mapping_matrix(self):
        cases = [
            (200, {"ok": True, "id": SLOT_A, "target": SHA_A, "consumed": True}, True, None),
            (404, {"error": "release_slot_not_found"}, False, "never issued"),
            (403, {"error": "release_slot_target_mismatch"}, False, "different deploy target"),
            (409, {"error": "release_slot_already_consumed"}, False, "already consumed"),
            (409, {"error": "release_slot_revoked"}, False, "revoked"),
            (409, {"error": "release_slot_expired"}, False, "expired"),
            (400, {"error": "invalid_request"}, False, "rejected"),
        ]
        for status, body, expect_ok, needle in cases:
            with self.subTest(status=status, body=body):
                with mock.patch.object(SLOT, "post_verify", return_value=(status, body, None)):
                    ok, result = SLOT.verify("https://hub.test", SLOT_A, SHA_A, "ci", clock=lambda s: None, attempts=1)
                self.assertEqual(ok, expect_ok)
                if needle:
                    self.assertIn(needle, str(result))

    def test_verify_retries_transport_and_5xx_only(self):
        sleeps = []
        responses = [(None, {}, OSError("down")), (500, {}, None),
                     (200, {"ok": True, "target": SHA_A, "consumed": True}, None)]
        with mock.patch.object(SLOT, "post_verify", side_effect=lambda *a: responses.pop(0)):
            ok, result = SLOT.verify("https://hub.test", SLOT_A, SHA_A, "ci",
                                     clock=sleeps.append, attempts=3)
        self.assertTrue(ok)
        self.assertEqual(sleeps, list(SLOT.RETRY_SLEEP_SECONDS[:2]))

        responses = [(404, {"error": "release_slot_not_found"}, None),
                     (200, {"ok": True, "target": SHA_A, "consumed": True}, None)]
        with mock.patch.object(SLOT, "post_verify", side_effect=lambda *a: responses.pop(0)):
            ok, result = SLOT.verify("https://hub.test", SLOT_A, SHA_A, "ci",
                                     clock=sleeps.append, attempts=2)
        self.assertFalse(ok)
        self.assertEqual(len(responses), 1, "a definitive 4xx must never retry")

    def test_check_only_sends_non_consuming_request(self):
        response = {"ok": True, "target": SHA_A, "consumed": False}
        with mock.patch.object(SLOT, "post_verify", return_value=(200, response, None)) as post:
            ok, _ = SLOT.verify("https://hub.test", SLOT_A, SHA_A, "ci", check_only=True, attempts=1)
        self.assertTrue(ok)
        self.assertFalse(post.call_args.args[1]["consume"])

    def test_verify_fails_closed_when_hub_unreachable(self):
        with mock.patch.object(SLOT, "post_verify", return_value=(None, {}, OSError("down"))):
            ok, result = SLOT.verify("https://hub.test", SLOT_A, SHA_A, "ci", clock=lambda s: None, attempts=2)
        self.assertFalse(ok)
        self.assertIn("fail closed", result)


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
