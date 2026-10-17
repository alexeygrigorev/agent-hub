"""M14a one-call status over the existing public API. Payloads are data, never execution."""
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from . import api, auth, config

BASE = '/api/status'
CONTROL_CHARS = {'\x1b', '\x9b', '\u202e'}


def escape_display(text):
    if not isinstance(text, str):
        return '' if text is None else str(text)
    return ''.join('' if ch in CONTROL_CHARS else ch for ch in text)


def build_query(args):
    params = {}
    if getattr(args, 'limit', None) is not None:
        params['limit'] = args.limit
    for field in ('project', 'task', 'host', 'since', 'after'):
        value = getattr(args, field, None)
        if value is not None:
            params[field] = value
    query = urllib.parse.urlencode(params)
    return BASE + ('?' + query if query else '')


def fetch_status_get(args):
    """Single authenticated GET /api/status (readonly paginated projection)."""
    return _call_status(args, 'GET', None)


def fetch_status_post(args):
    """Single authenticated POST /api/status: complete bounded summary plus
    the caller's own baseline advance, one call total."""
    return _call_status(args, 'POST', b'{}')


def fetch_status(args):
    """Exactly one authenticated /api/status call per invocation. Default is
    POST (complete summary + own baseline diff); --limit/--after stay on the
    readonly paginated GET, which never advances the baseline."""
    if getattr(args, 'limit', None) is not None or getattr(args, 'after', None) is not None:
        return fetch_status_get(args)
    return fetch_status_post(args)


