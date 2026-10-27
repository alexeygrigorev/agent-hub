import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import http.server
import urllib.error
import tempfile
import unittest
from unittest.mock import Mock, patch


from agent_hub_host import worker_auth as auth, config
OFFER = '11111111-2222-4333-8444-555555555555'


class WorkerAuthTests(unittest.TestCase):
    def test_managed_home_scrub_does_not_remove_repo_scoped_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            child = root / 'src'
            child.mkdir()
            with patch.object(Path, 'cwd', return_value=child), patch.dict(os.environ, {
                'HOME': str(root / 'managed-home'), 'XDG_CONFIG_HOME': str(root / 'managed-xdg'),
                'APLEXER_TAG': 'task-hub-' + OFFER, 'APLEXER_WORKSPACE': '',
            }):
                self.assertEqual(auth.config_path(), root / '.local/hub-worker-auth' / (OFFER + '.json'))

    def test_issuance_is_private_and_host_retry_reuses_exact_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = Mock(return_value=dict(worker_launch_id=OFFER, client_kind='agent',
                access_token='hubt_fake-test-only', refresh_token='hubr_fake-test-only',
                base_url='https://hub.test', worker_expires_at=9999999999999))
            path = auth.provision(tmp, OFFER, request)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            auth.provision(tmp, OFFER, request)
            request.assert_called_once()

    def test_active_cli_profile_and_refresh_save_survive_managed_home_without_host_token_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            request = Mock(return_value=dict(worker_launch_id=OFFER, client_kind='agent',
                access_token='hubt_fake-test-only', refresh_token='hubr_fake-test-only',
                base_url='https://hub.test', worker_expires_at=9999999999999))
            path = auth.provision(tmp, OFFER, request)
            with patch.object(Path, 'cwd', return_value=root), patch.dict(os.environ, {
                'HOME': str(root / 'managed'), 'XDG_CONFIG_HOME': str(root / 'managed/.config'),
                'APLEXER_TAG': 'hub-' + OFFER, 'APLEXER_WORKSPACE': '',
            }), patch.dict(sys.modules, {'agent_hub_host.worker_auth': auth}):
                # Explicit profile override is a separate supported operator path.
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop('HUB_CONFIG_FILE', None)
                    loaded = config.load_config()
                    self.assertEqual(loaded['client_kind'], 'agent')
                    loaded['access_token'] = 'hubt_refreshed-test-only'
                    config.save_config(loaded)
                    self.assertEqual(json.loads(path.read_text())['access_token'], 'hubt_refreshed-test-only')
                    self.assertFalse((root / 'managed/.config/agent-hub/host.json').exists())
                    self.assertFalse((root / 'managed/.config/agent-hub/cli.json').exists())

    def test_linked_private_profile_is_refused_without_overwriting_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / 'outside'
            outside.write_text('preserve')
            path = root / '.local/hub-worker-auth' / (OFFER + '.json')
            path.parent.mkdir(parents=True, mode=0o700)
            path.symlink_to(outside)
            request = Mock(return_value=dict(worker_launch_id=OFFER, client_kind='agent',
                access_token='hubt_test', refresh_token='hubr_test', base_url='https://hub.test', worker_expires_at=9999999999999))
            with self.assertRaises(OSError):
                auth.provision(tmp, OFFER, request)
            self.assertEqual(outside.read_text(), 'preserve')

    def test_published_workspace_finds_profile_after_worker_changes_directory(self):
        with patch.dict(os.environ, {'APLEXER_TAG':'task-hub-'+OFFER,
                                     'APLEXER_WORKSPACE':'/repo'}):
            self.assertEqual(auth.config_path(), Path('/repo/.local/hub-worker-auth') / (OFFER+'.json'))
        with patch.dict(os.environ, {'APLEXER_TAG':'hub-'+OFFER, 'APLEXER_WORKSPACE':'/repo'}):
            self.assertEqual(auth.config_path(), Path('/repo/.local/hub-worker-auth') / (OFFER+'.json'))
        for tag in ['hub-not-a-launch', 'task-hub-'+OFFER+'-other', 'unrelated-'+OFFER]:
            with patch.dict(os.environ, {'APLEXER_TAG':tag}):
                self.assertIsNone(auth.config_path())

    def test_missing_worker_profile_does_not_fall_back_to_host_or_human(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'APLEXER_TAG':'task-hub-'+OFFER, 'APLEXER_WORKSPACE':tmp,
        }), patch.object(config, 'load_human_config') as human, patch.object(config, 'load_host_config') as host:
            os.environ.pop('HUB_CONFIG_FILE', None)
            self.assertEqual(config.load_config(), {})
            human.assert_not_called(); host.assert_not_called()

    def test_two_cli_processes_rotate_one_shared_refresh_token_once(self):
        from agent_hub_host import private_fs
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                payload = json.dumps({'access_token':'hubt_rotated-test-only',
                                      'refresh_token':'hubr_rotated-test-only'}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json')
                self.end_headers(); self.wfile.write(payload)
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        children = []
        try:
            thread.start()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                profile = dict(worker_launch_id=OFFER, client_kind='agent',
                               access_token='hubt_initial-test-only',refresh_token='hubr_initial-test-only',
                               base_url='http://127.0.0.1:'+str(server.server_port))
                path=root/'.local/hub-worker-auth'/(OFFER+'.json')
                private_fs.atomic_write(path,json.dumps(profile).encode(),32768)
                env=dict(os.environ, APLEXER_TAG='task-hub-'+OFFER, APLEXER_WORKSPACE=tmp,
                         PYTHONPATH=str(Path(__file__).resolve().parents[1]), HOME=str(root/'managed'))
                env.pop('HUB_CONFIG_FILE',None)
                code = "from agent_hub_host.auth import refresh_token; assert refresh_token(expected_token='hubt_initial-test-only') == 'hubt_rotated-test-only'"
                for _ in range(2):
                    children.append(subprocess.Popen([sys.executable,'-c',code],env=env,
                                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
                for child in children:
                    output,error=child.communicate(timeout=15)
                    self.assertEqual(child.returncode,0,error)
                self.assertEqual(len(requests),1)
                self.assertEqual(requests[0]['refresh_token'],'hubr_initial-test-only')
                self.assertEqual(json.loads(path.read_text())['refresh_token'],'hubr_rotated-test-only')
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill(); child.communicate(timeout=5)
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_revoked_worker_never_starts_human_or_host_device_pairing(self):
        from agent_hub_host import auth as cli_auth
        cfg=dict(worker_launch_id=OFFER,client_kind='agent',access_token='hubt_initial-test-only',
                 refresh_token='hubr_initial-test-only',base_url='https://hub.test')
        with patch.object(cli_auth,'_active_profile',return_value=('agent',cfg,lambda:cfg,Mock())), \
             patch.object(auth,'config_path',return_value=None), \
             patch.object(cli_auth.api,'safe_http_json',side_effect=urllib.error.HTTPError('https://hub.test',401,'revoked',{},None)), \
             patch.object(cli_auth,'device_login') as pair:
            with self.assertRaisesRegex(RuntimeError,'Worker credential revoked'):
                cli_auth.refresh_token(expected_token=cfg['access_token'])
            pair.assert_not_called()


if __name__ == '__main__':
    unittest.main()
