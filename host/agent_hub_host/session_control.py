"""Hub session orchestration client (M12e). Prompts are data; this client never executes them.

Public HTTP surface only: talks to /api/sessions* and /api/session-commands/*
with the existing token/device credentials. Native execution belongs to the
sibling host worker; this module performs no native effects and imports no
native adapters. See docs/SESSION-CONTROL.md (proposal, pending head ACK).
"""
import argparse
import contextvars
import http.client
import json
import math
import os
import subprocess
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from . import api, auth, config

MAX_REQUEST = 32768
MAX_ACK_REQUEST = 262144
MAX_BRIEF = 8192
MAX_BRIEF_CHARS = 8000
MAX_BODY = 16384
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TIERS = ('quick', 'daily', 'deep')
PURPOSES = ('fix', 'implement', 'review', 'think')
WAIT_DEFAULT_TIMEOUT = 300.0
WAIT_MAX_TIMEOUT = 600.0
WAIT_MIN_INTERVAL = 1.0
CAPTURE_TIMEOUT = 30.0
# 2.4: `hub sessions start` waits by default, bounded at 150 s, so a start
# either reaches `running` or prints the typed failure; --no-wait opts out.
START_WAIT_TIMEOUT = 150.0
# A capture sent while the session is still offered/starting waits this long for its native
# binding (Hub target: offered -> bound under 60 s) before it is enqueued; the ack wait
# then gets its own full --timeout.
BIND_WAIT_TIMEOUT = 90.0
_request_deadline = contextvars.ContextVar('session_request_deadline', default=None)
# Terminal command states: only these confirm an outcome. `transported` alone
# is fetch/PTY bytes, never processing proof; waiting never treats it as done.
TERMINAL_COMMAND = ('acked', 'refused', 'uncertain', 'failed')
TERMINAL_SESSION = ('running', 'stopped', 'lost', 'failed', 'refused', 'uncertain', 'cancelled')

SAFE_ERROR = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
HOST_START_DETAIL_LIMIT = 240
_UNSAFE_DETAIL_CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]')
_CREDENTIAL_ASSIGNMENT = re.compile(
    r'\b(?:[A-Za-z0-9_-]*(?:authorization|proxy-authorization|www-authenticate|cookie|token|api[_-]?key|'
    r'password|passwd|secret|credential)[A-Za-z0-9_-]*)\b["\'`]?\s*[:=]', re.IGNORECASE)
_INLINE_CREDENTIAL = re.compile(r'\b(?:Bearer|Basic)\s+\S', re.IGNORECASE)
_URL_CREDENTIAL = re.compile(r'https?://[^\s/@:]+:[^\s/@]+@', re.IGNORECASE)
_TOKEN_CREDENTIAL = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16})\b')
# Error bodies are small; refuse unexpectedly large responses before parsing.
MAX_ERROR_RESPONSE = 64 * 1024


def sanitize_start_detail(value):
    """Return bounded human detail, rejecting controls and credential-shaped content."""
    if not isinstance(value, str) or not value.strip():
        return None
    detail = value[:2048]
    # Reject before normalizing: stripping a NUL from a broken-up credential
    # header can expose the value that followed it.
    if (_UNSAFE_DETAIL_CONTROL.search(detail)
            or _CREDENTIAL_ASSIGNMENT.search(detail)
            or _INLINE_CREDENTIAL.search(detail)
            or _URL_CREDENTIAL.search(detail)
            or _TOKEN_CREDENTIAL.search(detail)):
        return None
    if (re.search(r'\b(?:traceback|stack trace|raw stderr|stderr|stdout|request body|response body|environment dump)\b',
                  detail, re.IGNORECASE)
            or re.search(r'[{}\[\]]', detail)
            or re.search(r'\b[A-Za-z_][A-Za-z0-9_]{1,}\s*=\s*\S', detail)):
        return None
    detail = ' '.join(detail.split())[:HOST_START_DETAIL_LIMIT].strip()
    return detail or None

# Hub1-only route families the Hub2 worker never serves (verified against
# hub2/src/worker.ts routing). Requests fail here with a typed slug instead of
# a remote 404/traceback. Unknown paths stay allowed so new server routes and
# coordinated patches never false-block; HUB_ALLOW_LEGACY_ROUTES=1 bypasses
# the gate for Hub1-only test environments.
HUB2_UNSUPPORTED_PREFIXES = (
    '/api/bus/', '/api/device/', '/api/ops/', '/api/webhooks', '/api/prompts',
    '/api/session-schedules', '/api/driver', '/api/conversations',
    '/api/fleet/tasks', '/api/fleet/plan', '/api/fleet/launches', '/api/fleet/probe',
    '/api/fleet/reservations', '/api/fleet/accounts', '/api/fleet/limits',
    '/api/fleet/resources', '/api/fleet/routing/host-goal',
    '/api/fleet/routing/policy/grok', '/api/fleet/machines/',
)
HUB2_UNSUPPORTED_HINTS = {
    '/api/bus/': 'messaging moved to native mailboxes; `hub sessions send` covers bound sessions',
    '/api/webhooks': 'webhooks retired with Hub1',
    '/api/prompts': 'prompt library retired with Hub1',
}


def ensure_hub2_supported(method, path):
    """Raise a typed client-side refusal for Hub1-only routes (5.2c)."""
    if os.environ.get('HUB_ALLOW_LEGACY_ROUTES') == '1':
        return
    clean = str(path).split('?', 1)[0].rstrip('/')
    for prefix in HUB2_UNSUPPORTED_PREFIXES:
        base = prefix.rstrip('/')
        # A prefix written with a trailing slash (e.g. '/api/fleet/machines/')
        # refuses only its subpaths; the bare route itself stays allowed.
        if prefix.endswith('/'):
            hit = clean.startswith(base + '/')
        else:
            hit = clean == base or clean.startswith(base + '/')
        if hit:
            hint = HUB2_UNSUPPORTED_HINTS.get(prefix, 'this route exists only on the retired Hub1 server')
            raise SessionInputError(
                f'not_supported_on_hub2; {clean} is not served by Hub2 ({hint})'
            )


class SessionHTTPError(ValueError):
    def __init__(self, status, code='request_failed', message=None, pending=False, state=None):
        self.status = status
        self.code = code
        # Hub answers a capture/send on a session that has no native binding yet
        # with pending=true (intake e1ed7abe): early, retryable, not a failure.
        self.pending = pending
        self.state = state
        super().__init__(message or f'Session HTTP request failed with status {status} ({code})')


class SessionTransportError(ValueError):
    pass


class SessionInputError(ValueError):
    pass


def diagnostic(exc):
    """Never interpolate exception bodies: they may contain credentials or payloads."""
    from .worker_auth import WorkerContextError
    if isinstance(exc, WorkerContextError):
        return str(exc)
    if isinstance(exc, (SessionHTTPError, SessionTransportError, SessionInputError, config.ConfigPermissionError)):
        return str(exc)
    if isinstance(exc, UnicodeDecodeError):
        return 'Input is not valid UTF-8'
    if isinstance(exc, json.JSONDecodeError):
        return 'Invalid JSON input: expected a JSON object'
    if isinstance(exc, (TimeoutError, socket.timeout)) or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)):
        return 'Session request timed out (HTTP timeout: 10 seconds)'
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, socket.gaierror):
            return 'Session transport failed: DNS lookup failed'
        return 'Session transport failed: connection or TLS failure'
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return 'Session transport failed: connection interrupted'
    if isinstance(exc, RuntimeError) and str(exc) in ('Expected JSON response', 'Response too large'):
        return 'Session response error: ' + str(exc)
    if isinstance(exc, ValueError):
        msg = str(exc).strip()
        if msg:
            return msg
    return f'Session operation failed ({type(exc).__name__}); check authentication, session permissions and current versions.'


def hub_unavailable(exc):
    """Opaque Hub-side failure or lost response: a keyed replay is safe."""
    return isinstance(exc, SessionTransportError) or (
        isinstance(exc, SessionHTTPError) and exc.status in api.HUB_UNAVAILABLE_STATUS
        and exc.code in ('request_failed', 'internal_error'))


def replay_delays():
    """Replay delays that fit inside an active request deadline, if any."""
    deadline = _request_deadline.get()
    if deadline is None:
        return api.KEYED_REPLAY_DELAYS
    delays, remaining = [], deadline - time.monotonic() - 1
    for delay in api.KEYED_REPLAY_DELAYS:
        if delay > remaining:
            break
        delays.append(delay)
        remaining -= delay
    return tuple(delays)


def replay_notice(exc, delay):
    sys.stderr.write(f'Hub unavailable ({diagnostic(exc).split(";")[0]}); replaying the same key in {delay}s\n')


