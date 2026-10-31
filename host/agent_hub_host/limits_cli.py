"""`hub limits`: operator view and live set of fleet concurrency caps (intake cbcadaa1).

Caps live in the Hub fleet_accounts table, so a set is live (the next admission
reads it) and persists across Hub restarts and deploys. A target is an engine
(every account of that provider, e.g. claude -> hub-claude and shared-claude)
or one account label. Raise-only unless --allow-lower.
"""
import argparse
import json
import sys


def add_parser(sub) -> None:
    limits = sub.add_parser('limits', help='Operator fleet concurrency caps: show caps and usage, set a cap live')
    lsub = limits.add_subparsers(dest='limits_cmd', required=True)
    show = lsub.add_parser('show', help='Per-engine and per-account concurrency caps with current usage (admin)')
    show.add_argument('--json', action='store_true', help='Print the raw JSON response')
    st = lsub.add_parser('set', help='Set a concurrency cap live and persistently (admin; raise-only unless --allow-lower)')
    st.add_argument('target', help='Engine (claude, codex, gemini, ...) or account label (hub-claude)')
    st.add_argument('n', type=int, help='New max concurrency, 1..100')
    st.add_argument('--allow-lower', action='store_true',
                    help='Also lower caps above n; without it an engine set keeps higher caps and an account set refuses')
    st.add_argument('--json', action='store_true', help='Print the raw JSON response')


def execute(args: argparse.Namespace) -> dict:
    from . import session_control
    if args.limits_cmd == 'show':
        return session_control.request('GET', '/api/fleet/limits')
    if not 1 <= args.n <= 100:
        raise ValueError('n must be between 1 and 100')
    payload = {'target': args.target, 'max_concurrency': args.n}
    if args.allow_lower:
        payload['allow_lower'] = True
    return session_control.request('PATCH', '/api/fleet/limits', payload)


def _table(headers, rows) -> list:
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
    line = lambda cells: '  '.join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    return [line(headers)] + [line(r) for r in rows]


def render(args: argparse.Namespace, result: dict) -> str:
    if getattr(args, 'json', False):
        return json.dumps(result, indent=2, ensure_ascii=True)
    if args.limits_cmd == 'show':
        out = ['Engines (sum of account caps):']
        out += _table(['ENGINE', 'CAP', 'ACTIVE', 'ACCOUNTS'],
                      [[e.get('engine'), e.get('max_concurrency'), e.get('active'), ','.join(e.get('accounts') or [])]
                       for e in result.get('engines') or []])
        out += ['', 'Accounts:']
        out += _table(['ACCOUNT', 'ENGINE', 'CAP', 'ACTIVE', 'RESERVED', 'OBSERVED', 'LIVE', 'FREE', 'STATUS'],
                      [[a.get('account'), a.get('provider'), a.get('max_concurrency'), a.get('active'), a.get('reserved'),
                        a.get('observed_sessions'), a.get('native_live'), a.get('available_slots'),
                        a.get('status') if a.get('enabled') else 'disabled'] for a in result.get('accounts') or []])
        return '\n'.join(out)
    out = ['Set %s=%s (%s%s):' % (result.get('target'), result.get('max_concurrency'), result.get('kind'),
                                   ', allow-lower' if result.get('allow_lower') else '')]
    out += _table(['ACCOUNT', 'FROM', 'TO', 'RESULT'],
                  [[c.get('account'), c.get('from'), c.get('to'), c.get('result')] for c in result.get('changes') or []])
    return '\n'.join(out)


HINTS = {403: 'admin login required', 404: 'unknown engine or account',
         409: 'lowering an account cap needs --allow-lower'}


def run(args: argparse.Namespace) -> None:
    from . import session_control
    try:
        print(render(args, execute(args)))
    # Session errors subclass ValueError, so they must be caught first.
    except session_control.SessionHTTPError as exc:
        hint = HINTS.get(exc.status, '')
        # exc.code is the server error code already sanitized by http_error.
        print('hub limits failed: HTTP %s %s%s' % (exc.status, exc.code, (' (' + hint + ')') if hint else ''), file=sys.stderr)
        sys.exit(1)
    except session_control.SessionTransportError as exc:
        print(session_control.diagnostic(exc), file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print('hub limits: ' + str(exc)[:200], file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        # Never echo credentials or server bodies.
        print(session_control.diagnostic(exc), file=sys.stderr)
        sys.exit(1)
