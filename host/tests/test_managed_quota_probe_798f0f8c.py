"""798f0f8c: preserve account isolation with central-auth provider probes."""
import datetime
import json
import os
from pathlib import Path

import pytest
from agent_hub_host import provisioning as p, quota_server, credentials

REV = '42d8580e-3394-443a-b834-c5a944f30614'
SHARED_REV = 'e03c5570-2eb4-40b0-92cf-2119e0dfd6d4'


def _iso(delta_s=0):
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta_s)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _quse_codex(percent):
    return {'codex': {'status': 'ok', 'error': None, 'observed_at': _iso(-5),
                      'details': {'limit_reached': False},
                      'windows': {'7d': {'present': True, 'percent_remaining': percent,
                                         'reset_at': _iso(86400 * 5), 'rolling': False, 'error': None}}}}


def _install(managed, label, revision):
    dest = managed / (label + '-' + revision)
    dest.mkdir(mode=0o700)
    (dest / 'home' / '.codex').mkdir(parents=True, mode=0o700)
    (dest / 'bundle.json').write_text(json.dumps({
        'label': label, 'provider': 'codex', 'version': '0.163.0', 'revision': revision,
        'files': {'.codex/auth.json': '{"token":"synthetic-private-marker"}'}, 'env': {}, 'args': []}))
    (dest / 'bundle.json').chmod(0o600)
    return dest


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path / 'managed'
    root.mkdir(mode=0o700)
    (root / 'state.json').write_text(json.dumps([
        {'label': 'codex-orin', 'revision': REV, 'provider': 'codex'},
        {'label': 'shared-codex', 'revision': SHARED_REV, 'provider': 'codex'}]))
    (root / 'state.json').chmod(0o600)
    _install(root, 'codex-orin', REV)
    _install(root, 'shared-codex', SHARED_REV)
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(root))
    return root


def test_managed_accounts_use_their_own_materialized_home(managed, monkeypatch):
    calls = []
    def probe(provider, home):
        assert provider == 'codex'
        label = home.parent.name.rsplit('-', 5)[0]
        calls.append((label, str(home)))
        return _quse_codex({'codex-orin': 41, 'shared-codex': 77}[label])
    monkeypatch.setattr(credentials, 'probe_quota', probe)
    # Managed subscriptions carry no host quota command: Hub owns admission.
    assert all('quota_argv' not in sub for sub in p.managed_subscriptions())
    rows = {r['account']: r for r in p.quotas()['accounts']}
    assert rows['codex-orin']['quota']['windows']['7d']['percent_remaining'] == 41
    assert rows['shared-codex']['quota']['windows']['7d']['percent_remaining'] == 77
    assert dict(calls)['codex-orin'] == str(managed / ('codex-orin-' + REV) / 'home')
    assert dict(calls)['shared-codex'] == str(managed / ('shared-codex-' + SHARED_REV) / 'home')
    assert 'synthetic-private-marker' not in json.dumps(rows)


def test_unavailable_fallback_does_not_mask_hub_reading(managed, monkeypatch):
    monkeypatch.setattr(credentials, 'probe_quota', lambda *args: {})
    assert p.quotas() == {'accounts': []}


def test_server_collector_probes_the_catalog_install(tmp_path, monkeypatch):
    """The collector's managed probe reads the install its catalog came from."""
    live = tmp_path / 'live-managed'
    live.mkdir()
    (live / 'state.json').write_text(json.dumps([
        {'label': 'codex-orin', 'provider': 'codex', 'revision': REV},
        {'label': 'shared-codex', 'provider': 'codex', 'revision': SHARED_REV}]))
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'orphaned-copy'))
    seen = []

    def runner(argv, **kwargs):
        if isinstance(argv, str):
            # Personal server-local probe: Orin's native home is stale.
            return {'codex': _quse_codex(77)['codex'],
                    'codex-orin': {'status': 'error', 'error': 'unavailable', 'observed_at': None,
                                   'details': {}, 'windows': {}}}
        seen.append(os.environ.get('AGENT_HUB_MANAGED_DIR'))
        return {'accounts': [
            {'account': 'codex-orin', 'provider': 'codex', 'quota': _quse_codex(41)['codex']},
            {'account': 'shared-codex', 'provider': 'codex', 'quota': _quse_codex(77)['codex']}]}

    settings = {'accounts': [{'account': 'shared-codex', 'provider': 'codex'},
                             {'account': 'codex-orin', 'provider': 'codex-orin'}],
                'managed_state_file': str(live / 'state.json')}
    payload = quota_server.collect_accounts(settings, runner=runner)
    assert seen == [str(live.absolute())]
    # Environment restored after the probe.
    assert os.environ['AGENT_HUB_MANAGED_DIR'] == str(tmp_path / 'orphaned-copy')
    rows = {r['account']: r for r in payload['accounts']}
    assert len(payload['accounts']) == 2
    assert rows['codex-orin']['provider'] == 'codex'
    assert rows['codex-orin']['quota']['status'] == 'ok'
    assert rows['codex-orin']['quota']['windows']['7d']['percent_remaining'] == 41
    assert rows['shared-codex']['quota']['windows']['7d']['percent_remaining'] == 77
