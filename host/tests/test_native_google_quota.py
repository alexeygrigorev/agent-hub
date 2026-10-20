import datetime as dt
import io
import json
import shutil
from pathlib import Path
import urllib.error

import pytest

from agent_hub_host import native_google_quota as q, private_fs

NOW = dt.datetime(2030, 1, 2, 12, tzinfo=dt.timezone.utc)
MODEL = 'gemini-2.5-pro'


class Reply(io.BytesIO):
    def __init__(self, payload, url):
        super().__init__(json.dumps(payload).encode())
        self.url = url

    def geturl(self):
        return self.url


class Server:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        assert 0 < timeout <= 15
        response = self.responses.pop(0)
        if isinstance(response, int):
            raise urllib.error.HTTPError(request.full_url, response, 'private-error', {}, io.BytesIO(b'private-body'))
        return Reply(response, request.full_url)


@pytest.fixture
def profile(tmp_path):
    tmp_path.chmod(0o700)
    dest = tmp_path / 'native-profile'
    cred = dest / 'home/.gemini/oauth_creds.json'
    cred.parent.mkdir(parents=True, mode=0o700)
    private_fs.atomic_write(cred, json.dumps({
        'access_token': 'synthetic-account-a', 'refresh_token': 'synthetic-refresh-a',
        'expiry_date': int((NOW.timestamp() + 3600) * 1000),
    }).encode(), q.MAX_BODY)
    package = dest / 'engine/node_modules/@google/gemini-cli'
    (package / 'bundle').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'version': '0.64.0'}))
    (package / 'bundle/fixture.js').write_text(
        'var OAUTH_CLIENT_ID = "synthetic-public-client";\n'
        'var OAUTH_CLIENT_SECRET = "synthetic-public-app-config";\n')
    return {'provider': 'gemini', 'version': '0.64.0', 'args': []}, dest, cred


def quota(remaining=0.8, model=MODEL, reset=None):
    return {'buckets': [{'modelId': model, 'remainingFraction': remaining,
                         'resetTime': reset or (NOW + dt.timedelta(hours=4)).isoformat()}]}


def read(profile, server, **kwargs):
    bundle, dest, _ = profile
    return q.probe(bundle, dest, opener=server, now=NOW, **kwargs)['gemini']


def test_fresh_native_quota_reads_exact_account_and_never_calls_model(profile, monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'unrelated-global-account')
    server = Server({'cloudaicompanionProject': 'private-user-project'}, quota())
    result = read(profile, server)
    assert result['status'] == 'ok'
    assert result['windows']['google_bucket_0']['percent_remaining'] == 80
    assert result['details']['limit_reached'] is False
    assert [r.full_url for r in server.calls] == [q.BASE + 'loadCodeAssist', q.BASE + 'retrieveUserQuota']
    assert all(r.get_header('Authorization') == 'Bearer synthetic-account-a' for r in server.calls)
    assert json.loads(server.calls[1].data) == {'project': 'private-user-project'}
    assert all(word not in json.dumps(result) for word in ['synthetic-account', 'private-user-project', 'unrelated-global'])


def test_exhausted_selected_model_is_authoritative_but_other_model_does_not_block(profile):
    data = quota(0)
    data['buckets'].append(quota(1, 'another-model')['buckets'][0])
    result = read(profile, Server({'cloudaicompanionProject': {'id': 'existing-project'}}, data))
    assert result['status'] == 'ok'
    assert result['details']['limit_reached'] is True
    assert len(result['windows']) == 1


@pytest.mark.parametrize('bad', [None, True, float('nan'), float('inf'), -0.1, 1.1, '0.8'])
def test_missing_or_malformed_fraction_never_fabricates_full_quota(profile, bad):
    result = read(profile, Server({'cloudaicompanionProject': 'p'}, quota(bad)))
    assert result['status'] == 'unknown'
    assert result['windows'] == {}


@pytest.mark.parametrize('reset', ['bad', '2030-01-02T13:00:00', '2029-01-01T00:00:00Z'])
def test_invalid_or_stale_reset_is_unknown(profile, reset):
    assert read(profile, Server({'cloudaicompanionProject': 'p'}, quota(reset=reset)))['status'] == 'unknown'


def test_missing_selected_model_and_missing_login_do_not_use_ambient_accounts(profile, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'unrelated')
    result = read(profile, Server({'cloudaicompanionProject': 'p'}, quota(model='other-model')))
    assert result['error'] == 'native_google_model_quota_unavailable'
    profile[2].unlink()
    server = Server()
    assert read(profile, server)['status'] == 'unknown'
    assert not server.calls