def http_error(exc, key=None):
    # Surface the server error code and useful bounded details.
    # Best-effort: an unreadable body keeps the status-only form.
    name = ''
    code = 'request_failed'
    field = None
    detail = None
    reason = None
    stage = None
    pending = False
    pending_state = None
    provider_guidance = ''
    cap_guidance = ''
    try:
        raw = exc.read(MAX_ERROR_RESPONSE + 1) if hasattr(exc, 'read') else b''
        if len(raw) > MAX_ERROR_RESPONSE:
            code = reason = 'response_too_large'
            name = ' (response_too_large)'
            data = {}
        else:
            data = json.loads((raw or b'').decode('utf-8', 'replace'))
        err = data.get('error') if isinstance(data, dict) else None
        if isinstance(err, str) and err and SAFE_ERROR.match(err):
            code = err
            name = ' (%s)' % err
            if err in ('unknown_workspace', 'unknown_repo') and isinstance(data.get('matches'), list):
                if err == 'unknown_repo':
                    pattern = r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}'
                    label = 'registered repositories'
                    requested = data.get('repo')
                    if isinstance(requested, str) and re.fullmatch(pattern, requested):
                        name += '; unknown repository: ' + requested
                else:
                    pattern = r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}'
                    label = 'close matches'
                matches = [m for m in data['matches'][:5] if isinstance(m, str) and re.fullmatch(pattern, m)]
                if matches:
                    name += '; ' + label + ': ' + ', '.join(matches)
            if err == 'ambiguous_session_prefix' and isinstance(data.get('matches'), list):
                ids = [row.get('id') for row in data['matches'] if isinstance(row, dict)
                       and isinstance(row.get('id'), str) and re.fullmatch(r'[a-fA-F0-9-]{36}', row['id'])]
                if ids:
                    name += ' matches: ' + ', '.join(ids[:100])
            if err == 'provider_disabled':
                provider_guidance = ' requested engine/model is disabled by policy; choose an allowed model or ask the policy owner to change the model pool'
        if isinstance(data, dict):
            f = data.get('field')
            if isinstance(f, str) and SAFE_ERROR.match(f):
                field = f
            # Human-readable server detail/reason print bounded and
            # control-char-free instead of being swallowed by the slug
            # filter (2.4); slugs stay exact.
            d = data.get('detail')
            if isinstance(d, str) and d.strip():
                detail = ' '.join(re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', d).split())[:300]
            r = data.get('reason')
            if isinstance(r, str) and r.strip():
                reason = ' '.join(re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', r).split())[:300]
            st = data.get('stage')
            if isinstance(st, str) and SAFE_ERROR.match(st):
                stage = st
            if code == 'session_not_bound' and data.get('pending') is True:
                pending = True
                st = data.get('state')
                if isinstance(st, str) and SAFE_ERROR.match(st):
                    pending_state = st
            if code == 'active_session_limit':
                active, limit = data.get('active_count'), data.get('limit')
                if type(active) is int and type(limit) is int and 0 <= active <= 10000 and 1 <= limit <= 10000:
                    cap_guidance = f'; {active} live active sessions of {limit}; stop an active session and retry'
    except Exception:
        name = ''

    parts = [f'Session HTTP request failed with status {exc.code}{name}']
    if provider_guidance:
        parts.append(provider_guidance)
    if cap_guidance:
        parts.append(cap_guidance)
    if pending:
        parts.append(f'; session is {pending_state or "starting"} and not yet bound; retry shortly')
    if reason:
        parts.append(f'; reason: {reason}')
    if stage:
        parts.append(f'; stage: {stage}')
    if field:
        parts.append(f'; field: {field}')
    if detail:
        parts.append(f'; detail: {detail}')
    full_message = ''.join(parts)
    if key and f'reconcile with --key {key}' not in full_message:
        full_message += f' (reconcile with --key {key} via show/commands)'
    return SessionHTTPError(exc.code, code=code, message=full_message, pending=pending, state=pending_state)


def _repo_from_workspace(workspace):
    """owner/name from the workspace's git origin, or None (Hub2 picks the project from it)."""
    try:
        done = subprocess.run(['git', '-C', os.path.expanduser(workspace), 'remote', 'get-url', 'origin'],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'github\.com[:/]([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$', done.stdout.strip()) if done.returncode == 0 else None
    return match.group(1) if match else None


def request(method, path, payload=None):
    ensure_hub2_supported(method, path)
    from .worker_auth import validate_v2_request
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    max_req = MAX_ACK_REQUEST if (method == 'POST' and path.startswith('/api/session-commands/') and path.endswith('/ack')) else MAX_REQUEST
    if data is not None and len(data) > max_req:
        raise SessionInputError('Session request exceeds capacity')
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or (cfg['access_token'] if cfg.get('_hub2_worker') else auth.get_access_token())
    payload_key = payload.get('key') if isinstance(payload, dict) else None

    def send(access):
        validate_v2_request(cfg)
        req = urllib.request.Request(base.rstrip('/') + path, data=data, method=method,
                                     headers={'Authorization': 'Bearer ' + access, 'Content-Type': 'application/json'})
        try:
            deadline = _request_deadline.get()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionTransportError('capture_not_acked')
                return api.safe_http_json(req, timeout=min(10.0, remaining))
            return api.safe_http_json(req)
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException, ValueError, RuntimeError) as exc:
            reason = ('Session response error: invalid JSON or UTF-8 response'
                      if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) else diagnostic(exc))
            recovery = '; mutation outcome unknown; inspect before retrying with the same key and payload' if method != 'GET' else ''
            raise SessionTransportError(reason + recovery) from None

    try:
        return send(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not cfg.get('_hub2_worker') and not env_token and _request_deadline.get() is None:
            try:
                auth.refresh_token(expected_token=token) if cfg.get('worker_launch_id') else auth.refresh_token()
                return send(auth.get_access_token())
            except urllib.error.HTTPError as retry:
                raise http_error(retry, key=payload_key) from None
            except Exception as refresh_exc:
                raise SessionTransportError(diagnostic(refresh_exc)) from None
        raise http_error(exc, key=payload_key) from None


def fetch_fleet_snapshot(limit=500, max_pages=20):
    """GET /api/fleet with server-side sessions pagination walked client-side.

    Intake 94186660: sessions are keyset-paged (small default page); walk all
    pages so `hub fleet` keeps full output. Older servers ignore the params
    and return one full page without a cursor (the loop ends after one fetch).
    """
    from urllib.parse import quote as _quote
    result = request('GET', '/api/fleet?sessions_limit=%d' % max(1, min(500, limit)))
    if not isinstance(result, dict):
        return result
    merged = list(result.get('sessions') or [])
    cursor = result.get('sessions_cursor_next')
    seen = set()
    pages = 1
    while cursor and pages < max_pages and cursor not in seen:
        seen.add(cursor)
        nxt = request('GET', '/api/fleet?sessions_limit=%d&sessions_cursor=%s'
                      % (max(1, min(500, limit)), _quote(str(cursor), safe='')))
        if not isinstance(nxt, dict):
            break
        merged.extend(nxt.get('sessions') or [])
        cursor = nxt.get('sessions_cursor_next')
        pages += 1
    result['sessions'] = merged
    result.pop('sessions_cursor_next', None)
    return result


def read_file(path, limit=MAX_BODY, max_chars=None, field='Input'):
    try:
        if path == '-':
            raw = sys.stdin.buffer.read(limit + 1)
        else:
            with Path(path).open('rb') as stream:
                raw = stream.read(limit + 1)
    except OSError as exc:
        reason = {2: 'file not found', 13: 'permission denied', 21: 'path is a directory'}.get(exc.errno, 'file is unreadable')
        raise SessionInputError(f'{field}: {reason}') from None
    if len(raw) > limit:
        raise SessionInputError(f'{field} exceeds capacity (max {limit} bytes)')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise SessionInputError(f'{field} is not valid UTF-8') from None
    if max_chars is not None and len(text) > max_chars:
        raise SessionInputError(f'{field} exceeds character limit (max {max_chars} characters)')
    return text


def read_prompt_text(text, limit=MAX_BRIEF, max_chars=None, field='Prompt'):
    """Inline `--prompt` with the same boundaries as the corresponding file input.

    UTF-8, at most limit bytes (and max_chars chars when set), non-blank.
    Prompts are data: never executed locally, only forwarded as payload the
    server already handles. No shell is ever involved.
    """
    if not isinstance(text, str) or not text.strip():
        raise SessionInputError(f'{field} must be non-empty text')
    if max_chars is None and limit == MAX_BRIEF:
        max_chars = MAX_BRIEF_CHARS
    encoded = text.encode('utf-8')
    if len(encoded) > limit:
        raise SessionInputError(f'{field} exceeds capacity (max {limit} bytes)')
    if max_chars is not None and len(text) > max_chars:
        raise SessionInputError(f'{field} exceeds character limit (max {max_chars} characters)')
    return text


def check_session_name(name):
    """Friendly session name: stored with the start intent, shown in
    list/show, never identity or authority. Exact-match alias only."""
    if not isinstance(name, str) or not SESSION_NAME_RE.fullmatch(name):
        raise ValueError('Session name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}')
    return name


def add_wait(parser, *, timeout=WAIT_DEFAULT_TIMEOUT, interval=5.0):
    parser.add_argument('--wait', action='store_true',
                        help='Poll until a terminal state (bounded); never claims transport as delivery')
    parser.add_argument('--timeout', type=float, default=timeout,
                        help='Max seconds to wait (1..600, default %g)' % timeout)
    parser.add_argument('--interval', type=float, default=interval,
                        help='Seconds between polls (min 1)')


def check_wait_args(args):
    if getattr(args, 'wait', False):
        timeout = float(getattr(args, 'timeout', WAIT_DEFAULT_TIMEOUT))
        interval = float(getattr(args, 'interval', 5.0))
        if not 1 <= timeout <= WAIT_MAX_TIMEOUT:
            raise ValueError('Timeout must be between 1 and 600 seconds')
        if not math.isfinite(interval) or interval < WAIT_MIN_INTERVAL:
            raise ValueError('Interval must be at least 1 second')
        return timeout, interval
    return None, None


def wait_until(deadline, interval, fetch, done, label):
    while True:
        if _request_deadline.get() is not None and time.monotonic() >= deadline:
            raise ValueError('%s timed out; re-check status with the same key, do not blind-retry' % label)
        result = fetch()
        if done(result):
            return result
        if time.monotonic() >= deadline:
            raise ValueError('%s timed out; re-check status with the same key, do not blind-retry' % label)
        time.sleep(min(interval, max(0, deadline - time.monotonic()))
                   if _request_deadline.get() is not None else interval)


def _native_command_path(reference):
    if not isinstance(reference, str) or not reference.startswith('native:'):
        return None
    match = re.fullmatch(r'native:([A-Za-z0-9_.-]{1,80}):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', reference)
    if not match:
        raise SessionInputError('invalid_native_target; use the exact machine and UUID from sessions list')
    return '/api/machines/' + match[1] + '/native/' + match[2] + '/commands'


def _fetch_command_entry(session_id, command_id):
    """Fetch ONE command entry (bounded cursor walk, no polling).

    Raises ValueError when the command is missing from the server listing;
    callers reconcile via commands listing instead of blind-retrying.
    """
    native = _native_command_path(session_id)
    if native:
        result = request('GET', native + '/' + command_id)
        if not isinstance(result.get('command'), dict) or result['command'].get('id') != command_id:
            raise SessionInputError('invalid_native_command_receipt')
        return result['command']
    from urllib.parse import quote
    cursor = None
    for _ in range(10):
        path = '/api/sessions/' + session_id + '/commands?limit=100&command_id=' + quote(command_id, safe='')
        if cursor:
            path += '&cursor=' + quote(cursor, safe='')
        listing = request('GET', path)
        for entry in listing.get('commands', []):
            if entry.get('id') == command_id:
                return entry
        cursor = listing.get('next_cursor')
        if not cursor:
            break
    raise ValueError('Command missing from server listing; reconcile before retrying')


def _observe_cleanup_trees(args, applied):
    """Observe the OWNING-HOST tree outcomes for an --apply result (read-only).

    The CLI never executes Git: after the server enqueues owned stops, this
    polls each enqueued stop command's durable entry (existing commands
    listing, bounded cursor walk) and reports its stop state plus the host's
    `ack.tree` outcome (`removed`/`kept`/`uncertain` with the actual reason,
    or null while the host has not reported one). With `--wait`, polls until
    every enqueued command is terminal (shared deadline); without it, reports
    a single pass honestly (pending stays pending). Timeouts and missing
    entries are reported as data with reconcile notes — never exceptions,
    never pretend success, and never a blind retry of any effect.
    """
    enqueued = applied.get('enqueued', []) if isinstance(applied, dict) else []
    timeout, interval = check_wait_args(args)
    deadline = (time.monotonic() + timeout) if timeout is not None else None
    results: list = []
    pending = [e for e in enqueued if isinstance(e, dict) and e.get('session_id') and e.get('command_id')]
    first_pass = True
    while first_pass or (deadline is not None and pending and time.monotonic() < deadline):
        first_pass = False
        for entry in list(pending):
            try:
                current = _fetch_command_entry(entry['session_id'], entry['command_id'])
            except ValueError:
                continue  # not yet visible; retry within the deadline, else reported pending below
            state = current.get('state')
            if state in TERMINAL_COMMAND or deadline is None:
                tree = (current.get('ack') or {}).get('tree') if isinstance(current.get('ack'), dict) else None
                results.append({'session_key': entry.get('session_key'),
                                'session_id': entry.get('session_id'),
                                'command_id': entry.get('command_id'),
                                'stop_state': state,
                                'tree': tree,
                                'note': ('host has not reported a tree outcome yet; reconcile via show/commands'
                                         if tree is None else None)})
                pending.remove(entry)
        if deadline is None or not pending:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    for entry in pending:
        try:
            current = _fetch_command_entry(entry['session_id'], entry['command_id'])
            state = current.get('state')
            tree = (current.get('ack') or {}).get('tree') if isinstance(current.get('ack'), dict) else None
        except ValueError:
            state, tree = 'unknown', None
        results.append({'session_key': entry.get('session_key'),
                        'session_id': entry.get('session_id'),
                        'command_id': entry.get('command_id'),
                        'stop_state': state,
                        'tree': tree,
                        'note': ('observation deadline reached; re-check status with the same key, do not blind-retry'
                                 if timeout is not None else
                                 'not yet reported; reconcile via show/commands (add --wait to block)')})
    complete = not pending
    return {**applied, 'remote_trees': results, 'tree_observed': complete}


def wait_for_session(session_id, timeout, interval, *, stopping=False):
    def session_state(view):
        session = view.get('session') if isinstance(view, dict) else None
        return (session if isinstance(session, dict) else view).get('state') if isinstance(view, dict) else None
    deadline = time.monotonic() + timeout
    return wait_until(deadline, interval,
                     lambda: request('GET', '/api/sessions/' + session_id),
                     lambda view: session_state(view) in (('stopped', 'lost', 'failed', 'refused', 'uncertain') if stopping else TERMINAL_SESSION),
                     'Session wait')


def wait_for_command(session_id, command_id, timeout, interval):
    deadline = time.monotonic() + timeout
    if _request_deadline.get() is not None:
        deadline = min(deadline, _request_deadline.get())
    last_status = [None]
    def _fetch():
        entry = _fetch_command_entry(session_id, command_id)
        state = entry.get('state')
        reason = entry.get('reason') or entry.get('host_reason') or (entry.get('ack') or {}).get('reason')
        if state == 'waiting in inbox for recipient':
            status_str = f"waiting in inbox for recipient: {reason}" if reason else "waiting in inbox for recipient"
            if last_status[0] != status_str:
                last_status[0] = status_str
                import sys
                sys.stderr.write(f"Status: {status_str}\n")
        return entry

    return wait_until(deadline, interval,
                     _fetch,
                     lambda entry: entry.get('state') in TERMINAL_COMMAND,
                     'Command wait')


PING_PROMPT_LIMIT = 8000


def add_ping_arguments(node, name, required_id):
    if required_id:
        node.add_argument('id', help='Session id or friendly name')
    else:
        node.add_argument('id', nargs='?', default=None, help='Session id or name (default: this session from HUB_SESSION_ID or aplexer whoami)')
    if name in ('create', 'set'):
        node.add_argument('--every', required=True, help='Interval, e.g. 15m, 1h (min 1m, max 7d)')
        src = node.add_mutually_exclusive_group(required=True)
        src.add_argument('--prompt', default=None, help='Message delivered each tick (max 8 KiB)')
        src.add_argument('--body-file', default=None, help='UTF-8 prompt file, or - for stdin')
    if name in ('disable', 'clear'):
        node.add_argument('--reason', default=None, help='Why the ping is cleared (recorded)')
    node.add_argument('--json', action='store_true')


def current_session_ref():
    for key in ('HUB_SESSION_ID', 'AGENT_HUB_SESSION_ID'):
        value = os.environ.get(key, '').strip()
        if value:
            return value
    try:
        import subprocess as _sp
        out = _sp.run(['aplexer', 'whoami', '--json'], capture_output=True, text=True, timeout=15, check=True).stdout
        value = json.loads(out).get('id')
        if isinstance(value, str) and value:
            return value
    except Exception:
        pass
    raise SessionInputError('Cannot determine this session; pass the session id or set HUB_SESSION_ID')


def execute_ping(args):
    import re as _re
    ref = getattr(args, 'id', None) or current_session_ref()
    if not isinstance(ref, str) or not _re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}', ref):
        raise ValueError('Invalid session id or name')
    action = args.ping_cmd
    base = '/api/sessions/' + ref
    if action in ('create', 'set'):
        if getattr(args, 'prompt', None) is not None:
            text = read_prompt_text(args.prompt, limit=MAX_BRIEF, max_chars=MAX_BRIEF_CHARS, field='Prompt')
        else:
            text = read_file(args.body_file, limit=MAX_BRIEF, field='Body file')
        if not text or not text.strip() or len(text) > PING_PROMPT_LIMIT:
            raise SessionInputError('Prompt must be 1-8000 characters')
        seconds = duration_seconds(args.every)
        if seconds < 60 or seconds > 7 * 86400:
            raise SessionInputError('--every must be between 1m and 7d')
        return request('POST', base + '/ping', {'every_seconds': seconds, 'prompt': text})
    if action in ('disable', 'clear'):
        payload = {'reason': args.reason} if getattr(args, 'reason', None) else {}
        return request('POST', base + '/ping-clear', payload)
    return request('GET', base + '/pings')


