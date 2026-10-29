"""Keep a managed home's credential files in step with Hub (intake 5814e109).

Hub owns every managed engine login and publishes it as a numbered credential
generation. The host materializes the current generation into the account's
isolated HOME at sync and again right before a launch. If the engine itself
rotated a token locally (the file no longer matches what was materialized),
the host writes it back with a compare-and-set on the generation instead of
silently keeping a private copy; Hub decides which copy is live and every host
converges on it. Only digests of the materialized content are stored locally.

Nothing here prints, logs or raises with credential content.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from . import private_fs

STATE_FILE = 'credential-state.json'
OVERLAY_KEYS = ('credential_generation', 'credential_paths', 'credential_status', 'refresh_lease')
MAX_FILE = 16000


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def install_snapshot(bundle, paths=None):
    """The bundle minus Hub's mutable credential overlay: equal snapshots need no reinstall.

    ``paths`` defaults to the bundle's own credential paths; pass the incoming
    bundle's paths when comparing against a receipt written before Hub owned
    the credential, so an upgrade does not reinstall the engine.
    """
    if not isinstance(bundle, dict):
        return bundle
    paths = set(bundle.get('credential_paths') or [] if paths is None else paths)
    snap = {k: v for k, v in bundle.items() if k not in OVERLAY_KEYS}
    if paths and isinstance(snap.get('files'), dict):
        snap['files'] = {k: v for k, v in snap['files'].items() if k not in paths}
    return snap


def managed_paths(bundle):
    paths = bundle.get('credential_paths') if isinstance(bundle, dict) else None
    if not isinstance(paths, list) or bundle.get('credential_generation') is None:
        return []
    return [p for p in paths if isinstance(p, str)]


def _read_state(dest: Path):
    try:
        state = json.loads(private_fs.read_bytes(Path(dest) / STATE_FILE, 65536))
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get('generation'), int) or not isinstance(state.get('digests'), dict):
        return None
    return state


def _write_file(target: Path, data: bytes):
    private_fs.atomic_write(target, data, MAX_FILE * 2, engine_owned=True)
    if os.name != 'nt':
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass


def _local(target: Path):
    # Engines rewrite these files themselves (possibly with their own umask);
    # read without the private-mode precondition, then re-tighten the mode.
    try:
        data = private_fs.read_bytes(target, MAX_FILE * 2, private=False)
    except FileNotFoundError:
        return None
    if os.name != 'nt':
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return data if data else None


def reconcile(label, bundle, home, dest, request, hub_files=None, hub_generation=None):
    """Materialize Hub's credential generation, writing back a local rotation first.

    ``hub_files``/``hub_generation`` override the copy carried in ``bundle``
    (used at launch with a freshly fetched credential). Returns a non-secret
    result dict; never raises with credential content.
    """
    paths = managed_paths(bundle)
    if not paths:
        return {'status': 'unmanaged'}
    home, dest = Path(home), Path(dest)
    generation = bundle['credential_generation'] if hub_generation is None else hub_generation
    files = {p: bundle['files'][p] for p in paths if isinstance(bundle.get('files', {}).get(p), str)} if hub_files is None else dict(hub_files)
    state = _read_state(dest)
    local = {p: _local(home / p) for p in paths}
    # A local file that differs from what this host last materialized was
    # rotated by the engine. Without state (first sync after upgrade) any copy
    # that differs from Hub's is offered; Hub accepts it only if provably newer.
    changed = {}
    for p, data in local.items():
        if data is None:
            continue
        known = state['digests'].get(p) if state else None
        hub_copy = files.get(p)
        if (known is not None and _digest(data) != known) or (known is None and hub_copy is not None and data != hub_copy.encode()):
            try:
                changed[p] = data.decode('utf-8')
            except UnicodeDecodeError:
                continue
    result = {'status': 'in_step', 'generation': generation}
    if changed:
        base = state['generation'] if state else -1
        try:
            response = request('POST', '/api/engines/%s/credential' % label, {'base_generation': base, 'files': changed})
        except Exception:
            response = None
        if not isinstance(response, dict) or not isinstance(response.get('files'), dict) or not isinstance(response.get('generation'), int):
            # Hub unreachable: keep the live local rotation, retry next sync.
            return {'status': 'writeback_pending', 'generation': state['generation'] if state else None}
        files = {p: v for p, v in response['files'].items() if p in paths and isinstance(v, str)}
        generation = response['generation']
        result = {'status': 'written_back' if response.get('accepted') else 'adopted_hub', 'generation': generation}
    digests = {}
    for p in paths:
        value = files.get(p)
        if value is None:
            continue
        data = value.encode('utf-8')
        if local.get(p) != data:
            _write_file(home / p, data)
            if result['status'] == 'in_step':
                result['status'] = 'materialized'
        digests[p] = _digest(data)
    if not state or state.get('generation') != generation or state.get('digests') != digests:
        private_fs.atomic_write(dest / STATE_FILE, json.dumps({'generation': generation, 'digests': digests}).encode(), 65536)
    return result


def fetch_current(label, request):
    """Launch-time fetch of Hub's current credential generation (I3)."""
    response = request('GET', '/api/engines/%s/credential' % label)
    if not isinstance(response, dict) or not isinstance(response.get('files'), dict) or not isinstance(response.get('generation'), int):
        raise ValueError('Invalid credential response')
    return response


