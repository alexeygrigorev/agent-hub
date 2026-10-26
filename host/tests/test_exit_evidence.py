import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_hub_host.exit_evidence import snapshot_native, terminal_evidence, sanitize

NATIVE = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OFFER = '11111111-2222-4333-8444-555555555555'


class ExitEvidence(unittest.TestCase):
    def test_malformed_journal_fields_cannot_crash_or_poison_terminal_receipt(self):
        for malformed in (None, 'legacy', 7, [], {'log_tail': None},
                          {'log_tail': [], 'engine_outcome': {}, 'exit_code': True, 'exit_signal': 'SIGTERM'},
                          {'engine_outcome': 'unknown', 'exit_code': 2**60, 'exit_signal': -9}):
            with self.subTest(saved=malformed):
                result = terminal_evidence({'exit_evidence': malformed, 'runtime_exit': 'legacy'}, {})
                self.assertEqual(result['log_tail'], '')
                self.assertIsNone(result['exit_code'])
                self.assertIsNone(result['exit_signal'])
                self.assertIsNone(result['engine_outcome'])
        result = terminal_evidence({'exit_evidence': {'exit_code': 'bad'}, 'runtime_exit': {'code': 0}}, {})
        self.assertEqual(result['exit_code'], 0)

    def test_supplementary_unicode_respects_hub_utf16_budget(self):
        from agent_hub_host.exit_evidence import utf16_clip
        text=('a ' * 1023)+'b'+chr(0x1f600)
        record={'exit_evidence':{'log_tail':text}}
        result=terminal_evidence(record,{})
        self.assertLessEqual(len(result['log_tail'].encode('utf-16-le'))//2,2048)
        self.assertTrue(result['log_tail'].endswith(chr(0x1f600)))
        result['outcome']='failed-without-report'
        # Exercise the actual bundled Hub validator, rather than duplicating
        # JavaScript's length rule in another Python assertion.
        script = """
import {buildSync} from 'esbuild';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const build=buildSync({stdin:{contents:"export {sanitizeExitEvidence} from './src/exit-evidence.ts'",resolveDir:process.cwd()},bundle:true,write:false,platform:'node',format:'cjs'});
const mod={exports:{}};
new Function('module','exports','require',build.outputFiles[0].text)(mod,mod.exports,createRequire(import.meta.url));
process.exit(mod.exports.sanitizeExitEvidence(JSON.parse(readFileSync(0,'utf8'))) ? 0 : 2);
"""
        proc=subprocess.run(['node','--input-type=module','-e',script],
                            input=json.dumps(result),text=True,capture_output=True,timeout=20,
                            cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(proc.returncode,0,proc.stderr)

        self.assertEqual(utf16_clip('ab'+chr(0x1f600),3),'ab')
        self.assertEqual(utf16_clip(chr(0x1f600)+'ab',3,tail=True),'ab')
        git=Mock(side_effect=[Mock(stdout=''),Mock(stdout=' M '+chr(0x1f600)*255)])
        paths=terminal_evidence({'incarnation':1000,'workspace':'/repo'},{},git=git)['dirty_paths']
        self.assertLessEqual(len(paths[0].encode('utf-16-le'))//2,256)

    def test_generated_fallback_report_and_artifacts_pass_actual_finish_result_validator(self):
        from agent_hub_host.fleet import Dispatcher
        with tempfile.TemporaryDirectory() as tmp:
            record=dict(offer_id=OFFER,task_id='hub-'+OFFER,mode='headless',workspace=tmp,
                        native_id=NATIVE,tag='task-hub-'+OFFER,incarnation=1000,engine='opencode',
                        state='running',settled=False,result_path=str(Path(tmp)/'.local/hub-results'/(OFFER+'.json')),
                        runtime_exit={'code':0},exit_evidence={'log_tail':chr(0x1f600)*1024})
            d=Dispatcher({'version':1,'workspaces':{'main':tmp},'subscriptions':[],
                          'launcher':{'argv':['launcher'],'root':tmp,'config_dir':tmp}})
            d._active_launches={OFFER:record}
            sent=[]
            def request(method,path,payload=None):
                sent.append(payload);return {'id':OFFER,'state':payload['state']}
            with patch('agent_hub_host.fleet.verify_native_retirement',return_value=(True,{'finished_at_ms':3100},'verified')), \
                 patch.object(d,'runner',return_value={'state':'failed','native_id':NATIVE,'resources_reserved':False}), \
                 patch.object(d,'request',side_effect=request), patch.object(d,'_persist_launch_record'), \
                 patch.object(d,'_no_result_partial_report',return_value={'report':'work '*700,
                    'artifacts':[{'label':'partial work','text':chr(0x1f600)*900}]}), \
                 patch.object(d,'_maybe_cleanup_worktree'), \
                 patch('agent_hub_host.runtime_diagnostics.subprocess.run',side_effect=OSError):
                d.reconcile_launches()
            payload=sent[0]
            self.assertLessEqual(len(payload['report'].encode('utf-16-le'))//2,4096)
            self.assertLessEqual(len(payload['artifacts'][0]['text'].encode('utf-16-le'))//2,1024)
            script = 'import {buildSync} from \'esbuild\';\nimport {readFileSync} from \'node:fs\';\nimport {createRequire} from \'node:module\';\nconst build=buildSync({stdin:{contents:"export {FleetService} from \'./src/fleet.ts\'",resolveDir:process.cwd()},bundle:true,write:false,platform:\'node\',format:\'cjs\'});\nconst mod={exports:{}};new Function(\'module\',\'exports\',\'require\',build.outputFiles[0].text)(mod,mod.exports,createRequire(import.meta.url));\nconst input=JSON.parse(readFileSync(0,\'utf8\'));\nconst row={id:input.launch_id,state:\'running\',mode:\'headless\',machine_id:\'fixture-host\',engine:\'opencode\',account:\'fixture\',native_id:input.payload.native_id,launcher_task_id:input.payload.launcher_task_id,request_json:\'{}\',request_key:\'fixture\',actor:\'fixture\'};\nconst sql={exec(q,...args){return {toArray:()=>q.startsWith(\'SELECT * FROM fleet_launches WHERE id = ? AND machine_id = ?\')?[row]:[],one:()=>({n:0}),[Symbol.iterator]:function*(){}};}};\nconst fleet=new mod.exports.FleetService(sql,fn=>fn());\nconst res=await fleet.handle(new Request(\'https://hub.test/api/fleet/launches/\'+input.launch_id+\'/result\',{method:\'POST\',body:JSON.stringify(input.payload)}),{caller:{kind:\'host\',machineId:\'fixture-host\'},scopes:[\'host\']});\nconsole.log(JSON.stringify({status:res.status,body:await res.json()}));\nprocess.exit(res.status===200?0:2);\n'
            proc=subprocess.run(['node','--input-type=module','-e',script],
                input=json.dumps({'launch_id':OFFER,'payload':payload}),text=True,capture_output=True,timeout=20,
                cwd=Path(__file__).resolve().parents[2])
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)

    def test_durable_native_exit_receipt_survives_absent_catalog_observation(self):
        for code, signal in [(0,None),(3,None),(None,15)]:
            record={'incarnation':1000}
            receipt={'finished_at_ms':3100,'exit':{'code':code,'signal':signal,
                     'oom_killed':False,'exited_at_ms':3000}}
            result=terminal_evidence(record,receipt,git=Mock())
            self.assertEqual((result['exit_code'],result['exit_signal'],result['wall_time_ms']),
                             (code,signal,2000))
            self.assertEqual(record['runtime_exit']['code'],code)
        result=terminal_evidence({'incarnation':1000},{'exit':'malformed','finished_at_ms':3100})
        self.assertIsNone(result['exit_code'])

    def test_engine_exit_zero_without_result_is_failed_with_partial_work(self):
        from agent_hub_host.fleet import Dispatcher
        with tempfile.TemporaryDirectory() as tmp:
            record = dict(offer_id=OFFER, task_id='hub-' + OFFER, mode='headless',
                          workspace=tmp, engine='opencode', native_id=NATIVE,
                          tag='task-hub-' + OFFER, incarnation=1000, state='running', settled=False,
                          result_path=str(Path(tmp) / '.local' / 'hub-results' / (OFFER + '.json')),
                          runtime_exit={'code': 0}, exit_evidence={'log_tail': 'worked on feature', 'engine_outcome': 'succeeded'})
            d = Dispatcher({'version': 1, 'workspaces': {'main': tmp}, 'subscriptions': [],
                            'launcher': {'argv': ['launcher'], 'root': tmp, 'config_dir': tmp}})
            d._active_launches = {OFFER: record}
            seen = []
            def request(method, path, payload=None):
                seen.append(payload)
                return {'id': OFFER, 'state': payload['state']}
            with patch('agent_hub_host.fleet.verify_native_retirement', return_value=(True, {'finished_at_ms': 3100}, {})), \
                 patch.object(d, 'runner', return_value={'state': 'failed', 'resources_reserved': False, 'native_id': NATIVE}), \
                 patch.object(d, 'request', side_effect=request), \
                 patch.object(d, '_persist_launch_record'), patch.object(d, '_maybe_cleanup_worktree'), \
                 patch('agent_hub_host.runtime_diagnostics.subprocess.run', side_effect=OSError):
                result = d.reconcile_launches()
            self.assertEqual(result[0]['state'], 'failed')
            self.assertEqual(seen[0]['runtime']['exit_code'], 0)
            self.assertEqual(seen[0]['runtime']['wall_time_ms'], 2100)
            self.assertEqual(seen[0]['runtime']['engine_outcome'], 'succeeded')
            self.assertEqual(seen[0]['runtime']['outcome'], 'failed-with-partial-work')
            self.assertIn('worked on feature', seen[0]['report'])

    def test_exit_without_report_keeps_exact_code_signal_tail_and_wall_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / NATIVE / 'history.bin'
            history.parent.mkdir()
            history.write_text('worked on feature\nBearer private-value\nhubt_fake-private-value\nexiting\n')
            record = dict(offer_id=OFFER, native_id=NATIVE, incarnation=1000,
                          tag='task-hub-' + OFFER, workspace=tmp, engine='opencode')
            row = dict(id=NATIVE, created_at_ms=1000, tag=record['tag'], workspace=tmp,
                       engine='opencode', history_path=str(history), exit=dict(code=17, signal=9, exited_at_ms=3100))
            saved = snapshot_native(record, row)
            record['exit_evidence'] = saved
            git = Mock(side_effect=[Mock(stdout='a' * 40 + '\n'), Mock(stdout=' M host/example.py\n')])
            result = terminal_evidence(record, {}, git=git)
            self.assertEqual((result['exit_code'], result['exit_signal'], result['wall_time_ms']), (17, 9, 2100))
            self.assertIn('exiting', result['log_tail'])
            self.assertNotIn('private-value', json.dumps(result))
            self.assertEqual(result['observed_commits'], ['a' * 40])
            self.assertEqual(result['dirty_paths'], [' M host/example.py'])
            retained = root / '.local' / 'hub-session-logs' / (OFFER + '.json')
            self.assertEqual(retained.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(retained.read_text())['exit_code'], 17)

    def test_other_incarnation_or_symlink_never_supplies_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / NATIVE / 'history.bin'
            path.parent.mkdir()
            outside = Path(tmp) / 'private'
            outside.write_text('private payload')
            path.symlink_to(outside)
            record = dict(offer_id=OFFER, native_id=NATIVE, incarnation=1000,
                          tag='task', workspace=tmp, engine='opencode')
            row = dict(id=NATIVE, created_at_ms=1001, tag='task', workspace=tmp,
                       engine='opencode', history_path=str(path))
            self.assertIsNone(snapshot_native(record, row))
            row['created_at_ms'] = 1000
            self.assertNotIn('log_tail', snapshot_native(record, row))

    def test_unknown_measurements_are_null_and_credentials_are_redacted_before_clipping(self):
        result = terminal_evidence({}, {}, git=Mock())
        self.assertIsNone(result['exit_code'])
        self.assertIsNone(result['wall_time_ms'])
        raw = '\x1b[31mAuthorization: Bearer sensitive-value\nsecret=hidden\nhubt_' + 's' * 3000
        self.assertNotIn('sensitive-value', sanitize(raw))
        self.assertNotIn('hidden', sanitize(raw))
        self.assertNotIn('s' * 20, sanitize(raw))
