"""Goal starts preserve interactive semantics and submit Codex goals at a ready TUI."""
import argparse
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from agent_hub_host import fleet, session_control, session_rules
from agent_hub_host.session_rules import (
    AWS_MAIN_TEMP_RULE,
    CONTAINER_HYGIENE_RULE,
    GOAL_CHAR_LIMIT,
    compose_prompt,
)
from test_fleet import settings, offer


def parse(*args):
    p = argparse.ArgumentParser()
    session_control.add_commands(p.add_subparsers(dest='cmd'))
    return p.parse_args(['sessions', 'start', '--workspace', 'demo', *args])


def test_goal_cli_implies_interactive_and_preserves_prompt():
    with patch.object(session_control, 'request', return_value={'id': 's'}) as req:
        session_control.execute(parse('--with-goal', '--prompt', 'Fix flaky binding', '--key', 'goal-1'))
    assert req.call_args.args[2] == {'key': 'goal-1', 'workspace': 'demo', 'mode': 'interactive', 'with_goal': True, 'brief': 'Fix flaky binding'}


@pytest.mark.parametrize('args', [('--with-goal', '--headless', '--prompt', 'fix'), ('--with-goal',)])
def test_invalid_goal_cli_has_no_http_effect(args):
    with patch.object(session_control, 'request') as req, pytest.raises(ValueError):
        session_control.execute(parse(*args))
    req.assert_not_called()


def test_goal_native_start_uses_trusted_command_and_literal_argument(monkeypatch):
    # The shared preamble is larger than the engine goal limit, so compact it
    # here to exercise the inline literal-argument path. The oversized path
    # (pointer + private full brief) is covered by dedicated tests below.
    monkeypatch.setattr(session_rules, 'SESSION_PREAMBLE', 'Hub session rules:\n' + AWS_MAIN_TEMP_RULE)
    monkeypatch.setattr(fleet.Dispatcher, '_seed_interactive_readiness', lambda *a: None)
    registered = []
    monkeypatch.setattr('agent_hub_host.goal_registration.register_goal',
                        lambda *a, **kw: registered.append(kw) or {'status': 'active', 'thread_id': 'thread-1'})
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        cfg['subscriptions'][0]['startup_argv'] = ['trusted-codex', '--safe']
        calls, http = [], []
        def runner(argv, **kw):
            calls.append(argv)
            if argv[0] == 'maintained-gate':
                return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
            return {'id': 'native', 'workspace': tmp, 'tag': 'hub-' + offer()['id'], 'engine': 'codex', 'profile': None, 'phase': 'running'}
        d = fleet.Dispatcher(cfg, lambda *a: http.append(a) or {}, runner)
        result = d.dispatch(offer(with_goal=True, prompt='Fix $(literal) `text`'))
        assert result['state'] == 'running'
        start = calls[-1]
        assert start[-3:] == ['--', 'trusted-codex', '--safe']
        assert not any(isinstance(arg, str) and arg.startswith('/goal ') for arg in start)
        goal = fleet.launch_goal('', 'Fix $(literal) `text`', offer()['id'])
        assert goal.startswith('/goal ' + compose_prompt('Fix $(literal) `text`') + '\n')
        assert registered[0]['goal_command'] == goal
        assert registered[0]['engine'] == 'codex'
        assert AWS_MAIN_TEMP_RULE in goal
        assert 'docker run --rm' in goal
        assert 'agent.session=' in goal
        assert 'trap/finally' in goal
        with pytest.raises(ValueError):
            d.dispatch(offer(with_goal=True, mode='headless', prompt='fix'))


