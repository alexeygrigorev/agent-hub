import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from agent_hub_host import headless_health as health, fleet

SID = '99999999-9999-4999-8999-999999999999'
OID = '12345678-1234-4234-8234-123456789abc'
TASK = 'hub-' + OID

def event(text):
    return json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})

class HeadlessHealthTests(unittest.TestCase):
    def test_real_first_turn_refusals_and_native_router_error(self):
        for text in (event('Only read-only Bash commands work; writes are refused ("No permission client configured").'),
                     '\x1b[31mERROR\x1b[0m codex_core::tools::router: error=unsupported call: Write',
                     'No permission client configured for Bash',
                     json.dumps({'type':'item.completed','item':{'type':'command_execution','exit_code':1,'aggregated_output':'No permission client configured'}})):
            self.assertTrue(health.permission_refusal(text), text)
        self.assertNotIn('private-value', str(health.failure()))

    def test_echoes_tasks_fixtures_and_successful_commands_do_not_trip_guard(self):
        for text in ('Task: fix No permission client configured',
                     json.dumps({'goal': 'No permission client configured'}),
                     json.dumps({'type':'item.completed','item':{'type':'user_message','text':'No permission client configured; writes refused'}}),
                     json.dumps({'type':'item.completed','item':{'type':'command_execution','exit_code':0,'aggregated_output':'No permission client configured'}}),
                     event('The acceptance criteria reproduce No permission client configured; writes are refused.'),
                     'code = \"ERROR codex_core::tools::router: unsupported call: Write\"',
                     event('Added test_fixture for No permission client configured errors.'),
                     event('Sandbox is workspace-write; git status and network succeeded.')):
            self.assertFalse(health.permission_refusal(text), text)

    def setup_launch(self, root):
        root = Path(root).resolve(); (root / '.local').mkdir()
        doc = {'id': SID, 'tag': 'task-' + TASK, 'workspace': str(root), 'engine': 'zcodex', 'created_at_ms': 1000}
        path = root / '.local' / f'launch-{TASK}-start.json'; path.write_text(json.dumps(doc))
        return path, doc

    def test_managed_codex_and_zai_permissions_have_same_sandbox_contract(self):
        from agent_hub_host.provisioning import install_managed_engine_permissions
        with tempfile.TemporaryDirectory() as tmp:
            for provider,directory in [('codex','.codex'),('zai','.zcodex')]:
                install_managed_engine_permissions({'provider':provider},Path(tmp),trusted_paths=[tmp])
                text=(Path(tmp)/directory/'config.toml').read_text()
                self.assertIn('approval_policy = "never"',text)
                self.assertIn('sandbox_mode = "danger-full-access"',text)

    def test_exact_identity_required_before_capture_or_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, doc = self.setup_launch(tmp)
            self.assertEqual(health.launch_target(tmp,TASK,'zcodex').session_id,SID)
            for key,value in [('tag','another-task'),('engine','codex'),('workspace','/other'),('created_at_ms',True)]:
                path.write_text(json.dumps(dict(doc,**{key:value})))
                with self.assertRaises(Exception): health.launch_target(tmp,TASK,'zcodex')
            path.unlink(); path.symlink_to(Path(tmp)/'foreign')
            with self.assertRaises(Exception): health.launch_target(tmp,TASK,'zcodex')

    def test_guard_keeps_reservation_pending_full_terminal_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_launch(tmp); requests=[]
            def request(method,path,body=None):
                requests.append((path,body)); return {'id':OID,'state':body.get('state')} if body else {}
            d=fleet.Dispatcher({'subscriptions':[]},request)
            with patch('agent_hub_host.headless_health.inspect',return_value=health.failure()), \
                 patch('agent_hub_host.native_sessions.stop_session',return_value=SimpleNamespace(state='stopped')) as stop:
                result=d._check_headless_health(OID,TASK,tmp,'zcodex')
            self.assertEqual(result['state'],'uncertain'); self.assertNotIn('confirmed_exit', result)
            self.assertEqual(result['native_id'],SID); self.assertEqual(stop.call_args.args[1].session_id,SID)
            self.assertFalse(d._active_launches[OID]['settled'])
            self.assertTrue(d._active_launches[OID]['health_stop_confirmed'])
            self.assertEqual(requests[0][1]['reason'],health.REASON)
            self.assertNotIn('private-value',str(requests))

    def test_guard_retains_reservation_and_never_rekills_when_exit_unknown(self):
        from agent_hub_host.native_sessions import SessionUncertain
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_launch(tmp)
            d=fleet.Dispatcher({'subscriptions':[]},lambda *args:{})
            exc=SessionUncertain('unknown'); exc.oracle={'session_id':SID}; exc.kill={}
            with patch('agent_hub_host.headless_health.inspect',return_value=health.failure()), \
                 patch('agent_hub_host.native_sessions.stop_session',side_effect=exc) as stop, \
                 patch('agent_hub_host.native_sessions.reconcile_stop_oracle',return_value=(False,'unknown')):
                result=d._check_headless_health(OID,TASK,tmp,'zcodex')
                again=d._check_headless_health(OID,TASK,tmp,'zcodex')
            self.assertEqual(result['state'],'uncertain'); self.assertNotIn('confirmed_exit',result)
            self.assertEqual(again['state'],'uncertain'); self.assertEqual(stop.call_count,1)
            self.assertFalse(d._active_launches[OID]['settled'])

    def test_dispatch_launcher_error_checks_actual_owned_first_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_launch(tmp)
            cfg={'workspaces':{'demo':tmp},'subscriptions':[{'service':'zai','engine':'zcodex','account_label':'zai-test','profile':None,'admission_argv':['gate'],'launcher_provider':'zai','launcher_model':'glm-5.3-flash'}],
                 'launcher':{'argv':['launcher'],'root':tmp,'config_dir':str(Path(tmp)/'cfg')}}
            requests=[]
            def runner(argv,**kw):
                if argv[0]=='gate': return {'launch_allowed':True,'profile_supported':True,'account_label':'zai-test','observed_at':fleet.utcnow()}
                if 'plan' in argv: return {'eligible':[{'provider':'zai','model':'glm-5.3-flash','task_fit':1.0}]}
                if 'run' in argv: raise RuntimeError('The launcher command failed; unrecognized output was withheld.')
                return {}
            def request(method,path,body=None):
                requests.append((path,body)); return {'id':OID,'state':body.get('state')} if body else {}
            d=fleet.Dispatcher(cfg,request,runner)
            with patch('agent_hub_host.headless_health.inspect',return_value=health.failure()), \
                 patch('agent_hub_host.native_sessions.stop_session',return_value=SimpleNamespace(state='stopped')):
                result=d.dispatch({'id':OID,'engine':'zcodex','account':'zai-test','profile':None,'mode':'headless','workspace':'demo','prompt':'perform work'})
            self.assertEqual(result['state'],'uncertain'); self.assertEqual(result['reason'],health.REASON)
            self.assertEqual(requests[-1][1]['state'],'uncertain')
            self.assertNotIn('confirmed_exit', requests[-1][1])

    def test_journal_incarnation_cannot_be_replaced_by_new_start_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_launch(tmp)
            d=fleet.Dispatcher({'subscriptions':[]},lambda *args:{})
            d._active_launches[OID]={'native_id':SID,'tag':'task-'+TASK,'workspace':str(Path(tmp).resolve()),'engine':'zcodex','incarnation':999}
            with patch('agent_hub_host.headless_health.inspect') as capture:
                self.assertIsNone(d._check_headless_health(OID,TASK,tmp,'zcodex'))
            capture.assert_not_called()

    def test_mismatched_launch_never_captures_or_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,doc=self.setup_launch(tmp); path.write_text(json.dumps(dict(doc,tag='someone-else')))
            d=fleet.Dispatcher({'subscriptions':[]},lambda *args:{})
            with patch('agent_hub_host.headless_health.inspect') as capture, patch('agent_hub_host.native_sessions.stop_session') as stop:
                self.assertIsNone(d._check_headless_health(OID,TASK,tmp,'zcodex'))
            capture.assert_not_called(); stop.assert_not_called()
