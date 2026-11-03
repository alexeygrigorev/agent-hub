"""`hub asks` — Hub ask ledger (intake 55c10deb).

The Tabo replica watcher (intake 290b935a) is the primary writer; `hub asks
record` is the fallback a bot or operator uses to store one of Alexey's
messages verbatim. Text is read byte-exact from a file or stdin and is never
trimmed or rewritten. Everything else is read/maintenance over /api/asks*.
"""
import argparse
import json
import sys
import time
import urllib.parse

from . import session_control

# The shared request helper caps a request at 32 KiB; longer messages come
# from the watcher. Refuse instead of truncating.
MAX_RECORD_BYTES = 28000
STATES = ['pending', 'unmapped', 'mapped', 'verified', 'dropped_after_done', 'closed_unverified', 'cancelled', 'not_an_ask']


def _query(**params):
    clean = {k: v for k, v in params.items() if v not in (None, '')}
    return ('?' + urllib.parse.urlencode(clean)) if clean else ''


def _since_ms(value):
    if not value:
        return None
    if value.isdigit():
        return int(value)
    units = {'m': 60, 'h': 3600, 'd': 86400}
    if value[-1:] in units and value[:-1].isdigit():
        return int((time.time() - int(value[:-1]) * units[value[-1]]) * 1000)
    raise session_control.SessionInputError('--since takes epoch ms or a duration like 30m, 6h, 2d')


def execute(args: argparse.Namespace):
    cmd = args.asks_cmd
    if cmd == 'record':
        text = session_control.read_file(args.text_file, limit=MAX_RECORD_BYTES, field='Message text')
        if not text.strip():
            raise session_control.SessionInputError('Message text is empty')
        message = {'msg_id': args.ref, 'role': 'user', 'kind': 'message', 'text': text}
        if args.ts:
            message['ts'] = args.ts
        if args.intake:
            message['intake_id'] = args.intake
        agent = {'agent_id': args.agent, 'agent_name': args.agent_name, 'messages': [message]}
        return session_control.request('POST', '/api/asks/ingest', {'source': args.source, 'agents': [agent]})
    if cmd == 'list':
        return session_control.request('GET', '/api/asks' + _query(state=args.state, agent_id=args.agent, intake=args.intake, limit=args.limit))
    if cmd == 'map':
        payload = {'intake_id': args.intake}
        if args.ac is not None:
            payload['ac_line'] = args.ac
        return session_control.request('POST', f'/api/asks/{args.ask}/map', payload)
    if cmd == 'dismiss':
        return session_control.request('POST', f'/api/asks/{args.ask}/dismiss', {'reason': args.reason})
    if cmd == 'verify':
        return session_control.request('POST', f'/api/asks/{args.ask}/verify', {'evidence': args.evidence})
    if cmd in ('tail', 'search'):
        return session_control.request('GET', '/api/asks/messages' + _query(
            agent_id=args.agent, role=args.role, since=_since_ms(args.since), limit=args.limit))
    if cmd == 'summary':
        return session_control.request('GET', '/api/asks/summary')
    if cmd == 'check':
        return session_control.request('POST', '/api/asks/check', {})
    if cmd == 'settings':
        payload = {}
        if args.gap_scope:
            payload['gap_scope'] = args.gap_scope
        if args.reconcile_intake:
            payload['reconcile_intake'] = None if args.reconcile_intake == 'none' else args.reconcile_intake
        for key in ('reconcile_max_live', 'reconcile_daily_cap'):
            if getattr(args, key) is not None:
                payload[key] = getattr(args, key)
        return session_control.request('POST', '/api/asks/settings', payload)
    raise session_control.SessionInputError('unknown asks command')


def _line(text, width=100):
    return ' '.join(str(text or '').split())[:width]


