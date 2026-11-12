"""Managed Claude settings verifier: finite 1 MiB cap for generated .claude.json metadata.

Synthetic only: no credentials, provider, profile, service or real home config.
Everything runs in pytest tmp directories with in-process fakes for the Hub.

Intent under test (intake ee4d12b0): Claude Code legitimately grows the two
generated `.claude.json` metadata files past 64 KiB, which made every managed
headless launch refuse with `managed_settings_unverified`. Only those two
locations may read up to 1 MiB; settings.json / settings.local.json keep 64 KiB,
and the whole file is still parsed and validated (late auth keys included).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from agent_hub_host import credentials, native_import_host, provisioning as p

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='private_fs ownership guards are POSIX')

SETTINGS_LIMIT = 65536
METADATA_LIMIT = 1048576
METADATA_PATHS = [('.claude', '.claude.json'), ('.claude.json',)]
SETTINGS_PATHS = [('.claude', 'settings.json'), ('.claude', 'settings.local.json')]
LATE_AUTH_KEYS = ['apiKeyHelper', 'primaryApiKey', 'awsAuthRefresh', 'awsCredentialExport',
                  'forceLoginMethod', 'forceLoginOrgUUID']


def sized(total, **late):
    """Valid JSON object of exactly `total` bytes; padding precedes any late keys."""
    body = {'projects': {'/synthetic/work': {'history': ''}}, **late}
    raw = json.dumps(body, separators=(',', ':'))
    pad = total - len(raw.encode())
    assert pad >= 0
    body['projects']['/synthetic/work']['history'] = 'x' * pad
    raw = json.dumps(body, separators=(',', ':')).encode()
    assert len(raw) == total
    return raw


def put(home, parts, raw, mode=0o600):
    path = Path(home).joinpath(*parts)
    for directory in reversed([d for d in path.parents if d != Path(home).parent and d.is_relative_to(home)]):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    path.chmod(mode)
    return path


@pytest.fixture
def home(tmp_path):
    root = tmp_path / 'home'
    root.mkdir(mode=0o700)
    return root


def verify(home):
    with patch.object(p.subprocess, 'run') as run:
        p.managed_claude_settings(home, system_settings=())
    run.assert_not_called()


def refused(home, **kwargs):
    with patch.object(p.subprocess, 'run') as run, pytest.raises(ValueError, match='^managed_settings_unverified$'):
        p.managed_claude_settings(home, system_settings=kwargs.get('system_settings', ()))
    run.assert_not_called()


def test_documented_limits_are_finite_and_split():
    assert p.MANAGED_CLAUDE_SETTINGS_LIMIT == SETTINGS_LIMIT
    assert p.MANAGED_CLAUDE_METADATA_LIMIT == METADATA_LIMIT


# ---- unit: the verifier ---------------------------------------------------------------

@pytest.mark.parametrize('parts', METADATA_PATHS)
@pytest.mark.parametrize('size', [75 * 1024, SETTINGS_LIMIT + 1, METADATA_LIMIT])
def test_large_valid_metadata_passes_unchanged_at_both_locations(home, parts, size):
    path = put(home, parts, sized(size))
    before = path.read_bytes()
    verify(home)
    assert path.read_bytes() == before


def test_large_metadata_at_both_locations_together_passes(home):
    paths = [put(home, parts, sized(80 * 1024)) for parts in METADATA_PATHS]
    verify(home)
    assert all(path.stat().st_size == 80 * 1024 for path in paths)


@pytest.mark.parametrize('parts', METADATA_PATHS)
def test_metadata_over_one_mib_is_refused_not_truncated(home, parts):
    put(home, parts, sized(METADATA_LIMIT + 1))
    refused(home)


@pytest.mark.parametrize('parts', METADATA_PATHS)
@pytest.mark.parametrize('key', LATE_AUTH_KEYS)
def test_late_auth_routing_key_after_large_padding_is_refused(home, parts, key):
    put(home, parts, sized(200 * 1024, **{key: 'synthetic-late-value'}))
    refused(home)


@pytest.mark.parametrize('parts', METADATA_PATHS)
@pytest.mark.parametrize('env', [{'ANTHROPIC_AUTH_TOKEN': 'synthetic'}, {'X': ''}, None, [], 'x'])
def test_nonempty_or_non_object_env_after_large_padding_is_refused(home, parts, env):
    put(home, parts, sized(200 * 1024, env=env))
    refused(home)


@pytest.mark.parametrize('parts', METADATA_PATHS)
def test_empty_env_after_large_padding_is_still_accepted(home, parts):
    put(home, parts, sized(200 * 1024, env={}))
    verify(home)


@pytest.mark.parametrize('parts', METADATA_PATHS)
@pytest.mark.parametrize('raw', [
    sized(200 * 1024)[:-1],                 # truncated large JSON
    b'{' + b'"a":1,' * 20000 + b'}',        # large but malformed
    b'[' + b'1,' * 50000 + b'1]',           # large scalar-ish container: array, not object
    b'"' + b'x' * 100000 + b'"',            # large string scalar
    b'7', b'null', b'',
])
def test_malformed_or_non_object_metadata_is_refused(home, parts, raw):
    put(home, parts, raw)
    refused(home)


@pytest.mark.parametrize('parts', SETTINGS_PATHS)
def test_settings_files_keep_the_64_kib_limit(home, parts):
    path = put(home, parts, sized(SETTINGS_LIMIT))
    before = path.read_bytes()
    verify(home)
    assert path.read_bytes() == before
    put(home, parts, sized(SETTINGS_LIMIT + 1))
    refused(home)
    put(home, parts, sized(75 * 1024))
    refused(home)


@pytest.mark.parametrize('parts', SETTINGS_PATHS)
@pytest.mark.parametrize('key', LATE_AUTH_KEYS)
def test_settings_auth_keys_still_refused(home, parts, key):
    put(home, parts, sized(SETTINGS_LIMIT // 2, **{key: 'synthetic'}))
    refused(home)


def test_oversized_settings_refused_even_when_metadata_is_valid_and_large(home):
    put(home, METADATA_PATHS[0], sized(75 * 1024))
    put(home, SETTINGS_PATHS[0], sized(SETTINGS_LIMIT + 1))
    refused(home)


@pytest.mark.parametrize('kind', ['file', 'directory'])
@pytest.mark.parametrize('parts', METADATA_PATHS)
def test_system_policy_presence_refuses_even_valid_large_metadata(home, tmp_path, parts, kind):
    put(home, parts, sized(75 * 1024))
    policy = tmp_path / ('managed-settings.json' if kind == 'file' else 'managed-settings.d')
    policy.write_text('{}') if kind == 'file' else policy.mkdir()
    refused(home, system_settings=(str(policy),))
    verify(home)  # same home is fine once no system policy exists: the policy alone refused it


def test_default_system_policy_locations_are_the_enterprise_paths():
    assert p.MANAGED_CLAUDE_SYSTEM_SETTINGS == ('/etc/claude-code/managed-settings.json',
                                                '/etc/claude-code/managed-settings.d')


@pytest.mark.parametrize('parts', METADATA_PATHS)
def test_large_metadata_symlink_is_refused(home, tmp_path, parts):
    target = tmp_path / 'elsewhere.json'
    target.write_bytes(sized(75 * 1024))
    target.chmod(0o600)
    link = Path(home).joinpath(*parts)
    link.parent.mkdir(mode=0o700, exist_ok=True)
    link.symlink_to(target)
    refused(home)


@pytest.mark.parametrize('parts', METADATA_PATHS)
def test_large_metadata_hardlink_is_refused(home, tmp_path, parts):
    other = tmp_path / 'other-link.json'
    path = put(home, parts, sized(75 * 1024))
    os.link(path, other)
    refused(home)


@pytest.mark.parametrize('parts', METADATA_PATHS)
@pytest.mark.parametrize('mode', [0o644, 0o660, 0o604, 0o666])
def test_large_metadata_not_owner_private_is_refused(home, parts, mode):
    put(home, parts, sized(75 * 1024), mode=mode)
    refused(home)


@pytest.mark.parametrize('parts', METADATA_PATHS)
def test_large_metadata_in_group_writable_directory_is_refused(home, parts):
    path = put(home, parts, sized(75 * 1024))
    path.parent.chmod(0o770)
    try:
        refused(home)
    finally:
        path.parent.chmod(0o700)


def test_metadata_fifo_is_refused_without_blocking(home):
    path = Path(home) / '.claude.json'
    os.mkfifo(path, 0o600)
    refused(home)


def test_missing_files_are_still_skipped(home):
    verify(home)


def test_owner_guard_is_the_private_reader_not_a_new_code_path(home):
    """Ownership cannot be faked without root; assert the verifier still routes every read
    through private_fs.read_bytes (whose uid/mode/link checks are covered by its own tests)."""
    put(home, METADATA_PATHS[1], sized(75 * 1024))
    seen = []
    real = p.private_fs.read_bytes

    def spy(path, limit, **kwargs):
        seen.append((Path(path).name, limit, kwargs))
        return real(path, limit, **kwargs)
    with patch.object(p.private_fs, 'read_bytes', spy):
        verify(home)
    # All four files are read, each through the private reader (private=True default, never relaxed),
    # with 64 KiB for the two settings files and 1 MiB only for the two metadata files.
    assert seen == [('settings.json', SETTINGS_LIMIT, {}), ('settings.local.json', SETTINGS_LIMIT, {}),
                    ('.claude.json', METADATA_LIMIT, {}), ('.claude.json', METADATA_LIMIT, {})]


# ---- composed: real _execute() through settings, materialization, ACK, exec ----------------

class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        managed = tmp_path / 'managed'
        managed.mkdir(mode=0o700)
        monkeypatch.setattr(p, 'root', lambda: managed)
        monkeypatch.chdir(tmp_path)
        context = {'protocol': p.MANAGED_CLAUDE_PROTOCOL, 'label': 'hub-claude', 'profile': 'hub-hub-claude',
                   'account_id': 'claude', 'engine': 'claude', 'model': 'opus', 'effort': 'medium',
                   'session_id': 'proof', 'operation_id': 'op-proof:0', 'epoch': 3, 'workspace': str(tmp_path),
                   'task_id': 'hub2-proof', 'server': 'https://hub.test',
                   'runtime_deadline': int(p.time.time() * 1000) + 60000, 'request_digest': 'b' * 64,
                   'machine_id': 'host', 'exec_archive_sha256': 'a' * 64}
        archive = tmp_path / 'synthetic.pyz'
        archive.write_bytes(b'synthetic reviewed archive')
        context['exec_archive_sha256'] = p.hashlib.sha256(archive.read_bytes()).hexdigest()
        monkeypatch.setattr(p, '__file__', str(archive / 'agent_hub_host/provisioning.py'))
        monkeypatch.setattr(p, 'managed_exec_readiness', lambda archive: {'protocol': p.MANAGED_CLAUDE_PROTOCOL})
        self.record = managed / 'attempt.json'
        p.write_json(self.record, context)
        b = {'label': 'hub-claude', 'provider': 'claude', 'version': p.engine_versions.package_version('claude'),
             'revision': '22222222-2222-4222-8222-222222222222', 'files': {}, 'env': {}, 'args': []}
        dest = managed / (b['label'] + '-' + b['revision'])
        dest.mkdir(mode=0o700)
        p.write_json(dest / 'bundle.json', b)
        p.write_json(managed / 'state.json', [{k: b[k] for k in ('label', 'provider', 'revision')}])
        self.home = dest / 'home'
        self.home.mkdir(mode=0o700)
        (self.home / '.claude').mkdir(mode=0o700)
        monkeypatch.setattr(p, 'provision_managed_home', lambda *a: None)
        monkeypatch.setattr(p, 'install_managed_engine_permissions', lambda *a, **kw: None)
        monkeypatch.setattr(p, 'engine_argv', lambda *a: ['/synthetic/claude'])
        monkeypatch.setattr(p, 'engine_environment', lambda *a: {'PATH': 'synthetic'})
        monkeypatch.setattr(p, 'profile_logged_in', lambda *a: True)
        monkeypatch.setattr(p, 'MANAGED_CLAUDE_SYSTEM_SETTINGS', ())
        self.materialized = []
        self.requests = []
        monkeypatch.setattr(credentials, 'fetch_current', lambda *a: self.materialized.append('fetch') or
                            {'generation': 6, 'files': {}})
        monkeypatch.setattr(credentials, 'reconcile', lambda *a, **kw: self.materialized.append('reconcile') or
                            {'status': 'adopted_hub', 'generation': 6})

        def request(method, path, body):
            self.requests.append((method, path, body))
            return {'status': 'recorded'} if path.endswith('sync-report') else {
                'launch_allowed': True, 'credential_generation': 6}
        monkeypatch.setattr(native_import_host, 'bind_managed_claude', lambda base: ('host', request))
        native = {'id': '11111111-2222-4333-8444-555555555555', 'tag': 'task-hub2-proof', 'workspace': str(tmp_path),
                  'created_at_ms': int(p.time.time() * 1000) - 1000, 'phase': 'running',
                  'command': ['a', 'task', 'run', '--profile', 'hub-hub-claude',
                              '--engine-arg=' + p.MANAGED_CONTEXT_FLAG + str(self.record)]}
        monkeypatch.setattr(p.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(native), ''))
        self.launches = []
        monkeypatch.setattr(p.os, 'execve', lambda command, argv, env: self.launches.append((command, argv, env)))
        monkeypatch.setattr(sys, 'argv', ['hub', 'hub-claude', p.MANAGED_CONTEXT_FLAG + str(self.record), '--model',
                                          'opus', '--hub-session-effort=medium', '--output-format', 'stream-json',
                                          '-p', 'synthetic prompt'])

    @property
    def receipt(self):
        return self.record.with_suffix('.receipt.json')


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


@pytest.mark.parametrize('parts', METADATA_PATHS)
def test_composed_launch_gets_through_with_legitimate_large_metadata(harness, parts):
    path = put(harness.home, parts, sized(75 * 1024))
    before = path.read_bytes()
    p.execute()
    assert len(harness.launches) == 1 and harness.launches[0][2]['CLAUDE_CONFIG_DIR'] == str(harness.home / '.claude')
    assert harness.materialized == ['fetch', 'reconcile']
    assert [r[1] for r in harness.requests] == ['/api/engines/sync-report', '/api/engines/admission']
    assert harness.requests[0][2]['items'][0]['credential_generation'] == 6
    assert json.loads(harness.receipt.read_text())['credential_generation'] == 6
    assert path.read_bytes() == before


def test_composed_launch_with_large_metadata_at_both_locations(harness):
    for parts in METADATA_PATHS:
        put(harness.home, parts, sized(300 * 1024, env={}))
    p.execute()
    assert len(harness.launches) == 1 and harness.receipt.exists()


def assert_refused_before_materialization(harness, capsys, reason='managed_settings_unverified'):
    with pytest.raises(SystemExit) as result:
        p.execute()
    assert result.value.code == 75
    assert json.loads(capsys.readouterr().err) == {'provider': 'claude', 'launch_allowed': False, 'reason': reason}
    assert harness.materialized == [] and harness.requests == [] and harness.launches == []
    assert not harness.receipt.exists()


@pytest.mark.parametrize('parts', METADATA_PATHS)
@pytest.mark.parametrize('late', [{'apiKeyHelper': 'synthetic'}, {'primaryApiKey': 'synthetic'},
                                   {'env': {'ANTHROPIC_AUTH_TOKEN': 'synthetic'}}, {'forceLoginMethod': 'console'}])
def test_composed_late_auth_routing_after_padding_never_materializes_acks_or_launches(harness, capsys, parts, late):
    put(harness.home, parts, sized(200 * 1024, **late))
    assert_refused_before_materialization(harness, capsys)


@pytest.mark.parametrize('parts', METADATA_PATHS)
@pytest.mark.parametrize('raw', [sized(METADATA_LIMIT + 1), sized(200 * 1024)[:-1], b'[1,2,3]', b'7'])
def test_composed_oversized_malformed_or_scalar_metadata_never_materializes_acks_or_launches(harness, capsys, parts, raw):
    put(harness.home, parts, raw)
    assert_refused_before_materialization(harness, capsys)


def test_composed_oversized_settings_with_valid_large_metadata_never_materializes_acks_or_launches(harness, capsys):
    put(harness.home, METADATA_PATHS[0], sized(75 * 1024))
    put(harness.home, SETTINGS_PATHS[0], sized(SETTINGS_LIMIT + 1))
    assert_refused_before_materialization(harness, capsys)


def test_composed_system_policy_with_valid_large_metadata_never_materializes_acks_or_launches(
        harness, capsys, monkeypatch):
    put(harness.home, METADATA_PATHS[0], sized(75 * 1024))
    policy = harness.tmp / 'managed-settings.json'
    policy.write_text('{}')
    monkeypatch.setattr(p, 'MANAGED_CLAUDE_SYSTEM_SETTINGS', (str(policy),))
    assert_refused_before_materialization(harness, capsys)


@pytest.mark.parametrize('parts', METADATA_PATHS)
@pytest.mark.parametrize('mode', [0o644, 0o666])
def test_composed_non_private_large_metadata_never_materializes_acks_or_launches(harness, capsys, parts, mode):
    put(harness.home, parts, sized(75 * 1024), mode=mode)
    assert_refused_before_materialization(harness, capsys)


def test_composed_symlinked_large_metadata_never_materializes_acks_or_launches(harness, capsys):
    target = harness.tmp / 'elsewhere.json'
    target.write_bytes(sized(75 * 1024))
    target.chmod(0o600)
    (harness.home / '.claude.json').symlink_to(target)
    assert_refused_before_materialization(harness, capsys)
