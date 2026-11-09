"""Intake 06d2a0ac: a managed Codex profile without a credential is refused
as not_logged_in (it would sit at the "Sign in with ChatGPT" screen), the
refusal is advertised as a route rejection, and dispatch reports the slug so
Hub re-places the session."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

from agent_hub_host import provisioning as p
from agent_hub_host.dispatch_diagnostics import classify


@pytest.fixture(autouse=True)
def isolated_aplexer(tmp_path, monkeypatch):
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer' / 'config.toml'))


REV = '11111111-1111-4111-8111-111111111111'


def run_admission(tmp_path, monkeypatch, bundle, home_auth=False):
    os.chmod(tmp_path, 0o700)
    m_dir = tmp_path / 'managed-engines'
    m_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setattr(p, 'root', lambda: m_dir)
    monkeypatch.setattr(p, 'host_request', lambda m, path, pl: {'launch_allowed': True, 'profile_supported': True, 'account_label': bundle['label']})
    dest = m_dir / f"{bundle['label']}-{bundle['revision']}"
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.write_json(dest / 'bundle.json', bundle)
    p.write_json(m_dir / 'state.json', [{'label': bundle['label'], 'revision': bundle['revision'], 'provider': bundle['provider']}])
    if home_auth:
        (dest / 'home' / '.codex').mkdir(parents=True)
        (dest / 'home' / '.codex' / 'auth.json').write_text('{"synthetic": true}')
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'profile': 'hub-' + bundle['label'], 'account': bundle['label']})))
    monkeypatch.setattr(sys, 'argv', ['admission', bundle['label']])
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    p.admission()
    return json.loads(out.getvalue())


def test_codex_profile_without_credential_is_not_logged_in(tmp_path, monkeypatch):
    dummy = dict(label='dummy-sync-5814', provider='codex', version='0.163.0', revision=REV, files={}, env={}, args=[])
    res = run_admission(tmp_path, monkeypatch, dummy)
    assert res['launch_allowed'] is False
    assert res['reason'] == 'not_logged_in'


def test_codex_profile_with_bundle_or_home_credential_is_admitted(tmp_path, monkeypatch):
    real = dict(label='shared-codex', provider='codex', version='0.163.0', revision=REV,
                files={'.codex/auth.json': '{"synthetic": true}'}, env={}, args=[])
    assert run_admission(tmp_path, monkeypatch, real)['launch_allowed'] is True
    home_only = dict(label='codex-orin', provider='codex', version='0.163.0', revision=REV, files={}, env={}, args=[])
    assert run_admission(tmp_path, monkeypatch, home_only, home_auth=True)['launch_allowed'] is True


def test_providers_without_a_file_probe_are_not_judged():
    assert p.profile_logged_in({'provider': 'go', 'files': {}}, '/nonexistent') is True
    assert p.profile_logged_in({'provider': 'codex', 'files': {}}, '/nonexistent') is False


def test_dispatch_classifies_not_logged_in():
    d = classify(ValueError('not_logged_in: the engine profile has no credential on this host'))
    assert d['reason'] == 'not_logged_in'
    assert d['stage'] == 'dispatch_route'


def test_admission_projection_advertises_not_logged_in(monkeypatch):
    from agent_hub_host import fleet
    svc = fleet.Dispatcher.__new__(fleet.Dispatcher)
    svc.settings = {'subscriptions': [{'service': 'codex', 'engine': 'codex', 'account_label': 'dummy-sync-5814',
                                       'profile': 'hub-dummy-sync-5814', 'admission_argv': ['x']}], 'workspaces': {}}
    svc.runner = lambda argv, payload=None: {'launch_allowed': False, 'profile_supported': True, 'reason': 'not_logged_in',
                                            'account_label': 'dummy-sync-5814', 'observed_at': fleet.utcnow()}
    caps = {'fleet_modes': ['interactive', 'headless'], 'fleet_route_rejections': []}
    allowed, engines = svc._admission_projection(caps)
    assert allowed is False and engines == set()
    assert {(r['mode'], r['reason'], r['profile']) for r in caps['fleet_route_rejections']} == {
        ('interactive', 'not_logged_in', 'hub-dummy-sync-5814'), ('headless', 'not_logged_in', 'hub-dummy-sync-5814')}


def test_claude_profile_logged_out_unless_credential_readable(tmp_path):
    cred = tmp_path / 'home/.claude/.credentials.json'
    cred.parent.mkdir(parents=True)
    claude = {'provider': 'claude', 'files': {}, 'env': {}}
    assert p.profile_logged_in(claude, tmp_path) is False  # missing
    cred.write_text('{"claudeAiOauth": {}}')
    assert p.profile_logged_in(claude, tmp_path) is True
    cred.chmod(0)  # hub-claude on 2026-11-09: owner cannot read it -> "Not logged in"
    if os.access(cred, os.R_OK):
        pytest.skip('running as root')
    assert p.profile_logged_in(claude, tmp_path) is False
    assert p.profile_logged_in({'provider': 'claude', 'files': {}, 'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'x'}}, tmp_path) is True


def test_claude_profile_without_credential_is_not_admitted(tmp_path, monkeypatch):
    bundle = dict(label='hub-claude', provider='claude', version='2.1.304', revision=REV, files={}, env={}, args=[])
    res = run_admission(tmp_path, monkeypatch, bundle)
    assert res['launch_allowed'] is False
    assert res['reason'] == 'not_logged_in'


def test_reconcile_local_read_heals_mode_000(tmp_path):
    from agent_hub_host import credentials
    f = tmp_path / '.credentials.json'
    f.write_text('{"a": 1}')
    f.chmod(0)
    assert credentials._local(f) == b'{"a": 1}'
    assert (f.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize('argv,expected', [
    (['exec', '--json', '-m', 'glm-5.3-flash', '-c', 'model_reasoning_effort=medium', 'do it'], True),
    (['exec', '--skip-git-repo-check', 'prompt'], True),
    (['exec', '-'], False),
    (['exec', '--json'], False),
    (['--version'], False),
    (['resume'], False),
])
def test_exec_prompt_gets_closed_stdin(argv, expected):
    assert p.stdin_closed_for_exec('zai', argv) is expected
    assert p.stdin_closed_for_exec('claude', argv) is False


def test_exec_hang_after_turn_started_is_gone_with_open_pipe_parent(tmp_path):
    """A fake engine that waits for stdin EOF must finish when the wrapper closes stdin."""
    import subprocess, sys
    child = ("import sys,os; from agent_hub_host import provisioning as p; "
             "p.stdin_closed_for_exec('codex', ['exec','x']) and os.dup2(os.open(os.devnull, os.O_RDONLY), 0); "
             "print(len(sys.stdin.read()))")
    proc = subprocess.Popen([sys.executable, '-c', child], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            env={**os.environ, 'PYTHONPATH': str(Path(p.__file__).parents[1])})
    try:
        assert proc.wait(timeout=20) == 0  # the parent keeps its stdin pipe open throughout
    finally:
        proc.kill()
    assert proc.stdout.read().strip() == b'0'


def test_execute_refuses_logged_out_claude_profile_loudly(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'managed'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: root)
    bundle = dict(label='hub-claude', provider='claude', version='2.1.304', files={}, env={}, args=[],
                  revision='22222222-2222-4222-8222-222222222222')
    dest = root / ('hub-claude-' + bundle['revision'])
    dest.mkdir(mode=0o700)
    p.write_json(dest / 'bundle.json', bundle)
    p.write_json(root / 'state.json', [{'label': 'hub-claude', 'revision': bundle['revision'], 'provider': 'claude'}])
    for name in ('provision_managed_home', 'refresh_launch_credential', 'install_managed_engine_permissions'):
        monkeypatch.setattr(p, name, lambda *a, **k: None)
    monkeypatch.setattr(p, 'engine_argv', lambda *a, **k: ['/bin/true'])
    monkeypatch.setattr(p, 'hub_launch_gate', lambda *_a, **_k: pytest.fail('gate must not run'))
    monkeypatch.setattr(sys, 'argv', ['x', 'hub-claude', '-p', 'hi'])
    with pytest.raises(SystemExit) as exc:
        p.execute()
    assert exc.value.code == 75
    assert '"reason": "not_logged_in"' in capsys.readouterr().err


def test_claude_bundle_copy_alone_is_not_a_login(tmp_path):
    bundle = {'provider': 'claude', 'files': {'.claude/.credentials.json': '{"x": 1}'}, 'env': {}}
    assert p.profile_logged_in(bundle, tmp_path) is False
