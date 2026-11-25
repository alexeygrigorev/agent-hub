#!/usr/bin/env python3
"""Default-inert Root staging helper for the natural-quota release DATA.

Stdlib only. It never imports, executes or compiles USER code, an installed USER
module, a downloaded script, or wheel/archive code: every downloaded byte is data
that is statically validated and written as an immutable Root-owned file.
Authority comes only from the fixed Root-owned config below, never from the
command line, environment, caller PID or profile.

It does NOT install or replace any Root executable, start a service, or activate
runtime authority. Same-boot donation custody retirement is an UNRESOLVED
separate primitive: this helper never invents a retirement proof (a changed
UID/cgroup/PID string, timeout, or new epoch is not positive death). It
therefore writes the fixed donor profile ONLY when there has never been a profile,
ledger or gate node; it leaves any existing profile/ledger/gate node untouched and
reports custody UNKNOWN (non-gating) when the profile cannot be left as-is.
A future boot-id change proof could be documented but is deliberately not
implemented here.
"""
import argparse
import fcntl
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import socket
import ssl
import stat
import sys
import time
import zipfile

ENTRY_PATH = '/usr/local/lib/agent-hub/natural/hub-natural-stage.py'
CONFIG_PATH = '/etc/agent-hub/natural-stage.json'
PROFILE_PATH = '/etc/agent-hub/natural-donor.json'
RELEASE_ROOT = '/usr/local/lib/agent-hub/natural/releases'
LEDGER_PATH = '/var/lib/agent-hub/natural-donor/ledger.json'
CA_BUNDLE = '/etc/ssl/certs/ca-certificates.crt'
TARGET_UNIT = 'agent-hub2-host.service'
APPROVED_INTERPRETERS = ('/usr/bin/python3.11', '/usr/bin/python3.12')
# Trust anchor and Root uid are constants in production; tests substitute simulated facts.
ROOT_UID = 0
TRUST_ANCHOR = '/'
CONFIG_FIELDS = {'v', 'base_url', 'target_user', 'python_bin', 'health_config_path'}

OPERATION_DEADLINE_S = 180
MAX_CONFIG = 16 * 1024
MAX_MANIFEST = 128 * 1024
MAX_ARTIFACT = 32 * 1024 * 1024
MAX_ENTRY = 16 * 1024 * 1024  # donor/startup ceiling for host.pyz
MAX_PAYLOAD = 262144  # donor/startup ceiling for business sources
MAX_MEMBER = 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_MEMBERS = 512

ARCHIVE = 'agent-hub-quota-dispatcher.pyz'
PAYLOADS = ('hub-quota-publish.py', 'hub_alert_client.py', 'natural-quota-prelude.py')
HELPERS = ('hub-alert-natural-donor.py', 'hub-natural-stage.py')
ARCHIVE_PAYLOADS = ('hub-quota-publish.py', 'hub_alert_client.py', 'natural-quota-prelude.py')
OPTIONAL_NATURAL = (ARCHIVE, 'hub-quota-publish.py', 'natural-quota-prelude.py', *HELPERS)
TRANSPORT_WHEEL = 'websockets-16.0-py3-none-any.whl'
HOST_WHEEL = re.compile(r'agent_hub_host_v2-2\.0\.0\+g([0-9a-f]{40})-py3-none-any\.whl')
NATURAL_MAIN_STUB = (
    'from agent_hub_host.quota_dispatch_entry import main\n'
    'raise SystemExit(main())\n'
)
NATURAL_HOST_CLOSURE = (
    '__init__.py', 'alert_authority.py', 'alert_broker.py', 'alert_collector.py',
    'quota_dispatch.py', 'quota_dispatch_manager.py', 'quota_dispatch_entry.py',
    'quota_natural_primary.py', 'quota_natural_resources.py', 'quota_natural_startup.py',
    'agent.py', 'cli.py', 'transport.py', '_release.py',
)
STAGE_FILES = ('host.pyz', *PAYLOADS, 'selection.json')


class StageError(Exception):
    """Fail-closed refusal; the reason is a short stable token."""


def _require(cond, reason='stage_rejected'):
    if not cond:
        raise StageError(reason)


