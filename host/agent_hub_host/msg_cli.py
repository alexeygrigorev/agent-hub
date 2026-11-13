"""`hub msg` and the messaging top-level commands on the Hub2 mailbox.

Hub2 serves messages only on ``/api/messages`` (hub2/src/mailbox-do.ts); the
Hub1 ``/api/bus/*`` routes are gone and refused client-side. The sender is
always the credential that makes the request:
- inside a Hub2 session: the session worker credential (``session:<id>``);
- ``--profile NAME``: a named agent profile (``~/.config/agent-hub/NAME.json``);
- otherwise the normal login (``human:<sub>``).

``--to human:me`` posts into the web Inbox of the human the credential acts
for (the caller itself, or the human owning the session's intake), resolved by
the server (t3280u). ``--from`` is optional and refused when it differs from
a known identity. Hub1-only options (--tui, --thread, --ref, --deliver-by,
--lease-epoch, non-note --kind) fail with ``not_supported_on_hub2`` and the
replacement. Exit codes: 0 accepted, 2 refused (auth/validation), 3 recipient
unknown or ambiguous, 4 transport error after retries. Tokens are never printed.
"""
from __future__ import annotations

import argparse
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
        if any(code in text for code in ('unknown_recipient', 'recipient_unknown', 'ambiguous_recipient')):
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
    """Select the credential file (via HUB_CONFIG_FILE) and return (actor, source).

    Inside a Hub2 session the per-attempt worker context is the only credential
    the CLI uses (config.load_config), so its actor is the identity and a
    --profile would be silently ignored: refuse it instead."""
    from .worker_auth import is_v2_worker
    if is_v2_worker():
        if profile:
            raise MsgError('--profile is not used inside a Hub2 session; the session credential is the sender')
        from .config import load_config
        worker = load_config().get('_hub2_worker') or {}
        actor = worker.get('actor') if isinstance(worker, dict) else None
        return (actor if isinstance(actor, str) else None), 'hub2-session'
    path = None
    source = 'default'
    if profile:
        path, source = profile_path(profile), 'profile:' + profile
        if not path.is_file():
            raise MsgError('No such profile; ask an operator for `hub agents create`')
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


# Hub2 serves messaging only on /api/messages (hub2/src/mailbox-do.ts); the
# Hub1 /api/bus/* family is refused client-side by ensure_hub2_supported.
MESSAGES = '/api/messages'
BODY_LIMIT = 12000
TUI_REFUSAL = ('not_supported_on_hub2; --tui (typing into a session prompt) moved to '
               '`hub sessions send <session-id> --prompt ...`; `hub msg send` posts to the Hub2 mailbox')
DEAD_FLAGS = (('thread', '--thread'), ('thread_id', '--thread-id'), ('ref', '--ref'),
              ('deliver_by', '--deliver-by'), ('deliver_by_s', '--deliver-by-s'), ('lease_epoch', '--lease-epoch'))


def refuse_hub1_only_flags(args):
    """Hub1 bus options with no Hub2 mailbox meaning fail loudly, never silently."""
    if getattr(args, 'delivery', None) == 'tui':
        raise MsgError(TUI_REFUSAL)
    for attr, flag in DEAD_FLAGS:
        if getattr(args, attr, None) not in (None, ''):
            raise MsgError('not_supported_on_hub2; ' + flag + ' has no Hub2 mailbox equivalent '
                           '(threads follow `hub msg reply <id>`; bodies are inline, max 12000 characters)')
    kind = getattr(args, 'kind', None)
    if kind not in (None, '', 'note', 'reply'):
        raise MsgError('not_supported_on_hub2; --kind ' + str(kind)[:40] + ' has no Hub2 mailbox equivalent (all messages are notes)')


def _check_from(args, actor: str | None) -> None:
    """--from is optional; Hub2 always records the credential's identity as the
    sender, so a --from that disagrees with a known identity is refused."""
    expected = getattr(args, 'as_actor', None) or actor
    claimed = getattr(args, 'from_actor', None)
    if claimed and expected and claimed != expected:
        raise MsgError('--from differs from this identity')


def _view_query(args, mailbox: str | None = None) -> str:
    as_actor = getattr(args, 'as_actor', None)
    if as_actor:
        return '?as=' + quote(as_actor, safe='')
    if mailbox:
        return '?mailbox=' + quote(mailbox, safe='')
    return ''


