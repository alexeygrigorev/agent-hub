"""Unit tests never read the shared machine's live load (736ba282 AC6h).

Bare ``unittest discover`` has no pytest autouse fixture, so on a busy host
(load 22 > 1.5 x 12 cores) dispatch tests failed with host_load before the
path they exercise. The package import hook installs the same stable reader.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from agent_hub_host import host_load

HOST = Path(__file__).resolve().parents[1]
PROBE = ("import os, agent_hub_host.host_load as h; "
         "print('real' if h.os is os else 'stub', h.os.getloadavg()[0] if h.os is not os else '')")


def run_probe(prefix):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_TEST_GUARD")}
    env["PYTHONPATH"] = str(HOST)
    result = subprocess.run([sys.executable, "-c", prefix + PROBE], cwd=HOST, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.split()[0]


class UnittestHostLoadIsolationTests(unittest.TestCase):
    def test_test_runner_process_reads_a_stable_idle_host(self):
        status = host_load.live_load_status()
        self.assertEqual((status.get("load1"), status.get("cores")), (0.0, 12))
        self.assertEqual(run_probe("import unittest; "), "stub")

    def test_process_without_a_test_runner_keeps_the_real_reader(self):
        self.assertEqual(run_probe(""), "real")

    def test_injected_samples_override_the_stub(self):
        status = host_load.live_load_status(loadavg_fn=lambda: (40.0, 30.0, 20.0),
                                            cpu_count_fn=lambda: 12)
        self.assertEqual(status.get("load1"), 40.0)
        with self.assertRaisesRegex(ValueError, "host_overload"):
            host_load.check_live_load(loadavg_fn=lambda: (40.0, 30.0, 20.0), cpu_count_fn=lambda: 12)
        # An explicit patch of the module reader (the pattern of the dedicated
        # admission tests) also wins over the stub, and the stub comes back.
        from types import SimpleNamespace
        from unittest.mock import patch
        with patch.object(host_load, "os", SimpleNamespace(getloadavg=lambda: (25.0, 1.0, 1.0),
                                                           cpu_count=lambda: 12)):
            self.assertTrue(host_load.live_load_status()["overloaded"])
        self.assertFalse(host_load.live_load_status()["overloaded"])


if __name__ == "__main__":
    unittest.main()
