"""Unit tests for Hub-managed shared AWS Gate config (non-secret bundle)."""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_hub_host import aws_gate
from agent_hub_host import provisioning as p
from agent_hub_host import shared_config as sc


def _isolate_config(monkeypatch, tmp_path):
    xdg = tmp_path / 'xdg'
    xdg.mkdir()
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    monkeypatch.delenv('HUB_CONFIG_FILE', raising=False)
    return xdg


def test_bundle_has_default_gate_profile_without_static_keys():
    src = sc.source_dir()
    assert src is not None
    cfg = (src / 'aws' / 'config').read_text(encoding='utf-8')
    assert '[default]' in cfg
    assert 'eu-west-1' in cfg
    assert 'aws-gate credential-process' in cfg
    lowered = cfg.lower()
    assert 'aws_access_key_id' not in lowered
    assert 'aws_secret_access_key' not in lowered
    assert 'akia' not in lowered


def test_sync_writes_hub_owned_files(tmp_path, monkeypatch):
    xdg = _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv('HUB_BIN', '/usr/local/bin/hub')
    res = sc.sync_shared_config()
    assert res['status'] == 'synced'
    dest = xdg / 'agent-hub' / 'shared'
    assert Path(res['dest']) == dest
    cfg = dest / 'aws' / 'config'
    assert cfg.is_file()
    text = cfg.read_text(encoding='utf-8')
    assert '[default]' in text
    assert '/usr/local/bin/hub aws-gate credential-process' in text
    assert 'AKIA' not in text
    if os.name != 'nt':
        assert oct(cfg.stat().st_mode & 0o777) == oct(0o600)


def test_sync_dry_run_writes_nothing(tmp_path, monkeypatch):
    xdg = _isolate_config(monkeypatch, tmp_path)
    res = sc.sync_shared_config(dry_run=True)
    assert res['status'] == 'dry-run'
    assert not (xdg / 'agent-hub' / 'shared' / 'aws' / 'config').exists()


def test_sync_refuses_static_keys(tmp_path, monkeypatch):
    _isolate_config(monkeypatch, tmp_path)
    bad_src = tmp_path / 'bad-bundle'
    (bad_src / 'aws').mkdir(parents=True)
    (bad_src / 'manifest.json').write_text(json.dumps({'version': 1, 'files': ['aws/config']}))
    (bad_src / 'aws' / 'config').write_text(
        '[default]\nregion = eu-west-1\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\n')
    with patch.object(sc, 'SHARED_CONFIG_FILES', ('aws/config',)):
        with pytest.raises(ValueError, match='static keys'):
            sc.sync_shared_config(source=str(bad_src))


def test_engine_environment_sets_both_aws_files(tmp_path, monkeypatch):
    xdg = _isolate_config(monkeypatch, tmp_path)
    home = tmp_path / 'managed-home'
    home.mkdir()
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'SHOULD-BE-STRIPPED')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'SHOULD-BE-STRIPPED')
    monkeypatch.setenv('AWS_PROFILE', 'main-temp')
    b = {'provider': 'codex', 'env': {}, 'args': []}
    env = p.engine_environment(b, home, dest=tmp_path)
    expected_config = str(xdg / 'agent-hub' / 'shared' / 'aws' / 'config')
    assert env['AWS_CONFIG_FILE'] == expected_config
    creds = Path(env['AWS_SHARED_CREDENTIALS_FILE'])
    assert creds == home / '.aws' / 'credentials'
    # Inside the managed home, and nonexistent (real static keys unreachable).
    assert home in creds.parents
    assert not creds.exists()
    assert 'AWS_ACCESS_KEY_ID' not in env
    assert 'AWS_SECRET_ACCESS_KEY' not in env
    assert 'AWS_SESSION_TOKEN' not in env
    assert 'AWS_PROFILE' not in env


def test_engine_environment_bundle_cannot_override_aws_files(tmp_path, monkeypatch):
    _isolate_config(monkeypatch, tmp_path)
    home = tmp_path / 'managed-home'
    home.mkdir()
    b = {'provider': 'codex',
         'env': {'AWS_CONFIG_FILE': '/real/.aws/config',
                 'AWS_SHARED_CREDENTIALS_FILE': '/real/.aws/credentials'},
         'args': []}
    env = p.engine_environment(b, home, dest=tmp_path)
    assert env['AWS_CONFIG_FILE'] != '/real/.aws/config'
    assert env['AWS_SHARED_CREDENTIALS_FILE'] != '/real/.aws/credentials'
    assert Path(env['AWS_SHARED_CREDENTIALS_FILE']).is_relative_to(home)


