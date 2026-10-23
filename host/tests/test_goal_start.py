"""Goal starts preserve interactive semantics and pass goal text as one argv."""
import argparse
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from agent_hub_host import fleet, session_control
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
        assert calls[-1][-4:] == ['--', 'trusted-codex', '--safe', '/goal Fix $(literal) `text`']
        with pytest.raises(ValueError):
            d.dispatch(offer(with_goal=True, mode='headless', prompt='fix'))
