"""`hub msg`: cross-device session messaging CLI (docs/SESSION-MESSAGING.md §11).

Identity, never taken from flags:
- ``--profile NAME``: a named agent profile (``~/.config/agent-hub/NAME.json``,
  e.g. the Developer's ``agent:developer``) written by
  ``hub msg agent-credential NAME --output FILE``;
- inside a Hub session: the session's own credential that the bound host
  provisions (``session:<hub-session-id>``), found via APLEXER_SESSION_ID;
- otherwise the normal profile (HUB_CONFIG_FILE, human or host), which must
  name its sender with ``--from``.
Tokens are never printed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from . import private_fs

PROFILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
CREDENTIAL_LIMIT = 16384


def session_auth_dir() -> Path:
    """Per-user directory the host writes session credentials into."""
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
        return Path(base) / 'agent-hub' / 'session-auth'
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'agent-hub' / 'session-auth'


def profile_path(name: str) -> Path:
    if not PROFILE_NAME.fullmatch(name):
        raise ValueError('Invalid profile name')
    from .config import get_human_config_path
    saved = os.environ.pop('HUB_CONFIG_FILE', None)
    try:
        return get_human_config_path().parent / (name + '.json')
    finally:
        if saved is not None:
            os.environ['HUB_CONFIG_FILE'] = saved


def resolve_identity(profile: str | None) -> tuple[str | None, str]:
    """Select the credential file (via HUB_CONFIG_FILE) and return (actor, source)."""
    path = None
    source = 'default'
    if profile:
        path, source = profile_path(profile), 'profile:' + profile
        if not path.is_file():
            raise ValueError('No such profile; ask an operator for `hub msg agent-credential`')
    elif os.environ.get('APLEXER_SESSION_ID') and not os.environ.get('HUB_CONFIG_FILE'):
        native = os.environ['APLEXER_SESSION_ID'].strip().lower()
        if re.fullmatch(r'[0-9a-f-]{36}', native):
            candidate = session_auth_dir() / (native + '.json')
            if candidate.is_file():
                path, source = candidate, 'session'
    if path is None:
        return None, source
    os.environ['HUB_CONFIG_FILE'] = str(path)
    data = json.loads(private_fs.read_bytes(str(path), CREDENTIAL_LIMIT).decode('utf-8'))
    actor = data.get('actor')
    return (actor if isinstance(actor, str) else None), source


def _body(args) -> str:
    if args.body_file:
        text = Path(args.body_file).read_text(encoding='utf-8')
    elif args.body == '-':
        text = sys.stdin.read()
    else:
        text = args.body
    if not isinstance(text, str) or not text.strip():
        raise ValueError('A non-empty body is required')
    return text


def add_commands(sub):
    msg = sub.add_parser('msg', help='Cross-device messages to sessions, named agents and humans')
    msg.add_argument('--profile', help='Named agent profile, e.g. developer')
    ms = msg.add_subparsers(dest='msg_cmd', required=True)
    send = ms.add_parser('send', help='Send to session:<id>, <tag>@<machine>, agent:<name> or a bus address')
    send.add_argument('--to', required=True)
    send.add_argument('--body', default=None, help='Text, or - for stdin')
    send.add_argument('--body-file')
    send.add_argument('--from', dest='from_actor', help='Sender for default (human/host) profiles only')
    mode = send.add_mutually_exclusive_group()
    mode.add_argument('--tui', action='store_const', const='tui', dest='delivery', help='Type into the session prompt')
    mode.add_argument('--inbox', action='store_const', const='inbox', dest='delivery', help='Native inbox only')
    send.add_argument('--thread')
    send.add_argument('--deliver-by-s', type=int)
    send.add_argument('--key', help='Idempotency key; printed back for safe retries')
    reply = ms.add_parser('reply', help='Reply to a message; threads back to its sender on any host')
    reply.add_argument('id')
    reply.add_argument('--body', default=None, help='Text, or - for stdin')
    reply.add_argument('--body-file')
    reply.add_argument('--from', dest='from_actor')
    reply.add_argument('--key')
    inbox = ms.add_parser('inbox', help='List this identity\'s inbox')
    inbox.add_argument('--actor', help='Default: this identity')
    inbox.add_argument('--unread', action='store_true')
    inbox.add_argument('--wait', type=int, default=0, help='Poll up to N seconds (max 600) for new messages')
    inbox.add_argument('--cursor')
    show = ms.add_parser('show', help='Show one message (marks it read for its recipient)')
    show.add_argument('id')
    status = ms.add_parser('status', help='Delivery state and audit events of one message')
    status.add_argument('id')
    ms.add_parser('whoami', help='Print the sending identity (no secrets)')
    cred = ms.add_parser('agent-credential', help='Operator: create agent:<name> and write its own credential file')
    cred.add_argument('name')
    cred.add_argument('--output', required=True, help='New private file, e.g. ~/.config/agent-hub/developer.json')


def execute(args):
    from . import bus
    from .config import load_config
    if args.msg_cmd == 'agent-credential':
        if not PROFILE_NAME.fullmatch(args.name):
            raise ValueError('Invalid agent name')
        base_url = load_config().get('base_url')
        out = Path(os.path.expanduser(args.output)).absolute()
        if out.exists():
            raise ValueError('Output exists; choose a new file')
        minted = bus._do_request('POST', '/api/agents/named/' + quote(args.name, safe='') + '/credential', {})
        record = {'base_url': base_url, 'client_kind': 'agent', 'actor': minted['actor'],
                  'access_token': minted['access_token'], 'refresh_token': minted['refresh_token']}
        out.parent.mkdir(parents=True, exist_ok=True)
        private_fs.atomic_write(str(out), json.dumps(record).encode('utf-8'), CREDENTIAL_LIMIT)
        return {'actor': minted['actor'], 'expires_at': minted.get('expires_at'), 'written': str(out)}
    actor, source = resolve_identity(getattr(args, 'profile', None))
    if args.msg_cmd == 'whoami':
        return {'actor': actor, 'source': source}
    if args.msg_cmd in ('send', 'reply'):
        sender = actor or args.from_actor
        if not sender:
            raise ValueError('This profile has no messaging identity; pass --from')
        if actor and args.from_actor and args.from_actor != actor:
            raise ValueError('--from differs from this identity')
        key = args.key or str(uuid.uuid4())
        if args.msg_cmd == 'send':
            payload = {'from_actor': sender, 'to_actor': args.to, 'kind': 'note', 'body': _body(args), 'idempotency_key': key}
            if args.delivery:
                payload['delivery'] = args.delivery
            if args.thread:
                payload['thread_id'] = args.thread
            if args.deliver_by_s is not None:
                payload['deliver_by_s'] = args.deliver_by_s
            result = bus._do_request('POST', '/api/bus/send', payload)
        else:
            original = bus._do_request('GET', '/api/bus/messages/' + quote(args.id, safe=''))
            to = original['from_actor'] if original.get('to_actor') == sender else original['to_actor']
            result = bus._do_request('POST', '/api/bus/reply', {'from_actor': sender, 'to_actor': to, 'kind': 'reply',
                                                               'body': _body(args), 'reply_to': args.id, 'idempotency_key': key})
        return {**result, 'from': sender, 'idempotency_key': key}
    if args.msg_cmd == 'inbox':
        who = args.actor or actor
        if not who:
            raise ValueError('Pass --actor for this profile')
        deadline = time.monotonic() + max(0, min(args.wait, 600))
        while True:
            path = '/api/bus/inbox?actor=' + quote(who, safe='') + ('&cursor=' + quote(args.cursor, safe='') if args.cursor else '')
            rows = bus._do_request('GET', path)
            rows = rows if isinstance(rows, list) else rows.get('messages', [])
            if args.unread:
                rows = [r for r in rows if not r.get('read_at')]
            if rows or time.monotonic() >= deadline:
                return {'actor': who, 'messages': rows}
            time.sleep(3)
    if args.msg_cmd == 'show':
        return bus._do_request('GET', '/api/bus/messages/' + quote(args.id, safe=''))
    return bus._do_request('GET', '/api/bus/messages/' + quote(args.id, safe='') + '/status')
