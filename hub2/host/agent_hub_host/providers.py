"""Provision approved central generations before native admission."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from .client import save_config


def provision(client, offer, launch, directory, launcher, runner=subprocess.run):
    account = offer.get('account_id')
    if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', account):
        raise RuntimeError('provider_account_required')
    launch['account_id']=account
    prefix = '/api/accounts/' + account + '/api/account'
    view = client.request('GET', prefix)
    provider = view.get('account', {}).get('provider')
    route = launcher.config.get('routes', {}).get(launch['engine'], {})
    if provider not in {'go', 'codex', 'claude', 'zai'} or route.get('provider') != provider:
        raise RuntimeError('provider_route_mismatch')
    bundle = client.request('GET', prefix + '/credentials?min_validity_ms=' + str((launch['timeout_seconds'] + 360) * 1000))
    repository = launcher.config['repository']
    env = os.environ.copy()
    env['PYTHONPATH'] = repository
    request = dict(root=str(Path(directory).resolve() / 'providers'), account=account,
                   provider=provider, generation=bundle['generation'], files=bundle['files'], model=launch['model'], timeout_seconds=launch['timeout_seconds'])
    result = runner([sys.executable, '-m', 'launcher.provider_home'], input=json.dumps(request),
                    text=True, capture_output=True, timeout=20, check=False, env=env)
    if result.returncode or len(result.stdout) > 4096:
        raise RuntimeError('provider_materialization_refused')
    manifest = json.loads(result.stdout)['manifest']
    acknowledged = client.request('POST', prefix + '/credentials', {'generation': bundle['generation']})
    if acknowledged.get('acknowledged') is not True:
        raise RuntimeError('provider_generation_ack_unverified')
    binding = {key: offer[key] for key in ('session_id', 'operation_id', 'epoch', 'account_id')}
    binding['machine_id'] = launch['worker_context']['machine_id']
    controller = Path(directory).resolve() / 'controllers' / (offer['session_id'] + '-' + str(offer['epoch']) + '.json')
    save_config(controller, {'server': client.base_url, 'token': client.token, 'binding': binding})
    return manifest, str(controller)
