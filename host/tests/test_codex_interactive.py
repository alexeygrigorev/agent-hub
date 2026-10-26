from agent_hub_host.codex_interactive import isolated_argv, reconnect_failure

FATAL_SCREEN = ('■ Automatic reconnect could not restore this session. Your draft is still\n'
                'editable.\n■ Server connection could not be restored\n'
                '• Reconnect failed — check the endpoint, then relaunch (24m)\n\n'
                '› Ask Codex to do anything\n\n'
                '  GPT-6.1-Sol default · /repo · Goal stalled (/goal resume)\n  ^c quit')


def test_interactive_owns_its_server_without_changing_prompt():
    args = ['--model', 'gpt-6.1-sol', '/goal perform exec and review work']
    assert isolated_argv('codex', args) == ['--no-daemon', *args]
    assert args[0] == '--model'
    assert isolated_argv('codex', ['--no-daemon', *args]).count('--no-daemon') == 1


def test_headless_and_other_providers_keep_their_contract():
    for args in (['exec', '--json', 'task'], ['review'], ['app-server'], ['--help'], ['--version']):
        assert isolated_argv('codex', args) == args
    assert isolated_argv('zai', ['/goal task']) == ['/goal task']


def test_dead_tui_requires_complete_fatal_screen():
    screen = FATAL_SCREEN
    assert reconnect_failure(screen)
    assert reconnect_failure(screen.replace('—', '-'))
    assert reconnect_failure(screen.replace('could not', 'could\nnot').replace('then relaunch', 'then\nrelaunch'))
    assert not reconnect_failure('Server connection could not be restored')
    assert not reconnect_failure('Goal stalled (/goal resume)')
    assert not reconnect_failure(None)
    assert not reconnect_failure('Here are the error strings from the bug report:\n'
                                 + screen + '\nInvestigation complete. Ready for next task.')
    assert not reconnect_failure(screen.replace('■ ', '').replace('• ', ''))
    assert not reconnect_failure(screen.replace('› Ask Codex to do anything', '› pasted draft of error report'))
    assert not reconnect_failure(screen.replace('^c quit', '? for shortcuts'))


def test_progress_detects_dead_tui_without_exit_or_cleanup():
    from agent_hub_host.session_progress import SessionProgress
    screen = FATAL_SCREEN
    clock = [120000]
    screens = [screen]
    row = {'id': 'native', 'created_at_ms': 1000, 'engine': 'codex',
           'tag': 'hub-offer', 'command': ['codex', '/goal task']}
    stops = []
    sampler = SessionProgress(now_ms=lambda: clock[0], output=lambda _: None,
                              screen=lambda _: screens[0], stop=lambda row: stops.append(row))
    result = sampler.sample([row])['native']
    assert result['terminal_error'] == 'codex_reconnect_failed'
    assert result['headless'] is False
    assert result['agent_alive'] is None
    assert stops == []
    screens[0] = 'Recovered normal TUI'
    clock[0] += 60000
    assert 'terminal_error' not in sampler.sample([row])['native']
    row['command'] = ['codex', 'exec', 'task']
    screens[0] = screen
    assert 'terminal_error' not in sampler.sample([row])['native']


def test_managed_execute_prepares_workspace_before_isolated_launch(monkeypatch, tmp_path):
    from agent_hub_host import provisioning as p
    import pytest
    label, revision = 'test-codex', '11111111-1111-4111-8111-111111111111'
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'managed'))
    p.write_json(p.root() / 'state.json', [{'label': label, 'revision': revision, 'provider': 'codex'}])
    dest = p.root() / (label + '-' + revision)
    bundle = {'label': label, 'provider': 'codex', 'version': '0.163.0', 'revision': revision,
              'files': {}, 'env': {}, 'args': []}
    p.write_json(dest / 'bundle.json', bundle)
    calls = []
    monkeypatch.setattr(p, 'provision_managed_home', lambda *args: None)
    monkeypatch.setattr(p, 'install_managed_engine_permissions', lambda b, home, trusted_paths: calls.append(('trust', trusted_paths)))
    monkeypatch.setattr(p, 'engine_argv', lambda *args: ['/test/codex'])
    monkeypatch.setattr(p, 'engine_environment', lambda *args: {})
    monkeypatch.setattr(p, 'check_codex_quota_gate', lambda env: (True, 'ok'))
    monkeypatch.setattr(p.sys, 'argv', ['provisioning', label, '/goal verify workspace'])

    def launched(binary, argv, env):
        calls.append(('exec', argv))
        raise SystemExit(0)

    monkeypatch.setattr(p.os, 'execve', launched)
    with pytest.raises(SystemExit):
        p.execute()
    assert calls[0][0] == 'trust'
    assert calls[0][1][0] == str(p.Path.cwd().resolve())
    assert calls[1][1] == ['/test/codex', '--no-daemon', '--dangerously-bypass-approvals-and-sandbox', '/goal verify workspace']


def test_exact_workspace_trust_upgrades_existing_table_and_preserves_siblings(tmp_path):
    import json
    import tomllib
    from agent_hub_host.provisioning import ensure_toml_trusted_projects
    workspace = str(tmp_path.resolve())
    content = ('model = "gpt-6.1-sol"\n'
               f'[projects.{json.dumps(workspace)}]\ntrust_level = "untrusted"\ncustom = "retained"\n'
               '[projects."/unrelated"]\ntrust_level = "untrusted"\n')
    result = ensure_toml_trusted_projects(content, [workspace])
    parsed = tomllib.loads(result)
    assert parsed['projects'][workspace] == {'trust_level': 'trusted', 'custom': 'retained'}
    assert parsed['projects']['/unrelated']['trust_level'] == 'untrusted'
    assert parsed['model'] == 'gpt-6.1-sol'
    assert ensure_toml_trusted_projects(result, [workspace]) == result
    assert tomllib.loads(ensure_toml_trusted_projects(f'[projects.{json.dumps(workspace)}]\n', [workspace]))['projects'][workspace]['trust_level'] == 'trusted'


def test_capture_budget_covers_later_rows_across_sampler_restarts(monkeypatch, tmp_path):
    from agent_hub_host import session_progress as p
    elapsed, now = [0.0], [120000]
    monkeypatch.setattr(p.time, 'monotonic', lambda: elapsed[0])
    checked = []
    rows = [{'id': f'native{i}', 'created_at_ms': 1000, 'engine': 'codex', 'state': 'running',
             'tag': f'hub-offer{i}', 'command': ['codex', '/goal task']} for i in range(12)]

    def screen(row):
        checked.append(row['id'])
        elapsed[0] += .5
        return FATAL_SCREEN if row['id'] == 'native11' else None

    def sampler():
        def slow_output(row):
            elapsed[0] += 1
            return None
        return p.SessionProgress(now_ms=lambda: now[0], output=slow_output,
                                 screen=screen, state_path=tmp_path / 'progress.json')

    first = sampler().sample(rows)
    assert checked == [f'native{i}' for i in range(4)]
    assert 'terminal_error' not in first['native11']
    now[0] += 60000
    sampler().sample(rows)
    now[0] += 60000
    third = sampler().sample(rows)
    assert third['native11']['terminal_error'] == 'codex_reconnect_failed'
    assert set(checked) == {f'native{i}' for i in range(12)}