def test_expired_profile_refreshes_only_its_credential_with_vendor_app_metadata(profile):
    bundle, dest, cred = profile
    before = json.loads(cred.read_text())
    before['expiry_date'] = int((NOW.timestamp() - 1) * 1000)
    private_fs.atomic_write(cred, json.dumps(before).encode(), q.MAX_BODY)
    server = Server({'access_token': 'synthetic-account-a-new', 'expires_in': 3600},
                    {'cloudaicompanionProject': 'p'}, quota())
    assert read(profile, server)['status'] == 'ok'
    assert server.calls[0].full_url == q.TOKEN_URL
    assert b'refresh_token=synthetic-refresh-a' in server.calls[0].data
    assert all(r.get_header('Authorization') == 'Bearer synthetic-account-a-new' for r in server.calls[1:])
    updated = json.loads(cred.read_text())
    assert updated['refresh_token'] == 'synthetic-refresh-a'
    assert updated['access_token'] == 'synthetic-account-a-new'
    assert cred.stat().st_mode & 0o777 == 0o600


def test_server_expired_token_refreshes_once_without_switching_accounts(profile):
    server = Server(401, {'access_token': 'synthetic-new', 'expires_in': 3600},
                    {'cloudaicompanionProject': 'p'}, quota())
    assert read(profile, server)['status'] == 'ok'
    assert [r.full_url for r in server.calls].count(q.TOKEN_URL) == 1


@pytest.mark.parametrize('code', [401, 403, 429, 500])
def test_errors_are_sanitized_and_no_model_or_credit_fallback(profile, code):
    profile[2].write_text(json.dumps({'access_token': 'synthetic-a'}))
    result = read(profile, Server(code))
    assert result['status'] == 'unknown'
    assert result['error'] == 'native_google_http_' + str(code)
    assert 'private' not in json.dumps(result)


def test_missing_existing_project_never_onboards_or_creates_one(profile):
    server = Server({'allowedTiers': [{'id': 'free-tier'}]})
    assert read(profile, server)['error'] == 'native_google_login_incomplete'
    assert len(server.calls) == 1


def test_refresh_metadata_mismatch_fails_before_network(profile):
    bundle, dest, cred = profile
    data = json.loads(cred.read_text()); data['expiry_date'] = 0
    private_fs.atomic_write(cred, json.dumps(data).encode(), q.MAX_BODY)
    bundle['version'] = '1.0.0'
    server = Server()
    assert read(profile, server)['error'] == 'native_google_package_mismatch'
    assert not server.calls


def test_symlink_credentials_cannot_borrow_another_profile(profile, tmp_path):
    _, _, cred = profile
    sibling = tmp_path / 'sibling.json'; sibling.write_text(cred.read_text()); sibling.chmod(0o600)
    cred.unlink(); cred.symlink_to(sibling)
    server = Server()
    assert read(profile, server)['status'] == 'unknown'
    assert not server.calls


def test_authorization_never_follows_redirect():
    request = q.urllib.request.Request(q.BASE + 'loadCodeAssist', headers={'Authorization': 'Bearer synthetic'})
    with pytest.raises(q.ProbeError, match='redirect_refused'):
        q.NoRedirect().redirect_request(request, None, 302, 'found', {}, 'https://example.invalid')


def test_same_provider_sibling_accounts_use_their_own_tokens(profile):
    bundle, dest, cred = profile
    sibling = dest.parent / 'second-profile'
    shutil.copytree(dest, sibling)
    other_cred = sibling / 'home/.gemini/oauth_creds.json'
    data = json.loads(other_cred.read_text()); data['access_token'] = 'synthetic-account-b'
    private_fs.atomic_write(other_cred, json.dumps(data).encode(), q.MAX_BODY)
    a = Server({'cloudaicompanionProject': 'project-a'}, quota(0.8))
    b = Server({'cloudaicompanionProject': 'project-b'}, quota(0.3))
    assert read(profile, a)['windows']['google_bucket_0']['percent_remaining'] == 80
    result = q.probe(bundle, sibling, opener=b, now=NOW)['gemini']
    assert result['windows']['google_bucket_0']['percent_remaining'] == 30
    assert b.calls[0].get_header('Authorization') == 'Bearer synthetic-account-b'
    assert json.loads(cred.read_text())['access_token'] == 'synthetic-account-a'


def test_credits_are_not_subscription_quota(profile):
    credits = quota()
    credits['buckets'][0]['tokenType'] = 'AI_CREDITS'
    assert read(profile, Server({'cloudaicompanionProject': 'p'}, credits))['status'] == 'unknown'


def test_malformed_refresh_cannot_overwrite_credential(profile):
    _, _, cred = profile
    before = json.loads(cred.read_text()); before['expiry_date'] = 0
    private_fs.atomic_write(cred, json.dumps(before).encode(), q.MAX_BODY)
    server = Server({'access_token': 'synthetic-new', 'expires_in': True})
    assert read(profile, server)['status'] == 'unknown'
    assert json.loads(cred.read_text()) == before
