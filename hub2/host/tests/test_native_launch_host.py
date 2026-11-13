"""Native launch adapter and provider home tests."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host import native_launch as nl
from agent_hub_host import provider_home as ph


class AdapterArgvTest(unittest.TestCase):
    def test_zai_route_pins_harness_flags_and_model(self):
        argv = nl.adapter_argv('zai', 'goal-text', 'glm-5.3-flash')
        self.assertEqual(argv[0], 'zcodex')
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', argv)
        self.assertIn('--model', argv)
        self.assertEqual(argv[argv.index('--model') + 1], 'glm-5.3-flash')
        self.assertIn('--disable', argv)
        self.assertEqual(argv[-1], 'goal-text')

    def test_go_route_pins_approved_subscription_model(self):
        argv = nl.adapter_argv('go', 'goal-text', None)
        self.assertEqual(argv[0], 'opencode')
        self.assertIn(nl.OPENCODE_MODEL, argv)
        with self.assertRaises(ValueError):
            nl.adapter_argv('go', 'goal-text', 'other/model')

    def test_glm_model_never_leaves_zai_route(self):
        with self.assertRaises(ValueError):
            nl.adapter_argv('codex', 'goal', 'glm-5.3-flash')
        with self.assertRaises(ValueError):
            nl.adapter_argv('go', 'goal', 'glm-5.3-flash')

    def test_claude_headless_shape(self):
        argv = nl.adapter_argv('claude', 'goal-text', 'sonnet')
        self.assertEqual(argv[0], 'claude')
        self.assertIn('--dangerously-skip-permissions', argv)
        self.assertIn('-p', argv)


class EffortAndInteractiveTest(unittest.TestCase):
    def test_aplexer_prompt_flattens_windows_line_breaks_only(self):
        prompt = 'First line\r\n\r\nSecond line\nThird line'
        self.assertEqual(nl.aplexer_prompt(prompt, windows=True), 'First line Second line Third line')
        self.assertEqual(nl.aplexer_prompt(prompt, windows=False), prompt)

    def test_command_effort_accepts_aplexer_normalized_config(self):
        self.assertEqual(nl.command_effort(['codex', 'exec', '-c', 'model_reasoning_effort=medium']), 'medium')

    def test_command_effort_keeps_json_and_rejects_unknown_bare_values(self):
        self.assertEqual(nl.command_effort(['codex', 'exec', '-c', 'model_reasoning_effort="medium"']), 'medium')
        self.assertIsNone(nl.command_effort(['codex', 'exec', '-c', 'model_reasoning_effort=unverified']))

    def test_headless_effort_codex_maps_max_to_xhigh(self):
        base = nl.adapter_argv('zai', 'goal', 'glm-5.3-flash')
        argv = nl.headless_effort_argv('zai', base, 'max')
        self.assertIn('model_reasoning_effort="xhigh"', argv)
        self.assertEqual(argv[-1], 'goal')

    def test_headless_effort_claude(self):
        base = nl.adapter_argv('claude', 'goal', 'sonnet')
        argv = nl.headless_effort_argv('claude', base, 'high')
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')

    def test_interactive_removes_exec_and_keeps_model(self):
        base = nl.adapter_argv('zai', 'goal', 'glm-5.3-flash')
        argv = nl.interactive_argv('zai', base, 'goal', model='glm-5.3-flash', effort='medium')
        self.assertNotIn('exec', argv)
        self.assertNotIn('--json', argv)
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertEqual(argv[-1], 'goal')

    def test_goal_removes_prompt_from_argv(self):
        base = nl.adapter_argv('zai', 'goal', 'glm-5.3-flash')
        argv = nl.interactive_argv('zai', base, 'goal', model='glm-5.3-flash', with_goal=True)
        self.assertNotIn('goal', argv)

    def test_goal_requires_goal_capable_provider(self):
        base = nl.adapter_argv('go', 'goal', nl.OPENCODE_MODEL)
        with self.assertRaises(ValueError):
            nl.interactive_argv('go', base, 'goal', model=nl.OPENCODE_MODEL, with_goal=True)


class GoalNativeEngineTest(unittest.TestCase):
    def temporary_workspace(self):
        directory = Path(__file__).resolve().parents[3] / '.local/tmp'
        directory.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=directory, prefix='native-goal-engine-')

    def goal_launch(self, provider, *, returned_engine=None, conflicting_proof=False):
        with self.temporary_workspace() as workspace:
            task_id = 'goal-native-engine'
            created = int(time.time() * 1000)
            captured = {}
            native_id = 'd6cf9872-8597-48a5-be64-990cdef4db31'
            model = 'glm-5.3-flash' if provider == 'zai' else 'gpt-6.1-sol'
            spec = {'workspace': workspace, 'engine': 'codex', 'model': model,
                    'prompt': 'complete the exact scoped objective', 'interactive': True,
                    'with_goal': True, 'effort': 'medium', 'timeout_seconds': 600,
                    'provider_home': '/synthetic/private-provider', 'client': mock.Mock(),
                    'session_id': 'session-goal', 'operation_id': 'operation-goal', 'epoch': 1}
            prompt = nl.aplexer_prompt(spec['prompt'] + nl.FIRST_ACTION_INSTRUCTION.format(
                path=Path(workspace) / '.local' / ('first-action-' + task_id + '.json')))
            expected_child = nl.interactive_argv(provider, nl.adapter_argv(provider, prompt, model), prompt,
                                                 model=model, effort='medium', with_goal=True)
            if provider == 'codex':
                expected_child[0] = '/synthetic/launch-codex.sh'
            row = {}

            def runner(argv, **options):
                captured.update(argv=argv, options=options)
                # Match aplexer's actual contract: explicit argv does not imply
                # its engine; engine defaults to shell without --engine/profile.
                prefix = argv[:argv.index('--')]
                engine = prefix[prefix.index('--engine') + 1] if '--engine' in prefix else 'shell'
                child = argv[argv.index('--') + 1:]
                row.update(id=native_id, tag='task-' + task_id, workspace=workspace,
                           engine=returned_engine or engine, command=child,
                           created_at_ms=created, phase='running')
                return subprocess.CompletedProcess(argv, 0, json.dumps(row), '')

            adapter = nl.NativeLauncher({'private_provider_homes': True,
                                         'routes': {'codex': {'provider': provider}}}, runner=runner)
            proof = {'thread_id': 'synthetic-exact-thread'}
            original_goal_prompt = nl.goal_prompt

            def make_goal(*args):
                goal = original_goal_prompt(*args)
                captured['objective'] = goal
                proof['objective_hash'] = nl.objective_hash('conflicting goal' if conflicting_proof else goal)
                return goal
            delivery = {'id': native_id, 'delivery': {'engine': 'codex', 'ready': True,
                        'landed': True, 'submitted': True, 'status': 'submitted', 'sends': 1, 'enter_presses': 1}}
            with mock.patch.object(nl, 'goal_prompt', side_effect=make_goal), \
                 mock.patch.object(nl, 'session_binding', return_value={
                    'runtime_deadline': int(time.time() * 1000) + 500000}), \
                 mock.patch.object(nl, 'native_environment', return_value={'CODEX_HOME': '/synthetic/trusted-codex'}), \
                 mock.patch.object(adapter, 'status', side_effect=lambda *args, **kw: dict(row)), \
                 mock.patch.object(nl, 'goal_store', side_effect=[None, proof]) as store, \
                 mock.patch.object(adapter, 'native', return_value=subprocess.CompletedProcess([], 0, json.dumps(delivery), '')) as send, \
                 mock.patch.object(nl, 'codex_launch_wrapper', return_value=Path('/synthetic/launch-codex.sh')), \
                 mock.patch.object(adapter, 'record_evidence'):
                receipt = adapter.launch(task_id, spec)
            expected_goal = captured['objective']
            self.assertEqual(receipt['goal_registration'], proof)
            self.assertEqual(captured['argv'][captured['argv'].index('--') + 1:], expected_child)
            self.assertNotIn(expected_goal, expected_child, 'goal must use guarded send, not CLI prompt')
            self.assertIn('CODEX_HOME=/synthetic/trusted-codex', captured['argv'])
            self.assertNotIn('--profile', captured['argv'][:captured['argv'].index('--')])
            self.assertEqual(send.call_args.args[:3], ('send', native_id, '/goal ' + expected_goal))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(store.call_count, 2)
            return captured

    def test_codex_and_zai_goals_pin_native_codex_family_without_rewriting_child(self):
        for provider in ('codex', 'zai'):
            with self.subTest(provider=provider):
                captured = self.goal_launch(provider)
                prefix = captured['argv'][:captured['argv'].index('--')]
                self.assertEqual(prefix[prefix.index('--engine') + 1], 'codex')

    def test_shell_receipt_is_still_rejected_for_goal_registration(self):
        with self.assertRaisesRegex(RuntimeError, '^native_goal_provider_unsupported$'):
            self.goal_launch('codex', returned_engine='shell')

    def test_wrong_objective_proof_is_still_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '^native_goal_objective_conflict$'):
            self.goal_launch('codex', conflicting_proof=True)



class ClaudeGoalConfigEnvironmentTests(unittest.TestCase):
    # Exact metadata-only resolver extracted from native Claude2.1.322 (Qt).
    NATIVE_RESOLVER = 'function Qt(e){return{globalConfig:nt(e||Bt(),".claude.json"),userSettings:nt(e||nt(Bt(),".claude"),"settings.json")}}'
    NODE_METADATA_TIMEOUT_SECONDS = 20

    def setUp(self):
        root=Path(__file__).resolve().parents[3]/'.local/tmp'
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root,prefix='claude-config-root-')
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)/'home';self.home.mkdir(mode=0o700)
        self.workspace=Path(self.temp.name)/'workspace';self.workspace.mkdir(mode=0o700)
        self.launch_count=0
        (self.home/'.claude').mkdir(mode=0o700)
        (self.home/'.claude.json').write_text(json.dumps({'hasCompletedOnboarding':True}))
        (self.home/'.claude/.claude.json').write_text(json.dumps({'hasCompletedOnboarding':False}))

    def launch(self, inherited=None, private=None):
        self.launch_count+=1
        task_id='claude-config-root-'+str(self.launch_count)
        captured={}
        def runner(argv,**options):
            child=argv[argv.index('--')+1:]
            row={'id':'d6cf9872-8597-48a5-be64-990cdef4db31','tag':'task-'+task_id,
                 'workspace':str(self.workspace),'engine':'claude','command':child,
                 'created_at_ms':int(time.time()*1000),'phase':'running'}
            captured.update(argv=argv,options=options,row=row)
            return subprocess.CompletedProcess(argv,0,json.dumps(row),'')
        adapter=nl.NativeLauncher({'routes':{'claude':{'provider':'claude'}}},runner=runner,aplexer='synthetic-a')
        spec={'workspace':str(self.workspace),'engine':'claude','model':'sonnet','prompt':'synthetic goal',
              'interactive':True,'with_goal':True,'timeout_seconds':600,
              'session_id':'synthetic-session','operation_id':'synthetic-operation','epoch':1}
        if private:spec.update(provider_home='/synthetic/private-manifest',client=mock.Mock())
        binding={'runtime_deadline':int(time.time()*1000)+500000,'engine_binary_path':'/synthetic/private/claude'}
        with mock.patch.dict(os.environ,{'HOME':str(self.home),'USERPROFILE':str(self.home)}), \
                mock.patch.object(Path,'home',return_value=self.home), \
                mock.patch.object(nl,'session_binding',return_value=binding), \
                mock.patch.object(nl,'native_environment',return_value=private or {}), \
                mock.patch.object(adapter,'register_goal',return_value={'status':'active'}) as register:
            os.environ.pop('CLAUDE_CONFIG_DIR',None)
            if inherited is not None:os.environ['CLAUDE_CONFIG_DIR']=inherited
            adapter.launch(task_id,spec)
        args=captured['argv'];prefix=args[:args.index('--')]
        pairs=dict(prefix[i+1].split('=',1) for i,arg in enumerate(prefix[:-1]) if arg=='--env')
        captured.update(child_env=pairs,reader_root=register.call_args.kwargs['claude_config_dir'])
        captured['effective_env']={**captured['options']['env'],**pairs}  # Maintained builtin engine env is empty.
        self.assertNotIn('--env-unset',prefix)  # Not an aplexer start option.
        return captured

    def resolve_native_metadata(self, config_dir):
        # Runs only the extracted pure resolver against owned synthetic metadata;
        # no native provider CLI, login, auth file or real configuration is read.
        script=('const nt=require("node:path").join;const Bt=()=>process.argv[1];'+self.NATIVE_RESOLVER+
                ';const paths=Qt(process.argv[2]||undefined);let onboarding=false;'
                'try{onboarding=JSON.parse(require("node:fs").readFileSync(paths.globalConfig,"utf8")).hasCompletedOnboarding===true}'
                'catch(e){if(e.code!=="ENOENT")throw e}process.stdout.write(JSON.stringify({...paths,onboarding}));')
        # A cold Node start can exceed five seconds on a busy CI runner. Keep a
        # finite fixture-only budget, with captured output in any timeout failure.
        try:
            done=subprocess.run(['node','-e',script,str(self.home),config_dir or ''],capture_output=True,text=True,
                                check=True,timeout=self.NODE_METADATA_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            self.fail(f'Node metadata resolver exceeded {error.timeout}s; stdout={error.stdout!r}; stderr={error.stderr!r}')
        return json.loads(done.stdout)

    def test_absent_and_empty_local_values_preserve_native_onboarding_default(self):
        explicit_default=self.resolve_native_metadata(str(self.home/'.claude'))
        self.assertFalse(explicit_default['onboarding'])
        self.assertEqual(explicit_default['globalConfig'],str(self.home/'.claude/.claude.json'))
        for inherited in (None,''):
            with self.subTest(inherited=inherited):
                captured=self.launch(inherited)
                self.assertNotIn('CLAUDE_CONFIG_DIR',captured['child_env'])
                self.assertNotIn('CLAUDE_CONFIG_DIR',captured['options']['env'])
                metadata=self.resolve_native_metadata(captured['effective_env'].get('CLAUDE_CONFIG_DIR'))
                self.assertTrue(metadata['onboarding'])
                self.assertEqual(metadata['globalConfig'],str(self.home/'.claude.json'))
                self.assertEqual(metadata['userSettings'],str(self.home/'.claude/settings.json'))
                self.assertEqual(captured['reader_root'],str(self.home/'.claude'))

    def test_nonempty_explicit_and_private_home_overrides_pin_same_child_and_reader(self):
        explicit=str(self.home/'explicit override')
        trusted=str(self.home/'trusted private/.claude')
        for inherited,private,expected in [(explicit,None,explicit),
                (explicit,{'HOME':str(self.home/'trusted private'),'CLAUDE_CONFIG_DIR':trusted},trusted)]:
            with self.subTest(private=private):
                captured=self.launch(inherited,private)
                self.assertEqual(captured['child_env']['CLAUDE_CONFIG_DIR'],expected)
                self.assertEqual(captured['effective_env']['CLAUDE_CONFIG_DIR'],expected)
                self.assertEqual(captured['reader_root'],expected)
                if private:self.assertEqual(captured['child_env']['HOME'],private['HOME'])
                metadata=self.resolve_native_metadata(expected)
                self.assertEqual(metadata['globalConfig'],str(Path(expected)/'.claude.json'))
                self.assertEqual(metadata['userSettings'],str(Path(expected)/'settings.json'))

    def test_unresolved_custom_engine_root_does_not_prove_a_goal_at_default_reader(self):
        from datetime import datetime,timezone
        import re
        captured=self.launch()
        row=captured['row'];thread=nl.claude_goal.conversation_id(row['command'])
        injected=self.home/'custom-engine-config'
        path=injected/'projects'/re.sub(r'[^A-Za-z0-9]','-',row['workspace'])/(thread+'.jsonl')
        path.parent.mkdir(parents=True)
        event={'type':'attachment','isSidechain':False,'sessionId':thread,'cwd':row['workspace'],
               'timestamp':datetime.now(timezone.utc).isoformat(),
               'attachment':{'type':'goal_status','met':False,'sentinel':True,'condition':'synthetic goal'}}
        path.write_text(json.dumps(event)+'\n')
        self.assertEqual(nl.claude_goal.goal_store(row,injected,thread,nl.objective_hash)['status'],'active')
        self.assertIsNone(nl.claude_goal.goal_store(row,captured['reader_root'],thread,nl.objective_hash))


class FirstActionTest(unittest.TestCase):
    def start_record(self, tmp):
        return {'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-t1',
                'workspace': str(tmp), 'command': ['zcodex', 'exec', '--model', 'glm-5.3-flash'],
                'phase': 'starting', 'schema_version': 1, 'created_at_ms': int(time.time() * 1000) - 1000}

    def whoami(self, tmp, **overrides):
        record = self.start_record(tmp)
        whoami = {'id': record['id'], 'tag': record['tag'], 'workspace': record['workspace'],
                  'command': record['command'], 'phase': 'working', 'schema_version': 2,
                  'worker_pid': os.getpid() + 1, 'updated_at_ms': record['created_at_ms'] + 500,
                  'boot_id': 'x'}
        whoami.update(overrides)
        return record, whoami

    def test_genuine_whoami_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, whoami = self.whoami(tmp)
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps(whoami))
            os.utime(path, (time.time(), time.time()))
            self.assertTrue(nl.validate_first_action(str(path), record, time.time() - 1))

    def test_start_record_clone_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, _ = self.whoami(tmp)
            clone = dict(record, phase='working', worker_pid=os.getpid() + 1, extra='x')
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps(clone))
            os.utime(path, (time.time(), time.time()))
            self.assertFalse(nl.validate_first_action(str(path), record, time.time() - 1))

    def test_identity_and_phase_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, whoami = self.whoami(tmp)
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps(whoami | {'id': 'other-id'}))
            os.utime(path, (time.time(), time.time()))
            self.assertFalse(nl.validate_first_action(str(path), record, time.time() - 1))
            path.write_text(json.dumps(whoami | {'phase': 'exited'}))
            self.assertFalse(nl.validate_first_action(str(path), record, time.time() - 1))

    def test_missing_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, whoami = self.whoami(tmp)
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps({k: v for k, v in whoami.items() if k != 'worker_pid'}))
            os.utime(path, (time.time(), time.time()))
            self.assertFalse(nl.validate_first_action(str(path), record, time.time() - 1))


class RetirementOriginTest(unittest.TestCase):
    def evidence(self, tmp):
        sid = '11111111-2222-4333-8444-555555555555'
        nid = '22222222-3333-4444-8555-666666666666'
        workspace = str(Path(tmp)/'original')
        expected = {'session_id': sid, 'native_id': nid, 'operation_id': 'operation:0', 'epoch': 231,
                    'digest': 'a'*64, 'tag': 'task-hub2-'+sid, 'workspace': workspace}
        manifest = Path(workspace)/'.local'/('launch-hub2-'+sid+'.json')
        manifest.parent.mkdir(parents=True, mode=0o700)
        state = Path(tmp)/'native'
        tombstone = state/'retired-sessions'/nid/'tombstone.json'
        tombstone.parent.mkdir(parents=True, mode=0o700)
        now = int(time.time()*1000)
        launch = {'task_id': 'hub2-'+sid, 'session_id': nid, 'tag': expected['tag'], 'workspace': workspace,
                  'created_at_ms': now-2000, 'native_started_at': (now-1000)/1000}
        retired = {'id': nid, 'tag': expected['tag'], 'workspace': str(Path(tmp)/'moved'), 'created_at_ms': now-2000,
                   'cause': 'finished', 'containment_empty': True, 'exit': {'code': 127, 'signal': None, 'exited_at_ms': now},
                   'finished_at_ms': now}
        for path, value in ((manifest, launch), (tombstone, retired)):
            path.write_text(json.dumps(value)); path.chmod(0o600)
        adapter = nl.NativeLauncher({'aplexer_state_dir': str(state)})
        return adapter, expected, manifest, tombstone, launch, retired

    def test_legacy_origin_requires_exact_birth_positive_exit_and_preserves_live_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, expected, manifest, tombstone, launch, retired = self.evidence(tmp)
            self.assertIsNone(adapter.retired(expected['native_id'], expected))
            value = adapter.retired_origin(expected['native_id'], expected)
            self.assertEqual(value['exit']['code'], 127)
            self.assertEqual(value['workspace'], expected['workspace'])
            self.assertEqual(value['retirement_origin']['observed_workspace'], retired['workspace'])
            self.assertNotIn('first_action', value)
            # A legacy manifest has no fabricated direct attempt fields.
            self.assertNotIn('epoch', launch)

    def test_invalid_retirement_or_launch_never_qualifies(self):
        changes = [('retired', {'created_at_ms': 1}), ('retired', {'tag': 'other'}),
            ('retired', {'id': '00000000-0000-4000-8000-000000000000'}),
            ('retired', {'containment_empty': False}), ('retired', {'cause': 'forgotten'}),
            ('retired', {'exit': {}}), ('retired', {'finished_at_ms': 1}),
            ('retired', {'finished_at_ms': int(time.time()*1000)+60000}),
            ('retired', {'created_at_ms': True}), ('launch', {'workspace': '/foreign'}),
            ('launch', {'session_id': 'other'}), ('launch', {'tag': 'other'}),
            ('launch', {'native_started_at': float('nan')}), ('launch', {'created_at_ms': None}),
            ('launch', {'operation_id': 'other'}), ('launch', {'epoch': 230}),
            ('launch', {'epoch': 232}), ('launch', {'digest': 'b'*64})]
        for target, patch in changes:
            with self.subTest(target=target, patch=patch), tempfile.TemporaryDirectory() as tmp:
                adapter, expected, manifest, tombstone, launch, retired = self.evidence(tmp)
                value = launch if target == 'launch' else retired
                value.update(patch)
                (manifest if target == 'launch' else tombstone).write_text(json.dumps(value))
                self.assertIsNone(adapter.retired_origin(expected['native_id'], expected))

    def test_invalid_expected_attempt_and_foreign_owner_are_unknown(self):
        for field, value in [('session_id', 'foreign'), ('epoch', True), ('epoch', 0),
                             ('digest', 'invalid'), ('operation_id', ''), ('workspace', 'relative')]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                adapter, expected, _, _, _, _ = self.evidence(tmp)
                expected[field] = value
                self.assertIsNone(adapter.retired_origin(expected['native_id'], expected))
        with tempfile.TemporaryDirectory() as tmp:
            adapter, expected, _, _, _, _ = self.evidence(tmp)
            with patch.object(nl.os, 'geteuid', return_value=os.geteuid()+1):
                self.assertIsNone(adapter.retired_origin(expected['native_id'], expected))

    def test_private_reader_refuses_unsafe_duplicate_and_blocking_files(self):
        for target in ('manifest', 'tombstone'):
            for fault in ('group_file','group_parent','symlink','hardlink','fifo','duplicate','nested_duplicate','oversize','metadata_oversize','truncated','ancestor_link'):
                with self.subTest(target=target, fault=fault), tempfile.TemporaryDirectory() as tmp:
                    adapter, expected, manifest, tombstone, launch, retired = self.evidence(tmp)
                    path = manifest if target == 'manifest' else tombstone
                    if fault == 'group_file': path.chmod(0o660)
                    elif fault == 'group_parent': path.parent.chmod(0o770)
                    elif fault == 'symlink':
                        copy = path.with_suffix('.original'); path.rename(copy); path.symlink_to(copy)
                    elif fault == 'hardlink': os.link(path, path.with_suffix('.link'))
                    elif fault == 'fifo': path.unlink(); os.mkfifo(path, 0o600)
                    elif fault == 'ancestor_link':
                        directory = path.parent; moved = directory.with_name(directory.name+'-moved'); directory.rename(moved); directory.symlink_to(moved, target_is_directory=True)
                    elif fault == 'duplicate': path.write_text('{"id":"first","id":"second"}')
                    elif fault == 'nested_duplicate': path.write_text('{"exit":{"code":127,"code":0}}')
                    elif fault == 'oversize': path.write_text(' '*1100000)
                    elif fault == 'metadata_oversize': path.write_text(json.dumps({**(launch if target == 'manifest' else retired), 'extra': 'x'*65536}))
                    elif fault == 'truncated': path.write_text('{')
                    self.assertIsNone(adapter.retired_origin(expected['native_id'], expected))


class TombstoneTest(unittest.TestCase):
    def tombstone(self, tmp, native_id='D6CF9872-8597-48A5-BE64-990CDEF4DB31', output=None, meta=None, **overrides):
        base = {'id': native_id, 'tag': 'task-t1', 'workspace': str(tmp), 'cause': 'finished',
                'exit': {'code': 0, 'signal': None}, 'containment_empty': True,
                'deadline_ms': 1000, 'deadline_exceeded': False}
        if output is not None:
            base['output'] = output
        if meta is not None:
            base['meta'] = meta
        base.update(overrides)
        directory = Path(tmp) / '.local' / 'state' / 'aplexer' / 'retired-sessions' / native_id.lower()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'tombstone.json'
        path.write_text(json.dumps(base))
        return str(Path(tmp) / '.local' / 'state' / 'aplexer'), base

    def test_unmanaged_stop_deleted_or_recreated_workspace_uses_only_exact_positive_tombstone(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = str(Path(tmp) / 'gone')
            native_id = 'd6cf9872-8597-48a5-be64-990cdef4db31'
            exit_info = {'code': 0, 'signal': None, 'exited_at_ms': round(time.time()*1000)-1000}
            state_dir, _ = self.tombstone(tmp, native_id=native_id, workspace=workspace + ' (deleted)', exit=exit_info)
            calls = []
            def runner(*args, **kw):
                calls.append(args)
                raise AssertionError('no live native call for positive retirement')
            adapter = nl.NativeLauncher({'routes': {}, 'aplexer_state_dir': state_dir}, runner=runner)
            command = {'kind': 'stop', 'native_id': native_id,
                       'identity': {'id': native_id, 'tag': 'task-t1', 'workspace': workspace},
                       'deadline_ms': round(time.time()*1000)+60000}
            for recreated in (False, True):
                if recreated:
                    Path(workspace).mkdir()
                result = adapter.native_control(command)
                self.assertTrue(result['stopped'])
                self.assertEqual(result['evidence']['workspace'], workspace)
                self.assertEqual(result['evidence']['source'], 'aplexer_retirement')
            self.assertEqual(calls, [])
            with self.assertRaisesRegex(RuntimeError, 'workspace_unavailable'):
                adapter.native_control({**command, 'identity': {**command['identity'], 'workspace': str(Path(tmp)/'other')}})
            self.assertEqual(calls, [])

    def test_unmanaged_stop_absence_never_proves_exit_and_capture_does_not_guess_deleted_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = str(Path(tmp) / 'gone')
            calls = []
            adapter = nl.NativeLauncher({'routes': {}, 'aplexer_state_dir': tmp}, runner=lambda *a, **k: calls.append(a))
            for kind in ('stop', 'capture'):
                with self.assertRaisesRegex(RuntimeError, 'workspace_unavailable'):
                    adapter.native_control({'kind': kind, 'native_id': 'd6cf9872-8597-48a5-be64-990cdef4db31',
                        'identity': {'tag': 'peer', 'workspace': workspace}, 'deadline_ms': round(time.time()*1000)+60000})
            self.assertEqual(calls, [])

    def test_read_verify_and_native_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, document = self.tombstone(tmp)
            read = nl.read_tombstone(document['id'], state_dir=state_dir)
            self.assertEqual(read['id'].lower(), document['id'].lower())
            valid, _ = nl.verify_tombstone(read, document['id'], 'task-t1', tmp)
            self.assertTrue(valid)
            adapter = nl.NativeLauncher({'routes': {}, 'aplexer_state_dir': state_dir}, runner=lambda *a, **k: None)
            result = adapter.retired(document['id'], {'tag': 'task-t1', 'workspace': tmp})
            self.assertIsNotNone(result)
            self.assertEqual(result['exit']['code'], 0)
            self.assertTrue(result['containment_empty'])
            self.assertEqual(result['retirement'], 'verified_finished')

    def test_identity_and_containment_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, document = self.tombstone(tmp)
            adapter = nl.NativeLauncher({'routes': {}, 'aplexer_state_dir': state_dir}, runner=lambda *a, **k: None)
            self.assertIsNone(adapter.retired(document['id'], {'tag': 'other', 'workspace': tmp}))
            self.tombstone(tmp, containment_empty=False)
            self.assertIsNone(adapter.retired(document['id'], {'tag': 'task-t1', 'workspace': tmp}))

    def test_deleted_workspace_retirement_requires_exact_positive_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'work'
            native_id = 'D6CF9872-8597-48A5-BE64-990CDEF4DB31'
            deleted = str(workspace) + ' (deleted)'
            exit_info = {'code': 0, 'signal': None, 'exited_at_ms': 1000}
            state_dir, _ = self.tombstone(tmp, workspace=deleted, exit=exit_info)
            adapter = nl.NativeLauncher({'aplexer_state_dir': state_dir})
            expected = {'tag': 'task-t1', 'workspace': str(workspace)}
            for recreated in (False, True):
                with self.subTest(recreated=recreated):
                    if recreated:
                        workspace.mkdir()
                    result = adapter.retired(native_id, expected)
                    self.assertIsNotNone(result)
                    self.assertEqual(result['workspace'], str(workspace))
                    self.assertEqual(result['id'], native_id)
                    self.assertEqual(result['exit'], exit_info)
            for changes in (
                {'id': 'A1B2C3D4-0000-4000-8000-000000000001'},
                {'tag': 'task-t1 '},
                {'workspace': str(workspace) + '-other (deleted)'},
                {'workspace': deleted + ' (deleted)'},
                {'containment_empty': False},
                {'cause': 'forgotten'},
                {'exit': {'code': 0, 'signal': None}},
                {'exit': {'code': 0, 'signal': None, 'exited_at_ms': True}},
                {'exit': {'code': 0, 'signal': 9, 'exited_at_ms': 1000}},
            ):
                with self.subTest(changes=changes):
                    evidence = {'workspace': deleted, 'exit': exit_info}
                    evidence.update(changes)
                    self.tombstone(tmp, **evidence)
                    self.assertIsNone(adapter.retired(native_id, expected))

    def test_oversize_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, document = self.tombstone(tmp)
            path = Path(state_dir) / 'retired-sessions' / document['id'].lower() / 'tombstone.json'
            path.write_text(json.dumps({'id': document['id'], 'pad': 'x' * 200000}))
            self.assertIsNone(nl.read_tombstone(document['id'], state_dir=state_dir))
            link = Path(state_dir) / 'retired-sessions' / 'link-id' / 'tombstone.json'
            link.parent.mkdir(parents=True)
            link.symlink_to(path)
            self.assertIsNone(nl.read_tombstone('link-id', state_dir=state_dir))
            self.assertIsNone(nl.read_tombstone('not-a-uuid', state_dir=state_dir))

    def test_retained_output_is_stripped_before_metadata_cap(self):
        """A tombstone whose retained PTY output enlarges the 1 MiB envelope
        must still verify: output is dropped before the 64 KiB metadata cap
        (the real 70,196-byte retirement case), while genuine oversized
        metadata keeps failing closed."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, document = self.tombstone(tmp, output=[0] * 40000)
            raw = (Path(state_dir) / 'retired-sessions' / document['id'].lower() / 'tombstone.json').read_bytes()
            self.assertGreater(len(raw), 65536, 'fixture output must exceed the old metadata cap')
            read = nl.read_tombstone(document['id'], state_dir=state_dir)
            self.assertIsNotNone(read, 'retained output must not reject the tombstone')
            self.assertNotIn('output', read)
            adapter = nl.NativeLauncher({'routes': {}, 'aplexer_state_dir': state_dir}, runner=lambda *a, **k: None)
            result = adapter.retired(document['id'], {'tag': 'task-t1', 'workspace': tmp})
            self.assertIsNotNone(result)
            self.assertEqual(result['exit']['code'], 0)
            fat = self.tombstone(tmp, native_id='A1B2C3D4-0000-4000-8000-000000000001', meta='y' * 70000)
            self.assertIsNone(nl.read_tombstone(fat[1]['id'], state_dir=state_dir), 'oversized metadata still refused')


