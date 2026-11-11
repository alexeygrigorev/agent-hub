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
from .native_launch import NativeLauncher, LOCAL_ROUTES, UUID_RE, canonical, native_returncode, same_workspace

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
          CREATE TABLE IF NOT EXISTS stop_fences(
            session_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS commands(
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
        """)
        if "interactive" not in {row[1] for row in self.db.execute("PRAGMA table_info(launches)")}:
            self.db.execute("ALTER TABLE launches ADD COLUMN interactive INTEGER NOT NULL DEFAULT 0")
        if "runtime_deadline" not in {row[1] for row in self.db.execute("PRAGMA table_info(launches)")}:
            self.db.execute("ALTER TABLE launches ADD COLUMN runtime_deadline INTEGER")
        if "native_dispatch_started" not in {row[1] for row in self.db.execute("PRAGMA table_info(launches)")}:
            self.db.execute("ALTER TABLE launches ADD COLUMN native_dispatch_started INTEGER")
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
            self.enqueue(key, path, payload)

    def enqueue(self, key, path, payload):
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
        self.engines = set(engines) | {"shell"}
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
        db = self.journal.db
        session, epoch = offer.get('session_id'), offer.get('epoch')
        if isinstance(session, str) and type(epoch) is int and 1 <= epoch <= 2**63 - 1:
            if db.execute('SELECT 1 FROM stop_fences WHERE session_id=? AND epoch>=?', (session, epoch)).fetchone():
                return None
        launch = self.validate_launch(offer)
        digest = hashlib.sha256(canonical(launch).encode()).hexdigest()
        operation, session, epoch = offer["operation_id"], offer["session_id"], offer["epoch"]
        previous=db.execute('SELECT * FROM launches WHERE session_id=?',(session,)).fetchone()
        if previous and previous['operation_id']!=operation:
            if previous['state']!='pending_admission' or previous['native_id'] or epoch<=previous['epoch']:
                raise ValueError('native_reconciliation_required')
            with db:
                changed = db.execute(
                    "UPDATE launches SET operation_id=?,epoch=?,digest=?,workspace=?,tag=?,interactive=?,runtime_deadline=NULL "
                    "WHERE session_id=? AND operation_id=? AND epoch=? AND state='pending_admission' AND native_id IS NULL",
                    (operation, epoch, digest, launch['workspace'], 'task-hub2-'+session, int(launch['interactive']),
                     session, previous['operation_id'], previous['epoch']))
                if changed.rowcount != 1:
                    return None
        prior = db.execute("SELECT * FROM launches WHERE operation_id=?", (operation,)).fetchone()
        if prior:
            if prior["session_id"] != session or prior["digest"] != digest or epoch < prior["epoch"]:
                raise ValueError("offer_fence_conflict")
            if prior['state'] == 'stopped_before_dispatch':
                return None
            if prior['state']=='exited':
                return prior['native_id']
            if epoch > prior["epoch"]:
                # A new fence cannot adopt a process under the old fence automatically.
                raise ValueError("native_reconciliation_required")
            if prior['state'] == 'launching':
                if prior['native_dispatch_started'] == 0:
                    self.settle_undispatched_launch(prior)
                    return None
                self.abort_launch(operation)
                raise RuntimeError('native_abort_pending')
            if acknowledge and prior['state']!='pending_admission':
                acknowledge(session, epoch)
            native = None
            if prior['state']=='aborting':
                raise RuntimeError('native_abort_pending')
            if prior["state"] != "pending_admission" and prior["native_id"]:
                native = self.lookup(prior["native_id"], prior)
            if not native and prior["state"] != "pending_admission":
                raise RuntimeError("launch_outcome_uncertain")
        else:
            task_id = "hub2-" + session
            tag = "task-" + task_id
            with db:
                changed = db.execute(
                    "INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive,runtime_deadline) "
                    "SELECT ?,?,?,?,?,?,NULL,'pending_admission',?,NULL "
                    "WHERE NOT EXISTS(SELECT 1 FROM stop_fences WHERE session_id=? AND epoch>=?)",
                    (operation, session, epoch, digest, launch["workspace"], tag, int(launch["interactive"]), session, epoch))
                if changed.rowcount != 1:
                    return None
        if not prior or prior["state"] == "pending_admission":
            task_id, tag = "hub2-" + session, "task-hub2-" + session
            launch['worker_context']={key:offer[key] for key in ('session_id','epoch','intake_id','project_id')}
            launch['worker_context']['machine_id']=self.machine_id
            if launch['engine']!='shell' and self.launcher.config.get('private_provider_homes', False):
                from .providers import provision
                launch['provider_home']=provision(self.client,offer,launch,self.journal.directory,self.launcher.config.get('routes'))
            if acknowledge:
                acknowledge(session, epoch)
            # The accepted claim already admitted this generation. The worker
            # context carries the server-issued expiry; persist it for host-side
            # containment without an additional admission RPC or CLI flag.
            launch['child_config_file']=self.child_context(offer,launch)
            launch['runtime_deadline']=round(json.loads(Path(launch['child_config_file']).read_text())['expires_at']*1000)
            launch['client']=self.client
            launch['session_id']=session
            launch['operation_id']=operation
            launch['epoch']=epoch
            launch['attempt_label'] = self.attempt_label(session, operation, epoch)
            launch['before_native_dispatch'] = lambda: self.mark_native_dispatch(operation, session, epoch)
            launch['native_identity_observed'] = lambda native_id: self.bind_dispatched_native(operation, session, epoch, native_id)
            with db:
                changed = db.execute(
                    "UPDATE launches SET state='launching',runtime_deadline=?,native_dispatch_started=0 "
                    "WHERE operation_id=? AND session_id=? AND epoch=? AND state='pending_admission' AND native_id IS NULL "
                    "AND NOT EXISTS(SELECT 1 FROM stop_fences WHERE session_id=? AND epoch>=?)",
                    (launch['runtime_deadline'], operation, session, epoch, session, epoch))
                if changed.rowcount != 1:
                    return None
            try:
                receipt = self.launcher.launch(task_id, launch)
                native = self.lookup(receipt["session_id"], {"tag": tag, "workspace": launch["workspace"]})
                if not native or native["id"] != receipt["session_id"]:
                    raise RuntimeError("launch_outcome_uncertain")
            except Exception as error:
                current = db.execute("SELECT * FROM launches WHERE operation_id=? AND epoch=?", (operation,epoch)).fetchone()
                throttled = isinstance(error, ApiError) and error.retry_at
                if current and current['state']=='stopped_before_dispatch':
                    if throttled:
                        raise
                    return None
                logging.getLogger(__name__).exception('Native launch failed session=%s', session)
                if current and current['native_dispatch_started']==0:
                    self.settle_undispatched_launch(current)
                    if throttled:
                        raise
                    return None
                self.abort_launch(operation)
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

    def attempt_label(self, session, operation, epoch):
        return hashlib.sha256(canonical([self.machine_id, session, operation, epoch]).encode()).hexdigest()

    def mark_native_dispatch(self, operation, session, epoch):
        with self.journal.db as db:
            changed = db.execute(
                "UPDATE launches SET native_dispatch_started=1 WHERE operation_id=? AND session_id=? AND epoch=? "
                "AND state='launching' AND native_dispatch_started=0 AND native_id IS NULL",
                (operation, session, epoch))
            if changed.rowcount != 1:
                raise RuntimeError('native_dispatch_fence_conflict')

    def bind_dispatched_native(self, operation, session, epoch, native_id):
        with self.journal.db as db:
            changed = db.execute(
                "UPDATE launches SET native_id=? WHERE operation_id=? AND session_id=? AND epoch=? "
                "AND state='launching' AND native_dispatch_started=1 AND native_id IS NULL",
                (native_id, operation, session, epoch))
            if changed.rowcount != 1:
                raise RuntimeError('native_dispatch_fence_conflict')

    def settle_undispatched_launch(self, row):
        operation, session, epoch = row['operation_id'], row['session_id'], row['epoch']
        with self.journal.db as db:
            changed = db.execute(
                "UPDATE launches SET state='stopped_before_dispatch' WHERE operation_id=? AND session_id=? AND epoch=? "
                "AND state IN ('launching','aborting') AND native_dispatch_started=0 AND native_id IS NULL",
                (operation, session, epoch))
            if changed.rowcount != 1:
                return
            self.journal.enqueue(f"no-dispatch:{operation}:{epoch}", f"/api/sessions/{session}/finish",
                                 {"operation_id": operation, "epoch": epoch, "native_id": None,
                                  "launch_failed": True, "no_dispatch": True})

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
        rows = self.journal.db.execute("SELECT * FROM launches WHERE state IN ('running','aborting','deadline_stopping','launching') LIMIT 1001").fetchall()
        if len(rows) > 1000:
            raise RuntimeError("active_native_bound_exceeded")
        for row in rows:
            if row['session_id'] in getattr(self, '_inflight', set()):
                continue
            if row['state'] in {'launching','aborting'} and row['native_dispatch_started'] == 0 and not row['native_id']:
                self.settle_undispatched_launch(row)
                continue
            if row['state']=='launching':
                # In-flight launches were excluded above; this is an interrupted dispatch.
                with self.journal.db:
                    self.journal.db.execute("UPDATE launches SET state='aborting' WHERE operation_id=? AND state='launching'",(row['operation_id'],))
                row = self.journal.db.execute("SELECT * FROM launches WHERE operation_id=?", (row['operation_id'],)).fetchone()
            workspace_available = self._workspace_is_available(row["workspace"])
            if not workspace_available and not row["native_id"]:
                self._warn_unavailable_workspace(row)
                continue
            if row['state'] in {'aborting','deadline_stopping'} and not row['native_id']:
                if row['native_dispatch_started'] != 1:
                    continue
                attempt = self.attempt_label(row['session_id'], row['operation_id'], row['epoch'])
                try:identity=self.launcher.aborted_identity('hub2-'+row['session_id'],row['workspace'],attempt)
                except RuntimeError:identity=None
                if not identity:continue
                with self.journal.db:
                    self.journal.db.execute('UPDATE launches SET native_id=? WHERE operation_id=?',(identity,row['operation_id']))
                row=self.journal.db.execute('SELECT * FROM launches WHERE operation_id=?',(row['operation_id'],)).fetchone()
            if workspace_available:
                native = self.lookup(row["native_id"], row)
            else:
                native = self.launcher.retired(row["native_id"], dict(row))
            if not native:
                if not workspace_available:
                    self._warn_unavailable_workspace(row)
                continue  # Missing process is uncertain, never a successful finish.
            phase = native.get("phase", native.get("state"))
            deadline = row['runtime_deadline']
            if row['state']=='running' and type(deadline) is int and int(time.time()*1000)>=deadline and phase!='exited':
                with self.journal.db:
                    self.journal.db.execute("UPDATE launches SET state='deadline_stopping' WHERE operation_id=? AND state='running'",
                                            (row['operation_id'],))
                row = self.journal.db.execute("SELECT * FROM launches WHERE operation_id=?",(row['operation_id'],)).fetchone()
            if phase != "exited":
                if row['state'] in {'aborting','deadline_stopping'}:
                    self.native('kill',row['native_id'],allow_failure=True)
                    native=self.lookup(row['native_id'],row)
                if not native or native.get('phase',native.get('state'))!='exited':continue
            if native.get("containment_empty") is not True:
                continue
            exit_info = native.get("exit") or {}
            code = native_returncode(exit_info)
            if code is None:
                continue
            launcher_state = (
                "retired-finished" if not workspace_available
                else self.launcher.finalize(row["native_id"], {"tag": row["tag"], "workspace": row["workspace"]})
            )
            payload = {"operation_id": row["operation_id"], "epoch": row["epoch"],
                       "native_id": row["native_id"], "exit_code": code,
                       "native_exit_confirmed": True,
                       "evidence": {"source": "aplexer_retirement" if native.get("retirement") else "aplexer_status",
                                    "native_identity": {key: native[key] for key in ("id", "tag", "workspace")},
                                    "containment_empty": True, "native_exit": exit_info,"launcher_state":launcher_state}}
            if type(row['runtime_deadline']) is int:
                exited_at = exit_info.get('exited_at_ms')
                exceeded = native.get('deadline_exceeded') is True or (type(exited_at) is int and exited_at >= row['runtime_deadline'])
                payload['evidence']['native_deadline']={'deadline_ms':row['runtime_deadline'],'exceeded':exceeded}
            elif type(native.get('deadline_ms')) is int:
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
        pre_dispatch = row['state'] in {'pending_admission','stopped_before_dispatch'} or (
            row['state'] in {'launching','aborting'} and row['native_dispatch_started']==0)
        if kind == 'stop' and not row['native_id'] and pre_dispatch:
            with db:
                changed = db.execute(
                    "UPDATE launches SET state='stopped_before_dispatch' "
                    "WHERE operation_id=? AND session_id=? AND epoch=? AND native_id IS NULL "
                    "AND (state IN ('pending_admission','stopped_before_dispatch') "
                    "OR (state IN ('launching','aborting') AND native_dispatch_started=0))",
                    (row['operation_id'], row['session_id'], row['epoch']))
                if changed.rowcount != 1:
                    raise ValueError('command_fence_conflict')
                result = {'stopped': True, 'evidence': 'dispatch_fenced'}
                self.begin_command(identity, digest)
                self.finish_command(identity, command, result)
            return result
        native_id, _status = self.resolve_launch_native(row)
        if not native_id and kind == 'stop' and row['state'] in {'launching', 'aborting', 'deadline_stopping'}:
            result = {'stopped': False, 'error': 'stop_unconfirmed', 'reason': 'stop_unconfirmed', 'stage': 'host_stop'}
            with db:
                self.begin_command(identity, digest)
                self.finish_command(identity, command, result)
            return result
        if not native_id:
            raise ValueError("command_fence_conflict")
        if kind == "send" and row["interactive"] != 1:
            raise ValueError("headless_send_refused")
        with db:
            self.begin_command(identity, digest)
        if kind == "capture":
            from .task_output import capture_task_output, bounded_capture
            output = (None if command.get("mode") == "screen" else
                      capture_task_output("hub2-" + row["session_id"], row, native_id))
            if output is None:
                output = self.native("capture", native_id, "--screen", "--plain").stdout[:16384]
            result = {"output": bounded_capture(output)}
        elif kind == "stop":
            self.native("kill", native_id)
            status = self.lookup(native_id, row)
            if status and status.get("phase", status.get("state")) == "exited" and status.get("containment_empty") is True:
                result = {'stopped': True, 'evidence': 'native_exit', 'native_id': native_id}
            else:
                result = {'stopped': False, 'error': 'stop_unconfirmed', 'reason': 'stop_unconfirmed', 'stage': 'host_stop'}
        else:
            text = command.get("text")
            if not isinstance(text, str) or len(text.encode()) > 16384:
                raise ValueError("invalid_command_text")
            self.native("send", native_id, "--stdin", "--enter", input=text, timeout=60)
            result = {"delivery": "native_acknowledged"}
        with db:
            self.finish_command(identity, command, result)
        return result

    def stop_pre_native_command(self, command, identity, digest):
        session = command.get('session_id')
        epoch = command.get('epoch')
        if (not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', session)
                or type(epoch) is not int or epoch < 1 or command.get('tag') != 'task-hub2-' + session):
            raise ValueError('command_fence_conflict')
        # A lost journal row cannot attest to an older dispatch. Fence any
        # queued offer for this epoch without killing a process by an unbound tag.
        result = {'stopped': False, 'error': 'stop_unconfirmed', 'reason': 'stop_unconfirmed', 'stage': 'host_stop'}
        db = self.journal.db
        with db:
            changed = db.execute(
                'INSERT INTO stop_fences(session_id,epoch) SELECT ?,? '
                'WHERE NOT EXISTS(SELECT 1 FROM launches WHERE session_id=?) '
                'ON CONFLICT(session_id) DO UPDATE SET epoch=MAX(stop_fences.epoch,excluded.epoch)',
                (session, epoch, session))
            if changed.rowcount != 1:
                raise ValueError('command_fence_conflict')
            self.begin_command(identity, digest)
            self.finish_command(identity, command, result)
        return result

    def finish_command(self, identity, command, result):
        self.journal.db.execute("UPDATE commands SET state='done',result=? WHERE id=?", (canonical(result), identity))
        self.journal.enqueue(f"command:{identity}", f"/api/sessions/{command['session_id']}/commands/{identity}/ack"
                             + ("?safety=operator_stop" if command.get("kind") == "stop" else ""),
                             {"epoch": command["epoch"], "result": result})

    def begin_command(self, identity, digest):
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
            stage = 'host_command'
            if event.get('type') == 'session.offer':
                stage = 'host_launch'
                row = self.journal.db.execute(
                    "SELECT state,native_dispatch_started FROM launches WHERE operation_id=? AND session_id=? AND epoch=?",
                    (target.get('operation_id'),target.get('session_id'),target.get('epoch'))).fetchone()
                if row and row['state'] in {'launching','running','aborting','deadline_stopping'} and row['native_dispatch_started'] != 0:
                    stage = 'host_launch_uncertain'
            socket.send(canonical({"type": "host.error", "reason": reason, "stage": stage, "detail": reason[:200],
                                   "session_id": target.get("session_id"), "operation_id": target.get("operation_id"),
                                   "message_id": target.get("id")}))
    def live_sessions(self):
        rows=self.journal.db.execute("SELECT session_id,operation_id,epoch,native_id,tag,workspace,state,native_dispatch_started FROM launches WHERE state IN ('launching','running','aborting','deadline_stopping') ORDER BY session_id LIMIT 100").fetchall()
        result=[]
        for row in rows:
            if row['session_id'] in self._inflight and row['state']=='launching':
                native_id,status,phase=None,None,'launching'
            elif not self._workspace_is_available(row['workspace']):
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
        if row['native_dispatch_started'] != 1:
            return None,None
        selector=f"{row['workspace']}:{row['tag']}"
        status=self.launcher.status(selector,workspace=row['workspace'])
        native_id=status.get('id') if isinstance(status,dict) else None
        if (not isinstance(native_id,str) or not UUID_RE.fullmatch(native_id)
                or not isinstance(status.get('labels'),dict)
                or status['labels'].get('hub.attempt') != self.attempt_label(row['session_id'],row['operation_id'],row['epoch'])
                or status.get('tag')!=row['tag']
                or not same_workspace(status.get('workspace'),row['workspace'])):
            return None,None
        with self.journal.db:
            self.journal.db.execute("UPDATE launches SET native_id=? WHERE session_id=? AND epoch=? AND native_id IS NULL",
                                    (native_id,row['session_id'],row['epoch']))
        return native_id,status

    def heartbeat(self,labels,include_native_sessions=True):
        available=self.launcher.capabilities()
        engines=sorted(engine for engine in self.engines.intersection(available) if available[engine])
        capabilities=['headless']+[cap+':'+engine for engine in engines for cap in sorted(available[engine]-{'headless'})]
        configuration = getattr(self.launcher, 'config', {})
        if not configuration.get('private_provider_homes', False):
            # Execution-source hints only, never login/account health proof.
            # Custom routes cannot inherit a canonical account association.
            accounts = {'codex': 'codex', 'claude': 'claude', 'go': 'shared-go',
                        'zai': 'zai-central', 'antigravity': 'gemini'}
            for engine in engines:
                if engine not in accounts or configuration.get('routes', {}).get(engine) != LOCAL_ROUTES[engine]:
                    continue
                for mode in ('headless', 'interactive'):
                    if mode in available[engine]:
                        capabilities.append('credentials:local:' + mode + ':' + engine + ':' + accounts[engine])
        capabilities = capabilities[:32]
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
                active=self.journal.db.execute("SELECT 1 FROM launches WHERE state IN ('running','aborting','deadline_stopping','launching') LIMIT 1").fetchone()
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
                        active = self.journal.db.execute("SELECT 1 FROM launches WHERE state IN ('running','aborting','deadline_stopping','launching') LIMIT 1").fetchone()
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
