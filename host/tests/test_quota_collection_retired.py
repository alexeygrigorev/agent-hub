import os
import subprocess
import sys
import unittest


HOST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestQuotaCollectionRetired(unittest.TestCase):
    def test_session_controller_has_no_quota_collector(self):
        from agent_hub_host import session_host
        self.assertFalse(hasattr(session_host, "QuotaCollector"))
        self.assertFalse(hasattr(session_host, "resolve_quse_cmd"))

    def test_host_quota_collector_and_post_helpers_are_absent(self):
        from agent_hub_host import quota_snapshot
        self.assertFalse(hasattr(quota_snapshot, "QuotaCollector"))
        self.assertFalse(hasattr(quota_snapshot, "post_quota_snapshot"))
        self.assertFalse(hasattr(quota_snapshot, "get_quota_snapshot"))

    def test_standalone_host_collector_refuses_without_running(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = HOST_DIR
        result = subprocess.run(
            [sys.executable, "-m", "agent_hub_host.quota_snapshot", "--once"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Per-machine quota collection is retired", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
