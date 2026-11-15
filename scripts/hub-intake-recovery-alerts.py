#!/usr/bin/env python3
"""Mail dev@dtcdev.click (never the requester) for every intake Hub could not recover.

Hub2 records a `developer_alert_raised` event and sets the intake owner to
dev@dtcdev.click when an unfinished intake exhausts its bounded recovery
sessions, a recovery start is refused, or the session cannot be recovered
(lost host, launch failure). This poller finds those intakes across projects,
mails each alert once through the dev-mail helper and remembers it in a private
receipt journal. It runs every five minutes from ops/systemd/hub-intake-recovery-alerts.timer.

It also mails each Hub2 host_disconnected alert once per disconnect episode: a
host that keeps heartbeating (or holds live sessions) without its socket for
more than 15 minutes (intake 41ef7048 AC2, t1801u). Hub2 publishes those on
/api/fleet/routing/machines as `host_alert`.
"""
import datetime
import argparse
import json
import hashlib
import re
import stat
import time
import types
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


def alert_client(path, expected_sha):
    """One immutable selected companion; consume every owned FD before close."""
    if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
            or type(expected_sha) is not str or not re.fullmatch('[0-9a-f]{64}', expected_sha)):
        raise ValueError('observer_unavailable')
    owned = {'directory': os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)}
    primary = None
    try:
        for part in path.parts[1:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=owned['directory'])
            previous, owned['directory'] = owned['directory'], nxt
            # Ownership of previous has already transferred out of the cleanup
            # set. Even if close consumed it before raising, never retry it.
            os.close(previous)
            info = os.fstat(owned['directory'])
            if info.st_uid not in (0,os.getuid()) or info.st_mode & 0o002:
                raise ValueError('observer_unavailable')
        owned['source'] = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                  dir_fd=owned['directory'])
        before = os.fstat(owned['source'])
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in (0,os.getuid())
                or before.st_nlink != 1 or before.st_mode & 0o022 or not 0 < before.st_size <= 65536):
            raise ValueError('observer_unavailable')
        data = os.read(owned['source'],65537)
        stamp = lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if (len(data) != before.st_size or hashlib.sha256(data).hexdigest() != expected_sha
                or stamp(os.fstat(owned['source'])) != stamp(before)
                or stamp(os.stat(path.name,dir_fd=owned['directory'],follow_symlinks=False)) != stamp(before)):
            raise ValueError('observer_unavailable')
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup = None
        for key in ('source','directory'):
            fd = owned.pop(key, None)
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as error:
                    if cleanup is None:
                        cleanup = error
        if primary is None and cleanup is not None:
            raise cleanup
    module = types.ModuleType('selected_intake_mailer_alert_client')
    exec(compile(data,'<verified-intake-mailer-alert-client>','exec'),module.__dict__)
    return module


class AlertObserver:
    """Bounded positive-only callbacks; no dispatch, admission or mail authority."""
    def __init__(self, args):
        self.child, self.dirty, self.frames = None, False, 0
        try:
            deadline = time.monotonic() + 600
            module = alert_client(args.alert_client, args.alert_client_sha256)
            self.child = module.positive_only_context('intake_mailer',
                socket_path=args.alert_broker_socket, deadline=deadline)
        except Exception:
            self.fail()

    def fail(self):
        self.dirty = True
        if self.child is not None:
            self.child.dirty = True

    def emit(self, phase, **fields):
        try:
            if self.child is None or self.frames >= 16:
                self.fail(); return False
            self.frames += 1
            self.child.dirty |= self.dirty
            if not self.child.emit(phase, **fields):
                self.fail(); return False
            return True
        except BaseException as error:
            self.fail()
            if not isinstance(error, Exception):
                raise
            return False


def observe(observer, phase, *, decision=None, condition=None, branch=None, success=False):
    if observer is None:
        return
    flags = {'log_requested': bool(os.environ.get('ALERT_LOG'))}
    if branch is not None:
        flags[branch] = True
    if phase == 'terminal':
        flags['exit_success'] = success
    try:
        observer.emit(phase, decision=decision, condition=condition, flags=flags)
    except Exception:
        try:
            observer.fail()
        except Exception:
            pass


def observation_error(observer, condition):
    # Called while handling an original failure; observer cancellation must not
    # replace that exception or replay the business operation.
    if observer is not None:
        try:
            observer.fail()
            observe(observer, 'decision', decision='observation', condition=condition)
        except BaseException:
            pass


def terminal(observer, success, *, unwinding=False):
    if unwinding:
        try:
            observe(observer, 'terminal', success=False)
        except BaseException:
            pass
    else:
        observe(observer, 'terminal', success=success)


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


def pending_alerts(transport, observer=None):
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
        else:
            observation_error(observer, 'mailer_scan_incomplete')


def host_alerts(transport):
    """Yield (key, machine, alert) for hosts Hub2 reports running but disconnected."""
    page = transport.get('/api/fleet/routing/machines')
    for machine in page.get('machines') or []:
        alert = machine.get('host_alert') if isinstance(machine, dict) else None
        machine_id = str((machine or {}).get('machine_id', ''))
        if (not isinstance(alert, dict) or alert.get('kind') != 'host_disconnected'
                or machine.get('connected') is not False or not machine_id
                or not all(c.isalnum() or c in '-_.' for c in machine_id)):
            continue
        yield 'host:' + machine_id + ':' + str(alert.get('disconnected_since')), machine, alert


