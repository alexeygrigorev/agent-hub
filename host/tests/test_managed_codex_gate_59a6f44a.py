"""Intake 59a6f44a AC1/AC3/AC4: managed Codex single root + host-side quota gate.

- Canonical root is provisioning.root() (default ~/.config/agent-hub/managed-engines).
- provisioning.admission() refuses codex launches when check_codex_quota_gate fails,
  with reason codex_quota_gate_<slug>.
- fleet dispatch propagates the slug so rejected[]/launch_error carry it.
- dispatch_diagnostics.classify preserves codex_quota_gate_* slugs.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import provisioning as p
from agent_hub_host import dispatch_diagnostics as dd


def test_canonical_root_default(monkeypatch, tmp_path):
    monkeypatch.delenv('AGENT_HUB_MANAGED_DIR', raising=False)
    monkeypatch.delenv('HUB_MANAGED_DIR', raising=False)
    # root() defaults to host config parent / managed-engines; just assert it
    # ends with managed-engines and is absolute.
    r = p.root()
    assert r.name == 'managed-engines'
    assert r.is_absolute()


def test_module_command_embeds_canonical_root(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'managed-engines'))
    cmd = p.module_command('execute', 'shared-codex')
    joined = ' '.join(cmd)
    assert str(tmp_path / 'managed-engines') in joined


def _admission_setup(monkeypatch, tmp_path, gate_result):
    label = 'shared-codex'
    rev = '395bd8f1-c046-4c42-8776-5c74be9a051b'
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path))
    # Use the real provisioning writers so private_fs permissions pass.
    p.write_json(tmp_path / 'state.json', [{'label': label, 'revision': rev, 'provider': 'codex'}])
    dest = tmp_path / (label + '-' + rev)
    bundle = {'label': label, 'provider': 'codex', 'version': '0.163.0',
              'revision': rev, 'files': {}, 'env': {}, 'args': []}
    p.write_json(dest / 'bundle.json', bundle)
    monkeypatch.setattr(p.sys, 'argv', ['provisioning', label])
    monkeypatch.setattr(p, 'host_request', lambda m, path, payload=None: {
        'launch_allowed': True, 'profile_supported': True,
        'account_label': label, 'observed_at': '2026-10-26T00:00:00Z'})
    monkeypatch.setattr(p, 'check_codex_quota_gate', lambda env: gate_result)
    monkeypatch.setattr(p, 'engine_environment', lambda bundle, home, dest=None: {'HOME': str(home)})
    return label


def test_admission_refuses_on_quota_gate(monkeypatch, tmp_path, capsys):
    _admission_setup(monkeypatch, tmp_path, (False, 'quota_unavailable'))
    p.admission()
    out = json.loads(capsys.readouterr().out)
    assert out['launch_allowed'] is False
    assert out['reason'] == 'codex_quota_gate_quota_unavailable'


def test_admission_allows_when_gate_ok(monkeypatch, tmp_path, capsys):
    _admission_setup(monkeypatch, tmp_path, (True, 'ok'))
    p.admission()
    out = json.loads(capsys.readouterr().out)
    assert out['launch_allowed'] is True
    assert 'codex_quota_gate' not in str(out.get('reason', ''))


def test_classify_preserves_gate_slug():
    d = dd.classify(ValueError('codex_quota_gate_quota_unavailable: managed Codex quota gate refused launch before execution'))
    assert d['reason'] == 'codex_quota_gate_quota_unavailable'
    d2 = dd.classify(ValueError('codex_quota_gate_codex_reserve_required: x'))
    assert d2['reason'] == 'codex_quota_gate_codex_reserve_required'


def _home_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'managed'))
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer/config.toml'))
    monkeypatch.setattr(p, 'load_git_identity', lambda: {})
    b = {'label': 'shared-codex', 'provider': 'codex', 'version': '0.163.0',
         'revision': '11111111-1111-4111-8111-111111111111',
         'files': {'.codex/auth.json': '{"token":"initial-test-token"}',
                   '.codex/config.toml': 'model = "gpt-6.1-sol"\n'}, 'env': {}, 'args': []}
    home = p.root() / (b['label'] + '-' + b['revision']) / 'home'
    p.provision_managed_home(b, home)
    return b, home


def test_reconcile_preserves_rotated_token_and_tightens_cli_rewrite(monkeypatch, tmp_path):
    import os
    b, home = _home_bundle(monkeypatch, tmp_path)
    auth = home / '.codex/auth.json'
    config = home / '.codex/config.toml'
    auth.write_text('{"token":"rotated-test-token"}')
    auth.chmod(0o644)
    config.write_text('model = "gpt-6.1-sol"\n# retained user setting\n')
    config.chmod(0o644)
    for _ in range(2):
        p.provision_managed_home(b, home)
    assert json.loads(auth.read_text())['token'] == 'rotated-test-token'
    assert 'retained user setting' in config.read_text()
    assert 'approval_policy = "never"' in config.read_text()
    assert 'sandbox_mode = "danger-full-access"' in config.read_text()
    assert (home / '.codex/hooks.json').is_file()
    assert auth.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o600


def test_reconcile_repairs_empty_or_missing_and_new_revision(monkeypatch, tmp_path):
    b, home = _home_bundle(monkeypatch, tmp_path)
    auth = home / '.codex/auth.json'
    auth.write_text('')
    p.provision_managed_home(b, home)
    assert auth.read_text() == b['files']['.codex/auth.json']
    auth.unlink()
    p.provision_managed_home(b, home)
    assert auth.read_text() == b['files']['.codex/auth.json']
    rotated = {**b, 'revision': '22222222-2222-4222-8222-222222222222',
               'files': {**b['files'], '.codex/auth.json': '{"token":"central-new-token"}'}}
    new_home = p.provision_managed_home(rotated)
    assert json.loads((new_home / '.codex/auth.json').read_text())['token'] == 'central-new-token'


def test_reconcile_rejects_symlink_hardlink_directory_fifo_and_parent_symlink(monkeypatch, tmp_path):
    import os
    import pytest
    b, home = _home_bundle(monkeypatch, tmp_path)
    target = home / '.codex/auth.json'
    outside = tmp_path / 'outside'
    outside.write_text('outside-unchanged')
    outside.chmod(0o600)
    for make in [lambda: target.symlink_to(outside), lambda: os.link(outside, target),
                 lambda: target.mkdir(), lambda: os.mkfifo(target)]:
        target.unlink()
        make()
        with pytest.raises((PermissionError, OSError)):
            p.provision_managed_home(b, home)
        assert outside.read_text() == 'outside-unchanged'
        if target.is_dir():
            target.rmdir()
            target.write_text('test')
        elif not target.is_symlink() and not target.is_file():
            target.unlink()
            target.write_text('test')
    target.unlink()
    directory = home / '.codex'
    directory.rename(home / '.saved-codex')
    directory.symlink_to(home / '.saved-codex', target_is_directory=True)
    with pytest.raises((PermissionError, OSError)):
        p.provision_managed_home(b, home)


def test_host_publishes_account_refusal_even_when_sibling_admits(monkeypatch, tmp_path):
    from agent_hub_host import fleet, system, api, workclaim
    monkeypatch.setattr(p, 'tick', lambda: None)
    monkeypatch.setattr(p, 'managed_subscriptions', lambda: [])
    monkeypatch.setattr(system, 'get_system_info', lambda: {'capabilities': {}, 'hostname': 'test', 'os': 'test', 'network': 'home', 'roles': []})
    monkeypatch.setattr(api, 'get_heartbeat_payload', lambda: {'type': 'heartbeat'})
    monkeypatch.setattr(workclaim, 'sync_once', lambda *a, **kw: {'ok': True})
    cfg = {'version': 1, 'workspaces': {'demo': str(tmp_path)}, 'subscriptions': [
        {'service': 'codex', 'account_label': label, 'plan': 'pro', 'engine': 'codex',
         'profile': 'hub-' + label, 'admission_argv': ['gate', label]}
        for label in ['shared-codex', 'codex-orin']]}
    for broken in (False, True):
        calls = []
        def request(method, path, body=None):
            calls.append((path, body))
            return {'offers': []}
        def runner(argv, **kw):
            if argv[0] != 'gate':
                return []
            label = argv[1]
            if label == 'shared-codex' and broken:
                raise TimeoutError()
            return {'launch_allowed': label == 'codex-orin', 'profile_supported': True,
                    'account_label': label, 'observed_at': fleet.utcnow(),
                    'reason': 'codex_quota_gate_quota_unavailable'}
        fleet.Dispatcher(cfg, request, runner).once()
        caps = next(body['capabilities'] for path, body in calls if path == '/api/machines/register')
        refusals = caps['fleet_route_rejections']
        assert len(refusals) == 1
        assert refusals[0]['account'] == 'shared-codex'
        assert refusals[0]['profile'] == 'hub-shared-codex'
        assert refusals[0]['mode'] == 'interactive'
        assert refusals[0]['reason'] == ('codex_quota_gate_quota_check_failed' if broken else 'codex_quota_gate_quota_unavailable')
        hb = next(body for path, body in calls if path == '/api/host/poll')
        assert hb['gate']['launch_allowed'] is True
