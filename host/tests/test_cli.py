import io
import json
import os
import subprocess
import sys
import unittest

HOST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

class TestCliIsolated(unittest.TestCase):
    def test_hub2_resource_settings_require_a_valid_floor(self):
        from agent_hub_host import cli
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        argv = ["hub", "resources", "settings", "--json"]
        valid = {"resource_settings": {"disk_floor_gb": 27.5}}
        with patch.object(sys, "argv", argv), patch("agent_hub_host.bus._do_request",
                return_value=valid) as transport, redirect_stdout(io.StringIO()) as output:
            cli.main()
        transport.assert_called_once_with("GET", "/api/resource-settings")
        self.assertEqual(json.loads(output.getvalue()), valid)
        for value in (None, True, "15", 0, float("nan"), float("inf")):
            with self.subTest(value=value), patch.object(sys, "argv", argv), patch(
                    "agent_hub_host.bus._do_request",
                    return_value={"resource_settings": {"disk_floor_gb": value}}), redirect_stdout(
                    io.StringIO()) as output, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as failed:
                    cli.main()
                self.assertEqual(failed.exception.code, 1)
                self.assertEqual(output.getvalue(), "")

    def test_interactive_prompt_is_sent_after_binding(self):
        from agent_hub_host import cli, session_control
        from unittest.mock import patch, mock_open
        from contextlib import redirect_stdout
        import io
        calls = []
        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {'id': 'owned-session'} if path == '/api/sessions' else {'command_id': 'prompt-command'}
        with patch.object(sys, 'argv', ['hub', 'start-session', '--machine', 'machine', '--workspace', 'home', '--tier', 'deep', '--prompt-file', 'goal', '--key', 'same-key']), \
                patch('pathlib.Path.open', mock_open(read_data='throwaway goal')), \
                patch.object(session_control, 'request', side_effect=request), \
                patch.object(session_control, 'wait_for_session', return_value={'state': 'running'}), \
                redirect_stdout(io.StringIO()):
            cli.main()
        self.assertEqual(calls[0][2]['mode'], 'interactive')
        self.assertNotIn('prompt', calls[0][2])
        self.assertEqual(calls[1], ('POST', '/api/sessions/owned-session/send', {'key': 'same-key', 'body': 'throwaway goal'}))

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
        from agent_hub_host import __version__
        self.assertIn(__version__, res.stdout)
        self.assertIn("(", res.stdout)
        self.assertIn(")", res.stdout)


