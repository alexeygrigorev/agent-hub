"""Private durable task context. Task text is data; this client never executes it."""
import argparse
import socket
import json
import os
import re
import sys
from urllib.parse import urlencode

BASE = '/api/intake'
MAX_REQUEST = 32768
EVENT_KINDS = ('intake', 'note', 'decision', 'followup', 'blocker', 'pending_approval', 'scope_decision',
               'link', 'session', 'report', 'commit', 'rule_reference', 'resolution',
               'approval_decision', 'owner_acceptance', 'first_action', 'work_action',
               'independent_review', 'accepted_outcome', 'human_delivery', 'obligation', 'obligation_response')


def __getattr__(name):
    """Load transport and helper modules on access while preserving the module API."""
    if name in ('api', 'auth', 'config'):
        module = __import__(f'{__package__}.{name}', fromlist=[name])
        globals()[name] = module
        return module
    if name == 'uuid':
        import uuid as module
        globals()['uuid'] = module
        return module
    if name == 'Path':
        from pathlib import Path as cls
        globals()['Path'] = cls
        return cls
    if name == 'urllib':
        import urllib.error
        import urllib.request
        globals()['urllib'] = urllib
        return urllib
    if name == 'http':
        import http.client
        globals()['http'] = http
        return http
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Server error slugs are lowercase snake_case (longest today is 30 chars).
# Passing the slug through keeps the exact failure (auth, grant, capacity,
# field) while tokens, URLs, hex digests and terminal controls can never
# match and still collapse to request_failed.
SAFE_ERROR = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


class StaleSnapshot(ValueError):
    pass


class IntakeHTTPError(ValueError):
    def __init__(self, status, code='request_failed', key=None, gate=None):
        self.status, self.code = status, code
        # Sanitized worktree close-gate refusal (409 worktree_close_gate) or None.
        self.gate = gate
        super().__init__(f'Intake HTTP {status}: {code}' + (f'; retry key: {key}' if key else ''))


class IntakeTransportError(ValueError):
    pass


class IntakeResponseTooLarge(IntakeTransportError):
    """A bounded response was refused before returning any page items."""


class IntakeInputError(ValueError):
    """A client diagnostic containing only fixed labels and limits."""


def _random_hex():
    import uuid
    return uuid.uuid4().hex


def valid_key(value):
    # Match the server identifier exactly; rewriting keys breaks idempotency.
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value):
        raise IntakeInputError('Invalid retry key: use 1-128 characters, starting with a letter or digit; allowed: letters, digits, . _ : -')


def diagnostic(exc):
    """Never interpolate exception bodies: they may contain credentials or payloads."""
    from .worker_auth import WorkerContextError
    if isinstance(exc, WorkerContextError):
        return str(exc)
    import http.client
    import urllib.error
    from .config import ConfigPermissionError

    if isinstance(exc, (IntakeHTTPError, IntakeTransportError, IntakeInputError, ConfigPermissionError)):
        return str(exc)
    if isinstance(exc, StaleSnapshot):
        return 'Snapshot changed; retry the command to restart pagination'
    if isinstance(exc, UnicodeDecodeError):
        return 'Input is not valid UTF-8'
    if isinstance(exc, json.JSONDecodeError):
        return 'Invalid JSON input: expected a JSON object'
    if isinstance(exc, (TimeoutError, socket.timeout)) or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)):
        return 'Intake request timed out (HTTP timeout: 10 seconds)'
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, socket.gaierror):
            return 'Intake transport failed: DNS lookup failed'
        return 'Intake transport failed: connection or TLS failure'
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return 'Intake transport failed: connection interrupted'
    if isinstance(exc, RuntimeError) and str(exc) in ('Expected JSON response', 'Response too large'):
        return 'Intake response error: ' + str(exc)
    return 'Intake client error; check inputs and authentication (unexpected error details withheld)'


GATE_FIELDS = ('worktree_id', 'machine', 'path', 'branch')


def _bounded_text(value, limit=1024):
    return value[:limit] if isinstance(value, str) else None


def merge_gate_refusal(body):
    """Sanitized merge-gate refusal: the problems and the unmerged commit ids."""
    problems = body.get('problems') if isinstance(body.get('problems'), list) else []
    commits = body.get('unmerged_commits') if isinstance(body.get('unmerged_commits'), list) else []
    result = {'error': 'merge_gate', 'state': _bounded_text(body.get('state'), 32),
              'problems': [_bounded_text(p, 300) for p in problems[:32] if isinstance(p, str)],
              'unmerged_commits': [c[:40] for c in commits[:64] if isinstance(c, str) and re.fullmatch(r'[0-9a-f]{7,40}', c)]}
    hint = _bounded_text(body.get('hint'), 512)
    if hint:
        result['hint'] = hint
    return result


def close_gate_refusal(body):
    """Keep only the documented gate fields; json.dumps escapes controls."""
    worktrees = body.get('worktrees') if isinstance(body.get('worktrees'), list) else []
    rows = []
    for row in worktrees[:64]:
        if not isinstance(row, dict):
            continue
        item = {field: _bounded_text(row.get(field)) for field in GATE_FIELDS}
        problems = row.get('problems') if isinstance(row.get('problems'), list) else []
        item['problems'] = [_bounded_text(p, 256) for p in problems[:32] if isinstance(p, str)]
        rows.append(item)
    result = {'error': 'worktree_close_gate', 'worktrees': rows}
    hint = _bounded_text(body.get('hint'), 512)
    if hint:
        result['hint'] = hint
    return result