def duration_seconds(value):
    match = re.fullmatch(r'([1-9][0-9]*)([smhd])', str(value))
    if not match:
        raise SessionInputError('Duration must be a positive integer followed by s, m, h or d')
    seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
    if seconds > 31536000:
        raise SessionInputError('Duration must not exceed 365d')
    return seconds


def threshold_path(args, path):
    value = getattr(args, 'threshold', None) or os.environ.get('HUB_SESSION_WARNING_THRESHOLD')
    if value:
        path += ('&' if '?' in path else '?') + 'threshold=' + str(duration_seconds(value))
    return path


def add_commands(sub):
    root = sub.add_parser('sessions', help='Hub session orchestration: grouped lifecycle listing by default; list/start/send/capture/stop/reap/cleanup/observe')
    commands = root.add_subparsers(dest='sessions_cmd', required=False)
    listing = commands.add_parser('list', help='List sessions visible to this credential')
    listing.add_argument('--threshold', help='Long-running threshold (default 3h; HUB_SESSION_WARNING_THRESHOLD)')
    listing.add_argument('--state')
    listing.add_argument('--limit', type=int, help='Maximum total sessions to return (default: all pages)')
    listing.add_argument('--all', action='store_true', help='Include historical finished sessions')
    listing.add_argument('--json', action='store_true', help='Output machine-readable JSON')
    listing.add_argument('--tree', action='store_true', help='Display session lineage tree')
    count = commands.add_parser('count', help='Count live sessions (running/queued/stalled) grouped by host, engine and/or account')
    count.add_argument('--by', default='host,engine,account',
                       help='Comma-separated grouping: any of host,engine,account (default host,engine,account)')
    count.add_argument('--json', action='store_true', help='Output machine-readable JSON')
    pick = commands.add_parser('pick', help='Dry run: show the engine/model/effort the Hub picker would choose now (starts nothing)')
    pick.add_argument('--tier', choices=TIERS, help='deep/complex tasks bias toward opus/sol')
    pick.add_argument('--json', action='store_true', help='Output machine-readable JSON')
    reap = commands.add_parser('reap', help='Find and stop inactive Hub sessions across hosts')
    reap.add_argument('--dry-run', action='store_true', help='Show inactive sessions without stopping or marking them stopped')
    reap.add_argument('--json', action='store_true', help='Output machine-readable JSON')
    first_try = commands.add_parser('first-attempt-report', help='First-attempt outcome ledger: per-cause counts and examples for one Berlin-day window (intake 95fd7f1a)')
    first_try.add_argument('--day', required=True, help='Berlin calendar day YYYY-MM-DD')
    first_try.add_argument('--examples', type=int, default=3, help='Example sessions per cause (0..10, default 3)')
    first_try.add_argument('--input-file', default=None,
                           help='Offline session snapshot JSON file (object with sessions list, or bare list); omit to page the live /api/sessions listing')
    tree = commands.add_parser('tree', help='Display session lineage tree')
    tree.add_argument('--json', action='store_true', help='Output machine-readable JSON')
    link = commands.add_parser('link', help='Link a session to an intake (operator or same-intake/machine/workspace worker credential; works while it runs and after it ended; repeats are idempotent, a different existing linkage is refused)')
    link.add_argument('session')
    link.add_argument('--intake-task-id', required=True)
    link.add_argument('--key')
    start = commands.add_parser('start', help='Start a model-routed session; waits (bounded 150s) until running or a typed failure; --no-wait returns the receipt only')
    start.add_argument('--engine', help='Engine override (operators only)')
    start.add_argument('--model', help='Explicit model label, e.g. fable or gpt-6.1-sol (operators only)')
    start.add_argument('--purpose', choices=PURPOSES, default='implement',
                       help='Start purpose for model policy: fix|implement|review|think (default implement; astra is drawn only for review/think)')
    start.add_argument('--reason', default=None,
                       help='Optional explanation for an explicit deep-tier, model or engine choice')
    start.add_argument('--machine')
    start.add_argument('--needs', action='append', default=None,
                       help='Capability hints passed with the launch request (repeatable)')
    start.add_argument('--workspace', default=None, help='Configured workspace label or absolute path on the target host (omit with --repo)')
    start.add_argument('--repo', default=None, help='Repository owner/name; selects that project and the same-named workspace under the placed host’s enrolled roots')
    start.add_argument('--force', action='store_true', help='Operator only: start despite unresolved intake dependencies')
    start.add_argument('--task-kind', choices=['implement','review','ask','goal'], help='Task kind for measured resource placement and session statistics')
    start.add_argument('--tier', choices=TIERS, help='deep/daily/quick task tier')
    start.add_argument('--effort', choices=['low', 'medium', 'high', 'max'], help='Per-session model reasoning effort')
    start.add_argument('--cwd')
    start.add_argument('--intake-worktree', dest='intake_worktree', choices=['detached', 'successor'], default=None,
                       help='detached: keep the intake link but never resume, create or lock its worktree (79458f2d). '
                            'successor: replacement of a live predecessor; Hub plans a fresh sibling worktree and a named --cwd is refused (afab8367)')
    start.add_argument('--name', default=None,
                       help='Friendly session name (stored, shown in list/show; exact-match alias, never identity)')
    start.add_argument('--parent', default=None,
                       help='Parent session id recording lineage')
    start.add_argument('--intake-task-id', dest='intake_task_id', default=None,
                       help='Linked intake task id (API field intake_task_id); refused when that task has empty acceptance criteria.')
    start.add_argument('--with-goal', action='store_true', help='Start interactive /goal work on a goal-capable engine (Codex-family or Claude) (for sessions expecting follow-ups); requires --prompt or --brief-file; refuses --headless')
    start.add_argument('--headless', action='store_true', help='Explicitly start in one-shot headless mode (no input channel; follow-ups must use codex exec resume)')
    mode = start.add_mutually_exclusive_group()
    mode.add_argument('--brief-file', help='UTF-8 goal file for headless work (max 8 KiB)')
    mode.add_argument('--prompt', default=None, help='Inline UTF-8 goal text for headless work (max 8 KiB; same boundaries as --brief-file)')
    mode.add_argument('--interactive', action='store_true', help='Start at an empty native prompt')
    start.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(start, timeout=START_WAIT_TIMEOUT)
    start.set_defaults(wait=True)
    start.add_argument('--no-wait', dest='wait', action='store_false',
                       help='Return the start receipt without waiting for a running binding')
    override = commands.add_parser('override', help='Dedicated command for engine override. Requires --engine.')
    override.add_argument('--engine', required=True, help='Specific model/engine to run')
    override.add_argument('--reason', default=None, help='Explanation why this specific model/engine must be chosen')
    override.add_argument('--machine')
    override.add_argument('--needs', action='append', default=None,
                          help='Label filter (repeatable, e.g. --needs os:windows --needs arch:arm64)')
    override.add_argument('--workspace', default=None)
    override.add_argument('--repo', default=None, help='Repository owner/name (host clones its own work tree)')
    override.add_argument('--tier', choices=TIERS)
    override.add_argument('--cwd')
    override.add_argument('--name', default=None, help='Friendly session name')
    override.add_argument('--parent', default=None, help='Parent session id recording lineage')
    override.add_argument('--intake-task-id', dest='intake_task_id', default=None,
                            help='Linked intake task id (API field intake_task_id); refused when that task has empty acceptance criteria.')
    override.add_argument('--headless', action='store_true', help='Explicitly start in headless mode')
    override_mode = override.add_mutually_exclusive_group()
    override_mode.add_argument('--brief-file', help='UTF-8 goal file for headless work (max 8 KiB)')
    override_mode.add_argument('--prompt', default=None, help='Inline UTF-8 goal text for headless work (max 8 KiB)')
    override_mode.add_argument('--interactive', action='store_true', help='Start at an empty native prompt')
    override.add_argument('--key', help='Stable idempotency key')
    override.add_argument('--purpose', choices=PURPOSES, default='implement',
                          help='Start purpose for model policy (default implement)')
    add_wait(override)
    send = commands.add_parser('send', help='Enqueue one prompt delivery on a bound session')
    send.add_argument('id')
    body_src = send.add_mutually_exclusive_group(required=True)
    body_src.add_argument('--prompt-id', help='Library prompt id, slug or slug@version (rendered by Hub; see --var)')
    body_src.add_argument('--body-file', help='UTF-8 prompt file, or - for stdin (max 16 KiB)')
    body_src.add_argument('--prompt', default=None,
                          help='Inline UTF-8 prompt text (max 16 KiB; same boundaries as --body-file)')
    send.add_argument('--var', action='append', metavar='KEY=VALUE', help='Variable for {{var.KEY}} in a --prompt-id prompt (repeatable)')
    send.add_argument('--require-ready', dest='require_ready', action='store_true',
                      help='Refuse send immediately if target session has no recent reported state')
    send.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(send)
    capture = commands.add_parser('capture', help='Enqueue one bounded sanitized capture on a bound session (default raw tail; --screen shows current rendered screen)')
    capture.add_argument('id')
    capture.add_argument('--max-bytes', type=int, default=65536)
    capture.add_argument('--screen', action='store_true', help='Capture current rendered screen (plain) instead of raw tail')
    capture.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(capture, timeout=CAPTURE_TIMEOUT, interval=1.0)
    capture.set_defaults(wait=True)
    capture.add_argument('--no-wait', dest='wait', action='store_false',
                         help='Return the queued command receipt without waiting for capture output')
    stop = commands.add_parser('stop', help='Stop a session; confirmed native exit frees a live-session slot immediately')
    stop.add_argument('id')
    stop.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(stop, timeout=30.0, interval=1.0)
    stop.set_defaults(wait=True)
    stop.add_argument('--no-wait', dest='wait', action='store_false', help='Record the stop intent without waiting for verified exit')
    cancel = commands.add_parser('cancel', help='Cancel an owned queued or unclaimed offered intent before native start')
    cancel.add_argument('id')
    cancel.add_argument('--key', required=True, help='Stable cancellation key, reused exactly after a lost response')
    protect = commands.add_parser('protect', help='Permanently protect an owned session from automated cleanup; never stops it')
    protect.add_argument('id')
    protect.add_argument('--key', required=True, help='Stable protection key; this operation has no unprotect counterpart')
    show = commands.add_parser('show', help='Show one session with its binding state')
    show.add_argument('id')
    show.add_argument('--threshold')
    show.add_argument('--json', action='store_true')
    wake = commands.add_parser('wake', help='Register a durable scheduled wake for a session waiting on an external event (no passive timers)')
    wake.add_argument('id', help='Session id or friendly name')
    wake.add_argument('--at', required=True, help='Wake time: ISO-8601 timestamp or "now" (deliver on next tick; watchers use --at now with --body-file result)')
    wake.add_argument('--key', default=None, help='Stable idempotency key (default: random); same key replays, never duplicates')
    wake_src = wake.add_mutually_exclusive_group(required=True)
    wake_src.add_argument('--body-file', help='UTF-8 prompt file, or - for stdin (max 8 KiB)')
    wake_src.add_argument('--prompt', default=None, help='Inline UTF-8 prompt text (max 8 KiB)')
    ping = commands.add_parser('ping', help='External /loop: Hub delivers a message to a live session every interval (any engine); the session or owner clears it')
    ping_cmds = ping.add_subparsers(dest='ping_cmd', required=True)
    for name, helptext in (('create', 'Create or replace the recurring ping of a session'),
                           ('disable', 'Clear the recurring ping (the session itself may do this when its goal is reached)'),
                           ('show', 'Show the active ping, history and delivery failures')):
        node = ping_cmds.add_parser(name, help=helptext)
        add_ping_arguments(node, name, required_id=True)
    loop = sub.add_parser('loop', help='Alias of sessions ping for the current session: set/clear/show a recurring Hub ping')
    loop_cmds = loop.add_subparsers(dest='ping_cmd', required=True)
    for name, helptext in (('set', 'Ping this session (or the given one) every interval'),
                           ('clear', 'Clear the ping; run from inside the session when the goal is reached'),
                           ('show', 'Show the ping state')):
        node = loop_cmds.add_parser(name, help=helptext)
        add_ping_arguments(node, name, required_id=False)
        node.set_defaults(sessions_cmd='ping')
    warnings = commands.add_parser('warnings', help='Current unsnoozed long-running warnings with output tails')
    warnings.add_argument('--threshold')
    warnings.add_argument('--json', action='store_true')
    ack = commands.add_parser('ack', help='Snooze a warning; does not stop the session')
    ack.add_argument('id')
    ack.add_argument('--snooze', help='Duration, e.g. 3h (default: threshold)')
    ack.add_argument('--threshold')
    ack.add_argument('--json', action='store_true')
    events = commands.add_parser('events', help='Show durable warning acknowledgement events')
    events.add_argument('id')
    history = commands.add_parser('commands', help='List durable commands with recorded/transported/acked states')
    history.add_argument('id')
    history.add_argument('--command-id', help='Read one exact command receipt without downloading history')
    receive = commands.add_parser('host-receive', help='Bounded host receiver: poll offers, claim, execute, ack (no daemon; Fleet-owned start rejected)')
    receive.add_argument('--once', action='store_true', required=True,
                         help='Run exactly one bounded poll cycle and exit')
    receive.add_argument('--machine-id', required=True, help='Enrolled host machine label (must match host credential)')
    receive.add_argument('--actor', help='Canonical bus actor for local custody journal (default machine:<machine-id>)')
    receive.add_argument('--credential-family', help='Local custody family label (default host:<machine-id>; server enforces the real family)')
    receive.add_argument('--journal-dir', help='Absolute journal directory (default .local/session/host-receiver under repo cwd)')
    receive.add_argument('--aplexer-bin', default='aplexer', help='Native executable (default aplexer)')
    receive.add_argument('--limit', type=int, default=20, help='Max offers per cycle (1..20)')
    receive.add_argument('--sender-workspace', default=None, help='Pinned authorized native sender workspace for replyable sends (default: receiver own whoami workspace)')
    receive.add_argument('--command-kind', choices=('stop', 'non_stop'), help='Run only the urgent stop path or all non-stop commands')
    receive.add_argument('--settings', default=None, help='Host settings file (subscriptions.json) supplying the trusted cleanup_roots allowlist for the STOP-bundled tree step (default: tree step disabled)')
    cleanup = commands.add_parser('cleanup', help='Plan finished/superseded cleanup (dry-run default; --apply enqueues real M12 stop intents)')
    cleanup.add_argument('--apply', action='store_true', help='Act: enqueue owned stops via M12 custody (server-authenticated owner only; owning host executes via claim/ack)')
    cleanup.add_argument('--key', default=None, help='Stable idempotency key for --apply (same key replays, never duplicates)')
    cleanup.add_argument('--tree', action='store_true', help='After enqueue, observe the OWNING-HOST tree outcome remotely (read-only; the CLI never executes Git — add --wait to block until terminal)')
    add_wait(cleanup)
    cleanup.add_argument('--limit', type=int, default=50)
    cleanup.add_argument('--owner', default=None, help='Scope to one owner (admins only; callers always see their own actor)')
    cleanup.add_argument('--host', default=None, help='Scope to one machine id')
    cleanup.add_argument('--task', default=None, help='Scope to one intake task id')
    observe = commands.add_parser('observe', help='Record a machine-scoped lifecycle observation (host credential only; owner flags never accepted)')
    observe.add_argument('--machine-id', required=True, help='Observed machine id (must match the host credential)')
    observe.add_argument('--native-id', required=True, help='Observed native session id')
    observe.add_argument('--observed-at', default=None, help='Explicit Z or ±HH:MM timestamp (default: server time)')
    observe.add_argument('--limit-hit', action='store_true')
    observe.add_argument('--limit-window', default=None)
    observe.add_argument('--limit-reset', default=None)
    observe.add_argument('--limit-zone', default=None)
    observe.add_argument('--awaiting-alexey', action='store_true')
    observe.add_argument('--awaiting-alexey-at', default=None)
    observe.add_argument('--report-written', action='store_true')
    observe.add_argument('--report-at', default=None)
    observe.add_argument('--screen-done', action='store_true')
    observe.add_argument('--terminal-evidence', choices=('explicit_completion', 'supersession_record'), default=None)
    observe.add_argument('--terminal-evidence-at', default=None)
    observe.add_argument('--authoritative-no-pending-proof', action='store_true')
    observe.add_argument('--pending-obligations', action='store_true')
    observe.add_argument('--has-active-children', action='store_true')
    observe.add_argument('--has-unsubmitted-draft', action='store_true')
    observe.add_argument('--has-unresolved-send-stop', action='store_true')
    observe.add_argument('--worktree-missing', action='store_true')
    observe.add_argument('--cwd-path-recreated', action='store_true',
                         help='Path with a stale deleted label now holds a valid worktree')
    observe.add_argument('--superseded-by', default=None)
    observe.add_argument('--note', default=None, help='Opt-in free text (bounded, redacted server-side; reason codes preferred)')
    audit = commands.add_parser('audit', help='Idle-session audit action log: recent runs, per-action counts and decisions (admin)')
    audit.add_argument('--last', type=int, default=5, help='How many recent runs to show (1-50)')
    audit.add_argument('--run', action='store_true', help='Trigger one audit pass now (admin; bypasses the 30-minute gate)')
    audit.add_argument('--export', action='store_true', help='Export the full decision dataset for decider training')
    audit.add_argument('--limit', type=int, default=5000, help='Row cap for --export')

