"""Hub-managed engine version rollout (intake 8cc9e31a). No secrets, no network."""
import json
import os
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

from agent_hub_host import engine_rollout as er
from agent_hub_host import provisioning as p


@pytest.fixture(autouse=True)
def managed_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'managed'))
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer' / 'config.toml'))
    return tmp_path


def test_desired_versions_match_reviewed_pins():
    from agent_hub_host import bootstrap_pins
    desired = er.desired_versions()
    assert set(desired) == {'codex', 'claude', 'opencode', 'agy', 'zcodex'}
    assert desired['codex'] == bootstrap_pins.ENGINE_PACKAGES['codex'].rsplit('@', 1)[1]
    assert desired['claude'] == bootstrap_pins.ENGINE_PACKAGES['claude'].rsplit('@', 1)[1]
    assert desired['opencode'] == bootstrap_pins.ENGINE_PACKAGES['opencode'].rsplit('@', 1)[1]
    assert desired['zcodex'] == desired['codex']
    assert desired['agy'] == p.antigravity_pinned_version()
    for version in desired.values():
        assert re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version)


def _chmod_private(path: Path):
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames:
            try:
                os.chmod(os.path.join(dirpath, name), 0o700)
            except OSError:
                pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def test_self_update_disabled_all_engines(tmp_path):
    home = tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    (home / '.codex' / 'config.toml').write_text('approval_policy = "never"\n')
    (home / '.zcodex').mkdir(parents=True)
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / 'settings.json').write_text('{}')
    (home / '.config' / 'opencode').mkdir(parents=True)
    (home / '.config' / 'opencode' / 'opencode.json').write_text('{"permission": {}}')
    _chmod_private(home)
    changed = er.ensure_self_update_disabled(home, 'codex')
    assert any(str(home / '.codex' / 'config.toml') == c for c in changed)
    assert 'check_for_update_on_startup' in (home / '.codex' / 'config.toml').read_text()
    changed = er.ensure_self_update_disabled(home, 'zai')
    assert 'check_for_update_on_startup' in (home / '.zcodex' / 'config.toml').read_text()
    er.ensure_self_update_disabled(home, 'claude')
    assert json.loads((home / '.claude' / 'settings.json').read_text())['autoUpdates'] is False
    er.ensure_self_update_disabled(home, 'go')
    assert json.loads((home / '.config' / 'opencode' / 'opencode.json').read_text())['autoupdate'] is False
    assert er.self_update_disabled_ok(home, 'codex')
    assert er.self_update_disabled_ok(home, 'zai')
    assert er.self_update_disabled_ok(home, 'claude')
    assert er.self_update_disabled_ok(home, 'go')
    assert er.self_update_disabled_ok(home, 'antigravity')
    # Idempotent: second pass changes nothing.
    assert er.ensure_self_update_disabled(home, 'codex') == []
    assert er.ensure_self_update_disabled(home, 'claude') == []


