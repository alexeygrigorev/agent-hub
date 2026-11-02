"""`hub ops`: orchestrator/watchdog liveness reports for Hub-side checks (intake 500fd60b)."""
import argparse
import json
import sys

ROLES = ('orchestrator', 'oncall')
STATES = ('ok', 'stalled', 'dead', 'recovering', 'proof_pending', 'proof_failed', 'census_failed', 'restart_failed')


def add_parser(sub) -> None:
    ops = sub.add_parser('ops', help='Ops orchestrator liveness: report a tick or watchdog run, read Hub-side status')
    osub = ops.add_subparsers(dest='ops_cmd', required=True)
    hb = osub.add_parser('heartbeat', help='Report one orchestrator tick or one watchdog run')
    hb.add_argument('--role', choices=ROLES, default='orchestrator')
    hb.add_argument('--kind', choices=('tick', 'watchdog'), required=True)
    hb.add_argument('--state', choices=STATES, help='Watchdog result (kind=watchdog only)')
    hb.add_argument('--recovery-failures', type=int, help='Consecutive failed recoveries (kind=watchdog only)')
    hb.add_argument('--tick-at', help='ISO time of the newest tick the watchdog saw (kind=watchdog only)')
    hb.add_argument('--summary', help='Non-secret one-line summary (max 240 chars)')
    osub.add_parser('status', help='Hub-side orchestrator, watchdog and backlog idle status (admin)')


def payload(args: argparse.Namespace) -> dict:
    body = {'role': args.role, 'kind': args.kind}
    if args.kind == 'watchdog':
        if args.state: body['state'] = args.state
        if args.recovery_failures is not None: body['recovery_failures'] = args.recovery_failures
        if args.tick_at: body['tick_at'] = args.tick_at
    elif args.state or args.recovery_failures is not None:
        raise ValueError('--state/--recovery-failures apply to --kind watchdog only')
    elif args.tick_at:
        body['tick_at'] = args.tick_at
    if args.summary: body['summary'] = ' '.join(args.summary.split())[:240]
    return body


def execute(args: argparse.Namespace) -> dict:
    from . import bus
    if args.ops_cmd == 'status':
        return bus._do_request('GET', '/api/ops/liveness')
    return bus._do_request('POST', '/api/ops/heartbeat', payload(args))


def run(args: argparse.Namespace) -> None:
    try:
        print(json.dumps(execute(args), indent=2))
    except ValueError as exc:
        print('hub ops: ' + str(exc)[:200], file=sys.stderr)
        sys.exit(2)
    except Exception:
        # Never echo credentials or server bodies.
        print('hub ops failed; check login (admin) or an agent credential with health:report.', file=sys.stderr)
        sys.exit(1)
