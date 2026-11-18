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
import os
import re
import subprocess
import sys
import time

# Fixed user policy floor for native dispatch, deliberately not the weaker
# configurable resource-management default and not operator-tunable here.
RESERVED_FLOOR_BYTES = 10 * 1024**3
DEFAULT_ALLOWANCE_BYTES = 1500 * 1024 * 1024
DEFAULT_MEMORY_MB = 1500
MAX_MEMORY_MB = 1500
DEFAULT_PIDS = 256
MAX_PIDS = 256
# The admission transaction holds the journal write lock only for bounded
# reads and one sample; exceeding the fixed window refuses fail-closed.
ADMISSION_WINDOW_SECONDS = 10
# Short original-monotonic permit anchored at the sample; the launcher checks
# it immediately before the runner and never renews it.
PERMIT_WINDOW_SECONDS = 30
# A resident worker is credited as mature only with positive same-incarnation
# liveness evidence (fresh census, exact bound native id/tag/workspace, live
# worker, running phase/state) and a birth older than this fixed window; its
# current usage is then already inside the physical sample. Anything younger
# or unproven keeps the full conservative allowance.
MATURE_RESIDENT_AGE_MS = 300_000
MAX_INFLIGHT_ROWS = 1000
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

    Duplicates, absence, malformed or negative values, available>total and
    oversized responses refuse; nothing is defaulted or extrapolated."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > _MEMINFO_MAX_BYTES:
        raise _unqualified()
    try:
        text = bytes(raw).decode('ascii')
    except UnicodeDecodeError:
        raise _unqualified() from None
    fields = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0] not in ('MemAvailable:', 'MemTotal:') or parts[2] != 'kB':
            continue
        if parts[0] in fields or not _MEMINFO_NUMBER.fullmatch(parts[1]):
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
    """Short original-monotonic permission to spawn the admitted dispatch.

    The deadline is fixed at the physical sample inside the admission
    transaction and is never renewed. Verification stays read-only: it never
    resets the dispatch marker or refunds the reservation, so a consumed
    permit leaves launch custody uncertain, never re-dispatched."""
    __slots__ = ('operation_id', 'session_id', 'epoch', 'sampled_monotonic', 'permit_deadline')

    def __init__(self, operation_id, session_id, epoch, sampled_monotonic):
        self.operation_id = operation_id
        self.session_id = session_id
        self.epoch = epoch
        self.sampled_monotonic = sampled_monotonic
        self.permit_deadline = sampled_monotonic + PERMIT_WINDOW_SECONDS

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


def _current_census(bridge):
    """One bounded private fresh registry census, or None.

    The collection is admission-private: it never reads, warms or publishes
    the launch reaper's shared census cache, because a census taken before
    this attempt spawned must never be applied later as expiry evidence for
    that same attempt. Absence stays conservative: without resident evidence
    every current attempt keeps its full allowance."""
    inventory = getattr(getattr(bridge, 'launcher', None), 'inventory', None)
    if not callable(inventory):
        return None
    try:
        return inventory()
    except _CENSUS_ERRORS:
        return None


