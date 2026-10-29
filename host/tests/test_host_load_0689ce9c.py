"""Intake 0689ce9c AC2: live host load gates new launches, never running ones."""
import unittest
from unittest.mock import patch

from agent_hub_host import fleet
from agent_hub_host import host_load
from agent_hub_host.dispatch_diagnostics import MESSAGES, classify


class LoadFactorTests(unittest.TestCase):
    def test_default_factor_is_one_point_five(self):
        self.assertEqual(host_load.load_factor(None), 1.5)
        self.assertEqual(host_load.load_factor({}), 1.5)

    def test_invalid_factor_falls_back_to_default(self):
        for bad in ("high", None, True, 0.1, 99.0, float("nan")):
            with self.subTest(bad=bad):
                self.assertEqual(host_load.load_factor({"host_load_factor": bad}), 1.5)

    def test_valid_override_is_honored(self):
        self.assertEqual(host_load.load_factor({"host_load_factor": 2.0}), 2.0)


class LiveLoadStatusTests(unittest.TestCase):
    def test_overloaded_above_factor_times_cores(self):
        status = host_load.live_load_status(
            {}, loadavg_fn=lambda: (19.0, 5.0, 2.0), cpu_count_fn=lambda: 12)
        self.assertTrue(status["overloaded"])
        self.assertFalse(status["unknown"])
        self.assertEqual(status, {"load1": 19.0, "cores": 12, "factor": 1.5,
                                 "overloaded": True, "unknown": False})

    def test_not_overloaded_at_or_below_threshold(self):
        for load1 in (0.0, 4.6, 18.0):
            with self.subTest(load1=load1):
                status = host_load.live_load_status(
                    {}, loadavg_fn=lambda: (load1, 1.0, 1.0), cpu_count_fn=lambda: 12)
                self.assertFalse(status["overloaded"])
                self.assertFalse(status["unknown"])

    def test_unreadable_inputs_fail_open(self):
        cases = [
            {"loadavg_fn": lambda: (_ for _ in ()).throw(OSError("no proc"))},
            {"cpu_count_fn": lambda: None},
            {"cpu_count_fn": lambda: 0},
            {"loadavg_fn": lambda: (-1.0, 0.0, 0.0)},
        ]
        for kw in cases:
            with self.subTest(kw=sorted(kw)):
                status = host_load.live_load_status({}, **kw)
                self.assertFalse(status["overloaded"])
                self.assertTrue(status["unknown"])

    def test_check_raises_bounded_slug_on_overload(self):
        with self.assertRaises(ValueError) as ctx:
            host_load.check_live_load(
                {}, loadavg_fn=lambda: (40.0, 1.0, 1.0), cpu_count_fn=lambda: 12)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("host_overload: "))
        self.assertIn("40.0", message)
        self.assertNotIn("\n", message)

    def test_check_passes_status_through_when_clear(self):
        status = host_load.check_live_load(
            {}, loadavg_fn=lambda: (4.6, 4.0, 4.0), cpu_count_fn=lambda: 12)
        self.assertFalse(status["overloaded"])


