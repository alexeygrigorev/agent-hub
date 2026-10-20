import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_hub_host import provisioning as p


@pytest.fixture(autouse=True)
def isolated_aplexer(tmp_path, monkeypatch):
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer' / 'config.toml'))


def bundle(revision='11111111-1111-4111-8111-111111111111'):
    return dict(label='test-account', provider='codex', version='0.163.0', revision=revision,
                files={'.codex/auth.json': '{"token":"synthetic-private-marker"}'}, env={'OPENAI_API_KEY': 'synthetic-private-marker'}, args=[])


def installer(argv, **kwargs):
    # Assert install never inherits credentials, even if caller is logged in elsewhere.
    assert 'OPENAI_API_KEY' not in kwargs['env']
    dest = Path(argv[argv.index('--prefix') + 1]) / 'node_modules' / '.bin' / 'codex'
    dest.parent.mkdir(parents=True)
    dest.write_text('#!/bin/sh\nexit 0\n')
    dest.chmod(0o700)


def test_add_rotate_remove_and_preserve_operator_profile(monkeypatch):
    requests = []
    desired = [bundle()]
    def request(method, path, payload=None):
        requests.append((method, path, payload))
        return {'engines': desired} if method == 'GET' else {'status': 'installed'}
    monkeypatch.setenv('OPENAI_API_KEY', 'unrelated-account')
    path = p.profile_path()
    path.parent.mkdir(parents=True)
    original = 'version = 1\n[profiles.operator]\nengine = "shell"\n'
    path.write_text(original)
    with patch.object(p.subprocess, 'run', side_effect=installer) as install:
        assert p.sync(request)['profiles'] == ['hub-test-account']
        assert install.call_count == 1
        p.sync(request)
        assert install.call_count == 1
        assert 'synthetic-private-marker' not in path.read_text()
        assert '[profiles.operator]' in path.read_text()
        credential = next(p.root().glob('test-account-*/home/.codex/auth.json'))
        assert credential.stat().st_mode & 0o777 == 0o600
        old = credential.parents[2]
        desired[:] = [bundle('22222222-2222-4222-8222-222222222222')]
        p.sync(request)
        assert not old.exists()
        assert install.call_count == 2
        desired.clear()
        p.sync(request)
    assert not list(p.root().glob('test-account-*'))
    assert not p.managed_subscriptions()
    assert '[profiles.operator]' in path.read_text()
    assert 'hub-test-account' not in path.read_text()
    assert requests[-2][0] == 'POST'


@pytest.mark.parametrize('relative', ['../escape', '/absolute', 'a/../../escape', 'a//b', 'a/./b'])
def test_refuses_traversal_before_any_install(relative):
    b = bundle(); b['files'] = {relative: 'secret'}
    with patch.object(p.subprocess, 'run') as run:
        with pytest.raises(ValueError):
            p.sync(lambda *a: {'engines': [b]})
        run.assert_not_called()


def test_symlink_cannot_read_credentials_or_overwrite_other_configuration(tmp_path, monkeypatch):
    target = tmp_path / 'operator.toml'
    target.write_text('operator data')
    path = p.profile_path()
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    with patch.object(p.subprocess, 'run') as run:
        with pytest.raises(OSError):
            p.sync(lambda *a: {'engines': [bundle()]})
        run.assert_not_called()
    assert target.read_text() == 'operator data'


def test_install_failure_does_not_publish_profile_or_ack():
    calls = []
    def request(*args):
        calls.append(args)
        return {'engines': [bundle()]}
    with patch.object(p.subprocess, 'run', side_effect=OSError('secret stderr')):
        with pytest.raises(ValueError, match='^Managed engine installation failed$'):
            p.sync(request)
    assert len(calls) == 1
    assert not p.managed_subscriptions()
    assert 'hub-test-account' not in p.profile_path().read_text()


def test_credentials_go_to_child_only_not_native_profile_or_argv():
    b = bundle()
    with patch.object(p.subprocess, 'run', side_effect=installer):
        p.sync(lambda *a: {'engines': [b]})
    with patch.object(p.sys, 'argv', ['wrapper', 'test-account', '--version']), patch.object(p.os, 'execve') as execute:
        p.execute()
    executable, argv, env = execute.call_args.args
    assert argv[-1] == '--version'
    assert 'synthetic-private-marker' not in json.dumps(argv)
    assert env['OPENAI_API_KEY'] == 'synthetic-private-marker'
    assert env['HOME'].endswith('/home')


def test_cli_refuses_public_bundle_file(tmp_path):
    from types import SimpleNamespace
    path = tmp_path / 'bundle.json'; path.write_text(json.dumps(bundle()))
    path.chmod(0o644)
    with patch.object(p.bus, '_do_request') as request:
        with pytest.raises(PermissionError):
            p.command(SimpleNamespace(engines_cmd='add', file=str(path)))
        request.assert_not_called()


def test_native_auth_import_is_one_command_and_does_not_echo_secret(tmp_path):
    from types import SimpleNamespace
    auth_dir = tmp_path / 'private'; auth_dir.mkdir(mode=0o700)
    auth = auth_dir / 'auth.json'; auth.write_text('{"token":"private-import-marker"}'); auth.chmod(0o600)
    args = SimpleNamespace(engines_cmd='add', file=None, provider='codex', label='second-account',
                           auth_file=str(auth), config_file=None, version=None)
    with patch.object(p.bus, '_do_request', return_value={'status': 'configured'}) as request:
        assert p.command(args) == {'status': 'configured'}
    method, path, payload = request.call_args.args
    assert path == '/api/engines/second-account'
    assert payload['files']['.codex/auth.json'] == auth.read_text()


