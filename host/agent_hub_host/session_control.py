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
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from . import api, auth, config

MAX_REQUEST = 32768
MAX_BRIEF = 8192
MAX_BRIEF_CHARS = 8000
MAX_BODY = 16384
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TIERS = ('quick', 'daily', 'deep')
WAIT_DEFAULT_TIMEOUT = 300.0
WAIT_MAX_TIMEOUT = 600.0
WAIT_MIN_INTERVAL = 1.0
CAPTURE_TIMEOUT = 30.0
_request_deadline = contextvars.ContextVar('session_request_deadline', default=None)
# Terminal command states: only these confirm an outcome. `transported` alone
# is fetch/PTY bytes, never processing proof; waiting never treats it as done.
TERMINAL_COMMAND = ('acked', 'refused', 'uncertain', 'failed')
TERMINAL_SESSION = ('running', 'stopped', 'failed', 'refused', 'held', 'uncertain', 'cancelled')

SAFE_ERROR = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
# Match the normal JSON response bound in api.safe_http_json. A held-session
# DTO can exceed 4 KiB when it includes per-route memory/admission evidence.
MAX_ERROR_RESPONSE = 10 * 1024 * 1024


class SessionHTTPError(ValueError):
    def __init__(self, status, code='request_failed', message=None):
        self.status = status
        self.code = code
        super().__init__(message or f'Session HTTP request failed with status {status} ({code})')


class SessionTransportError(ValueError):
    pass


class SessionInputError(ValueError):
    pass


def diagnostic(exc):
    """Never interpolate exception bodies: they may contain credentials or payloads."""
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
    return 'Session operation failed; check authentication, grants, capacity and current versions.'