def test_gate_env_derived_from_hub_owned_config(tmp_path, monkeypatch):
    base = tmp_path / 'realxdg'
    (base / 'agent-hub' / 'shared' / 'aws').mkdir(parents=True)
    cfg = base / 'agent-hub' / 'shared' / 'aws' / 'config'
    cfg.write_text('[default]\n')
    monkeypatch.setenv('AWS_CONFIG_FILE', str(cfg))
    assert aws_gate.default_gate_env_file() == base / 'aws-gate' / 'env'


def test_credential_process_successprints_standard_json(tmp_path):
    env_file = tmp_path / 'env'
    env_file.write_text(
        'export AWS_CONTAINER_CREDENTIALS_FULL_URI="https://gate.example/unused"\n'
        'export AWS_CONTAINER_AUTHORIZATION_TOKEN="secrettoken"\n')

    class FakeResponse:
        def read(self):
            return json.dumps({
                'AccessKeyId': 'ASIAEXAMPLE',
                'SecretAccessKey': 'secret',
                'Token': 'token',
                'Expiration': '2030-01-01T00:00:00Z',
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen = {}

    def fake_urlopen(request, timeout=None):
        seen['auth'] = request.headers.get('Authorization')
        seen['url'] = request.full_url
        return FakeResponse()

    buf = io.StringIO()
    with patch('urllib.request.urlopen', side_effect=fake_urlopen):
        with redirect_stdout(buf):
            rc = aws_gate.run_credential_process(env_file=str(env_file), timeout=5.0)
    assert rc == 0
    assert seen['auth'] == 'secrettoken'
    out = json.loads(buf.getvalue())
    assert out['Version'] == 1
    assert out['AccessKeyId'] == 'ASIAEXAMPLE'
    assert out['SessionToken'] == 'token'


def test_credential_process_missing_env_fails_closed_no_secrets(tmp_path, capsys):
    rc = aws_gate.run_credential_process(env_file=str(tmp_path / 'nope'), timeout=1.0)
    assert rc == 1
    captured = capsys.readouterr()
    assert 'AKIA' not in captured.err
    assert 'secrettoken' not in captured.err.lower()


def test_sync_uses_embedded_bundle_when_no_filesystem_source(tmp_path, monkeypatch):
    import sys
    import types
    _isolate_config(monkeypatch, tmp_path)
    fake = types.ModuleType('agent_hub_host._shared_bundle')
    fake.FILES = {
        'manifest.json': json.dumps({'version': 1, 'files': ['aws/config']}),
        'aws/config': '[default]\nregion = eu-west-1\ncredential_process = hub aws-gate credential-process\n',
    }
    monkeypatch.setitem(sys.modules, 'agent_hub_host._shared_bundle', fake)
    monkeypatch.setattr(sc, 'source_dir', lambda: None)
    monkeypatch.setenv('HUB_BIN', 'hub')
    res = sc.sync_shared_config()
    assert res['status'] == 'synced'
    cfg = tmp_path / 'xdg' / 'agent-hub' / 'shared' / 'aws' / 'config'
    assert cfg.is_file()
    assert 'aws-gate credential-process' in cfg.read_text(encoding='utf-8')


def test_install_on_new_machine_dry_run_against_temp_home(tmp_path, monkeypatch):
    """Acceptance 8: install path works against a temp HOME without touching real host."""
    fake_home = tmp_path / 'fakehome'
    fake_home.mkdir()
    xdg = fake_home / '.config'
    xdg.mkdir()
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    monkeypatch.setenv('HOME', str(fake_home))
    monkeypatch.delenv('HUB_CONFIG_FILE', raising=False)
    monkeypatch.setenv('HUB_BIN', 'hub')
    res = sc.sync_shared_config(dry_run=True)
    assert res['status'] == 'dry-run'
    assert res['dest'].startswith(str(xdg))
    assert any(f.endswith('aws/config') for f in res['files'])
    assert not (xdg / 'agent-hub' / 'shared' / 'aws' / 'config').exists()


def test_runtime_credential_override_does_not_relocate_shared_config(tmp_path, monkeypatch):
    xdg = _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv('HUB_CONFIG_FILE', str(tmp_path / 'runtime' / 'host.json'))
    sc.sync_shared_config(hub_bin='/usr/local/bin/hub')
    expected = xdg / 'agent-hub' / 'shared' / 'aws' / 'config'
    assert expected.is_file()
    home = tmp_path / 'managed'
    env = p.engine_environment({'provider': 'codex', 'env': {}}, home)
    assert env['AWS_CONFIG_FILE'] == str(expected)
    with patch.dict(os.environ, {'AWS_CONFIG_FILE': str(expected)}):
        assert aws_gate.default_gate_env_file() == xdg / 'aws-gate' / 'env'


def test_windows_shared_config_uses_appdata_not_credential_override(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.sys, 'platform', 'win32')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'appdata'))
    monkeypatch.setenv('HUB_CONFIG_FILE', str(tmp_path / 'runtime' / 'host.json'))
    assert sc.hub_aws_config_file() == tmp_path / 'appdata' / 'agent-hub' / 'shared' / 'aws' / 'config'


