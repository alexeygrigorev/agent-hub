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
        self.assertTrue(any("execute_transient_task_unit" in c["component"] for c in changes))
        self.assertTrue(any("headless-root-run.sh" in c["component"] for c in changes))
        self.assertTrue(any("pins" in c["component"] for c in changes))
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



class TestGenuineQualification(unittest.TestCase):
    """Genuine operational qualification: honest verdicts, no blanket pass."""

    def test_inspect_interfaces_is_honest_about_installed_launcher(self):
        report = LauncherQualification().inspect_interfaces()
        self.assertIn(report["status"], ("SUPPORTED", "UNSUPPORTED", "UNKNOWN"))
        if report["status"] == "UNSUPPORTED":
            self.assertTrue(report["reasons"])
            self.assertIsNotNone(report["dependency_packet"])

    def test_installed_machine_is_unsupported_with_concrete_reasons_and_no_pins(self):
        from agent_hub_host import root_dispatch as rd
        self.assertEqual(rd.ACCEPTED_CAG_SCRIPT_SHA256, frozenset())
        self.assertEqual(rd.ACCEPTED_LAUNCHER_COMMITS, frozenset())
        report = LauncherQualification().inspect_interfaces()
        self.assertEqual(report["status"], "UNSUPPORTED")
        text = " ".join(report["reasons"])
        self.assertIn("launcher_commit_not_pinned", text)
        self.assertFalse(report["launcher_pin_accepted"])
        self.assertFalse(report["cag_script_pin_accepted"])

    def _supported_report(self):
        return {
            "launcher_repo": "/x", "installed_sha": "abc", "status": "SUPPORTED",
            "reasons": [], "dependency_packet": None,
            "root_profile_supported": True, "per_job_env_supported": True,
            "codex_adapter_supported": True, "adapter_routes": ["codex"],
            "entrypoint": "/x/headless-root-run.sh", "entrypoint_status": "verified",
        }

    def _manager(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from agent_hub_host import root_dispatch as rd
        base = pathlib.Path(tmp.name)
        cfg = rd.TrustedRootConfig.build("/x", "/x", base / "headless-root", base / "host")
        gate = base / "quota-gate.py"
        gate.write_text("print('{}')\n")
        return RuntimeManager(config=cfg, quota_gate_script=str(gate), **kw)

    def test_can_process_root_run_fail_closed_when_scheduling_unbound_or_denied(self):
        mgr = self._manager()
        with patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces", return_value=self._supported_report()):
            eligible, reason, report = mgr.can_process_root_run()
            self.assertFalse(eligible)
            self.assertIn("scheduling_reader_unbound", reason)
            self.assertFalse(report.get("operational_qualified", True))
            with patch.object(RuntimeManager, "check_scheduling", return_value=(False, "scheduling_response_stale")):
                eligible, reason, report = mgr.can_process_root_run()
        self.assertFalse(eligible)
        self.assertIn("scheduling_response_stale", reason)
        self.assertFalse(report["operational_qualified"])

    def test_can_process_root_run_qualified_when_all_verified(self):
        mgr = self._manager()
        with patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")), \
             patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces", return_value=self._supported_report()):
            eligible, reason, report = mgr.can_process_root_run()
        self.assertTrue(eligible)
        self.assertTrue(report.get("operational_qualified"))
        self.assertFalse(report.get("is_fixture_only"))

    def test_injected_fixture_executor_can_never_be_operationally_qualified(self):
        mgr = self._manager(adapter_kwargs={"launcher_executor": lambda req: {}})
        with patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")), \
             patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces", return_value=self._supported_report()):
            eligible, reason, report = mgr.can_process_root_run()
        self.assertFalse(eligible)
        self.assertTrue(report["is_fixture_only"])
        self.assertFalse(report["operational_qualified"])

    def test_every_injected_seam_can_never_be_operationally_qualified(self):
        """F3: not only the launcher executor: probes, controller and checks are fixtures too."""
        seams = {"proc_probe": lambda pid: None, "invocation_probe": lambda unit: None,
                 "host_namespace": "pid:[1]", "unit_controller": object(), "scheduling_check": lambda: (True, "x"),
                 "quota_check": lambda: (True, "x"), "quse_provider": lambda: {}}
        for name, value in seams.items():
            mgr = self._manager(adapter_kwargs={name: value})
            with patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")), \
                 patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces",
                       return_value=self._supported_report()):
                eligible, reason, report = mgr.can_process_root_run()
            self.assertFalse(eligible, name)
            self.assertTrue(report["is_fixture_only"], name)
            self.assertFalse(report["operational_qualified"], name)
            self.assertIn(name, reason)

    def test_conflated_config_is_unqualified_before_any_effect(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"HR_STATE": d}), \
                 patch("agent_hub_host.root_dispatch.DEFAULT_HOST_STATE_DIR", d + "/host"):
                mgr = RuntimeManager()
        eligible, reason, report = mgr.can_process_root_run()
        self.assertFalse(eligible)
        self.assertIn("conflated", reason)

    def test_unbound_manager_check_scheduling_denies(self):
        self.assertEqual(self._manager().check_scheduling(), (False, "scheduling_reader_unbound"))

    def test_bind_hub_wires_a_live_reader_with_a_host_dir_cache(self):
        mgr = self._manager()
        calls = []

        def transport(url, token, timeout):
            import time as _t
            from agent_hub_host import root_dispatch as rd
            calls.append((url, token))
            return {"enabled": True, "updated_at": None, "server_time": rd.iso_utc(_t.time())}
        mgr.bind_hub("https://hub.example", lambda: "HOST-TOK", "host-1")
        mgr._scheduling._http_get = transport
        self.assertEqual(mgr.check_scheduling(), (True, "scheduling_enabled"))
        self.assertEqual(mgr.check_scheduling("JOB"), (True, "scheduling_enabled"))
        self.assertEqual(calls, [("https://hub.example/api/root/scheduling", "HOST-TOK"),
                                 ("https://hub.example/api/root/scheduling", "JOB")])
        self.assertTrue((mgr.config.host_state_dir / "scheduling.json").exists())
        self.assertFalse((mgr.config.canonical_state_dir).exists())

    def test_execute_job_dispatches_via_adapter_when_qualified(self):
        mgr = self._manager()
        with patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")), \
             patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces", return_value=self._supported_report()), \
             patch("agent_hub_host.root_dispatch.RootDispatchAdapter.dispatch") as disp:
            disp.return_value = {"status": "ok", "fixture_only": False}
            res = mgr.execute_job("job-x", "root-run", {}, "tok", 3, "https://hub")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(disp.call_args.kwargs["job_id"], "job-x")
        self.assertEqual(disp.call_args.kwargs["lease_epoch"], 3)

    def test_execute_job_and_begin_root_run_refuse_when_unqualified(self):
        mgr = RuntimeManager()
        for fn in (lambda: mgr.execute_job("j", "root-run", {}, "t", 1, "https://hub"),
                   lambda: mgr.begin_root_run("j", "t", 1, "https://hub", {})):
            with self.assertRaises(RuntimeError) as ctx:
                fn()
            self.assertIn("Refusing to execute root-run", str(ctx.exception))

    def test_execute_job_without_hub_start_callback_never_runs_a_unit(self):
        from agent_hub_host import root_dispatch as rd
        executed = []
        mgr = self._manager(adapter_kwargs={"launcher_executor": lambda req: executed.append(req) or {}})
        with patch.object(RuntimeManager, "can_process_root_run", return_value=(True, "q", {})):
            with self.assertRaises(rd.DispatchError) as ctx:
                mgr.execute_job("j", "root-run", {}, "t", 1, "https://hub")
        self.assertEqual(str(ctx.exception), "start_callback_required")
        self.assertEqual(executed, [])


    def test_clock_and_launcher_override_refused_before_dispatch_or_state_effects(self):
        class FrozenClock:
            def time(self):
                return 1_000_000_000.0
        for name, value in (("clock", FrozenClock()), ("launcher_repo", "/unqualified/launcher")):
            with self.subTest(seam=name):
                mgr = self._manager(adapter_kwargs={name: value})
                before = sorted(str(p) for p in mgr.config.host_state_dir.parent.rglob("*"))
                with patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces",
                           side_effect=lambda: self._supported_report()), \
                     patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")) as scheduling, \
                     patch("agent_hub_host.root_dispatch.RootDispatchAdapter.begin") as begin, \
                     patch("agent_hub_host.root_dispatch.RootDispatchAdapter.dispatch") as dispatch:
                    eligible, reason, report = mgr.can_process_root_run()
                    self.assertFalse(eligible)
                    self.assertIn(name, reason)
                    self.assertTrue(report["is_fixture_only"])
                    self.assertFalse(report["operational_qualified"])
                    with self.assertRaises(RuntimeError):
                        mgr.begin_root_run("job-g", "fixture-token", 1, "https://hub.example", {})
                    with self.assertRaises(RuntimeError):
                        mgr.execute_job("job-g", "root-run", {}, "fixture-token", 1, "https://hub.example")
                    begin.assert_not_called()
                    dispatch.assert_not_called()
                    scheduling.assert_not_called()
                self.assertEqual(sorted(str(p) for p in mgr.config.host_state_dir.parent.rglob("*")), before)

    def test_adapter_executor_always_uses_qualified_launcher_checkout(self):
        for override in (None, "/unqualified/launcher"):
            with self.subTest(override=override):
                mgr = self._manager(adapter_kwargs={} if override is None else {"launcher_repo": override})
                adapter = mgr._make_adapter()  # construction alone: no launch or qualification bypass
                self.assertEqual(adapter.executor.launcher_repo, str(mgr.qualification.launcher_path))
                self.assertFalse(adapter.is_fixture_executor)
                self.assertIs(adapter.clock, __import__("time"))

    def test_even_matching_caller_launcher_override_is_not_operational(self):
        mgr = self._manager()
        mgr._adapter_kwargs["launcher_repo"] = str(mgr.qualification.launcher_path)
        with patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces",
                   return_value=self._supported_report()), \
             patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")):
            eligible, reason, report = mgr.can_process_root_run()
        self.assertFalse(eligible)
        self.assertIn("launcher_repo", reason)
        self.assertTrue(report["is_fixture_only"])
        self.assertFalse(report["operational_qualified"])


    def test_bound_custom_clock_remains_fixture_after_trusted_rebinding(self):
        import time
        class FutureClock:
            def time(self):
                return time.time() + 3600
        mgr = self._manager()
        for clock in (FutureClock(), None, time):
            with self.subTest(clock=clock):
                mgr.bind_hub("https://hub.example", lambda: "fixture-host-token", "fixture-host", clock)
                with patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces",
                           return_value=self._supported_report()), \
                     patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")) as check:
                    eligible, reason, report = mgr.can_process_root_run()
                self.assertFalse(eligible)
                self.assertIn("clock", reason)
                self.assertTrue(report["is_fixture_only"])
                self.assertFalse(report["operational_qualified"])
                check.assert_not_called()

    def test_default_bound_clocks_do_not_create_fixture_qualification(self):
        import time
        mgr = self._manager()
        for clock in (None, time):
            with self.subTest(clock=clock):
                mgr.bind_hub("https://hub.example", lambda: "fixture-host-token", "fixture-host", clock)
                with patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces",
                           return_value=self._supported_report()), \
                     patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")):
                    eligible, reason, report = mgr.can_process_root_run()
                self.assertTrue(eligible)
                self.assertFalse(report["is_fixture_only"])
                self.assertTrue(report["operational_qualified"])