def http_error(exc, key=None):
    # Surface the server error code (top-level or inside a 409 hold
    # payload) plus the held session id, so the receiver can classify
    # terminal vs retryable failures without a follow-up call.
    # Best-effort: an unreadable body keeps the status-only form.
    name = ''
    code = 'request_failed'
    field = None
    detail = None
    reason = None
    routes = ''
    held_info = ''
    provider_guidance = ''
    concurrency_guidance = ''
    try:
        raw = exc.read(MAX_ERROR_RESPONSE + 1) if hasattr(exc, 'read') else b''
        if len(raw) > MAX_ERROR_RESPONSE:
            code = reason = 'response_too_large'
            name = ' (response_too_large)'
            data = {}
        else:
            data = json.loads((raw or b'').decode('utf-8', 'replace'))
        hold = data.get('hold') if isinstance(data, dict) else None
        err = data.get('error') if isinstance(data, dict) else None
        if (not isinstance(err, str) or not err) and isinstance(hold, dict):
            err = hold.get('error')
        elif (not isinstance(err, str) or not err) and isinstance(hold, str):
            err = hold
        if isinstance(err, str) and err and SAFE_ERROR.match(err):
            code = err
            name = ' (%s)' % err
            if err == 'unknown_workspace' and isinstance(data.get('matches'), list):
                matches = [m for m in data['matches'][:5] if isinstance(m, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', m)]
                if matches:
                    name += '; close matches: ' + ', '.join(matches)
            if err == 'ambiguous_session_prefix' and isinstance(data.get('matches'), list):
                ids = [row.get('id') for row in data['matches'] if isinstance(row, dict)
                       and isinstance(row.get('id'), str) and re.fullmatch(r'[a-fA-F0-9-]{36}', row['id'])]
                if ids:
                    name += ' matches: ' + ', '.join(ids[:100])
            if err == 'provider_disabled':
                provider_guidance = ' requested provider disabled by model policy; choose an enabled engine/model or ask the policy owner to enable it and register a fresh host route with verified quota'
        rejection = hold if isinstance(hold, dict) else data
        if isinstance(rejection, dict) and isinstance(rejection.get('rejected'), list):
            reasons = sorted({r['reason'] for r in rejection['rejected'][:100]
                              if isinstance(r, dict) and isinstance(r.get('reason'), str)
                              and re.fullmatch(r'[a-z][a-z_]{0,63}', r['reason'])})
            if reasons:
                routes = ' routes: ' + ', '.join(reasons)
                if 'account_concurrency' in reasons:
                    concurrency_guidance = '; configured account concurrency limit reached; quota is healthy'
        if isinstance(hold, dict) and isinstance(data.get('id'), str) and data.get('id'):
            held_info = ' held session %s state %s' % (data['id'], data.get('state'))
        if isinstance(data, dict):
            f = data.get('field')
            if isinstance(f, str) and SAFE_ERROR.match(f):
                field = f
            d = data.get('detail')
            if isinstance(d, str) and SAFE_ERROR.match(d):
                detail = d
            r = data.get('reason')
            if isinstance(r, str) and SAFE_ERROR.match(r):
                reason = r
    except Exception:
        name = ''

    parts = [f'Session HTTP request failed with status {exc.code}{name}']
    if provider_guidance:
        parts.append(provider_guidance)
    if routes:
        parts.append(routes)
    if concurrency_guidance:
        parts.append(concurrency_guidance)
    if held_info:
        parts.append(held_info)
    if reason:
        parts.append(f'; reason: {reason}')
    if field:
        parts.append(f'; field: {field}')
    if detail:
        parts.append(f'; detail: {detail}')
    full_message = ''.join(parts)
    if key and f'reconcile with --key {key}' not in full_message:
        full_message += f' (reconcile with --key {key} via show/commands)'
    return SessionHTTPError(exc.code, code=code, message=full_message)


def request(method, path, payload=None):
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    if data is not None and len(data) > MAX_REQUEST:
        raise SessionInputError('Session request exceeds capacity')
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or auth.get_access_token()
    payload_key = payload.get('key') if isinstance(payload, dict) else None

    def send(access):
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
        if exc.code == 401 and not env_token and _request_deadline.get() is None:
            try:
                auth.refresh_token(expected_token=token) if cfg.get('worker_launch_id') else auth.refresh_token()
                return send(auth.get_access_token())
            except urllib.error.HTTPError as retry:
                raise http_error(retry, key=payload_key) from None
            except Exception as refresh_exc:
                raise SessionTransportError(diagnostic(refresh_exc)) from None
        raise http_error(exc, key=payload_key) from None


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


def _fetch_command_entry(session_id, command_id):
    """Fetch ONE command entry (bounded cursor walk, no polling).

    Raises ValueError when the command is missing from the server listing;
    callers reconcile via commands listing instead of blind-retrying.
    """
    from urllib.parse import quote
    cursor = None
    for _ in range(10):
        path = '/api/sessions/' + session_id + '/commands?limit=100'
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
    deadline = time.monotonic() + timeout
    return wait_until(deadline, interval,
                     lambda: request('GET', '/api/sessions/' + session_id),
                     lambda view: view.get('state') in (('stopped', 'failed', 'refused', 'held', 'uncertain') if stopping else TERMINAL_SESSION),
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
    root = sub.add_parser('sessions', help='Hub session orchestration: grouped lifecycle listing by default; list/start/send/capture/stop/cleanup/observe')
    commands = root.add_subparsers(dest='sessions_cmd', required=False)
    listing = commands.add_parser('list', help='List sessions visible to this credential')
    listing.add_argument('--threshold', help='Long-running threshold (default 3h; HUB_SESSION_WARNING_THRESHOLD)')
    listing.add_argument('--state')
    listing.add_argument('--limit', type=int, default=50)
    listing.add_argument('--json', action='store_true', help='Output machine-readable JSON')
    listing.add_argument('--tree', action='store_true', help='Display session lineage tree')
    tree = commands.add_parser('tree', help='Display session lineage tree')
    tree.add_argument('--json', action='store_true', help='Output machine-readable JSON')
    start = commands.add_parser('start', help='Record a start intent using auto/policy routing (default). Casual use should prefer auto/policy.')
    start.add_argument('--engine', help='Engine override (operators only; requires --reason explaining why)')
    start.add_argument('--reason', default=None, help='Explanation why this specific model/engine must be chosen (required when --engine is specified)')
    start.add_argument('--machine')
    start.add_argument('--needs', action='append', default=None,
                       help='Label filter (repeatable, e.g. --needs os:windows --needs arch:arm64); eligible machines must satisfy all needs, else held with rejected[] reason needs_unmet')
    start.add_argument('--workspace', required=True)
    start.add_argument('--task-kind', choices=['implement','review','ask','goal'], help='Task kind for measured resource placement and session statistics')
    start.add_argument('--tier', choices=TIERS, help='deep/daily/quick task tier for policy routing')
    start.add_argument('--cwd')
    start.add_argument('--name', default=None,
                       help='Friendly session name (stored, shown in list/show; exact-match alias, never identity)')
    start.add_argument('--parent', default=None,
                       help='Parent session id recording lineage')
    start.add_argument('--intake-task-id', dest='intake_task_id', default=None,
                       help='Linked intake task id (API field intake_task_id); refused when that task has empty acceptance criteria.')
    start.add_argument('--with-goal', action='store_true', help='Start interactive /goal work on a Codex-family engine (for sessions expecting follow-ups); requires --prompt or --brief-file; refuses --headless')
    start.add_argument('--headless', action='store_true', help='Explicitly start in one-shot headless mode (no input channel; follow-ups must use codex exec resume)')
    mode = start.add_mutually_exclusive_group()
    mode.add_argument('--brief-file', help='UTF-8 goal file for headless work (max 8 KiB)')
    mode.add_argument('--prompt', default=None, help='Inline UTF-8 goal text for headless work (max 8 KiB; same boundaries as --brief-file)')
    mode.add_argument('--interactive', action='store_true', help='Start at an empty native prompt')
    start.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(start)
    override = commands.add_parser('override', help='Dedicated command for engine override. REQUIRES --engine and --reason.')
    override.add_argument('--engine', required=True, help='Specific model/engine to run')
    override.add_argument('--reason', required=True, help='Explanation why this specific model/engine must be chosen')
    override.add_argument('--machine')
    override.add_argument('--needs', action='append', default=None,
                          help='Label filter (repeatable, e.g. --needs os:windows --needs arch:arm64)')
    override.add_argument('--workspace', required=True)
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
    add_wait(override)
    send = commands.add_parser('send', help='Enqueue one prompt delivery on a bound session')
    send.add_argument('id')
    body_src = send.add_mutually_exclusive_group(required=True)
    body_src.add_argument('--body-file', help='UTF-8 prompt file, or - for stdin (max 16 KiB)')
    body_src.add_argument('--prompt', default=None,
                          help='Inline UTF-8 prompt text (max 16 KiB; same boundaries as --body-file)')
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
    stop = commands.add_parser('stop', help='Enqueue stop; the hub releases capacity only on confirmed native exit')
    stop.add_argument('id')
    stop.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(stop, timeout=10.0, interval=1.0)
    stop.set_defaults(wait=True)
    stop.add_argument('--no-wait', dest='wait', action='store_false', help='Record the stop intent without waiting for verified exit')
    cancel = commands.add_parser('cancel', help='Cancel an owned held, queued or unclaimed offered intent before native start')
    cancel.add_argument('id')
    cancel.add_argument('--key', required=True, help='Stable cancellation key, reused exactly after a lost response')
    protect = commands.add_parser('protect', help='Permanently protect an owned session from automated cleanup; never stops it')
    protect.add_argument('id')
    protect.add_argument('--key', required=True, help='Stable protection key; this operation has no unprotect counterpart')
    show = commands.add_parser('show', help='Show one session with its binding state')
    show.add_argument('id')
    show.add_argument('--threshold')
    show.add_argument('--json', action='store_true')
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
    if cmd == 'tree' or (cmd == 'list' and getattr(args, 'tree', False)):
        return request('GET', '/api/sessions/tree')
    if cmd == 'list':
        wanted = getattr(args, 'state', None)
        limit = int(getattr(args, 'limit', None) or 50)
        if not wanted:
            params = ''
            if getattr(args, 'limit', None):
                params = '?limit=%d' % limit
            return request('GET', threshold_path(args, '/api/sessions' + params))
        # State-filtered listing follows next_cursor (bounded, id-deduped) so
        # filtering is complete instead of first-page-only.
        from urllib.parse import quote
        seen: list = []
        known: set = set()
        cursor = None
        for _ in range(10):
            path = '/api/sessions?limit=%d' % limit
            if cursor:
                path += '&cursor=' + quote(cursor, safe='')
            page = request('GET', threshold_path(args, path))
            for entry in page.get('sessions', []):
                if entry.get('id') in known:
                    continue
                known.add(entry.get('id'))
                if match_session_state(entry, wanted):
                    seen.append(entry)
                    if len(seen) >= limit:
                        return {'sessions': seen[:limit], 'next_cursor': None}
            cursor = page.get('next_cursor')
            if not cursor:
                break
        return {'sessions': seen[:limit], 'next_cursor': None}
    if cmd == 'show':
        return request('GET', threshold_path(args, '/api/sessions/' + args.id))
    if cmd == 'events':
        return request('GET', '/api/sessions/' + args.id + '/events')
    if cmd == 'ack':
        payload = {'snooze': duration_seconds(args.snooze)} if args.snooze else {}
        return request('POST', threshold_path(args, '/api/sessions/' + args.id + '/ack'), payload)
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
        return request('GET', '/api/sessions/' + args.id + '/commands')
    if cmd in ('start', 'override'):
        engine = getattr(args, 'engine', None)
        reason = getattr(args, 'reason', None)
        if cmd == 'override':
            if not engine:
                raise ValueError('--engine is required for dedicated engine override command')
            if not reason or not reason.strip():
                raise ValueError('Dedicated engine override requires an explanation why this specific model/engine must be chosen (use --reason)')
        elif cmd == 'start' and engine:
            if not reason or not reason.strip():
                raise ValueError('Engine override requires an explanation why this specific model/engine must be chosen (use --reason). Casual use should prefer auto/policy without --engine.')
        key = getattr(args, 'key', None) or str(uuid.uuid4())
        payload = {'key': key, 'workspace': args.workspace}
        for field in ('engine', 'machine', 'tier', 'cwd'):
            value = getattr(args, field, None)
            if value:
                payload[field] = value
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
        created = dict(created, client_key=key)
        timeout, interval = check_wait_args(args)
        if timeout is not None:
            created['binding_wait'] = wait_for_session(created['id'], timeout, interval)
        return created
    if cmd in ('cancel', 'protect'):
        from urllib.parse import quote
        if not isinstance(args.id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', args.id):
            raise ValueError('Invalid session id or name')
        if not isinstance(args.key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.key):
            raise ValueError('Invalid protection key' if cmd == 'protect' else 'Invalid cancellation key')
        return request('POST', '/api/sessions/' + quote(args.id, safe='') + '/' + cmd, {'key': args.key})
    if cmd in ('send', 'capture', 'stop'):
        timeout, interval = check_wait_args(args)
        key = getattr(args, 'key', None) or str(uuid.uuid4())
        if cmd == 'send':
            if getattr(args, 'prompt', None) is not None and getattr(args, 'body_file', None):
                raise SessionInputError('Cannot specify both --body-file and --prompt')
            if getattr(args, 'prompt', None) is not None:
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
            queued = request('POST', '/api/sessions/' + args.id + '/' + cmd, payload)
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
                    reason = 'host_unreachable' if isinstance(exc, SessionTransportError) and time.monotonic() < _request_deadline.get() else 'capture_not_acked'
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
                            cleanup_roots=cleanup_roots)
    raise ValueError('Unknown sessions command')


def format_duration(seconds):
    if not isinstance(seconds, (int, float)):
        return '-'
    seconds = max(0, int(seconds))
    return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m{seconds % 60:02d}s'


def render_table(sessions):
    """Render a compact table for `hub sessions list` with columns:
    TYPE, NAME, ENGINE, MODEL, STATE, WORKSPACE, UPDATED and runtime warnings.
    """
    if not sessions:
        return 'No sessions found.'
    headers = ['TYPE', 'NAME', 'ENGINE', 'MODEL', 'STATE', 'WORKSPACE', 'UPDATED', 'LONG_RUNNING', 'RUNTIME', 'IDLE_FOR', 'ACTIVITY']
    rows = []
    for s in sessions:
        stype = s.get('session_type') or ('aplexer' if s.get('mode') == 'interactive' else 'headless')
        name = s.get('qualified_alias') or s.get('name') or s.get('id') or '-'
        engine = s.get('engine') or '-'
        state = s.get('state') or '-'
        if s.get('waiting_confirmation') or s.get('agent_state') == 'waiting':
            state = 'waiting-conf'
        elif s.get('agent_state') == 'idle' and state == 'running':
            state = 'idle'
        ws = s.get('workspace') or '-'
        upd = (s.get('updated_at') or '')[:19].replace('T', ' ')
        rows.append([stype, name, engine, s.get('model_id') or s.get('model') or '-', state, ws, upd, str(s.get('long_running', False)).lower(),
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
    if cmd == 'list':
        sessions = result.get('sessions', []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
        return render_table(sessions)
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

