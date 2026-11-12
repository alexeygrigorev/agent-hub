"""Managed Claude generation delivery into the actual maintained managed home.

Synthetic only: a temp HOME, managed-engines root, aplexer profile and a real
zipapp release archive; the real provisioning lock, receipt, exact profile
readiness, scoped writer, receipt publication and offline launch fallback. No
network, service, provider, live credential or live profile is touched.
"""
import ast
import contextlib
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
import zipfile
from unittest.mock import patch

import pytest

from agent_hub_host import credentials, managed_claude_deliver as deliver, provisioning as p

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='managed Claude delivery is Linux only')

REPO = Path(__file__).resolve().parents[2]
REV = '33333333-3333-4333-8333-333333333333'
OTHER_REV = '11111111-1111-4111-8111-111111111111'
PATH = '.claude/.credentials.json'
SECRET = 'synthetic-credential-marker'
EXPIRES = int(time.time() * 1000) + 86400000
BOOT_PREFIX = 'import sys; sys.path.insert(0, sys.argv[1]); '


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


def build_archive(path):
    """A real zipapp carrying this checkout's CLI package, like hub.pyz."""
    with zipfile.ZipFile(path, 'w') as archive:
        for source in sorted((REPO / 'host/agent_hub_host').rglob('*.py')):
            archive.write(source, str(source.relative_to(REPO / 'host')))
    return path


def daemon_boot():
    """The exact HELPER_BOOT line the Hub2 daemon uses."""
    source = (REPO / 'hub2/host/agent_hub_host/credential_delivery.py').read_text()
    return next(ast.literal_eval(node.value) for node in ast.parse(source).body
                if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == 'HELPER_BOOT')


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
        (tmp_path / 'release').mkdir()
        self.archive = str(build_archive(tmp_path / 'release' / 'hub.pyz'))

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
            with patch.object(sys, 'argv', [self.archive]):
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

    def offline_launch(self):
        """The maintained exec fallback with Hub unreachable."""
        def unreachable(*args, **kwargs):
            raise OSError('hub unreachable')
        receipt = p.load_bundle(self.dest)
        return p.refresh_launch_credential('hub-claude', receipt, self.dest / 'home', self.dest, unreachable)

    def run(self, value, argv=None):
        out = io.StringIO()
        stdin = io.BytesIO(json.dumps(value).encode() if not isinstance(value, bytes) else value)
        with contextlib.redirect_stdout(out):
            code = deliver.main(list(argv) if argv is not None else [self.archive, 'materialize'], stdin=stdin)
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
    assert world.run(request(8)) == (0, {'status': 'materialized', 'generation': 8, 'revision': REV})
    assert world.held() == credential(8)
    assert world.state() == {'generation': 8, 'digests': {PATH: hashlib.sha256(credential(8).encode()).hexdigest()}}
    receipt = p.load_bundle(world.dest)
    assert receipt['credential_generation'] == 8 and receipt['files'] == {PATH: credential(8)}
    after = world.snapshot()
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    assert changed <= {str(world.dest / 'home' / PATH), str(world.dest / credentials.STATE_FILE),
                       str(world.dest / 'bundle.json'), str(world.dest / p.CREDENTIALS_FILE)}
    assert str(world.profile) not in changed and str(world.managed / 'state.json') not in changed


def test_same_generation_is_in_step_and_idempotent(world):
    world.install(7)
    world.run(request(8))
    snapshot = world.snapshot()
    assert world.run(request(8)) == (0, {'status': 'in_step', 'generation': 8, 'revision': REV})
    assert world.snapshot() == snapshot


def test_offline_fallback_after_delivery_keeps_the_delivered_generation(world):
    world.install(7)
    world.run(request(8))
    world.offline_launch()
    assert world.held() == credential(8) and world.state()['generation'] == 8


def test_receipt_is_published_before_the_home(world):
    world.install(7)
    order = []
    real_save, real_materialize = p.save_bundle, credentials.materialize_current
    def save(*args, **kwargs):
        order.append('receipt')
        return real_save(*args, **kwargs)
    def materialize(*args, **kwargs):
        order.append('check' if kwargs.get('check_only') else 'home')
        return real_materialize(*args, **kwargs)
    with patch.object(p, 'save_bundle', save), patch.object(credentials, 'materialize_current', materialize):
        assert world.run(request(8))[0] == 0
    assert order == ['check', 'receipt', 'home']


