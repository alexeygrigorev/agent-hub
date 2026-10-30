"""Intake 9104edb4 review r1: only an explicit model reason becomes the sticky model_unsupported."""
import json
import sys
import io

import pytest

from agent_hub_host import dispatch_diagnostics, fleet, native_admission
from agent_hub_host import provisioning as p


SUB = {'account_label': 'acct1', 'admission_argv': ['admission', 'acct1']}
PAYLOAD = {'account': 'acct1', 'engine': 'claude', 'profile': None, 'mode': 'headless'}


def _dispatcher(gate):
    d = fleet.Dispatcher.__new__(fleet.Dispatcher)
    d._admission_guard = fleet.threading.Lock()
    d._admission_locks = {}
    d._admission_shared = {}
    d._managed_labels = set()
    d.runner = lambda argv, payload=None: dict(gate)
    return d


def _refusal(gate):
    with pytest.raises(ValueError) as exc_info:
        _dispatcher(gate)._dispatch_admission(SUB, PAYLOAD, None, {})
    return str(exc_info.value)


@pytest.mark.parametrize('reason', ['quota_exhausted', None, 'credential_stale'])
def test_native_refusal_without_selection_is_not_model_unsupported(reason, monkeypatch):
    monkeypatch.setattr(native_admission, 'routing_rules', lambda: {})
    verdict = {'account_label': 'acct1', 'launch_allowed': False}
    if reason:
        verdict['reason'] = reason
    gate = native_admission.probe(dict(PAYLOAD, selection=None), 'claude', 'acct1', 'claude',
                                  request=lambda *a: verdict)
    assert gate['model_supported'] is False
    message = _refusal(gate)
    assert not message.startswith('model_unsupported')
    assert message == 'Fresh admission refused'
    assert dispatch_diagnostics.classify(ValueError(message))['reason'] != 'model_unsupported'


@pytest.mark.parametrize('gate', [
    {'launch_allowed': False, 'profile_supported': False, 'account_label': 'acct1', 'model_supported': False},
    {'launch_allowed': False, 'profile_supported': False, 'account_label': 'acct1', 'model_supported': False,
     'reason': 'model_disabled_by_policy'},
])
def test_model_supported_false_alone_stays_generic(gate):
    assert _refusal(gate) == 'Fresh admission refused'


def test_codex_quota_gate_slug_survives_model_supported_false():
    gate = {'launch_allowed': False, 'profile_supported': False, 'account_label': 'acct1',
            'model_supported': False, 'reason': 'codex_quota_gate_codex_reserve_required'}
    assert _refusal(gate).startswith('codex_quota_gate_codex_reserve_required:')


@pytest.mark.parametrize('reason', ['model_mismatch', 'model_unsupported'])
def test_explicit_model_reason_is_model_unsupported(reason):
    gate = {'launch_allowed': False, 'profile_supported': False, 'account_label': 'acct1',
            'model_supported': False, 'reason': reason}
    assert _refusal(gate).startswith('model_unsupported:')


def test_native_unconfigured_selection_reports_model_unsupported(monkeypatch):
    monkeypatch.setattr(native_admission, 'routing_rules', lambda: {})
    calls = []
    gate = native_admission.probe(
        dict(PAYLOAD, selection={'id': 'nonexistent-model', 'model': 'nonexistent-model', 'effort': 'high'}),
        'claude', 'acct1', 'claude', request=lambda *a: calls.append(a) or {})
    assert gate['reason'] == 'model_unsupported'
    assert gate['launch_allowed'] is False
    assert calls == []
    assert _refusal(gate).startswith('model_unsupported:')


def test_host_identity_missing_dominates_model_mismatch(tmp_path, monkeypatch, capsys):
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(parents=True, mode=0o700)
    bundle_dir = root_dir / 'hub-claude-rev1'
    bundle_dir.mkdir(mode=0o700)
    p.write_json(bundle_dir / 'bundle.json', {'label': 'hub-claude', 'provider': 'claude', 'revision': 'rev1',
                                              'version': '2.1.304', 'args': [], 'credential_generation': 6})
    p.write_json(root_dir / 'state.json', [{'label': 'hub-claude', 'revision': 'rev1'}])
    prof = tmp_path / 'profiles.toml'
    prof.write_text('[profiles.hub-hub-claude]\nengine = "claude"\n', encoding='utf-8')
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    monkeypatch.setattr(p, 'profile_path', lambda: prof)

    def missing(*_a, **_k):
        raise p.HostIdentityMissingError('Host identity missing: host.json has no base_url')

    monkeypatch.setattr(p, 'host_request', missing)
    monkeypatch.setattr(sys, 'argv', ['admission', 'hub-claude'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'selection': {'model': 'nonexistent-model'}})))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    p.admission()
    out = json.loads(capsys.readouterr().out)
    assert out['launch_allowed'] is False
    assert out['model_supported'] is False
    assert out['reason'] == 'host_identity_missing'