class CapabilityTest(unittest.TestCase):
    def test_capabilities_follow_provider_and_binary_presence(self):
        config = {'routes': {'codex': {'provider': 'zai'}, 'opencode': {'provider': 'go'},
                             'claude': {'provider': 'claude'}, 'shell': {'provider': 'shell'}}}
        with mock.patch.object(nl.shutil, 'which', side_effect=lambda b: '/bin/' + b if b in ('zcodex', 'opencode') else None):
            adapter = nl.NativeLauncher(config)
            caps = adapter.capabilities()
        self.assertEqual(caps['codex'], {'headless', 'interactive', 'goal', 'effort'})
        self.assertEqual(caps['opencode'], {'headless', 'interactive'})
        self.assertNotIn('claude', caps)
        self.assertEqual(caps['shell'], {'headless'})

    def test_claude_capability_can_install_without_global_claude(self):
        config = {'private_provider_homes': True, 'routes': {'claude': {'provider': 'claude'}}}
        with mock.patch.object(nl.provider_home, 'claude_install_available', return_value=True), \
             mock.patch.object(nl.shutil, 'which', return_value=None):
            self.assertEqual(nl.NativeLauncher(config).capabilities()['claude'],
                             {'headless', 'interactive', 'goal', 'effort'})


class ProviderHomeTest(unittest.TestCase):
    zai_bundle = {'config': {'schemaVersion': 1}, 'zai_token': 'test-token-value-no-spaces',
                  'zai_base_url': 'https://api.z.ai/api/anthropic'}

    def test_materialize_creates_private_home_and_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'providers'
            files = {ph.PATHS['zai']: json.dumps(self.zai_bundle)}
            manifest = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            data = ph.verify(manifest)
            self.assertEqual(data['account_id'], 'zai-central')
            self.assertEqual(data['generation'], 3)
            self.assertEqual(data['model'], 'glm-5.3-flash')
            home = Path(manifest).parent / 'home'
            harness = (home / '.zcodex' / 'config.toml').read_text()
            self.assertIn('wire_api="zcode"', harness)
            self.assertIn('glm-5.3-flash', harness)
            if os.name != 'nt':
                # Windows ACL privacy is enforced through the icacls check in
                # make_private; the coarse stat mode does not map to ACL bits.
                mode = stat.S_IMODE((home / '.zcodex' / 'config.toml').stat().st_mode)
                self.assertEqual(mode & 0o077, 0)
            env = ph.native_environment(manifest)
            self.assertEqual(env['CODEX_HOME'], str(home / '.zcodex'))
            self.assertEqual(env['ZCODE_HOME'], str(home / '.zcode'))

    def test_materialize_is_idempotent_per_generation_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'providers'
            files = {ph.PATHS['zai']: json.dumps(self.zai_bundle)}
            first = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            second = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            self.assertEqual(first, second)
            bumped = ph.materialize(str(root), 'zai-central', 'zai', 4, files, 'glm-5.3-flash')
            self.assertNotEqual(Path(first).parent, Path(bumped).parent)

    def test_non_zai_bundle_requires_valid_expiry(self):
        bundle = {'tokens': {'access_token': 'header.' + __import__('base64').urlsafe_b64encode(
            json.dumps({'exp': int(time.time() / 1000) + 3600}).encode()).decode().rstrip('=') + '.sig',
            'refresh_token': 'r'}, 'last_refresh': '2020-01-01T00:00:00Z'}
        with tempfile.TemporaryDirectory() as tmp:
            files = {ph.PATHS['codex']: json.dumps(bundle)}
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'acc', 'codex', 1, files, 'gpt-6.1-sol', timeout_seconds=7200)

    def test_bundle_schema_and_account_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {ph.PATHS['zai']: json.dumps(self.zai_bundle)}
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'other-account', 'zai', 1, files, 'glm-5.3-flash')
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'zai-central', 'zai', 1, {'wrong': '{}'}, 'glm-5.3-flash')
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'zai-central', 'zai', 0, files, 'glm-5.3-flash')
            with self.assertRaises(ValueError):
                ph.materialize(str(tmp) + '/p', 'zai-central', 'shell', 1, files, 'glm-5.3-flash')


