"""Provision approved central generations before native admission."""
import hashlib
import json
import re
from pathlib import Path

from . import provider_home

CLAUDE_ENGINE_LABEL = 'hub-claude'
CLAUDE_VERSION = re.compile(r'\d+\.\d+\.\d+\Z')
CLAUDE_REVISION = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')


def _claude_sync_bundle(client):
    response = client.request('GET', '/api/engines/sync')
    engines = response.get('engines')
    matches = [row for row in engines if isinstance(row, dict) and row.get('label') == CLAUDE_ENGINE_LABEL] if isinstance(engines, list) else []
    if len(matches) != 1:
        raise RuntimeError('provider_engine_bundle_unavailable')
    bundle = matches[0]
    version = bundle.get('version')
    if (bundle.get('provider') != 'claude' or not isinstance(version, str)
            or not CLAUDE_VERSION.fullmatch(version) or tuple(map(int, version.split('.'))) < provider_home.CLAUDE_MIN_VERSION
            or not isinstance(bundle.get('revision'), str) or not CLAUDE_REVISION.fullmatch(bundle['revision'])
            or type(bundle.get('credential_generation')) is not int or bundle['credential_generation'] < 1
            or not isinstance(bundle.get('files'), dict) or set(bundle['files']) != {provider_home.PATHS['claude']}):
        raise RuntimeError('provider_engine_bundle_invalid')
    latest = response.get('latest_version', version)
    if not isinstance(latest, str) or not CLAUDE_VERSION.fullmatch(latest):
        raise RuntimeError('provider_engine_bundle_invalid')
    return bundle, latest


def _claude_report_is_green(client, version):
    report = client.request('GET', '/api/engines/versions')
    rows = report.get('reports')
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and row.get('engine') == 'claude'
               and row.get('installed') == version and row.get('result') == 'green' for row in rows)


def _report_claude_version(client, bundle, latest, result):
    reply = client.request('POST', '/api/engines/version-report', {
        'engines': {'claude': {'installed': bundle['version'], 'latest': latest, 'result': result}},
    })
    if not isinstance(reply, dict) or reply.get('status') != 'recorded':
        raise RuntimeError('provider_engine_version_report_unverified')


def _prepare_claude(client, bundle, latest, manifest):
    try:
        binary = provider_home.verify(manifest).get('engine_binary_path')
    except (OSError, ValueError):
        raise RuntimeError('provider_materialization_refused') from None
    if not isinstance(binary, str):
        raise RuntimeError('provider_sdk_native_binary_missing')
    current = _claude_report_is_green(client, bundle['version'])
    smoke_current = provider_home.claude_fable_smoke_is_current(binary)
    if current and smoke_current:
        return
    try:
        provider_home.claude_fable_smoke(manifest)
    except (OSError, ValueError):
        _report_claude_version(client, bundle, latest, 'failed')
        raise RuntimeError('provider_fable_smoke_failed') from None
    _report_claude_version(client, bundle, latest, 'green')

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
    claude_bundle = None
    latest = None
    if provider == 'claude':
        # The managed sync label is bound to AccountDO claude, regardless of
        # which other Claude accounts the registry may contain.
        if account != 'claude':
            raise RuntimeError('provider_claude_account_mismatch')
        claude_bundle, latest = _claude_sync_bundle(client)
        bundle = {'generation': claude_bundle['credential_generation'], 'files': claude_bundle['files']}
        try:
            engine_binary = provider_home.install_managed_claude(claude_bundle['version'])
        except (OSError, ValueError):
            raise RuntimeError('provider_sdk_install_refused') from None
    else:
        bundle = client.request('GET', prefix + '/credentials?min_validity_ms=' + str((launch['timeout_seconds'] + 360) * 1000))
        engine_binary = None
    # SDKs may normalize their own files; never reuse a different attempt's mutable home.
    fence=hashlib.sha256(json.dumps([offer['session_id'],offer['operation_id'],offer['epoch']],separators=(',',':')).encode()).hexdigest()
    root=str(Path(directory).resolve() / 'providers' / fence)
    # Materialization runs in-process from the internalized adapter; the external
    # launcher runtime and its repository path are no longer on the Hub2 host path.
    try:
        manifest = provider_home.materialize(root, account, provider, bundle['generation'],
                                             bundle['files'], launch['model'],
                                             timeout_seconds=launch['timeout_seconds'],
                                             engine_binary=engine_binary)
    except (OSError, ValueError):
        raise RuntimeError('provider_materialization_refused') from None
    if claude_bundle is not None:
        _prepare_claude(client, claude_bundle, latest, manifest)
        acknowledged = client.request('POST', '/api/engines/sync-report', {
            'state': 'synced',
            'items': [{'label': CLAUDE_ENGINE_LABEL, 'revision': claude_bundle['revision'], 'state': 'installed',
                       'credential_generation': bundle['generation']}],
        })
        if not isinstance(acknowledged, dict) or acknowledged.get('status') != 'recorded':
            raise RuntimeError('provider_generation_ack_unverified')
    else:
        acknowledged = client.request('POST', prefix + '/credentials', {'generation': bundle['generation']})
        if acknowledged.get('acknowledged') is not True:
            raise RuntimeError('provider_generation_ack_unverified')
    return manifest
