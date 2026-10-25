import json
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_hub_host import opencode_auth as auth
from agent_hub_host.fleet import Dispatcher


def config(tmp_path):
    return dict(binary='/installed/opencode', source_data=str(tmp_path / 'native'),
                source_config=str(tmp_path / 'config'), target_data=str(tmp_path / 'runtime'))


def test_device_login_sync_tracks_new_login_and_preserves_runtime_refresh(tmp_path):
    cfg = config(tmp_path)
    calls = []
    source = [dict(id='native-account', integrationID='opencode', active=True,
                   value=dict(type='oauth', methodID='device', access='fixture-access', refresh='fixture-refresh'))]

    def native(cfg, args, env, payload=None):
        calls.append((args, payload))
        assert 'OPENCODE_API_KEY' not in env
        if args[:2] == ['auth', 'export']:
            return source
        if args == ['auth', 'import']:
            assert json.loads(payload) == source
            db = Path(cfg['target_data']) / 'opencode' / 'opencode.db'
            db.parent.mkdir(exist_ok=True)
            db.touch()
        if args[:2] == ['auth', 'list']:
            return [dict(id='opencode', connections=[dict(method='oauth')])]
        if args == ['api', 'model.list']:
            return dict(data=[dict(providerID='opencode-go', id=auth.MODEL.split('/')[1], enabled=True)])

    for i in range(2):
        source[0]['value']['access'] = 'rotated-access-' + str(i)
        assert auth.prepare(cfg, native=native)['status'] == 'ok'
    assert sum(a == ['auth', 'export', 'opencode'] for a, _ in calls) == 2
    assert sum(a == ['auth', 'import'] for a, _ in calls) == 1
    source[0]['id'] = 'renewed-native-login'
    auth.prepare(cfg, native=native)
    assert sum(a == ['auth', 'import'] for a, _ in calls) == 2


@pytest.mark.parametrize('failure', ['legacy-key', 'missing', '401', '403', 'contaminated'])
def test_auth_failure_is_sanitized_and_never_inference(tmp_path, failure):
    cfg = config(tmp_path)
    def native(cfg, args, env, payload=None):
        assert args[0] in ('auth', 'api')
        if failure == 'missing':
            return []
        if failure in ('401', '403'):
            raise RuntimeError(f'HTTP {failure} invalid fixture-secret')
        if args[:2] == ['auth', 'export']:
            return [dict(integrationID='opencode', active=True,
                         value=dict(type='api' if failure == 'legacy-key' else 'oauth', methodID='device',
                                    access='fixture-secret', refresh='fixture-refresh'))]
        if args[:2] == ['auth', 'list']:
            return [dict(id='opencode-go', connections=[dict(method='key')])]
    with pytest.raises(ValueError) as exc:
        auth.prepare(cfg, native=native)
    assert str(exc.value).startswith('engine_auth_expired:')
    assert 'fixture-secret' not in str(exc.value)


def test_nested_launch_environment_uses_only_verified_store(tmp_path):
    cfg = config(tmp_path)
    argv = auth.launch_argv(cfg, ['/usr/bin/env', 'XDG_DATA_HOME=/old',
                                  'OPENCODE_API_KEY=fixture-secret', 'OPENCODE_DB=/old.db', 'launcher'])
    assert not any('fixture-secret' in a or '/old' in a for a in argv)
    assert 'XDG_DATA_HOME=' + cfg['target_data'] in argv
    assert 'OPENCODE_DB=' + cfg['target_data'] + '/opencode/opencode.db' in argv
    unit = auth.launch_argv(cfg, ['/usr/bin/systemd-run', '--user', '--', '/usr/bin/env',
                                  'XDG_DATA_HOME=/old', 'launcher'])
    assert unit[unit.index('--') + 1] == '/usr/bin/env'
    assert unit.index('OPENCODE_DB=' + cfg['target_data'] + '/opencode/opencode.db') > unit.index('--')


def test_preflight_refuses_before_gate_claim_or_native_start(tmp_path):
    cfg = dict(version=1, workspaces={'demo': str(tmp_path)}, opencode_auth=config(tmp_path),
               subscriptions=[dict(service='go', account_label='muse', engine='opencode', profile=None,
                                   launcher_provider='opencode', admission_argv=['gate'])])
    calls = []
    dispatcher = Dispatcher(cfg, request=lambda *a, **k: calls.append(a), runner=lambda *a, **k: calls.append(a))
    with patch.object(auth, 'prepare', side_effect=ValueError('engine_auth_expired: device login required')):
        with pytest.raises(ValueError, match='engine_auth_expired'):
            dispatcher.dispatch(dict(id='owned-offer', account='muse', engine='opencode', profile=None,
                                     mode='headless', workspace='demo', prompt='probe'))
    assert calls == []


def test_lock_contention_is_retryable_without_auth_hold(tmp_path):
    with patch.object(auth.private_fs, 'exclusive_lock', side_effect=BlockingIOError), \
            patch.object(auth.time, 'monotonic', side_effect=[0, 31]):
        with pytest.raises(auth.AuthBusy, match='engine_auth_busy'):
            auth.prepare(config(tmp_path), native=lambda *a: pytest.fail('must not invoke native'))


def test_managed_bootstrap_preserves_device_contract_across_home_isolation(tmp_path):
    from agent_hub_host.provisioning import engine_environment
    cfg = config(tmp_path)
    with patch.dict(auth.os.environ, auth.environment(cfg, base={})), \
            patch('agent_hub_host.fleet.load_settings', side_effect=AssertionError('isolated config is not the host config')), \
            patch.object(auth, 'prepare', return_value={'status': 'ok'}) as prepare:
        env = engine_environment({'provider': 'go', 'env': {}}, tmp_path / 'managed-home')
    prepare.assert_called_once_with(cfg)
    assert env['OPENCODE_DB'] == cfg['target_data'] + '/opencode/opencode.db'
    assert env['XDG_CONFIG_HOME'] == cfg['target_data'] + '/config'
