"""A transient OpenCode verification failure holds one offer and remains retryable."""
from unittest.mock import MagicMock, patch

from agent_hub_host import opencode_auth as auth
from agent_hub_host.fleet import Dispatcher, _opencode_auth_health


def test_transient_auth_check_holds_offer_without_claim_or_persistent_probe(tmp_path):
    request = MagicMock()
    dispatcher = Dispatcher(dict(version=1, workspaces={'demo': str(tmp_path)},
                                  subscriptions=[]), request, MagicMock())
    results = []
    failure = auth.AuthCheckUnavailable('model_list', 'nonzero_exit', exit_code=17)

    with patch.object(dispatcher, 'dispatch', side_effect=failure):
        dispatcher._dispatch_one({'id': 'transient-auth-offer'}, results)

    assert results == [{'id': 'transient-auth-offer', 'state': 'held',
                        'reason': 'engine_auth_check_unavailable'}]
    request.assert_not_called()


def test_auth_check_diagnostic_contains_only_safe_native_metadata():
    failure = auth.AuthCheckUnavailable('auth_list', 'nonzero_exit', exit_code=23)

    assert failure.diagnostic() == {'step': 'auth_list', 'failure': 'nonzero_exit',
                                    'exit_code': 23}
    assert 'secret' not in str(failure).lower()


def test_auth_health_does_not_persist_transient_check_failures():
    settings = {'opencode_auth': {'binary': '/opencode'}}

    with patch.object(auth, 'prepare', side_effect=auth.AuthCheckUnavailable(
            'model_list', 'nonzero_exit', exit_code=17)):
        assert _opencode_auth_health(settings) is None

    with patch.object(auth, 'prepare', side_effect=auth.AuthBusy('busy')):
        assert _opencode_auth_health(settings) is None


def test_auth_health_persists_only_confirmed_source_expiry():
    settings = {'opencode_auth': {'binary': '/opencode'}}

    with patch.object(auth, 'prepare', side_effect=auth.AuthExpired(
            'missing_device_login', step='auth_export')):
        assert _opencode_auth_health(settings) == {
            'engine': 'opencode', 'status': 'unavailable', 'reason': 'engine_auth_expired'}

    with patch.object(auth, 'prepare', side_effect=ValueError('unexpected')):
        try:
            _opencode_auth_health(settings)
        except ValueError as exc:
            assert str(exc) == 'unexpected'
        else:
            raise AssertionError('generic ValueError must not imply expired login')