def _message(result) -> dict:
    message = result.get('message') if isinstance(result, dict) else None
    if not isinstance(message, dict):
        raise MsgError('Hub returned no message', 4)
    return message


def get_message(args, message_id: str) -> dict:
    return _message(_request('GET', MESSAGES + '/' + quote(message_id, safe='') + _view_query(args)))


def post_message(args, recipient: str, actor: str | None, reply_to: dict | None = None) -> dict:
    """One Hub2 mailbox send. `human:me` is resolved server-side to the human
    this credential acts for (Alexey's web Inbox). Retries reuse the same key."""
    refuse_hub1_only_flags(args)
    _check_from(args, actor)
    body = _body(args)
    if len(body) > BODY_LIMIT:
        raise MsgError('Body exceeds the Hub2 12000-character message limit; link to a file instead')
    key = getattr(args, 'key', None) or str(uuid.uuid4())
    args.key = key
    payload = {'recipient': recipient, 'body': body, 'key': key}
    if reply_to is not None:
        payload['reply_to'] = reply_to['id']
        payload['parent_sender'] = reply_to['sender']
    message = _message(_request('POST', MESSAGES + _view_query(args), payload, retries=True))
    return {'id': message.get('id'), 'from': message.get('sender'), 'to': message.get('recipient'),
            'thread_id': message.get('thread_id'), 'state': message.get('state'),
            'epoch': message.get('epoch'), 'idempotency_key': key}


def whoami_actor() -> str | None:
    """The requesting human's actor from Hub2 /api/auth/me (humans only)."""
    from .bus import HttpRequestError
    try:
        me = _request('GET', '/api/auth/me')
    except HttpRequestError:
        return None  # not a human credential; transport errors propagate (exit 4)
    actor = me.get('actor') if isinstance(me, dict) else None
    return actor if isinstance(actor, str) else None


def reply_message(args, message_id: str, actor: str | None) -> dict:
    """Reply to the other end of a message: its sender, unless this identity sent it."""
    original = get_message(args, message_id)
    as_actor = getattr(args, 'as_actor', None)
    if as_actor and ':' not in as_actor:
        as_actor = 'agent:' + as_actor  # the server's delegation adds the same prefix
    me = as_actor or actor or whoami_actor()
    if not me:
        raise MsgError('Cannot tell which end of this message you are; pass --to')
    to = original.get('recipient') if original.get('sender') == me else original.get('sender')
    if not to:
        raise MsgError('Original message has no counterpart to reply to')
    return post_message(args, to, actor, reply_to=original)


def stage_message(args, message_id: str, stage: str) -> dict:
    """read/ack/accept/complete receipts are epoch-bound on Hub2."""
    message = get_message(args, message_id)
    path = MESSAGES + '/' + quote(message['id'], safe='') + '/' + stage + _view_query(args, message.get('recipient'))
    return _request('POST', path, {'epoch': message.get('epoch')})


