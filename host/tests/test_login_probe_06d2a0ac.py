"""Intake 06d2a0ac: a managed Codex profile without a credential is refused
as not_logged_in (it would sit at the "Sign in with ChatGPT" screen), the
refusal is advertised as a route rejection, and dispatch reports the slug so
Hub re-places the session."""
import io
import json
import os
import sys

import pytest

from agent_hub_host import provisioning as p
from agent_hub_host.dispatch_diagnostics import classify


@pytest.fixture(autouse=True)
def isolated_aplexer(tmp_path, monkeypatch):
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer' / 'config.toml'))


REV = '11111111-1111-4111-8111-111111111111'


def run_admission(tmp_path, monkeypatch, bundle, home_auth=False):
    os.chmod(tmp_path, 0o700)
    m_dir = tmp_path / 'managed-engines'
    m_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setattr(p, 'root', lambda: m_dir)
    monkeypatch.setattr(p, 'host_request', lambda m, path, pl: {'launch_allowed': True, 'profile_supported': True, 'account_label': bundle['label']})
    dest = m_dir / f"{bundle['label']}-{bundle['revision']}"
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.write_json(dest / 'bundle.json', bundle)
    p.write_json(m_dir / 'state.json', [{'label': bundle['label'], 'revision': bundle['revision'], 'provider': bundle['provider']}])
    if home_auth:
        (dest / 'home' / '.codex').mkdir(parents=True)
        (dest / 'home' / '.codex' / 'auth.json').write_text('{"synthetic": true}')
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'profile': 'hub-' + bundle['label'], 'account': bundle['label']})))
    monkeypatch.setattr(sys, 'argv', ['admission', bundle['label']])
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    p.admission()
    return json.loads(out.getvalue())


def test_codex_profile_without_credential_is_not_logged_in(tmp_path, monkeypatch):
    dummy = dict(label='dummy-sync-5814', provider='codex', version='0.163.0', revision=REV, files={}, env={}, args=[])
    res = run_admission(tmp_path, monkeypatch, dummy)
    assert res['launch_allowed'] is False
    assert res['reason'] == 'not_logged_in'


def test_codex_profile_with_bundle_or_home_credential_is_admitted(tmp_path, monkeypatch):
    real = dict(label='shared-codex', provider='codex', version='0.163.0', revision=REV,
                files={'.codex/auth.json': '{"synthetic": true}'}, env={}, args=[])
    assert run_admission(tmp_path, monkeypatch, real)['launch_allowed'] is True
    home_only = dict(label='codex-orin', provider='codex', version='0.163.0', revision=REV, files={}, env={}, args=[])
    assert run_admission(tmp_path, monkeypatch, home_only, home_auth=True)['launch_allowed'] is True


def test_providers_without_a_file_probe_are_not_judged():
    assert p.profile_logged_in({'provider': 'claude', 'files': {}}, '/nonexistent') is True
    assert p.profile_logged_in({'provider': 'codex', 'files': {}}, '/nonexistent') is False


def test_dispatch_classifies_not_logged_in():
    d = classify(ValueError('not_logged_in: the engine profile has no credential on this host'))
    assert d['reason'] == 'not_logged_in'
    assert d['stage'] == 'dispatch_route'


def test_admission_projection_advertises_not_logged_in(monkeypatch):
    from agent_hub_host import fleet
    svc = fleet.Dispatcher.__new__(fleet.Dispatcher)
    svc.settings = {'subscriptions': [{'service': 'codex', 'engine': 'codex', 'account_label': 'dummy-sync-5814',
                                       'profile': 'hub-dummy-sync-5814', 'admission_argv': ['x']}], 'workspaces': {}}
    svc.runner = lambda argv, payload=None: {'launch_allowed': False, 'profile_supported': True, 'reason': 'not_logged_in',
                                            'account_label': 'dummy-sync-5814', 'observed_at': fleet.utcnow()}
    caps = {'fleet_modes': ['interactive', 'headless'], 'fleet_route_rejections': []}
    allowed, engines = svc._admission_projection(caps)
    assert allowed is False and engines == set()
    assert {(r['mode'], r['reason'], r['profile']) for r in caps['fleet_route_rejections']} == {
        ('interactive', 'not_logged_in', 'hub-dummy-sync-5814'), ('headless', 'not_logged_in', 'hub-dummy-sync-5814')}
