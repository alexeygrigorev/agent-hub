"""Hub session orchestration client (M12e). Prompts are data; this client never executes them.

Public HTTP surface only: talks to /api/sessions* and /api/session-commands/*
with the existing token/device credentials. Native execution belongs to the
sibling host worker; this module performs no native effects and imports no
native adapters. See docs/SESSION-CONTROL.md (proposal, pending head ACK).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from . import api, auth, config

MAX_REQUEST = 32768
MAX_BRIEF = 8192
MAX_BODY = 16384
TIERS = ('quick', 'daily', 'deep')
WAIT_DEFAULT_TIMEOUT = 300.0
WAIT_MAX_TIMEOUT = 600.0
WAIT_MIN_INTERVAL = 1.0
# Terminal command states: only these confirm an outcome. `transported` alone
# is fetch/PTY bytes, never processing proof; waiting never treats it as done.
TERMINAL_COMMAND = ('acked', 'refused', 'uncertain')
TERMINAL_SESSION = ('running', 'stopped', 'failed', 'refused', 'held', 'uncertain')


def request(method, path, payload=None):
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    if data is not None and len(data) > MAX_REQUEST:
        raise ValueError('Session request exceeds capacity')
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or auth.get_access_token()

    def send(access):
        req = urllib.request.Request(base.rstrip('/') + path, data=data, method=method,
                                     headers={'Authorization': 'Bearer ' + access, 'Content-Type': 'application/json'})
        return api.safe_http_json(req)

    def failure(exc):
        # Surface the server error code alongside the status so the receiver
        # can classify terminal vs retryable 409s (claim_expired may heal
        # after the sweep; proof_replayed never heals). Best-effort: an
        # unreadable body keeps the status-only form.
        name = ''
        try:
            raw = exc.read(4096) if hasattr(exc, 'read') else b''
            data = json.loads((raw or b'').decode('utf-8', 'replace'))
            err = data.get('error') if isinstance(data, dict) else None
            if isinstance(err, str) and err and len(err) <= 64 and err.replace('_', '').isalnum():
                name = ' (%s)' % err
        except Exception:
            name = ''
        return ValueError('Session HTTP request failed with status %d%s' % (exc.code, name))

    try:
        return send(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not env_token:
            auth.refresh_token()
            try:
                return send(auth.get_access_token())
            except urllib.error.HTTPError as retry:
                raise failure(retry) from None
        raise failure(exc) from None


def read_file(path, limit=MAX_BODY):
    if path == '-':
        raw = sys.stdin.buffer.read(limit + 1)
    else:
        with Path(path).open('rb') as stream:
            raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Input exceeds capacity')
    return raw.decode('utf-8')


def add_wait(parser):
    parser.add_argument('--wait', action='store_true',
                        help='Poll until a terminal state (bounded); never claims transport as delivery')
    parser.add_argument('--timeout', type=float, default=WAIT_DEFAULT_TIMEOUT,
                        help='Max seconds to wait (1..600, default 300)')
    parser.add_argument('--interval', type=float, default=5.0,
                        help='Seconds between polls (min 1)')


def check_wait_args(args):
    if getattr(args, 'wait', False):
        timeout = float(getattr(args, 'timeout', WAIT_DEFAULT_TIMEOUT))
        interval = float(getattr(args, 'interval', 5.0))
        if not 1 <= timeout <= WAIT_MAX_TIMEOUT:
            raise ValueError('Timeout must be between 1 and 600 seconds')
        if interval < WAIT_MIN_INTERVAL:
            raise ValueError('Interval must be at least 1 second')
        return timeout, interval
    return None, None


def wait_until(deadline, interval, fetch, done, label):
    while True:
        result = fetch()
        if done(result):
            return result
        if time.monotonic() >= deadline:
            raise ValueError('%s timed out; re-check status with the same key, do not blind-retry' % label)
        time.sleep(interval)


def _require_cleanup_tree_args(args):
    """Fail closed BEFORE any server mutation when --tree is under-addressed.

    The tree step removes exactly ONE explicitly addressed owned worktree/
    branch. Every address field is required; scope flags alone never address
    a tree. Validated before POST /api/sessions/cleanup so an invalid tree
    invocation performs zero writes.
    """
    missing = [name for name in ('machine_id', 'native_id', 'owner', 'worktree', 'branch', 'journal_dir')
               if not getattr(args, name, None)]
    if missing:
        raise ValueError('cleanup --apply --tree requires %s (explicit address; scope flags never address a tree)'
                         % ', '.join('--' + n.replace('_', '-') for n in missing))
    roots = getattr(args, 'cleanup_root', None)
    if not roots or not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        raise ValueError('cleanup --apply --tree requires at least one --cleanup-root (trusted allowlist)')
    journal = getattr(args, 'journal_dir', None)
    if not isinstance(journal, str) or not os.path.isabs(journal):
        raise ValueError('cleanup --apply --tree requires an absolute --journal-dir')


def _execute_cleanup_tree(args, applied):
    """Client-local --tree execution is DISABLED (fail closed, M15d blocker).

    The prior implementation ran Git on the CLI caller machine with
    caller-supplied --owner/--worktree/--branch/--cleanup-root and synthesized
    custody, never matching the tree path/branch to the canonical native
    session binding. That is not remote owning-host cleanup. This stub raises
    unconditionally (defense in depth); `execute()` refuses --tree BEFORE any
    HTTP write. Real tree removal will run on the owning host through the
    canonical M12 claim/effect/ack pipeline (HOST-config roots + binding
    workspace + live native identity + registered branch).
    """
    raise ValueError(
        'cleanup --tree client-local execution is disabled (fail closed): '
        'run `hub sessions cleanup --apply` without --tree to enqueue owned stops; '
        'the owning host removes the tree via claim/effect/ack and the outcome '
        'is observed via show/commands')


def wait_for_session(session_id, timeout, interval, *, stopping=False):
    deadline = time.monotonic() + timeout
    return wait_until(deadline, interval,
                     lambda: request('GET', '/api/sessions/' + session_id),
                     lambda view: view.get('state') in (('stopped', 'failed', 'refused', 'held', 'uncertain') if stopping else TERMINAL_SESSION),
                     'Session wait')


def wait_for_command(session_id, command_id, timeout, interval):
    deadline = time.monotonic() + timeout

    def fetch():
        # Bounded complete pagination: commands past the first page (e.g.
        # command #101) live behind next_cursor; follow it instead of
        # failing on page one. Capped pages keep the wait bounded.
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

    return wait_until(deadline, interval,
                     fetch,
                     lambda entry: entry.get('state') in TERMINAL_COMMAND,
                     'Command wait')


def add_commands(sub):
    root = sub.add_parser('sessions', help='Hub session orchestration: grouped lifecycle listing by default; list/start/send/capture/stop/cleanup/observe')
    commands = root.add_subparsers(dest='sessions_cmd', required=False)
    listing = commands.add_parser('list', help='List sessions visible to this credential')
    listing.add_argument('--state')
    listing.add_argument('--limit', type=int, default=50)
    start = commands.add_parser('start', help='Record a start intent; placement reuses existing fleet routing')
    start.add_argument('--engine')
    start.add_argument('--machine')
    start.add_argument('--workspace', required=True)
    start.add_argument('--tier', choices=TIERS)
    start.add_argument('--cwd')
    mode = start.add_mutually_exclusive_group()
    mode.add_argument('--brief-file', help='UTF-8 goal file for headless work (max 8 KiB)')
    mode.add_argument('--interactive', action='store_true', help='Start at an empty native prompt')
    start.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(start)
    send = commands.add_parser('send', help='Enqueue one prompt delivery on a bound session')
    send.add_argument('id')
    send.add_argument('--body-file', required=True, help='UTF-8 prompt file, or - for stdin (max 16 KiB)')
    send.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(send)
    capture = commands.add_parser('capture', help='Enqueue one bounded sanitized capture on a bound session')
    capture.add_argument('id')
    capture.add_argument('--max-bytes', type=int, default=65536)
    capture.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(capture)
    stop = commands.add_parser('stop', help='Enqueue stop; the hub releases capacity only on confirmed native exit')
    stop.add_argument('id')
    stop.add_argument('--key', help='Stable idempotency key; printed back for safe retry after lost responses')
    add_wait(stop)
    show = commands.add_parser('show', help='Show one session with its binding state')
    show.add_argument('id')
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
    cleanup = commands.add_parser('cleanup', help='Plan finished/superseded cleanup (dry-run default; --apply enqueues real M12 stop intents)')
    cleanup.add_argument('--apply', action='store_true', help='Act: enqueue owned stops via M12 custody (server-authenticated owner only; host executes via claim/ack)')
    cleanup.add_argument('--key', default=None, help='Stable idempotency key for --apply (same key replays, never duplicates)')
    cleanup.add_argument('--tree', action='store_true', help='DISABLED fail-closed (M15d blocker): client-local tree execution refused before any HTTP; omit --tree, the owning host removes trees via claim/effect/ack')
    cleanup.add_argument('--machine-id', default=None, help='Cleanup target machine id (with --tree; must be the presenting host)')
    cleanup.add_argument('--native-id', default=None, help='Cleanup target native session id (with --tree)')
    # NOTE: --owner already exists above as the scope filter; --tree reuses it
    # as the tree-target owner (server custody decides, never JSON).
    cleanup.add_argument('--worktree', default=None, help='Explicit owned worktree path to remove (with --tree; must sit under --cleanup-root)')
    cleanup.add_argument('--branch', default=None, help='Explicit owned branch to delete (with --tree; must be the worktree branch)')
    cleanup.add_argument('--journal-dir', default=None, help='Absolute journal directory for durable cleanup records (with --tree)')
    cleanup.add_argument('--cleanup-root', action='append', default=None, help='Trusted cleanup root (repeatable; with --tree)')
    cleanup.add_argument('--aplexer-bin', default='aplexer', help='Native executable for live-use inventory (with --tree)')
    cleanup.add_argument('--actor', default=None, help='Local custody actor (with --tree; default machine:<machine-id>)')
    cleanup.add_argument('--credential-family', default=None, help='Local custody family label (with --tree; server enforces the real family)')
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
    observe.add_argument('--superseded-by', default=None)
    observe.add_argument('--note', default=None, help='Opt-in free text (bounded, redacted server-side; reason codes preferred)')


def execute(args):
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
            # (HOST-config roots + canonical binding workspace, never
            # caller-supplied paths). Client-local --tree Git execution is
            # DISABLED fail-closed: refuse BEFORE any HTTP write so an
            # invalid/future tree invocation performs zero mutations.
            # Never claim success here:
            # the server reports enqueued commands + kept reasons.
            if getattr(args, 'tree', False):
                raise ValueError(
                    'cleanup --tree client-local execution is disabled (fail closed): '
                    'run `hub sessions cleanup --apply` without --tree to enqueue owned stops; '
                    'the owning host removes the tree via claim/effect/ack and the outcome '
                    'is observed via show/commands')
            body = {'apply': True}
            if getattr(args, 'key', None):
                body['key'] = args.key
            applied = request('POST', path, body)
            return applied
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
                     'has_unresolved_send_stop', 'worktree_missing'):
            if getattr(args, flag, False):
                fields[flag] = True
        if getattr(args, 'note', None):
            fields['observation_note'] = args.note
        payload = build_observation_payload(fields)
        body = {'machine_id': args.machine_id, 'native_id': args.native_id, 'payload': payload}
        if getattr(args, 'observed_at', None):
            body['observed_at'] = args.observed_at
        return request('POST', '/api/sessions/observations', body)
    if cmd == 'list':
        wanted = getattr(args, 'state', None)
        limit = int(getattr(args, 'limit', None) or 50)
        if not wanted:
            params = ''
            if getattr(args, 'limit', None):
                params = '?limit=%d' % limit
            return request('GET', '/api/sessions' + params)
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
            page = request('GET', path)
            for entry in page.get('sessions', []):
                if entry.get('id') in known:
                    continue
                known.add(entry.get('id'))
                if entry.get('state') == wanted:
                    seen.append(entry)
                    if len(seen) >= limit:
                        return {'sessions': seen[:limit], 'next_cursor': None}
            cursor = page.get('next_cursor')
            if not cursor:
                break
        return {'sessions': seen[:limit], 'next_cursor': None}
    if cmd == 'show':
        return request('GET', '/api/sessions/' + args.id)
    if cmd == 'commands':
        return request('GET', '/api/sessions/' + args.id + '/commands')
    if cmd == 'start':
        key = getattr(args, 'key', None) or str(uuid.uuid4())
        payload = {'key': key, 'workspace': args.workspace}
        for field in ('engine', 'machine', 'tier', 'cwd'):
            value = getattr(args, field, None)
            if value:
                payload[field] = value
        payload['mode'] = 'interactive' if getattr(args, 'interactive', False) else 'headless'
        if getattr(args, 'brief_file', None):
            if payload['mode'] == 'interactive':
                raise ValueError('Interactive sessions start at an empty native prompt')
            payload['brief'] = read_file(args.brief_file, MAX_BRIEF)
        try:
            created = request('POST', '/api/sessions', payload)
        except ValueError as exc:
            # Lost-response reconciliation: the key decides replay vs conflict,
            # never an unkeyed retry.
            raise ValueError('%s (reconcile with --key %s via show/commands)' % (exc, key)) from None
        created = dict(created, client_key=key)
        timeout, interval = check_wait_args(args)
        if timeout is not None:
            created['binding_wait'] = wait_for_session(created['id'], timeout, interval)
        return created
    if cmd in ('send', 'capture', 'stop'):
        key = getattr(args, 'key', None) or str(uuid.uuid4())
        if cmd == 'send':
            payload = {'key': key, 'body': read_file(args.body_file)}
        elif cmd == 'capture':
            payload = {'key': key, 'max_bytes': args.max_bytes}
        else:
            payload = {'key': key}
        try:
            queued = request('POST', '/api/sessions/' + args.id + '/' + cmd, payload)
        except ValueError as exc:
            raise ValueError('%s (reconcile with --key %s via commands)' % (exc, key)) from None
        queued = dict(queued, client_key=key)
        timeout, interval = check_wait_args(args)
        if timeout is not None:
            if cmd == 'stop':
                queued['stop_wait'] = wait_for_session(args.id, timeout, interval, stopping=True)
            else:
                queued['command_wait'] = wait_for_command(args.id, queued['command_id'], timeout, interval)
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
        return receive_once(custody=custody, journal_dir=journal_dir,
                            executable=getattr(args, 'aplexer_bin', 'aplexer') or 'aplexer',
                            request=request, limit=limit,
                            sender_workspace=getattr(args, 'sender_workspace', None))
    raise ValueError('Unknown sessions command')
