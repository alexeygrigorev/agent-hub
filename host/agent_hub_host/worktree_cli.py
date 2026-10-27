"""`hub worktree`: intake-owned git worktrees (list, refresh, remove, register).

The Hub records each intake's worktrees and queues `status`/`remove` commands
for the owning host agent (docs/INTAKE-WORKTREES.md). This client only talks
to the Hub user endpoints; it never touches a worktree on disk itself.
"""
import re
import time
import uuid

from . import intake

BASE = intake.BASE
SESSION_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')
TERMINAL_STATES = ('done', 'failed')
FAILED_OUTCOMES = ('refused', 'error')
POLL_INTERVAL = 3.0
DEFAULT_TIMEOUT = 120
# Module-level clock hooks (patched in tests without touching the global time module).
monotonic = time.monotonic
sleep = time.sleep


class WaitIncomplete(intake.IntakeInputError):
    """A --wait poll that timed out or ended with a failed/refused command.

    `report` carries the collected command states; the CLI prints it before
    exiting non-zero, like the close-gate refusal."""

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def add_commands(sub):
    root = sub.add_parser('worktree', help='Intake-owned git worktrees: list, refresh status, remove (host-side, guarded), register')
    commands = root.add_subparsers(dest='worktree_cmd', required=True)
    listing = commands.add_parser('list', help='Recorded worktrees of an intake with last host status and pending commands')
    listing.add_argument('intake')
    refresh = commands.add_parser('refresh', help='Queue a host status check (fetch origin/main, merged/dirty/unpushed)')
    refresh.add_argument('intake')
    refresh.add_argument('--worktree', help='Only this worktree id (default: all of the intake)')
    remove = commands.add_parser('remove', help='Queue a guarded git worktree remove on the owning host (never --force, never rm)')
    remove.add_argument('target', metavar='intake|session', help='Intake id (prefix ok) or a full Hub session UUID')
    remove.add_argument('--worktree', help='Only this worktree id (intake target only)')
    register = commands.add_parser('register', help='Record an existing worktree as owned by an intake')
    register.add_argument('intake')
    register.add_argument('--machine', required=True, help='Hub machine id of the owning host')
    register.add_argument('--path', required=True, help='Absolute worktree path on that host')
    register.add_argument('--branch')
    register.add_argument('--session', help='Hub session id that used the worktree')
    for command in (refresh, remove):
        command.add_argument('--wait', action='store_true', help='Poll the queued command(s) until done/failed')
        command.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT, help=f'--wait bound in seconds (default {DEFAULT_TIMEOUT})')
    for command in (refresh, remove, register):
        command.add_argument('--key', help='Stable retry key; generated (wt-<hex>) when omitted')


def task_path(task_id):
    return BASE + '/tasks/' + intake.safe_id(task_id)


def mutate(path, payload):
    try:
        return intake.request('POST', path, payload)
    except intake.IntakeTransportError as exc:
        raise intake.IntakeTransportError(f'{exc}; Retry key: {payload["key"]}') from None


def is_session_target(target):
    """A full UUID that does not resolve to an intake is a Hub session id."""
    if not SESSION_UUID.match(target):
        return False
    try:
        intake.request('GET', task_path(target))
    except intake.IntakeHTTPError as exc:
        if exc.status == 404:
            return True
        raise
    return False


def command_ids(response):
    """Queued command ids from a refresh/remove response (tolerant of shape)."""
    ids = []
    if not isinstance(response, dict):
        return ids
    commands = response.get('commands')
    if isinstance(commands, list):
        ids += [c.get('id') for c in commands if isinstance(c, dict)]
    command = response.get('command')
    if isinstance(command, dict):
        ids.append(command.get('id'))
    if isinstance(response.get('command_id'), str):
        ids.append(response['command_id'])
    if isinstance(response.get('command_ids'), list):
        ids += response['command_ids']
    result = []
    for value in ids:
        if isinstance(value, str) and value and value not in result:
            result.append(intake.safe_id(value))
    return result


def outcome(command):
    """Host outcome: the Hub keeps it under `result.outcome` (ok|refused|error)."""
    result = command.get('result') if isinstance(command.get('result'), dict) else {}
    return command.get('outcome', result.get('outcome'))


def hub_refusals(response):
    refused = response.get('refused') if isinstance(response, dict) else None
    return refused if isinstance(refused, list) else []


def command_state(response):
    command = response.get('command') if isinstance(response, dict) and isinstance(response.get('command'), dict) else response
    return command if isinstance(command, dict) else {}


def wait_for(response, timeout):
    if timeout is None or timeout < 1:
        raise intake.IntakeInputError('--timeout must be a positive number of seconds')
    ids = command_ids(response)
    refused = hub_refusals(response)
    if not ids:
        message = ('Hub refused the remove before queuing it' if refused
                   else 'No queued command id in the Hub response; nothing to wait for')
        raise WaitIncomplete(message, {'response': response, 'refused': refused})
    deadline = monotonic() + timeout
    final = {}
    pending = list(ids)
    while True:
        for command_id in list(pending):
            current = command_state(intake.request('GET', BASE + '/worktree-commands/' + command_id))
            final[command_id] = current
            if current.get('state') in TERMINAL_STATES:
                pending.remove(command_id)
        if not pending or monotonic() + POLL_INTERVAL > deadline:
            break
        sleep(POLL_INTERVAL)
    report = {'response': response, 'commands': [final[i] for i in ids], 'refused': refused, 'complete': not pending}
    if pending:
        report['timed_out'] = pending
        raise WaitIncomplete(f'Worktree command(s) still pending after {timeout}s: ' + ', '.join(pending), report)
    failed = [i for i in ids if final[i].get('state') == 'failed' or outcome(final[i]) in FAILED_OUTCOMES]
    if failed:
        raise WaitIncomplete('Worktree command(s) failed or were refused: ' + ', '.join(failed), report)
    if refused:
        raise WaitIncomplete('Hub refused the remove for some worktrees; see refused', report)
    return report


def execute(args):
    command = args.worktree_cmd
    if command == 'list':
        return intake.request('GET', task_path(args.intake) + '/worktrees')
    key = args.key or 'wt-' + uuid.uuid4().hex
    intake.valid_key(key)
    payload = {'key': key}
    if command == 'register':
        if not args.path.startswith('/'):
            raise intake.IntakeInputError('--path must be an absolute path on the owning host')
        worktree = {'machine': args.machine, 'path': args.path}
        if args.branch:
            worktree['branch'] = args.branch
        if args.session:
            worktree['session_id'] = intake.safe_id(args.session)
        payload['worktree'] = worktree
        return mutate(task_path(args.intake) + '/worktrees', payload)
    if getattr(args, 'wait', False) and (args.timeout is None or args.timeout < 1):
        raise intake.IntakeInputError('--timeout must be a positive number of seconds')
    if command == 'refresh':
        if args.worktree:
            payload['worktree_id'] = intake.safe_id(args.worktree)
        response = mutate(task_path(args.intake) + '/worktrees/refresh', payload)
    elif command == 'remove':
        target = intake.safe_id(args.target)
        if is_session_target(target):
            if args.worktree:
                raise intake.IntakeInputError('--worktree applies to an intake target, not a session')
            payload['session_id'] = target
            response = mutate(BASE + '/worktrees/remove', payload)
        else:
            if args.worktree:
                payload['worktree_id'] = intake.safe_id(args.worktree)
            response = mutate(task_path(target) + '/worktrees/remove', payload)
    else:
        raise intake.IntakeInputError('Unknown worktree operation')
    return wait_for(response, args.timeout) if args.wait else response
