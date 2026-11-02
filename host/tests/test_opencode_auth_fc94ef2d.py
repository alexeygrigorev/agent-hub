"""Regression coverage for OpenCode auth checks that can fail transiently."""
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_hub_host import opencode_auth as auth


def config(tmp_path):
    return dict(binary='/installed/opencode', source_data=str(tmp_path / 'source'),
                source_config=str(tmp_path / 'config'), target_data=str(tmp_path / 'runtime'))


def stale_target(cfg, identity):
    """Create the private Hub target layout expected by private_fs."""
    target = Path(cfg['target_data'])
    target.mkdir(mode=0o700)
    (target / 'opencode').mkdir(mode=0o700)
    (target / 'opencode' / 'opencode.db').touch(mode=0o600)
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    auth.private_fs.atomic_write(target / 'device-source.json',
                                json.dumps({'digest': digest}).encode(), 1024)
    return target


class NativeProcess:
    def __init__(self, returncode, stdout, stderr, *, body=b'', timeout=False):
        self.returncode = returncode
        self.pid = 700001
        self.stdout = stdout
        self.stderr = stderr
        self.body = body
        self.timeout = timeout

    def communicate(self, _payload, timeout):
        if self.timeout:
            raise subprocess.TimeoutExpired(['private-argv'], timeout, output=b'fixture-secret')
        self.stdout.write(self.body)
        self.stderr.write(b'private stderr fixture-secret')
        return None, None

    def kill(self):
        pass

    def wait(self):
        return self.returncode


def test_nonzero_auth_list_exit_is_retryable_with_safe_step_and_exit_code(tmp_path):
    cfg = config(tmp_path)
    Path(cfg['target_data']).mkdir()
    calls = []

    def spawn(_argv, *, stdout, stderr, **_kwargs):
        calls.append(True)
        return NativeProcess(17, stdout, stderr, body=b'load unavailable fixture-secret')

    with patch.object(auth.subprocess, 'Popen', side_effect=spawn), \
            patch.object(auth.time, 'sleep'):
        with pytest.raises(auth.AuthCheckUnavailable) as caught:
            auth._native(cfg, ['auth', 'list', '--format', 'json'], {})

    assert len(calls) == 2
    assert caught.value.step == 'auth_list'
    assert caught.value.failure == 'nonzero_exit'
    assert caught.value.exit_code == 17
    assert 'fixture-secret' not in str(caught.value)
    assert 'expired' not in str(caught.value)


def test_native_json_decode_failure_is_retryable_and_sanitized(tmp_path):
    cfg = config(tmp_path)
    Path(cfg['target_data']).mkdir()

    with patch.object(auth.subprocess, 'Popen',
                      side_effect=lambda _argv, *, stdout, stderr, **_kwargs:
                      NativeProcess(0, stdout, stderr, body=b'{broken fixture-secret')):
        with pytest.raises(auth.AuthCheckUnavailable) as caught:
            auth._native(cfg, ['api', 'model.list'], {})

    assert caught.value.step == 'model_list'
    assert caught.value.failure == 'invalid_json'
    assert 'fixture-secret' not in str(caught.value)


def test_native_timeout_records_only_step_and_timeout(tmp_path):
    cfg = config(tmp_path)
    Path(cfg['target_data']).mkdir()

    with patch.object(auth.subprocess, 'Popen',
                      side_effect=lambda _argv, *, stdout, stderr, **_kwargs:
                      NativeProcess(0, stdout, stderr, timeout=True)), \
            patch.object(auth.os, 'killpg'):
        with pytest.raises(auth.AuthCheckUnavailable) as caught:
            auth._native(cfg, ['api', 'model.list'], {})

    assert caught.value.step == 'model_list'
    assert caught.value.failure == 'timeout'
    assert caught.value.timeout_s == 45
    assert 'fixture-secret' not in str(caught.value)


def test_source_refresh_rejection_is_expired_with_safe_step_and_exit_code(tmp_path):
    cfg = config(tmp_path)
    Path(cfg['target_data']).mkdir()

    with patch.object(auth.subprocess, 'Popen',
                      side_effect=lambda _argv, *, stdout, stderr, **_kwargs:
                      NativeProcess(1, stdout, stderr, body=b'HTTP 401 refresh token rejected fixture-secret')), \
            patch.object(auth.time, 'sleep'), \
            patch.object(auth.socket, 'gethostname', return_value='RMTHZ'):
        with pytest.raises(auth.AuthExpired) as caught:
            auth._native(cfg, ['auth', 'export', 'opencode'], {})

    assert 'fixture-secret' not in str(caught.value)
    assert 'opencode auth login opencode --method device on RMTHZ' in str(caught.value)
    assert caught.value.diagnostic()['step'] == 'auth_export'
    assert caught.value.diagnostic()['exit_code'] == 1


def test_missing_device_login_names_login_command_and_host(tmp_path):
    cfg = config(tmp_path)
    with patch.object(auth, '_verified_recently', return_value=False), \
            patch.object(auth, '_verification_identity', return_value=None), \
            patch.object(auth.socket, 'gethostname', return_value='RMTHZ'):
        with pytest.raises(auth.AuthExpired) as caught:
            auth.prepare(cfg, native=lambda *_args, **_kwargs: [])

    assert 'opencode auth login opencode --method device on RMTHZ' in str(caught.value)


