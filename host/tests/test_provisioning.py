import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from agent_hub_host import provisioning as p


@pytest.fixture(autouse=True)
def isolated_aplexer(tmp_path, monkeypatch):
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer' / 'config.toml'))
    monkeypatch.setattr(p,'MANAGED_CLAUDE_SYSTEM_SETTINGS',())


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


def test_codex_operator_wrapper_is_required_when_configured(tmp_path, monkeypatch):
    wrapper = tmp_path / 'launch-codex.sh'
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', str(wrapper))
    with pytest.raises(ValueError, match='wrapper unavailable'):
        p.engine_argv(bundle(), tmp_path)
    wrapper.write_text('#!/bin/sh\nexit 0\n')
    wrapper.chmod(0o700)
    assert p.engine_argv(bundle(), tmp_path) == [str(wrapper)]
    monkeypatch.setenv('CODEX_LAUNCH_WRAPPER', 'relative-wrapper')
    with pytest.raises(ValueError, match='wrapper unavailable'):
        p.engine_argv(bundle(), tmp_path)


def test_managed_codex_orin_reports_canonical_codex_service(monkeypatch):
    record = {'label': 'codex-orin', 'provider': 'codex', 'revision': 'rev'}
    def read(path, fallback=None):
        return [record] if path.name == 'state.json' else {'quota_provider': 'codex-orin'}
    monkeypatch.setattr(p, 'read_json', read)
    sub = p.managed_subscriptions()[0]
    # Canonical provider model: Orin's second Codex home reports service
    # `codex` (its label stays `codex-orin`); the legacy quse route key
    # `codex-orin` remains only inside the quota probe, never as a service.
    assert sub['service'] == 'codex'
    assert sub['engine'] == 'codex'
    assert sub['account_label'] == 'codex-orin'
    assert sub['profile'] == 'hub-codex-orin'


def test_add_rotate_remove_and_preserve_operator_profile(monkeypatch):
    from agent_hub_host import engine_rollout
    monkeypatch.setattr(engine_rollout, 'deferred_labels', lambda: set())
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
    hub_calls = [r for r in requests if r[1] != '/api/engines/sync-report']  # AC9 status report is separate
    assert hub_calls[-2][0] == 'POST'


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
    acks = [c for c in calls if c[1] != "/api/engines/sync-report"]  # failure report is not an ack (5814e109 AC9)
    assert len(acks) == 1
    assert "secret stderr" not in repr(calls)
    assert not p.managed_subscriptions()
    assert 'hub-test-account' not in p.profile_path().read_text()


def test_credentials_go_to_child_only_not_native_profile_or_argv():
    b = bundle()
    with patch.object(p.subprocess, 'run', side_effect=installer):
        p.sync(lambda *a: {'engines': [b]})
    with patch.object(p.sys, 'argv', ['wrapper', 'test-account', '--version']), \
         patch.object(p, 'hub_launch_gate', return_value=(True, 'ok')), \
         patch.object(p.os, 'execve') as execute:
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


def test_engines_list_worker_view_requests_sanitized_endpoint():
    from types import SimpleNamespace
    with patch.object(p.bus, '_do_request', return_value={'schema_version': '1.0', 'engines': []}) as request:
        assert p.command(SimpleNamespace(engines_cmd='list', view='worker')) == {'schema_version': '1.0', 'engines': []}
        assert p.command(SimpleNamespace(engines_cmd='list', view=None)) == {'schema_version': '1.0', 'engines': []}
    assert request.call_args_list[0].args == ('GET', '/api/engines?view=worker')
    assert request.call_args_list[1].args == ('GET', '/api/engines')


def test_engines_list_parser_accepts_only_worker_view():
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    p.add_commands(sub)
    args = parser.parse_args(['engines', 'list', '--view', 'worker'])
    assert args.view == 'worker'
    assert parser.parse_args(['engines', 'list']).view is None
    with pytest.raises(SystemExit):
        parser.parse_args(['engines', 'list', '--view', 'operator'])


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
    # Direct provider read fails (intake 5814e109: no quse): no row is reported,
    # so the collector never masks Hub's own fresh observation with an error.
    from agent_hub_host import credentials
    with patch.object(credentials.urllib.request.OpenerDirector, 'open', side_effect=OSError('private-provider-error')):
        result = p.quotas()
    assert result == {'accounts': []}
    assert 'private-provider-error' not in json.dumps(result)
    assert 'synthetic-private-marker' not in json.dumps(result)


def test_claude_uses_native_claude_quota():
    claude_bundle = {**bundle(), 'provider': 'claude', 'version': '2.1.304', 'files': {}, 'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'token-synthetic'}}
    def claude_installer(argv, **kwargs):
        if '--version' in argv:
            return subprocess.CompletedProcess(argv, 0)
        prefix = Path(argv[argv.index('--prefix') + 1])
        package = prefix / 'node_modules' / '@anthropic-ai' / 'claude-code-linux-x64'
        package.mkdir(parents=True, exist_ok=True)
        (package / 'package.json').write_text(json.dumps({'version': '2.1.304'}))
        binary = package / 'claude'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o700)
        return subprocess.CompletedProcess(argv, 0)
    with patch.object(p.subprocess, 'run', side_effect=claude_installer):
        p.sync(lambda *a: {'engines': [claude_bundle]})
    with patch('agent_hub_host.native_claude_quota.probe', return_value={'claude': {
        'status': 'ok',
        'details': {'limit_reached': False, 'source': 'native_claude_oauth_usage'},
        'windows': {'5h': {'percent_remaining': 80.0, 'reset_at': '2030-01-01T00:00:00Z'},
                    '7d': {'percent_remaining': 90.0, 'reset_at': '2030-01-05T00:00:00Z'}},
    }}):
        result = p.quotas()
    assert result['accounts'][0]['provider'] == 'claude'
    assert result['accounts'][0]['quota']['status'] == 'ok'
    assert result['accounts'][0]['quota']['windows']['5h']['percent_remaining'] == 80.0
    assert result['accounts'][0]['quota']['windows']['7d']['percent_remaining'] == 90.0


def test_gemini_machine_login_or_hub_held_oauth_credential():
    b = {**bundle(), 'provider': 'gemini', 'auth_method': 'machine-login', 'files': {}, 'env': {}}
    p.validate(b)
    # A machine-login bundle never carries a credential cache...
    b['files']['.gemini/oauth_creds.json'] = 'must stay local'
    with pytest.raises(ValueError, match='machine login or a Hub-held'):
        p.validate(b)
    # ...while a Hub-held Gemini account (intake 5814e109 I1) must carry one.
    hub_held = {**bundle(), 'provider': 'gemini', 'files': {'.gemini/oauth_creds.json': '{}'}, 'env': {}}
    p.validate(hub_held)
    with pytest.raises(ValueError):
        p.validate({**hub_held, 'files': {}})


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


def test_go_import_refuses_legacy_credentials_and_uses_machine_login(tmp_path):
    from types import SimpleNamespace
    d=tmp_path/'private';d.mkdir(mode=0o700)
    auth=d/'auth.json';auth.write_text(json.dumps({'opencode-go':{'type':'api','key':'go-private'},'unrelated':{'key':'must-not-copy'}}));auth.chmod(0o600)
    args=SimpleNamespace(engines_cmd='add',file=None,provider='go',label='my-go',auth_file=str(auth),token_file=None,config_file=None,version=None)
    with patch.object(p.bus,'_do_request',return_value={'status':'configured'}) as request:
        with pytest.raises(ValueError, match='device login'):
            p.command(args)
        request.assert_not_called()
        export = d/'export.json'
        export.write_text(json.dumps([{'id':'c1','active':True,'integrationID':'opencode','value':{'type':'oauth','methodID':'device','access':'A','refresh':'R'}}]));export.chmod(0o600)
        args.auth_file = str(export)
        p.command(args)
        stored = request.call_args.args[2]
        assert list(stored['files']) == [p.OPENCODE_DEVICE_EXPORT] and 'auth_method' not in stored
        args.auth_file = None
        p.command(args)
    payload=request.call_args.args[2]
    assert 'must-not-copy' not in json.dumps(payload)
    assert payload['args'] == ['--model','opencode-go/muse-spark-1.3-contributor']
    assert payload['files'] == {} and payload['env'] == {}
    assert payload['auth_method'] == 'machine-login'


def test_go_host_and_server_accept_same_diagnostic_arguments():
    b={**bundle(), 'provider':'go', 'files':{}, 'env':{}, 'args':['--model','opencode-go/muse-spark-1.3-contributor','--version']}
    p.validate(b)
    b['args']=['--model','glm-5.3','--version']
    with pytest.raises(ValueError):
        p.validate(b)


def test_zai_import_via_token_file(tmp_path):
    from types import SimpleNamespace
    d = tmp_path / 'private'; d.mkdir(mode=0o700)
    token = d / 'zai-token'; token.write_text('synthetic-zai-token\n'); token.chmod(0o600)
    args = SimpleNamespace(engines_cmd='add', file=None, provider='zai', label='my-zai', auth_file=None,
                           token_file=str(token), config_file=None, version=None)
    with patch.object(p.bus, '_do_request', return_value={'status': 'configured'}) as request:
        p.command(args)
    payload = request.call_args.args[2]
    assert payload['provider'] == 'zai'
    assert payload['version'] == '0.155.1'
    assert payload['env'] == {'ZAI_API_KEY': 'synthetic-zai-token'}
    assert payload['args'] == ['--model', 'glm-5.3-flash']
    assert '.config/goz/config.json' in payload['files']
    assert '.zcodex/config.toml' in payload['files']
    assert '.zcode/v2/credentials.json' not in payload['files']


def test_zai_import_via_credentials_auth_file(tmp_path):
    from types import SimpleNamespace
    d = tmp_path / 'private'; d.mkdir(mode=0o700)
    auth = d / 'credentials.json'
    auth.write_text(json.dumps({'oauth:zai:access_token': 'auth-file-token', 'zcodejwttoken': 'jwt-token'}))
    auth.chmod(0o600)
    args = SimpleNamespace(engines_cmd='add', file=None, provider='zai', label='my-zai', auth_file=str(auth),
                           token_file=None, config_file=None, version=None)
    with patch.object(p.bus, '_do_request', return_value={'status': 'configured'}) as request:
        p.command(args)
    payload = request.call_args.args[2]
    assert payload['provider'] == 'zai'
    assert payload['env'] == {'ZAI_API_KEY': 'auth-file-token'}
    assert payload['args'] == ['--model', 'glm-5.3-flash']
    assert '.zcode/v2/credentials.json' in payload['files']


def test_zai_installation_with_native_binary_and_execution(tmp_path):
    b = {'label': 'zai-account', 'provider': 'zai', 'version': '0.155.1',
         'revision': '33333333-3333-4333-8333-333333333333',
         'files': {'.zcode/v2/credentials.json': '{"token":"zai-secret"}'},
         'env': {'ZAI_API_KEY': 'zai-secret'},
         'args': ['--model', 'glm-5.3-flash']}
    fake_zcodex = tmp_path / 'fake-zcodex'
    fake_zcodex.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "codex-cli 0.0.0"; exit 0; fi\nexit 0\n')
    fake_zcodex.chmod(0o700)
    fake_runtime = tmp_path / 'zcode.cjs'
    fake_runtime.write_text('// fake runtime\n')

    with patch.object(p, 'locate_native_zcodex', return_value=fake_zcodex), \
         patch.object(p, 'locate_zcode_runtime', return_value=fake_runtime):
        assert p.sync(lambda method, path, payload=None: {'engines': [b]} if method == 'GET' else {'status': 'installed'})['profiles'] == ['hub-zai-account']

    config_text = p.profile_path().read_text()
    assert '[profiles.hub-zai-account]' in config_text
    assert 'engine = "codex"' in config_text

    cred = next(p.root().glob('zai-account-*/home/.zcode/v2/credentials.json'))
    assert cred.stat().st_mode & 0o777 == 0o600

    with patch.object(p.sys, 'argv', ['wrapper', 'zai-account', '--version']), patch.object(p.os, 'execve') as execute:
        p.execute()
    executable, argv, env = execute.call_args.args
    assert argv[-1] == '--version'
    assert '--model' in argv and 'glm-5.3-flash' in argv
    assert env['ZAI_API_KEY'] == 'zai-secret'
    assert env['CODEX_HOME'].endswith('/home/.zcodex')
    assert env['ZCODE_HOME'].endswith('/home/.zcode')


