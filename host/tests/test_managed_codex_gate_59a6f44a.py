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
    d2 = dd.classify(ValueError('codex_quota_gate_codex_reserve_15_percent_required: x'))
    assert d2['reason'] == 'codex_quota_gate_codex_reserve_15_percent_required'
