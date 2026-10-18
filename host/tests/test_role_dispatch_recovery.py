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

import io
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
import urllib.error
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
        self.reconciliations: List[Dict[str, Any]] = []
        self.payloads: Dict[str, Any] = {}
        self.job_statuses: Dict[str, Any] = {}
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
            return {"error": "unauthorized", "status_code": 401}
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

    def get_job_status(self, base_url, job_id, token):
        if job_id in self.job_statuses:
            return self.job_statuses[job_id]
        if self.stale_generation:
            return {
                "job": {
                    "id": job_id,
                    "status": "fenced",
                    "lease_epoch": 1,
                    "holder_host_id": "test-host-recover",
                    "kind": "writer-run",
                    "outcome_summary": "fenced: superseded by takeover",
                }
            }
        if self.deadline_exceeded:
            return {
                "job": {
                    "id": job_id,
                    "status": "expired",
                    "lease_epoch": 1,
                    "holder_host_id": "test-host-recover",
                    "kind": "writer-run",
                    "outcome_summary": "lease expired",
                }
            }
        for f in self.finishes:
            if f["job_id"] == job_id:
                return {
                    "job": {
                        "id": job_id,
                        "status": f["status"],
                        "lease_epoch": f["epoch"],
                        "outcome_summary": f["summary"],
                    }
                }
        return {"error": "not_found"}

    def reconcile_terminal_job(self, base_url, job_id, token, data):
        self.reconciliations.append({
            "job_id": job_id,
            "token": token,
            "data": data,
        })
        if getattr(self, "reconcile_override", None):
            return self.reconcile_override(job_id, token, data)
        if self.stale_generation:
            return {"error": "generation_mismatch", "status_code": 409}
        epoch = data.get("epoch", 1)
        generation = data.get("generation", 1)
        terminal_case = data.get("terminal_case", "unstarted")
        return {
            "reconciled": True,
            "status": "reconciled",
            "job_id": job_id,
            "epoch": epoch,
            "generation": generation,
            "outcome": "failed",
            "terminal_case": terminal_case,
            "receipt": {
                "epoch": epoch,
                "generation": generation,
                "job_id": job_id,
                "outcome": "failed",
                "reconciled": True,
                "status": "failed",
                "summary": data.get("summary", "terminal reconciliation"),
                "evidenceRefs": [],
                "original_status": self.job_statuses.get(job_id, {}).get("job", {}).get("status"),
                "terminal_case": terminal_case,
                **({"native_session": data["native_session"], "native_retirement": self.retirement_marker} if terminal_case == "executed" else {}),
            }
        }



