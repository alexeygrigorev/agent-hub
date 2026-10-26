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
import subprocess
import tempfile
import time
from contextlib import contextmanager

from . import private_fs

MODEL = 'opencode-go/muse-spark-1.3-contributor'
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


def prepare(cfg, *, native=None):
    """Sync active Console device login and prove pinned model discovery, no inference."""
    validate(cfg)
    native = native or _native
    target = Path(cfg['target_data'])
    try:
        with private_fs.parent(target / 'device-source.json', create=True):
            pass
        with _lock(target / 'device-auth.lock'):
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
        return {'status': 'ok', 'reason': 'device_auth_verified'}
    except AuthBusy:
        raise
    except Exception:
        raise ValueError('engine_auth_expired: OpenCode device login unavailable; run opencode auth login opencode --method device on this host') from None
