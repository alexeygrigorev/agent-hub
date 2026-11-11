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

from agent_hub_host.agent import Bridge, Journal
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
        if command[0] == "status":
            return subprocess.CompletedProcess(args, 0 if self.process else 1, json.dumps(self.process), "")
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
            self.native.process={'id':identity,'tag':'foreign' if wrong_tag else 'task-'+task,'workspace':str(self.root),
                                 'phase':'running' if exit_code is None else 'exited','containment_empty':exit_code is not None}
            if exit_code is not None:self.native.process['exit']={'code':exit_code}
            raise RuntimeError('maintained_launcher_receipt_unverified')
        self.bridge.launcher.launch=launch
        self.bridge.launcher.aborted_identity=lambda task,workspace:identity
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
        self.bridge.offer(offer)
        self.assertEqual(sum(call[1] == "start" for call, _ in self.native.calls), 1)

    def test_crash_before_native_launch_remains_uncertain_not_duplicated(self):
        self.bridge.offer(self.offer())
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET native_id=NULL,state='launching'")
        self.native.process = None
        with self.assertRaises(RuntimeError):
            self.bridge.offer(self.offer())
        self.assertEqual(sum(call[1] == "start" for call, _ in self.native.calls), 1)

    def test_missing_maintained_launcher_refuses_before_native_spawn(self):
        self.bridge.launcher.config = {}
        with self.assertRaises(RuntimeError):
            self.bridge.offer(self.offer())
        self.assertIsNone(self.native.process)

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

    def test_stop_kills_native_process_recovered_by_session_tag(self):
        self.insert_orphan_launch()
        self.native.process = {"id": "1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d",
                              "tag": "task-hub2-session-orphan", "workspace": str(self.root),
                              "phase": "running", "containment_empty": False}
        result = self.bridge.command({"id": "stop-orphan-live", "session_id": "session-orphan",
                                      "epoch": 7, "kind": "stop"})
        self.assertTrue(result["stopped"])
        self.assertEqual(self.native.process["phase"], "exited")
        self.assertTrue(any(args[1] == "kill" and args[2] == self.native.process["id"]
                            for args, _ in self.native.calls))

    def test_live_snapshot_recovers_native_id_by_session_tag(self):
        self.insert_orphan_launch()
        self.native.process = {"id": "1b2f9c3e-4d5a-46b7-8c9d-0e1f2a3b4c5d",
                              "tag": "task-hub2-session-orphan", "workspace": str(self.root),
                              "phase": "running", "last_activity_ms": int(time.time() * 1000),
                              "containment_empty": False}
        sessions = self.bridge.live_sessions()
        self.assertEqual(sessions[0]["native_id"], self.native.process["id"])
        self.assertEqual(sessions[0]["phase"], "running")
        saved = self.journal.db.execute("SELECT native_id FROM launches WHERE session_id='session-orphan'").fetchone()[0]
        self.assertEqual(saved, self.native.process["id"])

    def test_reconcile_sweeps_launch_orphan_to_aborting(self):
        self.insert_orphan_launch()
        self.bridge.reconcile()
        state = self.journal.db.execute(
            "SELECT state FROM launches WHERE session_id='session-orphan'").fetchone()[0]
        self.assertEqual(state, "aborting")

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
