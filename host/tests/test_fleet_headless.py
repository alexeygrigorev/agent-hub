import datetime as dt
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host.fleet import Dispatcher
from agent_hub_host.fleet_launch import launcher_payload


class TestFleetHeadless(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # private_fs checks mode & 0o077 == 0
        os.chmod(self.tmp.name, 0o700)
        self.workspace = Path(self.tmp.name) / "ws"
        self.workspace.mkdir(mode=0o700, parents=True)
        self.launcher_root = Path(self.tmp.name) / "launcher"
        self.launcher_root.mkdir(mode=0o700, parents=True)
        self.launcher_cfg = Path(self.tmp.name) / "launcher_cfg"
        self.launcher_cfg.mkdir(mode=0o700, parents=True)

    def _settings(self, subs=None):
        return {
            "version": 1,
            "workspaces": {"main": str(self.workspace)},
            "subscriptions": subs or [
                {
                    "service": "codex",
                    "account_label": "pro-account",
                    "engine": "codex",
                    "profile": "hub-pro-account",
                    "launcher_provider": "codex",
                    "launcher_model": "gpt-6.1-sol",
                    "admission_argv": ["/bin/true"],
                }
            ],
            "launcher": {
                "argv": ["launcher-cli"],
                "root": str(self.launcher_root),
                "config_dir": str(self.launcher_cfg),
            },
        }

    def _admission_response(self, account_label="pro-account"):
        return {
            "launch_allowed": True,
            "profile_supported": True,
            "account_label": account_label,
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def test_launcher_payload_with_profile(self):
        p = launcher_payload(
            target_cwd=self.workspace,
            goal="Do task",
            account_label="pro-account",
            profile="hub-pro-account",
            timeout=1800,
            model="gpt-6.1-sol",
            provider="codex",
        )
        self.assertEqual(p["account_label"], "pro-account")
        self.assertEqual(p["profile"], "model-task")
        self.assertEqual(p["account_profile"], "hub-pro-account")
        self.assertEqual(p["provider_profile"], "hub-pro-account")
        self.assertEqual(p["model_requirements"]["providers"], ["codex"])
        self.assertEqual(p["model_requirements"]["models"], ["gpt-6.1-sol"])

    def test_launcher_payload_default_timeout_allows_long_runs(self):
        # Intake 14453011: 1800 s killed a ~33 min headless codex run mid-run.
        # Default must be the launcher max (7200 s).
        p = launcher_payload(
            target_cwd=self.workspace,
            goal="Do task",
            account_label="pro-account",
        )
        self.assertEqual(p["timeout"], 7200)

    def test_headless_dispatch_named_profile_uses_aplexer_backend(self):
        settings = self._settings()
        requests = []
        runner_calls = []

        def fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            if path == "/api/fleet/launches/poll":
                return {
                    "offers": [
                        {
                            "id": "off1",
                            "engine": "codex",
                            "profile": "hub-pro-account",
                            "account": "pro-account",
                            "mode": "headless",
                            "workspace": "main",
                            "prompt": "solve gap",
                        }
                    ]
                }
            elif path.endswith("/claim"):
                return {"status": "claimed"}
            elif path.endswith("/result"):
                return {"id": "off1", "state": payload.get("state")}
            return {}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            runner_calls.append(argv)
            cmd_str = " ".join(argv)
            if "/bin/true" in cmd_str:
                return self._admission_response("pro-account")
            elif "submit" in cmd_str:
                return {"id": "hub-off1"}
            elif "plan" in cmd_str:
                return {
                    "eligible": [
                        {
                            "provider": "codex",
                            "model": "gpt-6.1-sol",
                            "task_fit": 1.0,
                            "profile": "hub-pro-account",
                            "account_label": "pro-account",
                        }
                    ]
                }
            elif "run" in cmd_str:
                return {
                    "backend": "aplexer",
                    "task_id": "hub-off1",
                    "receipt": {
                        "session_id": "22222222-2222-4222-8222-222222222222",
                        "state": "running",
                        "first_action_verified": True,
                        "tag": "task-hub-off1",
                        "workspace": str(self.workspace),
                        "engine": "codex",
                        "profile": "hub-pro-account",
                        "account_label": "pro-account",
                        "phase": "running",
                    },
                }
            return {}

        offer = {
            "id": "off1",
            "engine": "codex",
            "profile": "hub-pro-account",
            "account": "pro-account",
            "mode": "headless",
            "workspace": "main",
            "prompt": "solve gap",
        }
        d = Dispatcher(settings, request=fake_request, runner=fake_runner)
        res = d.dispatch(offer)

        self.assertEqual(res["id"], "off1")
        self.assertEqual(res["state"], "running")
        self.assertEqual(res["native_id"], "22222222-2222-4222-8222-222222222222")

        run_cmd = next(c for c in runner_calls if "run" in c)
        self.assertIn("--backend", run_cmd)
        b_idx = run_cmd.index("--backend")
        self.assertEqual(run_cmd[b_idx + 1], "aplexer")

        sub_cmd = next(c for c in runner_calls if "submit" in c)
        p_idx = sub_cmd.index("--payload")
        sub_payload = json.loads(sub_cmd[p_idx + 1])
        self.assertEqual(sub_payload["account_profile"], "hub-pro-account")
        self.assertEqual(sub_payload["provider_profile"], "hub-pro-account")
        self.assertEqual(sub_payload["memory_mb"], 1500)

    def test_receipt_mismatch_profile_rejected(self):
        settings = self._settings()
        offer = {
            "id": "off-mismatch",
            "engine": "codex",
            "profile": "hub-pro-account",
            "account": "pro-account",
            "mode": "headless",
            "workspace": "main",
            "prompt": "solve gap",
        }

        def fake_request(method, path, payload=None):
            return {"status": "claimed"}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            cmd_str = " ".join(argv)
            if "/bin/true" in cmd_str:
                return self._admission_response("pro-account")
            elif "submit" in cmd_str:
                return {"id": "hub-off-mismatch"}
            elif "plan" in cmd_str:
                return {
                    "eligible": [
                        {"provider": "codex", "model": "gpt-6.1-sol", "task_fit": 1.0}
                    ]
                }
            elif "run" in cmd_str:
                return {
                    "backend": "aplexer",
                    "task_id": "hub-off-mismatch",
                    "receipt": {
                        "session_id": "33333333-3333-4333-8333-333333333333",
                        "state": "running",
                        "first_action_verified": True,
                        "tag": "task-hub-off-mismatch",
                        "workspace": str(self.workspace),
                        "engine": "codex",
                        "profile": "hub-other-account",
                        "account_label": "pro-account",
                        "phase": "running",
                    },
                }
            return {}

        d = Dispatcher(settings, request=fake_request, runner=fake_runner)
        res = d.dispatch(offer)
        self.assertEqual(res["state"], "uncertain")
        self.assertEqual(res["error"], "ValueError")
        self.assertIn("dispatch_headless:ValueError", res["reason"])

    def test_receipt_missing_profile_raises_mismatch(self):
        settings = self._settings()
        offer = {
            "id": "off-no-prof",
            "engine": "codex",
            "profile": "hub-pro-account",
            "account": "pro-account",
            "mode": "headless",
            "workspace": "main",
            "prompt": "solve gap",
        }
        def fake_request(method, path, payload=None):
            return {"status": "claimed"}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            cmd_str = " ".join(argv)
            if "/bin/true" in cmd_str:
                return self._admission_response("pro-account")
            elif "submit" in cmd_str:
                return {"id": "hub-off-no-prof"}
            elif "plan" in cmd_str:
                return {"eligible": [{"provider": "codex", "model": "gpt-6.1-sol", "task_fit": 1.0}]}
            elif "run" in cmd_str:
                return {
                    "backend": "aplexer",
                    "task_id": "hub-off-no-prof",
                    "receipt": {
                        "session_id": "77777777-7777-4777-8777-777777777777",
                        "state": "running",
                        "first_action_verified": True,
                        "tag": "task-hub-off-no-prof",
                        "workspace": str(self.workspace),
                        "engine": "codex",
                        "account_label": "pro-account",
                        "phase": "running",
                    },
                }
            return {}

        d = Dispatcher(settings, request=fake_request, runner=fake_runner)
        res = d.dispatch(offer)
        self.assertEqual(res["state"], "uncertain")
        self.assertEqual(res["error"], "ValueError")

    def test_receipt_managed_zai_expects_codex_engine(self):
        settings = self._settings()
        settings["subscriptions"] = [{
            "service": "zai",
            "account_label": "zai-acc",
            "engine": "codex",
            "profile": "hub-zai-acc",
            "admission_argv": ["/bin/true"],
            "launcher_provider": "zai",
            "launcher_model": "glm-5.3-flash",
        }]
        offer = {
            "id": "off-zai",
            "engine": "codex",
            "profile": "hub-zai-acc",
            "account": "zai-acc",
            "mode": "headless",
            "workspace": "main",
            "prompt": "solve gap",
        }
        def fake_request(method, path, payload=None):
            return {"status": "claimed"}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            cmd_str = " ".join(argv)
            if "/bin/true" in cmd_str:
                return self._admission_response("zai-acc")
            elif "submit" in cmd_str:
                return {"id": "hub-off-zai"}
            elif "plan" in cmd_str:
                return {"eligible": [{"provider": "zai", "model": "glm-5.3-flash", "task_fit": 1.0}]}
            elif "run" in cmd_str:
                return {
                    "backend": "aplexer",
                    "task_id": "hub-off-zai",
                    "receipt": {
                        "session_id": "88888888-8888-4888-8888-888888888888",
                        "state": "running",
                        "first_action_verified": True,
                        "tag": "task-hub-off-zai",
                        "workspace": str(self.workspace),
                        "engine": "codex",
                        "profile": "hub-zai-acc",
                        "account_label": "zai-acc",
                        "phase": "running",
                    },
                }
            return {}

        d = Dispatcher(settings, request=fake_request, runner=fake_runner)
        res = d.dispatch(offer)
        self.assertEqual(res["state"], "running")
        self.assertEqual(res["native_id"], "88888888-8888-4888-8888-888888888888")

    def test_receipt_gemini_expects_gemini_engine(self):
        settings = self._settings()
        settings["subscriptions"] = [{
            "service": "gemini",
            "account_label": "gemini-acc",
            "engine": "gemini",
            "profile": "hub-gemini-acc",
            "admission_argv": ["/bin/true"],
            "launcher_provider": "gemini",
            "launcher_model": "gemini-2.5-pro",
        }]
        offer = {
            "id": "off-gemini",
            "engine": "gemini",
            "profile": "hub-gemini-acc",
            "account": "gemini-acc",
            "mode": "headless",
            "workspace": "main",
            "prompt": "solve gap",
        }
        def fake_request(method, path, payload=None):
            return {"status": "claimed"}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            cmd_str = " ".join(argv)
            if "/bin/true" in cmd_str:
                return self._admission_response("gemini-acc")
            elif "submit" in cmd_str:
                return {"id": "hub-off-gemini"}
            elif "plan" in cmd_str:
                return {"eligible": [{"provider": "gemini", "model": "gemini-2.5-pro", "task_fit": 1.0}]}
            elif "run" in cmd_str:
                return {
                    "backend": "aplexer",
                    "task_id": "hub-off-gemini",
                    "receipt": {
                        "session_id": "99999999-9999-4999-8999-999999999999",
                        "state": "running",
                        "first_action_verified": True,
                        "tag": "task-hub-off-gemini",
                        "workspace": str(self.workspace),
                        "engine": "gemini",
                        "profile": "hub-gemini-acc",
                        "account_label": "gemini-acc",
                        "phase": "running",
                    },
                }
            return {}

        d = Dispatcher(settings, request=fake_request, runner=fake_runner)
        res = d.dispatch(offer)
        self.assertEqual(res["state"], "running")
        self.assertEqual(res["native_id"], "99999999-9999-4999-8999-999999999999")

    def test_receipt_missing_account_raises_mismatch(self):
        settings = self._settings()
        offer = {
            "id": "off-no-acc",
            "engine": "codex",
            "profile": "hub-pro-account",
            "account": "pro-account",
            "mode": "headless",
            "workspace": "main",
            "prompt": "solve gap",
        }
        def fake_request(method, path, payload=None):
            return {"status": "claimed"}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            cmd_str = " ".join(argv)
            if "/bin/true" in cmd_str:
                return self._admission_response("pro-account")
            elif "submit" in cmd_str:
                return {"id": "hub-off-no-acc"}
            elif "plan" in cmd_str:
                return {"eligible": [{"provider": "codex", "model": "gpt-6.1-sol", "task_fit": 1.0}]}
            elif "run" in cmd_str:
                return {
                    "backend": "aplexer",
                    "task_id": "hub-off-no-acc",
                    "receipt": {
                        "session_id": "88888888-8888-4888-8888-888888888888",
                        "state": "running",
                        "first_action_verified": True,
                        "tag": "task-hub-off-no-acc",
                        "workspace": str(self.workspace),
                        "engine": "codex",
                        "profile": "hub-pro-account",
                        "phase": "running",
                    },
                }
            return {}

        d = Dispatcher(settings, request=fake_request, runner=fake_runner)
        res = d.dispatch(offer)
        self.assertEqual(res["state"], "uncertain")
        self.assertEqual(res["error"], "ValueError")
        self.assertIn("dispatch_headless:ValueError", res["reason"])

    def test_two_same_provider_labels_do_not_borrow_quota_or_fallback(self):
        subs = [
            {
                "service": "zai",
                "account_label": "zai-1",
                "engine": "zcodex",
                "profile": "hub-zai-1",
                "launcher_provider": "zai",
                "launcher_model": "glm-5.3-flash",
                "admission_argv": ["/bin/true"],
            },
            {
                "service": "zai",
                "account_label": "zai-2",
                "engine": "zcodex",
                "profile": "hub-zai-2",
                "launcher_provider": "zai",
                "launcher_model": "glm-5.3-flash",
                "admission_argv": ["/bin/true"],
            },
        ]
        settings = self._settings(subs)

        offer_1 = {
            "id": "off-z1",
            "engine": "zcodex",
            "profile": "hub-zai-1",
            "account": "zai-1",
            "mode": "headless",
            "workspace": "main",
            "prompt": "run task",
        }

        def fake_request(method, path, payload=None):
            return {"status": "claimed"}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            cmd_str = " ".join(argv)
            if "/bin/true" in cmd_str:
                return self._admission_response("zai-1")
            elif "submit" in cmd_str:
                return {"id": "hub-off-z1"}
            elif "plan" in cmd_str:
                return {
                    "eligible": [
                        {"provider": "zai", "model": "glm-5.3-flash", "task_fit": 1.0}
                    ]
                }
            elif "run" in cmd_str:
                return {
                    "backend": "aplexer",
                    "task_id": "hub-off-z1",
                    "receipt": {
                        "session_id": "44444444-4444-4444-8444-444444444444",
                        "state": "running",
                        "first_action_verified": True,
                        "tag": "task-hub-off-z1",
                        "workspace": str(self.workspace),
                        "engine": "zcodex",
                        "profile": "hub-zai-2",
                        "account_label": "zai-2",
                        "phase": "running",
                    },
                }
            return {}

        d = Dispatcher(settings, request=fake_request, runner=fake_runner)
        res = d.dispatch(offer_1)
        self.assertEqual(res["state"], "uncertain")
        self.assertEqual(res["error"], "ValueError")
        self.assertIn("dispatch_headless:ValueError", res["reason"])

    def test_natural_headless_completion_and_reconciliation(self):
        settings = self._settings()
        d = Dispatcher(settings)

        offer_id = "off-complete"
        task_id = f"hub-{offer_id}"
        native_id = "55555555-5555-4555-8555-555555555555"
        # AC a19cac9e: only exits WITH a valid result file settle as exited.
        result_dir = self.workspace / ".local" / "hub-results"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / f"{offer_id}.json").write_text(
            json.dumps({"status": "done", "report": "natural completion"}))
        d._active_launches[offer_id] = {
            "offer_id": offer_id,
            "task_id": task_id,
            "mode": "headless",
            "workspace": str(self.workspace),
            "engine": "codex",
            "account": "pro-account",
            "profile": "hub-pro-account",
            "native_id": native_id,
            "tag": f"task-{task_id}",
            "incarnation": 1000000,
            "result_path": str(self.workspace / ".local" / "hub-results" / f"{offer_id}.json"),
            "state": "running",
            "settled": False,
        }

        result_path = Path(d._active_launches[offer_id]['result_path'])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({'status': 'done', 'report': 'Completed fixture task.'}))
        mock_tombstone = {
            "cause": "finished",
            "finished_at_ms": 1005000,
            "session_id": native_id,
        }

        def fake_runner(argv, cwd=None):
            return {
                "id": task_id,
                "state": "completed-awaiting-review",
                "resources_reserved": False,
                "native_id": native_id,
            }

        hub_posts = []

        def fake_request(method, path, payload=None):
            hub_posts.append((method, path, payload))
            return {"id": offer_id, "state": "exited"}

        d.runner = fake_runner
        d.request = fake_request

        with patch("agent_hub_host.fleet.verify_native_retirement", return_value=(True, mock_tombstone, "detail")):
            reconciled = d.reconcile_launches()

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["id"], offer_id)
        self.assertEqual(reconciled[0]["state"], "exited")
        self.assertTrue(reconciled[0]["reconciled"])

        record = d._active_launches[offer_id]
        self.assertTrue(record["settled"])
        self.assertEqual(record["state"], "exited")

        self.assertEqual(len(hub_posts), 1)
        method, path, payload = hub_posts[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, f"/api/fleet/launches/{offer_id}/result")
        self.assertEqual(payload["native_id"], native_id)
        self.assertEqual(payload["state"], "exited")
        self.assertTrue(payload["confirmed_exit"])

    def test_canary_provisioning_execute_environment_isolation(self):
        from agent_hub_host import provisioning as p
        bundle_record = {
            "label": "canary-acc",
            "provider": "zai",
            "version": "0.155.1",
            "revision": "66666666-6666-4666-8666-666666666666",
            "files": {
                ".zcode/v2/credentials.json": '{"zcodejwttoken": "canary-token"}',
                ".zcodex/config.toml": 'model = "glm-5.3-flash"',
            },
            "env": {"ZAI_API_KEY": "canary-token"},
            "args": ["--model", "glm-5.3-flash"],
        }
        m_dir = Path(self.tmp.name) / "managed-engines"
        m_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        dest = m_dir / f"{bundle_record['label']}-{bundle_record['revision']}"
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = dest / "home"
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        (dest / "engine" / "bin").mkdir(mode=0o700, parents=True, exist_ok=True)
        zbin = dest / "engine" / "bin" / "zcodex"
        zbin.write_text("#!/bin/sh\nexit 0\n")
        zbin.chmod(0o700)
        p.write_json(dest / "bundle.json", bundle_record)
        p.write_json(m_dir / "state.json", [
            {"label": bundle_record["label"], "revision": bundle_record["revision"], "provider": bundle_record["provider"]}
        ])

        with patch.object(p, "root", return_value=m_dir), \
             patch.object(p.sys, "argv", ["provisioning.py", "canary-acc", "exec", "goal"]), \
             patch.object(p.os, "execve") as fake_exec:
            p.execute()

        fake_exec.assert_called_once()
        exec_path, exec_argv, exec_env = fake_exec.call_args.args
        self.assertEqual(exec_path, str(zbin))
        self.assertEqual(exec_argv[0], str(zbin))
        self.assertIn("--model", exec_argv)
        self.assertIn("glm-5.3-flash", exec_argv)
        self.assertIn("exec", exec_argv)
        self.assertIn("goal", exec_argv)

        self.assertEqual(exec_env["HOME"], str(home))
        self.assertEqual(exec_env["CODEX_HOME"], str(home / ".zcodex"))
        self.assertEqual(exec_env["ZCODE_HOME"], str(home / ".zcode"))
        self.assertEqual(exec_env["ZAI_API_KEY"], "canary-token")
        self.assertNotEqual(exec_env["HOME"], str(Path.home()))

    def test_codex_quota_gate_enforced_at_execution(self):
        from agent_hub_host import provisioning as p
        bundle_record = {
            "label": "codex-gate-acc",
            "provider": "codex",
            "version": "0.163.0",
            "revision": "77777777-7777-4777-8777-777777777777",
            "files": {".codex/auth.json": "{}"},
            "env": {"OPENAI_API_KEY": "tok"},
            "args": [],
        }
        m_dir = Path(self.tmp.name) / "managed-engines"
        m_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        dest = m_dir / f"{bundle_record['label']}-{bundle_record['revision']}"
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = dest / "home"
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        (dest / "engine" / "node_modules" / ".bin").mkdir(mode=0o700, parents=True, exist_ok=True)
        cbin = dest / "engine" / "node_modules" / ".bin" / "codex"
        cbin.write_text("#!/bin/sh\nexit 0\n")
        cbin.chmod(0o700)
        p.write_json(dest / "bundle.json", bundle_record)
        p.write_json(m_dir / "state.json", [
            {"label": bundle_record["label"], "revision": bundle_record["revision"], "provider": bundle_record["provider"]}
        ])

        # Quota gate fails when remaining <= 15%
        with patch.object(p, "root", return_value=m_dir), \
             patch.object(p.sys, "argv", ["provisioning.py", "codex-gate-acc", "exec", "goal"]), \
             patch("agent_hub_host.provisioning.check_codex_quota_gate", return_value=(False, "reserve 15%")), \
             self.assertRaises(SystemExit) as cm:
            p.execute()
        self.assertEqual(cm.exception.code, 75)

        # Quota gate passes when remaining > 15%
        with patch.object(p, "root", return_value=m_dir), \
             patch.object(p.sys, "argv", ["provisioning.py", "codex-gate-acc", "exec", "goal"]), \
             patch("agent_hub_host.provisioning.check_codex_quota_gate", return_value=(True, "ok")), \
             patch.object(p.os, "execve") as fake_exec:
            p.execute()
        fake_exec.assert_called_once()


if __name__ == '__main__':
    unittest.main()