class TestRoleDispatchRecovery(unittest.TestCase):
    def setUp(self):
        tmp_parent = pathlib.Path(".local/tmp").resolve()
        tmp_parent.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(tmp_parent))
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
        dummy_proc = None
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

            cgroup_path = "/test.scope"
            # Set up maintained store binding in mock backend
            binding_record = {
                "session_id": native_sid,
                "tag": f"task-{task_id}",
                "workspace": str(self.workspace.resolve()),
                "engine": "antigravity",
                "created_at_ms": int(time.time() * 1000) - 2000,
                "workload_pid": None,
                "worker_pid": 999998,
                "workload_cgroup": cgroup_path,
                "containment_empty": False,
                "phase": "running",
            }
            backend = runtime_mgr.role_adapter.launcher_backend
            backend.is_fixture_only = True
            backend.get_task_binding = MagicMock(return_value=binding_record)

            runs_dir = d1._get_role_runs_dir()
            run_dir = runs_dir / f"writer-run-{job_id}"
            first_action_file = run_dir / f"first-action-{job_id}.json"
            start_ack_file = run_dir / f"start-ack-{job_id}.json"

            fa_data = {
                "job_id": job_id,
                "epoch": 1,
                "role_name": f"automation:test:{job_id}",
                "generation": 1,
                "pid": None,
                "native_session": native_sid,
                "stage": "first_action",
                "utc": "2026-10-18T08:00:00Z",
                "isolation_kind": "native_cgroup",
            }

            worker_interrupt_event = threading.Event()

            def mock_backend_run(task_id, cwd, tmpdir=None, timeout_sec=1800.0):
                nonlocal dummy_proc
                # Start the fixture workload at the actual execution boundary so
                # strict PID provenance is independent of setup duration.
                dummy_proc = self._make_dummy_worker_process()
                groups = rld.read_proc_cgroups(dummy_proc.pid)
                binding_record.update(workload_pid=dummy_proc.pid,
                                      workload_cgroup=groups[0][2] if groups else "/test.scope")
                fa_data["pid"] = dummy_proc.pid
                run_dir.mkdir(parents=True, exist_ok=True)
                first_action_file.write_text(json.dumps(fa_data), encoding="utf-8")
                # Block until dispatcher d1 is interrupted/stopped
                worker_interrupt_event.wait(timeout=10.0)
                return 0, {"mode": "test"}, ""

            backend.run = MagicMock(side_effect=mock_backend_run)
            backend.stop_session = MagicMock(return_value=(True, "stopped", {}))

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
            # 1. Signal stop_event to d1 running job so d1 knows it was stopped externally
            if job_id in d1.running_jobs:
                d1.running_jobs[job_id]["stop_event"].set()
            # 2. Release the mock backend runner so exec_thread completes
            worker_interrupt_event.set()
            # 3. Deterministically wait for all d1 worker/renewal/runner threads to terminate
            for t in threading.enumerate():
                if t.name in (f"job-runner-{job_id}", f"renew-{job_id}", f"role-worker-{job_id}"):
                    t.join(timeout=5.0)
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

            # Allow d2 monitor thread to finalize and join it deterministically before custody checks
            recovery_thread = None
            for _ in range(100):
                if recovery_thread is None:
                    for t in threading.enumerate():
                        if t.name == f"job-recovery-{job_id}":
                            recovery_thread = t
                            break
                if len(client.finishes) >= 1:
                    break
                time.sleep(0.05)

            if recovery_thread is not None and recovery_thread.is_alive():
                recovery_thread.join(timeout=5.0)

            self.assertEqual(len(client.finishes), 1)
            self.assertEqual(client.finishes[0]["job_id"], job_id)
            self.assertEqual(client.finishes[0]["status"], "succeeded")
            self.assertIn("writer report completed", client.finishes[0]["summary"])

            # Verify active custody record was removed
            self.assertFalse(custody_file.exists())
            self.assertFalse((runs_dir / f"active-role-{job_id}.json").exists())
        finally:
            if dummy_proc is not None:
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

        # Durable uncertain proof recorded and custody proof retained on disk
        uncertain_file = run_dir / f"outcome-uncertain-{job_id}.json"
        custody_file = run_dir / f"custody-{job_id}.json"
        self.assertTrue(uncertain_file.exists())
        self.assertTrue(custody_file.exists(), "Custody proof MUST be preserved on disk upon stale fence")
        proof = json.loads(uncertain_file.read_text(encoding="utf-8"))
        self.assertEqual(proof.get("status"), "uncertain")
        self.assertEqual(proof.get("phase"), "stale_generation_fenced")
        self.assertTrue("unauthorized" in proof.get("cause") or "fenced" in proof.get("cause"))

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

        # Outcome-uncertain proof written and CUSTODY RETAINED
        uncertain_file = run_dir / f"outcome-uncertain-{job_id}.json"
        custody_file = run_dir / f"custody-{job_id}.json"
        self.assertTrue(uncertain_file.exists())
        self.assertTrue(custody_file.exists(), "Custody proof MUST be preserved on disk upon uncorroborated native identity")
        self.assertEqual(len(client.finishes), 0)

    def test_recovery_finish_refusal_retains_custody_and_holds(self):
        """When Hub finish_job refuses completion, recovery does NOT drop credentials/custody and returns HOLD."""
        job_id = "job-finish-refused-405"
        client = ControlledMockJobsClient()
        # Mock finish_job returning refusal error
        client.finish_job = MagicMock(return_value={"error": "synthetic_finish_refused"})
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"principal-wake-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        task_id = f"hub-role-principal-wake-{job_id}-g1-e1"
        binding_record = {
            "session_id": "88888888-8888-4888-8888-888888888888",
            "tag": f"task-{task_id}",
            "workspace": str(self.workspace.resolve()),
            "engine": "antigravity",
            "created_at_ms": 1792000000000,
        }
        backend = runtime_mgr.role_adapter.launcher_backend
        backend.get_task_binding = MagicMock(return_value=binding_record)
        backend.is_session_alive = MagicMock(return_value=(False, {"phase": "exited"}))

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
            "summary": "wake checks finished",
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
        self.assertFalse(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "HOLD")
        self.assertIn("finish_refused", recovered[0]["reason"])

        # CUSTODY MUST NOT BE DROPPED!
        custody_file = run_dir / f"custody-{job_id}.json"
        self.assertTrue(custody_file.exists(), "Custody MUST be preserved on finish refusal")
        custody_data = json.loads(custody_file.read_text(encoding="utf-8"))
        self.assertEqual(custody_data.get("phase"), "finish_refused")
        self.assertEqual(custody_data.get("finish_error"), "synthetic_finish_refused")
        # Credentials preserved
        self.assertEqual(custody_data.get("job_token"), f"tok-{job_id}")

    def test_recovery_finish_network_error_retains_custody_and_holds(self):
        """When Hub finish_job raises connection error / lost reply, recovery retains custody and holds."""
        job_id = "job-finish-neterr-406"
        client = ControlledMockJobsClient()
        client.finish_job = MagicMock(side_effect=ConnectionResetError("lost connection during finish reply"))
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"principal-wake-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        task_id = f"hub-role-principal-wake-{job_id}-g1-e1"
        binding_record = {
            "session_id": "99999999-9999-4999-8999-999999999999",
            "tag": f"task-{task_id}",
            "workspace": str(self.workspace.resolve()),
            "engine": "antigravity",
            "created_at_ms": 1792000000000,
        }
        backend = runtime_mgr.role_adapter.launcher_backend
        backend.get_task_binding = MagicMock(return_value=binding_record)
        backend.is_session_alive = MagicMock(return_value=(False, {"phase": "exited"}))

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
            "summary": "wake checks finished",
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
        self.assertFalse(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "HOLD")
        self.assertIn("finish_error", recovered[0]["reason"])

        # CUSTODY MUST NOT BE DROPPED!
        custody_file = run_dir / f"custody-{job_id}.json"
        self.assertTrue(custody_file.exists(), "Custody MUST be preserved on finish network error")
        custody_data = json.loads(custody_file.read_text(encoding="utf-8"))
        self.assertEqual(custody_data.get("phase"), "finish_error")
        self.assertIn("lost connection", custody_data.get("finish_error"))
        self.assertEqual(custody_data.get("job_token"), f"tok-{job_id}")

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

    def test_recovery_lost_finish_reply_reconciled_via_server_status(self):
        """When Hub finish_job network reply is lost, but Hub recorded finished, recovery reconciles status."""
        job_id = "job-finish-reconcile-808"
        client = ControlledMockJobsClient()
        # Mock finish_job throwing connection error
        client.finish_job = MagicMock(side_effect=ConnectionResetError("lost connection"))
        # But get_job_status reports that the server actually received and processed the finish!
        client.get_job_status = MagicMock(return_value={
            "job": {
                "id": job_id,
                "status": "succeeded",
                "lease_epoch": 1,
                "native_session": "88888888-8888-4888-8888-888888888888",
            }
        })
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"principal-wake-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        task_id = f"hub-role-principal-wake-{job_id}-g1-e1"
        binding_record = {
            "session_id": "88888888-8888-4888-8888-888888888888",
            "tag": f"task-{task_id}",
            "workspace": str(self.workspace.resolve()),
            "engine": "antigravity",
            "created_at_ms": 1792000000000,
        }
        backend = runtime_mgr.role_adapter.launcher_backend
        backend.get_task_binding = MagicMock(return_value=binding_record)
        backend.is_session_alive = MagicMock(return_value=(False, {"phase": "exited"}))

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
            "summary": "wake checks finished",
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

        # Custody must be cleanly deleted because finish was semantically reconciled
        custody_file = run_dir / f"custody-{job_id}.json"
        self.assertFalse(custody_file.exists())
        self.assertFalse((runs_dir / f"active-role-{job_id}.json").exists())

    def test_recovery_stale_renew_reconciled_when_already_finished_on_server(self):
        """When renew_job returns unauthorized_or_stale because server finished and released lease, status reconciles."""
        job_id = "job-stale-reconcile-909"
        client = ControlledMockJobsClient()
        client.stale_generation = True
        # Server reports job is already finished!
        client.get_job_status = MagicMock(return_value={
            "job": {
                "id": job_id,
                "status": "finished",
                "lease_epoch": 1,
                "holder_host_id": "test-host-recover",
                "kind": "writer-run",
            }
        })
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        task_id = f"hub-role-writer-run-{job_id}-g1-e1"
        binding_record = {
            "session_id": "77777777-7777-4777-8777-777777777777",
            "tag": f"task-{task_id}",
            "workspace": str(self.workspace.resolve()),
            "engine": "antigravity",
            "created_at_ms": 1792000000000,
        }
        backend = runtime_mgr.role_adapter.launcher_backend
        backend.get_task_binding = MagicMock(return_value=binding_record)

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
        self.assertTrue(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "finished")
        self.assertTrue(recovered[0].get("reconciled"))

        # Custody must be cleanly deleted because finish was semantically reconciled
        custody_file = run_dir / f"custody-{job_id}.json"
        self.assertFalse(custody_file.exists())
        self.assertFalse((runs_dir / f"active-role-{job_id}.json").exists())

    def test_incomplete_authoritative_binding_preserves_custody(self):
        """A terminal label without the real status DTO binding cannot stop work."""
        job_id = "job-incomplete-authority"
        host_id = "test-host-recover"
        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)
        role = {"name": f"automation:test:{job_id}", "generation": 1,
                "host_id": host_id, "profile": "writer-run", "ttl_seconds": 300}
        save_role_custody(runs_dir=runs_dir, run_dir=run_dir, job_id=job_id, data={
            "job_id": job_id, "kind": "writer-run", "role_binding": role,
            "lease_epoch": 1, "job_token": "synthetic-job-token",
            "deadline_at": time.time() + 600, "payload": {}, "host_id": host_id,
            "phase": "running",
        })
        custody = run_dir / f"custody-{job_id}.json"
        client = ControlledMockJobsClient()
        client.renew_job = MagicMock(return_value={"error": "unauthorized", "status_code": 401})
        d = JobDispatcher("https://hub.dtcdev.click", host_id,
                          token_provider=lambda: "synthetic-host-token", client=client,
                          runtime_mgr=RuntimeManager(role_config=self.role_cfg))
        complete = {"id": job_id, "kind": "writer-run", "holder_host_id": host_id,
                    "lease_epoch": 1, "status": "fenced"}
        for field, value in (("kind", None), ("holder_host_id", None),
                             ("lease_epoch", None), ("lease_epoch", True), ("lease_epoch", 0),
                             ("status", "unknown")):
            with self.subTest(field=field, value=value):
                client.get_job_status = MagicMock(return_value={"job": {**complete, field: value}})
                client.renew_job = MagicMock(return_value={
                    "error": "deadline_exceeded" if field == "status" else "unauthorized",
                    "status_code": 401,
                })
                with patch.object(d, "_corroborate_native_target") as corroborate, \
                     patch.object(d, "_stop_native_target_safely") as stop:
                    result = d._recover_one_role_job(custody, runs_dir)
                self.assertEqual(result["status"], "HOLD")
                corroborate.assert_not_called()
                stop.assert_not_called()
                self.assertTrue(custody.exists())
                self.assertTrue((runs_dir / f"active-role-{job_id}.json").exists())

    def test_actual_jobs_client_http401_normalization_fences_recovery(self):
        """Actual JobsClient normalizes HTTP401 to unauthorized dict; recovery queries authoritative status and fences work."""
        job_id = "job-actual-401-fence-1"
        client = JobsClient(timeout=5.0)

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

        def mock_safe_http_json(req, timeout=None):
            url = req.full_url
            if "renew" in url:
                # Exercise actual JobsClient._request normalization when safe_http_json raises HTTPError 401
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b'{"error": "unauthorized"}'))
            if "status" in url:
                return {
                    "job": {
                        "id": job_id,
                        "status": "fenced",
                        "lease_epoch": 1,
                        "holder_host_id": host_id,
                        "kind": "writer-run",
                        "outcome_summary": "fenced: superseded by takeover",
                    }
                }
            return {}

        with patch("agent_hub_host.jobs.api.safe_http_json", side_effect=mock_safe_http_json):
            # Verify actual JobsClient returns normalized 401 response
            norm_res = client.renew_job("https://hub.dtcdev.click", job_id, f"tok-{job_id}", 1)
            self.assertEqual(norm_res, {"error": "unauthorized", "status_code": 401})

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

            # Corroborated exact owned native target was stopped
            self.assertEqual(len(stopped_targets), 1)
            self.assertEqual(stopped_targets[0].session_id, native_sid)

            # Custody proof preserved on disk
            self.assertTrue((run_dir / f"custody-{job_id}.json").exists())
            uncertain_file = run_dir / f"outcome-uncertain-{job_id}.json"
            self.assertTrue(uncertain_file.exists())
            proof = json.loads(uncertain_file.read_text(encoding="utf-8"))
            self.assertEqual(proof.get("status"), "uncertain")
            self.assertEqual(proof.get("phase"), "stale_generation_fenced")

    def test_actual_jobs_client_http409_normalization_fences_recovery(self):
        """Actual JobsClient normalizes HTTP409 to conflict dict; recovery queries authoritative status and fences work."""
        job_id = "job-actual-409-fence-2"
        client = JobsClient(timeout=5.0)

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"
        native_sid = "77777777-7777-4777-8777-777777777778"
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

        def mock_safe_http_json_409(req, timeout=None):
            url = req.full_url
            if "renew" in url:
                raise urllib.error.HTTPError(url, 409, "Conflict", {}, io.BytesIO(b'{"error": "conflict"}'))
            if "status" in url:
                return {
                    "job": {
                        "id": job_id,
                        "status": "fenced",
                        "lease_epoch": 1,
                        "holder_host_id": host_id,
                        "kind": "writer-run",
                        "outcome_summary": "fenced: superseded by takeover",
                    }
                }
            return {}

        with patch("agent_hub_host.jobs.api.safe_http_json", side_effect=mock_safe_http_json_409):
            norm_res = client.renew_job("https://hub.dtcdev.click", job_id, f"tok-{job_id}", 1)
            self.assertEqual(norm_res, {"error": "conflict", "status_code": 409})

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
            self.assertEqual(len(stopped_targets), 1)
            self.assertTrue((run_dir / f"custody-{job_id}.json").exists())

    def test_network_status_failure_negative_retains_custody_no_stop(self):
        """When renewal fails with 401 but authoritative status is unreachable/offline, recovery returns HOLD with no stop and custody retained."""
        job_id = "job-neterr-status-3"
        client = ControlledMockJobsClient()
        client.stale_generation = True  # renew returns 401
        # Server status lookup raises network failure!
        client.get_job_status = MagicMock(side_effect=urllib.error.URLError("connection refused"))

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"
        native_sid = "77777777-7777-4777-8777-777777777779"
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
        self.assertEqual(recovered[0]["status"], "HOLD")
        self.assertEqual(recovered[0]["reason"], "authoritative_status_unavailable")

        # Native process MUST NOT be stopped when status is unavailable!
        self.assertEqual(len(stopped_targets), 0)

        # Custody proof MUST be retained on disk
        self.assertTrue((run_dir / f"custody-{job_id}.json").exists())
        self.assertTrue((runs_dir / f"active-role-{job_id}.json").exists())

        # No uncertain proof written so recovery can retry cleanly
        self.assertFalse((run_dir / f"outcome-uncertain-{job_id}.json").exists())

    def test_authoritative_status_still_running_negative_retains_custody_no_stop(self):
        """When renewal returns 401 but authoritative server status confirms job is still running, HOLD with no stop and no duplicate launch."""
        job_id = "job-still-running-4"
        client = ControlledMockJobsClient()
        client.stale_generation = True  # renew returns 401
        # Authoritative status says job is actually still running on server!
        client.get_job_status = MagicMock(return_value={
            "job": {
                "id": job_id,
                "status": "running",
                "lease_epoch": 1,
                "holder_host_id": "test-host-recover",
                "kind": "writer-run",
            }
        })

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"
        native_sid = "77777777-7777-4777-8777-777777777780"
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
        self.assertEqual(recovered[0]["status"], "HOLD")
        self.assertEqual(recovered[0]["reason"], "job_still_running")

        # Native process MUST NOT be stopped!
        self.assertEqual(len(stopped_targets), 0)

        # Custody proof retained on disk
        self.assertTrue((run_dir / f"custody-{job_id}.json").exists())
        self.assertTrue((runs_dir / f"active-role-{job_id}.json").exists())

        # No duplicate claim or start issued
        self.assertEqual(len(client.claims), 0)
        self.assertEqual(len(client.starts), 0)

    def test_already_finished_reconciles_failed_outcome_without_stale_success(self):
        """When renew returns 401 but server status is failed, recovery reconciles outcome as failed without reporting success."""
        job_id = "job-server-failed-reconcile-5"
        client = ControlledMockJobsClient()
        client.stale_generation = True
        client.get_job_status = MagicMock(return_value={
            "job": {
                "id": job_id,
                "status": "failed",
                "lease_epoch": 1,
                "holder_host_id": "test-host-recover",
                "kind": "writer-run",
            }
        })

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"
        task_id = f"hub-role-writer-run-{job_id}-g1-e1"

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
        self.assertTrue(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "finished")
        self.assertEqual(recovered[0]["outcome"], "failed")
        self.assertTrue(recovered[0].get("reconciled"))

        # Custody cleanly removed
        self.assertFalse((run_dir / f"custody-{job_id}.json").exists())
        self.assertFalse((runs_dir / f"active-role-{job_id}.json").exists())

    def test_mismatched_authoritative_identity_refusal(self):
        """Authoritative status with mismatched job id, host, or role binding causes recovery to HOLD, preserving custody without stopping native work."""
        job_id = "job-mismatch-test-6"
        host_id = "test-host-recover"
        native_sid = "77777777-7777-4777-8777-777777777781"
        task_id = f"hub-role-writer-run-{job_id}-g1-e1"

        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o700)

        custody_data = {
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
        }

        # Subtest 1: Mismatched host binding
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        backend = runtime_mgr.role_adapter.launcher_backend
        backend.get_task_binding = MagicMock(return_value={
            "session_id": native_sid,
            "tag": f"task-{task_id}",
            "workspace": str(self.workspace.resolve()),
            "engine": "antigravity",
            "created_at_ms": 1792000000000,
        })
        stopped_targets = []
        backend.stop_session = MagicMock(side_effect=lambda target: (stopped_targets.append(target), (True, "stopped", {}))[1])

        save_role_custody(runs_dir=runs_dir, run_dir=run_dir, job_id=job_id, data=custody_data)

        client = ControlledMockJobsClient()
        client.stale_generation = True
        client.get_job_status = MagicMock(return_value={
            "job": {
                "id": job_id,
                "status": "fenced",
                "lease_epoch": 1,
                "holder_host_id": "different-host-999",  # Mismatch!
                "kind": "writer-run",
            }
        })

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
        self.assertEqual(recovered[0]["reason"], "authoritative_identity_mismatch")
        self.assertEqual(len(stopped_targets), 0)
        self.assertTrue((run_dir / f"custody-{job_id}.json").exists())

        # Subtest 2: Mismatched role binding name
        client.get_job_status = MagicMock(return_value={
            "job": {
                "id": job_id,
                "status": "fenced",
                "lease_epoch": 1,
                "holder_host_id": host_id,
                "kind": "writer-run",
                "role_name": "automation:different:role",  # Mismatch!
            }
        })
        recovered = d.recover_active_role_jobs()
        self.assertEqual(len(recovered), 1)
        self.assertFalse(recovered[0]["recovered"])
        self.assertEqual(recovered[0]["status"], "HOLD")
        self.assertEqual(recovered[0]["reason"], "authoritative_identity_mismatch")
        self.assertEqual(len(stopped_targets), 0)
        self.assertTrue((run_dir / f"custody-{job_id}.json").exists())

    def test_no_duplicate_claim_or_start_during_recovery(self):
        """Recovery across all execution paths never invokes claim_job or start_job."""
        job_id = "job-no-dup-claim-7"
        client = ControlledMockJobsClient()
        client.stale_generation = True

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "test-host-recover"
        native_sid = "77777777-7777-4777-8777-777777777782"
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
        backend.stop_session = MagicMock(return_value=(True, "stopped", {}))

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
        self.assertEqual(recovered[0]["status"], "fenced")

        # Strictly assert zero duplicate claims, zero duplicate starts, zero commits
        self.assertEqual(len(client.claims), 0)
        self.assertEqual(len(client.starts), 0)
        self.assertEqual(len(client.executes), 0)

    def test_recovery_refuses_supervisor_pid_and_leaves_start_ack_absent(self):
        """Recovery refuses first-action with supervisor PID; start_job is never called and start_ack_file remains absent."""
        client = ControlledMockJobsClient()
        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        host_id = "host-recovery-sup"
        job_id = "job-sup-pid-refuse-99"
        native_sid = "88888888-8888-4888-8888-888888888899"
        task_id = f"hub-role-writer-run-{job_id}-g1-e1"

        dummy_proc = subprocess.Popen(["sleep", "60"])
        try:
            supervisor_pid = dummy_proc.pid
            workload_pid = dummy_proc.pid + 1000

            binding_record = {
                "session_id": native_sid,
                "tag": f"task-{task_id}",
                "workspace": str(self.workspace.resolve()),
                "engine": "antigravity",
                "created_at_ms": 1792000000000,
                "worker_pid": supervisor_pid,
                "workload_pid": workload_pid,
                "phase": "running",
                "containment_empty": False,
            }
            backend = runtime_mgr.role_adapter.launcher_backend
            backend.is_fixture_only = True
            backend.get_task_binding = MagicMock(return_value=binding_record)
            backend.is_session_alive = MagicMock(return_value=(True, {"phase": "running"}))
            backend.stop_session = MagicMock(return_value=(True, "stopped", {}))

            runs_dir = self.role_cfg.runs_dir
            run_dir = runs_dir / f"writer-run-{job_id}"
            run_dir.mkdir(parents=True, exist_ok=True)
            run_dir.chmod(0o700)

            # First-action evidence with supervisor PID (counterexample)
            fa_file = run_dir / f"first-action-{job_id}.json"
            fa_file.write_text(json.dumps({
                "job_id": job_id,
                "epoch": 1,
                "role_name": f"automation:test:{job_id}",
                "generation": 1,
                "pid": supervisor_pid,  # supervisor PID, NOT workload PID!
                "native_session": native_sid,
                "stage": "first_action",
                "utc": "2026-10-18T08:00:00Z",
                "isolation_kind": "native_cgroup",
            }), encoding="utf-8")

            start_ack_file = run_dir / f"start-ack-{job_id}.json"

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

            client.job_statuses[job_id] = {
                "job": {
                    "id": job_id,
                    "status": "running",
                    "lease_epoch": 1,
                    "holder_host_id": host_id,
                    "kind": "writer-run",
                    "role_binding": {
                        "name": f"automation:test:{job_id}",
                        "generation": 1,
                        "host_id": host_id,
                        "profile": "writer-run",
                        "ttl_seconds": 300,
                    },
                }
            }

            d = JobDispatcher(
                base_url="https://hub.dtcdev.click",
                host_id=host_id,
                token_provider=lambda: "host-token-123",
                client=client,
                runtime_mgr=runtime_mgr,
            )

            recovered = d.recover_active_role_jobs()
            # Recovery did NOT call start_job on server because supervisor PID was refused
            self.assertEqual(len(client.starts), 0)
            # start_ack_file was NEVER created
            self.assertFalse(start_ack_file.exists())
        finally:
            dummy_proc.terminate()
            dummy_proc.wait()

    def _unstarted_terminal_fixture(self, job_id):
        client = ControlledMockJobsClient()
        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        host_id = "test-host-recover"

        # Custody created at claim, but no execution intent, start, or native session recorded
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
                "deadline_at": time.time() - 10.0,
                "payload": {"date": "2026-10-18"},
                "base_url": "https://hub.dtcdev.click",
                "host_id": host_id,
                "task_id": f"hub-role-writer-run-{job_id}-g1-e1",
                "phase": "claimed",
                "claimed_at": time.time() - 30.0,
            },
        )

        active_idx = runs_dir / f"active-role-{job_id}.json"
        self.assertTrue(active_idx.exists())

        # Server confirms expired with role_effect = None, started_at = None
        client.deadline_exceeded = True
        client.job_statuses[job_id] = {
            "job": {
                "id": job_id,
                "status": "expired",
                "lease_epoch": 1,
                "holder_host_id": host_id,
                "kind": "writer-run",
                "role_name": f"automation:test:{job_id}",
                "role_generation": 1,
                "role_effect": None,
                "started_at": None,
                "first_action_at": None,
                "native_session": None,
            }
        }

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id=host_id,
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        return d, client, runs_dir, run_dir, active_idx

    def test_terminal_reconciliation_unstarted_v18_style(self):
        job_id = "job-unstarted-v18-001"
        d, client, runs_dir, run_dir, active_idx = self._unstarted_terminal_fixture(job_id)
        recovered = d.recover_active_role_jobs()
        self.assertEqual(len(recovered), 1)
        rec = recovered[0]
        self.assertTrue(rec.get("recovered"))
        self.assertEqual(rec.get("status"), "reconciled")
        self.assertEqual(rec.get("outcome"), "failed")
        self.assertEqual(rec.get("terminal_case"), "unstarted")

        # Active pointer unlinked only after validated ACK
        self.assertFalse(active_idx.exists())

        # Custody file preserved and updated with terminal_reconciled
        custody_file = run_dir / f"custody-{job_id}.json"
        self.assertTrue(custody_file.exists())
        custody, _, _ = rld.safe_read_bounded_json(custody_file)
        self.assertEqual(custody.get("phase"), "terminal_reconciled")
        self.assertTrue(custody.get("reconciled"))
        self.assertEqual(custody.get("reconciled_outcome"), "failed")

        # Second recovery run skips already-reconciled custody
        recovered_second = d.recover_active_role_jobs()
        self.assertEqual(len(recovered_second), 0)

    def test_terminal_reconciliation_archive_retry_preserves_original_and_durable_ack(self):
        job_id = "job-terminal-archive-retry"
        d, client, runs_dir, run_dir, active_idx = self._unstarted_terminal_fixture(job_id)
        before = active_idx.read_bytes()
        with patch("agent_hub_host.jobs.os.rename", side_effect=PermissionError("archive unavailable")):
            held = d.recover_active_role_jobs()
        self.assertEqual(held[0]["reason"], "terminal_archive_pending")
        self.assertEqual(active_idx.read_bytes(), before)
        self.assertEqual(len(client.reconciliations), 1)
        self.assertEqual(d.recover_active_role_jobs(), [])
        self.assertEqual(len(client.reconciliations), 1, "local archive retry must not repeat server request")
        self.assertFalse(active_idx.exists())
        archives = list(run_dir.glob(f"settled-active-{job_id}-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), before)

    def test_terminal_reconciliation_refuses_incomplete_ack_without_archiving(self):
        job_id = "job-terminal-missing-receipt"
        d, client, runs_dir, run_dir, active_idx = self._unstarted_terminal_fixture(job_id)
        before = active_idx.read_bytes()
        client.reconcile_override = lambda jid, tok, data: {
            "status": "reconciled", "reconciled": True, "job_id": jid,
            "epoch": 1, "generation": 1, "terminal_case": "unstarted", "outcome": "failed"
        }
        held = d.recover_active_role_jobs()
        self.assertEqual(held[0]["reason"], "uncorrelated_reconciliation_ack")
        self.assertEqual(active_idx.read_bytes(), before)
        self.assertEqual(list(run_dir.glob("settled-active-*")), [])

    def test_terminal_reconciliation_executed_v17_style_corroborated(self):
        """Executed job with corroborated native target reconciles to failed terminal receipt."""
        job_id = "job-executed-v17-002"
        client = ControlledMockJobsClient()
        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        host_id = "test-host-recover"
        native_sid = "60bbfd26-6666-4666-8666-666666666666"
        task_id = f"hub-role-writer-run-{job_id}-g1-e1"

        # Native session record in mock store
        binding_record = {
            "session_id": native_sid,
            "tag": f"task-{task_id}",
            "workspace": str(self.workspace),
            "generation": 1,
            "launcher_task_id": task_id,
        }
        (self.role_cfg.state_dir / "native-sessions.json").write_text(
            json.dumps({"sessions": {native_sid: binding_record}}), encoding="utf-8"
        )

        # First action and start ack exist locally
        (run_dir / f"first-action-{job_id}.json").write_text("{}", encoding="utf-8")
        (run_dir / f"start-ack-{job_id}.json").write_text("{}", encoding="utf-8")

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
                "deadline_at": time.time() - 10.0,
                "payload": {"date": "2026-10-18"},
                "base_url": "https://hub.dtcdev.click",
                "host_id": host_id,
                "task_id": task_id,
                "phase": "running",
                "native_session": native_sid,
                "claimed_at": time.time() - 30.0,
            },
        )

        active_idx = runs_dir / f"active-role-{job_id}.json"
        self.assertTrue(active_idx.exists())

        # Server confirmed expired with role_effect present but native_session NULL (V17 shape)
        client.deadline_exceeded = True
        client.job_statuses[job_id] = {
            "job": {
                "id": job_id,
                "status": "expired",
                "lease_epoch": 1,
                "holder_host_id": host_id,
                "kind": "writer-run",
                "role_name": f"automation:test:{job_id}",
                "role_generation": 1,
                "role_effect": json.dumps({"action": "execute", "epoch": 1, "generation": 1}),
                "started_at": "2026-10-18T08:00:00Z",
                "first_action_at": "2026-10-18T08:00:00Z",
                "native_session": native_sid,
            }
        }

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id=host_id,
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        client.retirement_marker = {"native_id": native_sid, "tag": f"task-{task_id}", "workspace": str(self.workspace),
                                    "engine": "antigravity", "created_at_ms": 1000, "generation": 1,
                                    "cause": "finished", "finished_at_ms": 2000}
        # Local maintained positive retirement and full server association are both required.
        with patch("agent_hub_host.jobs.verify_native_retirement", return_value=(True, {"finished_at_ms": 2000}, "retired_tombstone_verified")), patch.object(JobDispatcher, "_corroborate_native_target", return_value=NativeTarget(
            session_id=native_sid,
            tag=f"task-{task_id}",
            workspace=str(self.workspace), created_at_ms=1000, engine="antigravity",
        )):
            qualified = dict(client.job_statuses[job_id]["job"])
            client.job_statuses[job_id]["job"].update(native_session=None, started_at=None, first_action_at=None)
            held = d.recover_active_role_jobs()
            self.assertEqual(held[0].get("reason"), "missing_server_native_association")
            self.assertTrue(active_idx.exists())
            self.assertEqual(client.reconciliations, [])
            client.job_statuses[job_id]["job"] = qualified
            recovered = d.recover_active_role_jobs()
            self.assertEqual(len(recovered), 1)
            rec = recovered[0]
            self.assertTrue(rec.get("recovered"))
            self.assertEqual(rec.get("status"), "reconciled")
            self.assertEqual(rec.get("outcome"), "failed")
            self.assertEqual(rec.get("terminal_case"), "executed")

            # Active index unlinked
            self.assertFalse(active_idx.exists())
            archives = list(run_dir.glob(f"settled-active-{job_id}-*.json"))
            self.assertEqual(len(archives), 1)
            original, _, _ = rld.safe_read_bounded_json(archives[0])
            self.assertEqual(original.get("phase"), "running")

            # Reconcile called with native_session
            self.assertEqual(len(client.reconciliations), 1)
            self.assertEqual(client.reconciliations[0]["data"].get("terminal_case"), "executed")
            self.assertEqual(client.reconciliations[0]["data"].get("native_session"), native_sid)

    def test_terminal_reconciliation_holds_on_running_process(self):
        """Executed job with alive running physical process fails closed (HOLD) without reconciling."""
        job_id = "job-running-proc-003"
        client = ControlledMockJobsClient()
        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        host_id = "test-host-recover"
        native_sid = "77777777-7777-4777-8777-777777777777"
        task_id = f"hub-role-writer-run-{job_id}-g1-e1"

        dummy_proc = self._make_dummy_worker_process()
        try:
            (run_dir / f"first-action-{job_id}.json").write_text("{}", encoding="utf-8")
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
                    "deadline_at": time.time() - 10.0,
                    "payload": {"date": "2026-10-18"},
                    "base_url": "https://hub.dtcdev.click",
                    "host_id": host_id,
                    "task_id": task_id,
                    "phase": "running",
                    "native_session": native_sid,
                    "claimed_at": time.time() - 30.0,
                },
            )

            client.deadline_exceeded = True
            client.job_statuses[job_id] = {
                "job": {
                    "id": job_id,
                    "status": "expired",
                    "lease_epoch": 1,
                    "holder_host_id": host_id,
                    "kind": "writer-run",
                    "role_name": f"automation:test:{job_id}",
                    "role_generation": 1,
                    "role_effect": "{}",
                    "started_at": "2026-10-18T08:00:00Z", "first_action_at": "2026-10-18T08:00:00Z",
                    "native_session": native_sid,
                }
            }

            runtime_mgr = RuntimeManager(role_config=self.role_cfg)
            d = JobDispatcher(
                base_url="https://hub.dtcdev.click",
                host_id=host_id,
                token_provider=lambda: "host-token-123",
                client=client,
                runtime_mgr=runtime_mgr,
            )

            target = NativeTarget(
                session_id=native_sid,
                tag=f"task-{task_id}",
                workspace=str(self.workspace), created_at_ms=1000, engine="antigravity",
            )

            with patch.object(JobDispatcher, "_corroborate_native_target", return_value=target), \
                 patch("agent_hub_host.jobs.verify_native_retirement", return_value=(False, None, "target_still_active")),                  patch.object(JobDispatcher, "_stop_native_target_safely", side_effect=AssertionError("passive reconciliation must not stop")):
                recovered = d.recover_active_role_jobs()
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].get("status"), "HOLD")
                self.assertEqual(recovered[0].get("reason"), "native_retirement_unproved")
                # Active index remains preserved
                self.assertTrue((runs_dir / f"active-role-{job_id}.json").exists())
                # Reconcile was NEVER called
                self.assertEqual(len(client.reconciliations), 0)
        finally:
            dummy_proc.terminate()
            dummy_proc.wait()

    def test_terminal_reconciliation_rejects_uncorrelated_or_generic_server_ack(self):
        """SDK rejects generic or mismatched server ACK; custody preserved without archiving."""
        job_id = "job-uncorrelated-ack-004"
        client = ControlledMockJobsClient()
        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        host_id = "test-host-recover"

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
                "deadline_at": time.time() - 10.0,
                "payload": {},
                "base_url": "https://hub.dtcdev.click",
                "host_id": host_id,
                "task_id": f"hub-role-writer-run-{job_id}-g1-e1",
                "phase": "claimed",
                "claimed_at": time.time() - 30.0,
            },
        )

        client.deadline_exceeded = True
        client.job_statuses[job_id] = {
            "job": {
                "id": job_id,
                "status": "expired",
                "lease_epoch": 1,
                "holder_host_id": host_id,
                "kind": "writer-run",
                "role_name": f"automation:test:{job_id}",
                "role_generation": 1,
                "role_effect": None,
                "started_at": None,
                "first_action_at": None,
                "native_session": None,
            }
        }

        # Override reconcile response to return wrong job_id (or generic success)
        client.reconcile_override = lambda jid, tok, d: {
            "reconciled": True,
            "job_id": "wrong-job-id",
            "epoch": 1,
            "generation": 1,
            "outcome": "succeeded",  # business success forbidden
        }

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id=host_id,
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        recovered = d.recover_active_role_jobs()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].get("status"), "HOLD")
        self.assertEqual(recovered[0].get("reason"), "uncorrelated_reconciliation_ack")
        # Active pointer NOT unlinked
        self.assertTrue((runs_dir / f"active-role-{job_id}.json").exists())
        # Custody NOT marked reconciled
        custody, _, _ = rld.safe_read_bounded_json(run_dir / f"custody-{job_id}.json")
        self.assertFalse(custody.get("reconciled", False))

    def test_terminal_reconciliation_holds_on_server_hold_or_missing_marker(self):
        """When server returns HOLD (e.g. missing retirement marker), client preserves custody."""
        job_id = "job-missing-marker-005"
        client = ControlledMockJobsClient()
        runs_dir = self.role_cfg.runs_dir
        run_dir = runs_dir / f"writer-run-{job_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        host_id = "test-host-recover"

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
                "deadline_at": time.time() - 10.0,
                "payload": {},
                "base_url": "https://hub.dtcdev.click",
                "host_id": host_id,
                "task_id": f"hub-role-writer-run-{job_id}-g1-e1",
                "phase": "claimed",
                "claimed_at": time.time() - 30.0,
            },
        )

        client.deadline_exceeded = True
        client.job_statuses[job_id] = {
            "job": {
                "id": job_id,
                "status": "expired",
                "lease_epoch": 1,
                "holder_host_id": host_id,
                "kind": "writer-run",
                "role_name": f"automation:test:{job_id}",
                "role_generation": 1,
                "role_effect": None,
                "started_at": None,
                "first_action_at": None,
                "native_session": None,
            }
        }

        # Server returns HOLD due to missing retirement marker
        client.reconcile_override = lambda jid, tok, d: {
            "status": "HOLD",
            "reconciled": False,
            "reason": "retirement_marker_missing",
        }

        runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        d = JobDispatcher(
            base_url="https://hub.dtcdev.click",
            host_id=host_id,
            token_provider=lambda: "host-token-123",
            client=client,
            runtime_mgr=runtime_mgr,
        )

        recovered = d.recover_active_role_jobs()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].get("status"), "HOLD")
        self.assertEqual(recovered[0].get("reason"), "server_reconciliation_hold: retirement_marker_missing")
        # Active pointer remains preserved
        self.assertTrue((runs_dir / f"active-role-{job_id}.json").exists())


if __name__ == "__main__":
    unittest.main()