def test_zai_installation_fails_closed_when_runtime_missing(tmp_path):
    b = {'label': 'zai-account', 'provider': 'zai', 'version': '0.155.1',
         'revision': '77777777-7777-7777-8777-777777777777',
         'files': {'.config/goz/config.json': '{}'},
         'env': {'ZAI_API_KEY': 'zai-secret'},
         'args': ['--model', 'glm-5.3-flash']}
    fake_zcodex = tmp_path / 'fake-zcodex'
    fake_zcodex.write_text('#!/bin/sh\nexit 0\n')
    fake_zcodex.chmod(0o700)

    with patch.object(p, 'locate_native_zcodex', return_value=fake_zcodex), \
         patch.object(p, 'ensure_zcode_runtime', return_value=None):
        with pytest.raises(ValueError, match='ZCode runtime missing'):
            p.sync(lambda method, path, payload=None: {'engines': [b]} if method == 'GET' else {'status': 'installed'})


def test_zai_multiple_labels_coexist_on_one_machine(tmp_path):
    b1 = {'label': 'zai-personal', 'provider': 'zai', 'version': '0.155.1',
          'revision': '44444444-4444-4444-8444-444444444444',
          'files': {'.zcode/v2/credentials.json': 'personal-marker'},
          'env': {'ZAI_API_KEY': 'personal-key'},
          'args': ['--model', 'glm-5.3-flash']}
    b2 = {'label': 'zai-work', 'provider': 'zai', 'version': '0.155.1',
          'revision': '55555555-5555-4555-8555-555555555555',
          'files': {'.zcode/v2/credentials.json': 'work-marker'},
          'env': {'ZAI_API_KEY': 'work-key'},
          'args': ['--model', 'glm-5.3-flash']}
    fake_zcodex = tmp_path / 'fake-zcodex'
    fake_zcodex.write_text('#!/bin/sh\nexit 0\n')
    fake_zcodex.chmod(0o700)
    fake_runtime = tmp_path / 'zcode.cjs'
    fake_runtime.write_text('// fake runtime\n')

    with patch.object(p, 'locate_native_zcodex', return_value=fake_zcodex), \
         patch.object(p, 'locate_zcode_runtime', return_value=fake_runtime):
        result = p.sync(lambda method, path, payload=None: {'engines': [b1, b2]} if method == 'GET' else {'status': 'installed'})
        assert sorted(result['profiles']) == ['hub-zai-personal', 'hub-zai-work']

    config_text = p.profile_path().read_text()
    assert '[profiles.hub-zai-personal]' in config_text
    assert '[profiles.hub-zai-work]' in config_text

    subs = p.managed_subscriptions()
    labels = {s['account_label'] for s in subs}
    assert labels == {'zai-personal', 'zai-work'}
    assert all(s['service'] == 'zai' and s['engine'] == 'codex' for s in subs)


def test_zai_rejection_of_opencode_muse_model():
    b = {'label': 'bad-zai', 'provider': 'zai', 'version': '0.155.1',
         'revision': '66666666-6666-4666-8666-666666666666',
         'files': {}, 'env': {},
         'args': ['--model', 'opencode-go/muse-spark-1.3-contributor']}
    with pytest.raises(ValueError, match='Z.ai cannot route OpenCode Muse'):
        p.validate(b)


def test_real_clean_install_diagnostic_on_hetzner(tmp_path, monkeypatch):
    """Real clean install check requiring genuine local zcodex binary and genuine ZCode runtime."""
    real_zcodex = Path('/home/alexey/.local/bin/zcodex')
    real_runtime = Path('/opt/ZCode/resources/glm/zcode.cjs')
    if not real_zcodex.is_file() or not real_runtime.is_file():
        pytest.skip('Native zcodex or /opt/ZCode runtime not installed on this host')

    b = {'label': 'diag-zai', 'provider': 'zai', 'version': '0.155.1',
         'revision': '88888888-8888-4888-8888-888888888888',
         'files': {'.config/goz/config.json': '{"zai_token":"diag-token"}'},
         'env': {'ZAI_API_KEY': 'diag-token'},
         'args': ['--model', 'glm-5.3-flash']}

    monkeypatch.setattr(p, 'root', lambda: tmp_path / 'managed-engines')
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer' / 'config.toml'))

    res = p.sync(lambda method, path, payload=None: {'engines': [b]} if method == 'GET' else {'status': 'installed'})
    assert res['profiles'] == ['hub-diag-zai']

    wrapper = tmp_path / 'managed-engines' / f"diag-zai-{b['revision']}" / 'engine/bin/zcodex'
    assert wrapper.is_file() and os.access(wrapper, os.X_OK)

    import subprocess
    proc = subprocess.run([str(wrapper), '--version'], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0
    assert 'codex-cli' in proc.stdout


def test_parse_deb_manifest_rejects_missing_or_malformed_checksum():
    # Missing checksum
    no_sha = "files:\n  - url: https://cdn.example.com/zcode.deb\n"
    url, sha = p.parse_deb_manifest(no_sha)
    assert url is None and sha is None

    # Invalid base64 checksum length
    bad_len = "files:\n  - url: https://cdn.example.com/zcode.deb\n    sha512: dG9vc2hvcnQ=\n"
    url, sha = p.parse_deb_manifest(bad_len)
    assert url == "https://cdn.example.com/zcode.deb" and sha is None

    # Valid base64 sha512 (64 bytes = 88 base64 chars)
    import base64
    valid_b64 = base64.b64encode(b"a" * 64).decode()
    good = f"files:\n  - url: https://cdn.example.com/zcode.deb\n    sha512: {valid_b64}\n"
    url, sha = p.parse_deb_manifest(good)
    assert url == "https://cdn.example.com/zcode.deb" and sha == valid_b64


def test_ensure_zcode_runtime_rejects_missing_manifest_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'locate_zcode_runtime', lambda dest=None: None)
    # Mock curl to write a manifest without sha512
    def fake_run(cmd, *args, **kwargs):
        if 'manifest' in str(cmd):
            for i, arg in enumerate(cmd):
                if arg == '-o':
                    Path(cmd[i+1]).write_text("files:\n  - url: https://cdn.example.com/zcode.deb\n")
                    return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    with pytest.raises(ValueError, match='ZCode runtime missing'):
        p.ensure_zcode_runtime(tmp_path)


def test_install_rejects_zcodex_missing_or_mismatched_sha256(tmp_path, monkeypatch):
    b = {'label': 'zai-bad-sha', 'provider': 'zai', 'version': '0.155.1',
         'revision': '99999999-9999-4999-8999-999999999999',
         'files': {}, 'env': {'ZAI_API_KEY': 'tok'},
         'args': ['--model', 'glm-5.3-flash']}

    monkeypatch.setattr(p, 'locate_native_zcodex', lambda bundle=None: None)
    monkeypatch.setattr(p, 'root', lambda: tmp_path / 'managed-engines')

    # Mock curl to return SHA256SUMS with no matching hash or bad length
    def fake_run(cmd, *args, **kwargs):
        cmd_str = ' '.join(str(c) for c in cmd)
        if 'SHA256SUMS' in cmd_str:
            for i, arg in enumerate(cmd):
                if arg == '-o':
                    Path(cmd[i+1]).write_text("1234not64chars zcodex-linux-amd64.tar.zst\n")
                    return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    with pytest.raises(ValueError, match='Managed engine installation failed'):
        p.install(b)


def test_launcher_metadata_mapping():
    assert p.launcher_metadata('go') == {
        'launcher_provider': 'opencode',
        'launcher_model': 'opencode-go/muse-spark-1.3-contributor',
        'launcher_effort': None,
    }
    assert p.launcher_metadata('zai') == {
        'launcher_provider': 'zai',
        'launcher_model': 'glm-5.3-flash',
        'launcher_effort': 'high',
    }
    assert p.launcher_metadata('codex') == {
        'launcher_provider': 'codex',
        'launcher_model': 'gpt-6.1-sol',
        'launcher_effort': 'medium',
    }
    # With bundle override
    bundle_override = {'args': ['--model', 'gpt-6-astra', '--effort', 'max']}
    assert p.launcher_metadata('codex', bundle_override) == {
        'launcher_provider': 'codex',
        'launcher_model': 'gpt-6-astra',
        'launcher_effort': 'max',
    }


def test_admission_payload_matching(tmp_path, monkeypatch):
    os.chmod(tmp_path, 0o700)
    m_dir = tmp_path / 'managed-engines'
    m_dir.mkdir(mode=0o700, parents=True)
    b = bundle()
    monkeypatch.setattr(p, 'root', lambda: m_dir)
    monkeypatch.setattr(p, 'host_request', lambda m, p, pl: {'launch_allowed': True, 'profile_supported': True})
    # Intake 59a6f44a: host-side Codex gate is authoritative; synthetic test
    # bundles have no real quota, so approve the gate explicitly here.
    monkeypatch.setattr(p, 'hub_launch_gate', lambda label, request=None: (True, 'ok'))

    # Prepare bundle on disk
    dest = m_dir / f"{b['label']}-{b['revision']}"
    dest.mkdir(mode=0o700, parents=True)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(m_dir / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']}])

    # Valid payload
    payload_valid = json.dumps({
        'profile': 'hub-test-account',
        'account': 'test-account',
        'selection': {'model': 'gpt-6.1-sol', 'effort': 'medium'},
    })
    import io, sys
    monkeypatch.setattr(sys, 'stdin', io.StringIO(payload_valid))
    monkeypatch.setattr(sys, 'argv', ['admission', 'test-account'])
    out_valid = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out_valid)
    p.admission()
    res = json.loads(out_valid.getvalue())
    assert res['launch_allowed'] is True
    assert res['model_supported'] is True

    # Profile mismatch
    payload_mismatch = json.dumps({
        'profile': 'hub-wrong-account',
        'account': 'test-account',
    })
    monkeypatch.setattr(sys, 'stdin', io.StringIO(payload_mismatch))
    monkeypatch.setattr(sys, 'argv', ['admission', 'test-account'])
    out_mismatch = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out_mismatch)
    p.admission()
    res = json.loads(out_mismatch.getvalue())
    assert res['launch_allowed'] is False
    assert res['reason'] == 'profile_mismatch'


def test_codex_protected_guard_behavioral_regression(tmp_path, monkeypatch):
    """Behavioral regression test for Codex protected quota guard.

    1. Configure trusted test wrapper to record invocation then reject;
       assert managed Codex provider binary is NEVER executed.
    2. Ensure real pinned Codex binary is executed after guard approval.
    3. Diagnostic --version lifecycle bypasses quota gate and executes binary.
    4. Active host catalog vs default catalog resolution and env isolation.
    """
    os.chmod(tmp_path, 0o700)
    active_m_dir = tmp_path / 'active-managed-engines'
    active_m_dir.mkdir(mode=0o700, parents=True)
    default_m_dir = tmp_path / 'default-managed-engines'
    default_m_dir.mkdir(mode=0o700, parents=True)

    b = bundle()
    b['provider'] = 'codex'
    b['label'] = 'codex-acc'
    dest = active_m_dir / f"{b['label']}-{b['revision']}"
    dest.mkdir(mode=0o700, parents=True)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(active_m_dir / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']}])

    # Point AGENT_HUB_MANAGED_DIR to active_m_dir, while default_m_dir is empty
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(active_m_dir))
    monkeypatch.setenv('HUB_CONFIG_FILE', str(default_m_dir.parent / 'host.json'))
    monkeypatch.setenv('HUB_TOKEN', 'secret-host-token')

    # Case 1: Guard rejects invocation (below the configured reserve)
    calls_execve = []
    def fake_execve(cmd, argv, env):
        calls_execve.append((cmd, argv, env))

    monkeypatch.setattr(os, 'execve', fake_execve)

    def fake_hub_reject(label, request=None):
        return False, 'quota_exhausted'

    monkeypatch.setattr(p, 'hub_launch_gate', fake_hub_reject)
    monkeypatch.setattr(sys, 'argv', ['execute', 'codex-acc', 'run', 'prompt'])

    with pytest.raises(SystemExit) as exc:
        p.execute()
    assert exc.value.code == 75
    # Assert provider binary was NEVER executed
    assert len(calls_execve) == 0

    # Case 2: Diagnostic --version bypasses quota gate
    gate_called = []
    def fake_gate_tracker(label, request=None):
        gate_called.append(True)
        return False, 'quota_exhausted'

    monkeypatch.setattr(p, 'hub_launch_gate', fake_gate_tracker)
    monkeypatch.setattr(sys, 'argv', ['execute', 'codex-acc', '--version'])

    p.execute()
    assert len(gate_called) == 0  # bypassed!
    assert len(calls_execve) == 1
    cmd, argv, env = calls_execve[0]
    assert '--version' in argv
    calls_execve.clear()

    # A task argument that resembles a diagnostic flag must still be gated.
    monkeypatch.setattr(sys, 'argv', ['execute', 'codex-acc', 'exec', '--help'])
    with pytest.raises(SystemExit) as exc:
        p.execute()
    assert exc.value.code == 75
    assert len(gate_called) == 1
    assert not calls_execve

    # Case 3: Guard approves invocation (remaining above the configured reserve)
    def fake_hub_approve(label, request=None):
        return True, 'ok'

    monkeypatch.setattr(p, 'hub_launch_gate', fake_hub_approve)
    monkeypatch.setattr(sys, 'argv', ['execute', 'codex-acc', 'exec', 'task-prompt'])

    p.execute()
    assert len(calls_execve) == 1
    cmd, argv, env = calls_execve[0]
    assert cmd == str(dest / 'engine/node_modules/.bin/codex')
    assert 'task-prompt' in argv
    # Check strict env isolation: host secrets removed
    assert 'HUB_CONFIG_FILE' not in env
    assert 'HUB_TOKEN' not in env
    assert 'HUB_JOB_TOKEN' not in env
    assert 'AGENT_HUB_MANAGED_DIR' not in env
    calls_execve.clear()

    # Case 4: Wrong label or revision raises ValueError
    monkeypatch.setattr(sys, 'argv', ['execute', 'unknown-acc'])
    with pytest.raises(ValueError, match='Managed profile removed'):
        p.execute()


