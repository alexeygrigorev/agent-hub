"""Intake 0689ce9c AC2: live host load gates new launches, never running ones."""
import unittest
from unittest.mock import patch

from agent_hub_host import fleet
from agent_hub_host import host_load
from agent_hub_host.dispatch_diagnostics import MESSAGES, classify


GIB = 1024 ** 3


def clear_probes(**over):
    """Idle-host probes; override single signals per test."""
    probes = {"loadavg_fn": lambda: (4.0, 4.0, 4.0), "cpu_count_fn": lambda: 12,
              "cpu_busy_fn": lambda: (35.0, 10.0), "mem_fn": lambda: (30 * GIB, 64 * GIB)}
    probes.update(over)
    return probes


class LoadFactorTests(unittest.TestCase):
    def test_default_factor_is_runaway_cap(self):
        self.assertEqual(host_load.load_factor(None), 6.0)
        self.assertEqual(host_load.load_factor({}), 6.0)

    def test_invalid_factor_falls_back_to_default(self):
        for bad in ("high", None, True, 0.1, 99.0, float("nan")):
            with self.subTest(bad=bad):
                self.assertEqual(host_load.load_factor({"host_load_factor": bad}), 6.0)

    def test_valid_override_is_honored(self):
        self.assertEqual(host_load.load_factor({"host_load_factor": 2.0}), 2.0)

    def test_override_key_is_a_valid_setting(self):
        # 2026-11-02: the key was missing from the load_settings whitelist, so
        # adding it made every fleet-host cycle fail "Invalid subscription configuration".
        import json
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "subscriptions.json"
            path.write_text(json.dumps({"version": 1, "subscriptions": [], "workspaces": {"home": tmp},
                                        "host_load_factor": 3.0}))
            self.assertEqual(fleet.load_settings(path)["host_load_factor"], 3.0)


class LiveLoadStatusTests(unittest.TestCase):
    def test_high_load_average_alone_is_not_busy(self):
        # RMTHZ 2026-11-02: load 36.7 on 12 cores, CPU 68%, 31G free.
        status = host_load.live_load_status(
            {}, **clear_probes(loadavg_fn=lambda: (36.7, 27.0, 22.0), cpu_busy_fn=lambda: (68.0, 10.0),
                               mem_fn=lambda: (31 * GIB, 64 * GIB)))
        self.assertFalse(status["overloaded"])
        self.assertIsNone(status["trigger"])

    def test_cpu_pegged_over_window_is_busy(self):
        status = host_load.live_load_status({}, **clear_probes(cpu_busy_fn=lambda: (98.0, 12.0)))
        self.assertTrue(status["overloaded"])
        self.assertEqual(status["trigger"], "cpu_busy")
        self.assertEqual(host_load.overload_reason(status), "host_overload: cpu busy 98.0% over 12.0s")

    def test_cpu_just_below_98_is_not_busy(self):
        status = host_load.live_load_status({}, **clear_probes(cpu_busy_fn=lambda: (97.9, 10.0)))
        self.assertFalse(status["overloaded"])

    def test_low_memory_is_busy(self):
        status = host_load.live_load_status({}, **clear_probes(mem_fn=lambda: (5 * GIB, 64 * GIB)))
        self.assertEqual(status["trigger"], "mem_available")

    def test_memory_floor_scales_down_on_small_hosts(self):
        status = host_load.live_load_status({}, **clear_probes(mem_fn=lambda: (3 * GIB, 8 * GIB)))
        self.assertFalse(status["overloaded"])

    def test_runaway_load_is_busy_without_measuring_cpu(self):
        # 2026-10-28 signature: load 275-295 on 12 cores.
        def boom():
            raise AssertionError("cpu must not be sampled when load is runaway")
        status = host_load.live_load_status(
            {}, **clear_probes(loadavg_fn=lambda: (73.0, 1.0, 1.0), cpu_busy_fn=boom))
        self.assertEqual(status["trigger"], "load_runaway")
        self.assertEqual(host_load.overload_reason(status), "host_overload: load1 73.0 above 6.0x12 cores")

    def test_runaway_boundary(self):
        for load1, expected in ((72.0, False), (72.1, True)):
            with self.subTest(load1=load1):
                status = host_load.live_load_status({}, **clear_probes(loadavg_fn=lambda: (load1, 1.0, 1.0)))
                self.assertEqual(status["overloaded"], expected)

    def test_unreadable_inputs_fail_open(self):
        def unreadable(*a):
            raise OSError("no proc")
        status = host_load.live_load_status(
            {}, loadavg_fn=unreadable, cpu_count_fn=lambda: None,
            cpu_busy_fn=lambda: None, mem_fn=lambda: None)
        self.assertFalse(status["overloaded"])
        self.assertTrue(status["unknown"])
        status = host_load.live_load_status(
            {}, **clear_probes(loadavg_fn=lambda: (-1.0, 0.0, 0.0), cpu_count_fn=unreadable))
        self.assertFalse(status["overloaded"])

    def test_check_raises_bounded_slug_on_overload(self):
        with self.assertRaises(ValueError) as ctx:
            host_load.check_live_load({}, **clear_probes(loadavg_fn=lambda: (80.0, 1.0, 1.0)))
        message = str(ctx.exception)
        self.assertTrue(message.startswith("host_overload: "))
        self.assertIn("80.0", message)
        self.assertNotIn("\n", message)

    def test_check_passes_status_through_when_clear(self):
        status = host_load.check_live_load({}, **clear_probes(loadavg_fn=lambda: (36.0, 4.0, 4.0)))
        self.assertFalse(status["overloaded"])


