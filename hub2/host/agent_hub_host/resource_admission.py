"""Private same-journal physical-memory admission; no native or network calls."""
import ctypes
import hashlib
import re
import json
import math
import os
import stat
import time
from pathlib import Path

MIB = 1024 ** 2
FLOOR_BYTES = 10 * 1024 ** 3
ALLOWANCE_MB = 1500
RAMP_SECONDS = 180
MAX_AGE = 1.0
ACTIVE = {'launching', 'running', 'prompt_pending', 'aborting', 'deadline_stopping'}
RELEASED = {'exited', 'stopped_before_dispatch', 'launch_failed_unconfirmed',
            'launch_failed_reported', 'expired_unconfirmed'}


class Refused(RuntimeError):
    def __init__(self, reason):
        super().__init__('native_dispatch_not_started')
        self.reason = reason


def clock():
    value = time.monotonic()
    if not math.isfinite(value) or value < 0:
        raise Refused('host_memory_admission_stale')
    return value


def boot():
    if os.name != 'posix':
        return None  # No Windows per-worker residency claim.
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        import uuid
        return str(uuid.UUID(value)) if len(value) == 36 else None
    except (OSError, ValueError):
        return None


def caps(memory, pids):
    if type(memory) is not int or not 0 < memory <= ALLOWANCE_MB or type(pids) is not int or not 0 < pids <= 256:
        raise Refused('host_worker_caps_unverified')


def linux_available(raw):
    values = {}
    for line in raw.splitlines():
        fields = line.split()
        if fields and fields[0] in ('MemAvailable:', 'MemTotal:'):
            if len(fields) != 3 or fields[2] != 'kB' or not fields[1].isdigit() or fields[0] in values:
                raise Refused('host_memory_sample_unavailable')
            values[fields[0]] = int(fields[1]) * 1024
    available, total = values.get('MemAvailable:'), values.get('MemTotal:')
    if type(available) is not int or type(total) is not int or not 0 <= available <= total or not 0 < total < 2**64:
        raise Refused('host_memory_sample_unavailable')
    return available


class MemoryStatus(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32)] + [
        (key, ctypes.c_uint64) for key in ('total_phys', 'avail_phys', 'total_page', 'avail_page',
                                         'total_virtual', 'avail_virtual', 'avail_extended')]


def windows_available(kernel=None):
    kernel = kernel if kernel is not None else ctypes.WinDLL('kernel32', use_last_error=True)
    fn = kernel.GlobalMemoryStatusEx
    fn.argtypes, fn.restype = [ctypes.POINTER(MemoryStatus)], ctypes.c_int
    sample = MemoryStatus()
    sample.length = ctypes.sizeof(sample)
    if not fn(ctypes.byref(sample)) or sample.length != 64 or not 0 <= sample.avail_phys <= sample.total_phys or not sample.total_phys:
        raise Refused('host_memory_sample_unavailable')
    return sample.avail_phys


def available_bytes():
    try:
        if os.name == 'nt':
            return windows_available()
        with open('/proc/meminfo', encoding='ascii') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise Refused('host_memory_sample_unavailable')
        return linux_available(raw)
    except (OSError, ValueError, AttributeError, UnicodeError):
        raise Refused('host_memory_sample_unavailable') from None


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def starting(row):
    return encode({'version': 1, 'tuple': [row['operation_id'], row['session_id'], row['epoch']],
                   'allowance_mb': ALLOWANCE_MB, 'released': False, 'boot': None,
                   'dispatch_mono': None, 'identity': None, 'incarnation': None})


def metadata(row):
    try:
        raw = row['admission_json']
        if not isinstance(raw, str) or len(raw) > 16384:
            raise ValueError()
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        value = json.loads(raw, object_pairs_hook=unique)
        if (type(value['version']) is not int or value['version'] != 1 or value['tuple'] != [row['operation_id'], row['session_id'], row['epoch']]
                or type(value['allowance_mb']) is not int or value['allowance_mb'] != ALLOWANCE_MB
                or type(value['released']) is not bool):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, IndexError):
        raise Refused('host_memory_reservation_unknown') from None


def release(db, row, reason):
    """Called only INSIDE an existing evidence-qualified lifecycle transaction."""
    value = json.loads(starting(row))
    value.update(released=True, release_reason=reason)
    db.execute('UPDATE launches SET admission_json=? WHERE operation_id=? AND session_id=? AND epoch=?',
               (encode(value), row['operation_id'], row['session_id'], row['epoch']))


def _persisted_payload(raw):
    if not isinstance(raw, str) or len(raw) > 65536:
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        return value if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        return None


