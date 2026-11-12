import json
import hashlib
import io
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_hub_host.agent import Bridge, Journal, offer_ack_frame, OfferAcknowledgement
from agent_hub_host.client import ApiError, Client, save_config
from agent_hub_host.cli import main as cli_main


class FakeNative:
    def __init__(self):
        self.calls, self.process, self.fail_send = [], None, False

    def __call__(self, args, **options):
        self.calls.append((args, options))
        command = args[1:]
        if "start" in command[:1]:
            child = args[args.index("--") + 1:]
            tag = args[args.index("--tag") + 1]
            created = int(time.time() * 1000) - 1000
            self.process = {"id": "1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d", "tag": tag,
                            "workspace": options["cwd"], "engine": "codex", "created_at_ms": created,
                            "command": child, "phase": "running"}
            # The native worker proves its first action with a whoami artifact.
            task = tag[len("task-"):]
            artifact = Path(options["cwd"]) / ".local" / f"first-action-{task}.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps({"id": self.process["id"], "tag": tag,
                "workspace": options["cwd"], "command": child, "phase": "working",
                "schema_version": 2, "worker_pid": os.getpid() + 7,
                "updated_at_ms": created + 500, "boot_id": "fixture"}))
            return subprocess.CompletedProcess(args, 0, json.dumps(self.process), "")
        if command[0] == "list":
            rows = [{**self.process, 'schema_version': 1}] if self.process else []
            return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")
        if command[0] == "status":
            return subprocess.CompletedProcess(args, 0 if self.process else 1, json.dumps(self.process), "")
        if command[0] == 'send' and '--strict' in command:
            if self.fail_send:
                raise subprocess.TimeoutExpired(args, options['timeout'])
            operation = command[command.index('--idempotency-key')+1]
            deadline = int(command[command.index('--deadline-ms')+1])
            digest = hashlib.sha256(json.dumps([command[1], operation,
                command[command.index('--expect-tag')+1], command[command.index('--expect-workspace')+1],
                int(command[command.index('--expect-created-at-ms')+1]), deadline, options['input']], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            receipt = {'version':1,'session_id':command[1],'operation_id':operation,'request_sha256':digest,
                'deadline_ms':deadline,'status':'submitted_verified','mutation':True,'landed':True,'submitted':True,
                'consumed':None,'reason':'submission_observed',**getattr(self,'strict_changes',{})}
            return subprocess.CompletedProcess(args, 0 if receipt['status']=='submitted_verified' else 4, json.dumps(receipt), '')
        if command[0] == "send" and self.fail_send:
            raise TimeoutError()
        if command[0] == "kill":
            self.process.update(phase="exited", exit={"code": 143, "exited_at_ms": int(time.time()*1000)}, containment_empty=True)
        return subprocess.CompletedProcess(args, 0, "hello", "")


class FakeClient:
    def __init__(self):
        self.calls, self.fail = [], False
        self.base_url='http://127.0.0.1:1'

    def request(self, method, path, body=None, key=None):
        if self.fail:
            raise ApiError("unavailable")
        self.calls.append((method, path, body, key))
        if path.endswith("/admission"):
            return {"launch_allowed": True, "epoch": body["epoch"],
                    "runtime_deadline": int((time.time()+600)*1000)}
        if path.endswith('/credentials'):
            return {'token':'worker-token-'+'x'*40,'expires_at':int((time.time()+30)*1000),'session_id':path.split('/')[3],
                    'operation_id':body['operation_id'],'epoch':body['epoch'],'machine_id':'machine-1',
                    'intake_id':'intake-1','project_id':'org/repo','actor':'session:'+path.split('/')[3]}
        return {}


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / "private/journal.sqlite")
        self.native, self.client = FakeNative(), FakeClient()
        self.bridge = Bridge(self.client, self.journal, [self.root], ["codex"],
                             executable=os.sys.executable, runner=self.native,
                             launcher_config={"private_provider_homes": True, "argv": [os.sys.executable], "config_dir": str(self.root / "launcher"),
                                              "routes": {"codex": {"provider": "codex"}}},machine_id='machine-1')
        # This synthetic native fixture explicitly implements both modes.
        self.bridge.launcher.capabilities=lambda:{'codex':{'headless','interactive'}}
        # Model-process fixtures isolate lifecycle semantics from real provider
        # APIs. test_providers separately verifies the actual materializer ABI.
        def provision_fixture(client,offer,launch,*args):
            launch['account_id']='synthetic-account'
            # Lifecycle fixtures isolate Bridge semantics from the provider gate:
            # no provider home means the adapter skips the final admission recheck.
            return None
        provisioning=patch('agent_hub_host.providers.provision',side_effect=provision_fixture)
        provisioning.start()
        self.addCleanup(provisioning.stop)
        # Existing lifecycle fixtures never inspect the operator's real login.
        auth = patch('agent_hub_host.agent.local_claude_missing_contexts', return_value=[])
        self.auth_observer = auth.start()
        self.addCleanup(auth.stop)

    def tearDown(self):
        self.journal.db.close()
        self.temp.cleanup()

    def test_heartbeat_local_source_is_mode_engine_account_bound_and_canonical_only(self):
        from agent_hub_host.native_launch import LOCAL_ROUTES
        captured = []
        self.client.heartbeat = lambda *args, **kw: captured.append((args, kw))
        self.bridge.launcher.config = {'routes': {k: dict(v) for k, v in LOCAL_ROUTES.items()}}
        self.bridge.engines = {'codex', 'claude', 'go', 'zai', 'antigravity', 'shell', 'unavailable'}
        self.bridge.launcher.capabilities = lambda: {'codex': {'headless', 'interactive'}, 'claude': {'headless'},
            'go': {'headless'}, 'zai': {'headless'}, 'antigravity': {'headless', 'interactive'}, 'shell': {'headless'}, 'codex-orin': {'headless', 'interactive'}}
        self.bridge.heartbeat(['worker'], include_native_sessions=False)
        flags = {v for v in captured[-1][0][2] if v.startswith('credentials:')}
        self.assertEqual(flags, {'credentials:local:headless:codex:codex', 'credentials:local:interactive:codex:codex',
            'credentials:local:headless:claude:claude', 'credentials:local:headless:go:shared-go',
            'credentials:local:headless:zai:zai-central', 'credentials:local:headless:antigravity:gemini',
            'credentials:local:interactive:antigravity:gemini'})
        self.assertNotIn('codex-orin', str(flags))
        self.assertLessEqual(len(captured[-1][0][2]), 32)
        self.bridge.launcher.config['private_provider_homes'] = True
        self.bridge.heartbeat([], include_native_sessions=False)
        self.assertFalse(any(v.startswith('credentials:') for v in captured[-1][0][2]))
        self.bridge.launcher.config['private_provider_homes'] = False
        self.bridge.launcher.config['routes']['codex']['profile'] = 'custom-secret-profile'
        self.bridge.launcher.config['routes']['antigravity']['account_id'] = 'custom-account'
        self.bridge.heartbeat([], include_native_sessions=False)
        flags = [v for v in captured[-1][0][2] if v.startswith('credentials:')]
        self.assertFalse(any(':codex:' in v or ':antigravity:' in v for v in flags))
        self.assertNotIn('custom-secret-profile', json.dumps(captured[-1]))
        self.assertNotIn('custom-account', json.dumps(captured[-1]))

    def test_scoped_missing_local_claude_keeps_engine_modes_and_unverified_source_hint(self):
        from agent_hub_host.native_launch import LOCAL_ROUTES
        captured = []
        self.client.heartbeat = lambda *args, **kwargs: captured.append(args)
        self.bridge.launcher.config = {'routes': {key: dict(value) for key, value in LOCAL_ROUTES.items()}}
        self.bridge.engines = {'claude'}
        self.bridge.launcher.capabilities = lambda: {'claude': {'headless', 'interactive', 'effort'}}
        self.auth_observer.return_value = ['auth:missing:l:h:claude:' + 'x'*43 + ':1800000000']
        self.bridge.heartbeat([], include_native_sessions=False)
        self.assertEqual(captured[-1][1], ['claude'])
        flags = captured[-1][2]
        self.assertIn('interactive:claude', flags)
        self.assertIn(self.auth_observer.return_value[0], flags)
        self.assertIn('credentials:local:headless:claude:claude', flags)
        self.assertIn('credentials:local:interactive:claude:claude', flags)
        self.assertLessEqual(len(flags), 32)
        self.auth_observer.return_value *= 2
        self.bridge.launcher.capabilities = lambda: {'claude': {'headless','interactive', *('cap'+str(i) for i in range(27))}}
        self.bridge.heartbeat([], include_native_sessions=False)
        self.assertEqual(len(captured[-1][2]),32)
        self.assertEqual(sum(flag.startswith('auth:missing:') for flag in captured[-1][2]),1)
        self.assertIn('credentials:local:headless:claude:claude',captured[-1][2])
        self.assertIn('credentials:local:interactive:claude:claude',captured[-1][2])
        self.bridge.launcher.capabilities = lambda: {'claude': {'headless','interactive', *('cap'+str(i) for i in range(40))}}
        self.bridge.heartbeat([], include_native_sessions=False)
        self.assertEqual(len(captured[-1][2]),32)
        self.assertFalse(any(flag.startswith('auth:missing:') for flag in captured[-1][2]))
        self.bridge.launcher.capabilities = lambda: {'claude': {'headless','interactive','effort'}}
        self.auth_observer.side_effect = RuntimeError('synthetic-secret')
        self.bridge.heartbeat([], include_native_sessions=False)
        self.assertIn('credentials:local:headless:claude:claude', captured[-1][2])
        self.assertFalse(any(flag.startswith('auth:missing:') for flag in captured[-1][2]))
        self.auth_observer.reset_mock()
        self.bridge.launcher.config['routes']['claude'] = {'provider': 'claude', 'profile': 'custom'}
        self.bridge.heartbeat([], include_native_sessions=False)
        self.auth_observer.assert_not_called()
        self.assertFalse(any(flag.startswith('credentials:') for flag in captured[-1][2]))

    def test_known_missing_observation_does_not_gate_explicit_launch_validation(self):
        self.bridge.engines.add('claude')
        self.bridge.launcher.capabilities = lambda: {'claude': {'headless'}}
        self.auth_observer.return_value = ['auth:missing:l:h:claude:' + 'x'*43 + ':1800000000']
        offer = self.offer()
        offer['launch'].update(engine='claude', model='sonnet')
        self.assertEqual(self.bridge.validate_launch(offer)['engine'], 'claude')
        self.auth_observer.assert_not_called()

    def test_local_source_modes_launch_without_central_home_injection(self):
        from agent_hub_host.native_launch import LOCAL_ROUTES
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                self.bridge.launcher.config = {'routes': {k: dict(v) for k, v in LOCAL_ROUTES.items()}}
                offer = self.offer()
                offer['session_id'] += '-interactive' if interactive else '-headless'
                offer['operation_id'] += '-interactive' if interactive else '-headless'
                offer['launch']['interactive'] = interactive
                with patch('agent_hub_host.providers.provision', side_effect=AssertionError('central home must not be provisioned')):
                    self.bridge.offer(offer)
                argv, options = next((args, kw) for args, kw in reversed(self.native.calls) if args[1] == 'start')
                child = argv[argv.index('--') + 1:]
                self.assertNotIn('CODEX_HOME=', ' '.join(argv))
                self.assertNotIn('CLAUDE_CONFIG_DIR=', ' '.join(argv))
                self.assertNotIn('HUB2_LAUNCH_CONTEXT', options['env'])
                if interactive:
                    self.assertEqual(child[0], 'codex')
                    self.assertNotIn('exec', child)
                else:
                    self.assertIn('task', child); self.assertIn('run', child)
                    self.assertEqual(child[child.index('--profile') + 1], 'hub-shared-codex')

    def test_existing_journal_schema_adds_persistent_runtime_deadline(self):
        path=self.root/'legacy-journal.sqlite'
        db=sqlite3.connect(path)
        db.execute("CREATE TABLE launches(operation_id TEXT PRIMARY KEY,session_id TEXT UNIQUE NOT NULL,epoch INTEGER NOT NULL,digest TEXT NOT NULL,workspace TEXT NOT NULL,tag TEXT NOT NULL,native_id TEXT,state TEXT NOT NULL,interactive INTEGER NOT NULL DEFAULT 0)")
        db.close()
        migrated=Journal(path)
        try:
            columns={row[1] for row in migrated.db.execute('PRAGMA table_info(launches)')}
            self.assertIn('runtime_deadline',columns)
            self.assertIn('workspace_repair_digest',columns)
        finally:
            migrated.db.close()

    def test_provider_refusal_stays_unclaimed_and_later_epoch_replaces_only_unspawned_intent(self):
        offer=self.offer()
        acknowledgements=[]
        with patch('agent_hub_host.providers.provision',side_effect=RuntimeError('provider_access_lifetime_insufficient')):
            with self.assertRaises(RuntimeError):
                self.bridge.offer(offer,lambda *args:acknowledgements.append(args))
        self.assertFalse(acknowledgements)
        self.assertFalse(any(path.endswith('/admission') for method,path,body,key in self.client.calls))
        self.assertFalse(any(argv[1]=='start' for argv,_ in self.native.calls))
        retried={**offer,'operation_id':offer['operation_id']+'-retry','epoch':offer['epoch']+1}
        self.bridge.offer(retried)
        row=self.journal.db.execute('SELECT * FROM launches').fetchone()
        self.assertEqual((row['operation_id'],row['epoch'],row['state']),(retried['operation_id'],retried['epoch'],'running'))
        self.assertEqual(sum(argv[1]=='start' for argv,_ in self.native.calls),1)

    def failed_native(self,exit_code=None,wrong_tag=False):
        identity='11111111-1111-4111-8111-111111111111'
        def launch(task,specification):
            specification['before_native_dispatch']()
            self.native.process={'id':identity,'tag':'foreign' if wrong_tag else 'task-'+task,'workspace':str(self.root),
                                 'phase':'running' if exit_code is None else 'exited','containment_empty':exit_code is not None}
            if exit_code is not None:self.native.process['exit']={'code':exit_code}
            raise RuntimeError('maintained_launcher_receipt_unverified')
        self.bridge.launcher.launch=launch
        self.bridge.launcher.aborted_identity=lambda task,workspace,attempt=None:identity
        with self.assertRaisesRegex(RuntimeError,'maintained_launcher_receipt_unverified'):
            self.bridge.offer(self.offer())
        return identity

    def test_unverified_native_result_aborts_only_owned_process_and_replays_exit_proof(self):
        identity=self.failed_native()
        self.bridge.reconcile()
        self.assertEqual(self.native.process['phase'],'exited')
        row=self.journal.db.execute('SELECT * FROM outbox').fetchone()
        body=json.loads(row['payload'])
        self.assertTrue(body['launch_failed'])
        self.assertEqual(body['native_id'],identity)
        self.assertEqual(body['exit_code'],143)
        self.assertTrue(body['evidence']['containment_empty'])
        self.assertFalse(any('/started' in item['path'] for item in self.journal.db.execute('SELECT * FROM outbox')))
        self.client.fail=True
        with self.assertRaises(ApiError):self.journal.flush(self.client)
        self.client.fail=False
        self.journal.flush(self.client)
        self.assertEqual(self.client.calls[-1][2],body)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())

    def test_reconcile_reports_bound_deleted_workspace_retirement(self):
        self.bridge.offer(self.offer())
        with self.journal.db:
            self.journal.db.execute("DELETE FROM outbox")
        row = self.journal.db.execute("SELECT * FROM launches").fetchone()
        workspace = Path(row["workspace"])
        # Keep the journal and retired registry in their original private root.
        missing = workspace / "deleted-worktree"
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET workspace=?", (str(missing),))
        state_dir = self.root / "native-state"
        retired = state_dir / "retired-sessions" / row["native_id"].lower()
        retired.mkdir(parents=True)
        self.bridge.launcher.state_dir = state_dir
        exit_info = {"code": 0, "signal": None, "exited_at_ms": row["runtime_deadline"] - 1}
        tombstone = {"id": row["native_id"], "tag": row["tag"], "workspace": str(missing) + " (deleted)",
                     "cause": "finished", "exit": exit_info, "containment_empty": True}
        self.native.process = None  # Active registry entry has already been removed.
        for recreated in (False, True):
            with self.subTest(recreated=recreated):
                if recreated:
                    missing.mkdir()
                    with self.journal.db:
                        self.journal.db.execute("DELETE FROM outbox")
                        self.journal.db.execute("UPDATE launches SET state='running',runtime_deadline=NULL")
                    tombstone.update(deadline_ms=row["runtime_deadline"], deadline_exceeded=False)
                (retired / "tombstone.json").write_text(json.dumps(tombstone))
                self.native.calls.clear()
                self.bridge.reconcile()
                self.bridge.reconcile()
                commands = [argv[1] for argv, _ in self.native.calls]
                self.assertEqual(commands, ["status"] if recreated else [])
                self.assertEqual(self.journal.db.execute("SELECT state FROM launches").fetchone()[0], "exited")
                receipts = self.journal.db.execute("SELECT path,payload FROM outbox").fetchall()
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0]["path"], "/api/sessions/" + row["session_id"] + "/finish")
                payload = json.loads(receipts[0]["payload"])
                self.assertEqual(payload["operation_id"], row["operation_id"])
                self.assertEqual(payload["epoch"], row["epoch"])
                self.assertEqual(payload["native_id"], row["native_id"])
                self.assertTrue(payload["native_exit_confirmed"])
                self.assertEqual(payload["evidence"]["source"], "aplexer_retirement")
                self.assertEqual(payload["evidence"]["native_identity"]["workspace"], str(missing))
                self.assertEqual(payload["evidence"]["native_exit"], exit_info)
                self.assertEqual(payload["evidence"]["native_deadline"],
                                 {"deadline_ms": row["runtime_deadline"], "exceeded": False})

    def origin_retirement_fixture(self):
        self.bridge.offer(self.offer(session_id='11111111-2222-4333-8444-555555555555'))
        row = self.journal.db.execute("SELECT * FROM launches").fetchone()
        manifest = json.loads((Path(row['workspace']) / '.local' / ('launch-hub2-' + row['session_id'] + '.json')).read_text())
        state = self.root / 'origin-native-state'
        retired = state / 'retired-sessions' / row['native_id']
        retired.mkdir(parents=True, mode=0o700, exist_ok=True)
        now = int(time.time()*1000)
        (retired / 'tombstone.json').write_text(json.dumps({'id': row['native_id'], 'tag': row['tag'],
            'workspace': str(self.root / 'different-cwd'), 'created_at_ms': manifest['created_at_ms'],
            'cause': 'finished', 'containment_empty': True, 'finished_at_ms': now,
            'exit': {'code': 127, 'signal': None, 'exited_at_ms': now}}))
        (retired / 'tombstone.json').chmod(0o600)
        self.bridge.launcher.state_dir = state
        self.native.process = None
        with self.journal.db:
            self.journal.db.execute('DELETE FROM outbox')
        self.native.calls.clear()
        return row, manifest, retired

    def test_reconcile_origin_retirement_preserves_failed_exit_without_native_effects(self):
        row, manifest, retired = self.origin_retirement_fixture()
        self.bridge.reconcile()
        self.bridge.reconcile()
        self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0], 'exited')
        self.assertEqual([args[1] for args, _ in self.native.calls], ['status'])
        payload = json.loads(self.journal.db.execute('SELECT payload FROM outbox').fetchone()[0])
        self.assertEqual(payload['exit_code'], 127)
        self.assertEqual(payload['operation_id'], row['operation_id'])
        self.assertEqual(payload['epoch'], row['epoch'])
        self.assertEqual(payload['evidence']['native_identity']['workspace'], row['workspace'])
        self.assertEqual(payload['evidence']['retirement_origin']['observed_workspace'], str(self.root / 'different-cwd'))
        self.assertEqual(payload['evidence']['retirement_origin']['created_at_ms'], manifest['created_at_ms'])

    def test_origin_retirement_restart_keeps_one_durable_failed_finish(self):
        row, _, _ = self.origin_retirement_fixture()
        self.bridge.reconcile()
        self.journal.db.close()
        self.journal = Journal(self.root / 'private/journal.sqlite')
        restarted = Bridge(self.client, self.journal, [self.root], ['codex'],
                           executable=os.sys.executable, runner=self.native)
        self.native.calls.clear()
        restarted.reconcile()
        self.assertEqual(self.native.calls, [])
        receipts = self.journal.db.execute('SELECT payload FROM outbox').fetchall()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0][0])['exit_code'], 127)

    def test_origin_retirement_oversized_finish_stays_unresolved(self):
        self.origin_retirement_fixture()
        original = self.bridge.launcher.retired_origin
        def enlarged(*args):
            result = original(*args)
            result['retirement_origin']['observed_workspace'] = '/' + 'x'*8192
            return result
        with patch.object(self.bridge.launcher, 'retired_origin', side_effect=enlarged):
            self.bridge.reconcile()
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
        self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0], 'running')

    def test_origin_retirement_duplicate_native_journal_binding_is_unresolved(self):
        row, _, _ = self.origin_retirement_fixture()
        duplicate = dict(row, session_id='other-session', operation_id='other-operation')
        columns = list(duplicate)
        with self.journal.db:
            self.journal.db.execute('INSERT INTO launches ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                                    [duplicate[key] for key in columns])
        self.bridge.reconcile()
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
        self.assertEqual({v[0] for v in self.journal.db.execute('SELECT state FROM launches')}, {'running'})
        self.assertFalse(any(args[1] in ('kill','send','start') for args, _ in self.native.calls))

    def test_origin_retirement_rechecks_attempt_after_evidence_read(self):
        for field, value in [('epoch', 232), ('digest', 'f'*64), ('native_id', '00000000-0000-4000-8000-000000000000')]:
            with self.subTest(field=field):
                if self.journal.db.execute('SELECT 1 FROM launches').fetchone():
                    with self.journal.db:
                        self.journal.db.execute('DELETE FROM launches')
                self.native.process = None
                row, _, _ = self.origin_retirement_fixture()
                original = self.bridge.launcher.retired_origin
                def changed(*args):
                    result = original(*args)
                    with self.journal.db:
                        self.journal.db.execute('UPDATE launches SET '+field+'=?', (value,))
                    return result
                with patch.object(self.bridge.launcher, 'retired_origin', side_effect=changed):
                    self.bridge.reconcile()
                self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
                self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0], 'running')

    def test_origin_retirement_never_overrides_conflicting_live_identity(self):
        row, _, _ = self.origin_retirement_fixture()
        self.native.process = {'id': row['native_id'], 'tag': row['tag'], 'workspace': str(self.root/'foreign'), 'phase': 'running'}
        with patch.object(self.bridge.launcher, 'retired_origin', side_effect=AssertionError('must not qualify a live conflict')):
            with self.assertRaisesRegex(RuntimeError, 'native_identity_mismatch'):
                self.bridge.reconcile()
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
        self.assertEqual([args[1] for args, _ in self.native.calls], ['status'])

    def test_missing_workspace_without_bound_retirement_remains_unresolved(self):
        self.bridge.offer(self.offer())
        with self.journal.db:
            self.journal.db.execute("DELETE FROM outbox")
            self.journal.db.execute("UPDATE launches SET workspace=?,state='aborting'",
                                    (str(self.root / "deleted-worktree"),))
        self.native.calls.clear()
        self.bridge.launcher.state_dir = self.root / "empty-native-state"
        self.bridge.reconcile()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.journal.db.execute("SELECT state FROM launches").fetchone()[0], "aborting")
        self.assertFalse(self.journal.db.execute("SELECT 1 FROM outbox").fetchone())

    def test_zero_native_exit_still_reports_failed_launch_without_fabricating_exit(self):
        self.failed_native(exit_code=0)
        self.bridge.reconcile()
        body=json.loads(self.journal.db.execute('SELECT payload FROM outbox').fetchone()[0])
        self.assertTrue(body['launch_failed'])
        self.assertEqual(body['exit_code'],0)
        self.assertEqual(body['evidence']['native_exit']['code'],0)

    def test_real_signal_termination_preserves_raw_evidence_and_negative_returncode(self):
        self.failed_native(exit_code=0)
        raw={'code':None,'signal':1,'exited_at_ms':int(time.time()*1000)}
        self.native.process['exit']=raw
        self.bridge.reconcile()
        body=json.loads(self.journal.db.execute('SELECT payload FROM outbox').fetchone()[0])
        self.assertEqual(body['exit_code'],-1)
        self.assertEqual(body['evidence']['native_exit'],raw)
        self.assertIsNone(body['evidence']['native_exit']['code'])
        self.assertTrue(body['launch_failed'])

    def test_genuine_windows_signed_exit_is_preserved(self):
        self.failed_native(exit_code=-1073741510)
        self.bridge.reconcile()
        body=json.loads(self.journal.db.execute('SELECT payload FROM outbox').fetchone()[0])
        self.assertEqual(body['exit_code'],-1073741510)
        self.assertEqual(body['evidence']['native_exit']['code'],-1073741510)

    def test_unknown_or_contradictory_native_exit_remains_fenced(self):
        self.failed_native(exit_code=0)
        cases=[{'code':None,'signal':0},{'code':None,'signal':128},
               {'code':None,'signal':True},{'signal':1},{'code':0,'signal':1},
               {'code':None,'signal':None},{'code':False,'signal':None}]
        for raw in cases:
            with self.subTest(raw=raw):
                self.native.process['exit']=raw
                self.bridge.reconcile()
                self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
                self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0],'aborting')

    def test_unverified_foreign_identity_is_never_killed_or_reported_finished(self):
        self.failed_native(wrong_tag=True)
        with self.assertRaisesRegex(RuntimeError,'native_identity_mismatch'):
            self.bridge.reconcile()
        self.assertFalse(any('kill' in argv for argv,options in self.native.calls))
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())

    def test_scoped_context_is_private_before_spawn_without_metadata_token_leak(self):
        with patch.dict(os.environ,{'HUB2_TOKEN':'host-human-token','HUB2_SERVER':'https://host-secret.invalid'}):
            self.bridge.offer(self.offer())
        context=next((self.root/'.local/hub2-contexts').glob('*.json'))
        value=json.loads(context.read_text())
        self.assertEqual(value['worker']['intake_id'],'intake-1')
        self.assertEqual(value['worker']['actor'],'session:session-1')
        for args,options in self.native.calls:
            self.assertNotIn(value['token'],str(args))
            self.assertNotIn('host-human-token',str(options.get('env',{})))
        native_start=next(opt for arg,opt in self.native.calls if arg[1]=='start')
        self.assertNotIn('HUB2_TOKEN',native_start['env'])
        self.assertNotIn('HUB2_SERVER',native_start['env'])
        if os.name!='nt':self.assertEqual(context.stat().st_mode&0o777,0o600)

    def test_uncertain_scoped_issue_does_not_spawn_and_replays_same_fence(self):
        original=self.client.request
        failed=[False]
        def request(method,path,body=None,key=None):
            reply=original(method,path,body,key)
            if path.endswith('/credentials') and not failed[0]:failed[0]=True;raise ApiError('lost_response')
            return reply
        self.client.request=request
        with self.assertRaises(ApiError):self.bridge.offer(self.offer())
        self.assertFalse(any(args[1]=='start' for args,_ in self.native.calls))
        self.bridge.offer(self.offer())
        self.assertEqual(sum(args[1]=='start' for args,_ in self.native.calls),1)

    def offer(self, **changes):
        offer = {"operation_id": "operation-1", "session_id": "session-1", "epoch": 1,
                 'intake_id':'intake-1','project_id':'org/repo',
                 "expires_at": time.time() * 1000 + 60000,
                 "launch": {"workspace": str(self.root), "engine": "codex", "model": "test-model",
                                 "prompt": "Print hello; $(do not interpret as shell)", "interactive": False}}
        offer.update(changes)
        return offer

    def test_duplicate_offer_reconnect_launches_exactly_once(self):
        self.bridge.offer(self.offer())
        self.bridge.offer(self.offer())
        starts = [call for call, _ in self.native.calls if call[1]=='start']
        self.assertEqual(len(starts), 1)
        self.assertIn("start", starts[0])
        self.assertEqual(" ".join(starts[0]).count("Print hello; $(do not interpret as shell)"), 1)
        self.assertFalse(any(call[1] == "send" for call, _ in self.native.calls))
        self.assertEqual(len(self.journal.db.execute("SELECT * FROM outbox").fetchall()), 1)

    def test_registered_workspace_label_resolves_and_launches_native_process(self):
        workspace = self.root / "relay"
        workspace.mkdir()
        offer = self.offer()
        offer["launch"]["workspace"] = "relay"
        self.bridge.offer(offer)
        row = self.journal.db.execute("SELECT * FROM launches WHERE session_id=?", (offer["session_id"],)).fetchone()
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["native_id"], self.native.process["id"])
        self.assertEqual(row["workspace"], str(workspace.resolve()))
        self.assertEqual(self.native.process["workspace"], str(workspace.resolve()))
        self.assertTrue(any(call[1] == "start" for call, _ in self.native.calls))

    def test_label_resolution_is_bound_in_existing_ack_before_native_dispatch(self):
        workspace = self.root / 'relay'
        workspace.mkdir()
        offer = self.offer()
        offer['launch']['workspace'] = 'relay'
        acknowledgements = []
        def acknowledge(*args):
            self.assertFalse(any(call[1] == 'start' for call, _ in self.native.calls))
            row = self.journal.db.execute('SELECT * FROM launches').fetchone()
            self.assertEqual(row['workspace'], str(workspace.resolve()))
            acknowledgements.append(args)
        self.bridge.offer(offer, acknowledge)
        self.assertEqual(acknowledgements, [('session-1', 1, {
            'version': 1, 'requested_workspace': 'relay',
            'resolved_workspace': str(workspace.resolve())})])

    def test_label_reconnect_keeps_same_binding_and_root_change_cannot_rebind(self):
        workspace = self.root / 'relay'
        workspace.mkdir()
        offer = self.offer()
        offer['launch']['workspace'] = 'relay'
        acknowledgements = []
        self.bridge.offer(offer, lambda *args: acknowledgements.append(args))
        self.bridge.offer(offer, lambda *args: acknowledgements.append(args))
        self.assertEqual(acknowledgements[0], acknowledgements[1])
        self.assertEqual(sum(args[1] == 'start' for args, _ in self.native.calls), 1)
        replacement = self.root / 'other-root'
        (replacement / 'relay').mkdir(parents=True)
        self.bridge.roots = [replacement]
        with self.assertRaisesRegex(ValueError, 'offer_fence_conflict'):
            self.bridge.offer(offer, lambda *args: acknowledgements.append(args))
        self.assertEqual(len(acknowledgements), 2)

    def test_old_absolute_ack_arity_and_qualified_samefile_evidence_spelling_are_preserved(self):
        acknowledgements = []
        self.bridge.offer(self.offer(), lambda *args: acknowledgements.append(args))
        self.assertEqual(acknowledgements, [('session-1', 1)])
        if os.name == 'nt':
            alias = str(self.root).swapcase()
        else:
            link = self.root / 'same-directory-alias'
            link.symlink_to(self.root, target_is_directory=True)
            alias = str(link)
        self.native.process.update(workspace=alias, phase='exited', containment_empty=True,
                                   exit={'code': 0, 'exited_at_ms': int(time.time()*1000)})
        self.bridge.launcher.finalize = lambda *args: 'finished'
        self.bridge.reconcile()
        payload = json.loads(self.journal.db.execute("SELECT payload FROM outbox WHERE path LIKE '%/finish'").fetchone()['payload'])
        self.assertEqual(payload['evidence']['native_identity']['workspace'], str(self.root.resolve()))
        self.assertEqual(payload['native_id'], self.native.process['id'])

    def test_ack_unicode_encoding_fits_real_socket_utf16_limit_without_ascii_expansion(self):
        for workspace in ('/' + '\u754c' * 1023, 'C:\\' + '\U0001f642' * 510):
            binding = {'version': 1, 'requested_workspace': 'relay', 'resolved_workspace': workspace}
            frame = offer_ack_frame('session-1', 1, binding)
            self.assertEqual(json.loads(frame)['workspace_binding'], binding)
            self.assertLessEqual(len(frame.encode('utf-16-le')) // 2, 8192)
            self.assertLessEqual(len(frame.encode('utf-8')), 8192)
            self.assertNotIn('\\u754c', frame)

    def legacy_label_finished(self, epoch=3):
        workspace = self.root / 'relay'
        workspace.mkdir(exist_ok=True)
        offer = self.offer(epoch=epoch)
        offer['launch']['workspace'] = 'relay'
        self.bridge.offer(offer)
        self.journal.flush(self.client)
        self.native.process.update(phase='exited', containment_empty=True,
                                   exit={'code': 0, 'exited_at_ms': int(time.time()*1000)})
        self.bridge.reconcile()
        receipt = self.journal.db.execute("SELECT * FROM outbox WHERE key='finish:operation-1'").fetchone()
        recovery = {**offer, 'claimed': True, 'expires_at': None,
                    'recovery_native_id': self.native.process['id']}
        return recovery, dict(receipt)

    def test_exited_label_repair_reaches_pending_and_deadletter_via_dispatch_and_drain_once(self):
        recovery, receipt = self.legacy_label_finished()
        acknowledged, delivered = [], []
        original = self.client.request
        def request(method, path, body=None, key=None):
            if path == '/api/offers':
                return {'offers': [recovery]}
            if path.endswith('/finish'):
                delivered.append((json.dumps(body, sort_keys=True, separators=(',', ':')), key))
                if not acknowledged:
                    raise ApiError('old label comparison', status=409, code='unqualified_native_exit_evidence')
            return original(method, path, body, key)
        self.client.request = request
        with self.assertLogs('agent_hub_host.agent', level='ERROR'):
            self.journal.flush(self.client)
        prior_native_calls = list(self.native.calls)
        def acknowledge(*args):
            acknowledged.append(args)
            return True
        # The actual polling upgrade route, not an otherwise unreachable direct offer.
        self.bridge.drain(acknowledge)
        self.assertEqual(len(acknowledged), 1)
        self.assertEqual(acknowledged[0][2]['requested_workspace'], 'relay')
        self.assertEqual(delivered, [(receipt['payload'], receipt['key'])] * 2)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
        self.assertEqual(self.native.calls, prior_native_calls)
        # Retain the exact stale server offer across a process restart. Repair
        # must not recreate a receipt that has already been retried/delivered.
        path = self.journal.path
        self.journal.db.close()
        self.journal = Journal(path)
        self.bridge.journal = self.journal
        self.bridge.drain(acknowledge)
        self.assertEqual(len(delivered), 2)
        self.assertEqual(self.native.calls, prior_native_calls)

    def test_exited_label_never_acknowledges_a_different_epoch_or_native(self):
        recovery, _ = self.legacy_label_finished()
        acknowledged = []
        for changed in ({'epoch': 2}, {'epoch': 4},
                        {'epoch': 4, 'claimed': False, 'expires_at': time.time()*1000+60000, 'recovery_native_id': None},
                        {'recovery_native_id': '99999999-2222-4333-8444-555555555555'}):
            with self.subTest(changed=changed), self.assertRaises((ValueError, RuntimeError)):
                candidate = {**recovery, **changed}
                if candidate.get('recovery_native_id') is None:
                    candidate.pop('recovery_native_id', None)
                self.bridge.offer(candidate, lambda *args: acknowledged.append(args))
        self.assertFalse(acknowledged)

    def test_pending_exited_label_repair_keeps_frozen_body_and_unrelated_deadletters(self):
        recovery, receipt = self.legacy_label_finished()
        unrelated = ('other-finish', '/api/sessions/foreign/finish', '{"private":"preserve"}', 409,
                     'unqualified_native_exit_evidence', 1)
        with self.journal.db:
            self.journal.db.execute('INSERT INTO outbox_dead_letters VALUES(?,?,?,?,?,?)', unrelated)
        calls = list(self.native.calls)
        acknowledged = []
        def acknowledge(*args):
            acknowledged.append(args)
            return True
        self.bridge.dispatch({'type': 'session.offer', 'offer': recovery}, None,
                             acknowledge)
        self.assertEqual(len(acknowledged), 1)
        self.assertEqual(dict(self.journal.db.execute('SELECT * FROM outbox').fetchone()), receipt)
        self.assertEqual(tuple(self.journal.db.execute('SELECT * FROM outbox_dead_letters').fetchone()), unrelated)
        self.assertEqual(self.native.calls, calls)

    def test_binding_echo_is_required_only_for_historical_repair_not_fresh_starts(self):
        recovery, receipt = self.legacy_label_finished()
        with self.journal.db:
            self.journal.db.execute('INSERT INTO outbox_dead_letters VALUES(?,?,?,?,?,?)',
                                    (receipt['key'], receipt['path'], receipt['payload'], 409, 'unqualified_native_exit_evidence', 1))
            self.journal.db.execute('DELETE FROM outbox')
        binding = {'version': 1, 'requested_workspace': 'relay', 'resolved_workspace': str((self.root/'relay').resolve())}
        calls = list(self.native.calls)
        for echo in (None, {**binding, 'resolved_workspace': '/wrong'}):
            acknowledgement = OfferAcknowledgement(binding)
            acknowledgement.accept({'type': 'ack', 'session_id': recovery['session_id'], 'epoch': recovery['epoch'],
                                    **({'workspace_binding': echo} if echo else {})})
            self.bridge.offer(recovery, lambda *args: acknowledgement.wait(0))
            self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
            self.assertIsNone(self.journal.db.execute('SELECT workspace_repair_digest FROM launches').fetchone()[0])
        acknowledgement = OfferAcknowledgement(binding)
        with self.assertRaisesRegex(RuntimeError, 'claim_ack_unverified'):
            self.bridge.offer(recovery, lambda *args: acknowledgement.wait(0))
        self.assertIsNone(self.journal.db.execute('SELECT workspace_repair_digest FROM launches').fetchone()[0])
        acknowledgement.accept({'workspace_binding': binding})
        self.bridge.offer(recovery, lambda *args: acknowledgement.wait(0))
        self.assertEqual(dict(self.journal.db.execute('SELECT * FROM outbox').fetchone()), receipt)
        self.assertEqual(self.native.calls, calls)
        # An old server's normal claim ACK still admits a fresh launch.
        fresh = self.offer(operation_id='fresh-op', session_id='fresh-session')
        fresh['launch']['workspace'] = 'relay'
        self.bridge.offer(fresh, lambda *args: False)
        self.assertEqual(sum(args[1] == 'start' for args, _ in self.native.calls), 2)

    def test_repair_crash_after_binding_echo_rolls_back_marker_and_retries_after_restart(self):
        recovery, receipt = self.legacy_label_finished()
        with self.journal.db:
            self.journal.db.execute('INSERT INTO outbox_dead_letters VALUES(?,?,?,?,?,?)',
                                    (receipt['key'], receipt['path'], receipt['payload'], 409, 'unqualified_native_exit_evidence', 1))
            self.journal.db.execute('DELETE FROM outbox')
            self.journal.db.execute("CREATE TRIGGER fail_repair_insert BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'synthetic crash'); END")
        calls = list(self.native.calls)
        # Actual socket dispatch contains the fault instead of poisoning the host connection.
        frames = []
        class Socket:
            def send(self, text):
                frames.append(json.loads(text))
        with self.assertLogs('agent_hub_host.agent', level='ERROR'):
            self.bridge.dispatch({'type': 'session.offer', 'offer': recovery}, Socket(), lambda *args: True)
        self.assertEqual(frames[-1]['type'], 'host.error')
        self.assertIsNone(self.journal.db.execute('SELECT workspace_repair_digest FROM launches').fetchone()[0])
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
        path = self.journal.path
        self.journal.db.close()
        self.journal = Journal(path)
        self.bridge.journal = self.journal
        self.journal.db.execute('DROP TRIGGER fail_repair_insert')
        self.journal.db.commit()
        self.bridge.offer(recovery, lambda *args: True)
        self.assertEqual(dict(self.journal.db.execute('SELECT * FROM outbox').fetchone()), receipt)
        self.assertEqual(self.native.calls, calls)

    def test_recovery_offer_missing_local_identity_never_dispatches_or_acknowledges(self):
        offer = self.offer(claimed=True, expires_at=None)
        offer['recovery_native_id'] = '11111111-2222-4333-8444-555555555555'
        acknowledged = []
        for claimed in (True, False):
            with self.assertRaisesRegex(ValueError, 'native_reconciliation_required'):
                self.bridge.offer({**offer, 'claimed': claimed}, lambda *args: acknowledged.append(args))
        self.assertEqual(self.native.calls, [])
        self.assertFalse(acknowledged)

    def test_unqualified_frozen_exit_and_other_permanent_failures_never_requeue(self):
        recovery, receipt = self.legacy_label_finished()
        original = json.loads(receipt['payload'])
        for field, value in (('epoch', 2), ('native_id', '99999999-2222-4333-8444-555555555555'),
                             ('native_exit_confirmed', False), ('exit_code', True)):
            body = {**original, field: value}
            self.journal.db.execute('UPDATE outbox SET payload=?', (json.dumps(body),))
            self.journal.db.commit()
            called = []
            self.bridge.offer(recovery, lambda *args: called.append(args) or True)
            self.assertFalse(called)
        for field, value in (('containment_empty', False), ('native_identity', {'id': recovery['recovery_native_id'], 'tag': 'foreign', 'workspace': str(self.root/'relay')}),
                             ('native_exit', {'code': 0, 'exited_at_ms': int(time.time()*1000)+60000})):
            body = {**original, 'evidence': {**original['evidence'], field: value}}
            self.journal.db.execute('UPDATE outbox SET payload=?', (json.dumps(body),))
            self.journal.db.commit()
            called = []
            self.bridge.offer(recovery, lambda *args: called.append(args) or True)
            self.assertFalse(called)
        with self.journal.db:
            self.journal.db.execute('DELETE FROM outbox')
            self.journal.db.execute('INSERT INTO outbox_dead_letters VALUES(?,?,?,?,?,?)',
                                    (receipt['key'], receipt['path'], receipt['payload'], 409, 'stale_or_unmatched_finish', 1))
        called = []
        self.bridge.offer(recovery, lambda *args: called.append(args) or True)
        self.assertFalse(called)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
        with self.journal.db:
            self.journal.db.execute('DELETE FROM outbox_dead_letters')
            self.journal.db.execute('INSERT INTO outbox VALUES(?,?,?)',
                                    (receipt['key'], receipt['path'], '{"native_exit_confirmed":false,' + receipt['payload'][1:]))
        self.bridge.offer(recovery, lambda *args: called.append(args) or True)
        self.assertFalse(called)

    def test_one_failed_repair_keeps_original_deadletter_and_cannot_loop(self):
        recovery, receipt = self.legacy_label_finished()
        original = (receipt['key'], receipt['path'], receipt['payload'], 409, 'unqualified_native_exit_evidence', 1)
        with self.journal.db:
            self.journal.db.execute('INSERT INTO outbox_dead_letters VALUES(?,?,?,?,?,?)', original)
            self.journal.db.execute('DELETE FROM outbox')
        self.bridge.offer(recovery, lambda *args: True)
        old = self.client.request
        self.client.request = lambda *args, **kwargs: (_ for _ in ()).throw(ApiError('still refused', status=409, code='stale_or_unmatched_finish'))
        with self.assertLogs('agent_hub_host.agent', level='ERROR'):
            self.journal.flush(self.client)
        self.assertEqual(tuple(self.journal.db.execute('SELECT * FROM outbox_dead_letters').fetchone()), original)
        self.bridge.offer(recovery, lambda *args: True)
        self.assertFalse(self.journal.db.execute('SELECT 1 FROM outbox').fetchone())
        self.client.request = old

    def test_unavailable_workspace_label_fails_with_a_stable_reason(self):
        offer = self.offer()
        offer["launch"]["workspace"] = "missing-repo"
        with self.assertRaisesRegex(ValueError, "workspace_label_unavailable"):
            self.bridge.validate_launch(offer)
        self.assertFalse(any(call[1] == "start" for call, _ in self.native.calls))

    def test_dispatch_attempt_operation_id_preserved_in_receipt(self):
        operation = "d04dedbf-951e-4eaf-a41f-03eeb8514135:0"
        self.bridge.offer(self.offer(operation_id=operation))
        self.assertFalse(any(path.endswith("/admission") for _, path, _, _ in self.client.calls))
        self.journal.flush(self.client)
        self.assertEqual(self.client.calls[-1][2]["operation_id"], operation)

    def test_launcher_cwd_and_pwd_match_assigned_workspace(self):
        self.bridge.offer(self.offer())
        launches = [(call, options) for call, options in self.native.calls if call[1] == "start"]
        self.assertEqual(len(launches), 1)
        for _, options in launches:
            self.assertTrue(Path(options["cwd"]).samefile(self.root))
            self.assertTrue(Path(options["env"]["PWD"]).samefile(self.root))

    def test_expired_stale_changed_spec_and_workspace_escape_refused(self):
        with self.assertRaisesRegex(ValueError, "expired"):
            self.bridge.offer(self.offer(expires_at=0))
        self.bridge.offer(self.offer())
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.bridge.offer(self.offer(epoch=2))
        changed = self.offer()
        changed["launch"]["prompt"] = "different"
        with self.assertRaisesRegex(ValueError, "fence_conflict"):
            self.bridge.offer(changed)
        changed = self.offer()
        changed["launch"]["workspace"] = str(self.root.parent)
        with self.assertRaisesRegex(ValueError, "outside_allowlist"):
            self.bridge.offer(changed)

    def test_crash_between_native_start_and_receipt_does_not_relaunch(self):
        offer = self.offer()
        self.bridge.offer(offer)
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET native_id=NULL,state='launching'")
        with self.assertRaisesRegex(RuntimeError, 'native_abort_pending'):
            self.bridge.offer(offer)
        state = self.journal.db.execute("SELECT state FROM launches").fetchone()[0]
        self.assertEqual(state, 'aborting')
        self.assertEqual(sum(call[1] == "start" for call, _ in self.native.calls), 1)

    def test_crash_before_native_launch_remains_uncertain_not_duplicated(self):
        self.bridge.offer(self.offer())
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET native_id=NULL,state='launching'")
        self.native.process = None
        with self.assertRaises(RuntimeError):
            self.bridge.offer(self.offer())
        self.assertEqual(sum(call[1] == "start" for call, _ in self.native.calls), 1)

    def test_adapter_preparation_failure_dispatches_only_the_durable_receipt(self):
        offer = self.offer()
        sent = []
        socket = type("Socket", (), {"send": lambda _, data: sent.append(json.loads(data))})()
        with patch.object(self.bridge.launcher, "launch", side_effect=TypeError("adapter_preparation_failed")):
            self.bridge.dispatch({"type": "session.offer", "offer": offer}, socket, None)
        self.assertEqual(sent, [])
        self.assertEqual(self.native.calls, [])
        receipt = self.journal.db.execute("SELECT payload FROM outbox").fetchone()
        self.assertEqual(json.loads(receipt[0]), {"operation_id": offer["operation_id"], "epoch": offer["epoch"],
                                                "native_id": None, "launch_failed": True, "no_dispatch": True})

    def test_adapter_preparation_throttle_keeps_budget_pause_and_dispatch_proof(self):
        offer = self.offer()
        retry_at = int(time.time() * 1000) + 60000
        error = ApiError("budget throttled", status=429, code="budget_paused", retry_at=retry_at)
        with patch.object(self.bridge.launcher, "launch", side_effect=error):
            with self.assertRaises(ApiError):
                self.bridge.dispatch({"type": "session.offer", "offer": offer}, None, None)
        self.assertEqual(self.bridge.budget_paused_until, retry_at)
        self.assertEqual(self.native.calls, [])
        receipt = self.journal.db.execute("SELECT payload FROM outbox").fetchone()
        self.assertTrue(json.loads(receipt[0])["no_dispatch"])

    def test_missing_maintained_launcher_refuses_before_native_spawn(self):
        self.bridge.launcher.config = {}
        self.assertIsNone(self.bridge.offer(self.offer()))
        self.assertIsNone(self.native.process)
        self.assertTrue(json.loads(self.journal.db.execute("SELECT payload FROM outbox").fetchone()[0])["no_dispatch"])

    def test_accepted_claim_spawns_without_another_admission_rpc(self):
        real_request = self.client.request
        def request(method, path, *args):
            if path.endswith('/admission'):
                raise AssertionError('claim must be sufficient for launch')
            return real_request(method, path, *args)
        self.client.request = request
        self.bridge.offer(self.offer())
        self.assertIsNotNone(self.native.process)

    def test_claimed_reconnect_requires_local_intent_and_does_not_relaunch(self):
        self.bridge.offer(self.offer())
        self.bridge.offer(self.offer(claimed=True, expires_at=None))
        self.assertEqual(sum(call[1] == "start" for call, _ in self.native.calls), 1)
        with self.assertRaisesRegex(ValueError, "local_intent"):
            self.bridge.offer(self.offer(operation_id="unknown-op", session_id="unknown-session", claimed=True, expires_at=None))

    def test_finished_evidence_survives_failed_delivery_and_restart(self):
        self.bridge.offer(self.offer())
        self.native.process.update(phase="exited", exit={"code": 7}, containment_empty=True)
        self.bridge.reconcile()
        self.client.fail = True
        with self.assertRaises(ApiError):
            self.journal.flush(self.client)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)
        path = self.journal.directory / "journal.sqlite"
        self.journal.db.close()
        self.journal = Journal(path)
        self.client.fail = False
        self.journal.flush(self.client)
        finished = self.client.calls[-1][2]
        self.assertEqual(finished["exit_code"], 7)
        self.assertIs(finished["native_exit_confirmed"], True)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_deadline_evidence_keeps_actual_zero_exit_through_durable_replay(self):
        self.bridge.offer(self.offer())
        self.journal.db.execute("UPDATE launches SET runtime_deadline=123456")
        self.native.process.update(phase='exited',exit={'code':0,'exited_at_ms':123456},containment_empty=True)
        self.bridge.reconcile()
        self.client.fail=True
        with self.assertRaises(ApiError):self.journal.flush(self.client)
        self.client.fail=False
        self.journal.flush(self.client)
        finished=self.client.calls[-1][2]
        self.assertEqual(finished['exit_code'],0)
        self.assertEqual(finished['evidence']['native_deadline'],{'deadline_ms':123456,'exceeded':True})
        self.assertTrue(finished['native_exit_confirmed'])

    def test_running_process_is_killed_when_server_deadline_expires(self):
        self.bridge.offer(self.offer())
        deadline=int(time.time()*1000)-1
        self.journal.db.execute("UPDATE launches SET runtime_deadline=?",(deadline,))
        self.bridge.reconcile()
        self.assertTrue(any(args[1]=='kill' and args[2]==self.native.process['id'] for args,_ in self.native.calls))
        finish=json.loads(self.journal.db.execute("SELECT payload FROM outbox WHERE path LIKE '%/finish'").fetchone()[0])
        self.assertEqual(finish['evidence']['native_deadline'],{'deadline_ms':deadline,'exceeded':True})
        self.assertNotIn('launch_failed',finish)

    def test_missing_process_never_counts_as_finished(self):
        self.bridge.offer(self.offer())
        self.native.process = None
        self.bridge.reconcile()
        self.assertFalse(any("finish" in row["path"] for row in self.journal.db.execute("SELECT * FROM outbox")))

    def test_exit_with_live_descendants_never_confirms_completion(self):
        self.bridge.offer(self.offer())
        self.native.process.update(phase="exited", exit={"code": 0}, containment_empty=False)
        self.bridge.reconcile()
        self.assertFalse(any("finish" in row["path"] for row in self.journal.db.execute("SELECT * FROM outbox")))

    def test_mismatched_native_identity_refuses_finish(self):
        self.bridge.offer(self.offer())
        original = dict(self.native.process)
        for field, value in (("id", "other-native"), ("tag", "other-tag"), ("workspace", str(self.root.parent))):
            self.native.process = {**original, field: value, "phase": "exited", "exit": {"code": 0}, "containment_empty": True}
            with self.assertRaisesRegex(RuntimeError, "identity"):
                self.bridge.reconcile()
        self.assertFalse(any("finish" in row["path"] for row in self.journal.db.execute("SELECT * FROM outbox")))

    def test_command_exactly_once_fenced_and_payload_conflict(self):
        offer = self.offer()
        offer["launch"]["interactive"] = True
        self.bridge.offer(offer)
        command = {"id": "command-1", "session_id": "session-1", "epoch": 1, "kind": "send", "text": "hello"}
        self.assertEqual(self.bridge.command(command), self.bridge.command(command))
        self.assertEqual(sum(call[1] == "send" for call, _ in self.native.calls), 1)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.bridge.command({**command, "text": "different"})
        with self.assertRaisesRegex(ValueError, "fence"):
            self.bridge.command({**command, "id": "command-2", "epoch": 2})

    def test_headless_send_is_refused_without_native_effect(self):
        self.bridge.offer(self.offer())
        with self.assertRaisesRegex(ValueError, "headless"):
            self.bridge.command({"id": "send-1", "session_id": "session-1", "epoch": 1, "kind": "send", "text": "hello"})
        self.assertFalse(any(call[1] == "send" for call, _ in self.native.calls))

    def test_disconnect_after_command_effect_replays_ack_not_native_effect(self):
        offer = self.offer()
        offer["launch"]["interactive"] = True
        self.bridge.offer(offer)
        self.journal.flush(self.client)
        command = {"id": "command-replay", "session_id": "session-1", "epoch": 1, "kind": "send", "text": "hello"}
        self.bridge.command(command)
        self.client.fail = True
        with self.assertRaises(ApiError):
            self.journal.flush(self.client)
        path = self.journal.directory / "journal.sqlite"
        self.journal.db.close()
        self.journal = Journal(path)
        self.bridge.journal = self.journal
        self.bridge.command(command)
        self.client.fail = False
        self.journal.flush(self.client)
        self.assertEqual(sum(call[1] == "send" for call, _ in self.native.calls), 1)
        self.assertEqual(self.client.calls[-1][1], "/api/sessions/session-1/commands/command-replay/ack")
        self.assertEqual(self.client.calls[-1][2]["epoch"], 1)

    def test_uncertain_command_after_crash_never_blindly_reexecutes(self):
        offer = self.offer()
        offer["launch"]["interactive"] = True
        self.bridge.offer(offer)
        command = {"id": "uncertain-command", "session_id": "session-1", "epoch": 1, "kind": "send", "text": "hello"}
        self.bridge.command(command)
        with self.journal.db:
            self.journal.db.execute("UPDATE commands SET state='executing',result=NULL WHERE id=?", (command["id"],))
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.bridge.command(command)
        self.assertEqual(sum(call[1] == "send" for call, _ in self.native.calls), 1)

    def test_socket_reconnect_replayed_command_executes_once_before_and_after_ack_loss(self):
        class StopServe(BaseException):
            pass
        offer = self.offer()
        offer["launch"]["interactive"] = True
        self.bridge.offer(offer)
        command = {"id": "socket-command", "session_id": "session-1", "epoch": 1, "kind": "send", "text": "hello"}
        self.journal.flush(self.client)
        real_request = self.client.request
        lost_ack = [False]
        def request(method, path, body=None, key=None):
            result = real_request(method, path, body, key)
            if path.endswith("/socket-command/ack") and not lost_ack[0]:
                lost_ack[0] = True
                raise ApiError("response lost after acknowledgement persisted")
            return result
        self.client.request = request
        connections = []
        class Socket:
            def __init__(self, index):
                self.index, self.delivered = index, False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def recv(self, timeout=None):
                if self.index == 0:
                    raise RuntimeError("disconnect before command delivery")
                if not self.delivered:
                    self.delivered = True
                    return json.dumps({"type": "session.command", "command": command})
                raise StopServe()
            def send(self, text):
                pass
        def socket(_):
            value = Socket(len(connections))
            connections.append(value)
            return value
        self.client.socket, self.client.heartbeat = socket, lambda labels, engines, capabilities, native_sessions=None: None
        with patch("agent_hub_host.agent.time.sleep"), self.assertRaises(StopServe):
            self.bridge.serve("machine-1", [])
        self.assertEqual(len(connections), 3)
        self.assertEqual(sum(call[1] == "send" for call, _ in self.native.calls), 1)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_bad_application_event_reports_hold_without_dropping_other_work(self):
        sent = []
        class Socket:
            def send(self, value):
                sent.append(json.loads(value))
        self.bridge.dispatch({"type": "session.command", "command": {
            "id": "bad-command", "session_id": "unknown", "epoch": 1, "kind": "stop"}}, Socket(), None)
        self.assertEqual(sent[0]["type"], "host.error")
        self.assertEqual(sent[0]["reason"], "command_fence_conflict")
        self.assertEqual(self.native.calls, [])

    def insert_orphan_launch(self, session="session-orphan", operation="op-orphan:0", epoch=7, state="launching"):
        with self.journal.db:
            self.journal.db.execute(
                "INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) "
                "VALUES(?,?,?,?,?,?,NULL,?,0)",
                (operation, session, epoch, "digest", str(self.root), "task-hub2-" + session, state))

    def test_interrupted_pre_dispatch_emits_one_durable_failure_without_native_exit(self):
        session, operation, epoch = 'session-no-dispatch', 'op-no-dispatch', 7
        self.insert_orphan_launch(session, operation, epoch)
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET native_dispatch_started=0 WHERE operation_id=?", (operation,))
        self.bridge.reconcile()
        self.bridge.reconcile()
        row = self.journal.db.execute("SELECT state,native_id FROM launches WHERE operation_id=?", (operation,)).fetchone()
        self.assertEqual(tuple(row), ('stopped_before_dispatch', None))
        receipts = self.journal.db.execute("SELECT key,path,payload FROM outbox").fetchall()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['key'], f'no-dispatch:{operation}:{epoch}')
        self.assertEqual(receipts[0]['path'], f'/api/sessions/{session}/finish')
        self.assertEqual(json.loads(receipts[0]['payload']), {'operation_id':operation,'epoch':epoch,'native_id':None,
                         'launch_failed':True,'no_dispatch':True})
        self.assertEqual(self.native.calls, [])

    def test_ambiguous_dispatched_attempt_does_not_adopt_a_different_attempt_label(self):
        session, operation, epoch = 'session-attempt-label', 'op-attempt-label', 7
        self.insert_orphan_launch(session, operation, epoch, 'aborting')
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET native_dispatch_started=1 WHERE operation_id=?", (operation,))
        self.native.process = {'id':'11111111-1111-4111-8111-111111111111','tag':'task-hub2-'+session,
                               'workspace':str(self.root),'phase':'running','containment_empty':False,
                               'labels':{'hub.attempt':'previous-attempt'}}
        self.bridge.reconcile()
        row = self.journal.db.execute("SELECT state,native_id FROM launches WHERE operation_id=?", (operation,)).fetchone()
        self.assertEqual(tuple(row), ('aborting', None))
        self.assertFalse(any(args[1]=='kill' for args,_ in self.native.calls))
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],0)

        self.native.process['labels']['hub.attempt'] = self.bridge.attempt_label(session, operation, epoch)
        self.bridge.reconcile()
        row = self.journal.db.execute("SELECT state,native_id FROM launches WHERE operation_id=?", (operation,)).fetchone()
        self.assertEqual(tuple(row), ('exited', self.native.process['id']))
        self.assertTrue(any(args[1:3] == ['kill', self.native.process['id']] for args, _ in self.native.calls))

    def test_stop_without_native_evidence_is_unconfirmed(self):
        for state in ("launching", "aborting", "deadline_stopping"):
            with self.subTest(state=state):
                session = "session-" + state
                operation = "op-" + state
                command_id = "stop-" + state.replace("_", "-")
                self.insert_orphan_launch(session, operation, 7, state)
                result = self.bridge.command({"id": command_id, "session_id": session, "epoch": 7, "kind": "stop"})
                self.assertEqual(result, {"stopped": False, "error": "stop_unconfirmed",
                                          "reason": "stop_unconfirmed", "stage": "host_stop"})
                self.assertFalse(any(args[1] == "kill" for args, _ in self.native.calls))
                self.journal.flush(self.client)
                self.assertIn(("POST", f"/api/sessions/{session}/commands/{command_id}/ack?safety=operator_stop",
                               {"epoch": 7, "result": result}, f"command:{command_id}"), self.client.calls)

    def test_stop_fences_admitted_offer_before_dispatch(self):
        offer = self.offer()
        command = {"id": "stop-pending", "session_id": offer["session_id"], "epoch": offer["epoch"], "kind": "stop"}

        def stop_during_claim(session, epoch):
            result = self.bridge.command(command)
            self.assertEqual(result, {"stopped": True, "evidence": "dispatch_fenced"})

        self.assertIsNone(self.bridge.offer(offer, stop_during_claim))
        row = self.journal.db.execute("SELECT state,native_id FROM launches WHERE session_id=?", (offer["session_id"],)).fetchone()
        self.assertEqual(tuple(row), ("stopped_before_dispatch", None))
        self.assertIsNone(self.bridge.offer(offer))
        self.assertFalse(any(args[1] in ("start", "kill") for args, _ in self.native.calls))
        self.journal.flush(self.client)
        result = self.bridge.command(command)
        self.assertEqual(result, {"stopped": True, "evidence": "dispatch_fenced"})
        self.journal.flush(self.client)
        receipt = ("POST", f"/api/sessions/{offer['session_id']}/commands/stop-pending/ack?safety=operator_stop",
                   {"epoch": offer["epoch"], "result": result}, "command:stop-pending")
        self.assertEqual(self.client.calls.count(receipt), 2)

    def test_stop_can_fence_while_native_launcher_is_preparing(self):
        offer = self.offer()
        command = {"id": "stop-during-preparation", "session_id": offer["session_id"],
                   "epoch": offer["epoch"], "kind": "stop"}
        launch = self.bridge.launcher.launch

        def stop_then_launch(task_id, specification):
            result = self.bridge.command(command)
            self.assertEqual(result, {"stopped": True, "evidence": "dispatch_fenced"})
            return launch(task_id, specification)

        with patch.object(self.bridge.launcher, "launch", side_effect=stop_then_launch):
            self.assertIsNone(self.bridge.offer(offer))
        state = self.journal.db.execute("SELECT state FROM launches").fetchone()[0]
        self.assertEqual(state, "stopped_before_dispatch")
        self.assertFalse(any(args[1] in {"start", "kill"} for args, _ in self.native.calls))

    def test_stop_without_local_launch_fences_replayed_offer_and_leaves_tagged_process_alone(self):
        offer = self.offer()
        session, epoch = offer["session_id"], offer["epoch"]
        tag = "task-hub2-" + session
        self.native.process = {"id": "1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d", "tag": tag,
                               "workspace": str(self.root), "phase": "running", "containment_empty": False}
        command = {"id": "stop-untracked", "session_id": session, "epoch": epoch,
                   "kind": "stop", "pre_native": True, "tag": tag}
        result = self.bridge.command(command)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["error"], "stop_unconfirmed")
        self.assertEqual(self.native.process["phase"], "running")
        self.assertIsNone(self.bridge.offer(offer))
        self.assertIsNone(self.bridge.offer({**offer, "claimed": True}))
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.journal.db.execute("SELECT epoch FROM stop_fences WHERE session_id=?", (session,)).fetchone()[0], epoch)
        self.journal.flush(self.client)
        self.assertIn(("POST", f"/api/sessions/{session}/commands/stop-untracked/ack?safety=operator_stop",
                       {"epoch": epoch, "result": result}, "command:stop-untracked"), self.client.calls)

    def test_stale_pre_native_stop_cannot_kill_a_newer_epoch_launch(self):
        session = "session-epoch-fence"
        self.insert_orphan_launch(session, "op-epoch-2", epoch=2, state="launching")
        self.native.process = {"id": "1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d",
                               "tag": "task-hub2-" + session, "workspace": str(self.root),
                               "phase": "running", "containment_empty": False}
        with self.assertRaisesRegex(ValueError, "command_fence_conflict"):
            self.bridge.command({"id": "old-stop-epoch-1", "session_id": session,
                                 "epoch": 1, "kind": "stop", "pre_native": True,
                                 "workspace": str(self.root), "tag": "task-hub2-" + session})
        self.assertEqual(self.native.process["phase"], "running")
        self.assertEqual(self.native.calls, [])

    def test_legacy_unbound_launch_does_not_claim_or_stop_process_by_tag(self):
        self.insert_orphan_launch()
        self.native.process = {"id": "1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d",
                               "tag": "task-hub2-session-orphan", "workspace": str(self.root),
                               "phase": "running", "containment_empty": False}
        self.assertIsNone(self.bridge.live_sessions()[0]["native_id"])
        result = self.bridge.command({"id": "stop-orphan-live", "session_id": "session-orphan",
                                      "epoch": 7, "kind": "stop"})
        self.assertEqual(result["error"], "stop_unconfirmed")
        self.bridge.reconcile()
        row = self.journal.db.execute("SELECT state,native_id FROM launches WHERE session_id='session-orphan'").fetchone()
        self.assertEqual(tuple(row), ("aborting", None))
        self.assertEqual(self.native.process["phase"], "running")
        self.assertEqual([args[1:] for args, _ in self.native.calls], [['list', '--all', '--json']])

    def test_reconcile_closes_legacy_launch_with_unconfirmed_failure(self):
        self.insert_orphan_launch()
        self.bridge.reconcile()
        state = self.journal.db.execute(
            "SELECT state FROM launches WHERE session_id='session-orphan'").fetchone()[0]
        self.assertEqual(state, "launch_failed_unconfirmed")
        self.bridge.reconcile()
        row = self.journal.db.execute('SELECT * FROM launches').fetchone()
        payload = json.loads(row['legacy_failure_json'])
        self.assertEqual(payload['native_outcome'], 'unconfirmed')
        self.assertEqual(payload['failure_kind'], 'legacy_census_absent')
        self.assertEqual(payload['native_id'], None)
        self.assertNotIn('native_exit_confirmed', payload)
        self.assertNotIn('no_dispatch', payload)
        self.assertNotIn('exit_code', payload)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        self.client.fail = True
        with self.assertRaises(ApiError):
            self.journal.flush(self.client)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        self.client.fail = False
        self.journal.flush(self.client)
        self.assertEqual(self.client.calls[-1][2], payload)
        reopened = Journal(self.journal.path)
        try:
            saved = reopened.db.execute('SELECT state,legacy_failure_json FROM launches').fetchone()
            self.assertEqual(saved['state'], 'launch_failed_unconfirmed')
            self.assertEqual(json.loads(saved['legacy_failure_json']), payload)
        finally:
            reopened.close_thread()

    def test_legacy_no_match_closes_even_when_workspace_is_deleted(self):
        self.insert_orphan_launch(state='aborting')
        workspace = self.root / 'deleted'
        workspace.mkdir()
        with self.journal.db:
            self.journal.db.execute('UPDATE launches SET workspace=?', (str(workspace),))
        workspace.rmdir()
        self.bridge.reconcile()
        row = self.journal.db.execute('SELECT state,legacy_failure_json FROM launches').fetchone()
        self.assertEqual(row['state'], 'launch_failed_unconfirmed')
        self.assertEqual(json.loads(row['legacy_failure_json'])['evidence']['workspace'], str(workspace))
        self.assertFalse(any(args[1] in {'status', 'kill'} for args, _ in self.native.calls))

    def test_legacy_census_failure_retains_original_state_and_no_receipt(self):
        self.insert_orphan_launch(state='aborting')
        for raw in ('null', '{"sessions":[]}', '[{"id":"short"}]'):
            with self.subTest(raw=raw), patch.object(self.bridge.launcher, 'runner',
                    return_value=subprocess.CompletedProcess(['a'], 0, raw, '')):
                self.bridge.reconcile()
                self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0], 'aborting')
                self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        from agent_hub_host.native_inventory import collect_native_inventory
        row={'id':'11111111-1111-4111-8111-111111111111','tag':'other','workspace':str(self.root),'schema_version':1}
        with patch('agent_hub_host.native_inventory.MAX_CENSUS_ROWS',0), patch.object(self.bridge.launcher,'runner',
                return_value=subprocess.CompletedProcess(['a'],0,json.dumps([row]),'')):
            self.bridge.reconcile()
        self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0], 'aborting')

    def test_legacy_failure_receipt_and_terminal_journal_commit_together(self):
        self.insert_orphan_launch(state='aborting')
        inventory = self.bridge.launcher.inventory()
        row = self.journal.db.execute('SELECT * FROM launches').fetchone()
        with patch.object(self.journal, 'enqueue', side_effect=sqlite3.OperationalError('failed outbox write')):
            with self.assertRaises(sqlite3.OperationalError):
                self.bridge.settle_legacy_unobserved_launch(row, inventory)
        saved = self.journal.db.execute('SELECT state,legacy_failure_json FROM launches').fetchone()
        self.assertEqual(tuple(saved), ('aborting', None))
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET tag='wrong-tag'")
        self.bridge.reconcile()
        self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0], 'aborting')

    def test_legacy_resolution_cas_preserves_native_binding_and_inflight_attempt(self):
        for mutation in ('native_id', 'epoch', 'native_dispatch_started', 'inflight'):
            with self.subTest(mutation=mutation):
                with self.journal.db:
                    self.journal.db.execute('DELETE FROM launches')
                self.bridge._inflight.clear()
                self.insert_orphan_launch(state='aborting')
                inventory = self.bridge.launcher.inventory()
                original = self.journal.db.execute('SELECT * FROM launches').fetchone()
                if mutation == 'inflight':
                    self.bridge._inflight.add(original['session_id'])
                else:
                    value = {'native_id': '11111111-1111-4111-8111-111111111111',
                             'epoch': 8, 'native_dispatch_started': 1}[mutation]
                    with self.journal.db:
                        self.journal.db.execute('UPDATE launches SET '+mutation+'=?', (value,))
                self.bridge.settle_legacy_unobserved_launch(original, inventory)
                self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0], 'aborting')
                self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    def test_non_stop_command_still_refused_for_launch_orphan(self):
        self.insert_orphan_launch()
        sent = []
        class Socket:
            def send(self, value):
                sent.append(json.loads(value))
        self.bridge.dispatch({"type": "session.command", "command": {
            "id": "cap-orphan", "session_id": "session-orphan", "epoch": 7, "kind": "capture"}}, Socket(), None)
        self.assertEqual(sent[0]["type"], "host.error")
        self.assertEqual(sent[0]["reason"], "command_fence_conflict")

    def test_capture_reads_bound_headless_stdout_through_existing_command_fence(self):
        from agent_hub_host.task_output import prepare_output
        self.bridge.offer(self.offer())
        row=self.journal.db.execute('SELECT * FROM launches').fetchone()
        spec={'workspace':row['workspace'],'operation_id':row['operation_id'],'epoch':row['epoch']}
        task='hub2-'+row['session_id']
        binding=prepare_output(task,spec,'synthetic-profile','codex','synthetic prompt')
        directory=Path(row['workspace']).joinpath(*binding['parts'])
        record={'session_id':row['native_id'],'tag':row['tag'],'workspace':row['workspace'],
                'created_at_ms':1,'task_output':binding}
        (Path(row['workspace'])/'.local'/('launch-'+task+'.json')).write_text(json.dumps(record))
        start={'schema_version':1,'parent_session':{'id':row['native_id'],'tag':row['tag'],'workspace':row['workspace']},
               'cwd':row['workspace'],'output_dir':str(directory),'profile':'synthetic-profile','engine':'codex',
               'started_ms':int(time.time()*1000),'prompt_sha256':binding['prompt_sha256']}
        (directory/'START.json').write_text(json.dumps(start))
        (directory/'stdout.log').write_text('real child stdout')
        result=self.bridge.command({'id':'bound-capture','session_id':row['session_id'],'epoch':row['epoch'],'kind':'capture'})
        self.assertEqual(result['output'],'real child stdout')
        screen=self.bridge.command({'id':'screen-capture','session_id':row['session_id'],'epoch':row['epoch'],'kind':'capture','mode':'screen'})
        self.assertEqual(screen['output'],'hello')
        with self.assertRaisesRegex(ValueError,'command_fence_conflict'):
            self.bridge.command({'id':'wrong-epoch-capture','session_id':row['session_id'],'epoch':row['epoch']+1,'kind':'capture'})

    def test_capture_on_platform_without_safe_file_reads_uses_native_pane(self):
        self.bridge.offer(self.offer())
        with patch.object(os,'supports_dir_fd',set()):
            captured=self.bridge.command({'id':'fallback-capture','session_id':'session-1','epoch':1,'kind':'capture'})
        self.assertEqual(captured['output'],'hello')

    def test_no_shell_flags_and_fenced_capture_stop(self):
        self.bridge.offer(self.offer())
        capture = self.bridge.command({"id": "capture-1", "session_id": "session-1", "epoch": 1, "kind": "capture"})
        self.assertEqual(capture["output"], "hello")
        result = self.bridge.command({"id": "stop-1", "session_id": "session-1", "epoch": 1, "kind": "stop"})
        self.assertIs(result["stopped"], True)
        self.assertTrue(all(not options.get("shell") for _, options in self.native.calls))

    def test_private_config_and_external_http_refusal(self):
        path = self.root / "config/client.json"
        save_config(path, {"token": "secret"})
        self.assertEqual(json.loads(path.read_text()), {"token": "secret"})
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            Client("http://example.com", "secret")
        with self.assertRaises(ValueError):
            Client("https://user:pass@example.com", "secret")
        with self.assertRaises(ValueError):
            Client("https://example.com").request("GET", "//attacker.invalid/")

    def test_http_requests_identify_the_hub2_host_agent(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"{}"

        class Opener:
            def open(self, request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return Response()

        with patch("agent_hub_host.client.urllib.request.build_opener", return_value=Opener()):
            result = Client("https://hub.example", "secret-token").request("GET", "/api/health")

        self.assertEqual(result, {})
        self.assertEqual(captured["request"].get_header("User-agent"), "agent-hub2-host/2.0")
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(captured["timeout"], 30)

    def test_top_level_status_command_uses_the_hub_status_endpoint(self):
        output = io.StringIO()
        config = self.root / "operator.json"
        with patch("agent_hub_host.cli.Client", return_value=self.client), redirect_stdout(output):
            result = cli_main(["--config", str(config), "--server", "https://hub.example", "status"])

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "{}\n")
        self.assertEqual(self.client.calls, [("GET", "/api/status", None, None)])


if __name__ == "__main__":
    unittest.main()


class RetiredLookupRowTest(unittest.TestCase):
    def test_lookup_passes_plain_mapping_to_retired(self):
        import sqlite3
        from types import SimpleNamespace
        from agent_hub_host.agent import Bridge
        conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT 'tag-1' AS tag, '/ws' AS workspace").fetchone()
        seen = []
        bridge = SimpleNamespace(
            native=lambda *a, **k: SimpleNamespace(returncode=1, stdout=''),
            launcher=SimpleNamespace(retired=lambda selector, expected: seen.append(expected.get('tag'))))
        Bridge.lookup(bridge, 'sel', row)
        self.assertEqual(seen, ['tag-1'])


class ResourceHeartbeatLoopTest(unittest.TestCase):
    def test_beats_repeatedly_and_survives_failures(self):
        import threading
        from agent_hub_host.agent import run_heartbeat_loop
        calls, stop = [], threading.Event()
        def beat():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('hub unreachable')
            if len(calls) >= 3:
                stop.set()
        thread = threading.Thread(target=run_heartbeat_loop, args=(beat, stop, 0.01))
        thread.start(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(calls), 3)

    def test_interval_is_well_inside_the_hub_staleness_window(self):
        from agent_hub_host.agent import LIVE_SESSION_HEARTBEAT_SECONDS
        self.assertLessEqual(LIVE_SESSION_HEARTBEAT_SECONDS * 4, 300)

class NativeControlTests(unittest.TestCase):
    setUp = HostTests.setUp
    tearDown = HostTests.tearDown

    def command(self, kind='stop', **changes):
        native = '11111111-1111-4111-8111-111111111111'
        self.native.process = {'id': native, 'tag': 'unmanaged', 'workspace': str(self.root),
            'phase': 'running', 'state':'running', 'worker_alive':True, 'created_at_ms':42, 'schema_version': 1}
        command = {'id': '22222222-2222-4222-8222-222222222222', 'target_kind': 'native',
                'protocol': 1, 'machine_id': 'machine-1', 'native_id': native, 'kind': kind,
                'identity': {'id': native, 'tag': 'unmanaged', 'workspace': str(self.root)},
                'deadline_ms': round(time.time()*1000)+60000, **changes}
        if kind == 'send':
            text=changes.get('text','owned synthetic input')
            command.pop('text',None)
            digest=hashlib.sha256(json.dumps([native,command['id'],'unmanaged',str(self.root),42,command['deadline_ms'],text],
                ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            command['submission']={'operation_id':command['id'],'created_at_ms':42,'deadline_ms':command['deadline_ms'],
                'text':text,'request_sha256':digest}
        return command

    def test_stop_positive_exit_replays_after_lost_ack_without_second_kill(self):
        command = self.command()
        result = self.bridge.command(command)
        self.assertTrue(result['stopped'])
        self.assertEqual(result['evidence']['native_id'], command['native_id'])
        self.assertTrue(result['evidence']['containment_empty'])
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM launches').fetchone()[0], 0)
        self.client.fail = True
        with self.assertRaises(ApiError):
            self.journal.flush(self.client)
        self.assertEqual(self.bridge.command(command), result)
        self.assertEqual(sum(args[1] == 'kill' for args, _ in self.native.calls), 1)
        self.client.fail = False
        self.journal.flush(self.client)
        path = self.client.calls[-1][1]
        self.assertEqual(path, f"/api/machines/machine-1/native/{command['native_id']}/commands/{command['id']}/ack")
        self.assertNotIn('/api/sessions/', path)

    def test_unknown_uuid_and_wrong_workspace_never_kill(self):
        for case in ['absent', 'wrong-workspace', 'wrong-tag', 'wrong-uuid']:
            command = self.command(id='33333333-3333-4333-8333-33333333333' + str(len(self.native.calls) % 10))
            if case == 'absent':
                self.native.process = None
            elif case == 'wrong-workspace':
                self.native.process['workspace'] = '/unrelated-workspace'
            elif case == 'wrong-tag':
                self.native.process['tag'] = 'other'
            else:
                self.native.process['id'] = '44444444-4444-4444-8444-444444444444'
            result = self.bridge.command(command)
            self.assertIn('error', result)
        self.assertFalse(any(args[1] == 'kill' for args, _ in self.native.calls))

    def test_no_containment_proof_is_unconfirmed_not_stopped(self):
        command = self.command()
        original = self.native.__call__
        def runner(args, **kw):
            if args[1] == 'kill':
                return subprocess.CompletedProcess(args, 0, '', '')
            return original(args, **kw)
        self.bridge.launcher.runner = runner
        result = self.bridge.command(command)
        self.assertFalse(result['stopped'])
        self.assertEqual(result['error'], 'stop_unconfirmed')

    def test_capture_sanitized_bounded_and_does_not_create_managed_launch(self):
        command = self.command('capture', mode='screen')
        original = self.native.__call__
        def runner(args, **kw):
            if args[1] == 'capture':
                self.native.calls.append((args, kw))
                return subprocess.CompletedProcess(args, 0, '\x1b[31mhello\x00\x07\n' + 'é'*5000, '')
            return original(args, **kw)
        self.bridge.launcher.runner = runner
        result = self.bridge.command(command)
        self.assertLessEqual(len(result['output'].encode()), 4096)
        self.assertTrue(result['output'].startswith('hello\n'))
        self.assertNotIn('\x1b', result['output'])
        self.assertIn('--screen', self.native.calls[-1][0])
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM launches').fetchone()[0], 0)

    def test_send_expiry_and_managed_native_have_no_native_effect(self):
        for kind, extra, reason in [('send', {'deadline_ms': 1}, 'native_command_deadline'),
                                    ('stop', {'deadline_ms': 1}, 'native_command_deadline')]:
            command = self.command(kind, **extra)
            result = self.bridge.command(command)
            self.assertEqual(result['error'], reason)
            self.journal.db.execute('DELETE FROM commands')
        command = self.command()
        self.journal.db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state) VALUES('op','managed',1,'digest',?,?,?,'running')", (str(self.root), 'managed', command['native_id']))
        result = self.bridge.command(command)
        self.assertEqual(result['error'], 'native_target_managed')
        self.assertEqual(self.native.calls, [])

    def test_strict_send_is_journaled_before_effect_and_replay_after_restart_never_sends_twice(self):
        command=self.command('send',text='owned π input\nsecond line')
        original=self.native.__call__
        def checked_runner(args,**kw):
            if args[1]=='send':
                row=self.journal.db.execute('SELECT state FROM commands WHERE id=?',(command['id'],)).fetchone()
                self.assertEqual(row['state'],'executing')
                self.assertNotIn(command['submission']['text'],' '.join(args))
            return original(args,**kw)
        self.bridge.launcher.runner=checked_runner
        result=self.bridge.command(command)
        self.assertEqual(result['submission']['status'],'submitted_verified')
        self.assertIsNone(result['submission']['consumed'])
        self.assertEqual(set(command['identity']),{'id','tag','workspace'})
        self.client.fail=True
        with self.assertRaises(ApiError):self.journal.flush(self.client)
        self.journal.db.close()
        self.journal=Journal(self.root/'private/journal.sqlite');self.bridge.journal=self.journal
        self.bridge.launcher.journal=self.journal
        self.assertEqual(self.bridge.command(command),result)
        self.assertEqual(sum(args[1]=='send' for args,_ in self.native.calls),1)
        changed=self.command('send',text='changed immutable text')
        with self.assertRaisesRegex(ValueError,'command_id_conflict'):self.bridge.command(changed)
        self.client.fail=False;self.journal.flush(self.client)
        self.assertNotIn(command['submission']['text'],json.dumps(self.client.calls))

    def test_uncertain_strict_send_is_terminal_and_executing_restart_never_dispatches(self):
        command=self.command('send');self.native.fail_send=True
        result=self.bridge.command(command)
        self.assertEqual(result['error'],'native_send_uncertain')
        self.native.fail_send=False
        self.assertEqual(self.bridge.command(command),result)
        self.assertEqual(sum(args[1]=='send' for args,_ in self.native.calls),1)
        second=self.command('send',id='33333333-3333-4333-8333-333333333333')
        from agent_hub_host.native_launch import canonical
        with self.journal.db:self.bridge.begin_command(second['id'],hashlib.sha256(canonical(second).encode()).hexdigest())
        self.journal.db.close();self.journal=Journal(self.root/'private/journal.sqlite')
        self.bridge.journal=self.journal;self.bridge.launcher.journal=self.journal
        self.assertEqual(self.bridge.command(second)['error'],'native_command_outcome_uncertain')
        self.assertEqual(sum(args[1]=='send' for args,_ in self.native.calls),1)

    def test_strict_send_rechecks_managed_transition_after_census(self):
        command=self.command('send');collect=self.bridge.launcher.inventory
        def raced():
            observed=collect()
            self.journal.db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state) VALUES('op','managed',1,'digest',?,?,?,'running')",(str(self.root),'managed',command['native_id']))
            return observed
        self.bridge.launcher.inventory=raced
        self.assertEqual(self.bridge.command(command)['error'],'native_target_managed')
        self.assertFalse(any(args[1]=='send' for args,_ in self.native.calls))

    def test_executing_receipt_after_restart_is_uncertain_without_redispatch(self):
        command = self.command()
        from agent_hub_host.native_launch import canonical
        digest = hashlib.sha256(canonical(command).encode()).hexdigest()
        with self.journal.db:
            self.bridge.begin_command(command['id'], digest)
        self.journal.db.close()
        self.journal = Journal(self.root / 'private/journal.sqlite')
        self.bridge.journal = self.journal
        result = self.bridge.command(command)
        self.assertEqual(result['error'], 'native_command_outcome_uncertain')
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.bridge.command(command), result)

    def test_machine_and_command_digest_identity_fences(self):
        command = self.command(machine_id='other-host')
        with self.assertRaisesRegex(ValueError, 'invalid_native_command'):
            self.bridge.command(command)
        command = self.command('capture')
        self.bridge.command(command)
        with self.assertRaisesRegex(ValueError, 'command_id_conflict'):
            self.bridge.command({**command, 'kind': 'stop'})
        self.assertFalse(any(args[1] == 'kill' for args, _ in self.native.calls))