class SessionBindingTest(unittest.TestCase):
    def test_session_binding_does_not_gate_on_quota_or_credential_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'providers'
            files = {ph.PATHS['zai']: json.dumps(ProviderHomeTest.zai_bundle)}
            manifest = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            now = time.time() * 1000
            result = {'session_id': 's1', 'operation_id': 'op1', 'epoch': 2, 'account_id': 'zai-central',
                      'launch_allowed': True, 'model': 'glm-5.3-flash', 'runtime_deadline': int(now + 600000),
                      'quota': {'remaining': 0, 'captured_at': 0, 'valid_until': 0}, 'credential_usable': False}
            client = mock.Mock()
            client.request.return_value = result
            data = ph.session_binding(manifest, client, 's1', 'op1', 2)
            self.assertEqual(data['runtime_deadline'], result['runtime_deadline'])
            client.request.assert_called_once_with('POST', '/api/sessions/s1/admission', mock.ANY)
            body = client.request.call_args[0][2]
            self.assertEqual(body, {'operation_id': 'op1', 'epoch': 2})

    def test_session_binding_still_checks_accepted_session_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'providers'
            files = {ph.PATHS['zai']: json.dumps(ProviderHomeTest.zai_bundle)}
            manifest = ph.materialize(str(root), 'zai-central', 'zai', 3, files, 'glm-5.3-flash')
            now = time.time() * 1000
            base = {'session_id': 's1', 'operation_id': 'op1', 'epoch': 2, 'account_id': 'zai-central',
                    'launch_allowed': True, 'model': 'glm-5.3-flash', 'runtime_deadline': int(now + 600000)}
            client = mock.Mock()
            client.request.return_value = base | {'account_id': 'other'}
            with self.assertRaises(ValueError):
                ph.session_binding(manifest, client, 's1', 'op1', 2)
            binding = base | {'epoch': 9}
            client.request.return_value = binding
            with self.assertRaises(ValueError):
                ph.session_binding(manifest, client, 's1', 'op1', 2)