def test_stale_runtime_auth_store_is_reimported_from_valid_source(tmp_path):
    cfg = config(tmp_path)
    identity = {'id': 'source-login', 'method': 'device', 'refresh': 'refresh-fixture', 'metadata': None}
    stale_target(cfg, identity)

    source = [dict(id='source-login', integrationID='opencode', active=True,
                   value=dict(type='oauth', methodID='device', access='access-fixture',
                              refresh='refresh-fixture'))]
    calls = []
    auth_list_count = 0

    def native(_cfg, args, _env, payload=None):
        nonlocal auth_list_count
        calls.append(args)
        if args[:2] == ['auth', 'export']:
            return source
        if args == ['auth', 'import']:
            assert json.loads(payload) == source
            return None
        if args[:2] == ['auth', 'list']:
            auth_list_count += 1
            if auth_list_count == 1:
                return []  # The existing Hub target store lost its device connection.
            return [dict(id='opencode', connections=[dict(method='oauth')])]
        if args == ['api', 'model.list']:
            return dict(data=[dict(providerID='opencode-go',
                                   id=auth.MODEL.split('/', 1)[1], enabled=True)])
        raise AssertionError('unexpected native operation')

    with patch.object(auth, '_verified_recently', return_value=False), \
            patch.object(auth, '_verification_identity', return_value=None):
        result = auth.prepare(cfg, native=native)

    assert result == {'status': 'ok', 'reason': 'device_auth_verified'}
    assert calls.count(['auth', 'import']) == 1
    assert auth_list_count == 2


def test_target_refresh_rejection_reimports_and_never_marks_source_expired(tmp_path, capsys):
    cfg = config(tmp_path)
    identity = {'id': 'source-login', 'method': 'device', 'refresh': 'refresh-fixture', 'metadata': None}
    stale_target(cfg, identity)
    source = [dict(id='source-login', integrationID='opencode', active=True,
                   value=dict(type='oauth', methodID='device', access='access-fixture',
                              refresh='refresh-fixture'))]
    calls = []

    def native(_cfg, args, _env, payload=None):
        calls.append(args)
        if args[:2] == ['auth', 'export']:
            return source
        if args == ['auth', 'import']:
            assert json.loads(payload) == source
            return None
        if args[:2] == ['auth', 'list']:
            return [dict(id='opencode', connections=[dict(method='oauth')])]
        if args == ['api', 'model.list']:
            if calls.count(['api', 'model.list']) == 1:
                raise auth.AuthExpired('refresh_rejected', step='model_list', exit_code=1)
            return dict(data=[dict(providerID='opencode-go',
                                   id=auth.MODEL.split('/', 1)[1], enabled=True)])
        raise AssertionError('unexpected native operation')

    with patch.object(auth, '_verified_recently', return_value=False), \
            patch.object(auth, '_verification_identity', return_value=None):
        result = auth.prepare(cfg, native=native)

    assert result == {'status': 'ok', 'reason': 'device_auth_verified'}
    assert calls.count(['auth', 'import']) == 1
    assert calls.count(['api', 'model.list']) == 2
    logged = capsys.readouterr().out
    assert json.loads(logged) == {'event': 'opencode_auth_target_reimport',
                                  'step': 'model_list', 'failure': 'auth_rejected',
                                  'exit_code': 1}
    assert 'refresh-fixture' not in logged


def test_native_target_rejection_repairs_the_runtime_store(tmp_path, capsys):
    cfg = config(tmp_path)
    identity = {'id': 'source-login', 'method': 'device', 'refresh': 'refresh-fixture', 'metadata': None}
    stale_target(cfg, identity)
    source = [dict(id='source-login', integrationID='opencode', active=True,
                   value=dict(type='oauth', methodID='device', access='access-fixture',
                              refresh='refresh-fixture'))]
    model_calls = 0
    import_calls = 0

    def spawn(argv, *, stdout, stderr, **_kwargs):
        nonlocal model_calls, import_calls
        args = argv[1:-1]
        if args[:2] == ['auth', 'export']:
            return NativeProcess(0, stdout, stderr, body=json.dumps(source).encode())
        if args[:2] == ['auth', 'import']:
            import_calls += 1
            return NativeProcess(0, stdout, stderr)
        if args[:2] == ['auth', 'list']:
            integrations = [dict(id='opencode', connections=[dict(method='oauth')])]
            return NativeProcess(0, stdout, stderr, body=json.dumps(integrations).encode())
        if args[:2] == ['api', 'model.list']:
            model_calls += 1
            if model_calls == 1:
                return NativeProcess(1, stdout, stderr,
                                    body=b'HTTP 401 invalid refresh fixture-secret')
            models = {'data': [dict(providerID='opencode-go',
                                    id=auth.MODEL.split('/', 1)[1], enabled=True)]}
            return NativeProcess(0, stdout, stderr, body=json.dumps(models).encode())
        raise AssertionError('unexpected native operation')

    with patch.object(auth, '_verified_recently', return_value=False), \
            patch.object(auth, '_verification_identity', return_value=None), \
            patch.object(auth.subprocess, 'Popen', side_effect=spawn):
        result = auth.prepare(cfg)

    assert result == {'status': 'ok', 'reason': 'device_auth_verified'}
    assert import_calls == 1
    assert model_calls == 2
    logged = capsys.readouterr().out
    assert json.loads(logged) == {'event': 'opencode_auth_target_reimport',
                                  'step': 'model_list', 'failure': 'auth_rejected',
                                  'exit_code': 1}
    assert 'fixture-secret' not in logged


