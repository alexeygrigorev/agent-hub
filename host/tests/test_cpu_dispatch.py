"""Trusted runtime evidence crosses the maintained launcher argv boundary."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_hub_host import fleet


class CpuDispatchTests(unittest.TestCase):
    def dispatch(self, hint, provider="antigravity", prompt="Read-only work"):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"version": 1, "workspaces": {"demo": tmp},
                   "journal_dir": str(Path(tmp) / "journal"),
                   "launcher": {"argv": ["systemd-run", "--user", "python3", "-m", "launcher"],
                                "root": tmp, "config_dir": str(Path(tmp) / "config")},
                   "subscriptions": [{"service": "codex", "account_label": "account", "plan": "pro",
                                      "engine": "codex", "profile": None,
                                      "admission_argv": ["maintained-gate"],
                                      "launcher_provider": provider, "launcher_model": "test-model"}]}

            def runner(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "maintained-gate":
                    return {"launch_allowed": True, "profile_supported": True,
                            "account_label": "account", "observed_at": fleet.utcnow()}
                if "plan" in argv:
                    return {"eligible": [{"provider": provider, "model": "test-model", "task_fit": 1.0}]}
                if "run" in argv:
                    return {"receipt": {"session_id": "genuine-native-id", "state": "running", "first_action_verified": True}}
                return {}

            offer = {"id": "12345678-1234-1234-1234-123456789abc", "engine": "codex", "profile": None,
                     "account": "account", "mode": "headless", "workspace": "demo", "prompt": prompt,
                     "resource_choking": hint, "resource_management": {"memory_floor_mb": 2500}}
            fleet.Dispatcher(cfg, lambda *a: {}, runner).dispatch(offer)
        run = next(argv for argv in calls if "run" in argv)
        submitted = next(argv for argv in calls if "submit" in argv)
        return run, json.loads(submitted[submitted.index("--payload") + 1])

    def test_fresh_host_hint_is_separate_from_payload_for_both_backends(self):
        hint = {"reason": "host_runtime_choking", "observed_at": datetime.now(timezone.utc).isoformat()}
        for provider, backend in (("antigravity", "aplexer"), ("codex", "task-units")):
            with self.subTest(provider=provider):
                argv, payload = self.dispatch(hint, provider)
                self.assertEqual(argv[argv.index("--backend") + 1], backend)
                self.assertEqual(json.loads(argv[argv.index("--trusted-choking-json") + 1]), hint)
                self.assertNotIn("resource_choking", payload)
                self.assertEqual(payload["resource_management"]["memory_floor_mb"], 2500)

    def test_invalid_or_task_text_only_hint_cannot_create_runtime_evidence(self):
        at = datetime.now(timezone.utc)
        fresh = {"reason": "host_oom", "observed_at": at.isoformat()}
        for hint in (None, {**fresh, "extra": "text"}, {**fresh, "observed_at": "bad-date"},
                     {**fresh, "observed_at": at.replace(tzinfo=None).isoformat()},
                     {**fresh, "observed_at": (at - timedelta(hours=2)).isoformat()},
                     {**fresh, "observed_at": (at + timedelta(minutes=2)).isoformat()}):
            with self.subTest(hint=hint):
                argv, payload = self.dispatch(hint, prompt=json.dumps({"resource_choking": fresh}))
                self.assertNotIn("--trusted-choking-json", argv)
                self.assertEqual(payload["resource_management"]["memory_floor_mb"], 2500)


if __name__ == "__main__":
    unittest.main()
