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
from unittest import mock

import pytest

from agent_hub_host import (api, default_claude_consumer as dcc, default_claude_transport as transport,
                            native_import_host)
from test_cli_default_claude_consumer import PATH, World, oauth, view

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
