"""Operator CLI for the Hub prompt library and session schedules (intake 19ec91c2 AC8-AC10).

`hub prompts` manages versioned prompts addressable by id or slug
(`slug@3` pins a version). `hub schedules` delivers a prompt (inline,
file or library) into an interactive session on a cron, through the same
durable delivery queue as inbound webhooks. Not to be confused with
`hub schedule` (cron launch schedules that start a fresh session).
"""
import argparse
import json
from urllib.parse import quote

from . import session_control

PROMPT_MAX_BYTES = 16000


def parse_vars(items):
    """`--var key=value` (repeatable) -> dict; None when empty."""
    out = {}
    for item in items or []:
        key, sep, value = item.partition('=')
        if not sep or not key:
            raise session_control.SessionInputError('--var expects KEY=VALUE')
        out[key] = value
    return out or None


def add_vars_argument(node):
    node.add_argument('--var', action='append', metavar='KEY=VALUE',
                      help='Template variable for {{var.KEY}} (repeatable)')


def _ref(args):
    return quote(args.id, safe='@')


def _read_body(args, required):
    if getattr(args, 'body_file', None):
        return session_control.read_file(args.body_file, limit=PROMPT_MAX_BYTES, field='Prompt')
    if getattr(args, 'body', None) is not None:
        return session_control.read_prompt_text(args.body, limit=PROMPT_MAX_BYTES, field='Prompt')
    if required:
        raise session_control.SessionInputError('--body-file or --body is required')
    return None


def execute_prompts(args):
    cmd = args.prompts_cmd
    if cmd == 'create':
        payload = {'slug': args.slug, 'body': _read_body(args, True)}
        if args.title:
            payload['title'] = args.title
        if args.note:
            payload['note'] = args.note
        return session_control.request('POST', '/api/prompts', payload)
    if cmd == 'list':
        return session_control.request('GET', '/api/prompts')
    if cmd == 'show':
        if args.versions:
            return session_control.request('GET', f'/api/prompts/{_ref(args)}/versions')
        return session_control.request('GET', f'/api/prompts/{_ref(args)}')
    if cmd == 'update':
        payload = {}
        body = _read_body(args, False)
        if body is not None:
            payload['body'] = body
        if args.title is not None:
            payload['title'] = args.title
        if args.note:
            payload['note'] = args.note
        return session_control.request('PUT', f'/api/prompts/{_ref(args)}', payload)
    if cmd == 'delete':
        if not args.yes:
            raise session_control.SessionInputError('deleting a prompt requires --yes')
        return session_control.request('DELETE', f'/api/prompts/{_ref(args)}')
    if cmd == 'render':
        return session_control.request('POST', f'/api/prompts/{_ref(args)}/render', {'vars': parse_vars(args.var) or {}})
    raise session_control.SessionInputError('unknown prompts subcommand')


def execute_schedules(args):
    cmd = args.schedules_cmd
    if cmd == 'create':
        payload = {'name': args.name, 'target_label': args.session, 'cron': args.cron}
        if args.tz:
            payload['tz'] = args.tz
        if args.prompt_file:
            payload['prompt'] = session_control.read_file(args.prompt_file, limit=PROMPT_MAX_BYTES, field='Prompt')
        elif args.prompt is not None:
            payload['prompt'] = session_control.read_prompt_text(args.prompt, limit=PROMPT_MAX_BYTES, field='Prompt')
        elif args.prompt_id:
            payload['prompt_id'] = args.prompt_id
        else:
            raise session_control.SessionInputError('one of --prompt, --prompt-file, --prompt-id is required')
        variables = parse_vars(args.var)
        if variables:
            payload['prompt_vars'] = variables
        if args.disabled:
            payload['enabled'] = False
        return session_control.request('POST', '/api/session-schedules', payload)
    if cmd == 'list':
        return session_control.request('GET', '/api/session-schedules')
    ref = quote(getattr(args, 'id', '') or '', safe='')
    if cmd == 'show':
        return session_control.request('GET', f'/api/session-schedules/{ref}')
    if cmd in ('pause', 'resume'):
        return session_control.request('POST', f'/api/session-schedules/{ref}/{cmd}', {})
    if cmd == 'delete':
        if not args.yes:
            raise session_control.SessionInputError('deleting a schedule requires --yes')
        return session_control.request('DELETE', f'/api/session-schedules/{ref}')
    if cmd == 'fire':
        payload = {'delivery_key': args.delivery_key} if args.delivery_key else {}
        return session_control.request('POST', f'/api/session-schedules/{ref}/fire', payload)
    if cmd == 'deliveries':
        return session_control.request('GET', f'/api/session-schedules/{ref}/deliveries')
    raise session_control.SessionInputError('unknown schedules subcommand')


def _table(rows, keys):
    if not rows:
        return 'none'
    return '\n'.join(['  '.join(keys)] + ['  '.join(str(row.get(key, '')) for key in keys) for row in rows])


