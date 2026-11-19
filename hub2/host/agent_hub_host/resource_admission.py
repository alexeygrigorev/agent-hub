"""Same-journal pre-dispatch physical RAM admission for native launches.

Immediately before a native dispatch, one BEGIN IMMEDIATE transaction on the
Host journal re-reads the exact launch/epoch/state/marker row and its STOP
fence, sums the conservative uncovered inflight reservations, samples physical
available RAM last, then commits the attempt's reservation together with the
original native_dispatch_started 0->1 CAS. Concurrent Bridge instances on the
same journal therefore cannot spend the same headroom, and the sample never
renews its clock: the returned dispatch permit carries a fixed original
monotonic deadline that the launcher re-checks immediately before the runner.

Only positively qualified evidence releases a reservation (evidence-qualified
no-dispatch settlement or a whole-attempt containment-empty terminal commit);
uncertain custody, crash rows and missing OS/inflight/identity proof are
charged conservatively or refuse truthfully, never fabricated to zero.
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .native_inventory import MAX_CENSUS_AGE_SECONDS

# Fixed user policy floor for native dispatch, deliberately not the weaker
# configurable resource-management default and not operator-tunable here.
RESERVED_FLOOR_BYTES = 10 * 1024**3
DEFAULT_ALLOWANCE_BYTES = 1500 * 1024 * 1024
DEFAULT_MEMORY_MB = 1500
MAX_MEMORY_MB = 1500
DEFAULT_PIDS = 256
MAX_PIDS = 256
# ONE original monotonic deadline, established before any evidence
# collection or lock wait, clips the whole admission: the private census,
# the BEGIN IMMEDIATE wait, the row/fence reads, the charges, the physical
# sample and the commit. The dispatch permit carries that same original
# deadline and the launcher re-checks it immediately before the runner; the
# budget is never renewed and no fresh window starts after the census.
ADMISSION_WINDOW_SECONDS = 30
# A resident worker is credited as mature only with positive same-incarnation
# OS evidence: exact bound native UUID from a fresh private raw census, live
# worker, running phase/state, a workload pid whose kernel birth (Linux
# /proc starttime versus btime; Windows GetProcessTimes creation time) matches
# the observed birth within tolerance, and a cwd bound to the exact workspace.
# Every step revalidates at the final transaction boundary right before the
# physical sample; any absent/malformed/mismatched proof keeps the full
# conservative allowance. Historical rows and release timestamps never credit.
MATURE_RESIDENT_AGE_MS = 300_000
MATURE_BIRTH_TOLERANCE_MS = 5_000
MAX_INFLIGHT_ROWS = 1000
MAX_CENSUS_ROWS = 5000
_CENSUS_MAX_BYTES = 16 * 1024 * 1024
PROC_ENTRY_MAX_BYTES = 16384
_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_EPOCH_FILETIME_MS = 11644473600000
_MEMINFO_MAX_BYTES = 65536
_MEMINFO_NUMBER = re.compile(r'[0-9]+')
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_CENSUS_ERRORS = (RuntimeError, OSError, AttributeError, subprocess.SubprocessError)


class MemoryStatusEx(ctypes.Structure):
    """Exact 64-byte MEMORYSTATUSEX layout with fixed-size fields.

    Declared with raw ctypes types so the structure itself stays usable on
    every platform; the win32 call path is the only consumer of kernel32."""
    _fields_ = [('dwLength', ctypes.c_uint32),
                ('dwMemoryLoad', ctypes.c_uint32),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


assert ctypes.sizeof(MemoryStatusEx) == 64


def _unqualified():
    return RuntimeError('host_operation_failed')


def _validated_cap(raw, default, limit):
    # Strict positive ints only: bool/string/float/oversized values refuse the
    # launch instead of being clamped to the cap.
    if raw is None:
        return default
    if type(raw) is not int or not 0 < raw <= limit:
        raise ValueError('launcher_capability_unavailable')
    return raw


def validate_worker_caps(memory_mb, pids):
    """Validate the exact caps the argv will emit; defaults only when absent."""
    return {'memory_mb': _validated_cap(memory_mb, DEFAULT_MEMORY_MB, MAX_MEMORY_MB),
            'pids': _validated_cap(pids, DEFAULT_PIDS, MAX_PIDS)}


def parse_linux_meminfo(raw):
    """Strict bounded /proc/meminfo parse: MemAvailable/MemTotal integer kB.

    Any second occurrence of a known field refuses, whatever its shape, and a
    known field with wrong units or token counts refuses; absence, malformed
    or negative values, available>total and oversized responses refuse too.
    Nothing is defaulted or extrapolated."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > _MEMINFO_MAX_BYTES:
        raise _unqualified()
    try:
        text = bytes(raw).decode('ascii')
    except UnicodeDecodeError:
        raise _unqualified() from None
    fields = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ('MemAvailable:', 'MemTotal:'):
            continue
        if parts[0] in fields:
            raise _unqualified()
        if len(parts) != 3 or parts[2] != 'kB' or not _MEMINFO_NUMBER.fullmatch(parts[1]):
            raise _unqualified()
        fields[parts[0]] = int(parts[1]) * 1024
    if 'MemAvailable:' not in fields or 'MemTotal:' not in fields:
        raise _unqualified()
    available, total = fields['MemAvailable:'], fields['MemTotal:']
    if total <= 0 or not 0 <= available <= total:
        raise _unqualified()
    return available, total


