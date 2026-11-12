"""Windows config-path resolution and partial-bundle admission (intake 47e5e706)."""
import io
import json
import sys
from pathlib import Path

from agent_hub_host import provisioning as p


def _clear(monkeypatch):
    monkeypatch.delenv('APLEXER_CONFIG', raising=False)


def test_windows_uses_appdata_over_xdg(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    assert p.profile_path() == tmp_path / 'Roaming' / 'aplexer' / 'config.toml'


def test_linux_uses_xdg_and_override_wins(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    assert p.profile_path() == tmp_path / 'xdg' / 'aplexer' / 'config.toml'
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'x.toml'))
    assert p.profile_path() == tmp_path / 'x.toml'


def _admit(monkeypatch, tmp_path, capsys, bundles):
    cfg = tmp_path / 'config.toml'
    cfg.write_text('version = 1\n')
    monkeypatch.setenv('APLEXER_CONFIG', str(cfg))
    monkeypatch.setattr(sys, 'argv', ['x', 'claude'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    managed = tmp_path / 'managed'
    managed.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: managed)
    state = []
    for b in bundles:
        # Installed bundle.json carries the same revision as its state record;
        # admission's login probe resolves the profile directory from it.
        b = {**b, 'revision': '1'}
        state.append({'label': b['label'], 'revision': '1'})
        d = managed / (b['label'] + '-1')
        d.mkdir(mode=0o700)
        if b.get('provider') == 'claude':
            # profile_logged_in reads only the materialized home credential for
            # Claude; a synthetic, non-secret file satisfies that contract.
            cred = d / 'home' / '.claude' / '.credentials.json'
            cred.parent.mkdir(parents=True, mode=0o700)
            cred.write_text('{"synthetic": true}', encoding='utf-8')
        p.private_fs.atomic_write(d / 'bundle.json', json.dumps(b).encode(), 1048576)
    p.private_fs.atomic_write(managed / 'state.json', json.dumps(state).encode(), 1048576)
    monkeypatch.setattr(p, 'host_request', lambda *a, **k: {'launch_allowed': True, 'profile_supported': True})
    p.admission()
    return json.loads(capsys.readouterr().out), cfg


def test_partial_bundle_does_not_block_other_profiles(monkeypatch, tmp_path, capsys):
    good = {'label': 'claude', 'provider': 'claude', 'args': [], 'env': {}}
    bad = {'label': 'weird', 'provider': 'no-such-provider'}
    result, cfg = _admit(monkeypatch, tmp_path, capsys, [good, bad])
    assert result['launch_allowed'] is True
    assert '[profiles.hub-claude]' in cfg.read_text()


def test_profiles_failure_surfaces_exception_type_only(monkeypatch, tmp_path, capsys):
    def boom(_):
        raise PermissionError('secret-path-detail')
    monkeypatch.setattr(p, 'profiles', boom)
    good = {'label': 'claude', 'provider': 'claude', 'args': [], 'env': {}}
    result, _ = _admit(monkeypatch, tmp_path, capsys, [good])
    assert result['reason'] == 'local_profile_missing'
    assert result['profile_error'] == 'PermissionError'
    assert 'secret' not in json.dumps(result)