def test_gemini_launcher_metadata_defaults_to_native_gemini_2_5_pro():
    meta = p.launcher_metadata('gemini', {'args': []})
    assert meta['launcher_provider'] == 'gemini'
    assert meta['launcher_model'] == 'gemini-2.5-pro'


def test_claude_engine_environment_clears_ambient_cloud_and_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CODE_USE_BEDROCK', '1')
    monkeypatch.setenv('CLAUDE_CODE_USE_VERTEX', '1')
    monkeypatch.setenv('CLAUDE_CODE_USE_FOUNDRY', '1')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-ambient')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'token-ambient')
    monkeypatch.setenv('ANTHROPIC_CUSTOM_HEADERS', 'h1')

    b = {
        'provider': 'claude',
        'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'exact-portable-token'},
    }
    env = p.engine_environment(b, tmp_path / 'home', dest=tmp_path)
    assert env['CLAUDE_CODE_OAUTH_TOKEN'] == 'exact-portable-token'
    assert 'CLAUDE_CODE_USE_BEDROCK' not in env
    assert 'CLAUDE_CODE_USE_VERTEX' not in env
    assert 'CLAUDE_CODE_USE_FOUNDRY' not in env
    assert 'ANTHROPIC_API_KEY' not in env
    assert 'ANTHROPIC_AUTH_TOKEN' not in env
    assert 'ANTHROPIC_CUSTOM_HEADERS' not in env


def test_claude_uses_native_claude_quota(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    b = bundle()
    b['provider'] = 'claude'
    b['env'] = {'CLAUDE_CODE_OAUTH_TOKEN': 'tok-c'}
    state = [{'label': b['label'], 'revision': b['revision'], 'provider': 'claude'}]
    p.write_json(root_dir / 'state.json', state)
    inst_dir = root_dir / f"{b['label']}-{b['revision']}"
    inst_dir.mkdir(mode=0o700)
    p.write_json(inst_dir / 'bundle.json', b)

    mock_res = {
        'claude': {
            'status': 'ok',
            'details': {'limit_reached': False, 'source': 'native_claude_oauth_usage'},
            'windows': {'5h': {'percent_remaining': 80.0, 'reset_at': '2030-01-01T00:00:00Z'}},
        }
    }
    with patch('agent_hub_host.native_claude_quota.probe', return_value=mock_res) as mock_probe:
        res = p.quotas()
        assert len(res['accounts']) == 1
        assert res['accounts'][0]['provider'] == 'claude'
        assert mock_probe.called


def test_normalize_and_render_git_identity():
    # Direct keys
    raw1 = {"user.name": "Alexey Grigorev", "user.email": "alexey.s.grigoriev@gmail.com", "push.default": "simple"}
    norm1 = p.normalize_git_identity(raw1)
    assert norm1["user.name"] == "Alexey Grigorev"
    assert norm1["user.email"] == "alexey.s.grigoriev@gmail.com"
    assert norm1["push.default"] == "simple"

    rendered1 = p.render_gitconfig(norm1)
    assert "[user]" in rendered1
    assert "\tname = Alexey Grigorev" in rendered1
    assert "\temail = alexey.s.grigoriev@gmail.com" in rendered1
    assert "[push]" in rendered1
    assert "\tdefault = simple" in rendered1

    # Shorthand keys
    raw2 = {"name": "Alexey Grigorev", "email": "alexey.s.grigoriev@gmail.com", "signing_key": "KEY123", "push_default": "current"}
    norm2 = p.normalize_git_identity(raw2)
    assert norm2["user.name"] == "Alexey Grigorev"
    assert norm2["user.email"] == "alexey.s.grigoriev@gmail.com"
    assert norm2["user.signingkey"] == "KEY123"
    assert norm2["push.default"] == "current"

    # Nested section mapping
    raw3 = {"user": {"name": "Alexey Grigorev", "email": "alexey.s.grigoriev@gmail.com"}, "push": {"default": "matching"}}
    norm3 = p.normalize_git_identity(raw3)
    assert norm3["user.name"] == "Alexey Grigorev"
    assert norm3["user.email"] == "alexey.s.grigoriev@gmail.com"
    assert norm3["push.default"] == "matching"


def test_provision_managed_home_and_rebootstrap(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)

    configured_git = {
        "user.name": "Alexey Grigorev",
        "user.email": "alexey.s.grigoriev@gmail.com",
        "push.default": "simple",
    }
    monkeypatch.setattr(p, 'load_git_identity', lambda: configured_git)

    b = bundle()
    dest = root_dir / f"{b['label']}-{b['revision']}"
    dest.mkdir(parents=True, mode=0o700)
    home = dest / 'home'

    # Initial home provisioning
    p.provision_managed_home(b, home, dest)
    gitconfig = home / '.gitconfig'
    assert gitconfig.is_file()
    content = gitconfig.read_text(encoding='utf-8')
    assert 'name = Alexey Grigorev' in content
    assert 'email = alexey.s.grigoriev@gmail.com'
    assert 'default = simple' in content
    assert oct(gitconfig.stat().st_mode & 0o777) == oct(0o600)

    # Re-apply on home recreation / re-bootstrap even if bundle.json matches receipt
    p.write_json(dest / 'bundle.json', b)
    gitconfig.unlink()
    assert not gitconfig.exists()
    p.install(b)
    assert gitconfig.is_file()
    assert 'name = Alexey Grigorev' in gitconfig.read_text(encoding='utf-8')


def test_engine_environment_fallback_git_env(tmp_path, monkeypatch):
    configured_git = {
        "user.name": "Alexey Grigorev",
        "user.email": "alexey.s.grigoriev@gmail.com",
    }
    monkeypatch.setattr(p, 'load_git_identity', lambda: configured_git)

    b = bundle()
    env = p.engine_environment(b, tmp_path / 'home', dest=tmp_path)
    assert env['GIT_AUTHOR_NAME'] == 'Alexey Grigorev'
    assert env['GIT_AUTHOR_EMAIL'] == 'alexey.s.grigoriev@gmail.com'
    assert env['GIT_COMMITTER_NAME'] == 'Alexey Grigorev'
    assert env['GIT_COMMITTER_EMAIL'] == 'alexey.s.grigoriev@gmail.com'


def test_regression_bootstrapped_engine_home_can_git_commit(tmp_path, monkeypatch):
    """AC (d): A bootstrapped fresh engine home can git commit with the configured identity."""
    tmp_path.chmod(0o700)
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)

    configured_git = {
        "user.name": "Alexey Grigorev",
        "user.email": "alexey.s.grigoriev@gmail.com",
    }
    monkeypatch.setattr(p, 'load_git_identity', lambda: configured_git)

    b = bundle()
    dest = root_dir / f"{b['label']}-{b['revision']}"
    dest.mkdir(parents=True, mode=0o700)
    home = dest / 'home'

    p.provision_managed_home(b, home, dest)
    assert (home / '.gitconfig').is_file()

    # Create a fresh temporary git repository
    repo_dir = tmp_path / 'test-repo'
    repo_dir.mkdir(mode=0o700)

    # Isolated environment: HOME pointing to the engine home, no system config overriding it
    test_env = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        'HOME': str(home),
        'USERPROFILE': str(home),
        'GIT_CONFIG_NOSYSTEM': '1',  # Ignore system gitconfig to prove identity comes from engine home
    }

    subprocess.run(['git', 'init'], cwd=str(repo_dir), env=test_env, check=True, capture_output=True)
    (repo_dir / 'test.txt').write_text('hello world\n')
    subprocess.run(['git', 'add', 'test.txt'], cwd=str(repo_dir), env=test_env, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'initial commit'], cwd=str(repo_dir), env=test_env, check=True, capture_output=True)

    log_res = subprocess.run(['git', 'log', '-1', '--format=%an <%ae>'], cwd=str(repo_dir), env=test_env, check=True, capture_output=True, text=True)
    author = log_res.stdout.strip()
    assert author == 'Alexey Grigorev <alexey.s.grigoriev@gmail.com>'

    # Also test fallback with environment variables when .gitconfig is deleted
    (home / '.gitconfig').unlink()
    env_with_fallback = p.engine_environment(b, home, dest)
    env_with_fallback['GIT_CONFIG_NOSYSTEM'] = '1'

    (repo_dir / 'test2.txt').write_text('second file\n')
    subprocess.run(['git', 'add', 'test2.txt'], cwd=str(repo_dir), env=env_with_fallback, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'second commit'], cwd=str(repo_dir), env=env_with_fallback, check=True, capture_output=True)

    log_res2 = subprocess.run(['git', 'log', '-1', '--format=%an <%ae>'], cwd=str(repo_dir), env=env_with_fallback, check=True, capture_output=True, text=True)
    assert log_res2.stdout.strip() == 'Alexey Grigorev <alexey.s.grigoriev@gmail.com>'


def test_managed_home_bootstrap_installs_aplexer_state_hooks(tmp_path, monkeypatch):
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    b = bundle()
    dest = root_dir / f"{b['label']}-{b['revision']}"

    with patch.object(p.subprocess, 'run', side_effect=installer):
        p.install(b)

    codex_hooks = dest / 'home' / '.codex' / 'hooks.json'
    assert codex_hooks.is_file()
    assert codex_hooks.stat().st_mode & 0o777 == 0o600
    data = json.loads(codex_hooks.read_text())
    assert 'hooks' in data
    # Stop hook reports idle
    stop_cmds = [h['command'] for hook_grp in data['hooks'].get('Stop', []) for h in hook_grp.get('hooks', [])]
    assert any('state-report idle' in cmd for cmd in stop_cmds)
    # SessionStart / UserPromptSubmit report working
    start_cmds = [h['command'] for hook_grp in data['hooks'].get('SessionStart', []) for h in hook_grp.get('hooks', [])]
    assert any('state-report working' in cmd for cmd in start_cmds)


def test_managed_home_rebootstrap_reapplies_state_hooks(tmp_path, monkeypatch):
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    b = bundle()
    dest = root_dir / f"{b['label']}-{b['revision']}"

    with patch.object(p.subprocess, 'run', side_effect=installer):
        p.install(b)

    codex_hooks = dest / 'home' / '.codex' / 'hooks.json'
    assert codex_hooks.is_file()

    # Delete the hooks file to simulate drift or corruption
    codex_hooks.unlink()
    assert not codex_hooks.exists()

    # Re-bootstrap (install again with same receipt)
    p.install(b)
    assert codex_hooks.is_file()
    data = json.loads(codex_hooks.read_text())
    stop_cmds = [h['command'] for hook_grp in data['hooks'].get('Stop', []) for h in hook_grp.get('hooks', [])]
    assert any('state-report idle' in cmd for cmd in stop_cmds)