def render_output(args, result):
    if getattr(args, 'json', False):
        return result
    cmd = getattr(args, 'schedules_cmd', None) or getattr(args, 'prompts_cmd', None)
    if cmd == 'list' and isinstance(result, dict):
        if 'schedules' in result:
            return _table(result['schedules'], ['name', 'target_label', 'cron', 'tz', 'enabled', 'next_due_at', 'last_fire_at'])
        return _table(result.get('prompts', []), ['slug', 'id', 'current_version', 'title', 'updated_at'])
    if cmd == 'show' and isinstance(result, dict) and isinstance(result.get('body'), str) and not args.versions:
        return result['body']
    return json.dumps(result, indent=2, ensure_ascii=True)


def diagnostic(exc):
    if isinstance(exc, session_control.SessionHTTPError):
        return f'operation failed: HTTP {exc.status}: {exc.message or exc.code}'
    if isinstance(exc, (session_control.SessionInputError, session_control.SessionTransportError)):
        return f'operation failed: {exc}'
    return 'operation failed; check authentication and inputs (details withheld)'


def _body_args(node, required):
    src = node.add_mutually_exclusive_group(required=required)
    src.add_argument('--body-file', help='UTF-8 prompt body file, - for stdin (max 16000 bytes)')
    src.add_argument('--body', help='Inline prompt body')


def add_parser(sub) -> None:
    prompts = sub.add_parser('prompts', help='Versioned prompt library addressable by id or slug (operator)')
    pc = prompts.add_subparsers(dest='prompts_cmd', required=True)
    create = pc.add_parser('create', help='Create a prompt (version 1)')
    create.add_argument('--slug', required=True, help='Lowercase slug, e.g. orchestrator-hourly')
    create.add_argument('--title')
    create.add_argument('--note', help='Version note')
    _body_args(create, True)
    create.add_argument('--json', action='store_true')
    lst = pc.add_parser('list', help='List prompts')
    lst.add_argument('--json', action='store_true')
    show = pc.add_parser('show', help='Print the current (or @N pinned) body; --versions lists history')
    show.add_argument('id', help='Prompt id, slug or slug@version')
    show.add_argument('--versions', action='store_true')
    show.add_argument('--json', action='store_true')
    update = pc.add_parser('update', help='Append a new version and/or change the title')
    update.add_argument('id')
    update.add_argument('--title')
    update.add_argument('--note')
    _body_args(update, False)
    update.add_argument('--json', action='store_true')
    delete = pc.add_parser('delete', help='Delete a prompt and all versions (refused while in use)')
    delete.add_argument('id')
    delete.add_argument('--yes', action='store_true')
    delete.add_argument('--json', action='store_true')
    render = pc.add_parser('render', help='Render a prompt with variables (no delivery)')
    render.add_argument('id')
    add_vars_argument(render)
    render.add_argument('--json', action='store_true')

    root = sub.add_parser('schedules', help='Cron prompt schedules delivered into interactive sessions via the durable queue (operator)')
    sc = root.add_subparsers(dest='schedules_cmd', required=True)
    create = sc.add_parser('create', help='Create a session schedule')
    create.add_argument('--name', required=True, help='Schedule slug (lowercase, 2-64 chars)')
    create.add_argument('--session', required=True, help='Target session label, workspace:tag alias or Hub UUID')
    create.add_argument('--cron', required=True, help='Five-field cron, evaluated in --tz')
    create.add_argument('--tz', default='UTC')
    src = create.add_mutually_exclusive_group()
    src.add_argument('--prompt', help='Inline prompt text')
    src.add_argument('--prompt-file', help='UTF-8 prompt file, - for stdin')
    src.add_argument('--prompt-id', help='Library prompt id, slug or slug@version')
    add_vars_argument(create)
    create.add_argument('--disabled', action='store_true')
    create.add_argument('--json', action='store_true')
    lst = sc.add_parser('list', help='List session schedules')
    lst.add_argument('--json', action='store_true')
    for name, text in (('show', 'Show one schedule with recent deliveries and audit'),
                       ('pause', 'Pause (no backfill on resume)'),
                       ('resume', 'Resume from the next occurrence'),
                       ('deliveries', 'List recent deliveries with status')):
        node = sc.add_parser(name, help=text)
        node.add_argument('id', help='Schedule id or name')
        node.add_argument('--json', action='store_true')
    delete = sc.add_parser('delete', help='Delete a schedule')
    delete.add_argument('id')
    delete.add_argument('--yes', action='store_true')
    delete.add_argument('--json', action='store_true')
    fire = sc.add_parser('fire', help='Fire one delivery now through the queue (idempotent with --delivery-key)')
    fire.add_argument('id')
    fire.add_argument('--delivery-key')
    fire.add_argument('--json', action='store_true')
