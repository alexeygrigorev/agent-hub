"""Configured models must survive both fleet execution paths."""
import datetime as dt
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_hub_host.fleet import Dispatcher
from agent_hub_host.model_contract import configured_selection, policy_models, native_model
from agent_hub_host.session_control import render_table


@pytest.mark.parametrize('engine,provider,model_id', [
    ('antigravity', 'antigravity', 'argon'), ('codex', 'codex', 'astra'),
    ('codex', 'zai', 'glm-flash'),
    ('codex', 'codex', 'sol'), ('codex', 'codex', 'luna'),
    ('claude', 'claude', 'opus'), ('claude', 'claude', 'haiku'),
])
@pytest.mark.parametrize('mode', ['headless', 'interactive'])
def test_model_survives_default_subscription(engine, provider, model_id, mode):
    model = next(m for m in policy_models() if m['id'] == model_id and m['engine'] == engine)
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        profile = 'hub-test-account' if provider in ('codex', 'claude', 'zai') else None
        sub = dict(service=provider, engine=engine, account_label='test-account', profile=profile,
                   launcher_provider=provider, launcher_model='wrong-daily-default', admission_argv=['gate'],
                   startup_argv=['native-bin', '--model', 'wrong-daily-default'])
        settings = dict(version=1, workspaces={'main': tmp}, subscriptions=[sub],
                        launcher={'argv': ['launcher'], 'root': tmp, 'config_dir': tmp})
        offer = dict(id='offer1', engine=engine, account='test-account', profile=profile,
                     mode=mode, workspace='main', prompt='Check model' if mode == 'headless' else '', selection=model)
        calls = []
        def runner(argv, **kw):
            calls.append(argv)
            if argv == ['gate']:
                return dict(launch_allowed=True, profile_supported=True, model_supported=True,
                            model=model['model'], effort=model['effort'], account_label='test-account',
                            observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
            if 'model-support' in argv:
                return dict(supported=True, provider=provider, model=model['model'])
            if 'submit' in argv:
                payload = json.loads(argv[argv.index('--payload') + 1])
                assert payload['model_requirements']['models'] == [model['model']]
                return {'id': 'hub-offer1'}
            if 'plan' in argv:
                return {'eligible': [dict(provider=provider, model=model['model'], task_fit=1.0)]}
            command = argv[argv.index('--') + 1:] if mode == 'interactive' else ['native-bin', '--model', model['model']]
            if mode == 'interactive':
                assert native_model(command) == model['model']
                return dict(id='22222222-2222-4222-8222-222222222222', workspace=tmp, tag='hub-offer1',
                            engine=engine, profile=profile, phase='running', command=command)
            return {'receipt': dict(session_id='22222222-2222-4222-8222-222222222222', state='running',
                                    first_action_verified=True, model=model['model'], model_verified=True,
                                    engine=engine, workspace=tmp, tag='task-hub-offer1',
                                    profile=profile, account_label='test-account')}
        with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
            def request(method, path, payload=None):
                if path.endswith('/claim'):
                    assert payload['model'] == model['model']
                return {}
            dispatcher = Dispatcher(settings, request=request, runner=runner)
            result = dispatcher.dispatch(offer)
        assert result['state'] == 'running'
        assert sub['launcher_model'] == 'wrong-daily-default'


def test_model_fail_closed_before_any_native_effect():
    selection = dict(id='astra', model='gpt-6.1-sol', effort='high')
    with pytest.raises(ValueError, match='model_unsupported'):
        configured_selection(selection, 'codex', 'codex')
    assert native_model(['native', '--model', 'astra', '--model', 'sol']) is None


def test_model_visible_in_list():
    assert 'astra' in render_table([dict(id='test', engine='codex', model_id='astra')])


def test_managed_execute_replaces_bundle_model(tmp_path, monkeypatch):
    import sys
    from agent_hub_host import provisioning as p
    from test_provisioning import bundle
    catalog = tmp_path / 'managed'
    catalog.mkdir(mode=0o700)
    monkeypatch.setattr(p, 'root', lambda: catalog)
    b = bundle()
    b.update(provider='codex', label='my-codex', args=['--model', 'gpt-6.1-sol'])
    dest = catalog / f"{b['label']}-{b['revision']}"
    dest.mkdir(mode=0o700)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(catalog / 'state.json', [dict(label=b['label'], revision=b['revision'], provider='codex')])
    calls = []
    monkeypatch.setattr(p.os, 'execve', lambda cmd, argv, env: calls.append(argv))
    monkeypatch.setattr(p, 'check_codex_quota_gate', lambda env: (True, 'ok'))
    monkeypatch.setattr(sys, 'argv', ['execute', 'my-codex', 'exec', '--model', 'gpt-6-astra', 'check'])
    p.execute()
    assert native_model(calls[0]) == 'gpt-6-astra'
    assert 'gpt-6.1-sol' not in calls[0]


def test_interactive_unsupported_adapter_refused_before_claim(tmp_path):
    selected = next(m for m in policy_models() if m['id'] == 'deepseek-flash')
    settings = dict(version=1, workspaces={'main': str(tmp_path)}, subscriptions=[dict(
        service='go', engine='opencode', account_label='test', profile=None,
        launcher_provider='opencode', launcher_model='opencode-go/muse-spark-1.3-contributor',
        admission_argv=['gate'], startup_argv=['opencode'])],
        launcher=dict(argv=['launcher'], root=str(tmp_path), config_dir=str(tmp_path)))
    requests, calls = [], []
    def runner(argv, **kw):
        calls.append(argv)
        if argv == ['gate']:
            return dict(launch_allowed=True, profile_supported=True, account_label='test',
                        observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        if 'model-support' in argv:
            return dict(provider='opencode', model=selected['model'], supported=False)
        raise AssertionError('Unexpected native effect')
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        dispatcher = Dispatcher(settings, request=lambda *a: requests.append(a), runner=runner)
        with pytest.raises(ValueError, match='model_unsupported'):
            dispatcher.dispatch(dict(id='unsupported', mode='interactive', engine='opencode', account='test',
                                     profile=None, workspace='main', prompt='', selection=selected))
    assert not requests
    assert len(calls) == 2
