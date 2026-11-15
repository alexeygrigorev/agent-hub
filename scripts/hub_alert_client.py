"""Immutable stdlib API-v1 producer client; no dispatcher or broker imports."""
import json,os,re,select,socket,struct,time,uuid
class ram:
    ROLES={'canary':'start_canary','quota_publish':'quota_stale','quota_monitor':'quota_stale','backup_pull':'backup','backup_notifier':'backup'}
    CLAIM_ROLES={'intake_mailer':'intake_mailer'}
    DECISIONS={'observation':False,'alert':True,'alert_dedup':True,'recovery':True}
    @staticmethod
    def identity(value):return type(value) is str and re.fullmatch('[0-9a-f]{32}',value) is not None

FRAME = 2048

REPLY = 8192

BUDGET = 0.15

CONDITIONS = frozenset(('pass', 'recovering', 'failure_below_threshold', 'recovery', 'critical', 'critical_dedup', 'lock_busy', 'deploy_gate', 'metadata_only', 'error', 'backup_failed', 'stale', 'fresh', 'recovery_pending', 'quota_stale', 'quota_fresh', 'quota_recovery_pending', 'quota_publication', 'health_critical', 'backup_begin', 'backup_terminal', 'drill', 'pull_terminal', 'intake_recovery','host_disconnected','mailer_api_error','mailer_render_error','mailer_state_error','mailer_helper_error','mailer_helper_timeout','mailer_journal_error','mailer_scan_complete','mailer_scan_empty','mailer_scan_incomplete','mailer_input_error'))

FLAGS = frozenset(('test', 'drill', 'no_mail', 'dry_run', 'prior_stale', 'prior_drill', 'stale_drill', 'repeat_due', 'decision_seen', 'exit_success', 'intake_recovery', 'host_disconnected', 'log_requested'))

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
    require(type(frame['v']) is int and frame['v'] == 1 and ram.identity(frame['invocation_id']) and (frame['parent_id'] is None or ram.identity(frame['parent_id'])) and (type(frame['role']) is str) and (frame['role'] in (ram.ROLES.keys() | ram.CLAIM_ROLES.keys())) and (frame['phase'] in ('begin', 'decision', 'terminal')) and (type(frame['seq']) is int) and (0 <= frame['seq'] < 16) and (type(frame['observer_ok']) is bool))
    require(type(frame['flags']) is dict and set(frame['flags']) <= FLAGS and all((type(v) is bool for v in frame['flags'].values())) and (frame['recipient_class'] in ('dev', 'none')))
    if frame['phase'] == 'decision':
        require(frame['decision'] in ram.DECISIONS and frame['condition'] in CONDITIONS)
    else:
        require(frame['decision'] is None and frame['condition'] is None)
    if frame['role'] in ram.CLAIM_ROLES:
        require(frame['parent_id'] is None and set(frame['flags']) <=
            {'log_requested','exit_success','intake_recovery','host_disconnected'})
        if frame['phase'] == 'decision':
            branch = frame['condition'] in ('intake_recovery','host_disconnected')
            require((branch and frame['decision'] in ('alert','alert_dedup')
                and frame['flags'].get(frame['condition']) is True
                and set(frame['flags']) <= {'log_requested',frame['condition']})
                or (not branch and frame['decision'] == 'observation'
                    and frame['condition'].startswith('mailer_')
                    and set(frame['flags']) <= {'log_requested'}))
        else:
            require(set(frame['flags']) <= ({'log_requested','exit_success'}
                if frame['phase'] == 'terminal' else {'log_requested'}))
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
    """One top-level quota/mailer SOURCE CLAIM, never an admitted invocation.

    No environment mutation, inherited context adoption, child spawn or retry.
    Backup/canary owner integration is deliberately outside this first splice.
    """
    env = os.environ if env is None else env
    require(type(role) is str and role in ('quota_publish','quota_monitor','intake_mailer') and parent_id is None)
    require(type(socket_path) is str and socket_path.startswith('/') and '\0' not in socket_path
        and len(os.fsencode(socket_path)) <= 107 and '..' not in socket_path.split('/'))
    require(not any(key in env for key in _CONTEXT_KEYS))
    context = uuid.uuid4().hex
    require(ram.identity(context) and context not in _CLAIMED_SOURCE_CONTEXTS
        and len(_CLAIMED_SOURCE_CONTEXTS) < 128)
    _CLAIMED_SOURCE_CONTEXTS.add(context)  # Burn before use; failed delivery never replays it.
    return Child({'HUB_ALERT_BROKER_SOCKET':socket_path,'HUB_ALERT_INVOCATION_ID':context,
        'HUB_ALERT_ROLE':role},deadline=deadline)


class SourceSessionError(Exception):
    """Only fixed protocol reasons may cross the local source-session pipe."""


def source_nonblocking(fd):
    import stat
    mode = os.fstat(fd).st_mode
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        raise SourceSessionError('transport_unavailable')
    if stat.S_ISSOCK(mode):
        # Packet os.read can silently discard an oversized packet suffix.
        # Only byte streams preserve the complete NDJSON/total-byte budget.
        duplicate = os.dup(fd)
        try:
            endpoint = socket.socket(fileno=duplicate)
        except BaseException:
            os.close(duplicate)
            raise
        try:
            if endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
                raise SourceSessionError('transport_unavailable')
        finally:
            endpoint.close()
    original = os.get_blocking(fd)
    os.set_blocking(fd, False)
    return original


