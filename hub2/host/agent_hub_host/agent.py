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
from . import native_launch, launch_reaper, credential_delivery
from .implement_workspace import prepare_workspace
from .advertisement import Advertisement, INVENTORY_OVERFLOW_TAG
from .repository_identity import QUALIFIED_WORKSPACE, REPOSITORY_IDENTITY_CAPABILITY
from .native_launch import NativeLauncher, LOCAL_ROUTES, UUID_RE, canonical, native_returncode, same_workspace, spawn_environment
from .local_auth_readiness import local_claude_missing_contexts
from .git_salvage import salvage_branch

# Keep the live native-session census fresh for the global Hub session limit.
LIVE_SESSION_HEARTBEAT_SECONDS = 30
_NATIVE_INVENTORY_START_LOCK = threading.Lock()
_NATIVE_OBSERVATION_REPORT_LOCK = threading.Lock()
_NATIVE_OBSERVATION_REPORT_LIMIT = 128


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
        if "legacy_failure_json" not in {row[1] for row in self.db.execute("PRAGMA table_info(launches)")}:
            self.db.execute("ALTER TABLE launches ADD COLUMN legacy_failure_json TEXT")
        if "workspace_repair_digest" not in {row[1] for row in self.db.execute("PRAGMA table_info(launches)")}:
            self.db.execute("ALTER TABLE launches ADD COLUMN workspace_repair_digest TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS launches_active ON launches(state,operation_id)")
        launch_reaper.initialize(self)

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
                    repaired = self.db.execute("SELECT 1 FROM launches WHERE 'finish:'||operation_id=? AND workspace_repair_digest IS NOT NULL", (row['key'],)).fetchone()
                    verb = 'IGNORE' if repaired else 'REPLACE'
                    self.db.execute(f"INSERT OR {verb} INTO outbox_dead_letters VALUES(?,?,?,?,?,?)",
                                    (row['key'], row['path'], row['payload'], error.status, reason, int(time.time()*1000)))
                    self.db.execute("DELETE FROM outbox WHERE key=? AND payload=?", (row['key'], row['payload']))
                logging.getLogger(__name__).error('Host receipt dead-lettered key=%s status=%s reason=%s',
                                                 row['key'], error.status, reason)
                continue
            with self.db:
                self.db.execute("DELETE FROM outbox WHERE key=? AND payload=?", (row["key"], row['payload']))