def test_set_toml_top_level_keys():
    import tomllib

    # 1. Empty content
    res1 = p.set_toml_top_level_keys("", {"approval_policy": "never", "sandbox_mode": "danger-full-access"})
    assert 'approval_policy = "never"' in res1
    assert 'sandbox_mode = "danger-full-access"' in res1

    # 2. Existing [subtable] - keys must be inserted before subtable
    orig2 = '[projects."/test"]\ntrust_level = "trusted"\n'
    res2 = p.set_toml_top_level_keys(orig2, {"approval_policy": "never", "sandbox_mode": "danger-full-access"})
    assert 'approval_policy = "never"' in res2
    assert 'sandbox_mode = "danger-full-access"' in res2
    assert '[projects."/test"]' in res2
    # Verify keys appear before the section header
    sec_idx = res2.index('[projects."/test"]')
    assert res2.index('approval_policy = "never"') < sec_idx
    assert res2.index('sandbox_mode = "danger-full-access"') < sec_idx

    # 3. Existing top-level keys updated
    orig3 = 'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\nmodel = "gpt-5"\n'
    res3 = p.set_toml_top_level_keys(orig3, {"approval_policy": "never", "sandbox_mode": "danger-full-access"})
    assert 'approval_policy = "never"' in res3
    assert 'sandbox_mode = "danger-full-access"' in res3
    assert 'approval_policy = "on-request"' not in res3
    assert 'sandbox_mode = "workspace-write"' not in res3
    assert 'model = "gpt-5"' in res3

    typed = p.set_toml_top_level_keys('approval_policy = "never"\n', {
        'check_for_update_on_startup': False,
    })
    parsed = tomllib.loads(typed)
    assert parsed['check_for_update_on_startup'] is False
    assert parsed['approval_policy'] == 'never'

    array_tables = ('[[profiles]]\nname = "one"\n\n[[profiles]]\nname = "two"\n')
    array_update = p.set_toml_top_level_keys(array_tables, {'check_for_update_on_startup': False})
    assert tomllib.loads(array_update) == {
        'check_for_update_on_startup': False,
        'profiles': [{'name': 'one'}, {'name': 'two'}],
    }

    windows_path = p.set_toml_top_level_keys('model = "gpt-5"\n', {
        'model': r'C:\Users\test',
    })
    assert tomllib.loads(windows_path)['model'] == r'C:\Users\test'


@pytest.mark.parametrize(('provider', 'config_path'), [
    ('codex', '.codex/config.toml'),
    ('zai', '.zcodex/config.toml'),
])
def test_managed_codex_config_keeps_update_check_boolean_type(tmp_path, provider, config_path):
    import tomllib

    home = tmp_path / 'home'
    config = home / config_path
    config.parent.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    # Reconcile a previously string-valued setting and keep the disable flag
    # boolean on subsequent syncs.
    p.private_fs.atomic_write(config, b'check_for_update_on_startup = "true"\n', 131072)

    for _ in range(2):
        p.install_managed_engine_permissions({'provider': provider}, home)
        parsed = tomllib.loads(config.read_text())
        assert parsed['check_for_update_on_startup'] is False
        assert parsed['approval_policy'] == 'never'
        assert parsed['sandbox_mode'] == 'danger-full-access'


@pytest.mark.parametrize(('provider', 'config_path'), [
    ('codex', '.codex/config.toml'),
    ('zai', '.zcodex/config.toml'),
])
def test_rollout_self_update_disable_writes_and_verifies_toml_boolean(tmp_path, provider, config_path):
    import tomllib
    from agent_hub_host import engine_rollout

    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    config = home / config_path
    config.parent.mkdir(mode=0o700)
    config.write_text('check_for_update_on_startup = "false"\n')
    config.chmod(0o600)

    assert not engine_rollout.self_update_disabled_ok(home, provider)
    engine_rollout.ensure_self_update_disabled(home, provider)
    parsed = tomllib.loads(config.read_text())
    assert parsed['check_for_update_on_startup'] is False
    assert engine_rollout.self_update_disabled_ok(home, provider)


def test_provision_managed_home_permissions_for_all_engines(tmp_path, monkeypatch):
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)

    # 1. Codex: ensures .codex/config.toml has approval_policy="never" and sandbox_mode="danger-full-access"
    b_codex = bundle()
    b_codex['provider'] = 'codex'
    b_codex['label'] = 'test-codex'
    dest_codex = root_dir / f"{b_codex['label']}-{b_codex['revision']}"
    home_codex = dest_codex / 'home'
    p.provision_managed_home(b_codex, home_codex, dest_codex)

    codex_conf = home_codex / '.codex' / 'config.toml'
    assert codex_conf.is_file()
    assert oct(codex_conf.stat().st_mode & 0o777) == oct(0o600)
    text = codex_conf.read_text(encoding='utf-8')
    assert 'approval_policy = "never"' in text
    assert 'sandbox_mode = "danger-full-access"' in text

    # Simulate existing config.toml with projects and re-provision (reconcile)
    codex_conf.write_text('[projects."/workspace"]\ntrust_level = "trusted"\n')
    p.provision_managed_home(b_codex, home_codex, dest_codex)
    text2 = codex_conf.read_text(encoding='utf-8')
    assert 'approval_policy = "never"' in text2
    assert 'sandbox_mode = "danger-full-access"' in text2
    assert '[projects."/workspace"]' in text2

    # 2. Z.ai (zcodex): ensures .zcodex/config.toml has approval_policy="never" and sandbox_mode="danger-full-access"
    b_zai = bundle()
    b_zai['provider'] = 'zai'
    b_zai['label'] = 'test-zai'
    dest_zai = root_dir / f"{b_zai['label']}-{b_zai['revision']}"
    home_zai = dest_zai / 'home'
    p.provision_managed_home(b_zai, home_zai, dest_zai)

    zcodex_conf = home_zai / '.zcodex' / 'config.toml'
    assert zcodex_conf.is_file()
    assert oct(zcodex_conf.stat().st_mode & 0o777) == oct(0o600)
    ztext = zcodex_conf.read_text(encoding='utf-8')
    assert 'approval_policy = "never"' in ztext
    assert 'sandbox_mode = "danger-full-access"' in ztext

    # 3. Claude: ensures .claude/settings.json has skipDangerousModePermissionPrompt=True
    b_claude = bundle()
    b_claude['provider'] = 'claude'
    b_claude['label'] = 'test-claude'
    dest_claude = root_dir / f"{b_claude['label']}-{b_claude['revision']}"
    home_claude = dest_claude / 'home'
    p.provision_managed_home(b_claude, home_claude, dest_claude)

    claude_settings = home_claude / '.claude' / 'settings.json'
    assert claude_settings.is_file()
    assert oct(claude_settings.stat().st_mode & 0o777) == oct(0o600)
    claude_data = json.loads(claude_settings.read_text(encoding='utf-8'))
    assert claude_data.get('skipDangerousModePermissionPrompt') is True

    # 4. OpenCode (go): ensures .config/opencode/opencode.json has permission.* = "allow"
    b_go = bundle()
    b_go['provider'] = 'go'
    b_go['label'] = 'test-go'
    dest_go = root_dir / f"{b_go['label']}-{b_go['revision']}"
    home_go = dest_go / 'home'
    p.provision_managed_home(b_go, home_go, dest_go)

    opencode_conf = home_go / '.config' / 'opencode' / 'opencode.json'
    assert opencode_conf.is_file()
    assert oct(opencode_conf.stat().st_mode & 0o777) == oct(0o600)
    op_data = json.loads(opencode_conf.read_text(encoding='utf-8'))
    assert op_data.get('permission', {}).get('*') == 'allow'


def test_execute_bypasses_permissions_for_goal_and_interactive(tmp_path, monkeypatch):
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    monkeypatch.delenv('CODEX_LAUNCH_WRAPPER', raising=False)

    b = bundle()
    b['provider'] = 'codex'
    b['label'] = 'my-codex'
    dest = root_dir / f"{b['label']}-{b['revision']}"
    dest.mkdir(parents=True, mode=0o700)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(root_dir / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']}])

    calls = []
    monkeypatch.setattr(p.os, 'execve', lambda cmd, argv, env: calls.append((cmd, argv, env)))
    monkeypatch.setattr(p, 'hub_launch_gate', lambda label, request=None: (True, 'ok'))

    # Case 1: --with-goal execution appends --dangerously-bypass-approvals-and-sandbox
    monkeypatch.setattr(sys, 'argv', ['execute', 'my-codex', '/goal Fix the flaky binding'])
    p.execute()
    assert len(calls) == 1
    cmd, argv, env = calls[0]
    assert '--dangerously-bypass-approvals-and-sandbox' in argv
    assert argv[-1] == '/goal Fix the flaky binding'
    # Verify config.toml in home was also provisioned with approval_policy="never"
    cfg_text = (dest / 'home' / '.codex' / 'config.toml').read_text()
    assert 'approval_policy = "never"' in cfg_text
    assert 'sandbox_mode = "danger-full-access"' in cfg_text
    cwd_str = str(Path(os.getcwd()).resolve())
    assert f'[projects."{cwd_str}"]' in cfg_text or f'[projects.\"{cwd_str}\"]' in cfg_text
    assert 'trust_level = "trusted"' in cfg_text
    calls.clear()

    # Case 2: Interactive launch that already has bypass flag does not duplicate it
    monkeypatch.setattr(sys, 'argv', ['execute', 'my-codex', '--dangerously-bypass-approvals-and-sandbox'])
    p.execute()
    assert len(calls) == 1
    cmd, argv, env = calls[0]
    assert argv.count('--dangerously-bypass-approvals-and-sandbox') == 1
    calls.clear()

    # Case 2b: headless `exec` also gets the flag, before the subcommand
    monkeypatch.setattr(sys, 'argv', ['execute', 'my-codex', 'exec', '--json', 'task'])
    p.execute()
    cmd, argv, env = calls[0]
    assert argv.index('--dangerously-bypass-approvals-and-sandbox') < argv.index('exec')
    calls.clear()

    # Case 3: Diagnostic --version does NOT append bypass flag
    monkeypatch.setattr(sys, 'argv', ['execute', 'my-codex', '--version'])
    p.execute()
    assert len(calls) == 1
    cmd, argv, env = calls[0]
    assert '--dangerously-bypass-approvals-and-sandbox' not in argv
    assert argv[-1] == '--version'
    calls.clear()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX headless output wrapper')
def test_managed_headless_capture_releases_provisioning_lock_and_keeps_scrubbed_env(tmp_path, monkeypatch):
    from agent_hub_host import headless_output
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    b = bundle()
    dest = root_dir / (b['label'] + '-' + b['revision'])
    dest.mkdir(mode=0o700)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(root_dir / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']}])
    native = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    offer = '11111111-2222-4333-8444-555555555555'
    monkeypatch.setenv('APLEXER_SESSION_ID', native)
    monkeypatch.setenv('APLEXER_TAG', 'task-hub-' + offer)
    monkeypatch.setenv('APLEXER_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('HUB_TOKEN', 'operator-synthetic-token')
    monkeypatch.setenv('HUB_CONFIG_FILE', '/operator/config.json')
    monkeypatch.setattr(p, 'hub_launch_gate', lambda label, request=None: (True, 'ok'))
    # A real child must acquire this same lock immediately. Keeping the parent
    # lock throughout capture would block all other managed launches.
    code = ('import os,fcntl; '
            'fd=os.open(' + repr(str(root_dir / 'reconcile.lock')) + ',os.O_RDWR); '
            'fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); '
            'assert "HUB_TOKEN" not in os.environ; '
            'assert "HUB_CONFIG_FILE" not in os.environ; '
            'print("headless child acquired released lock",flush=True); raise SystemExit(3)')
    monkeypatch.setattr(p, 'engine_argv', lambda *_: [sys.executable, '-c', code])
    monkeypatch.setattr(sys, 'argv', ['execute', b['label'], '/goal small real task'])
    with pytest.raises(SystemExit) as ended:
        p.execute()
    assert ended.value.code == 3
    path = tmp_path / '.local/hub-headless-output' / (offer + '.json')
    value = json.loads(path.read_text())
    assert value['engine'] == 'codex'
    assert value['native_id'] == native
    assert value['capture_state'] == 'complete'
    assert 'acquired released lock' in value['log_tail']
    assert 'operator-synthetic-token' not in path.read_text()


def test_ensure_toml_trusted_projects():
    initial = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n\n[projects."/home/alexey"]\ntrust_level = "trusted"\n'
    res = p.ensure_toml_trusted_projects(initial, ['/home/alexey', '/tmp/foo/bar', '/home/alexey/git'])
    assert '[projects."/home/alexey"]' in res
    assert '[projects."/tmp/foo/bar"]' in res
    assert '[projects."/home/alexey/git"]' in res
    # Ensure idempotency
    res2 = p.ensure_toml_trusted_projects(res, ['/tmp/foo/bar'])
    assert res2.count('/tmp/foo/bar') == 1


def test_ensure_toml_trusted_hooks(tmp_path):
    initial = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
    hooks_file = tmp_path / 'hooks.json'
    hooks_doc = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'a state-report working'}]}]}}
    p.private_fs.atomic_write(hooks_file, json.dumps(hooks_doc).encode('utf-8'), 131072)
    res = p.ensure_toml_trusted_hooks(initial, hooks_file)
    trust = p.hook_trust.entries(str(hooks_file.resolve()), hooks_doc)
    assert len(trust) == 1
    for key, value in trust.items():
        assert json.dumps(key) in res
        assert f'trusted_hash = "{value}"' in res
    # Ensure idempotency
    res2 = p.ensure_toml_trusted_hooks(res, hooks_file)
    assert res2 == res