def sample_linux(read=None):
    """Direct strict MemAvailable read from /proc/meminfo on Linux."""
    if read is None:
        if sys.platform != 'linux':
            raise _unqualified()
        try:
            with open('/proc/meminfo', 'rb') as stream:
                raw = stream.read(_MEMINFO_MAX_BYTES + 1)
        except OSError:
            raise _unqualified() from None
    else:
        raw = read()
    return parse_linux_meminfo(raw)[0]


def sample_windows(status_call=None):
    """GlobalMemoryStatusEx via ctypes with explicit signature and ranges.

    No PowerShell, CIM or provider process; a failed call, wrong struct
    length, out-of-range load or impossible physical ranges refuse."""
    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if status_call is None:
        if sys.platform != 'win32':
            raise _unqualified()
        status_call = ctypes.windll.kernel32.GlobalMemoryStatusEx
        status_call.argtypes = [ctypes.POINTER(MemoryStatusEx)]
        status_call.restype = ctypes.c_int
    if not status_call(ctypes.byref(status)):
        raise _unqualified()
    if (status.dwLength != ctypes.sizeof(MemoryStatusEx)
            or not 0 <= status.dwMemoryLoad <= 100):
        raise _unqualified()
    if status.ullTotalPhys <= 0 or not 0 <= status.ullAvailPhys <= status.ullTotalPhys:
        raise _unqualified()
    return status.ullAvailPhys


def sample_available_bytes(deadline=None, platform=None):
    """One strict physical available-RAM sample inside the fixed window.

    The sample is taken only within the caller's original monotonic window;
    overshoot, unknown platforms and read failures refuse fail-closed."""
    if deadline is not None and time.monotonic() > deadline:
        raise _unqualified()
    platform = sys.platform if platform is None else platform
    if platform == 'linux':
        available = sample_linux()
    elif platform == 'win32':
        available = sample_windows()
    else:
        raise _unqualified()
    if deadline is not None and time.monotonic() > deadline:
        raise _unqualified()
    return available


class DispatchPermit:
    """Permission to spawn the admitted dispatch, clipped by the original budget.

    The deadline is the admission's one original monotonic deadline
    established before any evidence collection or lock wait; it is never
    renewed and no fresh window starts after the census or the sample.
    Verification stays read-only: it never resets the dispatch marker or
    refunds the reservation, so a consumed permit leaves launch custody
    uncertain, never re-dispatched."""
    __slots__ = ('operation_id', 'session_id', 'epoch', 'permit_deadline')

    def __init__(self, operation_id, session_id, epoch, permit_deadline):
        self.operation_id = operation_id
        self.session_id = session_id
        self.epoch = epoch
        self.permit_deadline = permit_deadline

    def expired(self, now=None):
        return (time.monotonic() if now is None else now) > self.permit_deadline


