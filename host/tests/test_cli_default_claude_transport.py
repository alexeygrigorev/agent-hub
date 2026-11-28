"""Official default-Claude enroll/check path: command -> binder -> synthetic transport.

The only fake is the lowest HTTP seam (the opener ``api.safe_http_json`` uses), so
the real command, the real passive-only binder, real enrollment-file identity checks
and the real request construction all run. No network, provider, token or Claude.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

import pytest

from agent_hub_host import (api, config, default_claude_consumer as dcc, default_claude_transport as transport,
                            managed_claude_deliver as deliver, native_claude_store as native,
                            native_import_host, private_fs, worker_auth)
from test_cli_default_claude_consumer import MCP, PATH, World, oauth, request, view

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='default Claude consumer is Linux only')

SERVER = 'https://hub.test'
TOKEN = 'syn-host-token'
ENROLLMENT = {'server': SERVER, 'token': TOKEN, 'machine_id': 'test-host', 'roots': [], 'engines': ['claude']}


class Opener:
    """Records every request that reaches the wire seam and answers a canned view."""

    def __init__(self, body):
        self.requests, self.body = [], body

    def open(self, req, timeout=None):
        self.requests.append(req)
        body = json.dumps(self.body).encode()

        class Response:
            headers = {'Content-Type': 'application/json', 'Content-Length': str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                return body
        return Response()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    profiles = world.home / '.config/agent-hub'
    profiles.mkdir(parents=True, mode=0o700)
    world.v2 = profiles / 'v2.json'
    world.write_enrollment = lambda value: (world.v2.write_text(json.dumps(value)), os.chmod(world.v2, 0o600))
    world.write_enrollment(ENROLLMENT)
    for key in ('HUB_CONFIG_FILE', 'HUB_BASE_URL', 'HUB2_CONFIG', 'HUB_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HUB_URL', SERVER)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(world.home / '.config'))
    world.opener = Opener(view(7))
    monkeypatch.setattr(api, '_get_opener', lambda: world.opener)
    return world


def run(action, **flags):
    return dcc.command(argparse.Namespace(default_claude_cmd=action, **flags))


def test_enroll_and_check_reach_only_the_passive_get_with_the_enrolled_identity(hub):
    hub.put(oauth(7))
    result = run('enroll', confirm_same_account=True)
    assert result['status'] == 'enrolled' and result['mode'] == 'observe'
    checked = run('check')
    assert checked == {'status': 'observe_only', 'enabled': False, 'generation': 7}
    assert len(hub.opener.requests) == 2
    for req in hub.opener.requests:
        assert req.get_method() == 'GET' and req.data is None
        assert req.full_url == SERVER + transport.PASSIVE_PATH == SERVER + '/api/engines/hub-claude/passive-credential'
        assert req.get_header('Authorization') == 'Bearer ' + TOKEN
    # Neither command wrote a credential or called any refresh/POST route.
    assert hub.doc()['claudeAiOauth'] == oauth(7)


def test_status_disable_and_mode_commands_never_touch_the_network(hub):
    hub.put(oauth(7))
    run('enroll', confirm_same_account=True)
    hub.opener.requests.clear()
    run('status'), run('enable-writes', confirm_writer_handoff=True), run('observe'), run('disable')
    assert hub.opener.requests == []


def refusal_fixtures(hub):
    """name -> (environment overrides, enrollment document or None to delete it)."""
    return {
        'wrong_origin': ({'HUB_URL': 'https://other.test'}, ENROLLMENT),
        'http_origin': ({'HUB_URL': 'http://hub.test'}, ENROLLMENT),
        'explicit_config': ({'HUB_CONFIG_FILE': str(hub.v2)}, ENROLLMENT),
        'worker_key': ({}, dict(ENROLLMENT, worker={'x': 1})),
        'launch_id': ({}, dict(ENROLLMENT, worker_launch_id='x')),
        'bad_machine': ({}, dict(ENROLLMENT, machine_id='bad id')),
        'bad_token': ({}, dict(ENROLLMENT, token='bad token')),
        'human_kind': ({}, dict(ENROLLMENT, client_kind='human')),
        'other_server': ({}, dict(ENROLLMENT, server='https://other.test')),
        'missing': ({}, None),
    }


def apply(hub, overrides, document):
    if document is None:
        hub.v2.unlink()
    else:
        hub.write_enrollment(document)
    return mock.patch.dict(os.environ, overrides)


def test_positive_control_both_enrolled_host_bindings_accept_the_valid_enrollment(hub):
    assert transport.bind_passive(SERVER)[0] == 'test-host'
    assert native_import_host.bind_managed_claude(SERVER)[0] == 'test-host'


@pytest.mark.parametrize('name', ['wrong_origin', 'http_origin', 'explicit_config', 'worker_key', 'launch_id',
                                  'bad_machine', 'bad_token', 'human_kind', 'other_server', 'missing'])
def test_wrong_origin_or_identity_is_refused_before_any_request_like_the_managed_binding(hub, name):
    hub.put(oauth(7))
    overrides, document = refusal_fixtures(hub)[name]
    with apply(hub, overrides, document):
        base = os.environ['HUB_URL']
        with pytest.raises(ValueError):
            native_import_host.bind_managed_claude(base)     # the reference binding refuses it...
        with pytest.raises(ValueError):
            transport.bind_passive(base)                     # ...and so does the passive binding
        with pytest.raises(ValueError):
            run('enroll', confirm_same_account=True)
        with pytest.raises(ValueError):
            run('check')
    assert hub.opener.requests == [], name
    assert not (hub.managed / dcc.STATE_NAME).exists()


def test_the_binding_is_passive_only_and_old_binders_still_refuse_the_endpoint(hub):
    machine, fetch = transport.bind_passive(SERVER)
    assert machine == 'test-host' and callable(fetch)
    # The returned callable takes no method/path/payload: nothing but the passive GET.
    with pytest.raises(TypeError):
        fetch('POST', '/api/engines/hub-claude/credential', {})
    # The existing binders keep their narrower allowlists and still refuse this endpoint.
    for binder in (native_import_host.bind, native_import_host.bind_managed_claude,
                   native_import_host.bind_managed_claude_repin):
        _, request = binder(SERVER)
        with pytest.raises(ValueError, match='Native import host engine API path required'):
            request('GET', transport.PASSIVE_PATH, None)
    assert hub.opener.requests == []
    fetch()
    assert [r.get_method() for r in hub.opener.requests] == ['GET']


def test_legacy_or_ordinary_profile_is_never_a_fallback_identity(hub):
    hub.v2.unlink()
    legacy = hub.home / '.config/agent-hub/host.json'
    legacy.write_text(json.dumps({'base_url': SERVER, 'access_token': 'syn-legacy-token', 'machine_id': 'legacy'}))
    os.chmod(legacy, 0o600)
    with pytest.raises(ValueError, match='current enrolled host'):
        transport.bind_passive(SERVER)
    assert hub.opener.requests == []


ALL_COMMANDS = (
    ('status', {}),
    ('check', {}),
    ('enroll', {'confirm_same_account': True}),
    ('disable', {}),
    ('enable-writes', {'confirm_writer_handoff': True}),
    ('observe', {}),
)


def _make_worker_context(tmp_path):
    sid = '11111111-1111-4111-8111-111111111111'
    intake = '33333333-3333-4333-8333-333333333333'
    workspace = tmp_path / 'worker-ws'
    contexts = workspace / '.local' / 'hub2-contexts'
    contexts.mkdir(mode=0o700, parents=True, exist_ok=True)
    ctx_file = contexts / f'{sid}-7.json'
    ctx_file.write_text(json.dumps({
        'server': SERVER,
        'token': 'FAKE_WORKER_ONLY_' + 'x' * 40,
        'expires_at': time.time() + 600,
        'worker': {
            'session_id': sid,
            'operation_id': '22222222-2222-4222-8222-222222222222:0',
            'epoch': 7,
            'intake_id': intake,
            'project_id': 'synthetic/project',
            'machine_id': 'synthetic-machine',
            'actor': 'session:' + sid,
        },
    }), encoding='utf-8')
    os.chmod(ctx_file, 0o600)
    return {
        'APLEXER_TAG': 'task-hub2-' + sid,
        'APLEXER_WORKSPACE': str(workspace),
        'HUB2_CONFIG': str(ctx_file),
    }


def _assert_all_refused_with_zero_effects(hub, monkeypatch, env_overrides):
    calls = []
    monkeypatch.setattr(native, 'probe_version', lambda *a, **kw: calls.append('probe_version'))
    monkeypatch.setattr(native, 'bind_executable', lambda *a, **kw: calls.append('bind_executable'))
    monkeypatch.setattr(native, 'verify_executable', lambda *a, **kw: calls.append('verify_executable'))
    monkeypatch.setattr(config, 'load_config', lambda *a, **kw: calls.append('load_config'))
    real_read_bytes = private_fs.read_bytes
    monkeypatch.setattr(private_fs, 'read_bytes',
                        lambda path, *a, **kw: (calls.append(('read_bytes', str(path))),
                                                real_read_bytes(path, *a, **kw))[1])
    monkeypatch.setattr(dcc, 'save_state', lambda *a, **kw: calls.append('save_state'))
    monkeypatch.setattr(dcc, '_remove_durably', lambda *a, **kw: calls.append('remove_durably'))

    state_file = hub.managed / dcc.STATE_NAME
    state_before = state_file.read_bytes() if state_file.exists() else None
    cred_before = hub.raw()

    with mock.patch.dict(os.environ, env_overrides):
        for action, flags in ALL_COMMANDS:
            with pytest.raises(worker_auth.WorkerContextError):
                run(action, **flags)
        with pytest.raises(worker_auth.WorkerContextError):
            transport.bind_passive(SERVER)

    assert calls == []
    assert hub.opener.requests == []
    assert (state_file.read_bytes() if state_file.exists() else None) == state_before
    assert hub.raw() == cred_before


@pytest.mark.parametrize('use_url_shortcut', [True, False])
@pytest.mark.parametrize('pre_enrolled', [False, True])
def test_v2_worker_context_refuses_all_six_commands_and_binder_without_side_effects(
    hub, tmp_path, monkeypatch, use_url_shortcut, pre_enrolled
):
    hub.put(oauth(7))
    if pre_enrolled:
        run('enroll', confirm_same_account=True)
        hub.opener.requests.clear()
    worker_env = _make_worker_context(tmp_path)
    if not use_url_shortcut:
        monkeypatch.delenv('HUB_URL', raising=False)
        monkeypatch.delenv('HUB_BASE_URL', raising=False)
    else:
        worker_env['HUB_URL'] = SERVER
    _assert_all_refused_with_zero_effects(hub, monkeypatch, worker_env)


@pytest.mark.parametrize('case', ['bad_tag', 'prefix_tag', 'missing_selector', 'malformed_selector'])
def test_malformed_v2_worker_markers_refuse_without_fallback(hub, tmp_path, monkeypatch, case):
    hub.put(oauth(7))
    run('enroll', confirm_same_account=True)
    hub.opener.requests.clear()
    overrides = {
        'bad_tag': {'APLEXER_TAG': 'task-hub2-corrupt'},
        'prefix_tag': {'APLEXER_TAG': 'task-hub2-'},
        'missing_selector': {'HUB2_CONFIG': str(tmp_path / 'hub2-contexts' / 'missing.json')},
        'malformed_selector': {'HUB2_CONFIG': '/nonexistent/hub2-contexts/bad'},
    }[case]
    _assert_all_refused_with_zero_effects(hub, monkeypatch, overrides)


def test_positive_ordinary_operator_and_clean_host_helper_delivery_still_work(hub):
    hub.put(oauth(7))
    assert transport.bind_passive(SERVER)[0] == 'test-host'
    assert run('enroll', confirm_same_account=True)['mode'] == 'observe'
    assert run('check')['generation'] == 7
    assert run('enable-writes', confirm_writer_handoff=True)['mode'] == 'write'
    assert run('status')['mode'] == 'write'

    # Clean host delivery helper continues to materialize validated material.
    delivered = deliver.deliver(None, request(8))
    assert delivered['status'] == 'absent'
    assert delivered['default'] == 'materialized'
    assert delivered['default_enabled'] is True
    assert hub.doc()['claudeAiOauth'] == oauth(8)
    assert hub.doc()['mcpOAuth'] == MCP['mcpOAuth']

    assert run('observe')['mode'] == 'observe'
    assert run('disable')['removed'] is True