def offer_ack_frame(session, epoch, workspace_binding=None):
    # Keep legal Unicode paths compact on the existing 8192 UTF-16-unit frame.
    return json.dumps({'type': 'offer_ack', 'session_id': session, 'epoch': epoch,
                       **({'workspace_binding': workspace_binding} if workspace_binding else {})},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class OfferAcknowledgement:
    """A claim ACK stays compatible; only an exact binding echo proves repair."""
    def __init__(self, binding):
        self.binding, self.binding_verified = binding, False
        self.ready = threading.Event()

    def accept(self, event):
        self.binding_verified = self.binding is not None and event.get('workspace_binding') == self.binding
        self.ready.set()

    def wait(self, timeout):
        if not self.ready.wait(timeout):
            raise RuntimeError('claim_ack_unverified')
        return self.binding_verified


class Bridge:
    def __init__(self, client, journal, roots, engines, executable="a", runner=subprocess.run, launcher_config=None,machine_id=None,capabilities=None,workspaces=None):
        self.client, self.journal, self.runner = client, journal, runner
        self.roots = [Path(root).resolve(strict=True) for root in roots]
        self.advertisement = Advertisement(self.roots, capabilities, workspaces)
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

    def lookup(self, selector, expected=None, *, timeout=30):
        result = self.native("status", selector, "--json", allow_failure=True, timeout=timeout)
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
        managed = {}
        if 'managed_credential_label' in launch:
            if (launch['managed_credential_label'] != 'hub-claude' or engine != 'claude' or model != 'opus'
                    or effort != 'medium' or launch.get('interactive') or with_goal or offer.get('account_id') != 'claude'
                    or not isinstance(offer.get('request_digest'), str) or not re.fullmatch('[0-9a-f]{64}',offer['request_digest'])
                    or not isinstance(self.machine_id,str) or not self.machine_id):
                raise ValueError('invalid_managed_credential_route')
            try:
                ready = self.launcher.managed_claude_readiness()
            except Exception:
                ready = None
            if not ready:
                raise ValueError('managed_exec_unavailable')
            managed = {'managed_credential_label':'hub-claude','account_id':'claude',
                       'request_digest':offer['request_digest'],'machine_id':self.machine_id}
        return {"workspace": str(workspace), "engine": engine, "model": model,
                "prompt": prompt, "interactive": bool(launch.get("interactive", False)), "timeout_seconds": timeout,
                'with_goal':with_goal,'effort':effort,**managed}

    def resolve_workspace(self, value):
        if not isinstance(value, str) or not value or len(value) > 1024 or "\0" in value:
            raise ValueError("invalid_workspace")
        candidate = Path(value)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:\\", value):
            try:
                workspace = candidate.resolve(strict=True)
            except OSError:
                raise ValueError(f"workspace_unavailable path={value}") from None
            if not workspace.is_dir():
                raise ValueError(f"workspace_unavailable path={value}")
            if not any(workspace.is_relative_to(root) for root in self.roots):
                raise ValueError(f"workspace_outside_allowlist path={workspace}")
            return workspace
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError("invalid_workspace")

        # New repository-only starts carry a reserved qualified token. The host
        # re-reads the checkout's Git metadata for every launch; older hosts
        # without this mapping cannot pass the server's strict workspace match.
        if QUALIFIED_WORKSPACE.fullmatch(value.lower()):
            if value != value.lower():
                raise ValueError("workspace_identity_unavailable")
            return self.advertisement.resolve_repository(value)

        alias = self.advertisement.resolve_alias(value)
        if alias is not None:
            return alias

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

    def is_session_dead(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            return False
        row = self.journal.db.execute(
            'SELECT state FROM launches WHERE session_id=?', (session_id,)
        ).fetchone()
        if row is not None:
            state = row['state']
            if state in {'exited', 'stopped_before_dispatch', 'launch_failed_unconfirmed', 'launch_failed_reported'}:
                return True
            if state in {'pending_admission', 'launching', 'running', 'aborting', 'deadline_stopping'}:
                return False
        try:
            reply = self.client.request('GET', f'/api/sessions/{session_id}')
        except ApiError as err:
            return err.status == 404
        except Exception:
            return False
        session = reply.get('session') if isinstance(reply, dict) else None
        if isinstance(session, dict):
            state = session.get('state')
            if state in {'stopped', 'finished', 'failed', 'refused', 'cancelled', 'expired', 'dead'}:
                return True
            if state in {'queued', 'held', 'offered', 'claimed', 'starting', 'running', 'uncertain'}:
                return False
        return False

    def offer(self, offer, acknowledge=None):
        db = self.journal.db
        session, epoch = offer.get('session_id'), offer.get('epoch')
        if 'recovery_native_id' in offer:
            prior = db.execute('SELECT * FROM launches WHERE operation_id=?', (offer.get('operation_id'),)).fetchone()
            recovery_native = offer.get('recovery_native_id')
            if (offer.get('claimed') is not True or not isinstance(recovery_native, str)
                    or not UUID_RE.fullmatch(recovery_native) or not prior
                    or prior['session_id'] != session or prior['epoch'] != epoch
                    or prior['native_id'] != recovery_native):
                raise ValueError('native_reconciliation_required')
        if isinstance(session, str) and type(epoch) is int and 1 <= epoch <= 2**63 - 1:
            if db.execute('SELECT 1 FROM stop_fences WHERE session_id=? AND epoch>=?', (session, epoch)).fetchone():
                return None
        launch = self.validate_launch(offer)
        prior = self.journal.db.execute('SELECT workspace,state FROM launches WHERE operation_id=?', (offer['operation_id'],)).fetchone()
        if prior and prior['state'] != 'pending_admission':
            # Reconciliation after an upgrade must never relocate a live worker.
            workspace = Path(prior['workspace'])
            source = Path(launch['workspace'])
            if workspace != source and not workspace.is_relative_to(source / '.local' / 'hub-worktrees'):
                raise ValueError('offer_fence_conflict')
        else:
            workspace = prepare_workspace(
                launch['workspace'],
                offer['launch'].get('purpose'),
                offer.get('intake_id'),
                offer.get('project_id'),
                session_is_dead=self.is_session_dead,
                allowed_roots=self.roots,
            )
        launch['workspace'] = str(workspace)
        requested_workspace = offer['launch']['workspace']
        workspace_binding = ({'version': 1, 'requested_workspace': requested_workspace,
                              'resolved_workspace': launch['workspace']}
                             if launch['workspace'] != requested_workspace or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', requested_workspace)
                             else None)
        def acknowledge_workspace():
            if acknowledge:
                if workspace_binding is None:
                    return acknowledge(session, epoch)
                else:
                    return acknowledge(session, epoch, workspace_binding)
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
        launch_reaper.remember(self.journal, operation, offer.get("expires_at"))
        prior = db.execute("SELECT * FROM launches WHERE operation_id=?", (operation,)).fetchone()
        if prior:
            if prior["session_id"] != session or prior["digest"] != digest or epoch < prior["epoch"]:
                raise ValueError("offer_fence_conflict")
            if epoch > prior["epoch"]:
                # A new fence cannot adopt a process under the old fence automatically.
                raise ValueError("native_reconciliation_required")
            if prior['state'] in {'stopped_before_dispatch', 'launch_failed_unconfirmed', 'launch_failed_reported'}:
                return None
            recovery_native = offer.get('recovery_native_id')
            if recovery_native is not None and recovery_native != prior['native_id']:
                raise ValueError('native_reconciliation_required')
            if prior['state']=='exited':
                if workspace_binding and acknowledge and recovery_native == prior['native_id']:
                    self.repair_label_finish(prior, workspace_binding, acknowledge_workspace, launch['timeout_seconds'])
                return prior['native_id']
            if prior['state'] == 'launching':
                if prior['native_dispatch_started'] == 0:
                    self.settle_undispatched_launch(prior)
                    return None
                self.abort_launch(operation)
                raise RuntimeError('native_abort_pending')
            if acknowledge and prior['state']!='pending_admission':
                acknowledge_workspace()
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
            if launch['engine']!='shell' and self.launcher.config.get('private_provider_homes', False) and not launch.get('managed_credential_label'):
                from .providers import provision
                launch['provider_home']=provision(self.client,offer,launch,self.journal.directory,self.launcher.config.get('routes'))
            if acknowledge:
                acknowledge_workspace()
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

    def repair_label_finish(self, row, binding, acknowledge, timeout_seconds):
        """One exact frozen terminal receipt, after a durable binding ACK echo.

        No native command is used: state=exited and this private receipt were
        committed together only after reconcile's native identity/retirement
        checks. The original body and idempotency key are never rewritten.
        """
        if row['workspace_repair_digest'] is not None:
            return
        native_id = row['native_id']
        if not isinstance(native_id, str) or not UUID_RE.fullmatch(native_id):
            return
        key, path = 'finish:' + row['operation_id'], f"/api/sessions/{row['session_id']}/finish"
        pending = self.journal.db.execute('SELECT * FROM outbox WHERE key=?', (key,)).fetchone()
        receipt = pending or self.journal.db.execute(
            "SELECT * FROM outbox_dead_letters WHERE key=? AND status=409 AND reason='unqualified_native_exit_evidence'", (key,)).fetchone()
        if not receipt or receipt['path'] != path or len(receipt['payload'].encode()) > 12288:
            return
        try:
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('duplicate_terminal_field')
                    value[key] = item
                return value
            body = json.loads(receipt['payload'], object_pairs_hook=unique_object)
            evidence = body['evidence']
            identity, exit_info, deadline = evidence['native_identity'], evidence['native_exit'], evidence['native_deadline']
            code, exited = native_returncode(exit_info), exit_info.get('exited_at_ms')
            now = int(time.time()*1000)
            if (body.get('operation_id') != row['operation_id'] or type(body.get('epoch')) is not int
                    or body['epoch'] != row['epoch'] or body.get('native_id') != native_id
                    or body.get('native_exit_confirmed') is not True or type(body.get('exit_code')) is not int
                    or ('launch_failed' in body and type(body['launch_failed']) is not bool)
                    or body['exit_code'] != code or evidence.get('source') not in {'aplexer_status', 'aplexer_retirement'}
                    or evidence.get('containment_empty') is not True or identity.get('id') != native_id
                    or identity.get('tag') != row['tag'] or row['tag'] != 'task-hub2-' + row['session_id']
                    or identity.get('workspace') != row['workspace'] or binding['resolved_workspace'] != row['workspace']
                    or type(exited) is not int or exited < 1 or exited > now + 5000
                    or type(row['runtime_deadline']) is not int or deadline.get('deadline_ms') != row['runtime_deadline']
                    or type(deadline.get('exceeded')) is not bool
                    or exited < row['runtime_deadline'] - timeout_seconds*1000
                    or (deadline['exceeded'] and row['runtime_deadline'] > now)
                    or (exited >= row['runtime_deadline'] and deadline['exceeded'] is not True)):
                return
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        # Old/delayed claim ACKs can match session+epoch without confirming
        # metadata. They must not consume the durable one-shot repair marker.
        if acknowledge() is not True:
            return
        digest = hashlib.sha256(receipt['payload'].encode()).hexdigest()
        with self.journal.db as db:
            current = db.execute('SELECT * FROM launches WHERE operation_id=?', (row['operation_id'],)).fetchone()
            if (not current or current['session_id'] != row['session_id'] or current['epoch'] != row['epoch']
                    or current['native_id'] != native_id or current['state'] != 'exited'
                    or current['digest'] != row['digest'] or current['workspace'] != row['workspace']
                    or current['tag'] != row['tag'] or current['runtime_deadline'] != row['runtime_deadline']
                    or current['workspace_repair_digest'] is not None):
                return
            source = db.execute('SELECT * FROM outbox WHERE key=?', (key,)).fetchone()
            if source and (source['path'] != path or source['payload'] != receipt['payload']):
                return
            if not source:
                source = db.execute("SELECT * FROM outbox_dead_letters WHERE key=? AND status=409 AND reason='unqualified_native_exit_evidence'", (key,)).fetchone()
                if not source or source['path'] != path or source['payload'] != receipt['payload']:
                    return
            db.execute('UPDATE launches SET workspace_repair_digest=? WHERE operation_id=?', (digest, row['operation_id']))
            db.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?)', (key, path, receipt['payload']))

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

    def settle_legacy_unobserved_launch(self, row, inventory):
        if (not isinstance(row['session_id'], str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', row['session_id'])
                or not isinstance(row['operation_id'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', row['operation_id'])
                or type(row['epoch']) is not int or row['epoch'] < 1 or row['tag'] != 'task-hub2-' + row['session_id']
                or not isinstance(row['workspace'], str) or len(row['workspace'].encode('utf-8')) > 1024):
            raise RuntimeError('legacy_launch_identity_unverified')
        evidence = inventory.absence_evidence(row['workspace'], row['tag'])
        if evidence is None:
            return
        operation, session, epoch = row['operation_id'], row['session_id'], row['epoch']
        payload = {'operation_id': operation, 'epoch': epoch, 'native_id': None,
                   'launch_failed': True, 'failure_kind': 'legacy_census_absent',
                   'native_outcome': 'unconfirmed', 'evidence': evidence}
        with self._inflight_lock:
            if session in self._inflight or not inventory.fresh():
                return
            with self.journal.db as db:
                changed = db.execute(
                    "UPDATE launches SET state='launch_failed_unconfirmed',legacy_failure_json=? "
                    "WHERE operation_id=? AND session_id=? AND epoch=? AND digest=? "
                    "AND workspace=? AND tag=? AND state=? AND state IN ('launching','aborting') "
                    "AND native_dispatch_started IS NULL AND native_id IS NULL",
                    (canonical(payload), operation, session, epoch, row['digest'], row['workspace'], row['tag'], row['state']))
                if changed.rowcount != 1:
                    return
                self.journal.enqueue(f"legacy-unobserved:{operation}:{epoch}", f"/api/sessions/{session}/finish", payload)

    def settle_legacy_census(self, inventory):
        rows = self.journal.db.execute("SELECT * FROM launches WHERE state='aborting' AND native_dispatch_started IS NULL "
                                       "AND native_id IS NULL LIMIT 1000").fetchall()
        for row in rows:
            try:
                self.settle_legacy_unobserved_launch(row, inventory)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                logging.getLogger(__name__).warning('Native census unusable; legacy launches remain unresolved')
                return

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

    def _report_native_observation(self, row):
        """Best-effort fixed diagnostic, bounded once per attempt/socket."""
        session, operation, epoch = row['session_id'], row['operation_id'], row['epoch']
        if (not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', session)
                or not isinstance(operation, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', operation)
                or type(epoch) is not int or not 1 <= epoch <= 9007199254740991):
            return
        socket = getattr(self, '_managed_socket', None)
        if socket is None:
            return  # Connection setup immediately reconciles again with its socket.
        with _NATIVE_OBSERVATION_REPORT_LOCK:
            if socket is not getattr(self, '_managed_socket', None):
                return
            state = getattr(self, '_native_observation_reports', None)
            if state is None or state[0] is not socket:
                state = self._native_observation_reports = (socket, set())
            key = (session, operation, epoch)
            if key in state[1] or len(state[1]) >= _NATIVE_OBSERVATION_REPORT_LIMIT:
                return
            state[1].add(key)  # Failed transport retries on the next connection.
        try:
            socket.send(canonical({'type': 'host.error', 'stage': 'host_observation',
                'reason': 'native_observation_unverified', 'detail': 'native_observation_unverified',
                'session_id': session, 'operation_id': operation, 'epoch': epoch}))
        except (RuntimeError, ValueError, OSError):
            pass  # Diagnostic transport never converts UNKNOWN into a resolved row.

    def reconcile(self):
        launch_reaper.reap_expired_launches(self)
        rows = self.journal.db.execute("SELECT * FROM launches WHERE state IN ('running','aborting','deadline_stopping','launching') LIMIT 1001").fetchall()
        if len(rows) > 1000:
            raise RuntimeError("active_native_bound_exceeded")
        for row in rows:
            if row['session_id'] in getattr(self, '_inflight', set()):
                continue
            if row['state'] in {'launching','aborting'} and row['native_dispatch_started'] == 0 and not row['native_id']:
                self.settle_undispatched_launch(row)
                continue
            interrupted_bound = row['state']=='launching' and bool(row['native_id'])
            if row['state']=='launching' and not interrupted_bound:
                # In-flight launches were excluded above; this is an interrupted dispatch.
                with self.journal.db:
                    self.journal.db.execute("UPDATE launches SET state='aborting' WHERE operation_id=? AND state='launching'",(row['operation_id'],))
                row = self.journal.db.execute("SELECT * FROM launches WHERE operation_id=?", (row['operation_id'],)).fetchone()
            if row['state']=='aborting' and row['native_dispatch_started'] is None and not row['native_id']:
                # Census collection runs off this control loop (single in-flight);
                # the worker also settles legacy rows as soon as it completes.
                inventory = launch_reaper.request_census(self)
                if inventory is None and not callable(getattr(self.launcher, 'inventory', None)):
                    logging.getLogger(__name__).warning('Native census unavailable; legacy launches remain unresolved')
                if inventory is not None:
                    try:
                        self.settle_legacy_unobserved_launch(row, inventory)
                    except (RuntimeError, OSError, subprocess.SubprocessError):
                        logging.getLogger(__name__).warning('Native census unusable; legacy launches remain unresolved')
                continue
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
            try:
                if workspace_available:
                    native = self.lookup(row["native_id"], row)
                else:
                    native = self.launcher.retired(row["native_id"], dict(row))
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
                self._report_native_observation(row)
                continue
            if not native:
                # Only automatic reconciliation may join a positive tombstone
                # to the original host launch evidence. Never reuse this for a
                # live operation or a missing-process-as-exit inference.
                matches = self.journal.db.execute("SELECT COUNT(*) FROM launches WHERE native_id=?",
                                                  (row['native_id'],)).fetchone()[0]
                current = self.journal.db.execute("SELECT * FROM launches WHERE operation_id=?", (row['operation_id'],)).fetchone()
                if (matches == 1 and current is not None
                        and all(current[key] == row[key] for key in ('session_id','epoch','digest','native_id','workspace','tag','state'))
                        and callable(getattr(self.launcher, 'retired_origin', None))):
                    try:
                        native = self.launcher.retired_origin(row['native_id'], dict(row))
                    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
                        self._report_native_observation(row)
                        continue
            if not native:
                if not workspace_available:
                    self._warn_unavailable_workspace(row)
                continue  # Missing process is uncertain, never a successful finish.
            if interrupted_bound:
                # Do not rewrite an interrupted bound row until identity qualifies.
                with self.journal.db:
                    self.journal.db.execute("UPDATE launches SET state='aborting' WHERE operation_id=? AND state='launching'",(row['operation_id'],))
                row = self.journal.db.execute("SELECT * FROM launches WHERE operation_id=?", (row['operation_id'],)).fetchone()
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
                    try:
                        native=self.lookup(row['native_id'],row)
                    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
                        self._report_native_observation(row)
                        continue
                if not native or native.get('phase',native.get('state'))!='exited':continue
            if native.get("containment_empty") is not True:
                continue
            exit_info = native.get("exit") or {}
            code = native_returncode(exit_info)
            if code is None:
                continue
            launcher_state = (
                "retired-finished" if not workspace_available or native.get("retirement") == "verified_origin"
                else self.launcher.finalize(row["native_id"], {"tag": row["tag"], "workspace": row["workspace"]})
            )
            payload = {"operation_id": row["operation_id"], "epoch": row["epoch"],
                       "native_id": row["native_id"], "exit_code": code,
                       "native_exit_confirmed": True,
                       "evidence": {"source": "aplexer_retirement" if native.get("retirement") else "aplexer_status",
                                    # lookup/retired already qualified UUID/tag and
                                    # samefile (or exact deleted retirement). Use
                                    # the pinned spelling across Windows aliases.
                                    "native_identity": {"id": native['id'], "tag": native['tag'], "workspace": row['workspace']},
                                    "containment_empty": True, "native_exit": exit_info,"launcher_state":launcher_state}}
            if native.get('retirement_origin'):
                payload['evidence']['retirement_origin'] = native['retirement_origin']
            if type(row['runtime_deadline']) is int:
                exited_at = exit_info.get('exited_at_ms')
                exceeded = native.get('deadline_exceeded') is True or (type(exited_at) is int and exited_at >= row['runtime_deadline'])
                payload['evidence']['native_deadline']={'deadline_ms':row['runtime_deadline'],'exceeded':exceeded}
            elif type(native.get('deadline_ms')) is int:
                payload['evidence']['native_deadline']={'deadline_ms':native['deadline_ms'],'exceeded':native.get('deadline_exceeded') is True}
            if row['state']=='aborting':payload['launch_failed']=True
            # All verified exits (normal, deadline and explicit stop) converge
            # here. Preserve committed work before journaling the finish, so a
            # continuation cannot race ahead of its salvage receipt.
            salvage = salvage_branch(row['workspace']) if workspace_available else None
            if salvage is not None:
                payload['evidence']['git_salvage'] = salvage
            if native.get('retirement') == 'verified_origin' and len(canonical(payload['evidence']).encode('utf8')) > 8192:
                logging.getLogger(__name__).warning('Origin retirement evidence too large session=%s', row['session_id'])
                continue
            with self.journal.db:
                if native.get('retirement') == 'verified_origin':
                    keys = ('operation_id','session_id','epoch','digest','native_id','workspace','tag','state')
                    changed = self.journal.db.execute("UPDATE launches SET state='exited' WHERE "
                        + ' AND '.join(key+'=?' for key in keys)
                        + ' AND (SELECT COUNT(*) FROM launches WHERE native_id=?)=1',
                        tuple(row[key] for key in keys)+(row['native_id'],))
                    if changed.rowcount != 1:
                        continue
                else:
                    self.journal.db.execute("UPDATE launches SET state='exited' WHERE operation_id=?", (row["operation_id"],))
                self.journal.db.execute("INSERT OR REPLACE INTO outbox VALUES(?,?,?)",
                                       (f'finish:{row["operation_id"]}', f'/api/sessions/{row["session_id"]}/finish', canonical(payload)))

    def bound_capture(self, command, row):
        """Read only this exact attempt; a finished task never falls back to a pane."""
        from .task_output import capture_task_output, bounded_capture, capture_tail, CAPTURE_OUTPUT_BYTES
        binding = command.get('capture_binding')
        expected_digest = hashlib.sha256(json.dumps({'kind':'capture','text':None,'epoch':command['epoch']},
            separators=(',',':')).encode()).hexdigest()
        if (not isinstance(binding, dict) or set(binding) != {'native_id','operation_id','request_digest'}
                or type(command.get('epoch')) is not int or command.get('mode') is not None or row['interactive'] != 0
                or binding.get('native_id') != row['native_id'] or not row['native_id']
                or binding.get('operation_id') != row['operation_id'] or binding.get('request_digest') != expected_digest
                or row['state'] not in {'running','exited'}):
            raise ValueError('capture_fence_conflict')
        terminal = row['state'] == 'exited'
        metadata = {'capture_binding':binding,'capture_source':'retained_task_output'}
        try:
            try:
                # Use private retained-file guards even if completion races the
                # journal snapshot taken above.
                output = capture_task_output('hub2-'+row['session_id'],row,row['native_id'],limit=CAPTURE_OUTPUT_BYTES,retained_only=True)
            except RuntimeError as exc:
                if not terminal and str(exc)=='task_output_unavailable':
                    output = None
                else:
                    raise
            if output is None:
                # Running legacy/central launches retain their existing native
                # capture path. Completion never reaches this branch.
                if not self._workspace_is_available(row['workspace']):
                    raise RuntimeError('task_output_unavailable')
                native_id,status = self.resolve_launch_native(row)
                current=self.journal.db.execute('SELECT state,epoch,native_id,operation_id FROM launches WHERE session_id=?',(row['session_id'],)).fetchone()
                if (not native_id or not status or status.get('phase',status.get('state'))!='running'
                        or not current or current['state']!='running' or current['epoch']!=row['epoch']
                        or current['native_id']!=row['native_id'] or current['operation_id']!=row['operation_id']):
                    raise RuntimeError('task_output_unavailable')
                output = capture_tail(self.native('capture',native_id,'--screen','--plain').stdout)
                metadata['capture_source'] = 'native_pane'
            return {**metadata,'output':bounded_capture(output,metadata)}
        except (OSError,ValueError,RuntimeError) as exc:
            reason = str(exc) if isinstance(exc,RuntimeError) and re.fullmatch('[a-z_]{1,80}',str(exc)) else 'task_output_unavailable'
            return {**metadata,'error':reason,'reason':reason}

    def command(self, command):
        if command.get("target_kind") == "native":
            return self.native_command(command)
        identity = command.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", identity):
            raise ValueError("invalid_command_id")
        digest = hashlib.sha256(canonical(command).encode()).hexdigest()
        db = self.journal.db
        old = db.execute("SELECT * FROM commands WHERE id=?", (identity,)).fetchone()
        if old:
            if old["digest"] != digest:
                # A server upgrade annotates legacy stop envelopes with their
                # original deadline. Match only that exact old envelope; never
                # repeat an executing stop or weaken any other identity field.
                legacy = {key: value for key, value in command.items() if key != 'deadline_ms'}
                legacy_digest = hashlib.sha256(canonical(legacy).encode()).hexdigest()
                if not (command.get('kind') == 'stop' and type(command.get('deadline_ms')) is int
                        and old['digest'] == legacy_digest):
                    raise ValueError("command_id_conflict")
            if old["state"] != "done":
                if old['state']=='executing' and command.get('kind')=='capture':
                    # Capture is read-only. Resolve interruption as failure, never
                    # repeat the read or weaken input/stop uncertainty fences.
                    result=self.capture_failure(command,'capture_outcome_uncertain')
                    with db:
                        self.finish_command(identity,command,result)
                    logging.getLogger(__name__).warning('Capture interrupted command_id=%s',identity)
                    return result
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
        if kind == 'capture':
            if not row['native_id']:
                native_id,_status=self.resolve_launch_native(row)
                if not native_id:
                    raise ValueError('command_fence_conflict')
                current=db.execute('SELECT * FROM launches WHERE session_id=?',(command['session_id'],)).fetchone()
                if not current or any(current[key]!=row[key] for key in ['epoch','operation_id']) or current['native_id']!=native_id:
                    raise ValueError('command_fence_conflict')
                row=current
            # This envelope is frozen at enqueue, so a completion race cannot
            # change the command digest on replay or introduce a native effect.
            with db:
                self.begin_command(identity,digest)
            try:
                result = self.bound_capture(command,row) if command.get('capture_binding') is not None else self.legacy_capture(command,row)
            except Exception:
                result = self.capture_failure(command,'capture_failed')
                logging.getLogger(__name__).warning('Capture failed command_id=%s',identity)
            with db:
                self.finish_command(identity,command,result)
            return result
        deadline = command.get('deadline_ms')
        bounded_stop = kind == 'stop' and type(deadline) is int
        until = time.monotonic() + min(5, max(0, (deadline - time.time() * 1000) / 1000)) if bounded_stop else None

        def remaining_stop():
            left = min(until - time.monotonic(), (deadline - time.time() * 1000) / 1000)
            if left <= 0:
                raise subprocess.TimeoutExpired('native_stop', 0)
            return left

        def unconfirmed_stop():
            return {'stopped': False, 'error': 'stop_unconfirmed', 'reason': 'stop_unconfirmed', 'stage': 'host_stop'}

        try:
            native_id, _status = (self.resolve_launch_native(row, timeout=remaining_stop())
                                  if bounded_stop else self.resolve_launch_native(row))
            if bounded_stop:
                remaining_stop()
        except subprocess.TimeoutExpired:
            if not bounded_stop:
                raise
            result = unconfirmed_stop()
            with db:
                self.begin_command(identity, digest)
                self.finish_command(identity, command, result)
            return result
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
        if kind == "stop":
            try:
                if bounded_stop:
                    self.native("kill", native_id, timeout=remaining_stop())
                    remaining_stop()
                else:
                    self.native("kill", native_id)
                birth = (_status or {}).get('created_at_ms')
                while True:
                    status = (self.lookup(native_id, row, timeout=remaining_stop())
                              if bounded_stop else self.lookup(native_id, row))
                    if bounded_stop:
                        remaining_stop()
                    if (status and status.get("phase", status.get("state")) == "exited"
                            and status.get("containment_empty") is True and native_returncode(status.get('exit')) is not None):
                        observed_birth = status.get('created_at_ms')
                        if type(birth) is int and status.get('retirement') == 'verified_finished':
                            tombstone = native_launch.read_tombstone(native_id, state_dir=self.launcher.state_dir)
                            observed_birth = (tombstone or {}).get('created_at_ms')
                        if type(birth) is int and observed_birth != birth:
                            raise RuntimeError('native_identity_mismatch')
                        if bounded_stop:
                            remaining_stop()
                        result = {'stopped': True, 'evidence': 'native_exit', 'native_id': native_id}
                        break
                    if not bounded_stop:
                        result = unconfirmed_stop()
                        break
                    time.sleep(min(.1, remaining_stop()))
            except subprocess.TimeoutExpired:
                result = unconfirmed_stop()
        else:
            text = command.get("text")
            if not isinstance(text, str) or len(text.encode()) > 16384:
                raise ValueError("invalid_command_text")
            self.native("send", native_id, "--stdin", "--enter", input=text, timeout=60)
            result = {"delivery": "native_acknowledged"}
        with db:
            self.finish_command(identity, command, result)
        return result

    def native_command(self, command):
        identity, native_id = command.get('id'), command.get('native_id')
        target = command.get('identity')
        if (command.get('protocol') != 1 or not isinstance(identity, str) or not UUID_RE.fullmatch(identity)
                or not isinstance(native_id, str) or not UUID_RE.fullmatch(native_id)
                or command.get('machine_id') != self.machine_id
                or not isinstance(target, dict) or set(target) != {'id', 'tag', 'workspace'}
                or target.get('id') != native_id or not isinstance(target.get('tag'), str)
                or not target['tag'] or len(target['tag'].encode()) > 512
                or not isinstance(target.get('workspace'), str) or not Path(target['workspace']).is_absolute()
                or len(target['workspace'].encode()) > 8192 or '\0' in target['workspace']
                or type(command.get('deadline_ms')) is not int
                or command.get('kind') not in {'stop', 'capture', 'send'}):
            raise ValueError('invalid_native_command')
        if command['kind'] == 'send':
            native_launch.native_submission(command)
        digest = hashlib.sha256(canonical(command).encode()).hexdigest()
        path = f"/api/machines/{self.machine_id}/native/{native_id}/commands/{identity}/ack"
        db = self.journal.db
        # Serialize on the command UUID even when socket deliveries overlap.
        with self._inflight_lock:
            lock = self._session_locks.setdefault('native-command:' + identity, threading.RLock())
        with lock:
            old = db.execute('SELECT * FROM commands WHERE id=?', (identity,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('command_id_conflict')
            def finish(result):
                payload = {'native_id': native_id, 'deadline_ms': command['deadline_ms'], 'result': result}
                with db:
                    db.execute("UPDATE commands SET state='done',result=? WHERE id=?", (canonical(result), identity))
                    self.journal.enqueue('command:' + identity, path, payload)
                return result
            if old:
                # No redispatch after a process died between native effect and receipt.
                return finish(json.loads(old['result']) if old['state'] == 'done'
                              else {'error': 'native_command_outcome_uncertain'})
            with db:
                self.begin_command(identity, digest)
            if command['deadline_ms'] <= time.time()*1000 or command['deadline_ms'] > time.time()*1000+90000:
                return finish({'error': 'native_command_deadline'})
            if db.execute('SELECT 1 FROM launches WHERE native_id=?', (native_id,)).fetchone():
                return finish({'error': 'native_target_managed'})
            try:
                return finish(self.launcher.native_control(command))
            except Exception as exc:
                reason = str(exc)
                if not re.fullmatch(r'[a-z_]{1,80}', reason):
                    reason = 'native_command_outcome_uncertain'
                return finish({'error': reason})

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

    def legacy_capture(self, command, row):
        from .task_output import capture_task_output, bounded_capture, capture_tail, CAPTURE_OUTPUT_BYTES
        native_id, _status = self.resolve_launch_native(row)
        if not native_id:
            raise RuntimeError('capture_identity_unavailable')
        output = (None if command.get('mode') == 'screen' else
                  capture_task_output('hub2-' + row['session_id'], row, native_id, limit=CAPTURE_OUTPUT_BYTES))
        if output is None:
            output = capture_tail(self.native('capture', native_id, '--screen', '--plain').stdout)
        return {'output': bounded_capture(output)}

    @staticmethod
    def capture_failure(command, reason):
        result={'error':reason,'reason':reason}
        if command.get('capture_binding') is not None:
            result.update(capture_binding=command['capture_binding'],capture_source='unavailable')
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
            elif event.get("type") == "engine.update":
                threading.Thread(target=self.run_engine_update, args=(event["job"],), daemon=True).start()
            elif event.get("type") == "resync":
                self.reconcile()
                self.journal.flush(self.client)
        except Exception as error:
            command=event.get('command')
            command_id=command.get('id') if event.get('type')=='session.command' and isinstance(command,dict) else None
            if not isinstance(command_id,str) or not re.fullmatch('[A-Za-z0-9-]{1,80}',command_id):
                command_id=None
            logging.getLogger(__name__).exception('Host event failed type=%s command_id=%s', event.get('type'),command_id)
            if isinstance(error,ApiError) and error.retry_at:
                self.budget_paused_until = max(getattr(self, 'budget_paused_until', 0), error.retry_at)
                raise
            raw = error.code if isinstance(error, ApiError) and error.code else str(error)
            cleaned = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(raw or '')).strip()
            head = cleaned.split(None, 1)[0] if cleaned else ''
            reason = head if re.fullmatch(r"[a-z_]{1,80}", head) else "host_operation_failed"
            detail = (cleaned or reason)[:240]
            target = event.get("command") or event.get("message") or event.get("offer") or {}
            stage = 'host_command'
            if event.get('type') == 'session.offer':
                stage = 'host_launch'
                row = self.journal.db.execute(
                    "SELECT state,native_dispatch_started FROM launches WHERE operation_id=? AND session_id=? AND epoch=?",
                    (target.get('operation_id'),target.get('session_id'),target.get('epoch'))).fetchone()
                if row and row['state'] in {'launching','running','aborting','deadline_stopping'} and row['native_dispatch_started'] != 0:
                    stage = 'host_launch_uncertain'
            frame = {"type": "host.error", "reason": reason, "stage": stage, "detail": detail,
                     "session_id": target.get("session_id"), "operation_id": target.get("operation_id"),
                     "message_id": target.get("id")}
            if stage == 'host_launch' and target.get("epoch") is not None:
                frame["epoch"] = target.get("epoch")
            socket.send(canonical(frame))

    def run_engine_update(self, job):
        job_id = job.get("id")
        version_command = job.get("version_command", "")
        update_command = job.get("update_command", "")
        version_re = re.compile(r"(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)")

        def _probe_version():
            if not version_command:
                return None
            try:
                proc = subprocess.run(version_command, shell=True, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
                out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                m = version_re.search(out)
                return m.group(1) if m else (out.splitlines()[0][:80] if proc.returncode == 0 and out else None)
            except Exception:
                return None

        old_version = _probe_version()
        ok = False
        exit_code = None
        err_text = None
        output = None
        try:
            proc = subprocess.run(update_command, shell=True, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
            exit_code = proc.returncode
            combined = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
            output = combined[-1500:] if combined else None
            if proc.returncode == 0:
                ok = True
            else:
                err_text = f"exit_code_{proc.returncode}" + (f": {combined.splitlines()[-1][:200]}" if combined else "")
        except subprocess.TimeoutExpired:
            err_text = "update_command_timeout"
        except Exception as exc:
            err_text = f"{type(exc).__name__}: {exc}"[:300]

        new_version = _probe_version()
        if self.machine_id and job_id:
            try:
                self.client.request(
                    "POST",
                    f"/api/machines/{self.machine_id}/engine-updates/{job_id}/ack",
                    {
                        "ok": ok,
                        "old_version": old_version,
                        "new_version": new_version,
                        "exit_code": exit_code,
                        "error": err_text,
                        "output": output,
                    },
                )
            except Exception:
                logging.getLogger(__name__).exception("Failed to ack engine update job_id=%s", job_id)
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
                try:
                    native_id,status=self.resolve_launch_native(row)
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
                    self._report_native_observation(row)
                    continue
                phase=str((status or {}).get('phase',(status or {}).get('state',
                    'launching' if row['state']=='launching' else 'missing')))
            activity=(status or {}).get('last_activity_ms',(status or {}).get('updated_at_ms',(status or {}).get('created_at_ms')))
            if type(activity) is not int:activity=None
            exit_code=native_returncode((status or {}).get('exit') or {}) if phase=='exited' else None
            result.append({'session_id':row['session_id'],'native_id':native_id,'phase':phase,
                           'last_activity_at':activity,'exit_code':exit_code,
                           'containment_empty':(status or {}).get('containment_empty') is True})
        return result

    def resolve_launch_native(self,row, *, timeout=None):
        expected={'tag':row['tag'],'workspace':row['workspace']}
        timeout_args = {} if timeout is None else {'timeout': timeout}
        if row['native_id']:
            return row['native_id'],self.lookup(row['native_id'],expected, **timeout_args)
        if not self._workspace_is_available(row['workspace']):
            self._warn_unavailable_workspace(row)
            return None,None
        if row['native_dispatch_started'] != 1:
            return None,None
        selector=f"{row['workspace']}:{row['tag']}"
        status=self.launcher.status(selector,workspace=row['workspace'], **timeout_args)
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

    def publish_managed_readiness(self, socket=None):
        """Bind optional code readiness to this daemon's actual current socket."""
        socket = socket if socket is not None else getattr(self, '_managed_socket', None)
        ready = getattr(self, '_managed_code_ready', False)
        if socket is None or not (ready or getattr(self, '_managed_ready_published', False)):
            return
        try:
            socket.send(canonical({'type':'managed.claude.ready','protocol':'hub2-managed-claude-exec-v1','ready':ready}))
            self._managed_ready_published = ready
        except Exception:
            pass  # A reconnect/observation failure never blocks normal host work.

    def heartbeat(self,labels,include_native_sessions=True):
        available=self.launcher.capabilities()
        engines=sorted(engine for engine in self.engines.intersection(available) if available[engine])
        capabilities=['headless']+[cap+':'+engine for engine in engines for cap in sorted(available[engine]-{'headless'})]
        advertisement = getattr(self, 'advertisement', None)
        resource_tags = advertisement.tags() if advertisement is not None else []
        workspace_tags = [tag for tag in resource_tags if tag.startswith('workspace:')]
        resource_tags = [tag for tag in resource_tags if not tag.startswith('workspace:')]
        # Accepted configuration leads the payload: when derived engine hints
        # overflow the 32-slot heartbeat they are shed by the bound below,
        # never an accepted tag, and never with an exception before the
        # socket/heartbeat connection.
        capabilities = list(dict.fromkeys(resource_tags + capabilities))
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
        if (len(capabilities) < 32 and 'claude' in engines and not configuration.get('private_provider_homes', False)
                and configuration.get('routes', {}).get('claude') == LOCAL_ROUTES['claude']):
            try:
                # Bounded known contexts only; a cold/label context stays unknown.
                contexts = []
                journal = getattr(self, 'journal', None)
                if journal is not None:
                    contexts = [row[0] for row in journal.db.execute(
                        'SELECT workspace FROM launches ORDER BY rowid DESC LIMIT 16').fetchall()]
                contexts += [str(root) for root in getattr(self, 'roots', [])[:4]]
                contexts = list(dict.fromkeys(value for value in contexts if isinstance(value, str) and value.startswith('/')))[:4]
                modes = [short for mode, short in (('headless', 'h'), ('interactive', 'i')) if mode in available['claude']]
                markers = local_claude_missing_contexts(contexts, modes, spawn_environment(False))
                capabilities.extend(markers[:32-len(capabilities)])
            except Exception:
                pass  # Read-only observation is never a heartbeat/start gate.
        self._managed_code_ready = False
        if len(capabilities) < 32 and 'claude' in engines:
            try:
                if self.launcher.managed_claude_readiness():
                    capabilities.append('managed:hub-claude:opus:medium:v1')
                    self._managed_code_ready = True
            except Exception:
                pass  # Code readiness is an optional observation, never a heartbeat gate.
        if len(capabilities) < 32:
            # This fixed, non-configurable marker fences new repo-only routing
            # from old hosts that could otherwise advertise a matching alias.
            capabilities.append(REPOSITORY_IDENTITY_CAPABILITY)
        else:
            # Preserve the 32 non-workspace capability bound and every legacy
            # label. Without a marker slot, repo-only placement is unavailable.
            qualified = [tag for tag in workspace_tags if tag.startswith('workspace:')
                         and QUALIFIED_WORKSPACE.fullmatch(tag[len('workspace:'):])]
            if qualified:
                qualified_set = set(qualified)
                workspace_tags = [tag for tag in workspace_tags if tag not in qualified_set]
                if INVENTORY_OVERFLOW_TAG not in workspace_tags:
                    workspace_tags.append(INVENTORY_OVERFLOW_TAG)
        # Workspace inventory has its own protocol allowance; repository count
        # must never evict engine/credential hints or silently lose clone labels.
        capabilities.extend(workspace_tags)
        if include_native_sessions:
            self.client.heartbeat(labels,engines,capabilities,native_sessions=self.live_sessions())
            self.publish_managed_readiness()
            try:
                credential_delivery.request(self)  # Bounded single-inflight daemon worker; never waits.
            except Exception:
                pass
            # Observation transport is independent of managed lifecycle/admission.
            # Failed/unsupported collection never publishes an empty replacement.
            inventory = getattr(self.launcher, 'inventory', None)
            publish = getattr(self.client, 'publish_native_inventory', None)
            if self.machine_id and callable(inventory) and callable(publish):
                # One observational worker per bridge; collection/upload must not
                # delay connection, offers, managed heartbeats or safety stops.
                with _NATIVE_INVENTORY_START_LOCK:
                    stopped = getattr(self, '_native_inventory_stop', None)
                    if stopped is None:
                        stopped = self._native_inventory_stop = threading.Event()
                    if stopped.is_set():
                        return
                    previous = getattr(self, '_native_inventory_thread', None)
                    if previous is None or not previous.is_alive():
                        machine_id = self.machine_id
                        def publish_observation():
                            try:
                                observation = inventory()
                                if not stopped.is_set():
                                    publish(machine_id, observation, stop_event=stopped)
                            except Exception:
                                logging.getLogger(__name__).warning('Native inventory publication unavailable')
                        self._native_inventory_thread = threading.Thread(target=publish_observation, name='hub2-native-inventory', daemon=True)
                        self._native_inventory_thread.start()
        else:
            self.client.heartbeat(labels,engines,capabilities)
            self.publish_managed_readiness()

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
        with _NATIVE_INVENTORY_START_LOCK:
            previous_stop = getattr(self, '_native_inventory_stop', None)
            if previous_stop is not None:
                previous_stop.set()
            self._native_inventory_stop = threading.Event()
        credential_delivery.start(self)
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
            with _NATIVE_INVENTORY_START_LOCK:
                self._native_inventory_stop.set()
            credential_delivery.stop(self)
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
                self._managed_socket = None
                self.heartbeat(labels)
                with self.client.socket(machine_id) as socket:
                    self._managed_socket = socket
                    self.publish_managed_readiness(socket)
                    logging.getLogger(__name__).info('Host connected machine=%s', machine_id)
                    waiters, waiter_lock = {}, threading.Lock()
                    pool, commands, slots = self._launch_pool, self._command_pool, self._worker_slots
                    def acknowledge(session, epoch, workspace_binding=None, _socket=socket, _waiters=waiters, _lock=waiter_lock):
                        ready = OfferAcknowledgement(workspace_binding)
                        with _lock:
                            _waiters[(session, epoch)] = ready
                        try:
                            _socket.send(offer_ack_frame(session, epoch, workspace_binding))
                            return ready.wait(15)
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
                                        ready.accept(event)
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
                            elif event.get('type') == 'session.command' and event.get('command', {}).get('target_kind') == 'native':
                                session = 'native:' + str(event['command'].get('native_id'))
                                with self._inflight_lock:
                                    self._session_locks.setdefault(session, threading.RLock())
                                if not slots.acquire(blocking=False):
                                    raise RuntimeError('host_command_queue_full')
                                commands.submit(execute, event, session)
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
                self._managed_socket = None
                logging.getLogger(__name__).exception('Host disconnected machine=%s', machine_id)
                if isinstance(error,ApiError) and error.retry_at:
                    self.budget_paused_until=max(self.budget_paused_until,error.retry_at)
                    if safety:self.budget_safety_paused_until=max(self.budget_safety_paused_until,error.retry_at)
                # Backoff does not issue empty-queue reads or claim a launch completed.
                time.sleep(delay)
                delay = min(delay * 2, 60)
