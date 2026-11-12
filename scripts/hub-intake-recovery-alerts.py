#!/usr/bin/env python3
"""Mail dev@dtcdev.click (never the requester) for every intake Hub could not recover.

Hub2 records a `developer_alert_raised` event and sets the intake owner to
dev@dtcdev.click when an unfinished intake exhausts its bounded recovery
sessions, a recovery start is refused, or the session cannot be recovered
(lost host, launch failure). This poller finds those intakes across projects,
mails each alert once through the dev-mail helper and remembers it in a private
receipt journal. It runs every five minutes from ops/systemd/hub-intake-recovery-alerts.timer.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

RECIPIENT = 'dev@dtcdev.click'
STATUSES = ('open', 'failed')
MAX_PAGES = 20
MAX_SENT = 5000


class Transport:
    def __init__(self, base, token):
        url = urllib.parse.urlsplit(base)
        if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1')):
            raise ValueError('invalid Hub origin')
        self.base = base.rstrip('/')
        self.token = token

    def get(self, path):
        request = urllib.request.Request(self.base + path, headers={'Authorization': 'Bearer ' + self.token})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(4 * 1024 * 1024))


def pending_alerts(transport):
    """Yield (key, intake, alert body) for unresolved developer alerts."""
    for status in STATUSES:
        cursor = None
        for _ in range(MAX_PAGES):
            query = {'status': status, 'search': RECIPIENT, 'limit': '50'}
            if cursor:
                query['cursor'] = cursor
            page = transport.get('/api/intakes?' + urllib.parse.urlencode(query))
            for item in page.get('items') or []:
                intake_id = str(item.get('id', ''))
                if not intake_id or not all(c.isalnum() or c == '-' for c in intake_id):
                    continue
                context = transport.get('/api/intakes/' + intake_id + '/context')
                if (context.get('task') or {}).get('owner') != RECIPIENT:
                    continue  # already picked up by someone, or restarted
                for event in context.get('events') or []:
                    if event.get('kind') == 'developer_alert_raised':
                        yield intake_id + ':' + str(event.get('seq')), item, event.get('body') or {}
            cursor = page.get('next')
            if not cursor:
                break


def render(item, alert):
    return ('Hub could not recover intake {id} ({project}): "{title}". Alert {kind} ({reason}) after '
            'session {session}; {depth} of {max} recovery sessions used. Status {status}. '
            'Inspect: hub intake context {id}; restart: hub intake start {id}.').format(
        id=item.get('id'), project=item.get('project'), title=str(item.get('title', ''))[:200],
        kind=alert.get('kind'), reason=alert.get('reason'), session=alert.get('session_id'),
        depth=alert.get('depth'), max=alert.get('max_continuations'), status=item.get('status'))


def load_state(path):
    if not path.exists():
        return {'version': 1, 'sent': []}
    state = json.loads(path.read_text())
    if state.get('version') != 1 or not isinstance(state.get('sent'), list):
        raise ValueError('invalid recovery alert journal')
    return state


def save_state(path, state):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state['sent'] = state['sent'][-MAX_SENT:]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.recovery-alerts-')
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream)
    os.replace(tmp, path)


def deliver(transport, state_path, helper):
    state = load_state(state_path)
    sent = set(state['sent'])
    result = {'sent': 0, 'errors': 0}
    for key, item, alert in pending_alerts(transport):
        if key in sent:
            continue
        message = render(item, alert)
        env = dict(os.environ, PRINCIPAL_ALERT_TO=RECIPIENT, PING_ALERT_MESSAGE=message)
        completed = subprocess.run([str(helper), message], env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=120, check=False)
        if completed.returncode:
            result['errors'] += 1
            continue
        state['sent'].append(key)
        sent.add(key)
        save_state(state_path, state)
        result['sent'] += 1
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='https://hub.dtcdev.click')
    parser.add_argument('--cli-json', type=Path, required=True)
    parser.add_argument('--helper', type=Path, required=True)
    parser.add_argument('--state', type=Path,
                        default=Path.home() / '.local/state/hub-intake-recovery-alerts/sent.json')
    args = parser.parse_args(argv)
    token = json.loads(args.cli_json.read_text())['access_token']
    result = deliver(Transport(args.base_url, token), args.state, args.helper)
    print('intake recovery alerts: ' + json.dumps(result))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
