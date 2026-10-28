"""`hub msg` and the messaging top-level commands (docs/SESSION-MESSAGING.md §11).

Identity, never taken from flags:
- ``--profile NAME``: a named agent profile (``~/.config/agent-hub/NAME.json``,
  e.g. the Developer's ``agent:developer``) written by
  ``hub msg agent-credential NAME --output FILE``;
- inside a Hub session: the session's own credential that the bound host
  provisions (``session:<hub-session-id>``), found via APLEXER_SESSION_ID;
- otherwise the normal profile (HUB_CONFIG_FILE, human or host), which must
  name its sender with ``--from`` / ``--actor``.

``--from`` is optional and refused when it differs from the credential for
new address kinds. Exit codes: 0 accepted, 2 refused (auth/validation),
3 recipient unknown or ambiguous, 4 transport error after retries.
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
from urllib.error import URLError
from urllib.parse import quote

from . import private_fs

PROFILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
CREDENTIAL_LIMIT = 16384
DURATION = re.compile(r'^(\d+)([smhd]?)([smhd]?)$', re.IGNORECASE)
DURATION_UNITS = {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}
HTTP_STATUS = re.compile(r'status (\d{3})')


class MsgError(ValueError):
    """Messaging failure with a CLI exit code (0/2/3/4 per §11).

    Subclasses ValueError so existing callers (tests, scripts) that catch
    ValueError for refused sends keep working."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


def exit_code_for(exc: BaseException) -> int:
    """Map a messaging failure to §11 exit codes: 2 refused, 3 unknown or
    ambiguous recipient, 4 transport error after retries."""
    if isinstance(exc, MsgError):
        return exc.exit_code
    text = str(exc)
    match = HTTP_STATUS.search(text)
    if match:
        status = int(match.group(1))
        if 'unknown_recipient' in text or 'ambiguous_recipient' in text:
            return 3
        if status >= 500:
            return 4
        return 2
    if isinstance(exc, ValueError):
        return 2
    return 4


def session_auth_dir(workspace: str) -> Path:
    """The session's private credential directory inside its own workspace
    (``<workspace>/.local/hub-session-auth``, docs/SESSION-MESSAGING.md §8);
    ``.local`` is gitignored and private_fs writes owner-only files."""
    return Path(workspace) / '.local' / 'hub-session-auth'


def profile_path(name: str) -> Path:
    if not PROFILE_NAME.fullmatch(name):
        raise MsgError('Invalid profile name')
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
            raise MsgError('No such profile; ask an operator for `hub msg agent-credential`')
    elif os.environ.get('APLEXER_SESSION_ID') and os.environ.get('APLEXER_WORKSPACE') and not os.environ.get('HUB_CONFIG_FILE'):
        native = os.environ['APLEXER_SESSION_ID'].strip().lower()
        if re.fullmatch(r'[0-9a-f-]{36}', native):
            candidate = session_auth_dir(os.environ['APLEXER_WORKSPACE']) / (native + '.json')
            if candidate.is_file():
                path, source = candidate, 'session'
    if path is None:
        return None, source
    os.environ['HUB_CONFIG_FILE'] = str(path)
    data = json.loads(private_fs.read_bytes(str(path), CREDENTIAL_LIMIT).decode('utf-8'))
    actor = data.get('actor')
    return (actor if isinstance(actor, str) else None), source


def resolve_sender(args, actor: str | None = None) -> str:
    """Sending identity: the credential's actor, else --from; a --from that
    differs from the credential is refused (§11). Pass the actor already
    resolved by resolve_identity — a second resolve would miss the session
    credential now that HUB_CONFIG_FILE points at it."""
    if actor is None:
        actor, _source = resolve_identity(getattr(args, 'profile', None))
    sender = actor or getattr(args, 'from_actor', None)
    if not sender:
        raise MsgError('This profile has no messaging identity; pass --from')
    if actor and getattr(args, 'from_actor', None) and args.from_actor != actor:
        raise MsgError('--from differs from this identity')
    return sender


def parse_duration(text: str) -> int:
    """`--deliver-by 24h` style: plain seconds or <n><s|m|h|d>, 60 s..7 d."""
    match = DURATION.fullmatch(str(text).strip().lower())
    if not match:
        raise MsgError('Invalid --deliver-by duration (use e.g. 3600, 90m, 24h, 7d)')
    value, unit, extra = match.groups()
    if extra:
        raise MsgError('Invalid --deliver-by duration (use one unit, e.g. 24h)')
    seconds = int(value) * DURATION_UNITS[unit]
    if seconds < 60 or seconds > 7 * 86400:
        raise MsgError('--deliver-by must be between 60 s and 7 d')
    return seconds


