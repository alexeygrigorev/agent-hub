import json
import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host.agent import Bridge, Journal
from agent_hub_host.client import ApiError, Client, save_config
from agent_hub_host.launcher import Launcher


class FakeNative:
    def __init__(self):
        self.calls, self.process, self.fail_send = [], None, False

    def __call__(self, args, **options):
        self.calls.append((args, options))
        command = args[1:]
        if "submit" in command:
            self.mode=json.loads(args[args.index('--payload')+1])['mode']
            return subprocess.CompletedProcess(args, 0, '{"task_id":"test"}', "")
        if "reconcile" in command:
            return subprocess.CompletedProcess(args, 0, '{"native_status":"retired-finished","resources_reserved":false,"state":"completed-awaiting-review"}', "")
        if "run" in command:
            self.process = {"id": "native-123456", "phase": "running", "tag": "task-hub2-session-1",
                            "workspace": options["cwd"], "containment_empty": False}
            return subprocess.CompletedProcess(args, 0, json.dumps({'receipt':{'session_id':'native-123456','first_action_verified':True,'model_verified':True,'model':'test-model','mode':self.mode,'interactive_verified':self.mode=='interactive'}}), "")
        if "--config-dir" in command and "status" in command:
            if self.process:
                body = {"native_binding": {"native_id": "native-123456", "workspace": options["cwd"],
                        "tag": "task-hub2-session-1", "details": {"first_action_validated_at": "2026-01-01T00:00:00Z"}}}
            else:
                body = {"native_binding": {}}
            return subprocess.CompletedProcess(args, 0, json.dumps(body, indent=2), "")
        if "start" in command:
            self.process = {"id": "native-123456", "phase": "running"}
            return subprocess.CompletedProcess(args, 0, json.dumps(self.process), "")
        if command[0] == "status":
            return subprocess.CompletedProcess(args, 0 if self.process else 1, json.dumps(self.process), "")
        if command[0] == "send" and self.fail_send:
            raise TimeoutError()
        if command[0] == "kill":
            self.process.update(phase="exited", exit={"code": 143}, containment_empty=True)
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
            return {"launch_allowed": True, "epoch": body["epoch"]}
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
                             launcher_config={"argv": [os.sys.executable], "config_dir": str(self.root / "launcher"),
                                              "routes": {"codex": {"provider": "codex"}}},machine_id='machine-1')
        # This synthetic native fixture explicitly implements both modes.
        self.bridge.launcher.capabilities=lambda:{'codex':{'headless','interactive'}}
        # Model-process fixtures isolate lifecycle semantics from real provider
        # APIs. test_providers separately verifies the actual materializer ABI.
        def provision_fixture(client,offer,launch,*args):
            launch['account_id']='synthetic-account'
            return str(self.root/'synthetic-provider.json'),str(self.root/'synthetic-controller.json')
        provisioning=patch('agent_hub_host.providers.provision',side_effect=provision_fixture)
        provisioning.start()
        self.addCleanup(provisioning.stop)

    def tearDown(self):
        self.journal.db.close()
        self.temp.cleanup()

    def test_provider_refusal_stays_unclaimed_and_later_epoch_replaces_only_unspawned_intent(self):
        offer=self.offer()
        acknowledgements=[]
        with patch('agent_hub_host.providers.provision',side_effect=RuntimeError('provider_access_lifetime_insufficient')):
            with self.assertRaises(RuntimeError):
                self.bridge.offer(offer,lambda *args:acknowledgements.append(args))
        self.assertFalse(acknowledgements)
        self.assertFalse(any(path.endswith('/admission') for method,path,body,key in self.client.calls))
        self.assertFalse(any('run' in argv for argv,options in self.native.calls))
        retried={**offer,'operation_id':offer['operation_id']+'-retry','epoch':offer['epoch']+1}
        self.bridge.offer(retried)
        row=self.journal.db.execute('SELECT * FROM launches').fetchone()
        self.assertEqual((row['operation_id'],row['epoch'],row['state']),(retried['operation_id'],retried['epoch'],'running'))
        self.assertEqual(sum('run' in argv for argv,options in self.native.calls),1)

    def failed_native(self,exit_code=None,wrong_tag=False):
        identity='11111111-1111-4111-8111-111111111111'
        def launch(task,specification):
            self.native.process={'id':identity,'tag':'foreign' if wrong_tag else 'task-'+task,'workspace':str(self.root),
                                 'phase':'running' if exit_code is None else 'exited','containment_empty':exit_code is not None}
            if exit_code is not None:self.native.process['exit']={'code':exit_code}
            raise RuntimeError('maintained_launcher_receipt_unverified')
        self.bridge.launcher.launch=launch
        self.bridge.launcher.aborted_identity=lambda task,workspace:identity
        with self.assertRaisesRegex(RuntimeError,'native_launch_unverified'):
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

    def test_installed_capability_hashes_fence_old_or_changed_launcher(self):
        repository=self.root/'installed'
        files={}
        for name in ('launcher/launch.py','launcher/cli.py','launcher/interactive.py','launcher/provider_home.py'):
            target=repository/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('reviewed-source')
            files[name]=hashlib.sha256(target.read_bytes()).hexdigest()
        manifest=self.root/'capabilities.json'
        native=self.root/'aplexer';native.write_bytes(b'\x7fELFsynthetic-native-fixture')
        data={'version':3,'files':files,'native_sha256':hashlib.sha256(native.read_bytes()).hexdigest(),'providers':{'codex':['headless','interactive','goal','effort']}}
        manifest.write_text(json.dumps(data))
        selected=patch('agent_hub_host.launcher.shutil.which',return_value=str(native));selected.start();self.addCleanup(selected.stop)
        launcher=Launcher({'repository':str(repository),'capabilities_manifest':str(manifest),'routes':{'codex':{'model_requirements':{'providers':['codex']}}}})
        self.assertEqual(launcher.capabilities()['codex'],{'headless','interactive','goal','effort'})
        renamed=self.root/'aplexer-windows-amd64.exe';renamed.write_bytes(native.read_bytes())
        with patch('agent_hub_host.launcher.shutil.which',return_value=str(renamed)):
            self.assertEqual(launcher.capabilities()['codex'],set())
        native.write_bytes(b'\x7fELFchanged-native')
        self.assertEqual(launcher.capabilities()['codex'],set())
        native.write_bytes(b'\x7fELFsynthetic-native-fixture')
        manifest.write_text(json.dumps({**data,'version':2}))
        self.assertEqual(launcher.capabilities()['codex'],set())
        manifest.write_text(json.dumps(data))
        (repository/'launcher/launch.py').write_text('different installed version')
        self.assertEqual(launcher.capabilities()['codex'],set())

    def test_claimed_modes_need_positive_native_receipts(self):
        for requirements in ({'interactive':True},{'effort':'max'},{'with_goal':True}):
            with self.subTest(requirements=requirements),self.assertRaises(RuntimeError):
                Launcher.verify_modes({},requirements)

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
        native_start=next(options for args,options in self.native.calls if 'run' in args)
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
        self.assertFalse(any('run' in args for args,_ in self.native.calls))
        self.bridge.offer(self.offer())
        self.assertEqual(sum('run' in args for args,_ in self.native.calls),1)

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
        starts = [call for call, _ in self.native.calls if "run" in call]
        self.assertEqual(len(starts), 1)
        self.assertIn("run", starts[0])
        self.assertNotIn("Print hello", " ".join(starts[0]))
        self.assertFalse(any(call[1] == "send" for call, _ in self.native.calls))
        self.assertEqual(len(self.journal.db.execute("SELECT * FROM outbox").fetchall()), 1)

    def test_dispatch_attempt_operation_id_preserved_in_admission_and_receipt(self):
        operation = "d04dedbf-951e-4eaf-a41f-03eeb8514135:0"
        self.bridge.offer(self.offer(operation_id=operation))
        self.assertEqual(self.client.calls[0][2]["operation_id"], operation)
        self.journal.flush(self.client)
        self.assertEqual(self.client.calls[-1][2]["operation_id"], operation)

    def test_launcher_cwd_and_pwd_match_assigned_workspace(self):
        self.bridge.offer(self.offer())
        launches = [(call, options) for call, options in self.native.calls if "run" in call or "submit" in call]
        self.assertEqual(len(launches), 2)
        for _, options in launches:
            self.assertEqual(options["cwd"], str(self.root))
            self.assertEqual(options["env"]["PWD"], str(self.root.resolve()))

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
        self.assertEqual(sum("run" in call for call, _ in self.native.calls), 1)

    def test_crash_before_native_launch_remains_uncertain_not_duplicated(self):
        self.bridge.offer(self.offer())
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET native_id=NULL,state='launching'")
        self.native.process = None
        with self.assertRaises(RuntimeError):
            self.bridge.offer(self.offer())
        self.assertEqual(sum("run" in call for call, _ in self.native.calls), 1)

    def test_missing_maintained_launcher_refuses_before_native_spawn(self):
        self.bridge.launcher.config = {}
        with self.assertRaises(RuntimeError):
            self.bridge.offer(self.offer())
        self.assertIsNone(self.native.process)

    def test_server_admission_refusal_or_wrong_epoch_never_spawns(self):
        self.client.request = lambda *args: {"launch_allowed": True, "epoch": 99}
        with self.assertRaisesRegex(RuntimeError, "server_admission"):
            self.bridge.offer(self.offer())
        self.assertIsNone(self.native.process)
        self.assertFalse(any("run" in call for call, _ in self.native.calls))

    def test_claimed_reconnect_resumes_only_existing_durable_intent_with_fresh_admission(self):
        real_request = self.client.request
        self.client.request = lambda *args: {"launch_allowed": False}
        with self.assertRaisesRegex(RuntimeError, "server_admission"):
            self.bridge.offer(self.offer())
        self.assertIsNone(self.native.process)
        self.client.request = real_request
        self.bridge.offer(self.offer(claimed=True, expires_at=None))
        self.assertEqual(sum("run" in call for call, _ in self.native.calls), 1)
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
        self.native.process.update(phase='exited',exit={'code':0},containment_empty=True,
                                   deadline_ms=123456,deadline_exceeded=True)
        self.bridge.reconcile()
        self.client.fail=True
        with self.assertRaises(ApiError):self.journal.flush(self.client)
        self.client.fail=False
        self.journal.flush(self.client)
        finished=self.client.calls[-1][2]
        self.assertEqual(finished['exit_code'],0)
        self.assertEqual(finished['evidence']['native_deadline'],{'deadline_ms':123456,'exceeded':True})
        self.assertTrue(finished['native_exit_confirmed'])

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
        self.client.socket, self.client.heartbeat = socket, lambda labels, engines, capabilities: None
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


if __name__ == "__main__":
    unittest.main()
