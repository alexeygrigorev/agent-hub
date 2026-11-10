"""Native process receipts are journaled before delivery and replayed on reconnect."""
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

from .client import ApiError, private_file, save_config
from . import native_launch
from .native_launch import NativeLauncher, canonical, native_returncode, same_workspace

# Keep the live native-session census fresh for the global Hub session limit.
LIVE_SESSION_HEARTBEAT_SECONDS = 30


def run_heartbeat_loop(beat, stop, interval=LIVE_SESSION_HEARTBEAT_SECONDS):
    """Keep live native-session snapshots fresh while launch/reconcile work is busy."""
    while not stop.wait(interval):
        try:
            beat()
        except Exception:
            logging.getLogger(__name__).exception("Host heartbeat failed")



class Journal:
    def __init__(self, path):
        path = Path(path)
        self.directory = path.parent
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_file(path.parent)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        private_file(path)
        self.path = path
        self._connections = threading.local()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS launches(
            operation_id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
            epoch INTEGER NOT NULL, digest TEXT NOT NULL, workspace TEXT NOT NULL,
            tag TEXT NOT NULL, native_id TEXT, state TEXT NOT NULL, interactive INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS outbox(
            key TEXT PRIMARY KEY, path TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox_dead_letters(
            key TEXT PRIMARY KEY, path TEXT NOT NULL, payload TEXT NOT NULL,
            status INTEGER NOT NULL, reason TEXT NOT NULL, observed_at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS commands(
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
        """)
        if "interactive" not in {row[1] for row in self.db.execute("PRAGMA table_info(launches)")}:
            self.db.execute("ALTER TABLE launches ADD COLUMN interactive INTEGER NOT NULL DEFAULT 0")
        self.db.execute("CREATE INDEX IF NOT EXISTS launches_active ON launches(state,operation_id)")

    @property
    def db(self):
        """A connection and transaction context belong to exactly one thread."""
        if not hasattr(self._connections, 'db'):
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            self._connections.db = connection
        return self._connections.db

    def close_thread(self):
        connection = getattr(self._connections, 'db', None)
        if connection is not None:
            connection.close()
            del self._connections.db

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
            try:
                client.request("POST", row["path"], json.loads(row["payload"]), row["key"])
            except ApiError as error:
                # Authentication, budget pauses, timeouts and server failures
                # can recover. A rejected payload/retired fence cannot: retain
                # its complete receipt privately without blocking the socket.
                if error.retry_at or error.status not in {400, 404, 409, 410, 422}:
                    raise
                reason = error.code or 'http_' + str(error.status)
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO outbox_dead_letters VALUES(?,?,?,?,?,?)",
                                    (row['key'], row['path'], row['payload'], error.status, reason, int(time.time()*1000)))
                    self.db.execute("DELETE FROM outbox WHERE key=? AND payload=?", (row['key'], row['payload']))
                logging.getLogger(__name__).error('Host receipt dead-lettered key=%s status=%s reason=%s',
                                                 row['key'], error.status, reason)
                continue
            with self.db:
                self.db.execute("DELETE FROM outbox WHERE key=? AND payload=?", (row["key"], row['payload']))


class Bridge:
    def __init__(self, client, journal, roots, engines, executable="a", runner=subprocess.run, launcher_config=None,machine_id=None):
        self.client, self.journal, self.runner = client, journal, runner
        self.roots = [Path(root).resolve(strict=True) for root in roots]
        self.engines = set(engines)
        # The Hub picks a route; the host spawns it directly through aplexer.
        # The external launcher runtime is not consulted for admission or spawn.
        self.launcher = NativeLauncher(launcher_config or {}, runner, aplexer=executable, journal=journal)
        if not self.launcher.config.get('private_provider_homes'):
            self.engines.add('antigravity')
        self._session_locks = {}
        self._inflight = set()
        self._inflight_lock = threading.Lock()
        self.machine_id=machine_id
        self.budget_paused_until=0
        self.budget_safety_paused_until=0
        self._unavailable_workspace_warnings=set()
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
            return self.launcher.retired(selector, dict(expected)) if expected else None
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
        workspace = self.resolve_workspace(launch.get("workspace"))
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

    def resolve_workspace(self, value):
        if not isinstance(value, str) or not value or len(value) > 1024 or "\0" in value:
            raise ValueError("invalid_workspace")
        candidate = Path(value)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:\\", value):
            try:
                workspace = candidate.resolve(strict=True)
            except OSError:
                raise ValueError("workspace_unavailable") from None
            if not workspace.is_dir():
                raise ValueError("workspace_unavailable")
            if not any(workspace.is_relative_to(root) for root in self.roots):
                raise ValueError("workspace_outside_allowlist")
            return workspace
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError("invalid_workspace")

        # Enrolled roots may name one workspace directly or contain several
        # named workspace directories. Prefer an exact root label when present.
        exact = [root for root in self.roots if root.name.casefold() == value.casefold()]
        candidates = exact or [root / value for root in self.roots]
        matches = {}
        for path in candidates:
            try:
                workspace = path.resolve(strict=True)
            except OSError:
                continue
            if workspace.is_dir() and any(workspace.is_relative_to(root) for root in self.roots):
                matches[str(workspace)] = workspace
        if len(matches) > 1:
            raise ValueError("workspace_label_ambiguous")
        if not matches:
            raise ValueError("workspace_label_unavailable")
        return next(iter(matches.values()))

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
            if launch['engine']!='shell' and self.launcher.config.get('private_provider_homes', False):
                from .providers import provision
                launch['provider_home']=provision(self.client,offer,launch,self.journal.directory,self.launcher.config.get('routes'))
            if acknowledge:
                acknowledge(session, epoch)
            # The acknowledged claim already admitted this generation. Do not
            # insert a second network gate between acceptance and native spawn.
            launch['child_config_file']=self.child_context(offer,launch)
            launch['runtime_deadline']=round(json.loads(Path(launch['child_config_file']).read_text())['expires_at']*1000)
            # Optional managed-provider homes retain their binding metadata.
            launch['client']=self.client
            launch['session_id']=session
            launch['operation_id']=operation
            launch['epoch']=epoch
            with db:
                db.execute("UPDATE launches SET state='launching' WHERE operation_id=?", (operation,))
            try:
                receipt = self.launcher.launch(task_id, launch)
                native = self.lookup(receipt["session_id"], {"tag": tag, "workspace": launch["workspace"]})
                if not native or native["id"] != receipt["session_id"]:
                    raise RuntimeError("launch_outcome_uncertain")
            except (RuntimeError,ValueError,subprocess.SubprocessError,OSError) as error:
                self.abort_launch(operation)
                logging.getLogger(__name__).exception('Native launch failed session=%s', session)
                raise
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

    @staticmethod
    def _workspace_is_available(workspace):
        if not isinstance(workspace, str) or not workspace:
            return False
        try:
            path = Path(workspace)
            return path.is_absolute() and path.is_dir()
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _warn_unavailable_workspace(self, row):
        session_id = row["session_id"]
        warnings = getattr(self, "_unavailable_workspace_warnings", None)
        if warnings is None:
            warnings = set()
            self._unavailable_workspace_warnings = warnings
        if session_id in warnings:
            return
        warnings.add(session_id)
        logging.getLogger(__name__).warning(
            "Hub2 host journal session %s has an unavailable workspace; leaving native state unresolved",
            session_id,
        )

    def reconcile(self):
        rows = self.journal.db.execute("SELECT * FROM launches WHERE state IN ('running','aborting','launching') LIMIT 1001").fetchall()
        if len(rows) > 1000:
            raise RuntimeError("active_native_bound_exceeded")
        for row in rows:
            if row['session_id'] in getattr(self, '_inflight', set()):
                continue
            if row['state']=='launching':
                # In-flight launches were excluded above; this is an interrupted dispatch.
                with self.journal.db:
                    self.journal.db.execute("UPDATE launches SET state='aborting' WHERE operation_id=? AND state='launching'",(row['operation_id'],))
                row = self.journal.db.execute("SELECT * FROM launches WHERE operation_id=?", (row['operation_id'],)).fetchone()
            if not self._workspace_is_available(row["workspace"]):
                self._warn_unavailable_workspace(row)
                continue
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
            launcher_state=self.launcher.finalize(row["native_id"], {"tag": row["tag"], "workspace": row["workspace"]})
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
        kind = command.get("kind")
        if kind not in {"capture", "send", "stop"}:
            raise ValueError("command_not_allowed")
        row = db.execute("SELECT * FROM launches WHERE session_id=?", (command.get("session_id"),)).fetchone()
        if not row:
            if kind == "stop" and command.get("pre_native") is True:
                return self.stop_pre_native_command(command, identity, digest)
            raise ValueError("command_fence_conflict")
        if row["epoch"] != command.get("epoch"):
            raise ValueError("command_fence_conflict")
        native_id, _status = self.resolve_launch_native(row)
        if not native_id and row["state"] in {"pending_admission", "launching", "aborting"}:
            # A stop for an interrupted launch is confirmed only after the
            # supervisor lookup by session tag finds no native process.
            if kind != "stop":
                raise ValueError("command_fence_conflict")
            self.begin_command(identity, digest)
            result = {"stopped": True}
            self.finish_command(identity, command, result)
            return result
        if not native_id:
            raise ValueError("command_fence_conflict")
        if kind == "send" and row["interactive"] != 1:
            raise ValueError("headless_send_refused")
        self.begin_command(identity, digest)
        if kind == "capture":
            result = {"output": self.native("capture", native_id, "--screen", "--plain").stdout[:16384]}
        elif kind == "stop":
            self.native("kill", native_id)
            status = self.lookup(native_id, row)
            result = {"stopped": bool(status and status.get("phase", status.get("state")) == "exited"
                                      and status.get("containment_empty") is True)}
        else:
            text = command.get("text")
            if not isinstance(text, str) or len(text.encode()) > 16384:
                raise ValueError("invalid_command_text")
            self.native("send", native_id, "--stdin", "--enter", input=text, timeout=60)
            result = {"delivery": "native_acknowledged"}
        self.finish_command(identity, command, result)
        return result

    def stop_pre_native_command(self, command, identity, digest):
        session = command.get("session_id")
        workspace = command.get("workspace")
        tag = command.get("tag")
        if (not isinstance(session, str) or not isinstance(workspace, str) or not workspace
                or tag != "task-hub2-" + session):
            raise ValueError("command_fence_conflict")
        if self.journal.db.execute("SELECT 1 FROM launches WHERE session_id=?", (session,)).fetchone():
            raise ValueError("command_fence_conflict")
        try:
            resolved = Path(workspace).resolve(strict=True)
        except OSError as error:
            raise ValueError("command_fence_conflict") from error
        if not resolved.is_dir() or not any(resolved.is_relative_to(root) for root in self.roots):
            raise ValueError("command_fence_conflict")
        workspace = str(resolved)
        expected = {"tag": tag, "workspace": workspace}
        self.begin_command(identity, digest)
        status = self.launcher.status(f"{workspace}:{tag}", workspace=workspace)
        if status:
            if (status.get("tag") != tag or not same_workspace(status.get("workspace"), workspace)
                    or not isinstance(status.get("id"), str)):
                raise RuntimeError("native_abort_identity_mismatch")
            native_id = status["id"]
            if status.get("phase", status.get("state")) != "exited":
                self.native("kill", native_id, allow_failure=True)
                status = self.lookup(native_id, expected)
            result = {"stopped": bool(status and status.get("phase", status.get("state")) == "exited"
                                      and status.get("containment_empty") is True)}
        else:
            result = {"stopped": True}
        self.finish_command(identity, command, result)
        return result

    def finish_command(self, identity, command, result):
        with self.journal.db:
            self.journal.db.execute("UPDATE commands SET state='done',result=? WHERE id=?", (canonical(result), identity))
        self.journal.emit(f"command:{identity}", f"/api/sessions/{command['session_id']}/commands/{identity}/ack"
                          + ("?safety=operator_stop" if command.get("kind") == "stop" else ""),
                          {"epoch": command["epoch"], "result": result})

    def begin_command(self, identity, digest):
        with self.journal.db:
            self.journal.db.execute("INSERT INTO commands VALUES(?,?,'executing',NULL)", (identity, digest))

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
        except Exception as error:
            logging.getLogger(__name__).exception('Host event failed type=%s', event.get('type'))
            if isinstance(error,ApiError) and error.retry_at:
                self.budget_paused_until = max(getattr(self, 'budget_paused_until', 0), error.retry_at)
                raise
            reason = error.code if isinstance(error,ApiError) and error.code else str(error)
            if not re.fullmatch(r"[a-z_]{1,80}", reason):
                reason = "host_operation_failed"
            target = event.get("command") or event.get("message") or event.get("offer") or {}
            socket.send(canonical({"type": "host.error", "reason": reason, "stage": "host_launch" if event.get("type") == "session.offer" else "host_command", "detail": reason[:200],
                                   "session_id": target.get("session_id"), "operation_id": target.get("operation_id"),
                                   "message_id": target.get("id")}))
    def live_sessions(self):
        rows=self.journal.db.execute("SELECT session_id,epoch,native_id,tag,workspace,state FROM launches WHERE state IN ('launching','running','aborting') ORDER BY session_id LIMIT 100").fetchall()
        result=[]
        for row in rows:
            if not self._workspace_is_available(row['workspace']):
                self._warn_unavailable_workspace(row)
                native_id,status,phase=row['native_id'],None,'unknown'
            else:
                native_id,status=self.resolve_launch_native(row)
                phase=str((status or {}).get('phase',(status or {}).get('state',
                    'launching' if row['state']=='launching' else 'missing')))
            activity=(status or {}).get('last_activity_ms',(status or {}).get('updated_at_ms',(status or {}).get('created_at_ms')))
            if type(activity) is not int:activity=None
            exit_code=native_returncode((status or {}).get('exit') or {}) if phase=='exited' else None
            result.append({'session_id':row['session_id'],'native_id':native_id,'phase':phase,
                           'last_activity_at':activity,'exit_code':exit_code,
                           'containment_empty':(status or {}).get('containment_empty') is True})
        return result

    def resolve_launch_native(self,row):
        expected={'tag':row['tag'],'workspace':row['workspace']}
        if row['native_id']:
            return row['native_id'],self.lookup(row['native_id'],expected)
        if not self._workspace_is_available(row['workspace']):
            self._warn_unavailable_workspace(row)
            return None,None
        selector=f"{row['workspace']}:{row['tag']}"
        status=self.launcher.status(selector,workspace=row['workspace'])
        native_id=status.get('id') if isinstance(status,dict) else None
        if (not isinstance(native_id,str) or not native_id
                or status.get('tag')!=row['tag']
                or not same_workspace(status.get('workspace'),row['workspace'])):
            return None,None
        state='aborting' if row['state']=='aborting' else 'running'
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET native_id=?,state=? WHERE session_id=? AND epoch=? AND native_id IS NULL",
                                    (native_id,state,row['session_id'],row['epoch']))
        return native_id,status

    def heartbeat(self,labels,include_native_sessions=True):
        available=self.launcher.capabilities()
        engines=sorted(engine for engine in self.engines.intersection(available) if available[engine])
        capabilities=['headless']+[cap+':'+engine for engine in engines for cap in sorted(available[engine]-{'headless'})]
        if include_native_sessions:
            self.client.heartbeat(labels,engines,capabilities,native_sessions=self.live_sessions())
        else:
            self.client.heartbeat(labels,engines,capabilities)

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
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
        self._launch_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='hub2-launch')
        self._command_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='hub2-command')
        self._worker_slots = threading.BoundedSemaphore(32)
        stop = threading.Event()
        def beats():
            try:
                run_heartbeat_loop(lambda: self.heartbeat(labels), stop)
            finally:
                self.journal.close_thread()
        thread = threading.Thread(target=beats, name='hub2-live-session-heartbeat', daemon=True)
        thread.start()
        try:
            self._serve(machine_id, labels)
        finally:
            stop.set()
            thread.join(timeout=1)
            self._launch_pool.shutdown(wait=False)
            self._command_pool.shutdown(wait=False)

    def _serve(self, machine_id, labels):
        self.machine_id=machine_id
        if not hasattr(self, '_inflight'):
            self._inflight, self._inflight_lock = set(), threading.Lock()
        self._session_locks = getattr(self, '_session_locks', {})
        delay = 1
        while True:
            safety=time.time()*1000 < self.budget_paused_until
            try:
                if safety:
                    self.safety_pause(machine_id)
                    continue
                self.heartbeat(labels)
                with self.client.socket(machine_id) as socket:
                    logging.getLogger(__name__).info('Host connected machine=%s', machine_id)
                    waiters, waiter_lock = {}, threading.Lock()
                    pool, commands, slots = self._launch_pool, self._command_pool, self._worker_slots
                    def acknowledge(session, epoch, _socket=socket, _waiters=waiters, _lock=waiter_lock):
                        ready = threading.Event()
                        with _lock:
                            _waiters[(session, epoch)] = ready
                        try:
                            _socket.send(canonical({"type": "offer_ack", "session_id": session, "epoch": epoch}))
                            if not ready.wait(15):
                                raise RuntimeError("claim_ack_unverified")
                        finally:
                            with _lock:
                                _waiters.pop((session, epoch), None)
                    def execute(event, session, _socket=socket, _acknowledge=acknowledge, _slots=slots):
                        try:
                            logging.getLogger(__name__).info('Host event type=%s session=%s', event.get('type'), session)
                            with self._session_locks[session]:
                                self.dispatch(event, _socket, _acknowledge)
                            self.journal.flush(self.client)
                            logging.getLogger(__name__).info('Host event complete session=%s', session)
                        except Exception:
                            logging.getLogger(__name__).exception('Host worker failed session=%s', session)
                        finally:
                            with self._inflight_lock:
                                if event.get('type') == 'session.offer':
                                    self._inflight.discard(session)
                            _slots.release()
                            self.journal.close_thread()
                    next_live_session_heartbeat = time.monotonic() + LIVE_SESSION_HEARTBEAT_SECONDS
                    self.journal.flush(self.client)
                    self.reconcile()
                    delay = 1
                    while True:
                        if time.time() * 1000 < self.budget_paused_until:
                            break
                        now = time.monotonic()
                        if now >= next_live_session_heartbeat:
                            self.heartbeat(labels)
                            next_live_session_heartbeat = time.monotonic() + LIVE_SESSION_HEARTBEAT_SECONDS
                        active = self.journal.db.execute("SELECT 1 FROM launches WHERE state IN ('running','aborting','launching') LIMIT 1").fetchone()
                        recv_timeout = 5 if active else min(
                            LIVE_SESSION_HEARTBEAT_SECONDS,
                            max(0.01, next_live_session_heartbeat - now),
                        )
                        try:
                            event = json.loads(socket.recv(timeout=recv_timeout))
                            if event.get('type') == 'ack':
                                with waiter_lock:
                                    ready = waiters.get((event.get('session_id'), event.get('epoch')))
                                    if ready:
                                        ready.set()
                            elif event.get('type') == 'session.offer':
                                session = event.get('offer', {}).get('session_id')
                                with self._inflight_lock:
                                    if session in self._inflight:
                                        continue
                                    if not slots.acquire(blocking=False):
                                        raise RuntimeError('host_launch_queue_full')
                                    self._inflight.add(session)
                                    self._session_locks.setdefault(session, threading.RLock())
                                pool.submit(execute, event, session)
                            elif event.get('type') == 'session.command' and event.get('command', {}).get('session_id') in self._inflight:
                                session = event['command']['session_id']
                                if not slots.acquire(blocking=False):
                                    raise RuntimeError('host_command_queue_full')
                                commands.submit(execute, event, session)
                            else:
                                self.dispatch(event, socket, acknowledge)
                        except TimeoutError:
                            pass
                        now = time.monotonic()
                        if now >= next_live_session_heartbeat:
                            self.heartbeat(labels)
                            next_live_session_heartbeat = time.monotonic() + LIVE_SESSION_HEARTBEAT_SECONDS
                        self.reconcile()
                        self.journal.flush(self.client)
            except Exception as error:
                logging.getLogger(__name__).exception('Host disconnected machine=%s', machine_id)
                if isinstance(error,ApiError) and error.retry_at:
                    self.budget_paused_until=max(self.budget_paused_until,error.retry_at)
                    if safety:self.budget_safety_paused_until=max(self.budget_safety_paused_until,error.retry_at)
                # Backoff does not issue empty-queue reads or claim a launch completed.
                time.sleep(delay)
                delay = min(delay * 2, 60)