def initialize(journal):
    """Create the private same-journal reservation table.

    Historical launches rows are never migrated, seeded or annotated: only
    attempts admitted by this module own reservation rows."""
    with journal.db as db:
        db.execute('CREATE TABLE IF NOT EXISTS resource_reservations('
                   'operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, epoch INTEGER NOT NULL, '
                   'digest TEXT NOT NULL, attempt TEXT NOT NULL, native_id TEXT, '
                   'allowance_bytes INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)')


def release_reservation(db, operation, session, epoch):
    """Release exactly this attempt's reservation inside the caller's terminal
    transaction (evidence-qualified no-dispatch or containment-empty exit)."""
    db.execute('DELETE FROM resource_reservations WHERE operation_id=? AND session_id=? AND epoch=?',
               (operation, session, epoch))


def verify_permit(permit, journal=None):
    """Read-only freshness/fence re-check immediately before the runner.

    Age is measured against the original sample clock; a STOP fence or a
    changed launch row refuses the spawn. The durable marker is never
    re-checked towards 0 and the reservation is never refunded here: refusal
    after commit keeps launch custody uncertain."""
    if permit is None:
        return
    if permit.expired():
        raise RuntimeError('native_start_uncertain')
    if journal is None:
        return
    db = journal.db
    row = db.execute('SELECT state FROM launches WHERE operation_id=? AND session_id=? AND epoch=?',
                     (permit.operation_id, permit.session_id, permit.epoch)).fetchone()
    if row is None or row['state'] != 'launching':
        raise RuntimeError('native_start_uncertain')
    if db.execute('SELECT 1 FROM stop_fences WHERE session_id=? AND epoch>=?',
                  (permit.session_id, permit.epoch)).fetchone():
        raise RuntimeError('native_start_uncertain')


def _unique_object(pairs):
    """Duplicate keys in a raw observation row refuse; nothing is last-wins."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise _unqualified()
        result[key] = value
    return result


def _bounded_read(path):
    """One bounded byte read of a kernel/proc file; failures refuse."""
    with open(path, 'rb') as stream:
        raw = stream.read(PROC_ENTRY_MAX_BYTES + 1)
    if len(raw) > PROC_ENTRY_MAX_BYTES:
        raise _unqualified()
    return raw


def _linux_birth_ms(pid, read=None):
    """Kernel birth time of one pid in wall-clock ms, or None.

    Derived from /proc/<pid>/stat field 22 (starttime, clock ticks since boot)
    against /proc/stat btime; both kernel-sourced, so the derivation carries
    only the btime second-granularity, bounded by the fixed tolerance. Absent,
    malformed or unbounded evidence refuses (None), never guesses."""
    if read is None:
        if sys.platform != 'linux':
            return None
        read = _bounded_read
    try:
        stat_raw = read(f'/proc/{pid}/stat')
        boot_raw = read('/proc/stat')
        clock_ticks = os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(clock_ticks, int) or clock_ticks <= 0:
        return None
    try:
        text = stat_raw.decode('ascii', errors='strict')
        tail = text[text.rindex(')') + 2:].split()
        start_ticks = int(tail[22 - 3])  # field 22, with fields 3.. after the comm
        boot = None
        for line in boot_raw.decode('ascii', errors='strict').splitlines():
            parts = line.split()
            if parts[:1] == ['btime']:
                if len(parts) != 2 or not _MEMINFO_NUMBER.fullmatch(parts[1]):
                    return None
                boot = int(parts[1])
        if boot is None:
            return None
    except (ValueError, IndexError):
        return None
    return boot * 1000 + (start_ticks * 1000) // clock_ticks


def _windows_creation_ms(open_process, get_times, close_handle, pid):
    """Kernel creation time from one OpenProcess/GetProcessTimes exchange.

    Synthetic identity controls exercise this exact protocol cross-platform;
    a failed open, failed times read or missing handle holds the credit
    (None) instead of pretending."""
    handle = open_process(_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exit_time, kernel_time, user_time = (ctypes.c_ulonglong(),) * 4
        if not get_times(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                         ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return creation.value // 10000 - _WINDOWS_EPOCH_FILETIME_MS
    finally:
        close_handle(handle)


def _windows_birth_ms(pid):
    """Kernel creation time of one pid via OpenProcess/GetProcessTimes, or None.

    Same-user pids only; an unavailable process handle or times structure
    conservatively holds the credit (None) instead of pretending."""
    if sys.platform != 'win32':
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return _windows_creation_ms(kernel32.OpenProcess, kernel32.GetProcessTimes,
                                kernel32.CloseHandle, pid)


def _process_birth_ms(pid, read=None):
    """Positive OS birth evidence in wall-clock ms; None when unprovable."""
    if sys.platform == 'win32':
        return _windows_birth_ms(pid)
    return _linux_birth_ms(pid, read=read)


def _process_cwd(pid):
    """Exact bound workspace of one pid's kernel cwd, or None."""
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except (OSError, ValueError):
        return None


