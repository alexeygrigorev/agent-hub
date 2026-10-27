import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_hub_host import opencode_auth as auth
from agent_hub_host.fleet import Dispatcher


def config(tmp_path):
    return dict(binary='/installed/opencode', source_data=str(tmp_path / 'native'),
                source_config=str(tmp_path / 'config'), target_data=str(tmp_path / 'runtime'))


def test_device_login_sync_tracks_new_login_and_preserves_runtime_refresh(tmp_path):
    cfg = config(tmp_path)
    calls = []
    source = [dict(id='native-account', integrationID='opencode', active=True,
                   value=dict(type='oauth', methodID='device', access='fixture-access', refresh='fixture-refresh'))]

    def native(cfg, args, env, payload=None):
        calls.append((args, payload))
        assert 'OPENCODE_API_KEY' not in env
        if args[:2] == ['auth', 'export']:
            return source
        if args == ['auth', 'import']:
            assert json.loads(payload) == source
            db = Path(cfg['target_data']) / 'opencode' / 'opencode.db'
            db.parent.mkdir(exist_ok=True)
            db.touch()
        if args[:2] == ['auth', 'list']:
            return [dict(id='opencode', connections=[dict(method='oauth')])]
        if args == ['api', 'model.list']:
            return dict(data=[dict(providerID='opencode-go', id=auth.MODEL.split('/')[1], enabled=True)])

    for i in range(2):
        source[0]['value']['access'] = 'rotated-access-' + str(i)
        assert auth.prepare(cfg, native=native)['status'] == 'ok'
    assert sum(a == ['auth', 'export', 'opencode'] for a, _ in calls) == 2
    assert sum(a == ['auth', 'import'] for a, _ in calls) == 1
    source[0]['id'] = 'renewed-native-login'
    auth.prepare(cfg, native=native)
    assert sum(a == ['auth', 'import'] for a, _ in calls) == 2


@pytest.mark.parametrize('failure', ['legacy-key', 'missing', '401', '403', 'contaminated'])
def test_auth_failure_is_sanitized_and_never_inference(tmp_path, failure):
    cfg = config(tmp_path)
    def native(cfg, args, env, payload=None):
        assert args[0] in ('auth', 'api')
        if failure == 'missing':
            return []
        if failure in ('401', '403'):
            raise RuntimeError(f'HTTP {failure} invalid fixture-secret')
        if args[:2] == ['auth', 'export']:
            return [dict(integrationID='opencode', active=True,
                         value=dict(type='api' if failure == 'legacy-key' else 'oauth', methodID='device',
                                    access='fixture-secret', refresh='fixture-refresh'))]
        if args[:2] == ['auth', 'list']:
            return [dict(id='opencode-go', connections=[dict(method='key')])]
    with pytest.raises(ValueError) as exc:
        auth.prepare(cfg, native=native)
    assert str(exc.value).startswith('engine_auth_expired:')
    assert 'fixture-secret' not in str(exc.value)


def test_nested_launch_environment_uses_only_verified_store(tmp_path):
    cfg = config(tmp_path)
    argv = auth.launch_argv(cfg, ['/usr/bin/env', 'XDG_DATA_HOME=/old',
                                  'OPENCODE_API_KEY=fixture-secret', 'OPENCODE_DB=/old.db', 'launcher'])
    assert not any('fixture-secret' in a or '/old' in a for a in argv)
    assert 'XDG_DATA_HOME=' + cfg['target_data'] in argv
    assert 'OPENCODE_DB=' + cfg['target_data'] + '/opencode/opencode.db' in argv
    unit = auth.launch_argv(cfg, ['/usr/bin/systemd-run', '--user', '--', '/usr/bin/env',
                                  'XDG_DATA_HOME=/old', 'launcher'])
    assert unit[unit.index('--') + 1] == '/usr/bin/env'
    assert unit.index('OPENCODE_DB=' + cfg['target_data'] + '/opencode/opencode.db') > unit.index('--')


def test_preflight_refuses_before_gate_claim_or_native_start(tmp_path):
    cfg = dict(version=1, workspaces={'demo': str(tmp_path)}, opencode_auth=config(tmp_path),
               subscriptions=[dict(service='go', account_label='muse', engine='opencode', profile=None,
                                   launcher_provider='opencode', admission_argv=['gate'])])
    calls = []
    dispatcher = Dispatcher(cfg, request=lambda *a, **k: calls.append(a), runner=lambda *a, **k: calls.append(a))
    with patch.object(auth, 'prepare', side_effect=ValueError('engine_auth_expired: device login required')):
        with pytest.raises(ValueError, match='engine_auth_expired'):
            dispatcher.dispatch(dict(id='owned-offer', account='muse', engine='opencode', profile=None,
                                     mode='headless', workspace='demo', prompt='probe'))
    assert calls == []