def _absence(payload, row, provenance, now_ms):
    evidence = payload.get('evidence')
    if not isinstance(evidence, dict):
        return False
    started, observed = evidence.get('started_at_ms'), evidence.get('observed_at_ms')
    return (evidence.get('source') == 'aplexer_all_registry_census'
            and evidence.get('complete') is True and type(evidence.get('match_count')) is int
            and evidence['match_count'] == 0 and type(evidence.get('row_count')) is int
            and 0 <= evidence['row_count'] <= 5000
            and isinstance(evidence.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', evidence['sha256']) is not None
            and evidence.get('workspace') == row['workspace'] and evidence.get('tag') == row['tag']
            and evidence.get('dispatch_provenance') == provenance
            and type(started) is int and type(observed) is int
            and 0 < started <= observed <= now_ms + 5000 and observed - started <= 6000)


def recover_release(db, row):
    """Accounting only: exact trusted journal producers, never new observations.

    Outbox provenance is local journal authority, not a signature or proof against
    a malicious same-user database writer. Delivery/age/state alone is ignored.
    Missing/deleted receipts leave UNKNOWN. No receipt is sent/retried/modified.
    """
    if (row['admission_json'] is not None or row['state'] not in RELEASED
            or row['tag'] != 'task-hub2-' + row['session_id'] or not Path(row['workspace']).is_absolute()):
        return False
    operation, epoch = row['operation_id'], row['epoch']
    keys = [('no-dispatch:' + operation + ':' + str(epoch), 'no_dispatch'),
            ('finish:' + operation, 'qualified_exit'),
            ('launch-expired:' + operation + ':' + str(epoch), 'qualified_absence'),
            ('legacy-unobserved:' + operation + ':' + str(epoch), 'qualified_legacy_absence')]
    receipts = []
    for key, reason in keys:
        saved = db.execute('SELECT path,substr(payload,1,65537) AS payload FROM outbox WHERE key=?', (key,)).fetchone()
        if saved and saved['path'] == '/api/sessions/' + row['session_id'] + '/finish':
            receipts.append((reason, saved['payload']))
    if row['state'] in {'launch_failed_unconfirmed', 'launch_failed_reported'} and row['legacy_failure_json']:
        receipts.append(('qualified_legacy_absence', row['legacy_failure_json']))
    for reason, raw in receipts:
        payload = _persisted_payload(raw)
        if (not payload or payload.get('operation_id') != operation or type(payload.get('epoch')) is not int
                or payload['epoch'] != epoch or payload.get('native_id') != row['native_id']):
            continue
        qualified = False
        if reason == 'no_dispatch':
            qualified = (row['state'] == 'stopped_before_dispatch' and row['native_id'] is None
                and (row['native_dispatch_started'] == 0 or row['native_dispatch_started'] is None
                     and payload.get('admission_expired') is True)
                and payload.get('launch_failed') is True and payload.get('no_dispatch') is True)
        elif reason == 'qualified_exit':
            from .native_launch import native_returncode, UUID_RE
            evidence = payload.get('evidence')
            qualified = (row['state'] == 'exited' and isinstance(row['native_id'], str) and UUID_RE.fullmatch(row['native_id']) is not None
                and payload.get('native_exit_confirmed') is True and type(payload.get('exit_code')) is int
                and isinstance(evidence, dict) and evidence.get('source') in ('aplexer_status', 'aplexer_retirement')
                and evidence.get('containment_empty') is True
                and evidence.get('native_identity') == {'id': row['native_id'], 'tag': row['tag'], 'workspace': row['workspace']}
                and native_returncode(evidence.get('native_exit')) is not None
                and native_returncode(evidence['native_exit']) == payload['exit_code'])
        elif reason == 'qualified_absence':
            deadline = row['runtime_deadline']
            qualified = (row['state'] == 'expired_unconfirmed' and payload.get('launch_expired') is True
                and payload.get('native_outcome') == 'unconfirmed' and type(deadline) is int
                and type(payload.get('runtime_deadline')) is int and payload['runtime_deadline'] == deadline
                and _absence(payload, row, 'host_journal', int(time.time()*1000))
                and payload['evidence']['started_at_ms'] >= deadline)
        elif reason == 'qualified_legacy_absence':
            qualified = (row['state'] in {'launch_failed_unconfirmed', 'launch_failed_reported'}
                and row['native_id'] is None and row['native_dispatch_started'] is None
                and payload.get('launch_failed') is True and payload.get('failure_kind') == 'legacy_census_absent'
                and payload.get('native_outcome') == 'unconfirmed'
                and _absence(payload, row, 'legacy_unknown', int(time.time()*1000)))
        if qualified:
            # The enclosing caller holds BEGIN IMMEDIATE/the existing reaper
            # transition transaction. Only this private column is updated.
            value = json.loads(starting(row))
            value.update(released=True, release_reason=reason,
                         persisted_evidence_sha256=hashlib.sha256(raw.encode('utf8')).hexdigest())
            changed = db.execute('UPDATE launches SET admission_json=? WHERE operation_id=? AND session_id=? AND epoch=? '
                'AND digest=? AND workspace=? AND tag=? AND state=? AND native_id IS ? '
                'AND native_dispatch_started IS ? AND admission_json IS NULL',
                (encode(value), operation, row['session_id'], epoch, row['digest'], row['workspace'], row['tag'],
                 row['state'], row['native_id'], row['native_dispatch_started']))
            return changed.rowcount == 1
    return False


def bind(db, row, native):
    """A later qualified binding starts, never backdates, a missing ramp anchor."""
    if (row['state'] != 'running' or not native or native.get('id') != row['native_id'] or native.get('tag') != row['tag']
            or native.get('workspace') != row['workspace'] or native.get('phase') not in ('running', 'working')
            or type(native.get('created_at_ms')) is not int or native['created_at_ms'] <= 0):
        return
    try:
        value = metadata(row)
    except Refused:
        value = json.loads(starting(row))
    if value['released']:
        return  # A late observation cannot turn qualified release into a hold.
    # A native incarnation change is not an opportunity to reuse an old age.
    previous = value.get('identity')
    identity = {key: native.get(key) for key in ('id', 'tag', 'workspace', 'engine', 'created_at_ms',
        'workload_pid', 'workload_cgroup', 'containment_cgroup', 'containment_cgroup_identity', 'containment_empty')}
    # A repeated UUID with a different birth must not adopt another incarnation.
    if value.get('incarnation') not in (None, identity['created_at_ms']):
        value['identity'] = None
        db.execute('UPDATE launches SET admission_json=? WHERE operation_id=? AND session_id=? AND epoch=? '
                   "AND state='running' AND native_id IS ? AND native_dispatch_started IS ? AND admission_json IS ?",
                   (encode(value), row['operation_id'], row['session_id'], row['epoch'], row['native_id'],
                    row['native_dispatch_started'], row['admission_json']))
        return
    value['incarnation'] = identity['created_at_ms']
    current_boot = boot()
    if (value.get('boot') != current_boot or value.get('dispatch_mono') is None
            or previous and previous.get('created_at_ms') != identity['created_at_ms']):
        value['boot'], value['dispatch_mono'] = current_boot, clock()
    value.update(identity=identity, released=False)
    db.execute('UPDATE launches SET admission_json=? WHERE operation_id=? AND session_id=? AND epoch=? '
               "AND state='running' AND native_id IS ? AND native_dispatch_started IS ? AND admission_json IS ?",
               (encode(value), row['operation_id'], row['session_id'], row['epoch'], row['native_id'],
                row['native_dispatch_started'], row['admission_json']))


def _read_at(fd, name):
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    with os.fdopen(child) as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError()
    return raw


def _kernel_identity(root, proc):
    st = root.stat()
    result = {'boot_id': (proc / 'sys/kernel/random/boot_id').read_text().strip(),
              'cgroup_root_device': st.st_dev, 'cgroup_root_inode': st.st_ino}
    for name, label in (('cgroup', 'cgroup'), ('mnt', 'mount')):
        ns = (proc / 'self/ns' / name).stat()
        result[label + '_namespace_device'], result[label + '_namespace_inode'] = ns.st_dev, ns.st_ino
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fields = (proc / 'self/fdinfo' / str(fd)).read_text().splitlines()
        result['cgroup_mount_id'] = int(next(x.split()[1] for x in fields if x.startswith('mnt_id:')))
    finally:
        os.close(fd)
    return result


def anonymous_mb(identity, root=Path('/sys/fs/cgroup'), proc=Path('/proc')):
    """Credit only held, exact live workload anonymous pages, never cache."""
    fd = None
    try:
        relative, pid = identity['workload_cgroup'], identity['workload_pid']
        if type(pid) is not int or pid <= 0 or not isinstance(relative, str) or not relative.startswith('/'):
            return 0
        path = root / relative.lstrip('/')
        if (path.name != 'aplexer-workload-' + identity['id'] + '.scope' or path != path.resolve()
                or not path.is_relative_to(root) or identity['containment_cgroup'] != str(path)
                or identity['containment_cgroup_identity'] != _kernel_identity(root, proc)):
            return 0
        def process():
            base = proc / str(pid)
            if (base / 'cgroup').read_text().strip() != '0::' + relative:
                raise ValueError()
            fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] in ('Z', 'X', 'x'):
                raise ValueError()
            return int(fields[19])
        born = process()
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = os.fstat(fd)
        def sample():
            limit = int(_read_at(fd, 'memory.max').strip())
            current = int(_read_at(fd, 'memory.current').strip())
            anon = int(dict(x.split() for x in _read_at(fd, 'memory.stat').splitlines())['anon'])
            populated = dict(x.split() for x in _read_at(fd, 'cgroup.events').splitlines())['populated']
            members = [int(x) for x in _read_at(fd, 'cgroup.procs').split()]
            if not 0 < limit <= ALLOWANCE_MB * MIB or not 0 <= anon <= current <= limit or populated != '1' or pid not in members:
                raise ValueError()
            return anon
        credit = min(sample(), sample()) // MIB
        after = path.stat()
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or process() != born or identity['containment_cgroup_identity'] != _kernel_identity(root, proc)):
            return 0
        return min(ALLOWANCE_MB, credit)
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration):
        return 0
    finally:
        if fd is not None:
            os.close(fd)