def test_managed_codex_goal_route_has_admission_and_uses_installed_execution(tmp_path, monkeypatch):
    from agent_hub_host.model_contract import policy_models, native_model
    monkeypatch.setattr(session_rules, 'SESSION_PREAMBLE', 'Hub session rules:\n' + AWS_MAIN_TEMP_RULE)
    monkeypatch.setattr(fleet.Dispatcher, '_seed_interactive_readiness', lambda *a: None)
    registered = []
    monkeypatch.setattr('agent_hub_host.goal_registration.register_goal',
                        lambda *a, **kw: registered.append(kw) or {'status': 'active', 'thread_id': 'thread-1'})
    selected = next(m for m in policy_models() if m['id'] == 'astra')
    sub = dict(service='codex', engine='codex', account_label='shared-codex',
               profile='hub-shared-codex', admission_argv=['managed-admission'],
               launcher_provider='codex', launcher_model='gpt-6.1-sol')
    cfg = dict(version=1, workspaces={'demo': str(tmp_path)}, subscriptions=[sub],
               launcher=dict(argv=['launcher'], root=str(tmp_path), config_dir=str(tmp_path)))
    calls = []
    def runner(argv, **kw):
        calls.append(argv)
        if argv == ['managed-admission']:
            return dict(launch_allowed=True, profile_supported=True, model_supported=True,
                        model=selected['model'], effort=selected['effort'],
                        account_label='shared-codex', observed_at=fleet.utcnow())
        if 'model-support' in argv:
            return dict(supported=True, provider='codex', model=selected['model'])
        command = argv[argv.index('--') + 1:]
        assert native_model(command) == selected['model']
        assert not any(isinstance(arg, str) and arg.startswith('/goal ') for arg in command)
        assert "from agent_hub_host.provisioning import execute" in command[2]
        assert 'shared-codex' in command
        return dict(id='native', workspace=str(tmp_path), tag='hub-' + offer()['id'],
                    engine='codex', profile=sub['profile'], phase='running', command=command)
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[sub]):
        d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
        assert d.goal_routes() == [dict(engine='codex', account='shared-codex', profile=sub['profile'])]
        assert d.dispatch(offer(with_goal=True, profile=sub['profile'], prompt='Literal goal', selection=selected))['state'] == 'running'
    assert len(calls) == 3
    assert len(registered) == 1 and registered[0]['engine'] == 'codex'
    assert registered[0]['goal_command'].startswith('/goal ' + compose_prompt('Literal goal'))


def test_goal_advertisement_excludes_unlaunchable_accounts(tmp_path):
    cfg = settings(tmp_path)
    cfg['subscriptions'] += [dict(service='zai', engine='zcodex', account_label='shared-zai',
                                  profile=None, admission_argv=['gate']),
                             dict(service='codex', engine='codex', account_label='monitor-only',
                                  profile=None, startup_argv=['codex'])]
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a, **kw: {})
        assert d.goal_routes() == []
        cfg['subscriptions'][0]['startup_argv'] = ['codex']
        assert d.goal_routes() == [dict(engine='codex', account='shared-codex', profile=None)]


ZCODEX_STARTUP_ARGV = ['/usr/local/bin/zcodex', '--dangerously-bypass-approvals-and-sandbox',
                       '-c', 'check_for_update_on_startup=false']


