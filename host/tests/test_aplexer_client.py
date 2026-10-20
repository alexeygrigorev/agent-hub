"""Transport contracts: SDK routing, scoped identity, failures and uv overlays."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub_host.aplexer_client import client_call


def test_published_dependency_loads_native_client(tmp_path):
    from aplexer import Client, _native
    from importlib.metadata import version
    assert version('aplexer-client') == '0.1.19'
    assert _native is not None
    assert Client(state_dir=tmp_path / 'state', runtime_dir=tmp_path / 'run').snapshot() == []


def test_scoped_target_resolves_uuid_and_kill_signal():
    client = Mock()
    client.snapshot.return_value = [dict(id='own-id', tag='same', workspace='/own'),
                                    dict(id='other-id', tag='same', workspace='/other')]
    with patch('aplexer.Client', return_value=client):
        result = client_call('aplexer', ['kill', '--workspace', '/own', '--tag', 'same',
                                          '--signal', 'TERM', '--grace-ms', '0', '--json'])
    assert result.returncode == 0
    client.kill.assert_called_once_with('own-id', signal=15, grace_ms=0)


def test_ambiguous_target_never_controls_session():
    client = Mock()
    client.snapshot.return_value = [dict(id=str(i), tag='same', workspace='/own') for i in range(2)]
    with patch('aplexer.Client', return_value=client):
        result = client_call('a', ['status', '--workspace', '/own', '--tag', 'same', '--json'])
    assert result.returncode == 1
    client.status.assert_not_called()


def test_client_error_does_not_retry_cli_or_leak_details():
    from aplexer import AplexerError
    with patch('aplexer.Client') as factory:
        factory.return_value.snapshot.side_effect = AplexerError('private-detail')
        result = client_call('a', ['list', '--all', '--json'])
    assert result.returncode == 1
    assert 'private-detail' not in result.stderr


def test_snapshot_status_capture_start_and_explicit_paths(tmp_path):
    client = Mock()
    client.snapshot.return_value = [dict(id='uuid', tag='tag', workspace='/own')]
    client.status.return_value = SimpleNamespace(raw={'id': 'uuid', 'worker_alive': True})
    client.capture.return_value = b'hello\xff'
    client.start.return_value = SimpleNamespace(raw={'id': 'new'})
    with patch('aplexer.Client', return_value=client) as factory:
        env = {'APLEXER_STATE_DIR': str(tmp_path), 'APLEXER_RUNTIME_DIR': '/run/explicit'}
        listed = client_call('a', ['list', '--all', '--json'], env=env)
        assert json.loads(listed.stdout)[0]['id'] == 'uuid'
        factory.assert_called_with(state_dir=str(tmp_path), runtime_dir='/run/explicit', config=None)
        assert json.loads(client_call('a', ['status', 'uuid', '--json']).stdout)['worker_alive']
        assert client_call('a', ['capture', 'uuid', '--bytes', '123']).stdout == 'hello\ufffd'
        client.capture.assert_called_once_with('uuid', max_bytes=123)
        assert json.loads(client_call('a', ['start', '--workspace', '/own', '--tag', 'tag',
                          '--engine', 'shell', '--pids', '256', '--', 'echo', 'hi']).stdout)['id'] == 'new'
        client.start.assert_called_once_with(workspace='/own', tag='tag', engine='shell',
                                             pids=256, command=['echo', 'hi'])


def test_cli_only_protocols_do_not_load_client():
    for executable, args in [('/experimental/a', ['list']), ('a', ['message', 'send']),
                             ('a', ['whoami']), ('a', ['capture', 'tag', '--screen', '--plain']),
                             ('systemd-run', ['--user', 'aplexer', 'start'])]:
        with patch('aplexer.Client', side_effect=AssertionError('unexpected Client')):
            assert client_call(executable, args) is None


def test_uv_path_override_leaves_production_lock_unchanged(tmp_path):
    """A local experimental package wins in uv's temporary overlay (no native build)."""
    repo = Path(__file__).resolve().parents[2]
    original = (repo / 'uv.lock').read_bytes()
    package = tmp_path / 'client'
    (package / 'aplexer').mkdir(parents=True)
    (package / 'pyproject.toml').write_text('''[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"
[project]
name = "aplexer-client"
version = "99.0.0"
''')
    (package / 'aplexer' / '__init__.py').write_text('class Client: experimental = True\n')
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env['TMPDIR'] = str(tmp_path)
    result = subprocess.run(['uv', 'run', '--frozen', '--with', str(package), 'python', '-c',
                             'from aplexer import Client; assert Client.experimental'],
                            cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert (repo / 'uv.lock').read_bytes() == original


def test_native_session_catalog_and_fleet_start_use_client():
    from agent_hub_host import native_sessions, fleet
    client = Mock()
    client.snapshot.return_value = []
    client.start.return_value = SimpleNamespace(raw={'id': 'new'})
    with patch('aplexer.Client', return_value=client), patch('subprocess.run', side_effect=AssertionError('CLI used')):
        assert native_sessions.list_sessions() == []
        result = fleet.run_json(['aplexer', 'start', '--workspace', '/own', '--tag', 'new',
                                 '--engine', 'shell', '--json'])
    assert result == {'id': 'new'}
    client.start.assert_called_once_with(command=[], workspace='/own', tag='new', engine='shell')


def test_conversation_bridge_catalog_uses_client():
    from agent_hub_host.conversation_bridge import NativeMailbox
    with patch('aplexer.Client') as factory, patch('subprocess.run', side_effect=AssertionError('CLI used')):
        factory.return_value.snapshot.return_value = []
        assert NativeMailbox().call('list') == []


def test_standalone_zipapp_without_sdk_preserves_binary_transport(tmp_path):
    """A clean Python process can load the adapter from zip without native wheels."""
    import zipfile
    source = Path(__file__).resolve().parents[1] / 'agent_hub_host' / 'aplexer_client.py'
    archive = tmp_path / 'bootstrap.pyz'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.write(source, 'aplexer_client.py')
    code = ('import sys; sys.path.insert(0, sys.argv[1]); '
            'from aplexer_client import client_call; '
            'assert client_call("aplexer", ["list", "--json"]) is None')
    result = subprocess.run([sys.executable, '-S', '-c', code, str(archive)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_zipapp_broken_installed_sdk_never_falls_back(tmp_path):
    import zipfile
    source = Path(__file__).resolve().parents[1] / 'agent_hub_host' / 'aplexer_client.py'
    archive = tmp_path / 'broken-sdk.pyz'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.write(source, 'aplexer_client.py')
        bundle.writestr('aplexer/__init__.py', 'raise ModuleNotFoundError("broken dependency", name="sdk_dependency")')
    code = '''import sys
sys.path.insert(0, sys.argv[1])
from aplexer_client import client_call
try:
    client_call('aplexer', ['list', '--json'])
except ModuleNotFoundError as exc:
    assert exc.name == 'sdk_dependency'
else:
    raise AssertionError('broken SDK fell back to CLI')
'''
    result = subprocess.run([sys.executable, '-S', '-c', code, str(archive)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_fleet_client_catalog_bound_and_failure():
    import pytest
    from aplexer import AplexerError
    from agent_hub_host.fleet import run_json
    with patch('aplexer.Client') as factory:
        factory.return_value.snapshot.return_value = [{'detail': 'x' * 200_000}]
        assert len(run_json(['aplexer', 'list', '--all', '--json'])) == 1
        factory.return_value.snapshot.return_value = [{'detail': 'x' * (8 * 1024 * 1024)}]
        with pytest.raises(RuntimeError, match='exceeds bound'):
            run_json(['aplexer', 'list', '--all', '--json'])
        factory.return_value.snapshot.side_effect = AplexerError('private error')
        with pytest.raises(RuntimeError, match='reconcile effects'):
            run_json(['aplexer', 'list', '--all', '--json'])
