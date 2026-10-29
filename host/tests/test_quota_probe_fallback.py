"""Hub owns the quota verdict (intake 5814e109): warnings pass, exhaustion refuses."""
import json
import pytest
from agent_hub_host import provisioning as p


@pytest.mark.parametrize('verdict', [{'launch_allowed': True, 'quota_warning': 'quota_probe_unavailable'}, OSError('down')])
def test_unknown_quota_or_unreachable_hub_warns(verdict):
    def req(*a, **k):
        if isinstance(verdict, Exception):
            raise verdict
        return verdict
    allowed, reason = p.hub_launch_gate('shared-codex', req)
    assert allowed
    assert reason.startswith('quota_probe_warning:')


@pytest.mark.parametrize('reason', ['quota_exhausted', 'credential_invalid', 'credential_rejected'])
def test_hub_refusals_refuse(reason):
    assert p.hub_launch_gate('shared-codex', lambda *a, **k: {'launch_allowed': False, 'reason': reason}) == (False, reason)


def test_non_credential_refusal_does_not_block_execution():
    # e.g. a transient profile repair state: the offer already passed Hub admission.
    assert p.hub_launch_gate('shared-codex', lambda *a, **k: {'launch_allowed': False, 'reason': 'profile_not_installed'})[0] is True


@pytest.mark.parametrize('warning', ['quota_probe_unavailable'])
def test_admission_preserves_warning(monkeypatch, tmp_path, capsys, warning):
    from test_managed_codex_gate_59a6f44a import _admission_setup
    _admission_setup(monkeypatch, tmp_path, (True, 'quota_probe_warning:' + warning))
    p.admission()
    out = json.loads(capsys.readouterr().out)
    assert out['launch_allowed'] is True
    assert out['quota_warning'] == 'quota_probe_warning:' + warning