_SESSION_ENV_ENTRY = b'APLEXER_SESSION_ID='
_ENVIRON_MAX_BYTES = 65536


def _process_session_binding(pid, native_id, read=None):
    """Exact bound incarnation anchor: the pid carries THIS native session id.

    The workload pid's kernel environment must contain the exact
    APLEXER_SESSION_ID entry equal to the bound native UUID, so a recycled
    pid can never inherit another attempt's credit. Oversized, unreadable or
    missing evidence refuses the binding (no tolerance, no inference); the
    Linux-only anchor conservatively holds on platforms without it."""
    if sys.platform == 'win32':
        return False  # no exact pid-environment anchor: conservative hold
    if read is None:
        read = _bounded_read
    try:
        raw = read(f'/proc/{pid}/environ')
    except (OSError, ValueError):
        return False
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > _ENVIRON_MAX_BYTES:
        return False
    expected = _SESSION_ENV_ENTRY + native_id.strip().lower().encode('ascii')
    for entry in bytes(raw).split(b'\0'):
        if entry.startswith(_SESSION_ENV_ENTRY):
            return entry == expected
    return False


def _process_alive(pid):
    """Liveness revalidation at the final boundary; failures refuse."""
    if sys.platform == 'win32':
        return _windows_birth_ms(pid) is not None
    try:
        return os.kill(pid, 0) is None
    except ProcessLookupError:
        return False
    except OSError:
        return None  # Permission ambiguity is not liveness proof.



def parse_raw_census(raw):
    """Strict private parse of one bounded raw `list --all` response.

    Mirrors the public parser's completeness strictness (array bounds, exact
    UUID identities, duplicates refuse, phase/state vocabularies, timestamp
    ranges) but additionally retains the workload pid coordinate the public
    projection deliberately strips. Any malformed row refuses the whole
    observation; the caller then charges every attempt in full."""
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > _CENSUS_MAX_BYTES:
        raise _unqualified()
    try:
        rows = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        raise _unqualified() from None
    if not isinstance(rows, list) or len(rows) > MAX_CENSUS_ROWS:
        raise _unqualified()
    parsed, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise _unqualified()
        native_id, tag, workspace = row.get('id'), row.get('tag'), row.get('workspace')
        if (not isinstance(native_id, str) or not _UUID_RE.fullmatch(native_id)
                or native_id.lower() in seen
                or not isinstance(tag, str) or not tag or '\0' in tag or len(tag.encode('utf-8')) > 512
                or not isinstance(workspace, str) or not Path(workspace).is_absolute()
                or '\0' in workspace or len(workspace.encode('utf-8')) > 8192
                or type(row.get('schema_version')) is not int or row['schema_version'] < 1):
            raise _unqualified()
        seen.add(native_id.lower())
        phase = row.get('phase')
        if phase is not None and phase not in ('starting', 'running', 'exiting', 'stopping', 'exited', 'failed', 'unknown'):
            raise _unqualified()
        state = row.get('state')
        if state is not None and state not in ('starting', 'running', 'exiting', 'exited', 'failed', 'broken', 'unknown'):
            raise _unqualified()
        alive = row.get('worker_alive')
        if alive is not None and type(alive) is not bool:
            raise _unqualified()
        parsed_row = {'id': native_id.lower(), 'tag': tag, 'workspace': workspace,
                      'phase': phase or 'unknown', 'state': state or 'unknown',
                      'worker_alive': alive}
        for field in ('created_at_ms', 'workload_pid'):
            value = row.get(field)
            if value is not None and (type(value) is not int or not 0 < value <= 9007199254740991):
                raise _unqualified()
            parsed_row[field] = value
        parsed.append(parsed_row)
    return tuple(parsed)


