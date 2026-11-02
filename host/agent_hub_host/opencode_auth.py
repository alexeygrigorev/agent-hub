"""Native v2 device-login synchronization. Credential bytes never leave this host.

Use auth export/import, never edit OpenCode's SQLite schema. A dedicated runtime
store avoids legacy key migration; unchanged source receipts preserve refreshes
performed by the runtime. All launches share that runtime store.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from . import private_fs

MODEL = 'opencode-go/muse-spark-1.3-contributor'
VERIFICATION_TTL_S = 60
STRIP = ('OPENCODE_API_KEY', 'OPENCODE_GO_API_KEY', 'OPENCODE_ZEN_API_KEY', 'OPENCODE_CONFIG',
         'OPENCODE_CONFIG_CONTENT', 'OPENCODE_CONFIG_DIR', 'OPENCODE_DB',
         'OPENCODE_SERVER', 'OPENCODE_SERVER_URL', 'OPENCODE_SERVER_PASSWORD',
         'AGENT_HUB_OPENCODE_AUTH',
         # A user-level proxy that cannot serve the provider catalogue (desktop:
         # HTTP(S)_PROXY=127.0.0.1:8899) made `api model.list` return only the free
         # 'opencode' provider, so a valid Hub login read as 'device login unavailable'.
         # The Hub client already ignores these (0fe413b); the OpenCode store does too.
         'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
         'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')


class AuthBusy(ValueError):
    """Another local sync owns the store; this is not authentication evidence."""


class AuthCheckUnavailable(ValueError):
    """Native verification failed without proving that credentials expired."""

    def __init__(self, step='native_check', failure='check_failed', *, exit_code=None,
                 timeout_s=None):
        self.step = step if step in {'auth_export', 'auth_import', 'auth_list', 'model_list',
                                     'native_check'} else 'native_check'
        self.failure = failure if failure in {'nonzero_exit', 'timeout', 'invalid_json',
                                             'invalid_response', 'output_too_large',
                                             'spawn_error', 'unexpected_failure',
                                             'auth_rejected', 'missing_login'} else 'check_failed'
        self.exit_code = exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None
        self.timeout_s = timeout_s if type(timeout_s) is int and 1 <= timeout_s <= 300 else None
        detail = f'engine_auth_check_unavailable: {self.step} {self.failure}'
        if self.exit_code is not None:
            detail += f' (exit {self.exit_code})'
        if self.timeout_s is not None:
            detail += f' (timeout {self.timeout_s}s)'
        super().__init__(detail)

    def diagnostic(self):
        """A fixed, secret-free event payload for host logs."""
        result = {'step': self.step, 'failure': self.failure}
        if self.exit_code is not None:
            result['exit_code'] = self.exit_code
        if self.timeout_s is not None:
            result['timeout_s'] = self.timeout_s
        return result


class AuthExpired(ValueError):
    """The source device login is absent or its refresh was explicitly rejected."""

    def __init__(self, reason='device_login_unavailable', *, step='auth_export', exit_code=None):
        self.reason = reason if reason in {'missing_device_login', 'refresh_rejected'} else 'device_login_unavailable'
        self.step = step if step in {'auth_export', 'auth_import', 'auth_list', 'model_list'} else 'auth_export'
        self.exit_code = exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None
        host = socket.gethostname() or 'unknown host'
        super().__init__(f'engine_auth_expired: OpenCode device login is unavailable; '
                         f'run opencode auth login opencode --method device on {host}')

    def diagnostic(self):
        result = {'reason': 'engine_auth_expired', 'failure': self.reason,
                  'step': self.step, 'message': str(self)}
        if self.exit_code is not None:
            result['exit_code'] = self.exit_code
        return result


class _StaleTargetStore(Exception):
    """The source login is valid, but the isolated Hub store needs a fresh import."""

    def __init__(self, step, failure='invalid_response', exit_code=None):
        self.step = step
        self.failure = failure
        self.exit_code = exit_code
        super().__init__(step)


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


_WINDOWS_ENV_WRAPPER = (
    'import json,os,subprocess,sys;'
    's=json.loads(sys.argv[1]);e=dict(os.environ);'
    '[e.pop(k,None) for k in s["unset"]];e.update(s["set"]);'
    'sys.exit(subprocess.call(sys.argv[2:],env=e))')


def launch_argv(cfg, argv):
    """Apply the same store to nested trusted env wrappers and native children."""
    env = environment(cfg, base={})
    clean = []
    for arg in argv:
        key, sep, _ = arg.partition('=')
        if sep and key in STRIP:
            continue
        clean.append(key + '=' + env[key] if sep and key in env else arg)
    if os.name == 'nt':
        # Windows has no /usr/bin/env (every opencode launch failed with
        # dispatch_local_command_failed). Same contract through a tiny wrapper.
        spec = json.dumps({'unset': sorted(STRIP), 'set': env}, sort_keys=True)
        return [sys.executable, '-c', _WINDOWS_ENV_WRAPPER, spec] + clean
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


def _native_step(args):
    if args[:2] == ['auth', 'export']:
        return 'auth_export'
    if args[:2] == ['auth', 'import']:
        return 'auth_import'
    if args[:2] == ['auth', 'list']:
        return 'auth_list'
    if args[:2] == ['api', 'model.list']:
        return 'model_list'
    return 'native_check'


_AUTH_REJECTION = re.compile(
    r'\bHTTP(?:\s+status)?\s*401\b|\bstatus(?:\s+code)?\s*[:=]?\s*401\b'
    r'|\b(?:unauthorized|invalid_grant|credential_rejected|refresh_rejected)\b'
    r'|\b403\b.{0,80}\b(?:invalid|expired|revoked|rejected|unauthorized)\b'
    r'|(?:refresh|oauth|device)\s+(?:token\s+)?(?:was\s+)?(?:expired|invalid|revoked|rejected)'
    r'|(?:invalid|expired|revoked|rejected)\s+(?:refresh\s+)?(?:token|credential)',
    re.IGNORECASE)
_MISSING_LOGIN = re.compile(
    r'(?:no|missing|could not find)\s+(?:active\s+)?(?:oauth\s+)?(?:device\s+)?(?:credentials?|login)'
    r'|not logged in|not authenticated|authentication required', re.IGNORECASE)


def _native_auth_rejection(raw):
    # Native output may contain credential material. Inspect it only in memory;
    # all outward errors below contain fixed step/failure labels only.
    text = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        safe = []
        error_keys = {'error', 'errors', 'message', 'detail', 'status', 'statuscode',
                      'code', 'reason', 'cause', 'description'}

        def collect(value, key='', error_context=False):
            if any(word in key.lower() for word in ('access', 'refresh', 'token', 'secret', 'credential', 'key')):
                return
            error_context = error_context or key.lower() in error_keys
            if isinstance(value, dict):
                for child_key, child in value.items():
                    if error_context or str(child_key).lower() in error_keys:
                        collect(child, str(child_key), error_context)
            elif isinstance(value, list):
                for child in value:
                    collect(child, key, error_context)
            elif error_context and isinstance(value, (str, int, float)):
                safe.append(str(value))

        collect(parsed)
        text = ' '.join(safe)
    return bool(_AUTH_REJECTION.search(text))


def _report_unavailable(exc):
    # The result identifies the native operation and failure class only. The
    # CLI output, exception text, argv and all credential fields stay private.
    print(json.dumps({'event': 'opencode_auth_check_unavailable',
                      'reason': 'engine_auth_check_unavailable', **exc.diagnostic()}), flush=True)


def _report_expired(exc):
    # This message includes only a fixed command and local hostname.
    print(json.dumps({'event': 'opencode_auth_expired', **exc.diagnostic()}), flush=True)


def _report_target_reimport(stale):
    # A recovered runtime-store failure is useful evidence too, but never
    # include native output or credential values in logs.
    result = {'event': 'opencode_auth_target_reimport', 'step': stale.step,
              'failure': stale.failure}
    if type(stale.exit_code) is int and -255 <= stale.exit_code <= 255:
        result['exit_code'] = stale.exit_code
    print(json.dumps(result), flush=True)


def _native(cfg, args, env, payload=None):
    # A native diagnostic can contain credentials. Bound it privately and never
    # attach stdout, stderr, argv or exception strings to an outward diagnostic.
    step = _native_step(args)
    attempts = 2 if payload is None else 1
    for attempt in range(attempts):
        with tempfile.TemporaryFile(dir=cfg['target_data']) as output, \
                tempfile.TemporaryFile(dir=cfg['target_data']) as errors:
            try:
                proc = subprocess.Popen([cfg['binary'], *args, '--standalone'], env=env,
                                        stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                        start_new_session=(os.name != 'nt'))
            except OSError:
                raise AuthCheckUnavailable(step, 'spawn_error') from None
            try:
                proc.communicate(payload, timeout=45)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                proc.wait()
                raise AuthCheckUnavailable(step, 'timeout', timeout_s=45) from None
            except OSError:
                if os.name != 'nt':
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                proc.wait()
                raise AuthCheckUnavailable(step, 'spawn_error') from None
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
                raise AuthCheckUnavailable(step, 'output_too_large')
            if errors.tell() > 1048576:
                raise AuthCheckUnavailable(step, 'output_too_large')
            output.seek(0)
            raw = output.read(1048577)
            if proc.returncode:
                errors.seek(0)
                error_raw = errors.read(1048577)
            else:
                error_raw = b''
        if not proc.returncode:
            try:
                if not raw.strip():
                    return [] if step == 'auth_export' else None
                return json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AuthCheckUnavailable(step, 'invalid_json') from None
        combined = raw + b'\n' + error_raw
        if _native_auth_rejection(combined):
            if step == 'auth_export':
                raise AuthExpired('refresh_rejected', step=step, exit_code=proc.returncode) from None
            if step in {'auth_list', 'model_list'}:
                raise _StaleTargetStore(step, 'auth_rejected', proc.returncode) from None
            raise AuthCheckUnavailable(step, 'auth_rejected', exit_code=proc.returncode) from None
        if _MISSING_LOGIN.search(combined.decode('utf-8', 'ignore')):
            if step == 'auth_export':
                raise AuthExpired('missing_device_login', step=step, exit_code=proc.returncode) from None
            if step in {'auth_list', 'model_list'}:
                raise _StaleTargetStore(step, 'missing_login', proc.returncode) from None
            raise AuthCheckUnavailable(step, 'missing_login', exit_code=proc.returncode) from None
        if attempt + 1 < attempts:
            time.sleep(0.5)
    raise AuthCheckUnavailable(step, 'nonzero_exit', exit_code=proc.returncode) from None



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
    except Exception:
        # This optional positive cache never decides auth failure. Decoder,
        # shape and numeric errors all require the ordinary native check.
        return False


def prepare(cfg, *, native=None):
    """Verify device login/model, sharing a positive proof for at most 60s.

    Timer, dispatcher and managed bootstrap processes use the same private
    proof. Quota/resource admission is independent and still runs per launch.
    Failures and expired/changed/unknown identities never reuse a proof.
    """
    native = native or _native
    try:
        cfg = dict(validate(cfg))
        target = Path(cfg['target_data'])

        def run_native(args, env, payload=None):
            step = _native_step(args)
            try:
                return native(cfg, args, env, payload)
            except (AuthBusy, AuthCheckUnavailable, _StaleTargetStore):
                raise
            except AuthExpired as exc:
                # An explicit rejection from the isolated runtime is evidence
                # that its copy is stale, not that the source device login expired.
                # Refresh/source export is the only operation that can prove expiry.
                if step == 'auth_export':
                    raise
                if step in {'auth_list', 'model_list'}:
                    failure = 'missing_login' if exc.reason == 'missing_device_login' else 'auth_rejected'
                    raise _StaleTargetStore(step, failure, exc.exit_code) from None
                raise AuthCheckUnavailable(step, 'auth_rejected', exit_code=exc.exit_code) from None
            except subprocess.TimeoutExpired:
                raise AuthCheckUnavailable(step, 'timeout', timeout_s=45) from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AuthCheckUnavailable(step, 'invalid_json') from None
            except OSError:
                raise AuthCheckUnavailable(step, 'spawn_error') from None
            except Exception as exc:
                # Compatibility adapters may still raise a native error string
                # directly. Inspect only for explicit auth rejection signals.
                if _native_auth_rejection(str(exc)):
                    if step == 'auth_export':
                        raise AuthExpired('refresh_rejected', step=step) from None
                    if step in {'auth_list', 'model_list'}:
                        raise _StaleTargetStore(step, 'auth_rejected') from None
                    raise AuthCheckUnavailable(step, 'auth_rejected') from None
                if _MISSING_LOGIN.search(str(exc)):
                    if step == 'auth_export':
                        raise AuthExpired('missing_device_login', step=step) from None
                    if step in {'auth_list', 'model_list'}:
                        raise _StaleTargetStore(step, 'missing_login') from None
                    raise AuthCheckUnavailable(step, 'missing_login') from None
                raise AuthCheckUnavailable(step, 'unexpected_failure') from None

        with private_fs.parent(target / 'device-source.json', create=True):
            pass
        if _verified_recently(cfg):
            return {'status': 'ok', 'reason': 'device_auth_verified'}
        with _lock(target / 'device-auth.lock'):
            # A sibling may have completed verification while we waited.
            if _verified_recently(cfg):
                return {'status': 'ok', 'reason': 'device_auth_verified'}
            before = _verification_identity(cfg, source_only=True)
            rows = run_native(['auth', 'export', 'opencode'], environment(cfg, source=True))
            if rows is None:
                rows = []
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise AuthCheckUnavailable('auth_export', 'invalid_response')
            active = [r for r in rows if r.get('active') is True
                      and r.get('integrationID') == 'opencode']
            if not active:
                raise AuthExpired('missing_device_login', step='auth_export')
            if len(active) != 1:
                raise AuthCheckUnavailable('auth_export', 'invalid_response')
            value = active[0].get('value', {})
            if not isinstance(value, dict):
                raise AuthCheckUnavailable('auth_export', 'invalid_response')
            if (value.get('type') != 'oauth' or value.get('methodID') != 'device'
                    or not isinstance(value.get('access'), str) or not value['access']
                    or not isinstance(value.get('refresh'), str) or not value['refresh']):
                raise AuthExpired('missing_device_login', step='auth_export')
            # Hash is local change detection, never an exported auth receipt.
            identity = {'id': active[0].get('id'), 'method': value['methodID'],
                        'refresh': value['refresh'], 'metadata': value.get('metadata')}
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            receipt = target / 'device-source.json'
            try:
                previous = json.loads(private_fs.read_bytes(receipt, 1024))
            except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
                previous = None
            env = environment(cfg)

            def import_source():
                run_native(['auth', 'import'], env, json.dumps(active).encode())
                private_fs.atomic_write(receipt, json.dumps({'digest': digest}).encode(), 1024)

            def verify_target():
                # A valid source login is authoritative. Missing, stale or
                # contaminated target state is repaired once from that source.
                integrations = run_native(['auth', 'list', '--format', 'json'], env)
                if not isinstance(integrations, list) or any(not isinstance(row, dict) for row in integrations):
                    raise AuthCheckUnavailable('auth_list', 'invalid_response')
                connections = []
                for row in integrations:
                    items = row.get('connections', [])
                    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                        raise AuthCheckUnavailable('auth_list', 'invalid_response')
                    connections.extend((row.get('id'), item.get('method')) for item in items)
                if not connections or any(i != 'opencode' or method != 'oauth'
                                          for i, method in connections):
                    raise _StaleTargetStore('auth_list')

                models = run_native(['api', 'model.list'], env)
                if (not isinstance(models, dict) or not isinstance(models.get('data'), list)
                        or any(not isinstance(row, dict) for row in models['data'])):
                    raise AuthCheckUnavailable('model_list', 'invalid_response')
                if not any(row.get('providerID') == 'opencode-go'
                           and row.get('id') == MODEL.split('/', 1)[1]
                           and row.get('enabled') is True for row in models['data']):
                    raise _StaleTargetStore('model_list')

            def repair_target_store(stale):
                _report_target_reimport(stale)
                import_source()
                try:
                    verify_target()
                except _StaleTargetStore as retry_stale:
                    if retry_stale.failure == 'auth_rejected':
                        raise AuthExpired('refresh_rejected', step=retry_stale.step,
                                           exit_code=retry_stale.exit_code) from None
                    raise AuthCheckUnavailable(retry_stale.step, retry_stale.failure,
                                               exit_code=retry_stale.exit_code) from None
                except AuthExpired as retry_expired:
                    if retry_expired.reason == 'refresh_rejected':
                        raise AuthExpired('refresh_rejected', step=retry_expired.step,
                                           exit_code=retry_expired.exit_code) from None
                    raise AuthCheckUnavailable(retry_expired.step, 'missing_login',
                                               exit_code=retry_expired.exit_code) from None

            if previous != {'digest': digest} or not (target / 'opencode' / 'opencode.db').is_file():
                import_source()
            try:
                verify_target()
            except _StaleTargetStore as stale:
                repair_target_store(stale)
            except AuthExpired as exc:
                # A target-side rejection can be a stale Hub import. Re-import
                # from the verified source before deciding that refresh failed.
                if exc.step not in {'auth_list', 'model_list'}:
                    raise
                repair_target_store(_StaleTargetStore(
                    exc.step, 'missing_login' if exc.reason == 'missing_device_login' else 'auth_rejected',
                    exc.exit_code))
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
    except AuthCheckUnavailable as exc:
        _report_unavailable(exc)
        raise
    except AuthExpired as exc:
        _report_expired(exc)
        raise
    except AuthBusy:
        raise
    except (OSError, subprocess.TimeoutExpired):
        exc = AuthCheckUnavailable('native_check', 'spawn_error')
        _report_unavailable(exc)
        raise exc from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        exc = AuthCheckUnavailable('native_check', 'invalid_json')
        _report_unavailable(exc)
        raise exc from None
    except Exception as exc:
        # Unclassified failures are never evidence that a human must log in.
        unavailable = AuthCheckUnavailable('native_check', 'unexpected_failure')
        _report_unavailable(unavailable)
        raise unavailable from None