class RetainedManagedCaptureTests(unittest.TestCase):
    setUp=HostTests.setUp
    tearDown=HostTests.tearDown

    def seed(self,terminal=True):
        from agent_hub_host import task_output
        self.bridge.offer(HostTests.offer(self))
        row=self.journal.db.execute('SELECT * FROM launches').fetchone()
        task='hub2-'+row['session_id'];created=int(time.time()*1000)-1000
        spec={'workspace':str(self.root),'operation_id':row['operation_id'],'epoch':row['epoch']}
        binding=task_output.prepare_output(task,spec,'local-codex','codex','synthetic prompt')
        directory=self.root.joinpath(*binding['parts'])
        record={'session_id':row['native_id'],'workspace':str(self.root),'tag':row['tag'],'created_at_ms':created,'task_output':binding}
        start={'schema_version':1,'parent_session':{'id':row['native_id'],'workspace':str(self.root),'tag':row['tag']},
            'cwd':str(self.root),'output_dir':str(directory),'started_ms':created+1,'profile':'local-codex','engine':'codex','prompt_sha256':binding['prompt_sha256']}
        self.manifest=self.root/'.local'/('launch-'+task+'.json');self.manifest.write_text(json.dumps(record));self.manifest.chmod(0o600)
        (directory/'START.json').write_text(json.dumps(start));(directory/'START.json').chmod(0o600)
        self.stdout=directory/'stdout.log';self.stdout.write_text('retained actual synthetic output\n');self.stdout.chmod(0o664)
        if terminal:self.journal.db.execute("UPDATE launches SET state='exited'")
        request_digest=hashlib.sha256(json.dumps({'kind':'capture','text':None,'epoch':row['epoch']},separators=(',',':')).encode()).hexdigest()
        command={'id':'33333333-3333-4333-8333-333333333333','session_id':row['session_id'],'kind':'capture','epoch':row['epoch'],
            'capture_binding':{'native_id':row['native_id'],'operation_id':row['operation_id'],'request_digest':request_digest}}
        self.native.calls=[]
        return command

    def test_positive_finished_attempt_reads_retained_output_with_no_native_call_and_replays(self):
        command=self.seed();result=self.bridge.command(command)
        self.assertEqual(result['output'],'retained actual synthetic output\n')
        self.assertEqual(result['capture_binding'],command['capture_binding'])
        self.assertEqual(result['capture_source'],'retained_task_output')
        self.assertEqual(self.native.calls,[])
        self.stdout.write_text('changed after receipt')
        self.assertEqual(self.bridge.command(command),result)
        self.assertEqual(self.native.calls,[])
        self.assertEqual(self.journal.db.execute('SELECT state FROM launches').fetchone()[0],'exited')

    def test_missing_terminal_output_is_typed_and_never_falls_back_to_old_native_pane(self):
        command=self.seed();self.stdout.unlink()
        result=self.bridge.command(command)
        self.assertEqual(result['error'],'task_output_unavailable')
        self.assertNotIn('output',result);self.assertEqual(self.native.calls,[])
        self.assertEqual(self.bridge.command(command),result)

    def test_foreign_binding_epoch_and_screen_fail_before_any_native_or_output_read(self):
        command=self.seed()
        for key,value in [('native_id','foreign'),('operation_id','foreign'),('request_digest','0'*64)]:
            wrong={**command,'id':'44444444-4444-4444-8444-'+str(len(self.native.calls)).zfill(12),'capture_binding':{**command['capture_binding'],key:value}}
            self.journal.db.execute('DELETE FROM commands')
            self.assertEqual(self.bridge.command(wrong)['error'],'capture_failed')
        self.journal.db.execute('DELETE FROM commands')
        with self.assertRaisesRegex(ValueError,'command_fence_conflict'):self.bridge.command({**command,'epoch':command['epoch']+1})
        self.assertEqual(self.bridge.command({**command,'mode':'screen'})['error'],'capture_failed')
        self.assertEqual(self.native.calls,[])

    def test_interrupted_screen_capture_fails_once_without_native_retry_or_input_relaxation(self):
        from agent_hub_host.native_launch import canonical
        command=self.seed(terminal=False);command.pop('capture_binding');command['mode']='screen'
        digest=hashlib.sha256(canonical(command).encode()).hexdigest()
        with self.journal.db:self.bridge.begin_command(command['id'],digest)
        result=self.bridge.command(command)
        self.assertEqual(result,{'error':'capture_outcome_uncertain','reason':'capture_outcome_uncertain'})
        self.assertEqual(self.bridge.command(command),result)
        self.assertEqual(self.native.calls,[])
        self.assertEqual(self.journal.db.execute('SELECT state FROM commands').fetchone()[0],'done')
        self.assertEqual(json.loads(self.journal.db.execute('SELECT payload FROM outbox WHERE key=?',('command:'+command['id'],)).fetchone()[0])['result'],result)
        with self.assertRaisesRegex(ValueError,'command_id_conflict'):
            self.bridge.command({**command,'mode':None})
        for kind in ['send','stop']:
            other={**command,'id':'uncertain-'+kind,'kind':kind}
            with self.journal.db:self.bridge.begin_command(other['id'],hashlib.sha256(canonical(other).encode()).hexdigest())
            with self.assertRaisesRegex(RuntimeError,'command_outcome_uncertain'):self.bridge.command(other)
        self.assertEqual(self.native.calls,[])

    def test_initial_screen_capture_exception_is_durable_failure_and_replay_never_calls_native(self):
        command=self.seed(terminal=False);command.pop('capture_binding');command['mode']='screen'
        original=self.bridge.native;captures=[]
        def failing_capture(*args,**kwargs):
            if args[0]=='capture':
                captures.append(args);raise RuntimeError('private native error must not leak')
            return original(*args,**kwargs)
        self.bridge.native=failing_capture
        result=self.bridge.command(command)
        self.assertEqual(result,{'error':'capture_failed','reason':'capture_failed'})
        calls=list(self.native.calls)
        self.assertEqual(self.bridge.command(command),result)
        self.assertEqual(self.native.calls,calls);self.assertEqual(len(captures),1)

    def test_capture_lookup_failure_is_durable_and_never_retried(self):
        command=self.seed(terminal=False);command.pop('capture_binding');command['mode']='screen'
        with patch.object(self.bridge,'resolve_launch_native',side_effect=RuntimeError('private lookup error')) as lookup:
            self.assertEqual(self.bridge.command(command)['error'],'capture_failed')
            self.assertEqual(self.bridge.command(command)['error'],'capture_failed')
            self.assertEqual(lookup.call_count,1)
        self.assertEqual(self.native.calls,[])

    def test_interrupted_bound_terminal_capture_has_no_read_and_preserves_exact_binding(self):
        from agent_hub_host.native_launch import canonical
        command=self.seed();self.stdout.unlink()
        with self.journal.db:self.bridge.begin_command(command['id'],hashlib.sha256(canonical(command).encode()).hexdigest())
        with patch('agent_hub_host.task_output.capture_task_output',side_effect=AssertionError('must not reread')):
            result=self.bridge.command(command)
        self.assertEqual(result['error'],'capture_outcome_uncertain')
        self.assertEqual(result['capture_binding'],command['capture_binding'])
        self.assertEqual(result['capture_source'],'unavailable');self.assertNotIn('output',result)
        self.assertEqual(self.native.calls,[])

    def test_running_legacy_output_keeps_qualified_pane_capture_without_reexecution(self):
        command=self.seed(terminal=False);self.manifest.unlink()
        result=self.bridge.command(command)
        self.assertEqual(result['capture_source'],'native_pane')
        self.assertEqual([args[1] for args,_ in self.native.calls],['status','capture'])

    def test_completion_racing_running_snapshot_never_falls_back_to_retired_pane(self):
        command=self.seed(terminal=False);self.manifest.unlink()
        self.native.process['phase']='exited'
        result=self.bridge.command(command)
        self.assertEqual(result['error'],'task_output_unavailable')
        self.assertEqual([args[1] for args,_ in self.native.calls],['status'])

    def test_private_attempt_failure_never_uses_native_fallback_even_while_running(self):
        command=self.seed(terminal=False);self.stdout.parent.chmod(0o755)
        result=self.bridge.command(command)
        self.assertEqual(result['error'],'task_output_private_path_required')
        self.assertEqual(self.native.calls,[])

    def group_writable_parent_capture(self,terminal):
        command=self.seed(terminal=terminal)
        (self.root/'.local').chmod(0o775)
        (self.root/'.local/hub2-output').chmod(0o775)
        self.assertEqual(self.stdout.parent.stat().st_mode & 0o777,0o700)
        result=self.bridge.command(command)
        self.assertEqual(result.get('output'),'retained actual synthetic output\n')
        self.assertEqual(result['capture_source'],'retained_task_output')
        self.assertEqual(self.native.calls,[])

    def test_running_capture_allows_owned_group_writable_parents_and_private_attempt(self):
        self.group_writable_parent_capture(terminal=False)

    def test_terminal_capture_allows_owned_group_writable_parents_and_private_attempt(self):
        self.group_writable_parent_capture(terminal=True)


