#!/usr/bin/env python3
"""Submit durable Hub picker alerts through the configured dev-mail helper.

Delivery is at least once if this process dies after helper success and before
the local receipt is committed. A saved receipt suppresses another submission
when the server acknowledgement was lost.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

RECIPIENT = 'dev@dtcdev.click'
KIND = 'hub_picker_degraded'
MAX_STATE = 4 * 1024 * 1024


def private_read(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private file required')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('private file exceeds limit')
    return json.loads(raw)


def base_url(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1'))
            or not url.netloc or url.username or url.password or url.query or url.fragment):
        raise ValueError('invalid Hub origin')
    return value.rstrip('/')


class Transport:
    def __init__(self, base, token):
        self.base = base_url(base)
        if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(c) < 33 for c in token):
            raise ValueError('invalid CLI credential')
        self.token = token

    def request(self, method, path, body=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(self.base + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            raw = response.read(262_145)
        if len(raw) > 262_144:
            raise ValueError('Hub response exceeds limit')
        return json.loads(raw)


def number(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise ValueError('invalid picker counter')
    return value


def text(value, limit):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError('invalid picker text')
    return value


def render(offer):
    if not isinstance(offer, dict) or offer.get('recipient') != RECIPIENT:
        raise ValueError('invalid picker alert recipient')
    occurrence = number(offer.get('occurrence'), 1)
    payload = offer.get('payload')
    if not isinstance(payload, dict) or payload.get('to') != RECIPIENT or payload.get('occurrence') != occurrence:
        raise ValueError('invalid picker alert payload')
    picker = payload.get('picker')
    pool = picker.get('pool') if isinstance(picker, dict) else None
    if not isinstance(pool, list) or not 1 <= len(pool) <= 32:
        raise ValueError('invalid picker pool')
    models = []
    for row in pool:
        if not isinstance(row, dict):
            raise ValueError('invalid picker pool row')
        model, engine = text(row.get('model'), 120), text(row.get('engine'), 120)
        effort = row.get('effort')
        if effort is not None:
            effort = text(effort, 16)
        models.append(model + ' (' + engine + (', ' + effort if effort else '') + ')')
    attempt = number(payload.get('attempt'))
    if attempt > 1:
        raise ValueError('invalid picker attempt')
    return '\n'.join([
        text(payload.get('subject'), 240),
        f'Picker alert occurrence {occurrence}, receipt {number(payload.get("receipt_seq"), 1)}',
        'Project ' + text(payload.get('project_id'), 180),
        'Operation ' + text(payload.get('operation_id'), 180) + f', attempt {attempt}',
        'Eligible pool ' + ', '.join(dict.fromkeys(models)),
    ])


def save_state(path, state):
    data = (json.dumps(state, sort_keys=True) + '\n').encode()
    if len(data) > MAX_STATE:
        raise ValueError('picker receipt capacity reached')
    fd, temporary = tempfile.mkstemp(prefix='.picker-receipts-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def deliver(transport, state_path, helper):
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = state_path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077 or state_path.parent.is_symlink():
        raise ValueError('private state directory required')
    lock_path = state_path.with_name(state_path.name + '.lock')
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'a') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private lock required')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state_path.is_symlink():
            raise ValueError('private state file required')
        saved = private_read(state_path, MAX_STATE) if state_path.exists() else {'version': 1, 'base_url': transport.base, 'sent': {}}
        if (not isinstance(saved, dict) or saved.get('version') != 1 or saved.get('base_url') != transport.base
                or not isinstance(saved.get('sent'), dict)):
            raise ValueError('invalid picker receipt journal')
        response = transport.request('GET', '/api/picker-alerts/offers')
        offers = response.get('offers') if isinstance(response, dict) else None
        if not isinstance(offers, list) or len(offers) > 10:
            raise ValueError('invalid picker alert offers')
        result = {'saved': 0, 'acknowledged': 0, 'errors': 0}
        for offer in offers:
            try:
                message = render(offer)
                token = text(offer.get('lease_token'), 256)
                if not re.fullmatch(r'[0-9a-f]{64}', token):
                    raise ValueError('invalid picker alert lease')
                occurrence = str(offer['occurrence'])
                prefix = '/api/picker-alerts/' + occurrence
                digest = hashlib.sha256(message.encode()).hexdigest()
                prior = saved['sent'].get(occurrence)
                if prior is not None and prior != digest:
                    raise ValueError('picker occurrence conflict')
                if prior is None:
                    remaining = number(offer.get('lease_until'), 1) / 1000 - time.time() - 1
                    if remaining <= 0:
                        raise ValueError('picker alert lease expired')
                    candidate = {**saved, 'sent': {**saved['sent'], occurrence: digest}}
                    if len(json.dumps(candidate).encode()) > MAX_STATE - 1:
                        raise ValueError('picker receipt capacity reached')
                    env = dict(os.environ, PRINCIPAL_ALERT_TO=RECIPIENT, PING_ALERT_MESSAGE=message)
                    env.pop('ALERT_LOG', None)
                    completed = subprocess.run([str(helper)], env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=min(120, remaining), pass_fds=(lock.fileno(),), check=False)
                    if completed.returncode:
                        raise RuntimeError('picker mail helper failed')
                    save_state(state_path, candidate)
                    saved = candidate
                    result['saved'] += 1
                transport.request('POST', prefix + '/ack', {'lease_token': token})
                result['acknowledged'] += 1
            except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired):
                result['errors'] += 1
                # Malformed offers cannot choose a path or leak a token.
                if (isinstance(offer, dict) and type(offer.get('occurrence')) is int
                        and 0 < offer['occurrence'] <= 9007199254740991
                        and isinstance(offer.get('lease_token'), str)
                        and re.fullmatch(r'[0-9a-f]{64}', offer['lease_token'])):
                    try:
                        transport.request('POST', '/api/picker-alerts/' + str(offer['occurrence']) + '/nack',
                                          {'lease_token': offer['lease_token']})
                    except (ValueError, OSError, RuntimeError):
                        pass
        return result


def watermark(transport):
    counts = transport.request('GET', '/api/picker-alerts/counters')
    if not isinstance(counts, dict) or counts.get('recipient') != RECIPIENT:
        raise ValueError('invalid picker counter recipient')
    fields = ('total_picks', 'degraded_picks', 'consecutive_degraded', 'occurrence_watermark', 'receipt_watermark')
    verified = {key: number(counts.get(key)) for key in fields}
    if verified['consecutive_degraded'] > verified['degraded_picks'] or verified['degraded_picks'] > verified['total_picks'] or verified['occurrence_watermark'] > verified['degraded_picks'] // 3:
        raise ValueError('inconsistent picker counters')
    coverage = counts.get('coverage')
    if (not isinstance(coverage, dict) or coverage.get('available') is not True
            or not isinstance(coverage.get('epoch'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', coverage['epoch'])):
        raise ValueError('picker coverage unavailable')
    verified_coverage = {'available': True, 'epoch': coverage['epoch'], 'since': number(coverage.get('since'))}
    return {'kind': KIND, 'recipient': RECIPIENT, **verified, 'coverage': verified_coverage}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.environ.get('HUB_BASE_URL', 'https://hub.dtcdev.click'))
    parser.add_argument('--cli-json', type=Path, default=Path(os.environ.get('HUB_CLI_JSON', str(Path.home() / '.config/agent-hub/cli.json'))))
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/hub-picker-alerts/receipts.json')
    parser.add_argument('--helper', type=Path, default=Path(os.environ.get('HUB_PICKER_ALERT_CMD', str(Path.home() / '.local/bin/principal-alert.sh'))))
    parser.add_argument('--watermark', action='store_true')
    args = parser.parse_args(argv)
    try:
        credential = private_read(args.cli_json, 65_536)
        transport = Transport(args.base_url, credential.get('access_token') if isinstance(credential, dict) else None)
        result = watermark(transport) if args.watermark else deliver(transport, args.state, args.helper)
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get('errors') else 0
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired):
        print('Picker alert delivery unavailable; credentials and message bodies omitted.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