def test_worker_sync_omits_main_role_and_alternate_credential_sources(tmp_path):
    import configparser
    source = tmp_path / 'source'
    (source / 'aws').mkdir(parents=True)
    (source / 'manifest.json').write_text(json.dumps({'version': 2, 'files': ['aws/config']}))
    raw = ('[DEFAULT]\nrole_arn = arn:aws:iam::387546586013:role/example\n'
           '[default]\nregion = eu-west-1\noutput = json\n'
           'credential_source = Environment\nweb_identity_token_file = /operator/token\n'
           'credential_process = arbitrary-operator-provider\n'
           '[profile main]\nsource_profile = default\n'
           'role_arn = arn:aws:iam::387546586013:role/example\n')
    (source / 'aws/config').write_text(raw)
    dest = tmp_path / 'worker'
    sc.sync_shared_config(source=source, dest=dest, hub_bin='/opt/hub')
    cfg = configparser.RawConfigParser()
    cfg.read(dest / 'aws/config')
    assert cfg.sections() == ['default']
    assert dict(cfg.items('default')) == {
        'region': 'eu-west-1', 'output': 'json',
        'credential_process': '/opt/hub aws-gate credential-process',
    }
    assert cfg.defaults() == {}
    assert (source / 'aws/config').read_text() == raw


@pytest.mark.parametrize('material', [
    'aws_session_token = example-session',
    'aws_security_token = example-security',
    '# ASIA' + 'A' * 16,
    '# AKIA' + 'A' * 16,
])
def test_sync_refuses_session_credential_material(tmp_path, material):
    source = tmp_path / 'source'
    (source / 'aws').mkdir(parents=True)
    (source / 'manifest.json').write_text(json.dumps({'version': 1, 'files': ['aws/config']}))
    (source / 'aws/config').write_text('[default]\n' + material + '\n')
    dest = tmp_path / 'worker'
    with pytest.raises(ValueError, match='credentials'):
        sc.sync_shared_config(source=source, dest=dest)
    assert not dest.exists()


def test_shared_config_prose_can_name_geographic_asia(tmp_path):
    # Credential IDs have fixed shape; ordinary text must not trigger rejection.
    source = tmp_path / 'source'
    (source / 'aws').mkdir(parents=True)
    (source / 'manifest.json').write_text(json.dumps({'version': 1, 'files': ['aws/config']}))
    (source / 'aws/config').write_text('[default]\nregion = ap-south-1\n# Asia\n')
    sc.sync_shared_config(source=source, dest=tmp_path / 'worker')


@pytest.mark.parametrize('material', ['aws_session_token = example-session', '# ASIA' + 'A' * 16])
def test_packaged_bundle_build_refuses_session_credentials(tmp_path, monkeypatch, material):
    import importlib.util
    script = Path(__file__).resolve().parents[2] / 'scripts/build-hub-cli.py'
    spec = importlib.util.spec_from_file_location('shared_config_cli_builder', script)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    repo = tmp_path / 'repo'
    pkg = repo / 'host/agent_hub_host'
    pkg.mkdir(parents=True)
    (pkg / '__init__.py').write_text('')
    (repo / 'config').mkdir()
    (repo / 'config/model-routing.json').write_text('{}')
    # The build copies the maintained supervisor before the bundle embed.
    (repo / 'scripts').mkdir()
    (repo / 'scripts/session-host-supervisor.py').write_text('#')
    (repo / 'shared-config/aws').mkdir(parents=True)
    (repo / 'shared-config/manifest.json').write_text(json.dumps({'version': 1, 'files': ['aws/config']}))
    (repo / 'shared-config/aws/config').write_text('[default]\n' + material + '\n')
    monkeypatch.setattr(builder, 'REPO', repo)
    monkeypatch.setattr(builder, 'HOST_PKG', pkg)
    with pytest.raises(SystemExit, match='must not contain'):
        builder.build(tmp_path / 'out', 'test')
    assert not (tmp_path / 'out/hub.pyz').exists()
