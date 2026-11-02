"""zcodex on Windows: hooks must parse in PowerShell and the warm bridge must run in yolo mode."""
import json
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import provisioning as p


def test_windows_zai_hooks_are_not_bash_syntax(tmp_path, monkeypatch):
    monkeypatch.setattr(p, '_windows_hooks', lambda: True)
    monkeypatch.setattr(p.state_hook_commands, 'sys', type('S', (), {'platform': 'win32'}))
    monkeypatch.setenv('HOME', str(tmp_path)); monkeypatch.setenv('USERPROFILE', str(tmp_path))
    home = tmp_path / 'managed' / 'home'
    p.install_aplexer_state_hooks({'provider': 'zai'}, home)
    for rel in ('.codex/hooks.json', '.zcodex/hooks.json'):
        text = (home / rel).read_text(encoding='utf-8')
        data = json.loads(text)
        commands = [h['command'] for groups in data['hooks'].values() for g in groups for h in g['hooks']]
        assert commands
        for command in commands:
            assert command.startswith('powershell.exe ')
            assert '|| true' not in command and '/dev/null' not in command


def test_windows_ignores_bash_user_template(tmp_path, monkeypatch):
    monkeypatch.setattr(p, '_windows_hooks', lambda: True)
    monkeypatch.setattr(p.state_hook_commands, 'sys', type('S', (), {'platform': 'win32'}))
    monkeypatch.setenv('HOME', str(tmp_path)); monkeypatch.setenv('USERPROFILE', str(tmp_path))
    (tmp_path / '.codex').mkdir()
    (tmp_path / '.codex' / 'hooks.json').write_text(json.dumps(
        {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'a state-report idle || true'}]}]}}))
    home = tmp_path / 'm' / 'home'
    p.install_aplexer_state_hooks({'provider': 'zai'}, home)
    assert '|| true' not in (home / '.zcodex' / 'hooks.json').read_text(encoding='utf-8')


def test_zai_engine_environment_sets_warm_yolo(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'locate_zcode_runtime', lambda dest=None: None)
    monkeypatch.delenv('ZCODE_WARM_MODE', raising=False)
    monkeypatch.delenv('ZCODE_WARM', raising=False)
    env = p.engine_environment({'provider': 'zai'}, tmp_path / 'home', tmp_path)
    assert env['ZCODE_WARM'] == '1' and env['ZCODE_WARM_MODE'] == 'yolo'


def test_zai_engine_environment_keeps_operator_override(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'locate_zcode_runtime', lambda dest=None: None)
    monkeypatch.setenv('ZCODE_WARM_MODE', 'plan')
    env = p.engine_environment({'provider': 'zai'}, tmp_path / 'home', tmp_path)
    assert env['ZCODE_WARM_MODE'] == 'plan'


def test_non_zai_environment_has_no_warm_mode(tmp_path, monkeypatch):
    monkeypatch.delenv('ZCODE_WARM_MODE', raising=False)
    env = p.engine_environment({'provider': 'codex'}, tmp_path / 'home', tmp_path)
    assert 'ZCODE_WARM_MODE' not in env