def match_session_state(entry: dict, wanted: str) -> bool:
    """Classify session state for filtering, treating confirmation/question waiting as waiting-idle."""
    entry_state = entry.get('state')
    agent_state = entry.get('agent_state')
    is_waiting_confirmation = bool(
        agent_state in ('waiting-confirmation', 'waiting_confirmation')
        or entry_state in ('waiting-confirmation', 'waiting_confirmation')
        or entry.get('waiting_confirmation') is True
    )
    is_waiting = bool(
        is_waiting_confirmation
        or agent_state in ('waiting', 'waiting-input', 'waiting_input')
        or entry_state in ('waiting', 'waiting-input', 'waiting_input', 'waiting_alexey')
        or entry.get('waiting_input') is True
    )
    is_idle = bool(
        agent_state == 'idle'
        or entry_state == 'idle'
        or entry.get('idle') is True
        or is_waiting
    )
    if wanted in ('waiting-confirmation', 'waiting_confirmation'):
        return is_waiting_confirmation
    if wanted in ('waiting', 'waiting-idle', 'waiting-input', 'waiting_input'):
        return is_waiting
    if wanted == 'idle':
        return is_idle
    if wanted in ('running', 'active', 'working'):
        return bool(entry_state in ('running', 'active') and not is_waiting and agent_state != 'idle')
    return entry_state == wanted


