"""Native Claude Code credential-store access for the one-way default consumer.

Protocol facts (pinned as ``PROTOCOL``; observed in the public Claude Code
2.1.322 binary by reading bounded code excerpts, never an auth or config file):

* The file store is ``<store>/.credentials.json``. ``<store>`` is
  ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` when that variable is defined (an empty
  value means ``~/.claude``), otherwise the ordinary config directory
  (``CLAUDE_CONFIG_DIR`` or ``~/.claude``). Only enrollment resolves this; the
  runtime consumer never follows the ambient environment.
* A refresh takes ``<store>/.oauth_refresh.lock`` and then the legacy lock
  ``realpath(<store>) + '.lock'`` (proper-lockfile mkdir locks, ``realpath``
  off, stale 60 s, heartbeat 5 s) before the HTTP refresh and before touching
  the store. Its refresh-save then runs a storage mutation that takes
  ``<store>/.storage-write.lock`` (stale 15 s, heartbeat 7.5 s) across a fresh
  read, the callback and the full-document update. The refresh-save
  compare-and-set also accepts a stored ``refreshToken`` of ``''``, so the
  storage lock alone does not exclude a refresh that is already in flight.

This module takes the same locks in the same order (refresh, legacy, storage)
so a writer here and a native refresh are totally ordered. Limits, stated
honestly: it does NOT coordinate with a writer that ignores these locks (a
direct file writer, the legacy ``claude-cred-sync`` copier, a different native
release with another protocol). It never removes a lock it does not own: a
stale or foreign lock is a bounded refusal, because breaking it is the native
contenders' own rule and a wrong guess would corrupt a live refresh. Own locks
carry a heartbeat and are released only while this exact directory incarnation
is still the one created; a crashed owner's incarnation is recorded privately
so a later run can remove precisely that dead incarnation.

Nothing here logs, prints or raises with credential content or paths.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import unicodedata

from . import private_fs

PROTOCOL = 'claude-file-store-locks-v1'
SUPPORTED_PROTOCOLS = (PROTOCOL,)
CREDENTIAL_NAME = '.credentials.json'
MAX_DOCUMENT = 32000
LOCK_WAIT_S = 20.0
RETRY_MIN_S = 0.1
RETRY_MAX_S = 1.0
# (name, stale seconds, heartbeat seconds) in the native acquisition order.
REFRESH_LOCK = ('refresh', 60.0, 5.0)
LEGACY_LOCK = ('legacy', 60.0, 5.0)
STORAGE_LOCK = ('storage', 15.0, 7.5)
LOCK_ORDER = (REFRESH_LOCK, LEGACY_LOCK, STORAGE_LOCK)


class StoreRefused(Exception):
    """A fixed, non-sensitive reason code (never a path, token or exception text)."""


def effective_store(environ=None, home=None):
    """The directory the native CLI would use for its credential file."""
    environ = os.environ if environ is None else environ
    home = Path(os.path.expanduser('~') if home is None else home)
    explicit = environ.get('CLAUDE_SECURESTORAGE_CONFIG_DIR')
    if explicit is not None:
        value = explicit or str(home / '.claude')
    else:
        value = environ.get('CLAUDE_CONFIG_DIR') or str(home / '.claude')
    value = unicodedata.normalize('NFC', value)
    if '\0' in value or not os.path.isabs(value):
        raise StoreRefused('store_path_invalid')
    return Path(os.path.normpath(value))


def lock_paths(store):
    store = Path(store)
    return {'refresh': store / '.oauth_refresh.lock',
            'legacy': store.parent / (store.name + '.lock'),
            'storage': store / '.storage-write.lock'}


def _dir_safe(info):
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022)


def store_identity(store):
    """(dev, ino) of a verified store directory: owned, not group/world writable,
    reached without following any symlink."""
    store = Path(store)
    try:
        with private_fs.parent(store / CREDENTIAL_NAME, private=False) as anchor:
            info = os.fstat(anchor)
            if not _dir_safe(info):
                raise StoreRefused('store_untrusted')
            with private_fs.parent(store, private=False) as outer:
                if not _dir_safe(os.fstat(outer)):
                    raise StoreRefused('store_untrusted')
            return info.st_dev, info.st_ino
    except StoreRefused:
        raise
    except FileNotFoundError:
        raise StoreRefused('store_missing') from None
    except (OSError, ValueError):
        raise StoreRefused('store_untrusted') from None


def read_raw(store):
    """Bytes of the credential file, or None when absent. Refuses symlinks,
    hard links, foreign owners, non-regular and oversize files."""
    target = Path(store) / CREDENTIAL_NAME
    try:
        with private_fs.parent(target, private=False) as anchor:
            if not _dir_safe(os.fstat(anchor)):
                raise StoreRefused('store_untrusted')
            try:
                fd = private_fs._open(anchor, target, os.O_RDONLY, private=False)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if info.st_uid != os.getuid() or info.st_size > MAX_DOCUMENT:
                    raise StoreRefused('store_untrusted')
                raw = stream.read(MAX_DOCUMENT + 1)
                if len(raw) > MAX_DOCUMENT:
                    raise StoreRefused('store_untrusted')
                return raw
    except StoreRefused:
        raise
    except FileNotFoundError:
        raise StoreRefused('store_missing') from None
    except (OSError, ValueError):
        raise StoreRefused('store_untrusted') from None


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate')
        value[key] = item
    return value


def _constant(_):
    raise ValueError('non-finite number')


def loads_strict(text):
    """JSON.parse-compatible: no duplicate keys, no NaN/Infinity."""
    return json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)


def parse_document(raw):
    """(document, oauth) for a well-formed native credential file."""
    try:
        document = loads_strict(raw.decode('utf8'))
    except (ValueError, UnicodeDecodeError, AttributeError):
        raise StoreRefused('store_malformed') from None
    oauth = document.get('claudeAiOauth') if isinstance(document, dict) else None
    if (not isinstance(oauth, dict) or not isinstance(oauth.get('accessToken'), str)
            or not oauth['accessToken'].strip()):
        raise StoreRefused('store_malformed')
    return document, oauth


def oauth_digest(oauth):
    canonical = json.dumps(oauth, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode('utf8')).hexdigest()


def render_document(document, original_raw):
    """Serialize like the file it replaces (JS compact or two-space indented)."""
    try:
        if original_raw is not None and b'\n' in original_raw.strip():
            return (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf8')
        return json.dumps(document, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf8')
    except ValueError:
        raise StoreRefused('store_malformed') from None


def write_document(store, raw):
    """Atomic, fsynced replacement of the credential file (caller holds the locks)."""
    private_fs.atomic_write(Path(store) / CREDENTIAL_NAME, raw, MAX_DOCUMENT, engine_owned=True)
    with contextlib.suppress(OSError):
        os.chmod(Path(store) / CREDENTIAL_NAME, 0o600)


# --------------------------------------------------------------------------
# proper-lockfile compatible mkdir locks
# --------------------------------------------------------------------------

def _process_start(pid):
    try:
        with open('/proc/%d/stat' % pid, 'rb') as handle:
            return handle.read().rsplit(b') ', 1)[1].split()[19].decode()
    except (OSError, IndexError):
        return None


def process_alive(pid, start):
    """True unless this exact process incarnation is provably gone."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    current = _process_start(pid)
    if current is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True  # signal 0 succeeded and /proc is unreadable: not provably gone
    return start is None or current == start


