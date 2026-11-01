"""Cancel is an operator request, never an installation or forged host report."""
import argparse
from unittest.mock import Mock
import pytest
from agent_hub_host import provisioning

ROLLOUT = '0de287c8-245c-4b18-882b-00450a91f5c5'


def args(*extra):
    parser = argparse.ArgumentParser()
    provisioning.add_commands(parser.add_subparsers(dest='command', required=True))
    return parser.parse_args(['engines', 'cancel', ROLLOUT, *extra])


def test_cancel_uses_operator_transport_only(monkeypatch):
    operator = Mock(return_value={'state': 'rolled_back'})
    monkeypatch.setattr(provisioning.bus, '_do_request', operator)
    monkeypatch.setattr(provisioning, 'host_request', Mock(side_effect=AssertionError('no host evidence')))
    monkeypatch.setattr(provisioning, 'sync', Mock(side_effect=AssertionError('no installation')))
    assert provisioning.command(args('--reason', ' incident repair ')) == {'state': 'rolled_back'}
    operator.assert_called_once_with('POST', '/api/engines/rollouts/' + ROLLOUT + '/cancel', {'reason': 'incident repair'})


@pytest.mark.parametrize('reason', ['', 'bad\nreason', 'x' * 241])
def test_cancel_rejects_unsafe_audit_reason_before_request(monkeypatch, reason):
    operator = Mock(side_effect=AssertionError('no request'))
    monkeypatch.setattr(provisioning.bus, '_do_request', operator)
    with pytest.raises(ValueError):
        provisioning.command(args('--reason', reason))
    operator.assert_not_called()


def test_cancel_rejects_path_injection(monkeypatch):
    operator = Mock(side_effect=AssertionError('no request'))
    monkeypatch.setattr(provisioning.bus, '_do_request', operator)
    values = args()
    values.rollout_id = '../update'
    with pytest.raises(ValueError):
        provisioning.command(values)
    operator.assert_not_called()