def parse_ref(text: str | None) -> dict | None:
    """`--ref JSON`: bounded large-payload reference, validated server-side."""
    if text is None:
        return None
    try:
        ref = json.loads(text)
    except ValueError:
        raise MsgError('--ref must be valid JSON')
    if not isinstance(ref, dict):
        raise MsgError('--ref must be a JSON object')
    return ref


def _request(method: str, path: str, payload=None, retries: bool = False):
    """One bus request; send/reply retry transport failures and 5xx with the
    SAME payload (same idempotency key), so a retry can never double-send."""
    from . import bus
    attempts = 3 if retries else 1
    for attempt in range(attempts):
        try:
            return bus._do_request(method, path, payload)
        except (URLError, TimeoutError, OSError) as exc:
            if attempt + 1 >= attempts:
                raise MsgError(f'Transport error after {attempts} attempts: {exc}'[:300], 4) from None
            time.sleep(1 + attempt)
        except ValueError as exc:
            match = HTTP_STATUS.search(str(exc))
            if match and int(match.group(1)) >= 500 and attempt + 1 < attempts:
                time.sleep(1 + attempt)
                continue
            raise
    raise MsgError('Transport error after retries', 4)


def _body(args) -> str:
    if getattr(args, 'body_file', None):
        text = Path(args.body_file).read_text(encoding='utf-8')
    elif args.body == '-':
        # Legacy bound: reject oversize stdin locally with the same message
        # and exit code the pre-§11 CLI used.
        text = sys.stdin.read(16385)
        if len(text.encode('utf-8')) > 16384:
            print('Error: stdin body exceeds 16 KiB limit', file=sys.stderr)
            raise SystemExit(1)
    else:
        text = args.body
    if not isinstance(text, str) or not text.strip():
        raise MsgError('A non-empty body is required')
    return text


def _send_payload(args, sender: str) -> dict:
    payload = {'from_actor': sender, 'to_actor': args.to, 'kind': getattr(args, 'kind', None) or 'note',
               'body': _body(args), 'idempotency_key': getattr(args, 'key', None) or str(uuid.uuid4())}
    delivery = getattr(args, 'delivery', None)
    if delivery:
        payload['delivery'] = delivery
    if getattr(args, 'thread', None):
        payload['thread_id'] = args.thread
    if getattr(args, 'reply_to', None):
        payload['reply_to'] = args.reply_to
    if getattr(args, 'lease_epoch', None) is not None:
        payload['lease_epoch'] = int(args.lease_epoch)
    deliver_by = getattr(args, 'deliver_by', None)
    if deliver_by is not None:
        payload['deliver_by_s'] = parse_duration(deliver_by)
    ref = parse_ref(getattr(args, 'ref', None))
    if ref is not None:
        payload['ref'] = ref
    return payload


def add_commands(sub):
    msg = sub.add_parser('msg', help='Cross-device messages to sessions, named agents and humans')
    msg.add_argument('--profile', help='Named agent profile, e.g. developer')
    ms = msg.add_subparsers(dest='msg_cmd', required=True)
    send = ms.add_parser('send', help='Send to session:<id>, <tag>@<machine>, agent:<name>, human:<sub> or a bus address')
    send.add_argument('--to', required=True)
    send.add_argument('--body', default=None, help='Text, or - for stdin')
    send.add_argument('--body-file')
    send.add_argument('--from', dest='from_actor', help='Sender for default (human/host) profiles only')
    mode = send.add_mutually_exclusive_group()
    mode.add_argument('--tui', action='store_const', const='tui', dest='delivery', help='Type into the session prompt')
    mode.add_argument('--inbox', action='store_const', const='inbox', dest='delivery', help='Native inbox only')
    send.add_argument('--thread')
    send.add_argument('--ref', help='Large payload by reference, JSON object (§10)')
    send.add_argument('--deliver-by', help='TUI delivery deadline, e.g. 24h (default 24 h, max 7 d)')
    send.add_argument('--deliver-by-s', type=int)
    send.add_argument('--key', help='Idempotency key; printed back for safe retries')
    reply = ms.add_parser('reply', help='Reply to a message; threads back to its sender on any host')
    reply.add_argument('id')
    reply.add_argument('--body', default=None, help='Text, or - for stdin')
    reply.add_argument('--body-file')
    reply.add_argument('--from', dest='from_actor')
    reply_mode = reply.add_mutually_exclusive_group()
    reply_mode.add_argument('--tui', action='store_const', const='tui', dest='delivery')
    reply_mode.add_argument('--inbox', action='store_const', const='inbox', dest='delivery')
    reply.add_argument('--ref', help='Large payload by reference, JSON object (§10)')
    reply.add_argument('--deliver-by', help='TUI delivery deadline, e.g. 24h')
    reply.add_argument('--key')
    inbox = ms.add_parser('inbox', help='List this identity\'s inbox')
    inbox.add_argument('--actor', help='Default: this identity')
    inbox.add_argument('--unread', action='store_true')
    inbox.add_argument('--thread', help='Only one thread id')
    inbox.add_argument('--wait', type=int, default=0, help='Long-poll up to N seconds (max 60) for new messages')
    inbox.add_argument('--mark-read', action='store_true', help='Mark the returned messages read')
    inbox.add_argument('--limit', type=int, default=50)
    inbox.add_argument('--cursor')
    show = ms.add_parser('show', help='Show one message (marks it read for its recipient)')
    show.add_argument('id')
    status = ms.add_parser('status', help='Delivery state and audit events of one message')
    status.add_argument('id')
    ms.add_parser('whoami', help='Print the sending identity (no secrets)')
    cred = ms.add_parser('agent-credential', help='Operator: create agent:<name> and write its own credential file')
    cred.add_argument('name')
    cred.add_argument('--output', required=True, help='New private file, e.g. ~/.config/agent-hub/developer.json')
    readdr = ms.add_parser('readdress', help='Operator: point an undelivered session message at a replacement session')
    readdr.add_argument('id')
    readdr.add_argument('--to', required=True, help='New session:<hub-session-id>')
    delegate = ms.add_parser('agent-delegate', help='Operator: grant or revoke an MCP client\'s delegation for agent:<name>')
    delegate.add_argument('name')
    delegate.add_argument('--client', help='MCP client id to grant (omit to list)')
    delegate.add_argument('--remove', action='store_true', help='Revoke instead of grant')