def http_error(exc, method, payload):
    if exc.code == 409 and method == 'GET':
        return StaleSnapshot('Snapshot changed')
    if exc.code == 410 and method == 'GET':
        # Pinned listing token expired or was evicted; the walk restarts from page one.
        return StaleSnapshot('Listing snapshot expired')
    # Never echo arbitrary error bodies, tokens, URLs or terminal controls.
    # The server only emits lowercase snake_case slugs; surface the exact
    # slug so auth/grant/capacity/field detail survives, collapse the rest.
    body = None
    try:
        body = json.loads(exc.read(65537))
        code = body.get('error') if isinstance(body, dict) else None
    except Exception:
        code = None
    if not isinstance(code, str) or not SAFE_ERROR.match(code):
        code = 'request_failed'
    key = payload.get('key') if payload else None
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', key):
        key = None
    gate = close_gate_refusal(body) if code == 'worktree_close_gate' and isinstance(body, dict) else None
    if code == 'merge_gate' and isinstance(body, dict):
        gate = merge_gate_refusal(body)
    result = IntakeHTTPError(exc.code, code, key, gate)
    detail = ''
    if isinstance(body, dict):
        stage = body.get('stage')
        if isinstance(stage, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', stage):
            detail += f'; stage: {stage}'
        server_detail = body.get('detail')
        if isinstance(server_detail, str) and server_detail.strip():
            safe_detail = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', server_detail)
            detail += '; detail: ' + ' '.join(safe_detail.split())[:300]
        reason = body.get('reason')
        if isinstance(reason, str) and SAFE_ERROR.match(reason):
            detail += f'; reason: {reason}'
        field = body.get('field')
        if field in ('text', 'title', 'project', 'acceptance_criteria', 'current_state', 'next_step', 'owner', 'uncertainty'):
            detail += f'; field: {field}'
            limit = body.get('max_bytes')
            if type(limit) is int and 0 < limit <= MAX_REQUEST:
                detail += f'; max {limit} UTF-8 bytes'
    if code in ('invalid_limit', 'limit_out_of_range'):
        detail += '; limit must be a positive integer; min 1, max 50 (larger values clamp)'
    if code == 'invalid_search':
        detail += '; search must be 1-128 characters'
    if code == 'done_evidence_required':
        detail += '; append a DONE note with commit SHA or deploy run URL, Live check: command/URL, and Result: output before closing'
    if code == 'cancel_reason_required':
        detail += '; provide --reason and --superseded-by, or reason containing dropped by Alexey'
    if code == 'worktree_close_gate':
        detail += '; worktrees block close: hub worktree refresh <intake> | hub worktree remove <intake> | --force --reason TEXT'
    if code == 'merge_gate':
        detail += '; commits must be in origin/main with a green Deploy: merge, wait for CI, then hub intake merge-check <intake> (operator override: --force --reason TEXT)'
    if code == 'invalid_event_kind':
        detail += '; valid kinds: ' + ', '.join(EVENT_KINDS)
    if code == 'worker_event_forbidden':
        detail += '; workers may append only note, report, or blocker events; pass JSON like {"kind":"note","text":"..."} to --event-file (use - for stdin)'
    # Only locally generated wording is printed, never arbitrary server messages.
    result.args = (str(result) + detail,)
    return result


from .summary_fast import (
    _fast_load_posix_config,
    _fast_summary_get,
    _parse_https_base,
    _ssl_context,
    _summary_tls_pool,
)
_https_conn_pool = {}


def _https_get_json(base, path, access):
    """Keep-alive HTTPS GET with capath SSLContext and gzip decompression for multi-page export."""
    import http.client
    import io
    import urllib.error
    from urllib.parse import urlparse
    from . import _prod_guard
    full_url = base.rstrip('/') + path
    _prod_guard.assert_request_allowed(full_url)
    parsed = urlparse(base.rstrip('/'))
    host = parsed.hostname
    port = int(parsed.port) if parsed.port is not None else 443
    key = (host, port)
    headers = {
        'Authorization': 'Bearer ' + access,
        'Content-Type': 'application/json',
        'User-Agent': 'agent-hub-host/1',
        'Connection': 'keep-alive',
    }
    if path.startswith(BASE + '/tasks'):
        headers['Accept-Encoding'] = 'gzip'
    for attempt in range(2):
        conn = _https_conn_pool.pop(key, None)
        if conn is not None and getattr(conn, 'sock', None) is None:
            conn.close()
            conn = None
        fresh = conn is None
        if fresh:
            conn = http.client.HTTPSConnection(host, port, timeout=10.0, context=_ssl_context())
        try:
            conn.request('GET', (parsed.path or '') + path, headers=headers)
            res = conn.getresponse()
            break
        except Exception as err:
            conn.close()
            if not fresh and attempt == 0:
                continue
            if isinstance(err, OSError):
                raise urllib.error.URLError(err)
            raise
    if res.status != 200:
        err_body = res.read(65537)
        conn.close()
        raise urllib.error.HTTPError(full_url, res.status, res.reason, res.headers, io.BytesIO(err_body))
    ct = res.headers.get('Content-Type', '')
    if 'application/json' not in ct:
        conn.close()
        raise RuntimeError('Expected JSON response')
    cl = res.headers.get('Content-Length')
    if cl is not None and int(cl) > 10 * 1024 * 1024:
        conn.close()
        raise RuntimeError('Response too large')
    body = res.read(10 * 1024 * 1024 + 1)
    if len(body) > 10 * 1024 * 1024:
        conn.close()
        raise RuntimeError('Response too large')
    if not getattr(res, 'will_close', True) and res.isclosed() and getattr(conn, 'sock', None) is not None:
        _https_conn_pool[key] = conn
    else:
        conn.close()
    if (res.headers.get('Content-Encoding') or '').lower() == 'gzip':
        import gzip
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
            body = gz.read(10 * 1024 * 1024 + 1)
        if len(body) > 10 * 1024 * 1024:
            raise RuntimeError('Response too large')
    return json.loads(body.decode('utf-8'))


def request(method, path, payload=None):
    if method == 'GET' and payload is None and path.startswith(BASE + '/summary') and 'api' not in globals():
        fast = _fast_summary_get(path)
        if fast is not None:
            return fast
    import http.client
    import urllib.error
    import urllib.request
    from . import _test_isolation, api, auth, config

    from .worker_auth import validate_v2_request
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    if data is not None and len(data) > MAX_REQUEST:
        raise IntakeInputError(f'Intake request exceeds {MAX_REQUEST} UTF-8 bytes (JSON envelope included)')
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or cfg.get('access_token') or auth.get_access_token()
    # GETs are safe to repeat. Task writes are safe only when the exact
    # idempotency key and serialized payload can be replayed; the Hub stores
    # their response receipt in the same transaction as the write.
    retry_key = None
    if method == 'POST' and isinstance(payload, dict) and (
            path == BASE + '/tasks' or path.startswith(BASE + '/tasks/')):
        candidate = payload.get('key')
        if isinstance(candidate, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', candidate):
            retry_key = candidate
    max_attempts = 2 if method == 'GET' or retry_key else 1

    def send(access):
        validate_v2_request(cfg)
        try:
            if (
                method == 'GET'
                and data is None
                and base.lower().startswith('https://')
                and not _test_isolation.under_test_runner()
                and getattr(api.safe_http_json, '__module__', '') == 'agent_hub_host.api'
            ):
                return _https_get_json(base, path, access)
            req = urllib.request.Request(base.rstrip('/') + path, data=data, method=method,
                                         headers={'Authorization': 'Bearer ' + access, 'Content-Type': 'application/json'})
            return api.safe_http_json(req)
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException, ValueError, RuntimeError) as exc:
            reason = ('Intake response error: invalid JSON or UTF-8 response'
                      if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) else diagnostic(exc))
            recovery = '; mutation outcome unknown; inspect before retrying with the same key and payload' if method != 'GET' else ''
            error_type = IntakeResponseTooLarge if isinstance(exc, RuntimeError) and str(exc) == 'Response too large' else IntakeTransportError
            raise error_type(reason + recovery) from None

    def send_with_retry(access):
        for attempt in range(max_attempts):
            try:
                return send(access)
            except urllib.error.HTTPError as exc:
                if attempt + 1 < max_attempts and exc.code in (408, 500, 502, 503, 504):
                    try:
                        exc.close()
                    except Exception:
                        pass
                    continue
                raise
            except IntakeTransportError as exc:
                if attempt + 1 < max_attempts and not isinstance(exc, IntakeResponseTooLarge):
                    continue
                if retry_key and f'retry key: {retry_key}' not in str(exc):
                    raise type(exc)(f'{exc}; retry key: {retry_key}') from None
                raise

    try:
        return send_with_retry(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not cfg.get('_hub2_worker') and not env_token:
            auth.refresh_token(expected_token=token) if cfg.get('worker_launch_id') else auth.refresh_token()
            try:
                return send_with_retry(auth.get_access_token())
            except urllib.error.HTTPError as retry:
                raise http_error(retry, method, payload) from None
        raise http_error(exc, method, payload) from None


def read_file(path, limit=MAX_REQUEST, field='Input'):
    from pathlib import Path
    try:
        if path == '-':
            raw = sys.stdin.buffer.read(limit + 1)
        else:
            with Path(path).open('rb') as stream:
                raw = stream.read(limit + 1)
    except OSError as exc:
        reason = {2: 'file not found', 13: 'permission denied', 21: 'path is a directory'}.get(exc.errno, 'file is unreadable')
        raise IntakeInputError(f'{field}: {reason}') from None
    if len(raw) > limit:
        raise IntakeInputError(f'{field} exceeds {limit} UTF-8 bytes')
    return raw.decode('utf-8')


def add_commands(sub):
    root = sub.add_parser('intake', help='Private intake tasks and durable context; payloads are data, never execution')
    commands = root.add_subparsers(dest='intake_cmd', required=True)
    from . import intake_stats
    intake_stats.options(commands.add_parser('stats', help='Timing and attempts for one intake'), individual=True)
    depend = commands.add_parser('depend', help='Add a prerequisite; original work waits until it is DONE')
    depend.add_argument('id')
    depend.add_argument('--on', required=True)
    depend.add_argument('--remove', action='store_true')
    depend.add_argument('--key')
    depend.add_argument('--version', type=int)
    blocker = commands.add_parser('blocker', help='Create a follow-up with AC, link it and block the original atomically')
    blocker.add_argument('id')
    blocker.add_argument('--title', required=True)
    blocker.add_argument('--text', required=True)
    blocker.add_argument('--acceptance-criteria', required=True)
    blocker.add_argument('--key', required=True)
    blocker.add_argument('--version', type=int)
    decompose = commands.add_parser('decompose', help='Queue a deep-tier agent to create child intakes and dependencies')
    decompose.add_argument('id')
    decompose.add_argument('--workspace')
    decompose.add_argument('--key', required=True)
    decompose.add_argument('--version', type=int)
    forgotten = commands.add_parser('forgotten', help='Open intakes forgotten by live sessions; includes queued restart reason')
    forgotten.add_argument('--project')
    queue = commands.add_parser('queue', help='Autonomous intake queue (promoted first, then priority high > normal > low, then newest created_at)')
    queue.add_argument('--project')
    queue.add_argument('--cursor')
    queue.add_argument('--limit', type=int)
    create = commands.add_parser('create', help='Capture verbatim UTF-8 text from file/stdin; missing provenance stays unknown')
    create.add_argument('--project', required=True)
    create.add_argument('--title', required=True)
    intake_body = create.add_mutually_exclusive_group(required=True)
    intake_body.add_argument('--text', help='Verbatim intake text (max 8192 UTF-8 bytes)')
    intake_body.add_argument('--text-file')
    intake_body.add_argument('--voice-pending', action='store_true', help='Capture voice pointer now with null text and pending transcription')
    create.add_argument('--source-type', choices=['chat', 'voice', 'forwarded', 'unknown'], default='unknown')
    create.add_argument('--source-reference')
    create.add_argument('--source-time')
    create.add_argument('--acceptance-criteria', default=None,
                        help='Acceptance criteria text (max 8 KiB); required non-empty before assign/start')
    create.add_argument('--acceptance-criteria-file', default=None,
                        help='Read acceptance criteria from a UTF-8 file, or - for stdin (max 8 KiB)')
    create.add_argument('--verification', choices=['required', 'skip', 'auto'], default=None,
                        help='Post-DONE verifier policy (default auto: Hub decides from the change)')
    create.add_argument('--priority', choices=['high', 'normal', 'low'], default=None,
                        help='Queue priority (default normal; autonomous queue takes high before normal before low)')
    create.add_argument('--key', required=True)
    listing = commands.add_parser('list', help='List one page, or all matching tasks with --all')
    listing.add_argument('--status', default=None)
    listing.add_argument('--project')
    listing.add_argument('--search', help='Server-side dedup filter: case-insensitive substring over title, project, original text and context (max 128 chars)')
    listing.add_argument('--cursor', help='Opaque next-page cursor from the previous list page (legacy alias: --after)')
    listing.add_argument('--after', dest='cursor', help=argparse.SUPPRESS)
    listing.add_argument('--revision', type=int)
    listing.add_argument('--authorization-valid-until', type=int)
    listing.add_argument('--limit', type=int, default=None, help='Positive integer; default 25 (50 with --all), server clamps above max 50')
    listing.add_argument('--all', action='store_true', help='Follow all pages with a pinned snapshot; restart if it changes')
    listing.add_argument('--archived', action='store_true', help='List archived intakes')
    summary = commands.add_parser('summary', help='Current authorized task counts and five recent tasks; no histories')
    summary.add_argument('--project')
    audit = commands.add_parser('audit', help='Durable lifecycle events for one intake, or recent events')
    audit.add_argument('id', nargs='?')
    audit.add_argument('--limit', type=int, default=25)
    audit.add_argument('--after')
    audit.add_argument('--revision', type=int)
    for name in ('show', 'history', 'links', 'context'):
        command = commands.add_parser(name)
        command.add_argument('id')
        if name == 'history':
            command.add_argument('--archived', action='store_true', help='Read one page of losslessly retained note history')
            command.add_argument('--after', type=int)
            command.add_argument('--limit', type=int, default=100)
    start = commands.add_parser('continue', help='CAS ready -> active context only; does not start a session')
    start.add_argument('id')
    start.add_argument('--version', type=int, required=True)
    start.add_argument('--key', required=True)
    promote = commands.add_parser('promote', help='Promote an intake to the front of the autonomous queue (sets promoted_at; newest promotion first)')
    promote.add_argument('id')
    promote.add_argument('--version', type=int, help='Explicit CAS version; otherwise fetch and retry version conflicts up to three times')
    promote.add_argument('--key', help='Stable retry key; generated automatically when omitted')
    update = commands.add_parser('update', help='CAS status/context update; cannot imply execution, acceptance or approval')
    update.add_argument('id')
    update.add_argument('--version', type=int, help='Explicit CAS version; otherwise fetch and retry version conflicts up to three times')
    update.add_argument('--key', help='Stable retry key; generated automatically when omitted')
    update.add_argument('--clear', action='append', choices=['current_state', 'next_step', 'owner', 'uncertainty', 'acceptance_criteria'], default=[], help='Explicitly clear a context field to unknown (repeatable)')
    for field in ('project', 'status', 'title', 'current-state', 'next-step', 'owner', 'uncertainty', 'acceptance-criteria'):
        update.add_argument('--' + field)
    update.add_argument('--verification', choices=['required', 'skip', 'auto'], help='Post-DONE verifier policy')
    update.add_argument('--priority', choices=['high', 'normal', 'low'], help='Queue priority (high is taken before normal before low)')
    update.add_argument('--reason', help='Reason for close, cancellation or reopen')
    update.add_argument('--superseded-by', help='Replacement intake id; required for cancel unless dropped by Alexey')
    update.add_argument('--force', action='store_true', help='Override the worktree close gate when closing; requires --reason')
    update.add_argument('--note', help='Note recorded with this update (max 4000 characters); given alone it appends a plain note')
    close = commands.add_parser('close', help='CAS status done; refused while intake worktrees are present or unmerged unless --force --reason')
    close.add_argument('id')
    close.add_argument('--version', type=int, help='Explicit CAS version; otherwise fetch and retry version conflicts up to three times')
    close.add_argument('--key', help='Stable retry key; generated automatically when omitted')
    close.add_argument('--force', action='store_true', help='Override the worktree close gate; requires --reason')
    close.add_argument('--reason', help='Close reason; required with --force or --outcome')
    close.add_argument('--note', help='Note recorded with the close (max 4000 characters)')
    close.add_argument('--superseded-by', help='Replacement intake id; recorded in the close note of a superseded intake')
    close.add_argument('--outcome', choices=['closed_unverified'],
                       help='Operator only: close without DONE evidence, recorded as closed_unverified with --reason (never counted as verified)')
    merge_check = commands.add_parser('merge-check', help='Collect git ancestry + Deploy evidence for this intake and record it in Hub (merge gate)')
    merge_check.add_argument('id')
    merge_check.add_argument('--repo', default=None, help='Git checkout holding the work, also checked for its HEAD (default: a private clone of the project repo, independent of the current directory)')
    merge_check.add_argument('--sha', action='append', default=[], help='Extra commit to verify (the DONE note commits and HEAD are always checked)')
    merge_check.add_argument('--key', help='Stable retry key; generated automatically when omitted')
    merge_check.add_argument('--dry-run', action='store_true', help='Print the evidence without recording it')
    merge_state = commands.add_parser('merge-state', help='Merge state of one intake, or the list of unmerged intakes')
    merge_state.add_argument('id', nargs='?')
    event = commands.add_parser('append', help='Append a note in one command (--note TEXT or --note-file F; key and version are automatic), or an intake/note/session/report/commit/followup/blocker/decision/rule reference as JSON with --event-file; approval decisions require operator')
    event.add_argument('id')
    event.add_argument('--version', type=int, help='CAS version for --event-file appends; not needed for a plain note')
    event.add_argument('--key', help='Stable retry key; generated automatically when omitted')
    event.add_argument('--note', help='Plain note text (max 4000 characters)')
    event.add_argument('--note-file', help='Read the plain note from a UTF-8 file, or - for stdin')
    event.add_argument('--event-file', help='Bounded UTF-8 JSON event file, or - for stdin. Register an aplexer session with {"kind":"session","native_id":"FULL_UUID","host":"MACHINE_ID","verify_live":true}; operator-only, requires fresh host census')
    assign = commands.add_parser('assign', help='Assign work: create the canonical Hub session for a task and record its real id automatically')
    assign.add_argument('id')
    assign.add_argument('--version', type=int, required=True)
    assign.add_argument('--key', required=True)
    assign.add_argument('--force', action='store_true', help='Operator only: start despite unresolved dependencies')
    assign.add_argument('--workspace', required=True, help='Configured workspace label on the selected host')
    assign.add_argument('--engine', help='Requested engine label (existing model tiers still decide placement; requires --reason)')
    assign.add_argument('--reason', default=None, help='Explanation why this specific model/engine must be chosen (required when --engine is specified)')
    assign.add_argument('--tier', choices=['quick', 'daily', 'deep'], help='Existing model tier; unknown tiers are refused, never upgraded')
    assign.add_argument('--machine', help='Requested machine label')
    assign.add_argument('--cwd', help='Workspace-relative confined working directory')
    assign.add_argument('--parent', help='Optional parent Hub session UUID')
    assign_mode = assign.add_mutually_exclusive_group()
    assign_mode.add_argument('--brief-file', help='UTF-8 goal file for headless work (max 8 KiB), or - for stdin')
    assign_mode.add_argument('--interactive', action='store_true', help='Start at an empty native prompt')
    assign.add_argument('--source-type', choices=['chat', 'voice', 'forwarded', 'unknown'], default='unknown')
    assign.add_argument('--source-reference')
    assign.add_argument('--source-time')
    assign.add_argument('--next-step', help='Handoff next step recorded with the assignment')
    assign.add_argument('--current-state', help='Task state description recorded with the assignment')
    assign.add_argument('--owner', help='Ownership description recorded with the assignment')
    for name in ('rules', 'grants'):
        command = commands.add_parser(name, help='Operator only: list or submit explicit JSON mutation')
        command.add_argument('--payload-file')
    grantees = commands.add_parser('grantees', help='Operator only: discover active grantable agent credential metadata for an actor')
    grantees.add_argument('--actor', required=True, help='Explicit canonical agent actor (e.g. project:tag or tag)')
    grantees.add_argument('--family', help='Optional explicit family selection')
    grantees.add_argument('--limit', type=int, default=25)
    grantees.add_argument('--after')
    grantees.add_argument('--revision', type=int)
    revoke = commands.add_parser('revoke', help='Operator only: revoke an intake grant, preserving its history')
    revoke.add_argument('id')
    revoke.add_argument('--payload-file', required=True)
    handoff = sub.add_parser('handoff', help='Self-contained ALL authorized open work and operator rules; fails without a complete consistent snapshot')
    handoff.add_argument('--project')
    handoff.add_argument('--page-size', type=int, default=25)


def safe_id(value):
    valid_key(value)
    if value in ('.', '..') or '/' in value:
        raise IntakeInputError('Invalid identifier')
    return value


def json_file(path):
    result = json.loads(read_file(path))
    if not isinstance(result, dict):
        raise IntakeInputError('JSON object required')
    return result


def handoff(project=None, page_size=25):
    if not 1 <= page_size <= 25:
        raise IntakeInputError('Invalid page size')
    for _ in range(3):
        tasks, seen, revision, rules = [], set(), None, {}
        after = None
        valid_until = None
        try:
            for _ in range(257):
                params = {'limit': page_size}
                if project is not None:
                    params['project'] = project
                if revision is not None:
                    params['revision'] = revision
                if after:
                    params['after'] = after
                if valid_until is not None:
                    params['authorization_valid_until'] = valid_until
                page = request('GET', BASE + '/handoff?' + urlencode(params))
                current = page['snapshot_version']
                if revision is not None and revision != current:
                    raise StaleSnapshot('Snapshot changed')
                if revision is not None and valid_until != page.get('authorization_valid_until'):
                    raise StaleSnapshot('Authorization snapshot changed')
                revision = current
                valid_until = page.get('authorization_valid_until')
                for task in page['items']:
                    if task['id'] in seen:
                        raise IntakeInputError('Duplicate snapshot item')
                    seen.add(task['id'])
                    tasks.append(task)
                for rule in page['operator_rules']:
                    rules[rule['id']] = rule
                if len(tasks) > 256:
                    raise IntakeInputError('Snapshot exceeds capacity')
                after = page['next']
                if not after:
                    if len(tasks) != page['total']:
                        raise IntakeInputError('Incomplete snapshot')
                    return {'snapshot_version': revision, 'complete': True, 'authorized_scope': page['authorized_scope'],
                            'data_notice': page['data_notice'], 'operator_rules': list(rules.values()), 'tasks': tasks,
                            'total': len(tasks)}
            raise IntakeInputError('Snapshot page bound exceeded')
        except StaleSnapshot:
            continue
    raise IntakeInputError('Snapshot kept changing; retry handoff')


def list_all(params):
    """Follow all pages of one pinned listing. The server pins the matching id
    set behind list_token, so pages survive intake create/close churn; on the
    rare expiry (410) the whole walk restarts, bounded like the handoff loop.
    Servers without a token fall back to the revision-pinned walk; servers
    with neither pin (plain keyset pages) are walked with cursor-uniqueness
    and duplicate-id guards only."""
    if 'after' in params or 'cursor' in params:
        raise IntakeInputError('--all cannot be combined with --cursor')
    for _ in range(3):
        try:
            return _list_all_once(params)
        except StaleSnapshot:
            continue
    raise IntakeInputError('Snapshot kept changing; retry the command')


def _request_page(page_params, params):
    """One GET with the oversized-response retry from the revision walk."""
    while True:
        try:
            return request('GET', BASE + '/tasks?' + urlencode(page_params))
        except IntakeResponseTooLarge:
            current = min(page_params.get('limit', 25), 100)
            if current <= 1:
                raise
            page_params['limit'] = max(1, current // 2)
            params['limit'] = page_params['limit']


def _list_all_once(params):
    items, seen, cursors = [], set(), set()
    first = None
    token = None
    after = None
    reduced = False
    for _ in range(10001):
        page_params = dict(params)
        if token is not None:
            page_params['list_token'] = token
        elif first is None:
            page_params['list_token'] = 'new'
        if after:
            page_params['cursor'] = after
            if first.get('authorization_valid_until') is not None:
                page_params['authorization_valid_until'] = first['authorization_valid_until']
            if token is None and isinstance(first.get('snapshot_version'), int):
                # Tokenless fallback: the classic revision-pinned page walk.
                page_params['revision'] = first['snapshot_version']
        if 'limit' not in page_params:
            page_params['limit'] = 50
            params['limit'] = 50
        before_limit = page_params.get('limit')
        page = _request_page(page_params, params)
        if page_params.get('limit') != before_limit:
            reduced = True
        if first is None:
            first = page
            token = page.get('list_token')
        elif token is None and isinstance(first.get('snapshot_version'), int) and (
                                page['snapshot_version'] != first['snapshot_version'] or
                                page.get('authorization_valid_until') != first.get('authorization_valid_until') or
                                page['total'] != first['total']):
            raise StaleSnapshot('Snapshot changed')
        for task in page['items']:
            if task['id'] in seen:
                raise IntakeInputError('Duplicate snapshot item; retry the command')
            seen.add(task['id'])
            items.append(task)
        after = page['next']
        if not after:
            if token is None and isinstance(first.get('total'), int) and len(items) != first['total']:
                raise IntakeInputError('Incomplete snapshot; retry the command')
            result = dict(first, items=items, next=None, total=len(items))
            if reduced:
                result.update(effective_limit=page.get('effective_limit', min(params.get('limit', 50), 50)), page_size_reduced=True)
            return result
        if after in cursors:
            raise IntakeInputError('Invalid snapshot cursor; retry the command')
        cursors.add(after)
    raise IntakeInputError('Snapshot page bound exceeded')


def execute(args):
    if args.cmd == 'handoff':
        return handoff(args.project, args.page_size)
    command = args.intake_cmd
    if command == 'forgotten':
        params = {'project': args.project} if args.project else {}
        return request('GET', BASE + '/forgotten' + ('?' + urlencode(params) if params else ''))
    if command == 'queue':
        params = {field: getattr(args, field) for field in ('project', 'cursor', 'limit') if getattr(args, field, None) is not None}
        return request('GET', BASE + '/queue' + ('?' + urlencode(params) if params else ''))
    if command == 'create':
        valid_key(args.key)
        criteria = getattr(args, 'acceptance_criteria', None)
        criteria_file = getattr(args, 'acceptance_criteria_file', None)
        if criteria is not None and criteria_file is not None:
            raise IntakeInputError('Cannot specify both --acceptance-criteria and --acceptance-criteria-file')
        if args.text_file == '-' and criteria_file == '-':
            raise IntakeInputError('Cannot read both --text-file and --acceptance-criteria-file from stdin')
        if criteria_file is not None:
            criteria = read_file(criteria_file, 8192, '--acceptance-criteria-file')
        if criteria is not None and len(criteria.encode('utf-8')) > 8192:
            raise IntakeInputError('Acceptance criteria exceed 8192 UTF-8 bytes')
        text = None if args.voice_pending else (args.text if args.text is not None else read_file(args.text_file, 8192, '--text-file'))
        if text is not None and len(text.encode('utf-8')) > 8192:
            raise IntakeInputError('Text exceeds 8192 UTF-8 bytes')
        body = {'key': args.key, 'project': args.project, 'title': args.title,
                       'text': text,
                       'transcription': 'pending' if args.voice_pending else ('provided' if args.source_type == 'voice' else 'not_applicable'), 'source': {'type': args.source_type,
                       'reference': args.source_reference, 'timestamp': args.source_time}}
        if criteria is not None:
            body['acceptance_criteria'] = criteria
        if getattr(args, 'verification', None) is not None:
            body['verification'] = args.verification
        if getattr(args, 'priority', None) is not None:
            body['priority'] = args.priority
        return request('POST', BASE + '/tasks', body)
    if command == 'summary':
        params = {'project': args.project} if args.project is not None else {}
        return request('GET', BASE + '/summary?' + urlencode(params))
    if command == 'list':
        params = {'limit': args.limit if args.limit is not None else (50 if args.all else 25)}
        if args.status is not None:
            params['status'] = args.status
        if args.search is not None:
            search = args.search.strip()
            if not search:
                raise IntakeInputError('--search must be a non-empty filter (1-128 characters)')
            if len(search) > 128:
                raise IntakeInputError('--search exceeds 128 characters')
            params['q'] = search
        if args.archived:
            params['archived'] = 'true'
        elif args.all:
            params['all'] = 'true'
        for field in ('project', 'revision', 'authorization_valid_until'):
            if getattr(args, field) is not None:
                params[field] = getattr(args, field)
        # The wire pagination parameter is `cursor` (Hub2 keyset pages carry an
        # opaque JSON cursor in `next`); `--after` stays as a hidden alias.
        if getattr(args, 'cursor', None) is not None:
            if args.all:
                raise IntakeInputError('--all cannot be combined with --cursor')
            params['cursor'] = args.cursor
        if args.all:
            return list_all(params)
        return request('GET', BASE + '/tasks?' + urlencode(params))
    if command == 'audit':
        path = BASE + ('/tasks/' + safe_id(args.id) + '/audit' if args.id else '/audit')
        params = {field: getattr(args, field) for field in ('limit', 'after', 'revision') if getattr(args, field) is not None}
        return request('GET', path + '?' + urlencode(params))
    if command == 'grantees':
        if not getattr(args, 'actor', None) or not args.actor.strip():
            raise IntakeInputError('Actor required')
        params = {'actor': args.actor.strip(), 'limit': args.limit}
        if getattr(args, 'family', None):
            params['family_id'] = args.family.strip()
        for field in ('after', 'revision'):
            if getattr(args, field, None) is not None:
                params[field] = getattr(args, field)
        return request('GET', BASE + '/grantees?' + urlencode(params))
    if command == 'merge-state':
        return request('GET', BASE + ('/tasks/' + safe_id(args.id) + '/merge-state' if args.id else '/merge-state'))
    if command == 'merge-check':
        from . import merge_evidence
        shown = request('GET', BASE + '/tasks/' + safe_id(args.id))
        history = ((shown.get('task') or {}).get('payload_data') or {}).get('history') or []
        task = shown.get('task') or {}
        text = '\n'.join(e.get('text', '') for e in history if isinstance(e, dict) and e.get('kind') == 'note' and str(e.get('text', '')).lstrip().startswith('DONE'))
        explicit = merge_evidence.explicit_commit_refs(history)
        recorded = merge_evidence.hub_recorded_shas(task.get('merge_state'))
        state = task.get('merge_state') if isinstance(task.get('merge_state'), dict) else {}
        recorded_head = str(state.get('head') or '').strip().lower()
        if not merge_evidence.SHA.match(recorded_head):
            recorded_head = ''
        try:
            from pathlib import Path
            repo = str(Path(args.repo).resolve()) if args.repo else merge_evidence.own_clone()
            candidates = merge_evidence.shas_from_text(text, exclude=(task.get('id'),) + tuple(e.get('hub_session_id') or e.get('id') for e in history if isinstance(e, dict)))
            merge_evidence.fetch_origin(repo)
            remaining_recorded = [sha for sha in recorded if sha != recorded_head]
            wanted = ([recorded_head] if recorded_head else []) + explicit + merge_evidence.existing_commits(repo, candidates) + [x for x in args.sha if isinstance(x, str)] + remaining_recorded
            evidence = merge_evidence.collect(str(Path(repo).resolve()), wanted, fetch=False, include_head=args.repo is not None,
                                              preferred_deploy_sha=recorded_head or None)
        except RuntimeError as exc:
            raise IntakeInputError(str(exc)) from None
        if args.dry_run:
            return {'dry_run': True, 'evidence': evidence}
        key = args.key or 'merge-check-' + _random_hex()
        valid_key(key)
        return request('POST', BASE + '/tasks/' + safe_id(args.id) + '/merge-evidence', {'key': key, **evidence})
    if command in ('rules', 'grants'):
        return request('POST' if args.payload_file else 'GET', BASE + '/' + command,
                       json_file(args.payload_file) if args.payload_file else None)
    path = BASE + '/tasks/' + safe_id(args.id)
    if command in ('depend', 'blocker', 'decompose'):
        key = args.key or (command + '-' + _random_hex())
        valid_key(key)
        version = args.version
        if version is None:
            version = request('GET', path)['task']['version']
        payload = {'key': key, 'expected_version': version}
        if command == 'depend':
            payload.update(on=safe_id(args.on), remove=args.remove)
        elif command == 'blocker':
            payload.update(title=args.title, text=args.text, acceptance_criteria=args.acceptance_criteria)
        elif args.workspace:
            payload['workspace'] = args.workspace
        return request('POST', path + '/' + command, payload)
    if command in ('show', 'history', 'links', 'context'):
        if command == 'history' and getattr(args, 'archived', False):
            limit, after = getattr(args, 'limit', 100), getattr(args, 'after', None)
            if not 1 <= limit <= 100 or (after is not None and after < 0):
                raise IntakeInputError('Invalid archive page: limit is 1-100 and after is a nonnegative sequence')
            params = {'archived': '1', 'limit': limit}
            if after is not None:
                params['after'] = after
            return request('GET', path + '/history?' + urlencode(params))
        result = request('GET', path + ('' if command == 'show' else '/' + command))
        if command == 'show' and isinstance(result, dict):
            task = result.get('task')
            archive = task.get('history_archive') if isinstance(task, dict) else None
            if isinstance(archive, dict) and isinstance(archive.get('count'), int) and archive['count'] > 0:
                latest = archive.get('latest_sequence')
                if type(latest) is int and latest > 0:
                    archive['read_command'] = f'hub intake history {safe_id(args.id)} --archived --after {latest - 1} --limit 1'
                else:
                    archive['read_command'] = f'hub intake history {safe_id(args.id)} --archived'
        return result
    if command == 'revoke':
        return request('POST', BASE + '/grants/' + safe_id(args.id) + '/revoke', json_file(args.payload_file))
    plain_note = (command == 'update' and _note_only(args)) or (
        command == 'append' and (args.note is not None or args.note_file is not None))
    if command == 'append' and sum(value is not None for value in (args.note, args.note_file, args.event_file)) != 1:
        _usage_error('hub intake append: give exactly one of --note, --note-file or --event-file')
    if command == 'append' and not plain_note and (args.version is None or args.key is None):
        _usage_error('hub intake append: --event-file needs --version (from hub intake context) and --key')
    key = args.key or (command + '-' + _random_hex() if plain_note or command in ('update', 'close', 'promote') else None)
    valid_key(key)
    if plain_note:
        text = args.note if args.note is not None else read_file(args.note_file, 16000, '--note-file')
        return request('POST', path + '/events', {'key': key, 'note': _note_text(text)})
    payload = {'key': key, 'expected_version': args.version}
    if command == 'continue':
        return request('POST', path + '/continue', payload)
    if command == 'promote':
        return cas_update(path + '/promote', payload, args.version is None, key, read_path=path)
    if command == 'assign':
        session = {'workspace': args.workspace}
        if getattr(args, 'force', False):
            session['force'] = True
        for field in ('engine', 'tier', 'machine', 'cwd'):
            if getattr(args, field, None):
                session[field] = getattr(args, field)
        if getattr(args, 'engine', None):
            reason = getattr(args, 'reason', None)
            if not isinstance(reason, str) or not reason.strip():
                raise IntakeInputError('Engine override requires an explanation why this specific model/engine must be chosen (use --reason). Casual use should prefer auto/policy without --engine.')
            session['engine_override_reason'] = reason.strip()
        if getattr(args, 'parent', None):
            session['parent_id'] = args.parent
        session['mode'] = 'interactive' if args.interactive else 'headless'
        if args.brief_file:
            from pathlib import Path
            if session['mode'] == 'interactive':
                raise IntakeInputError('Interactive sessions start at an empty native prompt')
            with (sys.stdin.buffer if args.brief_file == '-' else Path(args.brief_file).open('rb')) as stream:
                raw = stream.read(8192 + 1)
            if len(raw) > 8192:
                raise IntakeInputError('Brief exceeds capacity')
            session['brief'] = raw.decode('utf-8')
        payload['session'] = session
        payload['provenance'] = {'type': args.source_type, 'reference': args.source_reference,
                                 'timestamp': args.source_time}
        for field in ('next_step', 'current_state', 'owner'):
            if getattr(args, field, None) is not None:
                payload[field] = getattr(args, field)
        return request('POST', path + '/assign', payload)
    if command == 'append':
        ev = json_file(args.event_file)
        if isinstance(ev, dict) and 'note' in ev and 'kind' not in ev and len(ev) == 1:
            payload['note'] = ev['note']
        elif isinstance(ev, dict) and 'event' in ev and 'kind' not in ev and len(ev) == 1:
            payload['event'] = ev['event']
        else:
            payload['event'] = ev
        return request('POST', path + '/events', payload)
    if command in ('update', 'close') and getattr(args, 'force', False):
        reason = getattr(args, 'reason', None)
        if not isinstance(reason, str) or not reason.strip():
            raise IntakeInputError('--force requires a non-empty --reason')
        payload['force'] = True
    if command == 'close':
        payload['status'] = 'done'
        if getattr(args, 'outcome', None):
            reason = getattr(args, 'reason', None)
            if not isinstance(reason, str) or not reason.strip():
                raise IntakeInputError('--outcome closed_unverified requires a non-empty --reason')
            payload['outcome'] = args.outcome
        if getattr(args, 'reason', None) is not None:
            payload['reason'] = args.reason
        if getattr(args, 'note', None) is not None:
            payload['note'] = _note_text(args.note)
        if getattr(args, 'superseded_by', None) is not None:
            payload['superseded_by'] = safe_id(args.superseded_by)
        return cas_update(path, payload, args.version is None, key)
    if command == 'update':
        if args.status == 'closed':
            args.status = 'done'
        if getattr(args, 'note', None) is not None:
            payload['note'] = _note_text(args.note)
        for field in ('project', 'status', 'title', 'current_state', 'next_step', 'owner', 'uncertainty', 'acceptance_criteria', 'priority', 'verification', 'reason', 'superseded_by'):
            if getattr(args, field, None) is not None:
                if field == 'acceptance_criteria' and len(args.acceptance_criteria.encode('utf-8')) > 8192:
                    raise IntakeInputError('Acceptance criteria exceed 8192 UTF-8 bytes')
                payload[field] = getattr(args, field)
        for field in args.clear:
            if getattr(args, field) is not None:
                raise IntakeInputError('Cannot set and clear one field together')
            payload[field] = None
        return cas_update(path, payload, args.version is None, key)
    raise IntakeInputError('Unknown intake operation')


UPDATE_FIELDS = ('project', 'status', 'title', 'current_state', 'next_step', 'owner', 'uncertainty', 'acceptance_criteria', 'priority', 'verification', 'reason', 'superseded_by')


def _usage_error(message):
    """Conditional argument requirements keep argparse's usage-error exit code."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _note_only(args):
    """`hub intake update <id> --note TEXT` with nothing else is a plain note append."""
    return (getattr(args, 'note', None) is not None and not getattr(args, 'clear', None) and not getattr(args, 'force', False)
            and all(getattr(args, field, None) is None for field in UPDATE_FIELDS))


def _note_text(text):
    if not isinstance(text, str) or not text.strip():
        raise IntakeInputError('Note is empty')
    if len(text) > 4000:
        raise IntakeInputError('Note exceeds 4000 characters; split it into several notes')
    return text


def cas_update(path, payload, automatic, key, read_path=None):
    """POST a task update, refetching the version on CAS conflicts when automatic."""
    import http.client
    for attempt in range(3):
        if automatic:
            current = request('GET', read_path or path)
            payload['expected_version'] = current['task']['control']['version']
        try:
            return request('POST', path, dict(payload))
        except IntakeHTTPError as exc:
            if not automatic or exc.status != 409 or exc.code != 'version_conflict' or attempt == 2:
                raise
        except (IntakeTransportError, OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError, RuntimeError) as exc:
            attempted = payload.get('expected_version')
            version_info = f'; attempted version: {attempted}' if attempted is not None else ''
            raise IntakeTransportError(f'{diagnostic(exc)}; Intake update outcome unknown; inspect the task before retrying. Retry key: {key}{version_info}') from None
    raise IntakeInputError('Intake update did not complete')


def summary_main():
    """Small shared entry point; skip unrelated CLI imports/registration."""
    argv = sys.argv[3:]
    project = None
    use_argparse = False
    if not argv:
        pass
    elif len(argv) == 2 and argv[0] == '--project' and not argv[1].startswith('-'):
        project = argv[1]
    elif len(argv) == 1 and argv[0].startswith('--project='):
        project = argv[0].split('=', 1)[1]
    else:
        use_argparse = True
    if use_argparse:
        import argparse
        parser = argparse.ArgumentParser(prog='hub intake summary', description='Current task counts and five recent tasks; no histories')
        parser.add_argument('--project')
        args = parser.parse_args(argv)
    else:
        class _Args:
            pass
        args = _Args()
        args.project = project
    args.cmd, args.intake_cmd = 'intake', 'summary'
    try:
        print(json.dumps(execute(args), ensure_ascii=True))
    except Exception as exc:
        print(diagnostic(exc), file=sys.stderr)
        sys.exit(1)
