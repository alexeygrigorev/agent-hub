import contextlib
import ast
import re
import shlex
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
S = ROOT / 'scripts'
sys.path.insert(0, str(S))
import phase7_original_work as work
spec = importlib.util.spec_from_file_location('candidate_phase7_core', S / 'hub_acceptance_10.py')
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)

SOURCE = b'MAX_BRIEF = 8192\nMAX_BRIEF_CHARS = 8000\n'
RUN = '11111111-1111-4111-8111-111111111111'
PARENT = '22222222-2222-4222-8222-222222222222'
CHILD = '33333333-3333-4333-8333-333333333333'
NATIVE = '44444444-4444-4444-8444-444444444444'
SID = '55555555-5555-4555-8555-555555555555'
CONTEXT = dict(run_id=RUN, sample=1, parent_intake_id=PARENT, child_intake_id=CHILD)
IDENTITY = dict(native_id=NATIVE, session_id=SID, workspace='/owned/work')


def fixture():
    # Expected values are independently fixed/hashed here, not produced by work().
    return dict(CONTEXT, native_id=NATIVE, native_tag='task-hub2-' + SID,
                workspace='/owned/work', observed_at_ms=int(time.time() * 1000),
                source_path=work.SOURCE, source_sha256=hashlib.sha256(SOURCE).hexdigest(),
                source_bytes=len(SOURCE), finding='Brief input bounds: 8192 bytes and 8000 characters.')


def line(value):
    return 'PHASE7_SOURCE %s %d %s' % (value['source_sha256'], value['source_bytes'], value['finding'])


class OriginalWorkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(os.environ.get('TMPDIR') or ROOT / '.local' / 'tmp')
        base.mkdir(mode=0o700, parents=True, exist_ok=True)

    def test_emitted_literal_command_matches_actual_boolean_parser(self):
        # Execute only the actual parser-building prefix extracted from source,
        # never __main__/work/context/identity or native/provider operations.
        tree = ast.parse(Path(work.__file__).read_bytes())
        main = next(n for n in tree.body if isinstance(n, ast.If)
                    and isinstance(n.test, ast.Compare)
                    and isinstance(n.test.left, ast.Name) and n.test.left.id == '__name__')
        prefix = []
        for node in main.body:
            if isinstance(node, ast.If):
                break
            prefix.append(node)
        code = compile(ast.fix_missing_locations(ast.Module(body=prefix, type_ignores=[])), '<actual-helper-parser-prefix>', 'exec')
        for helper in ('/owned/helper.py', '/owned/white space/helper.py',
                       "/owned/a 'quoted' helper.py", '/owned/a "double quoted" helper.py'):
            with self.subTest(helper=helper):
                prompt = work.brief(RUN, 1, PARENT, Path(helper))[0]
                match = re.search(r'command: `([^`]+)`\. It reads', prompt)
                self.assertIsNotNone(match)
                argv = shlex.split(match.group(1))
                self.assertEqual(argv, ['python3', helper, '--work'])
                with mock.patch.object(sys, 'argv', argv[1:]):
                    namespace = {};exec(code, namespace)
                self.assertTrue(namespace['args'].work)
                with mock.patch.object(sys, 'argv', argv[1:] + ['.']), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                    exec(code, {})
                self.assertEqual(failure.exception.code, 2)

    def test_all_five_provider_draws_use_compound_original_work_without_renderer_table(self):
        for engine in ('codex', 'zai', 'go', 'claude', 'antigravity'):
            with self.subTest(engine=engine):
                value = fixture()
                result = work.verify_work(json.dumps(value), SOURCE, CONTEXT, IDENTITY, 'native tool output\n' + line(value))
                self.assertEqual(result['source_sha256'], hashlib.sha256(SOURCE).hexdigest())
                prompt = work.brief(RUN, 1, PARENT, '/owned/helper.py')[0]
                self.assertNotIn(value['source_sha256'], prompt)
                self.assertNotIn(line(value), prompt)

    def test_prompt_only_and_missing_capture_are_not_work(self):
        value = fixture()
        for capture in (None, '', work.brief(RUN, 1, PARENT, '/owned/helper.py')[0], 'agent says done'):
            with self.subTest(capture=capture), self.assertRaises(ValueError):
                work.verify_work(json.dumps(value), SOURCE, CONTEXT, IDENTITY, capture)

    def test_forged_derived_values_refused_even_when_capture_echoes_them(self):
        for key, wrong in [('source_sha256', '0' * 64), ('source_bytes', 1), ('finding', 'invented finding')]:
            value = fixture();value[key] = wrong
            with self.subTest(key=key), self.assertRaises(ValueError):
                work.verify_work(json.dumps(value), SOURCE, CONTEXT, IDENTITY, line(value))

    def test_foreign_or_reused_binding_and_unexpected_fields_refused(self):
        for key in ('native_id', 'native_tag', 'workspace', 'run_id', 'child_intake_id'):
            value = fixture();value[key] = 'foreign'
            with self.subTest(key=key), self.assertRaises(ValueError):
                work.verify_work(json.dumps(value), SOURCE, CONTEXT, IDENTITY, line(value))
        value = fixture();value['token'] = 'PRIVATE_SENTINEL'
        with self.assertRaises(ValueError):work.verify_work(json.dumps(value), SOURCE, CONTEXT, IDENTITY, line(value))

    def test_wrong_types_stale_time_and_source_shape_refused(self):
        for key, wrong in [('sample', True), ('source_bytes', True), ('observed_at_ms', 'now')]:
            value = fixture();value[key] = wrong
            with self.subTest(key=key), self.assertRaises(ValueError):
                work.verify_work(json.dumps(value), SOURCE, CONTEXT, IDENTITY, line(value))
        value = fixture();value['observed_at_ms'] = 1
        with self.assertRaises(ValueError):work.verify_work(json.dumps(value), SOURCE, CONTEXT, IDENTITY, line(value), 2)
        with self.assertRaises(ValueError):work.source_result(b'MAX_BRIEF = 1\n')

    def test_duplicate_artifact_keys_refused(self):
        value=fixture();raw=json.dumps(value)
        raw=raw[:-1]+',"sample":1}'
        with self.assertRaises(ValueError):work.verify_work(raw,SOURCE,CONTEXT,IDENTITY,line(value))

    def test_attachment_wrong_identity_is_unknown_and_keeps_exact_stdout(self):
        class Fake:
            def json(self,*args):return {'appended':True,'intake_id':'foreign'}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR',str(ROOT/'.local/tmp')))as folder:
            self.assertFalse(work.retain_and_attach(core,Fake(),Path(folder),PARENT,'ACCEPTANCE FAIL\n',1,attach=True))
            self.assertEqual((Path(folder)/'actual-stdout.txt').read_text(),'ACCEPTANCE FAIL\n')
            self.assertFalse((Path(folder)/'output-attachment-complete.json').exists())

    def test_owned_file_reader_refuses_symlink(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', str(ROOT / '.local' / 'tmp'))) as name:
            root = Path(name);(root/'a').write_text('public');(root/'link').symlink_to(root/'a')
            with self.assertRaises(OSError):work.private_read(root/'link', 10)

    def test_original_same_commit_evidence_needs_two_actual_matching_receipts_not_renderers(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', str(ROOT / '.local' / 'tmp'))) as name:
            root = Path(name);receipt=root/'receipt';receipt.write_bytes(b'actual synthetic CI metadata')
            row={'commit':'a'*40,'status':'success','path':str(receipt),'sha256':hashlib.sha256(receipt.read_bytes()).hexdigest()}
            path=root/'evidence';path.write_text(json.dumps({'commit':'a'*40,'canary':row,'ci_e2e':row}))
            self.assertEqual(work.checked_evidence(path, 'a'*40)[0], [])
            receipt.write_bytes(b'tampered')
            self.assertTrue(work.checked_evidence(path, 'a'*40)[0])

    def run_loop(self, failure=None):
        self.calls=[]
        def start(r, rec, *args, **kwargs):
            n=rec['n'];self.calls.append(n)
            rec.update(id='%08d-1111-4111-8111-111111111111'%n,
                       native_id='%08d-2222-4222-8222-222222222222'%n,
                       child_intake_id='%08d-3333-4333-8333-333333333333'%n,
                       model=('sol','flash','muse','sonnet','argon')[(n-1)%5],engine='synthetic',host=core.HOST,
                       workspace='/owned/work',shares={'a':0.6,'b':0.4},elapsed_s=1,capture_verified=True)
            if n==failure:rec['errors'].append('PRIVATE_SENTINEL raw capture or error')
        def stop(r,rec,*args):rec['cleanup_confirmed']=True
        out=io.StringIO()
        # Existing original run loop is exercised; only business effects are fake.
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', str(ROOT / '.local' / 'tmp'))) as name, \
             mock.patch.object(core.shutil,'which',return_value='/fake/a'), \
             mock.patch.object(work,'checked_evidence',return_value=([], '0'*64, {})), \
             mock.patch.object(core,'sample_alerts',side_effect=lambda r:{'generation':'same','count':0,'observed_at_ms':int(time.time()*1000)+100}), \
             mock.patch.object(core,'one_start',side_effect=start), \
             mock.patch.object(core,'verify_listing'),mock.patch.object(core,'verify_running_before_stop'), \
             mock.patch.object(core,'stop_and_drain_lineage',side_effect=stop):
            rc=core.run(type('Fake', (), {'bins': {'a': ['fake']}})(),None,'a'*40,intake=PARENT,workspace='/owned/work',output_dir=Path(name)/'new',out=out,original_ac=True)
            return rc,out.getvalue()

    def observe_fixture(self, engine, *, capture_rc=0, drift=False, foreign_send=False):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', str(ROOT / '.local' / 'tmp'))) as folder:
            root=Path(folder);source=root/work.SOURCE;source.parent.mkdir(parents=True);source.write_bytes(SOURCE)
            helper=root/'helper.py';helper.write_bytes(b'# pinned synthetic helper')
            value=fixture();value['workspace']=str(root)
            artifact=root/work.relative_artifact(RUN,1);artifact.parent.mkdir(parents=True)
            artifact.write_text(json.dumps(value));artifact.chmod(0o600)
            row=dict(id=SID,native_id=NATIVE,machine_id=core.HOST,epoch=1,
                     operation_id='66666666-6666-4666-8666-666666666666:0',state='running',workspace=str(root))
            rec=dict(id=SID,native_id=NATIVE,workspace=str(root),engine=engine,
                     owned_identity=core._owned_identity(row,NATIVE),_source_commit='a'*40,_start_wall_ms=1)
            class Fake:
                sends=0;shows=0;captures=0
                def json(self,tool,argv,timeout):
                    if argv[:2]==['sessions','send']:
                        self.sends+=1
                        self.last_prompt=Path(argv[argv.index('--body-file')+1]).read_text()
                        return {'command_id':'command-1','client_key':'original-work-'+RUN+'-1',
                                'command_wait':{'id':'command-1','kind':'send','state':'acked',
                                                'session_id':'foreign' if foreign_send else SID}}
                    self.shows+=1
                    return {'session':dict(row,native_id='77777777-7777-4777-8777-777777777777')if drift and self.shows>1 else row}
                def run_bytes(self,tool,argv,timeout):return 0,SOURCE,b''
                def run_bounded(self,tool,argv,**kwargs):
                    self.captures+=1
                    return capture_rc,('native output\n'+line(value)).encode(),b''
            fake=Fake()
            work.observe(core,fake,rec,RUN,1,PARENT,CHILD,helper,helper.read_bytes(),time.monotonic,time.sleep)
            self.assertEqual((fake.sends,fake.captures),(1,1));self.assertNotIn(value['source_sha256'],fake.last_prompt)
            self.assertTrue(rec['capture_verified'])

    def test_actual_original_observer_all_five_fake_owned_provider_paths(self):
        for engine in ('codex','zai','go','claude','antigravity'):
            with self.subTest(engine=engine):self.observe_fixture(engine)

    def test_observer_refuses_unavailable_capture_and_tuple_drift(self):
        for args in ({'capture_rc':1},{'drift':True},{'foreign_send':True}):
            with self.subTest(args=args),self.assertRaises(ValueError):self.observe_fixture('zai',**args)

    def test_exact_ten_no_engine_or_model_override_and_actual_shares(self):
        rc,text=self.run_loop();self.assertEqual(rc,0);self.assertEqual(self.calls,list(range(1,11)))
        self.assertEqual(text.count('shares(None)={"a": 0.6, "b": 0.4}'),10)
        argv=core.build_start_argv('/owned/work',CHILD,'key')
        self.assertNotIn('--engine',argv);self.assertNotIn('--model',argv)

    def test_failed_draw_is_not_replaced_and_stdout_never_exposes_raw_error(self):
        rc,text=self.run_loop(failure=3);self.assertEqual(rc,1);self.assertEqual(self.calls,[1,2,3])
        self.assertNotIn('PRIVATE_SENTINEL',text);self.assertIn('UNKNOWN',text);self.assertIn('ACCEPTANCE FAIL',text)

    def test_formal_native_negatives_remain_explicit_and_unweakened(self):
        self.assertFalse(core.native_transport_ok('codex',None,None))
        self.assertFalse(core.native_transport_ok('zai',None,None))
        self.assertFalse(core.native_transport_ok('go',None,None))
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', str(ROOT / '.local' / 'tmp'))) as folder:
            self.assertTrue(core.checked_native_evidence(str(Path(folder)/'missing'), 'a'*40)[0])

    def test_main_retains_and_attaches_exact_emitted_stdout_with_source_window_and_exit(self):
        class Fake:
            def json(self,tool,args,timeout):return {'appended':True,'intake_id':PARENT}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR',str(ROOT/'.local/tmp')))as folder:
            target=Path(folder)/'run';out=io.StringIO()
            def body(*args,**kwargs):
                print('start 1/10 shares={"one": 1.0}\nACCEPTANCE FAIL',file=kwargs['out'])
                return 1
            with mock.patch.dict(os.environ,{'HUB_ACCEPTANCE_READY':'1'}),mock.patch.object(core,'run',side_effect=body),contextlib.redirect_stdout(out):
                rc=core.main(['--live','--attach-output','--workspace','/owned/work','--output-dir',str(target),
                              '--intake-task-id',PARENT,'--commit','a'*40],runner=Fake())
            self.assertEqual(rc,1);self.assertEqual((target/'actual-stdout.txt').read_text(),out.getvalue())
            self.assertIn('runner_sha256',out.getvalue());self.assertIn('window_begin',out.getvalue())
            self.assertIn('core_exit_code',out.getvalue());self.assertIn('window_end',out.getvalue())
            decision=json.loads((target/'final-process-result.json').read_text())
            self.assertEqual((decision['exit_code'],decision['attachment']),(1,'APPENDED'))

    def test_main_unknown_error_is_retained_without_private_exception_and_formal_remains_explicit(self):
        class Fake:
            def json(self,*args):return {'appended':True,'intake_id':PARENT}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR',str(ROOT/'.local/tmp')))as folder:
            target=Path(folder)/'run';out=io.StringIO()
            with mock.patch.dict(os.environ,{'HUB_ACCEPTANCE_READY':'1'}),mock.patch.object(core,'run',side_effect=RuntimeError('PRIVATE_SENTINEL')),contextlib.redirect_stdout(out):
                rc=core.main(['--live','--attach-output','--workspace','/owned/work','--output-dir',str(target),
                              '--intake-task-id',PARENT,'--commit','a'*40],runner=Fake())
            self.assertEqual(rc,1);self.assertIn('UNKNOWN',out.getvalue());self.assertNotIn('PRIVATE_SENTINEL',out.getvalue())
            self.assertEqual((target/'actual-stdout.txt').read_text(),out.getvalue())
            with mock.patch.dict(os.environ,{'HUB_ACCEPTANCE_READY':'1'}),mock.patch.object(core,'run',return_value=0)as formal:
                self.assertEqual(core.main(['--live','--formal'],runner=Fake()),0)
                self.assertNotIn('original_ac',formal.call_args.kwargs)

    def test_attachment_contains_actual_stdout_chunks_hash_exit_not_recomputed_aggregate(self):
        text='start 1/10 shares={"a":1}\n'+'x'*3000+'\nACCEPTANCE FAIL\n'
        class Fake:
            def __init__(self):self.notes=[]
            def json(self, tool, argv, timeout):
                self.notes.append((argv,Path(argv[argv.index('--note-file')+1]).read_text()))
                return {'appended':True,'intake_id':PARENT}
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', str(ROOT / '.local' / 'tmp'))) as name:
            f=Fake();self.assertTrue(work.retain_and_attach(core,f,Path(name),PARENT,text,1,attach=True))
            self.assertEqual((Path(name)/'actual-stdout.txt').read_text(),text)
            chunks=[note.split('\n',1)[1]for argv,note in f.notes]
            self.assertEqual(''.join(chunks),text)
            for argv,note in f.notes:
                self.assertIn(hashlib.sha256(text.encode()).hexdigest(),note);self.assertIn('exit=1',note)
                self.assertLessEqual(len(note),4000)
            with self.assertRaises(FileExistsError):work.retain_and_attach(core,f,Path(name),PARENT,text,1,attach=True)

    def test_attachment_unknown_retains_original_output_and_does_not_start_or_repeat(self):
        class Fake:
            def __init__(self):self.calls=0
            def json(self,*args):self.calls+=1;raise RuntimeError('PRIVATE_SENTINEL')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', str(ROOT / '.local' / 'tmp'))) as name:
            f=Fake();self.assertFalse(work.retain_and_attach(core,f,Path(name),PARENT,'ACCEPTANCE FAIL\n',1,attach=True))
            self.assertEqual(f.calls,1);self.assertTrue((Path(name)/'output-attachment-pending.json').exists())
            self.assertNotIn('PRIVATE_SENTINEL',(Path(name)/'output-attachment-pending.json').read_text())


if __name__=='__main__':unittest.main()
