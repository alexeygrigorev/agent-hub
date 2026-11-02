"""Operator CLI for Hub cron launch schedules (intake a839dfb6).

Public HTTP surface only: talks to /api/schedules* with the operator
credential through the same request helper as session control. Each tick the
server creates a fresh intake and starts a headless session through the normal
admission path; this CLI only manages schedule definitions and reads run
history, it never starts sessions directly.
"""
import argparse
import json

from . import session_control

PROMPT_MAX_BYTES = 7000
AC_MAX_BYTES = 8192


def _resolve_ref(args) -> str:
    ref = getattr(args, 'id', None)
    if not ref:
        raise session_control.SessionInputError('schedule id or name required')
    return ref


def execute(args: argparse.Namespace):
    cmd = getattr(args, 'schedules_cmd', None)
    if cmd == 'create':
        payload = {
            'name': args.name,
            'project': args.project,
            'cron': args.cron,
        }
        if args.tz:
            payload['tz'] = args.tz
        if args.engine:
            payload['engine'] = args.engine
        if args.tier:
            payload['tier'] = args.tier
        if args.task_kind != 'implement':
            payload['task_kind'] = args.task_kind
        if args.workspace:
            payload['workspace'] = args.workspace
        if args.overlap != 'skip':
            payload['overlap'] = args.overlap
        if args.prompt_file:
            payload['prompt'] = session_control.read_file(
                args.prompt_file, limit=PROMPT_MAX_BYTES, field='Prompt')
        elif args.prompt:
            payload['prompt'] = session_control.read_prompt_text(
                args.prompt, limit=PROMPT_MAX_BYTES, field='Prompt')
        if args.acceptance_criteria_file:
            payload['acceptance_criteria'] = session_control.read_file(
                args.acceptance_criteria_file, limit=AC_MAX_BYTES, field='Acceptance criteria')
        elif args.acceptance_criteria:
            payload['acceptance_criteria'] = session_control.read_prompt_text(
                args.acceptance_criteria, limit=AC_MAX_BYTES, field='Acceptance criteria')
        if args.disabled:
            payload['enabled'] = False
        return session_control.request('POST', '/api/schedules', payload)
    if cmd == 'list':
        return session_control.request('GET', '/api/schedules')
    ref = _resolve_ref(args)
    if cmd == 'show':
        return session_control.request('GET', '/api/schedules/' + ref)
    if cmd == 'pause':
        return session_control.request('POST', f'/api/schedules/{ref}/pause', {})
    if cmd == 'resume':
        return session_control.request('POST', f'/api/schedules/{ref}/resume', {})
    if cmd == 'tick':
        return session_control.request('POST', f'/api/schedules/{ref}/tick', {})
    if cmd == 'runs':
        return session_control.request('GET', f'/api/schedules/{ref}/runs?limit={int(args.limit)}')
    if cmd == 'delete':
        if not getattr(args, 'yes', False):
            raise session_control.SessionInputError('deleting a launch schedule requires --yes (run history is kept)')
        return session_control.request('DELETE', '/api/schedules/' + ref)
    raise session_control.SessionInputError('unknown schedules subcommand')


def render_table(data: dict) -> str:
    rows = data.get('schedules', []) if isinstance(data, dict) else []
    if not rows:
        return 'no launch schedules'
    headers = ['name', 'id', 'project', 'cron', 'tz', 'overlap', 'next due', 'enabled']
    lines = ['  '.join(headers)]
    for row in rows:
        values = [row.get(key, '') for key in ['name', 'id', 'project', 'cron', 'tz', 'overlap', 'next_due_at', 'enabled']]
        if not values[6]:
            values[6] = 'paused' if not row.get('enabled') else ''
        lines.append('  '.join(str(value) for value in values))
    return '\n'.join(lines)


def render_runs(data: dict) -> str:
    rows = data.get('runs', []) if isinstance(data, dict) else []
    if not rows:
        return 'no runs'
    headers = ['tick at', 'fired at', 'outcome', 'intake', 'session', 'reason']
    lines = ['  '.join(headers)]
    for row in rows:
        lines.append('  '.join(str(row.get(key, '')) for key in
                               ['tick_at', 'fired_at', 'outcome', 'intake_id', 'session_id', 'reason']))
    return '\n'.join(lines)


