"""Hub session orchestration client (M12e). Prompts are data; this client never executes them.

Public HTTP surface only: talks to /api/sessions* and /api/session-commands/*
with the existing token/device credentials. Native execution belongs to the
sibling host worker; this module performs no native effects and imports no
native adapters. See docs/SESSION-CONTROL.md (proposal, pending head ACK).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from . import api, auth, config

MAX_REQUEST = 32768
MAX_BRIEF = 8192
MAX_BODY = 16384
TIERS = ('quick', 'daily', 'deep')


def request(method, path, payload=None):
    cfg = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
    api.check_url(base)
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    if data is not None and len(data) > MAX_REQUEST:
        raise ValueError('Session request exceeds capacity')
    env_token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
    token = env_token or auth.get_access_token()

    def send(access):
        req = urllib.request.Request(base.rstrip('/') + path, data=data, method=method,
                                     headers={'Authorization': 'Bearer ' + access, 'Content-Type': 'application/json'})
        return api.safe_http_json(req)

    try:
        return send(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not env_token:
            auth.refresh_token()
            try:
                return send(auth.get_access_token())
            except urllib.error.HTTPError as retry:
                raise ValueError('Session HTTP request failed with status %d' % retry.code) from None
        raise ValueError('Session HTTP request failed with status %d' % exc.code) from None


def read_file(path, limit=MAX_BODY):
    if path == '-':
        raw = sys.stdin.buffer.read(limit + 1)
    else:
        with Path(path).open('rb') as stream:
            raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Input exceeds capacity')
    return raw.decode('utf-8')


def add_commands(sub):
    root = sub.add_parser('sessions', help='Hub session orchestration: list/start/send/capture/stop with confirmed delivery')
    commands = root.add_subparsers(dest='sessions_cmd', required=True)
    listing = commands.add_parser('list', help='List sessions visible to this credential')
    listing.add_argument('--state')
    listing.add_argument('--limit', type=int, default=50)
    start = commands.add_parser('start', help='Record a start intent; placement reuses existing fleet routing')
    start.add_argument('--engine')
    start.add_argument('--machine')
    start.add_argument('--workspace', required=True)
    start.add_argument('--tier', choices=TIERS)
    start.add_argument('--cwd')
    mode = start.add_mutually_exclusive_group()
    mode.add_argument('--brief-file', help='UTF-8 goal file for headless work (max 8 KiB)')
    mode.add_argument('--interactive', action='store_true', help='Start at an empty native prompt')
    start.add_argument('--key')
    send = commands.add_parser('send', help='Enqueue one prompt delivery on a bound session')
    send.add_argument('id')
    send.add_argument('--body-file', required=True, help='UTF-8 prompt file, or - for stdin (max 16 KiB)')
    send.add_argument('--key')
    capture = commands.add_parser('capture', help='Enqueue one bounded sanitized capture on a bound session')
    capture.add_argument('id')
    capture.add_argument('--max-bytes', type=int, default=65536)
    capture.add_argument('--key')
    stop = commands.add_parser('stop', help='Enqueue stop; the hub releases capacity only on confirmed native exit')
    stop.add_argument('id')
    stop.add_argument('--key')
    show = commands.add_parser('show', help='Show one session with its binding state')
    show.add_argument('id')
    history = commands.add_parser('commands', help='List durable commands with recorded/transported/acked states')
    history.add_argument('id')


def execute(args):
    import uuid
    cmd = getattr(args, 'sessions_cmd', None)
    if cmd == 'list':
        params = ''
        if getattr(args, 'limit', None):
            params = '?limit=%d' % int(args.limit)
        return request('GET', '/api/sessions' + params)
    if cmd == 'show':
        return request('GET', '/api/sessions/' + args.id)
    if cmd == 'commands':
        return request('GET', '/api/sessions/' + args.id + '/commands')
    if cmd == 'start':
        payload = {'key': args.key or str(uuid.uuid4()), 'workspace': args.workspace}
        for field in ('engine', 'machine', 'tier', 'cwd'):
            value = getattr(args, field, None)
            if value:
                payload[field] = value
        payload['mode'] = 'interactive' if getattr(args, 'interactive', False) else 'headless'
        if getattr(args, 'brief_file', None):
            if payload['mode'] == 'interactive':
                raise ValueError('Interactive sessions start at an empty native prompt')
            payload['brief'] = read_file(args.brief_file, MAX_BRIEF)
        return request('POST', '/api/sessions', payload)
    if cmd == 'send':
        return request('POST', '/api/sessions/' + args.id + '/send',
                       {'key': args.key or str(uuid.uuid4()), 'body': read_file(args.body_file)})
    if cmd == 'capture':
        return request('POST', '/api/sessions/' + args.id + '/capture',
                       {'key': args.key or str(uuid.uuid4()), 'max_bytes': args.max_bytes})
    if cmd == 'stop':
        return request('POST', '/api/sessions/' + args.id + '/stop', {'key': args.key or str(uuid.uuid4())})
    raise ValueError('Unknown sessions command')