def wait_until_bound(session_id, interval, timeout=BIND_WAIT_TIMEOUT):
    """Poll a pending session until it is running (bound); False when the budget runs out."""
    from urllib.parse import quote
    end = time.monotonic() + timeout
    while True:
        row = request('GET', '/api/sessions/' + quote(session_id, safe=''))
        if isinstance(row, dict):
            state = row.get('state')
            if state == 'running':
                return True
            if state not in ('queued', 'offered', 'starting', 'uncertain'):
                return False  # terminal or unknown: no bind is coming
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def _session_page(page):
    """Accept both Hub generations without turning an incomplete page into EOF."""
    if not isinstance(page, dict):
        raise ValueError('Invalid session list page')
    rows = page.get('sessions', page.get('items'))
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
            for row in rows):
        raise ValueError('Invalid session list entries')
    aliases = [page.get(key) for key in ('next_cursor', 'next')]
    if any(value is not None and not isinstance(value, str) for value in aliases):
        raise ValueError('Invalid session list cursor')
    cursors = {value for value in aliases if value}
    if len(cursors) > 1:
        raise ValueError('Conflicting session list cursors')
    cursor = next(iter(cursors), None)
    if 'complete' in page and type(page['complete']) is not bool:
        raise ValueError('Invalid session list completion flag')
    if page.get('complete') is False and cursor is None:
        raise ValueError('Incomplete session list has no continuation cursor')
    if page.get('complete') is True and cursor is not None:
        raise ValueError('Complete session list has a continuation cursor')
    return rows, cursor


def execute(args):
    # Capture's budget covers enqueue, every paginated HTTP request and sleep.
    # A context-local deadline cannot affect concurrent host receiver requests.
    if getattr(args, 'sessions_cmd', None) in ('capture', 'stop'):
        timeout, _ = check_wait_args(args)
        if timeout is not None:
            token = _request_deadline.set(time.monotonic() + timeout)
            try:
                return _execute(args)
            finally:
                _request_deadline.reset(token)
    return _execute(args)


