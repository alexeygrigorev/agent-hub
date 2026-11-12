"""Managed Claude generation delivery into the actual maintained managed home.

Synthetic only: a temp HOME, managed-engines root and aplexer profile; the real
provisioning lock, receipt, scoped writer and receipt publication. No network,
service, provider, live credential or live profile is read or changed.
"""
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from agent_hub_host import credentials, managed_claude_deliver as deliver, provisioning as p

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='managed Claude delivery is Linux only')

REPO = Path(__file__).resolve().parents[2]
REV = '33333333-3333-4333-8333-333333333333'
OTHER_REV = '11111111-1111-4111-8111-111111111111'
PATH = '.claude/.credentials.json'
SECRET = 'synthetic-credential-marker'
EXPIRES = int(time.time() * 1000) + 86400000


def credential(generation):
    return json.dumps({'claudeAiOauth': {'accessToken': SECRET + '-%d' % generation, 'refreshToken': '',
                                         'expiresAt': EXPIRES}})


def target_bundle(generation=7):
    return dict(label='hub-claude', provider='claude', version='2.1.39', revision=REV,
                credential_generation=generation, credential_paths=[PATH], files={PATH: credential(generation)},
                env={}, args=[])


def other_bundle():
    return dict(label='test-account', provider='codex', version='0.163.0', revision=OTHER_REV,
                files={}, env={}, args=[])


def request(generation=8, revision=REV, files=None):
    return {'label': 'hub-claude', 'generation': generation, 'revision': revision,
            'files': {PATH: credential(generation)} if files is None else files}


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.home = tmp_path / 'home'
        self.managed = tmp_path / 'managed'
        self.profile = tmp_path / 'aplexer' / 'config.toml'
        self.env = {'HOME': str(self.home), 'APLEXER_CONFIG': str(self.profile),
                    'AGENT_HUB_MANAGED_DIR': str(self.managed)}
        for key, value in self.env.items():
            monkeypatch.setenv(key, value)
        self.dest = self.managed / ('hub-claude-' + REV)

    def install(self, generation=7, profile=True, state=True):
        self.managed.mkdir(mode=0o700)
        if state:
            rows = [{'label': 'test-account', 'revision': OTHER_REV, 'provider': 'codex'},
                    {'label': 'hub-claude', 'revision': REV, 'provider': 'claude'}]
            (self.managed / 'state.json').write_text(json.dumps(rows, indent=1) + '\n')
            (self.managed / 'state.json').chmod(0o600)
        unrelated = self.managed / ('test-account-' + OTHER_REV)
        unrelated.mkdir(mode=0o700)
        (unrelated / 'keep.bin').write_bytes(b'unrelated exact bytes')
        (self.dest / 'home/.claude').mkdir(parents=True, mode=0o700)
        self.dest.chmod(0o700)
        (self.dest / 'home').chmod(0o700)
        bundle = target_bundle(generation)
        p.save_bundle(self.dest, bundle)
        target = self.dest / 'home' / PATH
        target.write_text(bundle['files'][PATH])
        target.chmod(0o600)
        p.write_json(self.dest / credentials.STATE_FILE, {'generation': generation, 'digests': {
            PATH: hashlib.sha256(bundle['files'][PATH].encode()).hexdigest()}})
        if profile:
            self.profile.parent.mkdir(mode=0o700)
            self.profile.write_text('version = 1\n')
            self.profile.chmod(0o600)
            p.profiles([other_bundle(), bundle])
        return self

    def snapshot(self):
        files = {}
        for root in (self.managed, self.profile.parent):
            if root.exists():
                for path in sorted(root.rglob('*')):
                    if path.is_file() and path.name != 'reconcile.lock':
                        files[str(path)] = path.read_bytes()
        return files

    def held(self):
        return (self.dest / 'home' / PATH).read_text()

    def state(self):
        return json.loads((self.dest / credentials.STATE_FILE).read_text())


def run(value, argv=('materialize',)):
    out = io.StringIO()
    stdin = io.BytesIO(json.dumps(value).encode() if not isinstance(value, bytes) else value)
    import contextlib
    with contextlib.redirect_stdout(out):
        code = deliver.main(list(argv), stdin=stdin)
    text = out.getvalue()
    assert SECRET not in text
    return code, json.loads(text)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('network is not part of the helper authority')
    monkeypatch.setattr(socket, 'socket', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)


def test_new_generation_is_materialized_into_the_maintained_home(world):
    world.install(7)
    before = world.snapshot()
    code, result = run(request(8))
    assert (code, result) == (0, {'status': 'materialized', 'generation': 8, 'revision': REV})
    assert world.held() == credential(8)
    assert world.state() == {'generation': 8, 'digests': {PATH: hashlib.sha256(credential(8).encode()).hexdigest()}}
    # The receipt's offline copy is the delivered generation, as scoped install publishes it.
    receipt = p.load_bundle(world.dest)
    assert receipt['credential_generation'] == 8 and receipt['files'] == {PATH: credential(8)}
    after = world.snapshot()
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    assert changed <= {str(world.dest / 'home' / PATH), str(world.dest / credentials.STATE_FILE),
                       str(world.dest / 'bundle.json'), str(world.dest / p.CREDENTIALS_FILE)}
    assert str(world.profile) not in changed
    assert str(world.managed / 'state.json') not in changed