class TestCliUpdate(unittest.TestCase):
    def test_version_formatting(self):
        from agent_hub_host import cli, session_control
        v_str = cli.format_version()
        from agent_hub_host import __version__
        self.assertTrue(v_str.startswith(__version__ + " ("))
        self.assertTrue(v_str.endswith(")"))

    def test_auth_status_explains_unsafe_profile_permissions(self):
        import argparse
        import io
        from unittest.mock import patch
        from agent_hub_host import cli, config
        error = io.StringIO()
        with patch.object(cli.human_auth, 'human_status', side_effect=config.ConfigPermissionError('Profile must use mode 700')), \
             patch('sys.stderr', error):
            with self.assertRaises(SystemExit):
                cli.cmd_auth_status(argparse.Namespace(json=False))
        self.assertIn('mode 700', error.getvalue())
        self.assertNotIn('Not logged in', error.getvalue())

    def test_auth_status_prints_distinct_nonzero_next_steps(self):
        import argparse
        import io
        from unittest.mock import patch
        from agent_hub_host import cli

        cases = (
            ({'logged_in': False, 'state': 'never_logged_in'},
             'Status: Never logged in as human. Run `hub auth login` to sign in.\n'),
            ({'logged_in': False, 'state': 'revoked'},
             'Status: Revoked by server (refresh replay or admin). Run `hub auth login` to create a new human session.\n'),
        )
        for status, expected in cases:
            output = io.StringIO()
            with patch.object(cli.human_auth, 'human_status', return_value=status), \
                 patch('sys.stdout', output):
                with self.assertRaises(SystemExit) as raised:
                    cli.cmd_auth_status(argparse.Namespace(json=False))
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(output.getvalue(), expected)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX symlinks')
    def test_auth_status_surfaces_config_permission_error_on_symlink_paths(self):
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from agent_hub_host import cli, config
        tmp_dir = Path(__file__).resolve().parents[2] / '.local/tmp'
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_dir) as folder:
            root = Path(folder)
            target = root / 'target.json'
            target.write_text('{"client_kind":"human","access_token":"synthetic-fixture"}')
            target.chmod(0o600)

            # File symlink
            file_link = root / 'cli.json'
            file_link.symlink_to(target)

            # Dangling file symlink
            dangling_file = root / 'dangling.json'
            dangling_file.symlink_to(root / 'nonexistent.json')

            # Parent symlink
            parent_link = root / 'linkdir'
            parent_link.symlink_to(root)

            # Dangling parent symlink
            dangling_parent = root / 'dangling_dir'
            dangling_parent.symlink_to(root / 'nonexistent_dir')

            for path in (file_link, dangling_file, parent_link / 'cli.json', dangling_parent / 'cli.json'):
                with self.subTest(path=str(path)):
                    out, err = io.StringIO(), io.StringIO()
                    with patch.object(config, 'get_human_config_path', return_value=path), \
                         patch('sys.argv', ['hub', 'auth', 'status']), \
                         patch('sys.stdout', out), patch('sys.stderr', err):
                        with self.assertRaises(SystemExit) as cm:
                            cli.main()
                        self.assertEqual(cm.exception.code, 1)
                        self.assertIn('Unsafe Hub profile', err.getvalue())
                        self.assertNotIn('Not logged in', out.getvalue())

    def test_update_defaults_to_custom_domain_despite_legacy_profile(self):
        import argparse
        import io
        from unittest.mock import patch, MagicMock
        from agent_hub_host import cli, session_control
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"built_from_commit":"fixture"}'
        response.__enter__.return_value = response
        for explicit, expected in ((None, 'https://hub.dtcdev.click'), ('https://hub.test', 'https://hub.test')):
            with patch.dict(os.environ, {'HUB_URL': '', 'HUB_BASE_URL': ''}), \
                 patch.object(cli.config, 'load_config', return_value={'base_url': 'https://old.workers.dev'}) as profile, \
                 patch('urllib.request.urlopen', return_value=response) as http, patch('sys.stdout', io.StringIO()):
                cli.cmd_update(argparse.Namespace(url=explicit, check=True, force=False))
                self.assertEqual(http.call_args.args[0].full_url, expected + '/cli/manifest.json')
                profile.assert_not_called()

    def test_update_check_up_to_date(self):
        import io
        from unittest.mock import patch, MagicMock
        from agent_hub_host import cli, session_control

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
        from agent_hub_host import cli, session_control

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
        from agent_hub_host import cli, session_control

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
        from agent_hub_host import cli, session_control

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

    def test_sessions_start_missing_brief_file_surfaces_input_error(self):
        from agent_hub_host import cli
        from unittest.mock import patch
        import io
        err = io.StringIO()
        with patch.object(sys, 'argv', ['hub', 'sessions', 'start', '--workspace', '/test', '--brief-file', '/nonexistent/file.txt', '--key', 'k']), \
             patch.object(sys, 'stderr', err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 1)
        output = err.getvalue()
        self.assertIn('Brief file: file not found', output)
        self.assertNotIn('Session operation failed; check authentication', output)

    def test_sessions_start_http_error_surfaces_status_and_code(self):
        from agent_hub_host import cli, session_control
        from unittest.mock import patch
        import io
        err = io.StringIO()
        http_err = session_control.SessionHTTPError(400, code='invalid_session_request',
                                                    message='Session HTTP request failed with status 400 (invalid_session_request); field: brief; detail: invalid_brief (reconcile with --key k via show/commands)')
        with patch.object(sys, 'argv', ['hub', 'sessions', 'start', '--workspace', '/test', '--prompt', 'do something', '--key', 'k']), \
             patch.object(session_control, 'request', side_effect=http_err), \
             patch.object(sys, 'stderr', err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 1)
        output = err.getvalue()
        self.assertIn('status 400', output)
        self.assertIn('invalid_session_request', output)
        self.assertIn('field: brief', output)
        self.assertNotIn('Session operation failed; check authentication', output)

    def test_sessions_start_response_without_session_id_fails_loud(self):
        from agent_hub_host import cli, session_control
        from unittest.mock import patch
        import io
        err, out = io.StringIO(), io.StringIO()
        # Hub2 once answered POST /api/sessions with the GET projection (200, no id).
        with patch.object(sys, 'argv', ['hub', 'sessions', 'start', '--repo', 'o/r', '--prompt', 'x', '--key', 'k']), \
             patch.object(session_control, 'request', return_value={'items': [], 'sessions': [], 'next': None}), \
             patch.object(sys, 'stdout', out), patch.object(sys, 'stderr', err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 1)
        self.assertIn('returned no session id', err.getvalue())
        self.assertNotIn('"sessions"', out.getvalue())

    def test_sessions_start_tbm_denial_surfaces_reason_code(self):
        from agent_hub_host import cli, session_control
        from unittest.mock import patch
        import io
        err = io.StringIO()
        http_err = session_control.SessionHTTPError(409, code='tbm_admission_denied',
                                                    message='Session HTTP request failed with status 409 (tbm_admission_denied); reason: kill_switch_active (reconcile with --key k via show/commands)')
        with patch.object(sys, 'argv', ['hub', 'sessions', 'start', '--workspace', '/test', '--prompt', 'do something', '--key', 'k']), \
             patch.object(session_control, 'request', side_effect=http_err), \
             patch.object(sys, 'stderr', err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 1)
        output = err.getvalue()
        self.assertIn('status 409', output)
        self.assertIn('tbm_admission_denied', output)
        self.assertIn('reason: kill_switch_active', output)
        self.assertNotIn('Session operation failed; check authentication', output)

    def test_sessions_start_effort_is_sent_to_hub(self):
        from agent_hub_host import cli, session_control
        from unittest.mock import patch
        with patch.object(sys, 'argv', ['hub', 'sessions', 'start', '--workspace', '/test', '--prompt', 'do something', '--effort', 'low', '--key', 'effort-key', '--no-wait']), \
             patch.object(session_control, 'request', return_value={'id': 'session-1'}) as request:
            cli.main()
        payload = request.call_args.args[2]
        self.assertEqual(payload['effort'], 'low')