def test_managed_quota_unknown_on_probe_failure_without_stale_success():
    with patch.object(p.subprocess, 'run', side_effect=installer):
        p.sync(lambda *a: {'engines': [bundle()]})
    with patch.object(p.subprocess, 'run', side_effect=OSError('private-provider-error')):
        result = p.quotas()
    assert result['accounts'][0]['quota']['status'] == 'unknown'
    assert 'private-provider-error' not in json.dumps(result)
    assert 'synthetic-private-marker' not in json.dumps(result)


def test_gemini_uses_native_machine_login_and_rejects_cache_copy():
    b = {**bundle(), 'provider': 'gemini', 'auth_method': 'machine-login', 'files': {}, 'env': {}}
    p.validate(b)
    b['files']['.gemini/oauth_creds.json'] = 'must stay local'
    with pytest.raises(ValueError, match='native per-machine login'):
        p.validate(b)


def test_google_login_url_validation():
    from agent_hub_host.engine_login import login_url
    assert login_url('URL: https://accounts.google.com/o/oauth2/v2/auth?state=public-state\n') == 'https://accounts.google.com/o/oauth2/v2/auth?state=public-state'
    assert login_url('https://accounts.google.com.evil.invalid/auth') is None
    assert login_url('https://accounts.google.com@evil.invalid/auth') is None
    assert login_url('https://evil.invalid/google') is None


def test_native_login_relay_keeps_authorization_code_out_of_reports(tmp_path):
    from agent_hub_host import engine_login
    import threading
    import sys
    b = {**bundle(), 'provider': 'gemini', 'auth_method': 'machine-login', 'files': {}, 'env': {}}
    dest = tmp_path / 'isolated'
    binary = dest / 'engine/node_modules/.bin/gemini'; binary.parent.mkdir(parents=True)
    binary.write_text('#!' + sys.executable + '\nimport os,json,time\nfrom pathlib import Path\nprint("https://accounts.google.com/o/oauth2/v2/auth?state=synthetic-state", flush=True)\nprint("Enter the authorization code:", flush=True)\ncode=input()\np=Path(os.environ["HOME"])/".gemini/oauth_creds.json"\np.write_text(json.dumps({"token":"synthetic-login-token"}))\ntime.sleep(30)\n')
    binary.chmod(0o700)
    reports = []
    def request(method, path, data=None):
        if method == 'GET': return {'status': 'submitted', 'code': 'one-time-private-code'}
        reports.append(data)
        return {'status': data['status']}
    engine_login._run({'id': 'test-login'}, b, dest, request, threading.Event())
    assert reports[-1]['status'] == 'authenticated'
    assert 'one-time-private-code' not in json.dumps(reports)
    assert 'synthetic-login-token' not in json.dumps(reports)


def test_claude_portable_subscription_token_not_refreshable_cache(tmp_path):
    from types import SimpleNamespace
    d = tmp_path / 'private'; d.mkdir(mode=0o700)
    token = d / 'token'; token.write_text('synthetic-setup-token\n'); token.chmod(0o600)
    args = SimpleNamespace(engines_cmd='add',file=None,provider='claude',label='my-claude',auth_file=None,
                           token_file=str(token),config_file=None,version=None)
    with patch.object(p.bus,'_do_request',return_value={'status':'configured'}) as request:
        p.command(args)
    payload = request.call_args.args[2]
    assert payload['env'] == {'CLAUDE_CODE_OAUTH_TOKEN':'synthetic-setup-token'}
    assert payload['files'] == {}


def test_default_machine_settings_need_no_account_configuration(tmp_path, monkeypatch):
    from agent_hub_host import fleet
    monkeypatch.setenv('HOME',str(tmp_path))
    settings=fleet.load_settings()
    assert settings['workspaces'] == {'home':str(tmp_path)}
    assert settings['subscriptions'] == []


def test_go_import_does_not_copy_other_provider_accounts(tmp_path):
    from types import SimpleNamespace
    d=tmp_path/'private';d.mkdir(mode=0o700)
    auth=d/'auth.json';auth.write_text(json.dumps({'opencode-go':{'type':'api','key':'go-private'},'unrelated':{'key':'must-not-copy'}}));auth.chmod(0o600)
    args=SimpleNamespace(engines_cmd='add',file=None,provider='go',label='my-go',auth_file=str(auth),token_file=None,config_file=None,version=None)
    with patch.object(p.bus,'_do_request',return_value={'status':'configured'}) as request:
        p.command(args)
    payload=request.call_args.args[2]
    assert 'must-not-copy' not in json.dumps(payload)
    assert payload['args'] == ['--model','opencode-go/muse-spark-1.3-contributor']


def test_go_host_and_server_accept_same_diagnostic_arguments():
    b={**bundle(), 'provider':'go', 'files':{}, 'env':{}, 'args':['--model','opencode-go/muse-spark-1.3-contributor','--version']}
    p.validate(b)
    b['args']=['--model','glm-5.3','--version']
    with pytest.raises(ValueError):
        p.validate(b)
