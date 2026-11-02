"""Managed Claude homes trust Hub work trees (intake 7833bf13, found in bf0863c4).

A Hub-created repo-identity work tree opened Claude at the folder-trust dialog
(Tabo 62f02136): the managed home trusted projects for Codex/zcodex only.
"""
import json
import os

from agent_hub_host import provisioning as p


def _home(tmp_path):
    os.chmod(tmp_path, 0o700)
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True, mode=0o700)
    return home


def _claude_json(home):
    return json.loads((home / '.claude' / '.claude.json').read_text(encoding='utf-8'))


def test_claude_launch_trusts_session_cwd_and_git_root(tmp_path, monkeypatch):
    monkeypatch.setattr(p.hook_command, 'tool_command', lambda name: '/usr/bin/a')
    home = _home(tmp_path)
    work = str(tmp_path / 'work' / 'bf0863c4' / 'agent-hub')
    p.install_managed_engine_permissions({'provider': 'claude'}, home, trusted_paths=[work + '/sub', work])
    projects = _claude_json(home)['projects']
    assert projects[work]['hasTrustDialogAccepted'] is True
    assert projects[work + '/sub']['hasTrustDialogAccepted'] is True
    # A freshly created managed config must not open the first-run onboarding.
    assert _claude_json(home)['hasCompletedOnboarding'] is True


def test_windows_paths_get_the_forward_slash_key_too():
    assert p.claude_project_keys('C:\\Users\\alexe\\work\\bf0863c4\\agent-hub') == [
        'C:\\Users\\alexe\\work\\bf0863c4\\agent-hub', 'C:/Users/alexe/work/bf0863c4/agent-hub']
    assert p.claude_project_keys('/home/u/work/x') == ['/home/u/work/x']


def test_existing_claude_state_is_preserved(tmp_path):
    home = _home(tmp_path)
    path = home / '.claude' / '.claude.json'
    path.write_text(json.dumps({'oauthAccount': {'emailAddress': 'fixture'}, 'numStartups': 7,
                                'projects': {'/w': {'allowedTools': ['Bash'], 'hasTrustDialogAccepted': False}}}),
                    encoding='utf-8')
    p.ensure_claude_trusted_projects(path, ['/w', '/w2'])
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['oauthAccount'] == {'emailAddress': 'fixture'} and data['numStartups'] == 7
    assert data['hasCompletedOnboarding'] is True
    assert data['projects']['/w'] == {'allowedTools': ['Bash'], 'hasTrustDialogAccepted': True}
    assert data['projects']['/w2'] == {'hasTrustDialogAccepted': True}


def test_already_trusted_config_is_not_rewritten(tmp_path, monkeypatch):
    home = _home(tmp_path)
    path = home / '.claude' / '.claude.json'
    path.write_text(json.dumps({'hasCompletedOnboarding': True,
                                'projects': {'/w': {'hasTrustDialogAccepted': True}}}), encoding='utf-8')
    writes = []
    monkeypatch.setattr(p, '_write_engine_config', lambda *a: writes.append(a))
    p.ensure_claude_trusted_projects(path, ['/w'])
    assert writes == []


def test_unparsable_or_non_object_config_is_never_replaced(tmp_path):
    home = _home(tmp_path)
    path = home / '.claude' / '.claude.json'
    for content in ('{not json', '[1, 2]', '{"projects": []}'):
        path.write_text(content, encoding='utf-8')
        p.ensure_claude_trusted_projects(path, ['/w'])
        assert path.read_text(encoding='utf-8') == content


def test_refused_trust_write_does_not_block_the_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(p.hook_command, 'tool_command', lambda name: '/usr/bin/a')
    home = _home(tmp_path)

    def refuse(path, paths):
        raise OSError('read-only')
    monkeypatch.setattr(p, 'ensure_claude_trusted_projects', refuse)
    p.install_managed_engine_permissions({'provider': 'claude'}, home, trusted_paths=['/w'])
    assert (home / '.claude' / 'settings.json').is_file()