def source_options(argv):
    allowed = {'--socket', '--role', '--parent-source-claim', '--lifetime-seconds'}
    if not argv or argv[0] != 'source-session' or len(argv) % 2 != 1:
        raise SourceSessionError('invalid_arguments')
    options = {}
    for index in range(1, len(argv), 2):
        key, value = argv[index:index + 2]
        if key not in allowed or key in options:
            raise SourceSessionError('invalid_arguments')
        options[key] = value
    role, path = options.get('--role'), options.get('--socket', '')
    parent = options.get('--parent-source-claim')
    lifetime = options.get('--lifetime-seconds', '900')
    if (role not in ('backup_pull', 'backup_notifier') or not path.startswith('/')
            or '\0' in path or '..' in path.split('/') or len(os.fsencode(path)) > 107
            or not re.fullmatch('[1-9][0-9]{0,2}', lifetime) or int(lifetime) > 900
            or parent is not None and (role != 'backup_notifier' or not ram.identity(parent))):
        raise SourceSessionError('invalid_arguments')
    if any(key in os.environ for key in _CONTEXT_KEYS):
        raise SourceSessionError('inherited_context')
    return path, role, parent, int(lifetime)


def source_write(fd, value, deadline):
    """One bounded local reply, never a business action or broker retry."""
    raw = encode(value, 511) + b'\n'
    end = min(deadline, time.monotonic() + BUDGET)
    while raw:
        if not select.select([], [fd], [], left(end))[1]:
            raise SourceSessionError('output_unavailable')
        try:
            count = os.write(fd, raw)
        except BlockingIOError:
            continue
        if count <= 0:
            raise SourceSessionError('output_unavailable')
        raw = raw[count:]
        left(end)


def source_event(raw, child, count):
    try:
        value = decode(raw, 1024)
        require(type(value) is dict and 'phase' in value and set(value) <= {
            'phase', 'decision', 'condition', 'flags', 'recipient_class'})
        frame = dict(v=1, invocation_id=child.identity, parent_id=child.parent_id,
            role=child.role, phase=value['phase'], seq=child.seq,
            decision=value.get('decision'), condition=value.get('condition'),
            flags=value.get('flags', {}), recipient_class=value.get('recipient_class', 'none'),
            observer_ok=not child.dirty)
        checked(frame)
        require((count == 0 and frame['phase'] == 'begin')
            or (count > 0 and frame['phase'] != 'begin'))
        return {key: frame[key] for key in ('phase', 'decision', 'condition', 'flags', 'recipient_class')}
    except Exception:
        raise SourceSessionError('invalid_event') from None


def run_source_session(argv, input_fd=0, output_fd=1):
    """One stable-PID backup source CLAIM; never admitted or complete authority.

    EOF does not synthesize a terminal. A valid terminal ends this helper;
    callers must match each typed sequence ACK and never replay business.
    """
    child, original = None, {}
    deadline = time.monotonic() + BUDGET
    try:
        path, role, parent, lifetime = source_options(argv)
        deadline = time.monotonic() + lifetime
        for fd in (output_fd, input_fd):
            if fd not in original:
                original[fd] = source_nonblocking(fd)
        child = Client(path, uuid.uuid4().hex, parent, role, deadline=deadline)
        source_write(output_fd, {'kind': 'hub_alert_source_session_v1',
            'state': 'ready_unqualified', 'role': role, 'admitted': False,
            'source_claim_id': child.identity, 'parent_source_claim': parent}, deadline)
        buffer, total, count = b'', 0, 0
        while count < 8:
            while b'\n' not in buffer:
                if len(buffer) > 1024:
                    raise SourceSessionError('input_limit')
                if not select.select([input_fd], [], [], left(deadline))[0]:
                    raise SourceSessionError('deadline')
                try:
                    chunk = os.read(input_fd, 1025)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise SourceSessionError('eof')
                total += len(chunk)
                if total > 4096:
                    raise SourceSessionError('input_limit')
                buffer += chunk
            raw, buffer = buffer.split(b'\n', 1)
            event = source_event(raw, child, count)
            if event['phase'] == 'terminal' and buffer.strip():
                raise SourceSessionError('invalid_event')
            sequence = child.seq
            broker_ok = child.emit(**event)
            count += 1
            source_write(output_fd, {'kind': 'hub_alert_source_session_ack_v1',
                'seq': sequence, 'local_ok': True, 'broker_ok': broker_ok,
                'observer_ok': not child.dirty, 'admitted': False}, deadline)
            if event['phase'] == 'terminal':
                return 0
        raise SourceSessionError('event_limit')
    except BaseException as error:
        if child is not None:
            child.dirty = True
        if not isinstance(error, Exception):
            raise
        reason = str(error) if isinstance(error, SourceSessionError) else 'transport_unavailable'
        try:
            if output_fd not in original:
                original[output_fd] = source_nonblocking(output_fd)
            source_write(output_fd, {'kind': 'source_session_error_v1', 'reason': reason,
                'admitted': False}, deadline)
        except Exception:
            pass  # Closed/stalled output is loss, never an ACK or retry.
        return 2
    finally:
        for fd, blocking in original.items():
            try:
                os.set_blocking(fd, blocking)
            except OSError:
                pass


def source_session_main(argv=None):
    import sys
    try:
        return run_source_session(sys.argv[1:] if argv is None else argv)
    except BaseException:
        # Control interruption ends the observer process; no raw traceback/data.
        return 130


if __name__ == '__main__':
    raise SystemExit(source_session_main())
