import unittest

from agent_hub_host import resources
from agent_hub_host.resource_management import _update_rss_median, _sample_row, update_peaks


class RssMedianTests(unittest.TestCase):
    def test_median_tracks_typical_size_not_peak(self):
        prior = {}
        for rss in (100, 100, 100, 900):
            prior.update(_update_rss_median(prior, rss))
        self.assertLessEqual(prior["p50_rss_mb"], 112)  # 16 MiB upper bucket
        self.assertGreaterEqual(prior["p50_rss_mb"], 100)

    def test_unknown_samples_never_become_zero(self):
        out = _update_rss_median({}, None)
        self.assertIsNone(out["p50_rss_mb"])
        out = _update_rss_median({}, 0)
        self.assertIsNone(out["p50_rss_mb"])

    def test_frozen_session_stops_counting(self):
        state = _update_rss_median({}, 64)
        state = _update_rss_median(state, 4000, frozen=True)
        self.assertEqual(state["p50_rss_mb"], 64.0)

    def test_median_reaches_report_row(self):
        state = {"peaks": {}}
        peak = update_peaks(state, "s", "n", {"rss_mb": 200.0}, "2026-11-02T10:00:00.000Z")
        row = _sample_row("s", "n", peak, "2026-11-02T10:00:00.000Z")
        self.assertEqual(row["p50_rss_mb"], 208.0)
        self.assertEqual(row["peak_rss_mb"], 200.0)


class OomCounterTests(unittest.TestCase):
    def test_oom_kills_are_monotonic_and_reported(self):
        state = {"peaks": {}}
        update_peaks(state, "s", "n", {"rss_mb": 200.0, "oom_kills": 3}, "2026-11-02T10:00:00.000Z")
        peak = update_peaks(state, "s", "n", {"rss_mb": 100.0, "oom_kills": None}, "2026-11-02T10:01:00.000Z")
        self.assertEqual(peak["oom_kills"], 3)
        row = _sample_row("s", "n", peak, "2026-11-02T10:01:00.000Z")
        self.assertEqual(row["oom_kills"], 3)

    def test_ledger_line_shows_oom_count(self):
        text = resources.render_text({"sessions": [{"session_id": "s", "oom_kills": 4}]}, "ledger")
        self.assertIn("oom=4", text)


class LedgerRenderTests(unittest.TestCase):
    def test_ledger_has_one_line_per_task_with_all_fields(self):
        text = resources.render_text({"sessions": [{
            "session_id": "sess-1", "intake_id": "6aadd6c9-1c87", "project_label": "agent-hub", "engine": "claude",
            "model": "sonnet", "machine_id": "M1", "state": "exited", "runtime_s": 125,
            "peak_rss_mb": 900, "p50_rss_mb": 300, "avg_cpu_cores": 0.4, "p95_cpu_cores": 1.2}]}, "ledger")
        for want in ("task=6aadd6c9", "project=agent-hub", "engine=claude", "model=sonnet", "machine=M1",
                     "rss_peak=900 MiB", "rss_p50=300 MiB", "cpu_avg=0.40", "cpu_p95=1.20", "runtime=2m5s"):
            self.assertIn(want, text)

    def test_headroom_shows_full_reason_and_room(self):
        hosts = [{"capacity": {"machine_id": "WIN", "cores": 8, "cpu_busy_cores": 1.0, "cpu_busy_percent": 12.5,
                               "cpu_busy_percent_10s": 10, "free_cores": 7, "mem_available_mb": 20000, "mem_total_mb": 32000,
                               "full": False, "full_reasons": [], "telemetry_state": "measured", "est_cpu_per_task": 1.25,
                               "est_mem_per_task_mb": 1200, "extra_tasks_by_cpu": 5, "extra_tasks_by_memory": 11, "extra_tasks": 5}},
                 {"capacity": {"machine_id": "FULLBOX", "cores": 12, "cpu_busy_cores": 12, "free_cores": 0,
                               "mem_available_mb": 4000, "full": True, "full_reasons": ["mem_available_below_6144mb"],
                               "extra_tasks": 0}}]
        text = resources.render_text({"hosts": hosts}, "headroom")
        self.assertIn("WIN has-room", text)
        self.assertIn("telemetry=measured", text)
        self.assertIn("=> 5", text)
        self.assertIn("FULLBOX FULL(mem_available_below_6144mb)", text)

    def test_headroom_without_hosts_is_explicit(self):
        self.assertEqual(resources.render_text({"hosts": []}, "headroom"), "No host capacity reported.")

    def test_views_are_registered(self):
        import argparse
        parser = argparse.ArgumentParser()
        resources.add_commands(parser.add_subparsers())
        for view in ("ledger", "headroom"):
            self.assertEqual(parser.parse_args(["resources", view]).view, view)


if __name__ == "__main__":
    unittest.main()
