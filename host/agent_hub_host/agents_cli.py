"""Operator administration of Hub2 named agents (sticky delivery identities).

Hub2 surface (intake 408b585e): `create` registers a named agent with an
optional machine pin, `forward` retargets or clears the live sticky session,
`sticky` configures the machine pin and per-family forward grants, and
`claim` lets the sticky session itself (its worker credential) claim the
forward target. Legacy Hub1 credential minting is not part of Hub2.
"""
from urllib.parse import quote

from . import bus


def add_commands(sub):
    agents = sub.add_parser('agents', help='Operator: register, forward, pin and grant Hub2 named agents')
    # Register at the root CLI while routing through msg_cli's existing
    # operator-safe dispatcher, so CLI wiring stays in one command family.
    agents.set_defaults(cmd='msg', msg_cmd='agents')
    commands = agents.add_subparsers(dest='agents_cmd', required=True)
    create = commands.add_parser('create', help='Register a named agent (operator)')
    create.add_argument('name')
    create.add_argument('--display', required=True)
    create.add_argument('--machine', help='Sticky machine pin, e.g. DESKTOP-FP4OP26')
    show = commands.add_parser('show')
    show.add_argument('name')
    forward = commands.add_parser('forward')
    forward.add_argument('name')
    destination = forward.add_mutually_exclusive_group(required=True)
    destination.add_argument('--to', help='session:<Hub session UUID>')
    destination.add_argument('--clear', action='store_true')
    sticky = commands.add_parser('sticky', help='Set or clear the machine pin / forward grants')
    sticky.add_argument('name')
    sticky.add_argument('--machine', help='Pin the agent to one machine id')
    sticky.add_argument('--clear-machine', action='store_true')
    sticky.add_argument('--family', help='Grant (or with --revoke remove) a forward-claim family')
    sticky.add_argument('--revoke', action='store_true')
    claim = commands.add_parser('claim', help='Sticky session worker: claim this agent for the current session')
    claim.add_argument('name')


def _name(value):
    from .msg_cli import PROFILE_NAME
    if not PROFILE_NAME.fullmatch(value):
        raise ValueError('Invalid agent name')
    return value


def _session_id(value):
    prefix = 'session:'
    if value.startswith(prefix):
        value = value[len(prefix):]
    import re
    if not re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', value):
        raise ValueError('--to expects session:<Hub session UUID>')
    return value


def _find_agent(name):
    agent_id = 'agent:' + name
    after = ''
    seen = 0
    while seen <= 2000:
        listing = bus._do_request('GET', '/api/named-agents?limit=50' + (f'&after={quote(after, safe="")}' if after else ''))
        for item in listing.get('items', []):
            if item.get('id') == agent_id:
                return item
        seen += len(listing.get('items', []))
        after = listing.get('next') or ''
        if not after:
            break
    raise ValueError('Named agent not found: %s' % name)


def execute(args):
    name = _name(args.name)
    if args.agents_cmd == 'create':
        body = {'name': name, 'display_name': args.display}
        if args.machine:
            body['machine'] = args.machine
        return bus._do_request('POST', '/api/named-agents', body)
    if args.agents_cmd == 'show':
        return {'agent': _find_agent(name)}
    if args.agents_cmd == 'claim':
        return bus._do_request('POST', '/api/named-agents/' + quote(name, safe='') + '/claim', {})
    if args.agents_cmd == 'sticky':
        body = {}
        if args.machine and args.clear_machine:
            raise ValueError('--machine and --clear-machine are mutually exclusive')
        if args.machine:
            body['machine'] = args.machine
        elif args.clear_machine:
            body['machine'] = None
        if args.family:
            body['family_id'] = args.family
            if args.revoke:
                body['revoke'] = True
        elif args.revoke:
            raise ValueError('--revoke requires --family')
        if not body:
            raise ValueError('sticky requires --machine, --clear-machine or --family')
        return bus._do_request('POST', '/api/named-agents/' + quote(name, safe='') + '/sticky', body)
    if args.agents_cmd == 'forward':
        current = _find_agent(name)
        body = {'name': name, 'display_name': current.get('display_name') or name,
                'enabled': bool(current.get('enabled', True)),
                'forward_session': None if args.clear else _session_id(args.to)}
        return bus._do_request('POST', '/api/named-agents', body)
    raise ValueError('Unknown agents command')
