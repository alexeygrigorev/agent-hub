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
            if "--deadline-ms" in args:
                self.process["deadline_ms"] = int(args[args.index("--deadline-ms") + 1])
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
            # Lifecycle fixtures isolate Bridge semantics from the provider gate:
            # no provider home means the adapter skips the final admission recheck.
            return None
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

    def test_dispatch_attempt_operation_id_preserved_in_admission_and_receipt(self):
        operation = "d04dedbf-951e-4eaf-a41f-03eeb8514135:0"
        self.bridge.offer(self.offer(operation_id=operation))
        self.assertEqual(self.client.calls[0][2]["operation_id"], operation)
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

    def test_server_admission_refusal_or_wrong_epoch_never_spawns(self):
        self.client.request = lambda *args: {"launch_allowed": True, "epoch": 99}
        with self.assertRaisesRegex(RuntimeError, "server_admission"):
            self.bridge.offer(self.offer())
        self.assertIsNone(self.native.process)
        self.assertFalse(any(call[1] == "start" for call, _ in self.native.calls))

    def test_claimed_reconnect_resumes_only_existing_durable_intent_with_fresh_admission(self):
        real_request = self.client.request
        self.client.request = lambda *args: {"launch_allowed": False}
        with self.assertRaisesRegex(RuntimeError, "server_admission"):
            self.bridge.offer(self.offer())
        self.assertIsNone(self.native.process)
        self.client.request = real_request
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
        self.client.socket, self.client.heartbeat = socket, lambda labels, engines, capabilities, resources=None: None
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


if __name__ == "__main__":
    unittest.main()