def test_trust_refresh_removes_stale_hashes_when_generated_hooks_become_empty(tmp_path):
    hooks_file = tmp_path / 'hooks.json'
    p.private_fs.atomic_write(hooks_file, json.dumps({'hooks': {}}).encode('utf-8'), 131072)
    key = str(hooks_file.resolve()) + ':SessionStart:0:0'
    stale = ('approval_policy = "never"\n\n'
             f'[hooks.state.{json.dumps(key)}]\n'
             'trusted_hash = "sha256:stale-generated-hash"\n')
    refreshed = p.ensure_toml_trusted_hooks(stale, hooks_file)
    assert refreshed == 'approval_policy = "never"\n'
    assert 'stale-generated-hash' not in refreshed


def test_sync_rotation_cleans_readonly_go_mod_cache_and_preserves_profile(tmp_path, monkeypatch):
    """Regression c4973943: Go module cache 0555/0444 inside managed home must not abort sync() rotation."""
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)

    b1 = bundle('11111111-1111-4111-8111-111111111111')
    b1['label'] = 'shared-codex'
    desired = [b1]

    def request(method, path, payload=None):
        return {'engines': desired} if method == 'GET' else {'status': 'installed'}

    with patch.object(p.subprocess, 'run', side_effect=installer):
        assert p.sync(request)['profiles'] == ['hub-shared-codex']
        old_dir = root_dir / f"shared-codex-{b1['revision']}"
        mod_dir = old_dir / 'home' / 'go' / 'pkg' / 'mod' / 'github.com' / 'gorilla' / 'websocket@v1.5.3'
        mod_dir.mkdir(parents=True, mode=0o700)
        ro_file = mod_dir / 'conn.go'
        ro_file.write_text('package websocket\n')
        ro_file.chmod(0o444)
        mod_dir.chmod(0o555)

        b2 = bundle('22222222-2222-4222-8222-222222222222')
        b2['label'] = 'shared-codex'
        desired[:] = [b2]
        assert p.sync(request)['profiles'] == ['hub-shared-codex']
        assert not old_dir.exists()
        assert (root_dir / f"shared-codex-{b2['revision']}" / 'bundle.json').is_file()
        assert '[profiles.hub-shared-codex]' in p.profile_path().read_text()


def test_admission_self_heals_missing_aplexer_profile_from_installed_state(tmp_path, monkeypatch, capsys):
    """Regression c4973943: if config.toml lost [profiles.hub-<label>], admission self-heals it from state.json."""
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)

    b1 = bundle('33333333-3333-4333-8333-333333333333')
    b1['label'] = 'shared-codex'
    with patch.object(p.subprocess, 'run', side_effect=installer):
        p.sync(lambda method, path, payload=None: {'engines': [b1]} if method == 'GET' else {'status': 'installed'})

    # Simulate wiped profile section in config.toml
    p.profile_path().write_text('version = 1\n[profiles.operator]\nengine = "shell"\n')
    assert '[profiles.hub-shared-codex]' not in p.profile_path().read_text()

    monkeypatch.setattr(p, 'host_request', lambda m, path, payload=None: {
        'launch_allowed': True,
        'profile_supported': True,
        'account_label': 'shared-codex',
        'observed_at': datetime.now(timezone.utc).isoformat(),
    })
    monkeypatch.setattr(p, 'hub_launch_gate', lambda label, request=None: (True, 'ok'))
    monkeypatch.setattr(sys, 'argv', ['admission', 'shared-codex'])
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    p.admission()
    out = json.loads(capsys.readouterr().out)
    assert out['launch_allowed'] is True
    assert out['profile_supported'] is True
    assert '[profiles.hub-shared-codex]' in p.profile_path().read_text()










def test_engine_rewritten_config_toml_is_accepted_and_preserved_6cbb862a(tmp_path):
    """Regression 6cbb862a: codex rewrites config.toml with its own umask/ACL; install must not fail or drop it."""
    home = tmp_path / 'home'
    codex = home / '.codex'
    codex.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    cfg = codex / 'config.toml'
    p.install_managed_engine_permissions({'provider': 'codex'}, home)
    cfg.write_text(cfg.read_text() + '\n[notice]\nhide_rate_limit_model_nudge = true\n')
    cfg.chmod(0o644)  # engine replaced the file with its default umask
    p.install_managed_engine_permissions({'provider': 'codex'}, home)
    assert 'hide_rate_limit_model_nudge = true' in cfg.read_text()
    assert 'approval_policy = "never"' in cfg.read_text()
    assert cfg.stat().st_mode & 0o777 == 0o600


def test_unchanged_engine_configs_are_not_rewritten_6cbb862a(tmp_path):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    p.install_managed_engine_permissions(None, home)
    with patch.object(p.private_fs, 'atomic_write') as write:
        p.install_managed_engine_permissions(None, home)
    assert write.call_args_list == []


def test_windows_inherited_owner_only_acl_falls_back_to_verified_read_6cbb862a(tmp_path):
    target = tmp_path / 'config.toml'
    calls = []
    def read_bytes(path, limit, *, private=True):
        calls.append(private)
        if private:
            raise PermissionError('Private owner/protected DACL required')
        return b'model = "x"\n'
    with patch.object(p.private_fs, 'read_bytes', side_effect=read_bytes), patch.object(p.os, 'name', 'nt'):
        assert p.read_engine_file(target) == b'model = "x"\n'
        assert p.preserve_managed_file(target) is True
    assert calls == [True, False, True, False]
    # A foreign owner or extra ACE still fails closed.
    with patch.object(p.private_fs, 'read_bytes', side_effect=PermissionError('Unexpected or inherited private ACE')), \
         patch.object(p.os, 'name', 'nt'):
        with pytest.raises(PermissionError):
            p.read_engine_file(target)


def test_failed_reinstall_keeps_offered_bundle_dir_and_logs_type_only_6cbb862a(tmp_path, monkeypatch, capsys):
    """Regression 6cbb862a: a failed install must not rmtree an offered bundle under a live engine."""
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    shared, orin = bundle('44444444-4444-4444-8444-444444444444'), bundle('55555555-5555-4555-8555-555555555555')
    shared['label'], orin['label'] = 'shared-codex', 'codex-orin'
    desired = [shared, orin]

    def request(method, path, payload=None):
        return {'engines': desired} if method == 'GET' else {'status': 'installed'}

    with patch.object(p.subprocess, 'run', side_effect=installer):
        assert sorted(p.sync(request)['profiles']) == ['hub-codex-orin', 'hub-shared-codex']
    shared_dir = root_dir / f"shared-codex-{shared['revision']}"
    engine_file = shared_dir / 'engine' / 'node_modules' / '.bin' / 'codex'
    real_install = p.install

    def flaky(b, request=None):
        if b['label'] == 'shared-codex':
            raise PermissionError('C:\\secret\\path Private owner/protected DACL required')
        return real_install(b, request)

    with patch.object(p, 'install', side_effect=flaky):
        result = p.sync(request)
        assert result['failed_labels'] == ['shared-codex']
        p.sync(request)  # the next heartbeat must not delete the engine either
    assert engine_file.is_file()
    err = capsys.readouterr().err
    assert '"managed_install_failed": "shared-codex"' in err and 'PermissionError' in err
    assert 'secret' not in err
    with patch.object(p.subprocess, 'run', side_effect=installer):
        assert sorted(p.sync(request)['profiles']) == ['hub-codex-orin', 'hub-shared-codex']
        desired[:] = [orin]  # Hub withdrew the account: only then is the dir removed
        assert p.sync(request)['profiles'] == ['hub-codex-orin']
    assert not shared_dir.exists()


def test_codex_config_merge_is_idempotent_across_crlf_and_quoting(tmp_path):
    import tomllib

    hooks = tmp_path / 'hooks.json'
    hooks_doc = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'a state-report idle'}]}]}}
    p.private_fs.atomic_write(hooks, json.dumps(hooks_doc).encode('utf-8'), 131072)
    paths = ['/a/b', '/c']
    # Codex rewrote the file: CRLF endings and a literal-quoted table.
    base = ('approval_policy = "never"\r\n\r\n[projects."/a/b"]\r\ntrust_level = "untrusted"\r\n'
            '\r\n[projects.\'/c\']\r\ntrust_level = "trusted"\r\n')

    def run(text):
        out = p.ensure_toml_trusted_projects(text, paths)
        return p.ensure_toml_trusted_hooks(out, hooks)

    once = run(base)
    assert run(once) == once
    data = tomllib.loads(once)
    assert data['projects'][str(Path('/a/b').resolve())]['trust_level'] == 'trusted'
    assert len(data['hooks']['state']) == len(p.hook_trust.entries(str(hooks.resolve()), hooks_doc))


def test_repair_toml_duplicate_tables_fixes_already_broken_config():
    import tomllib

    broken = ('[projects."C:\\\\x"]\ntrust_level = "trusted"\n\n[hooks.state]\n\n[tui]\na = 1\n\n'
              '[projects."C:\\\\x"]\ntrust_level = "trusted"\n\n[hooks.state]\n')
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(broken)
    fixed = p.repair_toml_duplicate_tables(broken.replace('\n', '\r\n'))
    assert '\r' not in fixed
    assert tomllib.loads(fixed) == {
        'projects': {'C:\\x': {'trust_level': 'trusted'}},
        'hooks': {'state': {}},
        'tui': {'a': 1},
    }


@pytest.mark.parametrize('multiline', [
    'description = """\n[tui]\nmodel = "inside string"\ncheck_for_update_on_startup = "false"\nnot_a_table = true\n"""\n',
    "description = '''\n[tui]\nmodel = \"inside string\"\ncheck_for_update_on_startup = \"false\"\nnot_a_table = true\n'''\n",
])
def test_repair_toml_duplicate_tables_ignores_headers_inside_multiline_strings(multiline):
    import tomllib

    content = multiline + 'model = "gpt-5"\n\n[tui]\nshow_tooltips = false\n'
    repaired = p.repair_toml_duplicate_tables(content)
    parsed = tomllib.loads(repaired)
    expected_description = '[tui]\nmodel = "inside string"\ncheck_for_update_on_startup = "false"\nnot_a_table = true\n'
    assert parsed['description'] == expected_description
    assert parsed['model'] == 'gpt-5'
    assert parsed['tui'] == {'show_tooltips': False}

    updated = p.set_toml_top_level_keys(content, {
        'model': r'C:\Users\test',
        'check_for_update_on_startup': False,
    })
    updated_parsed = tomllib.loads(updated)
    assert updated_parsed['description'] == parsed['description']
    assert updated_parsed['model'] == r'C:\Users\test'
    assert updated_parsed['tui'] == parsed['tui']


def test_set_toml_top_level_keys_replaces_multiline_value():
    import tomllib

    source = 'check_for_update_on_startup = """false\nlegacy detail\n"""\nmodel = "gpt-5"\n'
    updated = p.set_toml_top_level_keys(source, {'check_for_update_on_startup': False})
    assert tomllib.loads(updated) == {
        'check_for_update_on_startup': False,
        'model': 'gpt-5',
    }
