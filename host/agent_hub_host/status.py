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


def fetch_status(args):
    """Exactly one authenticated GET /api/status per invocation."""
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    path = build_query(args)
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or auth.get_access_token()

    def send(access):
        req = urllib.request.Request(
            base.rstrip('/') + path,
            method='GET',
            headers={'Authorization': 'Bearer ' + access},
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
    for task in data.get('tasks', []):
        lines.append('task %s [%s] %s updated=%s' % (
            escape_display(task.get('id')), escape_display(task.get('status')),
            escape_display(task.get('title')), escape_display(task.get('updated_at'))))
        for sess in task.get('sessions', []):
            lines.append('  session %s/%s engine=%s phase=%s linkage=%s observed=%s' % (
                escape_display(sess.get('machine_id')), escape_display(sess.get('native_id')),
                escape_display(sess.get('engine')), escape_display(sess.get('phase')),
                escape_display(sess.get('linkage')), escape_display(sess.get('observed_at'))))
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
    cats = data.get('categories') or {}
    diff = cats.get('diff') or {}
    if diff:
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
