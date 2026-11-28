"""Synthetic files, SQLite journal and resolver only; never a real native call."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from agent_hub_host import resource_admission, zai_diagnostic_selector as selector

HOST = "2ce62436c565182b0c889e91d87c42d6fabf4ce1"
SID = "73c0d694-b7f4-5d3c-a407-647387c2f079"
OP = "bd605b97-8481-5164-a4e3-18a7f2d0ab65"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class Fixture:
    def __init__(self, root):
        self.root = root
        self.original = root / "original-ELF"
        self.artifact = root / "diagnostic-ELF"
        self.abi = root / "abi-fixture.json"
        for path, marker in ((self.original, b"ORIGINAL"), (self.artifact, b"DIAGTEST")):
            raw = bytearray(64)
            raw[:6] = b"\x7fELF\x02\x01"
            raw[16:20] = b"\x03\x00\x3e\x00"
            raw[40:48] = marker
            path.write_bytes(raw)
            path.chmod(0o700)
        self.abi.write_bytes(b"Synthetic ABI-record bytes; not actual qualification")
        self.abi.chmod(0o600)
        self.config = root / "config.toml"
        self.config.write_text('[engines.codex]\ncommand=["/unselected", "--engine-tail"]\n'
                               '[profiles.zcodex]\nengine="codex"\nexecutable='+json.dumps(str(self.original))+'\n'
                               'args=["--original-arg"]\nenv={ACCOUNT="SYNTHETIC_SECRET"}\n'
                               'limits={memory_bytes=1572864000,pids=256}\n')
        self.config.chmod(0o600)
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE launches(operation_id,session_id,epoch,workspace,state,native_id,native_dispatch_started,interactive)')
        db.execute('INSERT INTO launches VALUES(?,?,?,?,?,?,?,?)', (OP+":0",SID,7,str(root),'launching',None,0,0))
        self.launcher = types.SimpleNamespace(journal=types.SimpleNamespace(directory=root,db=db),
                                             native_binary="/synthetic/aplexer",runner=self.preview)
        self.spec = dict(session_id=SID,operation_id=OP+":0",epoch=7,workspace=str(root),
                         runtime_deadline=int(time.time()*1000)+240000,interactive=False,with_goal=False,
                         worker_context=dict(project_id="project",intake_id="intake"))
        self.route = dict(provider="zai",profile="zcodex")
        self.env = dict(APLEXER_CONFIG=str(self.config),HOME=str(root),PATH="/synthetic/host-path",
                        APLEXER_STATE_DIR="/synthetic/host-state",APLEXER_RUNTIME_DIR="/synthetic/host-run")
        self.child_env = dict(TMPDIR=str(root),APLEXER_RETAIN_RETIRED_OUTPUT="1")
        self.calls = []
        self.strip = []
        self.tamper = None
        info = self.abi.stat()
        self.d = dict(schema="hub2-zai-diagnostic-selector-v1",session_id=SID,operation_id=OP,
                      attempt_operation_id=OP+":0",project_id="project",intake_id="intake",start_key="fresh-key",
                      host_version=HOST,workspace=str(root),expires_at_ms=int(time.time()*1000)+60000,
                      config_sha256=digest(self.config.read_bytes()),
                      original_sdk=dict(sha256=digest(self.original.read_bytes()),bytes=64),
                      diagnostic_sdk=dict(path=str(self.artifact),sha256=digest(self.artifact.read_bytes()),bytes=64,
                                          source_commit="f"*40,abi_record=dict(path=str(self.abi),sha256=digest(self.abi.read_bytes()),
                                                                            device=info.st_dev,inode=info.st_ino)))
        self.descriptor = root / "zai-diagnostics" / (SID+".json")

    def publish(self):
        self.descriptor.parent.mkdir(mode=0o700,exist_ok=True)
        self.descriptor.write_text(json.dumps(self.d))
        self.descriptor.chmod(0o600)

    def preview(self, argv, **kwargs):
        import tomllib
        self.calls.append((argv,kwargs))
        self.assertions(argv)
        data = tomllib.loads(Path(kwargs['env']['APLEXER_CONFIG']).read_text())
        profile = data['profiles']['zcodex']
        result = dict(engine=profile['engine'],profile='zcodex',cwd=str(self.root),
                      argv=[profile['executable'],*data['engines']['codex']['command'][1:],*profile['args'],'--skip'],
                      env_set=profile['env'],env_unset=self.strip)
        if self.tamper:
            self.tamper(result,len(self.calls))
        return types.SimpleNamespace(returncode=0,stdout=json.dumps(result),stderr="")

    def assertions(self, argv):
        assert argv == ['/synthetic/aplexer','launch-spec','--profile','zcodex','--cwd',str(self.root),'--json']

    def prepare(self):
        return selector.prepare(self.launcher,self.spec,self.route,['a','task','run','--profile','zcodex'],
                                self.child_env,self.env,local=True)


class DiagnosticSelectorTests(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get('TMPDIR') or Path(__file__).resolve().parents[3] / '.local/tmp')
        previous = os.umask(0o077)
        try:
            parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            self.temp = tempfile.TemporaryDirectory(prefix='zai-selector-',dir=parent)
        finally:
            os.umask(previous)
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(Path(self.temp.name))
        self.addCleanup(self.f.launcher.journal.db.close)
        release = types.ModuleType('agent_hub_host._release')
        release.HOST_VERSION = HOST
        self.release = patch.dict(sys.modules,{'agent_hub_host._release':release})
        self.release.start()
        self.addCleanup(self.release.stop)

    def test_absence_is_no_config_or_resolver_effect(self):
        self.f.config.unlink()
        self.assertIsNone(self.f.prepare())
        self.assertEqual(self.f.calls,[])
        self.assertFalse(self.f.descriptor.parent.exists())

    def test_noneligible_paths_ignore_even_malformed_record(self):
        self.f.publish()
        self.f.descriptor.write_text('not JSON')
        for route,flags in ((dict(provider='claude',profile='zcodex'),{}),
                            (dict(provider='zai'),{}),(self.f.route,dict(interactive=True)),
                            (self.f.route,dict(with_goal=True)),(dict(provider='zai',profile='another'),{})):
            with self.subTest(route=route,flags=flags):
                spec=dict(self.f.spec,**flags)
                self.assertIsNone(selector.prepare(self.f.launcher,spec,route,[],{},self.f.env,local=True))
        self.assertEqual(self.f.calls,[])

    def test_joined_synthetic_files_resolver_journal_and_explicit_transport(self):
        self.f.publish()
        original=self.f.config.read_bytes()
        env=copy.deepcopy(self.f.env)
        plan=self.f.prepare()
        self.assertIsNotNone(plan)
        self.assertEqual(self.f.config.read_bytes(),original)
        self.assertEqual(self.f.env,env)
        self.assertEqual(len(self.f.calls),2)
        self.assertEqual({k:v for k,v in self.f.calls[0][1]['env'].items() if k!='APLEXER_CONFIG'},
                         {k:v for k,v in self.f.calls[1][1]['env'].items() if k!='APLEXER_CONFIG'})
        self.assertEqual(plan.private_config.stat().st_mode & 0o777,0o600)
        child=['a','task','run','--profile','zcodex']
        argv,selected=plan.transport(['a','start','--json','--',*child],child)
        self.assertEqual(selected,child+['--env','APLEXER_CONFIG='+str(plan.private_config),'--env',selector.FLAG+'=1'])
        self.assertEqual(argv,['a','start','--json','--env','APLEXER_CONFIG='+str(plan.private_config),
                               '--env',selector.FLAG+'=1','--',*selected])
        proof=plan.private_config.parent/'selection.json'
        metadata=json.loads(proof.read_bytes())
        self.assertEqual(metadata['operation_id'],OP+':0')
        self.assertEqual(metadata['epoch'],7)
        self.assertNotIn('SYNTHETIC_SECRET',proof.read_text())
        self.assertNotIn('env',metadata)
        self.assertNotIn('argv',metadata)
        self.assertEqual(self.f.launcher.journal.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0],0)
        plan.before_dispatch()
        self.assertTrue(plan.private_config.exists())

    def test_attempt_epoch_identity_host_and_expiry_refuse_before_resolver(self):
        for change in ('attempt','epoch','host','expiry','session'):
            with self.subTest(change=change):
                self.f.spec['operation_id']=OP+':0';self.f.spec['epoch']=7
                self.f.d['host_version']=HOST;self.f.d['expires_at_ms']=int(time.time()*1000)+60000
                self.f.d['session_id']=SID
                if change=='attempt':self.f.spec['operation_id']=OP+':1'
                if change=='epoch':self.f.spec['epoch']=8
                if change=='host':self.f.d['host_version']='0'*40
                if change=='expiry':self.f.d['expires_at_ms']=0
                if change=='session':self.f.d['session_id']='0'*36
                self.f.publish()
                with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()
        self.assertEqual(self.f.calls,[])

    def test_command_wrapper_is_not_bypassed(self):
        self.f.config.write_text('[profiles.zcodex]\nengine="codex"\ncommand=["/trusted/wrapper"]\n')
        self.f.config.chmod(0o600);self.f.d['config_sha256']=digest(self.f.config.read_bytes());self.f.publish()
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()
        self.assertEqual(self.f.calls,[])

    def test_strip_wins_refuses_and_never_spawns(self):
        self.f.publish();self.f.strip=[selector.FLAG]
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()
        self.assertEqual(len(self.f.calls),1)

    def test_preview_tail_or_environment_drift_is_refused(self):
        self.f.publish()
        def drift(value,number):
            if number==2:value['env_set']['ACCOUNT']='CHANGED'
        self.f.tamper=drift
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()
        self.assertEqual(len(self.f.calls),2)

    def test_terminal_elf_hash_not_basename_is_authority(self):
        self.f.publish();self.f.original.write_bytes(b'not an ELF'+b'x'*54)
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()
        self.assertEqual(len(self.f.calls),1)

    def test_actual_epoch_race_recheck_refuses_and_retains_private_files(self):
        self.f.publish()
        def race(value,number):
            if number==2:self.f.launcher.journal.db.execute('UPDATE launches SET epoch=8')
        self.f.tamper=race
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()
        files=list((self.f.descriptor.parent/'attempts').glob('*/config.toml'))
        self.assertEqual(len(files),1)

    def test_final_identity_and_journal_rechecks_hold_unknown_files(self):
        self.f.publish();plan=self.f.prepare()
        self.f.launcher.journal.db.execute('UPDATE launches SET native_dispatch_started=1')
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):plan.before_dispatch()
        self.assertTrue(plan.private_config.exists())

    def test_preview_timeout_is_fixed_refusal_without_raw_command(self):
        self.f.publish()
        def timeout(*args,**kwargs):raise subprocess.TimeoutExpired('PRIVATE_SENTINEL',3)
        self.f.launcher.runner=timeout
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()

    def test_duplicate_descriptor_keys_and_record_identity_are_refused(self):
        self.f.publish();self.f.descriptor.write_text('{"schema":1,"schema":2}')
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()
        self.f.d['diagnostic_sdk']['abi_record']['inode']+=1;self.f.publish()
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):self.f.prepare()
        self.assertEqual(self.f.calls,[])

    def test_server_uuid_vectors_and_no_automatic_later_attempt(self):
        self.assertEqual(selector._uuid('hub2-start-session-v1','project','intake','fresh-key'),SID)
        self.assertEqual(selector._uuid('hub2-start-operation-v1','project','intake','fresh-key'),OP)


import tomllib
from agent_hub_host.zai_profile_mutator import (
    MAX_CANDIDATES, MAX_CONFIG_BYTES, SelectorRefused, select_profile_executable,
)


class SameProfileSelectorTests(unittest.TestCase):
    def assert_mutation(self, source, profile="target", path="/owned/release/zcodex"):
        before = tomllib.loads(source.decode())
        expected = copy.deepcopy(before)
        expected["profiles"][profile]["executable"] = path
        output = select_profile_executable(source, profile, path)
        self.assertEqual(tomllib.loads(output.decode()), expected)
        return output

    def assert_refused(self, source, profile="target", path="/owned/release/zcodex", code=None):
        with self.assertRaises(SelectorRefused) as raised:
            select_profile_executable(source, profile, path)
        if code:
            self.assertEqual(str(raised.exception), code)
        return str(raised.exception)

    def test_existing_executable_preserves_all_values_and_text_outside_literal(self):
        source = b"""version = 1