def test_zcodex_goal_route_advertised_and_started_from_trusted_argv(tmp_path, monkeypatch):
    """Zcodex receives /goal through guarded TUI delivery, not initial argv."""
    monkeypatch.setattr(session_rules, 'SESSION_PREAMBLE', 'Hub session rules:\n' + AWS_MAIN_TEMP_RULE)
    monkeypatch.setattr(fleet.Dispatcher, '_seed_interactive_readiness', lambda *a: None)
    registered = []
    monkeypatch.setattr('agent_hub_host.goal_registration.register_goal',
                        lambda *a, **kw: registered.append(kw) or {'status': 'active', 'thread_id': 'thread-1'})
    cfg = settings(tmp_path)
    cfg['subscriptions'] += [dict(service='zai', engine='zcodex', account_label='shared-zai',
                                  profile=None, admission_argv=['gate'],
                                  startup_argv=list(ZCODEX_STARTUP_ARGV))]
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if argv[0] == 'gate':
            return {'launch_allowed': True, 'profile_supported': True,
                    'account_label': 'shared-zai', 'observed_at': fleet.utcnow()}
        return {'id': 'native', 'workspace': str(tmp_path), 'tag': 'hub-' + offer()['id'],
                'engine': 'zcodex', 'profile': None, 'phase': 'running'}

    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
        assert {'engine': 'zcodex', 'account': 'shared-zai', 'profile': None} in d.goal_routes()
        result = d.dispatch(offer(engine='zcodex', account='shared-zai',
                                  with_goal=True, prompt='Fix $(literal) `text`'))
    assert result['state'] == 'running'
    assert calls[-1][-len(ZCODEX_STARTUP_ARGV) - 1:] == ['--'] + ZCODEX_STARTUP_ARGV
    assert not any(isinstance(arg, str) and arg.startswith('/goal ') for arg in calls[-1])
    goal_arg = fleet.launch_goal('', 'Fix $(literal) `text`', offer()['id'])
    assert goal_arg.startswith('/goal ' + compose_prompt('Fix $(literal) `text`') + '\n')
    assert registered[0]['goal_command'] == goal_arg
    assert registered[0]['engine'] == 'zcodex'
    assert AWS_MAIN_TEMP_RULE in goal_arg
    assert 'docker run --rm' in goal_arg
    assert 'agent.session=' in goal_arg
    assert 'trap/finally' in goal_arg


def test_zcodex_goal_without_trusted_startup_refuses_before_native_start(tmp_path):
    """An unmanaged zcodex subscription without a trusted startup argv stays
    out of the goal advertisement and refuses a goal start with no effect."""
    cfg = settings(tmp_path)
    cfg['subscriptions'] += [dict(service='zai', engine='zcodex', account_label='shared-zai',
                                  profile=None, admission_argv=['gate'])]
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if argv[0] == 'gate':
            return {'launch_allowed': True, 'profile_supported': True,
                    'account_label': 'shared-zai', 'observed_at': fleet.utcnow()}
        return {'id': 'native', 'workspace': str(tmp_path), 'tag': 'hub-' + offer()['id'],
                'engine': 'zcodex', 'profile': None, 'phase': 'running'}

    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
        assert d.goal_routes() == []
        with pytest.raises(ValueError, match='Goal startup requires a trusted local command'):
            d.dispatch(offer(engine='zcodex', account='shared-zai', with_goal=True, prompt='Fix'))
    assert not [c for c in calls if c[:1] == ['aplexer']]


