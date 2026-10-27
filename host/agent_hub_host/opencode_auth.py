"""Native v2 device-login synchronization. Credential bytes never leave this host.

Use auth export/import, never edit OpenCode's SQLite schema. A dedicated runtime
store avoids legacy key migration; unchanged source receipts preserve refreshes
performed by the runtime. All launches share that runtime store.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager

from . import private_fs

MODEL = 'opencode-go/muse-spark-1.3-contributor'
VERIFICATION_TTL_S = 60
STRIP = ('OPENCODE_API_KEY', 'OPENCODE_GO_API_KEY', 'OPENCODE_ZEN_API_KEY', 'OPENCODE_CONFIG',
         'OPENCODE_CONFIG_CONTENT', 'OPENCODE_CONFIG_DIR', 'OPENCODE_DB',
         'OPENCODE_SERVER', 'OPENCODE_SERVER_URL', 'OPENCODE_SERVER_PASSWORD',
         'AGENT_HUB_OPENCODE_AUTH')


class AuthBusy(ValueError):
    """Another local sync owns the store; this is not authentication evidence."""


@contextmanager
def _lock(path):
    deadline = time.monotonic() + 30
    while True:
        try:
            lock = private_fs.exclusive_lock(path)
            lock.__enter__()
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise AuthBusy('engine_auth_busy: device auth synchronization is in progress') from None
            time.sleep(0.1)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def validate(cfg):
    if not isinstance(cfg, dict) or set(cfg) != {'binary', 'source_data', 'source_config', 'target_data'}:
        raise ValueError('Invalid OpenCode device auth configuration')
    if any(not isinstance(v, str) or not Path(v).is_absolute() or '\0' in v for v in cfg.values()):
        raise ValueError('OpenCode device auth paths must be absolute')
    if Path(cfg['source_data']).resolve() == Path(cfg['target_data']).resolve():
        raise ValueError('OpenCode device runtime must have a separate data store')
    return cfg


def environment(cfg, *, source=False, base=None):
    validate(cfg)
    env = dict(os.environ if base is None else base)
    for key in STRIP:
        env.pop(key, None)
    data = cfg['source_data'] if source else cfg['target_data']
    env['XDG_DATA_HOME'] = data
    env['XDG_CONFIG_HOME'] = cfg['source_config'] if source else str(Path(data) / 'config')
    # Explicit DB prevents channel/ambient overrides from selecting another store.
    env['OPENCODE_DB'] = str(Path(data) / 'opencode' / 'opencode.db')
    env['GH_CONFIG_DIR'] = str(Path(cfg['source_config']) / 'gh')
    # Managed profiles change XDG_CONFIG_HOME again. Carry this non-secret
    # machine-local path contract through the launcher instead of rediscovering
    # subscriptions.json inside an isolated engine home.
    env['AGENT_HUB_OPENCODE_AUTH'] = json.dumps(cfg, sort_keys=True)
    return env


def launch_argv(cfg, argv):
    """Apply the same store to nested trusted env wrappers and native children."""
    env = environment(cfg, base={})
    clean = []
    for arg in argv:
        key, sep, _ = arg.partition('=')
        if sep and key in STRIP:
            continue
        clean.append(key + '=' + env[key] if sep and key in env else arg)
    prefix = ['/usr/bin/env']
    for key in STRIP:
        prefix += ['-u', key]
    prefix += [k + '=' + v for k, v in env.items()]
    # Transient units inherit the service manager's environment, not this
    # process's. Put the verified environment INSIDE their command boundary.
    if clean and Path(clean[0]).name == 'systemd-run' and '--' in clean:
        boundary = clean.index('--') + 1
        return clean[:boundary] + prefix + clean[boundary:]
    return prefix + clean


def _native(cfg, args, env, payload=None):
    # A native diagnostic can contain credentials. Bound it privately and never
    # attach stdout, stderr, argv or exception strings to an outward diagnostic.
    with tempfile.TemporaryFile(dir=cfg['target_data']) as output:
        proc = subprocess.Popen([cfg['binary'], *args, '--standalone'], env=env,
                                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                start_new_session=(os.name != 'nt'))
        try:
            proc.communicate(payload, timeout=45)
        except BaseException:
            if os.name != 'nt':
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            proc.wait()
            raise
        if output.tell() > 1048576:
            raise ValueError('engine_auth_expired: OpenCode device auth check failed')
        output.seek(0)
        raw = output.read(1048577)
    if proc.returncode:
        raise ValueError('engine_auth_expired: OpenCode device auth check failed')
    return json.loads(raw) if raw.strip() else None


def _source_login_revision(cfg):
    """Read-only attestation of native credential revisions, never auth writes.

    Export also writes unrelated native state, so database data_version/stat
    alone cannot associate its output with a login. Hash bounded OpenCode
    records before and after export/model verification. Values stay in memory;
    only a digest is retained. An unsupported schema simply disables reuse.
    """
    source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
    conn = None
    try:
        before = source.stat()
        conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=0.2)
        rows = conn.execute('SELECT id,active,time_created,time_updated,'
                            'CASE WHEN length(CAST(value AS BLOB))<=1048576 THEN value END '
                            'FROM credential WHERE integration_id=? ORDER BY id LIMIT 129', ('opencode',))
        digest = hashlib.sha256()
        count, size = 0, 0
        for row in rows:
            count += 1
            value = row[4]
            if count > 128 or not isinstance(value, str):
                return None
            raw = value.encode()
            size += len(raw)
            if size > 1048576:
                return None
            digest.update(json.dumps(row[:4]).encode())
            digest.update(hashlib.sha256(raw).digest())
        after = source.stat()
        if not count or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return None
        return digest.hexdigest()
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None
    finally:
        if conn is not None:
            conn.close()


def _verification_identity(cfg, *, source_only=False):
    """Bind a short positive proof to the installed binary and login store.

    Native runtime refreshes and session writes change the runtime database,
    so its incarnation (not its contents/mtime) is checked. The source uses its
    incarnation plus semantic credential revision: committed login changes in
    the WAL invalidate, while quota exports/checkpoints/session writes do not.
    Missing or ambiguous filesystem evidence disables reuse entirely.
    """
    def identity(path, *, contents=True, optional=False):
        try:
            info = Path(path).stat()
        except FileNotFoundError:
            if optional:
                return None
            raise
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Not a regular device store file')
        result = [info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)]
        if contents:
            result += [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        return result

    try:
        source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
        target = Path(cfg['target_data']) / 'opencode'
        result = {'config': cfg, 'binary': identity(cfg['binary']),
                  'source': identity(source, contents=False),
                  'login_revision': _source_login_revision(cfg)}
        if result['login_revision'] is None:
            return None
        if not source_only:
            result.update(runtime=identity(target / 'opencode.db', contents=False),
                          legacy=identity(target / 'auth.json', optional=True),
                          source_receipt=identity(Path(cfg['target_data']) / 'device-source.json'))
        return result
    except (OSError, ValueError):
        return None


def _verified_recently(cfg):
    identity = _verification_identity(cfg)
    if identity is None:
        return False
    try:
        proof = json.loads(private_fs.read_bytes(Path(cfg['target_data']) / 'device-verification.json', 8192))
        verified_at = proof.get('verified_at')
        return (type(verified_at) in (int, float)
                and 0 <= time.time() - verified_at <= VERIFICATION_TTL_S
                and proof.get('identity') == identity)
    except (OSError, ValueError, AttributeError):
        return False


def prepare(cfg, *, native=None):
    """Verify device login/model, sharing a positive proof for at most 60s.

    Timer, dispatcher and managed bootstrap processes use the same private
    proof. Quota/resource admission is independent and still runs per launch.
    Failures and expired/changed/unknown identities never reuse a proof.
    """
    cfg = dict(validate(cfg))
    native = native or _native
    target = Path(cfg['target_data'])
    try:
        with private_fs.parent(target / 'device-source.json', create=True):
            pass
        if _verified_recently(cfg):
            return {'status': 'ok', 'reason': 'device_auth_verified'}
        with _lock(target / 'device-auth.lock'):
            # A sibling may have completed verification while we waited.
            if _verified_recently(cfg):
                return {'status': 'ok', 'reason': 'device_auth_verified'}
            before = _verification_identity(cfg, source_only=True)
            rows = native(cfg, ['auth', 'export', 'opencode'], environment(cfg, source=True))
            active = [r for r in rows if isinstance(r, dict) and r.get('active') is True
                      and r.get('integrationID') == 'opencode'] if isinstance(rows, list) else []
            if len(active) != 1:
                raise ValueError()
            value = active[0].get('value', {})
            if (value.get('type') != 'oauth' or value.get('methodID') != 'device'
                    or not value.get('access') or not value.get('refresh')):
                raise ValueError()
            # Hash is local change detection, never an exported auth receipt.
            identity = {'id': active[0].get('id'), 'method': value['methodID'],
                        'refresh': value['refresh'], 'metadata': value.get('metadata')}
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            receipt = target / 'device-source.json'
            try:
                previous = json.loads(private_fs.read_bytes(receipt, 1024))
            except FileNotFoundError:
                previous = None
            env = environment(cfg)
            if previous != {'digest': digest} or not (target / 'opencode' / 'opencode.db').is_file():
                native(cfg, ['auth', 'import'], env, json.dumps(active).encode())
                private_fs.atomic_write(receipt, json.dumps({'digest': digest}).encode(), 1024)
            # Refuse a contaminated store, including a legacy auth.json migration.
            integrations = native(cfg, ['auth', 'list', '--format', 'json'], env)
            connections = [(r.get('id'), c.get('method')) for r in integrations
                           for c in r.get('connections', [])]
            if not connections or any(i != 'opencode' or m != 'oauth' for i, m in connections):
                raise ValueError()
            models = native(cfg, ['api', 'model.list'], env)
            if not any(r.get('providerID') == 'opencode-go' and r.get('id') == MODEL.split('/', 1)[1]
                       and r.get('enabled') is True for r in models.get('data', [])):
                raise ValueError()
            identity = _verification_identity(cfg)
            # Native export can write/checkpoint unrelated SQLite state. The
            # credential revision must stay stable across the ENTIRE check,
            # including export; never attest login B with an export of A.
            source_keys = ('config', 'binary', 'login_revision')
            if (identity is not None and before is not None
                    and all(before[k] == identity[k] for k in source_keys)
                    and before['source'][:4] == identity['source'][:4]):
                private_fs.atomic_write(target / 'device-verification.json',
                    json.dumps({'identity': identity, 'verified_at': time.time()}).encode(), 8192)
        return {'status': 'ok', 'reason': 'device_auth_verified'}
    except AuthBusy:
        raise
    except Exception:
        raise ValueError('engine_auth_expired: OpenCode device login unavailable; run opencode auth login opencode --method device on this host') from None
