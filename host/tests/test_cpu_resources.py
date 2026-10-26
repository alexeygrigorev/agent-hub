import unittest
from agent_hub_host.cpu_resources import cpu_delta, update_cpu_distribution, read_cpu_stat, sample_cpu_usage


class CpuResourcesTests(unittest.TestCase):
    def test_cross_repository_wire_fixture(self):
        import json
        from pathlib import Path
        from agent_hub_host.cpu_resources import sanitize_cpu_admission
        fixture = json.loads((Path(__file__).resolve().parents[2] / "test/fixtures/cpu-admission-v2.json").read_text())
        self.assertEqual(sanitize_cpu_admission(fixture["cpu"]), fixture["cpu"])

    def test_proc_stat_guest_fields_and_heartbeat_shape(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pressure").mkdir()
            (root / "pressure/cpu").write_text("some avg10=12.34 avg60=10 total=100\nfull avg10=0.5 total=10\n")
            (root / "pressure/memory").write_text("full avg10=0.2 total=2\n")
            (root / "vmstat").write_text("oom_kill 10\n")
            (root / "stat").write_text("cpu 10 0 0 10 80 0 0 0 900 0\ncpu0 1\ncpu1 1\n")
            self.assertEqual(read_cpu_stat(directory)["ticks"], [10, 0, 0, 10, 80, 0, 0, 0])
            def next_stat(_):
                (root / "stat").write_text("cpu 20 0 0 20 160 0 0 0 1800 0\ncpu0 1\ncpu1 1\n")
            times = iter([0, .2])
            result = sample_cpu_usage(directory, sleep=next_stat, monotonic=lambda: next(times), clock=lambda: 1000)
            self.assertEqual(result["busy_percent"], 10)
            self.assertAlmostEqual(result["busy_cores"], .2)
            self.assertEqual(result["psi_avg10"], 12.34)
            self.assertEqual(result["psi_full_avg10"], .5)
            self.assertEqual(result["memory_psi_full_avg10"], .2)
            self.assertEqual(result["observed_at"], "1970-01-01T00:16:40Z")
            unavailable = sample_cpu_usage(directory, sleep=lambda _: None)
            self.assertIsNone(unavailable["busy_cores"])
            self.assertEqual(unavailable["psi_full_avg10"], .5)

    def test_peak_journal_and_final_report_keep_lifetime_p95(self):
        from agent_hub_host.resource_management import update_peaks, _sample_row
        state = {}
        base = {"rss_mb": 50, "cpu_identity": "exact-native:1", "cpu_seconds": 10, "runtime_s": 10}
        first = update_peaks(state, "session", "native", base, "2026-10-26T00:00:00Z")
        self.assertIsNone(first["current_cpu_cores"])
        second = update_peaks(state, "session", "native", {**base, "cpu_seconds": 30, "runtime_s": 20}, "2026-10-26T00:00:10Z")
        self.assertEqual(second["p95_cpu_cores"], 2)
        self.assertEqual(_sample_row("session", "native", second, "2026-10-26T00:00:10Z")["p95_cpu_cores"], 2)
        self.assertNotIn("current_cpu_cores", _sample_row("session", "native", second, "2026-10-26T00:00:20Z"))

    def test_refusals_preserve_only_fixed_cpu_reason(self):
        from agent_hub_host.launch_diagnostics import diagnostic
        from agent_hub_host.dispatch_diagnostics import classify
        for reason in ("cpu_headroom", "cpu_pressure", "cpu_usage_unknown"):
            self.assertEqual(diagnostic("RuntimeError: " + reason)["reason"], reason)
            self.assertEqual(classify(ValueError(reason))["reason"], reason)
        self.assertEqual(diagnostic("cpu_headroom: arbitrary private output")["reason"], "launcher_command_failed")

    def test_structured_refusal_numbers_are_bounded_and_private_text_is_rejected(self):
        import json
        from agent_hub_host.launch_diagnostics import diagnostic
        from agent_hub_host.cpu_resources import sanitize_cpu_admission
        valid = {"expected_cpu_cores": 2, "current_cpu_cores": 12, "ramping_cpu_cores": 1,
                 "limit_cpu_cores": 12, "target_utilization": 1, "psi_full_avg10": 6}
        raw = "RuntimeError: cpu_pressure\nCPU_ADMISSION_JSON " + json.dumps(valid)
        self.assertEqual(diagnostic(raw)["cpu"], valid)
        for invalid in [{**valid, "secret": "private"}, {**valid, "current_cpu_cores": float('nan')},
                        {**valid, "psi_full_avg10": 101}, {**valid, "target_utilization": 0},
                        {**valid, "oom_kills_delta": True}, {**valid, "observed_at": "2026-10-26T00:00:00"}]:
            self.assertIsNone(sanitize_cpu_admission(invalid))
            self.assertNotIn("cpu", diagnostic("RuntimeError: cpu_pressure\nCPU_ADMISSION_JSON " + json.dumps(invalid)))
        self.assertNotIn("cpu", diagnostic("cpu_pressure\nCPU_ADMISSION_JSON {broken"))

    def test_iowait_and_guest_not_counted_as_extra_busy(self):
        before = {"cores": 12, "ticks": [0] * 8}
        after = {"cores": 12, "ticks": [10, 0, 0, 10, 80, 0, 0, 0]}
        result = cpu_delta(before, after)
        self.assertEqual(result["busy_percent"], 10)
        self.assertAlmostEqual(result["busy_cores"], 1.2)
        self.assertEqual(result["cores"], 12)
        self.assertIsNone(cpu_delta(after, before))
        self.assertIsNone(cpu_delta(before, before))
        self.assertIsNone(cpu_delta(before, {**after, "cores": 6}))

    def test_first_reset_missing_identity_and_out_of_order_are_unknown(self):
        state = {}
        self.assertIsNone(update_cpu_distribution(state, 50, "native:1", 1000)["current_cpu_cores"])
        self.assertEqual(update_cpu_distribution(state, 52, "native:1", 2000), {"current_cpu_cores": 2, "p95_cpu_cores": 2})
        for counter, identity, at in [(1, "native:1", 3000), (2, "native:2", 4000), (None, "native:2", 5000), (4, "native:2", 3500)]:
            self.assertIsNone(update_cpu_distribution(state, counter, identity, at)["current_cpu_cores"])
        self.assertEqual(state["cpu_baseline"]["at"], 4000)

    def test_lifetime_percentile_is_time_weighted_and_survives_journal_roundtrip(self):
        import json
        state = {}
        update_cpu_distribution(state, 0, "cg:1", 0)
        update_cpu_distribution(state, .1, "cg:1", 1000)
        result = update_cpu_distribution(state, 99.1, "cg:1", 100000)
        self.assertEqual(result["p95_cpu_cores"], 1)
        restored = json.loads(json.dumps(state))
        self.assertEqual(update_cpu_distribution(restored, None, "cg:1", 101000)["p95_cpu_cores"], 1)
        self.assertLessEqual(len(restored["cpu_histogram"]), 20481)

