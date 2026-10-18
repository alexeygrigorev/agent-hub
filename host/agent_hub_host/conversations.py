"""Stdlib conversation CLI. Author identity is always resolved by the server."""
from pathlib import Path
import sys
import re
from urllib.parse import quote, urlencode
import json
import os
import urllib.error
import urllib.request
from . import api, auth, config

MAX_BODY_BYTES = 16384


def read_body(path):
    """Read a bounded UTF-8 file or stdin; never put message text in argv."""
    if path == '-':
        text = sys.stdin.read(MAX_BODY_BYTES + 1)
    else:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BODY_BYTES + 1)
        text = raw.decode('utf-8')
    if not text.strip() or len(text.encode('utf-8')) > MAX_BODY_BYTES:
        raise ValueError('Message must contain 1..16384 UTF-8 bytes')
    return text


def add_commands(sub):
    root = sub.add_parser('conversations', help='Authenticated conversation operations (JSON output)')
    commands = root.add_subparsers(dest='conversation_cmd', required=True)
    listing = commands.add_parser('list', help='List conversations visible to this credential')
    listing.add_argument('--state', choices=['active', 'archived', 'all'], default='active')
    listing.add_argument('--limit', type=int, default=50)
    listing.add_argument('--cursor')
    commands.add_parser('participants', help='Discover verified self and registered participants')
    create = commands.add_parser('create', help='Create with an explicit stable retry key')
    create.add_argument('--title', required=True)
    create.add_argument('--kind', choices=['direct', 'group'], default='group')
    create.add_argument('--member', action='append', default=[], help='Registered actor (repeatable); creator is included automatically')
    create.add_argument('--key', required=True, help='Reuse exactly this key and payload after uncertain results')
    get = commands.add_parser('get')
    get.add_argument('id')
    members = commands.add_parser('members')
    members.add_argument('id')
    members.add_argument('--include-removed', action='store_true')
    change = members.add_mutually_exclusive_group()
    change.add_argument('--add', help='Owner only: add a registered participant')
    change.add_argument('--remove', help='Owner only: remove a member')
    members.add_argument('--role', choices=['member', 'owner'], default='member')
    history = commands.add_parser('history', help='Fetch one page; does not mark messages read')
    history.add_argument('id')
    history.add_argument('--limit', type=int, default=50)
    page = history.add_mutually_exclusive_group()
    page.add_argument('--after-seq', type=int)
    page.add_argument('--before-seq', type=int)
    send = commands.add_parser('send', help='Send UTF-8 file/stdin text using a stable retry key')
    send.add_argument('id')
    send.add_argument('--body-file', required=True, help='UTF-8 file or - for stdin')
    send.add_argument('--key', required=True, help='Reuse exactly this key and body after uncertain results')
    send.add_argument('--kind', choices=['message', 'note', 'reply', 'status', 'handoff', 'action'], default='message')
    send.add_argument('--reply-to', help='Message ID in the same conversation')
    read = commands.add_parser('read', help='Explicitly advance own read cursor')
    read.add_argument('id')
    read.add_argument('--seq', type=int, required=True, help='Explicit sequence already consumed; never implicitly marks latest')
    bridge = commands.add_parser('bridge', help='Opt-in bounded native inbox client for this agent credential')
    runs = bridge.add_mutually_exclusive_group()
    runs.add_argument('--once', dest='cycles', action='store_const', const=1)
    runs.add_argument('--cycles', type=int, default=1)
    bridge.add_argument('--enable-native', action='store_true', help='Explicitly allow own durable mailbox and hub read/reply effects')
    bridge.add_argument('--actor')
    bridge.add_argument('--project', help='Exact registered project label (never inferred from path)')
    bridge.add_argument('--workspace', help='Explicit canonical local workspace for this project')
    bridge.add_argument('--machine-id', help='Must match existing authenticated host config')
    bridge.add_argument('--conversation', action='append', default=[])
    links = commands.add_parser('links', help='Inspect or manage canonical conversation links')
    links.add_argument('id')
    links.add_argument('--attach-kind', choices=['intake_task', 'fleet_job', 'routing_job'], help='Kind of target to link')
    links.add_argument('--attach-ref', help='Target identifier (intake task UUID or job ID)')
    links.add_argument('--key', help='Idempotency retry key for attaching link')
    links.add_argument('--remove', help='Link ID to remove')
    links.add_argument('--search-kind', choices=['intake_task', 'fleet_job', 'routing_job'], help='Filter by target kind')
    links.add_argument('--search-ref', help='Filter by target ref')
    follow = commands.add_parser('follow-through', help='Read typed conversation follow-through authority facts and obligations')
    follow.add_argument('id')
    follow.add_argument('--link', help='Optional link ID to focus follow-through facts')


