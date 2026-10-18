"""Hermetic focused tests for M8 Platform Role Job Admission and Gate Evaluation.

Validates that:
1. Genuinely role-qualified real-manager configuration does NOT call the Root-only gate.
2. All 3 supported role profiles (writer-run, principal-wake, head-recovery) qualify and claim.
3. Unsupported, disabled, malformed, fixture-only, failed quota, and failed admission roles are refused before claim.
4. Changed role bindings returned from claim are strictly rejected.
5. Explicit mock runner tests stay explicit fixtures.
6. Genuine missing Root proof still denies root-run with pending#59.
7. Completely hermetic with zero dependency on personal Launcher checkouts or dotfiles.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from agent_hub_host.guarded_root import GuardedRootBridge
from agent_hub_host.jobs import JobDispatcher, JobsClient, SUPPORTED_ROLE_PROFILES
from agent_hub_host.role_dispatch import (
    LauncherBackend,
    RoleDispatchAdapter,
    RoleProfileConfig,
    RoleQualification,
    TrustedRoleConfig,
)
from agent_hub_host.runtime import RuntimeManager


def _create_synthetic_launcher_repo(
    base_dir: pathlib.Path,
    chosen_by_role: dict[str, dict[str, str]] | None = None,
) -> pathlib.Path:
    """Create a hermetic synthetic Launcher repository with a functional 'plan' CLI."""
    repo_dir = base_dir / "synthetic-launcher-repo"
    launcher_pkg = repo_dir / "launcher"
    launcher_pkg.mkdir(parents=True, exist_ok=True)
    (launcher_pkg / "__init__.py").write_text("", encoding="utf-8")

    chosen_map = chosen_by_role or {
        "writer-run": {"provider": "antigravity", "model": "gemini-3.1-pro-high"},
        "principal-wake": {"provider": "antigravity"},
        "head-recovery": {"provider": "opencode", "model": "muse-spark-1.3-contributor"},
    }

    main_script = f"""import json, sys

def main():
    payload = {{}}
    for i, arg in enumerate(sys.argv):
        if arg == "--payload" and i + 1 < len(sys.argv):
            try:
                payload = json.loads(sys.argv[i + 1])
            except Exception:
                pass

    chosen_map = {json.dumps(chosen_map)}
    owner = payload.get("owner", "")
    chosen = None
    for kind, ch in chosen_map.items():
        if kind in owner:
            chosen = ch
            break
    if chosen is None:
        chosen = {{"provider": "antigravity"}}

    if payload.get("fail_admission"):
        res = {{"mode": "dry-run", "chosen": None, "error": "simulated_admission_refusal"}}
    else:
        res = {{"mode": "dry-run", "chosen": chosen}}
    json.dump(res, sys.stdout)

if __name__ == "__main__":
    main()
