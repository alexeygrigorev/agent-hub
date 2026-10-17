"""Private durable task context. Task text is data; this client never executes it."""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode
from . import api, auth, config
from .conversations import valid_key

BASE = '/api/intake'
MAX_REQUEST = 32768


class StaleSnapshot(ValueError):
    pass


def request(method, path, payload=None):
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    if data is not None and len(data) > MAX_REQUEST:
        raise ValueError('Intake request exceeds capacity')
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or auth.get_access_token()

    def send(access):
        req = urllib.request.Request(base.rstrip('/') + path, data=data, method=method,
                                     headers={'Authorization': 'Bearer ' + access, 'Content-Type': 'application/json'})
        return api.safe_http_json(req)

    try:
        return send(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not env_token:
            auth.refresh_token()
            try:
                return send(auth.get_access_token())
            except urllib.error.HTTPError as retry:
                if retry.code == 409 and method == 'GET':
                    raise StaleSnapshot('Snapshot changed') from None
                raise ValueError('Intake HTTP request failed') from None
        if exc.code == 409 and method == 'GET':
            raise StaleSnapshot('Snapshot changed') from None
        raise ValueError('Intake HTTP request failed') from None


def read_file(path):
    if path == '-':
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    else:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise ValueError('Input exceeds capacity')
    return raw.decode('utf-8')


def add_commands(sub):
    root = sub.add_parser('intake', help='Private intake tasks and durable context; payloads are data, never execution')
    commands = root.add_subparsers(dest='intake_cmd', required=True)
    create = commands.add_parser('create', help='Capture verbatim UTF-8 text from file/stdin; missing provenance stays unknown')
    create.add_argument('--project', required=True)
    create.add_argument('--title', required=True)
    intake_body = create.add_mutually_exclusive_group(required=True)
    intake_body.add_argument('--text-file')
    intake_body.add_argument('--voice-pending', action='store_true', help='Capture voice pointer now with null text and pending transcription')
    create.add_argument('--source-type', choices=['chat', 'voice', 'forwarded', 'unknown'], default='unknown')
    create.add_argument('--source-reference')
    create.add_argument('--source-time')
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
    update.add_argument('--version', type=int, required=True)
    update.add_argument('--key', required=True)
    update.add_argument('--clear', action='append', choices=['current_state', 'next_step', 'owner', 'uncertainty'], default=[], help='Explicitly clear a context field to unknown (repeatable)')
    for field in ('status', 'title', 'current-state', 'next-step', 'owner', 'uncertainty'):
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
    revoke = commands.add_parser('revoke', help='Operator only: revoke an intake grant, preserving its history')
    revoke.add_argument('id')
    revoke.add_argument('--payload-file', required=True)
    handoff = sub.add_parser('handoff', help='Self-contained ALL authorized open work and operator rules; fails without a complete consistent snapshot')
    handoff.add_argument('--project')
    handoff.add_argument('--page-size', type=int, default=25)


def safe_id(value):
    valid_key(value)
    if value in ('.', '..') or '/' in value:
        raise ValueError('Invalid identifier')
    return value


def json_file(path):
    result = json.loads(read_file(path))
    if not isinstance(result, dict):
        raise ValueError('JSON object required')
    return result


def handoff(project=None, page_size=25):
    if not 1 <= page_size <= 25:
        raise ValueError('Invalid page size')
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
                        raise ValueError('Duplicate snapshot item')
                    seen.add(task['id'])
                    tasks.append(task)
                for rule in page['operator_rules']:
                    rules[rule['id']] = rule
                if len(tasks) > 256:
                    raise ValueError('Snapshot exceeds capacity')
                after = page['next']
                if not after:
                    if len(tasks) != page['total']:
                        raise ValueError('Incomplete snapshot')
                    return {'snapshot_version': revision, 'complete': True, 'authorized_scope': page['authorized_scope'],
                            'data_notice': page['data_notice'], 'operator_rules': list(rules.values()), 'tasks': tasks,
                            'total': len(tasks)}
            raise ValueError('Snapshot page bound exceeded')
        except StaleSnapshot:
            continue
    raise ValueError('Snapshot kept changing; retry handoff')


def execute(args):
    if args.cmd == 'handoff':
        return handoff(args.project, args.page_size)
    command = args.intake_cmd
    if command == 'create':
        valid_key(args.key)
        return request('POST', BASE + '/tasks', {'key': args.key, 'project': args.project, 'title': args.title,
                       'text': None if args.voice_pending else read_file(args.text_file),
                       'transcription': 'pending' if args.voice_pending else ('provided' if args.source_type == 'voice' else 'not_applicable'), 'source': {'type': args.source_type,
                       'reference': args.source_reference, 'timestamp': args.source_time}})
    if command == 'list':
        params = {'status': args.status, 'limit': args.limit}
        for field in ('project', 'after', 'revision', 'authorization_valid_until'):
            if getattr(args, field) is not None:
                params[field] = getattr(args, field)
        return request('GET', BASE + '/tasks?' + urlencode(params))
    if command in ('rules', 'grants'):
        return request('POST' if args.payload_file else 'GET', BASE + '/' + command,
                       json_file(args.payload_file) if args.payload_file else None)
    path = BASE + '/tasks/' + safe_id(args.id)
    if command in ('show', 'history', 'links', 'context'):
        return request('GET', path + ('' if command == 'show' else '/' + command))
    if command == 'revoke':
        return request('POST', BASE + '/grants/' + safe_id(args.id) + '/revoke', json_file(args.payload_file))
    valid_key(args.key)
    payload = {'key': args.key, 'expected_version': args.version}
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
                raise ValueError('Interactive sessions start at an empty native prompt')
            with (sys.stdin.buffer if args.brief_file == '-' else Path(args.brief_file).open('rb')) as stream:
                raw = stream.read(8192 + 1)
            if len(raw) > 8192:
                raise ValueError('Brief exceeds capacity')
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
        for field in ('status', 'title', 'current_state', 'next_step', 'owner', 'uncertainty'):
            if getattr(args, field) is not None:
                payload[field] = getattr(args, field)
        for field in args.clear:
            if getattr(args, field) is not None:
                raise ValueError('Cannot set and clear one field together')
            payload[field] = None
        return request('POST', path, payload)
    raise ValueError('Unknown intake operation')