def add_commands(sub):
    msg = sub.add_parser('msg', help='Hub2 mailbox messages: post to the human web Inbox (human:me), sessions and named agents',
                         description='Hub2 mailbox (/api/messages). Post a link into your own Hub web Inbox: '
                                     "hub msg send --to human:me --body '...'. Inside a Hub session the session "
                                     'credential is the sender; human:me resolves to the human who owns the '
                                     'session\'s intake. To type into a running session prompt use `hub sessions send`.')
    msg.add_argument('--profile', help='Named agent profile, e.g. developer')
    ms = msg.add_subparsers(dest='msg_cmd', required=True)
    send = ms.add_parser('send', help='Post to human:me (your Hub web Inbox), human:<sub>, session:<id> or agent:<name>')
    send.add_argument('--to', required=True, help='human:me = the human this credential acts for (Hub web Inbox)')
    send.add_argument('--body', default=None, help='Text, or - for stdin')
    send.add_argument('--body-file')
    send.add_argument('--from', dest='from_actor', help='Optional; the credential is always the sender, a mismatch is refused')
    send.add_argument('--as', dest='as_actor', help='Act as a delegated named agent, e.g. agent:oncall')
    mode = send.add_mutually_exclusive_group()
    mode.add_argument('--tui', action='store_const', const='tui', dest='delivery', help='Refused on Hub2: use `hub sessions send <id> --prompt`')
    mode.add_argument('--inbox', action='store_const', const='inbox', dest='delivery', help='Mailbox only (the Hub2 default)')
    send.add_argument('--thread', help='Refused on Hub2 (threads follow hub msg reply)')
    send.add_argument('--ref', help='Refused on Hub2 (inline body, max 12000 characters)')
    send.add_argument('--deliver-by', help='Refused on Hub2 (no TUI delivery deadline)')
    send.add_argument('--deliver-by-s', type=int, help=argparse.SUPPRESS)
    send.add_argument('--key', help='Idempotency key; printed back for safe retries')
    reply = ms.add_parser('reply', help='Reply to a message; threads back to its sender on any host')
    reply.add_argument('id')
    reply.add_argument('--body', default=None, help='Text, or - for stdin')
    reply.add_argument('--body-file')
    reply.add_argument('--from', dest='from_actor')
    reply.add_argument('--as', dest='as_actor', help='Act as a delegated named agent, e.g. agent:oncall')
    reply_mode = reply.add_mutually_exclusive_group()
    reply_mode.add_argument('--tui', action='store_const', const='tui', dest='delivery')
    reply_mode.add_argument('--inbox', action='store_const', const='inbox', dest='delivery')
    reply.add_argument('--ref', help='Refused on Hub2')
    reply.add_argument('--deliver-by', help='Refused on Hub2')
    reply.add_argument('--key')
    inbox = ms.add_parser('inbox', help='List this identity\'s inbox')
    inbox.add_argument('--actor', help='Default: this identity')
    inbox.add_argument('--unread', action='store_true')
    inbox.add_argument('--thread', help='Only one thread id')
    inbox.add_argument('--wait', type=int, default=0, help='When nothing matches, poll up to N seconds (max 60) for new messages')
    inbox.add_argument('--mark-read', action='store_true', help='Mark the returned messages read')
    inbox.add_argument('--limit', type=int, default=25, help='Newest N messages, 1..50')
    inbox.add_argument('--cursor')
    show = ms.add_parser('show', help='Show one message')
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
    delegate = ms.add_parser('agent-delegate', help='Operator: grant or revoke delegated MCP credential families for agent:<name>')
    delegate.add_argument('name')
    delegate.add_argument('--client', help='MCP client id to grant (omit to list)')
    delegate.add_argument('--remove', action='store_true', help='Revoke instead of grant')
    from . import agents_cli
    agents_cli.add_commands(sub)


def execute(args):
    from .config import load_config
    if getattr(args, 'msg_cmd', None) == 'agents':
        from . import agents_cli
        return agents_cli.execute(args)
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
    actor, source = resolve_identity(getattr(args, 'profile', None))
    if args.msg_cmd == 'readdress':
        message = get_message(args, args.id)
        key = 'readdress:%s:%s:%s' % (message['id'], message.get('epoch'), args.to)
        return _request('POST', MESSAGES + '/' + quote(message['id'], safe='') + '/readdress',
                        {'recipient': args.to, 'key': key, 'epoch': message.get('epoch')})
    if args.msg_cmd == 'whoami':
        return {'actor': actor, 'source': source}
    if args.msg_cmd == 'send':
        return post_message(args, args.to, actor)
    if args.msg_cmd == 'reply':
        return reply_message(args, args.id, actor)
    if args.msg_cmd == 'inbox':
        return inbox(args, actor)
    if args.msg_cmd == 'show':
        return get_message(args, args.id)
    return _request('GET', MESSAGES + '/' + quote(args.id, safe='') + '/status' + _view_query(args))


INBOX_DESC_PAGES = 4  # --unread looks back at most 4 x 50 newest messages


def _page(params, cursor):
    query = list(params) + (['cursor=' + quote(cursor, safe='')] if cursor else [])
    page = _request('GET', MESSAGES + '?' + '&'.join(query))
    if not isinstance(page, dict):
        raise MsgError('Hub returned no mailbox page', 4)
    return page.get('items') or [], page.get('next')


def _row_cursor(row):
    return json.dumps([row.get('created_at'), row.get('id')])


