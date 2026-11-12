"""Real private-file worker discovery; fake credentials and blocked transports."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error

import pytest

from agent_hub_host import api, auth, config, intake, session_control, worker_auth

SID = '11111111-1111-4111-8111-111111111111'
INTAKE = '33333333-3333-4333-8333-333333333333'
TOKEN = 'FAKE_WORKER_ONLY_' + 'x' * 40
DIAGNOSTIC = 'Worker context unavailable or unsafe; host re-issuance required.'


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(mode=0o700)
    folder = workspace / '.local' / 'hub2-contexts'
    folder.mkdir(mode=0o700, parents=True)
    path = folder / (SID + '-7.json')
    data = {'server': 'https://worker.example.invalid', 'token': TOKEN,
            'expires_at': time.time() + 600,
            'worker': {'session_id': SID, 'operation_id': '22222222-2222-4222-8222-222222222222:0',
                       'epoch': 7, 'intake_id': INTAKE, 'project_id': 'synthetic/project',
                       'machine_id': 'synthetic-machine', 'actor': 'session:' + SID}}
    def write(value=data):
        path.write_text(json.dumps(value), encoding='utf-8')
        path.chmod(0o600)
    write()
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('APLEXER_TAG', 'task-hub2-' + SID)
    monkeypatch.setenv('APLEXER_WORKSPACE', str(workspace))
    monkeypatch.setenv('APLEXER_SESSION_ID', '44444444-4444-4444-8444-444444444444')
    monkeypatch.setenv('HUB2_CONFIG', str(path))
    monkeypatch.setenv('HUB_CLI_SKIP_STALE_CHECK', '1')
    monkeypatch.setattr(api, 'safe_http_json', lambda *a, **kw: pytest.fail('network forbidden'))
    monkeypatch.setattr(auth, 'refresh_token', lambda *a, **kw: pytest.fail('refresh forbidden'))
    # Hostile profiles must never be read as a fallback from any worker failure.
    for profile in (config._default_human_config_path(), config.get_host_config_path()):
        profile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile.write_text(json.dumps({'base_url': 'https://privileged.example.invalid',
                                      'access_token': 'FAKE_PRIVILEGED_ONLY', 'client_kind': 'human'}))
        profile.chmod(0o600)
    return path, data, write


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def test_real_loader_translates_only_in_memory_and_does_not_borrow(scoped, tmp_path):
    path, data, _ = scoped
    before = snapshot(tmp_path)
    profile = config.load_config()
    assert profile['base_url'] == data['server']
    assert profile['access_token'] == TOKEN
    assert profile['_hub2_worker'] == data['worker']
    assert auth.get_access_token() == TOKEN
    assert 'refresh_token' not in profile
    assert worker_auth.config_path() == path
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('fault', ['missing', 'bad_json', 'duplicate', 'null', 'host_enrollment',
    'extra', 'missing_worker', 'expired', 'far_future', 'nan', 'bool_expiry', 'short_token',
    'token_control', 'http', 'userinfo', 'query', 'fragment', 'server_path', 'backslash',
    'wrong_session', 'wrong_actor', 'wrong_epoch', 'bool_epoch', 'bad_operation', 'bad_project',
    'bad_machine', 'bad_intake', 'extra_worker'])
def test_bad_context_never_falls_back_or_sends(scoped, monkeypatch, fault):
    path, original, write = scoped
    data = copy.deepcopy(original)
    if fault == 'missing': path.unlink()
    elif fault == 'bad_json': path.write_text('{')
    elif fault == 'duplicate': path.write_text(json.dumps(data)[:-1] + ',"token":"FAKE_OTHER"}')
    elif fault == 'null': write(None)
    elif fault == 'host_enrollment': write({'server': data['server'], 'token': TOKEN, 'machine_id': 'host'})
    else:
        if fault == 'extra': data['unknown'] = True
        elif fault == 'missing_worker': del data['worker']
        elif fault == 'expired': data['expires_at'] = time.time() - 1
        elif fault == 'far_future': data['expires_at'] = time.time() + 90000
        elif fault == 'nan': data['expires_at'] = float('nan')
        elif fault == 'bool_expiry': data['expires_at'] = True
        elif fault == 'short_token': data['token'] = 'short'
        elif fault == 'token_control': data['token'] = TOKEN + '\n'
        elif fault in ('http', 'userinfo', 'query', 'fragment', 'server_path', 'backslash'):
            data['server'] = {'http': 'http://localhost', 'userinfo': 'https://user@worker.example.invalid',
                'query': data['server'] + '?q=x', 'fragment': data['server'] + '#x',
                'server_path': data['server'] + '/evil', 'backslash': data['server'] + '/\\evil'}[fault]
        elif fault == 'wrong_session': data['worker']['session_id'] = INTAKE
        elif fault == 'wrong_actor': data['worker']['actor'] = 'operator:synthetic'
        elif fault == 'wrong_epoch': data['worker']['epoch'] = 8
        elif fault == 'bool_epoch': data['worker']['epoch'] = True
        elif fault == 'bad_operation': data['worker']['operation_id'] = 'bad\noperation'
        elif fault == 'bad_project': data['worker']['project_id'] = ''
        elif fault == 'bad_machine': data['worker']['machine_id'] = 'bad machine'
        elif fault == 'bad_intake': data['worker']['intake_id'] = 'not-an-intake'
        elif fault == 'extra_worker': data['worker']['unknown'] = 'unexpected'
        write(data)
    for call in (config.load_config, lambda: intake.request('GET', '/api/intakes/' + INTAKE),
                 lambda: session_control.request('GET', '/api/fleet')):
        with pytest.raises(worker_auth.WorkerContextError, match='^' + DIAGNOSTIC.replace('.', r'\.') + '$'):
            call()


@pytest.mark.parametrize('key,value', [('HUB_CONFIG_FILE', 'host_profile'),
    ('HUB_WORKER_CONFIG_FILE', 'legacy_profile'), ('HUB_TOKEN', 'FAKE_PRIVILEGED_ONLY'),
    ('HUB_JOB_TOKEN', 'FAKE_PRIVILEGED_ONLY'), ('HUB_URL', 'https://elsewhere.example.invalid'),
    ('HUB_BASE_URL', 'https://worker.example.invalid/evil'), ('HUB_URL', 'http://localhost')])
def test_bound_worker_rejects_profile_and_transport_overrides(scoped, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(worker_auth.WorkerContextError):
        config.load_config()


def test_matching_origin_and_token_overrides_are_not_privilege_changes(scoped, monkeypatch):
    monkeypatch.setenv('HUB_URL', 'https://WORKER.example.invalid:443/')
    monkeypatch.setenv('HUB_TOKEN', TOKEN)
    assert config.load_config()['access_token'] == TOKEN


@pytest.mark.parametrize('fault', ['no_selector', 'no_workspace', 'no_tag', 'bad_tag', 'relative',
    'outside', 'epoch_filename', 'dotdot', 'symlink', 'hardlink', 'parent_symlink',
    'wide_file', 'wide_parent', 'oversized', 'fifo'])
def test_path_identity_and_filesystem_attacks_refused(scoped, tmp_path, monkeypatch, fault):
    path, _, _ = scoped
    if fault == 'no_selector': monkeypatch.delenv('HUB2_CONFIG')
    elif fault == 'no_workspace': monkeypatch.delenv('APLEXER_WORKSPACE')
    elif fault == 'no_tag': monkeypatch.delenv('APLEXER_TAG')
    elif fault == 'bad_tag': monkeypatch.setenv('APLEXER_TAG', 'task-hub2-invalid')
    elif fault == 'relative': monkeypatch.setenv('HUB2_CONFIG', path.name)
    elif fault == 'outside': monkeypatch.setenv('HUB2_CONFIG', str(tmp_path / path.name))
    elif fault == 'epoch_filename': monkeypatch.setenv('HUB2_CONFIG', str(path.with_name(SID + '-8.json')))
    elif fault == 'dotdot': monkeypatch.setenv('HUB2_CONFIG', str(path.parent / '..' / 'hub2-contexts' / path.name))
    elif fault == 'symlink':
        saved = path.with_suffix('.saved'); path.rename(saved); path.symlink_to(saved)
    elif fault == 'hardlink': os.link(path, path.with_suffix('.linked'))
    elif fault == 'parent_symlink':
        saved = path.parent.with_name('saved'); path.parent.rename(saved); path.parent.symlink_to(saved, target_is_directory=True)
    elif fault == 'wide_file': path.chmod(0o644)
    elif fault == 'wide_parent': path.parent.chmod(0o755)
    elif fault == 'oversized': path.write_text(' ' * 16385)
    elif fault == 'fifo': path.unlink(); os.mkfifo(path, 0o600)
    with pytest.raises(worker_auth.WorkerContextError): config.load_config()


@pytest.mark.parametrize('consumer', [intake, session_control])
@pytest.mark.parametrize('status', [200, 401, 403])
def test_both_real_http_consumers_use_only_worker_and_do_not_refresh(scoped, monkeypatch, consumer, status):
    seen = []
    def transport(req, **kwargs):
        seen.append((req.full_url, req.get_header('Authorization')))
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, 'synthetic', {}, io.BytesIO(b'{"error":"forbidden"}'))
        return {'synthetic': True}
    monkeypatch.setattr(api, 'safe_http_json', transport)
    route = '/api/intakes/' + INTAKE if consumer is intake else '/api/fleet'
    if status == 200: assert consumer.request('GET', route) == {'synthetic': True}
    else:
        with pytest.raises((intake.IntakeHTTPError, session_control.SessionHTTPError)) as exc:
            consumer.request('GET', route)
        assert exc.value.status == status
    assert seen == [('https://worker.example.invalid' + route, 'Bearer ' + TOKEN)]


def test_retry_rechecks_expiry_before_second_send(scoped, monkeypatch):
    _, data, _ = scoped
    calls = []
    def transport(req, **kw):
        calls.append(req.full_url)
        monkeypatch.setattr(worker_auth.time, 'time', lambda: data['expires_at'] + 1)
        raise urllib.error.HTTPError(req.full_url, 503, 'synthetic', {}, io.BytesIO(b'{}'))
    monkeypatch.setattr(api, 'safe_http_json', transport)
    with pytest.raises(worker_auth.WorkerContextError): intake.request('GET', '/api/intakes/' + INTAKE)
    assert len(calls) == 1


def test_scoped_context_is_read_only_in_save_and_refresh_paths(scoped, tmp_path):
    before = snapshot(tmp_path)
    with pytest.raises(worker_auth.WorkerContextError): config.save_config({'access_token': 'FAKE'})
    with pytest.raises(worker_auth.WorkerContextError):
        with worker_auth.refresh_guard(): pytest.fail('must refuse refresh')
    assert snapshot(tmp_path) == before


def test_summary_fast_does_not_borrow_hostile_profile(scoped):
    from agent_hub_host.summary_fast import _fast_load_posix_config
    assert _fast_load_posix_config() is None
    scoped[0].unlink()
    assert _fast_load_posix_config() is None


def test_nonworker_explicit_profile_authority_unchanged(scoped, monkeypatch):
    monkeypatch.delenv('APLEXER_TAG'); monkeypatch.delenv('HUB2_CONFIG')
    profile = config.get_host_config_path()
    monkeypatch.setenv('HUB_CONFIG_FILE', str(profile))
    assert config.load_config()['access_token'] == 'FAKE_PRIVILEGED_ONLY'


@pytest.mark.parametrize('command', [['intake', 'show', INTAKE], ['sessions', 'list', '--json']])
def test_actual_cli_subprocess_reports_sanitized_missing_context_nonzero(scoped, tmp_path, command):
    path, _, _ = scoped
    path.unlink()
    before = snapshot(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'XDG_CONFIG_HOME', 'APPDATA',
        'APLEXER_TAG', 'APLEXER_WORKSPACE', 'APLEXER_SESSION_ID', 'HUB2_CONFIG', 'HUB_CLI_SKIP_STALE_CHECK')}
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # An audit hook blocks real DNS/connect without replacing socket.socket's
    # type (SSL imports subclass that type).
    code = '''import http.client,runpy,sys
sys.addaudithook(lambda event,args: (_ for _ in ()).throw(AssertionError('network forbidden')) if event in ('socket.connect','socket.getaddrinfo') else None)
runpy.run_module('agent_hub_host',run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', code, *command], env=env, text=True,
                            capture_output=True, timeout=10, cwd=tmp_path)
    assert result.returncode != 0
    assert DIAGNOSTIC in result.stderr
    assert TOKEN not in result.stderr and str(path) not in result.stderr
    assert 'Traceback' not in result.stderr and 'network forbidden' not in result.stderr
    assert snapshot(tmp_path) == before


def test_wrong_owner_is_refused_by_real_private_file_reader(scoped, monkeypatch):
    original = worker_auth.private_fs.os.fstat
    def other_owner(fd):
        value = list(original(fd))
        value[4] = os.getuid() + 1
        return os.stat_result(value)
    monkeypatch.setattr(worker_auth.private_fs.os, 'fstat', other_owner)
    with pytest.raises(worker_auth.WorkerContextError): config.load_config()


def test_nested_duplicate_binding_keys_are_refused(scoped):
    path, data, _ = scoped
    path.write_text(json.dumps(data).replace('"epoch": 7', '"epoch": 7, "epoch": 8'))
    with pytest.raises(worker_auth.WorkerContextError): config.load_config()


@pytest.mark.parametrize('field', ['token', 'operation_id', 'project_id', 'machine_id'])
def test_changed_binding_or_token_between_load_and_send_is_refused(scoped, field):
    _, data, write = scoped
    profile = config.load_config()
    value = copy.deepcopy(data)
    if field == 'token': value['token'] = 'FAKE_REPLACEMENT_' + 'x' * 40
    else: value['worker'][field] += '-changed'
    write(value)
    with pytest.raises(worker_auth.WorkerContextError): worker_auth.validate_v2_request(profile)


@pytest.mark.parametrize('key', ['HUB_URL', 'HUB_BASE_URL', 'HUB_TOKEN', 'HUB_JOB_TOKEN'])
def test_bus_overrides_are_blocked_centrally_before_transport(scoped, monkeypatch, key):
    from agent_hub_host import bus
    monkeypatch.setenv(key, 'https://elsewhere.example.invalid' if 'URL' in key else 'FAKE_OTHER')
    with pytest.raises(worker_auth.WorkerContextError): bus._do_request('GET', '/api/bus/inbox')


def test_nonworker_hub2_host_enrollment_does_not_change_explicit_operator_profile(scoped, monkeypatch, tmp_path):
    monkeypatch.delenv('APLEXER_TAG')
    enrollment = tmp_path / 'host-enrollment.json'
    enrollment.write_text(json.dumps({'server': 'https://host.example.invalid', 'token': 'FAKE_HOST', 'machine_id': 'fake'}))
    enrollment.chmod(0o600)
    monkeypatch.setenv('HUB2_CONFIG', str(enrollment))
    monkeypatch.setenv('HUB_CONFIG_FILE', str(config.get_host_config_path()))
    assert config.load_config()['access_token'] == 'FAKE_PRIVILEGED_ONLY'


@pytest.mark.parametrize('operation', ['load_host_config', 'load_human_config', 'save_host_config',
    'save_human_config', 'update_config', 'clear_config', 'clear_human_config'])
def test_direct_privileged_profile_io_cannot_escape_worker_binding(scoped, tmp_path, operation):
    before = snapshot(tmp_path)
    fn = getattr(config, operation)
    args = ({'access_token': 'FAKE_OTHER'},) if operation.startswith('save_') else ()
    with pytest.raises(worker_auth.WorkerContextError): fn(*args)
    assert snapshot(tmp_path) == before
    assert not list(tmp_path.rglob('*.lock'))


@pytest.mark.parametrize('identity', ['missing', 'corrupt'])
def test_full_cli_summary_missing_identity_never_reads_or_sends_hostile_profile(scoped, tmp_path, identity):
    before = snapshot(tmp_path)
    env = {key: value for key, value in os.environ.items() if key in
           ('PATH', 'HOME', 'XDG_CONFIG_HOME', 'APPDATA', 'HUB2_CONFIG', 'HUB_CLI_SKIP_STALE_CHECK')}
    if identity == 'corrupt':
        env['APLEXER_TAG'] = 'task-hub2-corrupt'
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    code = '''import http.client,runpy,sys
sys.addaudithook(lambda event,args: (_ for _ in ()).throw(AssertionError('network forbidden')) if event in ('socket.connect','socket.getaddrinfo') else None)
runpy.run_module('agent_hub_host',run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', code, 'intake', 'summary'], env=env,
                            capture_output=True, text=True, cwd=tmp_path, timeout=10)
    assert result.returncode != 0
    assert DIAGNOSTIC in result.stderr
    for secret in (TOKEN, 'FAKE_PRIVILEGED_ONLY', str(scoped[0]), 'network forbidden', 'Traceback'):
        assert secret not in result.stderr
    assert snapshot(tmp_path) == before


def test_deeply_nested_json_is_typed_and_does_not_fall_back(scoped):
    path, _, _ = scoped
    path.write_text('[' * 2000 + ']' * 2000)
    with pytest.raises(worker_auth.WorkerContextError): config.load_config()