def _unique_pairs(items):
    out = {}
    for key, value in items:
        _require(key not in out, 'duplicate_json_key')
        out[key] = value
    return out


def parse_json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: _require(False, 'invalid_json_constant'))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise StageError('invalid_json') from None


def _close(fds):
    clean = True
    while fds:
        fd = fds.pop()
        if fd is not None and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                clean = False
    return clean


# --------------------------------------------------------------------------
# Root-owned directory graph
# --------------------------------------------------------------------------
def _root_dir_ok(info):
    return stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_UID and not info.st_mode & 0o022


def open_root_dir(path, *, create_last=None):
    """Open a directory through Root-owned, non-symlink, non-writable ancestors.

    `create_last` (a mode) makes only the final missing component. Existing nodes
    are never modified.
    """
    p = Path(path)
    anchor = Path(TRUST_ANCHOR)
    _require(p.is_absolute() and '..' not in p.parts and str(p) == path
             and (p == anchor or anchor in p.parents), 'invalid_path')
    fd = os.open(str(anchor), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _require(_root_dir_ok(os.fstat(fd)), 'untrusted_root_directory')
        parts = p.relative_to(anchor).parts
        for index, part in enumerate(parts):
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                if create_last is None or index != len(parts) - 1:
                    raise StageError('missing_directory') from None
                os.mkdir(part, create_last, dir_fd=fd)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.fchmod(nxt, create_last)
                os.fsync(fd)
            except OSError:
                raise StageError('untrusted_directory') from None
            old, fd = fd, nxt
            os.close(old)
            _require(_root_dir_ok(os.fstat(fd)), 'untrusted_directory')
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            _close([fd])


def read_root_file(dir_fd, name, limit, *, forbid_mode=0o022, optional=False):
    """Read a Root-owned regular single-link file; returns (bytes, stat) or None."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        if optional:
            return None
        raise StageError('missing_root_file') from None
    except OSError:
        raise StageError('untrusted_root_file') from None
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == ROOT_UID and before.st_nlink == 1
                 and not before.st_mode & forbid_mode and 0 < before.st_size <= limit,
                 'untrusted_root_file')
        raw = os.pread(fd, before.st_size + 1, 0)
        after = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        _require(len(raw) == before.st_size and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'root_file_replaced')
        return raw, before
    finally:
        _close([fd])


# --------------------------------------------------------------------------
# Fixed configuration
# --------------------------------------------------------------------------
def validate_base_url(value):
    _require(type(value) is str and value.isascii() and 0 < len(value) <= 253, 'invalid_base_url')
    match = re.fullmatch(r'https://([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::([0-9]{1,5}))?/?', value)
    _require(match is not None and '..' not in match.group(1) and not re.fullmatch(r'[0-9.]+', match.group(1)),
             'invalid_base_url')
    port = int(match.group(2)) if match.group(2) else 443
    _require(0 < port < 65536, 'invalid_base_url')
    return match.group(1), port


def validate_user_path(value):
    _require(type(value) is str and '\0' not in value and 0 < len(os.fsencode(value)) <= 4096, 'invalid_health_config_path')
    p = Path(value)
    _require(p.is_absolute() and '..' not in p.parts and str(p) == value and p.name not in ('', '.', '..'),
             'invalid_health_config_path')
    return value


def load_config(path=CONFIG_PATH):
    p = Path(path)
    dir_fd = open_root_dir(str(p.parent))
    try:
        raw, _ = read_root_file(dir_fd, p.name, MAX_CONFIG, forbid_mode=0o077)
    finally:
        _close([dir_fd])
    cfg = parse_json(raw)
    _require(type(cfg) is dict and set(cfg) == CONFIG_FIELDS, 'invalid_config_schema')
    _require(type(cfg['v']) is int and cfg['v'] == 1, 'unsupported_config_version')
    validate_base_url(cfg['base_url'])
    _require(type(cfg['target_user']) is str and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', cfg['target_user']),
             'invalid_target_user')
    _require(cfg['python_bin'] in APPROVED_INTERPRETERS, 'unapproved_interpreter')
    validate_user_path(cfg['health_config_path'])
    return cfg


# --------------------------------------------------------------------------
# Fixed-origin HTTPS transport (the only network surface)
# --------------------------------------------------------------------------
class HttpsTransport:
    """GET one exact path from the configured origin; no redirects/proxies/env CA."""

    def __init__(self, base_url, deadline, *, ca_bundle=CA_BUNDLE, monotonic=time.monotonic,
                 connection_factory=http.client.HTTPSConnection):
        self.host, self.port = validate_base_url(base_url)
        self.deadline = deadline
        self.ca_bundle = ca_bundle
        self.monotonic = monotonic
        self.connection_factory = connection_factory
        self.context = None

    def _remaining(self):
        left = self.deadline - self.monotonic()
        _require(left > 0, 'deadline_expired')
        return left

    def _context(self):
        if self.context is None:
            # cafile pins the trust anchors; SSL_CERT_FILE/SSL_CERT_DIR are not consulted.
            context = ssl.create_default_context(cafile=self.ca_bundle)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.context = context
        return self.context

    def get(self, name, limit):
        _require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*', name) is not None, 'unsafe_artifact_name')
        connection = None
        try:
            connection = self.connection_factory(self.host, self.port, timeout=self._remaining(),
                                                 context=self._context())
            connection.request('GET', '/cli/' + name, headers={'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = connection.getresponse()
            _require(response.status == 200, 'unexpected_http_status')
            declared = response.getheader('Content-Length')
            if declared is not None:
                _require(declared.isdigit() and int(declared) <= limit, 'artifact_too_large')
            _require(response.getheader('Content-Encoding') in (None, 'identity'), 'unexpected_encoding')
            chunks = []
            total = 0
            while True:
                self._remaining()
                chunk = response.read(min(65536, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                _require(total <= limit, 'artifact_too_large')
                chunks.append(chunk)
            data = b''.join(chunks)
            if declared is not None:
                _require(len(data) == int(declared), 'artifact_truncated')
            return data
        except (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout):
            raise StageError('transport_failure') from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


# --------------------------------------------------------------------------
# Static validation (data only)
# --------------------------------------------------------------------------
def validate_manifest(manifest, python_bin):
    """Standalone mirror of the updater's schema check. Returns (host_wheel, natural set?)."""
    _require(type(manifest) is dict and manifest.get('schema_version') == 1
             and type(manifest.get('protocol_version')) is int and manifest['protocol_version'] >= 1,
             'unsupported_release_protocol')
    requirement = re.fullmatch(r'>=(\d+)\.(\d+)', str(manifest.get('python_requires', '')))
    minor = int(python_bin.rsplit('python3.', 1)[1])
    _require(requirement is not None and tuple(map(int, requirement.groups())) <= (3, minor), 'unsupported_python')
    version = manifest.get('host_version')
    _require(type(version) is str and re.fullmatch(r'[0-9a-f]{40}', version) is not None, 'invalid_host_version')
    files = manifest.get('artifacts')
    _require(type(files) is dict and 6 <= len(files) <= 20, 'invalid_artifact_list')
    for name, record in files.items():
        _require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*', name) is not None, 'unsafe_artifact_name')
        _require(type(record) is dict and set(record) == {'sha256', 'size'}
                 and type(record['sha256']) is str and re.fullmatch(r'[0-9a-f]{64}', record['sha256']) is not None,
                 'invalid_artifact_checksum')
        _require(type(record['size']) is int and 0 < record['size'] <= MAX_ARTIFACT, 'invalid_artifact_size')
    wheels = [name for name in files if HOST_WHEEL.fullmatch(name)]
    _require(len(wheels) == 1 and HOST_WHEEL.fullmatch(wheels[0]).group(1) == version and TRANSPORT_WHEEL in files,
             'release_wheel_mismatch')
    present = [name for name in OPTIONAL_NATURAL if name in files]
    if not present:
        return wheels[0], False
    # Partial or mixed natural sets are refused, never staged as a subset.
    _require(len(present) == len(OPTIONAL_NATURAL) and 'hub_alert_client.py' in files, 'partial_natural_set')
    for name in (*PAYLOADS, HELPERS[0], HELPERS[1]):
        _require(name in files, 'partial_natural_set')
    return wheels[0], True


def _safe_member(entry):
    name = entry.filename
    path = PurePosixPath(name)
    _require(not (path.is_absolute() or '..' in path.parts or '\\' in name or '\0' in name
                  or name.rstrip('/') != str(path) or any(ord(c) < 32 for c in name)
                  or not name.isascii() or stat.S_ISLNK(entry.external_attr >> 16)
                  or entry.flag_bits & 0x1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)),
             'unsafe_archive_member')


