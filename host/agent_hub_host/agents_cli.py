"""Operator administration of stable named messaging identities."""
import json
import os
from pathlib import Path
from urllib.parse import quote

from . import bus, private_fs
from .config import load_config
from .msg_cli import PROFILE_NAME, CREDENTIAL_LIMIT, profile_path


def add_commands(sub):
    agents = sub.add_parser('agents', help='Operator: create, inspect, delegate and forward named agents')
    # Register at the root CLI while routing through msg_cli's existing
    # operator-safe dispatcher, so CLI wiring stays in one command family.
    agents.set_defaults(cmd='msg', msg_cmd='agents')
    commands = agents.add_subparsers(dest='agents_cmd', required=True)
    create = commands.add_parser('create', help='Mint an identity into its own private profile')
    create.add_argument('name')
    create.add_argument('--output', help='Default: ~/.config/agent-hub/NAME.json')
    create.add_argument('--display')
    create.add_argument('--project')
    show = commands.add_parser('show')
    show.add_argument('name')
    delegate = commands.add_parser('delegate')
    delegate.add_argument('name')
    target = delegate.add_mutually_exclusive_group(required=True)
    target.add_argument('--family')
    target.add_argument('--mcp-client')
    target.add_argument('--session')
    delegate.add_argument('--revoke', action='store_true')
    forward = commands.add_parser('forward')
    forward.add_argument('name')
    destination = forward.add_mutually_exclusive_group(required=True)
    destination.add_argument('--to', help='session:<Hub session UUID>')
    destination.add_argument('--clear', action='store_true')


def execute(args):
    if not PROFILE_NAME.fullmatch(args.name):
        raise ValueError('Invalid agent name')
    base = '/api/agents/named/' + quote(args.name, safe='')
    if args.agents_cmd == 'show':
        return bus._do_request('GET', base)
    if args.agents_cmd == 'delegate':
        payload = {key: getattr(args, key) for key in ('family', 'mcp_client', 'session') if getattr(args, key)}
        if args.revoke:
            payload['revoke'] = True
        return bus._do_request('POST', base + '/delegate', payload)
    if args.agents_cmd == 'forward':
        return bus._do_request('POST', base + '/forward', {'to': None if args.clear else args.to})
    out = Path(args.output).expanduser().absolute() if args.output else profile_path(args.name)
    # Reserve an output path before minting. A retry must never rotate credentials
    # or overwrite the human profile (including through a symlink).
    human = profile_path('cli')
    if out == human or out.exists() or out.is_symlink():
        raise ValueError('Output exists or is the human profile; choose a new private file')
    config = load_config()
    with private_fs.parent(str(out), create=True) as anchor:
        fd = private_fs._open(anchor, out, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    payload = {key: getattr(args, key) for key in ('display', 'project') if getattr(args, key) is not None}
    minted = bus._do_request('POST', base + '/create', payload)
    record = {'base_url': config.get('base_url'), 'client_kind': 'agent', 'actor': minted['actor'],
              'access_token': minted['access_token'], 'refresh_token': minted['refresh_token']}
    private_fs.atomic_write(str(out), json.dumps(record).encode(), CREDENTIAL_LIMIT)
    return {'actor': minted['actor'], 'expires_at': minted.get('expires_at'), 'written': str(out)}
