"""Native process receipts are journaled before delivery and replayed on reconnect."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from .client import ApiError, private_file, save_config
from .launcher import Launcher, native_returncode


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def same_workspace(left, right):
    """Compare workspace identity across equivalent OS path spellings.

    Windows can report a temporary directory through both its long path and
    its DOS 8.3 alias. Both names must refer to the same existing directory;
    textual normalization alone is not sufficient for the native identity
    fence.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        left_path, right_path = Path(left), Path(right)
        return left_path.is_absolute() and right_path.is_absolute() and left_path.samefile(right_path)
    except (OSError, RuntimeError, ValueError):
        return False


class Journal:
    def __init__(self, path):
        path = Path(path)
        self.directory = path.parent
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_file(path.parent)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        private_file(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS launches(
            operation_id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
            epoch INTEGER NOT NULL, digest TEXT NOT NULL, workspace TEXT NOT NULL,
            tag TEXT NOT NULL, native_id TEXT, state TEXT NOT NULL, interactive INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS outbox(
            key TEXT PRIMARY KEY, path TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS commands(
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
        """)
        if "interactive" not in {row[1] for row in self.db.execute("PRAGMA table_info(launches)")}:
            self.db.execute("ALTER TABLE launches ADD COLUMN interactive INTEGER NOT NULL DEFAULT 0")
        self.db.execute("CREATE INDEX IF NOT EXISTS launches_active ON launches(state,operation_id)")

    def emit(self, key, path, payload):
        with self.db:
            self.db.execute("INSERT INTO outbox VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET path=excluded.path,payload=excluded.payload",
                            (key, path, canonical(payload)))

    def flush(self, client, safety_only=False):
        query="SELECT * FROM outbox"
        if safety_only:
            query+=" WHERE path LIKE '/api/sessions/%/finish' OR path LIKE '/api/sessions/%/started' OR path LIKE '/api/sessions/%/commands/%/ack?safety=operator_stop'"
        for row in self.db.execute(query+" ORDER BY rowid LIMIT 100").fetchall():
            if safety_only and not re.fullmatch(r"/api/sessions/[A-Za-z0-9-]{1,80}/(?:finish|started|commands/[A-Za-z0-9-]{1,80}/ack\?safety=operator_stop)",row["path"]):
                continue
            client.request("POST", row["path"], json.loads(row["payload"]), row["key"])
            with self.db:
                self.db.execute("DELETE FROM outbox WHERE key=?", (row["key"],))


class Bridge:
    def __init__(self, client, journal, roots, engines, executable="a", runner=subprocess.run, launcher_config=None,machine_id=None):
        self.client, self.journal, self.runner = client, journal, runner
        self.roots = [Path(root).resolve(strict=True) for root in roots]
        self.engines = set(engines)
        self.launcher = Launcher(launcher_config or {}, runner)
        self.machine_id=machine_id
        self.budget_paused_until=0
        self.budget_safety_paused_until=0
        self.executable = shutil.which(executable) or str(Path(executable).resolve(strict=True))

    def native(self, *args, input=None, allow_failure=False, timeout=30):
        result = self.runner([self.executable, *args], input=input, text=True,
                             capture_output=True, timeout=timeout, check=False)
        if result.returncode and not allow_failure:
            raise RuntimeError("native_command_failed")
        if len(result.stdout) > 262144:
            raise RuntimeError("native_response_too_large")
        return result

    def lookup(self, selector, expected=None):
        result = self.native("status", selector, "--json", allow_failure=True)
        if result.returncode:
            return self.launcher.retired(selector, expected) if expected else None
        row = json.loads(result.stdout)
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise RuntimeError("invalid_native_status")
        if expected and (row["id"] != selector or row.get("tag") != expected["tag"]
                         or not same_workspace(row.get("workspace"), expected["workspace"])):
            raise RuntimeError("native_identity_mismatch")
        return row

    def validate_launch(self, offer):
        if (not re.fullmatch(r"[A-Za-z0-9-]{1,80}", str(offer.get("session_id", "")))
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", str(offer.get("operation_id", "")))):
            raise ValueError("invalid_offer_identity")
        epoch = offer.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("invalid_offer_epoch")
        if offer.get("claimed") is True:
            saved = self.journal.db.execute("SELECT session_id,epoch FROM launches WHERE operation_id=?", (offer["operation_id"],)).fetchone()
            if not saved or saved["session_id"] != offer["session_id"] or saved["epoch"] != epoch:
                raise ValueError("claimed_offer_without_local_intent")
        elif not isinstance(offer.get("expires_at"), (int, float)) or offer["expires_at"] <= time.time() * 1000:
            raise ValueError("offer_expired")
        launch = offer.get("launch")
        if not isinstance(launch, dict):
            raise ValueError("launch_specification_required")
        workspace = Path(str(launch.get("workspace", ""))).resolve(strict=True)
        if not workspace.is_dir() or not any(workspace.is_relative_to(root) for root in self.roots):
            raise ValueError("workspace_outside_allowlist")
        engine = launch.get("engine")
        if engine not in self.engines:
            raise ValueError("engine_not_allowed")
        prompt = launch.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 16384:
            raise ValueError("invalid_prompt")
        model = launch.get("model")
        if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,120}", model)):
            raise ValueError("invalid_model")
        timeout = launch.get("timeout_seconds", 1800)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 60 <= timeout <= 7200:
            raise ValueError("invalid_task_timeout")
        with_goal=launch.get('with_goal',False)
        effort=launch.get('effort')
        if type(with_goal) is not bool or (with_goal and not launch.get('interactive')):raise ValueError('invalid_goal_mode')
        if effort is not None and effort not in {'low','medium','high','xhigh','max'}:raise ValueError('invalid_effort')
        supported=self.launcher.capabilities().get(engine,set())
        if any(needed and capability not in supported for capability,needed in [('headless',not launch.get('interactive')),('interactive',launch.get('interactive')),('goal',with_goal),('effort',effort is not None)]):raise ValueError('launcher_capability_unavailable')
        return {"workspace": str(workspace), "engine": engine, "model": model,
                "prompt": prompt, "interactive": bool(launch.get("interactive", False)), "timeout_seconds": timeout,
                'with_goal':with_goal,'effort':effort}

    def child_context(self,offer,launch):
        if not self.machine_id:raise RuntimeError('host_identity_required')
        expected={key:offer[key] for key in ('session_id','operation_id','epoch','intake_id','project_id')}
        expected.update(machine_id=self.machine_id,actor='session:'+offer['session_id'])
        root=Path(launch['workspace'])
        directory=root/'.local/hub2-contexts'
        if not directory.resolve().is_relative_to(root):raise RuntimeError('child_context_path_untrusted')
        path=directory/(offer['session_id']+'-'+str(offer['epoch'])+'.json')
        if path.is_symlink():raise RuntimeError('child_context_path_untrusted')
        if path.exists():
            with path.open('rb') as stream:raw=stream.read(16385)
            if len(raw)>16384:raise RuntimeError('child_context_unverified')
            config=json.loads(raw)
        else:
            reply=self.client.request('POST',f"/api/sessions/{offer['session_id']}/credentials",{'epoch':offer['epoch'],'operation_id':offer['operation_id']})
            if any(reply.get(key)!=value for key,value in expected.items()):raise RuntimeError('child_context_binding_unverified')
            config={'server':self.client.base_url,'token':reply.get('token'),'expires_at':reply.get('expires_at',0)/1000,'worker':expected}
        if config.get('server')!=self.client.base_url or config.get('worker')!=expected or not isinstance(config.get('token'),str) or not 32<=len(config['token'])<=256 or not time.time()<config.get('expires_at',0)<=time.time()+launch['timeout_seconds']+5:
            raise RuntimeError('child_context_unverified')
        save_config(path,config)
        return str(path)

    def offer(self, offer, acknowledge=None):
        launch = self.validate_launch(offer)
        digest = hashlib.sha256(canonical(launch).encode()).hexdigest()
        operation, session, epoch = offer["operation_id"], offer["session_id"], offer["epoch"]
        db = self.journal.db
        previous=db.execute('SELECT * FROM launches WHERE session_id=?',(session,)).fetchone()
        if previous and previous['operation_id']!=operation:
            if previous['state']!='pending_admission' or previous['native_id'] or epoch<=previous['epoch']:
                raise ValueError('native_reconciliation_required')
            with db:
                db.execute('UPDATE launches SET operation_id=?,epoch=?,digest=?,workspace=?,tag=?,interactive=? WHERE session_id=?',
                           (operation,epoch,digest,launch['workspace'],'task-hub2-'+session,int(launch['interactive']),session))
        prior = db.execute("SELECT * FROM launches WHERE operation_id=?", (operation,)).fetchone()
        if prior:
            if prior["session_id"] != session or prior["digest"] != digest or epoch < prior["epoch"]:
                raise ValueError("offer_fence_conflict")
            if prior['state']=='exited':
                return prior['native_id']
            if epoch > prior["epoch"]:
                # A new fence cannot adopt a process under the old fence automatically.
                raise ValueError("native_reconciliation_required")
            if acknowledge and prior['state']!='pending_admission':
                acknowledge(session, epoch)
            native = None
            if prior['state']=='aborting':
                raise RuntimeError('native_abort_pending')
            if prior["state"] != "pending_admission":
                try:
                    native_id = prior["native_id"] or self.launcher.recover("hub2-" + session, prior["workspace"],launch)
                except RuntimeError:
                    if prior['state']=='launching':self.abort_launch(operation)
                    raise
                native = self.lookup(native_id, prior)
                if native and not prior["native_id"]:
                    with db:
                        db.execute("UPDATE launches SET native_id=?,state='running' WHERE operation_id=?", (native_id, operation))
            if not native and prior["state"] != "pending_admission":
                raise RuntimeError("launch_outcome_uncertain")
        else:
            task_id = "hub2-" + session
            tag = "task-" + task_id
            with db:
                db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) VALUES(?,?,?,?,?,?,NULL,'pending_admission',?)",
                           (operation, session, epoch, digest, launch["workspace"], tag, int(launch["interactive"])))
        if not prior or prior["state"] == "pending_admission":
            task_id, tag = "hub2-" + session, "task-hub2-" + session
            launch['worker_context']={key:offer[key] for key in ('session_id','epoch','intake_id','project_id')}
            launch['worker_context']['machine_id']=self.machine_id
            if launch['engine']!='shell':
                from .providers import provision
                launch['provider_home'],launch['controller_config']=provision(self.client,offer,launch,self.journal.directory,self.launcher,self.runner)
            if acknowledge:
                acknowledge(session, epoch)
            admission = self.client.request("POST", f"/api/sessions/{session}/admission",
                                            {"epoch": epoch, "operation_id": operation})
            if admission.get("launch_allowed") is not True or admission.get("epoch") != epoch:
                raise RuntimeError("server_admission_refused")
            launch['child_config_file']=self.child_context(offer,launch)
            launch['runtime_deadline']=round(json.loads(Path(launch['child_config_file']).read_text())['expires_at']*1000)
            with db:
                db.execute("UPDATE launches SET state='launching' WHERE operation_id=?", (operation,))
            try:
                receipt = self.launcher.launch(task_id, launch)
                native = self.lookup(receipt["session_id"], {"tag": tag, "workspace": launch["workspace"]})
                if not native or native["id"] != receipt["session_id"]:
                    raise RuntimeError("launch_outcome_uncertain")
            except (RuntimeError,ValueError,subprocess.SubprocessError,OSError):
                self.abort_launch(operation)
                raise RuntimeError('native_launch_unverified') from None
            with db:
                db.execute("UPDATE launches SET native_id=?,state='running' WHERE operation_id=?",
                           (native["id"], operation))
        row = db.execute("SELECT * FROM launches WHERE operation_id=?", (operation,)).fetchone()
        if row["state"] == "prompt_pending":
            raise RuntimeError("prompt_delivery_uncertain")
        if row["state"] == "exited":
            return native["id"]
        self.journal.emit(f"started:{operation}:{epoch}", f"/api/sessions/{session}/started",
                          {"operation_id": operation, "epoch": epoch, "native_id": native["id"]})
        return native["id"]

    def abort_launch(self,operation):
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET state='aborting' WHERE operation_id=? AND state='launching'",(operation,))

    def reconcile(self):
        rows = self.journal.db.execute("SELECT * FROM launches WHERE state IN ('running','aborting') LIMIT 1001").fetchall()
        if len(rows) > 1000:
            raise RuntimeError("active_native_bound_exceeded")
        for row in rows:
            if row['state']=='aborting' and not row['native_id']:
                try:identity=self.launcher.aborted_identity('hub2-'+row['session_id'],row['workspace'])
                except RuntimeError:identity=None
                if not identity:
                    candidate=self.lookup(row['workspace']+':'+row['tag'])
                    if candidate:
                        if candidate.get('tag')!=row['tag'] or not same_workspace(candidate.get('workspace'),row['workspace']):
                            raise RuntimeError('native_abort_identity_mismatch')
                        identity=candidate['id']
                if not identity:continue
                with self.journal.db:
                    self.journal.db.execute('UPDATE launches SET native_id=? WHERE operation_id=?',(identity,row['operation_id']))
                row=self.journal.db.execute('SELECT * FROM launches WHERE operation_id=?',(row['operation_id'],)).fetchone()
            native = self.lookup(row["native_id"], row)
            if not native:
                continue  # Missing process is uncertain, never a successful finish.
            if native.get("phase", native.get("state")) != "exited":
                if row['state']=='aborting':
                    self.native('kill',row['native_id'],allow_failure=True)
                    native=self.lookup(row['native_id'],row)
                if not native or native.get('phase',native.get('state'))!='exited':continue
            if native.get("containment_empty") is not True:
                continue
            exit_info = native.get("exit") or {}
            code = native_returncode(exit_info)
            if code is None:
                continue
            launcher_state=self.launcher.finalize('hub2-'+row['session_id'],row['workspace'])
            payload = {"operation_id": row["operation_id"], "epoch": row["epoch"],
                       "native_id": row["native_id"], "exit_code": code,
                       "native_exit_confirmed": True,
                       "evidence": {"source": "aplexer_retirement" if native.get("retirement") else "aplexer_status",
                                    "native_identity": {key: native[key] for key in ("id", "tag", "workspace")},
                                    "containment_empty": True, "native_exit": exit_info,"launcher_state":launcher_state}}
            if type(native.get('deadline_ms')) is int:
                payload['evidence']['native_deadline']={'deadline_ms':native['deadline_ms'],'exceeded':native.get('deadline_exceeded') is True}
            if row['state']=='aborting':payload['launch_failed']=True
            with self.journal.db:
                self.journal.db.execute("UPDATE launches SET state='exited' WHERE operation_id=?", (row["operation_id"],))
                self.journal.db.execute("INSERT OR REPLACE INTO outbox VALUES(?,?,?)",
                                       (f'finish:{row["operation_id"]}', f'/api/sessions/{row["session_id"]}/finish', canonical(payload)))

    def command(self, command):
        identity = command.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", identity):
            raise ValueError("invalid_command_id")
        digest = hashlib.sha256(canonical(command).encode()).hexdigest()
        db = self.journal.db
        old = db.execute("SELECT * FROM commands WHERE id=?", (identity,)).fetchone()
        if old:
            if old["digest"] != digest:
                raise ValueError("command_id_conflict")
            if old["state"] != "done":
                raise RuntimeError("command_outcome_uncertain")
            result = json.loads(old["result"])
            self.journal.emit(f"command:{identity}", f"/api/sessions/{command['session_id']}/commands/{identity}/ack"+("?safety=operator_stop" if command.get("kind")=="stop" else ""),
                              {"epoch": command["epoch"], "result": result})
            return result
        row = db.execute("SELECT * FROM launches WHERE session_id=?", (command.get("session_id"),)).fetchone()
        if not row or row["epoch"] != command.get("epoch") or not row["native_id"]:
            raise ValueError("command_fence_conflict")
        kind = command.get("kind")
        if kind not in {"capture", "send", "stop"}:
            raise ValueError("command_not_allowed")
        if kind == "send" and row["interactive"] != 1:
            raise ValueError("headless_send_refused")
        with db:
            db.execute("INSERT INTO commands VALUES(?,?,'executing',NULL)", (identity, digest))
        if kind == "capture":
            result = {"output": self.native("capture", row["native_id"], "--screen", "--plain").stdout[:16384]}
        elif kind == "stop":
            self.native("kill", row["native_id"])
            status = self.lookup(row["native_id"], row)
            result = {"stopped": bool(status and status.get("phase", status.get("state")) == "exited"
                                      and status.get("containment_empty") is True)}
        else:
            text = command.get("text")
            if not isinstance(text, str) or len(text.encode()) > 16384:
                raise ValueError("invalid_command_text")
            self.native("send", row["native_id"], "--stdin", "--enter", input=text, timeout=60)
            result = {"delivery": "native_acknowledged"}
        with db:
            db.execute("UPDATE commands SET state='done',result=? WHERE id=?", (canonical(result), identity))
        self.journal.emit(f"command:{identity}", f"/api/sessions/{row['session_id']}/commands/{identity}/ack"+("?safety=operator_stop" if kind=="stop" else ""),
                          {"epoch": row["epoch"], "result": result})
        return result

    def drain(self, acknowledge=None):
        self.journal.flush(self.client)
        self.reconcile()
        response = self.client.request("GET", "/api/offers")
        for offer in response.get("offers", []):
            self.offer(offer, acknowledge)
        self.journal.flush(self.client)

    def dispatch(self, event, socket, acknowledge):
        try:
            if event.get("type") == "session.command":
                self.command(event["command"])
            elif event.get("type") == "mailbox.deliver":
                from .messages import deliver
                deliver(self, event["message"])
            elif event.get("type") == "session.offer":
                self.offer(event["offer"], acknowledge)
            elif event.get("type") == "resync":
                self.reconcile()
                self.journal.flush(self.client)
        except (RuntimeError, ValueError) as error:
            if isinstance(error,ApiError) and error.retry_at:
                raise
            reason = str(error)
            if not re.fullmatch(r"[a-z_]{1,80}", reason):
                reason = "host_operation_failed"
            target = event.get("command") or event.get("message") or event.get("offer") or {}
            socket.send(canonical({"type": "host.error", "reason": reason,
                                   "session_id": target.get("session_id"), "operation_id": target.get("operation_id"),
                                   "message_id": target.get("id")}))
    def heartbeat(self,labels):
        available=self.launcher.capabilities()
        engines=sorted(engine for engine in self.engines.intersection(available) if available[engine])
        self.client.heartbeat(labels,engines,['headless']+[cap+':'+engine for engine in engines for cap in sorted(available[engine]-{'headless'})])

    def safety_pause(self,machine_id):
        self.reconcile()
        if time.time()*1000 < self.budget_safety_paused_until:
            time.sleep(min(5,max(0.01,self.budget_safety_paused_until/1000-time.time())))
            return
        self.journal.flush(self.client,safety_only=True)
        with self.client.socket(machine_id,safety=True) as socket:
            while time.time()*1000 < self.budget_paused_until:
                active=self.journal.db.execute("SELECT 1 FROM launches WHERE state IN ('running','aborting','launching') LIMIT 1").fetchone()
                try:
                    event=json.loads(socket.recv(timeout=min(5 if active else 60,max(0.01,self.budget_paused_until/1000-time.time()))))
                    if event.get('type')=='session.command' and event.get('command',{}).get('kind')=='stop':
                        self.command(event['command'])
                except TimeoutError:
                    pass
                self.reconcile()
                self.journal.flush(self.client,safety_only=True)

    def serve(self, machine_id, labels):
        self.machine_id=machine_id
        delay = 1
        while True:
            safety=time.time()*1000 < self.budget_paused_until
            try:
                if safety:
                    self.safety_pause(machine_id)
                    continue
                self.heartbeat(labels)
                with self.client.socket(machine_id) as socket:
                    pending = []
                    def acknowledge(session, epoch):
                        socket.send(canonical({"type": "offer_ack", "session_id": session, "epoch": epoch}))
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            try:
                                frame = json.loads(socket.recv(timeout=max(0.01, deadline-time.monotonic())))
                            except TimeoutError:
                                break
                            if frame.get("type") == "ack" and frame.get("session_id") == session and frame.get("epoch") == epoch:
                                return
                            if len(pending) >= 100:
                                raise RuntimeError("claim_ack_backlog_exceeded")
                            pending.append(frame)
                        socket.send(canonical({"type": "resync"}))
                        raise RuntimeError("claim_ack_unverified")
                    self.journal.flush(self.client)
                    self.reconcile()
                    delay = 1
                    while True:
                        active = self.journal.db.execute("SELECT 1 FROM launches WHERE state IN ('running','aborting','launching') LIMIT 1").fetchone()
                        try:
                            event = pending.pop(0) if pending else json.loads(socket.recv(timeout=5 if active else None))
                            self.dispatch(event, socket, acknowledge)
                        except TimeoutError:
                            pass
                        self.reconcile()
                        self.journal.flush(self.client)
            except (ApiError, OSError, RuntimeError, ValueError) as error:
                if isinstance(error,ApiError) and error.retry_at:
                    self.budget_paused_until=max(self.budget_paused_until,error.retry_at)
                    if safety:self.budget_safety_paused_until=max(self.budget_safety_paused_until,error.retry_at)
                # Backoff does not issue empty-queue reads or claim a launch completed.
                time.sleep(delay)
                delay = min(delay * 2, 60)
