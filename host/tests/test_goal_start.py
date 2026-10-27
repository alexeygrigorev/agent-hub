"""Goal starts preserve interactive semantics and pass goal text as one argv."""
import argparse
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from agent_hub_host import fleet, session_control
from agent_hub_host.session_rules import AWS_MAIN_TEMP_RULE, compose_prompt
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


def test_goal_native_start_uses_trusted_command_and_literal_argument():
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
        assert calls[-1][-4:-1] == ['--', 'trusted-codex', '--safe']
        assert calls[-1][-1].startswith('/goal ' + compose_prompt('Fix $(literal) `text`') + '\n')
        assert AWS_MAIN_TEMP_RULE in calls[-1][-1]
        assert 'docker run --rm' in calls[-1][-1]
        assert 'agent.session=' in calls[-1][-1]
        assert 'trap/finally' in calls[-1][-1]
        with pytest.raises(ValueError):
            d.dispatch(offer(with_goal=True, mode='headless', prompt='fix'))


def test_managed_codex_goal_route_has_admission_and_uses_installed_execution(tmp_path):
    from agent_hub_host.model_contract import policy_models, native_model
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
        assert command[-1].startswith('/goal ' + compose_prompt('Literal goal'))
        assert "from agent_hub_host.provisioning import execute" in command[2]
        assert 'shared-codex' in command
        return dict(id='native', workspace=str(tmp_path), tag='hub-' + offer()['id'],
                    engine='codex', profile=sub['profile'], phase='running', command=command)
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[sub]):
        d = fleet.Dispatcher(cfg, lambda *a: {}, runner)
        assert d.goal_routes() == [dict(engine='codex', account='shared-codex', profile=sub['profile'])]
        assert d.dispatch(offer(with_goal=True, profile=sub['profile'], prompt='Literal goal', selection=selected))['state'] == 'running'
    assert len(calls) == 3


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


def test_zcodex_goal_route_advertised_and_started_from_trusted_argv(tmp_path):
    """t1654u: zcodex supports /goal — an unmanaged zcodex subscription with a
    trusted startup argv advertises a goal route and the launcher delivers the
    /goal brief as the literal final argument (intake 28302c74 AC1/AC2)."""
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
    assert calls[-1][-len(ZCODEX_STARTUP_ARGV) - 2:-1] == ['--'] + ZCODEX_STARTUP_ARGV
    goal_arg = calls[-1][-1]
    assert goal_arg.startswith('/goal ' + compose_prompt('Fix $(literal) `text`') + '\n')
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


def test_plain_interactive_preamble_queues_same_workspace(tmp_path):
    """Empty interactive preamble must be a same-workspace queued send.

    A --workspace send to the not-yet-existing native tag is rejected as a
    cross-workspace queued target before native binding exists; the host
    bridge's durable queued-send shape (cwd, no --workspace) is required.
    Goal starts carry /goal in native argv and must not queue a preamble.
    """
    from agent_hub_host.session_rules import SESSION_PREAMBLE
    cfg = settings(tmp_path)
    cfg['subscriptions'][0]['startup_argv'] = ['trusted-codex', '--safe']
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
                    '--kind', 'hub-session-preamble', '--json', SESSION_PREAMBLE]
    assert '--workspace' not in argv
    assert kw.get('cwd') == str(tmp_path)

    calls.clear()
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(cfg, request, runner)
        assert d.dispatch(offer(with_goal=True, prompt='Literal goal'))['state'] == 'running'
    assert not [c for c in calls if len(c[0]) > 2 and c[0][1:3] == ['message', 'send']]