def execute(args):
    if args.conversation_cmd == 'bridge':
        from .conversation_bridge import run_cycles
        if not args.enable_native:
            return run_cycles(args.cycles)
        cfg = config.load_config()
        if not args.machine_id or cfg.get('machine_id') != args.machine_id:
            raise ValueError('Bridge machine must match existing authenticated host config')
        base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
        api.check_url(base)
        return run_cycles(args.cycles, enabled=True, actor=args.actor, project=args.project,
                          workspace=args.workspace, machine_id=args.machine_id,
                          conversation_ids=args.conversation, origin=base.rstrip('/'))
    command = args.conversation_cmd
    base = '/api/conversations'
    if command == 'participants':
        return request('GET', base + '/participants')
    if command == 'list':
        params = {'state': args.state, 'limit': valid_limit(args.limit)}
        if args.cursor is not None:
            params['cursor'] = args.cursor
        return request('GET', base + '?' + urlencode(params))
    if command == 'create':
        valid_key(args.key)
        if not args.title.strip() or len(args.title) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in args.title):
            raise ValueError('Invalid title')
        return request('POST', base, {'kind': args.kind, 'title': args.title,
                       'members': args.member, 'idempotency_key': args.key})
    # Reject path separators/dot segments rather than let an intermediary normalize
    # a user-controlled conversation ID into another authenticated API route.
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.id):
        raise ValueError('Invalid conversation id')
    path = base + '/' + quote(args.id, safe='')
    if command == 'get':
        return request('GET', path)
    if command == 'members':
        if args.add is not None:
            return request('POST', path + '/members', {'actor': args.add, 'role': args.role})
        if args.remove is not None:
            return request('POST', path + '/members/remove', {'actor': args.remove})
        return request('GET', path + '/members' + ('?include_removed=true' if args.include_removed else ''))
    if command == 'history':
        params = {'limit': valid_limit(args.limit)}
        if args.after_seq is not None:
            params['after_seq'] = valid_seq(args.after_seq)
        if args.before_seq is not None:
            params['before_seq'] = valid_seq(args.before_seq, minimum=1)
        return request('GET', path + '/messages?' + urlencode(params))
    if command == 'send':
        valid_key(args.key)
        payload = {'body': read_body(args.body_file), 'kind': args.kind, 'idempotency_key': args.key}
        if args.reply_to is not None:
            payload['reply_to'] = args.reply_to
        return request('POST', path + '/messages', payload)
    if command == 'read':
        return request('POST', path + '/read', {'seq': valid_seq(args.seq)})
    if command == 'links':
        if args.attach_kind is not None or args.attach_ref is not None:
            if not (args.attach_kind and args.attach_ref):
                raise ValueError('Both --attach-kind and --attach-ref are required to attach a link')
            payload = {'kind': args.attach_kind, 'ref': args.attach_ref}
            if args.key:
                valid_key(args.key)
                payload['idempotency_key'] = args.key
            return request('POST', path + '/links', payload)
        if args.remove is not None:
            return request('DELETE', path + '/links/' + quote(args.remove, safe=''))
        params = {}
        if args.search_kind is not None:
            params['target_kind'] = args.search_kind
        if args.search_ref is not None:
            params['target_ref'] = args.search_ref
        query = ('?' + urlencode(params)) if params else ''
        return request('GET', path + '/links' + query)
    if command == 'follow-through':
        if args.link:
            return request('GET', path + '/links/' + quote(args.link, safe='') + '/follow-through')
        return request('GET', path + '/follow-through')
    raise ValueError('Unknown conversation operation')


def valid_key(value):
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value):
        raise ValueError('Invalid retry key')


def valid_limit(value):
    if not 1 <= value <= 100:
        raise ValueError('Invalid limit')
    return value


def valid_seq(value, minimum=0):
    if not minimum <= value <= 9007199254740991:
        raise ValueError('Invalid sequence')
    return value


def request(method, path, payload=None):
    """Reuse safe no-redirect JSON HTTP and the existing bearer/refresh flow.

    Send UTF-8 JSON directly: ASCII-escaping a valid 16 KiB Unicode body can
    exceed the API's separate 32 KiB HTTP request bound.
    """
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    if data is not None and len(data) > 32768:
        raise ValueError('Encoded request exceeds 32 KiB')
    env_token = os.environ.get('HUB_JOB_TOKEN') or os.environ.get('HUB_TOKEN')
    token = env_token or auth.get_access_token()
    url = base.rstrip('/') + path

    def send(access_token):
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + access_token, 'Content-Type': 'application/json'})
        return api.safe_http_json(req)

    try:
        return send(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not env_token:
            auth.refresh_token()
            return send(auth.get_access_token())
        raise ValueError('Conversation HTTP request failed') from None
