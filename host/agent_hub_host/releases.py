"""`hub releases`: orchestrator release slots for deploys (intake 2c87c231).

Every deploy of the Hub Worker consumes one single-use, target-bound slot the
orchestrator issued. `issue` binds a slot to an exact commit SHA (or, for
rollbacks, the exact Worker version id). The CI deploy job consumes the slot
referenced by the head commit's `Release-Slot:` trailer; a consumed, revoked
or expired slot refuses the deploy.
"""
import argparse
import json
import re
import sys

SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SLOT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


def add_parser(sub) -> None:
    releases = sub.add_parser('releases', help='Orchestrator release slots: issue, show, list, revoke (admin)')
    rsub = releases.add_subparsers(dest='releases_cmd', required=True)
    issue = rsub.add_parser('issue', help='Issue a single-use deploy slot bound to an exact SHA (or --target)')
    issue.add_argument('--sha', help='Exact commit SHA the slot authorizes for deploy (40 hex)')
    issue.add_argument('--target', help='Exact Worker version id for a rollback release slot')
    issue.add_argument('--id', help='Random UUID v4 slot id; required for --sha so it can be included in the commit trailer')
    issue.add_argument('--ttl-hours', type=float, default=None, help='Slot lifetime in hours (default 48, max 336)')
    issue.add_argument('--note', help='Optional note recorded with the slot (max 512 chars)')
    issue.add_argument('--json', action='store_true', help='Print the raw JSON response')
    show = rsub.add_parser('show', help='Show one release slot by id (admin)')
    show.add_argument('id', help='Slot id')
    show.add_argument('--json', action='store_true', help='Print the raw JSON response')
    lst = rsub.add_parser('list', help='Recent release slots, newest first (admin, bounded)')
    lst.add_argument('--limit', type=int, default=None, help='Max slots to return (1..200, default 50)')
    lst.add_argument('--status', choices=['issued', 'consumed', 'revoked'], default=None, help='Filter by status')
    lst.add_argument('--json', action='store_true', help='Print the raw JSON response')
    revoke = rsub.add_parser('revoke', help='Revoke an issued slot so it can never deploy (admin)')
    revoke.add_argument('id', help='Slot id')
    revoke.add_argument('--json', action='store_true', help='Print the raw JSON response')


def _validate_ttl(args):
    ttl = getattr(args, 'ttl_hours', None)
    if ttl is not None and not (0 < ttl <= 336):
        raise ValueError('--ttl-hours must be between 0 and 336')
    limit = getattr(args, 'limit', None)
    if limit is not None and not (1 <= limit <= 200):
        raise ValueError('--limit must be between 1 and 200')


def execute(args: argparse.Namespace) -> dict:
    from . import session_control
    if args.releases_cmd == 'issue':
        if not args.sha and not args.target:
            raise ValueError('issue needs --sha (40 hex) or --target')
        if args.sha and args.target:
            raise ValueError('pass --sha or --target, not both')
        if args.sha and not SHA_RE.match(args.sha):
            raise ValueError('--sha must be a 40-hex commit SHA')
        payload = {'target': args.target} if args.target else {'sha': args.sha.lower()}
        target_is_sha = bool(args.target and SHA_RE.fullmatch(args.target))
        if (args.sha or target_is_sha) and not args.id:
            raise ValueError('--id (random UUID v4) is required with --sha so the commit trailer can carry it')
        if args.id:
            if not SLOT_ID_RE.fullmatch(args.id):
                raise ValueError('--id must be a random UUID v4')
            payload['id'] = args.id
        _validate_ttl(args)
        if args.ttl_hours is not None:
            payload['ttl_hours'] = args.ttl_hours
        if args.note:
            payload['note'] = args.note[:512]
        return session_control.request('POST', '/api/releases/slots', payload)
    _validate_ttl(args)
    if args.releases_cmd == 'show':
        if not SLOT_ID_RE.fullmatch(args.id):
            raise ValueError('slot id must be a random UUID v4')
        return session_control.request('GET', '/api/releases/slots/' + args.id)
    if args.releases_cmd == 'list':
        query = '?limit=%d' % args.limit if args.limit else ''
        if args.status:
            query += ('&' if query else '?') + 'status=' + args.status
        return session_control.request('GET', '/api/releases/slots' + query)
    if args.releases_cmd == 'revoke':
        if not SLOT_ID_RE.fullmatch(args.id):
            raise ValueError('slot id must be a random UUID v4')
        return session_control.request('POST', '/api/releases/slots/' + args.id + '/revoke', {})
    raise ValueError('unknown releases command')


def _table(headers, rows) -> list:
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
    line = lambda cells: '  '.join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    return [line(headers)] + [line(r) for r in rows]


def _slot_row(s: dict) -> list:
    return [s.get('id'), (s.get('sha') or s.get('target') or '')[:12], s.get('status'),
            s.get('issued_by'), s.get('expires_at'), s.get('consumer'), (s.get('note') or '')[:40]]


def render(args: argparse.Namespace, result: dict) -> str:
    if getattr(args, 'json', False):
        return json.dumps(result, indent=2, ensure_ascii=True)
    if args.releases_cmd == 'list':
        out = _table(['SLOT ID', 'SHA/TARGET', 'STATUS', 'ISSUED BY', 'EXPIRES AT', 'CONSUMER', 'NOTE'],
                     [_slot_row(s) for s in result.get('slots') or []])
        return '\n'.join(out)
    if args.releases_cmd in ('show', 'issue', 'revoke'):
        s = result.get('slot') if isinstance(result.get('slot'), dict) else result
        return '\n'.join(_table(['SLOT ID', 'SHA/TARGET', 'STATUS', 'ISSUED BY', 'EXPIRES AT', 'CONSUMER', 'NOTE'], [_slot_row(s)]))
    return json.dumps(result, indent=2, ensure_ascii=True)


HINTS = {400: 'invalid sha, target, id, ttl or note', 403: 'admin (orchestrator/operator) credential required',
         404: 'unknown slot id', 409: 'conflict: slot id exists, target already has a live slot, or slot not in issued state'}


def run(args: argparse.Namespace) -> None:
    from . import session_control
    try:
        print(render(args, execute(args)))
    # Session errors subclass ValueError, so they must be caught first.
    except session_control.SessionHTTPError as exc:
        hint = HINTS.get(exc.status, '')
        print('hub releases failed: HTTP %s %s%s' % (exc.status, exc.code, (' (' + hint + ')') if hint else ''), file=sys.stderr)
        sys.exit(1)
    except session_control.SessionTransportError as exc:
        print(session_control.diagnostic(exc), file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print('hub releases: ' + str(exc)[:200], file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(session_control.diagnostic(exc), file=sys.stderr)
        sys.exit(1)