def _execute(args):
    import uuid
    cmd = getattr(args, 'sessions_cmd', None)
    if cmd is None:
        # Default grouped needs-action lifecycle projection (M15c). The
        # existing `list` spelling keeps the flat session intent listing.
        return request('GET', '/api/sessions?view=lifecycle')
    if cmd == 'link':
        from urllib.parse import quote
        return request('POST', '/api/sessions/' + quote(args.session, safe='') + '/link',
                       {'key': getattr(args, 'key', None) or 'link-' + uuid.uuid4().hex,
                        'intake_task_id': args.intake_task_id})
    if cmd == 'cleanup':
        from urllib.parse import quote
        params = []
        limit = int(getattr(args, 'limit', None) or 50)
        params.append('limit=%d' % max(1, min(50, limit)))
        for flag, name in (('owner', 'owner'), ('host', 'host'), ('task', 'task')):
            value = getattr(args, flag, None)
            if value:
                params.append('%s=%s' % (name, quote(str(value), safe='')))
        path = '/api/sessions/cleanup'
        if params:
            path += '?' + '&'.join(params)
        if getattr(args, 'apply', False):
            # M15d REAL apply: the server enqueues M12 stop intents for
            # freshly eligible OWN sessions (owner custody inside the txn).
            # The owning host executes each enqueued stop through the existing
            # claim/effect/ack path with containment-confirmed exit; tree
            # removal follows on the owning host via the same pipeline
            # (HOST-config roots + canonical binding workspace + live native
            # identity + discovered branch, never caller-supplied paths).
            # `--tree` only OBSERVES that remote outcome (read-only commands
            # listing, bounded); this CLI never executes Git. Never claim
            # success here: the server reports enqueued commands + kept
            # reasons, and --tree additionally reports the remote tree
            # outcomes below.
            body = {'apply': True}
            if getattr(args, 'key', None):
                body['key'] = args.key
            applied = request('POST', path, body)
            if not getattr(args, 'tree', False):
                return applied
            return _observe_cleanup_trees(args, applied)
        # Dry-run default: read-only plan, zero server mutation.
        return request('POST', path, {'dry_run': True})
    if cmd == 'observe':
        from .session_lifecycle import build_observation_payload
        fields = {}
        if getattr(args, 'limit_hit', False):
            fields['limit_hit'] = True
        for flag, name in (('limit_window', 'limit_window'), ('limit_reset', 'limit_reset_at'),
                           ('limit_zone', 'limit_zone'), ('awaiting_alexey_at', 'awaiting_alexey_at'),
                           ('report_at', 'report_at'), ('terminal_evidence', 'terminal_evidence'),
                           ('terminal_evidence_at', 'terminal_evidence_at'), ('superseded_by', 'superseded_by')):
            value = getattr(args, flag, None)
            if value:
                fields[name] = value
        for flag in ('awaiting_alexey', 'report_written', 'screen_done', 'authoritative_no_pending_proof',
                     'pending_obligations', 'has_active_children', 'has_unsubmitted_draft',
                     'has_unresolved_send_stop', 'worktree_missing', 'cwd_path_recreated'):
            if getattr(args, flag, False):
                fields[flag] = True
        if getattr(args, 'note', None):
            fields['observation_note'] = args.note
        payload = build_observation_payload(fields)
        body = {'machine_id': args.machine_id, 'native_id': args.native_id, 'payload': payload}
        if getattr(args, 'observed_at', None):
            body['observed_at'] = args.observed_at
        return request('POST', '/api/sessions/observations', body)
    if cmd == 'audit':
        if getattr(args, 'run', False):
            return request('POST', '/api/sessions/audit/run', {})
        if getattr(args, 'export', False):
            return request('GET', '/api/sessions/audit/export?limit=%d' % max(1, min(20000, int(getattr(args, 'limit', None) or 5000))))
        return request('GET', '/api/sessions/audit?last=%d' % max(1, min(50, int(getattr(args, 'last', None) or 5))))
    if cmd == 'pick':
        return request('POST', '/api/model-picker/pick', {'tier': args.tier} if getattr(args, 'tier', None) else {})
    if cmd == 'count':
        from urllib.parse import quote
        by = normalize_breakdown_dims(getattr(args, 'by', None))
        return request('GET', '/api/sessions?view=breakdown&by=' + quote(','.join(by), safe=','))
    if cmd == 'reap':
        return request('POST', '/api/sessions/reap', {'dry_run': bool(getattr(args, 'dry_run', False))})
    if cmd == 'first-attempt-report':
        from .first_attempt_outcome import berlin_day_range, report_for_day
        day = getattr(args, 'day', None)
        berlin_day_range(day)
        examples = getattr(args, 'examples', 3)
        if examples is None:
            examples = 3
        if type(examples) is not int or not 0 <= examples <= 10:
            raise ValueError('Examples per cause must be 0..10')
        input_file = getattr(args, 'input_file', None)
        if input_file:
            import json as _json
            with open(input_file, 'r', encoding='utf-8') as stream:
                snapshot = _json.load(stream)
            sessions = snapshot.get('sessions') if isinstance(snapshot, dict) else snapshot
            if not isinstance(sessions, list):
                raise ValueError('Input file must hold a sessions list or an object with a sessions list')
        else:
            # Page the full historical listing (the API caps pages at 100).
            from urllib.parse import quote
            sessions = []
            known = set()
            cursor = None
            cursors = set()
            while True:
                path = '/api/sessions?limit=100'
                if cursor:
                    path += '&cursor=' + quote(cursor, safe='')
                page = request('GET', threshold_path(args, path))
                entries, cursor = _session_page(page)
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get('id') in known:
                        continue
                    known.add(entry.get('id'))
                    sessions.append(entry)
                if not cursor:
                    break
                if cursor in cursors:
                    raise ValueError('Session list pagination repeated a cursor; retry the listing')
                cursors.add(cursor)
        return report_for_day(sessions, day, examples)
    if cmd == 'tree' or (cmd == 'list' and getattr(args, 'tree', False)):
        return request('GET', '/api/sessions/tree')
    if cmd == 'list':
        wanted = getattr(args, 'state', None)
        total_limit = getattr(args, 'limit', None)
        if total_limit is not None and total_limit < 1:
            raise ValueError('Session list limit must be at least 1')
        # The API intentionally caps each indexed page at 100. Walk its cursor
        # until exhausted so the default list (including --json) cannot hide
        # older live sessions behind that page boundary.
        from urllib.parse import quote
        seen: list = []
        known: set = set()
        cursor = None
        cursors: set = set()
        first_page = None
        native_inventories = {}
        inventory_generations = {}
        # 3.2: live=1 is the default view; --all (or an explicitly terminal
        # --state) walks history. A --limit caps rows, it does not request
        # history.
        live_only = (not getattr(args, 'all', False)
                     and wanted not in ('stopped', 'failed', 'cancelled', 'refused', 'done',
                                        'expired', 'completed-awaiting-review'))
        while True:
            page_size = min(100, total_limit - len(seen)) if total_limit is not None else 100
            path = '/api/sessions?%slimit=%d' % ('live=1&' if live_only else '', page_size)
            if cursor:
                path += '&cursor=' + quote(cursor, safe='')
            page = request('GET', threshold_path(args, path))
            if first_page is None:
                first_page = page
            entries, cursor = _session_page(page)
            observations = page.get('native_inventories', []) if isinstance(page, dict) else []
            if isinstance(page, dict) and isinstance(page.get('native_inventory'), dict):
                observations = [*observations, page['native_inventory']]
            for observation in observations:
                if isinstance(observation, dict):
                    machine = observation.get('machine_id')
                    generation = (observation.get('snapshot_id'), observation.get('fresh'), observation.get('complete'), observation.get('error'))
                    if machine in inventory_generations and inventory_generations[machine] != generation:
                        raise ValueError('Native inventory changed during pagination; restart the listing')
                    inventory_generations[machine] = generation
                    identity = (machine, observation.get('snapshot_id'))
                    native_inventories[identity] = observation
            if len(entries) > page_size:
                raise ValueError('Session list page exceeds the requested limit')
            for entry in entries:
                if entry.get('id') in known:
                    continue
                known.add(entry.get('id'))
                if not wanted or match_session_state(entry, wanted):
                    seen.append(entry)
                    if total_limit is not None and len(seen) >= total_limit:
                        break
            if cursor in cursors:
                raise ValueError('Session list pagination repeated a cursor; retry the listing')
            if (total_limit is not None and len(seen) >= total_limit) or not cursor:
                break
            cursors.add(cursor)
        result = dict(first_page or {})
        result['sessions'] = seen
        if native_inventories:
            result['native_inventories'] = list(native_inventories.values())
        result['next_cursor'] = cursor
        if 'items' in result:
            result['items'] = seen
        if 'next' in result:
            result['next'] = cursor
        if 'complete' in result:
            result['complete'] = cursor is None
        return result
    if cmd == 'show':
        return request('GET', threshold_path(args, '/api/sessions/' + args.id))
    if cmd == 'events':
        return request('GET', '/api/sessions/' + args.id + '/events')
    if cmd == 'ack':
        payload = {'snooze': duration_seconds(args.snooze)} if args.snooze else {}
        return request('POST', threshold_path(args, '/api/sessions/' + args.id + '/ack'), payload)
    if cmd == 'ping':
        return execute_ping(args)
    if cmd == 'wake':
        import uuid as _uuid
        import re as _re
        if not isinstance(args.id, str) or not _re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', args.id):
            raise ValueError('Invalid session id or name')
        if getattr(args, 'prompt', None) is not None and getattr(args, 'body_file', None):
            raise SessionInputError('Cannot specify both --body-file and --prompt')
        if getattr(args, 'prompt', None) is not None:
            prompt_text = read_prompt_text(args.prompt, limit=MAX_BRIEF, max_chars=MAX_BRIEF_CHARS, field='Prompt')
        elif getattr(args, 'body_file', None):
            prompt_text = read_file(args.body_file, limit=MAX_BRIEF, field='Body file')
        else:
            raise SessionInputError('Either --body-file or --prompt is required')
        at_value = (getattr(args, 'at', None) or '').strip()
        if not at_value:
            raise SessionInputError('Missing --at wake time (ISO-8601 or "now")')
        key = getattr(args, 'key', None) or str(_uuid.uuid4())
        try:
            queued = request('POST', '/api/sessions/' + args.id + '/wake',
                             {'key': key, 'at': at_value, 'prompt': prompt_text})
        except ValueError as exc:
            msg = str(exc)
            if key and f'reconcile with --key {key}' not in msg:
                msg = f'{msg} (reconcile with --key {key} via show/commands)'
            raise ValueError(msg) from None
        return dict(queued, client_key=key)
    if cmd == 'warnings':
        result = request('GET', threshold_path(args, '/api/sessions/warnings'))
        from concurrent.futures import ThreadPoolExecutor
        def capture_warning(warning):
            try:
                captured = execute(argparse.Namespace(sessions_cmd='capture', id=warning['id'],
                                   max_bytes=4096, wait=True, timeout=CAPTURE_TIMEOUT, interval=1.0))
                warning['output_tail'] = captured['command_wait']['ack']['capture'].splitlines()[-5:]
                warning['output_status'] = 'captured'
            except (ValueError, KeyError):
                warning['output_tail'] = []
                warning['output_status'] = 'unavailable; use hub sessions capture ' + warning['id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(capture_warning, result.get('warnings', [])))
        return result
    if cmd == 'commands':
        command_id = getattr(args, 'command_id', None)
        if command_id is not None:
            if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', command_id):
                raise SessionInputError('invalid_command_id')
            return {'commands': [_fetch_command_entry(args.id, command_id)], 'next_cursor': None}
        return request('GET', _native_command_path(args.id) or '/api/sessions/' + args.id + '/commands')
    if cmd in ('start', 'override'):
        engine = getattr(args, 'engine', None)
        model = getattr(args, 'model', None)
        reason = getattr(args, 'reason', None)
        if model and model in ('fable', 'claude-fable-5-2') and engine not in (None, 'claude'):
            raise ValueError('--model fable requires --engine claude')
        if cmd == 'override' and not engine:
            raise ValueError('--engine is required for dedicated engine override command')
        key = getattr(args, 'key', None) or str(uuid.uuid4())
        repo = getattr(args, 'repo', None)
        if not repo and not getattr(args, 'workspace', None):
            raise ValueError('Give --workspace or --repo (Hub2 needs --workspace for placement; add --repo when several projects exist)')
        payload = {'key': key}
        if not repo and getattr(args, 'workspace', None):
            repo = _repo_from_workspace(args.workspace)
        if repo:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}', repo):
                raise ValueError('--repo must be owner/name')
            payload['repo'] = repo
        if getattr(args, 'workspace', None):
            payload['workspace'] = args.workspace
        for field in ('engine', 'machine', 'tier', 'cwd'):
            value = getattr(args, field, None)
            if value:
                payload[field] = value
        if model:
            payload['model'] = model
        if getattr(args, 'effort', None) is not None:
            payload['effort'] = args.effort
        if getattr(args, 'intake_worktree', None):
            if payload.get('cwd'):
                raise ValueError('--intake-worktree successor keeps Hub in charge of the cwd; drop --cwd (a detached start never names one either)')
            payload['intake_worktree'] = args.intake_worktree
        needs = getattr(args, 'needs', None)
        if needs:
            if not isinstance(needs, list) or len(needs) > 64 or any(
                    not isinstance(n, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:_-]{0,95}', n) for n in needs):
                raise ValueError('Invalid --needs label (lowercase [a-z0-9:_-], e.g. os:windows)')
            if len(set(needs)) != len(needs):
                raise ValueError('Duplicate --needs label')
            payload['needs'] = list(needs)
        if getattr(args, 'task_kind', None) is not None:
            payload['task_kind'] = args.task_kind
        payload['purpose'] = getattr(args, 'purpose', None) or 'implement'
        if getattr(args, 'force', False):
            payload['force'] = True
        if getattr(args, 'intake_task_id', None):
            val = str(args.intake_task_id).strip()
            m = re.search(r'/intake/([A-Za-z0-9._:-]{6,128})', val)
            payload['intake_task_id'] = m.group(1) if m else val
        if reason and reason.strip():
            payload['engine_override_reason'] = reason.strip()
        if getattr(args, 'name', None) is not None:
            payload['name'] = check_session_name(args.name)
        if getattr(args, 'parent', None) is not None:
            payload['parent_id'] = args.parent
        with_goal = getattr(args, 'with_goal', False)
        if with_goal and getattr(args, 'headless', False):
            raise ValueError('--with-goal requires interactive mode; cannot use --headless')
        if with_goal:
            payload['with_goal'] = True
        payload['mode'] = 'interactive' if getattr(args, 'interactive', False) or with_goal else 'headless'
        if getattr(args, 'headless', False):
            payload['mode'] = 'headless'
        if getattr(args, 'brief_file', None):
            if payload['mode'] == 'interactive' and not with_goal:
                raise SessionInputError('Interactive sessions start at an empty native prompt')
            payload['brief'] = read_file(args.brief_file, limit=MAX_BRIEF, max_chars=MAX_BRIEF_CHARS, field='Brief file')
        if getattr(args, 'prompt', None) is not None:
            if payload['mode'] == 'interactive' and not with_goal:
                raise SessionInputError('Interactive sessions start at an empty native prompt')
            payload['brief'] = read_prompt_text(args.prompt, limit=MAX_BRIEF, max_chars=MAX_BRIEF_CHARS, field='Prompt')
        if with_goal and not payload.get('brief', '').strip():
            raise SessionInputError('--with-goal requires --prompt or --brief-file')
        try:
            created = request('POST', '/api/sessions', payload)
        except ValueError as exc:
            # Lost-response reconciliation: the key decides replay vs conflict,
            # never an unkeyed retry.
            msg = str(exc)
            if key and f'reconcile with --key {key}' not in msg:
                msg = f'{msg} (reconcile with --key {key} via show/commands)'
            raise ValueError(msg) from None
        # 2.4: the server may answer with the picker envelope
        # {session: {...}, picker: {...}}; hoist the session id/state so both
        # the flat and nested shapes drive the same wait and rendering.
        if isinstance(created, dict) and not created.get('id') and isinstance(created.get('session'), dict):
            created = {**created, **created['session']}
        if not isinstance(created, dict) or not isinstance(created.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', created['id']):
            raise ValueError(f'Hub accepted the start but returned no session id (unexpected response shape); start outcome unconfirmed (reconcile with --key {key} via show/commands)')
        created = dict(created, client_key=key)
        timeout, interval = check_wait_args(args)
        if timeout is not None:
            binding = wait_for_session(created['id'], timeout, interval)
            created['binding_wait'] = binding
            bound = binding.get('session') if isinstance(binding, dict) else None
            bound = bound if isinstance(bound, dict) else binding
            if not isinstance(bound, dict) or bound.get('state') != 'running':
                state = bound.get('state') if isinstance(bound, dict) else None
                attention = bound.get('attention') if isinstance(bound, dict) else None
                reason = attention.get('reason') if isinstance(attention, dict) else None
                detail = f' ({reason})' if isinstance(reason, str) and SAFE_ERROR.fullmatch(reason) else ''
                stage = attention.get('stage') if isinstance(attention, dict) else None
                if not isinstance(stage, str) or not SAFE_ERROR.fullmatch(stage):
                    stage = bound.get('stage') if isinstance(bound, dict) else None
                if isinstance(stage, str) and SAFE_ERROR.fullmatch(stage):
                    detail += f' (stage={stage})'
                raw_detail = attention.get('detail') if isinstance(attention, dict) else None
                if raw_detail is None and isinstance(bound, dict):
                    raw_detail = bound.get('detail')
                safe_detail = sanitize_start_detail(raw_detail)
                if safe_detail:
                    detail += f'; detail: {safe_detail}'
                raise SessionInputError(
                    f'session_start_failed; session {created["id"]} reached {state or "an unconfirmed state"}{detail}; '
                    f'reconcile with --key {key} via show/events'
                )
            created.update(bound)
            if isinstance(created.get('session'), dict):
                created['session'] = {**created['session'], **bound}
        return created
    if cmd in ('cancel', 'protect'):
        from urllib.parse import quote
        if not isinstance(args.id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', args.id):
            raise ValueError('Invalid session id or name')
        if not isinstance(args.key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.key):
            raise ValueError('Invalid protection key' if cmd == 'protect' else 'Invalid cancellation key')
        return request('POST', '/api/sessions/' + quote(args.id, safe='') + '/' + cmd, {'key': args.key})
    if cmd in ('send', 'capture', 'stop') and _native_command_path(args.id):
        native = _native_command_path(args.id)
        timeout, interval = check_wait_args(args)
        key = getattr(args, 'key', None) or str(uuid.uuid4())
        payload = {'kind': cmd, 'key': key}
        if cmd == 'send':
            if getattr(args, 'prompt', None) is not None and getattr(args, 'body_file', None):
                raise SessionInputError('Cannot specify both --body-file and --prompt')
            if getattr(args, 'prompt_id', None):
                from .prompt_schedules import parse_vars
                rendered = request('POST', '/api/prompts/' + __import__('urllib.parse').parse.quote(args.prompt_id, safe='@') + '/render',
                                   {'vars': parse_vars(getattr(args, 'var', None)) or {}})
                text = rendered.get('text')
            elif getattr(args, 'body_file', None):
                text = read_file(args.body_file, limit=8192, field='Body file')
            else:
                text = getattr(args, 'prompt', None)
            try:
                text = read_prompt_text(text, limit=8192, max_chars=8192, field='Prompt')
            except UnicodeError:
                raise SessionInputError('invalid_native_send_text') from None
            if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', text):
                raise SessionInputError('invalid_native_send_text')
            payload['text'] = text
        if cmd == 'capture' and getattr(args, 'screen', False):
            payload['mode'] = 'screen'
        def enqueue_native():
            return request('POST', native, payload)
        try:
            queued = api.replay_keyed(enqueue_native, hub_unavailable, delays=replay_delays(), notify=replay_notice)
        except ValueError as exc:
            raise SessionInputError(f'{exc}; reconcile with --key {key} via commands') from None
        command = queued.get('command')
        if (not isinstance(command, dict) or not isinstance(command.get('id'), str)
                or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', command['id']) or command.get('target_kind') != 'native'):
            raise SessionInputError(f'invalid_native_command_receipt; reconcile with --key {key} via commands')
        if cmd == 'send' and (command.get('kind') != 'send' or command.get('machine_id') != args.id.split(':')[1]
                                  or command.get('native_id') != args.id.split(':')[2] or type(command.get('deadline_ms')) is not int):
            raise SessionInputError(f'invalid_native_command_receipt; reconcile with --key {key} via commands')
        queued = dict(queued, client_key=key, command_id=command['id'])
        if timeout is not None:
            entry = wait_for_command(args.id, command['id'], timeout, interval)
            ack = entry.get('ack') or {}
            if entry.get('state') != 'acked' or cmd == 'stop' and ack.get('confirmed_exit') is not True:
                reason = ack.get('error')
                if not isinstance(reason, str) or not SAFE_ERROR.fullmatch(reason):
                    reason = 'native_command_unconfirmed'
                raise SessionInputError(f'{reason}; command {command["id"]}; reconcile with commands')
            if cmd == 'send':
                if not isinstance(ack, dict):
                    raise SessionInputError(f'native_send_unconfirmed; command {command["id"]}; reconcile with commands')
                proof = ack.get('submission') if isinstance(ack, dict) else None
                if not isinstance(proof, dict):
                    proof = {}
                if (entry.get('id') != command['id'] or entry.get('native_id') != command['native_id']
                        or ack.get('submitted') is not True or 'consumed' not in ack or ack['consumed'] is not None
                        or type(proof.get('version')) is not int or proof.get('version') != 1 or proof.get('operation_id') != command['id']
                        or proof.get('session_id') != command['native_id'] or proof.get('status') != 'submitted_verified'
                        or proof.get('landed') is not True or proof.get('submitted') is not True or proof.get('mutation') is not True
                        or proof.get('deadline_ms') != command['deadline_ms'] or proof.get('reason') != 'submission_observed'
                        or not isinstance(proof.get('request_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', proof['request_sha256'])
                        or 'consumed' not in proof or proof['consumed'] is not None):
                    raise SessionInputError(f'native_send_unconfirmed; command {command["id"]}; reconcile with commands')
            queued['stop_wait' if cmd == 'stop' else 'command_wait'] = entry
        return queued
    if cmd in ('send', 'capture', 'stop'):
        timeout, interval = check_wait_args(args)
        key = getattr(args, 'key', None) or str(uuid.uuid4())
        if cmd == 'send':
            if getattr(args, 'prompt', None) is not None and getattr(args, 'body_file', None):
                raise SessionInputError('Cannot specify both --body-file and --prompt')
            if getattr(args, 'prompt_id', None):
                from .prompt_schedules import parse_vars
                rendered = request('POST', '/api/prompts/' + __import__("urllib.parse").parse.quote(args.prompt_id, safe='@') + '/render',
                                   {'vars': parse_vars(getattr(args, 'var', None)) or {}})
                body = rendered.get('text') or ''
                if not body.strip():
                    raise SessionInputError('Prompt rendered empty')
            elif getattr(args, 'prompt', None) is not None:
                body = read_prompt_text(args.prompt, limit=MAX_BODY, field='Prompt')
            elif getattr(args, 'body_file', None):
                body = read_file(args.body_file, limit=MAX_BODY, field='Body file')
            else:
                raise SessionInputError('Either --body-file or --prompt is required')
            payload = {'key': key, 'body': body}
            if getattr(args, 'require_ready', False):
                payload['require_ready'] = True
        elif cmd == 'capture':
            # Default tail wire unchanged (no mode field); --screen encodes
            # explicit mode=screen so the server persists the projection.
            payload = {'key': key, 'max_bytes': args.max_bytes}
            if getattr(args, 'screen', False):
                payload['mode'] = 'screen'
        else:
            payload = {'key': key}
        try:
            def enqueue():
                try:
                    return request('POST', '/api/sessions/' + args.id + '/' + cmd, payload)
                except SessionHTTPError as exc:
                    if cmd != 'capture' or not exc.pending or timeout is None:
                        raise
                    # Early capture: wait for the native binding instead of failing with
                    # a bare 409, then give the acknowledgement its full budget.
                    print(f'Session {args.id} is {exc.state or "starting"}: waiting up to {BIND_WAIT_TIMEOUT:g}s for its native binding',
                          file=sys.stderr, flush=True)
                    outer = _request_deadline.get()
                    bind_token = _request_deadline.set(time.monotonic() + BIND_WAIT_TIMEOUT + 10)
                    try:
                        bound = wait_until_bound(args.id, min(interval, 2.0))
                    finally:
                        _request_deadline.reset(bind_token)
                    if not bound:
                        raise SessionInputError(f'session_not_bound; session {args.id} did not reach running within {BIND_WAIT_TIMEOUT:g}s; retry later') from None
                    if outer is not None:
                        _request_deadline.set(time.monotonic() + timeout)
                    return request('POST', '/api/sessions/' + args.id + '/' + cmd, payload)
            # Capture is diagnostic and stays single-shot; keyed mutations
            # replay, never past the caller's wait budget.
            queued = (api.replay_keyed(enqueue, hub_unavailable, delays=replay_delays(), notify=replay_notice)
                      if cmd in ('send', 'stop') else enqueue())
        except ValueError as exc:
            msg = str(exc)
            if cmd == 'stop' and isinstance(exc, SessionTransportError):
                msg = 'stop_hub_unreachable; ' + msg
            if key and f'reconcile with --key {key}' not in msg:
                msg = f'{msg} (reconcile with --key {key} via commands)'
            raise ValueError(msg) from None
        queued = dict(queued, client_key=key)
        if queued.get('warning') == 'target_no_reported_state':
            sys.stderr.write('Warning: target session has no recent reported state\n')
        if queued.get('delivery') == 'next_turn':
            sys.stderr.write('Note: headless session; message queued in its inbox for its next turn '
                             '(acked when the recipient acknowledges it; add --wait to follow)\n')
        if timeout is not None:
            if cmd == 'stop' and queued.get('state') == 'cancelled' and (queued.get('cancellation') or {}).get('effect') == 'no_native_start':
                queued['stop_wait'] = {'state': 'cancelled', 'effect': 'no_native_start'}
            elif cmd == 'stop':
                from .session_stop import wait_for_confirmed_stop
                command_id = queued.get('command_id')
                if not command_id:
                    raise SessionInputError('stop_not_recorded; no command receipt returned')
                queued['stop_wait'] = wait_for_confirmed_stop(args.id, command_id, timeout, interval,
                    wait_command=wait_for_command, request=request)
            else:
                command_id = queued['command_id']
                if cmd == 'capture':
                    print(f'Capture command {command_id}: waiting for host acknowledgement (timeout {timeout:g}s)', file=sys.stderr, flush=True)
                try:
                    queued['command_wait'] = wait_for_command(args.id, command_id, timeout, interval)
                except ValueError as exc:
                    if cmd != 'capture':
                        raise
                    reason = 'hub_unreachable' if isinstance(exc, SessionTransportError) and time.monotonic() < _request_deadline.get() else 'capture_not_acked'
                    raise SessionInputError(f'{reason}; command {command_id}; inspect commands before retrying with the same key') from None
                if cmd == 'capture':
                    entry = queued['command_wait']
                    ack = entry.get('ack') or {}
                    if entry.get('state') != 'acked':
                        reason = ack.get('reason')
                        if not isinstance(reason, str) or not SAFE_ERROR.fullmatch(reason):
                            reason = 'capture_failed'
                        raise SessionInputError(f'{reason}; command {command_id}')
                    capture = ack.get('capture')
                    if not isinstance(capture, str) or not capture.strip():
                        raise SessionInputError(f'capture_empty; command {command_id}')
        return queued
    if cmd == 'host-receive':
        if not getattr(args, 'once', False):
            raise ValueError('host-receive runs one bounded cycle only; pass --once')
        machine_id = getattr(args, 'machine_id', None)
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError('--machine-id is required')
        limit = int(getattr(args, 'limit', 20) or 20)
        if not 1 <= limit <= 20:
            raise ValueError('--limit must be 1..20')
        actor = getattr(args, 'actor', None) or ('machine:' + machine_id)
        family = getattr(args, 'credential_family', None) or ('host-' + machine_id)
        journal_dir = getattr(args, 'journal_dir', None)
        if not journal_dir:
            journal_dir = os.path.join(os.getcwd(), '.local', 'session', 'host-receiver')
        from .session_executor import CallerCustody
        from .session_receiver import receive_once
        custody = CallerCustody(actor=actor, machine_id=machine_id,
                                credential_family=family).validated()
        # M15d STOP-bundled tree roots come from HOST config, never CLI paths:
        # the supervised session_host passes its loaded settings allowlist
        # programmatically; manual runs opt in via --settings (a host settings
        # file validated by fleet.load_settings). Absent either, stops are
        # still executed and acked tree-free (fail closed, honestly observed).
        cleanup_roots = getattr(args, 'cleanup_roots', None)
        settings_path = getattr(args, 'settings', None)
        if cleanup_roots is None and settings_path:
            from .fleet import load_settings
            try:
                cleanup_roots = load_settings(settings_path).get('cleanup_roots', [])
            except (ValueError, OSError) as exc:
                raise ValueError('host-receive --settings unreadable: %s' % type(exc).__name__) from None
        return receive_once(custody=custody, journal_dir=journal_dir,
                            executable=getattr(args, 'aplexer_bin', 'aplexer') or 'aplexer',
                            request=request, limit=limit,
                            sender_workspace=getattr(args, 'sender_workspace', None),
                            cleanup_roots=cleanup_roots,
                            command_kind=getattr(args, 'command_kind', None))
    raise ValueError('Unknown sessions command')


def format_duration(seconds):
    if not isinstance(seconds, (int, float)):
        return '-'
    seconds = max(0, int(seconds))
    return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m{seconds % 60:02d}s'


def _format_timestamp(value):
    """UTC 'YYYY-MM-DD HH:MM:SS' from an ISO string or numeric epoch (s or ms)."""
    if isinstance(value, (int, float)) and value > 0:
        from datetime import datetime, timezone
        seconds = value / 1000 if value > 10 ** 12 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        except (OverflowError, OSError, ValueError):
            return str(value)
    if not isinstance(value, str) or not value:
        return '-'
    return value[:19].replace('T', ' ')


BREAKDOWN_DIMS = ('host', 'engine', 'account')


def normalize_breakdown_dims(value):
    """Validate `--by host,engine,account` (order kept, duplicates dropped)."""
    if value is None or not str(value).strip():
        return list(BREAKDOWN_DIMS)
    dims = []
    for part in str(value).split(','):
        part = part.strip().lower()
        if not part:
            continue
        if part not in BREAKDOWN_DIMS:
            raise SessionInputError('--by accepts only host, engine and account (comma-separated)')
        if part not in dims:
            dims.append(part)
    if not dims:
        raise SessionInputError('--by needs at least one of host, engine, account')
    return dims


def render_breakdown(result):
    """Text table for `hub sessions count`: one row per group plus totals."""
    if not isinstance(result, dict):
        return str(result)
    dims = [d for d in (result.get('by') or BREAKDOWN_DIMS) if d in BREAKDOWN_DIMS]
    totals = result.get('totals') or {}
    rows = []
    for r in result.get('rows') or []:
        cells = []
        for d in dims:
            value = r.get(d) or '-'
            if d == 'host':
                label = r.get('host_label') or value
                aliases = [a.get('label') or a.get('id') for a in (r.get('host_aliases') or []) if isinstance(a, dict)]
                value = label + (' (aka ' + ','.join(aliases) + ')' if aliases else '')
            cells.append(str(value))
        cells += [str(r.get('running', 0)), str(r.get('queued', 0)), str(r.get('stalled', 0)), str(r.get('total', 0))]
        rows.append(cells)
    headers = [d.upper() for d in dims] + ['RUNNING', 'QUEUED', 'STALLED', 'TOTAL']
    footer = ['TOTAL'] + [''] * (len(dims) - 1) + [str(totals.get(k, 0)) for k in ('running', 'queued', 'stalled', 'total')]
    widths = [max(len(str(x[i])) for x in [headers, footer] + rows) for i in range(len(headers))]
    line = lambda cells: '  '.join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    out = [line(headers), '  '.join('-' * w for w in widths)]
    out += [line(r) for r in rows]
    out += ['  '.join('-' * w for w in widths), line(footer)]
    if result.get('truncated'):
        out.append('note: truncated at %s live sessions' % result.get('scanned'))
    return '\n'.join(out)


def render_table(sessions):
    """Render a compact table for `hub sessions list` with columns:
    TYPE, NAME, HOST, ENGINE, ACCOUNT, MODEL, EFFORT, STATE, WORKSPACE, UPDATED and runtime warnings.
    """
    if not sessions:
        return 'No sessions found.'
    headers = ['TYPE', 'NAME', 'HOST', 'ENGINE', 'ACCOUNT', 'MODEL', 'EFFORT', 'STATE', 'WORKSPACE', 'UPDATED', 'NATIVE', 'LONG_RUNNING', 'RUNTIME', 'IDLE_FOR', 'ACTIVITY']
    rows = []
    for s in sessions:
        # 3.2: Hub2 projections use machine_id/account_id/interactive and
        # numeric epoch timestamps; Hub1 names stay as fallbacks so one CLI
        # renders both server generations.
        mode = s.get('mode') or ('interactive' if s.get('interactive') is True else 'headless')
        stype = s.get('session_type') or ('aplexer' if mode == 'interactive' else 'headless')
        name = s.get('qualified_alias') or s.get('name') or s.get('id') or '-'
        engine = s.get('engine') or '-'
        state = s.get('state') or '-'
        if s.get('waiting_until'):
            state = 'waiting_until ' + str(s['waiting_until'])
        elif s.get('waiting_confirmation') or s.get('agent_state') == 'waiting':
            state = 'waiting-conf'
        elif s.get('agent_state') == 'idle' and state == 'running':
            state = 'idle'
        ws = s.get('workspace') or '-'
        upd = _format_timestamp(s.get('updated_at') or s.get('last_activity_at'))
        host = s.get('machine') or s.get('machine_id') or '-'
        account = s.get('account') or s.get('account_id') or '-'
        # Native pane identity: Hub1 binds carry native_tag, Hub2 projections
        # carry native_id; both are the aplexer session id for `a capture`/send.
        native = s.get('native_tag') or s.get('native_id') or '-'
        rows.append([stype, name, host, engine, account, s.get('model_id') or s.get('model') or '-', s.get('effort') or '-', state, ws, upd, native, str(s.get('long_running', False)).lower(),
                     format_duration(s.get('runtime')), format_duration(s.get('idle_for')), s.get('activity_state', '-')])

    widths = [len(h) for h in headers]
    for row in rows:
        for i, col in enumerate(row):
            widths[i] = max(widths[i], len(str(col)))

    fmt = '  '.join('%-' + str(w) + 's' for w in widths)
    lines = [fmt % tuple(headers)]
    lines.append('  '.join('-' * w for w in widths))
    for row in rows:
        lines.append(fmt % tuple(row))
    return '\n'.join(lines)


def render_tree(tree_data):
    """Render an ASCII lineage tree for `hub sessions tree`."""
    nodes = tree_data.get('tree', []) if isinstance(tree_data, dict) else (tree_data if isinstance(tree_data, list) else [])
    if not nodes:
        return 'No session hierarchy found.'
    lines = ['Sessions Lineage Tree:']

    def _format_node(node):
        stype = node.get('session_type') or ('aplexer' if node.get('mode') == 'interactive' else 'headless')
        name = node.get('qualified_alias') or node.get('name') or node.get('id')
        state = node.get('state') or '-'
        ws = node.get('workspace') or ''
        ws_part = ' (%s)' % ws if ws else ''
        return '[%s] %s (%s)%s' % (stype, name, state, ws_part)

    def _walk(items, prefix=''):
        for i, item in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = '└── ' if is_last else '├── '
            lines.append(prefix + connector + _format_node(item))
            children = item.get('children', [])
            if children:
                ext = '    ' if is_last else '│   '
                _walk(children, prefix + ext)

    _walk(nodes)
    return '\n'.join(lines)


def render_commands(result):
    commands = result.get('commands', []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
    if not commands:
        return 'No commands found.'
    headers = ['ID', 'KIND', 'STATE', 'REASON', 'CREATED']
    rows = []
    for c in commands:
        cid = str(c.get('id', ''))[:8]
        kind = str(c.get('kind', ''))
        state = str(c.get('state', ''))
        reason = str(c.get('reason') or c.get('host_reason') or (c.get('ack') or {}).get('reason') or '')
        created = str(c.get('created_at', ''))[:19]
        rows.append([cid, kind, state, reason[:200], created])
    col_widths = [max(len(r[i]) for r in [headers] + rows) for i in range(len(headers))]
    lines = ['  '.join(h.ljust(col_widths[i]) for i, h in enumerate(headers))]
    lines.append('  '.join('-' * col_widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append('  '.join(row[i].ljust(col_widths[i]) for i in range(len(headers))))
    return '\n'.join(lines)


def render_audit(result):
    """Compact action log: per-run action counts plus one line per decision."""
    lines = []
    info = result.get('audit') or {}
    lines.append(f"audit: state={info.get('state')} last_run={info.get('last_run_at')} threshold={info.get('threshold_s')}s")
    for run in result.get('runs', []):
        counts = run.get('counts') or {}
        summary = ' '.join(f"{k}={v}" for k, v in sorted(counts.items()) if v)
        lines.append(f"run {run.get('id')}  {run.get('at')}  {run.get('source')}  scanned={run.get('scanned')}  {summary}")
    for d in result.get('decisions', []):
        outcome = f"  outcome={d['outcome']}" if d.get('outcome') else ''
        target = d.get('session_id') or (f"native {d.get('native_id')}" if d.get('native_id') else '-')
        lines.append(f"  [{d.get('action')}] {d.get('rule')}  {target}  idle={d.get('idle_s')}s{outcome}")
        if d.get('detail'):
            lines.append(f"      {d['detail']}")
    return '\n'.join(lines)


def render_output(args, result):
    """Dispatch text table/tree vs raw machine JSON."""
    if getattr(args, 'json', False):
        return result
    cmd = getattr(args, 'sessions_cmd', None)
    if cmd == 'tree' or (cmd == 'list' and getattr(args, 'tree', False)):
        return render_tree(result)
    if cmd == 'count':
        return render_breakdown(result)
    if cmd == 'reap':
        mode = 'dry run' if result.get('dry_run') else 'applied'
        return (f"Reaper {mode}: {result.get('reaped', 0)} sessions stopped; "
                f"live active count {result.get('active_count_before', '?')} -> {result.get('active_count_after', '?')}; "
                f"stop requests pending {result.get('stop_requested', 0)}.")
    if cmd == 'list':
        sessions = result.get('sessions', []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
        text = render_table(sessions)
        if isinstance(result, dict) and 'active_count' in result:
            text += f"\nActive sessions: {result['active_count']}"
        return text
    if cmd == 'pick':
        lines = [f"Picked route: {result.get('route')} (policy {result.get('policy_id')}, host {result.get('machine_id')})"]
        lines += [f"  pool  {row['policy_id']:<24} {row['share'] * 100:5.1f}%" for row in result.get('pool', [])]
        lines += [f"  EXCLUDED {row['policy_id']} ({row['reason']})" for row in result.get('excluded', [])]
        return '\n'.join(lines)
    if cmd == 'start' and isinstance(result, dict):
        # 2.4/native-tag: once bound, name the native pane (Hub1 native_tag /
        # Hub2 native_id) so `a capture`/`a send` have the exact target.
        binding = result.get('binding_wait') if isinstance(result.get('binding_wait'), dict) else None
        bound = binding.get('session') if isinstance(binding, dict) else None
        bound = bound if isinstance(bound, dict) else binding
        native = None
        if isinstance(bound, dict):
            native = bound.get('native_tag') or bound.get('native_id')
        if native:
            print(f"Native pane: {native}", file=sys.stderr)
    if cmd == 'start' and isinstance(result, dict) and (result.get('route') or result.get('engine') or result.get('model')):
        route = result.get('route') or '/'.join([
            str(result.get('engine') or '-'), str(result.get('model') or '-'),
            str(result.get('effort') or 'default')])
        line = f"Picked route: {route}"
        if result.get('policy_id'):
            line += f" (policy {result['policy_id']}"
            if result.get('machine_id'):
                line += f", host {result['machine_id']}"
            line += ')'
        print(line, file=sys.stderr)
        picker = result.get('picker') if isinstance(result.get('picker'), dict) else {}
        pool = picker.get('pool') if isinstance(picker.get('pool'), list) else []
        for row in pool:
            if isinstance(row, dict) and row.get('policy_id'):
                share = row.get('share')
                share_text = f"{share * 100:5.1f}%" if isinstance(share, (int, float)) else '    - %'
                print(f"  pool  {str(row['policy_id']):<24} {share_text}", file=sys.stderr)
        return result
    if cmd == 'warnings':
        lines = []
        for row in result.get('warnings', []):
            lines.extend([
                f"{row['id']}  {row.get('qualified_name') or row.get('name') or '-'}  engine={row.get('engine') or '-'}",
                f"  intake={row.get('intake_id') or '-'}  runtime={format_duration(row.get('runtime'))}  last_activity={row.get('last_activity_at')}",
                f"  idle_for={format_duration(row.get('idle_for'))}  long and {row.get('activity_state')}",
                *['  | ' + line for line in row.get('output_tail', [])],
                '  output: ' + row.get('output_status', 'unavailable'),
                '  ' + row['snooze_command'],
            ])
        for alert in result.get('wake_alerts', []):
            lines.extend([
                f"{alert.get('session_id')}  missed wake {str(alert.get('wake_id') or '')[:8]}  {alert.get('kind')}",
                f"  detail={alert.get('detail') or '-'}  intake={alert.get('intake_task_id') or '-'}",
            ])
        return '\n'.join(lines) or 'No long-running warnings.'
    if cmd == 'commands':
        if getattr(args, 'table', False) or (hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()):
            return render_commands(result)
        return result
    if cmd == 'audit':
        if 'exported_at' in result:
            return result  # raw dataset export (AC3): --export always yields the full rows
        return render_audit(result)
    return result