def test_plain_interactive_preamble_queues_same_workspace(tmp_path, monkeypatch):
    """Empty interactive preamble must be a same-workspace queued send.

    A --workspace send to the not-yet-existing native tag is rejected as a
    cross-workspace queued target before native binding exists; the host
    bridge's durable queued-send shape (cwd, no --workspace) is required.
    Codex goal starts submit their goal through the registration helper and
    must not queue the ordinary session preamble.
    """
    from agent_hub_host.session_rules import compose_prompt
    cfg = settings(tmp_path)
    cfg['subscriptions'][0]['startup_argv'] = ['trusted-codex', '--safe']
    monkeypatch.setattr(fleet.Dispatcher, '_seed_interactive_readiness', lambda *a: None)
    monkeypatch.setattr('agent_hub_host.goal_registration.register_goal',
                        lambda *a, **kw: {'status': 'active', 'thread_id': 'thread-1'})
    calls, http = [], []

    def runner(argv, **kw):
        calls.append((argv, kw))
        if argv[0] == 'maintained-gate':
            return {'launch_allowed': True, 'profile_supported': True,
                    'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
        if argv[1:3] == ['message', 'send']:
            return {'id': 'queued-preamble'}
        return {'id': 'native', 'workspace': str(tmp_path), 'tag': 'hub-' + offer()['id'],
                'engine': 'codex', 'profile': None, 'phase': 'running'}

    def request(method, path, body=None):
        http.append((method, path, body))
        return {}

    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(cfg, request, runner)
        assert d.dispatch(offer())['state'] == 'running'
    preamble = [c for c in calls if len(c[0]) > 2 and c[0][1:3] == ['message', 'send']]
    assert len(preamble) == 1
    argv, kw = preamble[0]
    assert argv == ['aplexer', 'message', 'send', '--to', 'hub-' + offer()['id'], '--queue',
                    '--kind', 'hub-session-preamble', '--json', compose_prompt("")]
    assert '--workspace' not in argv
    assert kw.get('cwd') == str(tmp_path)

    calls.clear()
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(cfg, request, runner)
        assert d.dispatch(offer(with_goal=True, prompt='Literal goal'))['state'] == 'running'
    assert not [c for c in calls if len(c[0]) > 2 and c[0][1:3] == ['message', 'send']
                and 'hub-session-preamble' in c[0]]
    assert not any(isinstance(arg, str) and arg.startswith('/goal ')
                   for argv, _ in calls if argv[:2] == ['aplexer', 'start'] for arg in argv)


@pytest.mark.parametrize(('stop_state', 'expected_state'), [('stopped', 'failed'), ('uncertain', 'running')])
def test_goal_registration_failure_reports_visible_receipt_without_retry(tmp_path, monkeypatch, stop_state, expected_state):
    from agent_hub_host.goal_registration import GoalRegistrationError
    cfg = settings(tmp_path)
    cfg['subscriptions'][0]['startup_argv'] = ['trusted-codex', '--safe']
    monkeypatch.setattr(fleet.Dispatcher, '_seed_interactive_readiness', lambda *a: None)
    registration_calls = []
    stop_calls = []

    def fail_registration(*args, **kwargs):
        registration_calls.append((args, kwargs))
        raise GoalRegistrationError()

    monkeypatch.setattr('agent_hub_host.goal_registration.register_goal', fail_registration)
    monkeypatch.setattr('agent_hub_host.native_sessions.stop_session',
                        lambda *args, **kwargs: stop_calls.append((args, kwargs)) or SimpleNamespace(state=stop_state))
    calls, http = [], []
    native_id = '12345678-1234-4234-8234-123456789abc'

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'maintained-gate':
            return {'launch_allowed': True, 'profile_supported': True,
                    'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
        return {'id': native_id, 'workspace': str(tmp_path), 'tag': 'hub-' + offer()['id'],
                'engine': 'codex', 'profile': None, 'phase': 'running', 'created_at_ms': 1793707200000}

    def request(method, path, body=None):
        http.append((method, path, body))
        return {}

    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        result = fleet.Dispatcher(cfg, request, runner).dispatch(
            offer(with_goal=True, prompt='Fix the binding'))
    assert result['state'] == expected_state
    assert len(registration_calls) == 1
    assert len(stop_calls) == 1
    assert sum(argv[:2] == ['aplexer', 'start'] for argv in calls) == 1
    receipts = [body for method, path, body in http if method == 'POST' and path.endswith('/result')]
    assert len(receipts) == 1
    assert receipts[0]['diagnostic']['reason'] == 'goal_registration_failed'
    assert receipts[0]['diagnostic']['stage'] == 'dispatch_interactive'
    if stop_state == 'stopped':
        assert receipts[0]['confirmed_exit'] is True
    else:
        assert 'confirmed_exit' not in receipts[0]


def test_dispatch_refuses_goal_for_engine_without_goal_support(tmp_path):
    cfg = settings(tmp_path)
    cfg['subscriptions'].append({
        'service': 'gemini', 'engine': 'gemini', 'account_label': 'shared-gemini',
        'profile': None, 'admission_argv': ['gemini-gate'], 'startup_argv': ['gemini'],
    })
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'gemini-gate':
            return {'launch_allowed': True, 'profile_supported': True,
                    'account_label': 'shared-gemini', 'observed_at': fleet.utcnow()}
        return {}

    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        dispatcher = fleet.Dispatcher(cfg, lambda *a: {}, runner)
        with pytest.raises(ValueError, match='goal-capable engine'):
            dispatcher.dispatch(offer(engine='gemini', account='shared-gemini',
                                      with_goal=True, prompt='A goal'))
    assert not any(argv[:2] == ['aplexer', 'start'] for argv in calls)


def test_claude_goal_route_advertised_and_started_with_goal(tmp_path, monkeypatch):
    """Claude Code supports /goal (intake bf0863c4): a launchable Claude
    subscription advertises a goal route and the goal text is the literal
    final argument of the trusted startup command."""
    monkeypatch.setattr(session_rules, 'SESSION_PREAMBLE', 'Hub session rules:\n' + AWS_MAIN_TEMP_RULE)
    cfg = settings(tmp_path)
    cfg['subscriptions'] += [dict(service='claude', engine='claude', account_label='shared-claude',
                                  profile=None, admission_argv=['gate'],
                                  startup_argv=['trusted-claude', '--dangerously-skip-permissions'])]
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if argv[0] == 'gate':
            return {'launch_allowed': True, 'profile_supported': True,
                    'account_label': 'shared-claude', 'observed_at': fleet.utcnow()}
        return {'id': 'native', 'workspace': str(tmp_path), 'tag': 'hub-' + offer()['id'],
                'engine': 'claude', 'profile': None, 'phase': 'running'}

    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
        assert {'engine': 'claude', 'account': 'shared-claude', 'profile': None} in d.goal_routes()
        result = d.dispatch(offer(engine='claude', account='shared-claude', with_goal=True, prompt='Ship the fix'))
    assert result['state'] == 'running'
    assert calls[-1][-4:-1] == ['--', 'trusted-claude', '--dangerously-skip-permissions']
    assert calls[-1][-1].startswith('/goal ' + compose_prompt('Ship the fix') + '\n')


def test_oversized_claude_start_and_replacement_goals_use_full_private_brief(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    cfg['subscriptions'].append(dict(service='claude', engine='claude', account_label='shared-claude',
                                     profile=None, admission_argv=['claude-gate'],
                                     startup_argv=['trusted-claude', '--dangerously-skip-permissions']))
    calls = []
    monkeypatch.setattr(session_rules, 'goal_brief_dir', lambda: tmp_path / 'private-goals')

    def runner(argv, **kw):
        calls.append(argv)
        if argv[0] == 'claude-gate':
            return {'launch_allowed': True, 'profile_supported': True,
                    'account_label': 'shared-claude', 'observed_at': fleet.utcnow()}
        offer_id = next(part[4:] for part in argv if isinstance(part, str) and part.startswith('hub-'))
        return {'id': 'native-' + offer_id, 'workspace': str(tmp_path), 'tag': 'hub-' + offer_id,
                'engine': 'claude', 'profile': None, 'phase': 'running'}

    d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
    d._lock_session_worktree = lambda *a: True
    with patch('agent_hub_host.intake_worktree.launch_cwd',
               side_effect=lambda request, offer, workspace, target_cwd: (target_cwd, '', None)):
        cases = [
            offer(id='initial-goal', engine='claude', account='shared-claude', with_goal=True,
                  prompt='Initial brief. ' + 'A' * 5000),
            offer(id='replacement-goal', engine='claude', account='shared-claude', with_goal=True,
                  prompt='Replacement brief. ' + 'B' * 5000, parent_id='stalled-session',
                  intake_task_id='7970260d-0158-4a53-accd-445666555317', intake_worktree='successor'),
        ]
        for item in cases:
            result = d.dispatch(item)
            assert result['state'] == 'running'
            goal = calls[-1][-1]
            assert goal.startswith('/goal ')
            assert len(goal) <= GOAL_CHAR_LIMIT
            assert 'Hub session rules and the full brief are in ' in goal
            path_start = goal.index('Hub session rules and the full brief are in ') + len(
                'Hub session rules and the full brief are in ')
            path_end = goal.index('. Read that file completely', path_start)
            stored = Path(goal[path_start:path_end])
            assert stored.read_text() == compose_prompt(item['prompt']) + '\n' + CONTAINER_HYGIENE_RULE

    assert len([c for c in calls if c[0] == 'aplexer']) == 2
