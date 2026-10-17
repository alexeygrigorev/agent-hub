import io
import json
import unittest
from unittest.mock import MagicMock

from agent_hub_host import host_health_monitor
from agent_hub_host.host_health_monitor import (
    HostHealthMonitor,
    collect_host_health,
    collect_load_averages,
    collect_memory_stats,
)

SAMPLE_MEMINFO = """MemTotal:       16283724 kB
MemFree:         2134560 kB
MemAvailable:    8219484 kB
Buffers:          345600 kB
Cached:          5678900 kB
SwapTotal:       2097148 kB
SwapFree:        2097148 kB
"""

SAMPLE_MEMINFO_CRITICAL = """MemTotal:       10000000 kB
MemFree:          100000 kB
MemAvailable:     400000 kB
Buffers:           50000 kB
Cached:           250000 kB
"""

SAMPLE_MEMINFO_NO_AVAILABLE = """MemTotal:       10000000 kB
MemFree:         1000000 kB
Buffers:         1000000 kB
Cached:          2000000 kB
"""


class TestHostHealthMonitor(unittest.TestCase):
    def test_collect_load_averages(self):
        mock_load = lambda: (0.456, 1.234, 2.501)
        loads = collect_load_averages(getloadavg_fn=mock_load)
        self.assertEqual(loads, {"load1": 0.46, "load5": 1.23, "load15": 2.5})

    def test_collect_load_averages_exception(self):
        def bad_load():
            raise OSError("load error")

        loads = collect_load_averages(getloadavg_fn=bad_load)
        self.assertEqual(loads, {"load1": 0.0, "load5": 0.0, "load15": 0.0})

    def test_collect_memory_stats(self):
        def mock_open(path, *args, **kwargs):
            return io.StringIO(SAMPLE_MEMINFO)

        mem = collect_memory_stats(open_fn=mock_open)
        self.assertEqual(mem["total_bytes"], 16283724 * 1024)
        self.assertEqual(mem["available_bytes"], 8219484 * 1024)
        expected_used = (16283724 - 8219484) * 1024
        self.assertEqual(mem["used_bytes"], expected_used)
        expected_pct = round((expected_used / (16283724 * 1024)) * 100.0, 1)
        self.assertEqual(mem["percent"], expected_pct)
        self.assertEqual(mem["status"], "ok")

    def test_collect_memory_stats_critical(self):
        def mock_open(path, *args, **kwargs):
            return io.StringIO(SAMPLE_MEMINFO_CRITICAL)

        mem = collect_memory_stats(open_fn=mock_open)
        # used = 10000000 - 400000 = 9600000 => 96%
        self.assertEqual(mem["percent"], 96.0)
        self.assertEqual(mem["status"], "critical")

    def test_collect_memory_stats_fallback(self):
        def mock_open(path, *args, **kwargs):
            return io.StringIO(SAMPLE_MEMINFO_NO_AVAILABLE)

        mem = collect_memory_stats(open_fn=mock_open)
        # fallback avail = 1000000 + 1000000 + 2000000 = 4000000 kB => 40% avail => 60% used
        self.assertEqual(mem["available_bytes"], 4000000 * 1024)
        self.assertEqual(mem["percent"], 60.0)
        self.assertEqual(mem["status"], "ok")

    def test_collect_memory_missing_meminfo(self):
        def mock_open(path, *args, **kwargs):
            raise FileNotFoundError("no meminfo")

        mem = collect_memory_stats(open_fn=mock_open)
        self.assertEqual(mem["status"], "unknown")
        self.assertEqual(mem["total_bytes"], 0)
        self.assertEqual(mem["error"], "meminfo_unavailable")

    def test_collect_host_health(self):
        def mock_open(path, *args, **kwargs):
            return io.StringIO(SAMPLE_MEMINFO)

        payload = collect_host_health(
            machine_id="test-host-1",
            getloadavg_fn=lambda: (0.1, 0.2, 0.3),
            open_fn=mock_open,
            time_fn=lambda: "2026-10-17T20:00:00.000Z",
        )
        self.assertEqual(payload["machine_id"], "test-host-1")
        self.assertEqual(payload["observed_at"], "2026-10-17T20:00:00.000Z")
        self.assertEqual(payload["load"]["load1"], 0.1)
        self.assertEqual(payload["memory"]["status"], "ok")

    def test_host_health_monitor_step_disabled(self):
        monitor = HostHealthMonitor(config={"enabled": False})
        res = monitor.step()
        self.assertTrue(res["disabled"])
        self.assertFalse(res["posted"])

    def test_host_health_monitor_step_rate_limit(self):
        submit_mock = MagicMock()
        mono_time = [100.0]

        def mock_mono():
            return mono_time[0]

        def mock_open(path, *args, **kwargs):
            return io.StringIO(SAMPLE_MEMINFO)

        monitor = HostHealthMonitor(
            config={"enabled": True, "machine_id": "test-host", "min_interval_s": 60},
            submit_fn=submit_mock,
            monotonic_fn=mock_mono,
            open_fn=mock_open,
        )

        res1 = monitor.step(submit=True)
        self.assertTrue(res1["posted"])
        self.assertEqual(submit_mock.call_count, 1)

        # Immediate next step -> rate limited
        mono_time[0] += 30.0
        res2 = monitor.step(submit=True)
        self.assertTrue(res2["rate_limited"])
        self.assertFalse(res2["posted"])
        self.assertEqual(submit_mock.call_count, 1)

        # After min_interval_s (60s) -> allowed
        mono_time[0] += 31.0
        res3 = monitor.step(submit=True)
        self.assertFalse(res3["rate_limited"])
        self.assertTrue(res3["posted"])
        self.assertEqual(submit_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