def reserve(row, now, current_boot):
    if row['state'] == 'pending_admission' and row['native_id'] is None and row['native_dispatch_started'] != 1:
        return 0
    value = metadata(row)
    if value['released']:
        if row['state'] not in RELEASED:
            raise Refused('host_memory_reservation_unknown')
        return 0
    if row['state'] == 'running' and row['native_dispatch_started'] == 0:
        raise Refused('host_memory_reservation_unknown')
    if row['state'] not in ACTIVE:
        # Terminal spellings without an explicit qualified release are not proof.
        raise Refused('host_memory_reservation_unknown')
    identity, started = value.get('identity'), value.get('dispatch_mono')
    if (row['state'] != 'running' or not row['native_id'] or not identity
            or identity.get('id') != row['native_id'] or identity.get('tag') != row['tag']
            or identity.get('workspace') != row['workspace'] or identity.get('containment_empty') is not False
            or identity.get('created_at_ms') != value.get('incarnation') or not current_boot
            or value.get('boot') != current_boot or type(started) not in (float, int)
            or not math.isfinite(started) or not 0 <= started <= now):
        return ALLOWANCE_MB
    credit = anonymous_mb(identity) if os.name == 'posix' else 0
    return (0 if now - started >= RAMP_SECONDS else max(0, ALLOWANCE_MB - credit)) if credit > 0 else ALLOWANCE_MB