XATTR = 'user.agent-hub-default-claude-lock'


class _Held:
    """One lock directory this process created and heartbeats.

    An O_DIRECTORY descriptor pins the live incarnation: while it is open the
    inode cannot be recycled, so a different (ino, dev) at the path always
    means somebody removed and recreated it. A random nonce is also written as
    an extended attribute (when the filesystem has them) so that a *crashed*
    owner's incarnation can later be proven exactly; without that proof it is
    never reaped automatically."""

    def __init__(self, path, update_s):
        self.path, self.update_s = path, update_s
        self.compromised = False
        self._fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(self._fd)
            seen = os.lstat(path)
            self.dev, self.ino, self.mtime_ns = info.st_dev, info.st_ino, seen.st_mtime_ns
            if (seen.st_dev, seen.st_ino) != (info.st_dev, info.st_ino):
                self.compromised = True
        except BaseException:
            os.close(self._fd)
            raise
        self.nonce = os.urandom(16).hex()
        try:
            os.setxattr(self._fd, XATTR, self.nonce.encode())
        except (OSError, AttributeError):
            self.nonce = None
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._thread = threading.Thread(target=self._beat, daemon=True, name='native-lock-heartbeat')
        self._thread.start()

    def _mine(self):
        try:
            info = os.lstat(self.path)
        except OSError:
            return None
        if (info.st_dev, info.st_ino) != (self.dev, self.ino) or not stat.S_ISDIR(info.st_mode):
            return None
        return info

    def touch(self):
        with self._guard:
            if self._fd is None:
                return False
            info = self._mine()
            if info is None or info.st_mtime_ns != self.mtime_ns or self.compromised:
                self.compromised = True
                return False
            now = time.time()
            try:
                os.utime(self.path, (now, now), follow_symlinks=False)
                info = self._mine()
            except OSError:
                info = None
            if info is None:
                self.compromised = True
                return False
            self.mtime_ns = info.st_mtime_ns
            return True

    def _beat(self):
        while not self._stop.wait(self.update_s):
            if not self.touch():
                return

    def release(self):
        self._stop.set()
        self._thread.join(timeout=2)
        with self._guard:
            fd, self._fd = self._fd, None
            try:
                info = self._mine()
                # Only the incarnation we created, with the mtime we last set.
                if info is None or info.st_mtime_ns != self.mtime_ns or self.compromised:
                    self.compromised = True
                    return False
                try:
                    os.rmdir(self.path)
                except OSError:
                    return False
                return True
            finally:
                if fd is not None:
                    os.close(fd)