def execute(args):
    from . import bus
    from .config import load_config
    if args.msg_cmd == 'agent-credential':
        if not PROFILE_NAME.fullmatch(args.name):
            raise MsgError('Invalid agent name')
        base_url = load_config().get('base_url')
        out = Path(os.path.expanduser(args.output)).absolute()
        if out.exists():
            raise MsgError('Output exists; choose a new file')
        minted = _request('POST', '/api/agents/named/' + quote(args.name, safe='') + '/credential', {})
        record = {'base_url': base_url, 'client_kind': 'agent', 'actor': minted['actor'],
                  'access_token': minted['access_token'], 'refresh_token': minted['refresh_token']}
        out.parent.mkdir(parents=True, exist_ok=True)
        private_fs.atomic_write(str(out), json.dumps(record).encode('utf-8'), CREDENTIAL_LIMIT)
        return {'actor': minted['actor'], 'expires_at': minted.get('expires_at'), 'written': str(out)}
    if args.msg_cmd == 'agent-delegate':
        if not PROFILE_NAME.fullmatch(args.name):
            raise MsgError('Invalid agent name')
        path = '/api/agents/named/' + quote(args.name, safe='') + '/delegates'
        if args.client is None:
            return _request('GET', path)
        return _request('POST', path, {'client': args.client, 'remove': bool(args.remove)})
    if args.msg_cmd == 'readdress':
        return _request('POST', '/api/bus/messages/' + quote(args.id, safe='') + '/readdress', {'to': args.to})
    actor, source = resolve_identity(getattr(args, 'profile', None))
    if args.msg_cmd == 'whoami':
        return {'actor': actor, 'source': source}
    if args.msg_cmd in ('send', 'reply'):
        sender = resolve_sender(args, actor)
        key = getattr(args, 'key', None) or str(uuid.uuid4())
        args.key = key
        if args.msg_cmd == 'send':
            result = _request('POST', '/api/bus/send', _send_payload(args, sender), retries=True)
        else:
            original = _request('GET', '/api/bus/messages/' + quote(args.id, safe=''))
            to = original['from_actor'] if original.get('to_actor') == sender else original['to_actor']
            args.to = to
            result = _request('POST', '/api/bus/reply', _send_payload(args, sender), retries=True)
        return {**result, 'from': sender, 'idempotency_key': key}
    if args.msg_cmd == 'inbox':
        return inbox(args, actor)
    if args.msg_cmd == 'show':
        return _request('GET', '/api/bus/messages/' + quote(args.id, safe=''))
    return _request('GET', '/api/bus/messages/' + quote(args.id, safe='') + '/status')


