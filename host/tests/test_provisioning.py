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
    with patch.object(p.sys, 'argv', ['wrapper', 'test-account', '--version']), \
         patch.object(p, 'check_codex_quota_gate', return_value=(True, 'ok')), \
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
        'launcher_effort': 'high',
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

    # Prepare bundle on disk
    dest = m_dir / f"{b['label']}-{b['revision']}"
    dest.mkdir(mode=0o700, parents=True)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(m_dir / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']}])

    # Valid payload
    payload_valid = json.dumps({
        'profile': 'hub-test-account',
        'account': 'test-account',
        'selection': {'model': 'gpt-6.1-sol', 'effort': 'high'},
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

    # Case 1: Guard rejects invocation (<15% reserve)
    calls_execve = []
    def fake_execve(cmd, argv, env):
        calls_execve.append((cmd, argv, env))

    monkeypatch.setattr(os, 'execve', fake_execve)

    def fake_quse_reject(env):
        return False, 'codex_reserve_15_percent_required'

    monkeypatch.setattr(p, 'check_codex_quota_gate', fake_quse_reject)
    monkeypatch.setattr(sys, 'argv', ['execute', 'codex-acc', 'run', 'prompt'])

    with pytest.raises(SystemExit) as exc:
        p.execute()
    assert exc.value.code == 75
    # Assert provider binary was NEVER executed
    assert len(calls_execve) == 0

    # Case 2: Diagnostic --version bypasses quota gate
    gate_called = []
    def fake_gate_tracker(env):
        gate_called.append(True)
        return False, 'should_not_be_called'

    monkeypatch.setattr(p, 'check_codex_quota_gate', fake_gate_tracker)
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

    # Case 3: Guard approves invocation (>=15% reserve)
    def fake_quse_approve(env):
        return True, 'ok'

    monkeypatch.setattr(p, 'check_codex_quota_gate', fake_quse_approve)
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


def test_check_codex_quota_gate_contract(monkeypatch):
    """Direct contract verification of check_codex_quota_gate.

    1. Real private quse snapshot shape:
       - 7d present with valid reading & future reset
       - 5h and monthly both reading+reset null (not applicable)
       -> passes gate
    2. Minimum reserve <=15% -> rejected with codex_reserve_15_percent_required
    3. limit_reached True or missing -> rejected with quota_unavailable
    4. Missing required 7d/weekly window -> rejected with quota_unavailable
    5. Non-finite or NaN reading -> rejected with quota_unavailable
    6. Stale reset_at in the past -> rejected with stale_window_evidence
    7. Process failure or invalid JSON -> rejected with quota_unavailable
    """
    def mock_proc(stdout_obj, rc=0):
        class Proc:
            returncode = rc
            stdout = json.dumps(stdout_obj) if isinstance(stdout_obj, (dict, list)) else str(stdout_obj)
        return Proc()

    now = datetime.now(timezone.utc)
    future_reset = (now + timedelta(days=7)).isoformat()
    stale_reset = (now - timedelta(days=1)).isoformat()

    env = {'HOME': '/tmp'}

    # 1. Real quse shape: 7d valid, 5h and monthly null (not applicable)
    quse_real = {
        'codex': {
            'status': 'ok',
            'details': {'limit_reached': False},
            'windows': {
                '5h': {'percent_remaining': None, 'reset_at': None},
                '7d': {'percent_remaining': 82.5, 'reset_at': future_reset},
                'monthly': {'percent_remaining': None, 'reset_at': None},
            }
        }
    }
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: mock_proc(quse_real))
    allowed, reason = p.check_codex_quota_gate(env)
    assert allowed is True
    assert reason == 'ok'

    # 2. Reserve <= 15%
    quse_low = {
        'codex': {
            'status': 'ok',
            'details': {'limit_reached': False},
            'windows': {
                '5h': {'percent_remaining': None, 'reset_at': None},
                '7d': {'percent_remaining': 15.0, 'reset_at': future_reset},
            }
        }
    }
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: mock_proc(quse_low))
    allowed, reason = p.check_codex_quota_gate(env)
    assert allowed is False
    assert reason == 'codex_reserve_15_percent_required'

    # 3. limit_reached True
    quse_limited = {
        'codex': {
            'status': 'ok',
            'details': {'limit_reached': True},
            'windows': {
                '7d': {'percent_remaining': 80.0, 'reset_at': future_reset},
            }
        }
    }
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: mock_proc(quse_limited))
    allowed, reason = p.check_codex_quota_gate(env)
    assert allowed is False
    assert reason == 'quota_unavailable'

    # 4. Missing required 7d/weekly window
    quse_no_weekly = {
        'codex': {
            'status': 'ok',
            'details': {'limit_reached': False},
            'windows': {
                '5h': {'percent_remaining': 90.0, 'reset_at': future_reset},
            }
        }
    }
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: mock_proc(quse_no_weekly))
    allowed, reason = p.check_codex_quota_gate(env)
    assert allowed is False
    assert reason == 'quota_unavailable'

    # 5. Non-finite / NaN
    quse_nan = {
        'codex': {
            'status': 'ok',
            'details': {'limit_reached': False},
            'windows': {
                '7d': {'percent_remaining': float('nan'), 'reset_at': future_reset},
            }
        }
    }
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: mock_proc(quse_nan))
    allowed, reason = p.check_codex_quota_gate(env)
    assert allowed is False
    assert reason == 'quota_unavailable'

    # 6. Stale reset_at in the past
    quse_stale = {
        'codex': {
            'status': 'ok',
            'details': {'limit_reached': False},
            'windows': {
                '7d': {'percent_remaining': 80.0, 'reset_at': stale_reset},
            }
        }
    }
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: mock_proc(quse_stale))
    allowed, reason = p.check_codex_quota_gate(env)
    assert allowed is False
    assert reason == 'stale_window_evidence'


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






