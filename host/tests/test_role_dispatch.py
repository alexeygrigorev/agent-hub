"""Comprehensive tests for M8b Platform Role Runtime Real Backend adapter.

Covers:
1. No generated nativeUUID accepted (strict Launcher binding corroboration)
2. No bare exit 0 success without verified bounded outcome artifact
3. Maintained CLI argv/plan and exact receipt binding
4. Fresh gate and model/account selection (allowlists and refusal)
5. Lost reply / restart no duplicate (idempotent task ID derivation)
6. Stale / reused first-action and same job different epoch rejection
7. Revoked authority and cancel uncertain containment verification
8. Symlink, FIFO, and oversize evidence security handling
9. Configured false consumer gate (no claims, settings precedence over journal)
10. Immutable provenance and no token output (scrubbing)
"""

import json
import os
import pathlib
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from agent_hub_host import jobs, role_dispatch, session_host
from agent_hub_host.role_dispatch import (
    DispatchError,
    LauncherBackend,
    RoleDispatchAdapter,
    RoleProfileConfig,
    RoleQualification,
    RoleRun,
    TrustedRoleConfig,
    ensure_workspace_tmp_dir,
    get_clock_ticks_per_sec,
    get_pid_starttime_ticks,
    get_proc_start_time,
    get_proc_state,
    get_role_first_action_digest,
    get_system_boot_time,
    is_pid_in_cgroup,
    normalize_cgroup_path,
    read_proc_cgroups,
    safe_read_bounded_json,
    scrub,
    scrub_dict,
    validate_entrypoint,
    validate_native_workload_pid,
)
from agent_hub_host.runtime import RuntimeManager


class TestRoleRealBackendAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        self.launcher_repo = self.base / "launcher-repo"
        (self.launcher_repo / "launcher").mkdir(parents=True)
        (self.launcher_repo / "launcher" / "__init__.py").write_text("", encoding="utf-8")

        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                    launcher_model="gemini-3.1-pro-high",
                    launcher_task_profile="model-task",
                ),
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                    launcher_model="gemini-3.1-pro-high",
                    launcher_task_profile="model-task",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=self.launcher_repo,
            allowed_workspaces=[self.workspace],
            allowed_models=["gemini-3.1-pro-high", "gemini-3.8-flash-high"],
            allowed_accounts=["antigravity"],
        )

        self.binding = {
            "name": "automation:demo:writer",
            "generation": 2,
            "epoch": 3,
            "profile": "writer-run",
            "host_id": "host-test",
            "ttl_seconds": 120,
        }

    def tearDown(self):
        self.tmp.cleanup()

    # 1. No generated nativeUUID accepted
    def test_no_generated_native_uuid_accepted(self):
        run = RoleRun(
            job_id="job-uuid-test",
            job_token="secret-token-123",
            lease_epoch=3,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        # Verify native_session is NOT pre-populated with a random UUID
        self.assertIsNone(run.native_session)

        # Nil UUID must be rejected
        ev_file = self.base / "first-action-nil.json"
        ev_file.write_text(json.dumps({
            "pid": os.getpid() + 1000,
            "job_id": "job-uuid-test",
            "epoch": 3,
            "role_name": "automation:demo:writer",
            "generation": 2,
            "stage": "first_action",
            "native_session": "00000000-0000-0000-0000-000000000000",
            "utc": "2026-10-17T20:00:00Z",
            "private_pid_namespace": f"pid:[{os.getpid() + 1000}]",
            "numeric_proc_entries": [os.getpid() + 1000],
        }), encoding="utf-8")
        with patch("os.kill"):
            digest = get_role_first_action_digest(
                ev_file,
                expected_job_id="job-uuid-test",
                expected_epoch=3,
                expected_role_name="automation:demo:writer",
                expected_generation=2,
            )
        self.assertIsNone(digest)

        # Invalid UUID format must be rejected
        ev_file_bad = self.base / "first-action-bad.json"
        ev_file_bad.write_text(json.dumps({
            "pid": os.getpid() + 1000,
            "job_id": "job-uuid-test",
            "epoch": 3,
            "role_name": "automation:demo:writer",
            "generation": 2,
            "stage": "first_action",
            "native_session": "not-a-valid-uuid",
            "utc": "2026-10-17T20:00:00Z",
            "private_pid_namespace": f"pid:[{os.getpid() + 1000}]",
            "numeric_proc_entries": [os.getpid() + 1000],
        }), encoding="utf-8")
        with patch("os.kill"):
            digest_bad = get_role_first_action_digest(
                ev_file_bad,
                expected_job_id="job-uuid-test",
                expected_epoch=3,
                expected_role_name="automation:demo:writer",
                expected_generation=2,
            )
        self.assertIsNone(digest_bad)

    # 2. No bare exit 0 success without verified outcome artifact
    def test_no_bare_exit0_success_without_outcome_artifact(self):
        run = RoleRun(
            job_id="job-exit0-test",
            job_token="token-xyz",
            lease_epoch=3,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        # Simulated run result with rc=0 but NO outcome file produced
        run._exec_result = {"rc": 0, "stdout": "process finished with exit 0"}
        out = run.outcome()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["hub_status"], "failed")
        self.assertIn("missing_or_invalid_outcome_artifact", out["summary"])

    # 2b. Outcome with valid outcome file succeeds
    def test_valid_outcome_artifact_succeeds(self):
        run = RoleRun(
            job_id="job-outcome-test",
            job_token="token-xyz",
            lease_epoch=3,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        run.outcome_file.write_text(json.dumps({
            "status": "succeeded",
            "summary": "article drafted successfully",
            "evidence_refs": [str(run.outcome_file)],
        }), encoding="utf-8")
        out = run.outcome()
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(out["hub_status"], "succeeded")
        self.assertIn("article drafted successfully", out["summary"])

    # 3. Maintained CLI argv/plan and exact receipt binding
    def test_maintained_cli_plan_and_receipt_binding(self):
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {
            "mode": "dry-run",
            "chosen": "antigravity",
            "eligible": [{"provider": "antigravity", "model": "gemini-3.1-pro-high", "health": 1.0}],
        }
        mock_backend.submit.return_value = {"task_id": "task-1", "state": "queued"}
        mock_backend.run.return_value = (
            0,
            {
                "session_id": "44444444-4444-4444-4444-444444444444",
                "tag": "task-hub-role-writer-run",
                "workspace": str(self.workspace),
                "engine": "antigravity",
            },
            '{"backend": "aplexer", "receipt": {"session_id": "44444444-4444-4444-4444-444444444444"}}',
        )

        qual = RoleQualification(
            config=self.role_cfg,
            quota_checker=lambda: (True, "ok"),
            backend=mock_backend,
        )
        rep = qual.inspect_profile("writer-run")
        self.assertTrue(rep["supported"])
        mock_backend.plan.assert_called_once()

    # 4. Fresh gate and model/account selection
    def test_unallowed_account_or_model_refused(self):
        # Configure profile with unallowed provider
        bad_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="unauthorized-provider",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=self.launcher_repo,
            allowed_workspaces=[self.workspace],
            allowed_accounts=["antigravity"],
        )
        qual = RoleQualification(config=bad_cfg)
        rep = qual.inspect_profile("writer-run")
        self.assertFalse(rep["supported"])
        self.assertIn("provider_not_in_allowed_accounts", rep["reasons"][0])

        # Configure profile with unallowed model
        bad_model_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                    launcher_model="unapproved-model",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=self.launcher_repo,
            allowed_workspaces=[self.workspace],
            allowed_models=["gemini-3.1-pro-high"],
            allowed_accounts=["antigravity"],
        )
        qual_m = RoleQualification(config=bad_model_cfg)
        rep_m = qual_m.inspect_profile("writer-run")
        self.assertFalse(rep_m["supported"])
        self.assertIn("model_not_in_allowed_models", rep_m["reasons"][0])

    def test_quota_refusal_or_omitted_qualification(self):
        # Quota gate returns refused
        qual = RoleQualification(
            config=self.role_cfg,
            quota_checker=lambda: (False, "quota_window_exhausted"),
        )
        rep = qual.inspect_profile("writer-run")
        self.assertFalse(rep["supported"])
        self.assertIn("quota_refused: quota_window_exhausted", rep["reasons"][0])

        # When quota_checker is omitted and launcher plan cannot run, stay refused
        qual_no_quota = RoleQualification(config=self.role_cfg, quota_checker=None)
        rep_no_q = qual_no_quota.inspect_profile("writer-run")
        self.assertFalse(rep_no_q["supported"])

    # 5. Lost reply / restart no duplicate (idempotent task ID derivation)
    def test_idempotent_task_id_derivation(self):
        run1 = RoleRun(
            job_id="job-idempotent-123456",
            job_token="token-abc",
            lease_epoch=5,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        run2 = RoleRun(
            job_id="job-idempotent-123456",
            job_token="token-abc",
            lease_epoch=5,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        # Deterministic and identical task_id and idempotency_key
        self.assertEqual(run1.task_id, run2.task_id)
        self.assertEqual(run1.idempotency_key, run2.idempotency_key)
        self.assertIn("g2-e5", run1.task_id)

    # 6. Stale / reused first-action and same job different epoch rejection
    def test_same_job_different_epoch_rejected(self):
        ev_file = self.base / "first-action-epoch.json"
        ev_file.write_text(json.dumps({
            "pid": os.getpid() + 1000,
            "job_id": "job-epoch-1",
            "epoch": 1,  # Old epoch
            "role_name": "automation:demo:writer",
            "generation": 2,
            "stage": "first_action",
            "native_session": "22222222-2222-2222-2222-222222222222",
            "utc": "2026-10-17T20:00:00Z",
            "private_pid_namespace": f"pid:[{os.getpid() + 1000}]",
            "numeric_proc_entries": [os.getpid() + 1000],
        }), encoding="utf-8")

        with patch("os.kill"):
            digest = get_role_first_action_digest(
                ev_file,
                expected_job_id="job-epoch-1",
                expected_epoch=2,  # Current epoch 2 != 1
                expected_role_name="automation:demo:writer",
                expected_generation=2,
            )
        self.assertIsNone(digest)

    def test_pre_existing_evidence_on_start_fails_closed(self):
        run = RoleRun(
            job_id="job-pre-exist",
            job_token="token-abc",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        # Create pre-existing evidence file
        run.first_action_file.write_text("{}", encoding="utf-8")
        with self.assertRaises(DispatchError) as ctx:
            run.start()
        self.assertIn("pre_existing_evidence_conflict", str(ctx.exception))
        # Ensure file was not unlinked!
        self.assertTrue(run.first_action_file.exists())

    # 7. Revoked authority and cancel uncertain containment verification
    def test_cancel_containment_verification(self):
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.stop_session.return_value = (True, "confirmed_stop", {"state": "stopped"})

        run = RoleRun(
            job_id="job-cancel-test",
            job_token="token-abc",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
            launcher_backend=mock_backend,
        )
        run.native_session = "33333333-3333-3333-3333-333333333333"
        outcome = run.cancel(reason="lease_revoked")
        self.assertTrue(outcome["verified"])
        self.assertEqual(mock_backend.stop_session.call_count, 1)

        # If stop_session reports failure, verified must be False
        mock_backend.stop_session.return_value = (False, "failed_stop", {})
        outcome_unverified = run.cancel(reason="failed_stop")
        self.assertFalse(outcome_unverified["verified"])

        # If thread is active but native_session is unbound, cancel cannot prove stop
        run_unbound = RoleRun(
            job_id="job-unbound-test",
            job_token="token-abc",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
            launcher_backend=mock_backend,
        )
        run_unbound._thread = MagicMock()
        run_unbound._thread.is_alive.return_value = True
        outcome_unbound = run_unbound.cancel(reason="unbound_stop")
        self.assertFalse(outcome_unbound["verified"])
        self.assertIn("unbound_native_session", outcome_unbound.get("detail", {}).get("error", ""))

    def test_stop_session_negative_unknown_and_live_witness(self):
        backend = LauncherBackend(launcher_repo=self.launcher_repo, config_dir=self.state_dir)

        # 1. Invalid UUID rejected immediately
        ok, reason, _ = backend.stop_session("not-a-uuid", tag="test-tag", workspace=str(self.workspace))
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_native_session_uuid")

        # 2. Nil UUID rejected
        ok, reason, _ = backend.stop_session(role_dispatch.NIL_UUID, tag="test-tag", workspace=str(self.workspace))
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_native_session_uuid")

        # 3. SessionControlError (target absent from catalog, tag reuse without tombstone)
        target = role_dispatch.NativeTarget(
            session_id="11111111-1111-1111-1111-111111111111",
            tag="test-tag",
            workspace=str(self.workspace.resolve()),
        )
        with patch.object(role_dispatch.ns, "stop_session", side_effect=role_dispatch.ns.SessionControlError("Target not in native catalog")):
            ok, reason, detail = backend.stop_session(target)
            self.assertFalse(ok)
            self.assertIn("control_error", reason)

        # 4. SessionUncertain (live witness, workload exit unproven)
        with patch.object(role_dispatch.ns, "stop_session", side_effect=role_dispatch.ns.SessionUncertain("Target terminal but workload exit unproven")):
            ok, reason, detail = backend.stop_session(target)
            self.assertFalse(ok)
            self.assertIn("uncertain_containment", reason)
            self.assertTrue(detail.get("uncertain"))

    def test_missing_start_record_refuses_forged_first_action(self):
        run = RoleRun(
            job_id="job-forged-test",
            job_token="token-abc",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        # Model authored a forged first-action file with a valid UUID
        forged_uuid = "44444444-4444-4444-4444-444444444444"
        run.first_action_file.write_text(json.dumps({
            "job_id": "job-forged-test",
            "epoch": 1,
            "role_name": "writer",
            "generation": 1,
            "native_session": forged_uuid,
            "utc": "2026-10-17T22:00:00Z",
        }), encoding="utf-8")

        # NO start record, receipt, or store binding exists
        # wait_bootstrap MUST refuse to accept the first action file or adopt its UUID
        res = run.wait_bootstrap(timeout=0.05)
        self.assertIsNone(res)
        self.assertIsNone(run.native_session)

    def test_genuine_binding_mismatch_refused(self):
        run = RoleRun(
            job_id="job-mismatch-test",
            job_token="token-abc",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        # Authoritative store binding with UUID A
        store_binding = {
            "task_id": run.task_id,
            "native_id": "11111111-1111-1111-1111-111111111111",
            "session_id": "11111111-1111-1111-1111-111111111111",
            "tag": f"task-{run.task_id}",
            "workspace": str(run.task_workspace.resolve()),
            "engine": "antigravity",
            "incarnation": 1729200000000,
            "created_at_ms": 1729200000000,
        }
        with patch.object(run.launcher_backend, "get_task_binding", return_value=store_binding):
            proc = subprocess.Popen(["sleep", "60"])
            try:
                # First action file contains different UUID B (e.g. forged or from another task)
                run.first_action_file.write_text(json.dumps({
                    "pid": proc.pid,
                    "job_id": run.job_id,
                    "epoch": run.lease_epoch,
                    "role_name": run.role_name,
                    "generation": run.role_generation,
                    "stage": "first_action",
                    "native_session": "22222222-2222-2222-2222-222222222222",
                    "utc": "2026-10-17T22:00:00Z",
                }), encoding="utf-8")

                # wait_bootstrap MUST reject the mismatched first action
                res = run.wait_bootstrap(timeout=0.05)
                self.assertIsNone(res)
                self.assertEqual(run.native_session, "11111111-1111-1111-1111-111111111111")
            finally:
                proc.kill()
                proc.wait()

    def test_incomplete_or_mismatched_maintained_binding_negatives(self):
        run = RoleRun(
            job_id="job-binding-neg-test",
            job_token="token-abc",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        start_rec_path = run.task_workspace / ".local" / f"launch-{run.task_id}-start.json"
        start_rec_path.parent.mkdir(parents=True, exist_ok=True)

        valid_base = {
            "id": "11111111-1111-1111-1111-111111111111",
            "tag": f"task-{run.task_id}",
            "workspace": str(run.task_workspace.resolve()),
            "engine": "antigravity",
            "created_at_ms": 1729200000000,
        }

        # 1. Incomplete: missing tag
        incomp_tag = dict(valid_base)
        del incomp_tag["tag"]
        start_rec_path.write_text(json.dumps(incomp_tag), encoding="utf-8")
        self.assertIsNone(run.corroborate_maintained_binding())

        # 2. Incomplete: missing workspace
        incomp_ws = dict(valid_base)
        del incomp_ws["workspace"]
        start_rec_path.write_text(json.dumps(incomp_ws), encoding="utf-8")
        self.assertIsNone(run.corroborate_maintained_binding())

        # 3. Incomplete: missing engine
        incomp_eng = dict(valid_base)
        del incomp_eng["engine"]
        start_rec_path.write_text(json.dumps(incomp_eng), encoding="utf-8")
        self.assertIsNone(run.corroborate_maintained_binding())

        # 4. Incomplete: missing or non-positive incarnation
        incomp_inc = dict(valid_base, created_at_ms=0)
        start_rec_path.write_text(json.dumps(incomp_inc), encoding="utf-8")
        self.assertIsNone(run.corroborate_maintained_binding())

        # 5. Mismatched: wrong tag
        mism_tag = dict(valid_base, tag="wrong-tag")
        start_rec_path.write_text(json.dumps(mism_tag), encoding="utf-8")
        self.assertIsNone(run.corroborate_maintained_binding())

        # 6. Mismatched: wrong workspace
        mism_ws = dict(valid_base, workspace="/wrong/path")
        start_rec_path.write_text(json.dumps(mism_ws), encoding="utf-8")
        self.assertIsNone(run.corroborate_maintained_binding())

        # 7. Mismatched: wrong engine
        mism_eng = dict(valid_base, engine="wrong-engine")
        start_rec_path.write_text(json.dumps(mism_eng), encoding="utf-8")
        self.assertIsNone(run.corroborate_maintained_binding())

    def test_complete_corroborated_binding_positive(self):
        run = RoleRun(
            job_id="job-pos-test",
            job_token="token-abc",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        sess_id = "06422043-238b-4460-86f3-2fdb76a7e381"
        expected_tag = f"task-{run.task_id}"
        ws_str = str(run.task_workspace.resolve())

        # Authoritative store binding with full 5 fields
        store_binding = {
            "task_id": run.task_id,
            "native_id": sess_id,
            "session_id": sess_id,
            "tag": expected_tag,
            "workspace": ws_str,
            "engine": "antigravity",
            "incarnation": 1729200000000,
            "created_at_ms": 1729200000000,
        }
        with patch.object(run.launcher_backend, "get_task_binding", return_value=store_binding):
            # Authoritative binding is successfully corroborated
            binding = run.corroborate_maintained_binding()
            self.assertIsNotNone(binding)
            self.assertEqual(binding["session_id"], sess_id)
            self.assertEqual(binding["tag"], expected_tag)
            self.assertEqual(binding["workspace"], ws_str)
            self.assertEqual(binding["engine"], "antigravity")
            self.assertEqual(binding["created_at_ms"], 1729200000000)

            proc = subprocess.Popen(["sleep", "60"])
            try:
                run.first_action_file.write_text(json.dumps({
                    "pid": proc.pid,
                    "job_id": run.job_id,
                    "epoch": run.lease_epoch,
                    "role_name": run.role_name,
                    "generation": run.role_generation,
                    "stage": "first_action",
                    "native_session": sess_id,
                    "utc": "2026-10-17T22:00:00Z",
                    "private_pid_namespace": f"pid:[{proc.pid}]",
                    "numeric_proc_entries": [proc.pid],
                }), encoding="utf-8")

                # wait_bootstrap successfully verifies and accepts
                res = run.wait_bootstrap(timeout=0.05)
                self.assertIsNotNone(res)
                self.assertEqual(res["native_session"], sess_id)
                self.assertEqual(run.native_session, sess_id)
            finally:
                proc.kill()
                proc.wait()

    def test_lost_startreply_reconciliation_without_duplicate_launch(self):
        backend = LauncherBackend(launcher_repo=self.launcher_repo, config_dir=self.state_dir)
        task_id = "hub-role-reconcile-test"

        # Mock existing task in store
        existing_status = {
            "id": task_id,
            "task_id": task_id,
            "state": "running",
            "idempotency_key": "key-123",
        }
        with patch.object(backend, "get_task_status", return_value=existing_status):
            with patch("subprocess.run") as mock_sub:
                res = backend.submit(task_id, "key-123", {"goal": "test"})
                self.assertEqual(res["id"], task_id)
                self.assertEqual(res["state"], "running")
                # subprocess.run should NOT be called because task was reconciled!
                mock_sub.assert_not_called()

    # 8. Symlink, FIFO, and oversize evidence security handling
    def test_symlink_fifo_oversize_evidence_rejected(self):
        # Symlink evidence rejected
        real_file = self.base / "real_ev.json"
        real_file.write_text("{}", encoding="utf-8")
        sym_file = self.base / "sym_ev.json"
        sym_file.symlink_to(real_file)

        data, digest, raw = safe_read_bounded_json(sym_file)
        self.assertIsNone(data)
        self.assertIsNone(digest)

        # FIFO evidence rejected
        fifo_file = self.base / "fifo_ev"
        os.mkfifo(fifo_file)
        data_f, digest_f, _ = safe_read_bounded_json(fifo_file)
        self.assertIsNone(data_f)
        self.assertIsNone(digest_f)

        # Oversize file (> 64KB) rejected
        big_file = self.base / "big_ev.json"
        big_file.write_bytes(b"A" * 70000)
        data_b, digest_b, _ = safe_read_bounded_json(big_file)
        self.assertIsNone(data_b)
        self.assertIsNone(digest_b)

    # 9. Configured false consumer gate (no claims, settings precedence over journal)
    def test_configured_false_consumer_no_claims(self):
        consumer = session_host.RoleConsumer(enabled=False)
        step_res = consumer.step()
        self.assertTrue(step_res.get("disabled"))
        self.assertEqual(step_res.get("reason"), "role_consumer_disabled")

    def test_config_file_change_fences_active_dispatcher(self):
        cfg_file = self.base / "live_roles.json"
        cfg_file.write_text(json.dumps({
            "version": 1,
            "enabled": False,
            "profiles": {},
        }), encoding="utf-8")

        mock_dispatcher = MagicMock()
        mock_dispatcher.runtime_mgr = RuntimeManager(role_config=TrustedRoleConfig.disabled())

        consumer = session_host.RoleConsumer(
            config_path=str(cfg_file),
            dispatcher=mock_dispatcher,
            enabled=True,
        )
        res1 = consumer.step()
        self.assertTrue(res1.get("disabled"))

        # Modify config on disk
        time.sleep(0.05)
        cfg_file.write_text(json.dumps({
            "version": 1,
            "enabled": True,
            "profiles": {
                "writer-run": {
                    "enabled": True,
                    "workspace": str(self.workspace),
                }
            },
        }), encoding="utf-8")

        consumer.last_run_monotonic = 0.0
        consumer.step()
        # Verify dispatcher runtime_mgr role_config was updated with new config
        self.assertTrue(mock_dispatcher.runtime_mgr.role_config.enabled)
        self.assertIn("writer-run", mock_dispatcher.runtime_mgr.role_config.profiles)

    # 10. Immutable provenance and no token output
    def test_scrub_tokens_from_summary(self):
        secret = "secret-token-789"
        text = f"Executed role with token {secret} on hub https://hub.example.com"
        clean = scrub(text, secret)
        self.assertNotIn(secret, clean)
        self.assertIn("[redacted]", clean)

    def test_parent_symlink_ancestor_rejected(self):
        real_parent = self.base / "real_parent"
        real_parent.mkdir(parents=True)
        target = real_parent / "evidence.json"
        target.write_text(json.dumps({"valid": True}), encoding="utf-8")

        sym_parent = self.base / "sym_parent"
        sym_parent.symlink_to(real_parent)
        path_through_symlink = sym_parent / "evidence.json"

        # Descriptor-relative traversal rejects symlinked parent ancestor
        data, digest, raw = safe_read_bounded_json(path_through_symlink)
        self.assertIsNone(data)
        self.assertIsNone(digest)

    def test_distinct_jobs_sharing_first16_do_not_collide(self):
        run1 = RoleRun(
            job_id="job-abcdefghijklmnop-1111",
            job_token="token-1",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        run2 = RoleRun(
            job_id="job-abcdefghijklmnop-2222",
            job_token="token-2",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        self.assertNotEqual(run1.task_id, run2.task_id)
        self.assertNotEqual(run1.idempotency_key, run2.idempotency_key)

    # 11. TMPDIR containment and Launcher check_resources integration
    def test_job_tmpdir_containment_and_resource_check(self):
        # ensure_workspace_tmp_dir creates owned 0700 dir under workspace/.local/tmp
        job_tmp = ensure_workspace_tmp_dir(self.workspace, "job-tmp-test")
        self.assertTrue(job_tmp.is_dir())
        self.assertTrue(job_tmp.resolve().is_relative_to(self.workspace.resolve() / ".local" / "tmp"))
        st = job_tmp.stat()
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)
        self.assertEqual(st.st_uid, os.getuid())

        # Escape via traversal/symlink is rejected
        with self.assertRaises(ValueError):
            ensure_workspace_tmp_dir(self.workspace, "../../../escape")

        # RoleRun assigns job_tmp_dir inside workspace .local/tmp
        run = RoleRun(
            job_id="job-tmp-check",
            job_token="secret-123",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-18"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        self.assertTrue(run.job_tmp_dir.resolve().is_relative_to(self.workspace.resolve() / ".local" / "tmp"))

        # Cross-project integration is optional on CI, where this separate
        # maintained checkout is absent. Keep the owned-path unit assertions
        # above portable and report this subtest explicitly when unavailable.
        launcher_root = pathlib.Path("/home/alexey/git/agent-quota-launcher")
        with self.subTest(integration="maintained Launcher resource guard"):
            if not (launcher_root / "launcher" / "resources.py").is_file():
                self.skipTest("maintained Launcher checkout unavailable for cross-project integration")
            import sys
            original_path = list(sys.path)
            # Never leave the Launcher's generic tests package ahead of the
            # Hub fixtures: the required CI subset runs test_jobs afterwards.
            with patch.object(sys, "path", [str(launcher_root), *sys.path]):
                from launcher.resources import check_resources
            self.assertEqual(sys.path, original_path)
            # Valid path under repo_root/.local/tmp
            check_resources(
                requested_memory_mb=100,
                requested_cwd=str(self.workspace),
                requested_tmpdir=str(run.job_tmp_dir),
                repo_root=str(self.workspace),
                ignore_ram=True,
            )
            # Invalid path outside repo_root/.local/tmp (the old buggy run_dir/tmp)
            bad_tmp = self.state_dir / "runs" / run.job_id / "tmp"
            bad_tmp.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ValueError) as ctx:
                check_resources(
                    requested_memory_mb=100,
                    requested_cwd=str(self.workspace),
                    requested_tmpdir=str(bad_tmp),
                    repo_root=str(self.workspace),
                    ignore_ram=True,
                )
            self.assertIn("must resolve under owned", str(ctx.exception).lower())

    # 12. Role bootstrap goal, payload, and scrubbed tokens
    def test_bootstrap_goal_and_payload_composition(self):
        run = RoleRun(
            job_id="job-boot-1",
            job_token="sensitive-job-secret-token",
            lease_epoch=2,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-18", "auth": "sensitive-job-secret-token"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        goal = run._build_goal()

        self.assertIn("Never use the main-temp AWS profile or static key unless this brief cites Alexey's explicit approval for this exact job; if you need AWS access that is not otherwise available, stop and report a blocker.", goal)
        self.assertIn("Worker rules: Never use the main-temp", goal)
        # Secret token must NOT appear in goal
        self.assertNotIn("sensitive-job-secret-token", goal)
        self.assertIn("[redacted]", goal)

        # 4-stage bootstrap protocol is explicitly specified in goal
        self.assertIn("TRUSTED BOOTSTRAP PROTOCOL:", goal)
        self.assertIn(str(run.first_action_file), goal)
        self.assertIn(str(run.start_ack_file), goal)
        self.assertIn(str(run.outcome_file), goal)
        self.assertIn('"stage": "first_action"', goal)
        self.assertIn('"pid": <workload_pid>', goal)
        self.assertIn("workload_pid", goal)
        self.assertNotIn("<worker_pid>", goal)
        self.assertIn("Start Acknowledgement Barrier:", goal)
        self.assertIn("CRITICAL: do NOT execute any business effects before the Hub start acknowledgement", goal)
        self.assertIn("Outcome Artifact:", goal)

        # Verify all supported profiles produce concrete execution actions
        for kind in ("writer-run", "principal-wake", "head-recovery"):
            p_run = RoleRun(
                job_id=f"job-{kind}",
                job_token="token",
                lease_epoch=1,
                role_binding={**self.binding, "profile": kind},
                kind=kind,
                payload={},
                hub_url="https://hub.example.com",
                host_id="host-test",
                config=self.role_cfg,
            )
            p_goal = p_run._build_goal()
            self.assertIn(f"Execute {kind}", p_goal)

        # Configured entrypoint execution
        entry_script = self.workspace / "custom_job.sh"
        entry_script.write_text("#!/bin/sh\necho test\n", encoding="utf-8")
        entry_script.chmod(0o700)

        entry_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                    launcher_model="gemini-3.1-pro-high",
                    entrypoint=entry_script,
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=self.launcher_repo,
            allowed_workspaces=[self.workspace],
            allowed_models=["gemini-3.1-pro-high"],
            allowed_accounts=["antigravity"],
        )
        entry_run = RoleRun(
            job_id="job-entry",
            job_token="token",
            lease_epoch=1,
            role_binding={**self.binding, "profile": "writer-run"},
            kind="writer-run",
            payload={"key": "val"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=entry_cfg,
        )
        entry_goal = entry_run._build_goal()
        self.assertIn(f"Execute configured entrypoint: {entry_script.resolve()}", entry_goal)

        # Invalid entrypoint is refused
        with self.assertRaises(DispatchError):
            validate_entrypoint("relative/path.sh")
        with self.assertRaises(DispatchError):
            validate_entrypoint(f"{self.workspace}/../escape.sh")
        non_exec = self.workspace / "not_exec.sh"
        non_exec.write_text("#!/bin/sh\n", encoding="utf-8")
        non_exec.chmod(0o600)
        with self.assertRaises(DispatchError):
            validate_entrypoint(non_exec)

        # Maintained submit call passes paths including bootstrap files
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.submit.return_value = {"task_id": run.task_id, "state": "queued"}
        mock_backend.run.return_value = (0, {"state": "running"}, "")
        run.launcher_backend = mock_backend
        run.start()
        mock_backend.submit.assert_called_once()
        _, kwargs = mock_backend.submit.call_args
        paths = kwargs.get("paths")
        self.assertIn(str(run.first_action_file), paths)
        self.assertIn(str(run.start_ack_file), paths)
        self.assertIn(str(run.outcome_file), paths)

    # 13. Lifecycle: workload liveness vs caller thread exit
    def test_lifecycle_native_workload_vs_caller_thread(self):
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.submit.return_value = {"task_id": "job-life", "state": "queued"}
        mock_backend.get_task_binding.side_effect = lambda tid: {
            "session_id": "55555555-5555-5555-5555-555555555555",
            "tag": f"task-{tid}",
            "workspace": str(self.workspace),
            "engine": "antigravity",
            "created_at_ms": 123456789,
        }
        mock_backend.run.return_value = (
            0,
            {
                "state": "running",
                "first_action_verified": True,
                "session_id": "55555555-5555-5555-5555-555555555555",
                "tag": "task-hub-role-writer-run",
                "workspace": str(self.workspace),
                "engine": "antigravity",
            },
            "",
        )
        # Session is alive initially
        mock_backend.is_session_alive.return_value = (True, "active")

        run = RoleRun(
            job_id="job-life",
            job_token="token",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-18"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
            launcher_backend=mock_backend,
        )
        run.start()
        # Give launcher thread a moment to finish run()
        time.sleep(0.05)
        self.assertFalse(run._thread.is_alive())

        # Workload is still running in background, no outcome file yet:
        # wait_done must return False (does NOT return True just because caller thread ended!)
        done = run.wait_done(timeout=0.05)
        self.assertFalse(done)

        # Worker writes verified outcome artifact
        run.outcome_file.write_text(json.dumps({
            "status": "succeeded",
            "summary": "Completed writer-run successfully",
            "evidence_refs": ["ref-1"],
        }), encoding="utf-8")

        # Now wait_done returns True and status is succeeded
        done = run.wait_done(timeout=0.1)
        self.assertTrue(done)
        self.assertEqual(run.outcome().get("status"), "succeeded")

        # Case: workload exits WITHOUT writing outcome file -> outcome_artifact_missing failure
        run2 = RoleRun(
            job_id="job-life-fail",
            job_token="token",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-18"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
            launcher_backend=mock_backend,
        )
        run2.start()
        time.sleep(0.05)
        # Workload exits in background
        mock_backend.is_session_alive.return_value = (False, "exited")
        done = run2.wait_done(timeout=0.1)
        self.assertTrue(done)
        self.assertEqual(run2.outcome().get("status"), "failed")
        self.assertIn("missing_or_invalid_outcome_artifact", run2.outcome().get("summary", ""))

    # 14. Cancellation containment verification
    def test_cancellation_containment_verification(self):
        mock_backend = MagicMock(spec=LauncherBackend)

        # Case A: Caller thread finished, receipt was running, but native session was not bound
        mock_backend.submit.return_value = {"task_id": "job-cancel-unbound", "state": "queued"}
        mock_backend.get_task_binding.return_value = None
        mock_backend.run.return_value = (0, {"state": "running"}, "")
        run = RoleRun(
            job_id="job-cancel-unbound",
            job_token="token",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-18"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
            launcher_backend=mock_backend,
        )
        run.start()
        time.sleep(0.05)
        self.assertIsNone(run.native_session)

        # Cancel fails closed because containment cannot be proven
        res = run.cancel(reason="test revocation")
        self.assertFalse(res["verified"])
        self.assertIn("unbound_native_session", str(res.get("detail", {})))

        # Case B: Prelaunch refusal (e.g. check_resources failed before child process started)
        mock_backend_refusal = MagicMock(spec=LauncherBackend)
        mock_backend_refusal.submit.return_value = {"task_id": "job-cancel-refusal", "state": "queued"}
        mock_backend_refusal.run.return_value = (
            1,
            {"state": "failed", "outcome": "resource-check-failed"},
            "ResourceCheckFailed: tmpdir must resolve under owned .local/tmp",
        )
        run_refusal = RoleRun(
            job_id="job-cancel-refusal",
            job_token="token",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-18"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
            launcher_backend=mock_backend_refusal,
        )
        run_refusal.start()
        time.sleep(0.05)
        self.assertTrue(getattr(run_refusal, "_prelaunch_refusal", False))

        # Prelaunch refusal is safely verified without native stop
        res_ref = run_refusal.cancel(reason="prelaunch cancel")
        self.assertTrue(res_ref["verified"])
        self.assertTrue(res_ref["detail"].get("prelaunch_refusal"))

        # Case C: Bound native session verified stop
        mock_backend_bound = MagicMock(spec=LauncherBackend)
        mock_backend_bound.submit.return_value = {"task_id": "job-cancel-bound", "state": "queued"}
        mock_backend_bound.get_task_binding.side_effect = lambda tid: {
            "session_id": "66666666-6666-6666-6666-666666666666",
            "tag": f"task-{tid}",
            "workspace": str(self.workspace),
            "engine": "antigravity",
            "created_at_ms": 123456789,
        }
        mock_backend_bound.run.return_value = (
            0,
            {
                "state": "running",
                "session_id": "66666666-6666-6666-6666-666666666666",
                "tag": "task-hub-role-writer-run",
                "workspace": str(self.workspace),
                "engine": "antigravity",
            },
            "",
        )
        mock_backend_bound.stop_session.return_value = (True, "stopped_session", {"state": "exited"})
        run_bound = RoleRun(
            job_id="job-cancel-bound",
            job_token="token",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-18"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
            launcher_backend=mock_backend_bound,
        )
        run_bound.start()
        time.sleep(0.05)

        res_bound = run_bound.cancel(reason="revoked")
        self.assertTrue(res_bound["verified"])
        mock_backend_bound.stop_session.assert_called_once()

    # 15. TMPDIR permissions fail closed and intra-root symlink rejection
    def test_ensure_workspace_tmp_dir_permissions_fail_closed_and_intra_root_symlink(self):
        # 1. Unqualified permissions on existing directory fail closed
        bad_perm_ws = self.base / "bad_perm_ws"
        bad_perm_ws.mkdir(parents=True)
        bad_tmp = bad_perm_ws / ".local" / "tmp"
        bad_tmp.mkdir(parents=True, mode=0o777)
        # Explicitly chmod because umask might reduce 0777
        os.chmod(bad_tmp, 0o777)
        with self.assertRaises(DispatchError) as ctx:
            ensure_workspace_tmp_dir(bad_perm_ws, "task-1")
        self.assertIn("tmpdir_unqualified_permissions", str(ctx.exception))

        # 2. Intra-root symlink on task_dir is rejected before resolve
        good_ws = self.base / "good_ws"
        good_ws.mkdir(parents=True)
        os.chmod(good_ws, 0o700)
        local_dir = good_ws / ".local"
        local_dir.mkdir(parents=True, mode=0o700)
        os.chmod(local_dir, 0o700)
        owned_tmp = local_dir / "tmp"
        owned_tmp.mkdir(parents=True, mode=0o700)
        os.chmod(owned_tmp, 0o700)
        real_target = owned_tmp / "real-dir"
        real_target.mkdir(mode=0o700)
        os.chmod(real_target, 0o700)
        symlink_task = owned_tmp / "sym-task"
        symlink_task.symlink_to(real_target)

        with self.assertRaises(DispatchError) as ctx_sym:
            ensure_workspace_tmp_dir(good_ws, "sym-task")
        self.assertIn("tmpdir_job_dir_is_symlink", str(ctx_sym.exception))

    # 16. Honest native_cgroup isolation and fabricated namespace rejection
    def test_honest_isolation_schema_and_fabricated_namespace_rejection(self):
        proc = subprocess.Popen(["sleep", "5"])
        try:
            pid = proc.pid
            # Honest native_cgroup schema:
            # 1. Uncorroborated synthetic UUID without live aplexer session returns None
            native_ev = {
                "pid": pid,
                "job_id": "job-iso-1",
                "epoch": 1,
                "role_name": "writer",
                "generation": 1,
                "stage": "first_action",
                "native_session": "77777777-7777-7777-7777-777777777777",
                "utc": "2026-10-18T00:00:00Z",
                "isolation_kind": "native_cgroup",
            }
            digest = get_role_first_action_digest(
                json.dumps(native_ev),
                expected_job_id="job-iso-1",
                expected_epoch=1,
                expected_role_name="writer",
                expected_generation=1,
            )
            self.assertIsNone(digest)

            # 2. With a valid disposable test fixture:
            # Valid workload PID matching workload_pid and cgroup is accepted
            cg_list = read_proc_cgroups(pid)
            cgroup_path = cg_list[0][2] if cg_list else "/test.scope"
            valid_fixture = {
                "workload_pid": pid,
                "worker_pid": 999998,
                "workload_cgroup": cgroup_path,
                "phase": "running",
                "containment_empty": False,
                "created_at_ms": int(time.time() * 1000) - 1000,
                "is_fixture": True,
            }
            digest_valid = get_role_first_action_digest(
                json.dumps(native_ev),
                expected_job_id="job-iso-1",
                expected_epoch=1,
                expected_role_name="writer",
                expected_generation=1,
                nonproduction_fixture=valid_fixture,
            )
            self.assertIsNotNone(digest_valid)
            self.assertEqual(len(digest_valid), 64)

            # 3. Supervisor PID is refused even with correct session binding
            sup_fixture = dict(valid_fixture, worker_pid=pid)
            self.assertIsNone(get_role_first_action_digest(
                json.dumps(native_ev),
                expected_job_id="job-iso-1",
                expected_epoch=1,
                expected_role_name="writer",
                expected_generation=1,
                nonproduction_fixture=sup_fixture,
            ))

            # 4. Outside arbitrary PID is refused
            outside_fixture = dict(valid_fixture, workload_pid=pid + 100000)
            self.assertIsNone(get_role_first_action_digest(
                json.dumps(native_ev),
                expected_job_id="job-iso-1",
                expected_epoch=1,
                expected_role_name="writer",
                expected_generation=1,
                nonproduction_fixture=outside_fixture,
            ))

            # 5. Wrong / non-matching cgroup is refused
            wrong_cg_fixture = dict(valid_fixture, workload_cgroup="/completely/different.scope")
            self.assertIsNone(get_role_first_action_digest(
                json.dumps(native_ev),
                expected_job_id="job-iso-1",
                expected_epoch=1,
                expected_role_name="writer",
                expected_generation=1,
                nonproduction_fixture=wrong_cg_fixture,
            ))

            # 6. Dead PID is refused
            dead_ev = dict(native_ev, pid=999999)
            self.assertIsNone(get_role_first_action_digest(
                json.dumps(dead_ev),
                expected_job_id="job-iso-1",
                expected_epoch=1,
                expected_role_name="writer",
                expected_generation=1,
                nonproduction_fixture=valid_fixture,
            ))

            # Fabricated private PID namespace matching host init namespace is rejected
            try:
                host_init_ns = os.readlink("/proc/1/ns/pid")
                fabricated_ev = {
                    "pid": pid,
                    "job_id": "job-iso-2",
                    "epoch": 1,
                    "role_name": "writer",
                    "generation": 1,
                    "stage": "first_action",
                    "native_session": "77777777-7777-7777-7777-777777777777",
                    "utc": "2026-10-18T00:00:00Z",
                    "isolation_kind": "private_pid_namespace",
                    "private_pid_namespace": host_init_ns,
                    "numeric_proc_entries": [pid],
                }
                digest_fab = get_role_first_action_digest(
                    json.dumps(fabricated_ev),
                    expected_job_id="job-iso-2",
                    expected_epoch=1,
                    expected_role_name="writer",
                    expected_generation=1,
                )
                self.assertIsNone(digest_fab)
            except OSError:
                pass  # If /proc/1/ns/pid is unreadable in test container
        finally:
            proc.terminate()
            proc.wait()

    # 17. is_session_alive: pruned/forgotten is uncertain, confirmed terminal requires exit proof
    def test_is_session_alive_uncertain_on_pruned_and_confirmed_on_tombstone(self):
        backend = LauncherBackend(launcher_repo=self.launcher_repo, config_dir=self.state_dir)
        target = role_dispatch.NativeTarget(
            session_id="88888888-8888-8888-8888-888888888888",
            tag="task-test",
            workspace=str(self.workspace),
            created_at_ms=1000,
            engine="antigravity",
        )

        with patch("agent_hub_host.native_sessions.status_of") as mock_status:
            # "pruned" without exit info does NOT prove death: returns alive=True, uncertain=True
            mock_status.return_value = {"phase": "pruned", "exit_info": None}
            alive, info = backend.is_session_alive(target)
            self.assertTrue(alive)
            self.assertTrue(info.get("uncertain"))

            # "forgotten" without exit info does NOT prove death
            mock_status.return_value = {"phase": "forgotten", "exit_info": None}
            alive_f, info_f = backend.is_session_alive(target)
            self.assertTrue(alive_f)
            self.assertTrue(info_f.get("uncertain"))

            # Confirmed terminal phase "exited" with exit_info proves death
            mock_status.return_value = {"phase": "exited", "exit_info": {"code": 0}, "state": "exited"}
            alive_ex, info_ex = backend.is_session_alive(target)
            self.assertFalse(alive_ex)
            self.assertEqual(info_ex.get("phase"), "exited")


    # 18. Start callback required before begin
    def test_dispatch_rejects_missing_or_noncallable_start_callback_before_begin(self):
        adapter = RoleDispatchAdapter(config=self.role_cfg)
        with self.assertRaises(DispatchError) as ctx_none:
            adapter.dispatch(
                job_id="job-no-cb",
                kind="writer-run",
                payload={"date": "2026-10-18"},
                job_token="tok-1",
                lease_epoch=1,
                role_binding=self.binding,
                hub_url="https://hub.example.com",
                host_id="host-test",
                start_callback=None,
            )
        self.assertIn("missing_or_invalid_start_callback", str(ctx_none.exception))

        with self.assertRaises(DispatchError) as ctx_non_callable:
            adapter.dispatch(
                job_id="job-bad-cb",
                kind="writer-run",
                payload={"date": "2026-10-18"},
                job_token="tok-2",
                lease_epoch=1,
                role_binding=self.binding,
                hub_url="https://hub.example.com",
                host_id="host-test",
                start_callback="not-callable",
            )
        self.assertIn("missing_or_invalid_start_callback", str(ctx_non_callable.exception))

    # 19. Defense: acknowledge_start itself rejects invalid, non-started, or error receipts
    def test_acknowledge_start_defensive_validation(self):
        run = RoleRun(
            job_id="job-ack-defense",
            job_token="tok-ack",
            lease_epoch=1,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-18"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        self.assertFalse(run.start_ack_file.exists())

        # Non-dict
        with self.assertRaises(DispatchError) as ctx1:
            run.acknowledge_start(None)
        self.assertIn("invalid_start_receipt", str(ctx1.exception))
        self.assertFalse(run.start_ack_file.exists())

        with self.assertRaises(DispatchError) as ctx1b:
            run.acknowledge_start("started")
        self.assertIn("invalid_start_receipt", str(ctx1b.exception))
        self.assertFalse(run.start_ack_file.exists())

        # Refused status
        with self.assertRaises(DispatchError) as ctx2:
            run.acknowledge_start({"status": "refused", "error": "timeout"})
        self.assertIn("invalid_start_receipt_status", str(ctx2.exception))
        self.assertFalse(run.start_ack_file.exists())

        # Error field present
        with self.assertRaises(DispatchError) as ctx3:
            run.acknowledge_start({"status": "started", "error": "server_rejected"})
        self.assertIn("start_receipt_contains_error", str(ctx3.exception))
        self.assertFalse(run.start_ack_file.exists())

        # Cancelled run rejects
        run._cancelled = True
        with self.assertRaises(DispatchError) as ctx4:
            run.acknowledge_start({"status": "started"})
        self.assertIn("cannot_acknowledge_start", str(ctx4.exception))
        self.assertFalse(run.start_ack_file.exists())

        # Valid start receipt writes start_ack_file
        run._cancelled = False
        run.acknowledge_start({"status": "started", "acknowledged_by": "hub"})
        self.assertTrue(run.start_ack_file.exists())
        data, _, _ = safe_read_bounded_json(run.start_ack_file)
        self.assertIsNotNone(data)
        self.assertEqual(data.get("status"), "started")
        self.assertEqual(data.get("receipt", {}).get("status"), "started")

    # 20. Controlled executor: refused callback with verified cancellation never creates ack, never executes business, cancels once
    def test_controlled_executor_refused_start_verified_cancel_never_acks_and_cancels_once(self):
        sess_id = "99999999-9999-9999-9999-999999999999"
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {"mode": "dry-run", "chosen": "antigravity"}
        mock_backend.submit.return_value = {"task_id": "task-test", "state": "queued"}
        mock_backend.stop_session.return_value = (True, "stopped_session", {"state": "exited"})

        proc = subprocess.Popen(["sleep", "60"])
        business_executed = []
        ack_file_existed = []

        try:
            def controlled_executor(ctx):
                # 1. Write first action
                fa_path = pathlib.Path(ctx["first_action_file"])
                ack_path = pathlib.Path(ctx["start_ack_file"])
                fa_path.parent.mkdir(parents=True, exist_ok=True)
                fa_path.write_text(json.dumps({
                    "pid": proc.pid,
                    "job_id": ctx["job_id"],
                    "epoch": 3,
                    "role_name": "automation:demo:writer",
                    "generation": 2,
                    "stage": "first_action",
                    "native_session": sess_id,
                    "utc": "2026-10-18T00:00:00Z",
                    "isolation_kind": "native_cgroup",
                }), encoding="utf-8")

                # 2. Start barrier: wait for start_ack_file
                start_w = time.time()
                while time.time() - start_w < 1.0:
                    if ack_path.exists():
                        ack_file_existed.append(True)
                        data, _, _ = safe_read_bounded_json(ack_path)
                        if data and data.get("status") == "started":
                            business_executed.append(True)
                            out_p = pathlib.Path(ctx["outcome_file"])
                            out_p.write_text(json.dumps({"status": "succeeded", "summary": "business done"}), encoding="utf-8")
                            return {"status": "succeeded"}
                    time.sleep(0.02)
                return {"status": "failed", "business_executed": False}

            # Injected fixture spawns no native workload of its own; declare
            # verified containment explicitly so cancel() does not depend on
            # this polling thread exiting within the join window.
            controlled_executor.cancel_verified = True

            cg_list = read_proc_cgroups(proc.pid)
            cgroup_path = cg_list[0][2] if cg_list else "/test.scope"
            mock_backend.is_fixture_only = True
            mock_backend.get_task_binding.return_value = {
                "native_id": sess_id,
                "session_id": sess_id,
                "tag": "task-hub-role-writer-run-job-refused-v-g2-e3",
                "workspace": str(self.workspace),
                "engine": "antigravity",
                "created_at_ms": int(time.time() * 1000) - 2000,
                "workload_pid": proc.pid,
                "worker_pid": 999998,
                "workload_cgroup": cgroup_path,
                "containment_empty": False,
                "phase": "running",
            }

            adapter = RoleDispatchAdapter(
                config=self.role_cfg,
                launcher_backend=mock_backend,
                launcher_executor=controlled_executor,
            )

            # Refused server start callback
            def start_callback(ev):
                return {"status": "refused", "error": "server_start_timeout"}

            # Track cancel on run
            original_begin = adapter.begin
            captured_run = []
            def track_begin(*args, **kwargs):
                r = original_begin(*args, **kwargs)
                captured_run.append(r)
                return r
            adapter.begin = track_begin

            res = adapter.dispatch(
                job_id="job-refused-v",
                kind="writer-run",
                payload={"date": "2026-10-18"},
                job_token="tok-refused",
                lease_epoch=3,
                role_binding=self.binding,
                hub_url="https://hub.example.com",
                host_id="host-test",
                start_callback=start_callback,
            )

            # Result is honest failed
            self.assertEqual(res["status"], "failed")
            self.assertIn("server_start_refused", res["summary"])

            # start_ack_file NEVER existed and does not exist
            self.assertEqual(len(ack_file_existed), 0)
            self.assertFalse(captured_run[0].start_ack_file.exists())

            # Business logic NEVER executed
            self.assertEqual(len(business_executed), 0)

            # Cancelled exactly ONCE
            self.assertEqual(captured_run[0]._cancel_count, 1)
        finally:
            proc.kill()
            proc.wait()

    # 21. Controlled executor: refused callback with unverified cancellation raises DispatchError for HOLD
    def test_controlled_executor_refused_start_unverified_cancel_raises_dispatch_error_for_hold(self):
        sess_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {"mode": "dry-run", "chosen": "antigravity"}
        mock_backend.submit.return_value = {"task_id": "task-test", "state": "queued"}
        # Unverified containment stop
        mock_backend.stop_session.return_value = (False, "uncertain_containment", {"uncertain": True})

        proc = subprocess.Popen(["sleep", "60"])
        business_executed = []
        ack_file_existed = []

        try:
            def controlled_executor(ctx):
                fa_path = pathlib.Path(ctx["first_action_file"])
                ack_path = pathlib.Path(ctx["start_ack_file"])
                fa_path.parent.mkdir(parents=True, exist_ok=True)
                fa_path.write_text(json.dumps({
                    "pid": proc.pid,
                    "job_id": ctx["job_id"],
                    "epoch": 3,
                    "role_name": "automation:demo:writer",
                    "generation": 2,
                    "stage": "first_action",
                    "native_session": sess_id,
                    "utc": "2026-10-18T00:00:00Z",
                    "isolation_kind": "native_cgroup",
                }), encoding="utf-8")

                start_w = time.time()
                while time.time() - start_w < 1.0:
                    if ack_path.exists():
                        ack_file_existed.append(True)
                        business_executed.append(True)
                        return {"status": "succeeded"}
                    time.sleep(0.02)
                return {"status": "failed", "cancel_verified": False}

            controlled_executor.cancel_verified = False

            cg_list = read_proc_cgroups(proc.pid)
            cgroup_path = cg_list[0][2] if cg_list else "/test.scope"
            mock_backend.is_fixture_only = True
            mock_backend.get_task_binding.return_value = {
                "native_id": sess_id,
                "session_id": sess_id,
                "tag": "task-hub-role-writer-run-job-refused-u-g2-e3",
                "workspace": str(self.workspace),
                "engine": "antigravity",
                "created_at_ms": int(time.time() * 1000) - 2000,
                "workload_pid": proc.pid,
                "worker_pid": 999998,
                "workload_cgroup": cgroup_path,
                "containment_empty": False,
                "phase": "running",
            }

            adapter = RoleDispatchAdapter(
                config=self.role_cfg,
                launcher_backend=mock_backend,
                launcher_executor=controlled_executor,
            )

            original_begin = adapter.begin
            captured_run = []
            def track_begin(*args, **kwargs):
                r = original_begin(*args, **kwargs)
                captured_run.append(r)
                return r
            adapter.begin = track_begin

            with self.assertRaises(DispatchError) as ctx_err:
                adapter.dispatch(
                    job_id="job-refused-u",
                    kind="writer-run",
                    payload={"date": "2026-10-18"},
                    job_token="tok-refused-u",
                    lease_epoch=3,
                    role_binding=self.binding,
                    hub_url="https://hub.example.com",
                    host_id="host-test",
                    start_callback=lambda ev: {"status": "refused", "error": "host_refusal"},
                )

            self.assertIn("server_start_failed_containment_unverified", str(ctx_err.exception))

            # Never created ack
            self.assertEqual(len(ack_file_existed), 0)
            self.assertFalse(captured_run[0].start_ack_file.exists())

            # Never executed business
            self.assertEqual(len(business_executed), 0)

            # Cancelled once
            self.assertEqual(captured_run[0]._cancel_count, 1)
        finally:
            proc.kill()
            proc.wait()

    # 22. Controlled executor: start_callback None, bad shape, or exception never creates ack and cancels once
    def test_controlled_executor_callback_none_badshape_exception_handling(self):
        sess_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {"mode": "dry-run", "chosen": "antigravity"}
        mock_backend.submit.return_value = {"task_id": "task-test", "state": "queued"}
        mock_backend.stop_session.return_value = (True, "stopped_session", {"state": "exited"})

        proc = subprocess.Popen(["sleep", "60"])
        try:
            def controlled_executor(ctx):
                fa_path = pathlib.Path(ctx["first_action_file"])
                fa_path.parent.mkdir(parents=True, exist_ok=True)
                fa_path.write_text(json.dumps({
                    "pid": proc.pid,
                    "job_id": ctx["job_id"],
                    "epoch": 3,
                    "role_name": "automation:demo:writer",
                    "generation": 2,
                    "stage": "first_action",
                    "native_session": sess_id,
                    "utc": "2026-10-18T00:00:00Z",
                    "isolation_kind": "native_cgroup",
                }), encoding="utf-8")
                return {"status": "failed"}

            cg_list = read_proc_cgroups(proc.pid)
            cgroup_path = cg_list[0][2] if cg_list else "/test.scope"
            mock_backend.is_fixture_only = True
            for case_idx, bad_callback in enumerate([
                lambda ev: None,                                        # None reply
                lambda ev: {"status": "unknown"},                      # bad status shape
                lambda ev: "started",                                  # non-dict shape
                lambda ev: (_ for _ in ()).throw(RuntimeError("net")),  # exception
            ]):
                job_id = f"job-badcb-{case_idx}"
                mock_backend.get_task_binding.return_value = {
                    "native_id": sess_id,
                    "session_id": sess_id,
                    "tag": f"task-hub-role-writer-run-{job_id}-g2-e3",
                    "workspace": str(self.workspace),
                    "engine": "antigravity",
                    "created_at_ms": int(time.time() * 1000) - 2000,
                    "workload_pid": proc.pid,
                    "worker_pid": 999998,
                    "workload_cgroup": cgroup_path,
                    "containment_empty": False,
                    "phase": "running",
                }
                adapter = RoleDispatchAdapter(
                    config=self.role_cfg,
                    launcher_backend=mock_backend,
                    launcher_executor=controlled_executor,
                )

                captured_run = []
                orig_begin = adapter.begin
                def track_b(*args, **kwargs):
                    r = orig_begin(*args, **kwargs)
                    captured_run.append(r)
                    return r
                adapter.begin = track_b

                res = adapter.dispatch(
                    job_id=job_id,
                    kind="writer-run",
                    payload={"date": "2026-10-18"},
                    job_token=f"tok-{case_idx}",
                    lease_epoch=3,
                    role_binding=self.binding,
                    hub_url="https://hub.example.com",
                    host_id="host-test",
                    start_callback=bad_callback,
                )
                self.assertEqual(res["status"], "failed")
                self.assertFalse(captured_run[0].start_ack_file.exists())
                self.assertEqual(captured_run[0]._cancel_count, 1)
        finally:
            proc.kill()
            proc.wait()

    # 23. Controlled executor: explicit started callback writes real ack and executes business
    def test_controlled_executor_explicit_started_writes_real_ack_and_executes_business(self):
        sess_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {"mode": "dry-run", "chosen": "antigravity"}
        mock_backend.submit.return_value = {"task_id": "task-test", "state": "queued"}

        proc = subprocess.Popen(["sleep", "60"])
        business_executed = []

        try:
            def controlled_executor(ctx):
                fa_path = pathlib.Path(ctx["first_action_file"])
                ack_path = pathlib.Path(ctx["start_ack_file"])
                out_path = pathlib.Path(ctx["outcome_file"])
                fa_path.parent.mkdir(parents=True, exist_ok=True)
                fa_path.write_text(json.dumps({
                    "pid": proc.pid,
                    "job_id": ctx["job_id"],
                    "epoch": 3,
                    "role_name": "automation:demo:writer",
                    "generation": 2,
                    "stage": "first_action",
                    "native_session": sess_id,
                    "utc": "2026-10-18T00:00:00Z",
                    "isolation_kind": "native_cgroup",
                }), encoding="utf-8")

                # Wait for start barrier
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if ack_path.exists():
                        data, _, _ = safe_read_bounded_json(ack_path)
                        if data and data.get("status") == "started":
                            business_executed.append(True)
                            out_path.write_text(json.dumps({
                                "status": "succeeded",
                                "summary": "article drafted after real start acknowledgement",
                                "evidence_refs": [str(out_path)],
                            }), encoding="utf-8")
                            return {"status": "succeeded"}
                    time.sleep(0.02)
                return {"status": "failed", "error": "ack_timeout"}

            job_id = "job-started-success"
            cg_list = read_proc_cgroups(proc.pid)
            cgroup_path = cg_list[0][2] if cg_list else "/test.scope"
            mock_backend.is_fixture_only = True
            mock_backend.get_task_binding.return_value = {
                "native_id": sess_id,
                "session_id": sess_id,
                "tag": f"task-hub-role-writer-run-{job_id}-g2-e3",
                "workspace": str(self.workspace),
                "engine": "antigravity",
                "created_at_ms": int(time.time() * 1000) - 2000,
                "workload_pid": proc.pid,
                "worker_pid": 999998,
                "workload_cgroup": cgroup_path,
                "containment_empty": False,
                "phase": "running",
            }

            adapter = RoleDispatchAdapter(
                config=self.role_cfg,
                launcher_backend=mock_backend,
                launcher_executor=controlled_executor,
            )

            captured_run = []
            orig_begin = adapter.begin
            def track_b(*args, **kwargs):
                r = orig_begin(*args, **kwargs)
                captured_run.append(r)
                return r
            adapter.begin = track_b

            res = adapter.dispatch(
                job_id=job_id,
                kind="writer-run",
                payload={"date": "2026-10-18"},
                job_token="tok-success",
                lease_epoch=3,
                role_binding=self.binding,
                hub_url="https://hub.example.com",
                host_id="host-test",
                start_callback=lambda ev: {"status": "started", "acknowledged_by": "hub_server"},
            )

            # Successfully completed
            self.assertEqual(res["status"], "succeeded")
            self.assertIn("article drafted after real start acknowledgement", res["summary"])

            # Real start_ack_file exists
            self.assertTrue(captured_run[0].start_ack_file.exists())
            ack_data, _, _ = safe_read_bounded_json(captured_run[0].start_ack_file)
            self.assertEqual(ack_data.get("status"), "started")
            self.assertEqual(ack_data.get("receipt", {}).get("status"), "started")

            # Business logic executed
            self.assertEqual(len(business_executed), 1)

            # cancel was NOT called
            self.assertEqual(captured_run[0]._cancel_count, 0)
        finally:
            proc.kill()
            proc.wait()


class TestRoleQualificationProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        self.launcher_repo = self.base / "launcher-repo"
        (self.launcher_repo / "launcher").mkdir(parents=True)
        (self.launcher_repo / "launcher" / "__init__.py").write_text("", encoding="utf-8")

        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                    launcher_model="gemini-3.1-pro-high",
                    launcher_task_profile="model-task",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=self.launcher_repo,
            allowed_workspaces=[self.workspace],
            allowed_models=["gemini-3.1-pro-high"],
            allowed_accounts=["antigravity"],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_bare_injected_quota_checker_without_launcher_plan_is_fixture_only(self):
        empty_repo = self.base / "empty-repo"
        empty_repo.mkdir()
        cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=empty_repo,
        )
        qual = RoleQualification(
            config=cfg,
            quota_checker=lambda: (True, "ok"),
        )
        rep = qual.inspect_profile("writer-run")
        self.assertTrue(rep["supported"])
        self.assertTrue(rep["is_fixture_only"])
        self.assertFalse(rep["operational_qualified"])
        self.assertFalse(rep["plan_ran"])

    def test_mock_backend_is_fixture_only(self):
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {
            "mode": "dry-run",
            "chosen": "antigravity",
            "eligible": [{"provider": "antigravity", "model": "gemini-3.1-pro-high"}],
        }
        qual = RoleQualification(
            config=self.role_cfg,
            backend=mock_backend,
        )
        rep = qual.inspect_profile("writer-run")
        self.assertTrue(rep["supported"])
        self.assertTrue(rep["is_fixture_only"])
        self.assertFalse(rep["operational_qualified"])
        self.assertTrue(rep["plan_admitted"])

    def test_route_mismatch_rejected(self):
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {
            "mode": "dry-run",
            "chosen": {"provider": "zai", "model": "glm-4"},
        }
        qual = RoleQualification(
            config=self.role_cfg,
            backend=mock_backend,
        )
        rep = qual.inspect_profile("writer-run")
        self.assertFalse(rep["supported"])
        self.assertIn("route_mismatch", "; ".join(rep["reasons"]))

    def test_model_mismatch_rejected(self):
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {
            "mode": "dry-run",
            "chosen": {"provider": "antigravity", "model": "wrong-model"},
        }
        qual = RoleQualification(
            config=self.role_cfg,
            backend=mock_backend,
        )
        rep = qual.inspect_profile("writer-run")
        self.assertFalse(rep["supported"])
        self.assertIn("model_mismatch", "; ".join(rep["reasons"]))

    def test_malformed_chosen_route_rejected(self):
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {
            "mode": "dry-run",
            "chosen": 12345,
        }
        qual = RoleQualification(
            config=self.role_cfg,
            backend=mock_backend,
        )
        rep = qual.inspect_profile("writer-run")
        self.assertFalse(rep["supported"])
        self.assertIn("malformed_chosen_route", "; ".join(rep["reasons"]))

    def test_plan_error_or_rejection_rejected(self):
        mock_backend = MagicMock(spec=LauncherBackend)
        mock_backend.plan.return_value = {
            "mode": "dry-run",
            "chosen": None,
            "error": "all_models_over_quota",
        }
        qual = RoleQualification(
            config=self.role_cfg,
            backend=mock_backend,
        )
        rep = qual.inspect_profile("writer-run")
        self.assertFalse(rep["supported"])
        self.assertIn("all_models_over_quota", "; ".join(rep["reasons"]))

    def test_adapter_injected_launcher_executor_is_fixture_only(self):
        adapter = RoleDispatchAdapter(
            config=self.role_cfg,
            quota_checker=lambda: (True, "ok"),
            launcher_executor=lambda p: {"status": "ok"},
        )
        ok, reason, rep = adapter.can_process("writer-run")
        self.assertTrue(rep["is_fixture_only"])
        self.assertFalse(rep["operational_qualified"])

    def test_genuine_launcher_backend_plan_is_operational_qualified(self):
        synth_repo = self.base / "synth-launcher"
        (synth_repo / "launcher").mkdir(parents=True)
        (synth_repo / "launcher" / "__init__.py").write_text("", encoding="utf-8")
        main_py = (
            "import json, sys\n"
            "res = {'mode': 'dry-run', 'chosen': {'provider': 'antigravity', 'model': 'gemini-3.1-pro-high'}}\n"
            "json.dump(res, sys.stdout)\n"
        )
        (synth_repo / "launcher" / "__main__.py").write_text(main_py, encoding="utf-8")

        cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                    launcher_model="gemini-3.1-pro-high",
                    launcher_task_profile="model-task",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=synth_repo,
            allowed_workspaces=[self.workspace],
            allowed_models=["gemini-3.1-pro-high"],
            allowed_accounts=["antigravity"],
        )
        backend = LauncherBackend(launcher_repo=synth_repo, config_dir=self.state_dir)
        qual = RoleQualification(config=cfg, backend=backend)
        rep = qual.inspect_profile("writer-run")
        self.assertTrue(rep["supported"])
        self.assertTrue(rep["operational_qualified"])
        self.assertFalse(rep["is_fixture_only"])
        self.assertTrue(rep["plan_admitted"])
        self.assertEqual(rep["chosen"], {"provider": "antigravity", "model": "gemini-3.1-pro-high"})


class TestStrictWorkloadPIDContainmentAndProvenance(unittest.TestCase):
    """Meaningful focused tests for M8 workload PID strictness defects.

    Verifies:
    1. Valid exact live workload accepted with OS start provenance, start-ticks, and cgroup corroboration.
    2. Counterexample 1 refused: unreadable process state and start provenance (None, dead, zombie, unreadable).
    3. Counterexample 2 refused: missing trusted workload_pid, phase, and containment_empty.
    4. Counterexample 3 refused: suffix-only cgroup, ancestor cgroup, traversal, blank/root cgroup.
    5. Contradictory cgroup membership refused: discrepancy between /proc/<pid>/cgroup and cgroup.procs.
    6. PID reuse / start-ticks swap refused: start-ticks mismatch, stale start time, future start time.
    7. RoleRun prestart handshake refuses invalid receipt and never generates start_ack_file.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)
        self.proc = subprocess.Popen(["sleep", "60"])

        cg_list = read_proc_cgroups(self.proc.pid)
        self.cgroup_path = cg_list[0][2] if cg_list else "/test.scope"
        self.start_ticks = get_pid_starttime_ticks(self.proc.pid)
        self.proc_start_time = get_proc_start_time(self.proc.pid)

        self.valid_workload_info = {
            "workload_pid": self.proc.pid,
            "worker_pid": 999998,
            "workload_cgroup": self.cgroup_path,
            "containment_empty": False,
            "phase": "running",
            "created_at_ms": int(time.time() * 1000) - 2000,
            "start_ticks": self.start_ticks,
            "proc_start_time": self.proc_start_time,
            "cgroup_procs_pids": [self.proc.pid],
        }

    def tearDown(self):
        try:
            self.proc.kill()
            self.proc.wait()
        except OSError:
            pass
        self.tmp.cleanup()

    def test_valid_exact_live_workload_accepted(self):
        """Live workload with corroborated start ticks, OS provenance, active state and exact cgroup passes."""
        valid, why = validate_native_workload_pid(
            self.proc.pid,
            self.valid_workload_info,
            started_wall=time.time() - 3.0,
        )
        self.assertTrue(valid, f"Expected valid workload, got refusal: {why}")
        self.assertEqual(why, "ok")

    def test_proc_start_time_has_no_boot_truncation_bias(self):
        """Measured start provenance of a fresh process falls inside its creation window.

        Integer btime biases derived start times early by up to 1s on hosts whose
        boot instant falls late in their second; that bias falsely fails the 1s
        started-before-run tolerance and raced role bootstrap into
        bootstrap_failed_or_timeout instead of server_start_refused.
        """
        t0 = time.time()
        proc = subprocess.Popen(["sleep", "5"])
        t1 = time.time()
        try:
            measured = get_proc_start_time(proc.pid)
        finally:
            proc.kill()
            proc.wait()
        self.assertIsNotNone(measured)
        self.assertGreaterEqual(measured, t0 - 0.25)
        self.assertLessEqual(measured, t1 + 0.25)

    def test_refuses_counterexample_1_unreadable_state_and_start_provenance(self):
        """Counterexample 1: unreadable process state and start provenance fail closed."""
        # 1. Unreadable process state
        with patch("agent_hub_host.role_dispatch.get_proc_state", return_value=None):
            valid, why = validate_native_workload_pid(self.proc.pid, self.valid_workload_info)
            self.assertFalse(valid)
            self.assertEqual(why, "unreadable_proc_state")

        # 2. Dead / zombie / stopped process states
        for dead_state in ("Z (zombie)", "X (dead)", "T (stopped)", "t (tracing stop)"):
            with patch("agent_hub_host.role_dispatch.get_proc_state", return_value=dead_state):
                valid, why = validate_native_workload_pid(self.proc.pid, self.valid_workload_info)
                self.assertFalse(valid)
                self.assertTrue(
                    any(k in why for k in ("zombie", "dead", "inactive", "stopped")),
                    f"Expected dead/inactive state refusal, got: {why}",
                )

        # 3. Missing/unreadable start provenance (proc_start_time None)
        info_no_start = dict(self.valid_workload_info)
        info_no_start.pop("proc_start_time", None)
        with patch("agent_hub_host.role_dispatch.get_proc_start_time", return_value=None):
            valid, why = validate_native_workload_pid(self.proc.pid, info_no_start)
            self.assertFalse(valid)
            self.assertEqual(why, "unreadable_proc_start_time")

    def test_refuses_counterexample_2_missing_trusted_workload_pid_phase_and_containment(self):
        """Counterexample 2: missing trusted workload_pid, phase, and containment_empty fail closed."""
        # 1. Missing workload_pid
        info = dict(self.valid_workload_info)
        info.pop("workload_pid")
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertEqual(why, "missing_trusted_workload_pid")

        # 2. Missing worker_pid (supervisor)
        info = dict(self.valid_workload_info)
        info.pop("worker_pid")
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertEqual(why, "missing_trusted_worker_pid")

        # 3. Workload PID equals supervisor PID
        info = dict(self.valid_workload_info, worker_pid=self.proc.pid)
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertEqual(why, "refused_supervisor_pid")

        # 4. Missing phase
        info = dict(self.valid_workload_info)
        info.pop("phase")
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertEqual(why, "missing_phase")

        # 5. Inactive / terminal phases
        for bad_phase in ("exited", "failed", "unknown", "stopped", "terminated", "crashed"):
            info = dict(self.valid_workload_info, phase=bad_phase)
            valid, why = validate_native_workload_pid(self.proc.pid, info)
            self.assertFalse(valid)
            self.assertTrue(
                any(k in why for k in ("terminal_phase", "unknown_phase", "inactive_phase", "non_active_phase")),
                f"Expected inactive phase refusal, got: {why}",
            )

        # 6. Missing containment_empty or containment_empty is True
        info = dict(self.valid_workload_info)
        info.pop("containment_empty")
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertEqual(why, "missing_or_unknown_containment_empty")

        info = dict(self.valid_workload_info, containment_empty=True)
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertEqual(why, "containment_empty")

    def test_refuses_counterexample_3_suffix_only_and_ancestor_and_traversal_cgroups(self):
        """Counterexample 3: suffix-only matches, ancestor scopes, traversals, and blank/root fail closed."""
        # 1. Suffix-only cgroup match is rejected
        suffix_scope = "/scope" if not self.cgroup_path.endswith("/scope") else "/different.scope"
        self.assertFalse(is_pid_in_cgroup(self.proc.pid, suffix_scope))
        info = dict(self.valid_workload_info, workload_cgroup=suffix_scope)
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertIn("pid_not_in_workload_cgroup", why)

        # 2. Ancestor cgroup match is rejected (e.g. /user.slice)
        ancestor_scope = "/user.slice"
        if normalize_cgroup_path(self.cgroup_path) != ancestor_scope:
            self.assertFalse(is_pid_in_cgroup(self.proc.pid, ancestor_scope))
            info = dict(self.valid_workload_info, workload_cgroup=ancestor_scope)
            valid, why = validate_native_workload_pid(self.proc.pid, info)
            self.assertFalse(valid)
            self.assertIn("pid_not_in_workload_cgroup", why)

        # 3. Path traversal attempts are rejected
        self.assertIsNone(normalize_cgroup_path("/../test.scope"))
        self.assertIsNone(normalize_cgroup_path("/user.slice/../../test.scope"))
        info = dict(self.valid_workload_info, workload_cgroup="/../test.scope")
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertIn("invalid_workload_cgroup", why)

        # 4. Root or empty/blank cgroups are rejected
        for bad_cg in ("", "   ", "/", "/sys/fs/cgroup", "/sys/fs/cgroup/"):
            self.assertIsNone(normalize_cgroup_path(bad_cg))
            info = dict(self.valid_workload_info, workload_cgroup=bad_cg)
            valid, why = validate_native_workload_pid(self.proc.pid, info)
            self.assertFalse(valid)
            self.assertTrue(
                any(k in why for k in ("invalid_workload_cgroup", "missing_workload_cgroup")),
                f"Expected cgroup refusal for {bad_cg!r}, got: {why}",
            )

    def test_refuses_pid_cgroup_discrepancy_and_unreadable_membership(self):
        """Proc-vs-cgroup discrepancy between /proc/<pid>/cgroup and cgroup.procs fails closed."""
        # /proc/<pid>/cgroup matches workload_cgroup, but cgroup.procs does not contain pid
        info = dict(self.valid_workload_info, cgroup_procs_pids=[999997, 999998])
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertIn("pid_not_in_workload_cgroup", why)

    def test_refuses_pid_reuse_and_start_ticks_mismatch(self):
        """Start-ticks mismatch (PID reuse), stale start time, and future start time fail closed."""
        # 1. start_ticks mismatch indicates PID reuse
        if self.start_ticks is not None:
            info = dict(self.valid_workload_info, start_ticks=self.start_ticks + 500)
            valid, why = validate_native_workload_pid(self.proc.pid, info)
            self.assertFalse(valid)
            self.assertIn("pid_start_ticks_mismatch", why)

        # 2. Stale PID started before native session
        info = dict(self.valid_workload_info, proc_start_time=100.0, created_at_ms=200000)
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertIn("stale_pid_started_before_session", why)

        # 3. Future start time (> time.time() + 2.0)
        info = dict(self.valid_workload_info, proc_start_time=time.time() + 50.0)
        valid, why = validate_native_workload_pid(self.proc.pid, info)
        self.assertFalse(valid)
        self.assertIn("future_pid_start_time", why)

    def test_role_run_prestart_refuses_invalid_receipt_and_never_acks(self):
        """RoleRun prestart handshake refuses invalid first-action evidence and never creates start_ack_file."""
        cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                ),
            },
            state_dir=self.base / "roles",
            launcher_repo=self.base / "launcher-repo",
        )
        run = RoleRun(
            job_id="job-prestart-refuse",
            job_token="tok-1",
            lease_epoch=1,
            role_binding={
                "name": "automation:demo:writer",
                "generation": 1,
                "role_type": "writer",
                "profile": "writer-run",
            },
            kind="writer-run",
            payload={},
            hub_url="https://hub.example.com",
            host_id="host-1",
            config=cfg,
            launcher_executor=lambda ctx: {"status": "ok"},
        )
        run.native_session = "11111111-2222-3333-4444-555555555555"
        run.is_injected_test = True
        run.native_tag = "task-hub-role-writer-run-job-prestart-refuse-g1-e1"
        run.native_workspace = str(self.workspace)
        run.native_engine = "antigravity"
        run.created_at_ms = int(time.time() * 1000) - 2000

        # Write invalid first-action file (supervisor PID used instead of workload PID)
        fa_data = {
            "job_id": "job-prestart-refuse",
            "epoch": 1,
            "role_name": "automation:demo:writer",
            "generation": 1,
            "stage": "first_action",
            "pid": 999998,  # Matches worker_pid (supervisor)
            "native_session": "11111111-2222-3333-4444-555555555555",
            "utc": "2026-10-18T00:00:00Z",
            "isolation_kind": "native_cgroup",
        }
        run.first_action_file.parent.mkdir(parents=True, exist_ok=True)
        run.first_action_file.write_text(json.dumps(fa_data), encoding="utf-8")

        # Corroborated binding specifies worker_pid = 999998, workload_pid = self.proc.pid
        run.corroborate_maintained_binding = lambda: {
            "native_id": "11111111-2222-3333-4444-555555555555",
            "session_id": "11111111-2222-3333-4444-555555555555",
            "tag": "task-hub-role-writer-run-job-prestart-refuse-g1-e1",
            "workspace": str(self.workspace),
            "engine": "antigravity",
            "created_at_ms": int(time.time() * 1000) - 2000,
            "workload_pid": self.proc.pid,
            "worker_pid": 999998,
            "workload_cgroup": self.cgroup_path,
            "containment_empty": False,
            "phase": "running",
        }

        # wait_bootstrap must refuse the invalid evidence within fast timeout and never ack
        res = run.wait_bootstrap(timeout=0.2)
        self.assertIsNone(res)
        self.assertFalse(run.start_ack_file.exists())


if __name__ == "__main__":
    unittest.main()
