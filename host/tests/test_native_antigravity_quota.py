import contextlib
import datetime as dt
import json
from pathlib import Path

import pytest

from agent_hub_host import native_antigravity_quota as quota

NOW = dt.datetime(2030, 1, 2, 12, tzinfo=dt.timezone.utc)


def payload(response):
    return {'status': 'SUCCESS', 'error': None, 'response': response}


def test_usage_parser_reads_gemini_windows_and_ignores_other_groups():
    result = quota.parse_usage(payload(
        'Gemini Models\tWeekly Limit Remaining\t77%\t2030-01-07T12:00:00Z\n'
        'Gemini Models\tFive Hour Limit Remaining\t88%\t2030-01-02T17:00:00Z\n'
        'Claude and GPT models\tWeekly Limit Remaining\t10%\t2030-01-07T12:00:00Z\n'), now=NOW)
    route = result['gemini']
    assert route['status'] == 'ok'
    assert route['windows'] == {
        '5h': {'percent_remaining': 88.0, 'reset_at': '2030-01-02T17:00:00+00:00'},
        '7d': {'percent_remaining': 77.0, 'reset_at': '2030-01-07T12:00:00+00:00'},
    }
    assert route['details']['limit_reached'] is False


@pytest.mark.parametrize('line', [
    'Gemini Models\tWeekly Limit Remaining\t101%\t2030-01-07T12:00:00Z',
    'Gemini Models\tWeekly Limit Remaining\t??\t2030-01-07T12:00:00Z',
    'Gemini Models\tWeekly Limit Remaining\t77%\t2029-01-07T12:00:00Z',
    'Gemini Models\tWeekly Limit Remaining\t77%\t2030-01-07T12:00:00',
])
def test_usage_parser_rejects_invalid_or_stale_quota(line):
    with pytest.raises(ValueError):
        quota.parse_usage(payload(line), now=NOW)


def test_usage_parser_does_not_fabricate_when_selected_group_is_missing():
    with pytest.raises(ValueError, match='gemini_quota_missing'):
        quota.parse_usage(payload(
            'Claude and GPT models\tWeekly Limit Remaining\t100%\t2030-01-07T12:00:00Z'), now=NOW)


def test_probe_uses_only_the_exact_home_and_official_read_only_usage_command(tmp_path):
    home = tmp_path / 'account-home'
    token = home / quota.TOKEN_PATH
    token.parent.mkdir(parents=True)
    token.write_text(json.dumps({'token': {'access_token': 'synthetic-a', 'refresh_token': 'synthetic-r'}}))
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return payload('Gemini Models\tWeekly Limit Remaining\t50%\t2030-01-07T12:00:00Z')

    result = quota.probe('/pinned/agy', home, runner=runner, now=NOW)
    assert result['gemini']['windows']['7d']['percent_remaining'] == 50
    assert calls[0][0] == ['/pinned/agy', '-p', '/usage', '--output-format', 'json']
    assert calls[0][1]['env']['HOME'] == str(home)
    assert calls[0][1]['timeout'] <= 20
    assert 'GEMINI_API_KEY' not in calls[0][1]['env']


def test_probe_skips_cli_without_a_private_native_credential(tmp_path):
    result = quota.probe('/pinned/agy', tmp_path, runner=lambda *a, **k: pytest.fail('must not run'))
    assert result['gemini']['status'] == 'unknown'
    assert result['gemini']['windows'] == {}


def test_managed_quota_uses_antigravity_probe_for_its_bundle(tmp_path, monkeypatch):
    from agent_hub_host import native_antigravity_quota, provisioning

    root = tmp_path / 'managed'
    bundle_dir = root / 'hub-antigravity-rev-a'
    bundle_dir.mkdir(parents=True)
    state = [{'label': 'hub-antigravity', 'revision': 'rev-a', 'provider': 'antigravity'}]
    called = []

    monkeypatch.setattr(provisioning, 'root', lambda: root)
    monkeypatch.setattr(provisioning, 'lock', contextlib.nullcontext)
    monkeypatch.setattr(provisioning, 'read_json',
                        lambda path, fallback=None: state if Path(path).name == 'state.json'
                        else {'provider': 'antigravity'})
    monkeypatch.setattr(provisioning, 'validate', lambda bundle: None)
    monkeypatch.setattr(native_antigravity_quota, 'probe',
                        lambda binary, home: (called.append((binary, home)) or {'gemini': {
                            'status': 'ok', 'error': None, 'details': {'limit_reached': False},
                            'windows': {'5h': {'percent_remaining': 64, 'reset_at': '2030-01-02T17:00:00Z'}}}}))
    result = provisioning.quotas()
    assert called == [(bundle_dir / 'engine/bin/agy', bundle_dir / 'home')]
    assert result['accounts'][0]['account'] == 'hub-antigravity'
    assert result['accounts'][0]['quota']['status'] == 'ok'
    assert result['accounts'][0]['quota']['windows']['5h']['percent_remaining'] == 64
