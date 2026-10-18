"""Hermetic unit tests for M8 Platform Role Runtime Repair.

Validates:
1. Real nonmock RoleRun construction (success with 0700 workspace, failure with 0755).
2. Workspace tmp preflight checks:
   - 0700 ready positive
   - 0755 unqualified negative
   - strict proof that preflight qualification NEVER runs chmod or alters permissions
   - symlink and /tmp containment rejections
   - uncreated parent writability checks
3. Truthful preclaim qualification via can_process before claim.
4. Clock seam qualification:
   - Injected non-default clock fixture marks adapter/runtime unqualified (role_unqualified: clock_fixture_injected)
   - Production clock (time) qualifies operationally
5. Bounded bootstrap handshake:
   - Delayed genuine first-action >10s (e.g. 15s simulated with controlled clock/events) succeeds
   - Bootstrap timeout at 120s records durable uncertain proof and holds job without retry
6. Definite prelaunch refusal vs ambiguous launch:
   - Witnessed preflight DispatchError has prelaunch_refusal=True, ambiguous_launch=False
   - Regression: RuntimeError("workspace failure after native launch") remains prelaunch_refusal=False, ambiguous_launch=True (no lexical substring guessing)
7. Authority loss / containment:
   - Lease renewal failure triggers cancellation
   - Durable scrubbed proof outcome-uncertain-<job_id>.json written without credentials or tokens
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from agent_hub_host import jobs, role_dispatch
from agent_hub_host.jobs import JobDispatcher, JobsClient, write_role_uncertain_proof
from agent_hub_host.role_dispatch import (
    DispatchError,
    LauncherBackend,
    RoleDispatchAdapter,
    RoleProfileConfig,
    RoleQualification,
    RoleRun,
    TrustedRoleConfig,
    check_workspace_tmp_readiness,
    ensure_private_dir,
    ensure_workspace_tmp_dir,
)
from agent_hub_host.runtime import RuntimeManager


def _create_synthetic_launcher_repo(base_dir: pathlib.Path) -> pathlib.Path:
    """Create a hermetic synthetic Launcher repository for test execution."""
    repo_dir = base_dir / "synth-launcher-repo"
    launcher_pkg = repo_dir / "launcher"
    launcher_pkg.mkdir(parents=True, exist_ok=True)
    (launcher_pkg / "__init__.py").write_text("", encoding="utf-8")

    main_script = """import json, sys

def main():
    payload = {}
    for i, arg in enumerate(sys.argv):
        if arg == "--payload" and i + 1 < len(sys.argv):
            try:
                payload = json.loads(sys.argv[i + 1])
            except Exception:
                pass

    owner = payload.get("owner", "")
    if "writer-run" in owner:
        chosen = {"provider": "antigravity", "model": "gemini-3.1-pro-high"}
    elif "head-recovery" in owner:
        chosen = {"provider": "opencode", "model": "muse-spark-1.3-contributor"}
    else:
        chosen = {"provider": "antigravity"}

    res = {"mode": "dry-run", "chosen": chosen}
    json.dump(res, sys.stdout)

if __name__ == "__main__":
    main()