def render_host(machine, alert, now=None):
    since = alert.get('disconnected_since')
    stamp, minutes = '?', '?'
    if isinstance(since, int):
        stamp = datetime.datetime.fromtimestamp(since / 1000, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        minutes = str(max(0, int(((now if now is not None else datetime.datetime.now().timestamp() * 1000) - since) // 60000)))
    return ('Hub2 host {machine} is running but disconnected since {stamp} ({minutes} min; evidence: {evidence}). '
            'Sessions routed to it cannot start. On the host: check scheduled task AgentHub2Host and '
            '%LOCALAPPDATA%\\agent-hub2\\serve.log (Linux: journalctl --user -u agent-hub2-host); '
            'reinstall/update: hub2 self-update. Status: hub routing-machines.').format(
        machine=machine.get('machine_id'), stamp=stamp, minutes=minutes,
        evidence=alert.get('evidence') or 'unknown')


def render(item, alert):
    return ('Hub could not recover intake {id} ({project}): "{title}". Alert {kind} ({reason}) after '
            'session {session}; {depth} of {max} recovery sessions used. Status {status}. {cause}'
            'Inspect: hub intake context {id}; restart: hub intake start {id}.').format(
        cause=('Cause: ' + ' '.join(str(alert['cause']).split())[:1000] + '. ') if alert.get('cause') else '',
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


def alert_rows(transport, result, observer=None):
    stage = 'mailer_api_error'
    try:
        for key, item, alert in pending_alerts(transport, observer):
            stage = 'mailer_render_error'
            message = render(item, alert)
            yield key, message, 'intake_recovery'
            stage = 'mailer_api_error'
    except BaseException:
        observation_error(observer, stage)
        raise
    try:
        hosts = list(host_alerts(transport))
    except Exception:  # Preserve prior intake deliveries and original partial failure.
        result['errors'] += 1
        observation_error(observer, 'mailer_api_error')
        return
    for key, machine, alert in hosts:
        try:
            message = render_host(machine, alert)
        except BaseException:
            observation_error(observer, 'mailer_render_error')
            raise
        yield key, message, 'host_disconnected'


def all_alerts(transport, result):
    # Retain the existing two-field iterator API.
    for key, message, _branch in alert_rows(transport, result):
        yield key, message


def deliver(transport, state_path, helper, *, observer=None, _begun=False):
    success, unwinding = False, True
    stage = 'mailer_state_error'
    try:
        if not _begun:
            observe(observer, 'begin')
        state = load_state(state_path)
        sent = set(state['sent'])
        result = {'sent': 0, 'errors': 0}
        stage = None
        seen = False
        for key, message, branch in alert_rows(transport, result, observer):
            seen = True
            observe(observer, 'decision', decision='alert_dedup' if key in sent else 'alert',
                    condition=branch, branch=branch)
            if key in sent:
                continue
            env = dict(os.environ, PRINCIPAL_ALERT_TO=RECIPIENT, PING_ALERT_MESSAGE=message)
            stage = 'mailer_helper_error'
            try:
                completed = subprocess.run([str(helper), message], env=env, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, timeout=120, check=False)
            except subprocess.TimeoutExpired:
                stage = None
                observation_error(observer, 'mailer_helper_timeout')
                raise
            if completed.returncode:
                result['errors'] += 1
                observation_error(observer, 'mailer_helper_error')
                stage = None
                continue
            state['sent'].append(key)
            sent.add(key)
            stage = 'mailer_journal_error'
            save_state(state_path, state)
            result['sent'] += 1
            stage = None
        success = result['errors'] == 0
        if success:
            observe(observer, 'decision', decision='observation',
                    condition=('mailer_scan_incomplete' if observer is not None and observer.dirty
                               else 'mailer_scan_complete' if seen else 'mailer_scan_empty'))
        else:
            observation_error(observer, 'mailer_scan_incomplete')
        unwinding = False
        return result
    except BaseException:
        if stage is not None:
            observation_error(observer, stage)
        elif observer is not None:
            try:
                observer.fail()
            except BaseException:
                pass
        raise
    finally:
        terminal(observer, success, unwinding=unwinding)


def main(argv=None, *, observer=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='https://hub.dtcdev.click')
    parser.add_argument('--cli-json', type=Path, required=True)
    parser.add_argument('--helper', type=Path, required=True)
    parser.add_argument('--state', type=Path,
                        default=Path.home() / '.local/state/hub-intake-recovery-alerts/sent.json')
    parser.add_argument('--alert-client', type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument('--alert-client-sha256', default=None, help=argparse.SUPPRESS)
    parser.add_argument('--alert-broker-socket', default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if observer is None and any(value is not None for value in
            (args.alert_client, args.alert_client_sha256, args.alert_broker_socket)):
        observer = AlertObserver(args)
    try:
        observe(observer, 'begin')
        token = json.loads(args.cli_json.read_text())['access_token']
        transport = Transport(args.base_url, token)
    except BaseException:
        observation_error(observer, 'mailer_input_error')
        terminal(observer, False, unwinding=True)
        raise
    result = deliver(transport, args.state, args.helper, observer=observer, _begun=True)
    print('intake recovery alerts: ' + json.dumps(result))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
