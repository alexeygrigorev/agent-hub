"""Unit tests for Windows host provisioning and portability helpers."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

from agent_hub_host import fleet
from agent_hub_host import provisioning as p
from agent_hub_host import engine_login

PWSH_BIN = os.environ.get("PWSH_BIN") or shutil.which("pwsh")


class TestWindowsProvisioningPortability(unittest.TestCase):
    def tearDown(self):
        with engine_login._guard:
            engine_login._active.clear()

    def test_resolve_npm_command_posix(self):
        self.assertEqual(p.resolve_npm_command(is_windows=False), ['npm'])

    def test_resolve_npm_command_windows_with_node_and_npm_cli_js(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            # Create a path with spaces simulating "C:\Program Files\nodejs\node.exe"
            node_dir = tmp_p / "Program Files" / "nodejs"
            node_dir.mkdir(parents=True)
            node_exe = node_dir / "node.exe"
            node_exe.write_text("")

            npm_cli = node_dir / "node_modules" / "npm" / "bin" / "npm-cli.js"
            npm_cli.parent.mkdir(parents=True)
            npm_cli.write_text("// npm-cli")

            with patch('shutil.which', side_effect=lambda name: str(node_exe) if name == 'node' else None):
                cmd = p.resolve_npm_command(is_windows=True)
                self.assertEqual(cmd, [str(node_exe), str(npm_cli.resolve())])

    def test_resolve_npm_command_windows_execution_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            space_dir = tmp_p / "My Node Runtime" / "node modules" / "npm" / "bin"
            space_dir.mkdir(parents=True)
            entrypoint = space_dir / "npm-cli.js"
            # Fake JS entrypoint that outputs its arguments as JSON
            entrypoint.write_text("console.log(JSON.stringify(process.argv.slice(2)));")
            node_bin = shutil.which("node") or sys.executable

            with patch.dict(os.environ, {'HUB_NPM_CLI_JS': str(entrypoint)}), \
                 patch('shutil.which', return_value=node_bin):
                cmd = p.resolve_npm_command(is_windows=True)
                self.assertEqual(cmd, [node_bin, str(entrypoint.resolve())])
                # If node exists, test real execution with spaces in argument
                if node_bin and "node" in node_bin:
                    proc = subprocess.run(cmd + ["install", "pkg@1.0.0", "path with spaces"],
                                          capture_output=True, text=True, check=True)
                    received = json.loads(proc.stdout.strip())
                    self.assertEqual(received, ["install", "pkg@1.0.0", "path with spaces"])

    def test_resolve_npm_command_windows_fail_closed_no_batch_fallback(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch('shutil.which', return_value=None):
            with self.assertRaises(ValueError) as ctx:
                p.resolve_npm_command(is_windows=True)
            self.assertIn("refusing unsafe batch fallback", str(ctx.exception))

    def test_engine_environment_windows_profile_isolation(self):
        bundle = {
            'label': 'test-acc',
            'provider': 'codex',
            'version': '0.1.0',
            'files': {},
            'env': {},
            'args': [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            home.mkdir()
            env = p.engine_environment(bundle, home)
            self.assertEqual(env['HOME'], str(home))
            self.assertEqual(env['USERPROFILE'], str(home))
            self.assertEqual(env['APPDATA'], str(home / 'AppData' / 'Roaming'))
            self.assertEqual(env['LOCALAPPDATA'], str(home / 'AppData' / 'Local'))

    def test_absolute_workspace_ok_windows(self):
        # Valid Windows absolute paths
        self.assertTrue(fleet.absolute_workspace_ok(r'C:\workspaces\repo'))
        self.assertTrue(fleet.absolute_workspace_ok(r'C:\Users\Owner\My Project\sub dir'))
        self.assertTrue(fleet.absolute_workspace_ok('D:/projects/repo/nested'))
        self.assertTrue(fleet.absolute_workspace_ok(r'E:\workspaces\a.b-c_d'))

        # Traversal and bad segments rejected
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\..\escape'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\.\current'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:relative\path'))
        self.assertFalse(fleet.absolute_workspace_ok('C:\\'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\CON\file'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\AUX'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\NUL\sub'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\COM1'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\trailing '))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\trailing.'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\bad:char'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\bad*char'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\bad?char'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\bad<char>'))
        self.assertFalse(fleet.absolute_workspace_ok(r'C:\workspaces\bad|char'))

        # POSIX still works
        self.assertTrue(fleet.absolute_workspace_ok('/srv/repo'))
        self.assertTrue(fleet.absolute_workspace_ok('/home/alexey/project'))
        self.assertFalse(fleet.absolute_workspace_ok('/srv/../escape'))
        self.assertFalse(fleet.absolute_workspace_ok('/srv/has\\backslash'))

    def test_cleanup_roots_windows_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p_json = Path(tmp) / 'subscriptions.json'
            target = Path(tmp) / 'target'
            target.mkdir()
            cfg = {
                'version': 1,
                'subscriptions': [],
                'workspaces': {'demo': str(target)},
                'cleanup_roots': [r'C:\worktrees\test', 'D:/worktrees/other'],
            }
            p_json.write_text(json.dumps(cfg))
            loaded = fleet.load_settings(p_json)
            self.assertEqual(loaded['cleanup_roots'], [r'C:\worktrees\test', 'D:/worktrees/other'])

    def test_locate_native_zcodex_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_file = Path(tmp) / "zcodex.cmd"
            cmd_file.write_text("@echo off")
            with patch('shutil.which', side_effect=lambda name: str(cmd_file) if name in ('zcodex', 'zcodex.cmd') else None):
                located = p.locate_native_zcodex(is_windows=True)
                self.assertEqual(located, cmd_file)

    def test_executable_path_zai_windows_wrapper(self):
        bundle = {'label': 'my-zai', 'provider': 'zai', 'version': '1.0.0', 'files': {}, 'env': {}, 'args': []}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            wrapper_cmd = dest / 'engine' / 'bin' / 'zcodex.cmd'
            wrapper_cmd.parent.mkdir(parents=True)
            wrapper_cmd.write_text("@echo off\r\n")
            exec_path = p.executable_path(bundle, dest, is_windows=True)
            self.assertEqual(exec_path, wrapper_cmd)

    def test_engine_argv_windows_node_js(self):
        bundle = {'label': 'my-codex', 'provider': 'codex', 'version': '0.1.0', 'files': {}, 'env': {}, 'args': []}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            pkg_dir = dest / 'engine' / 'node_modules' / '@openai' / 'codex'
            pkg_dir.mkdir(parents=True)
            (pkg_dir / 'package.json').write_text(json.dumps({'bin': {'codex': 'bin/codex.js'}}))
            bin_file = pkg_dir / 'bin' / 'codex.js'
            bin_file.parent.mkdir(parents=True)
            bin_file.write_text("// codex.js")

            with patch('shutil.which', return_value=r'C:\Program Files\nodejs\node.exe'):
                argv = p.engine_argv(bundle, dest, is_windows=True)
                self.assertEqual(argv, [r'C:\Program Files\nodejs\node.exe', str(bin_file)])

    def test_engine_login_tick_windows_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = {'id': 'login-1', 'label': 'gemini-acc', 'status': 'requested'}
            bundle = {'label': 'gemini-acc', 'provider': 'gemini', 'revision': 'rev-1'}
            requested_paths = []
            def fake_request(method, path, body=None):
                requested_paths.append((method, path))
                return {'status': 'waiting'}

            with patch.dict(os.environ, {'AGENT_HUB_MANAGED_DIR': tmp}), \
                 patch('threading.Thread') as mock_thread:
                engine_login.tick([item], [bundle], fake_request, is_windows=True)
                self.assertTrue(mock_thread.called)

    def test_engine_login_windows_safe_pipe_reader_and_url_extraction(self):
        # Test the memory-only pipe reader behavior and cleanup
        item = {'id': 'login-test-1'}
        bundle = {'label': 'test-gemini', 'provider': 'gemini'}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            home = dest / 'home'
            home.mkdir(parents=True)

            # Create mock proc with stdout pipe
            class MockPipe:
                def __init__(self, data):
                    self.data = list(data)
                    self.closed = False
                def read1(self, n=1024):
                    if self.data:
                        return self.data.pop(0)
                    return b''
                def close(self):
                    self.closed = True

            test_url = "https://accounts.google.com/o/oauth2/auth?client_id=test1234&response_type=code"
            pipe = MockPipe([b"Please authorize at: ", test_url.encode(), b"\r\nEnter code:\r\n"])
            fake_proc = MagicMock()
            fake_proc.stdout = pipe
            stop = threading.Event()
            fake_proc.poll.side_effect = lambda: 0 if stop.is_set() else None
            fake_proc.wait.return_value = 0

            posted = []
            def fake_request(method, path, data=None):
                posted.append((method, path, data))
                if data and data.get('status') == 'waiting':
                    # Stop once waiting URL is captured
                    stop.set()
                return {}

            with patch('subprocess.Popen', return_value=fake_proc), \
                 patch.object(p, 'engine_argv', return_value=['fake-gemini']), \
                 patch.object(p, 'read_json', return_value={}), \
                 patch.object(p, 'write_json'):
                engine_login._run(item, bundle, dest, fake_request, stop, is_windows=True)

            waiting_events = [data for m, p_path, data in posted if data and data.get('status') == 'waiting']
            self.assertTrue(len(waiting_events) >= 1)
            self.assertEqual(waiting_events[0]['url'], test_url)
            self.assertTrue(pipe.closed)

    def test_engine_login_windows_conpty_process_reader_and_url_extraction(self):
        from agent_hub_host import win_console
        item = {'id': 'login-test-conpty'}
        bundle = {'label': 'test-gemini', 'provider': 'gemini'}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            home = dest / 'home'
            home.mkdir(parents=True)

            class FakeBackend:
                def __init__(self, data):
                    self.data = list(data)
                    self.pid = 9999
                    self.terminated = False
                    self.closed_console = False
                    self.closed_handles = False
                def resume(self):
                    pass
                def read(self, n=4096):
                    if self.data:
                        return self.data.pop(0)
                    return b''
                def write(self, data):
                    return len(data)
                def close_input(self):
                    pass
                def poll(self):
                    return 0 if stop.is_set() else None
                def wait(self, timeout=None):
                    return 0
                def terminate(self):
                    self.terminated = True
                def close_console(self):
                    self.closed_console = True
                def close_handles(self):
                    self.closed_handles = True

            test_url = "https://accounts.google.com/o/oauth2/auth?client_id=test_conpty&response_type=code"
            backend = FakeBackend([b"Please authorize at: ", test_url.encode(), b"\r\nEnter code:\r\n"])
            proc = win_console.ConsoleProcess(['fake.exe'], env={'A': 'B'}, cwd=str(home), _backend=backend)

            stop = threading.Event()
            posted = []
            def fake_request(method, path, data=None):
                posted.append((method, path, data))
                if data and data.get('status') == 'waiting':
                    stop.set()
                return {}

            with patch('agent_hub_host.win_console.ConsoleProcess', return_value=proc), \
                 patch.object(p, 'engine_argv', return_value=['fake-gemini.exe']), \
                 patch.object(p, 'read_json', return_value={}), \
                 patch.object(p, 'write_json'):
                engine_login._run(item, bundle, dest, fake_request, stop, is_windows=True)

            waiting_events = [data for m, p_path, data in posted if data and data.get('status') == 'waiting']
            self.assertTrue(len(waiting_events) >= 1)
            self.assertEqual(waiting_events[0]['url'], test_url)
            self.assertTrue(backend.closed_console)

    @unittest.skipUnless(PWSH_BIN and os.path.isfile(PWSH_BIN) and os.access(PWSH_BIN, os.X_OK), "Portable pwsh not available")
    def test_pwsh_install_script_execution_and_checksum_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            srv_dir = tmp_p / "server" / "cli"
            srv_dir.mkdir(parents=True)

            # Create mock py launcher that logs its invocations
            bin_dir = tmp_p / "fake_bin"
            bin_dir.mkdir()
            py_log = tmp_p / "py_invocations.log"
            fake_py = bin_dir / "py"
            fake_py.write_text(f"""#!/usr/bin/env bash
