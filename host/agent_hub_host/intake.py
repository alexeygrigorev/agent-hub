"""Private durable task context. Task text is data; this client never executes it."""
import http.client
import socket
import json
import os
import re
import uuid
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode
from . import api, auth, config

BASE = '/api/intake'
MAX_REQUEST = 32768
EVENT_KINDS = ('intake', 'note', 'decision', 'followup', 'blocker', 'pending_approval',
               'link', 'session', 'report', 'commit', 'rule_reference', 'resolution',
               'approval_decision', 'owner_acceptance', 'first_action', 'work_action',
               'independent_review', 'accepted_outcome', 'human_delivery', 'obligation', 'obligation_response')

# Server error slugs are lowercase snake_case (longest today is 30 chars).
# Passing the slug through keeps the exact failure (auth, grant, capacity,
# field) while tokens, URLs, hex digests and terminal controls can never
# match and still collapse to request_failed.
SAFE_ERROR = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


class StaleSnapshot(ValueError):
    pass


class IntakeHTTPError(ValueError):
    def __init__(self, status, code='request_failed', key=None):
        self.status, self.code = status, code
        super().__init__(f'Intake HTTP {status}: {code}' + (f'; retry key: {key}' if key else ''))


class IntakeTransportError(ValueError):
    pass


class IntakeInputError(ValueError):
    """A client diagnostic containing only fixed labels and limits."""


def valid_key(value):
    # Match the server identifier exactly; rewriting keys breaks idempotency.
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value):
        raise IntakeInputError('Invalid retry key: use 1-128 characters, starting with a letter or digit; allowed: letters, digits, . _ : -')


def diagnostic(exc):
    """Never interpolate exception bodies: they may contain credentials or payloads."""
    if isinstance(exc, (IntakeHTTPError, IntakeTransportError, IntakeInputError, config.ConfigPermissionError)):
        return str(exc)
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


def http_error(exc, method, payload):
    if exc.code == 409 and method == 'GET':
        return StaleSnapshot('Snapshot changed')
    # Never echo arbitrary error bodies, tokens, URLs or terminal controls.
    # The server only emits lowercase snake_case slugs; surface the exact
    # slug so auth/grant/capacity/field detail survives, collapse the rest.
    body = None
    try:
        body = json.loads(exc.read(4097))
        code = body.get('error') if isinstance(body, dict) else None
    except Exception:
        code = None
    if not isinstance(code, str) or not SAFE_ERROR.match(code):
        code = 'request_failed'
    key = payload.get('key') if payload else None
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', key):
        key = None
    result = IntakeHTTPError(exc.code, code, key)
    detail = ''
    if isinstance(body, dict):
        field = body.get('field')
        if field in ('text', 'title', 'project', 'acceptance_criteria', 'current_state', 'next_step', 'owner', 'uncertainty'):
            detail += f'; field: {field}'
            limit = body.get('max_bytes')
            if type(limit) is int and 0 < limit <= MAX_REQUEST:
                detail += f'; max {limit} UTF-8 bytes'
    if code == 'invalid_event_kind':
        detail += '; valid kinds: ' + ', '.join(EVENT_KINDS)
    # Only locally generated wording is printed, never arbitrary server messages.
    result.args = (str(result) + detail,)
    return result


