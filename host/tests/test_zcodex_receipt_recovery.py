"""Lost launcher receipts after native spawn must bind live runs (intake 643395c0).

The maintained launcher emits its headless receipt only after the engine's
first validated action. A zcodex start routinely outlives the Hub's bounded
receipt wait, so the dispatch used to record launcher_command_failed
("unrecognized output was withheld") and leave the running session uncertain
and unstoppable. The start receipt plus a live catalog row prove the native
workload runs: it must be bound as running instead.
"""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host.fleet import Dispatcher

NATIVE_ID = "22222222-2222-4222-8222-222222222222"
NON_UUID4 = "22222222-2222-1222-8222-222222222222"
PROMPT = "prove recovery"


@unittest.skipUnless(os.name == "posix", "POSIX-only dispatch path")
class ZcodexReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chmod(self.tmp.name, 0o700)
        self.workspace = Path(self.tmp.name) / "ws"
        self.workspace.mkdir(mode=0o700, parents=True)
        (self.workspace / ".local" / "hub-results").mkdir(mode=0o700, parents=True)
        self.launcher_root = Path(self.tmp.name) / "launcher"
        self.launcher_root.mkdir(mode=0o700)
        self.launcher_cfg = Path(self.tmp.name) / "launcher_cfg"
        self.launcher_cfg.mkdir(mode=0o700)
        self.journal = Path(self.tmp.name) / "journal"
        self.journal.mkdir(mode=0o700)
        self.offer_id = "33333333-3333-4333-8333-333333333333"
        self.task_id = "hub-" + self.offer_id

    def _settings(self):
        return {
            "version": 1,
            "workspaces": {"main": str(self.workspace)},
            "subscriptions": [{
                "service": "zai",
                "account_label": "zai-glm",
                "engine": "zcodex",
                "launcher_provider": "zai",
                "launcher_model": "glm-5.3-flash",
                "admission_argv": ["/bin/true"],
            }],
            "launcher": {
                "argv": ["launcher-cli"],
                "root": str(self.launcher_root),
                "config_dir": str(self.launcher_cfg),
            },
        }

    def _write_start_receipt(self, *, engine="zcodex", native_id=NATIVE_ID, tag=None):
        doc = {
            "id": native_id,
            "tag": tag or ("task-" + self.task_id),
            "workspace": str(self.workspace),
            "engine": engine,
            "created_at_ms": 1793107533693,
            "phase": "starting",
        }
        path = self.workspace / ".local" / f"launch-{self.task_id}-start.json"
        path.write_text(json.dumps(doc))
        return doc

    def _dispatch(self, run_exc, retirement):
        requests = []

        def fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            if path.endswith("/poll"):
                return {"offers": [{"id": self.offer_id, "engine": "zcodex",
                                    "account": "zai-glm", "mode": "headless",
                                    "workspace": "main", "prompt": PROMPT}]}
            return {"id": self.offer_id, "state": (payload or {}).get("state")}

        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            cmd = " ".join(argv)
            if "/bin/true" in cmd:
                return {"launch_allowed": True, "profile_supported": True,
                        "account_label": "zai-glm",
                        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            if "submit" in cmd:
                return {"id": self.task_id}
            if "plan" in cmd:
                return {"eligible": [{"provider": "zai", "model": "glm-5.3-flash",
                                      "task_fit": 1.0}]}
            if "run" in cmd:
                raise run_exc
            return {}

        offer = {"id": self.offer_id, "engine": "zcodex", "account": "zai-glm",
                 "mode": "headless", "workspace": "main", "prompt": PROMPT}
        dispatcher = Dispatcher(self._settings(), request=fake_request,
                                runner=fake_runner, journal_dir=str(self.journal))
        with patch("agent_hub_host.headless_health.inspect", return_value=None), \
                patch("agent_hub_host.retired_native.verify_native_retirement",
                      return_value=retirement):
            result = dispatcher.dispatch(offer)
        return result, requests, dispatcher

    def test_live_native_after_timeout_is_bound_running(self):
        self._write_start_receipt()
        result, requests, _ = self._dispatch(
            RuntimeError("Local command failed or exceeded resource limit"),
            (False, None, "target_still_active_in_catalog: phase=working"))
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["native_id"], NATIVE_ID)
        self.assertTrue(result.get("recovered_receipt"))
        posted = [p for (_m, path, p) in requests
                  if path.endswith("/" + self.offer_id + "/result")
                  and isinstance(p, dict) and p.get("state") == "running"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["native_id"], NATIVE_ID)
        self.assertEqual(posted[0]["launcher_task_id"], self.task_id)
        record = json.loads((self.journal / "fleet-launches" / (self.offer_id + ".json")).read_text())
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["native_id"], NATIVE_ID)
        self.assertEqual(record["tag"], "task-" + self.task_id)
        self.assertEqual(record["engine"], "zcodex")

    def test_native_death_after_receipt_loss_settles_failed_with_native_id(self):
        self._write_start_receipt()
        result, requests, _ = self._dispatch(
            RuntimeError("Local command failed or exceeded resource limit"),
            (True, {"id": NATIVE_ID}, "retired_tombstone_verified"))
        self.assertEqual(result["state"], "failed")
        posted = [p for (_m, path, p) in requests
                  if path.endswith("/" + self.offer_id + "/result") and isinstance(p, dict)]
        self.assertEqual(posted[-1]["state"], "failed")
        self.assertEqual(posted[-1].get("native_id"), NATIVE_ID)
        self.assertTrue(posted[-1].get("confirmed_exit"))

    def test_unknown_native_state_stays_uncertain(self):
        self._write_start_receipt()
        result, requests, _ = self._dispatch(
            RuntimeError("Local command failed or exceeded resource limit"),
            (False, None, "live_catalog_query_failed: boom"))
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(result["diagnostic"]["reason"], "launcher_command_failed")
        posted = [p for (_m, path, p) in requests
                  if path.endswith("/" + self.offer_id + "/result") and isinstance(p, dict)]
        self.assertEqual(posted[-1]["state"], "uncertain")

    def test_engine_identity_mismatch_never_binds(self):
        self._write_start_receipt(engine="opencode")
        result, requests, _ = self._dispatch(
            RuntimeError("Local command failed or exceeded resource limit"),
            (False, None, "target_still_active_in_catalog: phase=working"))
        self.assertEqual(result["state"], "uncertain")
        posted = [p for (_m, path, p) in requests
                  if path.endswith("/" + self.offer_id + "/result") and isinstance(p, dict)]
        self.assertEqual(posted[-1]["state"], "uncertain")

    def test_zai_binding_requires_uuid4(self):
        self._write_start_receipt(native_id=NON_UUID4)
        result, requests, _ = self._dispatch(
            RuntimeError("Local command failed or exceeded resource limit"),
            (False, None, "target_still_active_in_catalog: phase=working"))
        self.assertEqual(result["state"], "uncertain")
        posted = [p for (_m, path, p) in requests
                  if path.endswith("/" + self.offer_id + "/result") and isinstance(p, dict)]
        self.assertEqual(posted[-1]["state"], "uncertain")

    def test_wrong_tag_never_binds(self):
        self._write_start_receipt(tag="task-something-else")
        result, _requests, _ = self._dispatch(
            RuntimeError("Local command failed or exceeded resource limit"),
            (False, None, "target_still_active_in_catalog: phase=working"))
        self.assertEqual(result["state"], "uncertain")


if __name__ == "__main__":
    unittest.main()