class SpawnEnvironmentTest(unittest.TestCase):
    def test_fixed_credentials_always_stripped_provider_prefixes_only_with_central_home(self):
        hostile = {'OPENAI_API_KEY': 'ambient', 'ANTHROPIC_AUTH_TOKEN': 'ambient',
                   'ZAI_API_KEY': 'ambient', 'HUB2_TOKEN': 'host-token', 'HUB2_SERVER': 'https://x.invalid',
                   'HUB2_LAUNCH_CONTEXT': 'stale', 'HUB2_CONFIG': 'stale-config', 'OPENCODE_API_KEY': 'ambient',
                   'GO_API_KEY': 'ambient', 'AWS_ACCESS_KEY_ID': 'ambient-aws',
                   'AWS_SECRET_ACCESS_KEY': 'ambient-aws-secret', 'AWS_SESSION_TOKEN': 'ambient-session',
                   'PATH': '/bin', 'HOME': '/home/test'}
        with mock.patch.dict(os.environ, hostile, clear=True):
            plain = nl.spawn_environment(False)
            self.assertNotIn('OPENAI_API_KEY', plain)
            self.assertNotIn('HUB2_TOKEN', plain)
            self.assertNotIn('HUB2_LAUNCH_CONTEXT', plain)
            self.assertNotIn('HUB2_CONFIG', plain)
            self.assertNotIn('AWS_ACCESS_KEY_ID', plain)
            self.assertIn('PATH', plain)
            central = nl.spawn_environment(True)
            for key in ('OPENAI_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ZAI_API_KEY', 'OPENCODE_API_KEY',
                        'GO_API_KEY', 'HUB2_TOKEN', 'HUB2_SERVER', 'HUB2_CONFIG',
                        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'):
                self.assertNotIn(key, central)
            self.assertEqual(central.get('PATH'), '/bin')


class LaunchReceiptTest(unittest.TestCase):
    def test_literal_shell_prompt_never_uses_model_prompt_normalization(self):
        prompt = 'echo first\necho second'
        with tempfile.TemporaryDirectory() as workspace:
            child = (['cmd.exe', '/d', '/s', '/c', prompt] if os.name == 'nt' else ['/bin/sh', '-c', prompt])
            output = json.dumps({'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-t1',
                                 'workspace': workspace, 'command': child})
            runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=output))
            adapter = nl.NativeLauncher({'routes': {'shell': {'provider': 'shell'}}}, runner=runner)
            with mock.patch.object(nl, 'aplexer_prompt', return_value='echo first echo second') as normalize, \
                 mock.patch.object(adapter, 'record_evidence'):
                adapter.launch('t1', {'workspace': workspace, 'engine': 'shell', 'model': 'shell',
                                     'prompt': prompt, 'interactive': False, 'timeout_seconds': 60})
            normalize.assert_not_called()
            command = runner.call_args.args[0]
            self.assertEqual(command[command.index('--') + 1:], child)

    def test_launch_attempts_start_without_a_host_capacity_gate(self):
        config = {'private_provider_homes': True, 'routes': {'codex': {'provider': 'zai'}}, 'min_free_mem_mb': 6000, 'memory_mb': 1500}
        with tempfile.TemporaryDirectory() as workspace:
            output = json.dumps({'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-t1',
                                 'workspace': workspace, 'command': ['zcodex', 'exec', '--model', 'glm-5.3-flash']})
            runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=output))
            adapter = nl.NativeLauncher(config, runner=runner)
            spec = {'workspace': workspace, 'engine': 'codex', 'model': 'glm-5.3-flash',
                    'prompt': 'p', 'interactive': False, 'timeout_seconds': 600}
            with (mock.patch.object(adapter, 'wait_first_action', return_value={'session_id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'first_action_verified': True}),
                  mock.patch.object(adapter, 'record_evidence')):
                receipt = adapter.launch('t1', spec)
            self.assertFalse(receipt['first_action_verified'])
            command = runner.call_args.args[0]
            self.assertEqual(command[command.index('--memory') + 1], '1500M')

    def test_launch_refuses_unknown_route(self):
        adapter = nl.NativeLauncher({'private_provider_homes': True, 'routes': {}})
        with self.assertRaises(RuntimeError):
            adapter.launch('t1', {'engine': 'codex', 'workspace': '/', 'model': 'm', 'prompt': 'p',
                                  'interactive': False, 'timeout_seconds': 600})

    def test_managed_claude_launch_uses_absolute_versioned_binary(self):
        import subprocess
        with tempfile.TemporaryDirectory() as workspace:
            native_id = 'd6cf9872-8597-48a5-be64-990cdef4db31'
            deadline = int(time.time() * 1000) + 500000
            captured = {}

            def runner(argv, **options):
                child = argv[argv.index('--') + 1:]
                captured['child'] = child
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'id': native_id, 'tag': 'task-managed-claude', 'workspace': workspace,
                    'command': child, 'deadline_ms': deadline,
                }), '')

            adapter = nl.NativeLauncher({'private_provider_homes': True, 'routes': {'claude': {'provider': 'claude'}}, 'min_free_mem_mb': 0},
                                        runner=runner)
            spec = {'workspace': workspace, 'engine': 'claude', 'model': 'fable', 'effort': 'medium',
                    'prompt': 'analyze safely', 'interactive': False, 'timeout_seconds': 600,
                    'provider_home': '/private/manifest.json', 'client': mock.Mock(),
                    'session_id': 'session-managed', 'operation_id': 'operation-managed', 'epoch': 1}
            binding = {'runtime_deadline': deadline,
                       'engine_binary_path': '/private/engines/claude-2.1.322/claude'}
            with mock.patch.object(nl, 'session_binding', return_value=binding), \
                 mock.patch.object(nl, 'native_environment', return_value={'CLAUDE_CONFIG_DIR': '/private/home/.claude'}), \
                 mock.patch.object(adapter, 'wait_first_action', return_value={'session_id': native_id,
                                                                               'first_action_verified': True}), \
                 mock.patch.object(adapter, 'record_evidence'):
                adapter.launch('managed-claude', spec)
            self.assertEqual(captured['child'][0], binding['engine_binary_path'])
            self.assertEqual(captured['child'][captured['child'].index('--model') + 1], 'fable')
            self.assertEqual(captured['child'][captured['child'].index('--effort') + 1], 'medium')
            self.assertEqual(captured['child'][captured['child'].index('--output-format') + 1], 'stream-json')
            self.assertIn('--verbose', captured['child'])

    def test_wait_first_action_success_without_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-t1',
                      'workspace': tmp, 'command': ['x'], 'phase': 'starting', 'schema_version': 1,
                      'created_at_ms': int(time.time() * 1000) - 1000}
            whoami = {'id': record['id'], 'tag': record['tag'], 'workspace': tmp, 'command': ['x'],
                      'phase': 'working', 'schema_version': 2, 'worker_pid': os.getpid() + 1,
                      'updated_at_ms': record['created_at_ms'] + 500}
            path = Path(tmp) / 'fa.json'
            path.write_text(json.dumps(whoami))
            os.utime(path, (time.time(), time.time()))
            adapter = nl.NativeLauncher({'routes': {}})
            receipt = adapter.wait_first_action(str(path), record, time.time() - 1,
                                                {'workspace': tmp, 'timeout_seconds': 5},
                                                poll=lambda selector: {'phase': 'running'}, sleep=lambda s: None)
            self.assertEqual(receipt['session_id'], record['id'])
            self.assertTrue(receipt['first_action_verified'])

    def test_wait_first_action_dead_native_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-t1',
                      'workspace': tmp, 'command': ['x'], 'phase': 'starting', 'schema_version': 1,
                      'created_at_ms': int(time.time() * 1000) - 1000}
            adapter = nl.NativeLauncher({'routes': {}})
            with self.assertRaises(RuntimeError):
                adapter.wait_first_action(str(Path(tmp) / 'absent.json'), record, time.time() - 1,
                                          {'workspace': tmp, 'timeout_seconds': 1},
                                          poll=lambda selector: {'phase': 'dead'}, sleep=lambda s: None)


