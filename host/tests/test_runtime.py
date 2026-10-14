import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host.runtime import (
    LauncherQualification,
    RuntimeManager,
    get_first_action_digest,
    CODEX_MIN_PERCENT,
)


class TestRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_runtime_")
        self.work_dir = pathlib.Path(self.tmp_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_first_action_digest(self):
        content = json.dumps({
            "job_id": "test-1",
            "epoch": 1,
            "pid": 999999,
            "stage": "first_action",
            "utc": "2026-10-14T01:00:00Z",
            "private_pid_namespace": "pid:[4026531836]",
            "numeric_proc_entries": [1, 999999]
        }).encode("utf-8")
        expected = hashlib.sha256(content).hexdigest()
        
        # Test bytes
        self.assertEqual(get_first_action_digest(content), expected)

        # Test string
        self.assertEqual(get_first_action_digest(content.decode("utf-8")), expected)

        # Test file
        f = self.work_dir / "first-action.json"
        f.write_bytes(content)
        self.assertEqual(get_first_action_digest(str(f)), expected)
        self.assertEqual(get_first_action_digest(f), expected)

    def test_launcher_qualification_e537_detects_unsupported(self):
        # Point to the actual installed agent-quota-launcher on this host
        launcher_path = "/home/alexey/git/agent-quota-launcher"
        qual = LauncherQualification(launcher_path)
        report = qual.inspect_interfaces()

        self.assertEqual(report["status"], "UNSUPPORTED")
        self.assertFalse(report["root_profile_supported"])
        self.assertFalse(report["per_job_env_supported"])
        self.assertFalse(report["codex_adapter_supported"])

        # Check that reasons specifically point to missing pieces
        reasons_text = " ".join(report["reasons"])
        self.assertIn("root-run profile absent", reasons_text)
        self.assertIn("per-job environment", reasons_text)
        self.assertIn("codex route absent", reasons_text)

        # Verify dependency packet structure for issue 59
        dep_packet = report["dependency_packet"]
        self.assertIsNotNone(dep_packet)
        self.assertEqual(dep_packet["issue_ref"], "issue59")
        self.assertEqual(dep_packet["target_repo"], "alexeygrigorev/agent-quota-launcher")
        changes = dep_packet["required_changes"]
        self.assertTrue(any("task_profiles" in c["component"] for c in changes))
        self.assertTrue(any("spawn_ql_controller" in c["component"] for c in changes))
        self.assertTrue(any("admission" in c["component"] for c in changes))

    def test_refusal_before_claim_when_unsupported(self):
        mgr = RuntimeManager(launcher_repo_path="/home/alexey/git/agent-quota-launcher")
        eligible, reason, report = mgr.can_process_root_run()

        self.assertFalse(eligible)
        self.assertIn("launcher_unsupported", reason)

        # Ensure that attempting to execute raises an error rather than bypassing
        with self.assertRaises(RuntimeError) as ctx:
            mgr.execute_job("job-123", "root-run", {}, "tok-1", 1, "https://hub.local")
        self.assertIn("Refusing to execute root-run", str(ctx.exception))
        self.assertIn("Direct codex/API bypass forbidden", str(ctx.exception))

    @patch("subprocess.run")
    def test_quota_gate_check_failure_when_low_or_unavailable(self, mock_subproc):
        fake_script = self.work_dir / "quota-gate.py"
        fake_script.write_text("#!/usr/bin/env python3\n")

        mgr = RuntimeManager(quota_gate_script=str(fake_script))

        # Case 1: Quota gate refuses launch
        mock_subproc.return_value = MagicMock(
            returncode=75,
            stdout=json.dumps({"launch_allowed": False, "reason": "quota window depleted"}),
            stderr=""
        )
        ok, reason = mgr.check_codex_window()
        self.assertFalse(ok)
        self.assertIn("quota gate refused", reason)

        # Case 2: Quota gate reports window below 15% floor
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "launch_allowed": True,
                "reason": "ok",
                "windows": {
                    "primary": {"present": True, "percent_remaining": 12.5}
                }
            }),
            stderr=""
        )
        ok, reason = mgr.check_codex_window()
        self.assertFalse(ok)
        self.assertIn("below safety threshold", reason)
        self.assertNotIn("%", reason)

        # Case 3: Quota gate check times out
        import subprocess
        mock_subproc.side_effect = subprocess.TimeoutExpired(cmd=["quota-gate.py"], timeout=15)
        ok, reason = mgr.check_codex_window()
        self.assertFalse(ok)
        self.assertIn("timed out", reason)

    def test_mock_runner_in_private_test_isolation(self):
        mgr = RuntimeManager(allow_mock_runner=True)
        executed = []

        def mock_runner(job_id, kind, payload, token, epoch, url):
            executed.append((job_id, kind, token, epoch))
            return {"status": "succeeded", "summary": "mock run complete", "evidence_refs": ["test.log"]}

        mgr.set_mock_runner(mock_runner)

        eligible, reason, report = mgr.can_process_root_run()
        self.assertTrue(eligible)
        self.assertEqual(reason, "mock_runner_active")

        res = mgr.execute_job("job-mock-1", "root-run", {}, "tok-abc", 5, "https://hub.local")
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0], ("job-mock-1", "root-run", "tok-abc", 5))

    def test_get_first_action_digest_rejects_bool_pid_and_unbound_custody(self):
        # 1. Boolean pid must be rejected
        self.assertIsNone(get_first_action_digest(json.dumps({"pid": True})))
        # 2. Daemon / nobody pid must be rejected
        self.assertIsNone(get_first_action_digest(json.dumps({"pid": 65534})))
        self.assertIsNone(get_first_action_digest(json.dumps({"pid": os.getpid()})))
        # 3. Custody checks must fail closed when expected custody keys are omitted
        valid_evidence = {
            "pid": 999999,
            "job_id": "job-custody",
            "epoch": 3,
            "run_id": "run-xyz",
            "stage": "first_action",
            "utc": "2026-10-14T01:00:00Z",
            "private_pid_namespace": "pid:[4026531836]",
            "numeric_proc_entries": [1, 999999]
        }
        self.assertIsNotNone(get_first_action_digest(
            json.dumps(valid_evidence),
            expected_job_id="job-custody",
            expected_epoch=3,
            expected_run_id="run-xyz"
        ))
        # Missing job_id
        missing_job_id = dict(valid_evidence)
        missing_job_id.pop("job_id")
        self.assertIsNone(get_first_action_digest(
            json.dumps(missing_job_id),
            expected_job_id="job-custody"
        ))
        # Mismatched epoch
        self.assertIsNone(get_first_action_digest(
            json.dumps(valid_evidence),
            expected_epoch=4
        ))
        # Mismatched run_id
        self.assertIsNone(get_first_action_digest(
            json.dumps(valid_evidence),
            expected_run_id="run-different"
        ))

    def test_inspect_interfaces_ignores_docstrings_and_comments(self):
        fake_launcher = self.work_dir / "fake-launcher" / "launcher"
        fake_launcher.mkdir(parents=True, exist_ok=True)
        (fake_launcher / "__init__.py").write_text("", encoding="utf-8")
        (fake_launcher / "task_profiles.py").write_text("TASK_PROFILES = {}\n", encoding="utf-8")
        (fake_launcher / "admission.py").write_text("ADAPTER_ROUTES = ()\n", encoding="utf-8")
        (fake_launcher / "cli.py").write_text(
            '"""Docstring mentioning HUB_JOB_ID and job_env."""\n'
            '# comment mentioning HUB_JOB_ID\n'
            'def spawn_ql_controller():\n'
            '    pass\n',
            encoding="utf-8"
        )
        qual = LauncherQualification(str(self.work_dir / "fake-launcher"))
        report = qual.inspect_interfaces()
        self.assertFalse(report["per_job_env_supported"])

    def test_f1_first_action_digest_rejects_pid_1_ppid_and_invalid_utc(self):
        base = {
            "job_id": "job-1",
            "epoch": 1,
            "pid": 999999,
            "stage": "first_action",
            "utc": "2026-10-14T01:00:00Z",
            "private_pid_namespace": "pid:[4026531836]",
            "numeric_proc_entries": [1, 999999],
        }
        pid1 = dict(base, pid=1, numeric_proc_entries=[1])
        self.assertIsNone(get_first_action_digest(json.dumps(pid1)))

        ppid = dict(base, pid=os.getppid(), numeric_proc_entries=[1, os.getppid()])
        self.assertIsNone(get_first_action_digest(json.dumps(ppid)))

        bad_utc = dict(base, utc="not-an-iso-timestamp")
        self.assertIsNone(get_first_action_digest(json.dumps(bad_utc)))

    def test_f2_inspect_interfaces_ignores_generic_env_and_rejects_magicmock_or_nonbool_allow_mock(self):
        fake_launcher = self.work_dir / "fake-launcher-env" / "launcher"
        fake_launcher.mkdir(parents=True, exist_ok=True)
        (fake_launcher / "__init__.py").write_text("", encoding="utf-8")
        (fake_launcher / "task_profiles.py").write_text("TASK_PROFILES = {}\n", encoding="utf-8")
        (fake_launcher / "admission.py").write_text("ADAPTER_ROUTES = ()\n", encoding="utf-8")
        (fake_launcher / "cli.py").write_text(
            "import os\ndef spawn_ql_controller(cmd, env=None):\n    env = os.environ.copy()\n    return 0\n",
            encoding="utf-8",
        )
        qual = LauncherQualification(str(self.work_dir / "fake-launcher-env"))
        report = qual.inspect_interfaces()
        self.assertFalse(report["per_job_env_supported"])

        mgr_str = RuntimeManager(mock_runner=lambda *a: {"status": "succeeded"}, allow_mock_runner="yes")
        self.assertFalse(mgr_str.allow_mock_runner)

        mgr_mm = RuntimeManager(mock_runner=MagicMock(), allow_mock_runner=True)
        ok, _, _ = mgr_mm.can_process_root_run()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