def _same_workspace(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except (OSError, RuntimeError, ValueError):
        return False


def _resident_rows(census):
    if census is None or not census.fresh():
        return {}
    residents = {}
    for row in getattr(census, 'rows', ()):
        if isinstance(row, dict) and isinstance(row.get('id'), str):
            residents[row['id'].lower()] = row
    return residents


def _mature_resident(row, residents, credited, now_ms):
    """Positive same-incarnation liveness proof for one bound attempt.

    Requires the exact bound native UUID, its fresh census row with running
    phase/state, live worker, matching tag/workspace and a birth older than
    the fixed maturity window. Each census row credits at most one reservation;
    birth/tag/workspace mismatch or absence conservatively retains the full
    young allowance. Historical rows and release timestamps never credit."""
    native_id = row['native_id']
    if not isinstance(native_id, str) or not _UUID_RE.fullmatch(native_id):
        return False
    key = native_id.lower()
    if key in credited:
        return False
    resident = residents.get(key)
    if resident is None:
        return False
    birth = resident.get('created_at_ms')
    if (resident.get('worker_alive') is not True
            or resident.get('phase') != 'running' or resident.get('state') != 'running'
            or type(birth) is not int or birth <= 0 or now_ms - birth < MATURE_RESIDENT_AGE_MS
            or resident.get('tag') != row['tag']
            or not _same_workspace(resident.get('workspace'), row['workspace'])):
        return False
    credited.add(key)
    return True


def _uncovered_charges(db, census, exclude_operation, now_ms):
    """Conservative uncovered RAM charge of the current known-inflight set.

    Active dispatched attempts (marker=1, live states) are charged their
    reservation allowance, or the fixed conservative default when unreserved
    (upgrade/crash rows). Proven mature residents charge zero: their usage is
    already inside the physical sample. Undispatched pending/preparation rows
    hold no reservation and cannot spawn without passing this same serialized
    admission. Terminal history is never scanned and never poisons the gate."""
    rows = db.execute(
        'SELECT l.operation_id AS operation_id, l.tag AS tag, l.workspace AS workspace, '
        'l.native_id AS native_id, r.allowance_bytes AS allowance_bytes '
        'FROM launches l LEFT JOIN resource_reservations r ON r.operation_id=l.operation_id '
        "WHERE l.native_dispatch_started=1 AND l.state IN ('launching','running','aborting','deadline_stopping') "
        'AND l.operation_id<>? LIMIT ?', (exclude_operation, MAX_INFLIGHT_ROWS + 1)).fetchall()
    if len(rows) > MAX_INFLIGHT_ROWS:
        raise RuntimeError('active_native_bound_exceeded')
    residents = _resident_rows(census)
    credited = set()
    total = 0
    for row in rows:
        if _mature_resident(row, residents, credited, now_ms):
            continue
        allowance = row['allowance_bytes']
        if not (type(allowance) is int and 0 < allowance <= DEFAULT_ALLOWANCE_BYTES):
            allowance = DEFAULT_ALLOWANCE_BYTES
        total += allowance
    return total


def admit_dispatch(bridge, operation, session, epoch, caps, census=None, sample=None, now_ms=None):
    """Admit one prepared dispatch against physical RAM, journal-atomic.

    Inside one BEGIN IMMEDIATE transaction: re-read the exact launch row
    (session/epoch/digest/state=launching/marker=0/native NULL) and its STOP
    fence, validate the same caps the argv emits, refresh current inflight/
    resident evidence, sample physical available RAM last, require floor
    headroom, then commit the reservation with the original 0->1 marker CAS.
    Refusals leave the marker untouched so the caller settles them through
    the existing no-dispatch path; post-commit refusals stay uncertain."""
    db = bridge.journal.db
    if db.in_transaction:
        raise RuntimeError('native_dispatch_fence_conflict')
    if type(caps) is not dict:
        launcher_config = getattr(getattr(bridge, 'launcher', None), 'config', None)
        caps = validate_worker_caps((launcher_config or {}).get('memory_mb', None),
                                    (launcher_config or {}).get('pids', None))
    allowance = validate_worker_caps(caps.get('memory_mb'), caps.get('pids'))['memory_mb'] * 1024 * 1024
    census = _current_census(bridge) if census is None else census
    deadline = time.monotonic() + ADMISSION_WINDOW_SECONDS
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    sampler = sample if sample is not None else getattr(bridge, 'resource_sample', None)
    if sampler is None:
        sampler = sample_available_bytes
    db.execute('BEGIN IMMEDIATE')
    try:
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
        charged = _uncovered_charges(db, census, operation, now_ms)
        sampled = time.monotonic()
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
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return DispatchPermit(operation, session, epoch, sampled)