"""
    (launcher_pkg / "__main__.py").write_text(main_script, encoding="utf-8")
    return repo_dir


class TestRoleJobAdmissionRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(pathlib.Path(".local/tmp").resolve()))
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "roles"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(parents=True)

        self.synth_launcher_repo = _create_synthetic_launcher_repo(self.base)

        # Quota gate mock script
        self.quota_gate = self.base / "quota-gate.py"
        self.quota_gate.write_text(
            'import json, sys; json.dump({"launch_allowed": True, "reason": "quota ok", "windows": {}}, sys.stdout)\n',
            encoding="utf-8",
        )
        self.quota_gate.chmod(0o700)

        # Trusted configuration enabling all 3 profiles
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
                    launcher_task_profile="model-task",
                ),
                "head-recovery": RoleProfileConfig(
                    profile="head-recovery",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="opencode",
                    launcher_model="muse-spark-1.3-contributor",
                    launcher_task_profile="model-task",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=self.synth_launcher_repo,
            allowed_workspaces=[self.workspace],
            allowed_models=["gemini-3.1-pro-high", "muse-spark-1.3-contributor"],
            allowed_accounts=["antigravity", "opencode"],
        )

        self.runtime_mgr = RuntimeManager(
            role_config=self.role_cfg,
            quota_gate_script=str(self.quota_gate),
        )
        self.client = MagicMock(spec=JobsClient)
        self.dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=self.runtime_mgr,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _valid_binding(self, kind: str, generation: int = 2) -> dict:
        return {
            "name": f"automation:platform:{kind}",
            "generation": generation,
            "epoch": 2,
            "profile": kind,
            "host_id": "host-test",
            "ttl_seconds": 120,
        }

    # 1. Genuinely role-qualified real-manager does NOT call Root-only gate
    def test_genuinely_role_qualified_does_not_call_root_only_gate(self):
        self.client.claim_job.return_value = {
            "job_token": "role-tok-1",
            "lease_epoch": 2,
            "deadline_at": "2099-01-01T00:00:00Z",
            "role_binding": self._valid_binding("principal-wake"),
        }

        with patch("agent_hub_host.gate.get_gate_status") as mock_root_gate:
            res = self.dispatcher.handle_offer({
                "id": "job-wake-001",
                "kind": "principal-wake",
                "role_binding": self._valid_binding("principal-wake"),
                "payload": {"prompt": "wake"},
            })

            # Root-only gate must NEVER be called for genuinely qualified role run
            mock_root_gate.assert_not_called()

        self.assertTrue(res["accepted"], f"Expected accepted, got {res}")
        self.client.claim_job.assert_called_once()
        claim_call = self.client.claim_job.call_args
        self.assertTrue(claim_call.kwargs.get("fresh_gate"))
        gate_status = claim_call.kwargs.get("gate_status")
        self.assertTrue(gate_status.get("launch_allowed"))
        self.assertEqual(gate_status.get("reason"), "role_qualified")
        self.assertTrue(gate_status.get("profile_supported"))

    # 2. All 3 role profiles qualify and claim under genuine configuration
    def test_all_three_role_profiles_qualify_and_claim(self):
        for kind in SUPPORTED_ROLE_PROFILES:
            with self.subTest(profile=kind):
                binding = self._valid_binding(kind)
                self.client.claim_job.return_value = {
                    "job_token": f"token-{kind}",
                    "lease_epoch": 2,
                    "deadline_at": "2099-01-01T00:00:00Z",
                    "role_binding": binding,
                }
                with patch("agent_hub_host.gate.get_gate_status") as mock_gate:
                    res = self.dispatcher.handle_offer({
                        "id": f"job-{kind}-01",
                        "kind": kind,
                        "role_binding": binding,
                        "payload": {"test": True},
                    })
                    mock_gate.assert_not_called()
                self.assertTrue(res["accepted"], f"Failed for profile {kind}: {res}")

    # 3. Refusal before claim: unsupported role profile
    def test_unsupported_role_profile_refused_before_claim(self):
        # Arbitrary unsupported profile
        res = self.dispatcher.handle_offer({
            "id": "job-bad-kind",
            "kind": "arbitrary-role",
            "role_binding": {
                "name": "automation:test:arbitrary",
                "generation": 1,
                "epoch": 1,
                "profile": "arbitrary-role",
                "host_id": "host-test",
                "ttl_seconds": 120,
            },
        })
        self.assertFalse(res["accepted"])
        self.client.claim_job.assert_not_called()

    # 4. Refusal before claim: unconfigured role profile
    def test_unconfigured_role_profile_refused_before_claim(self):
        # Role config with only writer-run, leaving principal-wake unconfigured
        partial_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(profile="writer-run", enabled=True, workspace=self.workspace),
            },
            state_dir=self.state_dir,
            launcher_repo=self.synth_launcher_repo,
            allowed_workspaces=[self.workspace],
        )
        rt = RuntimeManager(role_config=partial_cfg, quota_gate_script=str(self.quota_gate))
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
        )
        res = disp.handle_offer({
            "id": "job-unconfigured-wake",
            "kind": "principal-wake",
            "role_binding": self._valid_binding("principal-wake"),
        })
        self.assertFalse(res["accepted"])
        self.assertIn("HOLD: role backend unsupported", res["reason"])
        self.client.claim_job.assert_not_called()

    # 5. Refusal before claim: disabled role configuration
    def test_disabled_role_configuration_refused_before_claim(self):
        disabled_cfg = TrustedRoleConfig.build(
            enabled=False,
            profiles={
                "principal-wake": RoleProfileConfig(profile="principal-wake", enabled=True, workspace=self.workspace),
            },
            state_dir=self.state_dir,
            launcher_repo=self.synth_launcher_repo,
            allowed_workspaces=[self.workspace],
        )
        rt = RuntimeManager(role_config=disabled_cfg, quota_gate_script=str(self.quota_gate))
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
        )
        res = disp.handle_offer({
            "id": "job-disabled-cfg",
            "kind": "principal-wake",
            "role_binding": self._valid_binding("principal-wake"),
        })
        self.assertFalse(res["accepted"])
        self.assertIn("HOLD: role backend unsupported", res["reason"])
        self.client.claim_job.assert_not_called()

    # 6. Refusal before claim: disabled role profile
    def test_disabled_profile_refused_before_claim(self):
        disabled_p_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "principal-wake": RoleProfileConfig(profile="principal-wake", enabled=False, workspace=self.workspace),
            },
            state_dir=self.state_dir,
            launcher_repo=self.synth_launcher_repo,
            allowed_workspaces=[self.workspace],
        )
        rt = RuntimeManager(role_config=disabled_p_cfg, quota_gate_script=str(self.quota_gate))
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
        )
        res = disp.handle_offer({
            "id": "job-disabled-profile",
            "kind": "principal-wake",
            "role_binding": self._valid_binding("principal-wake"),
        })
        self.assertFalse(res["accepted"])
        self.client.claim_job.assert_not_called()

    # 7. Refusal before claim: malformed role bindings
    def test_malformed_role_bindings_refused_before_claim(self):
        cases = [
            ("missing_binding", None, "missing_role_binding"),
            ("non_dict", "not-a-dict", "invalid_role_binding"),
            ("wrong_host", {**self._valid_binding("writer-run"), "host_id": "other-host"}, "invalid_role_binding"),
            ("bad_role_name_regex", {**self._valid_binding("writer-run"), "name": "bad:name"}, "invalid_role_binding"),
            ("zero_generation", {**self._valid_binding("writer-run"), "generation": 0}, "invalid_role_binding"),
            ("negative_generation", {**self._valid_binding("writer-run"), "generation": -1}, "invalid_role_binding"),
            ("bool_generation", {**self._valid_binding("writer-run"), "generation": True}, "invalid_role_binding"),
            ("ttl_too_low", {**self._valid_binding("writer-run"), "ttl_seconds": 10}, "invalid_role_binding"),
            ("ttl_too_high", {**self._valid_binding("writer-run"), "ttl_seconds": 5000}, "invalid_role_binding"),
            ("profile_mismatch", {**self._valid_binding("writer-run"), "profile": "principal-wake"}, "invalid_role_binding"),
        ]
        for name, binding, expected_reason in cases:
            with self.subTest(case=name):
                res = self.dispatcher.handle_offer({
                    "id": f"job-malformed-{name}",
                    "kind": "writer-run",
                    "role_binding": binding,
                })
                self.assertFalse(res["accepted"])
                self.assertEqual(res["reason"], expected_reason)
                self.client.claim_job.assert_not_called()

    # 8. Refusal before claim: expired offer
    def test_expired_offer_refused_before_claim(self):
        res = self.dispatcher.handle_offer({
            "id": "job-expired",
            "kind": "writer-run",
            "offer_expires_at": "2020-01-01T00:00:00Z",
            "role_binding": self._valid_binding("writer-run"),
        })
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], "offer_expired")
        self.client.claim_job.assert_not_called()

    # 9. Refusal before claim: fixture-only qualification in real operational mode
    def test_fixture_only_qualification_refused_in_operational_mode(self):
        # A synthetic repo without a launcher directory and an injected quota checker
        # is marked is_fixture_only=True and operational_qualified=False.
        empty_repo = self.base / "empty-repo-fixture"
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
        rt = RuntimeManager(
            role_config=cfg,
            adapter_kwargs={"role_quota_checker": lambda: (True, "fixture_ok")},
            quota_gate_script=str(self.quota_gate),
        )
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
        )
        # In non-mock operational dispatch, fixture-only qualification is rejected before claim
        with patch("agent_hub_host.gate.get_gate_status") as mock_gate:
            res = disp.handle_offer({
                "id": "job-fixture-rejected",
                "kind": "writer-run",
                "role_binding": self._valid_binding("writer-run"),
            })
            self.assertFalse(res["accepted"])
            self.assertIn("fixture_only_qualification_rejected", res["reason"])
            self.client.claim_job.assert_not_called()
            mock_gate.assert_not_called()

    # 10. Refusal before claim: failed quota check
    def test_failed_quota_check_refused_before_claim(self):
        rt = RuntimeManager(
            role_config=self.role_cfg,
            adapter_kwargs={"role_quota_checker": lambda: (False, "quota exhausted")},
        )
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
            root_bridge=GuardedRootBridge(state_dir=str(self.state_dir)),
        )
        eligible, reason, report = rt.can_process_role_run("writer-run")
        self.assertFalse(eligible)
        self.assertIn("quota exhausted", reason)

        res = disp.handle_offer({
            "id": "job-quota-fail",
            "kind": "writer-run",
            "role_binding": self._valid_binding("writer-run"),
        })
        self.assertFalse(res["accepted"])
        self.assertIn("HOLD: role backend unsupported", res["reason"])
        self.client.claim_job.assert_not_called()

    # 11. Refusal before claim: failed launcher plan admission
    def test_failed_launcher_plan_refused_before_claim(self):
        failing_launcher_repo = self.base / "failing-launcher"
        pkg = failing_launcher_repo / "launcher"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "__main__.py").write_text(
            'import json, sys; json.dump({"mode": "dry-run", "chosen": None, "error": "all_models_over_quota"}, sys.stdout)\n',
            encoding="utf-8",
        )

        cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "principal-wake": RoleProfileConfig(
                    profile="principal-wake",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=failing_launcher_repo,
            allowed_workspaces=[self.workspace],
        )
        rt = RuntimeManager(role_config=cfg, quota_gate_script=str(self.quota_gate))
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
            root_bridge=GuardedRootBridge(state_dir=str(self.state_dir)),
        )
        res = disp.handle_offer({
            "id": "job-plan-fail",
            "kind": "principal-wake",
            "role_binding": self._valid_binding("principal-wake"),
        })
        self.assertFalse(res["accepted"])
        self.assertTrue(
            "launcher_admission_refused" in res["reason"] or "HOLD: role backend unsupported" in res["reason"],
            f"Unexpected refusal reason: {res['reason']}",
        )
        self.client.claim_job.assert_not_called()

    # 12. Changed role bindings returned from claim are strictly rejected
    def test_claim_role_binding_changed_rejected(self):
        offered_binding = self._valid_binding("writer-run", generation=2)
        # Server returns generation 99
        self.client.claim_job.return_value = {
            "job_token": "token-xyz",
            "lease_epoch": 2,
            "deadline_at": "2099-01-01T00:00:00Z",
            "role_binding": {**offered_binding, "generation": 99},
        }

        res = self.dispatcher.handle_offer({
            "id": "job-gen-changed",
            "kind": "writer-run",
            "role_binding": offered_binding,
        })
        self.assertFalse(res["accepted"])
        self.assertIn("HOLD: claim role binding changed", res["reason"])
        self.client.claim_job.assert_called_once()

    # 13. Explicit mock runner tests stay explicit fixtures
    def test_mock_runner_stays_explicit_fixture(self):
        rt = RuntimeManager(allow_mock_runner=True)
        rt.set_mock_runner(lambda *a, **kw: {"status": "ok"})
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
            root_bridge=GuardedRootBridge(state_dir=str(self.state_dir)),
        )
        self.client.claim_job.return_value = {
            "job_token": "token-mock",
            "lease_epoch": 1,
            "deadline_at": "2099-01-01T00:00:00Z",
            "role_binding": self._valid_binding("writer-run"),
        }
        res = disp.handle_offer({
            "id": "job-mock-fixture",
            "kind": "writer-run",
            "role_binding": self._valid_binding("writer-run"),
        })
        self.assertTrue(res["accepted"])
        self.client.claim_job.assert_called_once()
        gate_status = self.client.claim_job.call_args.kwargs.get("gate_status")
        self.assertEqual(gate_status.get("reason"), "mock_allowed")

    # 14. Genuine missing Root proof still denies root-run with pending#59
    def test_missing_root_proof_denies_root_run(self):
        with patch("agent_hub_host.gate.get_gate_status", return_value={"launch_allowed": False, "reason": "pending#59"}):
            res = self.dispatcher.handle_offer({
                "id": "job-root-59",
                "kind": "root-run",
                "payload": {},
            })
            self.assertFalse(res["accepted"])
            self.assertTrue(
                "pending#59" in res["reason"] or "launcher_unsupported" in res["reason"],
                f"Expected missing root proof denial, got: {res['reason']}",
            )
    # 15. Codex quota denied/unknown admits role run when role plan succeeds
    def test_codex_quota_denied_admits_role_run_when_role_plan_succeeds(self):
        denied_codex = self.base / "quota-gate-denied.py"
        denied_codex.write_text(
            'import json, sys; json.dump({"launch_allowed": False, "reason": "codex_quota_exhausted"}, sys.stdout)\n',
            encoding="utf-8",
        )
        denied_codex.chmod(0o700)

        rt = RuntimeManager(
            role_config=self.role_cfg,
            quota_gate_script=str(denied_codex),
        )
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
            root_bridge=GuardedRootBridge(state_dir=str(self.state_dir)),
        )
        self.client.claim_job.return_value = {
            "job_token": "token-role-admitted",
            "lease_epoch": 1,
            "deadline_at": "2099-01-01T00:00:00Z",
            "role_binding": self._valid_binding("writer-run"),
        }
        res = disp.handle_offer({
            "id": "job-role-codex-denied",
            "kind": "writer-run",
            "role_binding": self._valid_binding("writer-run"),
        })
        self.assertTrue(res["accepted"], f"Role offer should be accepted despite Codex denial: {res}")
        self.client.claim_job.assert_called_once()
        gate_status = self.client.claim_job.call_args.kwargs.get("gate_status")
        self.assertEqual(gate_status.get("reason"), "role_qualified")
        self.assertTrue(gate_status.get("launch_allowed"))

    # 16. Actual role provider denied refuses role run even when Codex quota healthy
    def test_role_provider_denied_refuses_role_run_even_when_codex_healthy(self):
        healthy_codex = self.base / "quota-gate-healthy.py"
        healthy_codex.write_text(
            'import json, sys; json.dump({"launch_allowed": True, "reason": "codex_healthy", "windows": {}}, sys.stdout)\n',
            encoding="utf-8",
        )
        healthy_codex.chmod(0o700)

        # Create launcher repo where admission is refused for writer-run
        failing_launcher_repo = _create_synthetic_launcher_repo(self.base / "failing-admission")
        failing_role_cfg = TrustedRoleConfig.build(
            enabled=True,
            profiles={
                "writer-run": RoleProfileConfig(
                    profile="writer-run",
                    enabled=True,
                    workspace=self.workspace,
                    launcher_provider="antigravity",
                    launcher_model="gemini-3.1-pro-high",
                    launcher_task_profile="writer-run",
                ),
            },
            state_dir=self.state_dir,
            launcher_repo=failing_launcher_repo,
        )
        rt = RuntimeManager(
            role_config=failing_role_cfg,
            quota_gate_script=str(healthy_codex),
        )
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
            root_bridge=GuardedRootBridge(state_dir=str(self.state_dir)),
        )

        with patch.object(rt.role_adapter.launcher_backend, "plan", return_value={"mode": "dry-run", "chosen": None, "error": "antigravity_quota_exhausted"}):
            eligible, reason, report = rt.can_process_role_run("writer-run")
            self.assertFalse(eligible)
            self.assertIn("antigravity_quota_exhausted", reason)

            res = disp.handle_offer({
                "id": "job-role-denied",
                "kind": "writer-run",
                "role_binding": self._valid_binding("writer-run"),
            })
            self.assertFalse(res["accepted"])
            self.assertIn("HOLD: role backend unsupported", res["reason"])
            self.client.claim_job.assert_not_called()

    # 17. In non-is_mock operational branch: fixture-only report ALWAYS refuses before claim
    # even when Root gate is healthy; claim is never called and Root gate is never relied upon.
    def test_fixture_only_role_with_healthy_root_gate_refuses_before_claim_and_never_calls_root_gate(self):
        rt = RuntimeManager(
            role_config=self.role_cfg,
            adapter_kwargs={"role_quota_checker": lambda: (True, "fixture_ok")},
        )
        disp = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-test",
            token_provider=lambda: "test-token",
            client=self.client,
            runtime_mgr=rt,
            root_bridge=GuardedRootBridge(state_dir=str(self.state_dir)),
        )

        with patch("agent_hub_host.gate.get_gate_status", return_value={"launch_allowed": True, "profile_supported": True}) as mock_root_gate:
            res = disp.handle_offer({
                "id": "job-fixture-healthy-root",
                "kind": "writer-run",
                "role_binding": self._valid_binding("writer-run"),
            })
            self.assertFalse(res["accepted"])
            self.assertIn("fixture_only_qualification_rejected", res["reason"])
            self.client.claim_job.assert_not_called()
            mock_root_gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
