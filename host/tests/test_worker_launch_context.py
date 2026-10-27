"""Linked sessions obtain their own CLI profile before any engine starts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_hub_host import fleet, worker_auth, config, provisioning

LAUNCH = '11111111-2222-4333-8444-555555555555'
NATIVE = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
ROUTES = [('codex', 'codex'), ('zcodex', 'zai'), ('claude', 'claude'),
          ('gemini', 'gemini'), ('opencode', 'opencode'), ('antigravity', 'antigravity')]


class WorkerLaunchContextTests(unittest.TestCase):
    def test_all_engines_and_modes_provision_before_native_effect(self):
        for mode in ('interactive', 'headless'):
            for engine, provider in ROUTES:
                with self.subTest(mode=mode, engine=engine), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    profile = None if engine == 'zcodex' else 'hub-fixture'
                    sub = dict(engine=engine, service=provider, account_label='fixture', profile=profile,
                               launcher_provider=provider, launcher_model='fixture-model')
                    cfg = dict(version=1, workspaces={'repo':tmp}, subscriptions=[],
                               launcher={'argv':['launcher'], 'root':tmp, 'config_dir':tmp+'/launcher'})
                    offer = dict(id=LAUNCH, engine=engine, account='fixture', profile=profile,
                                 mode=mode, workspace='repo', prompt='' if mode=='interactive' else 'read intake',
                                 worker_credential_required=True)
                    events, argv_seen = [], []
                    secret = 'hubt_test-scope-only'
                    scoped = dict(worker_launch_id=LAUNCH, client_kind='agent', access_token=secret,
                                  refresh_token='hubr_test-scope-only', base_url='https://hub.test',
                                  worker_expires_at=9999999999999)
                    tag = ('task-hub-' if mode=='headless' else 'hub-') + LAUNCH
                    def request(method, path, body=None):
                        events.append(path.rsplit('/',1)[-1])
                        return scoped if path.endswith('/worker-credential') else ({'id':LAUNCH,'state':body['state']} if path.endswith('/result') else {})
                    def runner(argv, **kw):
                        argv_seen.append(argv)
                        if 'plan' in argv:
                            return {'eligible':[{'provider':provider,'model':'fixture-model','task_fit':1.0}]}
                        if 'submit' in argv:
                            return {}
                        if 'start' in argv or 'run' in argv:
                            self.assertIn('worker-credential', events)
                            # A fresh managed home and changed cwd still discover the private profile.
                            with patch.dict(os.environ, {'APLEXER_TAG':tag, 'APLEXER_WORKSPACE':tmp,
                                                        'HOME':tmp+'/managed', 'XDG_CONFIG_HOME':tmp+'/managed/.config'}):
                                os.environ.pop('HUB_CONFIG_FILE',None)
                                self.assertEqual(config.load_config()['access_token'],secret)
                            if mode=='interactive':
                                return dict(id=NATIVE,workspace=tmp,tag=tag,engine=engine,profile=profile,phase='running')
                            return {'receipt':dict(session_id=NATIVE,workspace=tmp,tag=tag,engine=engine,
                                                   profile=profile,account_label='fixture',state='running',first_action_verified=True)}
                        return {}
                    dispatcher = fleet.Dispatcher(cfg,request,runner)
                    with patch.object(dispatcher,'_route',return_value=sub), \
                         patch.object(dispatcher,'_dispatch_admission',return_value=fleet.utcnow()), \
                         patch.object(dispatcher,'_record_running_launch'), \
                         patch.object(dispatcher,'_check_headless_health',return_value=None):
                        result = dispatcher.dispatch(offer)
                    self.assertEqual(result['state'],'running',repr(result))
                    self.assertLess(events.index('claim'), events.index('worker-credential'))
                    self.assertNotIn(secret,json.dumps(argv_seen))
                    self.assertEqual((root/'.local/hub-worker-auth'/ (LAUNCH+'.json')).stat().st_mode & 0o777,0o600)

    def test_unprofiled_codex_task_unit_transports_selector_without_native_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub=dict(engine='codex',service='codex',account_label='fixture',profile=None,
                     launcher_provider='codex',launcher_model='fixture-model')
            cfg=dict(version=1,workspaces={'repo':tmp},subscriptions=[],
                     launcher={'argv':['launcher'],'root':tmp,'config_dir':tmp+'/launcher'})
            scoped=dict(worker_launch_id=LAUNCH,client_kind='agent',access_token='hubt_scoped-test',
                        refresh_token='hubr_scoped-test',base_url='https://hub.test',worker_expires_at=9999999999999)
            submitted={}
            def request(method,path,body=None):
                return scoped if path.endswith('/worker-credential') else {}
            def runner(argv,**kw):
                if 'submit' in argv:
                    submitted.update(json.loads(argv[argv.index('--payload')+1])); return {}
                if 'plan' in argv:
                    return {'eligible':[{'provider':'codex','model':'fixture-model','task_fit':1.0}]}
                if 'run' in argv:
                    self.assertIn('task-units',argv)
                    # The actual launch payload is the only identity transport;
                    # task units otherwise have HOME/USER/PATH, no APLEXER vars.
                    child_env=dict(HOME=tmp+'/managed',PATH=os.environ['PATH'],**submitted['extra_env'])
                    with patch.dict(os.environ,child_env,clear=True), \
                         patch.object(config,'load_human_config') as human, \
                         patch.object(config,'load_host_config') as host:
                        self.assertEqual(config.load_config()['access_token'],'hubt_scoped-test')
                        human.assert_not_called(); host.assert_not_called()
                        config.save_config({**scoped,'access_token':'hubt_rotated-test'})
                        self.assertEqual(config.load_config()['access_token'],'hubt_rotated-test')
                        Path(child_env['HUB_WORKER_CONFIG_FILE']).unlink()
                        self.assertEqual(config.load_config(),{})
                        human.assert_not_called(); host.assert_not_called()
                    return {'receipt':dict(session_id=NATIVE,state='running',first_action_verified=True)}
                return {}
            d=fleet.Dispatcher(cfg,request,runner)
            with patch.object(d,'_route',return_value=sub), \
                 patch.object(d,'_dispatch_admission',return_value=fleet.utcnow()), \
                 patch.object(d,'_record_running_launch'), \
                 patch.object(d,'_check_headless_health',return_value=None):
                result=d.dispatch(dict(id=LAUNCH,engine='codex',account='fixture',profile=None,
                    mode='headless',workspace='repo',prompt='read own intake',worker_credential_required=True))
            self.assertEqual(result['state'],'running',repr(result))
            self.assertEqual(set(submitted['extra_env']),{'HUB_WORKER_CONFIG_FILE'})
            self.assertNotIn('hubt_scoped-test',json.dumps(submitted))

    def test_interactive_provision_failure_never_starts_native_engine_or_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub=dict(engine='codex',service='codex',account_label='fixture',profile=None)
            runner_calls=[]
            dispatcher=fleet.Dispatcher(dict(version=1,workspaces={'repo':tmp},subscriptions=[]),lambda *a:{},
                                        lambda argv,**kw: runner_calls.append(argv) or {})
            with patch.object(dispatcher,'_route',return_value=sub), \
                 patch.object(dispatcher,'_dispatch_admission',return_value=fleet.utcnow()), \
                 patch.object(worker_auth,'provision',side_effect=RuntimeError('PRIVATE_RESPONSE')):
                result=dispatcher.dispatch(dict(id=LAUNCH,engine='codex',account='fixture',profile=None,
                    mode='interactive',workspace='repo',prompt='',worker_credential_required=True))
            self.assertEqual(result['state'],'uncertain')
            self.assertFalse(runner_calls)
            self.assertNotIn('PRIVATE_RESPONSE',json.dumps(result))

    def test_managed_environment_keeps_discovery_identity_and_scrubs_operator_tokens(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'APLEXER_TAG':'hub-'+LAUNCH, 'APLEXER_WORKSPACE':tmp,
            'HUB_TOKEN':'operator-secret', 'HUB_CONFIG_FILE':'/operator/profile',
        }), patch.object(provisioning,'load_git_identity',return_value={}):
            env=provisioning.engine_environment({'provider':'codex'},Path(tmp)/'managed')
            self.assertEqual(env['APLEXER_TAG'],'hub-'+LAUNCH)
            self.assertEqual(env['APLEXER_WORKSPACE'],tmp)
            self.assertNotIn('HUB_TOKEN',env)
            self.assertNotIn('HUB_CONFIG_FILE',env)