def inbox(args, actor: str | None):
    """Shared inbox path for `hub msg inbox` and `hub inbox` (§11): one long-
    polled request, then an optional batch mark-read of what was returned."""
    who = getattr(args, 'actor', None) or actor
    if not who:
        raise MsgError('Pass --actor for this profile')
    wait = max(0, min(getattr(args, 'wait', 0) or 0, 60))
    params = ['actor=' + quote(who, safe='')]
    if getattr(args, 'unread', False):
        params.append('unread=1')
    if getattr(args, 'thread', None):
        params.append('thread=' + quote(args.thread, safe=''))
    if getattr(args, 'cursor', None):
        params.append('cursor=' + quote(args.cursor, safe=''))
    limit = getattr(args, 'limit', None)
    if limit is not None:
        # The server clamps 1..100; mirror that instead of refusing.
        limit = max(1, min(int(limit), 100))
        params.append('limit=' + str(limit))
    if wait:
        params.append('wait=' + str(wait))
    rows = _request('GET', '/api/bus/inbox?' + '&'.join(params))
    rows = rows if isinstance(rows, list) else rows.get('messages', [])
    result = {'actor': who, 'messages': rows}
    if getattr(args, 'mark_read', False) and rows:
        result['marked_read'] = _request('POST', '/api/bus/messages/read', {'ids': [r['id'] for r in rows]})
    return result


def _normalize_top_args(args):
    """Top-level parsers name a few destinations differently from `hub msg`."""
    if getattr(args, 'to', None) is None and getattr(args, 'to_actor', None) is not None:
        args.to = args.to_actor
    if getattr(args, 'thread', None) is None and getattr(args, 'thread_id', None) is not None:
        args.thread = args.thread_id
    return args


def top_send(args):
    """`hub send` with the §11 surface: new addresses, --tui/--inbox, --ref,
    --deliver-by; --from optional and refused on a credential mismatch."""
    _normalize_top_args(args)
    actor, _source = resolve_identity(getattr(args, 'profile', None))
    sender = resolve_sender(args, actor)
    key = getattr(args, 'idempotency_key', None) or str(uuid.uuid4())
    args.key = key
    result = _request('POST', '/api/bus/send', _send_payload(args, sender), retries=True)
    return {**result, 'from': sender, 'idempotency_key': key}


def top_reply(args):
    """`hub reply` with the §11 surface: --from/--to optional; the recipient
    defaults to the other end of the replied-to message."""
    _normalize_top_args(args)
    actor, _source = resolve_identity(getattr(args, 'profile', None))
    sender = resolve_sender(args, actor)
    key = getattr(args, 'idempotency_key', None) or str(uuid.uuid4())
    args.key = key
    if not getattr(args, 'to_actor', None):
        original = _request('GET', '/api/bus/messages/' + quote(args.reply_to, safe=''))
        args.to_actor = original['from_actor'] if original.get('to_actor') == sender else original['to_actor']
        if getattr(args, 'to', None) is None:
            args.to = args.to_actor
    # The legacy `hub reply` carried reply_to on /api/bus/send; keep that
    # contract (the reply route stays available to `hub msg reply`).
    result = _request('POST', '/api/bus/send', _send_payload(args, sender), retries=True)
    return {**result, 'from': sender, 'to': args.to_actor, 'idempotency_key': key}


def top_inbox(args, actor: str | None = None):
    """`hub inbox` with the §11 surface: --actor optional (default: this
    identity), --unread, --thread, --wait long poll, --mark-read; exit
    codes 0/2/3/4. Prints the bare message list, exactly like the legacy
    command, so scripts parsing it keep working."""
    if actor is None and not getattr(args, 'actor', None):
        actor, _source = resolve_identity(getattr(args, 'profile', None))
    result = inbox(args, actor)
    marked = result.get('marked_read')
    if getattr(args, 'mark_read', False) and marked is not None:
        print('Marked ' + str(marked.get('marked', 0) if isinstance(marked, dict) else marked) + ' message(s) read.', file=sys.stderr)
    return result['messages']


def top_show(args):
    """`hub show` marks the message read for its recipient (server-side)."""
    return _request('GET', '/api/bus/messages/' + quote(args.id, safe=''))


def top_stage(args, stage: str):
    """`hub ack|accept|complete`: resolve the identity first so a session's
    own credential (APLEXER_SESSION_ID) or --profile applies, then stage."""
    resolve_identity(getattr(args, 'profile', None))
    payload = None
    if getattr(args, 'lease_epoch', None) is not None:
        payload = {'lease_epoch': int(args.lease_epoch)}
    else:
        epoch = os.environ.get('HUB_LEASE_EPOCH')
        if epoch is not None and epoch != '':
            payload = {'lease_epoch': int(epoch)}
    return _request('POST', '/api/bus/messages/' + quote(args.id, safe='') + '/' + stage, payload)