def report_rejection(label, generation, reason, request):
    """Tell Hub this host's copy of a generation failed authentication."""
    return request('POST', '/api/engines/%s/rejection' % label, {'generation': generation, 'reason': reason})


# ---------- Direct provider quota (replaces quse for managed accounts) ----------

CODEX_USAGE = 'https://chatgpt.com/backend-api/wham/usage'
CLAUDE_USAGE = 'https://api.anthropic.com/api/oauth/usage'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _iso(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e12 else value
        return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return None


def _remaining(used):
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        return max(0.0, min(100.0, 100.0 - float(used)))
    return None


def _span(seconds, fallback):
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        return fallback
    if seconds == 18000:
        return '5h'
    if seconds == 604800:
        return '7d'
    if seconds % 86400 == 0:
        return '%dd' % (seconds // 86400)
    if seconds % 3600 == 0:
        return '%dh' % (seconds // 3600)
    return '%ds' % seconds


def codex_quota(body, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    rl = body.get('rate_limit') if isinstance(body, dict) and isinstance(body.get('rate_limit'), dict) else {}
    present = [w for w in (rl.get('primary_window'), rl.get('secondary_window')) if isinstance(w, dict)]
    windows = {'5h': {'percent_remaining': None, 'reset_at': None, 'present': False},
               '7d': {'percent_remaining': None, 'reset_at': None, 'present': False}}
    for i, w in enumerate(present):
        name = _span(w.get('limit_window_seconds'), '7d' if len(present) == 1 or i == 1 else '5h')
        windows[name] = {'percent_remaining': _remaining(w.get('used_percent')), 'reset_at': _iso(w.get('reset_at')), 'present': True}
    weekly = windows['7d']
    limit = rl.get('limit_reached') is True or (weekly['present'] and weekly['percent_remaining'] == 0)
    return {'status': 'ok', 'observed_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'details': {'limit_reached': limit}, 'windows': windows}


def claude_quota(body, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)

    def win(w):
        if not isinstance(w, dict):
            return {'percent_remaining': None, 'reset_at': None, 'present': False}
        return {'percent_remaining': _remaining(w.get('utilization') or 0), 'reset_at': _iso(w.get('resets_at')), 'present': True}
    windows = {'5h': win(body.get('five_hour')), '7d': win(body.get('seven_day'))}
    limit = any(w['present'] and w['percent_remaining'] == 0 for w in windows.values())
    return {'status': 'ok', 'observed_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'details': {'limit_reached': limit}, 'windows': windows}


def probe_quota(provider, home, opener=None, timeout=15):
    """Read quota straight from the provider with the materialized Hub credential.

    Returns a quse-shaped ``{provider: route}`` observation or ``{}``; the
    caller sanitizes it. HTTP 401 yields ``{'unauthorized': True}``.
    """
    home = Path(home)
    opener = opener or urllib.request.build_opener(_NoRedirect())
    try:
        if provider == 'codex':
            auth = json.loads(private_fs.read_bytes(home / '.codex' / 'auth.json', MAX_FILE * 2, private=False))
            tokens = auth.get('tokens') or {}
            headers = {'Authorization': 'Bearer ' + tokens['access_token'], 'Accept': 'application/json',
                       'User-Agent': 'codex_cli_rs/0.162.1', 'originator': 'codex_cli_rs'}
            if isinstance(tokens.get('account_id'), str):
                headers['ChatGPT-Account-Id'] = tokens['account_id']
            url, parse = CODEX_USAGE, codex_quota
        elif provider == 'claude':
            creds = json.loads(private_fs.read_bytes(home / '.claude' / '.credentials.json', MAX_FILE * 2, private=False))
            headers = {'Authorization': 'Bearer ' + creds['claudeAiOauth']['accessToken'], 'Accept': 'application/json',
                       'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'Claude-Code/2.1.198'}
            url, parse = CLAUDE_USAGE, claude_quota
        else:
            return {}
        with opener.open(urllib.request.Request(url, headers=headers, method='GET'), timeout=timeout) as response:
            body = json.loads(response.read(131072))
        return {provider: parse(body)} if isinstance(body, dict) else {}
    except urllib.error.HTTPError as error:
        error.close()
        return {'unauthorized': True} if error.code == 401 else {}
    except Exception:
        # Exceptions may carry provider bodies or credential paths; report nothing.
        return {}


# ---------- Leased refresh (Hub egress blocked; one host refreshes for Hub) ----------

CODEX_TOKEN = 'https://auth.openai.com/oauth/token'
CODEX_CLIENT = 'app_EMoamEEZ73f0CkXaXp7hrann'
CLAUDE_TOKEN = 'https://platform.claude.com/v1/oauth/token'
CLAUDE_CLIENT = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
CLAUDE_SCOPES = ['user:profile', 'user:inference', 'user:sessions:claude_code', 'user:mcp_servers', 'user:file_upload']


def _post_json(url, body, opener, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    with opener.open(req, timeout=timeout) as response:
        value = json.loads(response.read(65536))
    if not isinstance(value, dict) or not isinstance(value.get('access_token'), str):
        raise ValueError('invalid token response')
    return value


def refresh_files(provider, files, opener=None, now=None):
    """Refresh one credential set; returns new files or None. Never raises with content."""
    opener = opener or urllib.request.build_opener(_NoRedirect())
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        if provider == 'codex':
            auth = json.loads(files['.codex/auth.json'])
            tok = _post_json(CODEX_TOKEN, {'client_id': CODEX_CLIENT, 'grant_type': 'refresh_token',
                                           'refresh_token': auth['tokens']['refresh_token'], 'scope': 'openid profile email'}, opener)
            tokens = dict(auth['tokens'], access_token=tok['access_token'])
            for key in ('id_token', 'refresh_token'):
                if isinstance(tok.get(key), str):
                    tokens[key] = tok[key]
            auth.update(tokens=tokens, last_refresh=now.strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
            return {'.codex/auth.json': json.dumps(auth, indent=2)}
        if provider == 'claude':
            creds = json.loads(files['.claude/.credentials.json'])
            oauth = creds['claudeAiOauth']
            tok = _post_json(CLAUDE_TOKEN, {'grant_type': 'refresh_token', 'refresh_token': oauth['refreshToken'],
                                            'client_id': CLAUDE_CLIENT, 'scope': ' '.join(oauth.get('scopes') or CLAUDE_SCOPES)}, opener)
            oauth = dict(oauth, accessToken=tok['access_token'])
            if isinstance(tok.get('refresh_token'), str):
                oauth['refreshToken'] = tok['refresh_token']
            if isinstance(tok.get('expires_in'), (int, float)):
                oauth['expiresAt'] = int(now.timestamp() * 1000 + tok['expires_in'] * 1000)
            if isinstance(tok.get('scope'), str):
                oauth['scopes'] = tok['scope'].split()
            return {'.claude/.credentials.json': json.dumps(dict(creds, claudeAiOauth=oauth))}
    except Exception:
        return None
    return None


def leased_refresh(label, bundle, home, request, opener=None):
    """Perform a Hub-granted refresh lease and publish it by CAS on the generation."""
    if not isinstance(bundle, dict) or bundle.get('refresh_lease') is not True:
        return {'status': 'no_lease'}
    paths = managed_paths(bundle)
    files = {}
    for p in paths:
        data = _local(Path(home) / p)
        if data is not None:
            files[p] = data.decode('utf-8', errors='strict')
    fresh = refresh_files(bundle.get('provider'), files, opener)
    if not fresh:
        return {'status': 'refresh_failed'}
    try:
        response = request('POST', '/api/engines/%s/credential' % label,
                           {'base_generation': bundle['credential_generation'], 'files': fresh})
    except Exception:
        response = None
    # Persist locally either way: this copy now holds the only valid refresh token.
    for p, value in fresh.items():
        _write_file(Path(home) / p, value.encode('utf-8'))
    if isinstance(response, dict) and isinstance(response.get('generation'), int):
        digests = {p: _digest(v.encode('utf-8')) for p, v in (response.get('files') or fresh).items() if p in paths}
        private_fs.atomic_write(Path(home).parent / STATE_FILE, json.dumps({'generation': response['generation'], 'digests': digests}).encode(), 65536)
        return {'status': 'refreshed', 'generation': response['generation']}
    return {'status': 'writeback_pending'}
