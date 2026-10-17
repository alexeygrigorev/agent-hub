"""Focused tests for M8b Platform Role Runtime (roles, qualification, custody, and lifecycle)."""
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_hub_host import fleet, session_host
from agent_hub_host.jobs import JobDispatcher, JobsClient, SUPPORTED_ROLE_PROFILES
from agent_hub_host.role_dispatch import (
    DispatchError,
    RoleDispatchAdapter,
    RoleProfileConfig,
    RoleQualification,
    RoleRun,
    TrustedRoleConfig,
    get_role_first_action_digest,
    load_role_config,
    validate_entrypoint,
    validate_workspace,
)
from agent_hub_host.runtime import RuntimeManager


class TestRoleConfigValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_config_returns_disabled(self):
        cfg = TrustedRoleConfig.load(self.base / "nonexistent.json")
        self.assertFalse(cfg.enabled)
        self.assertEqual(len(cfg.profiles), 0)

    def test_invalid_json_raises_dispatch_error(self):
        bad = self.base / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(DispatchError) as ctx:
            TrustedRoleConfig.load(bad)
        self.assertIn("unreadable_role_config", str(ctx.exception))

    def test_symlink_config_raises_dispatch_error(self):
        target = self.base / "real.json"
        target.write_text(json.dumps({"version": 1, "enabled": True, "profiles": {}}), encoding="utf-8")
        link = self.base / "link.json"
        link.symlink_to(target)
        with self.assertRaises(DispatchError) as ctx:
            TrustedRoleConfig.load(link)
        self.assertIn("invalid_role_config_file", str(ctx.exception))

    def test_disallowed_profile_rejected(self):
        with self.assertRaises(DispatchError) as ctx:
            TrustedRoleConfig.build(
                enabled=True,
                profiles={"arbitrary-profile": RoleProfileConfig(profile="arbitrary-profile", enabled=True)},
                state_dir=str(self.base / "roles"),
            )
        self.assertIn("unsupported_role_profile", str(ctx.exception))

    def test_world_writable_entrypoint_rejected(self):
        script = self.base / "exec.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o777)
        with self.assertRaises(DispatchError) as ctx:
            validate_entrypoint(script)
        self.assertIn("world_writable", str(ctx.exception))

    def test_symlink_entrypoint_rejected(self):
        real_script = self.base / "real.sh"
        real_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real_script.chmod(0o700)
        link_script = self.base / "link.sh"
        link_script.symlink_to(real_script)
        with self.assertRaises(DispatchError) as ctx:
            validate_entrypoint(link_script)
        self.assertIn("symlink_or_unresolved", str(ctx.exception))

    def test_conflated_canonical_root_state_dir_rejected(self):
        canon = self.base / "canonical_root"
        canon.mkdir(parents=True)
        with self.assertRaises(DispatchError) as ctx:
            TrustedRoleConfig.build(
                enabled=True,
                profiles={"writer-run": RoleProfileConfig(profile="writer-run", enabled=True)},
                state_dir=str(canon),
                canonical_root_state_dir=str(canon),
            )
        self.assertIn("role_state_dir_conflated_with_canonical_root_state", str(ctx.exception))

    def test_conflated_canonical_root_subdirectory_rejected(self):
        canon = self.base / "canonical_root"
        sub = canon / "roles"
        canon.mkdir(parents=True)
        sub.mkdir(parents=True)
        with self.assertRaises(DispatchError) as ctx:
            TrustedRoleConfig.build(
                enabled=True,
                profiles={"writer-run": RoleProfileConfig(profile="writer-run", enabled=True)},
                state_dir=str(sub),
                canonical_root_state_dir=str(canon),
            )
        self.assertIn("role_state_dir_conflated_with_canonical_root_state", str(ctx.exception))

    def test_disabled_by_default(self):
        cfg = TrustedRoleConfig.disabled()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.profiles, {})


class TestRoleCustodyAndEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.subproc = subprocess.Popen(["sleep", "30"])

    def tearDown(self):
        try:
            self.subproc.terminate()
            self.subproc.wait(timeout=1.0)
        except Exception:
            pass
        self.tmp.cleanup()

    def _valid_evidence_dict(self):
        pid = self.subproc.pid
        return {
            "pid": pid,
            "job_id": "job-1",
            "epoch": 2,
            "role_name": "writer",
            "generation": 3,
            "stage": "first_action",
            "native_session": "11111111-1111-1111-1111-111111111111",
            "utc": "2026-10-17T20:00:00Z",
            "private_pid_namespace": f"pid:[{pid}]",
            "numeric_proc_entries": [pid],
        }

    def test_valid_evidence_digest_verification(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        digest = get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        )
        self.assertIsNotNone(digest)
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)

    def test_mismatched_job_id_rejected(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        data["job_id"] = "other-job"
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        digest = get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        )
        self.assertIsNone(digest)

    def test_mismatched_epoch_rejected(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        data["epoch"] = 99
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        digest = get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        )
        self.assertIsNone(digest)

    def test_mismatched_role_or_generation_rejected(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        data["role_name"] = "wrong-role"
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        ))

        data = self._valid_evidence_dict()
        data["generation"] = 999
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        ))

    def test_nil_uuid_rejected(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        data["native_session"] = "00000000-0000-0000-0000-000000000000"
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        digest = get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        )
        self.assertIsNone(digest)

    def test_caller_or_daemon_pid_rejected(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        data["pid"] = os.getpid()  # caller pid forbidden
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        ))

    def test_dead_pid_rejected(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        data["pid"] = 99999999
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        digest = get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        )
        self.assertIsNone(digest)

    def test_retired_or_tombstone_stage_rejected(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        data["stage"] = "tombstone"
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        ))
        data["stage"] = "retired"
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
        ))

    def test_stale_mtime_before_started_wall_rejected(self):
        ev_file = self.base / "first-action-job-1.json"
        data = self._valid_evidence_dict()
        ev_file.write_text(json.dumps(data), encoding="utf-8")
        future_started_wall = time.time() + 10.0
        self.assertIsNone(get_role_first_action_digest(
            ev_file,
            expected_job_id="job-1",
            expected_epoch=2,
            expected_role_name="writer",
            expected_generation=3,
            started_wall=future_started_wall,
        ))


class TestRoleLifecycleAndFaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)

        self.script = self.base / "run_role.sh"
        self.script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.script.chmod(0o700)

        self.role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    entrypoint=self.script,
                    workspace=self.workspace,
                    launcher_provider="synthetic",
                    launcher_model="synth-1",
                    launcher_task_profile="writer-run",
                ),
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    entrypoint=self.script,
                    workspace=self.workspace,
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=self.base,  # Explicit synthetic repository, never ~/git on CI.
        )
        self.runtime_mgr = RuntimeManager(role_config=self.role_cfg)
        self.client = MagicMock(spec=JobsClient)
        self.dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=self.runtime_mgr,
        )
        self.binding = {
            "name": "automation:demo:writer",
            "generation": 1,
            "epoch": 2,
            "profile": "writer-run",
            "host_id": "host-test",
            "ttl_seconds": 120,
            "project": "demo",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_unsupported_role_profile_rejected_in_handle_offer(self):
        result = self.dispatcher.handle_offer({
            "id": "job-unsupported",
            "kind": "head-recovery",  # not configured in self.role_cfg
            "role_binding": {
                "name": "automation:demo:recovery",
                "generation": 1,
                "epoch": 1,
                "profile": "head-recovery",
                "host_id": "host-test",
                "ttl_seconds": 120,
            },
        })
        self.assertFalse(result["accepted"])
        self.assertIn("HOLD: role backend unsupported", result["reason"])

    def test_missing_role_binding_rejected(self):
        result = self.dispatcher.handle_offer({
            "id": "job-no-binding",
            "kind": "writer-run",
            "payload": {"date": "2026-10-17"},
            # missing role_binding
        })
        self.assertFalse(result["accepted"])
        self.assertIn("missing_role_binding", result["reason"])

    def test_stale_or_revoked_lease_cancelled_by_stop_event(self):
        stop_event = threading.Event()
        stop_event.set()  # Already cancelled

        run = RoleRun(
            job_id="job-cancelled",
            job_token="tok",
            lease_epoch=2,
            role_binding=self.binding,
            kind="writer-run",
            payload={"date": "2026-10-17"},
            hub_url="https://hub.example.com",
            host_id="host-test",
            config=self.role_cfg,
        )
        outcome = run.cancel(reason="lease_revoked")
        self.assertTrue(outcome["verified"])
        self.assertEqual(outcome["reason"], "lease_revoked")
        res = run.outcome()
        self.assertEqual(res["status"], "failed")
        self.assertIn("lease_revoked", res["summary"])

    def test_pre_existing_evidence_file_fails_closed(self):
        runs_dir = self.runtime_mgr.role_runs_dir
        runs_dir.mkdir(parents=True, exist_ok=True)
        bad_evidence = runs_dir / "first-action-job-conflict.json"
        bad_evidence.write_text("{}", encoding="utf-8")

        job_info = {
            "job_id": "job-conflict",
            "kind": "writer-run",
            "job_token": "tok",
            "lease_epoch": 2,
            "stop_event": threading.Event(),
            "role_binding": self.binding,
        }
        self.dispatcher._run_job_lifecycle(job_info, payload={"date": "2026-10-17"})
        self.client.finish_job.assert_called_once()
        args = self.client.finish_job.call_args[0]
        self.assertEqual(args[4], "failed")
        self.assertIn("pre-existing evidence", args[5])

    def test_lost_start_reply_retains_intent_without_retry(self):
        job_info = {
            "job_id": "job-uncertain",
            "kind": "writer-run",
            "job_token": "tok",
            "lease_epoch": 2,
            "stop_event": threading.Event(),
            "role_binding": self.binding,
        }
        # Commit fails or raises network exception
        self.client.commit_role_execution.side_effect = ConnectionResetError("lost reply")
        self.dispatcher._run_job_lifecycle(job_info, payload={"date": "2026-10-17"})
        # finish_job should NOT be called (uncertain intent retained without premature finish or duplicate attempt)
        self.client.finish_job.assert_not_called()

    def test_role_generation_mismatch_rejected(self):
        # Offer has role_binding with generation 2, but claimed binding is generation 99
        self.client.claim_job.return_value = {
            "job_token": "role-token",
            "lease_epoch": 2,
            "deadline_at": "2099-01-01T00:00:00Z",
            "role_binding": {**self.binding, "generation": 99},
        }
        # The synthetic backend paths above are explicit and machine-independent.
        with patch("agent_hub_host.gate.get_gate_status", return_value={"launch_allowed": True, "profile_supported": True}):
            result = self.dispatcher.handle_offer({
                "id": "job-gen-mismatch",
                "kind": "writer-run",
                "payload": {"date": "2026-10-17"},
                "role_binding": self.binding,
            })
        self.assertFalse(result["accepted"])
        self.assertIn("binding changed", result["reason"])
        self.client.claim_job.assert_called_once()


class TestSessionHostRoleConsumerIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_role_consumer_disabled_by_default(self):
        consumer = session_host.RoleConsumer()
        res = consumer.step()
        self.assertTrue(res.get("disabled"))

    def test_role_consumer_rate_limiting(self):
        consumer = session_host.RoleConsumer(min_interval_s=60.0)
        res1 = consumer.step()
        self.assertTrue(res1.get("disabled"))
        res2 = consumer.step()
        self.assertTrue(res2.get("rate_limited"))

    def test_get_cleanup_roots_safe_extraction(self):
        self.assertEqual(session_host.get_cleanup_roots(None), [])
        self.assertEqual(session_host.get_cleanup_roots({}), [])
        self.assertEqual(session_host.get_cleanup_roots({"cleanup_roots": "not-a-list"}), [])
        valid = ["/var/run/agents", "/tmp/valid"]
        invalid = ["relative/path", "/var/null\x00byte", 123]
        res = session_host.get_cleanup_roots({"cleanup_roots": valid + invalid})
        self.assertEqual(res, valid)

    def test_session_host_cycles_with_role_consumer(self):
        journal = self.base / "journal"
        journal.mkdir(parents=True)
        roles_path = journal / "roles.json"
        roles_path.write_text(json.dumps({
            "version": 1,
            "enabled": False,
            "profiles": {}
        }), encoding="utf-8")

        with patch.object(session_host.config, "load_config", return_value={"machine_id": "test-machine", "access_token": "tok"}), \
             patch.object(session_host.fleet, "load_settings", return_value={"role_consumer": {"enabled": False}}), \
             patch.object(session_host, "qualify_sender_binding", return_value=SimpleNamespace(session_id="native-1", workspace=str(self.base))), \
             patch.object(session_host.fleet, "Dispatcher"), \
             patch.object(session_host.session_control, "execute"):
            session_host.run("synthetic-settings", str(journal), cycles=1)


if __name__ == "__main__":
    unittest.main()
