"""Latency-sensitive `hub intake summary` path (intake ef429827).

Owns the lock-free summary read end to end: static XDG profile read, strict
https base parsing, raw TLS GET. Importing this module must stay cheap (only
os/sys/json/socket/stat at top level); everything else is function-local so a
fresh `hub intake summary` process pays no unrelated import cost. Any miss
returns False/None and the caller falls back to the full intake flow, which
keeps every validation and diagnostic in one place.
"""
import json
import os
import socket
import stat
import sys

PROJECT_CHARS = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-')


_cached_ssl_ctx = None
_cached_ssl_env = None
_summary_tls_pool = {}


def _ssl_context():
    global _cached_ssl_ctx, _cached_ssl_env
    import ssl
    vp = ssl.get_default_verify_paths()
    env_key = (
        os.environ.get(vp.openssl_cafile_env or 'SSL_CERT_FILE'),
        os.environ.get(vp.openssl_capath_env or 'SSL_CERT_DIR'),
    )
    if _cached_ssl_ctx is not None and _cached_ssl_env == env_key:
        return _cached_ssl_ctx
    if env_key == (None, None) and vp.capath and os.path.isdir(vp.capath):
        ctx = ssl.create_default_context(capath=vp.capath)
    else:
        ctx = ssl.create_default_context()
    _cached_ssl_ctx = ctx
    _cached_ssl_env = env_key
    return ctx


def _fast_load_posix_config():
    """Fast POSIX read of cli.json / host.json without importing pathlib/uuid/platform."""
    if os.name != 'posix' or os.environ.get('HUB_CONFIG_FILE') or os.environ.get('HUB_WORKER_CONFIG_FILE') or os.environ.get('HUB2_CONFIG'):
        return None
    if os.environ.get('APLEXER_TAG') or os.environ.get('APLEXER_WORKSPACE') or os.environ.get('APLEXER_SESSION_ID'):
        # A session-scoped worker profile would win over the static XDG files
        # in load_config(); only the static read below is valid when no
        # session profile exists. Any doubt falls back to the slow path.
        try:
            from . import worker_auth
            if worker_auth.config_path() is not None:
                return None
        except Exception:
            return None
    uid = os.getuid()
    xdg = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    folder = os.path.join(xdg, 'agent-hub')
    try:
        dst = os.lstat(folder)
        if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != uid or (dst.st_mode & 0o077):
            return None
    except OSError:
        return None
    for name in ('cli.json', 'host.json'):
        candidate = os.path.join(folder, name)
        try:
            fst = os.lstat(candidate)
            if not stat.S_ISREG(fst.st_mode) or fst.st_uid != uid or (fst.st_mode & 0o077) or fst.st_nlink != 1 or fst.st_size > 32768:
                continue
            with open(candidate, 'rb') as fp:
                data = json.loads(fp.read(32769).decode('utf-8'))
            if isinstance(data, dict) and data.get('access_token'):
                if not data.get('base_url'):
                    data['base_url'] = 'https://hub.dtcdev.click'
                return data
        except Exception:
            continue
    return None


def _parse_https_base(base):
    """Strict https base split without urllib: (host, port, prefix, host_hdr).

    Returns None for anything unusual (usernames, fragments, queries,
    whitespace, IPv6 literals, non-ASCII, bad ports) so the caller falls back
    to the full transport, which keeps the reference urllib behavior.
    """
    if not isinstance(base, str) or not base or base != base.strip():
        return None
    if base[:8].lower() != 'https://':
        return None
    rest = base[8:].rstrip('/')
    if not rest:
        return None
    for bad in (' ', '\t', '\r', '\n', '#', '@', '?', '\\'):
        if bad in rest:
            return None
    slash = rest.find('/')
    authority = rest if slash < 0 else rest[:slash]
    prefix = '' if slash < 0 else rest[slash:]
    if not authority or '[' in authority or ']' in authority or authority.count(':') > 1:
        return None
    host, sep, port_s = authority.partition(':')
    if not host or not host.isascii():
        return None
    port = 443
    if sep:
        if not port_s or not port_s.isascii() or not port_s.isdigit():
            return None
        try:
            port = int(port_s)
        except ValueError:
            return None
        if not (0 < port <= 65535):
            return None
    host = host.lower()
    host_hdr = host if port == 443 else f'{host}:{port}'
    return host, port, prefix, host_hdr