def test_fault_between_receipt_and_home_never_lets_offline_fallback_revert(world):
    """Deliver G8, then fail G9 after receipt publication but before the home write."""
    world.install(7)
    assert world.run(request(8))[0] == 0
    real = credentials.materialize_current
    def stopped(*args, **kwargs):
        if kwargs.get('check_only'):
            return real(*args, **kwargs)
        raise OSError('killed between receipt and home')
    with patch.object(credentials, 'materialize_current', stopped):
        assert world.run(request(9)) == (3, {'status': 'refused', 'reason': 'generation_refused'})
    assert world.held() == credential(8)  # home untouched; no ACK is possible
    world.offline_launch()
    # The fallback moves forward to the receipt's generation, never below 8.
    assert world.held() == credential(9) and world.state()['generation'] == 9
    assert world.run(request(9)) == (0, {'status': 'in_step', 'generation': 9, 'revision': REV})


def test_receipt_ahead_of_state_after_fault_is_never_moved_backwards(world):
    """After a G9 receipt-then-fault, a stale G8 or a changed G9 must not rewrite it."""
    world.install(7)
    assert world.run(request(8))[0] == 0
    real = credentials.materialize_current
    def stopped(*args, **kwargs):
        if kwargs.get('check_only'):
            return real(*args, **kwargs)
        raise OSError('killed between receipt and home')
    with patch.object(credentials, 'materialize_current', stopped):
        assert world.run(request(9))[0] == 3
    assert p.load_bundle(world.dest)['credential_generation'] == 9 and world.state()['generation'] == 8
    before = world.snapshot()
    assert world.run(request(8)) == (3, {'status': 'refused', 'reason': 'generation_refused'})
    changed = json.dumps({'claudeAiOauth': {'accessToken': SECRET + '-other', 'expiresAt': 1}})
    assert world.run(request(9, files={PATH: changed})) == (3, {'status': 'refused', 'reason': 'generation_refused'})
    assert world.snapshot() == before
    assert world.run(request(9)) == (0, {'status': 'materialized', 'generation': 9, 'revision': REV})
    assert world.held() == credential(9) and world.state()['generation'] == 9


def test_fault_inside_receipt_publication_never_reverts_the_home(world):
    """A failure after the credential store but before bundle.json is written."""
    world.install(7)
    assert world.run(request(8))[0] == 0
    real = p.write_json
    def partial(path, value):
        if Path(path).name == 'bundle.json':
            raise OSError('killed inside receipt publication')
        return real(path, value)
    with patch.object(p, 'write_json', partial):
        assert world.run(request(9)) == (3, {'status': 'refused', 'reason': 'receipt_publication_failed'})
    assert world.held() == credential(8)
    world.offline_launch()
    assert world.held() in (credential(8), credential(9))
    assert world.held() != credential(7)
    code, result = world.run(request(9))
    assert code == 0 and result['status'] in ('in_step', 'materialized') and result['generation'] == 9
    assert world.held() == credential(9) and world.state()['generation'] == 9


def test_refusal_before_any_write_leaves_receipt_and_home(world):
    world.install(7)
    world.run(request(8))
    before = world.snapshot()
    assert world.run(request(7)) == (3, {'status': 'refused', 'reason': 'generation_refused'})
    assert world.snapshot() == before
    world.offline_launch()
    assert world.held() == credential(8)


@pytest.mark.parametrize('profile,state', [(False, True), (True, False)])
def test_absent_consumer_writes_nothing(world, profile, state):
    world.install(7, profile=profile, state=state)
    before = world.snapshot()
    assert world.run(request(8)) == (0, {'status': 'absent'})
    assert world.snapshot() == before


def test_nothing_installed_is_absent(world):
    assert world.run(request(8)) == (0, {'status': 'absent'})


def test_foreign_marked_command_is_refused_without_effect(world):
    world.install(7)
    text = world.profile.read_text()
    marked = text.split(p.BEGIN, 1)[1].split(p.END, 1)[0]
    foreign = marked.replace(world.archive, world.archive + '.other')
    assert foreign != marked
    world.profile.write_text(text.replace(marked, foreign))
    before = world.snapshot()
    assert world.run(request(8)) == (3, {'status': 'refused', 'reason': 'profile_unverified'})
    assert world.snapshot() == before


def test_profile_bound_to_another_managed_root_is_refused(world, tmp_path):
    world.install(7)
    text = world.profile.read_text()
    world.profile.write_text(text.replace(repr(str(world.managed.resolve())), repr(str(tmp_path / 'elsewhere'))))
    before = world.snapshot()
    assert world.run(request(8)) == (3, {'status': 'refused', 'reason': 'profile_unverified'})
    assert world.snapshot() == before