class CpuWindowTests(unittest.TestCase):
    def run_measure(self, snapshots):
        import pathlib
        import tempfile
        clock = {"t": 1000.0}
        sleeps = []
        reads = iter(snapshots)

        def sleep(seconds):
            sleeps.append(seconds)
            clock["t"] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            result = host_load.measure_cpu_busy(
                read_fn=lambda: next(reads), sleep_fn=sleep, clock_fn=lambda: clock["t"],
                state_path=pathlib.Path(tmp) / "state.json")
        return result, sleeps

    def test_first_sample_waits_the_window_then_reports_busy_percent(self):
        before = {"ticks": [0] * 8, "cores": 12}
        after = {"ticks": [700, 0, 100, 200, 0, 0, 0, 0], "cores": 12}
        result, sleeps = self.run_measure([before, after])
        self.assertEqual(sleeps, [10.0])
        self.assertEqual(result, (80.0, 10.0))

    def test_iowait_counts_as_idle(self):
        before = {"ticks": [0] * 8, "cores": 12}
        after = {"ticks": [100, 0, 0, 0, 900, 0, 0, 0], "cores": 12}
        result, _ = self.run_measure([before, after])
        self.assertEqual(result[0], 10.0)

    def test_persisted_snapshot_avoids_sleeping(self):
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "state.json"
            state.write_text('[{"at": 940.0, "ticks": [0, 0, 0, 0, 0, 0, 0, 0], "cores": 12}]')
            sleeps = []
            result = host_load.measure_cpu_busy(
                read_fn=lambda: {"ticks": [50, 0, 0, 50, 0, 0, 0, 0], "cores": 12},
                sleep_fn=sleeps.append, clock_fn=lambda: 1000.0, state_path=state)
        self.assertEqual(sleeps, [])
        self.assertEqual(result, (50.0, 60.0))

    def test_unreadable_cpu_stat_is_unknown(self):
        result, _ = self.run_measure([None])
        self.assertIsNone(result)