def _fast_summary_get(path):
    """Low-overhead TLS 1.3 GET for /api/intake/summary before loading urllib.request/http.client."""
    from . import _test_isolation
    if _test_isolation.under_test_runner():
        return None
    cfg = _fast_load_posix_config()
    if cfg is None:
        return None
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    parsed = _parse_https_base(base)
    if parsed is None:
        return None
    host, port, prefix, host_hdr = parsed
    token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN') or cfg.get('access_token')
    if not token:
        return None
    ctx = _ssl_context()
    req_bytes = (
        f'GET {prefix + path} HTTP/1.1\r\n'
        f'Host: {host_hdr}\r\n'
        f'Authorization: Bearer {token}\r\n'
        f'User-Agent: agent-hub-host/1\r\n'
        f'Connection: keep-alive\r\n\r\n'
    ).encode('ascii')
    key = (host, port)
    for attempt in range(2):
        tls_sock = _summary_tls_pool.pop(key, None)
        fresh = tls_sock is None
        try:
            if fresh:
                raw_sock = socket.create_connection(key, timeout=10.0)
                tls_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
            tls_sock.sendall(req_bytes)
            buf = bytearray()
            while b'\r\n\r\n' not in buf:
                chunk = tls_sock.recv(8192)
                if not chunk:
                    raise OSError('eof')
                buf.extend(chunk)
                if len(buf) > 65536:
                    tls_sock.close()
                    return None
            head_end = buf.index(b'\r\n\r\n')
            head_lines = bytes(buf[:head_end]).decode('iso-8859-1').split('\r\n')
            if not head_lines or not head_lines[0].startswith('HTTP/1.1 200'):
                tls_sock.close()
                return None
            hdrs = {}
            for line in head_lines[1:]:
                if ':' in line:
                    k, v = line.split(':', 1)
                    hdrs[k.strip().lower()] = v.strip()
            if 'application/json' not in hdrs.get('content-type', '').lower() or 'content-encoding' in hdrs:
                tls_sock.close()
                return None
            will_close = 'close' in hdrs.get('connection', '').lower()
            body = bytearray(buf[head_end + 4:])
            if hdrs.get('transfer-encoding', '').lower() == 'chunked':
                while True:
                    if body.endswith(b'0\r\n\r\n') or len(body) > 1048576:
                        break
                    chunk = tls_sock.recv(16384)
                    if not chunk:
                        break
                    body.extend(chunk)
                decoded = bytearray()
                idx = 0
                while idx < len(body):
                    crlf = body.find(b'\r\n', idx)
                    if crlf < 0:
                        tls_sock.close()
                        return None
                    size = int(bytes(body[idx:crlf]).split(b';', 1)[0], 16)
                    idx = crlf + 2
                    if size == 0:
                        break
                    if idx + size > len(body):
                        tls_sock.close()
                        return None
                    decoded.extend(body[idx:idx + size])
                    idx += size + 2
                if will_close:
                    tls_sock.close()
                else:
                    _summary_tls_pool[key] = tls_sock
                return json.loads(bytes(decoded).decode('utf-8'))
            if 'content-length' in hdrs:
                need = int(hdrs['content-length'])
                if need > 1048576:
                    tls_sock.close()
                    return None
                while len(body) < need:
                    chunk = tls_sock.recv(min(16384, need - len(body)))
                    if not chunk:
                        tls_sock.close()
                        return None
                    body.extend(chunk)
                if will_close:
                    tls_sock.close()
                else:
                    _summary_tls_pool[key] = tls_sock
                return json.loads(bytes(body[:need]).decode('utf-8'))
            tls_sock.close()
            return None
        except Exception:
            if tls_sock is not None:
                try:
                    tls_sock.close()
                except Exception:
                    pass
            if not fresh and attempt == 0:
                continue
            return None
    return None


def try_fast_summary(argv=None):
    """Print the machine-readable summary via the fast path. True when done.

    Only the bare `hub intake summary` and `--project NAME` (safe charset)
    forms are attempted; anything else returns False so the full CLI keeps
    its exact argument handling. Any transport/config miss also returns
    False and the caller runs the full flow with its diagnostics.
    """
    from . import _test_isolation
    if _test_isolation.under_test_runner():
        return False
    args = argv if argv is not None else sys.argv
    rest = args[3:]
    project = None
    if rest:
        if len(rest) == 2 and rest[0] == '--project' and rest[1] and not rest[1].startswith('-'):
            project = rest[1]
        elif len(rest) == 1 and rest[0].startswith('--project=') and rest[0][len('--project='):]:
            project = rest[0].split('=', 1)[1]
        else:
            return False
        if len(project) > 128 or any(c not in PROJECT_CHARS for c in project):
            return False
    path = '/api/intake/summary' + ('?project=' + project if project is not None else '')
    try:
        data = _fast_summary_get(path)
    except Exception:
        return False
    if data is None:
        return False
    print(json.dumps(data, ensure_ascii=True))
    return True
