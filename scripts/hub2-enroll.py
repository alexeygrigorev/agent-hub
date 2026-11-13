#!/usr/bin/env python3
"""Enroll a release-installed host using Hub-delivered provider credentials.

Run with the verified release environment's Python on Linux or Windows.
Pairing codes and machine tokens are never printed or passed as arguments.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys


def enroll(client, save_config, config_path, roots, engines, code, *, local_profiles=False):
    if config_path.exists():
        raise ValueError('configuration exists; refusing to replace enrollment')
    roots = [str(Path(root).resolve(strict=True)) for root in roots]
    if not roots or not engines or not all(Path(root).is_dir() for root in roots):
        raise ValueError('workspace roots and supported engines are required')
    result = client.request('POST', '/api/enrollments/complete', {'code': code})
    config = {'server': client.base_url, 'machine_id': result['machine_id'], 'token': result['token'],
              'roots': roots, 'engines': engines,
              'launcher': {'private_provider_homes': not local_profiles}}
    save_config(config_path, config)
    return result['machine_id']


def main():
    from hub2_agent_hub_host.client import Client, save_config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='https://hub.dtcdev.click')
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/agent-hub/v2.json')
    parser.add_argument('--root', action='append', required=True)
    parser.add_argument('--engine', action='append', required=True)
    parser.add_argument('--local-profiles', action='store_true', help='Use existing native logins instead of central homes')
    args = parser.parse_args()
    try:
        code = os.environ.get('HUB2_PAIRING_CODE') or getpass.getpass('Pairing code: ')
        machine = enroll(Client(args.server, ''), save_config, args.config, args.root,
                         args.engine, code, local_profiles=args.local_profiles)
        print(json.dumps({'enrolled': machine, 'credential_mode': 'local' if args.local_profiles else 'central'}))
        return 0
    except Exception:
        print('Host enrollment failed; no credentials logged. Check enrollment authority and private configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
