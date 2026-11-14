"""Optional resident metadata observer. No admissions or complete authority.

No I/O occurs on import. All evidence failure is local to this component.
The host's websocket/launch/heartbeat threads never wait for the collector.
"""
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import sys
import threading
import time

from . import alert_authority as ram

FRAME = 2048
REPLY = 8192
BUDGET = .150
MAX_REQUESTS = 4096
MAX_JOBS = 256
MAX_UNQUALIFIED_FRAMES = 256
MAX_UNQUALIFIED_BYTES = 262144
MAX_UNQUALIFIED_CONTEXTS = 128
CONDITIONS = frozenset(('pass','recovering','failure_below_threshold','recovery','critical',
    'critical_dedup','lock_busy','deploy_gate','metadata_only','error','backup_failed','stale',
    'fresh','recovery_pending','quota_stale','quota_fresh','quota_recovery_pending',
    'quota_publication','health_critical','backup_begin','backup_terminal','drill','pull_terminal'))
FLAGS = frozenset(('test','drill','no_mail','dry_run','prior_stale','prior_drill','stale_drill',
                  'repeat_due','decision_seen','exit_success'))


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
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: require(False))


def encode(value, limit):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    require(len(raw) <= limit)
    return raw


def checked(frame):
    # API-v1 schema; no imported mutable script companion or free-form reasons.
    require(type(frame) is dict and set(frame) == {'v','invocation_id','parent_id','role',
        'phase','seq','decision','condition','flags','recipient_class','observer_ok'})
    require(type(frame['v']) is int and frame['v'] == 1 and ram.identity(frame['invocation_id'])
        and (frame['parent_id'] is None or ram.identity(frame['parent_id']))
        and type(frame['role']) is str and frame['role'] in ram.ROLES
        and frame['phase'] in ('begin','decision','terminal')
        and type(frame['seq']) is int and 0 <= frame['seq'] < 16
        and type(frame['observer_ok']) is bool)
    require(type(frame['flags']) is dict and set(frame['flags']) <= FLAGS
        and all(type(v) is bool for v in frame['flags'].values())
        and frame['recipient_class'] in ('dev','none'))
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
    left(end)  # An already expired call must not create/connect a socket.
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
        self.path, self.identity, self.parent_id, self.role = path, invocation_id, parent_id, role
        self.seq, self.dirty = 0, False
        self.deadline = deadline

    def emit(self, phase, *, decision=None, condition=None, flags=None, recipient_class=None):
        try:
            end = time.monotonic() + BUDGET
            if self.deadline is not None:
                end = min(end, self.deadline)
            left(end)
            frame = dict(v=1, invocation_id=self.identity, parent_id=self.parent_id, role=self.role,
                phase=phase, seq=self.seq, decision=decision, condition=condition, flags=flags or {},
                recipient_class=recipient_class or ('dev' if decision in ('alert','alert_dedup','recovery') else 'none'),
                observer_ok=not self.dirty)
            self.seq += 1
            checked(frame)
            answer = exchange(self.path, {'v':1,'op':'hook','frame':frame}, deadline=end)
            require(type(answer) is dict and set(answer) == {'seq','ok'}
                and type(answer['seq']) is int and answer['seq'] == frame['seq']
                and answer['ok'] is True and not self.dirty)
            return True
        except BaseException as error:
            self.dirty = True
            if not isinstance(error, Exception):
                raise
            return False


def process_key(pid):
    """Only kernel identity metadata; never cmdline/environ or executable argv."""
    require(type(pid) is int and pid > 0)
    raw = Path(f'/proc/{pid}/stat').read_bytes()
    require(len(raw) <= 8192)
    tail = raw[raw.rindex(b')') + 2:].split()
    birth = int(tail[19])
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return ram.ProcessKey(pid, birth, boot).checked()


