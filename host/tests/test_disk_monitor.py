"""Unit tests for host/agent_hub_host/disk_monitor.py and related status/session hooks."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_hub_host import disk_monitor, session_host, status


class TestScanTopConsumers(unittest.TestCase):
    def test_respects_device_boundary(self):
        # Mock filesystem where one entry has a different dev_id
        items = ["file1.txt", "file2.txt", "mounted_subdir"]

        def mock_listdir(path):
            if path == "/test":
                return items
            return []

        def mock_stat(path):
            if path == "/test/file1.txt":
                return SimpleNamespace(st_dev=100, st_size=1024)
            elif path == "/test/file2.txt":
                return SimpleNamespace(st_dev=100, st_size=2048)
            elif path == "/test/mounted_subdir":
                # Different filesystem device!
                return SimpleNamespace(st_dev=999, st_size=4096)
            return SimpleNamespace(st_dev=100, st_size=0)

        results, truncated = disk_monitor.scan_top_consumers(
            "/test",
            100,
            stat_fn=mock_stat,
            listdir_fn=mock_listdir,
            max_depth=2,
            limit=10,
        )
        self.assertFalse(truncated)
        paths = [r["path"] for r in results]
        self.assertIn("/test/file2.txt", paths)
        self.assertIn("/test/file1.txt", paths)
        self.assertNotIn("/test/mounted_subdir", paths)
        # Order should be descending size
        self.assertEqual(results[0]["path"], "/test/file2.txt")
        self.assertEqual(results[0]["size_bytes"], 2048)

    def test_respects_limit(self):
        items = [f"f{i}.dat" for i in range(20)]

        def mock_listdir(path):
            return items

        def mock_stat(path):
            idx = int(path.split("f")[-1].split(".")[0])
            return SimpleNamespace(st_dev=100, st_size=idx * 100)

        results, truncated = disk_monitor.scan_top_consumers(
            "/test",
            100,
            stat_fn=mock_stat,
            listdir_fn=mock_listdir,
            limit=5,
        )
        self.assertEqual(len(results), 5)
        # Largest should be f19.dat
        self.assertEqual(results[0]["path"], "/test/f19.dat")
        self.assertEqual(results[0]["size_bytes"], 1900)

    def test_timeout_bounds_scan(self):
        # Monotonic time advances past timeout
        times = [0.0, 0.5, 1.0, 2.5, 3.0]
        time_iter = iter(times)

        def mock_monotonic():
            try:
                return next(time_iter)
            except StopIteration:
                return 10.0

        def mock_listdir(path):
            return ["a.txt", "b.txt", "c.txt"]

        def mock_stat(path):
            return SimpleNamespace(st_dev=100, st_size=100)

        results, truncated = disk_monitor.scan_top_consumers(
            "/test",
            100,
            stat_fn=mock_stat,
            listdir_fn=mock_listdir,
            timeout_s=1.0,
            monotonic_fn=mock_monotonic,
        )
        self.assertTrue(truncated)


class TestDiskMonitor(unittest.TestCase):
    def test_load_active_config_defaults(self):
        monitor = disk_monitor.DiskMonitor(
            config={
                "enabled": True,
                "machine_id": "hetzner-1",
            }
        )
        cfg = monitor.load_active_config()
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["machine_id"], "hetzner-1")
        self.assertEqual(cfg["min_interval_s"], 60)
        self.assertEqual(cfg["warn_threshold_percent"], 85.0)
        self.assertEqual(cfg["critical_threshold_percent"], 95.0)
        self.assertIn("/", cfg["mounts"])
        self.assertIn("/data", cfg["mounts"])

    def test_load_active_config_disabled(self):
        monitor = disk_monitor.DiskMonitor(
            config={"enabled": False, "machine_id": "h1"}
        )
        self.assertIsNone(monitor.load_active_config())

        # Missing file config
        monitor2 = disk_monitor.DiskMonitor(config_path="/nonexistent/path/disk.json")
        self.assertIsNone(monitor2.load_active_config())

    def test_collect_thresholds_and_duplicate_devices(self):
        # Dev 100 on / and /home (bind mount), Dev 200 on /data
        def mock_stat(path):
            if path in ("/", "/home"):
                return SimpleNamespace(st_dev=100, st_size=4096)
            elif path == "/data":
                return SimpleNamespace(st_dev=200, st_size=4096)
            elif path == "/tmp":
                return SimpleNamespace(st_dev=300, st_size=4096)
            raise FileNotFoundError(path)

        def mock_statvfs(path):
            # Block size 4096
            frsize = 4096
            if path in ("/", "/home"):
                # Total: 100 blocks = 409600 bytes. Free: 30 blocks. Used: 70 blocks (70% ok)
                return SimpleNamespace(f_frsize=frsize, f_blocks=100, f_bfree=30, f_bavail=30)
            elif path == "/data":
                # Total: 100 blocks. Free: 4 blocks. Used: 96 blocks (96% critical >= 95%)
                return SimpleNamespace(f_frsize=frsize, f_blocks=100, f_bfree=4, f_bavail=4)
            elif path == "/tmp":
                # Total: 100 blocks. Free: 12 blocks. Used: 88 blocks (88% warn >= 85%)
                return SimpleNamespace(f_frsize=frsize, f_blocks=100, f_bfree=12, f_bavail=12)
            raise FileNotFoundError(path)

        monitor = disk_monitor.DiskMonitor(
            config={
                "enabled": True,
                "machine_id": "test-box",
                "mounts": ["/", "/home", "/data", "/tmp"],
                "allowed_roots": ["/data", "/tmp"],
            },
            stat_fn=mock_stat,
            statvfs_fn=mock_statvfs,
        )

        cfg = monitor.load_active_config()
        payload, errors = monitor.collect(cfg)
        self.assertEqual(len(errors), 0)
        self.assertEqual(payload["machine_id"], "test-box")
        fs_list = payload["filesystems"]
        self.assertEqual(len(fs_list), 4)

        root_fs = next(f for f in fs_list if f["mount_point"] == "/")
        self.assertTrue(root_fs["is_primary"])
        self.assertEqual(root_fs["status"], "ok")
        self.assertEqual(root_fs["percent"], 70.0)

        home_fs = next(f for f in fs_list if f["mount_point"] == "/home")
        self.assertFalse(home_fs["is_primary"])
        self.assertEqual(home_fs["primary_mount"], "/")

        data_fs = next(f for f in fs_list if f["mount_point"] == "/data")
        self.assertTrue(data_fs["is_primary"])
        self.assertEqual(data_fs["status"], "critical")
        self.assertEqual(data_fs["percent"], 96.0)

        tmp_fs = next(f for f in fs_list if f["mount_point"] == "/tmp")
        self.assertTrue(tmp_fs["is_primary"])
        self.assertEqual(tmp_fs["status"], "warn")
        self.assertEqual(tmp_fs["percent"], 88.0)

    def test_step_rate_limiting_and_submit(self):
        submit_mock = MagicMock()
        mono_time = [100.0, 110.0, 200.0]
        mono_iter = iter(mono_time)

        def mock_stat(path):
            return SimpleNamespace(st_dev=100, st_size=4096)

        def mock_statvfs(path):
            return SimpleNamespace(f_frsize=4096, f_blocks=100, f_bfree=50, f_bavail=50)

        monitor = disk_monitor.DiskMonitor(
            config={
                "enabled": True,
                "machine_id": "test-box",
                "min_interval_s": 60,
                "mounts": ["/"],
            },
            submit_fn=submit_mock,
            stat_fn=mock_stat,
            statvfs_fn=mock_statvfs,
            monotonic_fn=lambda: next(mono_iter),
        )

        # Step 1 at t=100.0 -> runs
        res1 = monitor.step(submit=True)
        self.assertFalse(res1["rate_limited"])
        self.assertTrue(res1["posted"])
        submit_mock.assert_called_once()

        # Step 2 at t=110.0 (10s later < 60s) -> rate limited
        res2 = monitor.step(submit=True)
        self.assertTrue(res2["rate_limited"])
        self.assertEqual(submit_mock.call_count, 1)

        # Step 3 at t=200.0 (90s later >= 60s) -> runs
        res3 = monitor.step(submit=True)
        self.assertFalse(res3["rate_limited"])
        self.assertTrue(res3["posted"])
        self.assertEqual(submit_mock.call_count, 2)


class TestSessionHostDiskThread(unittest.TestCase):
    def test_single_inflight_thread(self):
        import threading
        event = threading.Event()
        collector = MagicMock()
        collector.step.side_effect = lambda **kw: event.wait(timeout=2.0)
        thread1 = session_host.maybe_start_disk_thread(None, collector)
        self.assertIsNotNone(thread1)
        # While alive, starting again returns same thread
        thread2 = session_host.maybe_start_disk_thread(thread1, collector)
        self.assertIs(thread1, thread2)
        event.set()
        thread1.join(timeout=1.0)


class TestStatusRendering(unittest.TestCase):
    def test_render_human_includes_disk_and_needs_attention(self):
        data = {
            "tasks": [],
            "needs_attention": [
                {
                    "severity": "critical",
                    "subject": "hetzner-1:/data",
                    "detail": "/data is at 96.5% (965GB / 1000GB)",
                }
            ],
            "disk": [
                {
                    "machine_id": "hetzner-1",
                    "mount_point": "/data",
                    "percent": 96.5,
                    "status": "critical",
                    "is_primary": True,
                    "growth_per_day_bytes": 10 * 1024 * 1024 * 1024,  # +10 GB/day
                },
                {
                    "machine_id": "hetzner-1",
                    "mount_point": "/home",
                    "percent": 65.0,
                    "status": "ok",
                    "is_primary": False,
                    "primary_mount": "/",
                },
            ],
        }
        text = status.render_human(data)
        self.assertIn("needs attention: 1", text)
        self.assertIn("[critical] hetzner-1:/data: /data is at 96.5%", text)
        self.assertIn("disk:", text)
        self.assertIn("hetzner-1 /data: 96.5% (critical) growth=+10.0GB/day", text)
        self.assertIn("hetzner-1 /home: 65.0% (ok) (alias of /)", text)


if __name__ == "__main__":
    unittest.main()
