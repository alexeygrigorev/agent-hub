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
    get_role_first_action_digest,
    safe_read_bounded_json,
    scrub,
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


if __name__ == "__main__":
    unittest.main()
