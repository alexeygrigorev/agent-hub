"""Real public API/provider-home fixture; never launches or calls a model."""
import argparse
import json
import os
from pathlib import Path

from agent_hub_host.client import Client
from agent_hub_host.provider_home import verify, session_binding
from agent_hub_host.providers import provision


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host-config', required=True)
    parser.add_argument('--offer', required=True)
    parser.add_argument('--provider', required=True)
    args = parser.parse_args()
    config_path = Path(args.host_config).resolve()
    config = json.loads(config_path.read_text())
    offer = json.loads(Path(args.offer).read_text())
    launch = dict(offer['launch'])
    launch['worker_context'] = {'machine_id': config['machine_id']}
    client = Client(config['server'], config['token'])
    manifest = provision(client, offer, launch, config_path.parent,
                         {launch['engine']: {'provider': args.provider}})
    data = verify(manifest)
    binding = session_binding(manifest, client, offer['session_id'], offer['operation_id'], offer['epoch'])
    if data['provider'] != args.provider or binding['model'] != launch['model']:
        raise RuntimeError('public_provider_binding_unverified')
    if os.name != 'nt' and Path(manifest).stat().st_mode & 0o777 != 0o600:
        raise RuntimeError('private_provider_context_mode_invalid')
    print(json.dumps({'sdk_selected': True, 'provider': data['provider'], 'generation': data['generation'],
                      'provider_context_private': True, 'central_generation_ack': True,
                      'session_binding_verified': True, 'model_call': False, 'native_spawn': False}))


if __name__ == '__main__':
    main()
