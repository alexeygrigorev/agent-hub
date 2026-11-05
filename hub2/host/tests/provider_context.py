"""Real public API/provider-home fixture; never launches or calls a model."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_hub_host.client import Client
from agent_hub_host.native_launch import NativeLauncher
from agent_hub_host.providers import provision


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host-config', required=True)
    parser.add_argument('--offer', required=True)
    parser.add_argument('--launcher-root', required=True)
    parser.add_argument('--provider', required=True)
    args = parser.parse_args()
    config_path = Path(args.host_config).resolve()
    config = json.loads(config_path.read_text())
    offer = json.loads(Path(args.offer).read_text())
    launch = dict(offer['launch'])
    launch['worker_context'] = {'machine_id': config['machine_id']}
    client = Client(config['server'], config['token'])
    launcher = NativeLauncher({'routes': {launch['engine']: {'provider': args.provider}}})
    manifest, controller = provision(client, offer, launch, config_path.parent, launcher)
    raise SystemExit('provider_context: external launcher admission probe retired with the launcher adapter')
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(args.launcher_root).resolve())
    result = subprocess.run([sys.executable, '-c', script, manifest, controller], env=env,
                            capture_output=True, text=True, timeout=20, check=False)
    if result.returncode or len(result.stdout) > 4096:
        raise RuntimeError('public_provider_admission_unverified')
    proof = json.loads(result.stdout)
    if os.name != 'nt' and any(Path(path).stat().st_mode & 0o777 != 0o600 for path in [manifest, controller]):
        raise RuntimeError('private_provider_context_mode_invalid')
    print(json.dumps({**proof, 'provider_context_private': True, 'central_generation_ack': True,
                      'model_call': False, 'native_spawn': False}))


if __name__ == '__main__':
    main()
