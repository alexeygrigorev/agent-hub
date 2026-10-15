import os
import subprocess
import sys
import unittest

HOST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

class TestCliIsolated(unittest.TestCase):
    def test_cli_main_help_isolated_subprocess(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = HOST_DIR
        res = subprocess.run(
            [sys.executable, "-m", "agent_hub_host.cli", "--help"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Agent Hub CLI", res.stdout)
        self.assertIn("run", res.stdout)
        self.assertIn("status", res.stdout)
        self.assertIn("login", res.stdout)
        self.assertIn("send", res.stdout)
        self.assertIn("inbox", res.stdout)
        self.assertIn("claim", res.stdout)

    def test_cli_subcommands_help_no_network_or_config(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = HOST_DIR
        for subcmd in [
            "status", "login", "machines", "run", "send", "inbox",
            "show", "ack", "accept", "complete", "reply", "claim",
            "release", "claims"
        ]:
            res = subprocess.run(
                [sys.executable, "-m", "agent_hub_host.cli", subcmd, "--help"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(res.returncode, 0, f"Failed for subcommand {subcmd}")
            self.assertIn("usage:", res.stdout)

    def test_isolated_subprocess_import_bus(self):
        """Verify bus module can be imported in a fresh subprocess without circular import."""
        code = "import sys; sys.path.insert(0, sys.argv[1]); from agent_hub_host import bus; assert hasattr(bus, 'send')"
        res = subprocess.run(
            [sys.executable, "-c", code, HOST_DIR],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"Import failed: {res.stderr}")

    def test_isolated_subprocess_import_bridge(self):
        """Verify bridge module can be imported in a fresh subprocess without circular import."""
        code = "import sys; sys.path.insert(0, sys.argv[1]); from agent_hub_host import bridge; assert hasattr(bridge, 'MailboxBridge')"
        res = subprocess.run(
            [sys.executable, "-c", code, HOST_DIR],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"Import failed: {res.stderr}")

    def test_isolated_subprocess_import_native_bridge(self):
        """Verify native_bridge module can be imported in a fresh subprocess without circular import."""
        code = "import sys; sys.path.insert(0, sys.argv[1]); from agent_hub_host import native_bridge; assert hasattr(native_bridge, 'qualify_native_service_binding')"
        res = subprocess.run(
            [sys.executable, "-c", code, HOST_DIR],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"Import failed: {res.stderr}")

    def test_isolated_subprocess_import_api(self):
        """Verify HostAgent can be imported in a fresh subprocess without circular import."""
        code = "import sys; sys.path.insert(0, sys.argv[1]); from agent_hub_host.api import HostAgent; assert HostAgent is not None"
        res = subprocess.run(
            [sys.executable, "-c", code, HOST_DIR],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"Import failed: {res.stderr}")

    def test_isolated_subprocess_import_auth(self):
        """Verify auth module can be imported in a fresh subprocess without circular import."""
        code = "import sys; sys.path.insert(0, sys.argv[1]); import agent_hub_host.auth as a; assert hasattr(a, 'get_access_token')"
        res = subprocess.run(
            [sys.executable, "-c", code, HOST_DIR],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"Import failed: {res.stderr}")

if __name__ == "__main__":
    unittest.main()


class TestHeartbeatCommand(unittest.TestCase):
    def test_heartbeat_posts_status_and_summary_only(self):
        from unittest.mock import patch
        from agent_hub_host import cli, bus
        calls = []
        with patch.object(bus, '_do_request', side_effect=lambda m, p, b=None: calls.append((m, p, b)) or {'status': 'recorded'}), \
             patch.object(sys, 'argv', ['hub', 'heartbeat', '--status', 'idle', '--summary', 'waiting for review']):
            cli.main()
        self.assertEqual(calls, [('POST', '/api/heartbeat', {'status': 'idle', 'summary': 'waiting for review'})])

    def test_heartbeat_failure_does_not_echo_server_output(self):
        from unittest.mock import patch
        import io
        from agent_hub_host import cli, bus
        err = io.StringIO()
        with patch.object(bus, '_do_request', side_effect=RuntimeError('MUST_NOT_LEAK')), \
             patch.object(sys, 'argv', ['hub', 'heartbeat']), patch.object(sys, 'stderr', err):
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertNotIn('MUST_NOT_LEAK', err.getvalue())