class Permit:
    def __init__(self, identity, sampled):
        self.identity, self.sampled = identity, sampled

    def check(self):
        elapsed = clock() - self.sampled
        if not 0 <= elapsed <= MAX_AGE:
            raise Refused('host_memory_admission_stale')


def admit(db, operation, session, epoch):
    """The single short transaction owns sums, sample and original marker CAS."""
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM launches WHERE operation_id=?', (operation,)).fetchone()
        if (not row or row['session_id'] != session or row['epoch'] != epoch or row['state'] != 'launching'
                or row['native_dispatch_started'] != 0 or row['native_id'] is not None
                or db.execute('SELECT 1 FROM stop_fences WHERE session_id=? AND epoch>=?', (session, epoch)).fetchone()):
            raise Refused('host_memory_reservation_unknown')
        value = metadata(row)
        if value['released']:
            raise Refused('host_memory_reservation_unknown')
        began, current_boot = clock(), boot()
        reserved, candidates = 0, 0
        # Settled historical rows do not consume the active-custody bound.
        # Validate their private release record, never just their state name.
        for other in db.execute('SELECT * FROM launches WHERE operation_id<>?', (operation,)):
            if recover_release(db, other):
                other = db.execute('SELECT * FROM launches WHERE operation_id=?', (other['operation_id'],)).fetchone()
            amount = reserve(other, began, current_boot)
            reserved += amount
            if other['state'] in ACTIVE:
                candidates += 1
            if candidates > 1000:
                raise Refused('host_memory_reservation_unknown')
            if not 0 <= clock() - began <= MAX_AGE:
                raise Refused('host_memory_admission_stale')
        available = available_bytes()
        if not 0 <= clock() - began <= MAX_AGE:
            raise Refused('host_memory_admission_stale')
        if available - (reserved + ALLOWANCE_MB) * MIB < FLOOR_BYTES:
            raise Refused('host_memory_headroom_insufficient')
        value.update(boot=current_boot, dispatch_mono=began, identity=None)
        db.execute('UPDATE launches SET native_dispatch_started=1,admission_json=? WHERE operation_id=?',
                   (encode(value), operation))
        db.commit()
        permit = Permit((operation, session, epoch), began)
        permit.check()  # Commit/fsync may consume the sample age; marker stays1.
        return permit
    except BaseException:
        db.rollback()
        raise