def test_same_generation_is_in_step_and_idempotent(world):
    world.install(7)
    run(request(8))
    snapshot = world.snapshot()
    assert run(request(8)) == (0, {'status': 'in_step', 'generation': 8, 'revision': REV})
    assert world.snapshot() == snapshot


def test_offline_fallback_cannot_revert_a_delivered_generation(world):
    world.install(7)
    run(request(8))
    def unreachable(*args, **kwargs):
        raise OSError('hub unreachable')
    # The exec path's Hub-unreachable fallback re-materializes the receipt copy.
    credentials.reconcile('hub-claude', p.load_bundle(world.dest), world.dest / 'home', world.dest, unreachable)
    assert world.held() == credential(8)
    assert world.state()['generation'] == 8


@pytest.mark.parametrize('profile,state', [(False, True), (True, False)])
def test_absent_consumer_writes_nothing(world, profile, state):
    world.install(7, profile=profile, state=state)
    before = world.snapshot()
    assert run(request(8)) == (0, {'status': 'absent'})
    assert world.snapshot() == before


def test_nothing_installed_is_absent(world):
    assert run(request(8)) == (0, {'status': 'absent'})


def test_older_generation_is_refused(world):
    world.install(7)
    before = world.snapshot()
    assert run(request(6)) == (3, {'status': 'refused', 'reason': 'generation_refused'})
    assert world.snapshot() == before


def test_equal_generation_with_changed_material_is_refused(world):
    world.install(7)
    before = world.snapshot()
    changed = json.dumps({'claudeAiOauth': {'accessToken': SECRET + '-other', 'expiresAt': 1}})
    assert run(request(7, files={PATH: changed})) == (3, {'status': 'refused', 'reason': 'generation_refused'})
    assert world.snapshot() == before


def test_new_engine_revision_is_left_to_the_maintained_install(world):
    world.install(7)
    before = world.snapshot()
    other = '44444444-4444-4444-8444-444444444444'
    assert run(request(8, revision=other)) == (3, {'status': 'refused', 'reason': 'receipt_revision_mismatch'})
    assert world.snapshot() == before


@pytest.mark.parametrize('value,argv', [
    (dict(request(8), label='test-account'), ('materialize',)),
    (dict(request(8), files={PATH: credential(8), '.claude/settings.json': '{}'}), ('materialize',)),
    (dict(request(8), extra=True), ('materialize',)),
    (dict(request(8), generation=True), ('materialize',)),
    (b'x' * (deliver.MAX_INPUT + 1), ('materialize',)),
    (request(8), ('materialize', 'extra')),
    (request(8), ('sync',)),
])
def test_invalid_requests_are_refused_without_effect_or_secret(world, value, argv):
    world.install(7)
    before = world.snapshot()
    code, result = run(value, argv)
    assert code == 3 and result['status'] == 'refused'
    assert result['reason'] in {'invalid_request', 'invalid_arguments'}
    assert world.snapshot() == before


def test_waits_for_the_shared_provisioning_lock_and_times_out(world, monkeypatch):
    world.install(7)
    monkeypatch.setattr(deliver, 'LOCK_TIMEOUT_S', 0.3)
    entered, release = threading.Event(), threading.Event()
    def hold():
        with p.lock():
            entered.set()
            release.wait(10)
    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(5)
        before = world.snapshot()
        assert run(request(8)) == (3, {'status': 'refused', 'reason': 'lock_timeout'})
        assert world.snapshot() == before
    finally:
        release.set()
        holder.join(5)
    assert run(request(8))[0] == 0


def test_actual_daemon_boot_line_runs_the_helper_isolated(world):
    """The exact HELPER_BOOT the Hub2 daemon uses, with -I -S, material on stdin."""
    world.install(7)
    source = (REPO / 'hub2/host/agent_hub_host/credential_delivery.py').read_text()
    boot = next(ast.literal_eval(node.value) for node in ast.parse(source).body
                if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == 'HELPER_BOOT')
    env = {key: value for key, value in os.environ.items() if not key.startswith(('PYTHON', 'HUB'))}
    env.update(world.env)
    result = subprocess.run([sys.executable, '-I', '-S', '-c', boot, str(REPO / 'host'), 'materialize'],
                            input=json.dumps(request(8)).encode(), capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout) == {'status': 'materialized', 'generation': 8, 'revision': REV}
    assert SECRET.encode() not in result.stdout + result.stderr
    assert world.held() == credential(8)