def test_other_release_archive_is_refused(world, tmp_path):
    world.install(7)
    other = build_archive(tmp_path / 'other.pyz')
    before = world.snapshot()
    assert world.run(request(8), argv=[str(other), 'materialize']) == (
        3, {'status': 'refused', 'reason': 'profile_unverified'})
    assert world.snapshot() == before


def test_profile_change_while_waiting_for_the_lock_is_seen_under_the_lock(world, monkeypatch):
    """Readiness observed before the helper's lock is not trusted."""
    world.install(7)
    text = world.profile.read_text()
    marked = text.split(p.BEGIN, 1)[1].split(p.END, 1)[0]
    entered, release = threading.Event(), threading.Event()
    def hold():
        with p.lock():
            entered.set()
            release.wait(10)
    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(5)
    result = {}
    worker = threading.Thread(target=lambda: result.update(value=world.run(request(8))))
    worker.start()
    time.sleep(0.3)  # the helper is now waiting for the lock
    world.profile.write_text(text.replace(marked, marked.replace(world.archive, world.archive + '.other')))
    before = world.snapshot()
    release.set()
    holder.join(5)
    worker.join(30)
    assert result['value'] == (3, {'status': 'refused', 'reason': 'profile_unverified'})
    assert world.snapshot() == before


def test_older_generation_is_refused(world):
    world.install(7)
    before = world.snapshot()
    assert world.run(request(6)) == (3, {'status': 'refused', 'reason': 'generation_refused'})
    assert world.snapshot() == before


def test_equal_generation_with_changed_material_is_refused(world):
    world.install(7)
    before = world.snapshot()
    changed = json.dumps({'claudeAiOauth': {'accessToken': SECRET + '-other', 'expiresAt': 1}})
    assert world.run(request(7, files={PATH: changed})) == (3, {'status': 'refused', 'reason': 'generation_refused'})
    assert world.snapshot() == before


def test_new_engine_revision_is_left_to_the_maintained_install(world):
    world.install(7)
    before = world.snapshot()
    other = '44444444-4444-4444-8444-444444444444'
    assert world.run(request(8, revision=other)) == (3, {'status': 'refused', 'reason': 'receipt_revision_mismatch'})
    assert world.snapshot() == before


@pytest.mark.parametrize('value,argv', [
    (dict(request(8), label='test-account'), None),
    (dict(request(8), files={PATH: credential(8), '.claude/settings.json': '{}'}), None),
    (dict(request(8), extra=True), None),
    (dict(request(8), generation=True), None),
    (b'x' * (deliver.MAX_INPUT + 1), None),
    (request(8), ['materialize']),
    (request(8), ['relative.pyz', 'materialize']),
    (request(8), ['ARCHIVE', 'sync']),
])
def test_invalid_requests_are_refused_without_effect_or_secret(world, value, argv):
    world.install(7)
    if argv is not None:
        argv = [world.archive if part == 'ARCHIVE' else part for part in argv]
    before = world.snapshot()
    code, result = world.run(value, argv)
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
        assert world.run(request(8)) == (3, {'status': 'refused', 'reason': 'lock_timeout'})
        assert world.snapshot() == before
    finally:
        release.set()
        holder.join(5)
    assert world.run(request(8))[0] == 0


def test_actual_daemon_boot_line_runs_the_helper_from_the_archive(world, tmp_path):
    """The exact daemon boot line, -I -S, archive-only imports, material on stdin."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(('PYTHON', 'HUB'))}
    env.update(world.env)
    world.install(7, profile=False)
    # Generate the profile exactly as code running from this archive does.
    world.profile.parent.mkdir(mode=0o700)
    world.profile.write_text('version = 1\n')
    world.profile.chmod(0o600)
    generate = (BOOT_PREFIX + 'import json; sys.argv[0] = sys.argv[1]; '
                'from agent_hub_host import provisioning as p; p.profiles(json.loads(sys.stdin.read()))')
    subprocess.run([sys.executable, '-I', '-S', '-c', generate, world.archive],
                   input=json.dumps([other_bundle(), target_bundle(7)]).encode(), env=env, check=True, timeout=60)
    result = subprocess.run([sys.executable, '-I', '-S', '-c', daemon_boot(), world.archive, 'materialize'],
                            input=json.dumps(request(8)).encode(), capture_output=True, env=env, timeout=60,
                            cwd=str(tmp_path))
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout) == {'status': 'materialized', 'generation': 8, 'revision': REV}
    assert SECRET.encode() not in result.stdout + result.stderr
    assert world.held() == credential(8)
