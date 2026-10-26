import subprocess
import unittest

from agent_hub_host.host_health_monitor import HostHealthMonitor, collect_process_stats, collect_host_health


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

    def test_monitor_step_retries_without_processes_on_submit_failure_and_restarts_stale_supervision(self):
        submitted = []
        restarted = []

        def submit_fn(payload, endpoint="/api/monitoring/health"):
            submitted.append(dict(payload))
            if "processes" in payload:
                raise RuntimeError("HTTP 400: invalid_processes")
            return {"ok": True}

        mon = HostHealthMonitor(
            config={"enabled": True, "machine_id": "hetzner", "min_interval_s": 60},
            submit_fn=submit_fn,
            getloadavg_fn=lambda: (0.5, 0.5, 0.5),
            services=[
                {
                    "service_id": "supervision",
                    "workspace": "/nonexistent-ws",
                    "session_tag": "experiment-supervision",
                    "status_path": "/nonexistent-ws/status.json",
                }
            ],
            runner_fn=lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout='{"worker_alive": false, "state": "broken"}', stderr=""),
            restart_fn=lambda svc_id: (restarted.append(svc_id) or True),
            monotonic_fn=lambda: 1000.0,
            time_fn=lambda: 1700000000.0,
        )
        res = mon.step(submit=True)
        self.assertTrue(res["posted"])
        self.assertEqual(res["errors"], [])
        self.assertEqual(len(submitted), 2)
        self.assertIn("processes", submitted[0])
        self.assertNotIn("processes", submitted[1])
        self.assertEqual(restarted, ["supervision"])
        self.assertEqual(res["restarted_services"], ["supervision"])


if __name__ == "__main__":
    unittest.main()
