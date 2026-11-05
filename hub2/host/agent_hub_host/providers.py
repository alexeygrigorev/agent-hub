"""Provision approved central generations before native admission."""
import hashlib
import json
import os
import re
from pathlib import Path

from . import provider_home
from .client import save_config


def provision(client, offer, launch, directory, routes, runner=None):
    account = offer.get('account_id')
    if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', account):
        raise RuntimeError('provider_account_required')
    launch['account_id']=account
    prefix = '/api/accounts/' + account + '/api/account'
    view = client.request('GET', prefix)
    provider = view.get('account', {}).get('provider')
    route = (routes or {}).get(launch['engine'], {})
    if provider not in {'go', 'codex', 'claude', 'zai'} or route.get('provider') != provider:
        raise RuntimeError('provider_route_mismatch')
    bundle = client.request('GET', prefix + '/credentials?min_validity_ms=' + str((launch['timeout_seconds'] + 360) * 1000))
    # SDKs may normalize their own files; never reuse a different attempt's mutable home.
    fence=hashlib.sha256(json.dumps([offer['session_id'],offer['operation_id'],offer['epoch']],separators=(',',':')).encode()).hexdigest()
    root=str(Path(directory).resolve() / 'providers' / fence)
    # Materialization runs in-process from the internalized adapter; the external
    # launcher runtime and its repository path are no longer on the Hub2 host path.
    try:
        manifest = provider_home.materialize(root, account, provider, bundle['generation'],
                                             bundle['files'], launch['model'],
                                             timeout_seconds=launch['timeout_seconds'])
    except (OSError, ValueError):
        raise RuntimeError('provider_materialization_refused') from None
    acknowledged = client.request('POST', prefix + '/credentials', {'generation': bundle['generation']})
    if acknowledged.get('acknowledged') is not True:
        raise RuntimeError('provider_generation_ack_unverified')
    binding = {key: offer[key] for key in ('session_id', 'operation_id', 'epoch', 'account_id')}
    binding['machine_id'] = launch['worker_context']['machine_id']
    controller = Path(directory).resolve() / 'controllers' / (offer['session_id'] + '-' + str(offer['epoch']) + '.json')
    save_config(controller, {'server': client.base_url, 'token': client.token, 'binding': binding})
    return manifest, str(controller)
