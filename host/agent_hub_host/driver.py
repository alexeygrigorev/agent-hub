"""Interactive singleton driver CLI; lifecycle authority stays in Hub."""
import argparse
import math
import re
import time
import uuid

from . import intake, session_control


class DriverRequestError(intake.IntakeInputError):
    """Safe reconciliation advice without arbitrary transport error bodies."""


def wait_seconds(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('wait must be 0..600 seconds') from None
    if not math.isfinite(seconds) or not 0 <= seconds <= 600:
        raise argparse.ArgumentTypeError('wait must be 0..600 seconds')
    return seconds


def add_parser(sub):
    parser = sub.add_parser('driver', help='Manage the singleton interactive Hub driver')
    actions = parser.add_subparsers(dest='driver_cmd', required=True)
    actions.add_parser('status', help='Show driver, session and schedule state')
    for command in ('start', 'stop', 'restart'):
        action = actions.add_parser(command)
        action.add_argument('--key', help='Stable retry key (preserve after uncertain results)')
        action.add_argument('--wait', type=wait_seconds, default=120.0,
                            help='Wait up to 0..600 seconds for pending stop (default 120)')
        if command != 'stop':
            action.add_argument('--engine', choices=('claude', 'codex', 'antigravity', 'zai', 'opencode'))
            action.add_argument('--host', help='Host selection, validated by Hub placement')
            action.add_argument('--workspace', default='agent-hub' if command == 'start' else None)
        if command == 'start':
            action.add_argument('--force', action='store_true',
                                help='Replace the existing driver after confirmed termination')


def execute(args):
    command = args.driver_cmd
    if command == 'status':
        return session_control.request('GET', '/api/driver')
    if command not in ('start', 'stop', 'restart'):
        raise intake.IntakeInputError('Unknown driver action')
    key = args.key or str(uuid.uuid4())
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', key):
        raise intake.IntakeInputError('Driver key must contain 1..64 letters, digits, underscores or hyphens')
    payload = {'key': key}
    if command != 'stop':
        if args.workspace is not None:
            payload['workspace'] = args.workspace
        for field in ('engine', 'host'):
            if getattr(args, field, None) is not None:
                payload[field] = getattr(args, field)
    if command == 'start':
        payload['force'] = args.force
    deadline = time.monotonic() + args.wait
    path = '/api/driver/' + command
    def send():
        try:
            return session_control.request('POST', path, payload)
        except Exception as exc:
            raise DriverRequestError(
                diagnostic(exc) + '; reconcile with --key ' + key + ' before issuing a new intent') from None

    # Always issue the initial intent, including --wait 0. Repeat only the
    # server's explicit pending_stop response, with the identical key/payload.
    result = send()
    while isinstance(result, dict) and (result.get('pending_stop') is True or result.get('state') == 'pending_stop'):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, 'key': key, 'wait_timed_out': True}
        time.sleep(min(2.0, remaining))
        if time.monotonic() >= deadline:
            return {**result, 'key': key, 'wait_timed_out': True}
        result = send()
    return result


def diagnostic(exc):
    # The intake helper only exposes fixed-label or explicitly safe errors;
    # arbitrary exception messages may contain request credentials.
    if isinstance(exc, session_control.SessionHTTPError):
        return session_control.diagnostic(exc)
    return intake.diagnostic(exc)