def test_profile_command_imports_package_without_launcher_pythonpath(tmp_path, monkeypatch):
    # Intake 608a2f9a (A): hub-* profiles run outside the launcher .cmd, so the
    # -c command must carry the package parent itself instead of relying on
    # PYTHONPATH=<release>/package/host.
    managed = tmp_path / 'managed'
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(managed))
    argv = p.module_command('execute', 'test-account', managed_dir=managed)
    assert argv[0] == sys.executable and argv[1] == '-c' and argv[3] == 'test-account'
    assert str(Path(p.__file__).resolve().parents[1]) in argv[2]
    setup = argv[2].split('; from agent_hub_host', 1)[0]
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    probe = subprocess.run([sys.executable, '-c', setup + '; import agent_hub_host.provisioning'],
                           env=env, capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stderr


def _fake_windows_go_install(revision='33333333-3333-4333-8333-333333333333'):
    dest = p.root() / ('go-account-' + revision)
    package = dest / 'engine' / 'node_modules' / '@opencode' / 'cli-windows-x64'
    (package / 'bin').mkdir(parents=True, exist_ok=True)
    (package / 'package.json').write_text(json.dumps({'version': p.GO_CLI_VERSION}))
    executable = package / 'bin' / 'opencode.exe'
    executable.write_bytes(b'MZ')
    return dict(label='go-account', provider='go', version='1.0.0', revision=revision,
                files={}, env={}, args=['--model', 'opencode-go/muse-spark-1.3-contributor']), executable


@pytest.fixture(autouse=False)
def _launcher_stub(tmp_path, monkeypatch):
    # 67ae673 no longer falls back to python.exe; CI Pythons may ship no distlib t64.exe stub.
    stub = tmp_path / 't64.exe'
    stub.write_bytes(b'MZ')
    real = p._windows_exe_launcher
    monkeypatch.setattr(p, '_windows_exe_launcher', lambda command, label, stub=stub: real(command, label, stub))


def test_windows_go_bundle_pins_managed_opencode_engine(monkeypatch, _launcher_stub):
    # Intake 608a2f9a (B): aplexer's bare `opencode` resolves to the npm shim
    # on Windows (exit 127); the managed block pins the native binary and the
    # pin follows each installed revision.
    bundle, executable = _fake_windows_go_install()
    p.profiles([bundle], is_windows=True)
    text = p.profile_path().read_text()
    assert text.count('[engines.opencode]') == 1
    assert json.dumps([str(executable)]) in text
    assert '# BEGIN HUB MANAGED ENGINES' in text
    p.profiles([])  # bundle withdrawn: the pin goes with the managed block
    assert '[engines.opencode]' not in p.profile_path().read_text()


def test_go_engine_is_the_2x_cli_that_can_import_the_central_device_login(tmp_path, monkeypatch):
    # Intake 5814e109 / e0af55f3: opencode-ai 1.x has no `auth import`, so the
    # Hub-central muse login could never be installed on a host; the managed go
    # engine is the pinned @opencode/cli 2.x whatever version the bundle carries.
    assert p.PACKAGES['go'][0] == '@opencode/cli' and p.GO_CLI_VERSION == '2.0.32'
    bundle, executable = _fake_windows_go_install()
    bundle['version'] = '1.19.3'
    dest = p.root() / ('go-account-' + bundle['revision'])
    assert p.executable_path(bundle, dest, is_windows=True) == executable
    assert p._go_engine_current(bundle, dest) is True
    old_tree = p.root() / 'go-account-old' / 'engine' / 'node_modules'
    (old_tree / 'opencode-ai').mkdir(parents=True)
    assert p._go_engine_current(bundle, old_tree.parent.parent) is False  # leftover 1.x without the 2.x CLI
    (old_tree / '@opencode' / 'cli').mkdir(parents=True)
    assert p._go_engine_current(bundle, old_tree.parent.parent) is True


def test_opencode_engine_pin_absent_off_windows_and_for_operator_tables(monkeypatch, _launcher_stub):
    bundle, executable = _fake_windows_go_install()
    p.profiles([bundle], is_windows=False)  # POSIX engines resolve natively, no pin
    assert '[engines.opencode]' not in p.profile_path().read_text()
    path = p.profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('version = 1\n[engines.opencode]\ncommand = ["C:\\\\ops\\\\opencode.exe"]\n')
    p.profiles([bundle], is_windows=True)  # operator table outside our block wins
    text = path.read_text()
    assert text.count('[engines.opencode]') == 1
    assert 'C:\\\\ops\\\\opencode.exe' in text and str(executable) not in text


def test_install_fetches_windows_zcodex_release_exe(tmp_path, monkeypatch):
    # Intake 608a2f9a (C): with no native zcodex on a Windows host, the managed
    # install fetches the checksummed release exe instead of failing closed.
    import hashlib
    b = {'label': 'zai-win', 'provider': 'zai', 'version': '0.155.1',
         'revision': '88888888-8888-4888-8888-888888888888',
         'files': {}, 'env': {'ZAI_API_KEY': 'tok'}, 'args': ['--model', 'glm-5.3-flash']}
    monkeypatch.setattr(p, 'locate_native_zcodex', lambda bundle=None, is_windows=None: None)
    monkeypatch.setattr(p, 'root', lambda: tmp_path / 'managed-engines')
    monkeypatch.setattr(p, '_is_windows', lambda: True)
    monkeypatch.setattr(p, 'locate_zcode_runtime', lambda dest=None: None)
    payload = b'fake-zcodex-exe'
    sha = hashlib.sha256(payload).hexdigest()

    def fake_run(cmd, *args, **kwargs):
        cmd_str = ' '.join(str(c) for c in cmd)
        for i, arg in enumerate(cmd):
            if arg == '-o':
                out = Path(cmd[i + 1])
                if 'SHA256SUMS' in cmd_str:
                    out.write_text(f'{sha}  zcodex-windows-amd64.exe\n')
                elif any(str(c).startswith('https://github.com') and str(c).endswith('.exe') for c in cmd):
                    out.write_bytes(payload)
                return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    with pytest.raises(ValueError, match='ZCode runtime missing'):
        p.install(b)
    dest = tmp_path / 'managed-engines' / 'zai-win-88888888-8888-4888-8888-888888888888'
    assert (dest / 'engine' / 'bin' / 'zcodex.exe').read_bytes() == payload
    wrapper = (dest / 'engine' / 'bin' / 'zcodex.cmd').read_text()
    assert 'zcodex.exe' in wrapper

@pytest.mark.skipif(os.name == 'nt', reason='POSIX execve command capture')
@pytest.mark.parametrize('label', ['shared-codex', 'codex-orin'])
@pytest.mark.parametrize('model,explicit,expected', [
    ('gpt-6.1-sol', None, 'medium'),
    ('gpt-6-luna', None, 'xhigh'),
    ('gpt-6.1-sol', 'high', 'high'),
])
def test_managed_native_effort_4af7f1d6(tmp_path, monkeypatch, label, model, explicit, expected):
    managed = tmp_path / 'managed'
    managed.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: managed)
    monkeypatch.delenv('CODEX_LAUNCH_WRAPPER', raising=False)
    b = bundle()
    b.update(provider='codex', label=label, args=['-c', 'model_reasoning_effort="high"'])
    b['files']['.codex/config.toml'] = 'model_reasoning_effort = "xhigh"\n'
    dest = managed / (label + '-' + b['revision'])
    dest.mkdir(mode=0o700)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(managed / 'state.json', [{'label': label, 'revision': b['revision'], 'provider': 'codex'}])
    calls = []
    monkeypatch.setattr(p.os, 'execve', lambda cmd, argv, env: calls.append(argv))
    monkeypatch.setattr(p, 'hub_launch_gate', lambda *a: (True, 'ok'))
    args = ['execute', label, 'exec', '--model', model]
    if explicit:
        args += ['-c', 'model_reasoning_effort="' + explicit + '"']
    monkeypatch.setattr(sys, 'argv', args + ['task'])
    p.execute()
    overrides = [arg for arg in calls[0] if arg.startswith('model_reasoning_effort=')]
    assert overrides == ['model_reasoning_effort="' + expected + '"']


