import datetime as dt
import io
import json
from pathlib import Path
import urllib.error

import pytest

from agent_hub_host import native_claude_quota as q, private_fs

NOW = dt.datetime(2026, 10, 20, 15, 0, 0, tzinfo=dt.timezone.utc)
MODEL = 'claude-sonnet-4-6'


class Reply(io.BytesIO):
    def __init__(self, payload, url, headers=None):
        raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        super().__init__(raw)
        self.url = url
        self.headers = headers or {}

    def geturl(self):
        return self.url


class Server:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        assert 0 < timeout <= 15
        if not self.responses:
            raise urllib.error.HTTPError(request.full_url, 500, 'no-response', {}, io.BytesIO(b'{"error": "none"}'))
        response = self.responses.pop(0)
        if isinstance(response, int):
            raise urllib.error.HTTPError(request.full_url, response, 'error', {}, io.BytesIO(b'{"error": "http-error"}'))
        if isinstance(response, tuple):
            code, headers, body = response
            raise urllib.error.HTTPError(request.full_url, code, 'error', headers, io.BytesIO(json.dumps(body).encode()))
        headers = {}
        payload = response
        if isinstance(response, dict) and '__headers__' in response:
            headers = response['__headers__']
            payload = {k: v for k, v in response.items() if k != '__headers__'}
        return Reply(payload, request.full_url, headers=headers)


@pytest.fixture
def profile(tmp_path):
    tmp_path.chmod(0o700)
    dest = tmp_path / 'native-claude-profile'
    dest.mkdir(parents=True, mode=0o700)
    bundle = {
        'provider': 'claude',
        'version': '2.1.304',
        'args': ['--model', MODEL],
        'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'synthetic-claude-token-a'},
        'files': {},
    }
    return bundle, dest


def usage_payload(u5=25.0, u7=10.0, r5=None, r7=None, sub='Claude Pro', **extra):
    data = {
        'five_hour': {
            'utilization': u5,
            'resets_at': r5 or (NOW + dt.timedelta(hours=3)).isoformat().replace('+00:00', 'Z'),
        },
        'seven_day': {
            'utilization': u7,
            'resets_at': r7 or (NOW + dt.timedelta(days=5)).isoformat().replace('+00:00', 'Z'),
        },
        'subscription': sub,
    }
    data.update(extra)
    return data


def read(profile, server, **kwargs):
    bundle, dest = profile
    return q.probe(bundle, dest, opener=server, now=NOW, **kwargs)['claude']


def test_fresh_native_quota_reads_exact_account_token_and_sanitizes(profile, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'unrelated-global-key')
    server = Server(usage_payload(u5=20.0, u7=15.0))
    result = read(profile, server)
    assert result['status'] == 'ok'
    assert result['windows']['5h']['percent_remaining'] == 80.0
    assert result['windows']['7d']['percent_remaining'] == 85.0
    assert result['details']['limit_reached'] is False
    assert result['details']['source'] == 'native_claude_oauth_usage'
    assert result['details']['managed'] is True
    assert len(server.calls) == 1
    assert server.calls[0].full_url.startswith(q.USAGE_URL)
    assert server.calls[0].get_header('Authorization') == 'Bearer synthetic-claude-token-a'
    assert server.calls[0].get_header('Anthropic-beta') == 'oauth-2025-04-20'
    assert all(w not in json.dumps(result) for w in ['synthetic-claude-token', 'unrelated-global'])


def test_missing_utilization_never_defaults_to_zero(profile):
    bad_payload = {
        'five_hour': {'resets_at': (NOW + dt.timedelta(hours=1)).isoformat().replace('+00:00', 'Z')},
        'seven_day': {'utilization': 10.0, 'resets_at': (NOW + dt.timedelta(days=1)).isoformat().replace('+00:00', 'Z')},
    }
    server = Server(bad_payload)
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_claude_malformed_windows'
    assert result['windows'] == {}


@pytest.mark.parametrize('bad', [None, True, float('nan'), float('inf'), -1.0, 101.0, '50'])
def test_malformed_utilization_fails_closed(profile, bad):
    server = Server(usage_payload(u5=bad))
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_claude_malformed_windows'


