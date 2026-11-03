"""Antigravity as a Hub-registered engine type every host installs (intake 5814e109 AC8).

The CLI is a native binary: hosts install the sha512-pinned release from
bootstrap_pins into the bundle, never npm or a mutable latest. The only
credential is the Hub-held OAuth token file; quota belongs to the Gemini family.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_hub_host import provisioning as p
from agent_hub_host.bootstrap_pins import ANTIGRAVITY_RELEASES

TOKEN = '.gemini/antigravity-cli/antigravity-oauth-token'
PINNED = next(iter(ANTIGRAVITY_RELEASES.values()))['version']


def agy_bundle(**over):
    b = {'label': 'shared-agy', 'revision': '33333333-3333-4333-8333-333333333333', 'provider': 'antigravity',
         'version': PINNED, 'files': {TOKEN: 'synthetic-agy-token'}, 'env': {}, 'args': []}
    b.update(over)
    return b


def test_validate_accepts_only_the_hub_held_token():
    p.validate(agy_bundle())
    for bad in (agy_bundle(files={}), agy_bundle(env={'GEMINI_API_KEY': 'x'}),
                agy_bundle(files={TOKEN: 'x', '.gemini/settings.json': '{}'}), agy_bundle(auth_method='machine-login')):
        with pytest.raises(ValueError):
            p.validate(bad)
    with pytest.raises(ValueError):
        p.validate(agy_bundle(auth_method=None))


def test_install_uses_the_pinned_release_into_the_bundle(tmp_path):
    calls = []

    def ensure(root):
        calls.append(root)
        binary = root / 'bin' / 'agy'
        binary.write_text('#!/bin/sh\n')
        return binary
    with patch('agent_hub_host.bootstrap.ensure_antigravity', ensure):
        p.install_antigravity(agy_bundle(), tmp_path)
    assert calls == [tmp_path / 'engine']
    assert (tmp_path / 'engine/tmp').is_dir()
    assert p.executable_path(agy_bundle(), tmp_path, is_windows=False) == tmp_path / 'engine/bin/agy'
    assert p.executable_path(agy_bundle(), tmp_path, is_windows=True) == tmp_path / 'engine/bin/agy.exe'


def test_pin_bump_never_strands_older_bundles_and_installer_errors_stay_hidden(tmp_path):
    seen = []
    with patch('agent_hub_host.bootstrap.ensure_antigravity', lambda root: seen.append(root)):
        p.install_antigravity(agy_bundle(version='1.4.9'), tmp_path)
    assert seen == [tmp_path / 'engine']

    def broken(root):
        raise RuntimeError('/private/path token=abc')
    with patch('agent_hub_host.bootstrap.ensure_antigravity', broken):
        with pytest.raises(ValueError, match='^Managed engine installation failed$'):
            p.install_antigravity(agy_bundle(), tmp_path)


def test_subscription_is_antigravity_engine_on_the_gemini_quota_pool(monkeypatch):
    record = {'label': 'shared-agy', 'provider': 'antigravity', 'revision': 'rev'}
    monkeypatch.setattr(p, 'read_json', lambda path, fallback=None: [record] if path.name == 'state.json' else agy_bundle())
    sub = p.managed_subscriptions()[0]
    assert (sub['service'], sub['engine'], sub['profile'], sub['launcher_provider']) == \
        ('gemini', 'antigravity', 'hub-shared-agy', 'antigravity')


def test_cli_import_sends_only_the_token_file_and_never_echoes_it(tmp_path, capsys):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    token = private / 'antigravity-oauth-token'
    token.write_text('private-agy-marker')
    token.chmod(0o600)
    args = SimpleNamespace(engines_cmd='add', file=None, provider='antigravity', label='shared-agy',
                           auth_file=str(token), config_file=None, version=None)
    with patch.object(p.bus, '_do_request', return_value={'status': 'configured'}) as request:
        assert p.command(args) == {'status': 'configured'}
    method, path, payload = request.call_args.args
    assert (method, path) == ('PUT', '/api/engines/shared-agy')
    assert payload['files'] == {TOKEN: 'private-agy-marker'}
    assert payload['version'] == PINNED and payload['env'] == {} and 'auth_method' not in payload
    assert 'private-agy-marker' not in capsys.readouterr().out
    with pytest.raises(ValueError, match='only its OAuth token'):
        p.command(SimpleNamespace(**{**vars(args), 'config_file': str(token)}))


def test_requested_models_resolve_through_the_gemini_policy_family():
    from agent_hub_host.model_contract import policy_models
    policy = policy_models()
    default = p.launcher_metadata('antigravity', {'args': []})['launcher_model']
    assert default == 'gemini-4-argon-medium'
    assert p.managed_model_allowed('antigravity', default, policy)
    assert p.managed_model_allowed('antigravity', 'gemini-4-argon-medium', policy)
    assert not p.managed_model_allowed('antigravity', 'gemini-4-argon-high', policy)
    assert not p.managed_model_allowed('antigravity', 'opus', policy)
    assert not p.managed_model_allowed('antigravity', 'gemini-0-unknown', policy)
