"""Immutable stdlib API-v1 producer client; no dispatcher or broker imports."""
import json,os,re,select,socket,struct,time,uuid
class ram:
    ROLES={'canary':'start_canary','quota_publish':'quota_stale','quota_monitor':'quota_stale','backup_pull':'backup','backup_notifier':'backup'}
    DECISIONS={'observation':False,'alert':True,'alert_dedup':True,'recovery':True}
    @staticmethod
    def identity(value):return type(value) is str and re.fullmatch('[0-9a-f]{32}',value) is not None

FRAME = 2048

REPLY = 8192

BUDGET = 0.15

CONDITIONS = frozenset(('pass', 'recovering', 'failure_below_threshold', 'recovery', 'critical', 'critical_dedup', 'lock_busy', 'deploy_gate', 'metadata_only', 'error', 'backup_failed', 'stale', 'fresh', 'recovery_pending', 'quota_stale', 'quota_fresh', 'quota_recovery_pending', 'quota_publication', 'health_critical', 'backup_begin', 'backup_terminal', 'drill', 'pull_terminal'))

FLAGS = frozenset(('test', 'drill', 'no_mail', 'dry_run', 'prior_stale', 'prior_drill', 'stale_drill', 'repeat_due', 'decision_seen', 'exit_success'))

class Refused(Exception):
    pass

def require(ok):
    if not ok:
        raise Refused('alert_observation_unavailable')

def unique(pairs):
    out = {}
    for key, value in pairs:
        require(key not in out)
        out[key] = value
    return out

def decode(raw, limit):
    require(type(raw) is bytes and 0 < len(raw) <= limit)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=lambda _: require(False))

def encode(value, limit):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    require(len(raw) <= limit)
    return raw

def checked(frame):
    require(type(frame) is dict and set(frame) == {'v', 'invocation_id', 'parent_id', 'role', 'phase', 'seq', 'decision', 'condition', 'flags', 'recipient_class', 'observer_ok'})
    require(type(frame['v']) is int and frame['v'] == 1 and ram.identity(frame['invocation_id']) and (frame['parent_id'] is None or ram.identity(frame['parent_id'])) and (type(frame['role']) is str) and (frame['role'] in ram.ROLES) and (frame['phase'] in ('begin', 'decision', 'terminal')) and (type(frame['seq']) is int) and (0 <= frame['seq'] < 16) and (type(frame['observer_ok']) is bool))
    require(type(frame['flags']) is dict and set(frame['flags']) <= FLAGS and all((type(v) is bool for v in frame['flags'].values())) and (frame['recipient_class'] in ('dev', 'none')))
    if frame['phase'] == 'decision':
        require(frame['decision'] in ram.DECISIONS and frame['condition'] in CONDITIONS)
    else:
        require(frame['decision'] is None and frame['condition'] is None)
    return frame

def left(deadline):
    result = deadline - time.monotonic()
    require(result > 0)
    return result

def wait(sock, write, deadline):
    ready = select.select([] if write else [sock], [sock] if write else [], [], left(deadline))
    require(bool(ready[1] if write else ready[0]))

def exchange(path, request, *, deadline=None):
    """One request, no reconnect/replay. TOTAL <=150ms, including connect/ACK.

    This is observation only: callers execute their original business action
    once regardless of failure. No spawn API is exposed here.
    """
    end = time.monotonic() + BUDGET
    if deadline is not None:
        end = min(end, deadline)
    left(end)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        sock.setblocking(False)
        left(end)
        try:
            sock.connect(str(path))
        except BlockingIOError:
            wait(sock, True, end)
        require(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0)
        pid, uid, _gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        require(uid == os.getuid() and pid > 0)
        raw = encode(request, FRAME)
        wait(sock, True, end)
        require(sock.send(raw) == len(raw))
        wait(sock, False, end)
        raw, _anc, flags, _address = sock.recvmsg(REPLY + 1)
        require(not flags & socket.MSG_TRUNC)
        answer = decode(raw, REPLY)
        left(end)
        return answer
    finally:
        sock.close()

class Client:
    """Existing API-v1 emit interface, now one bounded Unix exchange per frame."""

    def __init__(self, path, invocation_id, parent_id, role, *, deadline=None):
        self.path, self.identity, self.parent_id, self.role = (path, invocation_id, parent_id, role)
        self.seq, self.dirty = (0, False)
        self.deadline = deadline

    def emit(self, phase, *, decision=None, condition=None, flags=None, recipient_class=None):
        try:
            end = time.monotonic() + BUDGET
            if self.deadline is not None:
                end = min(end, self.deadline)
            left(end)
            frame = dict(v=1, invocation_id=self.identity, parent_id=self.parent_id, role=self.role, phase=phase, seq=self.seq, decision=decision, condition=condition, flags=flags or {}, recipient_class=recipient_class or ('dev' if decision in ('alert', 'alert_dedup', 'recovery') else 'none'), observer_ok=not self.dirty)
            self.seq += 1
            checked(frame)
            answer = exchange(self.path, {'v': 1, 'op': 'hook', 'frame': frame}, deadline=end)
            require(type(answer) is dict and set(answer) == {'seq', 'ok'} and (type(answer['seq']) is int) and (answer['seq'] == frame['seq']) and (answer['ok'] is True) and (not self.dirty))
            return True
        except BaseException as error:
            self.dirty = True
            if not isinstance(error, Exception):
                raise
            return False

class Child(Client):
    def __init__(self, env=None, *, deadline=None):
        env=os.environ if env is None else env
        self.active=bool(env.get('HUB_ALERT_BROKER_SOCKET'))
        super().__init__(env.get('HUB_ALERT_BROKER_SOCKET'),env.get('HUB_ALERT_INVOCATION_ID'),
            env.get('HUB_ALERT_PARENT_ID') or None,env.get('HUB_ALERT_ROLE','canary'),deadline=deadline)
    def emit(self, phase, **kwargs):
        if not self.active:return False
        return super().emit(phase,**kwargs)


_CLAIMED_SOURCE_CONTEXTS = set()
_CONTEXT_KEYS = ('HUB_ALERT_INVOCATION_ID','HUB_ALERT_PARENT_ID','HUB_ALERT_ROLE',
    'HUB_ALERT_EVENT_FD','HUB_ALERT_ACK_FD','HUB_ALERT_BROKER_SOCKET',
    'HUB_ALERT_CONTROLLER_DIR','HUB_ALERT_LEDGER_DIR')


def positive_only_context(role, *, socket_path, parent_id=None, deadline=None, env=None):
    """One top-level quota SOURCE CLAIM, never an admitted invocation.

    No environment mutation, inherited context adoption, child spawn or retry.
    Backup/canary owner integration is deliberately outside this first splice.
    """
    env = os.environ if env is None else env
    require(type(role) is str and role in ('quota_publish','quota_monitor') and parent_id is None)
    require(type(socket_path) is str and socket_path.startswith('/') and '\0' not in socket_path
        and len(os.fsencode(socket_path)) <= 107 and '..' not in socket_path.split('/'))
    require(not any(key in env for key in _CONTEXT_KEYS))
    context = uuid.uuid4().hex
    require(ram.identity(context) and context not in _CLAIMED_SOURCE_CONTEXTS
        and len(_CLAIMED_SOURCE_CONTEXTS) < 128)
    _CLAIMED_SOURCE_CONTEXTS.add(context)  # Burn before use; failed delivery never replays it.
    return Child({'HUB_ALERT_BROKER_SOCKET':socket_path,'HUB_ALERT_INVOCATION_ID':context,
        'HUB_ALERT_ROLE':role},deadline=deadline)