echo "$@" >> "{py_log}"
if [ "$1" = "-3" ]; then
    shift
fi
exec "{sys.executable}" "$@"
""")
            fake_py.chmod(0o755)

            # Create dummy pyz and calculate SHA256
            dummy_pyz = b"PK\\x03\\x04dummy-hub-zipapp-content"
            sha = hashlib.sha256(dummy_pyz).hexdigest()

            (srv_dir / "hub.pyz").write_bytes(dummy_pyz)
            (srv_dir / "hub.pyz.sha256").write_text(f"{sha}  hub.pyz\n")
            (srv_dir / "manifest.json").write_text(json.dumps({"version": "0.1.0", "sha256": sha}))

            install_ps1 = Path(__file__).resolve().parents[2] / "public" / "cli" / "install.ps1"
            self.assertTrue(install_ps1.is_file())

            # Test 1: Successful install in a path with spaces
            install_dir = tmp_p / "User Local Programs" / "hub space test"
            pwsh_script = f"""
$env:PATH = "{bin_dir}:" + $env:PATH
$env:HUB_BASE_URL = "http://127.0.0.1:9"
$env:HUB_INSTALL_DIR = "{install_dir}"

# Mock Invoke-WebRequest to read from our local fixture directory
function Invoke-WebRequest {{
    param([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing)
    $fileName = Split-Path $Uri -Leaf
    $src = Join-Path "{srv_dir}" $fileName
    Copy-Item $src $OutFile -Force
}}

& "{install_ps1}"
"""
            res = subprocess.run([PWSH_BIN, "-NoProfile", "-Command", pwsh_script],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"pwsh stdout: {res.stdout}\npwsh stderr: {res.stderr}")
            self.assertTrue((install_dir / "hub.pyz").is_file())
            self.assertTrue((install_dir / "hub.cmd").is_file())
            self.assertTrue((install_dir / "hub.ps1").is_file())

            cmd_content = (install_dir / "hub.cmd").read_text()
            self.assertIn("goto use_py", cmd_content)
            self.assertNotIn("%errorlevel% (", cmd_content)

            # Check that py was called with -3 and not literally "py -3"
            log_content = py_log.read_text()
            self.assertIn("-3 -c", log_content)

            # Test 2: Checksum mismatch must fail and NOT overwrite existing installation
            corrupted_pyz = b"corrupted content"
            (srv_dir / "hub.pyz").write_bytes(corrupted_pyz)
            original_installed = (install_dir / "hub.pyz").read_bytes()

            res2 = subprocess.run([PWSH_BIN, "-NoProfile", "-Command", pwsh_script],
                                  capture_output=True, text=True)
            self.assertNotEqual(res2.returncode, 0)
            self.assertIn("sha256 mismatch", res2.stderr + res2.stdout)
            # Ensure existing valid install was not destroyed
            self.assertEqual((install_dir / "hub.pyz").read_bytes(), original_installed)


if __name__ == '__main__':
    unittest.main()