def read_zip_members(data, *, suffix_filter=None):
    """Bounded read of every regular member: returns {name: bytes}."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise StageError('invalid_zip') from None
    with archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        _require(len(entries) <= MAX_MEMBERS and len(set(names)) == len(names), 'archive_member_ambiguity')
        members = {}
        total = 0
        for entry in entries:
            _safe_member(entry)
            if entry.is_dir():
                continue
            if suffix_filter is not None and not suffix_filter(entry.filename):
                continue
            _require(0 <= entry.file_size <= MAX_MEMBER, 'archive_member_size')
            total += entry.file_size
            _require(total <= MAX_TOTAL, 'archive_total_size')
            try:
                with archive.open(entry) as stream:
                    raw = stream.read(entry.file_size + 1)
            except (zipfile.BadZipFile, RuntimeError, EOFError, ValueError, NotImplementedError):
                raise StageError('invalid_zip_member') from None
            _require(len(raw) == entry.file_size, 'archive_member_size')
            members[entry.filename] = raw
        return members


def wheel_python_hashes(wheel, package):
    members = read_zip_members(wheel, suffix_filter=lambda n: n.startswith(package + '/') and n.endswith('.py'))
    _require(package + '/__init__.py' in members, 'outer_wheel_package_missing')
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in members.items()}, members


def validate_archive(archive, *, manifest, host_wheel_name, wheel_bytes, transport_bytes, payloads):
    """Static comparison of the natural archive against the trusted outer manifest/wheels/payloads."""
    version = manifest['host_version']
    records = manifest['artifacts']
    marker = f'HOST_VERSION = {version!r}\nPROTOCOL_VERSION = {manifest["protocol_version"]}\n'
    outer_host, host_members = wheel_python_hashes(wheel_bytes, 'agent_hub_host')
    _require(host_members.get('agent_hub_host/_release.py', b'').decode('utf-8', 'replace') == marker,
             'outer_wheel_marker_mismatch')
    outer_transport, _ = wheel_python_hashes(transport_bytes, 'websockets')
    members = read_zip_members(archive)
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in members.items()}
    _require('MANIFEST.json' in members and '__main__.py' in members, 'archive_missing_manifest_or_entry')
    inner = parse_json(members['MANIFEST.json'])
    _require(type(inner) is dict and inner.get('v') == 1
             and inner.get('entry') == 'agent_hub_host.quota_dispatch_entry:main'
             and type(inner.get('sources')) is dict and type(inner.get('host_release')) is dict,
             'invalid_archive_manifest')
    sources = inner['sources']
    expected_members = set(hashes) - {'MANIFEST.json'}
    _require(set(sources) == expected_members, 'archive_manifest_member_mismatch')
    for name, digest in sources.items():
        _require(hashes[name] == digest, 'archive_member_checksum_mismatch')
    required = {'__main__.py', 'websockets/__init__.py', 'websockets/exceptions.py', 'websockets/sync/client.py',
                *(f'agent_hub_host/{n}' for n in NATURAL_HOST_CLOSURE),
                *(f'natural/{n}' for n in ARCHIVE_PAYLOADS)}
    _require(required <= expected_members, 'archive_lacks_required_closure')
    exact = {'__main__.py', *outer_host, *outer_transport, *(f'natural/{n}' for n in ARCHIVE_PAYLOADS)}
    _require(expected_members == exact, 'archive_members_differ_from_outer_inputs')
    for name, digest in {**outer_host, **outer_transport}.items():
        _require(hashes[name] == digest, 'archive_module_differs_from_outer_wheel')
    _require(members['__main__.py'].decode('utf-8', 'replace') == NATURAL_MAIN_STUB, 'archive_entry_stub_mismatch')
    release = inner['host_release']
    _require(release.get('commit') == version
             and release.get('host_wheel_sha256') == records[host_wheel_name]['sha256']
             and release.get('transport_wheel_sha256') == records[TRANSPORT_WHEEL]['sha256'],
             'archive_binding_mismatch')
    declared = release.get('natural_payloads')
    _require(type(declared) is dict and set(declared) == set(ARCHIVE_PAYLOADS), 'archive_payload_provenance_invalid')
    for name in ARCHIVE_PAYLOADS:
        digest = hashlib.sha256(payloads[name]).hexdigest()
        _require(declared[name] == digest == hashes['natural/' + name] == records[name]['sha256'],
                 'archive_payload_mismatch')
    _require(members['agent_hub_host/_release.py'].decode('utf-8', 'replace') == marker, 'archive_release_marker_mismatch')
    return True


# --------------------------------------------------------------------------
# Selection / profile (exact schemas of read_selection / load_fixed_profile)
# --------------------------------------------------------------------------
def gate_directory_for(uid):
    return f'/run/user/{uid}/agent-hub-natural/gate'


def broker_directory_for(uid):
    return f'/run/user/{uid}/agent-hub-natural/broker'


def build_selection(version, uid, health_config_path, digests):
    gate = gate_directory_for(uid)
    _require(len(os.fsencode(gate)) <= 80, 'gate_directory_too_long')
    selection = {
        'v': 1,
        'kind': 'natural_quota_roles_v1',
        'quota_path': f'{RELEASE_ROOT}/{version}/hub-quota-publish.py',
        'client_path': f'{RELEASE_ROOT}/{version}/hub_alert_client.py',
        'gate_directory': gate,
        'quota_sha256': digests['hub-quota-publish.py'],
        'client_sha256': digests['hub_alert_client.py'],
        'prelude_sha256': digests['natural-quota-prelude.py'],
        'roles': {
            'quota_publish': {'argv': []},
            'quota_monitor': {'argv': ['--check', '--health-check', '--health-config', health_config_path,
                                       '--health-timeout', '8', '--alert-timeout', '8']},
        },
    }
    return (json.dumps(selection, sort_keys=True, separators=(',', ':')) + '\n').encode()


def build_profile(cfg, version, uid, entry_sha256):
    profile = {
        'v': 1,
        'target_user': cfg['target_user'],
        'unit': TARGET_UNIT,
        'python_bin': cfg['python_bin'],
        'release_commit': version,
        'entry_archive': f'{RELEASE_ROOT}/{version}/host.pyz',
        'entry_sha256': entry_sha256,
        'source_directory': f'{RELEASE_ROOT}/{version}',
        'selection_path': f'{RELEASE_ROOT}/{version}/selection.json',
        'broker_directory': broker_directory_for(uid),
        'gate_directory': gate_directory_for(uid),
        'ledger_path': LEDGER_PATH,
    }
    return profile, (json.dumps(profile, sort_keys=True, separators=(',', ':')) + '\n').encode()


# --------------------------------------------------------------------------
# Immutable versioned release publication
# --------------------------------------------------------------------------
def _write_new(dir_fd, name, data, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            _require(written > 0, 'write_failed')
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        _close([fd])


def _release_matches(releases_fd, version, files):
    """Existing release must byte-match exactly; otherwise refuse (never overwrite)."""
    try:
        fd = os.open(version, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=releases_fd)
    except FileNotFoundError:
        return False
    except OSError:
        raise StageError('existing_release_untrusted') from None
    try:
        _require(_root_dir_ok(os.fstat(fd)) and set(os.listdir(fd)) == set(files), 'existing_release_mismatch')
        for name, data in files.items():
            found = read_root_file(fd, name, MAX_ARTIFACT, forbid_mode=0o222)
            _require(found[0] == data and stat.S_IMODE(found[1].st_mode) == 0o444, 'existing_release_mismatch')
        return True
    finally:
        _close([fd])


def publish_release(version, files, *, release_root=RELEASE_ROOT, token=None):
    """Publish `files` as an immutable directory. Returns 'staged' or 'already_staged'."""
    parent_path = str(Path(release_root).parent)
    parent_fd = open_root_dir(parent_path)
    releases_fd = -1
    scratch = None
    created = []
    try:
        name = Path(release_root).name
        try:
            releases_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        except FileNotFoundError:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
            releases_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            os.fchmod(releases_fd, 0o755)
            os.fsync(parent_fd)
        except OSError:
            raise StageError('untrusted_release_root') from None
        _require(_root_dir_ok(os.fstat(releases_fd)), 'untrusted_release_root')
        try:
            fcntl.flock(releases_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise StageError('release_root_busy') from None
        if _release_matches(releases_fd, version, files):
            return 'already_staged'
        _require(not _exists(releases_fd, version), 'existing_release_mismatch')
        scratch = f'.stage-{version[:12]}-{os.getpid()}-{token or time.monotonic_ns()}'
        os.mkdir(scratch, 0o700, dir_fd=releases_fd)
        scratch_fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=releases_fd)
        try:
            for fname, data in files.items():
                _write_new(scratch_fd, fname, data, 0o444)
                created.append(fname)
            os.fchmod(scratch_fd, 0o755)
            os.fsync(scratch_fd)
        finally:
            _close([scratch_fd])
        _require(not _exists(releases_fd, version), 'existing_release_mismatch')
        os.rename(scratch, version, src_dir_fd=releases_fd, dst_dir_fd=releases_fd)
        scratch = None
        os.fsync(releases_fd)
        return 'staged'
    except BaseException:
        if scratch is not None and releases_fd >= 0:
            _remove_own_scratch(releases_fd, scratch, created)
        raise
    finally:
        _close([releases_fd, parent_fd])


def _exists(dir_fd, name):
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove_own_scratch(releases_fd, scratch, created):
    """Remove only the files this call created in its own unique scratch directory."""
    try:
        fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=releases_fd)
    except OSError:
        return
    try:
        for name in created:
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                pass
    finally:
        _close([fd])
    try:
        os.rmdir(scratch, dir_fd=releases_fd)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Donor profile (custody-gated)
# --------------------------------------------------------------------------
def _user_dir_state(path, uid):
    """Metadata-only look at a USER directory. Never creates, chowns or removes it."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'unreadable'
    if stat.S_ISDIR(info.st_mode) and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o700:
        return 'ok'
    return 'untrusted'