def request(method, path, payload=None):
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    if data is not None and len(data) > MAX_REQUEST:
        raise IntakeInputError(f'Intake request exceeds {MAX_REQUEST} UTF-8 bytes (JSON envelope included)')
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or auth.get_access_token()

    def send(access):
        req = urllib.request.Request(base.rstrip('/') + path, data=data, method=method,
                                     headers={'Authorization': 'Bearer ' + access, 'Content-Type': 'application/json'})
        try:
            return api.safe_http_json(req)
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException, ValueError, RuntimeError) as exc:
            reason = ('Intake response error: invalid JSON or UTF-8 response'
                      if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) else diagnostic(exc))
            recovery = '; mutation outcome unknown; inspect before retrying with the same key and payload' if method != 'GET' else ''
            raise IntakeTransportError(reason + recovery) from None

    try:
        return send(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not env_token:
            auth.refresh_token()
            try:
                return send(auth.get_access_token())
            except urllib.error.HTTPError as retry:
                raise http_error(retry, method, payload) from None
        raise http_error(exc, method, payload) from None


def read_file(path, limit=MAX_REQUEST, field='Input'):
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
    create.add_argument('--key', required=True)
    listing = commands.add_parser('list', help='One explicit revision-pinned page')
    listing.add_argument('--status', default='open')
    listing.add_argument('--project')
    listing.add_argument('--after')
    listing.add_argument('--revision', type=int)
    listing.add_argument('--authorization-valid-until', type=int)
    listing.add_argument('--limit', type=int, default=25)
    for name in ('show', 'history', 'links', 'context'):
        command = commands.add_parser(name)
        command.add_argument('id')
    start = commands.add_parser('continue', help='CAS ready -> active context only; does not start a session')
    start.add_argument('id')
    start.add_argument('--version', type=int, required=True)
    start.add_argument('--key', required=True)
    update = commands.add_parser('update', help='CAS status/context update; cannot imply execution, acceptance or approval')
    update.add_argument('id')
    update.add_argument('--version', type=int, help='Explicit CAS version; otherwise fetch and retry version conflicts up to three times')
    update.add_argument('--key', help='Stable retry key; generated automatically when omitted')
    update.add_argument('--clear', action='append', choices=['current_state', 'next_step', 'owner', 'uncertainty', 'acceptance_criteria'], default=[], help='Explicitly clear a context field to unknown (repeatable)')
    for field in ('status', 'title', 'current-state', 'next-step', 'owner', 'uncertainty', 'acceptance-criteria'):
        update.add_argument('--' + field)
    event = commands.add_parser('append', help='Append intake/note/session/report/commit/followup/blocker/decision/rule reference as JSON; approval decisions require operator')
    event.add_argument('id')
    event.add_argument('--version', type=int, required=True)
    event.add_argument('--key', required=True)
    event.add_argument('--event-file', required=True, help='Bounded UTF-8 JSON event file, or - for stdin')
    assign = commands.add_parser('assign', help='Assign work: create the canonical Hub session for a task and record its real id automatically')
    assign.add_argument('id')
    assign.add_argument('--version', type=int, required=True)
    assign.add_argument('--key', required=True)
    assign.add_argument('--workspace', required=True, help='Configured workspace label on the selected host')
    assign.add_argument('--engine', help='Requested engine label (existing model tiers still decide placement)')
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


def execute(args):
    if args.cmd == 'handoff':
        return handoff(args.project, args.page_size)
    command = args.intake_cmd
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
        return request('POST', BASE + '/tasks', body)
    if command == 'list':
        params = {'status': args.status, 'limit': args.limit}
        for field in ('project', 'after', 'revision', 'authorization_valid_until'):
            if getattr(args, field) is not None:
                params[field] = getattr(args, field)
        return request('GET', BASE + '/tasks?' + urlencode(params))
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
    if command in ('rules', 'grants'):
        return request('POST' if args.payload_file else 'GET', BASE + '/' + command,
                       json_file(args.payload_file) if args.payload_file else None)
    path = BASE + '/tasks/' + safe_id(args.id)
    if command in ('show', 'history', 'links', 'context'):
        return request('GET', path + ('' if command == 'show' else '/' + command))
    if command == 'revoke':
        return request('POST', BASE + '/grants/' + safe_id(args.id) + '/revoke', json_file(args.payload_file))
    key = args.key or ('update-' + uuid.uuid4().hex if command == 'update' else None)
    valid_key(key)
    payload = {'key': key, 'expected_version': args.version}
    if command == 'continue':
        return request('POST', path + '/continue', payload)
    if command == 'assign':
        session = {'workspace': args.workspace}
        for field in ('engine', 'tier', 'machine', 'cwd'):
            if getattr(args, field, None):
                session[field] = getattr(args, field)
        if getattr(args, 'parent', None):
            session['parent_id'] = args.parent
        session['mode'] = 'interactive' if args.interactive else 'headless'
        if args.brief_file:
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
        payload['event'] = json_file(args.event_file)
        return request('POST', path + '/events', payload)
    if command == 'update':
        for field in ('status', 'title', 'current_state', 'next_step', 'owner', 'uncertainty', 'acceptance_criteria'):
            if getattr(args, field, None) is not None:
                if field == 'acceptance_criteria' and len(args.acceptance_criteria.encode('utf-8')) > 8192:
                    raise IntakeInputError('Acceptance criteria exceed 8192 UTF-8 bytes')
                payload[field] = getattr(args, field)
        for field in args.clear:
            if getattr(args, field) is not None:
                raise IntakeInputError('Cannot set and clear one field together')
            payload[field] = None
        automatic = args.version is None
        for attempt in range(3):
            if automatic:
                current = request('GET', path)
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
    raise IntakeInputError('Unknown intake operation')