class ManagedClaudeHostTests(unittest.TestCase):
    setUp=HostTests.setUp
    tearDown=HostTests.tearDown
    offer=HostTests.offer

    def configure(self):
        from agent_hub_host.native_launch import LOCAL_ROUTES
        self.bridge.engines={'claude'}
        self.bridge.launcher.config={'private_provider_homes':True,'routes':{'claude':dict(LOCAL_ROUTES['claude'])}}
        self.bridge.launcher.capabilities=lambda:{'claude':{'headless','effort'}}
        self.bridge.launcher.managed_claude_readiness=lambda:{'sha256':'a'*64}
        self.client.base_url='https://hub.test'

    def managed_offer(self,**changes):
        result=self.offer(account_id='claude',request_digest='b'*64)
        result['launch'].update(engine='claude',model='opus',effort='medium',managed_credential_label='hub-claude')
        result.update(changes)
        return result

    def test_selector_survives_validation_with_exact_account_and_original_request_digest(self):
        self.configure();offer=self.managed_offer()
        launch=self.bridge.validate_launch(offer)
        self.assertEqual(launch['managed_credential_label'],'hub-claude')
        self.assertEqual(launch['account_id'],'claude')
        self.assertEqual(launch['request_digest'],'b'*64)
        for changed in ({'account_id':'other'},{'request_digest':'not-a-digest'}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):self.bridge.validate_launch(self.managed_offer(**changed))
        self.bridge.launcher.managed_claude_readiness=lambda:None
        with self.assertRaisesRegex(ValueError,'managed_exec_unavailable'):self.bridge.validate_launch(offer)

    def test_normal_heartbeat_rechecks_profile_readiness_without_restart_or_changing_base_caps(self):
        self.configure();captured=[];frames=[]
        self.client.heartbeat=lambda *args,**kw:captured.append(args)
        marker='managed:hub-claude:opus:medium:v1'
        self.bridge.launcher.managed_claude_readiness=lambda:None
        self.bridge.heartbeat([],include_native_sessions=False)
        before=captured[-1][2];self.assertNotIn(marker,before)
        self.bridge.launcher.managed_claude_readiness=lambda:{'sha256':'a'*64}
        self.bridge.heartbeat([],include_native_sessions=False)
        self.assertEqual(frames,[],'preconnect HTTP heartbeat owns no socket to bless')
        from types import SimpleNamespace
        socket=SimpleNamespace(send=lambda raw:frames.append(json.loads(raw)))
        self.bridge._managed_socket=socket
        self.bridge.publish_managed_readiness(socket)
        self.assertEqual(frames[-1],{'type':'managed.claude.ready','protocol':'hub2-managed-claude-exec-v1','ready':True})
        self.assertIn(marker,captured[-1][2]);self.assertEqual(set(captured[-1][2])-{marker},set(before))
        self.bridge.launcher.managed_claude_readiness=lambda:None
        self.bridge.heartbeat([],include_native_sessions=False);self.assertNotIn(marker,captured[-1][2])
        self.assertFalse(frames[-1]['ready'])

    def test_explicit_managed_attempt_uses_fixed_profile_without_global_private_home_or_replay(self):
        self.configure();offer=self.managed_offer()
        with patch('agent_hub_host.providers.provision') as provision:
            self.bridge.offer(offer);self.bridge.offer(offer)
        provision.assert_not_called()
        starts=[args for args,kw in self.native.calls if args[1]=='start']
        self.assertEqual(len(starts),1)
        child=starts[0][starts[0].index('--')+1:]
        self.assertEqual(child[child.index('--profile')+1],'hub-hub-claude')
        context_args=[a for a in child if a.startswith('--engine-arg=--hub-managed-claude-context=')]
        self.assertEqual(len(context_args),1)
        intent=json.loads(Path(context_args[0].split('=',2)[2]).read_text())
        self.assertEqual(intent['machine_id'],'machine-1');self.assertEqual(intent['request_digest'],'b'*64)
        self.assertEqual(intent['session_id'],offer['session_id']);self.assertEqual(intent['epoch'],offer['epoch'])
        self.assertNotIn('worker-token',repr(intent));self.assertNotIn('token',repr(intent))

    def test_capability_budget_preserves_existing_tags_and_does_not_probe_when_full(self):
        self.configure();captured=[]
        self.client.heartbeat=lambda *args,**kw:captured.append(args)
        tags=['reserved-'+str(i) for i in range(32)]
        self.bridge.advertisement.tags=lambda:tags
        with patch.object(self.bridge.launcher,'managed_claude_readiness') as ready:
            self.bridge.heartbeat([],include_native_sessions=False)
        self.assertEqual(captured[-1][2],tags);ready.assert_not_called()


class CaptureAckCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'private/journal.sqlite'
        self.journal=Journal(self.path)
        self.addCleanup(self.journal.close_thread)

    @staticmethod
    def payload(output):
        return {'epoch':7,'result':{'output':output,'capture_source':'retained_task_output',
            'capture_binding':{'native_id':'11111111-1111-4111-8111-111111111111',
                               'operation_id':'op-frozen','request_digest':'a'*64}}}

    def test_stage_a_emission_remains_legacy_small(self):
        from agent_hub_host.task_output import bounded_capture
        result=bounded_capture('\0'*65536)
        self.assertLessEqual(len(result.encode()),4096)
        self.assertLessEqual(len(json.dumps({'output':result},ensure_ascii=True).encode()),8000)

    def test_future_large_frozen_receipt_replays_after_unaccepted_or_lost_200(self):
        import urllib.error
        from unittest.mock import MagicMock
        path='/api/sessions/s/commands/c/ack'
        for lost_after_accept in [False,True]:
            with self.subTest(lost_after_accept=lost_after_accept):
                key='command:c'
                payload=self.payload('\0'*65536)
                self.journal.emit(key,path,payload)
                frozen=self.journal.db.execute('SELECT payload FROM outbox WHERE key=?',(key,)).fetchone()[0]
                requests=[];accepted=[]
                response=MagicMock();response.__enter__.return_value.read.return_value=b'{"acknowledged":true}'
                opener=MagicMock()
                def post(request,timeout):
                    requests.append(request.data)
                    self.assertGreater(len(request.data),65536)
                    self.assertLessEqual(len(request.data),401*1024)
                    if len(requests)==1:
                        if lost_after_accept:accepted.append(request.data)
                        raise urllib.error.URLError('synthetic response lost')
                    if not accepted:accepted.append(request.data)
                    self.assertEqual(request.data,accepted[0])
                    return response
                opener.open.side_effect=post
                with patch('agent_hub_host.client.urllib.request.build_opener',return_value=opener):
                    with self.assertRaises(ApiError):self.journal.flush(Client('https://hub.test'))
                    self.assertEqual(self.journal.db.execute('SELECT payload FROM outbox WHERE key=?',(key,)).fetchone()[0],frozen)
                    reopened=Journal(self.path)
                    try:reopened.flush(Client('https://hub.test'))
                    finally:reopened.close_thread()
                self.assertEqual(requests[0],requests[1])
                self.assertEqual(json.loads(requests[1]),json.loads(frozen))
                self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox WHERE key=?',(key,)).fetchone()[0],0)

    def test_capture_only_transport_budget_handles_unicode_and_controls(self):
        from unittest.mock import MagicMock
        for output in ['x'*65536,'\0'*65536,'😀'*16384]:
            response=MagicMock();response.__enter__.return_value.read.return_value=b'{}'
            opener=MagicMock();opener.open.return_value=response
            with patch('agent_hub_host.client.urllib.request.build_opener',return_value=opener):
                Client('https://hub.test').request('POST','/api/sessions/s/commands/c/ack',self.payload(output))
            raw=opener.open.call_args.args[0].data
            self.assertLessEqual(len(raw),401*1024)
            self.assertEqual(json.loads(raw)['result']['output'],output)

    def test_generic_and_non_capture_shapes_keep_existing_transport_limit(self):
        payload=self.payload('\0'*65536)
        for path in ['/api/heartbeat','/api/sessions/s/finish','/api/sessions/s/commands/c/ack?safety=operator_stop',
                     '/api/machines/h/native/n/commands/c/ack']:
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,'request too large'):
                Client('https://hub.test').request('POST',path,payload)
        for result in [{'output':'x'*65537},{'stopped':True,'output':'\0'*65536},
                       {'output':'\0'*65536,'capture_binding':{'unknown':'x'*2000}}]:
            with self.subTest(keys=list(result)),self.assertRaisesRegex(ValueError,'request too large'):
                Client('https://hub.test').request('POST','/api/sessions/s/commands/c/ack',{'epoch':7,'result':result})