class GateMergeTests(unittest.TestCase):
    def test_overload_forces_gate_closed(self):
        gate = {"launch_allowed": True, "reason": "fleet local admission", "at": "t"}
        merged = host_load.gate_with_live_load(
            gate, {}, loadavg_fn=lambda: (40.0, 1.0, 1.0), cpu_count_fn=lambda: 12)
        self.assertFalse(merged["launch_allowed"])
        self.assertTrue(merged["reason"].startswith("host_overload: "))
        # Input is never mutated.
        self.assertTrue(gate["launch_allowed"])

    def test_clear_load_keeps_launcher_verdict(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                gate = {"launch_allowed": allowed, "reason": "r", "at": "t"}
                merged = host_load.gate_with_live_load(
                    gate, {}, loadavg_fn=lambda: (1.0, 1.0, 1.0),
                    cpu_count_fn=lambda: 12)
                self.assertIs(merged, gate)


class DispatchDiagnosticsTests(unittest.TestCase):
    def test_host_overload_has_fixed_message_and_classifies(self):
        self.assertIn("stay queued", MESSAGES["host_overload"])
        diagnostic = classify(ValueError("host_overload: load1 40.0 above 1.5x12 cores; launch held"))
        self.assertEqual(diagnostic["reason"], "host_overload")
        self.assertEqual(diagnostic["message"], MESSAGES["host_overload"])
        self.assertEqual(diagnostic["stage"], "dispatch_route")


class DispatchHoldsUnderLoadTests(unittest.TestCase):
    def offer(self):
        return {"id": "12345678-1234-1234-1234-123456789abc", "mode": "headless",
                "engine": "codex", "account": "account", "profile": None,
                "workspace": "demo", "prompt": "read-only work"}

    def dispatcher(self, tmp):
        cfg = {"version": 1, "workspaces": {"demo": tmp},
               "subscriptions": [{"service": "codex", "account_label": "account",
                                  "engine": "codex", "profile": None,
                                  "admission_argv": ["maintained-gate"],
                                  "launcher_provider": "codex",
                                  "launcher_model": "test-model"}]}
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return {}
        return fleet.Dispatcher(cfg, lambda *a: {}, runner), calls

    def test_dispatch_refuses_before_any_admission_or_claim(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher, calls = self.dispatcher(tmp)
            with patch("agent_hub_host.host_load.live_load_status",
                       return_value={"load1": 40.0, "cores": 12, "factor": 1.5,
                                     "overloaded": True, "unknown": False}):
                with self.assertRaises(ValueError) as ctx:
                    dispatcher.dispatch(self.offer())
            self.assertTrue(str(ctx.exception).startswith("host_overload: "))
            self.assertEqual(calls, [])

    def test_dispatch_proceeds_when_load_is_clear(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher, _ = self.dispatcher(tmp)
            with patch("agent_hub_host.host_load.live_load_status",
                       return_value={"load1": 1.0, "cores": 12, "factor": 1.5,
                                     "overloaded": False, "unknown": False}):
                # Fails later at admission (no gate configured in runner),
                # proving the load check passed without blocking.
                with self.assertRaises(ValueError) as ctx:
                    dispatcher.dispatch(self.offer())
            self.assertFalse(str(ctx.exception).startswith("host_overload"))

    def test_overload_is_held_queued_never_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher, _ = self.dispatcher(tmp)
            results = []
            with patch.object(dispatcher, '_persist_launch_record') as persist, patch("agent_hub_host.host_load.live_load_status",
                       return_value={"load1": 40.0, "cores": 12, "factor": 1.5,
                                     "overloaded": True, "unknown": False}):
                dispatcher._dispatch_one(self.offer(), results)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["state"], "held")
            self.assertEqual(results[0]["reason"], "host_overload")
            self.assertEqual(results[0]["diagnostic"]["reason"], "host_overload")
            persist.assert_not_called()  # An ambiguous earlier claim keeps its key.


# Reuse the real dispatch-path fixtures: no native command or Hub write escapes
# this in-memory server. Exercise load changing while slow preparation runs.
from host.tests import test_preclaim_retry_488b860e as helpers


class DispatchLoadRecoveryTests(unittest.TestCase):
    def setUp(self):
        import os
        helpers.PreclaimRetry.setUp(self)
        ceiling = patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.tmp.name})
        ceiling.start()
        self.addCleanup(ceiling.stop)
    _settings = helpers.PreclaimRetry._settings
    _admission = helpers.PreclaimRetry._admission
    _offer = helpers.PreclaimRetry._offer
    _runner = helpers.PreclaimRetry._runner

    clear = {"load1": 1.0, "cores": 12, "factor": 1.5, "overloaded": False, "unknown": False}
    busy = {"load1": 40.0, "cores": 12, "factor": 1.5, "overloaded": True, "unknown": False}

    def test_load_rising_during_preparation_holds_then_recovers_once(self):
        launch = helpers._valid_uuid4()
        hub = helpers.FakeHub(); hub.offer(launch)
        calls = []
        dispatcher = fleet.Dispatcher(self._settings(), hub.request, self._runner(calls, launch), journal_dir=self.tmp.name)
        held = []
        with patch("agent_hub_host.host_load.live_load_status", side_effect=[self.clear, self.busy]):
            dispatcher._dispatch_one(self._offer(launch), held)
        self.assertEqual(held[0]["reason"], "host_overload")
        self.assertEqual(hub.state[launch], "offered")
        self.assertEqual(hub.claim_calls, [])
        self.assertFalse(any("run" in argv for argv in calls))
        self.assertFalse(any(path.endswith("/reject") for _, path in hub.calls))
        recovered = []
        with patch("agent_hub_host.host_load.live_load_status", return_value=self.clear):
            dispatcher._dispatch_one(self._offer(launch), recovered)
        self.assertEqual(recovered[0]["state"], "running")
        self.assertEqual(len(hub.claim_calls), 1)
        self.assertEqual(sum("run" in argv for argv in calls), 1)

    def test_overload_preserves_pending_claim_key_through_restart(self):
        launch = helpers._valid_uuid4()
        hub = helpers.FakeHub(); hub.offer(launch)
        hub.fail_claim_with = lambda: TimeoutError("claim response lost")
        dispatcher = fleet.Dispatcher(self._settings(), hub.request, self._runner([], launch), journal_dir=self.tmp.name)
        with patch("agent_hub_host.host_load.live_load_status", return_value=self.clear), patch("agent_hub_host.fleet.time.sleep"):
            dispatcher._dispatch_one(self._offer(launch), [])
        key = dispatcher._pending_claim_key(launch)
        self.assertIsNotNone(key)
        with patch("agent_hub_host.host_load.live_load_status", return_value=self.busy):
            dispatcher._dispatch_one(self._offer(launch), [])
        restarted = fleet.Dispatcher(self._settings(), hub.request, self._runner([], launch), journal_dir=self.tmp.name)
        self.assertEqual(restarted._pending_claim_key(launch), key)
        self.assertFalse(any(path.endswith("/reject") for _, path in hub.calls))


if __name__ == "__main__":
    unittest.main()