def _gate_node_exists(gate_directory):
    try:
        os.stat(gate_directory + '/source-gate.sock', follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # cannot prove absence: treat as present
    return True


def apply_profile(cfg, version, uid, entry_sha256, *, profile_path=PROFILE_PATH, ledger_path=LEDGER_PATH,
                  token=None):
    """Return a dict with the profile decision. Never overwrites or deletes any node."""
    profile, raw = build_profile(cfg, version, uid, entry_sha256)
    profile_p, ledger_p = Path(profile_path), Path(ledger_path)
    unknown = {'profile': 'unchanged_custody_unknown', 'custody': 'UNKNOWN', 'gating': False}

    pdir = open_root_dir(str(profile_p.parent))
    try:
        existing = read_root_file(pdir, profile_p.name, MAX_CONFIG, optional=True)
        if existing is not None:
            if existing[0] == raw:
                return {'profile': 'unchanged_same_release', 'custody': 'not_evaluated', 'gating': False}
            # A different profile may have donated rights; no positive retirement exists.
            return dict(unknown, reason='existing_profile_differs_prior_custody_not_retired')
        # Initial profile only when there is no ledger and no gate node at all.
        try:
            ldir = open_root_dir(str(ledger_p.parent))
        except StageError as exc:
            if str(exc) != 'missing_directory':
                return dict(unknown, reason='ledger_directory_untrusted')
            ldir = None
        if ldir is not None:
            try:
                if _exists(ldir, ledger_p.name):
                    return dict(unknown, reason='prior_ledger_present')
            finally:
                _close([ldir])
        if _gate_node_exists(profile['gate_directory']):
            return dict(unknown, reason='existing_gate_node_preserved')
        for directory in (profile['gate_directory'], profile['broker_directory']):
            if _user_dir_state(directory, uid) != 'ok':
                return {'profile': 'not_written', 'custody': 'not_evaluated', 'gating': False,
                        'reason': 'user_directories_unprovisioned'}
        if ldir is None:
            # The donor writes its ledger atomically in a Root-only directory that must exist.
            _close([open_root_dir(str(ledger_p.parent), create_last=0o700)])
        tmp = f'.{profile_p.name}.tmp.{os.getpid()}-{token or time.monotonic_ns()}'
        _write_new(pdir, tmp, raw, 0o600)
        try:
            os.link(tmp, profile_p.name, src_dir_fd=pdir, dst_dir_fd=pdir, follow_symlinks=False)
        except FileExistsError:
            return dict(unknown, reason='profile_appeared_concurrently')
        finally:
            try:
                os.unlink(tmp, dir_fd=pdir)
            except OSError:
                pass
        os.fsync(pdir)
        return {'profile': 'initial_written', 'custody': 'none_known', 'gating': False}
    finally:
        _close([pdir])


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def stage_current(cfg, transport, *, uid, release_root=RELEASE_ROOT, profile_path=PROFILE_PATH,
                  ledger_path=LEDGER_PATH, token=None):
    manifest_raw = transport.get('host-release.json', MAX_MANIFEST)
    manifest = parse_json(manifest_raw)
    host_wheel, natural = validate_manifest(manifest, cfg['python_bin'])
    version = manifest['host_version']
    records = manifest['artifacts']
    if not natural:
        return {'result': 'legacy_no_natural_artifacts', 'release': version, 'profile': 'not_evaluated',
                'custody': 'not_evaluated', 'gating': False}

    def fetch(name, limit):
        data = transport.get(name, limit)
        _require(len(data) == records[name]['size'] and hashlib.sha256(data).hexdigest() == records[name]['sha256'],
                 'artifact_digest_mismatch')
        return data

    archive = fetch(ARCHIVE, MAX_ENTRY)
    payloads = {name: fetch(name, MAX_PAYLOAD) for name in PAYLOADS}
    wheel = fetch(host_wheel, MAX_ARTIFACT)
    transport_wheel = fetch(TRANSPORT_WHEEL, MAX_ARTIFACT)
    validate_archive(archive, manifest=manifest, host_wheel_name=host_wheel, wheel_bytes=wheel,
                     transport_bytes=transport_wheel, payloads=payloads)
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
    selection = build_selection(version, uid, cfg['health_config_path'], digests)
    files = {'host.pyz': archive, **payloads, 'selection.json': selection}
    staged = publish_release(version, files, release_root=release_root, token=token)
    outcome = apply_profile(cfg, version, uid, hashlib.sha256(archive).hexdigest(),
                            profile_path=profile_path, ledger_path=ledger_path, token=token)
    return {'result': staged, 'release': version, **outcome}


def run(argv, *, identity=None, config_loader=load_config, transport_factory=HttpsTransport,
        passwd=pwd.getpwnam, entry_file=None, clock=time.monotonic, stream=None, **paths):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--stage-current', action='store_true', required=True)
    parser.parse_args(argv)
    out = stream if stream is not None else sys.stdout
    uid_pair = identity if identity is not None else (os.getuid(), os.geteuid())
    try:
        _require(uid_pair == (0, 0), 'root_required')  # real UID and EUID, never a caller claim
        _require((entry_file if entry_file is not None else __file__) == ENTRY_PATH, 'not_fixed_entry')
        cfg = config_loader()
        entry = passwd(cfg['target_user'])
        _require(entry.pw_uid > 0 and entry.pw_name == cfg['target_user'], 'invalid_target_user')
        deadline = clock() + OPERATION_DEADLINE_S
        result = stage_current(cfg, transport_factory(cfg['base_url'], deadline), uid=entry.pw_uid, **paths)
        out.write(json.dumps(result, sort_keys=True) + '\n')
        return 0
    except StageError as exc:
        out.write(json.dumps({'result': 'refused', 'reason': str(exc), 'custody': 'UNKNOWN', 'gating': False},
                             sort_keys=True) + '\n')
        return 1
    except (OSError, KeyError) as exc:
        out.write(json.dumps({'result': 'failed', 'reason': type(exc).__name__, 'custody': 'UNKNOWN',
                              'gating': False}, sort_keys=True) + '\n')
        return 1


def main(argv=None):
    return run(sys.argv[1:] if argv is None else argv)


if __name__ == '__main__':
    raise SystemExit(main())