def test_managed_claude_execution_argv_forces_sonnet_medium(tmp_path, monkeypatch):
    managed = tmp_path / 'managed'
    managed.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: managed)
    label = 'shared-claude'
    b = {'label': label, 'provider': 'claude', 'version': '1.0.0',
         'revision': '22222222-2222-4222-8222-222222222222',
         'files': {}, 'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'synthetic'}, 'args': ['--model', 'opus', '--effort', 'high']}
    dest = managed / (label + '-' + b['revision'])
    dest.mkdir(mode=0o700)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(managed / 'state.json', [{'label': label, 'revision': b['revision'], 'provider': 'claude'}])
    monkeypatch.setattr(p, 'provision_managed_home', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(p, 'refresh_launch_credential', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(p, 'install_managed_engine_permissions', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(p, 'engine_argv', lambda *_args, **_kwargs: ['/managed/bin/claude'])
    monkeypatch.setattr(p, 'hub_launch_gate', lambda *_args: (True, 'ok'))
    calls = []
    monkeypatch.setattr(p.os, 'execve', lambda command, argv, env: calls.append((command, argv, env)))
    monkeypatch.setattr(sys, 'argv', [
        'hub', label, '--model', 'sonnet', '--effort', 'high',
        '--hub-session-effort=medium', '-p', 'synthetic prompt',
    ])

    p.execute()

    command, argv, _env = calls[0]
    assert command == '/managed/bin/claude'
    assert argv.count('--model') == 1
    assert argv.count('--effort') == 1
    assert argv[argv.index('--model') + 1] == 'sonnet'
    assert argv[argv.index('--effort') + 1] == 'medium'
    assert argv[-2:] == ['-p', 'synthetic prompt']
    assert '--hub-session-effort=medium' not in argv


def test_managed_claude_effort_marker_rejects_non_medium():
    with pytest.raises(ValueError, match='Claude routes require medium effort'):
        p.managed_claude_route_args(
            {'provider': 'claude', 'args': []}, [], ['--hub-session-effort=high'])


def test_managed_claude_route_preserves_explicit_fable_model():
    args = p.managed_claude_route_args(
        {'provider': 'claude', 'args': ['--model', 'sonnet']}, [],
        ['--model', 'fable', '--hub-session-effort=medium', '-p', 'say ok'])
    assert args[args.index('--model') + 1] == 'fable'
    assert args[args.index('--effort') + 1] == 'medium'
    assert args[-2:] == ['-p', 'say ok']


def test_antigravity_environment_drops_user_proxy_variables(tmp_path, monkeypatch):
    # Desktop: HTTPS_PROXY=127.0.0.1:8899 made agy's OAuth refresh fail ('Authentication required')
    # with a valid, Hub-identical token; the same home answered OK without the proxy env.
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY'):
        monkeypatch.setenv(key, 'http://127.0.0.1:8899')
    b = {'label': 'agy', 'provider': 'antigravity', 'revision': '66666666-6666-4666-8666-666666666666',
         'version': '1.4.12', 'files': {}, 'env': {}, 'args': []}
    env = p.engine_environment(b, root_dir / 'agy-x' / 'home', root_dir / 'agy-x')
    assert not [k for k in env if k.lower().endswith('_proxy')]
    codex = p.engine_environment(dict(b, provider='codex'), root_dir / 'c' / 'home', root_dir / 'c')
    assert codex.get('HTTPS_PROXY') == 'http://127.0.0.1:8899' or codex.get('https_proxy')  # other engines untouched


def test_antigravity_home_is_seeded_past_theme_and_data_sharing_prompts(tmp_path, monkeypatch):
    # Desktop agy sessions stopped at the theme picker and the data-sharing consent.
    root_dir = tmp_path / 'managed'
    root_dir.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root_dir)
    tmp_path = root_dir
    home = tmp_path / 'home'
    b = {'label': 'agy', 'provider': 'antigravity', 'revision': '55555555-5555-4555-8555-555555555555',
         'version': '1.4.12', 'files': {}, 'env': {}, 'args': []}
    p.provision_managed_home(b, home, tmp_path)
    marker = home / p.ANTIGRAVITY_ONBOARDING
    data = json.loads(marker.read_text())
    assert data['onboardingComplete'] is True and data['consumerOnboardingComplete'] is True
    marker.write_text('{"onboardingComplete": true, "kept": 1}')
    p.provision_managed_home(b, home, tmp_path)  # idempotent, never overwrites engine state
    assert json.loads(marker.read_text())['kept'] == 1
    # The CLI's own incomplete marker (written on a first headless start) is completed, keeping other keys.
    marker.write_text('{"onboardingComplete": false, "enterpriseOnboardingComplete": false, "kept": 2}')
    p.provision_managed_home(b, home, tmp_path)
    fixed = json.loads(marker.read_text())
    assert fixed['onboardingComplete'] is True and fixed['kept'] == 2
    other = tmp_path / 'other'
    p.provision_managed_home(dict(b, provider='codex'), other, tmp_path)
    assert not (other / p.ANTIGRAVITY_ONBOARDING).exists()


@pytest.mark.parametrize('fail_install',[False,True])
def test_scoped_native_sync_preserves_unrelated_files_profiles_and_state(tmp_path, monkeypatch,fail_install):
    from agent_hub_host import engine_login
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'managed'))
    target = dict(label='hub-claude', provider='claude', version='2.1.39', revision='33333333-3333-4333-8333-333333333333',
                  credential_generation=5, files={'.claude/.credentials.json': '{}'}, env={}, args=[])
    other = bundle()
    p.root().mkdir(mode=0o700)
    previous = [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']} for b in [other, target]]
    p.write_json(p.root() / 'state.json', previous)
    unrelated = p.root() / (other['label'] + '-' + other['revision'])
    unrelated.mkdir(); (unrelated / 'keep.bin').write_bytes(b'unrelated exact bytes')
    p.profiles([other, target])
    text = p.profile_path().read_text()
    unrelated_block = text.split('[profiles.hub-test-account]')[1].split('[profiles.hub-hub-claude]')[0]
    state_before = (p.root() / 'state.json').read_bytes()
    calls = []
    def request(method, path, payload=None):
        calls.append((method, path, payload))
        return {'engines': [target], 'logins': [{'id': 'unrelated-login'}]} if method == 'GET' else {}
    def install(b, request=None, **kw):
        assert b['label'] == 'hub-claude' and kw['credential_generation'] == 5
        dest = p.root() / (b['label'] + '-' + b['revision']); dest.mkdir(mode=0o700,exist_ok=True)
        if fail_install:
            raise RuntimeError('synthetic interrupted target installation')
        p.write_json(dest / 'bundle.json', {'label': b['label']})
    monkeypatch.setattr(p, 'install', install)
    monkeypatch.setattr(engine_login, 'cancel_removed', lambda *a: pytest.fail('must not cancel unrelated login'))
    monkeypatch.setattr(engine_login, 'tick', lambda *a: pytest.fail('must not tick login'))
    if fail_install:
        with pytest.raises(RuntimeError,match='interrupted target'):
            p.sync(request,target_label='hub-claude',expected_generation=5)
        assert (unrelated/'keep.bin').read_bytes()==b'unrelated exact bytes'
        assert (p.root()/'state.json').read_bytes()==state_before
        assert p.profile_path().read_text()==text
        assert not any(method=='POST' for method,path,payload in calls)
        return
    result = p.sync(request, target_label='hub-claude', expected_generation=5)
    assert result['profiles'] == ['hub-hub-claude']
    assert (unrelated / 'keep.bin').read_bytes() == b'unrelated exact bytes'
    assert (p.root() / 'state.json').read_bytes() == state_before
    assert unrelated_block in p.profile_path().read_text()
    assert '[profiles.hub-hub-claude]' in p.profile_path().read_text()
    config_after=p.profile_path().read_bytes()
    p.sync(request,target_label='hub-claude',expected_generation=5)
    assert p.profile_path().read_bytes()==config_after
    assert (p.root() / 'state.json').read_bytes()==state_before
    reports = [payload for method, path, payload in calls if path == '/api/engines/sync-report']
    assert reports[-1]['items'][0]['credential_generation'] == 5


def test_scoped_sync_rejects_identity_or_refresh_lease_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'untouched'))
    base = dict(label='hub-claude',provider='claude',version='2.1.39',revision='33333333-3333-4333-8333-333333333333',credential_generation=5,
                files={'.claude/.credentials.json':'{}'},env={},args=[])
    monkeypatch.setattr(p,'install',lambda *a,**kw:pytest.fail('must not install'))
    for changes in [{'credential_generation':4},{'credential_generation':6},{'refresh_lease':True},{'label':'other'}]:
        value=dict(base,**changes)
        with pytest.raises(ValueError):
            p.sync(lambda *a:{'engines':[value]},target_label='hub-claude',expected_generation=5)
        assert not p.root().exists()


def test_scoped_claude_install_materializes_exact_generation_without_refresh(tmp_path, monkeypatch):
    from agent_hub_host import credentials
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR',str(tmp_path / 'managed'))
    value=dict(label='hub-claude',provider='claude',version='2.1.39',revision='33333333-3333-4333-8333-333333333333',credential_generation=5,
               credential_paths=['.claude/.credentials.json'],files={'.claude/.credentials.json':'{}'},env={},args=[])
    def install(argv,**kwargs):
        if '--prefix' in argv:
            system='win32' if os.name=='nt' else sys.platform
            cpu={'x86_64':'x64','amd64':'x64','aarch64':'arm64','arm64':'arm64'}[p.platform.machine().lower()]
            musl='-musl' if system=='linux' and p.platform.libc_ver()[0]=='musl' else ''
            package=Path(argv[argv.index('--prefix')+1])/('node_modules/@anthropic-ai/claude-code-'+system+'-'+cpu+musl)
            package.mkdir(parents=True)
            (package/'package.json').write_text(json.dumps({'version':value['version']}))
            exe=package/('claude.exe' if system=='win32' else 'claude')
            exe.write_text('#!/bin/sh\nexit 0\n');exe.chmod(0o700)
        return subprocess.CompletedProcess(argv,0)
    monkeypatch.setattr(p.subprocess,'run',install)
    monkeypatch.setattr(p,'resolve_npm_command',lambda:['synthetic-npm'])
    monkeypatch.setattr(credentials,'leased_refresh',lambda *a,**kw:pytest.fail('must not refresh'))
    p.install(value,lambda *a:pytest.fail('no credential writeback'),credential_generation=5)
    dest=p.root()/(value['label']+'-'+value['revision'])
    assert json.loads((dest/credentials.STATE_FILE).read_text())['generation']==5
    assert (dest/'home/.claude/.credentials.json').read_text()=='{}'
    # A same-generation resume uses the existing engine and remains refresh-free.
    p.install(value,lambda *a:pytest.fail('no credential writeback'),credential_generation=5)