def private_directory(path):
    """FD-walk; no following mutable parent symlinks or pathname cleanup."""
    require(path.is_absolute() and '..' not in path.parts)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd); fd = nxt
            s = os.fstat(fd)
            require(s.st_uid in (0, os.getuid()) and not s.st_mode & 0o002)
        s = os.fstat(fd)
        require(s.st_uid == os.getuid() and stat.S_IMODE(s.st_mode) == 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


class Broker:
    def __init__(self, directory, pins, *, collector_factory=None, identity=process_key):
        self.directory = Path(directory)
        self.path = self.directory / 'observer.sock'
        self.ram = ram.LiveRAM(pins, checked)
        # No independently qualified dispatcher/source-open/system/cron binding
        # exists in this increment. No hook or job is allowed to self-admit.
        self.ram.poison(ram.PRODUCERS, 'dispatch_unqualified')
        self.identity = identity
        self.factory = collector_factory or self._collector
        self.stop_event = threading.Event()
        self.thread = None
        self.listener = None
        self.socket_identity = None
        self.directory_fd = None
        self.node_fd = None
        self.collector = None
        self.events = []
        self.unqualified_frames = []
        self.unqualified_bytes = 0
        self.unqualified_contexts = {}
        self.requests = 0
        self.closed = False

    def _collector(self, callback):
        from . import alert_collector as dbus
        return dbus.Collector(dbus.LibWire(), 'user', dbus.FIXED['user'], callback)

    def job(self, value):
        try:
            require(len(self.events) < MAX_JOBS)
            self.events.append(dict(value))
            # Job metadata is retained, not an execution admission.
            return True
        except BaseException:
            self.ram.collector_lost({'quota_stale'})
            raise

    def request(self, value, peer):
        try:
            require(type(peer) is tuple and len(peer) == 3 and peer[1] == os.getuid())
            before = self.identity(peer[0])
            require(type(value) is dict and value.get('v') == 1 and type(value['v']) is int)
            if value.get('op') == 'hook':
                require(set(value) == {'v','op','frame'})
                frame = checked(value['frame'])
                require(self.identity(peer[0]) == before)
                self.record_unqualified(frame, before)
                # No admission endpoint. Unadmitted frames receive negative ACK
                # from RAM and poison evidence instead of manufacturing counts.
                reply = self.ram.hook(frame, frame['invocation_id'], before)
            else:
                require(value.get('op') == 'snapshot' and set(value) == {'v','op','nonce'})
                reply = self.ram.barrier(value['nonce'], self.drain)
                reply['manager_observations'] = list(self.events[-8:])
                reply['manager_metadata_omitted'] = max(0, len(self.events) - 8)
                reply['unqualified_hook_frames'] = list(self.unqualified_frames[-2:])
                reply['unqualified_hook_omitted'] = max(0, len(self.unqualified_frames) - 2)
                reply['limitations'] = ['unqualified_dispatch_and_source_open','system_and_direct_cron_unqualified']
            require(self.identity(peer[0]) == before)
            return reply
        except BaseException:
            self.ram.collector_lost(ram.PRODUCERS)
            raise

    def record_unqualified(self, frame, process):
        """Positive-only source CLAIMS, never admission or authority counters.

        Strict context/sequence binding prevents mixed/replayed claims. Even
        successful retention leaves RAM's unadmitted negative ACK unchanged.
        """
        context = frame['invocation_id']
        previous = self.unqualified_contexts.get(context)
        if previous is None:
            require(frame['phase'] == 'begin' and frame['seq'] == 0
                and len(self.unqualified_contexts) < MAX_UNQUALIFIED_CONTEXTS)
        else:
            require(previous['process'] == process and previous['role'] == frame['role']
                and previous['parent'] == frame['parent_id'] and not previous['terminal']
                and frame['seq'] == previous['seq'] + 1 and frame['phase'] != 'begin')
        record = {'kind':'unqualified_api_v1_hook','qualification':'none',
            'broker_epoch':self.ram.epoch,'peer_process':{'pid':process.pid,
                'birth_ticks':process.birth_ticks,'boot_id':process.boot_id},'frame':dict(frame)}
        size = len(encode(record, 4096))
        require(len(self.unqualified_frames) < MAX_UNQUALIFIED_FRAMES
            and self.unqualified_bytes + size <= MAX_UNQUALIFIED_BYTES)
        self.unqualified_frames.append(record)
        self.unqualified_bytes += size
        self.unqualified_contexts[context] = {'process':process,'role':frame['role'],
            'parent':frame['parent_id'],'seq':frame['seq'],'terminal':frame['phase']=='terminal'}

    def drain(self):
        if self.collector is None or self.closed:
            return False
        self.collector.barrier()
        return not self.stop_event.is_set()

    def start(self):
        require(self.thread is None and not self.closed)
        self.thread = threading.Thread(target=self._run, name='hub2-alert-observer', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            # Existing directory must be operator-provisioned, never chmod/reap.
            self.directory_fd = private_directory(self.directory)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.listener = sock
            bound = f'/proc/self/fd/{self.directory_fd}/observer.sock'
            sock.bind(bound)  # EADDRINUSE is a refusal, never unlink/retry.
            self.node_fd = os.open('observer.sock', os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                                   dir_fd=self.directory_fd)
            s = os.fstat(self.node_fd)
            require(stat.S_ISSOCK(s.st_mode) and s.st_uid == os.getuid() and s.st_nlink == 1
                and not s.st_mode & 0o7000)
            self.socket_identity = (s.st_dev, s.st_ino)
            self.node_matches()
            # O_PATH pins the filesystem node. Socket FD inode is DIFFERENT;
            # never compare it to this identity or chmod the mutable pathname.
            os.chmod(f'/proc/self/fd/{self.node_fd}', 0o600)
            require(stat.S_IMODE(os.fstat(self.node_fd).st_mode) == 0o600)
            self.node_matches()
            sock.setblocking(False); sock.listen(8)
            self.collector = self.factory(self.job)
            self.collector.start()
            while not self.stop_event.is_set():
                self.collector.wire.receive(0, self.collector.handle)
                ready, _, _ = select.select([sock], [], [], .01)
                if not ready:
                    continue
                connection, _address = sock.accept()
                with connection:
                    end = time.monotonic() + BUDGET
                    connection.setblocking(False)
                    peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    wait(connection, False, end)
                    raw, _anc, flags, _addr = connection.recvmsg(FRAME + 1)
                    require(not flags & socket.MSG_TRUNC)
                    self.requests += 1; require(self.requests <= MAX_REQUESTS)
                    reply = self.request(decode(raw, FRAME), peer)
                    wait(connection, True, end)
                    raw = encode(reply, REPLY)
                    require(connection.send(raw) == len(raw)); left(end)
        except BaseException:
            self.ram.collector_lost(ram.PRODUCERS)
        finally:
            self.closed = True
            self.ram.collector_lost(ram.PRODUCERS)
            if self.collector is not None:
                try:
                    self.collector.close()
                except BaseException:
                    pass
            if self.listener is not None:
                listener, self.listener = self.listener, None
                try:
                    listener.close()
                except BaseException:
                    self.ram.collector_lost(ram.PRODUCERS)
            if self.socket_identity is not None:
                try:
                    self.node_matches()
                    os.unlink('observer.sock', dir_fd=self.directory_fd)
                except BaseException:
                    self.ram.collector_lost(ram.PRODUCERS)
            for name in ('node_fd', 'directory_fd'):
                fd = getattr(self, name)
                if fd is not None:
                    setattr(self, name, None)  # Never retry a possibly consumed FD.
                    try:
                        os.close(fd)
                    except BaseException:
                        self.ram.collector_lost(ram.PRODUCERS)

    def node_matches(self):
        s = os.stat('observer.sock', dir_fd=self.directory_fd, follow_symlinks=False)
        require(stat.S_ISSOCK(s.st_mode) and s.st_uid == os.getuid() and s.st_nlink == 1
            and (s.st_dev, s.st_ino) == self.socket_identity)

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=.2)
        # A stuck C call is not awaited by the host. No replacement owner or
        # same-epoch healthy snapshot is created; requests time out themselves.


def start_optional(spec=None):
    """No environment/config lookup. Owner supplies optional reviewed spec.

    Integration wires an explicit Bridge constructor argument. Disabled is the
    default; CLI/config enrollment is a separate unimplemented activation seam.
    """
    if spec is None or sys.platform != 'linux':
        return None
    try:
        require(type(spec) is dict and set(spec) == {'directory','source_pins'})
        require(set(spec['source_pins']) == set(ram.ROLES))
        pins = {role: ram.SourcePin(**value).checked() for role, value in spec['source_pins'].items()}
        broker = Broker(spec['directory'], pins)
        broker.start()
        return broker
    except Exception:
        return None


def configured(value):
    """Strict optional config only; no filesystem/Unix access or config writes."""
    try:
        require(type(value) is dict and set(value) == {'v','enabled','directory','source_pins'})
        require(type(value['v']) is int and value['v'] == 1 and value['enabled'] is True)
        directory = value['directory']
        require(type(directory) is str and directory.startswith('/') and '\x00' not in directory
            and '..' not in Path(directory).parts and len(os.fsencode(directory)) <= 80)
        require(type(value['source_pins']) is dict and set(value['source_pins']) == set(ram.ROLES))
        pins = {}
        for role, fields in value['source_pins'].items():
            require(type(fields) is dict and set(fields) == {'dev','ino','sha256'})
            ram.SourcePin(**fields).checked()
            pins[role] = dict(fields)
        return {'directory':directory,'source_pins':pins}
    except Exception:
        return None


def stop_optional(broker):
    if broker is not None:
        try:
            broker.close()
        except Exception:
            pass