if __name__ == '__main__':
    unittest.main()


class CentralSpawnEnvironmentTest(unittest.TestCase):
    """Regression: ambient credentials are sanitized in the spawn-client
    environment BEFORE the trusted native home is added, and the child
    receives the intended provider home through explicit --env entries."""

    def test_central_spawn_passes_trusted_home_and_strips_ambient(self):
        import json as json_module
        import subprocess as subprocess_module
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as providers_tmp:
            files = {ph.PATHS['zai']: json_module.dumps(ProviderHomeTest.zai_bundle)}
            manifest = ph.materialize(str(Path(providers_tmp) / 'providers'), 'zai-central', 'zai', 3,
                                      files, 'glm-5.3-flash', timeout_seconds=3600)
            home = Path(manifest).parent / 'home'
            now = time.time() * 1000
            admission = {'session_id': 'session-env', 'operation_id': 'operation-env', 'epoch': 1,
                         'account_id': 'zai-central', 'launch_allowed': True, 'provider': 'zai',
                         'credential_generation': 3, 'model': 'glm-5.3-flash',
                         'runtime_deadline': int(now + 600000),
                         'quota': {'source': 'provider:zai:generation:3', 'captured_at': int(now) - 1000,
                                   'valid_until': int(now) + 60000, 'remaining': 88.0}}
            client = mock.Mock()
            client.request.return_value = admission
            native_id = '9d9d7183-96cb-4c58-9f0e-b69e70f39b69'
            created = int(time.time() * 1000) - 1000
            calls = []

            def runner(argv, **options):
                calls.append((argv, options))
                child = argv[argv.index('--') + 1:]
                tag = argv[argv.index('--tag') + 1]
                cwd = options['cwd']
                process = {'id': native_id, 'tag': tag, 'workspace': cwd, 'engine': 'codex',
                           'created_at_ms': created, 'command': child, 'phase': 'running'}
                task = tag[len('task-'):]
                artifact = Path(cwd) / '.local' / ('first-action-' + task + '.json')
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(json_module.dumps({'id': native_id, 'tag': tag, 'workspace': cwd,
                    'command': child, 'phase': 'working', 'schema_version': 2, 'worker_pid': os.getpid() + 3,
                    'updated_at_ms': created + 500}))
                return subprocess_module.CompletedProcess(argv, 0, json_module.dumps(process), '')

            adapter = nl.NativeLauncher({'routes': {'codex': {'provider': 'zai'}}, 'min_free_mem_mb': 0,
                                         'memory_mb': 1500}, runner=runner)
            spec = {'workspace': workspace, 'engine': 'codex', 'model': 'glm-5.3-flash',
                    'prompt': 'ambient-vs-trusted regression', 'interactive': False, 'timeout_seconds': 600,
                    'provider_home': manifest, 'client': client, 'session_id': 'session-env',
                    'operation_id': 'operation-env', 'epoch': 1}
            with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'ambient-openai',
                                              'AWS_ACCESS_KEY_ID': 'ambient-aws',
                                              'HUB2_CONFIG': 'stale-path',
                                              'ANTHROPIC_AUTH_TOKEN': 'ambient-anthropic'}, clear=False):
                receipt = adapter.launch('task-env', spec)
            self.assertEqual(receipt['session_id'], native_id)
            argv, options = calls[0]
            self.assertNotIn('--deadline-ms', argv)
            env_pairs = {argv[i + 1].split('=', 1)[0]: argv[i + 1].split('=', 1)[1]
                         for i in range(len(argv) - 1) if argv[i] == '--env'}
            # Trusted native home paths survive and point at the materialized home.
            self.assertEqual(env_pairs.get('CODEX_HOME'), str(home / '.zcodex'))
            self.assertEqual(env_pairs.get('CLAUDE_CONFIG_DIR'), str(home / '.claude'))
            self.assertEqual(env_pairs.get('ZCODE_HOME'), str(home / '.zcode'))
            self.assertEqual(env_pairs.get('HOME'), str(home))
            # Ambient credentials are absent from both the child --env set and the client env.
            for key in ('OPENAI_API_KEY', 'AWS_ACCESS_KEY_ID', 'ANTHROPIC_AUTH_TOKEN', 'HUB2_CONFIG'):
                self.assertNotIn(key, env_pairs, key)
                self.assertNotIn(key, options['env'], key)
            # The spawn client itself carries no ambient provider credential.
            self.assertNotIn('CODEX_HOME', options['env'])
            client.request.assert_called_once_with('POST', '/api/sessions/session-env/admission', mock.ANY)


class StrictNativeSendTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        from types import SimpleNamespace
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace=self.temp.name
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE launches(native_id TEXT)')
        self.native='11111111-1111-4111-8111-111111111111'
        self.operation='22222222-2222-4222-8222-222222222222'
        self.row={'id':self.native,'tag':'synthetic-peer','workspace':self.workspace,'schema_version':1,
            'phase':'running','state':'running','worker_alive':True,'created_at_ms':42}
        self.calls=[];self.changes={};self.returncode=0;self.raw=None
        self.adapter=nl.NativeLauncher({'aplexer':'synthetic-a'},runner=self.runner,journal=SimpleNamespace(db=self.db))

    def command(self,text='synthetic π input\nline\t2'):
        deadline=round(time.time()*1000)+60000
        digest=__import__('hashlib').sha256(json.dumps([self.native,self.operation,self.row['tag'],self.workspace,42,deadline,text],
            ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        return {'id':self.operation,'kind':'send','native_id':self.native,'deadline_ms':deadline,
            'identity':{key:self.row[key] for key in ('id','tag','workspace')},
            'submission':{'operation_id':self.operation,'created_at_ms':42,'deadline_ms':deadline,'text':text,'request_sha256':digest}}

    def runner(self,args,**kw):
        self.calls.append((args,kw))
        if args[1]=='status':return subprocess.CompletedProcess(args,0,json.dumps(self.row),'')
        if args[1]=='list':return subprocess.CompletedProcess(args,0,json.dumps([self.row]),'')
        self.assertEqual(args[1],'send')
        deadline=int(args[args.index('--deadline-ms')+1])
        digest=__import__('hashlib').sha256(json.dumps([args[2],args[args.index('--idempotency-key')+1],
            args[args.index('--expect-tag')+1],args[args.index('--expect-workspace')+1],int(args[args.index('--expect-created-at-ms')+1]),deadline,kw['input']],
            ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        result={'version':1,'session_id':self.native,'operation_id':self.operation,'request_sha256':digest,
            'deadline_ms':deadline,'status':'submitted_verified','mutation':True,'landed':True,'submitted':True,
            'consumed':None,'reason':'submission_observed','untrusted_text':'must-never-leak',**self.changes}
        return subprocess.CompletedProcess(args,self.returncode,json.dumps(result) if self.raw is None else self.raw,'private stderr never exposed')

    def test_exact_strict_argv_stdin_deadline_and_whitelisted_positive_receipt(self):
        command=self.command();result=self.adapter.native_control(command)
        args,kw=self.calls[-1]
        self.assertEqual(args,['synthetic-a','send',self.native,'--strict','--enter','--expect-tag','synthetic-peer',
            '--expect-workspace',self.workspace,'--expect-created-at-ms','42','--idempotency-key',self.operation,'--deadline-ms',str(command['deadline_ms']),'--stdin','--json'])
        self.assertEqual(kw['input'],command['submission']['text'])
        self.assertNotIn(command['submission']['text'],' '.join(args))
        self.assertLessEqual(kw['timeout'],13)
        self.assertEqual(result['submission']['request_sha256'],command['submission']['request_sha256'])
        self.assertIsNone(result['submission']['consumed'])
        self.assertNotIn('untrusted_text',result['submission'])
        self.assertEqual([args[1] for args,_ in self.calls],['status','list','send'])

    def test_foreign_missing_or_malformed_receipts_are_unverified_and_never_fallback(self):
        for changes in ({'version':True},{'version':2},{'session_id':self.operation},{'operation_id':self.native},
                        {'request_sha256':'0'*64},{'deadline_ms':1},{'deadline_ms':True},{'consumed':False},
                        {'landed':False},{'mutation':1},{'reason':'unsafe_detail private'}, {'reason':'preflight_refused'}):
            self.changes=changes;self.calls=[]
            result=self.adapter.native_control(self.command())
            self.assertEqual(result['error'],'native_send_receipt_unverified',changes)
            self.assertEqual(sum(args[1]=='send' for args,_ in self.calls),1)
        for raw,code in (('',2),('{bad',0),('{"version":1,"version":2}',0)):
            self.raw=raw;self.returncode=code;self.calls=[]
            self.assertEqual(self.adapter.native_control(self.command())['error'],'native_send_receipt_unverified')
            self.assertEqual(sum(args[1]=='send' for args,_ in self.calls),1)

    def test_structured_refusal_and_uncertainty_do_not_claim_consumption(self):
        for status in ('refused','uncertain'):
            self.changes={'status':status,'mutation':False if status=='refused' else None,
                'landed':False,'submitted':False,'reason':'strict_platform_unsupported' if status=='refused' else 'strict_transport_uncertain'}
            self.returncode=4
            result=self.adapter.native_control(self.command())['submission']
            self.assertEqual(result['status'],status);self.assertIsNone(result['consumed'])

    def test_invalid_text_digest_or_incarnation_has_zero_send_effect(self):
        for text in ('','  ','x\x1b[200~','\x00','\u0080','界'*2731):
            with self.assertRaisesRegex(ValueError,'invalid_native_send'):self.adapter.native_control(self.command(text))
        command=self.command();command['submission']['request_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'invalid_native_send'):self.adapter.native_control(command)
        for value in (43,True,None):
            self.row['created_at_ms']=value
            with self.assertRaisesRegex(RuntimeError,'identity_mismatch'):self.adapter.native_control(self.command())
        self.assertFalse(any(args[1]=='send' for args,_ in self.calls))

    def test_fresh_census_liveness_and_deadline_are_rechecked_before_strict_input(self):
        from types import SimpleNamespace
        for changes in ({'created_at_ms':43},{'workspace':'/foreign'},{'worker_alive':False},{'phase':'exited'},{'state':'unknown'}):
            self.adapter.inventory=lambda:SimpleNamespace(rows=[{**self.row,**changes}],fresh=lambda:True)
            with self.assertRaisesRegex(RuntimeError,'identity_mismatch'):self.adapter.native_control(self.command())
        self.adapter.inventory=lambda:SimpleNamespace(rows=[self.row],fresh=lambda:False)
        with self.assertRaisesRegex(RuntimeError,'native_census_stale'):self.adapter.native_control(self.command())
        self.adapter.inventory=lambda:SimpleNamespace(rows=[self.row],fresh=lambda:True)
        command=self.command();command['deadline_ms']=round(time.time()*1000)+1000
        command['submission']['deadline_ms']=command['deadline_ms']
        command['submission']['request_sha256']=__import__('hashlib').sha256(json.dumps([self.native,self.operation,self.row['tag'],self.workspace,42,
            command['deadline_ms'],command['submission']['text']],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        with self.assertRaisesRegex(RuntimeError,'native_command_deadline'):self.adapter.native_control(command)
        self.assertFalse(any(args[1]=='send' for args,_ in self.calls))


class GoJsonOutputTests(unittest.TestCase):
    def test_direct_go_route_requests_documented_json_without_model_or_permission_changes(self):
        self.assertEqual(nl.adapter_argv('go','synthetic prompt',nl.OPENCODE_MODEL),
            ['opencode','run','--standalone','--auto','--format','json','--model',nl.OPENCODE_MODEL,'synthetic prompt'])

    def test_actual_local_task_route_threads_json_option_as_engine_args(self):
        with tempfile.TemporaryDirectory() as root:
            adapter=nl.NativeLauncher({'private_provider_homes':False},aplexer='synthetic-a')
            spec={'workspace':root,'operation_id':'op-local-go','epoch':1,'timeout_seconds':1800,'model':nl.OPENCODE_MODEL}
            argv=adapter.profile_command('hub2-local-go',spec,nl.LOCAL_ROUTES['go'],'synthetic prompt')
            self.assertEqual(argv[:3],['synthetic-a','task','run'])
            self.assertIn('--engine-arg=--format',argv);self.assertIn('--engine-arg=json',argv)
            self.assertIn('--engine-arg='+nl.OPENCODE_MODEL,argv)
            self.assertEqual(argv[argv.index('--profile')+1],nl.LOCAL_ROUTES['go']['profile'])


class HeadlessStreamOutputTests(unittest.TestCase):
    MODELS = {'claude': 'opus', 'antigravity': 'gemini-4-argon-medium'}

    def launch(self, provider, *, effort=None, interactive=False, local=False):
        parent = Path(__file__).resolve().parents[3] / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent, prefix='headless-stream-') as workspace:
            captured = {}
            native_id = 'd6cf9872-8597-48a5-be64-990cdef4db31'

            def runner(argv, **options):
                child = argv[argv.index('--') + 1:]
                captured['child'] = child
                captured['prompt'] = (Path(child[child.index('--prompt-file') + 1]).read_text()
                                      if local else child[-1])
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'id': native_id, 'tag': 'task-stream-test', 'workspace': workspace, 'command': child,
                }), '')

            adapter = nl.NativeLauncher({'private_provider_homes': not local,
                                         'routes': {provider: nl.LOCAL_ROUTES[provider]}},
                                        aplexer='synthetic-aplexer', runner=runner)
            spec = {'workspace': workspace, 'engine': provider, 'model': self.MODELS[provider],
                    'prompt': 'use the tool safely', 'interactive': interactive, 'timeout_seconds': 90,
                    'operation_id': 'stream-operation', 'epoch': 1}
            if effort is not None:
                spec['effort'] = effort
            with mock.patch.object(adapter, 'record_evidence'):
                receipt = adapter.launch('stream-test', spec)
            self.assertEqual(receipt['mode'], 'interactive' if interactive else 'headless')
            self.assertEqual(captured['prompt'], nl.aplexer_prompt(spec['prompt'] + nl.FIRST_ACTION_INSTRUCTION.format(
                path=Path(workspace) / '.local/first-action-stream-test.json')))
            return captured['child'], captured['prompt']

    def test_direct_headless_stream_flags_preserve_model_effort_and_argon_print_argument(self):
        for effort in (None, 'medium'):
            with self.subTest(provider='claude', effort=effort):
                child, prompt = self.launch('claude', effort=effort)
                self.assertEqual(child, ['claude', '--dangerously-skip-permissions', '--model', 'opus', '-p',
                                        *(['--effort', effort] if effort else []), '--setting-sources', 'user',
                                        '--output-format', 'stream-json', '--verbose', prompt])
            with self.subTest(provider='antigravity', effort=effort):
                child, prompt = self.launch('antigravity', effort=effort)
                self.assertEqual(child, ['agy', '--model', self.MODELS['antigravity'], '--dangerously-skip-permissions',
                                        *(['--effort', effort] if effort else []),
                                        '--output-format', 'stream-json', '-p', prompt])

    def test_local_profile_launch_forwards_stream_flags_and_preserves_first_action(self):
        for provider in self.MODELS:
            for effort in (None, 'medium'):
                with self.subTest(provider=provider, effort=effort):
                    child, _ = self.launch(provider, effort=effort, local=True)
                    flags = [arg.split('=', 1)[1] for arg in child if arg.startswith('--engine-arg=')]
                    self.assertEqual(flags, ['--model', self.MODELS[provider], '--output-format', 'stream-json',
                                             *(['--verbose'] if provider == 'claude' else []),
                                             *(['--setting-sources', 'user'] if provider == 'claude' else []),
                                             *(['--effort', effort] if effort else [])])
                    self.assertEqual(child[:3], ['synthetic-aplexer', 'task', 'run'])
                    selector = '--profile' if provider == 'antigravity' else '--engine'
                    self.assertEqual(child[child.index(selector) + 1],
                                     'hub-hub-antigravity' if provider == 'antigravity' else 'claude')

    def test_interactive_provider_argv_keeps_exact_original_bytes(self):
        for effort in (None, 'medium'):
            for provider in self.MODELS:
                with self.subTest(provider=provider, effort=effort):
                    child, prompt = self.launch(provider, effort=effort, interactive=True)
                    prefix = (['claude', '--dangerously-skip-permissions', '--model', 'opus'] if provider == 'claude'
                              else ['agy', '--model', self.MODELS[provider], '--dangerously-skip-permissions'])
                    self.assertEqual(child, [*prefix, *(['--effort', effort] if effort else []), *(['--prompt-interactive'] if provider == 'antigravity' else []), prompt])


class ManagedClaudeReadinessTests(unittest.TestCase):
    def test_actual_bundled_protocol_rejects_old_pin_then_accepts_exact_repin(self):
        if sys.platform != 'linux':
            self.assertIsNone(nl.NativeLauncher({}).managed_claude_readiness())
            return
        import hashlib
        import zipfile
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temporary:
            root=Path(temporary)
            release=root/('a'*40);release.mkdir(mode=0o700)
            (release/'bin').mkdir(mode=0o700)
            (release/'bin/python').symlink_to(sys.executable)
            current=root/'current';current.symlink_to(release,target_is_directory=True)
            archive=release/'hub.pyz'
            package=Path(__file__).resolve().parents[3]/'host/agent_hub_host'
            with zipfile.ZipFile(archive,'w') as out:
                for file in package.glob('*.py'):
                    out.write(file,'agent_hub_host/'+file.name)
                out.writestr('__main__.py','raise RuntimeError("CLI main must never run")')
            archive.chmod(0o600)
            raw=archive.read_bytes()
            receipt={'host_version':release.name,'artifacts':{'hub.pyz':{'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}}}
            (release/'release.json').write_text(json.dumps(receipt))
            (release/'release.json').chmod(0o600)
            profile=root/'profiles/config.toml';profile.parent.mkdir(mode=0o700)
            env={'APLEXER_CONFIG':str(profile)}
            with mock.patch.dict(os.environ,env):
                code=('import sys,json;sys.path.insert(0,sys.argv[1]);'
                      'from agent_hub_host.provisioning import module_command;'
                      'sys.argv[0]=sys.argv[1];print(json.dumps(module_command("execute","hub-claude")))')
                command=json.loads(subprocess.check_output([str(current/'bin/python'),'-I','-S','-c',code,str(archive)],text=True))
                self.assertEqual(command[0],str(release/'bin/python'))
                self.assertEqual(command[1:3],['-I','-S'])
                user_site=root/'untrusted-site';user_site.mkdir()
                sentinel=root/'site-executed'
                (user_site/'sitecustomize.py').write_text('from pathlib import Path;Path('+repr(str(sentinel))+').write_text("unexpected")')
                harmless=[command[0],*command[1:command.index('-c')],'-c','print("isolated-owned-test")']
                probe=subprocess.run(harmless,capture_output=True,text=True,timeout=3,
                                     env={**os.environ,'PYTHONPATH':str(user_site),'HOME':str(root)})
                self.assertEqual(probe.stdout.strip(),'isolated-owned-test');self.assertEqual(probe.returncode,0)
                self.assertFalse(sentinel.exists())
                def publish(argv):
                    profile.write_text('[profiles."hub-hub-claude"]\nengine="claude"\ncommand='+json.dumps(argv)+'\n')
                    profile.chmod(0o600)
                results=[]
                def probe(argv,**kwargs):
                    result=subprocess.run(argv,**kwargs);results.append(result);return result
                adapter=nl.NativeLauncher({},runner=probe)
                publish([sys.executable,'-c','raise RuntimeError("old18c")','hub-claude'])
                self.assertIsNone(adapter.managed_claude_readiness(release))
                publish(command)
                ready=adapter.managed_claude_readiness(release)
                self.assertIsNotNone(ready,results[-1].stderr if results else 'probe not reached')
                self.assertEqual(ready['sha256'],receipt['artifacts']['hub.pyz']['sha256'])
                context={'protocol':'hub2-managed-claude-exec-v1','label':'hub-claude','profile':'hub-hub-claude',
                    'account_id':'claude','engine':'claude','model':'opus','effort':'medium','session_id':'packaged',
                    'operation_id':'op-packaged:0','epoch':2,'workspace':str(root),'task_id':'hub2-packaged',
                    'server':'https://hub.test','runtime_deadline':int(time.time()*1000)+60000,'request_digest':'b'*64,
                    'machine_id':'host','exec_archive_sha256':ready['sha256']}
                intent=root/'context.json'
                def check_context():
                    intent.write_text(json.dumps(context));intent.chmod(0o600)
                    code=('import sys;sys.path.insert(0,sys.argv[1]);'
                          'from agent_hub_host.provisioning import managed_claude_context;'
                          'managed_claude_context(["--hub-managed-claude-context="+sys.argv[2]])')
                    return subprocess.run([str(release/'bin/python'),'-I','-S','-c',code,str(archive),str(intent)],
                                          cwd=root,capture_output=True,text=True,timeout=3)
                self.assertEqual(check_context().returncode,0)
                context['exec_archive_sha256']='0'*64
                self.assertNotEqual(check_context().returncode,0)
                publish(command[:-1]+['wrong-label'])
                self.assertIsNone(adapter.managed_claude_readiness(release))
                publish(command)
                archive.write_bytes(raw+b'corruption')
                self.assertIsNone(adapter.managed_claude_readiness(release))

    def test_opt_in_uses_fixed_profile_and_private_context_without_changing_default(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temporary:
            workspace=Path(temporary);journal=SimpleNamespace(directory=workspace/'journal')
            journal.directory.mkdir(mode=0o700)
            adapter=nl.NativeLauncher({},journal=journal,aplexer='synthetic-a')
            spec={'workspace':str(workspace),'operation_id':'op-managed:0','session_id':'managed',
                  'epoch':4,'timeout_seconds':1800,'model':'opus','engine':'claude','effort':'medium',
                  'runtime_deadline':int(time.time()*1000)+60000,'request_digest':'b'*64,'account_id':'claude',
                  'client':SimpleNamespace(base_url='https://hub.test'),'managed_credential_label':'hub-claude','machine_id':'host'}
            with mock.patch.object(adapter,'managed_claude_readiness',return_value={'sha256':'a'*64}):
                argv=adapter.profile_command('hub2-managed',spec,{'provider':'claude','profile':'hub-hub-claude'},'prompt')
            self.assertEqual(argv[argv.index('--profile')+1],'hub-hub-claude')
            markers=[v for v in argv if v.startswith('--engine-arg=--hub-managed-claude-context=')]
            self.assertEqual(len(markers),1)
            path=Path(markers[0].split('=',2)[2]);context=json.loads(path.read_text())
            prompt_path=Path(argv[argv.index('--prompt-file')+1])
            self.assertNotEqual(prompt_path,path)
            self.assertEqual(prompt_path.read_bytes(),b'prompt')
            self.assertEqual(context['account_id'],'claude');self.assertEqual(context['epoch'],4)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertNotIn('token',repr(context))
            self.assertNotIn('managed_credential_label',adapter.config['routes']['claude'])

    def test_opt_in_remains_profile_based_with_global_private_homes_enabled(self):
        from types import SimpleNamespace
        for private in (False,True):
            with self.subTest(private=private), tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temporary:
                workspace=Path(temporary);journal=SimpleNamespace(directory=workspace/'journal')
                journal.directory.mkdir(mode=0o700)
                captured=[]
                def runner(argv,**kwargs):
                    self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN',kwargs['env'])
                    self.assertNotIn('ANTHROPIC_CUSTOM_KEY',kwargs['env'])
                    self.assertNotIn('HUB2_TOKEN',kwargs['env'])
                    child=argv[argv.index('--')+1:];captured.append(child)
                    return subprocess.CompletedProcess(argv,0,json.dumps({'id':'11111111-2222-4333-8444-555555555555',
                        'tag':'task-hub2-managed','workspace':str(workspace),'command':child}), '')
                adapter=nl.NativeLauncher({'private_provider_homes':private,'routes':{'claude':{'provider':'claude','engine':'claude'}}},
                                         runner=runner,journal=journal,aplexer='synthetic-a')
                spec={'workspace':str(workspace),'operation_id':'op-managed:0','session_id':'managed','epoch':4,
                      'timeout_seconds':1800,'model':'opus','engine':'claude','effort':'medium','prompt':'real task',
                      'interactive':False,'runtime_deadline':int(time.time()*1000)+60000,'request_digest':'b'*64,
                      'account_id':'claude','machine_id':'host','client':SimpleNamespace(base_url='https://hub.test'),
                      'managed_credential_label':'hub-claude'}
                with mock.patch.object(adapter,'managed_claude_readiness',return_value={'sha256':'a'*64}), mock.patch.dict(
                        os.environ,{'CLAUDE_CODE_OAUTH_TOKEN':'synthetic-secret','ANTHROPIC_CUSTOM_KEY':'synthetic-secret','HUB2_TOKEN':'synthetic-host'}):
                    receipt=adapter.launch('hub2-managed',spec)
                self.assertEqual(receipt['session_id'],'11111111-2222-4333-8444-555555555555')
                self.assertEqual(captured[0][:3],['synthetic-a','task','run'])
                self.assertEqual(captured[0][captured[0].index('--profile')+1],'hub-hub-claude')
                self.assertIn('--engine-arg=opus',captured[0])
                prompt_path=Path(captured[0][captured[0].index('--prompt-file')+1])
                context_marker=next(arg for arg in captured[0] if arg.startswith('--engine-arg=--hub-managed-claude-context='))
                context_path=Path(context_marker.split('=',2)[2])
                self.assertNotEqual(prompt_path,context_path)
                expected=nl.aplexer_prompt('real task'+nl.FIRST_ACTION_INSTRUCTION.format(
                    path=workspace/'.local/first-action-hub2-managed.json')).encode()
                self.assertEqual(prompt_path.read_bytes(),expected)
                self.assertEqual(spec['_task_output']['prompt_sha256'],__import__('hashlib').sha256(expected).hexdigest())
                self.assertEqual(json.loads(context_path.read_bytes())['protocol'],'hub2-managed-claude-exec-v1')
                self.assertNotEqual(context_path.read_bytes(),expected)
                self.assertNotIn('provider_home',spec)


class CodexQuotaWrapperLaunchTests(unittest.TestCase):
    """Codex starts only through the maintained quota wrapper; refusals spawn nothing."""
    NATIVE_ID = 'd6cf9872-8597-48a5-be64-990cdef4db31'

    def setUp(self):
        base = Path(__file__).resolve().parents[3] / '.local/tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix='codex-wrapper-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.workspace = self.root / 'work'
        self.workspace.mkdir(mode=0o700)
        self.wrapper = self.write_wrapper('launch-codex.sh')
        patcher = mock.patch.dict(os.environ, {'CODEX_LAUNCH_WRAPPER': str(self.wrapper)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_wrapper(self, name, text=None, mode=0o700):
        path = self.root / name
        path.write_text(text or ('#!/usr/bin/env bash\nset -euo pipefail\nrepo=%s\n'
                                 'python3 "$repo/scripts/quota-gate.py"\nexec %s/codex "$@"\n' % (self.root, self.root)))
        path.chmod(mode)
        return path

    def launch(self, *, interactive, route=None, provider='codex', readiness=True, effort='high'):
        calls = []

        def runner(argv, **options):
            calls.append((argv, options))
            child = argv[argv.index('--') + 1:]
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                'id': self.NATIVE_ID, 'tag': 'task-t1', 'workspace': str(self.workspace), 'command': child}), '')
        route = route or ({'provider': provider} if interactive else {'provider': provider, 'profile': 'hub-shared-codex'})
        config = {'routes': {'codex': route}}
        if route.get('profile') is None:
            config['private_provider_homes'] = True
        adapter = nl.NativeLauncher(config, runner=runner)
        spec = {'workspace': str(self.workspace), 'engine': 'codex', 'model': 'gpt-6.1-sol', 'prompt': 'synthetic prompt',
                'interactive': interactive, 'effort': effort, 'timeout_seconds': 600,
                'codex_launch_wrapper': '/offer/supplied/wrapper'}
        proof = {'wrapper': str(self.wrapper.resolve()), 'profile': 'hub-shared-codex'} if readiness else None
        with mock.patch.object(adapter, 'codex_exec_readiness', return_value=proof), \
             mock.patch.object(adapter, 'record_evidence'):
            try:
                adapter.launch('t1', spec)
                error = None
            except RuntimeError as caught:
                error = str(caught)
        return calls, error

    def child(self, calls):
        argv = calls[0][0]
        return argv[argv.index('--') + 1:]

    def test_direct_headless_replaces_only_the_sdk_executable(self):
        calls, error = self.launch(interactive=False, route={'provider': 'codex'})
        self.assertIsNone(error)
        prompt = nl.aplexer_prompt('synthetic prompt' + nl.FIRST_ACTION_INSTRUCTION.format(
            path=self.workspace / '.local/first-action-t1.json'))
        expected = nl.headless_effort_argv('codex', nl.adapter_argv('codex', prompt, 'gpt-6.1-sol'), 'high')
        expected = nl._headless_insert_flags('codex', expected, nl.HEADLESS_OUTPUT_FLAGS.get('codex', ()))
        self.assertEqual(self.child(calls), [str(self.wrapper.resolve())] + expected[1:])
        self.assertNotIn('/offer/supplied/wrapper', ' '.join(calls[0][0]))

    def test_direct_interactive_replaces_only_the_sdk_executable(self):
        calls, error = self.launch(interactive=True, effort='medium')
        self.assertIsNone(error)
        prompt = nl.aplexer_prompt('synthetic prompt' + nl.FIRST_ACTION_INSTRUCTION.format(
            path=self.workspace / '.local/first-action-t1.json'))
        expected = nl.interactive_argv('codex', nl.adapter_argv('codex', prompt, 'gpt-6.1-sol'), prompt,
                                       model='gpt-6.1-sol', effort='medium')
        self.assertEqual(self.child(calls), [str(self.wrapper.resolve())] + expected[1:])
        self.assertIn('-c', self.child(calls))
        self.assertNotIn('exec', self.child(calls))

    def test_profile_route_keeps_task_run_and_propagates_only_the_verified_wrapper(self):
        calls, error = self.launch(interactive=False)
        self.assertIsNone(error)
        argv, options = calls[0]
        child = self.child(calls)
        self.assertEqual(child[1:3], ['task', 'run'])
        self.assertEqual(child[child.index('--profile') + 1], 'hub-shared-codex')
        self.assertNotIn(str(self.wrapper), child, 'the wrapper must not wrap or replace task run')
        self.assertIn('CODEX_LAUNCH_WRAPPER=' + str(self.wrapper.resolve()), argv[:argv.index('--')])
        self.assertNotIn('/offer/supplied/wrapper', ' '.join(argv))

    def test_missing_unsafe_or_incompatible_wrapper_refuses_before_spawn(self):
        raw = '#!/usr/bin/env bash\nset -euo pipefail\nexec %s/codex "$@"\n' % self.root
        cases = {'missing': str(self.root / 'absent.sh'), 'relative': 'launch-codex.sh', 'empty': '',
                 'world-writable': str(self.write_wrapper('open.sh', mode=0o707)),
                 'raw': str(self.write_wrapper('raw.sh', raw)),
                 'no-failclosed': str(self.write_wrapper('loose.sh', '#!/usr/bin/env bash\npython3 quota-gate.py\nexec %s/codex "$@"\n' % self.root))}
        for name, value in cases.items():
            for interactive in (False, True):
                with self.subTest(case=name, interactive=interactive), mock.patch.dict(os.environ, {'CODEX_LAUNCH_WRAPPER': value}):
                    calls, error = self.launch(interactive=interactive)
                    self.assertEqual(error, 'codex_launch_wrapper_unavailable')
                    self.assertEqual(calls, [])
        with mock.patch.object(nl.sys, 'platform', 'win32'):
            self.assertEqual(self.launch(interactive=True)[1], 'codex_launch_wrapper_unavailable')

    def test_profile_not_proven_wrapper_aware_refuses_before_spawn(self):
        calls, error = self.launch(interactive=False, readiness=False)
        self.assertEqual((calls, error), ([], 'codex_launch_wrapper_unavailable'))

    def test_custom_or_substituted_profile_never_proves_readiness(self):
        adapter = nl.NativeLauncher({})
        for profile in ('hub-other-codex', 'hub-codex-orin', None):
            self.assertIsNone(adapter.codex_exec_readiness({'provider': 'codex', 'profile': profile},
                                                           str(self.wrapper.resolve())))

    def test_other_providers_are_unchanged_even_with_a_bad_wrapper(self):
        with mock.patch.dict(os.environ, {'CODEX_LAUNCH_WRAPPER': '/nonexistent'}):
            calls, error = self.launch(interactive=False, route={'provider': 'zai'})
        self.assertIsNone(error)
        child = self.child(calls)
        self.assertEqual(child[0], 'zcodex')
        self.assertNotIn('CODEX_LAUNCH_WRAPPER', ' '.join(calls[0][0]))
        with mock.patch.dict(os.environ, {'CODEX_LAUNCH_WRAPPER': '/nonexistent'}):
            calls, error = self.launch(interactive=True, provider='zai')
        self.assertIsNone(error)
        self.assertEqual(self.child(calls)[0], 'zcodex')

    def test_capabilities_do_not_advertise_unusable_codex(self):
        config = {'routes': {'codex': {'provider': 'codex'}, 'opencode': {'provider': 'go'}}}
        with mock.patch.object(nl.shutil, 'which', side_effect=lambda b: '/bin/' + b):
            self.assertIn('codex', nl.NativeLauncher(config).capabilities())
            with mock.patch.dict(os.environ, {'CODEX_LAUNCH_WRAPPER': '/nonexistent'}):
                caps = nl.NativeLauncher(config).capabilities()
            self.assertNotIn('codex', caps)
            self.assertIn('opencode', caps)
            custom = {'routes': {'codex': {'provider': 'codex', 'profile': 'hub-custom'}}}
            self.assertNotIn('codex', nl.NativeLauncher(custom).capabilities())

    def test_packaged_archive_readiness_requires_wrapper_aware_pin_and_maintained_profile(self):
        if sys.platform != 'linux':
            self.skipTest('Linux wrapper')
        import hashlib
        import zipfile
        release = self.root / ('b' * 40)
        release.mkdir(mode=0o700)
        (release / 'bin').mkdir(mode=0o700)
        (release / 'bin/python').symlink_to(sys.executable)
        archive = release / 'hub.pyz'
        package = Path(__file__).resolve().parents[3] / 'host/agent_hub_host'

        def build(skip=()):
            with zipfile.ZipFile(archive, 'w') as out:
                for file in package.glob('*.py'):
                    out.write(file, 'agent_hub_host/' + file.name)
                out.writestr('__main__.py', 'raise RuntimeError("CLI main must never run")')
            archive.chmod(0o600)
            raw = archive.read_bytes()
            receipt = {'host_version': release.name, 'artifacts': {'hub.pyz': {
                'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}}}
            (release / 'release.json').write_text(json.dumps(receipt))
            (release / 'release.json').chmod(0o600)
        build()
        profile = self.root / 'profiles/config.toml'
        profile.parent.mkdir(mode=0o700)
        code = ('import sys,json;sys.path.insert(0,sys.argv[1]);'
                'from agent_hub_host.provisioning import module_command;'
                'sys.argv[0]=sys.argv[1];print(json.dumps(module_command("execute","shared-codex")))')
        command = json.loads(subprocess.check_output([str(release / 'bin/python'), '-I', '-S', '-c', code, str(archive)], text=True))

        def publish(argv, name='hub-shared-codex'):
            profile.write_text('[profiles."%s"]\nengine="codex"\ncommand=%s\n' % (name, json.dumps(argv)))
            profile.chmod(0o600)
        route = {'provider': 'codex', 'profile': 'hub-shared-codex'}
        wrapper = str(self.wrapper.resolve())
        results = []

        def probe(argv, **kwargs):
            results.append(subprocess.run(argv, **kwargs))
            return results[-1]
        adapter = nl.NativeLauncher({}, runner=probe)
        with mock.patch.dict(os.environ, {'APLEXER_CONFIG': str(profile)}):
            publish(command)
            ready = adapter.codex_exec_readiness(route, wrapper, release)
            self.assertIsNotNone(ready, results[-1].stderr if results else 'probe not reached')
            self.assertEqual(ready['wrapper'], wrapper)
            self.assertIsNone(adapter.codex_exec_readiness(route, str(self.root / 'other-wrapper'), release))
            publish([sys.executable, '-c', 'raise SystemExit(0)', 'shared-codex'])
            self.assertIsNone(adapter.codex_exec_readiness(route, wrapper, release), 'custom profile bypassing gated exec')
            publish(command, 'hub-custom-codex')
            self.assertIsNone(adapter.codex_exec_readiness(route, wrapper, release), 'profile substitution')
            publish(['/tmp/evil-python', *command[1:]])
            self.assertIsNone(adapter.codex_exec_readiness(route, wrapper, release), 'unverified interpreter')
            publish([command[0], '-c', command[2].replace('import os;', f'sys.path.insert(0, {repr(str(self.root / "old.pyz"))}); import os;'), 'shared-codex'])
            self.assertIsNone(adapter.codex_exec_readiness(route, wrapper, release), 'old archive inserted ahead')
            publish([command[0], '-c', command[2].replace('execute()', 'os.system("true"); execute()'), 'shared-codex'])
            self.assertIsNone(adapter.codex_exec_readiness(route, wrapper, release), 'additional statement')
            publish(command)
            # An old pin without the wrapper-aware readiness protocol is refused.
            with zipfile.ZipFile(archive, 'w') as out:
                out.writestr('agent_hub_host/__init__.py', '')
                out.writestr('agent_hub_host/provisioning.py', 'def module_command(*a):\n    return []\n')
            archive.chmod(0o600)
            raw = archive.read_bytes()
            (release / 'release.json').write_text(json.dumps({'host_version': release.name, 'artifacts': {
                'hub.pyz': {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}}}))
            self.assertIsNone(adapter.codex_exec_readiness(route, wrapper, release))

    def test_wrapper_rejects_bypass_scripts(self):
        if sys.platform != 'linux':
            self.skipTest('Linux wrapper')
        bypasses = {
            'shebang-semicolon': '#!/usr/bin/env bash; set -euo pipefail; echo quota-gate.py; exec /synthetic/codex "$@"\n',
            'echo-gate': '#!/usr/bin/env bash\nset -euo pipefail\necho quota-gate.py\nexec /synthetic/codex "$@"\n',
            'conditional-gate': '#!/usr/bin/env bash\nset -euo pipefail\nif false; then python3 /tmp/quota-gate.py; fi\nexec /synthetic/codex "$@"\n',
            'ignored-failure': '#!/usr/bin/env bash\nset -euo pipefail\npython3 /tmp/scripts/quota-gate.py || true\nexec /synthetic/codex "$@"\n',
            'shadowed-python': '#!/usr/bin/env bash\nset -euo pipefail\npython3() { :; }\nrepo=%s\npython3 "$repo/scripts/quota-gate.py"\nexec /synthetic/codex "$@"\n' % self.root,
            'reordered-statements': '#!/usr/bin/env bash\nrepo=%s\npython3 "$repo/scripts/quota-gate.py"\nset -euo pipefail\nexec /synthetic/codex "$@"\n' % self.root,
            'comment-vt-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\vpython3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'comment-ff-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\fpython3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'comment-nel-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\x85python3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'comment-ls-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\u2028python3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'comment-ps-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n# gate disabled\u2029python3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'ws-nbsp-smuggle': '#!/usr/bin/env bash\nset -euo pipefail\n\u00a0python3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'ctrl-cr': '#!/usr/bin/env bash\nset -euo pipefail\r\npython3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'ctrl-fs': '#!/usr/bin/env bash\nset -euo pipefail\n\x1cpython3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'ctrl-del': '#!/usr/bin/env bash\nset -euo pipefail\n# \x7f\npython3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
            'ctrl-bom': '#!/usr/bin/env bash\nset -euo pipefail\n\ufeffpython3 /synthetic/quota-gate.py\nexec /synthetic/codex "$@"\n',
        }
        for name, body in bypasses.items():
            bad = self.write_wrapper(f'{name}.sh', text=body)
            with mock.patch.dict(os.environ, {'CODEX_LAUNCH_WRAPPER': str(bad)}):
                with self.assertRaises(ValueError, msg=name):
                    nl.codex_launch_wrapper()
                calls, error = self.launch(interactive=False)
                self.assertEqual(error, 'codex_launch_wrapper_unavailable', name)
                self.assertEqual(calls, [], name)

    def test_wrapper_accepts_canonical_templates_with_shell_whitespace(self):
        if sys.platform != 'linux':
            self.skipTest('Linux wrapper')
        canonical = {
            'repo-template': (
                '#!/usr/bin/env bash\n'
                '  # maintained comment\n'
                '\tset -euo pipefail\n'
                f'repo={self.root}\n'
                'python3 "$repo/scripts/quota-gate.py"\n'
                f'exec {self.root}/codex "$@"\n'
            ),
            'direct-template': (
                '#!/bin/bash\n'
                'set -euo pipefail\n'
                f'\tpython3 "{self.root}/scripts/quota-gate.py"\n'
                f'exec {self.root}/codex "$@"\n'
            ),
        }
        for name, body in canonical.items():
            good = self.write_wrapper(f'{name}.sh', text=body)
            with mock.patch.dict(os.environ, {'CODEX_LAUNCH_WRAPPER': str(good)}):
                self.assertEqual(nl.codex_launch_wrapper(), good.resolve(), name)
