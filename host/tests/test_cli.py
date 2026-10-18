import json
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
            "release", "claims", "update"
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

    def test_isolated_subprocess_version(self):
        """Verify --version returns commit and build date."""
        env = os.environ.copy()
        env["PYTHONPATH"] = HOST_DIR
        res = subprocess.run(
            [sys.executable, "-m", "agent_hub_host.cli", "--version"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("0.1.0", res.stdout)
        self.assertIn("(", res.stdout)
        self.assertIn(")", res.stdout)


class TestCliUpdate(unittest.TestCase):
    def test_version_formatting(self):
        from agent_hub_host import cli
        v_str = cli.format_version()
        self.assertTrue(v_str.startswith("0.1.0 ("))
        self.assertTrue(v_str.endswith(")"))

    def test_update_check_up_to_date(self):
        import io
        from unittest.mock import patch, MagicMock
        from agent_hub_host import cli

        info = cli.get_build_info()
        manifest_data = {
            "version": "0.1.0",
            "built_from_commit": info["commit"],
            "sha256": "abc123def456",
        }
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(manifest_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        out = io.StringIO()
        args = MagicMock(url="https://hub.test", check=True, force=False)
        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("sys.stdout", out):
            cli.cmd_update(args)
        self.assertIn("hub is up to date", out.getvalue())

    def test_update_check_warns_when_stale(self):
        import io
        from unittest.mock import patch, MagicMock
        from agent_hub_host import cli

        manifest_data = {
            "version": "0.1.0",
            "built_from_commit": "different_server_commit_1234567890",
            "sha256": "abc123def456",
        }
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(manifest_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        out = io.StringIO()
        args = MagicMock(url="https://hub.test", check=True, force=False)
        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("sys.stdout", out):
            cli.cmd_update(args)
        self.assertIn("Warning: hub CLI is stale", out.getvalue())
        self.assertIn("different_server_commit_1234567890"[:12], out.getvalue())

    def test_update_downloads_and_verifies_checksum(self):
        import io
        import hashlib
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, MagicMock
        from agent_hub_host import cli

        dummy_binary = b"#!/usr/bin/env python3\nprint('hello')\n"
        digest = hashlib.sha256(dummy_binary).hexdigest()
        manifest_data = {
            "version": "0.1.0",
            "built_from_commit": "new_commit_abcdef123456",
            "sha256": digest,
        }

        def fake_urlopen(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            resp.status = 200
            if "manifest.json" in url:
                resp.read.return_value = json.dumps(manifest_data).encode("utf-8")
            else:
                resp.read.return_value = dummy_binary
            resp.__enter__.return_value = resp
            return resp

        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            args = MagicMock(url="https://hub.test", check=False, force=False, install_dir=td)
            with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("sys.stdout", out):
                cli.cmd_update(args)
            installed = Path(td) / "hub"
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.read_bytes(), dummy_binary)
            self.assertIn("Updated hub at", out.getvalue())

    def test_update_refuses_checksum_mismatch(self):
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, MagicMock
        from agent_hub_host import cli

        dummy_binary = b"#!/usr/bin/env python3\nprint('hello')\n"
        manifest_data = {
            "version": "0.1.0",
            "built_from_commit": "new_commit_abcdef123456",
            "sha256": "expected_hash_that_does_not_match",
        }

        def fake_urlopen(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            resp.status = 200
            if "manifest.json" in url:
                resp.read.return_value = json.dumps(manifest_data).encode("utf-8")
            else:
                resp.read.return_value = dummy_binary
            resp.__enter__.return_value = resp
            return resp

        with tempfile.TemporaryDirectory() as td:
            err = io.StringIO()
            args = MagicMock(url="https://hub.test", check=False, force=False, install_dir=td)
            with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("sys.stderr", err):
                with self.assertRaises(SystemExit):
                    cli.cmd_update(args)
            installed = Path(td) / "hub"
            self.assertFalse(installed.exists())
            self.assertIn("Checksum mismatch", err.getvalue())



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