def render_output(args, result):
    if getattr(args, 'json', False):
        return result
    cmd = args.asks_cmd
    if cmd == 'list':
        counts = result.get('counts') or {}
        out = ['counts: ' + ', '.join(f'{k}={counts.get(k, 0)}' for k in STATES + ['open_gaps', 'messages'])]
        for a in result.get('asks') or []:
            target = f"{(a.get('intake_id') or '-')[:8]}" + (f" AC{a['ac_line']}" if a.get('ac_line') is not None else '')
            out.append(f"{a['id'][:8]}  {a['state']:<18} {a.get('agent_name') or a.get('agent_id')}/{a['t_id']:<8} {target:<14} {_line(a.get('text'), 80)}")
        return '\n'.join(out)
    if cmd in ('tail', 'search'):
        rows = list(reversed(result.get('messages') or []))
        if cmd == 'search':
            needle = args.text.lower()
            rows = [r for r in rows if needle in (r.get('text') or '').lower()]
        return '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows)
    return result


def diagnostic(exc):
    return session_control.diagnostic(exc)


def add_parser(sub) -> None:
    root = sub.add_parser('asks', help="Ask ledger: Alexey's verbatim messages, their asks and intake AC mapping")
    commands = root.add_subparsers(dest='asks_cmd', required=True)
    record = commands.add_parser('record', help='Fallback writer: store one of Alexey\'s messages verbatim (idempotent on agent+ref)')
    record.add_argument('--ref', required=True, help='Message id, e.g. t1985u')
    record.add_argument('--agent', required=True, help='Bot agent id (Grok Bot agent uuid or a stable name)')
    record.add_argument('--agent-name', help='Display name, e.g. Developer')
    record.add_argument('--intake', help='Explicit intake link (operator credential)')
    record.add_argument('--ts', help='Message time (ISO-8601 or epoch ms); default now')
    record.add_argument('--source', choices=['bot', 'cli', 'backfill'], default='bot')
    record.add_argument('--text-file', required=True, help='UTF-8 file with the verbatim text, or - for stdin')
    record.add_argument('--json', action='store_true')
    listing = commands.add_parser('list', help='Asks with source ref, mapped intake + AC line and state')
    listing.add_argument('--state', choices=STATES)
    listing.add_argument('--agent')
    listing.add_argument('--intake')
    listing.add_argument('--limit', type=int, default=100)
    listing.add_argument('--json', action='store_true')
    mapping = commands.add_parser('map', help='Map an ask to an intake AC line')
    mapping.add_argument('ask', help='Ask id (or unique 6+ char prefix)')
    mapping.add_argument('--intake', required=True)
    mapping.add_argument('--ac', type=int, help='AC line number; default: best match or none')
    mapping.add_argument('--json', action='store_true')
    dismiss = commands.add_parser('dismiss', help='Mark an ask as not work (reason required, visible in the ledger)')
    dismiss.add_argument('ask')
    dismiss.add_argument('--reason', required=True)
    dismiss.add_argument('--json', action='store_true')
    verify = commands.add_parser('verify', help='Mark a mapped ask verified with evidence')
    verify.add_argument('ask')
    verify.add_argument('--evidence', required=True)
    verify.add_argument('--json', action='store_true')
    for name, helptext in (('tail', 'Recent messages as JSONL (both sides)'), ('search', 'Search recent messages as JSONL')):
        p = commands.add_parser(name, help=helptext)
        if name == 'search':
            p.add_argument('text')
        p.add_argument('--agent')
        p.add_argument('--role', choices=['user', 'assistant', 'system'])
        p.add_argument('--since', help='epoch ms or 30m / 6h / 2d')
        p.add_argument('--limit', type=int, default=100 if name == 'tail' else 500)
        p.add_argument('--json', action='store_true')
    summary = commands.add_parser('summary', help='Counts, sources, agents, gaps, reconciler')
    summary.add_argument('--json', action='store_true')
    check = commands.add_parser('check', help='Run the ledger pass now (operator)')
    check.add_argument('--json', action='store_true')
    settings = commands.add_parser('settings', help='Operator settings')
    settings.add_argument('--gap-scope', choices=['agent', 'global', 'window'])
    settings.add_argument('--reconcile-intake', help='Intake the reconciler sessions attach to, or none')
    settings.add_argument('--reconcile-max-live', type=int, help='Max concurrent reconciler sessions (default 1)')
    settings.add_argument('--reconcile-daily-cap', type=int, help='Max reconciler launch attempts per UTC day (default 8)')
    settings.add_argument('--json', action='store_true')