class IncarnationLedger:
    """Private record of lock directories this consumer created, so that a crashed
    consumer's own dead incarnation (and only that) can be cleaned up later.

    Reaping needs the exact durable proof: recorded (dev, ino), the recorded
    nonce still present as the directory's extended attribute, and a provably
    gone owner process. An inode number alone can be recycled by a foreign lock,
    so an entry without a nonce (no xattr support) is never reaped."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            value = json.loads(private_fs.read_bytes(self.path, 65536))
        except (FileNotFoundError, ValueError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self, value):
        private_fs.atomic_write(self.path, json.dumps(value, sort_keys=True).encode(), 65536)

    def record(self, name, held):
        value = self._load()
        value[name] = {'dev': held.dev, 'ino': held.ino, 'nonce': held.nonce, 'pid': os.getpid(),
                       'start': _process_start(os.getpid())}
        self._save(value)

    def forget(self, name):
        value = self._load()
        if value.pop(name, None) is not None:
            self._save(value)

    def reap(self, name, lock_path):
        """Remove ``lock_path`` iff it is provably the recorded incarnation of a
        dead owner. Never touches any other directory."""
        entry = self._load().get(name)
        if not isinstance(entry, dict) or not isinstance(entry.get('nonce'), str) or not entry['nonce']:
            return False
        try:
            info = os.lstat(lock_path)
            found = os.getxattr(lock_path, XATTR, follow_symlinks=False)
        except (OSError, AttributeError):
            return False
        if ((info.st_dev, info.st_ino) != (entry.get('dev'), entry.get('ino')) or not stat.S_ISDIR(info.st_mode)
                or found != entry['nonce'].encode() or process_alive(entry.get('pid'), entry.get('start'))):
            return False
        try:
            os.rmdir(lock_path)
        except OSError:
            return False
        self.forget(name)
        return True


def _acquire(name, path, stale_s, update_s, deadline, ledger, sleep, clock):
    delay = RETRY_MIN_S
    while True:
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            try:
                age = time.time() - os.lstat(path).st_mtime
            except FileNotFoundError:
                continue
            except OSError:
                raise StoreRefused('lock_unavailable') from None
            if ledger is not None and ledger.reap(name, path):
                continue
            if age > stale_s:
                # A stale lock that is not our own dead incarnation is foreign:
                # breaking it is the native contenders' decision, never ours.
                raise StoreRefused('lock_stale_foreign')
            remaining = deadline - clock()
            if remaining <= 0:
                raise StoreRefused('lock_busy')
            sleep(min(delay, remaining))
            delay = min(RETRY_MAX_S, delay * 2)
            continue
        except OSError:
            raise StoreRefused('lock_unavailable') from None
        try:
            held = _Held(path, update_s)
        except OSError:
            raise StoreRefused('lock_unavailable') from None
        if ledger is not None:
            try:
                ledger.record(name, held)
            except (OSError, ValueError):
                held.release()
                raise StoreRefused('lock_unavailable') from None
        return held


class NativeLocks:
    """Context manager holding the refresh, legacy and storage locks, in that order.

    ``compromised()`` becomes true when any held lock was lost; check it right
    before the replacement."""

    def __init__(self, store, ledger=None, wait_s=LOCK_WAIT_S, sleep=time.sleep, clock=time.monotonic):
        self.store, self.ledger = Path(store), ledger
        self.wait_s, self.sleep, self.clock = wait_s, sleep, clock
        self._held = []

    def __enter__(self):
        paths = lock_paths(self.store)
        deadline = self.clock() + self.wait_s
        try:
            for name, stale_s, update_s in LOCK_ORDER:
                self._held.append((name, _acquire(name, paths[name], stale_s, update_s, deadline,
                                                  self.ledger, self.sleep, self.clock)))
        except BaseException:
            self._release()
            raise
        return self

    def compromised(self):
        return any(held.compromised or held.touch() is False for _, held in self._held)

    def _release(self):
        while self._held:
            name, held = self._held.pop()
            held.release()
            if self.ledger is not None:
                with contextlib.suppress(OSError, ValueError):
                    self.ledger.forget(name)

    def __exit__(self, *exc):
        self._release()
        return False
