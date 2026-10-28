"""Operator CLI for inbound webhook routes (intake 19ec91c2).

Public HTTP surface only: talks to /api/webhooks* with the operator
credential through the same request helper as session control. Route secrets
are shown exactly once by the server; this CLI never stores, logs or echoes
them — `create` writes the secret to an explicit private file (mode 0600) or
prints it only with --stdout-secret. Without one of the two the secret is
lost by design and the CLI says so.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import api, config, session_control

TEST_INTERVAL = 3.0


def _base_url():
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    return base.rstrip('/')


def _write_secret_file(path: str, secret: str) -> None:
    target = Path(path).expanduser()
    if target.parent and str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(secret)
    except Exception:
        try:
            os.unlink(str(target))
        except OSError:
            pass
        raise
    try:
        os.chmod(str(target), 0o600)
    except OSError:
        pass


def _print_secret_once(created: dict, args: argparse.Namespace) -> dict:
    """Surface the one-time secret only through an explicit private channel."""
    secret = created.get('secret')
    result = dict(created)
    result.pop('secret', None)
    result['secret_delivered'] = False
    if not secret:
        return result
    if getattr(args, 'secret_file', None):
        _write_secret_file(args.secret_file, secret)
        result['secret_delivered'] = True
        result['secret_file'] = str(Path(args.secret_file).expanduser())
    elif getattr(args, 'stdout_secret', False):
        result['secret'] = secret
        result['secret_delivered'] = True
    else:
        print('webhook: secret NOT shown; rerun with --secret-file PATH (0600) or --stdout-secret; '
              'a route without a stored secret must be deleted and recreated.', file=sys.stderr)
    return result


def _resolve_ref(args) -> str:
    ref = getattr(args, 'id', None) or getattr(args, 'name', None)
    if not ref:
        raise session_control.SessionInputError('webhook id or name required')
    return ref


def execute(args: argparse.Namespace):
    cmd = getattr(args, 'webhooks_cmd', None)
    if cmd == 'create':
        payload = {
            'name': args.name,
            'target_label': args.session,
        }
        if args.template_file:
            payload['template'] = session_control.read_file(args.template_file, max_chars=4096, field='Template')
        elif args.template:
            payload['template'] = session_control.read_prompt_text(args.template, max_chars=4096, field='Template')
        if args.auth != 'token':
            payload['auth_mode'] = args.auth
        if args.rate_limit is not None:
            payload['rate_limit_per_min'] = args.rate_limit
        if args.max_body_bytes is not None:
            payload['max_body_bytes'] = args.max_body_bytes
        if args.disabled:
            payload['enabled'] = False
        created = session_control.request('POST', '/api/webhooks', payload)
        result = _print_secret_once(created, args)
        result['url'] = _base_url() + result.get('url_path', '/webhooks/' + str(result.get('id')))
        return result
    if cmd == 'list':
        return session_control.request('GET', '/api/webhooks')
    if cmd == 'show':
        return session_control.request('GET', '/api/webhooks/' + _resolve_ref(args))
    if cmd == 'delete':
        if not getattr(args, 'yes', False):
            raise session_control.SessionInputError('deleting a webhook route requires --yes')
        return session_control.request('DELETE', '/api/webhooks/' + _resolve_ref(args))
    if cmd == 'deliveries':
        ref = _resolve_ref(args)
        return session_control.request('GET', f'/api/webhooks/{ref}/deliveries?limit={int(args.limit)}')
    if cmd == 'test':
        ref = _resolve_ref(args)
        payload = {}
        if args.payload_file:
            payload['payload'] = session_control.read_file(args.payload_file, field='Payload')
        elif args.payload_json:
            try:
                payload['payload'] = json.loads(args.payload_json)
            except json.JSONDecodeError:
                raise session_control.SessionInputError('--payload-json is not valid JSON') from None
        elif args.payload:
            payload['payload'] = args.payload
        if args.delivery_key:
            payload['delivery_key'] = args.delivery_key
        started = session_control.request('POST', f'/api/webhooks/{ref}/test', payload)
        delivery_id = started.get('delivery_id')
        if not getattr(args, 'wait', False) or not delivery_id:
            return started
        deadline = time.monotonic() + session_control.check_wait_args(args)[0]
        result = dict(started)
        while time.monotonic() < deadline:
            listing = session_control.request('GET', f'/api/webhooks/{ref}/deliveries?limit=10')
            for entry in listing.get('deliveries', []):
                if entry.get('id') == delivery_id:
                    result['delivery'] = entry
                    if entry.get('status') in ('delivered', 'failed'):
                        return result
                    break
            time.sleep(TEST_INTERVAL)
        result['wait'] = 'timed_out'
        return result
    raise session_control.SessionInputError('unknown webhooks subcommand')


def render_table(data: dict) -> str:
    rows = data.get('webhooks', []) if isinstance(data, dict) else []
    if not rows:
        return 'no webhook routes'
    headers = ['name', 'id', 'target', 'auth', 'rate/min', 'max bytes', 'enabled', 'url path']
    lines = ['  '.join(headers)]
    for row in rows:
        lines.append('  '.join(str(row.get(key, '')) for key in
                               ['name', 'id', 'target_label', 'auth_mode', 'rate_limit_per_min',
                                'max_body_bytes', 'enabled', 'url_path']))
    return '\n'.join(lines)


def render_deliveries(data: dict) -> str:
    rows = data.get('deliveries', []) if isinstance(data, dict) else []
    if not rows:
        return 'no deliveries'
    headers = ['id', 'source', 'status', 'attempts', 'session', 'error', 'created', 'delivered']
    lines = ['  '.join(headers)]
    for row in rows:
        lines.append('  '.join(str(row.get(key, '')) for key in
                               ['id', 'source', 'status', 'attempts', 'session_id', 'last_error',
                                'created_at', 'delivered_at']))
    return '\n'.join(lines)


def render_output(args: argparse.Namespace, result):
    if getattr(args, 'json', False):
        return result
    cmd = getattr(args, 'webhooks_cmd', None)
    if cmd == 'list':
        return render_table(result)
    if cmd in ('deliveries', 'test'):
        return render_deliveries(result) if cmd == 'deliveries' else json.dumps(result, indent=2, ensure_ascii=True)
    if cmd == 'create':
        shown = dict(result)
        # --stdout-secret explicitly requested stdout as the private channel,
        # so the secret value itself stays in the output; any other path must
        # never carry it.
        if 'secret' in shown and not getattr(args, 'stdout_secret', False):
            shown['secret'] = '<redacted>'
        return json.dumps(shown, indent=2, ensure_ascii=True)
    return json.dumps(result, indent=2, ensure_ascii=True)


def diagnostic(exc: Exception) -> str:
    if isinstance(exc, session_control.SessionHTTPError):
        return f'webhook operation failed: HTTP {exc.status}: {exc.message or exc.code}'
    if isinstance(exc, (session_control.SessionInputError, session_control.SessionTransportError)):
        return f'webhook operation failed: {exc}'
    return 'webhook operation failed; check authentication and inputs (details withheld)'


def add_parser(sub) -> None:
    root = sub.add_parser('webhooks', help='Inbound webhook routes delivering payloads into Hub sessions (operator)')
    commands = root.add_subparsers(dest='webhooks_cmd', required=True)
    create = commands.add_parser('create', help='Create a route; the secret is shown exactly once')
    create.add_argument('--name', required=True, help='Route slug (lowercase, 2-64 chars)')
    create.add_argument('--session', required=True, dest='session',
                        help='Target session label (friendly name), qualified alias (workspace:tag) or Hub UUID')
    template_src = create.add_mutually_exclusive_group()
    template_src.add_argument('--template-file', help='UTF-8 template file (max 4096 chars)')
    template_src.add_argument('--template', help='Inline template ({{payload}}, {{payload_json}}, {{payload.field}})')
    create.add_argument('--auth', choices=['token', 'hmac'], default='token',
                        help='token: X-Webhook-Token bearer; hmac: X-Webhook-Signature sha256 HMAC over "<ts>.<body>"')
    create.add_argument('--rate-limit', type=int, default=None, help='Requests per minute (default 30)')
    create.add_argument('--max-body-bytes', type=int, default=None, help='Payload size cap in bytes (default 65536)')
    create.add_argument('--disabled', action='store_true', help='Create the route disabled')
    secret_out = create.add_mutually_exclusive_group()
    secret_out.add_argument('--secret-file', help='Write the one-time secret to this private file (mode 0600)')
    secret_out.add_argument('--stdout-secret', action='store_true', help='Print the one-time secret to stdout')

    listing = commands.add_parser('list', help='List routes (never shows secrets)')
    listing.add_argument('--json', action='store_true')

    show = commands.add_parser('show', help='Show one route with recent deliveries and audit entries')
    show.add_argument('id', help='Route id or name')
    show.add_argument('--json', action='store_true')

    delete = commands.add_parser('delete', help='Delete a route (deliveries history is kept)')
    delete.add_argument('id', help='Route id or name')
    delete.add_argument('--yes', action='store_true', help='Confirm deletion')

    deliveries = commands.add_parser('deliveries', help='List recent deliveries with status')
    deliveries.add_argument('id', help='Route id or name')
    deliveries.add_argument('--limit', type=int, default=50)
    deliveries.add_argument('--json', action='store_true')

    test = commands.add_parser('test', help='Inject a test payload through the same delivery queue')
    test.add_argument('id', help='Route id or name')
    payload_src = test.add_mutually_exclusive_group()
    payload_src.add_argument('--payload', help='Inline payload text')
    payload_src.add_argument('--payload-file', help='Payload file, - for stdin')
    payload_src.add_argument('--payload-json', help='Payload as JSON (stored pretty)')
    test.add_argument('--delivery-key', help='Stable idempotency key for safe retries')
    session_control.add_wait(test, timeout=session_control.WAIT_DEFAULT_TIMEOUT)
    test.set_defaults(wait=True)
    test.add_argument('--no-wait', dest='wait', action='store_false', help='Return the queued receipt without waiting')
    test.add_argument('--json', action='store_true')