class TestPinnedQualificationBoundary(unittest.TestCase):
    """The positive path is reachable only with pins, real script bytes and the full explicit API."""

    def setUp(self):
        import subprocess
        from agent_hub_host import root_dispatch as rd
        self.rd = rd
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.launcher = base / "launcher-repo"
        (self.launcher / "launcher").mkdir(parents=True)
        (self.launcher / "launcher" / "__init__.py").write_text("")
        (self.launcher / "launcher" / "task_profiles.py").write_text("TASK_PROFILES = {'root-run': {}}\n")
        (self.launcher / "launcher" / "admission.py").write_text("ADAPTER_ROUTES = ('codex',)\n")
        (self.launcher / "launcher" / "task_units.py").write_text(
            "def execute_transient_task_unit(task_id, command_argv, memory_mb, workspace, tmpdir=None, "
            "timeout_sec=1.0, quse_json=None, provider=None, check_capacity=False, profile=None, job_env=None):\n"
            "    return {}\n")
        git = ["git", "-C", str(self.launcher), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
        self.sha = subprocess.run(["git", "-C", str(self.launcher), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        self.repo = base / "cag"
        self.script = self.repo / "scripts" / "root" / "headless-root-run.sh"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("#!/bin/bash\nexit 0\n")
        self.script.chmod(0o755)
        self.cfg = rd.TrustedRootConfig.build(self.script, self.repo, base / "headless-root", base / "host")
        self.script_sha = rd.file_sha256(self.script)

    def qual(self, cag=None, launcher=None):
        return LauncherQualification(str(self.launcher), config=self.cfg,
                                     cag_pins=frozenset(cag if cag is not None else []),
                                     launcher_pins=frozenset(launcher if launcher is not None else []))

    def test_supported_only_with_both_pins_and_complete_boundary(self):
        rep = self.qual({self.script_sha}, {self.sha}).inspect_interfaces()
        self.assertEqual(rep["status"], "SUPPORTED", rep["reasons"])
        self.assertTrue(rep["per_job_env_supported"] and rep["root_profile_supported"])

    def test_missing_either_pin_is_unsupported(self):
        for cag, launcher, needle in (({self.script_sha}, set(), "launcher_commit_not_pinned"),
                                      (set(), {self.sha}, "cag_script_not_pinned"),
                                      ({"0" * 64}, {self.sha}, "cag_script_not_pinned"),
                                      ({self.script_sha}, {"f" * 40}, "launcher_commit_not_pinned")):
            rep = self.qual(cag, launcher).inspect_interfaces()
            self.assertEqual(rep["status"], "UNSUPPORTED")
            self.assertIn(needle, " ".join(rep["reasons"]))

    def test_changed_script_bytes_invalidate_the_pin(self):
        self.script.write_text("#!/bin/bash\nexit 1\n")
        rep = self.qual({self.script_sha}, {self.sha}).inspect_interfaces()
        self.assertEqual(rep["status"], "UNSUPPORTED")

    def test_pins_without_the_explicit_job_env_parameter_do_not_qualify(self):
        tu = self.launcher / "launcher" / "task_units.py"
        tu.write_text(tu.read_text().replace(", job_env=None", ""))
        import subprocess
        git = ["git", "-C", str(self.launcher), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["commit", "-qam", "y"], check=True)
        sha = subprocess.run(["git", "-C", str(self.launcher), "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        rep = self.qual({self.script_sha}, {sha}).inspect_interfaces()
        self.assertEqual(rep["status"], "UNSUPPORTED")
        self.assertIn("per-job environment", " ".join(rep["reasons"]))

    def test_gate_proof_follows_the_same_qualification(self):
        from agent_hub_host.runtime import operational_root_profile_proof
        ok, why = operational_root_profile_proof(qualification=self.qual({self.script_sha}, {self.sha}))
        self.assertTrue(ok, why)
        ok, why = operational_root_profile_proof(qualification=self.qual())
        self.assertFalse(ok)
        self.assertIn("root_profile_unproven", why)

    def test_cached_foreign_launcher_modules_cannot_answer_for_the_inspected_checkout(self):
        import types
        fake = types.ModuleType("launcher")
        tp = types.ModuleType("launcher.task_profiles")
        tp.TASK_PROFILES = {}
        with patch.dict(sys.modules, {"launcher": fake, "launcher.task_profiles": tp}):
            rep = self.qual({self.script_sha}, {self.sha}).inspect_interfaces()
            self.assertEqual(rep["status"], "SUPPORTED", rep["reasons"])
            self.assertIs(sys.modules["launcher"], fake)  # restored afterwards


class TestMissingSdkAndErrors(unittest.TestCase):
    def test_proof_denies_when_launcher_sdk_is_missing_or_inspection_raises(self):
        from agent_hub_host.runtime import operational_root_profile_proof
        ok, why = operational_root_profile_proof("/nonexistent/launcher")
        self.assertFalse(ok)
        with patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces", side_effect=RuntimeError("boom")):
            ok, why = operational_root_profile_proof()
        self.assertEqual((ok, why), (False, "root_profile_unproven: RuntimeError"))


if __name__ == "__main__":
    unittest.main()