def test_naive_timestamp_rejected_never_attaches_utc(profile):
    # Timestamps missing explicit timezone offset must be rejected, not assumed UTC
    naive_ts = (NOW + dt.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S')
    server = Server(usage_payload(r5=naive_ts))
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_claude_malformed_windows'


def test_past_reset_timestamp_rejected(profile):
    past_ts = (NOW - dt.timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
    server = Server(usage_payload(r5=past_ts))
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_claude_malformed_windows'


def test_exhausted_window_sets_limit_reached(profile):
    server = Server(usage_payload(u5=100.0, u7=10.0))
    result = read(profile, server)
    assert result['status'] == 'ok'
    assert result['windows']['5h']['percent_remaining'] == 0.0
    assert result['details']['limit_reached'] is True


def test_missing_token_returns_login_required(profile):
    bundle, dest = profile
    bundle['env'] = {}
    server = Server()
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_claude_login_required'
    assert not server.calls


def test_credentials_fallback_private_fs_claudeAiOauth(profile):
    bundle, dest = profile
    bundle['env'] = {}  # No env token
    creds_file = dest / 'home' / '.claude' / '.credentials.json'
    creds_file.parent.mkdir(parents=True, mode=0o700)
    creds_content = json.dumps({
        'claudeAiOauth': {
            'accessToken': 'token-from-credentials-json',
            'expiresAt': int((NOW + dt.timedelta(days=1)).timestamp() * 1000),
        }
    }).encode('utf-8')
    private_fs.atomic_write(str(creds_file), creds_content, q.MAX_BODY)

    server = Server(usage_payload(u5=10.0, u7=20.0))
    result = read(profile, server)
    assert result['status'] == 'ok'
    assert result['windows']['5h']['percent_remaining'] == 90.0
    assert server.calls[0].get_header('Authorization') == 'Bearer token-from-credentials-json'


def test_inference_only_token_with_count_tokens_unified_headers(profile):
    reset_5h = int((NOW + dt.timedelta(hours=4)).timestamp())
    reset_7d = int((NOW + dt.timedelta(days=6)).timestamp())
    count_headers = {
        'anthropic-ratelimit-unified-status': 'allowed',
        'anthropic-ratelimit-unified-5h-utilization': '0.35',
        'anthropic-ratelimit-unified-5h-reset': str(reset_5h),
        'anthropic-ratelimit-unified-7d-utilization': '0.15',
        'anthropic-ratelimit-unified-7d-reset': str(reset_7d),
    }
    server = Server(403, {'__headers__': count_headers, 'input_tokens': 2})
    result = read(profile, server)
    assert result['status'] == 'ok'
    assert result['windows']['5h']['percent_remaining'] == 65.0
    assert result['windows']['7d']['percent_remaining'] == 85.0
    assert result['details']['source'] == 'native_claude_unified_headers'
    assert len(server.calls) == 2
    assert server.calls[0].full_url.startswith(q.USAGE_URL)
    assert server.calls[1].full_url == q.COUNT_TOKENS_URL
    assert server.calls[1].get_header('Authorization') == 'Bearer synthetic-claude-token-a'


def test_inference_only_token_without_complete_unified_headers_fails_closed_honestly(profile):
    # When count_tokens returns ordinary token headers without unified subscription headers,
    # probe must fail closed honestly with native_claude_scope_inference_only.
    server = Server(403, {'input_tokens': 2})  # No unified rate limit headers
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_claude_scope_inference_only'
    assert result['windows'] == {}


def test_epoch_headers_out_of_range_or_past_rejected(profile):
    count_headers = {
        'anthropic-ratelimit-unified-status': 'allowed',
        'anthropic-ratelimit-unified-5h-utilization': '0.2',
        'anthropic-ratelimit-unified-5h-reset': str(int((NOW - dt.timedelta(hours=1)).timestamp())),  # past!
        'anthropic-ratelimit-unified-7d-utilization': '0.2',
        'anthropic-ratelimit-unified-7d-reset': str(int((NOW + dt.timedelta(days=1)).timestamp())),
    }
    server = Server(403, {'__headers__': count_headers, 'input_tokens': 2})
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_claude_scope_inference_only'


@pytest.mark.parametrize('code', [401, 429, 500])
def test_errors_are_sanitized(profile, code):
    server = Server(code)
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] in (f'native_claude_http_{code}', 'native_claude_auth_failed', 'native_claude_rate_limited')
    assert 'private' not in json.dumps(result)


def test_authorization_never_follows_redirect():
    request = q.urllib.request.Request(q.USAGE_URL, headers={'Authorization': 'Bearer synthetic'})
    with pytest.raises(q.ProbeError, match='redirect_refused'):
        q.NoRedirect().redirect_request(request, None, 302, 'found', {}, 'https://example.invalid')


def test_sibling_accounts_use_their_own_tokens(profile, tmp_path):
    bundle_a, dest_a = profile
    bundle_b = {
        'provider': 'claude',
        'version': '2.1.304',
        'args': ['--model', MODEL],
        'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'synthetic-claude-token-b'},
        'files': {},
    }
    dest_b = tmp_path / 'profile-b'
    dest_b.mkdir(parents=True, mode=0o700)

    server_a = Server(usage_payload(u5=20.0, u7=10.0))
    server_b = Server(usage_payload(u5=60.0, u7=40.0))

    res_a = q.probe(bundle_a, dest_a, opener=server_a, now=NOW)['claude']
    res_b = q.probe(bundle_b, dest_b, opener=server_b, now=NOW)['claude']

    assert res_a['windows']['5h']['percent_remaining'] == 80.0
    assert res_b['windows']['5h']['percent_remaining'] == 40.0
    assert server_a.calls[0].get_header('Authorization') == 'Bearer synthetic-claude-token-a'
    assert server_b.calls[0].get_header('Authorization') == 'Bearer synthetic-claude-token-b'


def test_model_specific_window_matching_sonnet(profile):
    # When model is Sonnet: seven_day_sonnet is parsed; seven_day_opus is ignored
    payload = usage_payload(
        u5=30.0,
        u7=20.0,
        seven_day_sonnet={
            'utilization': 45.0,
            'resets_at': (NOW + dt.timedelta(days=4)).isoformat().replace('+00:00', 'Z'),
        },
        seven_day_opus={
            'utilization': 95.0,
            'resets_at': (NOW + dt.timedelta(days=2)).isoformat().replace('+00:00', 'Z'),
        },
    )
    server = Server(payload)
    result = read(profile, server, model='claude-sonnet-4-6')
    assert result['status'] == 'ok'
    assert 'seven_day_sonnet' in result['windows']
    assert result['windows']['seven_day_sonnet']['percent_remaining'] == 55.0
    assert 'seven_day_opus' not in result['windows']


def test_model_specific_window_matching_opus(profile):
    # When model is Opus: seven_day_opus is parsed; seven_day_sonnet is ignored
    payload = usage_payload(
        u5=30.0,
        u7=20.0,
        seven_day_sonnet={
            'utilization': 45.0,
            'resets_at': (NOW + dt.timedelta(days=4)).isoformat().replace('+00:00', 'Z'),
        },
        seven_day_opus={
            'utilization': 95.0,
            'resets_at': (NOW + dt.timedelta(days=2)).isoformat().replace('+00:00', 'Z'),
        },
    )
    server = Server(payload)
    result = read(profile, server, model='claude-opus-4-8')
    assert result['status'] == 'ok'
    assert 'seven_day_opus' in result['windows']
    assert result['windows']['seven_day_opus']['percent_remaining'] == 5.0
    assert 'seven_day_sonnet' not in result['windows']


def test_resolve_api_model():
    assert q.resolve_api_model('sonnet') == 'claude-sonnet-4-6'
    assert q.resolve_api_model('opus') == 'claude-opus-4-8'
    assert q.resolve_api_model('haiku') == 'claude-haiku-4-5'
    assert q.resolve_api_model('claude-sonnet-5-5') == 'claude-sonnet-5-5'
    assert q.resolve_api_model(None) == 'claude-sonnet-4-6'


def test_inactive_five_hour_window_with_null_reset_and_null_model_window(profile):
    payload = {
        'five_hour': {'utilization': 0.0, 'resets_at': None},
        'seven_day': {
            'utilization': 15.0,
            'resets_at': (NOW + dt.timedelta(days=5)).isoformat().replace('+00:00', 'Z'),
        },
        'seven_day_sonnet': None,
        'seven_day_opus': None,
        'subscription': 'Claude Pro',
    }
    server = Server(payload)
    result = read(profile, server, model='claude-sonnet-4-6')
    assert result['status'] == 'ok'
    assert result['windows']['5h']['percent_remaining'] == 100.0
    assert result['windows']['5h']['reset_at'] is None
    assert result['windows']['5h']['inactive'] is True
    assert result['windows']['7d']['percent_remaining'] == 85.0
    assert 'seven_day_sonnet' not in result['windows']


def test_positive_utilization_with_null_reset_fails_closed(profile):
    payload = {
        'five_hour': {'utilization': 25.0, 'resets_at': None},
        'seven_day': {
            'utilization': 15.0,
            'resets_at': (NOW + dt.timedelta(days=5)).isoformat().replace('+00:00', 'Z'),
        },
    }
    server = Server(payload)
    result = read(profile, server)
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_claude_malformed_windows'


# Intake 798f0f8c: launch bursts (plan + run probe per launch) hit the usage
# endpoint's rate limit; one good reading is shared per token across processes.
def test_recent_reading_is_reused_without_another_request(profile, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(q, '_clock', lambda: clock[0])
    server = Server(usage_payload(u5=30.0))
    first = read(profile, server)
    clock[0] += q.USAGE_REUSE_S - 1
    second = read(profile, server)
    assert len(server.calls) == 1
    assert second == first and second['details']['source'] == 'native_claude_oauth_usage'
    raw = (profile[1] / q.USAGE_CACHE_FILE).read_bytes()
    assert b'synthetic-claude-token-a' not in raw


def test_rate_limit_falls_back_to_recent_reading_only(profile, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(q, '_clock', lambda: clock[0])
    read(profile, Server(usage_payload(u5=30.0)))
    clock[0] += q.USAGE_REUSE_S + 1
    limited = Server(429)
    result = read(profile, limited)
    assert len(limited.calls) == 1
    assert result['status'] == 'ok' and result['windows']['5h']['percent_remaining'] == 70.0
    clock[0] += q.USAGE_RATE_LIMITED_REUSE_S
    assert read(profile, Server(429))['error'] == 'native_claude_rate_limited'


def test_cached_reading_never_crosses_tokens_or_survives_a_passed_reset(profile, monkeypatch):
    monkeypatch.setattr(q, '_clock', lambda: 1000.0)
    bundle, dest = profile
    q.probe(bundle, dest, opener=Server(usage_payload()), now=NOW)
    rotated = dict(bundle, env={'CLAUDE_CODE_OAUTH_TOKEN': 'synthetic-claude-token-rotated'})
    server = Server(usage_payload(u5=50.0))
    assert q.probe(rotated, dest, opener=server, now=NOW)['claude']['windows']['5h']['percent_remaining'] == 50.0
    assert len(server.calls) == 1
    later = NOW + dt.timedelta(days=6)
    assert q.probe(rotated, dest, opener=Server(429), now=later)['claude']['status'] == 'unknown'


def test_429_persists_backoff_and_suppresses_fresh_process_requests(profile, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(q, '_clock', lambda: clock[0])
    limited = Server((429, {'Retry-After': '300'}, {}))
    assert read(profile, limited)['error'] == 'native_claude_rate_limited'
    path = q._backoff_path(profile[1], profile[0]['env']['CLAUDE_CODE_OAUTH_TOKEN'])
    entry = json.loads(path.read_text())
    assert entry['until'] == 1300 and entry['failures'] == 1
    assert path.stat().st_mode & 0o777 == 0o600
    assert 'synthetic-claude-token' not in path.read_text()
    clock[0] = 1299
    server = Server(usage_payload())
    assert read(profile, server)['error'] == 'native_claude_rate_limited'
    assert not server.calls
    clock[0] = 1300
    assert read(profile, server)['status'] == 'ok'
    assert len(server.calls) == 1
    assert json.loads(path.read_text())['failures'] == 0


def test_429_backoff_grows_exponentially_and_honors_http_date(profile, monkeypatch):
    from email.utils import format_datetime
    clock = [NOW.timestamp()]
    monkeypatch.setattr(q, '_clock', lambda: clock[0])
    read(profile, Server(429))
    token = profile[0]['env']['CLAUDE_CODE_OAUTH_TOKEN']
    first = q._read_backoff(profile[1], token)
    assert first['until'] - first['at'] == 120
    clock[0] += 120
    read(profile, Server(429))
    second = q._read_backoff(profile[1], token)
    assert second['until'] - second['at'] == 240
    clock[0] += 240
    retry = format_datetime(dt.datetime.fromtimestamp(clock[0] + 900, dt.timezone.utc))
    read(profile, Server((429, {'Retry-After': retry}, {})))
    third = q._read_backoff(profile[1], token)
    assert third['until'] - third['at'] == 900


@pytest.mark.parametrize('age,used,reset_delta,expected', [
    (1799, 30, 3600, 'ok'), (1801, 30, 3600, 'unknown'),
    (120, 100, 3600, 'unknown'), (120, 30, -1, 'unknown'),
])
def test_backoff_last_good_is_bounded_and_safe(profile, monkeypatch, age, used, reset_delta, expected):
    clock = [1000.0]
    monkeypatch.setattr(q, '_clock', lambda: clock[0])
    read(profile, Server(usage_payload(u5=used, r5=(NOW + dt.timedelta(seconds=3600)).isoformat())))
    clock[0] += age
    token = profile[0]['env']['CLAUDE_CODE_OAUTH_TOKEN']
    q._record_rate_limit(profile[1], token, {'Retry-After': '3600'})
    current = NOW if reset_delta > 0 else NOW + dt.timedelta(seconds=3601)
    server = Server(usage_payload())
    result = q.probe(profile[0], profile[1], opener=server, now=current)['claude']
    assert result['status'] == expected
    assert not server.calls


def test_backoff_checks_applicable_model_limit(profile, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(q, '_clock', lambda: clock[0])
    read(profile, Server(usage_payload(seven_day_opus={'utilization': 100, 'resets_at': (NOW + dt.timedelta(days=1)).isoformat()})))
    clock[0] += 61
    result = read(profile, Server(429), model='opus')
    assert result['error'] == 'native_claude_rate_limited'


def test_backoff_is_token_specific_and_corrupt_stamp_does_not_block_probe(profile, monkeypatch):
    monkeypatch.setattr(q, '_clock', lambda: 1000.0)
    read(profile, Server(429))
    bundle, dest = profile
    rotated = dict(bundle, env={'CLAUDE_CODE_OAUTH_TOKEN': 'rotated-token'})
    server = Server(usage_payload())
    assert q.probe(rotated, dest, opener=server, now=NOW)['claude']['status'] == 'ok'
    assert len(server.calls) == 1
    q._backoff_path(dest, bundle['env']['CLAUDE_CODE_OAUTH_TOKEN']).write_text('bad-json')
    server = Server(usage_payload())
    assert read(profile, server)['status'] == 'ok'
    assert len(server.calls) == 1


def test_count_tokens_429_also_suppresses_followup_network(profile, monkeypatch):
    monkeypatch.setattr(q, '_clock', lambda: 1000.0)
    server = Server(403, 429)
    assert read(profile, server)['error'] == 'native_claude_rate_limited'
    assert len(server.calls) == 2
    later = Server(usage_payload())
    assert read(profile, later)['error'] == 'native_claude_rate_limited'
    assert not later.calls


def test_parallel_probe_lock_contender_never_calls_provider(profile, monkeypatch):
    monkeypatch.setattr(q, '_clock', lambda: 1000.0)
    bundle, dest = profile
    token = bundle['env']['CLAUDE_CODE_OAUTH_TOKEN']
    lock = dest / ('claude-usage-' + q._token_key(token) + '.lock')
    with private_fs.exclusive_lock(lock):
        server = Server(usage_payload())
        assert read(profile, server)['error'] == 'native_claude_rate_limited'
        assert not server.calls