def _call_status(args, method, body):
    """Exactly one authenticated /api/status request per invocation."""
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    path = build_query(args)
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or auth.get_access_token()

    def send(access):
        headers = {'Authorization': 'Bearer ' + access}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(
            base.rstrip('/') + path,
            data=body,
            method=method,
            headers=headers,
        )
        return api.safe_http_json(req)

    try:
        return send(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not env_token:
            auth.refresh_token()
            try:
                return send(auth.get_access_token())
            except urllib.error.HTTPError:
                raise ValueError('Status HTTP request failed') from None
        raise ValueError('Status HTTP request failed') from None


def render_human(data):
    lines = []
    lines.append('snapshot: %s revision=%s coverage=%s' % (
        data.get('snapshot_at'), data.get('revision'),
        (data.get('coverage') or {}).get('sessions')))
    missing = (data.get('coverage') or {}).get('missing_hosts') or []
    if missing:
        lines.append('missing hosts: %s' % ', '.join(escape_display(h) for h in missing))
    offline = (data.get('coverage') or {}).get('expected_offline') or []
    if offline:
        lines.append('expected offline: %s' % ', '.join(escape_display(h) for h in offline))
    needs_attention = data.get('needs_attention') or []
    if needs_attention:
        lines.append('needs attention: %d' % len(needs_attention))
        for item in needs_attention:
            lines.append('  [%s] %s: %s' % (
                escape_display(item.get('severity')),
                escape_display(item.get('subject')),
                escape_display(item.get('detail'))))
    for task in data.get('tasks', []):
        lines.append('task %s [%s] %s updated=%s' % (
            escape_display(task.get('id')), escape_display(task.get('status')),
            escape_display(task.get('title')), escape_display(task.get('updated_at'))))
        if task.get('owner') or task.get('next_step'):
            lines.append('  owner=%s next=%s' % (
                escape_display(task.get('owner')), escape_display(task.get('next_step'))))
        for sess in task.get('sessions', []):
            lines.append('  session %s/%s engine=%s phase=%s linkage=%s observed=%s' % (
                escape_display(sess.get('machine_id')), escape_display(sess.get('native_id')),
                escape_display(sess.get('engine')), escape_display(sess.get('phase')),
                escape_display(sess.get('linkage')), escape_display(sess.get('observed_at'))))
        for hub in task.get('hub_sessions', []):
            life = hub.get('lifecycle') or {}
            decision = life.get('decision') or {}
            lines.append('  hub-session %s state=%s machine=%s lifecycle=%s/%s%s' % (
                escape_display(hub.get('hub_session_id')), escape_display(hub.get('state')),
                escape_display(hub.get('machine')),
                escape_display(life.get('group')), escape_display(life.get('state')),
                ' decision=%s' % escape_display(decision.get('kind')) if decision.get('kind') else ''))
        for action in task.get('pending_actions', []):
            lines.append('  pending %s by %s: %s' % (
                escape_display(action.get('type')), escape_display(action.get('actor')),
                escape_display(action.get('text_snippet'))))
        reports = task.get('reports', [])
        commits = task.get('commits', [])
        if reports:
            lines.append('  reports: %s (headline unknown, M14b)' % len(reports))
        if commits:
            lines.append('  commits: %s (deploy/CI unknown, M14c)' % len(commits))
    disk_fs = data.get('disk') or []
    if disk_fs:
        lines.append('disk:')
        for fs in disk_fs:
            primary_str = '' if fs.get('is_primary', True) else ' (alias of %s)' % escape_display(fs.get('primary_mount'))
            trend_str = ''
            if fs.get('growth_per_day_bytes') is not None:
                growth_gb = fs['growth_per_day_bytes'] / (1024 ** 3)
                trend_str = ' growth=%+.1fGB/day' % growth_gb
            elif fs.get('trend_state') and fs.get('trend_state') != 'unknown':
                trend_str = ' trend=%s' % escape_display(fs.get('trend_state'))
            lines.append('  %s %s: %.1f%% (%s)%s%s' % (
                escape_display(fs.get('machine_id')),
                escape_display(fs.get('mount_point')),
                float(fs.get('percent') or 0.0),
                escape_display(fs.get('status')),
                primary_str,
                trend_str))
    cats = data.get('categories') or {}
    summary = data.get('summary')
    if isinstance(summary, dict):
        by_status = summary.get('by_status') or {}
        lines.append('summary: %s tasks%s%s' % (
            summary.get('total'),
            ' truncated' if summary.get('truncated') else ' complete',
            ' by_status=%s' % ','.join('%s=%s' % (escape_display(k), v) for k, v in sorted(by_status.items())) if by_status else ''))
    diff = cats.get('diff') or {}
    if diff:
        if diff.get('state') in ('available', 'first_baseline'):
            lines.append('diff baseline: %s added=%d removed=%d changed=%d%s' % (
                escape_display(diff.get('state')),
                len(diff.get('added') or []), len(diff.get('removed') or []), len(diff.get('changed') or []),
                ' advanced' if diff.get('baseline_advanced') else ''))
        else:
            lines.append('diff baseline: %s (%s)' % (
                escape_display(diff.get('state')), escape_display(diff.get('reason'))))
    return '\n'.join(lines) + '\n'


def add_commands(sub):
    parser = sub.add_parser('status', help='One-call authorized task/session/report/commit/pending-action status')
    parser.add_argument('--since', default=None, help='Filter to changes since duration (e.g. 2h, 30m) or ISO time')
    parser.add_argument('--json', dest='json', action='store_true', help='Output raw JSON DTO')
    parser.add_argument('--task', default=None, help='Single task id')
    parser.add_argument('--project', default=None, help='Project scope filter')
    parser.add_argument('--host', default=None, help='Host/machine filter')
    parser.add_argument('--after', default=None, help='Pagination cursor (task id)')
    parser.add_argument('--limit', type=int, default=None, help='Page size 1..25')
    parser.add_argument('--local-only', action='store_true', help='Local config diagnostics only, no API call')


def execute(args):
    if getattr(args, 'local_only', False):
        cfg = config.load_config()
        if not cfg.get('access_token'):
            print('Status: Not logged in.')
            sys.exit(1)
        print('Status: Logged in.')
        print('Base URL: %s' % cfg.get('base_url'))
        return {'local': True, 'base_url': cfg.get('base_url')}
    limit = getattr(args, 'limit', None)
    if limit is not None and not 1 <= limit <= 25:
        raise ValueError('Invalid limit')
    data = fetch_status(args)
    if getattr(args, 'json', False):
        print(json.dumps(data, indent=2, ensure_ascii=True))
    else:
        sys.stdout.write(render_human(data))
    return data