def test_lock_contention_is_retryable_without_auth_hold(tmp_path):
    with patch.object(auth.private_fs, 'exclusive_lock', side_effect=BlockingIOError), \
            patch.object(auth.time, 'monotonic', side_effect=[0, 31]):
        with pytest.raises(auth.AuthBusy, match='engine_auth_busy'):
            auth.prepare(config(tmp_path), native=lambda *a: pytest.fail('must not invoke native'))


def test_managed_bootstrap_preserves_device_contract_across_home_isolation(tmp_path):
    from agent_hub_host.provisioning import engine_environment
    cfg = config(tmp_path)
    with patch.dict(auth.os.environ, auth.environment(cfg, base={})), \
            patch('agent_hub_host.fleet.load_settings', side_effect=AssertionError('isolated config is not the host config')), \
            patch.object(auth, 'prepare', return_value={'status': 'ok'}) as prepare:
        env = engine_environment({'provider': 'go', 'env': {}}, tmp_path / 'managed-home')
    prepare.assert_called_once_with(cfg)
    assert env['OPENCODE_DB'] == cfg['target_data'] + '/opencode/opencode.db'
    assert env['XDG_CONFIG_HOME'] == cfg['target_data'] + '/config'


def proof_config(tmp_path):
    cfg = config(tmp_path)
    binary = tmp_path / 'opencode-fixture'
    binary.write_bytes(b'fixture-binary')
    cfg['binary'] = str(binary)
    source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE credential(id TEXT, integration_id TEXT,active INTEGER,'
                   'time_created INTEGER,time_updated INTEGER,value TEXT)')
        db.execute('INSERT INTO credential VALUES(?,?,?,?,?,?)',
                   ('fixture-login', 'opencode', 1, 1, 1, json.dumps({'refresh': 'fixture-refresh'})))
        db.execute('CREATE TABLE maintenance(value TEXT)')
    return cfg


def proof_native(cfg, args, env, payload=None):
    # Exercise the real private proof/flock protocol; replace only the native
    # CLI, so tests never open an installed credential store or run inference.
    with (Path(cfg['target_data']) / 'fixture-calls.log').open('a') as stream:
        stream.write(' '.join(args) + '\n')
    if args[:2] == ['auth', 'export']:
        return [dict(id='fixture-login', integrationID='opencode', active=True,
                     value=dict(type='oauth', methodID='device', access='fixture-access', refresh='fixture-refresh'))]
    if args == ['auth', 'import']:
        db = Path(cfg['target_data']) / 'opencode' / 'opencode.db'
        db.parent.mkdir(exist_ok=True)
        db.write_bytes(b'fixture-runtime')
    if args[:2] == ['auth', 'list']:
        return [dict(id='opencode', connections=[dict(method='oauth')])]
    if args == ['api', 'model.list']:
        return dict(data=[dict(providerID='opencode-go', id=auth.MODEL.split('/')[1], enabled=True)])


def proof_child(cfg):
    return auth.prepare(cfg, native=proof_native)


def test_positive_device_proof_is_shared_across_independent_processes(tmp_path):
    import multiprocessing
    cfg = proof_config(tmp_path)
    # Separate process memory models timer, dispatcher and managed bootstrap.
    with multiprocessing.get_context('spawn').Pool(3) as pool:
        results = pool.map(proof_child, [cfg] * 3)
    assert all(r['status'] == 'ok' for r in results)
    calls = (Path(cfg['target_data']) / 'fixture-calls.log').read_text().splitlines()
    assert calls.count('auth export opencode') == 1
    assert calls.count('api model.list') == 1
    proof = Path(cfg['target_data']) / 'device-verification.json'
    assert proof.stat().st_mode & 0o777 == 0o600
    assert 'fixture-access' not in proof.read_text()
    assert 'fixture-refresh' not in proof.read_text()


