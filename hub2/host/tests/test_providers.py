import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_hub_host.client import ApiError
from agent_hub_host.launcher import Launcher
from agent_hub_host.providers import provision


class ProvisionTests(unittest.TestCase):
    def test_secret_stdin_materialization_precedes_generation_ack_and_controller_stays_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            class Client:
                token = 'synthetic-parent-token'
                base_url = 'http://127.0.0.1:1'
                def request(self, method, path, body=None):
                    calls.append((method, path, body))
                    if method == 'GET' and '/credentials?' in path:
                        return {'generation': 7, 'files': {'.codex/auth.json': 'synthetic-private-bundle'}}
                    if method == 'GET':
                        return {'account': {'provider': 'codex'}}
                    self_test.assertTrue((root / 'manifest.json').exists())
                    return {'acknowledged': True}
            def runner(argv, **kwargs):
                self.assertNotIn('synthetic-private-bundle', str(argv))
                request = json.loads(kwargs['input'])
                self.assertEqual(request['files']['.codex/auth.json'], 'synthetic-private-bundle')
                self.assertEqual(request['generation'], 7)
                (root / 'manifest.json').write_text('{}')
                return subprocess.CompletedProcess(argv, 0, json.dumps({'manifest': str(root / 'manifest.json')}), '')
            self_test = self
            launch = {'engine': 'codex', 'model': 'configured', 'timeout_seconds':60,'worker_context': {'machine_id': 'host-1'}}
            offer = {'account_id': 'codex-central', 'session_id': 'session-1', 'operation_id': 'operation-1', 'epoch': 2}
            launcher = Launcher({'repository': directory, 'routes': {'codex': {'provider': 'codex'}}})
            manifest, controller = provision(Client(), offer, launch, root, launcher, runner)
            self.assertEqual(calls[-1], ('POST', '/api/accounts/codex-central/api/account/credentials', {'generation': 7}))
            saved = json.loads(Path(controller).read_text())
            self.assertEqual(saved['token'], 'synthetic-parent-token')
            self.assertEqual(saved['binding'], {**offer, 'machine_id': 'host-1'})
            self.assertNotIn('synthetic-parent-token', json.dumps(launch))
            if os.name != 'nt':
                self.assertEqual(Path(controller).stat().st_mode & 0o777, 0o600)

    def test_sdk_mutation_isolated_between_attempts_and_pre_spawn_replay_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);acks=[];original='{"key":"synthetic","model":"configured"}'
            class Client:
                token='synthetic-parent-token'
                base_url='http://127.0.0.1:1'
                def request(self,method,path,body=None):
                    if method=='GET' and '/credentials?' in path:
                        return {'generation':7,'files':{'.codex/auth.json':original}}
                    if method=='GET':return {'account':{'provider':'codex'}}
                    acks.append(body);return {'acknowledged':True}
            def materializer(argv,**options):
                request=json.loads(options['input']);destination=Path(request['root'])
                destination.mkdir(parents=True,exist_ok=True)
                sdk=destination/'sdk-config.json';manifest=destination/'manifest.json'
                if sdk.exists() and sdk.read_text()!=original:
                    return subprocess.CompletedProcess(argv,1,'','SDK changed immutable initial files')
                if not sdk.exists():sdk.write_text(original)
                if not manifest.exists():manifest.write_text('{}')
                return subprocess.CompletedProcess(argv,0,json.dumps({'manifest':str(manifest)}),'')
            launcher=Launcher({'repository':directory,'routes':{'codex':{'provider':'codex'}}})
            launch={'engine':'codex','model':'configured','timeout_seconds':60,'worker_context':{'machine_id':'host-1'}}
            first={'account_id':'codex-central','session_id':'session-1','operation_id':'operation-1','epoch':1}
            manifest1,_=provision(Client(),first,dict(launch),root,launcher,materializer)
            sdk1=Path(manifest1).parent/'sdk-config.json'
            sdk1.write_text(original+'\nSDK-owned mutable metadata')
            with self.assertRaisesRegex(RuntimeError,'provider_materialization_refused'):
                provision(Client(),first,dict(launch),root,launcher,materializer)
            self.assertEqual(len(acks),1)
            second={**first,'session_id':'session-2','operation_id':'operation-2'}
            manifest2,_=provision(Client(),second,dict(launch),root,launcher,materializer)
            replay,_=provision(Client(),second,dict(launch),root,launcher,materializer)
            self.assertNotEqual(Path(manifest1).parent,Path(manifest2).parent)
            self.assertEqual(manifest2,replay)
            self.assertEqual((Path(manifest2).parent/'sdk-config.json').read_text(),original)
            self.assertEqual(sdk1.read_text(),original+'\nSDK-owned mutable metadata')
            higher={**second,'epoch':2}
            manifest3,_=provision(Client(),higher,dict(launch),root,launcher,materializer)
            self.assertNotEqual(Path(manifest2).parent,Path(manifest3).parent)

    def test_wrong_provider_refuses_before_bundles_or_ack(self):
        calls = []
        class Client:
            def request(self, *args):
                calls.append(args)
                return {'account': {'provider': 'zai'}}
        with tempfile.TemporaryDirectory() as directory:
            launcher = Launcher({'repository': directory, 'routes': {'codex': {'provider': 'codex'}}})
            with self.assertRaisesRegex(RuntimeError, 'provider_route_mismatch'):
                provision(Client(), {'account_id': 'codex-central'}, {'engine': 'codex'}, directory, launcher)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
