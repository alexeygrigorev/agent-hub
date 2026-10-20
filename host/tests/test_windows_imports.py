"""Import/CLI/native-path contracts without Unix lock modules (source evidence)."""
import contextlib
import io
import json
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ImportContracts(unittest.TestCase):
    def test_actual_conversation_and_bus_modules_import_without_fcntl(self):
        program = '''import sys
sys.modules['fcntl'] = None
from agent_hub_host import cli, auth, conversations, native_bridge, bridge, conversation_bridge
assert 'agent_hub_host.guarded_root' not in sys.modules
for args in [('conversations','bridge','--help'), ('bridge-resume','--help'), ('login','--help')]:
 sys.argv=['hub',*args]
 try: cli.main()
 except SystemExit as exc: assert exc.code == 0
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        result = subprocess.run([sys.executable, '-c', program], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_session_host_and_jobs_import_without_fcntl(self):
        program = '''import sys
sys.modules['fcntl'] = None
from agent_hub_host import session_host, jobs, guarded_root, provisioning, engine_login
bridge = guarded_root.GuardedRootBridge('/tmp/dummy.lock')
try:
    bridge.acquire(non_blocking=True)
    assert False, "Should have raised fail-closed"
except RuntimeError as exc:
    assert "not supported on Windows" in str(exc)
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        result = subprocess.run([sys.executable, '-c', program], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_windows_roots_match_readonly_rust_contract(self):
        from agent_hub_host import bridge
        environment = {'LOCALAPPDATA': r'C:\Users\Owner\AppData\Local',
                       'XDG_RUNTIME_DIR': '/wrong-unix-runtime', 'XDG_STATE_HOME': '/wrong-unix-state'}
        fake_os = types.SimpleNamespace(name='nt', path=ntpath, environ=environment)
        with patch.object(bridge, 'os', fake_os):
            self.assertEqual(bridge.get_authoritative_state_dir(), r'C:\Users\Owner\AppData\Local\aplexer\state')
            self.assertEqual(bridge.get_authoritative_runtime_dir(), r'C:\Users\Owner\AppData\Local\aplexer\run')
            for relative in ('relative', r'C:relative', r'\root-relative'):
                with self.assertRaises(bridge.BridgeCapabilityGap):
                    bridge.get_authoritative_state_dir(relative)
            environment['LOCALAPPDATA'] = 'relative'
            with self.assertRaises(bridge.BridgeCapabilityGap):
                bridge.get_authoritative_runtime_dir()

    def test_public_login_keeps_automatic_machine_binding_no_manual_metadata(self):
        # Client protocol simulation, not real enrollment/Worker proof.
        from agent_hub_host import auth, api, config
        token = {'access_token': 'fixture-only', 'machine_id': 'fixture-machine'}
        def flow(origin, post, **kwargs):
            status, response = post(origin + kwargs['token_path'], {'device_code': 'fixture'})
            self.assertEqual(status, 200)
            return response
        module = types.SimpleNamespace(device_login=flow, DeviceLoginError=RuntimeError)
        response = MagicMock()
        response.headers = {'Content-Type': 'application/json'}
        response.read.return_value = json.dumps(token).encode()
        response.__enter__.return_value = response
        with patch.object(auth, '_device_flow', return_value=module), \
             patch.object(auth, '_shared_device_login', return_value=module), \
             patch('urllib.request.OpenerDirector.open', return_value=response), \
             patch.object(config, 'load_config', return_value={}), patch.object(config, 'save_config') as save, \
             contextlib.redirect_stdout(io.StringIO()):
            auth.device_login('https://fixture.invalid', 'agent', 'fixture:worker', 'fixture-host', ['conversations:read'])
        self.assertEqual(save.call_args.args[0]['machine_id'], 'fixture-machine')
        self.assertEqual(save.call_args.args[0]['access_token'], 'fixture-only')
        if hasattr(config, '_pending_machine_binding'):
            config._pending_machine_binding.clear()


if __name__ == '__main__':
    unittest.main()
