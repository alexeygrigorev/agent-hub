"""Intake timing and attempts use the same authorized REST projection as the UI."""
from urllib.parse import quote, urlencode
from . import intake


def add_commands(sub):
    command = sub.add_parser('stats', help='Intake timing, attempts, and DONE aggregates')
    options(command)


def options(command, individual=False):
    if individual:
        command.add_argument('id')
    else:
        command.add_argument('--window', choices=['24h', '7d', '30d', 'all'], default='all')
        command.add_argument('--project')
        command.add_argument('--sort', choices=['created_at', 'done_at', 'lead_time_ms', 'cycle_time_ms', 'attempt_count', 'blocker_count'], default='attempt_count')
        command.add_argument('--order', choices=['asc', 'desc'], default='desc')
    command.add_argument('--json', action='store_true', help='Structured JSON output (also the default)')


def execute(args):
    if args.cmd == 'intake':
        path = '/api/intake/tasks/' + quote(args.id, safe='') + '/stats'
    else:
        params = {k: getattr(args, k) for k in ('window', 'project', 'sort', 'order') if getattr(args, k, None) is not None}
        path = '/api/intake/stats?' + urlencode(params)
    return intake.request('GET', path)
