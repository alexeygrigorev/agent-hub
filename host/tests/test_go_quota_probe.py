"""Managed OpenCode Go accounts read quota directly (was permanently 'unavailable')."""
import io
import json

import pytest

from agent_hub_host import credentials
from agent_hub_host.fleet import quota_metadata

USAGE = {'usage': {'rolling': {'status': 'ok', 'percent': 10, 'resetsAt': '2026-11-02T14:44:54.000Z'},
                   'weekly': {'status': 'ok', 'percent': 0, 'resetsAt': '2026-11-09T00:00:00.000Z'},
                   'monthly': {'status': 'ok', 'percent': 51, 'resetsAt': '2026-12-01T00:00:00.000Z'}}}


class Opener:
    def __init__(self, body):
        self.body, self.requests = body, []

    def open(self, request, timeout=15):
        self.requests.append(request)
        return io.BytesIO(json.dumps(self.body).encode())


@pytest.fixture(autouse=True)
def plain_reads(monkeypatch):
    # Owner-only ACL validation is covered by private_fs tests; temp dirs on
    # Windows hosts do not carry the required ACL.
    monkeypatch.setattr(credentials.private_fs, 'read_bytes', lambda path, limit, private=True: path.read_bytes())


def _home(tmp_path, active=True):
    path = tmp_path / '.local/share/opencode-hub'
    path.mkdir(parents=True)
    (path / 'device-auth.json').write_text(json.dumps([
        {'id': 'a', 'active': False, 'value': {'access': 'inactive-token'}},
        {'id': 'b', 'active': active, 'value': {'access': 'active-token', 'refresh': 'r'}}]))
    return tmp_path


def test_go_probe_uses_active_device_token_and_maps_windows(tmp_path):
    opener = Opener(USAGE)
    result = credentials.probe_quota('go', _home(tmp_path), opener=opener)
    request = opener.requests[0]
    assert request.full_url == credentials.GO_USAGE
    assert request.get_header('Authorization') == 'Bearer active-token'
    assert request.get_header('User-agent') != 'Python-urllib/3.12'
    quota = quota_metadata(result, 'go')
    assert quota['status'] == 'ok'
    assert quota['windows']['monthly']['percent_remaining'] == 49
    assert quota['windows']['5h']['percent_remaining'] == 90 and quota['windows']['5h']['rolling'] is True
    assert quota['windows']['7d']['percent_remaining'] == 100


def test_go_probe_without_active_login_reports_nothing(tmp_path):
    assert credentials.probe_quota('go', _home(tmp_path, active=False), opener=Opener(USAGE)) == {}


def test_go_probe_invalid_body_reports_nothing(tmp_path):
    assert credentials.probe_quota('go', _home(tmp_path), opener=Opener({'nope': 1})) == {}


def test_go_exhausted_window_marks_limit(tmp_path):
    body = {'usage': dict(USAGE['usage'], monthly={'status': 'exhausted', 'percent': 100, 'resetsAt': '2026-12-01T00:00:00.000Z'})}
    route = credentials.go_quota(body)
    assert route['details']['limit_reached'] is True