def _raw_census_rows(bridge):
    """One bounded private raw registry observation with its collection clock.

    Returns (rows, collected_monotonic), or (( ), None) when the collection is
    unavailable. The collection stays admission-private: it never reads,
    warms or publishes the launch reaper's shared census cache, because a
    census taken before this attempt spawned must never be applied later as
    expiry evidence for that same attempt. Unavailable observations stay
    conservative: every current attempt then keeps its full allowance."""
    query = getattr(getattr(bridge, 'launcher', None), 'raw_census_response', None)
    if not callable(query):
        return (), None
    try:
        return parse_raw_census(query()), time.monotonic()
    except (RuntimeError, OSError, ValueError, TypeError, subprocess.SubprocessError):
        return (), None


def _same_workspace(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except (OSError, RuntimeError, ValueError):
        return False


def _resident_rows(observation, now_ms):
    """Mature candidates from the fresh private raw observation.

    Freshness is enforced here, at the final transaction boundary, against
    the observation's own collection clock: an observation collected after
    the transaction began never renews the original budget, and one that went
    stale before this boundary credits nothing. Each exact UUID appears once."""
    if not observation:
        return {}
    raw_rows, collected = observation
    if not raw_rows or collected is None or time.monotonic() - collected > MAX_CENSUS_AGE_SECONDS:
        return {}
    residents = {}
    for row in raw_rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            continue
        birth = row.get('created_at_ms')
        if (row.get('worker_alive') is not True or row.get('phase') != 'running'
                or row.get('state') != 'running'
                or type(birth) is not int or birth <= 0 or now_ms - birth < MATURE_RESIDENT_AGE_MS
                or type(row.get('workload_pid')) is not int):
            continue
        residents.setdefault(row['id'].lower(), row)
    return residents


def _mature_resident(row, residents, credited, now_ms, read=None):
    """Positive same-incarnation OS proof for one bound attempt.

    Requires the exact bound native UUID in the fresh private observation,
    matching tag/workspace, and a workload pid whose kernel birth matches the
    observed birth within the fixed tolerance and whose kernel cwd resolves
    to the exact bound workspace (Linux; Windows proves kernel birth and
    conservatively holds when cwd/process proof is unavailable). Liveness is
    revalidated here at the final boundary, immediately before the physical
    sample. Each census row credits at most one reservation. Any absent,
    malformed or mismatched step keeps the full conservative allowance;
    missing proof never becomes zero."""
    native_id = row['native_id']
    if not isinstance(native_id, str) or not _UUID_RE.fullmatch(native_id):
        return False
    key = native_id.lower()
    if key in credited:
        return False
    resident = residents.get(key)
    if resident is None:
        return False
    if resident.get('tag') != row['tag'] or not _same_workspace(resident.get('workspace'), row['workspace']):
        return False
    pid = resident.get('workload_pid')
    birth = resident.get('created_at_ms')
    kernel_birth = _process_birth_ms(pid, read=read)
    if (not isinstance(kernel_birth, int)
            or abs(kernel_birth - birth) > MATURE_BIRTH_TOLERANCE_MS
            or not _process_cwd_is_bound(pid, row['workspace'])
            or not _process_session_binding(pid, native_id)
            or _process_alive(pid) is not True):
        return False
    credited.add(key)
    return True


def _process_cwd_is_bound(pid, workspace):
    if sys.platform == 'win32':
        # Windows cwd/job binding is not read here; birth proof alone holds
        # the credit only when present, and absence conservatively refuses.
        return _process_birth_ms(pid) is not None
    cwd = _process_cwd(pid)
    return cwd is not None and _same_workspace(cwd, workspace)


def _validated_reservation_allowance(reservation, attempt_label):
    """Sane allowance plus attempt-label proof for one reservation.

    Identity equality against the launches row is enforced by the caller's
    bound check; here the stored attempt label must re-derive from the exact
    (session, operation, epoch) and the allowance must be a sane positive int
    within the fixed cap. Any failure is unknown current inflight: the full
    conservative default, never zero, never dropped or migrated."""
    allowance = reservation['allowance_bytes']
    if not (type(allowance) is int and 0 < allowance <= DEFAULT_ALLOWANCE_BYTES):
        return DEFAULT_ALLOWANCE_BYTES
    if attempt_label is not None:
        try:
            expected = attempt_label(reservation['session_id'], reservation['operation_id'],
                                     reservation['epoch'])
        except (TypeError, ValueError):
            return DEFAULT_ALLOWANCE_BYTES
        if reservation['attempt'] != expected:
            return DEFAULT_ALLOWANCE_BYTES
    return allowance


def _uncovered_charges(db, observation, exclude_operation, now_ms, attempt_label=None):
    """Conservative uncovered RAM charge of every current candidate.

    Driven from ALL guard-owned unreleased reservations until their exact
    qualified release - regardless of any terminal-looking launches state, so
    a retained expired_unconfirmed custody keeps its charge - plus every
    active launches attempt without a reservation (legacy marker-NULL rows
    included). A candidate charges zero only with complete positive
    same-incarnation OS proof at this final boundary; unknown, unbound or
    invalid identities charge the full conservative default and never zero.
    Undispatched pending/preparation rows hold no reservation and cannot
    spawn without passing this same serialized admission. Terminal history is
    never scanned and never poisons the gate."""
    residents = _resident_rows(observation, now_ms)
    credited = set()
    total = 0
    charged = set()
    reservations = db.execute(
        'SELECT r.operation_id AS operation_id, r.session_id AS session_id, r.epoch AS epoch, '
        'r.digest AS digest, r.attempt AS attempt, r.native_id AS reserved_native_id, '
        'r.allowance_bytes AS allowance_bytes, l.session_id AS launch_session_id, '
        'l.epoch AS launch_epoch, l.digest AS launch_digest, l.tag AS tag, '
        'l.workspace AS workspace, l.native_id AS native_id '
        'FROM resource_reservations r LEFT JOIN launches l ON l.operation_id=r.operation_id '
        'WHERE r.operation_id<>? LIMIT ?', (exclude_operation, MAX_INFLIGHT_ROWS + 1)).fetchall()
    if len(reservations) > MAX_INFLIGHT_ROWS:
        raise RuntimeError('active_native_bound_exceeded')
    for reservation in reservations:
        charged.add(reservation['operation_id'])
        bound = (reservation['launch_session_id'] == reservation['session_id']
                 and reservation['launch_epoch'] == reservation['epoch']
                 and reservation['launch_digest'] == reservation['digest']
                 and isinstance(reservation['native_id'], str)
                 and (reservation['reserved_native_id'] is None
                      or reservation['reserved_native_id'] == reservation['native_id']))
        if not bound:
            # Unresolvable or mismatched identity: unknown current inflight
            # keeps the full conservative default, never the stored allowance.
            total += DEFAULT_ALLOWANCE_BYTES
            continue
        if _mature_resident(reservation, residents, credited, now_ms):
            continue
        total += _validated_reservation_allowance(reservation, attempt_label)
    actives = db.execute(
        'SELECT operation_id, tag, workspace, native_id FROM launches '
        "WHERE state IN ('launching','running','aborting','deadline_stopping') "
        'AND operation_id<>? LIMIT ?', (exclude_operation, MAX_INFLIGHT_ROWS + 1)).fetchall()
    if len(actives) > MAX_INFLIGHT_ROWS:
        raise RuntimeError('active_native_bound_exceeded')
    for row in actives:
        if row['operation_id'] in charged:
            continue
        # Active attempt without a guard reservation: pre-feature legacy rows
        # and upgrade/crash cases keep their full conservative allowance
        # unless positively OS-proven as current mature residents.
        if _mature_resident(row, residents, credited, now_ms):
            continue
        total += DEFAULT_ALLOWANCE_BYTES
    return total


def admit_dispatch(bridge, operation, session, epoch, caps, census=None, sample=None, now_ms=None):
    """Admit one prepared dispatch against physical RAM, journal-atomic.

    ONE original monotonic deadline is established before any evidence
    collection or lock wait and clips every step: the private raw census, the
    BEGIN IMMEDIATE wait, the exact launch/epoch/digest/state=launching/
    marker=0/native-NULL re-read with its STOP fence, the conservative
    uncovered charges (final-boundary freshness and OS-liveness revalidation
    included), the physical sample taken last, and the commit. The returned
    permit carries that same original deadline for the launcher's read-only
    check immediately before the runner; the budget is never renewed.
    Refusals before the commit leave the marker untouched so the caller
    settles them through the existing no-dispatch path; a refusal after the
    commit keeps marker 1 and the reservation held (uncertain custody)."""
    deadline = time.monotonic() + ADMISSION_WINDOW_SECONDS
    db = bridge.journal.db
    if db.in_transaction:
        raise RuntimeError('native_dispatch_fence_conflict')
    if type(caps) is not dict:
        launcher_config = getattr(getattr(bridge, 'launcher', None), 'config', None)
        caps = validate_worker_caps((launcher_config or {}).get('memory_mb', None),
                                    (launcher_config or {}).get('pids', None))
    allowance = validate_worker_caps(caps.get('memory_mb'), caps.get('pids'))['memory_mb'] * 1024 * 1024
    observation = _raw_census_rows(bridge) if census is None else census
    if time.monotonic() > deadline:
        raise _unqualified()
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    sampler = sample if sample is not None else getattr(bridge, 'resource_sample', None)
    if sampler is None:
        sampler = sample_available_bytes
    db.execute('BEGIN IMMEDIATE')
    try:
        # The original deadline clips the lock wait: an admission whose whole
        # budget expired while waiting refuses here, before any state work.
        if time.monotonic() > deadline:
            raise _unqualified()
        row = db.execute('SELECT * FROM launches WHERE operation_id=?', (operation,)).fetchone()
        if (row is None or row['session_id'] != session or row['epoch'] != epoch
                or row['state'] != 'launching' or row['native_dispatch_started'] != 0
                or row['native_id'] is not None):
            raise RuntimeError('native_dispatch_fence_conflict')
        if db.execute('SELECT 1 FROM stop_fences WHERE session_id=? AND epoch>=?',
                      (session, epoch)).fetchone():
            raise RuntimeError('native_dispatch_fence_conflict')
        if db.execute('SELECT 1 FROM resource_reservations WHERE operation_id=?', (operation,)).fetchone():
            raise RuntimeError('native_dispatch_fence_conflict')
        digest, attempt = row['digest'], bridge.attempt_label(session, operation, epoch)
        # Final boundary against the one original deadline: the private raw
        # observation must still be fresh on its own collection clock, OS
        # proof and liveness revalidate per candidate, and the physical
        # sample is taken last. The permit never re-anchors after the census.
        charged = _uncovered_charges(db, observation, operation, now_ms,
                                     attempt_label=bridge.attempt_label)
        if time.monotonic() > deadline:
            raise _unqualified()
        available = sampler(deadline)  # the physical sample is always taken last
        if available - (charged + allowance) < RESERVED_FLOOR_BYTES:
            raise RuntimeError('host_launch_queue_full')
        db.execute('INSERT INTO resource_reservations(operation_id,session_id,epoch,digest,attempt,'
                   'allowance_bytes,updated_at_ms) VALUES(?,?,?,?,?,?,?)',
                   (operation, session, epoch, digest, attempt, allowance, now_ms))
        changed = db.execute(
            'UPDATE launches SET native_dispatch_started=1 WHERE operation_id=? AND session_id=? AND epoch=? '
            "AND state='launching' AND native_dispatch_started=0 AND native_id IS NULL",
            (operation, session, epoch))
        if changed.rowcount != 1:
            raise RuntimeError('native_dispatch_fence_conflict')
        if time.monotonic() > deadline:
            raise _unqualified()  # marker 1 and the reservation stay held; never reset
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return DispatchPermit(operation, session, epoch, permit_deadline=deadline)