@pytest.mark.parametrize('change', ['source', 'wal', 'binary', 'runtime', 'legacy', 'receipt', 'expired', 'future', 'malformed'])
def test_changed_or_expired_device_proof_requires_real_verification(tmp_path, change):
    cfg = proof_config(tmp_path)
    auth.prepare(cfg, native=proof_native)
    target = Path(cfg['target_data'])
    proof = target / 'device-verification.json'
    wal = None
    if change == 'source':
        (Path(cfg['source_data']) / 'opencode' / 'opencode.db').write_bytes(b'new-login')
    elif change == 'wal':
        source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
        wal = sqlite3.connect(source)
        wal.execute('PRAGMA journal_mode=WAL')
        wal.execute('UPDATE credential SET value=?', (json.dumps({'refresh': 'committed-login-in-WAL'}),))
        wal.commit()  # Keep the connection open so the update remains in WAL.
    elif change == 'binary':
        Path(cfg['binary']).write_bytes(b'new-binary')
    elif change == 'runtime':
        replacement = target / 'replacement.db'
        replacement.write_bytes(b'different-runtime')
        replacement.replace(target / 'opencode' / 'opencode.db')
    elif change == 'legacy':
        (target / 'opencode' / 'auth.json').write_text('{}')
    elif change == 'receipt':
        (target / 'device-source.json').write_text('{}')
    elif change == 'malformed':
        proof.write_text('[]')
    else:
        data = json.loads(proof.read_text())
        data['verified_at'] += -61 if change == 'expired' else 61
        proof.write_text(json.dumps(data))
    # A previous success must never hide the next verification failure.
    try:
        with pytest.raises(ValueError, match='engine_auth_expired'):
            auth.prepare(cfg, native=lambda *a, **k: [])
    finally:
        if wal is not None:
            wal.close()


@pytest.mark.parametrize('verified_at', [10**400, -(10**400)])
def test_oversized_proof_timestamp_falls_back_to_successful_native_verification(tmp_path, verified_at):
    cfg = proof_config(tmp_path)
    auth.prepare(cfg, native=proof_native)
    target = Path(cfg['target_data'])
    proof = target / 'device-verification.json'
    data = json.loads(proof.read_text())
    data['verified_at'] = verified_at
    proof.write_text(json.dumps(data))
    assert auth.prepare(cfg, native=proof_native)['status'] == 'ok'
    calls = (target / 'fixture-calls.log').read_text().splitlines()
    assert calls.count('auth export opencode') == 2
    assert calls.count('api model.list') == 2
    assert abs(json.loads(proof.read_text())['verified_at']) < 10**400


@pytest.mark.parametrize('raw', [
    '[' * 2000 + '0' + ']' * 2000,
    '[]', 'null', 'true', '0', '"unknown"',
    '{"verified_at": true}', '{"verified_at": NaN}',
    '{"verified_at": Infinity}', '{"verified_at": {}}',
    '{"verified_at": []}', '{"verified_at": "unknown"}',
])
def test_malformed_optional_proof_falls_back_to_successful_native_verification(tmp_path, raw):
    cfg = proof_config(tmp_path)
    auth.prepare(cfg, native=proof_native)
    target = Path(cfg['target_data'])
    proof = target / 'device-verification.json'
    assert len(raw.encode()) <= 8192
    proof.write_text(raw)
    assert auth.prepare(cfg, native=proof_native)['status'] == 'ok'
    calls = (target / 'fixture-calls.log').read_text().splitlines()
    assert calls.count('auth export opencode') == 2
    assert calls.count('api model.list') == 2
    assert isinstance(json.loads(proof.read_text())['identity'], dict)


def test_optional_proof_decoder_failure_requires_successful_native_verification(tmp_path):
    cfg = proof_config(tmp_path)
    auth.prepare(cfg, native=proof_native)
    target = Path(cfg['target_data'])
    proof = target / 'device-verification.json'
    raw = proof.read_bytes()
    loads = json.loads

    def limited_decoder(value, *args, **kwargs):
        # Decoder recursion limits differ across supported Python versions.
        # Inject that exception only for the optional proof, leaving native
        # credentials and the replacement proof on the real decoder.
        if value == raw:
            raise RecursionError('fixture decoder limit')
        return loads(value, *args, **kwargs)

    with patch.object(auth.json, 'loads', side_effect=limited_decoder):
        assert auth.prepare(cfg, native=proof_native)['status'] == 'ok'
    calls = (target / 'fixture-calls.log').read_text().splitlines()
    assert calls.count('auth export opencode') == 2
    assert calls.count('api model.list') == 2
    assert json.loads(proof.read_text())['verified_at'] > 0


