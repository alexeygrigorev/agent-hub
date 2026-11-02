import contextlib
import datetime as dt
import json
import os
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
    assert called == [(bundle_dir / 'engine/bin' / ('agy.exe' if os.name == 'nt' else 'agy'), bundle_dir / 'home')]
    assert result['accounts'][0]['account'] == 'hub-antigravity'
    assert result['accounts'][0]['quota']['status'] == 'ok'
    assert result['accounts'][0]['quota']['windows']['5h']['percent_remaining'] == 64


def _alive(pid):
    import os
    import subprocess
    if os.name == 'nt':
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie is dead for our purposes
        return ') Z' not in open(f'/proc/{pid}/stat').read()
    except OSError:
        return True


@pytest.mark.parametrize('binary', [
    None, '', r'C:\Users\x\AppData\Local\Programs\Antigravity\Antigravity.exe',
    r'C:\Users\x\AppData\Local\Programs\Antigravity\bin\antigravity.cmd',
    '/usr/bin/antigravity', r'C:\Users\x\AppData\Local\Programs\Antigravity\bin\agy.exe',
])
def test_probe_rejects_desktop_app_and_launcher_without_spawning(tmp_path, binary):
    home = tmp_path / 'h'
    token = home / quota.TOKEN_PATH
    token.parent.mkdir(parents=True)
    token.write_text(json.dumps({'token': {'access_token': 'a', 'refresh_token': 'r'}}))
    assert not quota.valid_cli(binary)
    result = quota.probe(binary, home, runner=lambda *a, **k: pytest.fail('must not spawn'))
    assert result['gemini']['status'] == 'unknown'


def test_valid_cli_accepts_agy_names():
    assert quota.valid_cli('/home/u/.local/bin/agy')
    assert quota.valid_cli(r'C:\Users\x\AppData\Local\Programs\agy\agy.exe')


def test_hanging_probe_is_killed_with_its_children(tmp_path):
    import os
    import subprocess
    import sys
    import time
    pidfile = tmp_path / 'pids'
    script = tmp_path / 'hang.py'
    script.write_text(
        'import subprocess, sys, time\n'
        'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])\n'
        f'open({str(pidfile)!r}, "w").write(str(c.pid))\n'
        'time.sleep(300)\n')
    started = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        quota.run_bounded([sys.executable, str(script)], env=dict(os.environ), timeout=2)
    assert time.time() - started < 30
    child = int(pidfile.read_text())
    deadline = time.time() + 10
    while time.time() < deadline and _alive(child):
        time.sleep(0.2)
    assert not _alive(child)
