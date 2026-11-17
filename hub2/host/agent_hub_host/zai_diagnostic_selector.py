"""Private exact-initial-attempt diagnostic selection; no public activation field.

Same-UID ownership is the existing local trust boundary, not kernel inode CAS.
The operator must qualify the SDK/source/ABI record before preparing a descriptor.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from . import private_fs
from .zai_profile_mutator import select_profile_executable

FLAG = 'ZCODE_BRIDGE_DIAGNOSTICS'
PROFILE = 'zcodex'
_KEYS = {'schema', 'session_id', 'operation_id', 'attempt_operation_id', 'project_id',
         'intake_id', 'start_key', 'host_version', 'workspace', 'expires_at_ms',
         'config_sha256', 'original_sdk', 'diagnostic_sdk'}


def refuse():
    raise RuntimeError('zai_diagnostic_selector_refused') from None


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                refuse()
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _uuid(kind, project, intake, key):
    value = _sha(json.dumps([kind, project, intake, key], separators=(',', ':'),
                            ensure_ascii=False).encode())
    return value[:8]+'-'+value[8:12]+'-5'+value[13:16]+'-a'+value[17:20]+'-'+value[20:32]


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode,
            info.st_mtime_ns, info.st_ctime_ns)


def _elf(path, expected, check):
    if type(path) is not str or not Path(path).is_absolute():
        refuse()
    terminal = Path(path).resolve(strict=True)
    with private_fs.parent(terminal, private=False) as anchor:
        fd = private_fs._open(anchor, terminal, os.O_RDONLY, private=False)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (before.st_uid not in {0, os.getuid()} or before.st_mode & 0o022
                    or not before.st_mode & 0o111 or before.st_size != expected['bytes']
                    or not 64 <= before.st_size <= 536870912):
                refuse()
            digest = hashlib.sha256()
            header = stream.read(64)
            if (header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00'
                    or header[16:18] not in {b'\x02\x00', b'\x03\x00'}):
                refuse()
            digest.update(header)
            while True:
                check()
                chunk = stream.read(1048576)
                if not chunk:
                    break
                digest.update(chunk)
            if (digest.hexdigest() != expected['sha256']
                    or _stamp(before) != _stamp(os.fstat(stream.fileno()))
                    or _stamp(before) != _stamp(os.stat(terminal, follow_symlinks=False))
                    or Path(path).resolve(strict=True) != terminal):
                refuse()
    return {'terminal': str(terminal), 'stamp': list(_stamp(before)), 'sha256': digest.hexdigest()}


def _selected_config(env, workspace):
    selected = env.get('APLEXER_CONFIG')
    if selected:
        path = Path(selected)
        return path if path.is_absolute() else Path(workspace) / path
    if env.get('XDG_CONFIG_HOME'):
        path = Path(env['XDG_CONFIG_HOME'])
        if not path.is_absolute():
            refuse()
        return path / 'aplexer/config.toml'
    if not env.get('HOME') or not Path(env['HOME']).is_absolute():
        refuse()
    return Path(env['HOME']) / '.config/aplexer/config.toml'


def _write_new(path, raw):
    with private_fs.parent(path, create=True) as anchor:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=anchor)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(anchor)


class _Plan:
    def __init__(self, launcher, spec, descriptor, raw, context):
        self.launcher, self.spec, self.d = launcher, dict(spec), descriptor
        self.descriptor_sha256 = _sha(raw)
        self.env = context
        self.end_wall = min(descriptor['expires_at_ms'], spec['runtime_deadline']) / 1000
        self.end_mono = time.monotonic() + min(10, self.end_wall - time.time())

    def check(self):
        if time.time() >= self.end_wall or time.monotonic() >= self.end_mono:
            refuse()

    def journal(self):
        self.check()
        row = self.launcher.journal.db.execute(
            'SELECT session_id,epoch,workspace,state,native_id,native_dispatch_started,interactive '
            'FROM launches WHERE operation_id=?', (self.spec['operation_id'],)).fetchone()
        if (row is None or row['session_id'] != self.spec['session_id']
                or type(row['epoch']) is not int or row['epoch'] <= 0
                or row['epoch'] != self.spec['epoch'] or row['workspace'] != self.spec['workspace']
                or row['state'] != 'launching' or row['native_id'] is not None
                or row['native_dispatch_started'] != 0 or row['interactive'] != 0):
            refuse()
        self.check()  # SQLite may block; qualify the original budget after read.

    def preview(self, env):
        self.check()
        result = self.launcher.runner(
            [self.launcher.native_binary, 'launch-spec', '--profile', PROFILE,
             '--cwd', self.spec['workspace'], '--json'], env=env, cwd=self.spec['workspace'],
            text=True, encoding='utf-8', errors='strict', capture_output=True,
            check=False, timeout=min(3, self.end_mono-time.monotonic()))
        if result.returncode or type(result.stdout) is not str or len(result.stdout.encode()) > 262144:
            refuse()
        value = _json(result.stdout)
        if (type(value) is not dict or set(value) != {'engine','profile','argv','env_set','env_unset','cwd'}
                or value['engine'] != 'codex' or value['profile'] != PROFILE
                or value['cwd'] != self.spec['workspace'] or type(value['argv']) is not list
                or not 1 <= len(value['argv']) <= 128
                or not all(type(v) is str and len(v) <= 8192 for v in value['argv'])
                or type(value['env_set']) is not dict
                or not all(type(k) is str and type(v) is str for k,v in value['env_set'].items())
                or type(value['env_unset']) is not list
                or not all(type(v) is str for v in value['env_unset'])
                or {FLAG, 'APLEXER_CONFIG'} & set(value['env_unset'])):
            refuse()
        self.check()
        return value

    def transport(self, argv, child):
        if argv[-len(child)-1:] != ['--', *child]:
            refuse()
        entries = ['--env', 'APLEXER_CONFIG='+str(self.private_config), '--env', FLAG+'=1']
        # Outer env selects the private config on inner task reload. Explicit
        # inner task env overrides profile values, while strip-wins was checked.
        selected_child = [*child, *entries]
        return argv[:-len(child)-1] + entries + ['--', *selected_child], selected_child

    def before_dispatch(self):
        try:
            self.journal()
            if (_sha(private_fs.read_bytes(self.source_config, 262144, private=False)) != self.d['config_sha256']
                    or private_fs.read_bytes(self.private_config, 270336) != self.private_bytes
                    or _sha(private_fs.read_bytes(self.record_path, 65536)) != self.record_sha
                    or self.record_path.stat(follow_symlinks=False).st_dev != self.d['diagnostic_sdk']['abi_record']['device']
                    or self.record_path.stat(follow_symlinks=False).st_ino != self.d['diagnostic_sdk']['abi_record']['inode']):
                refuse()
            if (_elf(self.original_path, self.d['original_sdk'], self.check) != self.original_identity
                    or _elf(self.d['diagnostic_sdk']['path'], self.d['diagnostic_sdk'], self.check) != self.diagnostic_identity):
                refuse()
            self.journal()  # Re-read tuple after all potentially blocking reads.
        except (OSError, ValueError, KeyError, TypeError):
            refuse()


def prepare(launcher, spec, route, child, child_env, outer_env, *, local):
    # No descriptor lookup or environment/profile change on noneligible routes.
    if (os.name != 'posix' or not local or route.get('provider') != 'zai'
            or route.get('profile') != PROFILE or spec.get('interactive')
            or spec.get('with_goal') or launcher.journal is None):
        return None
    sid = spec.get('session_id')
    if type(sid) is not str or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', sid):
        return None
    path = launcher.journal.directory / 'zai-diagnostics' / (sid+'.json')
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    try:
        raw = private_fs.read_bytes(path, 16384)
        d = _json(raw)
        from ._release import HOST_VERSION
        ctx = spec['worker_context']
        if (type(d) is not dict or set(d) != _KEYS or d['schema'] != 'hub2-zai-diagnostic-selector-v1'
                or any(type(d[k]) is not str or not 0 < len(d[k]) <= 512
                       for k in ('session_id','operation_id','attempt_operation_id','project_id','intake_id','start_key','host_version','workspace'))
                or d['session_id'] != sid or d['project_id'] != ctx['project_id']
                or d['intake_id'] != ctx['intake_id'] or d['host_version'] != HOST_VERSION
                or not re.fullmatch('[0-9a-f]{40}', HOST_VERSION)
                or d['workspace'] != spec['workspace'] or not Path(d['workspace']).is_absolute()
                or type(d['expires_at_ms']) is not int or not _hash(d['config_sha256'])
                or type(spec['runtime_deadline']) is not int or type(spec['epoch']) is not int
                or spec['epoch'] <= 0
                or sid != _uuid('hub2-start-session-v1',d['project_id'],d['intake_id'],d['start_key'])
                or d['operation_id'] != _uuid('hub2-start-operation-v1',d['project_id'],d['intake_id'],d['start_key'])
                or d['attempt_operation_id'] != d['operation_id']+':0'
                or spec['operation_id'] != d['attempt_operation_id']):
            refuse()
        original, diagnostic = d['original_sdk'], d['diagnostic_sdk']
        if (type(original) is not dict or set(original) != {'sha256','bytes'}
                or type(diagnostic) is not dict or set(diagnostic) != {'path','sha256','bytes','source_commit','abi_record'}
                or not _hash(original['sha256']) or not _hash(diagnostic['sha256'])
                or type(original['bytes']) is not int or type(diagnostic['bytes']) is not int
                or not re.fullmatch('[0-9a-f]{40}', diagnostic['source_commit'])):
            refuse()
        record = diagnostic['abi_record']
        if (type(record) is not dict or set(record) != {'path','sha256','device','inode'}
                or not _hash(record['sha256']) or type(record['path']) is not str
                or not Path(record['path']).is_absolute()):
            refuse()
        environment = dict(outer_env)
        environment.update(child_env)
        plan = _Plan(launcher, spec, d, raw, environment)
        plan.journal()
        plan.record_path, plan.record_sha = Path(record['path']), record['sha256']
        record_bytes = private_fs.read_bytes(plan.record_path, 65536)
        info = plan.record_path.stat(follow_symlinks=False)
        if (_sha(record_bytes) != record['sha256'] or info.st_dev != record['device']
                or info.st_ino != record['inode']):
            refuse()
        plan.source_config = _selected_config(environment, spec['workspace'])
        config_bytes = private_fs.read_bytes(plan.source_config, 262144, private=False)
        if _sha(config_bytes) != d['config_sha256']:
            refuse()
        # Approved pure mutator also refuses command/wrapper profile styles.
        plan.private_bytes = select_profile_executable(config_bytes, PROFILE, diagnostic['path'])
        plan.diagnostic_identity = _elf(diagnostic['path'], diagnostic, plan.check)
        first = plan.preview(environment)
        plan.original_path = first['argv'][0]
        plan.original_identity = _elf(plan.original_path, original, plan.check)
        attempt = _sha((spec['operation_id']+':'+str(spec['epoch'])).encode())
        plan.private_config = path.parent / 'attempts' / attempt / 'config.toml'
        _write_new(plan.private_config, plan.private_bytes)
        private_env = dict(environment, APLEXER_CONFIG=str(plan.private_config))
        second = plan.preview(private_env)
        expected = dict(first, argv=[diagnostic['path'], *first['argv'][1:]])
        if second != expected:
            refuse()
        metadata = {'schema':'hub2-zai-diagnostic-selection-v1','session_id':sid,
                    'operation_id':spec['operation_id'],'epoch':spec['epoch'],
                    'host_version':HOST_VERSION,'descriptor_sha256':plan.descriptor_sha256,
                    'original_sdk_sha256':original['sha256'],'diagnostic_sdk_sha256':diagnostic['sha256'],
                    'diagnostic_source_commit':diagnostic['source_commit'],'abi_record_sha256':record['sha256'],
                    'private_config_sha256':_sha(plan.private_bytes)}
        _write_new(plan.private_config.parent / 'selection.json', json.dumps(metadata,sort_keys=True).encode())
        plan.before_dispatch()
        return plan
    except (OSError, ValueError, KeyError, TypeError, ImportError, AttributeError, subprocess.SubprocessError):
        refuse()
