"""Failed quota probes warn; real exhaustion remains a launch refusal."""
import json
import subprocess
from unittest.mock import patch
from types import SimpleNamespace
import pytest
from agent_hub_host import provisioning as p

@pytest.mark.parametrize('failure', [FileNotFoundError(), subprocess.TimeoutExpired('quse', 30), ValueError('bad json')])
def test_failed_probe_warns(failure):
    with patch.object(p.subprocess, 'run', side_effect=failure):
        allowed, reason = p.check_codex_quota_gate({})
    assert allowed
    assert reason.startswith('quota_probe_warning:')

@pytest.mark.parametrize('limit', [True, False])
def test_probe_error_does_not_erase_positive_exhaustion(limit):
    data = {'codex': {'status': 'error', 'details': {'limit_reached': limit}, 'windows': {}}}
    with patch.object(p.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(data))):
        allowed, reason = p.check_codex_quota_gate({})
    assert allowed is (not limit)
    assert reason == ('codex_reserve_required' if limit else 'quota_probe_warning:quota_unavailable')

@pytest.mark.parametrize('warning', ['quota_unavailable', 'quota_check_failed'])
def test_admission_preserves_warning(monkeypatch, tmp_path, capsys, warning):
    from test_managed_codex_gate_59a6f44a import _admission_setup
    _admission_setup(monkeypatch, tmp_path, (True, 'quota_probe_warning:' + warning))
    p.admission()
    out = json.loads(capsys.readouterr().out)
    assert out['launch_allowed'] is True
    assert out['quota_warning'] == 'quota_probe_warning:' + warning

@pytest.mark.parametrize('status', ['ok', 'error'])
@pytest.mark.parametrize('details', [{}, {'limit_reached': False}])
def test_known_zero_survives_unknown_sibling_or_route_status(status, details):
    data = {'codex': {'status': status, 'details': details, 'windows': {
        '5h': {'present': True, 'percent_remaining': None, 'reset_at': None},
        '7d': {'present': True, 'percent_remaining': 0, 'reset_at': None}}}}
    with patch.object(p.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(data))):
        assert p.check_codex_quota_gate({}) == (False, 'codex_reserve_required')