class TestCliStaleCheck(unittest.TestCase):
    """5.2: one-line stale-build notice; every failure mode stays silent."""

    BUILD = {'version': '1.0', 'commit': 'a' * 40, 'build_date': '2026-11-10'}

    def _cache(self, tmp):
        import pathlib
        return pathlib.Path(tmp) / 'cli-manifest-cache.json'

    def test_stale_deployed_cli_prints_one_stderr_line_and_caches_manifest(self):
        import tempfile
        from agent_hub_host import cli
        from unittest.mock import patch, MagicMock
        import urllib.request

        # Real context-manager closure catches reads attempted after __exit__.
        response = io.BytesIO(json.dumps({'built_from_commit': 'b' * 40}).encode())
        response.status = 200
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'HUB_CLI_SKIP_STALE_CHECK': '', 'HUB_URL': 'https://hub.test'}), \
                patch.object(cli, 'get_build_info', return_value=dict(self.BUILD)), \
                patch('urllib.request.urlopen', return_value=response) as fetch, \
                patch.object(sys, 'stderr') as err:
            cli.warn_if_cli_stale(cache_path=self._cache(tmp))
            self.assertEqual(fetch.call_count, 1)
            lines = [c for c in err.write.call_args_list if 'differs from the deployed CLI' in str(c)]
            self.assertEqual(len(lines), 1)
            self.assertIn("run 'hub update'", lines[0].args[0])
            # The manifest is cached for the TTL; a second call refetches nothing.
            err.write.call_args_list.clear()
            cli.warn_if_cli_stale(cache_path=self._cache(tmp))
        self.assertEqual(fetch.call_count, 1)

    def test_matching_commit_and_skip_env_stay_silent(self):
        import tempfile
        from agent_hub_host import cli
        from unittest.mock import patch, MagicMock
        import urllib.request

        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps({'built_from_commit': 'a' * 40}).encode()

        # The documented off-switch never touches the network.
        with tempfile.TemporaryDirectory() as tmp_off, \
                patch.dict(os.environ, {'HUB_CLI_SKIP_STALE_CHECK': '1'}), \
                patch('urllib.request.urlopen') as fetch, \
                patch.object(cli, 'get_build_info') as build:
            cli.warn_if_cli_stale(cache_path=self._cache(tmp_off))
        fetch.assert_not_called()
        build.assert_not_called()

    def test_offline_fetch_and_missing_commit_fail_silent(self):
        import tempfile
        from agent_hub_host import cli
        from unittest.mock import patch
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'HUB_CLI_SKIP_STALE_CHECK': '', 'HUB_URL': 'https://hub.test'}), \
                patch.object(cli, 'get_build_info', return_value=dict(self.BUILD)), \
                patch('urllib.request.urlopen', side_effect=OSError('offline')), \
                patch.object(sys, 'stderr') as err:
            cli.warn_if_cli_stale(cache_path=self._cache(tmp))  # must not raise
        err.write.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'HUB_CLI_SKIP_STALE_CHECK': ''}), \
                patch.object(cli, 'get_build_info', return_value={'version': 'dev'}), \
                patch('urllib.request.urlopen') as fetch:
            cli.warn_if_cli_stale(cache_path=self._cache(tmp))
        fetch.assert_not_called()



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


class RepoFromWorkspaceTests(unittest.TestCase):
    def test_github_origin_maps_to_owner_name(self):
        import subprocess, tempfile
        from agent_hub_host.session_control import _repo_from_workspace
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(['git', 'init', '-q', d], check=True)
            self.assertIsNone(_repo_from_workspace(d))
            subprocess.run(['git', '-C', d, 'remote', 'add', 'origin', 'git@github.com:DataTalksClub/relay.git'], check=True)
            self.assertEqual(_repo_from_workspace(d), 'DataTalksClub/relay')
