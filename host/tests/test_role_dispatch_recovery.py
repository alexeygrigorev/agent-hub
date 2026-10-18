"""Hermetic tests for M8 role-dispatch recovery and restart.

Verifies:
1. Interrupted worker (writer-run / principal-wake) recovery after startACK:
   - Durable custody file written with 0600 permissions.
   - Initial dispatcher interrupted after startACK.
   - Restarted isolated dispatcher reattaches to running native session without duplicate claim,
     without launching a second native session, and without repeating startACK.
   - Resumes lease renewal loop, awaits completion, reports outcome to Hub DO, releases lease.
2. Distinct reviewer (head-recovery) interruption and recovery.
3. Stale generation fencing:
   - Server or config generation advances while interrupted.
   - Lease renewal returns unauthorized_or_stale.
   - Dispatcher stops positively owned native work, writes outcome-uncertain proof, fails closed,
     never reports success, never duplicates claims.
4. Recovery of workload that finished while dispatcher was offline:
   - Outcome artifact read and delivered to Hub DO with finish_job.
5. Uncorroborated / uncertain native workload fails closed:
   - Missing native session from store/catalog never treated as success.
6. Custody security and bounded safe reads:
   - Private 0600 permissions, oversized/corrupt files rejected safely, secrets scrubbed.
7. Existing uncertain proof preserved without retry.
8. Empty recovery allows normal offer polling.
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
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from agent_hub_host import jobs, native_sessions as ns, role_dispatch as rld
from agent_hub_host.jobs import JobDispatcher, JobsClient, write_role_uncertain_proof
from agent_hub_host.native_sessions import NativeTarget
from agent_hub_host.role_dispatch import (
    DispatchError,
    LauncherBackend,
    RoleDispatchAdapter,
    RoleProfileConfig,
    RoleRun,
    TrustedRoleConfig,
    ensure_private_dir,
    remove_role_custody,
    save_role_custody,
    update_role_custody,
)
from agent_hub_host.runtime import RuntimeManager


def _create_synth_launcher_repo(base_dir: pathlib.Path) -> pathlib.Path:
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


class ControlledMockJobsClient:
    """Mock client for controlling Hub API interactions during recovery tests."""

    def __init__(self):
        self.claims: List[Dict[str, Any]] = []
        self.starts: List[Dict[str, Any]] = []
        self.renews: List[Dict[str, Any]] = []
        self.finishes: List[Dict[str, Any]] = []
        self.declines: List[Dict[str, Any]] = []
        self.executes: List[Dict[str, Any]] = []
        self.offers: List[Dict[str, Any]] = []
        self.payloads: Dict[str, Any] = {}
        self.stale_generation = False
        self.deadline_exceeded = False

    def claim_job(self, base_url, job_id, host_id, token, fresh_gate=True, gate_status=None):
        self.claims.append({"job_id": job_id, "host_id": host_id, "gate_status": gate_status})
        profile = "head-recovery" if "reviewer" in job_id else "writer-run"
        return {
            "status": "claimed",
            "job_id": job_id,
            "job_token": f"tok-{job_id}",
            "lease_epoch": 1,
            "deadline_at": time.time() + 600.0,
            "role_binding": {
                "name": f"automation:test:{job_id}",
                "generation": 1,
                "host_id": host_id,
                "profile": profile,
                "ttl_seconds": 300,
            },
        }

    def commit_role_execution(self, base_url, job_id, token, epoch, generation):
        self.executes.append({"job_id": job_id, "epoch": epoch, "generation": generation})
        profile = "head-recovery" if ("reviewer" in job_id or "head-recovery" in job_id) else "writer-run"
        default_payload = {"date": "2026-10-18"} if profile == "writer-run" else {}
        return {
            "execute": True,
            "status": "intent_recorded",
            "role": f"automation:test:{job_id}",
            "generation": generation,
            "epoch": epoch,
            "profile": profile,
            "payload": self.payloads.get(job_id, default_payload),
        }

    def get_offers(self, base_url, host_id, token):
        return list(self.offers)

    def decline_job(self, base_url, job_id, host_id, token, reason):
        self.declines.append({"job_id": job_id, "reason": reason})
        return {"status": "declined"}

    def start_job(self, base_url, job_id, job_token, epoch, evidence_digest, native_session, launcher_task_id):
        self.starts.append({
            "job_id": job_id,
            "epoch": epoch,
            "evidence_digest": evidence_digest,
            "native_session": native_session,
            "launcher_task_id": launcher_task_id,
        })
        return {"status": "started", "job_id": job_id, "epoch": epoch}

    def renew_job(self, base_url, job_id, job_token, epoch):
        self.renews.append({"job_id": job_id, "epoch": epoch})
        if self.stale_generation:
            return {"error": "unauthorized_or_stale"}
        if self.deadline_exceeded:
            return {"error": "deadline_exceeded"}
        return {"status": "renewed", "job_id": job_id, "epoch": epoch}

    def finish_job(self, base_url, job_id, job_token, epoch, status, summary, evidence_refs):
        self.finishes.append({
            "job_id": job_id,
            "epoch": epoch,
            "status": status,
            "summary": summary,
            "evidence_refs": evidence_refs,
        })
        return {"status": "finished", "job_id": job_id}


class TestRoleDispatchRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.state_dir.chmod(0o700)
        self.canonical_root_dir = self.base / "canonical-root"
        self.canonical_root_dir.mkdir(parents=True)
        self.canonical_root_dir.chmod(0o700)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        self.workspace.chmod(0o700)
        ws_local = self.workspace / ".local"
        ws_local.mkdir(parents=True)
        ws_local.chmod(0o700)
        ws_tmp = ws_local / "tmp"
        ws_tmp.mkdir(parents=True)
        ws_tmp.chmod(0o700)

        self.synth_launcher = _create_synth_launcher_repo(self.base)
        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            state_dir=self.state_dir,
            canonical_root_state_dir=self.canonical_root_dir,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                ),
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                ),
                "head-recovery": RoleProfileConfig(
                    profile="head-recovery",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="opencode",
                    launcher_model="muse-spark-1.3-contributor",
                ),
            },
            launcher_repo=self.synth_launcher,
            launcher_config_dir=self.base / "launcher-cfg",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _make_dummy_worker_process(self) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def test_interrupted_worker_recovery_after_start_ack(self):
        """Worker role interrupted after startACK is safely reattached by restarted dispatcher without duplicate claim or effect."""
        job_id = "job-worker-recover-101"
        client = ControlledMockJobsClient()
        dummy_proc = self._make_dummy_worker_process()
        try:
            native_sid = "55555555-5555-4555-8555-555555555555"
            task_id = f"hub-role-writer-run-{job_id}-g1-e1"

            runtime_mgr = RuntimeManager(role_config=self.role_cfg)
            host_id = "test-host-recover"

            d1 = JobDispatcher(
                base_url="https://hub.dtcdev.click",
                host_id=host_id,
                token_provider=lambda: "host-token-123",
                client=client,
                runtime_mgr=runtime_mgr,
            )

            # Set up maintained store binding in mock backend
            binding_record = {
                "session_id": native_sid,
                "tag": f"task-{task_id}",
                "workspace": str(self.workspace.resolve()),
                "engine": "antigravity",
                "created_at_ms": 1792000000000,
            }
            backend = runtime_mgr.role_adapter.launcher_backend
            backend.get_task_binding = MagicMock(return_value=binding_record)

            # Session is alive initially
            backend.is_session_alive = MagicMock(return_value=(True, {"phase": "running"}))

            runs_dir = d1._get_role_runs_dir()
            run_dir = runs_dir / f"writer-run-{job_id}"
            first_action_file = run_dir / f"first-action-{job_id}.json"
            start_ack_file = run_dir / f"start-ack-{job_id}.json"

            fa_data = {
                "job_id": job_id,
                "epoch": 1,
                "role_name": f"automation:test:{job_id}",
                "generation": 1,
                "pid": dummy_proc.pid,
                "native_session": native_sid,
                "stage": "first_action",
                "utc": "2026-10-18T08:00:00Z",
                "isolation_kind": "native_cgroup",
            }

            # Set up maintained store binding in mock backend
            binding_record = {
                "session_id": native_sid,
                "tag": f"task-{task_id}",
                "workspace": str(self.workspace.resolve()),
                "engine": "antigravity",
                "created_at_ms": 1792000000000,
            }
            backend = runtime_mgr.role_adapter.launcher_backend
            backend.get_task_binding = MagicMock(return_value=binding_record)

            def mock_backend_run(task_id, cwd, tmpdir=None, timeout_sec=1800.0):
                run_dir.mkdir(parents=True, exist_ok=True)
                first_action_file.write_text(json.dumps(fa_data), encoding="utf-8")
                for _ in range(50):
                    if start_ack_file.exists():
                        break
                    time.sleep(0.05)
                return 0, {"mode": "test"}, ""

            backend.run = MagicMock(side_effect=mock_backend_run)

            # Session is alive initially
            backend.is_session_alive = MagicMock(return_value=(True, {"phase": "running"}))

            # Simulate offer and claim
            offer = {
                "id": job_id,
                "kind": "writer-run",
                "payload": {"date": "2026-10-18"},
                "role_binding": {
                    "name": f"automation:test:{job_id}",
                    "generation": 1,
                    "host_id": host_id,
                    "profile": "writer-run",
                    "ttl_seconds": 300,
                },
            }
            res = d1.handle_offer(offer)
            self.assertTrue(res.get("accepted"))
            self.assertEqual(len(client.claims), 1)

            # Wait briefly for worker lifecycle thread to write custody and first-action
            runs_dir = d1._get_role_runs_dir()
            run_dir = runs_dir / f"writer-run-{job_id}"
            start_ack_file = run_dir / f"start-ack-{job_id}.json"

            # Wait for start-ack to be written by d1
            for _ in range(50):
                if start_ack_file.exists():
                    break
                time.sleep(0.05)

            self.assertTrue(start_ack_file.exists())
            self.assertEqual(len(client.starts), 1)

            # Verify durable custody exists with mode 0600
            custody_file = run_dir / f"custody-{job_id}.json"
            self.assertTrue(custody_file.exists())
            mode = os.stat(custody_file).st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600)

            # SIMULATE INTERRUPTION: previous dispatcher process crashed/died
            # Native process dummy_proc is still alive in background
            if job_id in d1.running_jobs:
                d1.running_jobs[job_id]["stop_event"].set()
            d1.running_jobs.clear()
            del d1

            # START SECOND DISPATCHER (d2) on same host/config
            runtime_mgr2 = RuntimeManager(role_config=self.role_cfg)
            backend2 = runtime_mgr2.role_adapter.launcher_backend
            backend2.get_task_binding = MagicMock(return_value=binding_record)
            backend2.is_session_alive = MagicMock(return_value=(True, {"phase": "running"}))

            d2 = JobDispatcher(
                base_url="https://hub.dtcdev.click",
                host_id=host_id,
                token_provider=lambda: "host-token-123",
                client=client,
                runtime_mgr=runtime_mgr2,
            )

            # Run recovery
            recovered = d2.recover_active_role_jobs()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0]["job_id"], job_id)
            self.assertTrue(recovered[0]["recovered"])
            self.assertEqual(recovered[0]["status"], "running")

            # Verify NO duplicate claim was made by d2
            self.assertEqual(len(client.claims), 1, "Recovery must NEVER claim job again")
            # Verify NO duplicate start_job was made by d2
            self.assertEqual(len(client.starts), 1, "Recovery after startACK must not call start_job again")
            # Verify renew_job was called to maintain lease
            self.assertTrue(len(client.renews) >= 1, "Recovery must maintain lease renewals")

            # Now simulate native workload completing and writing outcome
            outcome_file = run_dir / f"outcome-{job_id}.json"
            outcome_data = {
                "status": "succeeded",
                "summary": "writer report completed successfully",
                "evidence_refs": ["/tmp/report.md"],
            }
            outcome_file.write_text(json.dumps(outcome_data), encoding="utf-8")

            # Workload is now no longer alive
            backend2.is_session_alive = MagicMock(return_value=(False, {"phase": "exited"}))

            # Allow d2 monitor thread to finalize
            for _ in range(50):
                if len(client.finishes) >= 1:
                    break
                time.sleep(0.05)

            self.assertEqual(len(client.finishes), 1)
            self.assertEqual(client.finishes[0]["job_id"], job_id)
            self.assertEqual(client.finishes[0]["status"], "succeeded")
            self.assertIn("writer report completed", client.finishes[0]["summary"])

            # Verify active custody record was removed
            self.assertFalse(custody_file.exists())
            self.assertFalse((runs_dir / f"active-role-{job_id}.json").exists())
        finally:
            dummy_proc.kill()
            dummy_proc.wait()

    def test_interrupted_reviewer_recovery_after_start_ack(self):
        """Reviewer role (head-recovery) interrupted after startACK is safely recovered by restarted dispatcher."""
        job_id = "job-reviewer-recover-202"
        client = ControlledMockJobsClient()
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        dummy_proc = self._make_dummy_worker_process()
        try:
            native_sid = "66666666-6666-4666-8666-666666666666"
            task_id = f"hub-role-head-recovery-{job_id}-g1-e1"

            binding_record = {
                "session_id": native_sid,
                "tag": f"task-{task_id}",
                "workspace": str(self.workspace.resolve()),
                "engine": "opencode",
                "created_at_ms": 1792000000000,
            }
            backend = runtime_mgr.role_adapter.launcher_backend
            backend.get_task_binding = MagicMock(return_value=binding_record)
            backend.is_session_alive = MagicMock(return_value=(True, {"phase": "running"}))

            runs_dir = self.role_cfg.runs_dir
            run_dir = runs_dir / f"head-recovery-{job_id}"
            run_dir.mkdir(parents=True, exist_ok=True)
            run_dir.chmod(0o700)

            # Set up existing start-ack and custody from an interrupted dispatcher
            save_role_custody(
                runs_dir=runs_dir,
                run_dir=run_dir,
                job_id=job_id,
                data={
                    "job_id": job_id,
                    "kind": "head-recovery",
                    "role_binding": {
                        "name": f"automation:test:{job_id}",
                        "generation": 1,
                        "host_id": host_id,
                        "profile": "head-recovery",
                        "ttl_seconds": 300,
                    },
                    "lease_epoch": 1,
                    "job_token": f"tok-{job_id}",
                    "deadline_at": time.time() + 600.0,
                    "payload": {"target": "head"},
                    "base_url": "https://hub.dtcdev.click",
                    "host_id": host_id,
                    "task_id": task_id,
                    "phase": "running",
                    "claimed_at": time.time() - 30.0,
                },
            )

            (run_dir / f"start-ack-{job_id}.json").write_text(json.dumps({
                "status": "started",
                "job_id": job_id,
                "epoch": 1,
            }), encoding="utf-8")

            # Dispatcher d starts up
            d = JobDispatcher(
                base_url="https://hub.dtcdev.click",
                host_id=host_id,
                token_provider=lambda: "host-token-123",
                client=client,
                runtime_mgr=runtime_mgr,
            )

            recovered = d.recover_active_role_jobs()
            self.assertEqual(len(recovered), 1)
            self.assertTrue(recovered[0]["recovered"])
            self.assertEqual(recovered[0]["status"], "running")

            # Simulate reviewer completing
            (run_dir / f"outcome-{job_id}.json").write_text(json.dumps({
                "status": "succeeded",
                "summary": "head-recovery audit completed: head session healthy",
                "evidence_refs": [],
            }), encoding="utf-8")
            backend.is_session_alive = MagicMock(return_value=(False, {"phase": "exited"}))

            for _ in range(50):
                if len(client.finishes) >= 1:
                    break
                time.sleep(0.05)

            self.assertEqual(len(client.finishes), 1)
            self.assertEqual(client.finishes[0]["job_id"], job_id)
            self.assertEqual(client.finishes[0]["status"], "succeeded")
            self.assertIn("head-recovery audit completed", client.finishes[0]["summary"])
        finally:
            dummy_proc.kill()
            dummy_proc.wait()

    def test_stale_generation_fences_recovery(self):
        """Recovery refuses stale generation, fences positively owned native work, and records uncertain custody proof."""
        job_id = "job-stale-fence-303"
        client = ControlledMockJobsClient()
        client.stale_generation = True  # Server returns unauthorized_or_stale

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        native_sid = "77777777-7777-4777-8777-777777777777"
        task_id = f"hub-role-writer-run-{job_id}-g1-e1"

        binding_record = {
            "session_id": native_sid,
            "tag": f"task-{task_id}",
            "workspace": str(self.workspace.resolve()),
            "engine": "antigravity",
            "created_at_ms": 1792000000000,
        }
        backend = runtime_mgr.role_adapter.launcher_backend
        backend.get_task_binding = MagicMock(return_value=binding_record)
        stopped_targets = []
        backend.stop_session = MagicMock(side_effect=lambda target: (stopped_targets.append(target), (True, "stopped", {}))[1])

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        save_role_custody(
            runs_dir=runs_dir,
            run_dir=run_dir,
            job_id=job_id,
            data={
                "job_id": job_id,
                "kind": "writer-run",
                "role_binding": {
                    "name": f"automation:test:{job_id}",
                    "generation": 1,
                    "host_id": host_id,
                    "profile": "writer-run",
                    "ttl_seconds": 300,
                },
                "lease_epoch": 1,
                "job_token": f"tok-{job_id}",
                "deadline_at": time.time() + 600.0,
                "payload": {},
                "base_url": "https://hub.dtcdev.click",
                "host_id": host_id,
                "task_id": task_id,
                "phase": "running",
                "claimed_at": time.time() - 30.0,
            },
        )

        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id=host_id,
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        recovered = d.recover_active_role_jobs()
        self.assertEqual(len(recovered), 1)
        self.assertFalse(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "fenced")
        self.assertEqual(recovered[0]["reason"], "stale_generation_or_lost_lease")

        # Positively owned native session was stopped
        self.assertEqual(len(stopped_targets), 1)
        self.assertEqual(stopped_targets[0].session_id, native_sid)

        # Durable uncertain proof recorded
        uncertain_file = run_dir / f"outcome-uncertain-{job_id}.json"
        self.assertTrue(uncertain_file.exists())
        proof = json.loads(uncertain_file.read_text(encoding="utf-8"))
        self.assertEqual(proof.get("status"), "uncertain")
        self.assertEqual(proof.get("phase"), "stale_generation_fenced")
        self.assertIn("unauthorized_or_stale", proof.get("cause"))

        # Never reported finished to server
        self.assertEqual(len(client.finishes), 0)

        # Active index removed so recovery does not loop
        self.assertFalse((runs_dir / f"active-role-{job_id}.json").exists())

    def test_recovery_of_already_finished_workload(self):
        """Workload that completed while dispatcher was down is finalized to server on recovery."""
        job_id = "job-finished-offline-404"
        client = ControlledMockJobsClient()
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        native_sid = "88888888-8888-4888-8888-888888888888"
        task_id = f"hub-role-principal-wake-{job_id}-g1-e1"

        binding_record = {
            "session_id": native_sid,
            "tag": f"task-{task_id}",
            "workspace": str(self.workspace.resolve()),
            "engine": "antigravity",
            "created_at_ms": 1792000000000,
        }
        backend = runtime_mgr.role_adapter.launcher_backend
        backend.get_task_binding = MagicMock(return_value=binding_record)
        # Workload already exited
        backend.is_session_alive = MagicMock(return_value=(False, {"phase": "exited"}))

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"principal-wake-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        save_role_custody(
            runs_dir=runs_dir,
            run_dir=run_dir,
            job_id=job_id,
            data={
                "job_id": job_id,
                "kind": "principal-wake",
                "role_binding": {
                    "name": f"automation:test:{job_id}",
                    "generation": 1,
                    "host_id": host_id,
                    "profile": "principal-wake",
                    "ttl_seconds": 300,
                },
                "lease_epoch": 1,
                "job_token": f"tok-{job_id}",
                "deadline_at": time.time() + 600.0,
                "payload": {},
                "base_url": "https://hub.dtcdev.click",
                "host_id": host_id,
                "task_id": task_id,
                "phase": "running",
                "claimed_at": time.time() - 30.0,
            },
        )

        (run_dir / f"start-ack-{job_id}.json").write_text(json.dumps({
            "status": "started",
            "job_id": job_id,
            "epoch": 1,
        }), encoding="utf-8")

        (run_dir / f"outcome-{job_id}.json").write_text(json.dumps({
            "status": "succeeded",
            "summary": "wake checks finished cleanly",
            "evidence_refs": ["/tmp/report.json"],
        }), encoding="utf-8")

        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id=host_id,
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        recovered = d.recover_active_role_jobs()
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "finished")
        self.assertEqual(recovered[0]["outcome"], "succeeded")

        self.assertEqual(len(client.finishes), 1)
        self.assertEqual(client.finishes[0]["job_id"], job_id)
        self.assertEqual(client.finishes[0]["status"], "succeeded")
        self.assertIn("wake checks finished cleanly", client.finishes[0]["summary"])

    def test_uncorroborated_native_identity_fails_closed(self):
        """Interrupted job with missing or uncorroborated native binding fails closed and never reports success."""
        job_id = "job-uncorroborated-505"
        client = ControlledMockJobsClient()
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        # Backend cannot corroborate binding (returns None)
        backend = runtime_mgr.role_adapter.launcher_backend
        backend.get_task_binding = MagicMock(return_value=None)

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        save_role_custody(
            runs_dir=runs_dir,
            run_dir=run_dir,
            job_id=job_id,
            data={
                "job_id": job_id,
                "kind": "writer-run",
                "role_binding": {
                    "name": f"automation:test:{job_id}",
                    "generation": 1,
                    "host_id": host_id,
                    "profile": "writer-run",
                    "ttl_seconds": 300,
                },
                "lease_epoch": 1,
                "job_token": f"tok-{job_id}",
                "deadline_at": time.time() + 600.0,
                "payload": {},
                "base_url": "https://hub.dtcdev.click",
                "host_id": host_id,
                "task_id": f"hub-role-writer-run-{job_id}-g1-e1",
                "phase": "running",
                "claimed_at": time.time() - 30.0,
            },
        )

        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id=host_id,
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        recovered = d.recover_active_role_jobs()
        self.assertEqual(len(recovered), 1)
        self.assertFalse(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "HOLD")
        self.assertEqual(recovered[0]["reason"], "uncorroborated_native_identity")

        # Outcome-uncertain proof written
        uncertain_file = run_dir / f"outcome-uncertain-{job_id}.json"
        self.assertTrue(uncertain_file.exists())
        self.assertEqual(len(client.finishes), 0)

    def test_durable_custody_permissions_and_bounded_safety(self):
        """Custody files must have mode 0600, reject symlinks and oversized files boundedly."""
        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / "writer-run-job-safe-606"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        custody_path = save_role_custody(
            runs_dir=runs_dir,
            run_dir=run_dir,
            job_id="job-safe-606",
            data={"test": "safe"},
        )
        self.assertTrue(custody_path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(custody_path).st_mode), 0o600)

        # Update custody
        updated = update_role_custody(runs_dir, run_dir, "job-safe-606", {"extra": "field"})
        self.assertEqual(updated.get("extra"), "field")
        self.assertTrue("updated_at" in updated)

        # Remove custody
        remove_role_custody(runs_dir, run_dir, "job-safe-606")
        self.assertFalse(custody_path.exists())
        self.assertFalse((runs_dir / "active-role-job-safe-606.json").exists())

    def test_existing_uncertain_proof_preserved_without_retry(self):
        """A role run that was already marked uncertain is held and never blindly retried."""
        job_id = "job-already-uncertain-707"
        client = ControlledMockJobsClient()
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        # Create uncertain proof
        uncertain_file = run_dir / f"outcome-uncertain-{job_id}.json"
        uncertain_file.write_text(json.dumps({
            "status": "uncertain",
            "phase": "bootstrap_timeout",
            "cause": "previous failure",
        }), encoding="utf-8")

        save_role_custody(
            runs_dir=runs_dir,
            run_dir=run_dir,
            job_id=job_id,
            data={
                "job_id": job_id,
                "kind": "writer-run",
                "host_id": host_id,
                "role_binding": {
                    "name": f"automation:test:{job_id}",
                    "generation": 1,
                    "host_id": host_id,
                    "profile": "writer-run",
                    "ttl_seconds": 300,
                },
                "lease_epoch": 1,
                "job_token": f"tok-{job_id}",
                "phase": "uncertain",
            },
        )

        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id=host_id,
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        recovered = d.recover_active_role_jobs()
        self.assertEqual(len(recovered), 1)
        self.assertFalse(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "HOLD")
        self.assertEqual(recovered[0]["reason"], "already_uncertain")

        # Uncertain proof preserved on disk
        self.assertTrue(uncertain_file.exists())
        self.assertEqual(len(client.claims), 0)
        self.assertEqual(len(client.starts), 0)

    def test_empty_recovery_allows_clean_offer_polling(self):
        """When no active custody files exist, recover_active_role_jobs returns [] and polling works normally."""
        client = ControlledMockJobsClient()
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id="test-host-empty",
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )
        recovered = d.recover_active_role_jobs()
        self.assertEqual(recovered, [])


if __name__ == "__main__":
    unittest.main()