[engines.zai]
command = ["zcodex", "--engine-default"]
env = { ACCOUNT = "fixture-only", OPTION = "retain" }
[profiles.target]
engine = "zai"
executable = '/old/sdk' # retain this comment
args = ["--account", "fixture-account"]
cwd = "/fixture/workspace"
history_bytes = 1234
env = { CODEX_HOME = "/fixture/home", RUST_LOG = "error" }
limits = { memory_bytes = 1572864000, pids = 256 }
[profiles.other]
engine = "codex"
executable = "/other/sdk"
"""
        output = self.assert_mutation(source)
        self.assertEqual(output, source.replace(b"'/old/sdk'", b'"/owned/release/zcodex"', 1))

    def test_quoted_and_dotted_table_names_keep_same_profile(self):
        for header in (b'[profiles."target.with.dots"]', b'["profiles"."target.with.dots"]',
                       b"[profiles.'target.with.dots']"):
            with self.subTest(header=header):
                source = header + b'\nengine="zai"\nexecutable="/old/sdk"\n'
                self.assert_mutation(source, "target.with.dots")

    def test_missing_field_insertion_preserves_original_text_crlf_and_other_fields(self):
        source = b'[profiles."target"] # comment\r\nengine="zai"\r\nargs=["--fixture"]\r\n'
        output = self.assert_mutation(source)
        inserted = b'executable = "/owned/release/zcodex"\r\n'
        self.assertEqual(output.replace(inserted, b"", 1), source)

    def test_missing_field_eof_header_is_supported(self):
        source = b'[profiles.target]'
        output = self.assert_mutation(source)
        self.assertEqual(output, source + b'\nexecutable = "/owned/release/zcodex"\n')

    def test_command_wrapper_profiles_always_refuse_even_with_executable(self):
        for fields in (b'command=["/trusted/wrapper", "--original"]',
                       b'command=["/trusted/wrapper"]\nexecutable="/old/sdk"'):
            with self.subTest(fields=fields):
                self.assert_refused(b'[profiles.target]\n' + fields + b'\n', code="command_profile")

    def test_multiline_header_and_assignment_lookalikes_cannot_change_unrelated_value(self):
        source = b'''note = """retained