"""
    (launcher_pkg / "__main__.py").write_text(main_script, encoding="utf-8")
    return repo_dir


class ControlledClock:
    """Test clock that can be stepped deterministically without wall-clock sleep."""

    def __init__(self, start: float = 1700000000.0):
        self._time = start

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


class TestRoleRunRealConstruction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        self.synth_launcher = _create_synthetic_launcher_repo(self.base)

        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                )
            },
            canonical_root_state_dir=self.state_dir,
            launcher_repo=self.synth_launcher,
            launcher_config_dir=self.base / "launcher-cfg",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_nonmock_role_run_construction_success(self):
        """A valid workspace with 0700 permissions constructs a real nonmock RoleRun successfully."""
        # Setup private workspace .local and .local/tmp with 0700
        ws_local = self.workspace / ".local"
        ws_local.mkdir(parents=True)
        ws_local.chmod(0o700)
        ws_tmp = ws_local / "tmp"
        ws_tmp.mkdir(parents=True)
        ws_tmp.chmod(0o700)

        run = RoleRun(
            job_id="job-construction-ok-1234",
            job_token="token-secret-123",
            lease_epoch=2,
            role_binding={"name": "principal", "generation": 1},
            kind="principal-wake",
            payload={"action": "test"},
            hub_url="http://hub.local",
            host_id="test-host",
            config=self.role_cfg,
        )

        self.assertEqual(run.job_id, "job-construction-ok-1234")
        self.assertEqual(run.role_name, "principal")
        self.assertEqual(run.role_generation, 1)
        self.assertEqual(run.lease_epoch, 2)
        self.assertEqual(run.task_id, "hub-role-principal-wake-job-construction-ok-1234-g1-e2")
        self.assertEqual(run.idempotency_key, "hub-role-job-construction-ok-1234-g1-e2")
        self.assertEqual(run.run_id, "job-construction-ok-1234-g1-e2")
        self.assertTrue(run.run_dir.is_dir())
        self.assertTrue(run.job_tmp_dir.is_dir())
        self.assertEqual(os.stat(run.run_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(run.job_tmp_dir).st_mode & 0o777, 0o700)
        self.assertFalse(run._prelaunch_refusal)

    def test_real_nonmock_role_run_construction_fails_on_unqualified_workspace_tmp(self):
        """Workspace with group/other permissions (e.g. 0755) raises prelaunch refusal DispatchError."""
        ws_local = self.workspace / ".local"
        ws_local.mkdir(parents=True)
        ws_local.chmod(0o755)  # Unqualified: group/other accessible

        with self.assertRaises(DispatchError) as ctx:
            RoleRun(
                job_id="job-construction-fail-5678",
                job_token="token-secret-456",
                lease_epoch=1,
                role_binding={"name": "principal", "generation": 1},
                kind="principal-wake",
                payload={"action": "test"},
                hub_url="http://hub.local",
                host_id="test-host",
                config=self.role_cfg,
            )

        self.assertTrue(ctx.exception.prelaunch_refusal)
        self.assertIn("tmpdir_unqualified_permissions", str(ctx.exception))


class TestWorkspaceTmpPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.ws = self.base / "target-ws"
        self.ws.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_check_workspace_tmp_readiness_0700_positive(self):
        """0700 permissions on .local and .local/tmp qualify as ready."""
        local = self.ws / ".local"
        local.mkdir(parents=True)
        local.chmod(0o700)
        tmp = local / "tmp"
        tmp.mkdir(parents=True)
        tmp.chmod(0o700)

        ok, msg = check_workspace_tmp_readiness(self.ws)
        self.assertTrue(ok)
        self.assertEqual(msg, "workspace_tmp_ready")

    def test_check_workspace_tmp_readiness_0755_negative(self):
        """0755 permissions on .local fail preflight check."""
        local = self.ws / ".local"
        local.mkdir(parents=True)
        local.chmod(0o755)

        ok, msg = check_workspace_tmp_readiness(self.ws)
        self.assertFalse(ok)
        self.assertIn("tmpdir_unqualified_permissions", msg)
        self.assertIn("require 0o700", msg)

    def test_check_workspace_tmp_readiness_never_chmods(self):
        """Preflight qualification is strictly read-only and NEVER modifies permissions or chmods."""
        local = self.ws / ".local"
        local.mkdir(parents=True)
        local.chmod(0o755)

        initial_mode = os.stat(local).st_mode & 0o777
        self.assertEqual(initial_mode, 0o755)

        ok, msg = check_workspace_tmp_readiness(self.ws)
        self.assertFalse(ok)

        after_mode = os.stat(local).st_mode & 0o777
        self.assertEqual(after_mode, 0o755, "check_workspace_tmp_readiness must never chmod the target directory")

    def test_check_workspace_tmp_readiness_symlink_rejected(self):
        """Symlinks for .local or tmp are strictly rejected."""
        target_dir = self.base / "other-dir"
        target_dir.mkdir(parents=True)
        target_dir.chmod(0o700)

        local_symlink = self.ws / ".local"
        local_symlink.symlink_to(target_dir)

        ok, msg = check_workspace_tmp_readiness(self.ws)
        self.assertFalse(ok)
        self.assertIn("tmpdir_symlink_rejected", msg)

    def test_check_workspace_tmp_readiness_in_tmp_rejected(self):
        """Workspaces pointing directly inside /tmp are rejected."""
        ws_in_tmp = pathlib.Path("/tmp/some-arbitrary-workspace")
        ok, msg = check_workspace_tmp_readiness(ws_in_tmp)
        self.assertFalse(ok)
        self.assertIn("in_tmp_rejected", msg)

    def test_check_workspace_tmp_readiness_uncreated_parent_writable(self):
        """When .local does not exist yet but workspace is writable, check succeeds."""
        ok, msg = check_workspace_tmp_readiness(self.ws)
        self.assertTrue(ok)
        self.assertEqual(msg, "workspace_tmp_ready")

    def test_check_workspace_tmp_readiness_uncreated_parent_not_writable(self):
        """When workspace is read-only and .local does not exist, preflight fails."""
        self.ws.chmod(0o500)
        try:
            ok, msg = check_workspace_tmp_readiness(self.ws)
            self.assertFalse(ok)
            self.assertIn("tmpdir_parent_not_writable", msg)
        finally:
            self.ws.chmod(0o700)


class TestPreclaimQualification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        self.synth_launcher = _create_synthetic_launcher_repo(self.base)

        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                )
            },
            canonical_root_state_dir=self.state_dir,
            launcher_repo=self.synth_launcher,
            launcher_config_dir=self.base / "launcher-cfg",
        )
        self.adapter = RoleDispatchAdapter(config=self.role_cfg)

        self.offer = {
            "type": "offer",
            "job_id": "job-preclaim-1",
            "kind": "principal-wake",
            "role": {"name": "principal", "generation": 1},
            "lease_epoch": 1,
            "payload": {},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_can_process_rejects_unqualified_workspace_before_claim(self):
        """can_process truthfully rejects an unqualified workspace (0755) before claiming."""
        ws_local = self.workspace / ".local"
        ws_local.mkdir(parents=True)
        ws_local.chmod(0o755)

        ok, reason, _ = self.adapter.can_process("principal-wake")
        self.assertFalse(ok)
        self.assertIn("role_unqualified", reason)
        self.assertIn("tmpdir_unqualified_permissions", reason)

    def test_can_process_accepts_ready_workspace(self):
        """can_process accepts when workspace tmp is ready (0700)."""
        ws_local = self.workspace / ".local"
        ws_local.mkdir(parents=True)
        ws_local.chmod(0o700)
        ws_tmp = ws_local / "tmp"
        ws_tmp.mkdir(parents=True)
        ws_tmp.chmod(0o700)

        ok, reason, _ = self.adapter.can_process("principal-wake")
        self.assertTrue(ok, f"Expected qualification success, got: {reason}")
        self.assertEqual(reason, "role_qualified")


class TestClockSeamQualification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        ws_local = self.workspace / ".local"
        ws_local.mkdir(parents=True)
        ws_local.chmod(0o700)
        ws_tmp = ws_local / "tmp"
        ws_tmp.mkdir(parents=True)
        ws_tmp.chmod(0o700)

        self.synth_launcher = _create_synthetic_launcher_repo(self.base)
        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                )
            },
            canonical_root_state_dir=self.state_dir,
            launcher_repo=self.synth_launcher,
            launcher_config_dir=self.base / "launcher-cfg",
        )

        self.offer = {
            "type": "offer",
            "job_id": "job-clock-1",
            "kind": "principal-wake",
            "role": {"name": "principal", "generation": 1},
            "lease_epoch": 1,
            "payload": {},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_clock_fixture_injected_marks_adapter_unqualified(self):
        """Injected non-default clock fixture marks adapter unqualified and prevents operational claim."""
        fake_clock = ControlledClock()
        adapter = RoleDispatchAdapter(config=self.role_cfg, clock=fake_clock)

        ok, reason, _ = adapter.can_process("principal-wake")
        self.assertFalse(ok)
        self.assertEqual(reason, "role_unqualified: clock_fixture_injected")

    def test_runtime_manager_rejects_clock_fixture_promotion(self):
        """RuntimeManager with bound clock fixture refuses role jobs as unqualified."""
        fake_clock = ControlledClock()
        mgr = RuntimeManager(clock=fake_clock, role_config=self.role_cfg)

        ok, reason, _ = mgr.can_process_role_run("principal-wake")
        self.assertFalse(ok)
        self.assertEqual(reason, "role_unqualified: clock_fixture_injected")

    def test_production_clock_qualifies_operationally(self):
        """Standard production clock path (default time module) qualifies operationally."""
        adapter = RoleDispatchAdapter(config=self.role_cfg)
        ok, reason, _ = adapter.can_process("principal-wake")
        self.assertTrue(ok)
        self.assertEqual(reason, "role_qualified")


class StepControlledClock:
    """Deterministic step-controlled clock that tracks when polling begins."""

    def __init__(self, start: float = 1000.0):
        self._time = start
        self.polling_entered = threading.Event()
        self._call_count = 0
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            self._call_count += 1
            if self._call_count >= 2:
                self.polling_entered.set()
            return self._time

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._time += seconds


class TestBoundedBootstrapHandshake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        ws_local = self.workspace / ".local"
        ws_local.mkdir(parents=True)
        ws_local.chmod(0o700)
        ws_tmp = ws_local / "tmp"
        ws_tmp.mkdir(parents=True)
        ws_tmp.chmod(0o700)

        self.synth_launcher = _create_synthetic_launcher_repo(self.base)
        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                )
            },
            canonical_root_state_dir=self.state_dir,
            launcher_repo=self.synth_launcher,
            launcher_config_dir=self.base / "launcher-cfg",
        )
        self.base_url = "http://127.0.0.1:8787"
        self.host_id = "test-host"

    def tearDown(self):
        self.tmp.cleanup()

    def _valid_binding(self, kind: str = "principal-wake") -> dict:
        return {
            "name": f"automation:platform:{kind}",
            "generation": 1,
            "epoch": 1,
            "profile": kind,
            "host_id": self.host_id,
            "ttl_seconds": 120,
        }

    def test_delayed_first_action_fails_under_old_10s_bound(self):
        """Under the old 10s bound, evidence delayed by 15s fails closed with bootstrap_timeout."""
        job_id = f"job-delayed-10s-{time.time_ns()}"
        client = MagicMock()
        client.commit_role_execution.return_value = {
            "execute": True,
            "epoch": 1,
            "generation": 1,
            "role": "automation:platform:principal-wake",
            "profile": "principal-wake",
            "payload": {},
        }

        clock = StepControlledClock(1000.0)
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        dispatcher = JobDispatcher(
            base_url=self.base_url,
            host_id=self.host_id,
            token_provider=lambda: "host-tok",
            client=client,
            runtime_mgr=runtime_mgr,
        )
        dispatcher.clock = clock

        dummy_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            runs_dir = self.role_cfg.runs_dir
            runs_dir.mkdir(parents=True, exist_ok=True)
            fa_path = runs_dir / f"first-action-{job_id}.json"

            def mock_execute(*args, **kwargs):
                # Wait until dispatcher has entered the polling loop
                clock.polling_entered.wait(timeout=2.0)
                # Advance controlled clock past the 10s bound (to 15s) while evidence is still absent
                clock.advance(15.0)
                time.sleep(0.05)
                # Write evidence only after the 10s bound has expired
                ev = {
                    "job_id": job_id,
                    "role_name": "automation:platform:principal-wake",
                    "role": "automation:platform:principal-wake",
                    "kind": "principal-wake",
                    "epoch": 1,
                    "generation": 1,
                    "stage": "first_action",
                    "pid": dummy_proc.pid,
                    "native_session": "11111111-2222-3333-4444-555555555555",
                    "utc": "2026-10-18T05:00:00Z",
                    "isolation_kind": "native_cgroup",
                }
                fa_path.write_text(json.dumps(ev), encoding="utf-8")
                cb = kwargs.get("start_callback")
                if cb:
                    cb({"data": ev})
                return {"status": "ok"}

            with patch.object(runtime_mgr, "execute_job", side_effect=mock_execute):
                with patch.object(role_dispatch, "BOOTSTRAP_TIMEOUT_SEC", 10.0):
                    job_info = {
                        "job_id": job_id,
                        "kind": "principal-wake",
                        "op_qualified": False,
                        "job_token": "tok-123",
                        "lease_epoch": 1,
                        "deadline_at": None,
                        "started_at": 1000.0,
                        "started_wall": time.time() - 2.0,
                        "stop_event": threading.Event(),
                        "role_binding": self._valid_binding("principal-wake"),
                    }
                    dispatcher._run_job_lifecycle(job_info, {})

            proof_path = self.role_cfg.runs_dir / f"outcome-uncertain-{job_id}.json"
            self.assertTrue(proof_path.exists(), "Under old 10s bound, delayed evidence must record uncertain proof")
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertEqual(proof.get("status"), "uncertain")
            self.assertEqual(proof.get("phase"), "bootstrap_timeout")
            self.assertFalse(proof.get("prelaunch_refusal"))
            self.assertTrue(proof.get("ambiguous_launch"))
            client.start_job.assert_not_called()
        finally:
            dummy_proc.kill()
            dummy_proc.wait()

    def test_delayed_first_action_succeeds_under_repaired_120s_bound(self):
        """Under repaired 120s bound, dispatcher waits past 10s (at 15s), receives evidence, and completes server start."""
        job_id = f"job-delayed-120s-{time.time_ns()}"
        client = MagicMock()
        client.commit_role_execution.return_value = {
            "execute": True,
            "epoch": 1,
            "generation": 1,
            "role": "automation:platform:principal-wake",
            "profile": "principal-wake",
            "payload": {},
        }
        client.start_job.return_value = {"status": "started", "start_res": {"started": True}}
        client.finish_job.return_value = {"status": "finished"}

        clock = StepControlledClock(1000.0)
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        dispatcher = JobDispatcher(
            base_url=self.base_url,
            host_id=self.host_id,
            token_provider=lambda: "host-tok",
            client=client,
            runtime_mgr=runtime_mgr,
        )
        dispatcher.clock = clock

        dummy_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            runs_dir = self.role_cfg.runs_dir
            runs_dir.mkdir(parents=True, exist_ok=True)
            fa_path = runs_dir / f"first-action-{job_id}.json"

            def mock_execute(*args, **kwargs):
                # Wait until dispatcher has computed poll_deadline (1000 + 120 = 1120) and entered loop
                clock.polling_entered.wait(timeout=2.0)
                # Advance clock past old 10s limit (to 15s) while evidence is still absent
                clock.advance(15.0)
                time.sleep(0.02)
                # Dispatcher remains waiting because 1015 < 1120. Now produce custody-verified evidence
                ev = {
                    "job_id": job_id,
                    "role_name": "automation:platform:principal-wake",
                    "role": "automation:platform:principal-wake",
                    "kind": "principal-wake",
                    "epoch": 1,
                    "generation": 1,
                    "stage": "first_action",
                    "pid": dummy_proc.pid,
                    "native_session": "11111111-2222-3333-4444-555555555555",
                    "utc": "2026-10-18T05:00:00Z",
                    "isolation_kind": "native_cgroup",
                }
                tmp_fa = fa_path.with_suffix(".tmp")
                tmp_fa.write_text(json.dumps(ev), encoding="utf-8")
                tmp_fa.replace(fa_path)

                cb = kwargs.get("start_callback")
                if cb:
                    start_reply = cb({"data": ev})
                    if start_reply.get("status") != "started":
                        return {"status": "failed", "summary": "start ack refused"}

                return {"status": "succeeded", "summary": "completed"}

            with patch.object(runtime_mgr, "execute_job", side_effect=mock_execute):
                job_info = {
                    "job_id": job_id,
                    "kind": "principal-wake",
                    "op_qualified": False,
                    "job_token": "tok-123",
                    "lease_epoch": 1,
                    "deadline_at": None,
                    "started_at": 1000.0,
                    "started_wall": time.time() - 2.0,
                    "stop_event": threading.Event(),
                    "role_binding": self._valid_binding("principal-wake"),
                }
                dispatcher._run_job_lifecycle(job_info, {})

            # Verify start_job was called and handshake succeeded
            client.start_job.assert_called_once()
            # Verify no outcome-uncertain proof was written
            uncertain_proof = self.role_cfg.runs_dir / f"outcome-uncertain-{job_id}.json"
            self.assertFalse(uncertain_proof.exists(), "outcome-uncertain file should not exist on success")
        finally:
            dummy_proc.kill()
            dummy_proc.wait()

    def test_bootstrap_timeout_records_uncertain_proof_and_holds_job(self):
        """Worker that fails to produce first action within bootstrap window records durable scrubbed proof and holds."""
        job_id = "job-bootstrap-timeout-888"
        client = MagicMock()
        client.commit_role_execution.return_value = {
            "execute": True,
            "epoch": 1,
            "generation": 1,
            "role": "automation:platform:principal-wake",
            "profile": "principal-wake",
            "payload": {},
        }

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        dispatcher = JobDispatcher(
            base_url=self.base_url,
            host_id=self.host_id,
            token_provider=lambda: "host-tok",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        stop_worker = threading.Event()

        def hanging_execute(job_id, kind, payload, job_token, lease_epoch, base_url,
                            start_callback=None, role_binding=None, stop_event=None, deadline_sec=None):
            # Hangs without creating first-action evidence
            stop_worker.wait(timeout=1.0)
            return {"status": "ok"}

        with patch.object(runtime_mgr, "execute_job", side_effect=hanging_execute):
            with patch.object(role_dispatch, "BOOTSTRAP_TIMEOUT_SEC", 0.05):
                job_info = {
                    "job_id": job_id,
                    "kind": "principal-wake",
                    "op_qualified": False,
                    "job_token": "tok-secret-abc",
                    "lease_epoch": 1,
                    "deadline_at": None,
                    "started_at": time.time(),
                    "started_wall": time.time(),
                    "stop_event": threading.Event(),
                    "role_binding": self._valid_binding("principal-wake"),
                }
                dispatcher._run_job_lifecycle(job_info, {})

        stop_worker.set()

        # Verify durable scrubbed proof in runs_dir
        proof_path = self.role_cfg.runs_dir / f"outcome-uncertain-{job_id}.json"
        self.assertTrue(proof_path.exists(), "Durable proof file must exist")
        proof = json.loads(proof_path.read_text(encoding="utf-8"))

        self.assertEqual(proof.get("job_id"), job_id)
        self.assertEqual(proof.get("status"), "uncertain")
        self.assertTrue(proof.get("uncertain"))
        self.assertEqual(proof.get("phase"), "bootstrap_timeout")
        self.assertFalse(proof.get("prelaunch_refusal"))
        self.assertTrue(proof.get("ambiguous_launch"))
        self.assertNotIn("tok-secret-abc", json.dumps(proof))


class TestDefinitePrelaunchRefusalVsAmbiguousLaunch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.runs_dir = self.base / "runs"
        self.runs_dir.mkdir(parents=True)
        self.run_dir = self.runs_dir / "principal-wake-job-test"
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prelaunch_refusal_marks_proof_accurately(self):
        """Prelaunch refusal (e.g. preflight DispatchError) marks prelaunch_refusal=True, ambiguous_launch=False."""
        proof_path = write_role_uncertain_proof(
            runs_dir=self.runs_dir,
            run_dir=self.run_dir,
            job_id="job-prelaunch-1",
            role={"name": "principal", "generation": 1},
            kind="principal-wake",
            lease_epoch=1,
            phase="preflight",
            cause="entrypoint validation failed",
            is_prelaunch_refusal=True,
            exc=DispatchError("entrypoint invalid", prelaunch_refusal=True),
        )
        self.assertTrue(proof_path.exists())
        proof = json.loads(proof_path.read_text(encoding="utf-8"))

        self.assertTrue(proof.get("prelaunch_refusal"))
        self.assertFalse(proof.get("ambiguous_launch"))
        self.assertEqual(proof.get("phase"), "preflight")

    def test_regression_runtime_error_workspace_failure_after_native_launch(self):
        """Platform regression test: RuntimeError('workspace failure after native launch')
        must remain ambiguous_launch=True, prelaunch_refusal=False with NO lexical guessing.
        """
        # Exception contains "workspace", which previously triggered false lexical classification
        exc = RuntimeError("workspace failure after native launch")

        # Explicitly verify bool(getattr(exc, "prelaunch_refusal", False)) is False
        is_prelaunch = bool(getattr(exc, "prelaunch_refusal", False))
        self.assertFalse(is_prelaunch, "Generic RuntimeError must not be treated as prelaunch refusal")

        proof_path = write_role_uncertain_proof(
            runs_dir=self.runs_dir,
            run_dir=self.run_dir,
            job_id="job-regression-launch-1",
            role={"name": "principal", "generation": 1},
            kind="principal-wake",
            lease_epoch=1,
            phase="execution_error",
            cause=str(exc),
            is_prelaunch_refusal=is_prelaunch,
            exc=exc,
        )
        proof = json.loads(proof_path.read_text(encoding="utf-8"))

        self.assertFalse(proof.get("prelaunch_refusal"))
        self.assertTrue(proof.get("ambiguous_launch"))
        self.assertEqual(proof.get("phase"), "execution_error")
        self.assertIn("workspace failure after native launch", proof.get("cause"))

    def test_credentials_scrubbed_in_uncertain_proof(self):
        """Tokens, bearer headers, and credentials are scrubbed from uncertain proof causes."""
        exc = RuntimeError("Authentication failed with token bearer-secret-key-1234567890")
        scrubbed_cause = role_dispatch.scrub(str(exc), "bearer-secret-key-1234567890")

        proof_path = write_role_uncertain_proof(
            runs_dir=self.runs_dir,
            run_dir=self.run_dir,
            job_id="job-scrub-1",
            role={"name": "principal", "generation": 1},
            kind="principal-wake",
            lease_epoch=1,
            phase="execution_error",
            cause=scrubbed_cause,
            is_prelaunch_refusal=False,
            exc=exc,
        )
        content = proof_path.read_text(encoding="utf-8")
        self.assertNotIn("bearer-secret-key-1234567890", content)
        self.assertIn("[redacted]", content)


class TestAuthorityLossAndContainment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        ws_local = self.workspace / ".local"
        ws_local.mkdir(parents=True)
        ws_local.chmod(0o700)
        ws_tmp = ws_local / "tmp"
        ws_tmp.mkdir(parents=True)
        ws_tmp.chmod(0o700)

        self.synth_launcher = _create_synthetic_launcher_repo(self.base)
        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                )
            },
            canonical_root_state_dir=self.state_dir,
            launcher_repo=self.synth_launcher,
            launcher_config_dir=self.base / "launcher-cfg",
        )
        self.base_url = "http://127.0.0.1:8787"
        self.host_id = "test-host"

    def tearDown(self):
        self.tmp.cleanup()

    def _valid_binding(self, kind: str = "principal-wake") -> dict:
        return {
            "name": f"automation:platform:{kind}",
            "generation": 1,
            "epoch": 1,
            "profile": kind,
            "host_id": self.host_id,
            "ttl_seconds": 120,
        }

    def test_lease_renewal_failure_triggers_cancellation_and_uncertain_hold(self):
        """Lease renewal exception / authority loss triggers cancel and uncertain hold."""
        job_id = f"job-authority-loss-{time.time_ns()}"
        client = MagicMock()
        client.commit_role_execution.return_value = {
            "execute": True,
            "epoch": 1,
            "generation": 1,
            "role": "automation:platform:principal-wake",
            "profile": "principal-wake",
            "payload": {},
        }
        client.start_job.return_value = {"status": "started", "start_res": {"started": True}}
        # Renewal throws partition exception
        client.renew_job.side_effect = Exception("network partition during lease renewal")

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        dispatcher = JobDispatcher(
            base_url=self.base_url,
            host_id=self.host_id,
            token_provider=lambda: "host-tok",
            client=client,
            runtime_mgr=runtime_mgr,
        )
        dispatcher.renewal_interval_sec = 0.02

        dummy_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            runs_dir = self.role_cfg.runs_dir
            runs_dir.mkdir(parents=True, exist_ok=True)
            fa_path = runs_dir / f"first-action-{job_id}.json"

            def worker_exec(job_id, kind, payload, job_token, lease_epoch, base_url,
                            start_callback=None, role_binding=None, stop_event=None, deadline_sec=None):
                evidence = {
                    "job_id": job_id,
                    "role_name": "automation:platform:principal-wake",
                    "role": "automation:platform:principal-wake",
                    "kind": "principal-wake",
                    "epoch": lease_epoch,
                    "generation": 1,
                    "stage": "first_action",
                    "pid": dummy_proc.pid,
                    "native_session": "99999999-8888-7777-6666-555555555555",
                    "utc": "2026-10-18T05:00:00Z",
                    "isolation_kind": "native_cgroup",
                }
                fa_path.write_text(json.dumps(evidence), encoding="utf-8")
                if start_callback:
                    start_callback({"data": evidence})

                # Wait until cancelled or stop_event is set
                while not (stop_event and stop_event.is_set()):
                    time.sleep(0.01)
                return {"status": "cancelled"}

            with patch.object(runtime_mgr, "execute_job", side_effect=worker_exec):
                job_info = {
                    "job_id": job_id,
                    "kind": "principal-wake",
                    "op_qualified": False,
                    "job_token": "tok-123",
                    "lease_epoch": 1,
                    "deadline_at": None,
                    "started_at": time.time() - 2.0,
                    "started_wall": time.time() - 2.0,
                    "stop_event": threading.Event(),
                    "role_binding": self._valid_binding("principal-wake"),
                }
                dispatcher._run_job_lifecycle(job_info, {})

            # Verify durable proof was written
            proof_path = self.role_cfg.runs_dir / f"outcome-uncertain-{job_id}.json"
            self.assertTrue(proof_path.exists())
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertEqual(proof.get("status"), "uncertain")
            self.assertEqual(proof.get("phase"), "lease_renewal_failed")
            self.assertFalse(proof.get("prelaunch_refusal"))
            self.assertTrue(proof.get("ambiguous_launch"))
        finally:
            dummy_proc.kill()
            dummy_proc.wait()

    def test_constructor_clock_fixture_sticky_across_dispatcher_bind(self):
        """Constructor-injected fixture clock must remain strictly sticky across dispatcher bind_hub."""
        class FixtureClock:
            def time(self):
                return 123456789.0

        class NoHTTP:
            def __init__(self):
                self.claims = 0
            def claim_job(self, *args, **kwargs):
                self.claims += 1
                return {"error": "diagnostic_stop_before_any_HTTP"}
            def decline_job(self, *args, **kwargs):
                return {"diagnostic_stub": True}

        # Case 1: constructor fixture via adapter_kwargs
        fc = FixtureClock()
        m1 = RuntimeManager(role_config=self.role_cfg, adapter_kwargs={"clock": fc})
        self.assertTrue(m1._bound_clock_fixture)
        self.assertIs(m1.clock, fc)
        ok, reason, rep = m1.can_process_role_run("principal-wake")
        self.assertFalse(ok)
        self.assertEqual(reason, "role_unqualified: clock_fixture_injected")

        client1 = NoHTTP()
        d1 = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id="test-host-id",
            token_provider=lambda: "not-a-token",
            client=client1,
            runtime_mgr=m1,
        )
        # Even after JobDispatcher.__init__ called m1.bind_hub(..., clock=time), fixture remains sticky
        self.assertTrue(m1._bound_clock_fixture)
        self.assertIs(m1.clock, fc)
        ok_after, reason_after, _ = m1.can_process_role_run("principal-wake")
        self.assertFalse(ok_after)
        self.assertEqual(reason_after, "role_unqualified: clock_fixture_injected")

        offer = {
            "id": "job-offer-sticky-test-1",
            "kind": "principal-wake",
            "payload": {"project": "test"},
            "role_binding": self._valid_binding("principal-wake"),
        }
        res1 = d1.handle_offer(offer)
        self.assertFalse(res1.get("accepted"))
        self.assertEqual(client1.claims, 0, "Constructor fixture must never make claim calls")

        # Case 2: bound fixture then rebind with normal clock
        m2 = RuntimeManager(role_config=self.role_cfg)
        self.assertFalse(m2._bound_clock_fixture)
        m2.bind_hub("https://hub.dtcdev.click", lambda: "tok", "test-host-id", fc)
        self.assertTrue(m2._bound_clock_fixture)
        self.assertIs(m2.clock, fc)

        client2 = NoHTTP()
        d2 = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id="test-host-id",
            token_provider=lambda: "not-a-token",
            client=client2,
            runtime_mgr=m2,
        )
        self.assertTrue(m2._bound_clock_fixture)
        self.assertIs(m2.clock, fc)
        res2 = d2.handle_offer(offer)
        self.assertFalse(res2.get("accepted"))
        self.assertEqual(client2.claims, 0, "Bound fixture must never make claim calls")


if __name__ == "__main__":
    unittest.main()

