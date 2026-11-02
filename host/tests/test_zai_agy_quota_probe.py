"""Managed Z.AI and Antigravity accounts read quota directly (were permanently 'unavailable')."""
import io
import json

import pytest

from agent_hub_host import credentials
from agent_hub_host.fleet import quota_metadata

ZAI = {'data': {'limits': [
    {'type': 'TOKENS_LIMIT', 'unit': 3, 'percentage': 12, 'nextResetTime': 1793700000000},
    {'type': 'TOKENS_LIMIT', 'unit': 6, 'percentage': 41, 'nextResetTime': 1794000000000},
    {'type': 'TIME_LIMIT', 'unit': 5, 'percentage': 0}]}}
AGY = {'groups': [{'buckets': [
    {'window': 'weekly', 'resetTime': '2026-11-09T10:01:47Z', 'remainingFraction': 1},
    {'window': '5h', 'resetTime': '2026-11-02T15:01:47Z', 'remainingFraction': 0.75}]},
    {'buckets': [{'window': '5h', 'resetTime': '2026-11-02T14:00:00Z', 'remainingFraction': 0.5}]}]}


class Opener:
    def __init__(self, body):
        self.body, self.requests = body, []

    def open(self, request, timeout=15):
        self.requests.append(request)
        return io.BytesIO(json.dumps(self.body).encode())


@pytest.fixture(autouse=True)
def plain_reads(monkeypatch):
    monkeypatch.setattr(credentials.private_fs, 'read_bytes', lambda path, limit, private=True: path.read_bytes())


def _zai_home(tmp_path, token):
    path = tmp_path / '.config/goz'
    path.mkdir(parents=True)
    (path / 'config.json').write_text(json.dumps({'zai_token': token}))
    return tmp_path


def _agy_home(tmp_path, expiry):
    path = tmp_path / '.gemini/antigravity-cli'
    path.mkdir(parents=True)
    (path / 'antigravity-oauth-token').write_text(json.dumps(
        {'token': {'access_token': 'a-token', 'refresh_token': 'r', 'expiry': expiry}, 'auth_method': 'consumer'}))
    return tmp_path


def test_zai_probe_maps_five_hour_and_weekly(tmp_path):
    opener = Opener(ZAI)
    result = credentials.probe_quota('zai', _zai_home(tmp_path, 'plain-key'), opener=opener)
    assert opener.requests[0].full_url == credentials.ZAI_USAGE
    assert opener.requests[0].get_header('Authorization') == 'Bearer plain-key'
    quota = quota_metadata(result, 'zai')
    assert quota['status'] == 'ok'
    assert quota['windows']['5h']['percent_remaining'] == 88
    assert quota['windows']['7d']['percent_remaining'] == 59


def test_zai_probe_skips_encrypted_zcode_store(tmp_path):
    opener = Opener(ZAI)
    assert credentials.probe_quota('zai', _zai_home(tmp_path, 'enc:v1:abc'), opener=opener) == {}
    assert opener.requests == []


def test_zai_probe_invalid_body_reports_nothing(tmp_path):
    assert credentials.probe_quota('zai', _zai_home(tmp_path, 'k'), opener=Opener({'nope': 1})) == {}


def test_antigravity_probe_uses_stored_token_and_tightest_group(tmp_path):
    opener = Opener(AGY)
    result = credentials.probe_quota('antigravity', _agy_home(tmp_path, '2999-01-01T00:00:00Z'), opener=opener)
    request = opener.requests[0]
    assert request.full_url == credentials.AGY_QUOTA and request.get_header('Authorization') == 'Bearer a-token'
    quota = quota_metadata(result, 'antigravity')
    assert quota['status'] == 'ok'
    assert quota['windows']['5h']['percent_remaining'] == 50
    assert quota['windows']['7d']['percent_remaining'] == 100


def test_antigravity_expired_token_without_engine_reports_nothing(tmp_path):
    opener = Opener(AGY)
    assert credentials.probe_quota('antigravity', _agy_home(tmp_path, '2000-01-01T00:00:00Z'), opener=opener) == {}
    assert opener.requests == []


def test_antigravity_expired_token_is_refreshed_by_the_engine(tmp_path):
    import datetime as dt
    home = _agy_home(tmp_path, '2000-01-01T00:00:00Z')
    calls = []

    def refresh(path):
        calls.append(path)
        (path / credentials.AGY_TOKEN).write_text(json.dumps({'token': {
            'access_token': 'fresh', 'expiry': '2999-01-01T00:00:00.737548539+01:00'}}))
    token = credentials._agy_access_token(home, dt.datetime.now(dt.timezone.utc), refresh=refresh)
    assert token == 'fresh' and calls == [home]


def test_antigravity_exhausted_marks_limit():
    body = {'groups': [{'buckets': [{'window': 'weekly', 'resetTime': '2026-11-09T10:01:47Z', 'remainingFraction': 0}]}]}
    assert credentials.antigravity_quota(body)['details']['limit_reached'] is True