def test_runtime_session_writes_reuse_proof_but_login_change_during_check_does_not(tmp_path):
    cfg = proof_config(tmp_path)
    auth.prepare(cfg, native=proof_native)
    runtime = Path(cfg['target_data']) / 'opencode' / 'opencode.db'
    with runtime.open('ab') as stream:
        stream.write(b'ordinary-session-write-or-token-refresh')
    auth.prepare(cfg, native=lambda *a, **k: pytest.fail('fresh positive proof should be shared'))
    # Force a fresh verification, then alter the source while model discovery
    # runs: that new source was never exported and cannot receive a proof.
    source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
    source.write_bytes(b'login-before-discovery')
    def changing_native(cfg, args, env, payload=None):
        value = proof_native(cfg, args, env, payload)
        if args == ['api', 'model.list']:
            source.write_bytes(b'login-changed-during-discovery')
        return value
    auth.prepare(cfg, native=changing_native)
    with pytest.raises(ValueError, match='engine_auth_expired'):
        auth.prepare(cfg, native=lambda *a, **k: [])


def test_native_export_checkpoint_does_not_prevent_positive_proof(tmp_path):
    cfg = proof_config(tmp_path)
    def checkpointing_native(cfg, args, env, payload=None):
        result = proof_native(cfg, args, env, payload)
        if args[:2] == ['auth', 'export']:
            source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
            with sqlite3.connect(source) as db:
                db.execute('INSERT INTO maintenance VALUES(?)', ('native-export-checkpoint',))
        return result
    auth.prepare(cfg, native=checkpointing_native)
    auth.prepare(cfg, native=lambda *a, **k: pytest.fail('export checkpoint must not defeat sharing'))


def test_later_quota_export_state_writes_do_not_defeat_verified_login_reuse(tmp_path):
    cfg = proof_config(tmp_path)
    auth.prepare(cfg, native=proof_native)
    source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
    with sqlite3.connect(source) as db:
        db.execute('INSERT INTO maintenance VALUES(?)', ('independent-quota-export',))
    auth.prepare(cfg, native=lambda *a, **k: pytest.fail('quota state is not a login change'))


def test_login_switch_during_export_never_attests_the_new_source_with_old_login(tmp_path):
    cfg = proof_config(tmp_path)
    def switched_native(cfg, args, env, payload=None):
        result = proof_native(cfg, args, env, payload)
        if args[:2] == ['auth', 'export']:
            # Return A, but commit B before export returns. Deliberately keep
            # id/active/timestamps unchanged: value hashing must catch this.
            source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
            with sqlite3.connect(source) as db:
                db.execute('UPDATE credential SET value=?', (json.dumps({'refresh': 'different-login-B'}),))
        return result
    auth.prepare(cfg, native=switched_native)
    assert not (Path(cfg['target_data']) / 'device-verification.json').exists()
    with pytest.raises(ValueError, match='engine_auth_expired'):
        auth.prepare(cfg, native=lambda *a, **k: [])


def test_unsupported_native_revision_schema_disables_only_reuse(tmp_path):
    cfg = proof_config(tmp_path)
    source = Path(cfg['source_data']) / 'opencode' / 'opencode.db'
    with sqlite3.connect(source) as db:
        db.execute('DROP TABLE credential')
    for _ in range(2):
        assert auth.prepare(cfg, native=proof_native)['status'] == 'ok'
    calls = (Path(cfg['target_data']) / 'fixture-calls.log').read_text().splitlines()
    assert calls.count('auth export opencode') == 2
    assert not (Path(cfg['target_data']) / 'device-verification.json').exists()


def test_native_retries_once_on_transient_readonly_failure(tmp_path):
    cfg = config(tmp_path)
    Path(cfg['target_data']).mkdir(parents=True, exist_ok=True)
    attempts = []

    class FakeProc:
        def __init__(self, rc, out, stdout_file):
            self.returncode = rc
            self.pid = 12345
            self._out = out
            self._stdout = stdout_file

        def communicate(self, payload, timeout=None):
            self._stdout.write(self._out)
            self._stdout.flush()

    def fake_popen(argv, env=None, stdin=None, stdout=None, stderr=None, start_new_session=None):
        attempts.append(argv)
        if len(attempts) == 1:
            return FakeProc(1, b'', stdout)
        return FakeProc(0, b'[{"id":"ok"}]', stdout)

    with patch.object(auth.subprocess, 'Popen', side_effect=fake_popen), \
            patch.object(auth.time, 'sleep'):
        res = auth._native(cfg, ['auth', 'export', 'opencode'], {}, payload=None)
    assert res == [{'id': 'ok'}]
    assert len(attempts) == 2
