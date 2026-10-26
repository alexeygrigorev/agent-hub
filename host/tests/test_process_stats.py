import unittest

from agent_hub_host.host_health_monitor import collect_process_stats, collect_host_health


class TestProcessStats(unittest.TestCase):
    def test_collect_process_stats_counts_and_forks_rate(self):
        files = {
            "stat": "cpu  1 2 3\nprocesses 1000\nprocs_running 2\n",
            "1/status": "Name:\tinit\nThreads:\t1\n",
            "42/status": "Name:\tworker\nThreads:\t8\n",
        }

        def listdir(p):
            if p == "/proc":
                return ["1", "42", "self", "sys"]
            raise OSError("nope")

        def read_text(path):
            key = path.replace("/proc/", "")
            if key in files:
                return files[key]
            raise OSError("missing")

        first = collect_process_stats("/proc", listdir_fn=listdir, read_text_fn=read_text, now_mono=1000.0)
        self.assertEqual(first["process_count"], 2)
        self.assertEqual(first["thread_count"], 9)
        self.assertEqual(first["forks_total"], 1000)
        self.assertIsNone(first["forks_per_sec"])

        files["stat"] = "cpu  1 2 3\nprocesses 1600\nprocs_running 2\n"
        second = collect_process_stats(
            "/proc", prev={"forks_total": 1000, "at_mono": 1000.0},
            listdir_fn=listdir, read_text_fn=read_text, now_mono=1010.0,
        )
        self.assertEqual(second["forks_per_sec"], 60.0)

    def test_collect_process_stats_missing_degrades(self):
        res = collect_process_stats("/nonexistent-proc-xyz", listdir_fn=lambda p: (_ for _ in ()).throw(OSError("x")))
        self.assertIsNone(res["process_count"])
        self.assertIsNone(res["thread_count"])
        self.assertIsNone(res["forks_total"])
        self.assertIsNone(res["forks_per_sec"])

    def test_collect_host_health_includes_processes(self):
        payload = collect_host_health(
            "hetzner",
            getloadavg_fn=lambda: (0.1, 0.2, 0.3),
            meminfo_path="/nonexistent-meminfo",
            open_fn=lambda *a, **k: (_ for _ in ()).throw(OSError("x")),
            processes={"process_count": 123, "thread_count": 456, "forks_total": 789, "forks_per_sec": 1.5},
        )
        self.assertEqual(payload["processes"]["process_count"], 123)
        self.assertEqual(payload["processes"]["forks_per_sec"], 1.5)


if __name__ == "__main__":
    unittest.main()
