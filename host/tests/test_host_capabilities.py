"""Host capability tags for Hub --needs routing (intake 6cb8890f AC2)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_hub_host import fleet

HOST = Path(__file__).resolve().parents[1]
DESKTOP_TAGS = ['browser:orin-chrome', 'phone:adb', 'tool:smscli', 'tool:agent-browser']


def settings(tmp_path, **extra):
    (tmp_path / 'host').mkdir(exist_ok=True)
    path = tmp_path / 'host' / 'subscriptions.json'
    data = {'version': 1, 'subscriptions': [], 'capabilities': ['legacy'], 'max_agents': 2,
            'workspaces': {'home': str(tmp_path)}, **extra}
    path.write_text(json.dumps(data), encoding='utf-8')
    os.chmod(path, 0o600)
    return path


def test_add_remove_preserves_other_settings(tmp_path):
    path = settings(tmp_path)
    assert fleet.edit_capabilities(path, add=DESKTOP_TAGS + ['legacy']) == ['legacy'] + DESKTOP_TAGS
    assert fleet.edit_capabilities(path, remove=['legacy']) == DESKTOP_TAGS
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['max_agents'] == 2 and data['workspaces'] == {'home': str(tmp_path)}
    if os.name != 'nt':
        assert (path.stat().st_mode & 0o777) == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ['subscriptions.json']


def test_advertisement_carries_tags_as_needs(tmp_path):
    path = settings(tmp_path)
    fleet.edit_capabilities(path, add=DESKTOP_TAGS, remove=['legacy'])
    loaded = fleet.load_settings(str(path))
    dispatcher = fleet.Dispatcher.__new__(fleet.Dispatcher)
    dispatcher.settings = loaded
    assert dispatcher.capabilities_put_payload()['needs'] == DESKTOP_TAGS


@pytest.mark.parametrize('tag', ['BAD', 'tool smscli', '', ':x'])
def test_invalid_tag_leaves_file_unchanged(tmp_path, tag):
    path = settings(tmp_path)
    before = path.read_text(encoding='utf-8')
    with pytest.raises(ValueError):
        fleet.edit_capabilities(path, add=[tag])
    assert path.read_text(encoding='utf-8') == before


def test_cli_lists_and_rejects(tmp_path):
    path = settings(tmp_path)
    run = lambda *a: subprocess.run([sys.executable, '-m', 'agent_hub_host.cli', 'host-capabilities', '--config', str(path), *a],
                                    cwd=HOST, capture_output=True, text=True, timeout=60)
    ok = run('--add', 'tool:smscli')
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout) == {'capabilities': ['legacy', 'tool:smscli'], 'changed': True}
    assert json.loads(run().stdout) == {'capabilities': ['legacy', 'tool:smscli'], 'changed': False}
    bad = run('--add', 'BAD')
    assert bad.returncode == 2 and 'Invalid capability tag' in bad.stderr and 'Traceback' not in bad.stderr