def test_persistent_target_refresh_rejection_is_expired_after_reimport(tmp_path, capsys):
    cfg = config(tmp_path)
    identity = {'id': 'source-login', 'method': 'device', 'refresh': 'refresh-fixture', 'metadata': None}
    stale_target(cfg, identity)
    source = [dict(id='source-login', integrationID='opencode', active=True,
                   value=dict(type='oauth', methodID='device', access='access-fixture',
                              refresh='refresh-fixture'))]
    calls = []

    def native(_cfg, args, _env, _payload=None):
        calls.append(args)
        if args[:2] == ['auth', 'export']:
            return source
        if args == ['auth', 'import']:
            return None
        if args[:2] == ['auth', 'list']:
            return [dict(id='opencode', connections=[dict(method='oauth')])]
        if args == ['api', 'model.list']:
            raise auth.AuthExpired('refresh_rejected', step='model_list', exit_code=1)
        raise AssertionError('unexpected native operation')

    with patch.object(auth, '_verified_recently', return_value=False), \
            patch.object(auth, '_verification_identity', return_value=None):
        with pytest.raises(auth.AuthExpired) as caught:
            auth.prepare(cfg, native=native)

    assert calls.count(['auth', 'import']) == 1
    assert calls.count(['api', 'model.list']) == 2
    assert caught.value.reason == 'refresh_rejected'
    assert caught.value.step == 'model_list'
    assert caught.value.exit_code == 1
    logged = capsys.readouterr().out
    events = [json.loads(line) for line in logged.splitlines()]
    assert events[0] == {'event': 'opencode_auth_target_reimport',
                         'step': 'model_list', 'failure': 'auth_rejected',
                         'exit_code': 1}
    assert events[1]['event'] == 'opencode_auth_expired'
    assert events[1]['reason'] == 'engine_auth_expired'
    assert events[1]['failure'] == 'refresh_rejected'
    assert events[1]['step'] == 'model_list'
    assert events[1]['exit_code'] == 1
    assert 'refresh-fixture' not in logged


def test_generic_json_decode_error_from_native_adapter_is_unavailable(tmp_path):
    cfg = config(tmp_path)
    with patch.object(auth, '_verified_recently', return_value=False), \
            patch.object(auth, '_verification_identity', return_value=None):
        with pytest.raises(auth.AuthCheckUnavailable) as caught:
            auth.prepare(cfg, native=lambda *_args, **_kwargs:
                         (_ for _ in ()).throw(json.JSONDecodeError('private', 'fixture-secret', 0)))

    assert caught.value.failure == 'invalid_json'
    assert 'fixture-secret' not in str(caught.value)


def test_target_import_auth_rejection_is_unavailable_not_source_expired(tmp_path):
    cfg = config(tmp_path)
    source = [dict(id='source-login', integrationID='opencode', active=True,
                   value=dict(type='oauth', methodID='device', access='access-fixture',
                              refresh='refresh-fixture'))]

    def native(_cfg, args, _env, _payload=None):
        if args[:2] == ['auth', 'export']:
            return source
        if args == ['auth', 'import']:
            raise RuntimeError('HTTP 401 invalid fixture-secret')
        raise AssertionError('unexpected native operation')

    with patch.object(auth, '_verified_recently', return_value=False), \
            patch.object(auth, '_verification_identity', return_value=None):
        with pytest.raises(auth.AuthCheckUnavailable) as caught:
            auth.prepare(cfg, native=native)

    assert caught.value.step == 'auth_import'
    assert caught.value.failure == 'auth_rejected'
    assert 'fixture-secret' not in str(caught.value)
    assert 'engine_auth_expired' not in str(caught.value)


def test_prepare_logs_only_safe_native_failure_metadata(tmp_path, capsys):
    cfg = config(tmp_path)

    def native(*_args, **_kwargs):
        raise auth.AuthCheckUnavailable('auth_export', 'nonzero_exit', exit_code=17)

    with patch.object(auth, '_verified_recently', return_value=False), \
            patch.object(auth, '_verification_identity', return_value=None), \
            pytest.raises(auth.AuthCheckUnavailable):
        auth.prepare(cfg, native=native)

    event = json.loads(capsys.readouterr().out)
    assert event == {'event': 'opencode_auth_check_unavailable',
                     'reason': 'engine_auth_check_unavailable', 'step': 'auth_export',
                     'failure': 'nonzero_exit', 'exit_code': 17}
