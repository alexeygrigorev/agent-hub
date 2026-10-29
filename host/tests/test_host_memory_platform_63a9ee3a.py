"""Intake 63a9ee3a: Windows/macOS hosts read real memory; unknown is never 0."""
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host import api, host_health_monitor, host_memory

GIB = 1024 ** 3
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               100000.
Pages active:                             300000.
Pages inactive:                           150000.
Pages speculative:                         10000.
Pages purgeable:                            5000.
"""


def runner(outputs):
    def run(argv, **_kw):
        out = outputs.get(argv[0])
        return SimpleNamespace(returncode=0 if out is not None else 1, stdout=out or "")
    return run


class HostMemoryPlatform(unittest.TestCase):
    def test_windows_uses_global_memory_status(self):
        reading = host_memory.read_host_memory(system="Windows",
            windows_api=lambda: {"total_bytes": 16 * GIB, "available_bytes": 5 * GIB, "source": "global_memory_status_ex"})
        self.assertEqual(reading["available_bytes"], 5 * GIB)

    def test_windows_falls_back_to_cim(self):
        reading = host_memory.read_host_memory(system="Windows", windows_api=lambda: None,
            run_fn=runner({"powershell.exe": "16363452 4816896\r\n"}))
        self.assertEqual(reading["source"], "cim_win32_operating_system")
        self.assertEqual(reading["total_bytes"], 16363452 * 1024)
        self.assertEqual(reading["available_bytes"], 4816896 * 1024)

    def test_macos_vm_stat(self):
        reading = host_memory.read_host_memory(system="Darwin",
            run_fn=runner({"sysctl": "17179869184\n", "vm_stat": VM_STAT}))
        self.assertEqual(reading["total_bytes"], 16 * GIB)
        self.assertEqual(reading["available_bytes"], (100000 + 150000 + 10000 + 5000) * 16384)

    def test_unreadable_is_none(self):
        self.assertIsNone(host_memory.read_host_memory(system="Windows", windows_api=lambda: None, run_fn=runner({})))
        self.assertIsNone(host_memory.read_host_memory(system="Darwin", run_fn=runner({})))
        self.assertIsNone(host_memory.read_host_memory(system="Linux", meminfo_path="/nonexistent/meminfo"))

    def test_health_monitor_uses_native_reader_without_proc(self):
        def no_proc(*_a, **_k):
            raise FileNotFoundError
        stats = host_health_monitor.collect_memory_stats(open_fn=no_proc,
            platform_reader=lambda: {"total_bytes": 16 * GIB, "available_bytes": 5 * GIB, "source": "x"})
        self.assertEqual(stats["total_bytes"], 16 * GIB)
        self.assertEqual(stats["available_bytes"], 5 * GIB)
        self.assertNotIn("error", stats)
        unknown = host_health_monitor.collect_memory_stats(open_fn=no_proc, platform_reader=lambda: None)
        self.assertEqual(unknown["error"], "meminfo_unavailable")

    def test_heartbeat_reports_null_not_zero_when_unknown(self):
        with patch.object(host_memory, "read_host_memory", return_value=None):
            hb = api.get_heartbeat_payload()
        self.assertIsNone(hb["mem_avail_mb"])
        self.assertNotIn("mem_total_mb", hb)
        with patch.object(host_memory, "read_host_memory",
                          return_value={"total_bytes": 16 * GIB, "available_bytes": 5 * GIB, "source": "x"}):
            hb = api.get_heartbeat_payload()
        self.assertEqual(hb["mem_avail_mb"], 5 * 1024)
        self.assertEqual(hb["mem_total_mb"], 16 * 1024)

    def test_linux_proc_meminfo(self):
        text = "MemTotal: 16000000 kB\nMemFree: 1000 kB\nMemAvailable: 5000000 kB\n"
        reading = host_memory.read_proc_meminfo(open_fn=lambda *a, **k: io.StringIO(text))
        self.assertEqual(reading["available_bytes"], 5000000 * 1024)


if __name__ == "__main__":
    unittest.main()
