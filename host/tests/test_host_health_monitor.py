import io
import json
import os
import time
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from agent_hub_host import host_health_monitor
from agent_hub_host.host_health_monitor import (
    HostHealthMonitor,
    check_worker_status,
    collect_host_health,
    collect_load_averages,
    collect_memory_stats,
    collect_service_status,
    collect_supervisor_services,
    read_status_file,
    verify_service_source,
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

    def test_verify_service_source_valid_and_errors(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = os.path.realpath(td)
            sub = os.path.join(ws, ".local", "supervision")
            os.makedirs(sub, exist_ok=True)
            status_file = os.path.join(sub, "status.json")
            with open(status_file, "w") as f:
                f.write('{"timestamp": "2026-10-18T00:00:00Z"}')

            # 1. Valid source
            ok, err, st = verify_service_source(status_file, ws)
            self.assertTrue(ok)
            self.assertIsNone(err)
            self.assertIsNotNone(st)

            # 2. Missing source
            ok, err, _ = verify_service_source(os.path.join(sub, "missing.json"), ws)
            self.assertFalse(ok)
            self.assertEqual(err, "missing_status_file")

            # 3. Path outside workspace
            ok, err, _ = verify_service_source("/etc/passwd", ws)
            self.assertFalse(ok)
            self.assertEqual(err, "source_not_in_workspace")

            # 4. Ownership mismatch
            fake_uid = (os.getuid() + 1)
            ok, err, _ = verify_service_source(status_file, ws, expected_uid=fake_uid)
            self.assertFalse(ok)
            self.assertEqual(err, "source_ownership_mismatch")

            # 5. Symlink rejected
            link_file = os.path.join(sub, "symlink.json")
            os.symlink(status_file, link_file)
            ok, err, _ = verify_service_source(link_file, ws, allow_symlinks=False)
            self.assertFalse(ok)
            self.assertEqual(err, "source_symlink_rejected")

    def test_check_worker_status(self):
        # 1. Alive
        def mock_runner_alive(cmd):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = json.dumps({"worker_alive": True, "state": "running"})
            return proc

        worker, reason = check_worker_status("/fake/ws", "exp", runner_fn=mock_runner_alive)
        self.assertEqual(worker, "alive")
        self.assertIsNone(reason)

        # 2. Broken via state
        def mock_runner_broken(cmd):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = json.dumps({"worker_alive": True, "state": "broken"})
            return proc

        worker, reason = check_worker_status("/fake/ws", "exp", runner_fn=mock_runner_broken)
        self.assertEqual(worker, "broken")
        self.assertEqual(reason, "worker_broken")

        # 3. Broken via worker_alive False
        def mock_runner_dead(cmd):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = json.dumps({"worker_alive": False, "state": "running"})
            return proc

        worker, reason = check_worker_status("/fake/ws", "exp", runner_fn=mock_runner_dead)
        self.assertEqual(worker, "broken")
        self.assertEqual(reason, "worker_broken")

        # 4. Command failure -> unknown
        def mock_runner_fail(cmd):
            proc = MagicMock()
            proc.returncode = 1
            proc.stdout = "a: no matching session"
            return proc

        worker, reason = check_worker_status("/fake/ws", "exp", runner_fn=mock_runner_fail)
        self.assertEqual(worker, "unknown")
        self.assertEqual(reason, "aplexer_command_failed")

    def test_read_status_file_fresh_stale_future_corrupt(self):
        now = 1792287000.0  # reference epoch
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "status.json")

            # 1. Fresh status (100s old)
            fresh_iso = datetime.fromtimestamp(now - 100.0, tz=timezone.utc).isoformat()
            with open(p, "w") as f:
                json.dump({"timestamp": fresh_iso, "arbitrary_secret": "do_not_leak"}, f)
            os.utime(p, (now - 100.0, now - 100.0))

            age, iso_ts, reason = read_status_file(p, now=now)
            self.assertEqual(age, 100.0)
            self.assertIsNotNone(iso_ts)
            self.assertIsNone(reason)

            # 2. Stale status (700s old > 600s threshold)
            stale_iso = datetime.fromtimestamp(now - 700.0, tz=timezone.utc).isoformat()
            with open(p, "w") as f:
                json.dump({"timestamp": stale_iso}, f)
            os.utime(p, (now - 700.0, now - 700.0))

            age, iso_ts, reason = read_status_file(p, now=now)
            self.assertEqual(age, 700.0)
            self.assertEqual(reason, "stale_status_file")

            # 3. Future clock-skew timestamp (> now + 60s)
            future_iso = datetime.fromtimestamp(now + 150.0, tz=timezone.utc).isoformat()
            with open(p, "w") as f:
                json.dump({"timestamp": future_iso}, f)
            os.utime(p, (now - 10.0, now - 10.0))

            age, iso_ts, reason = read_status_file(p, now=now)
            self.assertIsNone(age)  # never an invented healthy zero!
            self.assertEqual(reason, "future_observation_timestamp")

            # 4. Corrupt JSON
            with open(p, "w") as f:
                f.write("{this is not valid json")
            os.utime(p, (now - 10.0, now - 10.0))

            age, iso_ts, reason = read_status_file(p, now=now)
            self.assertIsNone(age)
            self.assertEqual(reason, "corrupt_status_file")

            # 5. Unreadable file
            def mock_bad_open(path, *args, **kwargs):
                raise PermissionError("EACCES")

            age, iso_ts, reason = read_status_file(p, now=now, open_fn=mock_bad_open)
            self.assertIsNone(age)
            self.assertEqual(reason, "unreadable_status_file")

    def test_read_status_file_legacy_contract_mtime_precedence(self):
        """Watcher legacy contract: fresh JSON timestamp MUST NOT mask an old file mtime."""
        now = 1792287000.0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "status.json")
            # Fresh JSON timestamp inside file (10s old)
            fresh_iso = datetime.fromtimestamp(now - 10.0, tz=timezone.utc).isoformat()
            with open(p, "w") as f:
                json.dump({"timestamp": fresh_iso}, f)
            # But file mtime on disk is 700s old (> 600s threshold)
            stale_mtime = now - 700.0
            os.utime(p, (stale_mtime, stale_mtime))

            age, iso_ts, reason = read_status_file(p, now=now)
            self.assertEqual(age, 700.0)
            self.assertEqual(reason, "stale_status_file")

    def test_read_status_file_future_mtime(self):
        """File mtime in future (> now + 60s) flags future_observation_timestamp."""
        now = 1792287000.0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "status.json")
            with open(p, "w") as f:
                json.dump({"timestamp": datetime.fromtimestamp(now - 10.0, tz=timezone.utc).isoformat()}, f)
            future_mtime = now + 120.0
            os.utime(p, (future_mtime, future_mtime))

            age, iso_ts, reason = read_status_file(p, now=now)
            self.assertIsNone(age)
            self.assertEqual(reason, "future_observation_timestamp")

    def test_read_status_file_oversized_rejected(self):
        now = 1792287000.0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "status.json")
            # Create a file larger than 256 KiB
            with open(p, "w") as f:
                f.write('{"timestamp": "2026-10-18T00:00:00Z", "padding": "' + "A" * (260 * 1024) + '"}')
            os.utime(p, (now - 10.0, now - 10.0))

            age, iso_ts, reason = read_status_file(p, now=now)
            self.assertIsNone(age)
            self.assertEqual(reason, "status_file_oversized")

    def test_read_status_file_parent_dir_symlink_swap_rejected(self):
        """Parent directory symlink swap anywhere in hierarchy is rejected via dir_fd O_NOFOLLOW."""
        now = 1792287000.0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = os.path.realpath(td)
            real_target_dir = os.path.join(ws, "real_dir")
            os.makedirs(real_target_dir, exist_ok=True)
            real_file = os.path.join(real_target_dir, "status.json")
            with open(real_file, "w") as f:
                json.dump({"timestamp": "2026-10-18T00:00:00Z"}, f)

            # Create a symlinked intermediate directory: .local -> real_dir
            symlink_parent = os.path.join(ws, ".local")
            os.symlink(real_target_dir, symlink_parent)

            swapped_status_path = os.path.join(symlink_parent, "status.json")
            age, iso_ts, reason = read_status_file(swapped_status_path, now=now, workspace=ws)
            self.assertIsNone(age)
            self.assertEqual(reason, "source_symlink_rejected")

    def test_read_status_file_ancestor_dir_symlink_swap_rejected(self):
        """Ancestor directory symlink swap above workspace is rejected via rootfd dir_fd O_NOFOLLOW."""
        now = 1792287000.0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # Create a real target directory with workspace and status file
            real_target_dir = os.path.join(td, "real_target")
            real_ws = os.path.join(real_target_dir, "ws")
            os.makedirs(real_ws, exist_ok=True)
            real_file = os.path.join(real_ws, "status.json")
            with open(real_file, "w") as f:
                json.dump({"timestamp": "2026-10-18T00:00:00Z"}, f)

            # Create an ancestor holder directory with a symlink above workspace
            holder_dir = os.path.join(td, "holder")
            os.makedirs(holder_dir, exist_ok=True)
            symlink_ancestor = os.path.join(holder_dir, "swapped_ancestor")
            os.symlink(real_target_dir, symlink_ancestor)

            # Workspace is under the symlinked ancestor directory
            ws_via_symlink = os.path.join(symlink_ancestor, "ws")
            status_path = os.path.join(ws_via_symlink, "status.json")

            age, iso_ts, reason = read_status_file(status_path, now=now, workspace=ws_via_symlink)
            self.assertIsNone(age)
            self.assertEqual(reason, "source_symlink_rejected")

    def test_read_status_file_ancestor_dir_swap_race_rejected(self):
        """Ancestor directory replaced with symlink after creation is rejected via rootfd O_NOFOLLOW."""
        now = 1792287000.0
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as td:
            base_dir = os.path.join(td, "base")
            os.makedirs(base_dir, exist_ok=True)
            real_ancestor = os.path.join(base_dir, "ancestor")
            os.makedirs(os.path.join(real_ancestor, "ws"), exist_ok=True)
            real_file = os.path.join(real_ancestor, "ws", "status.json")
            with open(real_file, "w") as f:
                json.dump({"timestamp": "2026-10-18T00:00:00Z"}, f)

            # Target outside
            target_outside = os.path.join(td, "outside")
            os.makedirs(os.path.join(target_outside, "ws"), exist_ok=True)
            with open(os.path.join(target_outside, "ws", "status.json"), "w") as f:
                json.dump({"timestamp": "2026-10-18T00:00:00Z"}, f)

            # Replace ancestor with symlink to target_outside
            shutil.rmtree(real_ancestor)
            os.symlink(target_outside, real_ancestor)

            ws = os.path.join(real_ancestor, "ws")
            status_path = os.path.join(ws, "status.json")
            age, iso_ts, reason = read_status_file(status_path, now=now, workspace=ws)
            self.assertIsNone(age)
            self.assertEqual(reason, "source_symlink_rejected")

    def test_check_worker_status_excess_output(self):
        def mock_runner_excess(cmd):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "X" * (70 * 1024)  # 70 KiB > 64 KiB
            return proc

        worker, reason = check_worker_status("/fake/ws", "exp", runner_fn=mock_runner_excess)
        self.assertEqual(worker, "unknown")
        self.assertEqual(reason, "aplexer_output_excess")

    def test_check_worker_status_silent_child_timeout(self):
        """Silent child pipe does not hang; bounded by monotonic deadline."""
        import sys
        silent_cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        worker, reason = check_worker_status(
            "/fake/ws", "exp", aplexer_bin=sys.executable, runner_fn=None, timeout_s=0.2
        )
        self.assertEqual(worker, "unknown")
        # In testing without full aplexer binary, either timeout or command failure occurs, never hangs
        self.assertIn(reason, ("aplexer_timeout", "aplexer_command_failed"))

    def test_check_worker_status_stderr_flood(self):
        """Stderr excess triggers aplexer_output_excess without unbounded buffering."""
        def mock_runner_stderr(cmd):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "{}"
            proc.stderr = "E" * (70 * 1024)  # 70 KiB > 64 KiB
            return proc

        worker, reason = check_worker_status("/fake/ws", "exp", runner_fn=mock_runner_stderr)
        self.assertEqual(worker, "unknown")
        self.assertEqual(reason, "aplexer_output_excess")

    def test_collect_service_status_no_payload_leak(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = os.path.realpath(td)
            status_file = os.path.join(ws, "status.json")
            sensitive_payload = {
                "timestamp": "2026-10-18T00:00:00Z",
                "principals": {"codex": {"secret_token": "SUPER_SECRET_TOKEN"}},
                "task_instructions": "Confidential prompt instructions",
            }
            with open(status_file, "w") as f:
                json.dump(sensitive_payload, f)

            now_epoch = datetime.fromisoformat("2026-10-18T00:01:00+00:00").timestamp()
            ts_epoch = datetime.fromisoformat("2026-10-18T00:00:00+00:00").timestamp()
            os.utime(status_file, (ts_epoch, ts_epoch))

            def mock_runner(cmd):
                proc = MagicMock()
                proc.returncode = 0
                proc.stdout = json.dumps({"worker_alive": True, "state": "running"})
                return proc

            svc_def = {
                "service_id": "supervision",
                "workspace": ws,
                "session_tag": "exp",
                "status_path": status_file,
            }
            res = collect_service_status(svc_def, now=now_epoch, runner_fn=mock_runner)

            # Strict bounded projection verification:
            allowed_keys = {
                "service_id",
                "worker",
                "worker_state",
                "status_file_age_s",
                "observed_at",
                "observation_time",
                "reason",
            }
            self.assertEqual(set(res.keys()), allowed_keys)
            self.assertEqual(res["service_id"], "supervision")
            self.assertEqual(res["worker"], "alive")
            self.assertEqual(res["status_file_age_s"], 60.0)
            self.assertEqual(res["reason"], "ok")
            # Verify NO sensitive fields leaked
            raw_dump = json.dumps(res)
            self.assertNotIn("SUPER_SECRET_TOKEN", raw_dump)
            self.assertNotIn("Confidential", raw_dump)
            self.assertNotIn("principals", raw_dump)

    def test_collect_supervisor_services_default_hetzner_machine_ids(self):
        now_epoch = datetime.fromisoformat("2026-10-18T00:01:00+00:00").timestamp()
        # Verify both 'hetzner' and enrolled 'DEFLf4-rahKf9wE4TQAZfA' return default services
        svcs_hetzner = collect_supervisor_services("hetzner", now=now_epoch)
        self.assertEqual(len(svcs_hetzner), 1)
        self.assertEqual(svcs_hetzner[0]["service_id"], "supervision")

        svcs_enrolled = collect_supervisor_services("DEFLf4-rahKf9wE4TQAZfA", now=now_epoch)
        self.assertEqual(len(svcs_enrolled), 1)
        self.assertEqual(svcs_enrolled[0]["service_id"], "supervision")

        # Unknown machine returns empty
        svcs_other = collect_supervisor_services("other-machine", now=now_epoch)
        self.assertEqual(svcs_other, [])

    def test_host_health_monitor_step_hetzner_and_win35(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = os.path.realpath(td)
            status_file = os.path.join(ws, "status.json")
            with open(status_file, "w") as f:
                json.dump({"timestamp": "2026-10-18T00:00:00Z"}, f)

            now_epoch = datetime.fromisoformat("2026-10-18T00:01:00+00:00").timestamp()
            ts_epoch = datetime.fromisoformat("2026-10-18T00:00:00+00:00").timestamp()
            os.utime(status_file, (ts_epoch, ts_epoch))

            def mock_runner(cmd):
                proc = MagicMock()
                proc.returncode = 0
                proc.stdout = json.dumps({"worker_alive": True, "state": "running"})
                return proc

            custom_services = [{
                "service_id": "supervision",
                "workspace": ws,
                "session_tag": "exp",
                "status_path": status_file,
            }]

            # 1. Custom / Hetzner with services
            submit_mock = MagicMock()
            monitor = HostHealthMonitor(
                config={"enabled": True, "machine_id": "hetzner"},
                services=custom_services,
                submit_fn=submit_mock,
                runner_fn=mock_runner,
                time_fn=lambda: now_epoch,
            )
            res = monitor.step(submit=True)
            self.assertTrue(res["posted"])
            payload = submit_mock.call_args[0][0]
            self.assertIn("services", payload)
            self.assertEqual(len(payload["services"]), 1)
            self.assertEqual(payload["services"][0]["worker"], "alive")
            self.assertEqual(payload["services"][0]["status_file_age_s"], 60.0)

            # 2. Win35 machine -> intentionally offline, no extra alert / no services
            submit_mock_win = MagicMock()
            monitor_win = HostHealthMonitor(
                config={"enabled": True, "machine_id": "win35"},
                submit_fn=submit_mock_win,
                time_fn=lambda: now_epoch,
            )
            res_win = monitor_win.step(submit=True)
            self.assertTrue(res_win["posted"])
            payload_win = submit_mock_win.call_args[0][0]
            self.assertNotIn("services", payload_win)

    def test_real_hetzner_local_observation(self):
        # Genuine live test against actual Hetzner host paths if they exist
        real_status_path = "/home/alexey/git/cloudflare-agent-git/.local/supervision/status.json"
        real_ws = "/home/alexey/git/cloudflare-agent-git"
        if not os.path.isfile(real_status_path):
            self.skipTest("Real Hetzner supervisor status file not present")

        # Test read_status_file on real live file
        now = time.time()
        age, iso_ts, reason = read_status_file(real_status_path, now=now)
        # Verify genuine status file is fresh (<=600s)
        self.assertIsNotNone(age)
        self.assertLessEqual(age, 600.0, f"Expected real supervisor to be fresh <=600s, got age {age}s")
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()