@pytest.mark.skipif(os.name=='nt',reason='POSIX flock contract; Windows bounded lock tests are separate')
def test_scoped_posix_lock_timeout_is_bounded(tmp_path,monkeypatch):
    import time
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR',str(tmp_path/'managed'))
    p.root().mkdir(mode=0o700)
    code="import os,fcntl,sys,time; f=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(30)"
    child=subprocess.Popen([sys.executable,'-c',code,str(p.root()/'reconcile.lock')],stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='ready'
        start=time.monotonic()
        with pytest.raises(TimeoutError,match='lock timeout'):
            with p.lock(timeout=0.05,bounded=True):pytest.fail('must not enter')
        assert time.monotonic()-start<1
    finally:
        child.terminate();child.wait(timeout=5);child.stdout.close()


def test_scoped_state_keeps_unrelated_raw_bytes_on_revision_change(tmp_path):
    path=tmp_path/'state.json'
    other=' { "label" : "keep", "revision" : "preserve exact whitespace", "extra" : [1, 2] } '
    raw='[\n'+other+',\n {"label":"hub-claude","revision":"old","provider":"claude"}\n]\n'
    path.write_text(raw);path.chmod(0o600)
    snapshot,previous=p.scoped_state_snapshot(path)
    item={'label':'hub-claude','revision':'new','provider':'claude'}
    p.write_scoped_state(path,snapshot,previous,item)
    assert other in path.read_text()
    assert json.loads(path.read_text())[1]==item
    before=path.read_bytes()
    snapshot,previous=p.scoped_state_snapshot(path)
    p.write_scoped_state(path,snapshot,previous,item)
    assert path.read_bytes()==before
    snapshot,previous=p.scoped_state_snapshot(path)
    path.write_text('[]')
    with pytest.raises(ValueError,match='changed during scoped'):
        p.write_scoped_state(path,snapshot,previous,dict(item,revision='different'))
    assert path.read_text()=='[]'


@pytest.mark.skipif(os.name=='nt',reason='POSIX flock contract')
def test_scoped_lock_does_not_change_existing_start_lock_behavior(tmp_path,monkeypatch):
    import fcntl
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR',str(tmp_path/'managed'))
    operations=[]
    monkeypatch.setattr(fcntl,'flock',lambda fd,operation:operations.append(operation))
    with p.lock(timeout=2):pass
    with p.lock(timeout=2,bounded=True):pass
    assert operations==[fcntl.LOCK_EX,fcntl.LOCK_EX|fcntl.LOCK_NB]


@pytest.mark.parametrize('operator_target',[
    '[profiles."hub-hub-claude"]\nengine = "shell"\n',
    "[profiles.'hub-hub-claude']\nengine = 'shell'\n",
    '[profiles."hub-hub-claude".env]\nUNRELATED = "keep"\n',
    '[profiles]\n"hub-hub-claude" = { engine = "shell" }\n',
])
def test_scoped_profile_refuses_semantic_operator_target_without_changing_bytes(tmp_path,monkeypatch,operator_target):
    import tomllib
    path=tmp_path/'config.toml';path.write_text('version = 1\n'+operator_target);path.chmod(0o600)
    monkeypatch.setattr(p,'profile_path',lambda:path)
    before=path.read_bytes();tomllib.loads(before.decode())
    with pytest.raises(ValueError,match='Operator profile collision'):
        p.scoped_profile({'label':'hub-claude','provider':'claude'})
    assert path.read_bytes()==before
    tomllib.loads(path.read_text())


@pytest.mark.parametrize('target',[
    '[profiles."hub-hub-claude"]\nengine = "claude"\n',
    '[profiles."hub-hub-claude".env]\nKEY = "value"\n',
    '[profiles.hub-hub-claude]\nengine = "claude"\n[profiles."hub-hub-claude".env]\nKEY = "value"\n',
])
def test_scoped_profile_refuses_unrecognized_managed_target_tables(tmp_path,monkeypatch,target):
    import tomllib
    path=tmp_path/'config.toml';path.write_text('version = 1\n'+p.BEGIN+target+p.END);path.chmod(0o600)
    monkeypatch.setattr(p,'profile_path',lambda:path)
    before=path.read_bytes();tomllib.loads(before.decode())
    with pytest.raises(ValueError):p.scoped_profile({'label':'hub-claude','provider':'claude'})
    assert path.read_bytes()==before
    tomllib.loads(path.read_text())


def test_scoped_profile_parses_proposed_toml_before_publication(tmp_path,monkeypatch):
    import tomllib
    path=tmp_path/'config.toml';path.write_text('version = 1\n[profiles.keep]\nengine = "shell"\n');path.chmod(0o600)
    monkeypatch.setattr(p,'profile_path',lambda:path)
    # One unescaped table delimiter would make generated output invalid. This
    # injects formatting corruption without weakening any production label gate.
    monkeypatch.setattr(p.json,'dumps',lambda value:'"unterminated')
    before=path.read_bytes();tomllib.loads(before.decode())
    with pytest.raises(ValueError):p.scoped_profile({'label':'hub-claude','provider':'claude'})
    assert path.read_bytes()==before
    tomllib.loads(path.read_text())


def test_scoped_profile_never_moves_unrelated_continuation_keys_to_new_target(tmp_path,monkeypatch):
    import tomllib
    path=tmp_path/'config.toml'
    path.write_text('version = 1\n'+p.BEGIN+'[profiles.keep]\nengine = "shell"\n'+p.END+'args = ["preserve"]\n')
    path.chmod(0o600);monkeypatch.setattr(p,'profile_path',lambda:path)
    before=path.read_bytes()
    assert tomllib.loads(before.decode())['profiles']['keep']['args']==['preserve']
    with pytest.raises(ValueError):p.scoped_profile({'label':'hub-claude','provider':'claude'})
    assert path.read_bytes()==before


def test_managed_exec_readiness_requires_exact_semantic_profile(tmp_path, monkeypatch):
    import zipfile
    archive=tmp_path/'hub.pyz'
    with zipfile.ZipFile(archive,'w') as z: z.writestr('synthetic','owned fixture')
    monkeypatch.setattr(p.sys,'argv',[str(archive)])
    command=p.module_command('execute','hub-claude')
    path=p.profile_path();path.parent.mkdir(parents=True,mode=0o700)
    def publish(command,engine='claude'):
        path.write_text('[profiles."hub-hub-claude"]\nengine = '+json.dumps(engine)+'\ncommand = '+json.dumps(command)+'\n')
        path.chmod(0o600)
    publish(command)
    assert p.managed_exec_readiness(archive)['protocol']=='hub2-managed-claude-exec-v1'
    publish([*command[:-1],'other-label'])
    with pytest.raises(ValueError,match='unavailable'): p.managed_exec_readiness(archive)
    publish(command,'shell')
    with pytest.raises(ValueError,match='unavailable'): p.managed_exec_readiness(archive)
    path.write_text('version = 1\n');path.chmod(0o600)
    with pytest.raises(ValueError,match='unavailable'): p.managed_exec_readiness(archive)


def test_managed_opus_exception_is_context_only_and_medium(monkeypatch):
    b=dict(bundle(),label='hub-claude',provider='claude',args=[])
    with pytest.raises(ValueError): p.managed_claude_route_args(b,[],['--model','opus'])
    context={'model':'opus','effort':'medium'}
    assert p.managed_claude_route_args(b,[],['--model','opus','--hub-session-effort=medium'],managed=context)==['--model','opus','--effort','medium','--setting-sources','user']
    for model in ('haiku','arbitrary','glm-5.3-flash'):
        with pytest.raises(ValueError): p.managed_claude_route_args(b,[],['--model',model],managed=context)
    with pytest.raises(ValueError): p.managed_claude_route_args(b,[],['--model','opus','--hub-session-effort=high'],managed=context)


def test_managed_generation_uses_materialized_cas_winner_and_strict_ack(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'refresh_launch_credential',lambda *a:{'status':'adopted_hub','generation':6})
    calls=[]
    def request(method,path,body):
        calls.append((method,path,body));return {'status':'recorded'}
    b=dict(bundle(),label='hub-claude',provider='claude',credential_generation=5)
    assert p.managed_claude_materialize({},b,tmp_path,tmp_path,request)==6
    assert calls[0][2]['items'][0]['credential_generation']==6
    for status in ('hub_unreachable','writeback_pending','unmanaged'):
        monkeypatch.setattr(p,'refresh_launch_credential',lambda *a:{'status':status,'generation':5})
        with pytest.raises(ValueError,match='unverified'):p.managed_claude_materialize({},b,tmp_path,tmp_path,request)
    assert len(calls)==1
    monkeypatch.setattr(p,'refresh_launch_credential',lambda *a:{'status':'materialized','generation':6})
    def rotated(*a):raise ValueError('generation_conflict')
    with pytest.raises(ValueError,match='generation_conflict'):p.managed_claude_materialize({},b,tmp_path,tmp_path,rotated)


@pytest.mark.parametrize('admission_generation',[6,7])
def test_explicit_managed_claude_execute_binds_actual_attempt_generation_and_child(tmp_path,monkeypatch,admission_generation,capsys):
    from agent_hub_host import native_import_host
    managed=tmp_path/'managed';managed.mkdir(mode=0o700)
    monkeypatch.setattr(p,'root',lambda:managed)
    monkeypatch.chdir(tmp_path)
    context={'protocol':p.MANAGED_CLAUDE_PROTOCOL,'label':'hub-claude','profile':'hub-hub-claude',
             'account_id':'claude','engine':'claude','model':'opus','effort':'medium','session_id':'proof',
             'operation_id':'op-proof:0','epoch':3,'workspace':str(tmp_path),'task_id':'hub2-proof',
             'server':'https://hub.test','runtime_deadline':int(p.time.time()*1000)+60000,'request_digest':'b'*64,
             'machine_id':'host','exec_archive_sha256':'a'*64}
    archive=tmp_path/'synthetic.pyz';archive.write_bytes(b'synthetic reviewed archive')
    context['exec_archive_sha256']=p.hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(p,'__file__',str(archive/'agent_hub_host/provisioning.py'))
    monkeypatch.setattr(p,'managed_exec_readiness',lambda archive:{'protocol':p.MANAGED_CLAUDE_PROTOCOL})
    record=managed/'attempt.json';p.write_json(record,context)
    b={'label':'hub-claude','provider':'claude','version':p.engine_versions.package_version('claude'),'revision':'22222222-2222-4222-8222-222222222222',
       'files':{},'env':{},'args':[]}
    dest=managed/(b['label']+'-'+b['revision']);dest.mkdir(mode=0o700)
    p.write_json(dest/'bundle.json',b);p.write_json(managed/'state.json',[{k:b[k] for k in ('label','provider','revision')}])
    monkeypatch.setattr(p,'provision_managed_home',lambda *a:None)
    monkeypatch.setattr(p,'install_managed_engine_permissions',lambda *a,**kw:None)
    monkeypatch.setattr(p,'engine_argv',lambda *a:['/synthetic/claude'])
    monkeypatch.setattr(p,'engine_environment',lambda *a:{'PATH':'synthetic','CLAUDE_CODE_USE_BEDROCK':'1',
                          'ANTHROPIC_AUTH_TOKEN':'must-not-inherit','AWS_SESSION_TOKEN':'must-not-inherit','HUB2_TOKEN':'must-not-inherit'})
    monkeypatch.setattr(p,'profile_logged_in',lambda *a:True)
    monkeypatch.setattr(p,'refresh_launch_credential',lambda *a:{'status':'adopted_hub','generation':6})
    requests=[]
    def request(method,path,body):
        requests.append((method,path,body))
        return {'status':'recorded'} if path.endswith('sync-report') else {'launch_allowed':True,'credential_generation':admission_generation}
    monkeypatch.setattr(native_import_host,'bind_managed_claude',lambda base:('host',request))
    native={'id':'11111111-2222-4333-8444-555555555555','tag':'task-hub2-proof','workspace':str(tmp_path),
            'created_at_ms':int(p.time.time()*1000)-1000,'phase':'running','command':['a','task','run','--profile','hub-hub-claude',
            '--engine-arg='+p.MANAGED_CONTEXT_FLAG+str(record)]}
    monkeypatch.setattr(p.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,0,json.dumps(native),''))
    # Actual workspace alternative auth must remain outside the selected CLI
    # sources. This stub implements the pinned documented option contract and
    # fails if the generated argv would select a project helper; no helper runs.
    project=tmp_path/'.claude';project.mkdir(mode=0o700)
    project_settings={'apiKeyHelper':'printf synthetic-project-key','env':{'ANTHROPIC_AUTH_TOKEN':'project-key'}}
    (project/'settings.json').write_text(json.dumps(project_settings))
    before_project=(project/'settings.json').read_bytes()
    calls=[]
    def provider(command,argv,env):
        sources=argv[argv.index('--setting-sources')+1].split(',') if '--setting-sources' in argv else ['user','project','local']
        assert sources==['user'], 'project apiKeyHelper would be selected'
        assert (project/'settings.json').read_bytes()==before_project
        calls.append((command,argv,env))
    monkeypatch.setattr(p.os,'execve',provider)
    monkeypatch.setattr(sys,'argv',['hub','hub-claude',p.MANAGED_CONTEXT_FLAG+str(record),'--model','opus',
                                  '--hub-session-effort=medium','--output-format','stream-json','-p','synthetic prompt'])
    if admission_generation != 6:
        with pytest.raises(SystemExit) as refused:p.execute()
        assert refused.value.code==75 and calls==[]
        assert json.loads(capsys.readouterr().err)['reason']=='managed_generation_changed'
        assert not record.with_suffix('.receipt.json').exists()
        return
    p.execute()
    assert len(calls)==1
    assert calls[0][2]['CLAUDE_CONFIG_DIR']==str(dest/'home/.claude')
    assert not any(k.startswith(('ANTHROPIC_','AWS_')) or k in ('CLAUDE_CODE_USE_BEDROCK','HUB2_TOKEN') for k in calls[0][2])
    assert calls[0][1][calls[0][1].index('--model')+1]=='opus'
    assert calls[0][1][calls[0][1].index('--effort')+1]=='medium'
    assert not any(v.startswith(p.MANAGED_CONTEXT_FLAG) for v in calls[0][1])
    assert requests[0][1].endswith('sync-report') and requests[0][2]['items'][0]['credential_generation']==6
    assert requests[1][2]=={'label':'hub-claude','native':True}
    receipt=json.loads(record.with_suffix('.receipt.json').read_text())
    assert receipt['native_id']==native['id'] and receipt['epoch']==3 and receipt['credential_generation']==6
    assert receipt['request_digest']=='b'*64 and receipt['profile']=='hub-hub-claude'
    assert 'token' not in repr(receipt) and 'server' not in receipt
    assert record.with_suffix('.receipt.json').stat().st_mode&0o777==0o600


@pytest.mark.parametrize('change',[{'model':'sonnet'},{'account_id':'other'},{'epoch':True},{'runtime_deadline':1}])
def test_managed_context_refuses_unqualified_attempt_before_any_request(tmp_path,monkeypatch,change):
    monkeypatch.chdir(tmp_path)
    context={'protocol':p.MANAGED_CLAUDE_PROTOCOL,'label':'hub-claude','profile':'hub-hub-claude',
             'account_id':'claude','engine':'claude','model':'opus','effort':'medium','session_id':'proof',
             'operation_id':'op-proof:0','epoch':3,'workspace':str(tmp_path),'task_id':'hub2-proof',
             'server':'https://hub.test','runtime_deadline':int(p.time.time()*1000)+60000,'request_digest':'b'*64,
             'machine_id':'host','exec_archive_sha256':'a'*64,**change}
    path=tmp_path/'private/attempt.json';p.write_json(path,context)
    with pytest.raises(ValueError,match='binding_invalid'):p.managed_claude_context([p.MANAGED_CONTEXT_FLAG+str(path)])


def test_managed_failure_is_typed_without_echoing_exception_payload(monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['hub','hub-claude',p.MANAGED_CONTEXT_FLAG+'/private/context.json'])
    def refuse(*args):raise OSError('synthetic-secret-must-not-print')
    monkeypatch.setattr(p,'managed_claude_context',refuse)
    with pytest.raises(SystemExit) as result:p.execute()
    assert result.value.code==75
    output=capsys.readouterr()
    assert json.loads(output.err)=={'provider':'claude','launch_allowed':False,'reason':'managed_exec_refused'}
    assert 'synthetic-secret' not in output.err+output.out


@pytest.mark.parametrize('settings',[{'apiKeyHelper':'must-not-run'}, {'primaryApiKey':'must-not-use'},
    {'env':{'ANTHROPIC_AUTH_TOKEN':'must-not-use'}}, {'awsCredentialExport':'must-not-run'}])
def test_managed_user_auth_overrides_refuse_without_running_helpers(tmp_path,monkeypatch,settings):
    path=tmp_path/'.claude/settings.json';p.write_json(path,settings);before=path.read_bytes()
    with patch.object(p.subprocess,'run') as run,pytest.raises(ValueError,match='managed_settings_unverified'):
        p.managed_claude_settings(tmp_path,system_settings=())
    run.assert_not_called();assert path.read_bytes()==before


def test_managed_settings_keep_private_generated_hooks_and_refuse_system_policy(tmp_path):
    path=tmp_path/'.claude/settings.json'
    p.write_json(path,{'hooks':{'Stop':[{'hooks':[{'type':'command','command':'a reported-state idle'}]}]},
                       'permissions':{'defaultMode':'bypassPermissions'},'autoUpdates':False})
    before=path.read_bytes();p.managed_claude_settings(tmp_path,system_settings=())
    assert path.read_bytes()==before
    system=tmp_path/'system-managed.json';system.write_text('{}')
    with pytest.raises(ValueError,match='managed_settings_unverified'):
        p.managed_claude_settings(tmp_path,system_settings=(system,))


@pytest.mark.parametrize('override',[['--settings','/not-read.json'],['--setting-sources','project'],['--setting-sources=local']])
def test_managed_settings_source_cannot_be_overridden(override):
    with pytest.raises(ValueError,match='managed_settings_unverified'):
        p.managed_claude_route_args({'provider':'claude','args':[]},[],['--model','opus',*override],managed={'model':'opus'})