def inbox(args, actor: str | None):
    """Shared inbox path for `hub msg inbox` and `hub inbox`: the Hub2 mailbox
    the web Inbox shows. Reads newest-first (order=desc) in one bounded page
    (at most INBOX_DESC_PAGES for --unread) and prints the newest --limit
    messages oldest-to-newest; --cursor pages forward from a printed `next`.
    --wait polls forward from the newest message (max 60 s) when nothing
    matched; --mark-read records epoch-bound read receipts."""
    who = getattr(args, 'actor', None) or getattr(args, 'as_actor', None) or actor
    wait = max(0, min(getattr(args, 'wait', 0) or 0, 60))
    limit = max(1, min(int(getattr(args, 'limit', None) or 25), 50))
    params = ['limit=50']
    if getattr(args, 'thread', None):
        params.append('thread=' + quote(args.thread, safe=''))
    view = ''
    if getattr(args, 'as_actor', None) or (who and who.startswith('agent:') and who != actor):
        view = 'as=' + quote(who, safe='')
    elif who and who != actor:
        view = 'mailbox=' + quote(who, safe='')
    if view:
        params.append(view)
    unread = getattr(args, 'unread', False)
    keep = (lambda r: not r.get('read_at')) if unread else (lambda r: True)
    if getattr(args, 'cursor', None):
        items, _nxt = _page(params, args.cursor)
        rows, cursor = [], args.cursor
        for item in items:  # the printed `next` is the last message considered, so nothing is skipped
            if len(rows) >= limit:
                break
            cursor = _row_cursor(item)
            if keep(item):
                rows.append(item)
    else:
        newest, back = [], None
        for _ in range(INBOX_DESC_PAGES if unread else 1):
            items, back = _page(params + ['order=desc'], back)
            newest.extend(items)
            if not back or sum(1 for r in newest if keep(r)) >= limit:
                break
        rows = [r for r in newest if keep(r)][:limit][::-1]
        cursor = _row_cursor(newest[0]) if newest else None
    deadline = time.monotonic() + wait
    while not rows and time.monotonic() < deadline:
        time.sleep(min(3, max(0.1, deadline - time.monotonic())))
        items, _nxt = _page(params, cursor)
        if items:
            cursor = _row_cursor(items[-1])
        rows.extend(r for r in items if keep(r))
    rows = rows[-limit:]
    result = {'actor': who, 'messages': rows, 'next': cursor}
    if getattr(args, 'mark_read', False) and rows:
        marked = 0
        for row in rows:
            if not row.get('read_at'):
                path = MESSAGES + '/' + quote(row['id'], safe='') + '/read' + ('?' + view if view else '')
                _request('POST', path, {'epoch': row.get('epoch')})
                marked += 1
        result['marked_read'] = {'marked': marked}
    return result


def _normalize_top_args(args):
    """Top-level parsers name a few destinations differently from `hub msg`."""
    if getattr(args, 'to', None) is None and getattr(args, 'to_actor', None) is not None:
        args.to = args.to_actor
    if getattr(args, 'thread', None) is None and getattr(args, 'thread_id', None) is not None:
        args.thread = args.thread_id
    return args


def top_send(args):
    """`hub send`: same Hub2 mailbox path as `hub msg send`; --reply-to threads."""
    _normalize_top_args(args)
    actor, _source = resolve_identity(getattr(args, 'profile', None))
    args.key = getattr(args, 'idempotency_key', None) or getattr(args, 'key', None)
    if getattr(args, 'reply_to', None):
        return reply_message(args, args.reply_to, actor) if not args.to else post_message(
            args, args.to, actor, reply_to=get_message(args, args.reply_to))
    return post_message(args, args.to, actor)


def top_reply(args):
    """`hub reply`: --to optional; defaults to the other end of the message."""
    _normalize_top_args(args)
    actor, _source = resolve_identity(getattr(args, 'profile', None))
    args.key = getattr(args, 'idempotency_key', None) or getattr(args, 'key', None)
    if getattr(args, 'to', None):
        return post_message(args, args.to, actor, reply_to=get_message(args, args.reply_to))
    return reply_message(args, args.reply_to, actor)


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
    """`hub show`: one Hub2 mailbox message."""
    resolve_identity(getattr(args, 'profile', None))
    return get_message(args, args.id)


def top_stage(args, stage: str):
    """`hub ack|accept|complete`: resolve the identity first so a session's
    own credential (APLEXER_SESSION_ID) or --profile applies, then record the
    epoch-bound Hub2 receipt."""
    resolve_identity(getattr(args, 'profile', None))
    if getattr(args, 'lease_epoch', None) is not None:
        raise MsgError('not_supported_on_hub2; --lease-epoch has no Hub2 mailbox equivalent (receipts are message-epoch bound)')
    return stage_message(args, args.id, stage)