class GateMergeTests(unittest.TestCase):
    def test_overload_forces_gate_closed(self):
        gate = {"launch_allowed": True, "reason": "fleet local admission", "at": "t"}
        merged = host_load.gate_with_live_load(gate, {}, **clear_probes(cpu_busy_fn=lambda: (99.5, 10.0)))
        self.assertFalse(merged["launch_allowed"])
        self.assertTrue(merged["reason"].startswith("host_overload: "))
        # Input is never mutated.
        self.assertTrue(gate["launch_allowed"])

    def test_clear_load_keeps_launcher_verdict(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                gate = {"launch_allowed": allowed, "reason": "r", "at": "t"}
                merged = host_load.gate_with_live_load(
                    gate, {}, **clear_probes(loadavg_fn=lambda: (36.0, 1.0, 1.0)))
                self.assertIs(merged, gate)


class DispatchDiagnosticsTests(unittest.TestCase):
    def test_host_overload_has_fixed_message_and_classifies(self):
        self.assertIn("stay queued", MESSAGES["host_overload"])
        diagnostic = classify(ValueError("host_overload: load1 80.0 above 6.0x12 cores; launch held"))
        self.assertEqual(diagnostic["reason"], "host_overload")
        self.assertEqual(diagnostic["message"], MESSAGES["host_overload"])
        self.assertEqual(diagnostic["stage"], "dispatch_route")

    def test_hub_load_rejection_has_a_fixed_protocol_message(self):
        self.assertIn("stay queued", MESSAGES["host_load_high"])
        diagnostic = classify(ValueError("host_load_high: retry after heartbeat"))
        self.assertEqual(diagnostic["reason"], "host_load_high")
        self.assertEqual(diagnostic["message"], MESSAGES["host_load_high"])


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
                       return_value={"load1": 80.0, "cores": 12, "factor": 6.0, "trigger": "load_runaway",
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
                       return_value={"load1": 1.0, "cores": 12, "factor": 6.0, "trigger": None,
                                     "overloaded": False, "unknown": False}):
                # Fails later at admission (no gate configured in runner),
                # proving the load check passed without blocking.
                with self.assertRaises(ValueError) as ctx:
                    dispatcher.dispatch(self.offer())
            self.assertFalse(str(ctx.exception).startswith("host_overload"))

    def test_overload_refreshes_heartbeat_then_rejects_unclaimed_offer(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher, _ = self.dispatcher(tmp)
            results = []
            calls = []
            retry_id = "22345678-1234-1234-1234-123456789abc"

            def request(method, path, payload=None):
                calls.append((method, path, payload))
                return {"id": retry_id, "state": "queued", "queued": True,
                        "rejected": [{"reason": "host_load_high"}]}

            with patch.object(dispatcher, '_persist_launch_record') as persist, \
                    patch.object(dispatcher, '_refresh_checkin', side_effect=lambda hb: calls.append(("heartbeat", None, None)) or True), \
                    patch.object(dispatcher, 'request', side_effect=request), \
                    patch("agent_hub_host.host_load.live_load_status",
                       return_value={"load1": 80.0, "cores": 12, "factor": 6.0, "trigger": "load_runaway",
                                     "overloaded": True, "unknown": False}):
                dispatcher._dispatch_one(self.offer(), results)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["state"], "queued")
            self.assertNotEqual(results[0]["id"], self.offer()["id"])
            self.assertEqual(results[0]["rejected"][0]["reason"], "host_load_high")
            self.assertEqual(calls[0][0], "heartbeat")
            self.assertEqual(calls[1][0:2], ("POST", "/api/fleet/launches/12345678-1234-1234-1234-123456789abc/reject"))
            self.assertEqual(calls[1][2]["diagnostic"], {
                "reason": "host_load_high",
                "message": MESSAGES["host_load_high"],
                "stage": "dispatch_route",
                "error_type": "ValueError",
            })
            self.assertEqual(calls[1][2]["host_load_factor"], 6.0)
            self.assertEqual(persist.call_args_list[-1][0][0]["settled"], True)

    def test_overload_reports_validated_local_factor(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher, _ = self.dispatcher(tmp)
            dispatcher.settings["host_load_factor"] = 2.0
            receipt = {"id": "22345678-1234-1234-1234-123456789abc",
                       "state": "queued", "queued": True, "rejected": []}
            with patch.object(dispatcher, "_refresh_checkin", return_value=True), \
                    patch.object(dispatcher, "request", return_value=receipt) as request, \
                    patch("agent_hub_host.host_load.live_load_status", return_value={
                        "load1": 40.0, "cores": 12, "factor": 2.0, "trigger": "load_runaway",
                        "overloaded": True, "unknown": False}):
                dispatcher._dispatch_one(self.offer(), [])
            self.assertEqual(request.call_args.args[2]["host_load_factor"], 2.0)

    def test_overload_stays_offered_when_heartbeat_refresh_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher, _ = self.dispatcher(tmp)
            results = []
            with patch.object(dispatcher, "_pending_claim_key", return_value=None), \
                    patch.object(dispatcher, "_refresh_checkin", return_value=False), \
                    patch.object(dispatcher, "request") as request, \
                    patch("agent_hub_host.host_load.live_load_status",
                          return_value={"load1": 40.0, "cores": 12, "factor": 1.5,
                                        "overloaded": True, "unknown": False}):
                dispatcher._dispatch_one(self.offer(), results)
            self.assertEqual(results[0]["state"], "held")
            self.assertEqual(results[0]["stage"], "heartbeat_refresh")
            request.assert_not_called()

    def test_overload_does_not_reject_pending_claim_or_routed_task(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher, _ = self.dispatcher(tmp)
            with patch("agent_hub_host.host_load.live_load_status",
                       return_value={"load1": 80.0, "cores": 12, "factor": 6.0, "trigger": "load_runaway",
                                     "overloaded": True, "unknown": False}), \
                    patch.object(dispatcher, "_pending_claim_key", return_value="a" * 32), \
                    patch.object(dispatcher, "_refresh_checkin") as refresh, \
                    patch.object(dispatcher, "request") as request:
                results = []
                dispatcher._dispatch_one(self.offer(), results)
            self.assertEqual(results[0]["state"], "held")
            request.assert_not_called()
            refresh.assert_not_called()

            routed = self.offer()
            routed["task_id"] = "task-1"
            with patch("agent_hub_host.host_load.live_load_status",
                       return_value={"load1": 80.0, "cores": 12, "factor": 6.0, "trigger": "load_runaway",
                                     "overloaded": True, "unknown": False}), \
                    patch.object(dispatcher, "_pending_claim_key", return_value=None), \
                    patch.object(dispatcher, "_refresh_checkin") as refresh, \
                    patch.object(dispatcher, "request") as request:
                results = []
                dispatcher._dispatch_one(routed, results)
            self.assertEqual(results[0]["state"], "held")
            request.assert_not_called()
            refresh.assert_not_called()


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

    def test_load_rising_during_preparation_requeues_then_retry_recovers_once(self):
        launch = helpers._valid_uuid4()
        hub = helpers.FakeHub(); hub.offer(launch)
        calls = []
        retry_ids = []

        def request(method, path, payload=None):
            if path.endswith('/reject'):
                retry_id = helpers._valid_uuid4()
                hub.state[launch] = 'failed'
                hub.offer(retry_id)
                retry_ids.append(retry_id)
                hub.calls.append((method, path))
                return {'id': retry_id, 'state': 'queued', 'queued': True,
                        'rejected': [{'reason': 'host_load_high'}]}
            return hub.request(method, path, payload)

        dispatcher = fleet.Dispatcher(self._settings(), request, self._runner(calls, launch), journal_dir=self.tmp.name)
        held = []
        with patch("agent_hub_host.host_load.live_load_status", side_effect=[self.clear, self.busy]):
            dispatcher._dispatch_one(self._offer(launch), held)
        self.assertEqual(held[0]["state"], "queued")
        self.assertEqual(held[0]["rejected"][0]["reason"], "host_load_high")
        self.assertEqual(hub.state[launch], "failed")
        self.assertEqual(len(retry_ids), 1)
        self.assertEqual(hub.claim_calls, [])
        self.assertFalse(any("run" in argv for argv in calls))
        self.assertTrue(any(path.endswith("/reject") for _, path in hub.calls))
        recovered = []
        retry_dispatcher = fleet.Dispatcher(self._settings(), request,
                                            self._runner(calls, retry_ids[0]),
                                            journal_dir=self.tmp.name)
        with patch("agent_hub_host.host_load.live_load_status", return_value=self.clear):
            retry_dispatcher._dispatch_one(self._offer(retry_ids[0]), recovered)
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


class PendingReserveClaimTests(unittest.TestCase):
    setUp = DispatchLoadRecoveryTests.setUp
    _settings = DispatchLoadRecoveryTests._settings
    _offer = DispatchLoadRecoveryTests._offer
    _runner = DispatchLoadRecoveryTests._runner

    def prepare(self, state="claim_pending", key="a" * 32, **extra):
        launch = helpers._valid_uuid4()
        self.sent = []
        self.receipt = {"id": helpers._valid_uuid4(), "state": "queued", "queued": True, "rejected": []}
        def request(method, path, payload):
            self.sent.append((method, path, payload))
            if isinstance(self.receipt, Exception):
                raise self.receipt
            return self.receipt
        dispatcher = fleet.Dispatcher(self._settings(), request, self._runner([], launch), journal_dir=self.tmp.name)
        dispatcher._persist_launch_record({"offer_id": launch, "state": state, "claim_key": key,
                                          "settled": False, **extra})
        return dispatcher, self._offer(launch)

    def diagnostic(self):
        reason = "codex_quota_gate_codex_reserve_required"
        return {"reason": reason, "message": MESSAGES[reason], "stage": "dispatch_route", "error_type": "ValueError"}

    def test_owned_pending_reserve_refusal_requeues_and_settles_after_receipt(self):
        from agent_hub_host.terminal_reports import read_record
        dispatcher, offer = self.prepare()
        with patch.object(dispatcher, "dispatch", side_effect=ValueError("reserve refused")), \
                patch("agent_hub_host.dispatch_diagnostics.classify", return_value=self.diagnostic()):
            results = []
            dispatcher._dispatch_one(offer, results)
        self.assertEqual(results, [self.receipt])
        self.assertEqual(self.sent[0][2]["claim_key"], "a" * 32)
        self.assertEqual(self.sent[0][2]["reason"], self.diagnostic()["reason"])
        self.assertTrue(read_record(dispatcher, offer["id"])["settled"])
        self.assertIsNone(dispatcher._pending_claim_key(offer["id"]))

    def test_no_receipt_never_settles_pending_claim(self):
        from agent_hub_host.terminal_reports import read_record
        for receipt in (TimeoutError("lost response"), {}, {"id": "same", "state": "queued"}):
            with self.subTest(receipt=type(receipt).__name__):
                dispatcher, offer = self.prepare()
                self.receipt = receipt
                self.assertIsNone(dispatcher._reject_pending_reserve_claim(offer, self.diagnostic()))
                self.assertFalse(read_record(dispatcher, offer["id"])["settled"])
                self.assertEqual(dispatcher._pending_claim_key(offer["id"]), "a" * 32)

    def test_native_fence_missing_key_routed_task_and_missing_journal_never_reject(self):
        for state, key, extra in (("uncertain", "a" * 32, {}), ("claim_pending", None, {}),
                                  ("claim_pending", "a" * 32, {"native_id": "native"}),
                                  ("claim_pending", "a" * 32, {"task_id": "hub-task"})):
            with self.subTest(state=state, extra=extra):
                dispatcher, offer = self.prepare(state, key, **extra)
                self.assertIsNone(dispatcher._reject_pending_reserve_claim(offer, self.diagnostic()))
                self.assertEqual(self.sent, [])
        dispatcher, offer = self.prepare()
        offer["task_id"] = "routed-task"
        self.assertIsNone(dispatcher._reject_pending_reserve_claim(offer, self.diagnostic()))
        offer.pop("task_id"); offer["id"] = helpers._valid_uuid4()
        self.assertIsNone(dispatcher._reject_pending_reserve_claim(offer, self.diagnostic()))
        self.assertEqual(self.sent, [])

    def test_foreign_hub_claim_supersedes_only_pending_journal_without_dispatch(self):
        from agent_hub_host.terminal_reports import read_record
        for state in ("claim_pending", "uncertain"):
            dispatcher, offer = self.prepare(state)
            offer.update(state="starting", claim_key="b" * 32)
            with patch.object(dispatcher, "dispatch") as dispatch:
                dispatcher._dispatch_one(offer, [])
            dispatch.assert_not_called()
            self.assertEqual(self.sent, [])
            record = read_record(dispatcher, offer["id"])
            self.assertEqual(record["settled"], state == "claim_pending")
            self.assertEqual(record["state"], "superseded" if state == "claim_pending" else "uncertain")


if __name__ == "__main__":
    unittest.main()