[profiles.target]
executable = "/old/sdk"
"""
[profiles.target]
engine="zai"
executable="/old/sdk"
'''
        output = self.assert_mutation(source)
        self.assertEqual(output, source.rsplit(b'executable="/old/sdk"', 1)[0] + b'executable="/owned/release/zcodex"\n')
        self.assertEqual(tomllib.loads(output.decode())["note"], tomllib.loads(source.decode())["note"])

    def test_inline_target_and_multiline_executable_refuse_without_wrong_edit(self):
        inline = b'profiles = { target = { engine="zai", executable="/old/sdk" } }\n'
        self.assert_refused(inline, code="candidate_bounds")
        multiline = b'[profiles.target]\nexecutable="""/old/sdk"""\n'
        self.assert_refused(multiline, code="candidate_bounds")

    def test_inline_lookalikes_remain_identical_while_real_field_changes(self):
        source = b'metadata={ executable="/old/sdk", header="[profiles.target]" }\n[profiles.target]\nexecutable="/old/sdk"\n'
        output = self.assert_mutation(source)
        self.assertEqual(output.splitlines()[0], source.splitlines()[0])

    def test_multiple_lexical_candidates_select_only_semantically_exact_target(self):
        source = b'[profiles.other]\nexecutable="/old/sdk"\n[profiles.target]\nexecutable="/old/sdk"\n[profiles.last]\nexecutable="/old/sdk"\n'
        output = self.assert_mutation(source)
        self.assertEqual(output.count(b'/old/sdk'), 2)
        self.assertEqual(output.count(b'/owned/release/zcodex'), 1)
        self.assertEqual(tomllib.loads(output.decode())["profiles"]["other"], {"executable": "/old/sdk"})

    def test_duplicate_or_malformed_config_refuses_closed_without_parser_text(self):
        for source in (b'[profiles.target]\nexecutable="/old/sdk"\nexecutable="PRIVATE_SENTINEL"\n',
                       b'[profiles.target\n', b'\xff'):
            with self.subTest(source_length=len(source)):
                reason = self.assert_refused(source, code="invalid_toml")
                self.assertNotIn("PRIVATE_SENTINEL", reason)

    def test_missing_or_non_table_profile_and_non_string_executable_refuse(self):
        self.assert_refused(b'[profiles.other]\nengine="zai"\n', code="profile_missing")
        self.assert_refused(b'profiles={target="not a profile"}\n', code="profile_missing")
        self.assert_refused(b'[profiles.target]\nexecutable=4\n', code="unsupported_executable")

    def test_bounds_and_relative_control_or_surrogate_inputs_refuse(self):
        source = b'[profiles.target]\nexecutable="/old/sdk"\n'
        self.assert_refused(source + b'#' * MAX_CONFIG_BYTES, code="config_bounds")
        self.assert_refused(source, path="relative/sdk", code="sdk_path")
        self.assert_refused(source, path="/path\nPRIVATE_SENTINEL", code="sdk_path")
        self.assert_refused(source, path="/path/" + chr(0xD800), code="sdk_path")
        self.assert_refused(source, profile="x"*257, code="profile_id")
        self.assert_refused(source, path="/" + "x"*4096, code="sdk_path")
        huge = b'[profiles.target]\nexecutable="/old/sdk"\n' + b''.join(
            ('[profiles.p%d]\nexecutable="/old/sdk"\n' % i).encode() for i in range(MAX_CANDIDATES))
        self.assert_refused(huge, code="candidate_bounds")

    def test_parser_resource_styles_refuse_closed_without_input_echo(self):
        prefix = b'[profiles.target]\nexecutable="/old/sdk"\n'
        for suffix in (b'value=' + b'['*2000 + b'0' + b']'*2000,
                       b'value=' + b'9'*10000):
            with self.subTest(length=len(suffix)):
                self.assert_refused(prefix + suffix, code="invalid_toml")

    def test_new_path_escaping_is_toml_safe_and_noop_refuses(self):
        source = b'[profiles.target]\nexecutable="/old/sdk"\n'
        self.assert_mutation(source, path='/owned/a"b\\c#[]/zcodex')
        self.assert_refused(source, path="/old/sdk", code="no_change")

    def test_unrelated_toml_dates_arrays_booleans_and_nan_are_preserved(self):
        source = b'when=2026-11-17T03:00:00Z\nvalues=[true, 1, 1.5]\nfloat=nan\n[profiles.target]\nexecutable="/old/sdk"\n'
        output = select_profile_executable(source, "target", "/owned/release/zcodex")
        self.assertEqual(output, source.replace(b'"/old/sdk"', b'"/owned/release/zcodex"'))


class _Clock:
    def __init__(self):
        self.wall = time.time()
        self.mono = 100.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class ActualLaunchDeadlineTests(unittest.TestCase):
    setUp = DiagnosticSelectorTests.setUp

    def invoke_actual_launch(self, callback, *, publish=True, late_sql=False):
        import ast
        import re
        if publish:
            self.f.publish()
        clock = _Clock()
        db = self.f.launcher.journal.db
        self.start_calls = []
        self.callback_calls = []
        self.journal_reads = []
        if late_sql:
            outer = self
            class Database:
                def execute(self, query, args=()):
                    cursor = db.execute(query, args)
                    if query.startswith('SELECT session_id,epoch,workspace,state'):
                        outer.journal_reads.append(query)
                        number = len(outer.journal_reads)
                        class Cursor:
                            def fetchone(self):
                                row = cursor.fetchone()
                                if number == 5:
                                    clock.advance(11)
                                return row
                        return Cursor()
                    return cursor
            self.f.launcher.journal.db = Database()
        launcher = self.f.launcher
        launcher.config = dict(routes={'zai':self.f.route},memory_mb=1500,pids=256)
        launcher.profile_command = lambda *args: [launcher.native_binary,'task','run','--profile','zcodex']
        launcher.record_evidence = lambda *args: None
        original_preview = self.f.preview
        def runner(argv, **kwargs):
            if argv[1] == 'launch-spec':
                return original_preview(argv,**kwargs)
            self.start_calls.append(argv)
            self.assertEqual(argv[1],'start')
            result = dict(id='11111111-1111-4111-8111-111111111111',
                          tag=argv[argv.index('--tag')+1],workspace=str(self.f.root),
                          command=argv[argv.index('--')+1:])
            return types.SimpleNamespace(returncode=0,stdout=json.dumps(result),stderr='')
        launcher.runner = runner
        spec = dict(self.f.spec,engine='zai',model='fixture-model',prompt='synthetic input',timeout_seconds=240)
        def before_native_dispatch():
            self.callback_calls.append(True)
            with db:
                db.execute('UPDATE launches SET native_dispatch_started=1')
            callback(clock)
        spec['before_native_dispatch'] = before_native_dispatch
        # Compile the ACTUAL candidate method AST with declared fake globals,
        # never a duplicate dispatch implementation or a real native module import.
        # resource_admission is the real policy module: every call the launch
        # body makes on this path is pure cap validation or a no-op None-permit
        # check, so the actual admission seam runs without any stub.
        source = Path(selector.__file__).resolve().with_name('native_launch.py')
        tree = ast.parse(source.read_bytes())
        cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='NativeLauncher')
        method = next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='launch')
        namespace = dict(__name__='agent_hub_host.native_launch',__package__='agent_hub_host',
                         Path=Path,time=clock,os=os,sys=sys,json=json,subprocess=subprocess,
                         PROVIDER_CAPABILITIES={'zai':{}},FIRST_ACTION_PROVIDERS=set(),
                         aplexer_prompt=lambda text:text,private_file=lambda path:None,
                         resolve_windows_program=lambda program:program,
                         spawn_environment=lambda central:dict(self.f.env),
                         UUID_RE=re.compile(r'^[0-9a-f-]{36}$'),
                         same_workspace=lambda left,right:left==right,
                         validate_first_action=lambda *args:False,
                         resource_admission=resource_admission)
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),namespace)
        with patch.object(selector,'time',clock):
            return namespace['launch'](launcher,'hub2-'+SID,spec)

    def test_actual_callback_expiry_preserves_marker_and_skips_runner(self):
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):
            self.invoke_actual_launch(lambda clock:clock.advance(11))
        self.assertEqual(self.start_calls,[])
        self.assertEqual(self.callback_calls,[True])
        self.assertEqual(self.f.launcher.journal.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0],1)
        self.assertEqual(len(list((self.f.descriptor.parent/'attempts').glob('*/config.toml'))),1)

    def test_actual_final_sqlite_read_expiry_refuses_before_callback(self):
        with self.assertRaisesRegex(RuntimeError,'^zai_diagnostic_selector_refused$'):
            self.invoke_actual_launch(lambda clock:None,late_sql=True)
        self.assertEqual(len(self.journal_reads),5)
        self.assertEqual(self.callback_calls,[])
        self.assertEqual(self.start_calls,[])
        self.assertEqual(self.f.launcher.journal.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0],0)
        self.assertEqual(len(list((self.f.descriptor.parent/'attempts').glob('*/config.toml'))),1)

    def test_actual_timely_marker_one_still_runs_without_post_callback_tuple_recheck(self):
        result = self.invoke_actual_launch(lambda clock:None)
        self.assertEqual(len(self.start_calls),1)
        self.assertEqual(self.callback_calls,[True])
        self.assertEqual(self.f.launcher.journal.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0],1)
        self.assertEqual(result['session_id'],'11111111-1111-4111-8111-111111111111')

    def test_actual_absent_descriptor_keeps_ordinary_callback_and_runner(self):
        result = self.invoke_actual_launch(lambda clock:clock.advance(11),publish=False)
        self.assertEqual(len(self.start_calls),1)
        self.assertEqual(self.f.calls,[])
        self.assertEqual(result['session_id'],'11111111-1111-4111-8111-111111111111')


if __name__=='__main__':
    unittest.main()
