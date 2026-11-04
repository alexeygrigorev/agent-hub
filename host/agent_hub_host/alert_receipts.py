"""Operator CLI for alert onward-relay receipts (intake 1429bf88).

`hub alerts receipts` reads the bounded operator view (per alert: delivery,
inbox read/ack receipts, onward relay receipt or explicit `unconfirmed`).
`hub alerts relay` is the maintained way for the relay owner (Grok Bot /
Developer, through the operator credential) to report onward delivery to
Alexey per alert: forward receipt id and timestamp only, never message
content. Absent receipts stay explicitly unconfirmed on the server.
"""
import argparse
import json
from urllib.parse import quote

from . import session_control


def execute(args: argparse.Namespace):
    if args.alerts_cmd == 'receipts':
        path = '/api/alert-receipts'
        if getattr(args, 'limit', None):
            path += f'?limit={int(args.limit)}'
        return session_control.request('GET', path)
    if args.alerts_cmd == 'relay':
        alert_id = str(args.id)
        if not alert_id.isdigit():
            raise session_control.SessionInputError('alert id must be a number')
        if not getattr(args, 'ref', None):
            raise session_control.SessionInputError('--ref (forward receipt id) is required')
        payload = {'relay_ref': args.ref}
        if getattr(args, 'at', None):
            payload['relayed_at'] = args.at
        return session_control.request('POST', f'/api/alert-receipts/{quote(alert_id, safe="")}/relay', payload)
    raise session_control.SessionInputError('unknown alerts subcommand')


def _confirmed(rows):
    return [row for row in rows if row.get('relay_state') == 'confirmed']


def render_output(args, result):
    if getattr(args, 'json', False):
        return result
    if args.alerts_cmd == 'receipts' and isinstance(result, dict):
        alerts = result.get('alerts', [])
        if not alerts:
            return 'no alerts'
        lines = ['alert_id  state  relay  relay_ref  inbox_read  inbox_ack  subject']
        for row in alerts:
            inbox = row.get('inbox') or []
            read = any(i.get('read_at') for i in inbox)
            acked = any(i.get('acked_at') for i in inbox)
            relay = row.get('relay')
            lines.append('  '.join(str(v) for v in (
                row.get('id'), row.get('state'), row.get('relay_state'),
                (relay or {}).get('relay_ref', '-'),
                'yes' if read else 'no', 'yes' if acked else 'no',
                f"{row.get('subject_kind')}:{row.get('subject')}")))
        unconfirmed = result.get('unconfirmed')
        if unconfirmed is not None:
            lines.append(f'unconfirmed onward relay: {unconfirmed}')
        return '\n'.join(lines)
    return json.dumps(result, indent=2, ensure_ascii=True)


def diagnostic(exc):
    if isinstance(exc, session_control.SessionHTTPError):
        status = exc.status if isinstance(exc.status, int) and 100 <= exc.status <= 599 else 'unknown'
        code = exc.code if isinstance(exc.code, str) and session_control.SAFE_ERROR.fullmatch(exc.code) else 'request_failed'
        return f'operation failed: HTTP {status} ({code})'
    if isinstance(exc, (session_control.SessionInputError, session_control.SessionTransportError)):
        return f'operation failed: {exc}'
    return 'operation failed; check authentication and inputs (details withheld)'


def add_parser(sub) -> None:
    alerts = sub.add_parser('alerts', help='Alert inbox-ack and onward-relay receipts (operator)')
    ac = alerts.add_subparsers(dest='alerts_cmd', required=True)
    receipts = ac.add_parser('receipts', help='Per alert: delivery, inbox read/ack receipts, relay receipt or unconfirmed')
    receipts.add_argument('--limit', type=int, help='Bounded page size (default 100, max 500)')
    receipts.add_argument('--json', action='store_true')
    relay = ac.add_parser('relay', help='Report onward delivery of one alert to Alexey (forward receipt id only)')
    relay.add_argument('id', help='Alert id (from hub alerts receipts or /api/liveness)')
    relay.add_argument('--ref', required=True, help='Forward receipt id, e.g. the mail message-id (no message content)')
    relay.add_argument('--at', help='ISO timestamp of the onward relay (default: now)')
    relay.add_argument('--json', action='store_true')