def render_output(args: argparse.Namespace, result):
    if getattr(args, 'json', False):
        return result
    cmd = getattr(args, 'schedules_cmd', None)
    if cmd == 'list':
        return render_table(result)
    if cmd == 'runs':
        return render_runs(result)
    if cmd == 'tick':
        return json.dumps(result, indent=2, ensure_ascii=True)
    return json.dumps(result, indent=2, ensure_ascii=True)


def diagnostic(exc: Exception) -> str:
    if isinstance(exc, session_control.SessionHTTPError):
        # SessionHTTPError exposes status and code; its explanatory message is
        # stored as exception text, not as a `.message` attribute. The message
        # may contain details inappropriate for CLI output, so render only the
        # bounded status and parser-validated error code.
        status = exc.status if isinstance(exc.status, int) and 100 <= exc.status <= 599 else 'unknown'
        code = exc.code if isinstance(exc.code, str) and session_control.SAFE_ERROR.fullmatch(exc.code) else 'request_failed'
        return f'schedule operation failed: HTTP {status} ({code})'
    if isinstance(exc, session_control.SessionInputError):
        return f'schedule operation failed: {exc}'
    if isinstance(exc, session_control.SessionTransportError):
        return 'schedule operation failed: Hub transport error'
    return 'schedule operation failed; check authentication and inputs (details withheld)'


def add_parser(sub) -> None:
    root = sub.add_parser('schedule', help='Cron launch schedules: a fresh session per tick through normal admission (operator)')
    commands = root.add_subparsers(dest='schedules_cmd', required=True)

    create = commands.add_parser('create', help='Create a launch schedule')
    create.add_argument('--name', required=True, help='Schedule slug (lowercase, 2-40 chars; session names append .YYYYMMDD-HHMM)')
    create.add_argument('--project', required=True, help='Project every tick intake is filed under')
    create.add_argument('--cron', required=True, help='Five-field cron (minute hour dom month dow), evaluated in --tz')
    create.add_argument('--tz', default='UTC', help='IANA timezone for cron evaluation (default UTC)')
    create.add_argument('--engine', help='Pinned engine for launched sessions (requires operator policy)')
    create.add_argument('--tier', help='Tier for launched sessions')
    create.add_argument('--task-kind', choices=['implement', 'review', 'ask', 'goal'], default='implement')
    create.add_argument('--workspace', help='Workspace for launched sessions (default: the project)')
    create.add_argument('--overlap', choices=['skip', 'queue', 'allow'], default='skip',
                        help='What a due tick does while the previous run is still live (default skip)')
    prompt_src = create.add_mutually_exclusive_group(required=True)
    prompt_src.add_argument('--prompt-file', help='UTF-8 prompt file, - for stdin (max 7000 bytes)')
    prompt_src.add_argument('--prompt', help='Inline prompt text')
    ac_src = create.add_mutually_exclusive_group()
    ac_src.add_argument('--acceptance-criteria-file', help='UTF-8 acceptance criteria file (max 8192 bytes)')
    ac_src.add_argument('--acceptance-criteria', help='Inline acceptance criteria text')
    create.add_argument('--disabled', action='store_true', help='Create the schedule disabled')
    create.add_argument('--json', action='store_true')

    listing = commands.add_parser('list', help='List launch schedules')
    listing.add_argument('--json', action='store_true')

    show = commands.add_parser('show', help='Show one schedule with its 20 most recent runs')
    show.add_argument('id', help='Schedule id or name')
    show.add_argument('--json', action='store_true')

    pause = commands.add_parser('pause', help='Pause a schedule (no backfill on resume)')
    pause.add_argument('id', help='Schedule id or name')
    pause.add_argument('--json', action='store_true')

    resume = commands.add_parser('resume', help='Resume a schedule from the next occurrence')
    resume.add_argument('id', help='Schedule id or name')
    resume.add_argument('--json', action='store_true')

    delete = commands.add_parser('delete', help='Delete a schedule (run history is kept)')
    delete.add_argument('id', help='Schedule id or name')
    delete.add_argument('--yes', action='store_true', help='Confirm deletion')
    delete.add_argument('--json', action='store_true')

    runs = commands.add_parser('runs', help='Recent run history: tick time, intake, session, outcome')
    runs.add_argument('id', help='Schedule id or name')
    runs.add_argument('--limit', type=int, default=50)
    runs.add_argument('--json', action='store_true')

    tick = commands.add_parser('tick', help='Evaluate one schedule now (fires if an occurrence is due)')
    tick.add_argument('id', help='Schedule id or name')
    tick.add_argument('--json', action='store_true')