def test_provisioning_reconciles_self_update_disables(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    _chmod_private(home)
    p.install_managed_engine_permissions({'provider': 'codex'}, home)
    assert 'check_for_update_on_startup' in (home / '.codex' / 'config.toml').read_text()
    p.install_managed_engine_permissions({'provider': 'zai'}, home)
    assert 'check_for_update_on_startup' in (home / '.zcodex' / 'config.toml').read_text()
    p.install_managed_engine_permissions({'provider': 'claude'}, home)
    assert json.loads((home / '.claude' / 'settings.json').read_text())['autoUpdates'] is False
    p.install_managed_engine_permissions({'provider': 'go'}, home)
    assert json.loads((home / '.config' / 'opencode' / 'opencode.json').read_text())['autoupdate'] is False


def test_live_engines_and_deferred_labels(monkeypatch):
    rows = [
        {'session_id': 'a', 'engine': 'codex', 'phase': 'running'},
        {'session_id': 'b', 'engine': 'claude', 'phase': 'waiting'},
        {'session_id': 'c', 'engine': 'codex', 'phase': 'exited'},
        {'session_id': 'd', 'engine': 'shell', 'phase': 'running'},
    ]
    assert er.live_engines(rows) == {'codex', 'claude'}
    assert er.live_engines([]) == set()
    monkeypatch.setattr(er, 'live_engines', lambda list_rows=None: {'codex'})
    monkeypatch.setattr(p, 'read_json', lambda path, fallback=None: [{'label': 'codex-main', 'provider': 'codex'}, {'label': 'claude-main', 'provider': 'claude'}])
    assert er.deferred_labels() == {'codex-main'}


def test_sync_keeps_old_revision_while_live(monkeypatch, tmp_path):
    from agent_hub_host import provisioning as prov
    managed = Path(os.environ['AGENT_HUB_MANAGED_DIR'])
    managed.mkdir(parents=True, mode=0o700)
    os.chmod(managed, 0o700)
    old = {'label': 'codex-main', 'provider': 'codex', 'version': '0.163.0',
           'revision': '11111111-1111-4111-8111-111111111111',
           'files': {}, 'env': {}, 'args': []}
    new = dict(old, version='0.166.0', revision='22222222-2222-4222-8222-222222222222')
    old_dir = prov.root() / (old['label'] + '-' + old['revision'])
    (old_dir / 'home').mkdir(parents=True)
    _chmod_private(old_dir)
    prov.write_json(old_dir / 'bundle.json', old)
    monkeypatch.setattr(er, 'live_engines', lambda list_rows=None: {'codex'})
    # Install writes the new bundle dir so the test stays hermetic.
    def _install(bundle, request=None):
        dest = prov.root() / (bundle['label'] + '-' + bundle['revision'])
        (dest / 'home').mkdir(parents=True, exist_ok=True)
        (dest / 'bundle.json').write_text(json.dumps(bundle))
    with mock.patch.object(prov, 'install', side_effect=_install), \
            mock.patch.object(prov, 'profiles'), \
            mock.patch.object(prov, 'report_sync'):
        result = prov.sync(lambda method, path, payload=None: {'engines': [new]} if method == 'GET' else {'status': 'installed'})
    assert result['status'] == 'synced'
    assert old_dir.is_dir(), 'live revision must be kept side by side (AC2)'


def test_decide_canary():
    green = {'engine': 'codex', 'state': 'green'}
    gate = er.decide_canary(green, ['HOST2', 'HOST3'])
    assert gate == {'decision': 'rollout', 'hosts': ['HOST2', 'HOST3'], 'alert': None}
    failed = {'engine': 'codex', 'state': 'failed', 'error': 'smoke_failed'}
    gate = er.decide_canary(failed, ['HOST2'])
    assert gate['decision'] == 'rollback'
    assert gate['hosts'] == []
    assert isinstance(gate['alert'], dict)
    assert 'codex' in gate['alert']['subject']
    assert 'hub engines update --engine codex' in gate['alert']['detail']


def test_smoke_argv_bounded_no_shell():
    for engine in er.ENGINE_ORDER:
        argv = er.smoke_argv(engine, ['/bin/' + engine], er.PROVIDER_FOR[engine])
        assert isinstance(argv, list) and argv
        assert all(isinstance(a, str) for a in argv)
        assert er.SMOKE_PROMPT in argv
        assert '|' not in ''.join(argv) and '&&' not in ''.join(argv)


def test_check_host_shape(monkeypatch):
    monkeypatch.setattr(er, 'installed_version', lambda engine, timeout=30: '0.166.0')
    monkeypatch.setattr(er, 'latest_available', lambda engine, timeout=15: '0.166.0')
    monkeypatch.setattr(p, 'read_json', lambda path, fallback=None: [])
    report = er.check_host()
    assert set(report['engines']) == set(er.ENGINE_ORDER)
    for engine, row in report['engines'].items():
        assert row['installed'] == '0.166.0'
        assert row['desired'] == er.desired_versions()[engine]
        assert row['latest'] == '0.166.0'
        assert 'self_update_disabled' in row
        assert 'last_result' in row


def test_record_and_last_result():
    er.record_result('codex', {'state': 'green', 'version': '0.166.0', 'error': None})
    assert er.last_result('codex')['state'] == 'green'
    assert er.last_result('claude') is None
